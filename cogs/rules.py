"""Rules panel + click-to-verify. Hybrid."""
import asyncio
import re

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, err, foot, WHITE
from utils.checks import staff_or


async def post_panel(bot, gid) -> dict:
    with db.conn_ctx() as conn:
        panel = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
        panel = dict(panel) if panel else None
    if not panel:
        return None
    guild = bot.get_guild(int(gid))
    ch = guild and guild.get_channel(int(panel['channel_id'])) if panel.get('channel_id') else None
    if not ch:
        return None
    embed = discord.Embed(title=panel.get('title') or 'Rules', description=panel.get('description') or '', color=WHITE)
    embed.set_footer(text=foot())
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=panel.get('label') or 'Verify', style=discord.ButtonStyle.grey,
                                    custom_id=f"verify:{panel.get('message_id') or 'pending'}"))
    if panel.get('message_id'):
        try:
            msg = await ch.fetch_message(int(panel['message_id']))
            view.children[0].custom_id = f'verify:{panel["message_id"]}'
            await msg.edit(embed=embed, view=view)
            return panel
        except Exception:
            pass
    try:
        msg = await ch.send(embed=embed, view=view)
    except Exception:
        return None
    with db.conn_ctx() as conn:
        conn.execute('UPDATE verify_panels SET message_id=? WHERE guild_id=?', (str(msg.id), str(gid)))
    view.children[0].custom_id = f'verify:{msg.id}'
    try:
        await msg.edit(view=view)
    except Exception:
        pass
    panel['message_id'] = str(msg.id)
    return panel


class Rules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('verify:'):
            return
        gid = interaction.guild_id
        with db.conn_ctx() as conn:
            panel = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
        if not panel or not panel['role_id']:
            return await interaction.response.send_message(t(gid, 'vfy.none'), ephemeral=True)
        role = interaction.guild.get_role(int(panel['role_id']))
        if not role:
            return await interaction.response.send_message(t(gid, 'vfy.no_role'), ephemeral=True)
        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message(t(gid, 'vfy.have'), ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason='verified')
            except Exception:
                pass
            await interaction.response.send_message(t(gid, 'vfy.done', role=role.name), ephemeral=True)
        await asyncio.sleep(120)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    @commands.group(name='rules', description='Regulamin + weryfka')
    async def rules(self, ctx):
        await ctx.reply('.rules setup / edit / disable', ephemeral=True)

    @rules.command(name='setup', description='Postaw regulamin')
    @staff_or('manage_guild')
    async def setup_p(self, ctx, channel: discord.TextChannel, role: discord.Role,
                      label: str = None, title: str = None, *, text: str):
        gid = ctx.guild.id
        label = label or t(gid, 'rules.def_label')
        title = title or t(gid, 'rules.def_title')
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
            if old and old['message_id']:
                try:
                    ch = ctx.guild.get_channel(int(old['channel_id']))
                    await (await ch.fetch_message(int(old['message_id']))).delete()
                except Exception:
                    pass
            conn.execute('INSERT OR REPLACE INTO verify_panels (guild_id, channel_id, title, description, label, role_id) VALUES (?,?,?,?,?,?)',
                         (str(gid), str(channel.id), title, text, label, str(role.id)))
        if not await post_panel(self.bot, gid):
            return await ctx.reply(t(gid, 'rules.post_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'rules.live', ch=channel.mention, label=label, role=role.mention), ephemeral=True)

    @rules.command(name='edit', description='Edytuj regulamin')
    @staff_or('manage_guild')
    async def edit(self, ctx, text: str = None, title: str = None, label: str = None, role: discord.Role = None):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            panel = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
            if not panel:
                return await ctx.reply(t(gid, 'rules.no_panel'), ephemeral=True)
            conn.execute('''UPDATE verify_panels SET description=COALESCE(?,description), title=COALESCE(?,title),
                label=COALESCE(?,label), role_id=COALESCE(?,role_id) WHERE guild_id=?''',
                         (text, title, label, str(role.id) if role else None, str(gid)))
        await post_panel(self.bot, gid)
        await ctx.reply(t(gid, 'rules.updated'), ephemeral=True)

    @rules.command(name='disable', description='UsuÅ„ regulamin')
    @staff_or('manage_guild')
    async def disable(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            panel = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
            # only delete the message if Babka posted it herself (never touch webhook messages)
            try:
                managed = panel['managed'] if panel and 'managed' in panel.keys() else 1
            except Exception:
                managed = 1
            if panel and panel['message_id'] and managed:
                try:
                    ch = ctx.guild.get_channel(int(panel['channel_id']))
                    await (await ch.fetch_message(int(panel['message_id']))).delete()
                except Exception:
                    pass
            conn.execute('DELETE FROM verify_panels WHERE guild_id=?', (str(gid),))
        await ctx.reply(t(gid, 'rules.gone'), ephemeral=True)
    @rules.command(name='attach', description='Doklej weryfkÄ™ do swojej wiadomoÅ›ci')
    @staff_or('manage_guild')
    async def attach(self, ctx, message_link: str = None, role: str = None, label: str = None):
        from utils.wizard import parse_link, parse_role
        gid = ctx.guild.id
        if message_link and role:
            r = parse_role(ctx.guild, role)
            if not r:
                return await ctx.reply(t(gid, 'wiz.role_bad'), ephemeral=True)
            return await self._do_attach(ctx, message_link, r, label or t(gid, 'rules.def_label'))
        if ctx.interaction is None:
            return await ctx.reply(t(gid, 'wiz.args_needed',
                                     use='.rules attach <message-link> <@role> [label]'), ephemeral=True)
        await ctx.interaction.response.send_modal(_AttachModal(gid))

    async def _do_attach(self, ctx_or_ix, message_link: str, role: discord.Role, label: str, followup=None):
        from utils.wizard import parse_link
        guild = ctx_or_ix.guild
        gid = guild.id

        async def say(content: str):
            if followup is not None:
                try:
                    await followup.send(content, ephemeral=True)
                except Exception:
                    pass
            elif isinstance(ctx_or_ix, discord.Interaction):
                try:
                    await ctx_or_ix.response.send_message(content, ephemeral=True, delete_after=15)
                except Exception:
                    pass
            else:
                try:
                    await ctx_or_ix.reply(content, ephemeral=True)
                except Exception:
                    pass

        ch_id, msg_id = parse_link(message_link)
        ch = guild.get_channel(ch_id) if ch_id else None
        if not ch:
            return await say(t(gid, 'rules.bad_link'))
        try:
            msg = await ch.fetch_message(msg_id)
        except Exception:
            return await say(t(gid, 'rules.bad_link'))
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.grey,
                                        custom_id=f'verify:{msg.id}'))
        try:
            await msg.edit(view=view)
        except Exception:
            return await say(t(gid, 'rules.bad_link'))
        with db.conn_ctx() as conn:
            conn.execute('''INSERT OR REPLACE INTO verify_panels
                (guild_id, channel_id, message_id, title, description, label, role_id, managed)
                VALUES (?,?,?,?,?,?,?,0)''',
                         (str(gid), str(ch.id), str(msg.id), msg.embeds[0].title if msg.embeds else '',
                          '', label, str(role.id)))
        return await say(t(gid, 'rules.attached', ch=ch.mention, role=role.mention))


async def setup(bot):
    await bot.add_cog(Rules(bot))


class _AttachModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'rules.attach_title'))
        self.msg_in = discord.ui.TextInput(label=t(gid, 'sr.q_msg'), max_length=120)
        self.role_in = discord.ui.TextInput(label=t(gid, 'sr.q_role'), max_length=100)
        self.label_in = discord.ui.TextInput(label=t(gid, 'rules.attach_label'), required=False, max_length=80)
        for i in (self.msg_in, self.role_in, self.label_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        from utils.wizard import parse_role
        gid = interaction.guild_id
        role = parse_role(interaction.guild, self.role_in.value)
        if not role:
            return await interaction.response.send_message(t(gid, 'wiz.role_bad'), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        cog = interaction.client.get_cog('Rules')
        await cog._do_attach(interaction, self.msg_in.value, role,
                             self.label_in.value.strip() or t(gid, 'rules.def_label'),
                             followup=interaction.followup)
        await asyncio.sleep(15)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

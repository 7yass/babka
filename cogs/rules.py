"""Rules panel + click-to-verify. Hybrid.

The panel view is built and registered by services.persistent, so the
button keeps working after a restart (the role is read from the DB on
every click, never from a closure).
"""
import asyncio
import re

import discord
from discord.ext import commands

import database as db
from lang import t
from services import persistent
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
    # The custom_id is derived from the guild, so it is known before the
    # message exists and the panel can be rebuilt after a restart.
    view = persistent.build_rules_view(gid)
    if panel.get('message_id'):
        try:
            msg = await ch.fetch_message(int(panel['message_id']))
            await msg.edit(embed=embed, view=view)
            persistent.track(view, bot)
            return panel
        except Exception:
            pass
    try:
        msg = await ch.send(embed=embed, view=view)
    except Exception:
        return None
    with db.conn_ctx() as conn:
        conn.execute('UPDATE verify_panels SET message_id=? WHERE guild_id=?', (str(msg.id), str(gid)))
    persistent.track(view, bot)
    panel['message_id'] = str(msg.id)
    return panel


class Rules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # Registered panel views answer their own clicks (services/persistent
        # rebuilds them from the DB on every boot). This is only the safety
        # net for panels the registry does not know about.
        await persistent.handle_fallback(interaction)

    @commands.group(name='rules', description='Regulamin + weryfka')
    async def rules(self, ctx):
        await ctx.reply('.rules setup / edit / disable', ephemeral=True)

    @rules.command(name='setup', description='Postaw regulamin')
    @staff_or('manage_guild')
    async def setup_p(self, ctx, channel: discord.TextChannel, role: discord.Role,
                      label: str = None, title: str = None, *, text: str):
        gid = ctx.guild.id
        label = label or t(gid, 'pv.rules.def_label')
        title = title or t(gid, 'pv.rules.def_title')
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
            return await ctx.reply(t(gid, 'pv.rules.post_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'pv.rules.live', ch=channel.mention, label=label, role=role.mention), ephemeral=True)

    @rules.command(name='edit', description='Edytuj regulamin')
    @staff_or('manage_guild')
    async def edit(self, ctx, text: str = None, title: str = None, label: str = None, role: discord.Role = None):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            panel = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
            if not panel:
                return await ctx.reply(t(gid, 'pv.rules.no_panel'), ephemeral=True)
            conn.execute('''UPDATE verify_panels SET description=COALESCE(?,description), title=COALESCE(?,title),
                label=COALESCE(?,label), role_id=COALESCE(?,role_id) WHERE guild_id=?''',
                         (text, title, label, str(role.id) if role else None, str(gid)))
        await post_panel(self.bot, gid)
        await ctx.reply(t(gid, 'pv.rules.updated'), ephemeral=True)

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
        await ctx.reply(t(gid, 'pv.rules.gone'), ephemeral=True)
    @rules.command(name='attach', description='Doklej weryfkÄ™ do swojej wiadomoÅ›ci')
    @staff_or('manage_guild')
    async def attach(self, ctx, message_link: str = None, role: str = None, label: str = None):
        from utils.wizard import parse_link, parse_role
        gid = ctx.guild.id
        if message_link and role:
            r = parse_role(ctx.guild, role)
            if not r:
                return await ctx.reply(t(gid, 'pv.wiz.role_bad'), ephemeral=True)
            return await self._do_attach(ctx, message_link, r, label or t(gid, 'pv.rules.def_label'))
        if ctx.interaction is None:
            return await ctx.reply(t(gid, 'pv.wiz.args_needed',
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
            return await say(t(gid, 'pv.rules.bad_link'))
        try:
            msg = await ch.fetch_message(msg_id)
        except Exception:
            return await say(t(gid, 'pv.rules.bad_link'))
        # The button id is derived from the guild, so the same panel always
        # registers under the same id (and the panel can be rebuilt on boot).
        view = persistent.build_rules_view(gid, label=label)
        try:
            await msg.edit(view=view)
        except Exception:
            return await say(t(gid, 'pv.rules.bad_link'))
        with db.conn_ctx() as conn:
            conn.execute('''INSERT OR REPLACE INTO verify_panels
                (guild_id, channel_id, message_id, title, description, label, role_id, managed)
                VALUES (?,?,?,?,?,?,?,0)''',
                         (str(gid), str(ch.id), str(msg.id), msg.embeds[0].title if msg.embeds else '',
                          '', label, str(role.id)))
        persistent.track(view, self.bot)
        return await say(t(gid, 'pv.rules.attached', ch=ch.mention, role=role.mention))


async def setup(bot):
    await bot.add_cog(Rules(bot))


class _AttachModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'pv.rules.attach_title'))
        self.msg_in = discord.ui.TextInput(label=t(gid, 'pv.sr.q_msg'), max_length=120)
        self.role_in = discord.ui.TextInput(label=t(gid, 'pv.sr.q_role'), max_length=100)
        self.label_in = discord.ui.TextInput(label=t(gid, 'pv.rules.attach_label'), required=False, max_length=80)
        for i in (self.msg_in, self.role_in, self.label_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        from utils.wizard import parse_role
        gid = interaction.guild_id
        role = parse_role(interaction.guild, self.role_in.value)
        if not role:
            return await interaction.response.send_message(t(gid, 'pv.wiz.role_bad'), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        cog = interaction.client.get_cog('Rules')
        await cog._do_attach(interaction, self.msg_in.value, role,
                             self.label_in.value.strip() or t(gid, 'pv.rules.def_label'),
                             followup=interaction.followup)
        await asyncio.sleep(15)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

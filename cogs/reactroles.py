"""True reaction roles: react to get the role. Hybrid setup."""
import asyncio

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import WHITE
from utils.checks import staff_or


def _emoji_key(payload) -> str:
    e = payload.emoji
    return f'{e.name}:{e.id}' if e.is_custom_emoji() else e.name


class ReactRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or (payload.member and payload.member.bot):
            return
        await self._toggle(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        await self._toggle(payload, False)

    async def _toggle(self, payload, add: bool):
        key = _emoji_key(payload)
        with db.conn_ctx() as conn:
            row = conn.execute('''SELECT rr.role_id FROM reactroles rr
                JOIN reactpanels rp ON rp.id = rr.panel_id
                WHERE rp.guild_id=? AND rp.message_id=? AND rr.emoji=?''',
                               (str(payload.guild_id), str(payload.message_id), key)).fetchone()
        if not row:
            return
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(int(row['role_id'])) if guild else None
        member = guild.get_member(payload.user_id) if guild else None
        if not role or not member or member.bot:
            return
        try:
            if add and role not in member.roles:
                await member.add_roles(role, reason='reaction role')
            elif not add and role in member.roles:
                await member.remove_roles(role, reason='reaction role')
        except Exception:
            pass

    @commands.hybrid_group(name='reactionroles', description='Role za reakcje')
    async def rr(self, ctx):
        await ctx.reply('/reactionroles setup / add / list / delete', ephemeral=True)

    @rr.command(name='setup', description='Postaw panel')
    @staff_or('manage_roles')
    async def setup_p(self, ctx, channel: discord.TextChannel, title: str, *, description: str = ''):
        with db.conn_ctx() as conn:
            cur = conn.execute('INSERT INTO reactpanels (guild_id, channel_id, title, description) VALUES (?,?,?,?)',
                               (str(ctx.guild.id), str(channel.id), title, description))
            pid = cur.lastrowid
        try:
            msg = await channel.send(embed=discord.Embed(title=title, description=description or '​', color=WHITE))
        except Exception:
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM reactpanels WHERE id=?', (pid,))
            return await ctx.reply(t(ctx.guild.id, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE reactpanels SET message_id=? WHERE id=?', (str(msg.id), pid))
        await ctx.reply(t(ctx.guild.id, 'rr2.live', id=pid, ch=channel.mention), ephemeral=True)

    @rr.command(name='add', description='Podepnij emoji do roli')
    @staff_or('manage_roles')
    async def add(self, ctx, panel: int, emoji: str, role: discord.Role):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM reactpanels WHERE id=? AND guild_id=?', (panel, str(gid))).fetchone()
            if not prow or not prow['message_id']:
                return await ctx.reply(t(gid, 'rr2.no_panel'), ephemeral=True)
        ch = ctx.guild.get_channel(int(prow['channel_id']))
        try:
            msg = await ch.fetch_message(int(prow['message_id']))
            await msg.add_reaction(emoji)
        except Exception:
            return await ctx.reply(t(gid, 'rr2.bad_emoji'), ephemeral=True)
        # normalize key: custom emoji -> name:id
        key = emoji
        import re
        m = re.match(r'<a?:(\w+):(\d+)>', emoji)
        if m:
            key = f'{m.group(1)}:{m.group(2)}'
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO reactroles (panel_id, emoji, role_id) VALUES (?,?,?)',
                         (panel, key, str(role.id)))
        await ctx.reply(t(gid, 'rr2.added', emoji=emoji, role=role.mention), ephemeral=True)

    @rr.command(name='list', description='Lista paneli')
    async def list_p(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            panels = conn.execute('SELECT * FROM reactpanels WHERE guild_id=?', (str(gid),)).fetchall()
            if not panels:
                return await ctx.reply(t(gid, 'rr2.list_empty'), ephemeral=True)
            lines = []
            for p in panels:
                pairs = conn.execute('SELECT emoji, role_id FROM reactroles WHERE panel_id=?', (p['id'],)).fetchall()
                show = []
                for r in pairs:
                    if ':' in r['emoji']:
                        name, eid = r['emoji'].split(':')
                        show.append(f'<:{name}:{eid}> → <@&{r["role_id"]}>')
                    else:
                        show.append(f"{r['emoji']} → <@&{r['role_id']}>")
                lines.append(f"**#{p['id']}** {p['title']} → <#{p['channel_id']}>\n" + ('\n'.join(show) or '(empty)'))
        await ctx.reply(t(gid, 'rr2.panels') + '\n\n' + '\n\n'.join(lines), ephemeral=True)

    @rr.command(name='delete', description='Usuń panel')
    @staff_or('manage_roles')
    async def delete(self, ctx, panel: int):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM reactpanels WHERE id=? AND guild_id=?', (panel, str(gid))).fetchone()
            if prow and prow['message_id']:
                try:
                    ch = ctx.guild.get_channel(int(prow['channel_id']))
                    await (await ch.fetch_message(int(prow['message_id']))).delete()
                except Exception:
                    pass
            conn.execute('DELETE FROM reactroles WHERE panel_id=?', (panel,))
            conn.execute('DELETE FROM reactpanels WHERE id=? AND guild_id=?', (panel, str(gid)))
        await ctx.reply(t(gid, 'rr2.deleted'), ephemeral=True)

    @rr.command(name='quick', description='Szybko: link + emoji + rola')
    @staff_or('manage_roles')
    async def quick(self, ctx, message_link: str = None, emoji: str = None, role: str = None):
        import re
        from utils.wizard import parse_link, parse_role
        gid = ctx.guild.id
        if message_link and emoji and role:
            r = parse_role(ctx.guild, role)
            if not r:
                return await ctx.reply(t(gid, 'wiz.role_bad'), ephemeral=True)
            return await self._do_quick(ctx, message_link, emoji, r)
        if ctx.interaction is None:
            return await ctx.reply(t(gid, 'wiz.args_needed',
                                     use='.reactionroles quick <link> <emoji> <@role>'), ephemeral=True)
        await ctx.interaction.response.send_modal(_QuickModal(gid))

    async def _do_quick(self, ctx_or_interaction, message_link: str, emoji: str, role: discord.Role,
                        followup=None):
        import re
        from utils.wizard import parse_link
        guild = ctx_or_interaction.guild
        gid = guild.id

        async def say(content: str):
            if followup is not None:
                try:
                    await followup.send(content, ephemeral=True)
                except Exception:
                    pass
            else:
                await _reply(ctx_or_interaction, content)

        ch_id, msg_id = parse_link(message_link)
        if not ch_id:
            return await say(t(gid, 'rules.bad_link'))
        ch = guild.get_channel(ch_id)
        if not ch:
            return await say(t(gid, 'rules.bad_link'))
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.add_reaction(emoji)
        except Exception:
            return await say(t(gid, 'rr2.bad_emoji'))
        key = emoji
        mm = re.match(r'<a?:(\w+):(\d+)>', emoji)
        if mm:
            key = f'{mm.group(1)}:{mm.group(2)}'
        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM reactpanels WHERE message_id=? AND guild_id=?',
                                (str(msg.id), str(gid))).fetchone()
            if prow:
                pid = prow['id']
            else:
                cur = conn.execute('INSERT INTO reactpanels (guild_id, channel_id, message_id, title, description) VALUES (?,?,?,?,?)',
                                   (str(gid), str(ch.id), str(msg.id), 'Roles', ''))
                pid = cur.lastrowid
            conn.execute('INSERT INTO reactroles (panel_id, emoji, role_id) VALUES (?,?,?)',
                         (pid, key, str(role.id)))
        return await say(t(gid, 'rr2.quick_ok', emoji=emoji, role=role.mention))


async def _reply(target, content: str):
    """Ephemeral auto-deleting reply for both ctx and modal interactions."""
    try:
        if isinstance(target, discord.Interaction):
            await target.response.send_message(content, ephemeral=True, delete_after=15)
        else:
            await target.reply(content, ephemeral=True)
    except Exception:
        pass


class _QuickModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'rr2.quick_title'))
        self.msg_in = discord.ui.TextInput(label=t(gid, 'sr.q_msg'), max_length=120)
        self.emoji_in = discord.ui.TextInput(label=t(gid, 'rr2.q_emoji'), max_length=60)
        self.role_in = discord.ui.TextInput(label=t(gid, 'sr.q_role'), max_length=100)
        for i in (self.msg_in, self.emoji_in, self.role_in):
            self.add_item(i)
        self._cog = None

    async def on_submit(self, interaction: discord.Interaction):
        from utils.wizard import parse_role
        gid = interaction.guild_id
        role = parse_role(interaction.guild, self.role_in.value)
        if not role:
            return await interaction.response.send_message(t(gid, 'wiz.role_bad'), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        cog = interaction.client.get_cog('ReactRoles')
        await cog._do_quick(interaction, self.msg_in.value, self.emoji_in.value, role,
                            followup=interaction.followup)
        await asyncio.sleep(15)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(ReactRoles(bot))

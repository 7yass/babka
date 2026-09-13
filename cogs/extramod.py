"""Extended moderation pack: mutes, image/reaction mutes, permkicks, jail,
history, roles, cleanup, forcenick, blind, drag, lists. All hybrid."""
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, ok, WHITE
from utils.checks import staff_or
from utils.common import log_to_mod, parse_duration, fmt_duration


def mute_default(gid) -> int:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT mute_duration FROM modcfg WHERE guild_id=?', (str(gid),)).fetchone()
        if not row:
            conn.execute('INSERT INTO modcfg (guild_id) VALUES (?)', (str(gid),))
            return 600
        return row['mute_duration'] or 600


class ExtraMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- forcenick enforcement + permkick re-kick ----------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick == after.nick:
            return
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT nick FROM forcenick WHERE guild_id=? AND user_id=?',
                               (str(after.guild.id), str(after.id))).fetchone()
        if row and after.nick != row['nick']:
            try:
                await after.edit(nick=row['nick'], reason='forcenick')
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT 1 FROM permkicks WHERE guild_id=? AND user_id=?',
                               (str(member.guild.id), str(member.id))).fetchone()
        if row:
            try:
                await member.kick(reason='permkick rejoin')
            except Exception:
                pass

    # ---------- mute family ----------
    @commands.command(name='mute', description='Knebel z domyÅ›lnym czasem')
    @staff_or('moderate_members')
    async def mute(self, ctx, member: discord.Member, duration: str = None, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        secs = parse_duration(duration) if duration else mute_default(gid)
        if not secs:
            secs = mute_default(gid)
        try:
            await member.timeout(min(secs, 2419200), reason=reason)
        except Exception:
            return await ctx.reply(t(gid, 'mod.no_member'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.mute_dur', name=member.display_name, dur=fmt_duration(secs), reason=reason),
                        ephemeral=True)

    @commands.command(name='muteduration', description='DomyÅ›lna dÅ‚ugoÅ›Ä‡ knebla')
    @staff_or('manage_guild')
    async def muteduration(self, ctx, duration: str):
        gid = ctx.guild.id
        secs = parse_duration(duration)
        if not secs or secs < 60:
            return await ctx.reply(t(gid, 'mod.temp_use'), ephemeral=True)
        mute_default(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE modcfg SET mute_duration=? WHERE guild_id=?', (secs, str(gid)))
        await ctx.reply(t(gid, 'xm.mutedur_set', dur=fmt_duration(secs)), ephemeral=True)

    @commands.command(name='unmuteall', description='Zdejmij wszystkie kneble')
    @staff_or('moderate_members')
    async def unmuteall(self, ctx):
        now = discord.utils.utcnow()
        n = 0
        for m in ctx.guild.members:
            if m.timed_out_until and m.timed_out_until > now:
                try:
                    await m.timeout(None)
                    n += 1
                except Exception:
                    pass
        await ctx.reply(t(ctx.guild.id, 'xm.unmuted_all', n=n), ephemeral=True)

    @commands.command(name='mutelist', description='Kto siedzi na kneblu')
    @staff_or('moderate_members')
    async def mutelist(self, ctx):
        gid = ctx.guild.id
        now = discord.utils.utcnow()
        rows = [f'• {m.mention} — <t:{int(m.timed_out_until.timestamp())}:R>'
                for m in ctx.guild.members if m.timed_out_until and m.timed_out_until > now]
        if not rows:
            return await ctx.reply(t(gid, 'xm.mutelist_empty'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.mutelist_title') + '\n' + '\n'.join(rows[:25]), ephemeral=True)

    # ---------- image / reaction mutes (current channel) ----------
    @commands.command(name='imute', description='Blokada obrazków dla typa')
    @staff_or('manage_messages')
    async def imute(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        try:
            await ctx.channel.set_permissions(member, attach_files=False, embed_links=False, reason=reason)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.imuted', user=member.mention, ch=ctx.channel.mention), ephemeral=True)

    @commands.command(name='iunmute', description='Odblokuj obrazki')
    @staff_or('manage_messages')
    async def iunmute(self, ctx, member: discord.Member):
        try:
            await ctx.channel.set_permissions(member, attach_files=None, embed_links=None)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.iunmuted', user=member.mention, ch=ctx.channel.mention), ephemeral=True)

    @commands.command(name='imutelist', description='Kto bez obrazków')
    @staff_or('manage_messages')
    async def imutelist(self, ctx):
        found = set()
        for ch in ctx.guild.text_channels:
            for target, ow in ch.overwrites.items():
                if isinstance(target, discord.Member) and ow.attach_files is False:
                    found.add(target.mention)
        await ctx.reply(embed=ok((t(ctx.guild.id, 'xm.imutelist_title') + '\n' + '\n'.join(sorted(found)[:25]))
                        if found else t(ctx.guild.id, 'xm.imutelist_empty')), ephemeral=True)

    @commands.command(name='rmute', description='Blokada reakcji dla typa')
    @staff_or('manage_messages')
    async def rmute(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        try:
            await ctx.channel.set_permissions(member, add_reactions=False, reason=reason)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.rmuted', user=member.mention, ch=ctx.channel.mention), ephemeral=True)

    @commands.command(name='runmute', description='Odblokuj reakcje')
    @staff_or('manage_messages')
    async def runmute(self, ctx, member: discord.Member):
        try:
            await ctx.channel.set_permissions(member, add_reactions=None)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.runmuted', user=member.mention, ch=ctx.channel.mention), ephemeral=True)

    @commands.command(name='rmutelist', description='Kto bez reakcji')
    @staff_or('manage_messages')
    async def rmutelist(self, ctx):
        found = set()
        for ch in ctx.guild.text_channels:
            for target, ow in ch.overwrites.items():
                if isinstance(target, discord.Member) and ow.add_reactions is False:
                    found.add(target.mention)
        await ctx.reply(embed=ok((t(ctx.guild.id, 'xm.rmutelist_title') + '\n' + '\n'.join(sorted(found)[:25]))
                        if found else t(ctx.guild.id, 'xm.imutelist_empty')), ephemeral=True)

    # ---------- permkick ----------
    @commands.command(name='permkick', description='Kick z zakazem powrotu')
    @staff_or('kick_members')
    async def permkick(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO permkicks (guild_id, user_id, reason) VALUES (?,?,?)',
                         (str(gid), str(member.id), reason))
        try:
            await member.kick(reason=f'[permkick] {reason}')
        except Exception:
            pass
        await ctx.reply(t(gid, 'xm.pkicked', user=member.mention), ephemeral=True)

    @commands.command(name='permkicked', description='Lista permkicków')
    @staff_or('kick_members')
    async def permkicked(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM permkicks WHERE guild_id=?', (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'xm.imutelist_empty'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.pkicked_list') + '\n' +
                        '\n'.join(f"• <@{r['user_id']}> — {r['reason']}" for r in rows[:25]), ephemeral=True)

    @commands.command(name='unpermkick', description='Zdejmij permkicka (albo "all")')
    @staff_or('kick_members')
    async def unpermkick(self, ctx, target: str):
        gid = ctx.guild.id
        if target.lower() == 'all':
            with db.conn_ctx() as conn:
                n = conn.execute('DELETE FROM permkicks WHERE guild_id=?', (str(gid),)).rowcount
            return await ctx.reply(t(gid, 'xm.pun_all', n=n or 0), ephemeral=True)
        uid = target.strip('<@!>')
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM permkicks WHERE guild_id=? AND user_id=?', (str(gid), uid))
        await ctx.reply(t(gid, 'xm.pun', user=f'<@{uid}>'), ephemeral=True)

    # ---------- bans ----------
    @commands.command(name='bans', description='Bany serwera')
    @staff_or('ban_members')
    async def bans(self, ctx):
        gid = ctx.guild.id
        try:
            entries = [e async for e in ctx.guild.bans(limit=100)]
        except Exception:
            entries = []
        if not entries:
            return await ctx.reply(t(gid, 'xm.bans_empty'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.bans_title') + '\n' +
                        '\n'.join(f'• {e.user} (`{e.user.id}`)' for e in entries[:25]), ephemeral=True)

    @commands.command(name='hackban', description='Ban po samym ID')
    @staff_or('ban_members')
    async def hackban(self, ctx, user_id: str, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        try:
            await ctx.guild.ban(discord.Object(id=int(user_id)), reason=f'{ctx.author} — {reason}',
                                delete_message_days=1)
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_ban'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.hacked', id=user_id), ephemeral=True)

    @commands.command(name='unbanall', description='Odbanuj wszystkich')
    @staff_or('administrator')
    async def unbanall(self, ctx):
        await ctx.defer(ephemeral=True)
        try:
            entries = [e async for e in ctx.guild.bans(limit=200)]
        except Exception:
            entries = []
        n = 0
        for e in entries:
            try:
                await ctx.guild.unban(e.user, reason=f'unbanall by {ctx.author}')
                n += 1
            except Exception:
                pass
        await ctx.reply(embed=ok(t(ctx.guild.id, 'xm.unbanned_all', n=n)))

    # ---------- warns extras ----------
    @commands.command(name='unwarn', description='Zdejmij ostatniego warna')
    @staff_or('manage_messages')
    async def unwarn(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT id FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1',
                               (str(gid), str(member.id))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'mod.no_warns'), ephemeral=True)
            conn.execute('DELETE FROM warns WHERE id=?', (row['id'],))
        await ctx.reply(t(gid, 'xm.unwarned', user=member.mention), ephemeral=True)

    @commands.command(name='reason', description='Popraw powód warna')
    @staff_or('manage_messages')
    async def reason(self, ctx, member: discord.Member, *, text: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT id FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1',
                               (str(gid), str(member.id))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'mod.no_warns'), ephemeral=True)
            conn.execute('UPDATE warns SET reason=?, mod_id=? WHERE id=?', (text, str(ctx.author.id), row['id']))
        await ctx.reply(t(gid, 'xm.reason_set', user=member.mention), ephemeral=True)

    @commands.command(name='history', description='Kartoteka + knebel')
    @staff_or('manage_messages')
    async def history(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10',
                                (str(gid), str(member.id))).fetchall()
        lines = [f"**{i + 1}.** {r['reason']} — <t:{r['created_at']}:R>" for i, r in enumerate(rows)]
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            lines.append(t(gid, 'xm.hist_timeout', until=f'<t:{int(member.timed_out_until.timestamp())}:R>'))
        if not lines:
            lines = [t(gid, 'xm.hist_clean')]
        await ctx.reply(t(gid, 'xm.hist_title', name=member.display_name) + '\n' + '\n'.join(lines), ephemeral=True)

    @commands.command(name='modstats', description='Warny na moda')
    @staff_or('manage_guild')
    async def modstats(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT mod_id, COUNT(*) c FROM warns WHERE guild_id=? GROUP BY mod_id ORDER BY c DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'xm.stats_empty'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.stats_title') + '\n' +
                        '\n'.join(f"• <@{r['mod_id']}> — **{r['c']}**" for r in rows), ephemeral=True)

    @commands.command(name='naughty', description='Najbardziej niegrzeczni')
    @staff_or('manage_messages')
    async def naughty(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT user_id, COUNT(*) c FROM warns WHERE guild_id=? GROUP BY user_id ORDER BY c DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'xm.stats_empty'), ephemeral=True)
        await ctx.reply(t(gid, 'xm.naughty_title') + '\n' +
                        '\n'.join(f"**{i + 1}.** <@{r['user_id']}> — **{r['c']}**" for i, r in enumerate(rows)),
                        ephemeral=True)

    # ---------- channel visibility ----------
    @commands.command(name='hide', description='Schowaj kanaÅ‚')
    @staff_or('manage_channels')
    async def hide(self, ctx):
        try:
            await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=False)
        except Exception:
            pass
        await ctx.reply(embed=ok(t(ctx.guild.id, 'xm.hidden')))

    @commands.command(name='reveal', description='PokaÅ¼ kanaÅ‚')
    @staff_or('manage_channels')
    async def reveal(self, ctx):
        try:
            await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=None)
        except Exception:
            pass
        await ctx.reply(embed=ok(t(ctx.guild.id, 'xm.visible')))

    @commands.command(name='blind', description='Schowaj kanaÅ‚ przed typem')
    @staff_or('manage_channels')
    async def blind(self, ctx, member: discord.Member):
        try:
            await ctx.channel.set_permissions(member, view_channel=False)
        except Exception:
            pass
        await ctx.reply(embed=ok(t(ctx.guild.id, 'xm.blinded', user=member.mention)))

    @commands.command(name='unblind', description='PokaÅ¼ kanaÅ‚ z powrotem')
    @staff_or('manage_channels')
    async def unblind(self, ctx, member: discord.Member):
        try:
            await ctx.channel.set_permissions(member, view_channel=None)
        except Exception:
            pass
        await ctx.reply(embed=ok(t(ctx.guild.id, 'xm.unblinded', user=member.mention)))

    # ---------- roles ----------
    @commands.command(name='role', description='Daj rolÄ™')
    @staff_or('manage_roles')
    async def role(self, ctx, member: discord.Member, role: discord.Role):
        try:
            await member.add_roles(role, reason=f'by {ctx.author}')
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.roled', user=member.mention, role=role.mention), ephemeral=True)

    @commands.command(name='unrole', description='Zabierz rolÄ™')
    @staff_or('manage_roles')
    async def unrole(self, ctx, member: discord.Member, role: discord.Role):
        try:
            await member.remove_roles(role, reason=f'by {ctx.author}')
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.unroled', user=member.mention, role=role.mention), ephemeral=True)

    @commands.command(name='inrole', description='Kto ma rolÄ™')
    @staff_or('manage_roles')
    async def inrole(self, ctx, *, role: discord.Role):
        members = [m.display_name for m in role.members[:30]]
        await ctx.reply(t(ctx.guild.id, 'xm.inrole_title', role=role.name, n=len(role.members)) +
                        ('\n' + '\n'.join(members) if members else ''), ephemeral=True)

    @commands.command(name='stripstaff', description='Zdejmij wszystkie role')
    @staff_or('administrator')
    async def stripstaff(self, ctx, member: discord.Member):
        kept = []
        for r in list(member.roles[1:]):
            try:
                await member.remove_roles(r, reason=f'strip by {ctx.author}')
            except Exception:
                kept.append(r.name)
        await ctx.reply(t(ctx.guild.id, 'xm.stripped', user=member.mention) +
                        (f" (kept: {', '.join(kept)})" if kept else ''), ephemeral=True)

    # ---------- cleanup / nick / jail / misc ----------
    @commands.command(name='cleanup', description="Wywal moje wiadomoÅ›ci")
    @staff_or('manage_messages')
    async def cleanup(self, ctx, amount: int = 20):
        me = ctx.guild.me or self.bot.user
        deleted = 0

        def is_me(m):
            return m.author.id == (me.id if hasattr(me, 'id') else 0)

        try:
            batch = await ctx.channel.purge(limit=min(max(amount, 1), 100), check=is_me)
            deleted = len(batch)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.cleaned', n=deleted), ephemeral=True)

    @commands.command(name='forcenick', description='Zablokuj nick (albo "off")')
    @staff_or('manage_nicknames')
    async def forcenick(self, ctx, member: discord.Member, *, nick: str = None):
        gid = ctx.guild.id
        if not nick or nick.lower() == 'off':
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM forcenick WHERE guild_id=? AND user_id=?', (str(gid), str(member.id)))
            return await ctx.reply(t(gid, 'xm.fnick_off', user=member.mention), ephemeral=True)
        try:
            await member.edit(nick=nick, reason=f'forcenick by {ctx.author}')
        except Exception:
            pass
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO forcenick (guild_id, user_id, nick) VALUES (?,?,?)',
                         (str(gid), str(member.id), nick))
        await ctx.reply(t(gid, 'xm.fnick_set', user=member.mention, nick=nick), ephemeral=True)

    async def _jail_role(self, guild: discord.Guild):
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT role_id FROM jailcfg WHERE guild_id=?', (str(guild.id),)).fetchone()
            role = guild.get_role(int(row['role_id'])) if row and row['role_id'] else None
            if role:
                return role
            role = await guild.create_role(name='jailed', reason='jail system')
            with db.conn_ctx() as c2:
                c2.execute('INSERT OR REPLACE INTO jailcfg (guild_id, role_id) VALUES (?,?)',
                           (str(guild.id), str(role.id)))
            for ch in list(guild.text_channels) + list(guild.voice_channels):
                try:
                    if isinstance(ch, discord.TextChannel):
                        await ch.set_permissions(role, send_messages=False, add_reactions=False)
                    else:
                        await ch.set_permissions(role, speak=False, connect=False)
                except Exception:
                    pass
            return role

    @commands.command(name='jail', description='WsadÅº typa')
    @staff_or('moderate_members')
    async def jail(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        role = await self._jail_role(ctx.guild)
        try:
            await member.add_roles(role, reason=reason)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.jailed', user=member.mention), ephemeral=True)

    @commands.command(name='unjail', description='WypuÅ›Ä‡ typa')
    @staff_or('moderate_members')
    async def unjail(self, ctx, member: discord.Member):
        role = await self._jail_role(ctx.guild)
        try:
            await member.remove_roles(role, reason=f'by {ctx.author}')
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'xm.unjailed', user=member.mention), ephemeral=True)

    @commands.command(name='jailed', description='Kto siedzi')
    @staff_or('moderate_members')
    async def jailed(self, ctx):
        role = await self._jail_role(ctx.guild)
        names = [m.mention for m in role.members[:25]]
        await ctx.reply((t(ctx.guild.id, 'xm.jailed_list') + '\n' + '\n'.join(names)) if names
                        else t(ctx.guild.id, 'xm.jailed_empty'), ephemeral=True)

    @commands.command(name='drag', description='ZaciÄ…gnij na gÅ‚osówkÄ™')
    @staff_or('move_members')
    async def drag(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.reply(t(gid, 'xm.drag_use'), ephemeral=True)
        if not member.voice or not member.voice.channel:
            return await ctx.reply(t(gid, 'xm.drag_target'), ephemeral=True)
        try:
            await member.move_to(ctx.author.voice.channel, reason=f'drag by {ctx.author}')
        except Exception:
            pass
        await ctx.reply(t(gid, 'xm.dragged', user=member.mention, ch=ctx.author.voice.channel.mention),
                        ephemeral=True)

    @commands.command(name='newusers', description='NajÅ›wieÅ¼si na serwerze')
    @staff_or('manage_guild')
    async def newusers(self, ctx, count: int = 10):
        members = sorted([m for m in ctx.guild.members if m.joined_at],
                         key=lambda m: m.joined_at, reverse=True)[:min(max(count, 1), 20)]
        lines = []
        for m in members:
            age = (discord.utils.utcnow() - m.created_at).days
            lines.append(f'• {m.mention} — joined <t:{int(m.joined_at.timestamp())}:R>, account **{age}d**')
        await ctx.reply(t(ctx.guild.id, 'xm.newusers_title') + '\n' + '\n'.join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(ExtraMod(bot))

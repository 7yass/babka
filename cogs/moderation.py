"""Moderation: warns, timeouts, kick/ban family, purge suite, slowmode, lock. Hybrid."""
import asyncio
import re
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, err, ok, WHITE, RED, DARK_RED
from utils.checks import staff_or
from utils.common import log_to_mod, parse_duration, fmt_duration

CUSTOM_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')
UNICODE_EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u2640-\u2642\u2690-\u2691]', re.UNICODE)
MSG_LINK_RE = re.compile(r'(?:discord(?:app)?\.com/channels/\d+/)?(\d{15,25})(?:/(\d{15,25}))?/?$')


async def _safe_reply(ctx, *args, **kwargs):
    """ctx.reply, falling back to channel.send when the invoking
    message was wiped (purge/clear delete it, breaking the reference)."""
    try:
        return await ctx.reply(*args, **kwargs)
    except Exception:
        pass
    kwargs.pop('ephemeral', None)
    kwargs.pop('mention_author', None)
    try:
        return await ctx.channel.send(*args, **kwargs)
    except Exception:
        return None


async def _find_msg(ctx, ref: str):
    """Resolve a message ID or link to a Message."""
    ref = (ref or '').strip().strip('<>')
    m = MSG_LINK_RE.search(ref)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if b:
        ch = ctx.guild.get_channel(int(a))
        mid = int(b)
    else:
        ch, mid = ctx.channel, int(a)
    if ch is None:
        return None
    try:
        return await ch.fetch_message(mid)
    except Exception:
        return None


async def _wipe(messages, channel) -> int:
    """Bulk-delete recent messages in 100-chunks. Returns count."""
    now = discord.utils.utcnow().timestamp()
    recent = [m for m in messages if now - m.created_at.timestamp() < 13 * 86400]
    deleted = 0
    for i in range(0, len(recent), 100):
        try:
            await channel.delete_messages(recent[i:i + 100])
            deleted += len(recent[i:i + 100])
        except Exception:
            break
    return deleted


async def _scan(channel, cap: int = 500):
    pool, last = [], None
    while len(pool) < cap:
        batch = [m async for m in channel.history(limit=100, before=last)]
        if not batch:
            break
        pool.extend(batch)
        last = batch[-1]
        if len(batch) < 100:
            break
    return pool


async def _purge_log(guild, channel, count: int, author):
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT channel_id FROM purgelogs WHERE guild_id=?', (str(guild.id),)).fetchone()
        if not row or not row['channel_id']:
            return
        ign = conn.execute('SELECT 1 FROM purgeignore WHERE guild_id=? AND channel_id=?',
                           (str(guild.id), str(channel.id))).fetchone()
        if ign:
            return
    dest = guild.get_channel(int(row['channel_id']))
    if not dest:
        return
    try:
        await dest.send(embed=build(
            t(guild.id, 'pg.logged', n=count, ch=channel.mention, user=str(author)),
            title=t(guild.id, 'pg.log_title'), color=WHITE))
    except Exception:
        pass


async def try_dm(user, text: str):
    try:
        await user.send(text)
    except Exception:
        pass


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- mod group ----------
    @commands.group(name='mod', description='NarzÄ™dzia modów')
    async def mod(self, ctx):
        await ctx.reply('mod warn / warnings / timeout / kick / ban / unban / softban / tempban / nick / clear / slowmode / lock / unlock / set-log',
                        ephemeral=True)

    @mod.command(name='warn', description='Warn dla typa')
    @staff_or('manage_messages')
    async def warn(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)',
                         (str(gid), str(member.id), str(ctx.author.id), reason, int(time.time())))
            count = conn.execute('SELECT COUNT(*) c FROM warns WHERE guild_id=? AND user_id=?',
                                 (str(gid), str(member.id))).fetchone()['c']
        if count >= 3:
            await try_dm(member, t(gid, 'mod.dm_kick', server=ctx.guild.name, reason=reason))
            try:
                await member.kick(reason=f'3 strikes: {reason}')
            except Exception:
                return await ctx.reply(t(gid, 'mod.fail_kick'), ephemeral=True)
            await log_to_mod(ctx.guild, build(f'{member.mention} kicked by {ctx.author.mention} (3/3 strikes)\nReason: {reason}\nTotal warns: **{count}**',
                                              title='Kick', color=DARK_RED))
            return await ctx.reply(t(gid, 'mod.kicked', user=member.mention, reason=reason), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'{member.mention} warned by {ctx.author.mention}\nReason: {reason}\nTotal warns: **{count}**',
                                          title='Warn', color=WHITE))
        await ctx.reply(t(gid, 'mod.warned', user=member.mention, n=count, reason=reason), ephemeral=True)

    @mod.command(name='warnings', description='Warny typa')
    @staff_or('manage_messages')
    async def warnings(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20',
                                (str(gid), str(member.id))).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'mod.no_warns'), ephemeral=True)
        lines = [f"**{i + 1}.** {r['reason']} — <@{r['mod_id']}> <t:{r['created_at']}:R>" for i, r in enumerate(rows)]
        await ctx.reply('\n'.join(lines), ephemeral=True)

    @mod.command(name='clear-warns', description='WyczyÅ›Ä‡ warny')
    @staff_or('manage_guild')
    async def clear_warns(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM warns WHERE guild_id=? AND user_id=?', (str(gid), str(member.id)))
        await ctx.reply(t(gid, 'mod.warns_cleared', user=member.mention), ephemeral=True)

    @mod.command(name='timeout', description='Knebel (minuty)')
    @staff_or('moderate_members')
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        try:
            await member.timeout(minutes * 60, reason=reason)
        except Exception:
            return await ctx.reply(t(gid, 'mod.no_member'), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'{member.mention} timed out {minutes}m by {ctx.author.mention}\n{reason}',
                                          title='Timeout', color=DARK_RED))
        await ctx.reply(t(gid, 'mod.timed', user=member.mention, mins=minutes), ephemeral=True)

    @mod.command(name='untimeout', description='Zdejmij knebel')
    @staff_or('moderate_members')
    async def untimeout(self, ctx, member: discord.Member):
        try:
            await member.timeout(None)
        except Exception:
            pass
        await ctx.reply(t(ctx.guild.id, 'mod.untime', user=member.mention), ephemeral=True)

    @mod.command(name='kick', description='Wykop typa')
    @staff_or('kick_members')
    async def kick(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        await try_dm(member, t(gid, 'mod.dm_kick', server=ctx.guild.name, reason=reason))
        try:
            await member.kick(reason=reason)
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_kick'), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'User: {member} ({member.id})\nReason: {reason}\nBy: {ctx.author}',
                                          title='Kick', color=DARK_RED))
        await ctx.reply(t(gid, 'mod.kicked', user=member.mention, reason=reason), ephemeral=True)

    @mod.command(name='ban', description='Ban dla typa')
    @staff_or('ban_members')
    async def ban(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        await try_dm(member, t(gid, 'mod.dm_ban', server=ctx.guild.name, reason=reason))
        try:
            await ctx.guild.ban(member, reason=f'{ctx.author} — {reason}', delete_message_days=1)
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_ban'), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'User: {member} ({member.id})\nReason: {reason}\nBy: {ctx.author}',
                                          title='Ban', color=RED))
        await ctx.reply(t(gid, 'mod.banned', user=member.mention, reason=reason), ephemeral=True)

    @mod.command(name='unban', description='Odban po ID')
    @staff_or('ban_members')
    async def unban(self, ctx, user_id: str, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user, reason=f'{ctx.author} — {reason}')
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_unban'), ephemeral=True)
        await ctx.reply(t(gid, 'mod.unbanned', user=str(user), reason=reason), ephemeral=True)

    @mod.command(name='softban', description='Ban + odban')
    @staff_or('ban_members')
    async def softban(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        await try_dm(member, t(gid, 'mod.dm_soft', server=ctx.guild.name, reason=reason))
        try:
            await ctx.guild.ban(member, reason=f'[Softban] {ctx.author} — {reason}', delete_message_days=7)
            await asyncio.sleep(0.5)
            await ctx.guild.unban(member, reason='[Softban] automatic unban')
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_ban'), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'User: {member} ({member.id})\nReason: {reason}\nBy: {ctx.author}',
                                          title='Softban', color=DARK_RED))
        await ctx.reply(t(gid, 'mod.softbanned', user=member.mention, reason=reason), ephemeral=True)

    @mod.command(name='tempban', description='Ban na czas')
    @staff_or('ban_members')
    async def tempban(self, ctx, member: discord.Member, duration: str, *, reason: str = 'No reason'):
        gid = ctx.guild.id
        secs = parse_duration(duration)
        if not secs or secs < 60:
            return await ctx.reply(t(gid, 'mod.temp_use'), ephemeral=True)
        await try_dm(member, t(gid, 'mod.dm_temp', server=ctx.guild.name, dur=fmt_duration(secs), reason=reason))
        try:
            await ctx.guild.ban(member, reason=f'[Tempban {fmt_duration(secs)}] {ctx.author} — {reason}', delete_message_days=1)
        except Exception:
            return await ctx.reply(t(gid, 'mod.fail_ban'), ephemeral=True)
        await log_to_mod(ctx.guild, build(f'User: {member} ({member.id}) for **{fmt_duration(secs)}**\nReason: {reason}\nBy: {ctx.author}',
                                          title='Tempban', color=DARK_RED))
        await ctx.reply(t(gid, 'mod.tempbanned', user=member.mention, dur=fmt_duration(secs)), ephemeral=True)
        await asyncio.sleep(min(secs, 2147483))
        try:
            await ctx.guild.unban(member, reason='[Tempban] expired')
        except Exception:
            pass

    @mod.command(name='nick', description='ZmieÅ„ / zresetuj nick')
    @staff_or('manage_nicknames')
    async def nick(self, ctx, member: discord.Member, *, nickname: str = None):
        gid = ctx.guild.id
        try:
            await member.edit(nick=nickname)
        except Exception:
            return await ctx.reply(t(gid, 'mod.nick_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'mod.nick_done', nick=nickname) if nickname else t(gid, 'mod.nick_reset'), ephemeral=True)

    @mod.command(name='clear', description='Szybkie czyszczenie')
    @staff_or('manage_messages')
    async def mod_clear(self, ctx, amount: int):
        await ctx.channel.purge(limit=min(max(amount, 1), 100))
        await ctx.reply(t(ctx.guild.id, 'mod.cleared', n=amount), ephemeral=True)

    @mod.command(name='slowmode', description='Slowmode (sekundy)')
    @staff_or('manage_channels')
    async def slowmode(self, ctx, seconds: int):
        await ctx.channel.edit(slowmode_delay=max(0, min(seconds, 21600)))
        await ctx.reply(t(ctx.guild.id, 'mod.slow_set', s=max(0, min(seconds, 21600))), ephemeral=True)

    @mod.command(name='lock', description='Zamknij kanaÅ‚')
    @staff_or('manage_channels')
    async def lock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.reply(embed=ok(t(ctx.guild.id, 'mod.locked')))

    @mod.command(name='unlock', description='Otwórz kanaÅ‚')
    @staff_or('manage_channels')
    async def unlock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
        await ctx.reply(embed=ok(t(ctx.guild.id, 'mod.unlocked')))

    @mod.command(name='set-log', description='KanaÅ‚ logów')
    @staff_or('manage_guild')
    async def set_log(self, ctx, channel: discord.TextChannel = None):
        db.get_settings(ctx.guild.id)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET modlog_channel=? WHERE guild_id=?',
                         (str(channel.id) if channel else None, str(ctx.guild.id)))
        await ctx.reply(t(ctx.guild.id, 'mod.log_set', ch=channel.mention) if channel else t(ctx.guild.id, 'mod.log_off'),
                        ephemeral=True)

    # ---------- standalone clear (purge++) ----------
    @commands.command(name='clear', description='Masowe czyszczenie')
    @staff_or('manage_messages')
    async def clear(self, ctx, amount: int = 10, target: discord.Member = None, bots: bool = False,
                    wipe_all: bool = False, match: str = None):
        gid = ctx.guild.id
        ch = ctx.channel
        if wipe_all:
            deleted = 0
            for _ in range(10):
                batch = await ch.purge(limit=100)
                deleted += len(batch)
                if len(batch) < 100:
                    break
                await asyncio.sleep(1.2)
            return await _safe_reply(ctx, t(gid, 'clear.wiped', n=deleted), ephemeral=True)

        def check(m):
            if target and m.author.id != target.id:
                return False
            if bots and not m.author.bot:
                return False
            if match and match.lower() not in (m.content or '').lower():
                return False
            return True

        if not target and not bots and not match:
            batch = await ch.purge(limit=min(max(amount, 1), 100))
            return await _safe_reply(ctx, t(gid, 'clear.deleted', n=len(batch)), ephemeral=True)
        pool = []
        last = None
        while len(pool) < min(max(amount * 5, 200), 500):
            msgs = [m async for m in ch.history(limit=100, before=last)]
            if not msgs:
                break
            pool.extend(msgs)
            last = msgs[-1]
            if len(msgs) < 100:
                break
        hits = [m for m in pool if check(m)][:min(amount, 500)]
        deleted = 0
        now_ts = discord.utils.utcnow().timestamp()
        for i in range(0, len(hits), 100):
            chunk = [m for m in hits[i:i + 100] if now_ts - m.created_at.timestamp() < 13 * 86400]
            if chunk:
                try:
                    await ch.delete_messages(chunk)
                    deleted += len(chunk)
                except Exception:
                    pass
        if target:
            label = t(gid, 'clear.f_user', user=str(target))
        elif bots:
            label = t(gid, 'clear.f_bots')
        elif match:
            label = t(gid, 'clear.f_match', q=match)
        else:
            label = ''
        await _safe_reply(ctx, t(gid, 'clear.deleted2', n=deleted, label=label), ephemeral=True)

    # ---------- purge suite ----------
    @commands.group(name='purge', description='Czyszczenie PRO')
    @staff_or('manage_messages')
    async def purge(self, ctx, amount: int = None):
        gid = ctx.guild.id
        if amount is None:
            # bare purge = nuke the whole channel
            deleted = 0
            for _ in range(10):
                try:
                    batch = await ctx.channel.purge(limit=100)
                except Exception:
                    break
                deleted += len(batch)
                if len(batch) < 100:
                    break
                await asyncio.sleep(1.2)
            await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
            return await _safe_reply(ctx, embed=ok(t(gid, 'pg.nuked', n=deleted)))
        try:
            batch = await ctx.channel.purge(limit=min(max(amount, 1), 100))
        except Exception:
            return await _safe_reply(ctx, t(gid, 'pg.failed'), ephemeral=True)
        await _purge_log(ctx.guild, ctx.channel, len(batch), ctx.author)
        await _safe_reply(ctx, t(gid, 'pg.deleted', n=len(batch)), ephemeral=True)

    async def _purge_filtered(self, ctx, filt, amount: int, cap: int = 500):
        gid = ctx.guild.id
        pool = await _scan(ctx.channel, cap)
        hits = [m for m in pool if filt(m)][:min(max(amount, 1), cap)]
        deleted = await _wipe(hits, ctx.channel)
        await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
        await _safe_reply(ctx, t(gid, 'pg.deleted', n=deleted), ephemeral=True)

    @purge.command(name='nuke', description='WyczyÅ›Ä‡ CAÅY kanaÅ‚')
    @staff_or('manage_messages')
    async def purge_nuke(self, ctx):
        deleted = 0
        for _ in range(10):
            try:
                batch = await ctx.channel.purge(limit=100)
            except Exception:
                break
            deleted += len(batch)
            if len(batch) < 100:
                break
            await asyncio.sleep(1.2)
        await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
        await _safe_reply(ctx, embed=ok(t(ctx.guild.id, 'pg.nuked', n=deleted)))

    @purge.command(name='after', description='UsuÅ„ N po wiadomoÅ›ci')
    @staff_or('manage_messages')
    async def purge_after(self, ctx, message: str, amount: int):
        ref = await _find_msg(ctx, message)
        if not ref:
            return await _safe_reply(ctx, t(ctx.guild.id, 'pg.no_msg'), ephemeral=True)
        msgs = [m async for m in ctx.channel.history(limit=min(max(amount, 1), 500), after=ref, oldest_first=True)]
        deleted = await _wipe(msgs, ctx.channel)
        await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
        await _safe_reply(ctx, t(ctx.guild.id, 'pg.deleted', n=deleted), ephemeral=True)

    @purge.command(name='before', description='UsuÅ„ N przed wiadomoÅ›ciÄ…')
    @staff_or('manage_messages')
    async def purge_before(self, ctx, message: str, amount: int):
        ref = await _find_msg(ctx, message)
        if not ref:
            return await _safe_reply(ctx, t(ctx.guild.id, 'pg.no_msg'), ephemeral=True)
        msgs = [m async for m in ctx.channel.history(limit=min(max(amount, 1), 500), before=ref)]
        deleted = await _wipe(msgs, ctx.channel)
        await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
        await _safe_reply(ctx, t(ctx.guild.id, 'pg.deleted', n=deleted), ephemeral=True)

    @purge.command(name='between', description='UsuÅ„ miÄ™dzy wiadomoÅ›ciami')
    @staff_or('manage_messages')
    async def purge_between(self, ctx, start: str, end: str):
        a, b = await _find_msg(ctx, start), await _find_msg(ctx, end)
        if not a or not b:
            return await _safe_reply(ctx, t(ctx.guild.id, 'pg.no_msg'), ephemeral=True)
        if a.created_at > b.created_at:
            a, b = b, a
        msgs = [m async for m in ctx.channel.history(limit=500, after=a, before=b)]
        deleted = await _wipe(msgs, ctx.channel)
        await _purge_log(ctx.guild, ctx.channel, deleted, ctx.author)
        await _safe_reply(ctx, t(ctx.guild.id, 'pg.deleted', n=deleted), ephemeral=True)

    @purge.command(name='bots', description='UsuÅ„ od botów')
    @staff_or('manage_messages')
    async def purge_bots(self, ctx, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: m.author.bot, amount)

    @purge.command(name='humans', description='UsuÅ„ od ludzi')
    @staff_or('manage_messages')
    async def purge_humans(self, ctx, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: not m.author.bot and m.webhook_id is None, amount)

    @purge.command(name='contains', description='UsuÅ„ z tekstem')
    @staff_or('manage_messages')
    async def purge_contains(self, ctx, amount: int, *, text: str):
        q = text.lower()
        await self._purge_filtered(ctx, lambda m: q in (m.content or '').lower(), amount)

    @purge.command(name='emojis', description='UsuÅ„ z emoji')
    @staff_or('manage_messages')
    async def purge_emojis(self, ctx, amount: int = 50):
        await self._purge_filtered(
            ctx, lambda m: bool(CUSTOM_EMOJI_RE.search(m.content or '') or UNICODE_EMOJI_RE.search(m.content or '')), amount)

    @purge.command(name='files', description='UsuÅ„ z plikami')
    @staff_or('manage_messages')
    async def purge_files(self, ctx, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: len(m.attachments) > 0, amount)

    @purge.command(name='invites', description='UsuÅ„ z invite')
    @staff_or('manage_messages')
    async def purge_invites(self, ctx, amount: int = 50):
        from cogs.automod import INVITE_RE
        await self._purge_filtered(ctx, lambda m: bool(INVITE_RE.search(m.content or '')), amount)

    @purge.command(name='links', description='UsuÅ„ z linkami')
    @staff_or('manage_messages')
    async def purge_links(self, ctx, amount: int = 50):
        from cogs.automod import LINK_RE
        await self._purge_filtered(ctx, lambda m: bool(LINK_RE.search(m.content or '')), amount)

    @purge.command(name='mentions', description='UsuÅ„ z oznaczeniami')
    @staff_or('manage_messages')
    async def purge_mentions(self, ctx, amount: int = 50):
        await self._purge_filtered(
            ctx, lambda m: len(m.mentions) + len(m.role_mentions) > 0 or m.mention_everyone, amount)

    @purge.command(name='stickers', description='UsuÅ„ z naklejkami')
    @staff_or('manage_messages')
    async def purge_stickers(self, ctx, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: len(m.stickers) > 0, amount)

    @purge.command(name='user', description='UsuÅ„ od typa')
    @staff_or('manage_messages')
    async def purge_user(self, ctx, member: discord.Member, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: m.author.id == member.id, amount)

    @purge.command(name='voice', description='UsuÅ„ gÅ‚osówki')
    @staff_or('manage_messages')
    async def purge_voice(self, ctx, amount: int = 50):
        def is_voice(m):
            for a in m.attachments:
                ct = (a.content_type or '').lower()
                if ct.startswith('audio/') or (a.filename or '').lower().endswith('.ogg'):
                    return True
            return False
        await self._purge_filtered(ctx, is_voice, amount)

    @purge.command(name='webhooks', description='UsuÅ„ z webhooków')
    @staff_or('manage_messages')
    async def purge_webhooks(self, ctx, amount: int = 50):
        await self._purge_filtered(ctx, lambda m: m.webhook_id is not None, amount)

    @purge.group(name='logs', description='Logi czyszczenia')
    async def purge_logs(self, ctx):
        await ctx.reply('.purge logs set / remove / ignore / unignore / list', ephemeral=True)

    @purge_logs.command(name='set', description='Logi tutaj')
    @staff_or('manage_guild')
    async def pl_set(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO purgelogs (guild_id, channel_id) VALUES (?,?)',
                         (str(ctx.guild.id), str(channel.id)))
        await ctx.reply(t(ctx.guild.id, 'pg.logs_set', ch=channel.mention), ephemeral=True)

    @purge_logs.command(name='remove', description='WyÅ‚Ä…cz logi')
    @staff_or('manage_guild')
    async def pl_remove(self, ctx, channel: discord.TextChannel = None):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM purgelogs WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'pg.logs_off'), ephemeral=True)

    @purge_logs.command(name='ignore', description='PomiÅ„ kanaÅ‚')
    @staff_or('manage_guild')
    async def pl_ignore(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO purgeignore (guild_id, channel_id) VALUES (?,?)',
                         (str(ctx.guild.id), str(channel.id)))
        await ctx.reply(t(ctx.guild.id, 'pg.ignored', ch=channel.mention), ephemeral=True)

    @purge_logs.command(name='unignore', description='Loguj z powrotem')
    @staff_or('manage_guild')
    async def pl_unignore(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM purgeignore WHERE guild_id=? AND channel_id=?',
                         (str(ctx.guild.id), str(channel.id)))
        await ctx.reply(t(ctx.guild.id, 'pg.unignored', ch=channel.mention), ephemeral=True)

    @purge_logs.command(name='list', description='Pomijane kanaÅ‚y')
    @staff_or('manage_guild')
    async def pl_list(self, ctx):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT channel_id FROM purgeignore WHERE guild_id=?', (str(ctx.guild.id),)).fetchall()
        await ctx.reply('\n'.join(f'<#{r["channel_id"]}>' for r in rows) if rows else t(ctx.guild.id, 'pg.ignore_empty'),
                        ephemeral=True)

    # ---------- permabans ----------
    @commands.group(name='banlist', description='Permbany')
    async def banlist(self, ctx):
        await ctx.reply('.banlist add / remove / list', ephemeral=True)

    @banlist.command(name='add', description='Permban po ID')
    @staff_or('ban_members')
    async def bl_add(self, ctx, user_id: str, *, reason: str = 'Permabanned by staff'):
        gid = ctx.guild.id
        if not user_id.isdigit() or not (15 <= len(user_id) <= 25):
            return await ctx.reply(t(gid, 'bl.bad_id'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO permabans (guild_id, user_id, reason) VALUES (?,?,?)',
                         (str(gid), user_id, reason))
        try:
            await ctx.guild.ban(discord.Object(id=int(user_id)), reason=f'[Permaban] {ctx.author} — {reason}',
                                delete_message_days=7)
        except Exception:
            pass
        await ctx.reply(embed=ok(t(gid, 'bl.added', user=f'<@{user_id}>', id=user_id)))

    @banlist.command(name='remove', description='Zdejmij permbana')
    @staff_or('ban_members')
    async def bl_remove(self, ctx, user_id: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM permabans WHERE guild_id=? AND user_id=?', (str(gid), user_id))
        try:
            await ctx.guild.unban(discord.Object(id=int(user_id)))
        except Exception:
            pass
        await ctx.reply(embed=ok(t(gid, 'bl.removed', id=user_id)))

    @banlist.command(name='list', description='Lista permów')
    @staff_or('ban_members')
    async def bl_list(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM permabans WHERE guild_id=?', (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'bl.empty'), ephemeral=True)
        await ctx.reply(embed=ok('\n'.join(f"• <@{r['user_id']}> (`{r['user_id']}`) — {r['reason']}" for r in rows)),
                        ephemeral=True)

    # standalone twins (prefix + slash) — the mod group stays prefix-only
    @commands.hybrid_command(name='kick', description='Wykop typa')
    @staff_or('kick_members')
    async def kick_top(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        return await self.kick.callback(self, ctx, member, reason=reason)

    @commands.hybrid_command(name='ban', description='Ban dla typa')
    @staff_or('ban_members')
    async def ban_top(self, ctx, member: discord.Member, *, reason: str = 'No reason'):
        return await self.ban.callback(self, ctx, member, reason=reason)


async def setup(bot):
    await bot.add_cog(Moderation(bot))

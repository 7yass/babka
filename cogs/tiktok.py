"""TikTok notifier: RapidAPI (tiktok-api23) polling, posts video link + embed."""
import os

import aiohttp
import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import build, ok, WHITE
from utils.checks import staff_or

RAPID_HOST = 'tiktok-api23.p.rapidapi.com'


def _headers():
    return {'User-Agent': 'Mozilla/5.0', 'x-rapidapi-host': RAPID_HOST,
            'x-rapidapi-key': os.environ.get('RAPIDAPI_KEY', '')}


def _dig(obj, *paths):
    for path in paths:
        cur = obj
        try:
            for part in path.split('.'):
                cur = cur[part] if isinstance(cur, dict) else cur[int(part)]
            if cur:
                return cur
        except Exception:
            continue
    return None


async def _get(session: aiohttp.ClientSession, path: str, params: dict):
    async with session.get(f'https://{RAPID_HOST}{path}', headers=_headers(), params=params,
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status in (401, 403):
            raise RuntimeError('RapidAPI key missing/invalid — set RAPIDAPI_KEY in .env')
        if r.status == 429:
            raise RuntimeError('RapidAPI quota spent')
        if r.status != 200:
            raise RuntimeError(f'TikTok API HTTP {r.status}')
        return await r.json()


async def resolve_secuid(username: str) -> str:
    clean = username.lstrip('@').strip()
    async with aiohttp.ClientSession() as session:
        data = await _get(session, '/api/user/info', {'uniqueId': clean})
    sec = _dig(data, 'userInfo.user.secUid', 'user.secUid', 'data.user.secUid', 'secUid')
    if not sec:
        print(f'[tiktok] info keys: {list(data.keys())[:10]}')
        raise RuntimeError('no secUid in user info')
    return sec


async def fetch_latest(username: str, sec_uid: str = None):
    clean = username.lstrip('@').strip()
    async with aiohttp.ClientSession() as session:
        sec = sec_uid or await resolve_secuid(clean)
        data = await _get(session, '/api/user/posts', {'secUid': sec, 'count': 5, 'cursor': 0})
    videos = (_dig(data, 'data.videos', 'data.itemList', 'videos', 'items', 'aweme_list', 'data.aweme_list') or [])
    if not videos:
        print(f'[tiktok] posts keys: {list(data.keys())[:10]}')
        return {'secUid': sec, 'none': True}
    v = videos[0]
    vid = str(_dig(v, 'video_id', 'id', 'aweme_id') or '')
    desc = str(_dig(v, 'title', 'desc') or '')[:200]
    url = _dig(v, 'share_url', 'shareUrl') or f'https://www.tiktok.com/@{clean}/video/{vid}'
    cover = _dig(v, 'cover', 'ai_dynamic_cover', 'originCover', 'video.cover')
    return {'id': vid, 'desc': desc, 'url': url, 'cover': cover, 'author': clean, 'secUid': sec}


def render(tpl: str, video: dict) -> str:
    return tpl.replace('{author}', '@' + video['author']).replace('{url}', video['url']).replace('{desc}', video.get('desc') or '')


class TikTok(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Persisted, not in-memory: the host restarts the process and an
        # in-memory pause reset every time, so a spent RapidAPI key got hammered
        # again on every boot (201 identical log lines in one 80-minute window).
        try:
            self._quota_pause_until = int(db.meta_get('tiktok_pause_until', 0) or 0)
        except Exception:
            self._quota_pause_until = 0
        self.check.start()

    def cog_unload(self):
        self.check.cancel()

    @tasks.loop(minutes=15)
    async def check(self):
        await self.bot.wait_until_ready()
        import time as _t
        if _t.time() < self._quota_pause_until:
            return
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM tiktok_watch').fetchall()]
        by_user: dict = {}
        for r in rows:
            by_user.setdefault(r['tiktok_username'], []).append(r)
        for username, watchers in by_user.items():
            sec = next((w.get('sec_uid') for w in watchers if w.get('sec_uid')), None)
            try:
                latest = await fetch_latest(username, sec)
            except Exception as e:
                if 'quota' in str(e).lower():
                    import time as _t2
                    self._quota_pause_until = _t2.time() + 6 * 3600
                    try:
                        db.meta_set('tiktok_pause_until', self._quota_pause_until)
                    except Exception:
                        pass
                    print('[tiktok] quota spent — pausing checks for 6h')
                    return
                print(f'[tiktok] {username}: {e}')
                continue
            if not latest or latest.get('none'):
                continue
            if latest.get('secUid') and latest['secUid'] != sec:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE tiktok_watch SET sec_uid=? WHERE guild_id=? AND tiktok_username=?',
                                 (latest['secUid'], watchers[0]['guild_id'], username))
            for w in watchers:
                if w.get('last_video_id') == latest['id']:
                    continue
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE tiktok_watch SET last_video_id=? WHERE guild_id=? AND tiktok_username=?',
                                 (latest['id'], w['guild_id'], w['tiktok_username']))
                if not w.get('last_video_id'):
                    continue  # first run: seed quietly
                try:
                    guild = self.bot.get_guild(int(w['guild_id']))
                    ch = guild and guild.get_channel(int(w['channel_id']))
                    if not ch:
                        continue
                    embed = discord.Embed(title=t(w['guild_id'], 'tt.post', author=latest['author']),
                                          url=latest['url'], description=latest['desc'] or latest['url'],
                                          color=WHITE)
                    if latest.get('cover'):
                        embed.set_image(url=latest['cover'])
                    ping = f"<@&{w['ping_role']}> " if w.get('ping_role') else ''
                    await ch.send(ping + render(w.get('template') or t(w['guild_id'], 'tt.tpl'), latest),
                                  embed=embed,
                                  allowed_mentions=discord.AllowedMentions(roles=True))
                except Exception as e:
                    print(f'[tiktok send] {e}')
            import asyncio
            await asyncio.sleep(1.5)

    @check.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.group(name='tiktok', description='Powiadomienia TikTok')
    async def tiktok(self, ctx):
        await ctx.reply('.tiktok watch / unwatch / list / test', ephemeral=True)

    @tiktok.command(name='watch', description='Śledź konto')
    @staff_or('manage_guild')
    async def watch(self, ctx, username: str, channel: discord.TextChannel, ping: discord.Role = None,
                    template: str = None):
        gid = ctx.guild.id
        username = username.lstrip('@').strip()
        tpl = template or t(gid, 'tt.tpl')
        await ctx.defer(ephemeral=True)
        try:
            latest = await fetch_latest(username)
        except Exception as e:
            return await ctx.reply(embed=ok(t(gid, 'tt.fail', user=username, e=e)))
        if not latest:
            return await ctx.reply(embed=ok(t(gid, 'tt.latest_none', user=username)))
        empty = bool(latest.get('none'))
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO tiktok_watch (guild_id, tiktok_username, channel_id, last_video_id, template, sec_uid, ping_role) VALUES (?,?,?,?,?,?,?)',
                         (str(gid), username.lower(), str(channel.id), latest.get('id'),
                          tpl, latest.get('secUid'), str(ping.id) if ping else None))
        key = 'tt.watching_empty' if empty else 'tt.watching'
        await ctx.reply(embed=ok(t(gid, key, user=username, ch=channel.mention, url=latest.get('url') or '')))

    @tiktok.command(name='unwatch', description='Odśledź')
    @staff_or('manage_guild')
    async def unwatch(self, ctx, username: str):
        username = username.lstrip('@').lower()
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM tiktok_watch WHERE guild_id=? AND tiktok_username=?',
                         (str(ctx.guild.id), username))
        await ctx.reply(t(ctx.guild.id, 'tt.stopped', user=username), ephemeral=True)

    @tiktok.command(name='list', description='Śledzone konta')
    async def list_w(self, ctx):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM tiktok_watch WHERE guild_id=?', (str(ctx.guild.id),)).fetchall()
        if not rows:
            return await ctx.reply(t(ctx.guild.id, 'tt.empty'), ephemeral=True)
        await ctx.reply(embed=ok('\n'.join(f"• @**{r['tiktok_username']}** → <#{r['channel_id']}>" for r in rows)), ephemeral=True)

    @tiktok.command(name='test', description='Ostatni film')
    async def test(self, ctx, username: str):
        gid = ctx.guild.id
        await ctx.defer(ephemeral=True)
        try:
            v = await fetch_latest(username.lstrip('@'))
            if not v or v.get('none'):
                return await ctx.reply(embed=ok(t(gid, 'tt.latest_none', user=username)))
            await ctx.reply(embed=ok(t(gid, 'tt.latest', user=username, url=v['url'], desc=v['desc'])))
        except Exception as e:
            await ctx.reply(embed=ok(t(gid, 'tt.fail2', e=e)))


async def setup(bot):
    await bot.add_cog(TikTok(bot))

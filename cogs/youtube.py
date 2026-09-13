"""YouTube notifier: polls channel RSS, posts new uploads + optional ping. No API key."""
import re

import aiohttp
import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or


async def resolve_channel_id(ref: str) -> tuple:
    """Returns (channel_id, label) from an ID, URL, or @handle."""
    ref = (ref or '').strip()
    m = re.search(r'(UC[\w-]{22})', ref)
    if m:
        return m.group(1), ref
    m = re.search(r'@([\w.\-]+)', ref)
    handle = m.group(1) if m else ref.lstrip('@')
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://www.youtube.com/@{handle}',
                               headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en'},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            html = await r.text()
    m = re.search(r'"channelId":"(UC[\w-]{22})"', html)
    if m:
        return m.group(1), '@' + handle
    m = re.search(r'<link rel="alternate"[^>]*?channel_id=(UC[\w-]{22})', html)
    if m:
        return m.group(1), '@' + handle
    raise RuntimeError('channel not found — pass a /channel/UC… URL or ID instead')


async def fetch_latest(channel_id: str):
    import xml.etree.ElementTree as ET
    async with aiohttp.ClientSession() as session:
        async with session.get('https://www.youtube.com/feeds/videos.xml',
                               params={'channel_id': channel_id},
                               headers={'User-Agent': 'Mozilla/5.0'},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise RuntimeError(f'YouTube RSS HTTP {r.status}')
            xml = await r.text()
    ns = {'a': 'http://www.w3.org/2005/Atom', 'y': 'http://www.youtube.com/xml/schemas/2015'}
    root = ET.fromstring(xml)
    entries = root.findall('a:entry', ns)
    if not entries:
        return None
    e = entries[0]
    vid = (e.find('y:videoId', ns).text or '') if e.find('y:videoId', ns) is not None else ''
    title = (e.find('a:title', ns).text or '') if e.find('a:title', ns) is not None else ''
    thumb = None
    mt = e.find('a:group/y:thumbnail', ns)
    if mt is None:
        mt = e.find('media:thumbnail', {'media': 'http://search.yahoo.com/mrss/'})
    if mt is not None:
        thumb = mt.get('url')
    author = root.find('a:author/a:name', ns)
    return {'id': vid, 'title': title[:200], 'url': f'https://youtu.be/{vid}',
            'thumb': thumb, 'author': author.text if author is not None else channel_id}


class YouTube(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check.start()

    def cog_unload(self):
        self.check.cancel()

    @tasks.loop(minutes=10)
    async def check(self):
        await self.bot.wait_until_ready()
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM youtube_watch').fetchall()]
        for w in rows:
            try:
                latest = await fetch_latest(w['yt_channel_id'])
            except Exception as e:
                print(f'[youtube] {w["yt_channel_id"]}: {e}')
                continue
            if not latest or not latest['id']:
                continue
            if w.get('last_video_id') == latest['id']:
                continue
            with db.conn_ctx() as conn:
                conn.execute('UPDATE youtube_watch SET last_video_id=? WHERE guild_id=? AND yt_channel_id=?',
                             (latest['id'], w['guild_id'], w['yt_channel_id']))
            if not w.get('last_video_id'):
                continue  # first run: seed quietly
            try:
                guild = self.bot.get_guild(int(w['guild_id']))
                ch = guild and guild.get_channel(int(w['channel_id']))
                if not ch:
                    continue
                emb = discord.Embed(title=latest['title'] or t(w['guild_id'], 'yt.post'),
                                    url=latest['url'],
                                    description=t(w['guild_id'], 'yt.new', author=latest['author']),
                                    color=0xFFFFFF)
                if latest.get('thumb'):
                    emb.set_image(url=latest['thumb'])
                emb.set_footer(text='babka :3')
                ping = f"<@&{w['ping_role']}> " if w.get('ping_role') else ''
                await ch.send(f"{ping}**{latest['author']}** just uploaded\n{latest['url']}",
                              embed=emb, allowed_mentions=discord.AllowedMentions(roles=True))
            except Exception as e:
                print(f'[youtube send] {e}')
            import asyncio
            await asyncio.sleep(1.5)

    @check.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.group(name='youtube', description='Powiadomienia YouTube')
    async def youtube(self, ctx):
        await ctx.reply('.youtube watch / unwatch / list / test', ephemeral=True)

    @youtube.command(name='watch', description='Śledź kanał')
    @staff_or('manage_guild')
    async def watch(self, ctx, channel_ref: str, channel: discord.TextChannel, ping: discord.Role = None):
        gid = ctx.guild.id
        await ctx.defer(ephemeral=True)
        try:
            cid, label = await resolve_channel_id(channel_ref)
            latest = await fetch_latest(cid)
        except Exception as e:
            return await ctx.reply(embed=ok(t(gid, 'yt.fail', e=e)))
        with db.conn_ctx() as conn:
            conn.execute('''INSERT OR REPLACE INTO youtube_watch
                (guild_id, yt_channel_id, label, channel_id, last_video_id, ping_role)
                VALUES (?,?,?,?,?,?)''', (str(gid), cid, label, str(channel.id),
                                          latest['id'] if latest else None,
                                          str(ping.id) if ping else None))
        await ctx.reply(embed=ok(t(gid, 'yt.watching', ch=label, dc=channel.mention)))

    @youtube.command(name='unwatch', description='Odśledź')
    @staff_or('manage_guild')
    async def unwatch(self, ctx, channel_ref: str):
        try:
            cid, _ = await resolve_channel_id(channel_ref)
        except Exception:
            cid = channel_ref
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM youtube_watch WHERE guild_id=? AND yt_channel_id=?',
                         (str(ctx.guild.id), cid))
        await ctx.reply(t(ctx.guild.id, 'yt.stopped'), ephemeral=True)

    @youtube.command(name='list', description='Śledzone kanały')
    async def list_w(self, ctx):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT label, yt_channel_id, channel_id FROM youtube_watch WHERE guild_id=?',
                                (str(ctx.guild.id),)).fetchall()
        if not rows:
            return await ctx.reply(t(ctx.guild.id, 'yt.empty'), ephemeral=True)
        await ctx.reply(embed=ok('\n'.join(f"• **{r['label']}** → <#{r['channel_id']}>" for r in rows)),
                        ephemeral=True)

    @youtube.command(name='test', description='Ostatni film')
    async def test(self, ctx, channel_ref: str):
        gid = ctx.guild.id
        await ctx.defer(ephemeral=True)
        try:
            cid, label = await resolve_channel_id(channel_ref)
            v = await fetch_latest(cid)
            if not v or not v['id']:
                return await ctx.reply(embed=ok(t(gid, 'yt.none', ch=label)))
            await ctx.reply(embed=ok(f'**{v["title"]}**\n{v["url"]}'))
        except Exception as e:
            await ctx.reply(embed=ok(t(gid, 'yt.fail', e=e)))


async def setup(bot):
    await bot.add_cog(YouTube(bot))

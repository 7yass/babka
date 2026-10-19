"""Lookups: avatar, banner, Roblox user/avatar/previousnames. All hybrid."""
import aiohttp
import discord
from discord.ext import commands

from lang import t
from utils.embeds import ok
from utils.ui import panel, ACCENT, stat, divider
from discord.ui.media_gallery import MediaGalleryItem

UA = {'User-Agent': 'Mozilla/5.0'}


async def _get_json(session, url, **kwargs):
    async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=12), **kwargs) as r:
        if r.status != 200:
            return None
        try:
            return await r.json()
        except Exception:
            return None


class Lookups(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='avatar', description="CzyjÅ› awatar")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.reply(view=panel(str(member), '', accent=ACCENT['default'],
                                   image_url=str(member.display_avatar.with_size(1024).url)),
                        mention_author=False)

    @commands.command(name='banner', description="CzyjÅ› banner")
    async def banner(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        gid = ctx.guild.id if ctx.guild else None
        try:
            u = await self.bot.fetch_user(member.id)
            b = u.banner
        except Exception:
            b = None
        if not b:
            return await ctx.reply(t(gid, 'lk.no_banner', user=str(member)), ephemeral=True)
        await ctx.reply(view=panel(str(member), '', accent=ACCENT['default'],
                                   image_url=str(b.with_size(1024).url)),
                        mention_author=False)

    # ---------- roblox ----------
    @commands.group(name='roblox', description='Sprawdzanie Robloxa')
    async def roblox(self, ctx):
        await ctx.reply('.roblox user / avatar / previousnames', ephemeral=True)

    async def _resolve(self, username: str):
        async with aiohttp.ClientSession() as s:
            async with s.post('https://users.roblox.com/v1/usernames/users',
                              json={'usernames': [username], 'excludeBannedUsers': False},
                              headers=UA, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        items = data.get('data') or []
        return items[0] if items else None

    @roblox.command(name='user', description='Info o graczu')
    async def rbx_user(self, ctx, username: str):
        gid = ctx.guild.id if ctx.guild else None
        info = await self._resolve(username)
        if not info:
            return await ctx.reply(t(gid, 'lk.rbx_none', user=username), ephemeral=True)
        uid = info['id']
        async with aiohttp.ClientSession() as s:
            prof = await _get_json(s, f'https://users.roblox.com/v1/users/{uid}') or {}
            fc = await _get_json(s, f'https://friends.roblox.com/v1/users/{uid}/friends/count') or {}
            fl = await _get_json(s, f'https://friends.roblox.com/v1/users/{uid}/followers/count') or {}
        desc = (prof.get('description') or '')[:300] or '—'
        title = f"{prof.get('displayName', info.get('displayName'))} (@{prof.get('name', username)})"
        body = desc
        body += '\n' + stat(t(gid, 'lk.rbx_id'), str(uid))
        body += '\n' + stat(t(gid, 'lk.rbx_created'), (prof.get('created') or '?')[:10])
        body += '\n' + stat(t(gid, 'lk.rbx_friends'), f"{fc.get('count', '?')} / {fl.get('count', '?')}")
        body += '\n' + stat(t(gid, 'ui.rbx_profile'), f'https://www.roblox.com/users/{uid}/profile')
        if prof.get('isBanned'):
            body += '\n' + stat(t(gid, 'lk.rbx_banned'), t(gid, 'lk.yes'))
        await ctx.reply(view=panel(title, body, accent=ACCENT['default']), mention_author=False)

    @roblox.command(name='avatar', description='Awatar gracza')
    async def rbx_avatar(self, ctx, username: str):
        gid = ctx.guild.id if ctx.guild else None
        info = await self._resolve(username)
        if not info:
            return await ctx.reply(t(gid, 'lk.rbx_none', user=username), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            th = await _get_json(
                s, 'https://thumbnails.roblox.com/v1/users/avatar-headshot',
                params={'userIds': info['id'], 'size': '420x420', 'format': 'Png', 'isCircular': 'false'})
        items = (th or {}).get('data') or []
        if not items or not items[0].get('imageUrl'):
            return await ctx.reply(t(gid, 'lk.rbx_noimg'), ephemeral=True)
        body = stat(t(gid, 'ui.rbx_profile'), f'https://www.roblox.com/users/{info["id"]}/profile')
        await ctx.reply(view=panel(f'@{username}', body, accent=ACCENT['default'],
                                   image_url=items[0]['imageUrl']), mention_author=False)

    @roblox.command(name='previousnames', description='Stare nicki')
    async def rbx_names(self, ctx, username: str):
        gid = ctx.guild.id if ctx.guild else None
        info = await self._resolve(username)
        if not info:
            return await ctx.reply(t(gid, 'lk.rbx_none', user=username), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            hist = await _get_json(s, f'https://users.roblox.com/v1/users/{info["id"]}/username-history?limit=25&sortOrder=Desc')
        names = [h.get('name') for h in ((hist or {}).get('data') or []) if h.get('name')]
        if not names:
            return await ctx.reply(t(gid, 'lk.rbx_nonames', user=username), ephemeral=True)
        await ctx.reply(embed=ok(t(gid, 'lk.rbx_names', user=username) + '\n' + '\n'.join(f'• {n}' for n in names[:25])),
                        ephemeral=True)

    @commands.command(name='rbx', description='Info o koncie Roblox', aliases=['robloxinfo'])
    async def rbx(self, ctx, *, username: str = ''):
        """One-shot Roblox card: profile + avatar + friends + past names.
        Usage: `.rbx Builderman`."""
        import aiohttp as _aiohttp
        gid = ctx.guild.id if ctx.guild else None
        username = (username or '').strip()
        if not username:
            return await ctx.reply(t(gid, 'lk.rbx_usage'), ephemeral=True)
        info = await self._resolve(username)
        if not info:
            return await ctx.reply(t(gid, 'lk.rbx_none', user=username), ephemeral=True)
        uid = info['id']
        async with aiohttp.ClientSession() as s:
            prof = await _get_json(s, f'https://users.roblox.com/v1/users/{uid}') or {}
            fc = await _get_json(s, f'https://friends.roblox.com/v1/users/{uid}/friends/count') or {}
            fl = await _get_json(s, f'https://friends.roblox.com/v1/users/{uid}/followers/count') or {}
            th = await _get_json(
                s, 'https://thumbnails.roblox.com/v1/users/avatar-headshot',
                params={'userIds': uid, 'size': '420x420', 'format': 'Png', 'isCircular': 'false'})
            hist = await _get_json(s, f'https://users.roblox.com/v1/users/{uid}/username-history?limit=5&sortOrder=Desc')
        desc = (prof.get('description') or '')[:300] or '—'
        names = [h.get('name') for h in ((hist or {}).get('data') or []) if h.get('name')]
        title = f"{prof.get('displayName', info.get('displayName'))} (@{prof.get('name', username)})"
        body = desc
        body += '\n' + stat(t(gid, 'lk.rbx_id'), str(uid))
        body += '\n' + stat(t(gid, 'lk.rbx_created'), (prof.get('created') or '?')[:10])
        body += '\n' + stat(t(gid, 'lk.rbx_friends'), f"{fc.get('count', '?')} / {fl.get('count', '?')}")
        body += '\n' + stat(t(gid, 'ui.rbx_profile'), f'https://www.roblox.com/users/{uid}/profile')
        if names or prof.get('isBanned'):
            body += '\n' + divider()
        if names:
            body += '\n' + stat(t(gid, 'ui.rbx_past_names'), ', '.join(names[:5]))
        if prof.get('isBanned'):
            body += '\n' + stat(t(gid, 'lk.rbx_banned'), t(gid, 'lk.yes'))
        media = []
        items = (th or {}).get('data') or []
        if items and items[0].get('imageUrl'):
            media.append(MediaGalleryItem(media=items[0]['imageUrl']))
        await ctx.reply(view=panel(title, body, accent=ACCENT['default'], media_items=media,
                                   footer_text='Babka Danka · Roblox'), mention_author=False)


async def setup(bot):
    await bot.add_cog(Lookups(bot))

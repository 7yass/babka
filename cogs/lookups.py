"""Lookups: avatar, banner, Roblox user/avatar/previousnames. All hybrid."""
import aiohttp
import discord
from discord.ext import commands

from lang import t
from utils.embeds import WHITE, ok

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
        e = discord.Embed(title=str(member), color=WHITE)
        e.set_image(url=str(member.display_avatar.with_size(1024).url))
        await ctx.reply(embed=e, mention_author=False)

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
        e = discord.Embed(title=str(member), color=WHITE)
        e.set_image(url=str(b.with_size(1024).url))
        await ctx.reply(embed=e, mention_author=False)

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
        e = discord.Embed(title=f"{prof.get('displayName', info.get('displayName'))} (@{prof.get('name', username)})",
                          description=desc, color=WHITE,
                          url=f'https://www.roblox.com/users/{uid}/profile')
        e.add_field(name=t(gid, 'lk.rbx_id'), value=str(uid), inline=True)
        e.add_field(name=t(gid, 'lk.rbx_created'), value=(prof.get('created') or '?')[:10], inline=True)
        e.add_field(name=t(gid, 'lk.rbx_friends'), value=f"{fc.get('count', '?')} / {fl.get('count', '?')}",
                    inline=True)
        if prof.get('isBanned'):
            e.add_field(name=t(gid, 'lk.rbx_banned'), value=t(gid, 'lk.yes'), inline=True)
        await ctx.reply(embed=e, mention_author=False)

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
        e = discord.Embed(title=f"@{username}", color=WHITE,
                          url=f'https://www.roblox.com/users/{info["id"]}/profile')
        e.set_image(url=items[0]['imageUrl'])
        await ctx.reply(embed=e, mention_author=False)

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


async def setup(bot):
    await bot.add_cog(Lookups(bot))

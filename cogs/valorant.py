"""Valorant tracker (HenrikDev API, free key at https://henrikdev.xyz).
Set HENRIK_API_KEY in .env. `.val Name#Tag` shows level, rank + RR,
peak rank and last match."""
import os
import urllib.parse

import discord
from discord.ext import commands

from lang import t

API = 'https://api.henrikdev.xyz/valorant'
REGIONS = ('eu', 'na', 'ap', 'kr', 'latam', 'br')


def _headers():
    key = os.getenv('HENRIK_API_KEY', '')
    h = {'User-Agent': 'BabkaDanka/1.0'}
    if key:
        h['Authorization'] = key
    return h


async def _get(session, url):
    try:
        import aiohttp
        async with session.get(url, headers=_headers(),
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 401:
                return {'_need_key': True}
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


def _rank_emoji(tier_name: str) -> str:
    n = (tier_name or '').lower()
    if 'radiant' in n:
        return '🌟'
    if 'immortal' in n:
        return '🟥'
    if 'ascendant' in n:
        return '🟩'
    if 'diamond' in n:
        return '💠'
    if 'platinum' in n:
        return '🔷'
    if 'gold' in n:
        return '🟡'
    if 'silver' in n:
        return '⬜'
    if 'bronze' in n:
        return '🟫'
    if 'iron' in n:
        return '⬛'
    return '❓'


class Valorant(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='val', description='Statystyki Valorant', aliases=['valorant'])
    async def val(self, ctx, *, riot_id: str = ''):
        import aiohttp
        gid = ctx.guild.id
        riot_id = (riot_id or '').strip()
        if '#' not in riot_id:
            return await ctx.reply(t(gid, 'eco.val_use'), ephemeral=True)
        name, _, tag = riot_id.partition('#')
        name, tag = name.strip(), tag.strip()
        if not name or not tag:
            return await ctx.reply(t(gid, 'eco.val_use'), ephemeral=True)
        await ctx.typing()
        nu, tu = urllib.parse.quote(name), urllib.parse.quote(tag)
        async with aiohttp.ClientSession() as s:
            acc = await _get(s, f'{API}/v1/account/{nu}/{tu}')
            if (acc or {}).get('_need_key'):
                return await ctx.reply(t(gid, 'eco.val_key'), ephemeral=True)
            if not acc or acc.get('status') != 200:
                return await ctx.reply(t(gid, 'eco.val_no'), ephemeral=True)
            d = acc.get('data') or {}
            mmr, region = None, ''
            for rg in REGIONS:
                m = await _get(s, f'{API}/v1/mmr/{rg}/{nu}/{tu}')
                if m and m.get('status') == 200 and (m.get('data') or {}).get('current_data'):
                    mmr, region = m['data'], rg
                    break
            matches = await _get(s, f'{API}/v3/matches/{region or "eu"}/{nu}/{tu}?size=1')
        lvl = d.get('account_level', '?')
        card = ((d.get('card') or {}).get('small')
                or (d.get('card') or {}).get('large') or '')
        cur = (mmr or {}).get('current_data') or {}
        peak = (mmr or {}).get('highest_rank') or {}
        tier = cur.get('currenttierpatched') or 'Unrated'
        rr = cur.get('ranking_in_tier', '?')
        games = cur.get('number_of_games', '?')
        elo = cur.get('elo', '?')
        peak_t = peak.get('patched_tier') or '?'
        peak_s = peak.get('season') or '?'
        lines = [
            f"**{name}#{tag}** — {t(gid, 'eco.val_level', lvl=lvl)}",
            f"{_rank_emoji(tier)} **{tier}** — {rr} RR ({games} {t(gid, 'eco.val_games')}, {elo} ELO)",
            t(gid, 'eco.val_peak', tier=peak_t, season=peak_s),
        ]
        if region:
            lines.append(t(gid, 'eco.val_region', region=region.upper()))
        last = ''
        try:
            m0 = (matches.get('data') or [])[0]
            meta = m0.get('metadata') or {}
            me = next((p for p in m0.get('players') or []
                       if (p.get('name', '').lower(), p.get('tag', '').lower()) == (name.lower(), tag.lower())),
                      None)
            if me:
                st = me.get('stats') or {}
                last = t(gid, 'eco.val_last', agent=me.get('character', '?'),
                         map=meta.get('map', '?'), k=st.get('kills', '?'),
                         d=st.get('deaths', '?'), a=st.get('assists', '?'),
                         score=st.get('score', '?'))
        except Exception:
            pass
        if last:
            lines.append(last)
        e = discord.Embed(title=t(gid, 'eco.val_title'), description='\n'.join(lines),
                          color=0xFF4655)
        if card:
            e.set_thumbnail(url=card)
        e.set_footer(text='Babka Danka · henrikdev')
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot):
    await bot.add_cog(Valorant(bot))

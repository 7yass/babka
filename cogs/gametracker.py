"""Game trackers: League of Legends (Riot API) + Counter-Strike 2 (Steam API).

.lol Name#Tag [region] — rank, LP, winrate, top champs (needs RIOT_API_KEY).
.cs <steam vanity / id> — CS2 hours, kills, accuracy, last played (needs STEAM_API_KEY).
Without keys the bot explains how to add them instead of dying silently.
"""
import os
import urllib.parse

from discord.ext import commands

from lang import t
from utils.ui import panel, ACCENT, stat
from discord.ui.media_gallery import MediaGalleryItem


def _riot_headers():
    return {'X-Riot-Token': os.getenv('RIOT_API_KEY', '')}


REGION_MAP = {
    'eune': ('eun1', 'europe'), 'euw': ('euw1', 'europe'), 'eu': ('euw1', 'europe'),
    'na': ('na1', 'americas'), 'br': ('br1', 'americas'), 'lan': ('la1', 'americas'),
    'las': ('la2', 'americas'), 'kr': ('kr', 'asia'), 'jp': ('jp1', 'asia'),
}


async def _get_json(session, url, headers=None):
    try:
        import aiohttp
        async with session.get(url, headers=headers or {'User-Agent': 'BabkaDanka/1.0'},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


class GameTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='lol', description='Statystyki League of Legends', aliases=['league'])
    async def lol(self, ctx, *, riot_id: str = ''):
        import aiohttp
        gid = ctx.guild.id
        riot_id = (riot_id or '').strip()
        if '#' not in riot_id:
            return await ctx.reply(t(gid, 'gt.lol_usage_ex'), ephemeral=True)
        parts = riot_id.rsplit(' ', 1)
        region_arg = ''
        if len(parts) == 2 and '#' in parts[0]:
            riot_id, region_arg = parts
        name, _, tag = riot_id.partition('#')
        name, tag = name.strip(), tag.strip()
        if not name or not tag:
            return await ctx.reply(t(gid, 'gt.lol_usage'), ephemeral=True)
        if not os.getenv('RIOT_API_KEY'):
            return await ctx.reply(t(gid, 'gt.lol_nokey'), ephemeral=True)
        plat, _route = REGION_MAP.get((region_arg or 'euw').lower().replace(' ', ''), ('euw1', 'europe'))
        # riot id contains spaces — find route from region arg only; account api is regional routing value
        route = {'euw1': 'europe', 'eun1': 'europe', 'na1': 'americas', 'br1': 'americas',
                 'la1': 'americas', 'la2': 'americas', 'kr': 'asia', 'jp1': 'asia'}.get(plat, 'europe')
        await ctx.typing()
        async with aiohttp.ClientSession() as s:
            acc = await _get_json(s, f'https://{route}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}',
                                  _riot_headers())
            if not acc or not acc.get('puuid'):
                return await ctx.reply(f'Nie ma takiego gracza **{name}#{tag}**.', ephemeral=True)
            puuid = acc['puuid']
            summ = await _get_json(s, f'https://{plat}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}', _riot_headers()) or {}
            leagues = await _get_json(s, f'https://{plat}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}', _riot_headers()) or []
            masteries = await _get_json(s, f'https://{plat}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=3', _riot_headers()) or []
        solo = next((e for e in leagues if e.get('queueType') == 'RANKED_SOLO_5x5'), None)
        flex = next((e for e in leagues if e.get('queueType') == 'RANKED_FLEX_SR'), None)

        def fmt_entry(e):
            if not e:
                return 'Unranked'
            w, l = e.get('wins', 0), e.get('losses', 0)
            wr = round(100 * w / max(1, w + l))
            return f"{e.get('tier', '?').title()} {e.get('rank', '')} — {e.get('leaguePoints', 0)} LP ({w}W/{l}L, {wr}% WR)"

        body = f'**{name}#{tag}** — Lv {summ.get("summonerLevel", "?")} ({plat.upper()})'
        body += '\n' + stat(t(gid, 'ui.lol_solo'), fmt_entry(solo))
        body += '\n' + stat(t(gid, 'ui.lol_flex'), fmt_entry(flex))
        if masteries:
            tops = ', '.join(f"champ {m.get('championId')} ({m.get('championPoints', 0):,} pts)".replace(',', ' ') for m in masteries[:3])
            body += '\n' + stat(t(gid, 'ui.lol_top'), tops)
        media = []
        try:
            media.append(MediaGalleryItem(
                media=f'https://ddragon.leagueoflegends.com/cdn/14.10.1/img/profileicon/{summ.get("profileIconId", 29)}.png'))
        except Exception:
            pass
        await ctx.reply(view=panel('League of Legends', body, accent=ACCENT['default'],
                                   media_items=media, footer_text='Babka Danka · Riot Games API'),
                        mention_author=False)

    @commands.command(name='cs', description='Statystyki CS2', aliases=['cs2', 'counterstrike'])
    async def cs(self, ctx, *, who: str = ''):
        import aiohttp
        gid = ctx.guild.id
        who = (who or '').strip()
        if not who:
            return await ctx.reply(t(gid, 'gt.cs_usage_ex'), ephemeral=True)
        key = os.getenv('STEAM_API_KEY', '')
        if not key:
            return await ctx.reply(t(gid, 'gt.cs_nokey'), ephemeral=True)
        await ctx.typing()
        steamid = ''.join(c for c in who if c.isdigit())
        async with aiohttp.ClientSession() as s:
            if not steamid or len(steamid) < 10:
                vanity = await _get_json(s, f'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={key}&vanityurl={urllib.parse.quote(who)}')
                if (vanity or {}).get('response', {}).get('success') == 1:
                    steamid = (vanity['response'].get('steamid') or '')
                else:
                    return await ctx.reply(f'Nie znalazłem steam gracza **{who}**.', ephemeral=True)
            summ = await _get_json(s, f'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={key}&steamids={steamid}')
            players = ((summ or {}).get('response') or {}).get('players') or []
            if not players:
                return await ctx.reply(f'Nie znalazłem steam gracza **{who}**.', ephemeral=True)
            p = players[0]
            stats = await _get_json(s, f'https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/?appid=730&key={key}&steamid={steamid}') or {}
            owned = await _get_json(s, f'https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={key}&steamid={steamid}&appids_filter[0]=730') or {}
        sdict = {x.get('name'): x.get('value', 0) for x in ((stats.get('playerstats') or {}).get('stats') or [])}
        kills = sdict.get('total_kills', 0)
        deaths = sdict.get('total_deaths', 0)
        wins = sdict.get('total_matches_won', 0)
        played = sdict.get('total_matches_played', 0)
        shots = sdict.get('total_shots_fired', 0)
        hits = sdict.get('total_shots_hit', 0)
        hs = sdict.get('total_kills_headshot', 0)
        kd = round(kills / max(1, deaths), 2)
        acc = round(100 * hits / max(1, shots), 1)
        hsp = round(100 * hs / max(1, kills), 1)
        wr = round(100 * wins / max(1, played), 1) if played else 0
        hours = 0
        try:
            g = (((owned.get('response') or {}).get('games')) or [{}])[0]
            hours = round((g.get('playtime_forever') or 0) / 60, 1)
        except Exception:
            pass
        body = f"**{p.get('personaname', who)}** — `{steamid}`"
        body += '\n' + stat(t(gid, 'ui.cs_hours'), f'{hours}h w CS2')
        body += '\n' + stat(t(gid, 'ui.cs_matches'), f'{played} meczów ({wr}% WR)')
        body += '\n' + stat(t(gid, 'ui.cs_kd'),
                            f'{kills:,} kills / {deaths:,} deaths (K/D {kd})'.replace(',', ' '))
        body += '\n' + stat(t(gid, 'ui.cs_accuracy'), f'{acc}% • headshoty {hsp}%')
        body += '\n' + stat(t(gid, 'ui.cs_profile'), f'https://steamcommunity.com/profiles/{steamid}/')
        media = []
        try:
            if p.get('avatarfull'):
                media.append(MediaGalleryItem(media=p['avatarfull']))
        except Exception:
            pass
        await ctx.reply(view=panel('Counter-Strike 2', body, accent=ACCENT['default'],
                                   media_items=media, footer_text='Babka Danka · Steam Web API'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(GameTracker(bot))

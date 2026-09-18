"""Babka Pokemon: wild hunts, catching, collection, levels, evolution,
turn-based wild battles, auto PvP duels with wagers, trading, shinies.
Data from PokeAPI (cached in DB). `.pk` for the menu."""
import random
import time

import discord
from discord.ext import commands

import database as db
from lang import t, set_ctx_lang
from utils.cards import short as cshort
from utils.economy import POKE_SHOP_PRICES
from utils.emojis import em, emoji_id

POKEAPI = 'https://pokeapi.co/api/v2'
HUNT_CD = 7  # ~Discord rate-limit floor; spam-friendly hunts
ENC_TTL = 300
SHINY_ODDS = 256
XP_NEXT = staticmethod(lambda lv: lv ** 3)

BALLS = {
    'poke': (POKE_SHOP_PRICES['poke'].amount, 1.0),
    'great': (POKE_SHOP_PRICES['great'].amount, 1.5),
    'ultra': (POKE_SHOP_PRICES['ultra'].amount, 2.0),
    'master': (POKE_SHOP_PRICES['master'].amount, None),
}
POTIONS = {
    'potion': (POKE_SHOP_PRICES['potion'].amount, 0.5),
    'superpotion': (POKE_SHOP_PRICES['superpotion'].amount, 1.0),
}
EGG_CYCLES = 20  # hatch cycles: quantity, not money — stays local
CHECKLIST_NEED = {'catches': 5, 'battles': 3, 'duels': 1}
CHECKLIST_REWARD = 15000
DAILY_TARGET_REWARD = 7500

# NPC ladder: (key, display name, level, team size, prize)
NPCS = [
    ('joey', 'Youngster Joey', 10, 1, 2000),
    ('finn', 'Bug Catcher Finn', 25, 2, 8000),
    ('cyntia', 'Champion Cyntia', 50, 3, 40000),
]


def buddy_get(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT mid, hearts FROM pk_buddy WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    return dict(row) if row else {}


def team_get(gid, uid) -> list:
    """Explicit 3-mon duel team (mon ids), else [active]."""
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT s1, s2, s3 FROM pk_team WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    ids = [row[f's{i}'] for i in (1, 2, 3)] if row else []
    ids = [i for i in ids if i]
    if ids:
        mons = [m for m in my_mons(gid, uid) if m['id'] in ids]
        mons.sort(key=lambda m: ids.index(m['id']))
        if mons:
            return mons
    mons = my_mons(gid, uid)
    act = next((m for m in mons if m['active']), None)
    return [act] if act else []


def daily_row(gid, uid) -> dict:
    import time as _t
    today = int(_t.time()) // 86400
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM pk_daily WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
        if not row or (row['day'] or 0) != today:
            target = 0
            for _ in range(20):
                import random as _r
                d = _r.randint(1, 809)
                r = _dex_row(d)
                if r and not r.get('legendary'):
                    target = d
                    break
            conn.execute('INSERT OR REPLACE INTO pk_daily (guild_id, user_id, day, target, claimed, '
                         'catches, battles, duels, checklist) VALUES (?,?,?,?,0,0,0,0,0)',
                         (str(gid), str(uid), today, target))
            row = conn.execute('SELECT * FROM pk_daily WHERE guild_id=? AND user_id=?',
                               (str(gid), str(uid))).fetchone()
        return dict(row)


def release_value(mon: dict) -> int:
    row = _dex_row(mon['dex'])
    if row.get('legendary'):
        base = 5000
    else:
        base = max(100, 300 - (row.get('rate') or 45) * 2)
    val = base + mon['level'] * 25
    if mon.get('shiny'):
        val *= 5
    return val


def leg_streak_bump(gid, uid, legendary: bool) -> int:
    """Consecutive legendary catches. Returns current streak."""
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_stats (guild_id, user_id, duels_won, duels_lost) '
                     'VALUES (?,?,0,0)', (str(gid), str(uid)))
        if legendary:
            conn.execute('UPDATE pk_stats SET leg_streak=leg_streak+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))
        else:
            conn.execute('UPDATE pk_stats SET leg_streak=0 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))
        row = conn.execute('SELECT leg_streak FROM pk_stats WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
        return (row['leg_streak'] if row else 0) or 0
STARTERS = {'bulbasaur': 1, 'charmander': 4, 'squirtle': 7,
            'pikachu': 25, 'eevee': 133, 'turtwig': 387}
STARTER_TYPES = {1: 'grass', 4: 'fire', 7: 'water',
                 25: 'electric', 133: 'normal', 387: 'grass'}

# Battle forms (megas/primals/crowned): base dex + full override. Stats are
# base values in calc_stats key format (verified against PokeAPI).
FORMS = {
    'mega_rayquaza': {
        'name': 'Mega Rayquaza', 'dex': 384, 'types': ['dragon', 'flying'],
        'stats': {'hp': 105, 'atk': 180, 'dfn': 100, 'spa': 180, 'spd': 100, 'spe': 115},
        'sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/10079.png',
        'shiny_sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/shiny/10079.png',
        'emo': 'form_mega'},
    'primal_groudon': {
        'name': 'Primal Groudon', 'dex': 383, 'types': ['ground', 'fire'],
        'stats': {'hp': 100, 'atk': 180, 'dfn': 160, 'spa': 150, 'spd': 90, 'spe': 90},
        'sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/10078.png',
        'shiny_sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/shiny/10078.png',
        'emo': 'form_primal'},
    'zacian_crowned': {
        'name': 'Zacian Crowned', 'dex': 888, 'types': ['fairy', 'steel'],
        'stats': {'hp': 92, 'atk': 150, 'dfn': 115, 'spa': 80, 'spd': 115, 'spe': 148},
        'sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/10188.png',
        'shiny_sprite': 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/shiny/10188.png',
        'emo': 'form_crowned'},
}


def form_of(mon: dict):
    """FORMS entry for a mon, or None. Pure."""
    if not mon:
        return None
    return FORMS.get((mon.get('form') or '').lower())


def form_emo(gid, mon) -> str:
    """Form marker emoji (name carries the meaning, so no text fallback)."""
    f = form_of(mon)
    return em(gid, f['emo']) if f else ''


def form_sprite(mon: dict, shiny: bool = False):
    """Official-art URL for a formed mon, else None (caller uses pixel art)."""
    f = form_of(mon)
    if not f:
        return None
    return f['shiny_sprite'] if shiny else f['sprite']

# PokeTwo-style regions + quest tracks (catch milestones per region)
REGIONS = {'kanto': (1, 151), 'johto': (152, 251),
           'hoenn': (252, 386), 'sinnoh': (387, 493)}
QUEST_TIERS = [10, 30, 75, 150, 300]
QUEST_REWARDS = [2000, 6000, 15000, 40000, 100000]
REGION_BADGE = {'kanto': 'region_kanto', 'johto': 'region_johto',
                'hoenn': 'region_hoenn', 'sinnoh': 'region_sinnoh'}
# dex milestones: count -> cash
DEX_MILESTONES = {1: 500, 10: 2000, 50: 10000, 100: 50000}

INCENSE_SECONDS = 1800
# NOTE: candy/egg/incense/repel pricing lives in utils.economy
# (POKE_SHOP_PRICES) — do not reintroduce local literals here.
REPEL_SECONDS = 1800


def region_of(dex: int):
    for name, (lo, hi) in REGIONS.items():
        if lo <= dex <= hi:
            return name
    return None


def silhouette_image(sprite_bytes: bytes) -> bytes:
    """Anime-style mystery silhouette: yellow shape on show blue."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    try:
        sp = _Img.open(_io.BytesIO(sprite_bytes)).convert('RGBA')
        sp = sp.resize((360, 360))
        alpha = sp.split()[3].point(lambda a: 255 if a > 20 else 0)
        sil = _Img.new('RGBA', sp.size, (255, 205, 40, 255))
        sil.putalpha(alpha)
        img = _Img.new('RGB', (420, 420), (43, 101, 200))
        img.paste(sil, (30, 30), sil)
        d = _Dr.Draw(img)
        d.text((210, 392), '? ? ?', fill=(255, 255, 255), anchor='mm')
        buf = _io.BytesIO()
        img.save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return None


def incense_active(gid, uid) -> bool:
    import time as _t
    with db.conn_ctx() as conn:
        row = conn.execute("SELECT expires FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='incense'",
                           (str(gid), str(uid))).fetchone()
    return bool(row and row['expires'] and int(row['expires']) > int(_t.time()))


def repel_active(gid, uid) -> bool:
    import time as _t
    with db.conn_ctx() as conn:
        row = conn.execute("SELECT expires FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='repel'",
                           (str(gid), str(uid))).fetchone()
    return bool(row and row['expires'] and int(row['expires']) > int(_t.time()))


def streak_bump(gid, uid, caught: bool):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_stats (guild_id, user_id, duels_won, duels_lost) '
                     'VALUES (?,?,0,0)', (str(gid), str(uid)))
        if caught:
            conn.execute('UPDATE pk_stats SET catch_streak=catch_streak+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))
            conn.execute('UPDATE pk_stats SET best_streak=max(best_streak, catch_streak) '
                         'WHERE guild_id=? AND user_id=?', (str(gid), str(uid)))
        else:
            conn.execute('UPDATE pk_stats SET catch_streak=0 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))

TYPE_CHART = {
    'normal': {'rock': 0.5, 'ghost': 0, 'steel': 0.5},
    'fire': {'fire': 0.5, 'water': 0.5, 'grass': 2, 'ice': 2, 'bug': 2,
             'rock': 0.5, 'dragon': 0.5, 'steel': 2},
    'water': {'fire': 2, 'water': 0.5, 'grass': 0.5, 'ground': 2, 'rock': 2,
              'dragon': 0.5},
    'electric': {'water': 2, 'electric': 0.5, 'grass': 0.5, 'ground': 0,
                 'flying': 2, 'dragon': 0.5},
    'grass': {'fire': 0.5, 'water': 2, 'grass': 0.5, 'poison': 0.5, 'ground': 2,
              'flying': 0.5, 'bug': 0.5, 'rock': 2, 'dragon': 0.5, 'steel': 0.5},
    'ice': {'fire': 0.5, 'water': 0.5, 'grass': 2, 'ice': 0.5, 'ground': 2,
            'flying': 2, 'dragon': 2, 'steel': 0.5},
    'fighting': {'normal': 2, 'ice': 2, 'poison': 0.5, 'flying': 0.5,
                 'psychic': 0.5, 'bug': 0.5, 'rock': 2, 'ghost': 0, 'dark': 2,
                 'steel': 2, 'fairy': 0.5},
    'poison': {'grass': 2, 'poison': 0.5, 'ground': 0.5, 'rock': 0.5,
               'ghost': 0.5, 'steel': 0, 'fairy': 2},
    'ground': {'fire': 2, 'electric': 2, 'grass': 0.5, 'poison': 2,
               'flying': 0, 'bug': 0.5, 'rock': 2, 'steel': 2},
    'flying': {'electric': 0.5, 'grass': 2, 'fighting': 2, 'bug': 2,
               'rock': 0.5, 'steel': 0.5},
    'psychic': {'fighting': 2, 'poison': 2, 'psychic': 0.5, 'dark': 0, 'steel': 0.5},
    'bug': {'fire': 0.5, 'grass': 2, 'fighting': 0.5, 'poison': 0.5,
            'flying': 0.5, 'psychic': 2, 'ghost': 0.5, 'dark': 2, 'steel': 0.5,
            'fairy': 0.5},
    'rock': {'fire': 2, 'ice': 2, 'fighting': 0.5, 'ground': 0.5,
             'flying': 2, 'bug': 2, 'steel': 0.5},
    'ghost': {'normal': 0, 'psychic': 2, 'ghost': 2, 'dark': 0.5},
    'dragon': {'dragon': 2, 'steel': 0.5, 'fairy': 0},
    'dark': {'fighting': 0.5, 'psychic': 2, 'ghost': 2, 'dark': 0.5,
             'fairy': 0.5, 'steel': 0.5},
    'steel': {'fire': 0.5, 'water': 0.5, 'electric': 0.5, 'ice': 2,
              'rock': 2, 'steel': 0.5, 'fairy': 2},
    'fairy': {'fire': 0.5, 'fighting': 2, 'poison': 0.5, 'dragon': 2,
              'dark': 2, 'steel': 0.5},
}


def effectiveness(move_type: str, defender_types: list) -> float:
    mult = 1.0
    for dt in defender_types:
        mult *= TYPE_CHART.get(move_type, {}).get(dt, 1.0)
    return mult


# ---------- PokeAPI data layer (DB cached) ----------

async def _api(session, url):
    try:
        import aiohttp
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


def _dex_row(dex: int) -> dict:
    """Cached species row with types ALWAYS as a list (DB stores JSON text;
    every direct consumer assumed a list and shredded strings into chars)."""
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM pk_dex WHERE dex=?', (dex,)).fetchone()
        if not row:
            return {}
        d = dict(row)
    try:
        import json as _j
        t = d.get('types')
        d['types'] = _j.loads(t) if isinstance(t, str) else (t or [])
    except Exception:
        d['types'] = []
    return d


async def dex_get(session, dex: int) -> dict:
    """Full species data, cached. Fills evolution info on first fetch."""
    row = _dex_row(dex)
    if row and row.get('name'):
        return row
    p = await _api(session, f'{POKEAPI}/pokemon/{dex}')
    if not p:
        return {}
    stats = {s['stat']['name']: s['base_stat'] for s in p.get('stats', [])}
    types = [t['type']['name'] for t in p.get('types', [])]
    spr = ((p.get('sprites') or {}).get('other') or {}).get('official-artwork') or {}
    sprite = spr.get('front_default') or (p.get('sprites') or {}).get('front_default') or ''
    shiny_sprite = spr.get('front_shiny') or (p.get('sprites') or {}).get('front_shiny') or sprite
    rate, legendary, evo_to, evo_level = 45, 0, 0, 0
    sp = await _api(session, f'{POKEAPI}/pokemon-species/{dex}')
    if sp:
        rate = sp.get('capture_rate') or 45
        legendary = 1 if (sp.get('is_legendary') or sp.get('is_mythical')) else 0
        chain_url = (sp.get('evolution_chain') or {}).get('url')
        if chain_url:
            evo_to, evo_level = await _parse_evo(session, chain_url, dex)
    import json as _j
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
                     'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (dex, (p.get('name') or f'???').lower(), _j.dumps(types),
                      stats.get('hp', 50), stats.get('attack', 50), stats.get('defense', 50),
                      stats.get('special-attack', 50), stats.get('special-defense', 50),
                      stats.get('speed', 50), sprite + '|' + shiny_sprite, rate, legendary,
                      evo_to, evo_level))
    row = _dex_row(dex)
    row['types'] = types
    return row


async def _parse_evo(session, chain_url: str, dex: int):
    """Find (evolves_to_dex, min_level) for dex in its chain. 0 level = no auto-evo."""
    chain = await _api(session, chain_url)
    if not chain:
        return 0, 0

    def _id(url):
        try:
            return int((url or '').rstrip('/').split('/')[-1])
        except Exception:
            return 0

    def _walk(node):
        if _id((node.get('species') or {}).get('url')) == dex:
            evos = node.get('evolves_to') or []
            if not evos:
                return 0, 0
            nxt = evos[0]
            lv = 0
            for det in nxt.get('evolution_details') or []:
                if det.get('min_level'):
                    lv = int(det['min_level'])
                    break
            return _id((nxt.get('species') or {}).get('url')), lv
        for child in node.get('evolves_to') or []:
            found = _walk(child)
            if found != (0, 0) or _id((child.get('species') or {}).get('url')) == dex:
                return found
        return 0, 0

    try:
        return _walk(chain.get('chain') or {})
    except Exception:
        return 0, 0


async def move_get(session, name: str) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM pk_moves WHERE name=?', (name,)).fetchone()
        if row:
            return dict(row)
    m = await _api(session, f'{POKEAPI}/move/{name}')
    if not m or not m.get('power'):
        return {}
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_moves (name, power, ptype, acc) VALUES (?,?,?,?)',
                     (name, m['power'], (m.get('type') or {}).get('name', 'normal'),
                      m.get('accuracy') or 100))
    return {'name': name, 'power': m['power'],
            'ptype': (m.get('type') or {}).get('name', 'normal'),
            'acc': m.get('accuracy') or 100}


async def learnset_pool(session, dex: int) -> list:
    """Damaging moves the species can learn, as [name, min_level] pairs —
    cached per species in pk_learnsets (one PokeAPI pass, ever)."""
    import json as _j
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT pool FROM pk_learnsets WHERE dex=?', (dex,)).fetchone()
    if row:
        try:
            pool = _j.loads(row['pool'] or '[]')
            if isinstance(pool, list) and pool:
                return pool
        except Exception:
            pass
    p = await _api(session, f'{POKEAPI}/pokemon/{dex}')
    levelup, others = [], []
    for entry in (p or {}).get('moves', []):
        nm = (entry.get('move') or {}).get('name', '')
        if not nm:
            continue
        best_lv = 0
        for det in entry.get('version_group_details') or []:
            lv = det.get('level_learned_at') or 0
            if ((det.get('move_learn_method') or {}).get('name') == 'level-up'
                    and lv and (not best_lv or lv < best_lv)):
                best_lv = lv
        if best_lv:
            levelup.append((best_lv, nm))
        else:
            others.append(nm)
    levelup.sort(key=lambda x: -x[0])
    pool, seen = [], set()

    async def _add(nm, lv):
        mv = await move_get(session, nm)
        if mv and mv.get('power') and nm not in seen:
            seen.add(nm)
            pool.append([nm, lv])

    for lv, nm in levelup[:10]:
        await _add(nm, lv)
        if len(pool) >= 8:
            break
    if len(pool) < 8:
        for nm in others[:40]:
            await _add(nm, 0)
            if len(pool) >= 10:
                break
    if pool:
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO pk_learnsets (dex, pool) VALUES (?,?)',
                         (dex, _j.dumps(pool)))
    return pool


async def moveset_for(session, dex: int, level: int = 100, seed=None) -> list:
    """This pokemon's 4 damaging moves: species pool (cached), gated to
    learnable-by-`level`, STAB-weighted. `seed` makes the pick stable per
    mon (same species can differ); None = random roll (wild mons)."""
    pool = await learnset_pool(session, dex)
    early = [[nm, lv] for nm, lv in pool if 0 < lv <= level]
    if not early and level >= 15:
        early = [[nm, lv] for nm, lv in pool if lv == 0]
    if not early:
        early = [[nm, lv] for nm, lv in pool if lv > 0] or [['tackle', 1]]
    known = sorted(early, key=lambda x: -x[1])
    learned = []
    for nm, _lv in known[:8]:
        mv = await move_get(session, nm)
        if mv and mv.get('power') and all(mv['name'] != c['name'] for c in learned):
            learned.append(mv)
    learned.sort(key=lambda m: -m['power'])
    moves = list(learned[:4])
    seen_names = {m['name'] for m in moves}
    if len(moves) < 4:
        rng = random.Random(seed)
        types = (_dex_row(dex).get('types') or [])
        rest = []
        for nm, _lv in early:
            if nm in seen_names:
                continue
            mv = await move_get(session, nm)
            if mv and mv.get('power') and all(mv['name'] != c['name'] for c in rest):
                rest.append(mv)
        stab = [m for m in rest if m['ptype'] in types]
        cover = [m for m in rest if m['ptype'] not in types]
        rng.shuffle(stab)
        rng.shuffle(cover)
        for m in stab + cover + rest:
            if m['name'] in seen_names:
                continue
            seen_names.add(m['name'])
            moves.append(m)
            if len(moves) >= 4:
                break
    return moves or [dict(_TACKLE)]


IV_KEYS = ('hp', 'atk', 'dfn', 'spa', 'spd', 'spe')

# canonical natures: (name, boosted stat, cut stat) — 10% each, never HP
NATURES = [
    ('Hardy', None, None), ('Lonely', 'atk', 'dfn'), ('Brave', 'atk', 'spe'),
    ('Adamant', 'atk', 'spa'), ('Naughty', 'atk', 'spd'),
    ('Bold', 'dfn', 'atk'), ('Docile', None, None), ('Relaxed', 'dfn', 'spe'),
    ('Impish', 'dfn', 'spa'), ('Lax', 'dfn', 'spd'),
    ('Timid', 'spe', 'atk'), ('Hasty', 'spe', 'dfn'), ('Serious', None, None),
    ('Jolly', 'spe', 'spa'), ('Naive', 'spe', 'spd'),
    ('Modest', 'spa', 'atk'), ('Mild', 'spa', 'dfn'), ('Quiet', 'spa', 'spe'),
    ('Bashful', None, None), ('Rash', 'spa', 'spd'),
    ('Calm', 'spd', 'atk'), ('Gentle', 'spd', 'dfn'), ('Sassy', 'spd', 'spe'),
    ('Careful', 'spd', 'spa'), ('Quirky', None, None),
]
STAT_LABEL = {'atk': 'Atk', 'dfn': 'Def', 'spa': 'SpA', 'spd': 'SpD', 'spe': 'Spe'}
STAT_EMO = {'hp': 'stat_hp', 'atk': 'stat_atk', 'dfn': 'stat_def',
            'spa': 'stat_spa', 'spd': 'stat_spdef', 'spe': 'stat_speed'}
EEVEE_BRANCHES = ('Vaporeon', 'Jolteon', 'Flareon', 'Espeon', 'Umbreon',
                  'Leafeon', 'Glaceon', 'Sylveon')


def nature_of(mon: dict) -> tuple:
    """Stable nature per mon id. Wilds (no id) roll neutral Hardy. Pure."""
    try:
        mid = int((mon or {}).get('id') or 0)
    except Exception:
        mid = 0
    return NATURES[mid % len(NATURES)]


def _roll_ivs() -> str:
    """Fresh 0-31 IV spread, stored as JSON. Decides how special a mon is."""
    import json as _j
    return _j.dumps({k: random.randint(0, 31) for k in IV_KEYS})


def _ivs_of(mon: dict):
    """Parsed IVs or None (legacy mons predate the system: perfect 31s)."""
    try:
        import json as _j
        ivs = _j.loads((mon or {}).get('ivs') or '')
        if isinstance(ivs, dict) and all(k in ivs for k in IV_KEYS):
            return {k: max(0, min(31, int(ivs[k]))) for k in IV_KEYS}
    except Exception:
        pass
    return None


def _iv_pct(mon: dict) -> int:
    ivs = _ivs_of(mon)
    if not ivs:
        return 100
    return int(round(100 * sum(ivs.values()) / (31 * len(IV_KEYS))))


# ---------- EVs + held items ----------
EV_KEYS = ('hp', 'atk', 'dfn', 'spa', 'spd', 'spe')  # mirrors IV_KEYS order
EV_MAX_STAT = 252   # per-stat cap
EV_MAX_TOTAL = 510  # overall cap
EV_LABEL = {'hp': 'HP', 'atk': 'Atk', 'dfn': 'Def', 'spa': 'SpA', 'spd': 'SpD', 'spe': 'Spe'}
POWER_EV = 4        # bonus EVs a power item adds per won battle

# Held items: key -> record. 'effect' drives the battle hooks:
#   heal (Leftovers) / dmg (Life Orb) / survive (Focus Band) / xp (Lucky Egg)
#   / ev (power item: +POWER_EV into record['stat'] per won battle).
HELD_ITEMS = {
    'leftovers': {'name': 'Leftovers', 'price': POKE_SHOP_PRICES['leftovers'].amount, 'icon': 'potion', 'effect': 'heal'},
    'lifeorb': {'name': 'Life Orb', 'price': POKE_SHOP_PRICES['lifeorb'].amount, 'icon': 'rate_up', 'effect': 'dmg'},
    'focusband': {'name': 'Focus Band', 'price': POKE_SHOP_PRICES['focusband'].amount, 'icon': 'shield', 'effect': 'survive'},
    'luckyegg': {'name': 'Lucky Egg', 'price': POKE_SHOP_PRICES['luckyegg'].amount, 'icon': 'egg', 'effect': 'xp'},
    'pow_hp': {'name': 'Power Weight', 'price': POKE_SHOP_PRICES['pow_hp'].amount, 'icon': 'stat_hp',
               'effect': 'ev', 'stat': 'hp'},
    'pow_atk': {'name': 'Power Bracer', 'price': POKE_SHOP_PRICES['pow_atk'].amount, 'icon': 'stat_atk',
                'effect': 'ev', 'stat': 'atk'},
    'pow_dfn': {'name': 'Power Belt', 'price': POKE_SHOP_PRICES['pow_dfn'].amount, 'icon': 'stat_def',
                'effect': 'ev', 'stat': 'dfn'},
    'pow_spa': {'name': 'Power Lens', 'price': POKE_SHOP_PRICES['pow_spa'].amount, 'icon': 'stat_spa',
                'effect': 'ev', 'stat': 'spa'},
    'pow_spd': {'name': 'Power Band', 'price': POKE_SHOP_PRICES['pow_spd'].amount, 'icon': 'stat_spdef',
                'effect': 'ev', 'stat': 'spd'},
    'pow_spe': {'name': 'Power Anklet', 'price': POKE_SHOP_PRICES['pow_spe'].amount, 'icon': 'stat_speed',
                'effect': 'ev', 'stat': 'spe'},
}
HELD_ORDER = ('leftovers', 'lifeorb', 'focusband', 'luckyegg',
              'pow_hp', 'pow_atk', 'pow_dfn', 'pow_spa', 'pow_spd', 'pow_spe')
HELD_BY_NAME = {v['name'].lower(): k for k, v in HELD_ITEMS.items()}

# Evolution stones — Eevee branching + generic stone evos
# price 12k, icon fleet name mirrors key
EVO_STONES = {
    'water_stone':   {'name': 'Water Stone',   'price': POKE_SHOP_PRICES['water_stone'].amount, 'icon': 'water_stone',   'mons': {133: 134}},
    'thunder_stone': {'name': 'Thunder Stone', 'price': POKE_SHOP_PRICES['thunder_stone'].amount, 'icon': 'thunder_stone', 'mons': {133: 135}},
    'fire_stone':    {'name': 'Fire Stone',    'price': POKE_SHOP_PRICES['fire_stone'].amount, 'icon': 'fire_stone',    'mons': {133: 136}},
    'sun_stone':     {'name': 'Sun Stone',     'price': POKE_SHOP_PRICES['sun_stone'].amount, 'icon': 'sun_stone',     'mons': {133: 196}},
    'moon_stone':    {'name': 'Moon Stone',    'price': POKE_SHOP_PRICES['moon_stone'].amount, 'icon': 'moon_stone',    'mons': {133: 197}},
    'leaf_stone':    {'name': 'Leaf Stone',    'price': POKE_SHOP_PRICES['leaf_stone'].amount, 'icon': 'leaf_stone',    'mons': {133: 470}},
    'ice_stone':     {'name': 'Ice Stone',     'price': POKE_SHOP_PRICES['ice_stone'].amount, 'icon': 'ice_stone',     'mons': {133: 471}},
    'shiny_stone':   {'name': 'Shiny Stone',   'price': POKE_SHOP_PRICES['shiny_stone'].amount, 'icon': 'shiny_stone',   'mons': {133: 700}},
}
EEVEE_STONES = {k: v['mons'][133] for k, v in EVO_STONES.items()}  # stone -> dex
EVO_STONE_BY_NAME = {v['name'].lower(): k for k, v in EVO_STONES.items()}
# allow "water stone" and "water_stone"
for _k, _v in list(EVO_STONES.items()):
    EVO_STONE_BY_NAME[_k.replace('_', ' ')] = _k
STONE_ORDER = tuple(EVO_STONES.keys())

# Extra shop items as per spec (Balls/Items/Buddy)
SHOP_EXTRAS = {
    'amuletcoin':    {'name': 'Amulet Coin',    'price': POKE_SHOP_PRICES['amuletcoin'].amount, 'icon': 'coin'},
    'questscroll':   {'name': 'Quest Scroll',   'price': POKE_SHOP_PRICES['questscroll'].amount, 'icon': 'quest_scroll'},
    'repel':         {'name': 'Repel',          'price': POKE_SHOP_PRICES['repel'].amount, 'icon': 'item_repel'},
    'shinycharm':    {'name': 'Shiny Charm',    'price': POKE_SHOP_PRICES['shinycharm'].amount, 'icon': 'star'},
    'lootbox':       {'name': 'Lootbox',        'price': POKE_SHOP_PRICES['lootbox'].amount, 'icon': 'box_box'},
    'wailmer_pail':  {'name': 'Wailmer Pail',   'price': POKE_SHOP_PRICES['wailmer_pail'].amount, 'icon': 'potion'},
    'expshare':      {'name': 'EXP Share',      'price': POKE_SHOP_PRICES['expshare'].amount, 'icon': 'candy'},
    'evolutionstone':{'name': 'Evolution Stone','price': POKE_SHOP_PRICES['evolutionstone'].amount, 'icon': 'evo_burst'},
    'mega_bracelet': {'name': 'Mega Bracelet',  'price': POKE_SHOP_PRICES['mega_bracelet'].amount, 'icon': 'mega_bracelet'},
}
SHOP_EXTRA_ORDER = ('amuletcoin','questscroll','repel','shinycharm','lootbox','wailmer_pail')
SHOP_BUDDY_ORDER = ('expshare','evolutionstone','mega_bracelet')


def _evs_parse(raw) -> dict:
    """EV text -> full stat dict, clamped. Junk/legacy reads as all zeros."""
    import json as _j
    try:
        d = _j.loads(raw or '')
        if isinstance(d, dict):
            return {k: max(0, min(EV_MAX_STAT, int(d.get(k) or 0))) for k in EV_KEYS}
    except Exception:
        pass
    return {k: 0 for k in EV_KEYS}


def _evs_of(mon: dict) -> dict:
    """Parsed EVs of a mon (missing/legacy value = untrained)."""
    return _evs_parse((mon or {}).get('evs') if isinstance(mon, dict) else '')


def _ev_total(mon: dict) -> int:
    return sum(_evs_of(mon).values())


def _ev_top(mon: dict):
    """(stat, ev) of the biggest trained stat, or ('', 0) when untrained."""
    evs = _evs_of(mon)
    stat = max(EV_KEYS, key=lambda k: evs[k])
    return (stat, evs[stat]) if evs[stat] else ('', 0)


def _ev_yield(row: dict, legendary=None) -> dict:
    """EVs a beaten species trains: 1 into each of its best base stats, 2 for
    legends. Derived from base stats so already-cached dex rows work too."""
    row = row or {}
    bases = {k: int(row.get(k) or 50) for k in EV_KEYS}
    top = max(bases.values())
    leg = row.get('legendary') if legendary is None else legendary
    pts = 2 if leg else 1
    return {k: pts for k, v in bases.items() if v == top}


def _ev_apply(cur: dict, gains: dict) -> tuple:
    """Cap-aware EV math (252/stat, 510 total). Pure: (applied, new_evs)."""
    out = {k: max(0, min(EV_MAX_STAT, int((cur or {}).get(k) or 0))) for k in EV_KEYS}
    total = sum(out.values())
    applied = {}
    for k, n in (gains or {}).items():
        if k not in EV_KEYS:
            continue
        room = min(EV_MAX_STAT - out[k], EV_MAX_TOTAL - total)
        add = min(int(n or 0), max(0, room))
        if add <= 0:
            continue
        out[k] += add
        total += add
        applied[k] = add
    return applied, out


def evs_add(gid, mid: int, gains: dict) -> dict:
    """Cap-aware EV add for one mon. Returns {'applied', 'total', 'evs'}."""
    import json as _j
    if not mid:
        return {'applied': {}, 'total': 0, 'evs': {}}
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT evs FROM pk_mons WHERE id=? AND guild_id=?',
                           (mid, str(gid))).fetchone()
    if not row:
        return {'applied': {}, 'total': 0, 'evs': {}}
    applied, new = _ev_apply(_evs_parse(row['evs']), gains)
    if applied:
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET evs=? WHERE id=? AND guild_id=?',
                         (_j.dumps(new), mid, str(gid)))
    return {'applied': applied, 'total': sum(new.values()), 'evs': new}


def held_key(mon) -> str:
    return ((mon or {}).get('held') or '') if isinstance(mon, dict) else ''


def held_of(mon) -> dict:
    """Held-item record of a mon ({} when it holds nothing)."""
    return HELD_ITEMS.get(held_key(mon), {})


def held_label(gid, key: str) -> str:
    """`[icon]Name` for a held-item key ('' when unknown/empty)."""
    it = HELD_ITEMS.get(key) or {}
    if not it:
        return ''
    ic = em(gid, it.get('icon', ''))
    return f"{ic + ' ' if ic else ''}{it['name']}"


def held_mark(gid, mon) -> str:
    """` [icon]Name` marker for cards and battle lines ('' when bare)."""
    lab = held_label(gid, held_key(mon))
    return f' [{lab}]' if lab else ''


def _held_line(gid, mon) -> str:
    """Mon-card held line: the item, or the empty-slot phrasing."""
    lab = held_label(gid, held_key(mon))
    return t(gid, 'eco.pk_held_line', item=lab) if lab else t(gid, 'eco.pk_held_none')


def _ev_line(gid, mon) -> str:
    """`EV 132/510 · Spe 96` summary for the mon card."""
    stat, ev = _ev_top(mon)
    if not stat:
        return t(gid, 'eco.pk_ev_top_none')
    return t(gid, 'eco.pk_ev_line', total=_ev_total(mon), max=EV_MAX_TOTAL,
             top=f'{EV_LABEL.get(stat, stat)} {ev}/{EV_MAX_STAT}')


def held_strike(att: dict, dfn: dict, dmg: int) -> int:
    """Attacker's Life Orb boost, then the defender's Focus Band save."""
    if held_of(att).get('effect') == 'dmg':
        dmg = int(dmg * 1.3)
    if dmg >= dfn.get('hp', 0) and held_of(dfn).get('effect') == 'survive' \
            and not dfn.get('fb_used'):
        dfn['fb_used'] = True
        dmg = max(0, dfn.get('hp', 0) - 1)
    return dmg


def held_xp(mon: dict, gain: int) -> int:
    """Lucky Egg: +50% battle XP."""
    return int(gain * 1.5) if held_of(mon).get('effect') == 'xp' else gain


def _held_heal(gid, mon: dict, log: list) -> None:
    """Leftovers: 1/16 max HP at the end of every battle turn."""
    if held_of(mon).get('effect') != 'heal' or mon.get('hp', 0) <= 0:
        return
    mx = mon['stats']['maxhp']
    if mon['hp'] >= mx:
        return
    heal = max(1, mx // 16)
    before = mon['hp']
    mon['hp'] = min(mx, mon['hp'] + heal)
    log.append(t(gid, 'eco.pk_held_heal', name=mon['name'], hp=mon['hp'] - before))


def evs_win(gid, mid: int, row: dict, mon: dict) -> list:
    """EVs for a won battle: the beaten species' yield + a power item's bonus."""
    if not mid:
        return []
    gains = _ev_yield(row)
    it = held_of(mon)
    if it.get('effect') == 'ev' and it.get('stat'):
        gains[it['stat']] = gains.get(it['stat'], 0) + POWER_EV
    res = evs_add(gid, mid, gains)
    lines = []
    for k, n in sorted((res.get('applied') or {}).items(), key=lambda kv: -kv[1]):
        lines.append(t(gid, 'eco.pk_ev_gain', n=n, stat=EV_LABEL.get(k, k),
                       total=res.get('total', 0), max=EV_MAX_TOTAL))
    return lines


def calc_stats(base: dict, level: int, ivs: dict = None, nature: tuple = None,
               evs: dict = None) -> dict:
    """Base stats at level (+IVs, +EV/4; nature swings two stats 10%, never HP)."""
    ivs, evs = ivs or {}, evs or {}

    def _ev(k):
        return evs.get(k, 0) // 4

    def _s(b, k):
        return (2 * b + ivs.get(k, 31) + _ev(k)) * level // 100 + 5
    out = {'maxhp': (2 * base.get('hp', 50) + ivs.get('hp', 31) + _ev('hp')) * level // 100 + level + 10,
           'atk': _s(base.get('atk', 50), 'atk'), 'dfn': _s(base.get('dfn', 50), 'dfn'),
           'spa': _s(base.get('spa', 50), 'spa'), 'spd': _s(base.get('spd', 50), 'spd'),
           'spe': _s(base.get('spe', 50), 'spe')}
    if nature and nature[1] and nature[2]:
        out[nature[1]] = int(out[nature[1]] * 1.1)
        out[nature[2]] = int(out[nature[2]] * 0.9)
    return out


def damage(att_level: int, move: dict, atk_stats: dict, dfn_stats: dict,
           att_types: list, dfn_types: list, weather=None) -> tuple:
    """Returns (damage, crit). Misses deal 0. Rain/sun boost water/fire."""
    a = atk_stats['atk'] if atk_stats['atk'] >= atk_stats['spa'] else atk_stats['spa']
    d = dfn_stats['dfn'] if atk_stats['atk'] >= atk_stats['spa'] else dfn_stats['spd']
    if random.random() * 100 > (move.get('acc') or 100):
        return 0, False
    crit = random.random() < 0.0625
    stab = 1.5 if move.get('ptype') in att_types else 1.0
    eff = effectiveness(move.get('ptype', 'normal'), dfn_types)
    wmult = 1.0
    if weather == 'rain' and move.get('ptype') == 'water':
        wmult = 1.2
    elif weather == 'sun' and move.get('ptype') == 'fire':
        wmult = 1.2
    base = ((2 * att_level / 5 + 2) * (move.get('power') or 40) * max(1, a) / max(1, d)) / 50 + 2
    dmg = base * stab * eff * wmult * random.uniform(0.85, 1.0) * (1.5 if crit else 1.0)
    return max(1, int(dmg)), crit


WEATHER_LINE = {
    'rain': 'eco.pk_wx_rain',
    'sun': 'eco.pk_wx_sun',
}


def roll_weather():
    r = random.random()
    if r < 0.25:
        return 'rain'
    if r < 0.5:
        return 'sun'
    return None


def hp_dot(frac: float, fainted: bool = False, gid=0) -> str:
    if fainted:
        return em(gid, 'hp_empty') or 'x'
    if frac > 0.5:
        return em(gid, 'hp_full') or 'O'
    if frac > 0.2:
        return em(gid, 'hp_mid') or '-'
    return em(gid, 'hp_low') or '!'


def _weather_line(gid, weather: str) -> str:
    """Weather line with custom icon; plain text when unresolvable."""
    key = WEATHER_LINE.get(weather, '')
    text = t(gid, key) if key else ''
    if not text:
        return ''
    emo = em(gid, 'weather_rain' if weather == 'rain' else 'weather_sun')
    return f'{emo} {text}' if emo else text


def _hit_tag(gid, eff: float, crit: bool, dmg: int = 1) -> str:
    """Battle log markers. Render-only: math and behavior unchanged."""
    if dmg == 0:
        return ' ' + (em(gid, 'hit_miss') or 'MISS')
    tag = ''
    if eff > 1:
        tag += ' ' + (em(gid, 'hit_super') or '!')
    elif eff < 1:
        tag += ' ' + (em(gid, 'shield') or '=')
    if crit:
        tag += ' ' + (em(gid, 'hit_crit') or 'CRIT')
    return tag


def catch_chance(rate: int, level: int, hp_frac: float, ball_mult) -> float:
    if ball_mult is None:
        return 1.0
    p = (rate / 255) * ball_mult * (1.6 - 1.1 * max(0.0, min(1.0, hp_frac)))
    p *= max(0.25, 1 - level / 150)
    return max(0.05, min(0.98, p))


# ---------- collection helpers ----------

def my_mons(gid, uid) -> list:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT * FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id',
                            (str(gid), str(uid))).fetchall()
    return [dict(r) for r in rows]


def get_mon(gid, uid, slot: int) -> dict:
    mons = my_mons(gid, uid)
    if 1 <= slot <= len(mons):
        return mons[slot - 1]
    return {}


def mon_name(mon: dict, gid=0) -> str:
    if not mon:
        return '?'
    form = form_of(mon)
    row = _dex_row(mon['dex'])
    base = (form['name'] if form else (row.get('name') or f"#{mon['dex']}").capitalize())
    name = (mon.get('nick') or base)
    if mon.get('shiny'):
        name = (em(gid, 'rarity_shiny') or '*') + name
    if form:
        mark = form_emo(gid, mon)
        name = (mark + ' ' + name) if mark else name
    return name


def balls_get(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT ball, qty FROM pk_balls WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
    out = {b: 0 for b in BALLS}
    for r in rows:
        if r['ball'] in out:
            out[r['ball']] = r['qty'] or 0
    return out


def balls_add(gid, uid, ball: str, n: int):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) VALUES (?,?,?,0)',
                     (str(gid), str(uid), ball))
        conn.execute('UPDATE pk_balls SET qty=qty+? WHERE guild_id=? AND user_id=? AND ball=?',
                     (n, str(gid), str(uid), ball))


def balls_take(gid, uid, ball: str) -> bool:
    # Atomic: single guarded UPDATE so a fast double-click can't take twice
    # (the old SELECT-then-UPDATE raced to qty=-1).
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) VALUES (?,?,?,0)',
                     (str(gid), str(uid), ball))
        cur = conn.execute('UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball=? AND qty>0',
                           (str(gid), str(uid), ball))
        return (cur.rowcount or 0) > 0


def best_ball(gid, uid):
    """Nicest ball the user actually owns (master first)."""
    b = balls_get(gid, uid)
    for name in ('master', 'ultra', 'great', 'poke'):
        if b.get(name, 0) > 0:
            return name
    return ''


def potions_get(gid, uid) -> dict:
    out = {p: 0 for p in POTIONS}
    with db.conn_ctx() as conn:
        for p in POTIONS:
            row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                               (str(gid), str(uid), p)).fetchone()
            out[p] = (row['qty'] if row else 0) or 0
    return out


def held_bag(gid, uid) -> dict:
    """Owned held-item counts, one key per catalog entry."""
    out = {k: 0 for k in HELD_ORDER}
    with db.conn_ctx() as conn:
        for k in HELD_ORDER:
            row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                               (str(gid), str(uid), k)).fetchone()
            out[k] = (row['qty'] if row else 0) or 0
    return out


async def fetch_sprite(session, url: str):
    try:
        import aiohttp
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.read()
    except Exception:
        pass
    return None


def pix_url(dex: int, shiny: bool = False, back: bool = False) -> str:
    """Classic pixel sprites, no API call needed."""
    base = 'https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon'
    if back:
        return f'{base}/back/{"shiny/" if shiny else ""}{dex}.png'
    return f'{base}/{"shiny/" if shiny else ""}{dex}.png'


TYPE_EMOJI = {'fire': 'fire', 'water': 'water', 'grass': 'leaf',
              'electric': 'bolt', 'ice': 'snow', 'fighting': 'fist',
              'normal': 'type_normal', 'poison': 'type_poison',
              'ground': 'type_ground', 'flying': 'type_flying',
              'psychic': 'type_psychic', 'bug': 'type_bug',
              'rock': 'rock',
              'ghost': 'type_ghost', 'dragon': 'type_dragon',
              'dark': 'type_dark', 'steel': 'type_steel',
              'fairy': 'type_fairy'}


def types_str(gid, types: list) -> str:
    """Types with custom emoji where deployed, plain text fallback."""
    out = []
    for t in (types or ['?']):
        e = em(gid, TYPE_EMOJI.get(t, ''))
        out.append(f'{e} {t}' if e else t)
    return '/'.join(out)


def move_str(gid, mv: dict) -> str:
    e = em(gid, TYPE_EMOJI.get(mv.get('ptype', ''), ''))
    base = f"{mv['name']}({mv['power']})"
    return f'{e} {base}' if e else base


def _mv_name(gid, mv: dict) -> str:
    """Move name with type badge for battle logs."""
    te = em(gid, TYPE_EMOJI.get((mv or {}).get('ptype', ''), ''))
    return f"{te + ' ' if te else ''}{(mv or {}).get('name', '?')}"


# (key, fleet_emoji, unicode_fallback, accent) — PokeMeow-style rarity.
# Fleet emoji win when deployed via .emojisetup, unicode keeps old servers working.
RARITY_COMMON = ('common', 'rarity_common', '⚪', 0x58CC02)
RARITY_UNCOMMON = ('uncommon', 'rarity_uncommon', '🔵', 0x3498DB)
RARITY_RARE = ('rare', 'rarity_rare', '🟣', 0x9B59B6)
RARITY_LEGENDARY = ('legendary', 'rarity_legendary', '🔶', 0xFFD700)
RARITY_SHINY = ('shiny', 'rarity_shiny', '✨', 0xFF6FB5)


def rarity_of(dex_row: dict, shiny: bool = False, gid=0) -> tuple:
    """(key, emoji, accent). Shiny overrides; legendaries gold;
    the rest split by capture rate (lower = rarer)."""
    if shiny:
        entry = RARITY_SHINY
    else:
        row = dex_row or {}
        if row.get('legendary'):
            entry = RARITY_LEGENDARY
        else:
            try:
                rate = int(row.get('rate') or 255)
            except Exception:
                rate = 255
            if rate <= 45:
                entry = RARITY_RARE
            elif rate <= 120:
                entry = RARITY_UNCOMMON
            else:
                entry = RARITY_COMMON
    key, name, fb, accent = entry
    return key, (em(gid, name) if gid else None) or fb, accent


def _btn_emoji(gid, name):
    """PartialEmoji for a fleet emoji, else None (button is sent bare).
    IDs come only from the deployed registry — never fabricated."""
    try:
        eid = emoji_id(name, gid)
        if eid:
            return discord.PartialEmoji(name=name, id=eid)
    except Exception:
        pass
    return None


def _move_emoji_name(mv: dict) -> str:
    """Fleet emoji name for a move's type badge, or '' when unsupported."""
    return TYPE_EMOJI.get((mv or {}).get('ptype', ''), '')


def _switch_emoji_name(cur: bool, dead: bool = False) -> str:
    return 'slot_active' if cur else ('faint_dot' if dead else 'switch_dot')


def _picker_emoji_name(m: dict, active: bool) -> str:
    """Pre-battle picker icon: active marker, else the mon's first type."""
    if active:
        return 'slot_active'
    try:
        types = _dex_row(m.get('dex', 0)).get('types') or []
    except Exception:
        types = []
    return TYPE_EMOJI.get(types[0], '') if types else ''


# Encounter shares under the real hunt algorithm (300k-trial sim, dex 1-493).
RARITY_RATE = {'common': 30, 'uncommon': 29, 'rare': 39, 'legendary': 2}
TIER_WORD = {
    'en': {'common': 'Common', 'uncommon': 'Uncommon', 'rare': 'Rare', 'legendary': 'Legendary'},
    'pl': {'common': 'Zwykły', 'uncommon': 'Niecodzienny', 'rare': 'Rzadki', 'legendary': 'Legendarny'},
}
CATCH_COINS_LVL, CATCH_COINS_NEW, CATCH_COINS_SHINY = 10, 250, 1000
BALL_FLEET = {'poke': 'pokeball', 'great': 'greatball', 'ultra': 'ultraball', 'master': 'masterball'}


FORM_SUFFIX = ('-mega', '-megax', '-megay', '-primal', '-crowned',
               '-galar', '-alola', '-hisui', '-paldea', '-therian', '-origin')


def showdown_gif(name: str, shiny: bool = False) -> str:
    """Animated sprite URL. '' when unmappable (caller falls back).
    Forme sprites keep hyphens (rayquaza-mega); base species strip all
    punctuation (mr-mime -> mrmime, ho-oh -> hooh)."""
    low = (name or '').lower()
    if any(low.endswith(s) for s in FORM_SUFFIX):
        slug = ''.join(c for c in low if c.isascii() and (c.isalnum() or c == '-'))
    else:
        slug = ''.join(c for c in low if c.isascii() and c.isalnum())
    if not slug:
        return ''
    base = 'https://play.pokemonshowdown.com/sprites'
    return f"{base}/{'ani-shiny' if shiny else 'ani'}/{slug}.gif"


def streak_get(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT catch_streak, best_streak FROM pk_stats WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    if not row:
        return {'catch_streak': 0, 'best_streak': 0}
    return {'catch_streak': row['catch_streak'] or 0, 'best_streak': row['best_streak'] or 0}


def _tier_word(gid, rk: str) -> str:
    try:
        from lang import get_lang
        lang = get_lang(gid)
    except Exception:
        lang = 'en'
    return TIER_WORD.get(lang, TIER_WORD['en']).get(rk, rk.title())


def _rarity_line(gid, row: dict, shiny: bool) -> str:
    rk, _, _ = rarity_of(row, False)
    _, re, _ = rarity_of(row, shiny, gid)
    pre = 'Shiny ' if shiny else ''
    return t(gid, 'eco.pk_rarity_line', emo=re, tier=f'{pre}{_tier_word(gid, rk)}',
             pct=RARITY_RATE.get(rk, 0))


def _balls_left_line(gid, uid) -> str:
    b = balls_get(gid, uid)
    row1 = f"Pokeballs: {b.get('poke',0)}  •  Greatballs: {b.get('great',0)}"
    row2 = f"Ultraballs: {b.get('ultra',0)}  •  Masterballs: {b.get('master',0)}"
    return f"===== Balls left =====\n{row1}\n{row2}"


BOX_SORTS = ['rarity', 'level', 'dex', 'new']
_BOX_RANK = {'shiny': 0, 'legendary': 1, 'rare': 2, 'uncommon': 3, 'common': 4}
DEFAULT_AVATAR = 'https://cdn.discordapp.com/embed/avatars/0.png'


def _box_tier(mon: dict, row: dict) -> str:
    if mon.get('shiny'):
        return 'shiny'
    return rarity_of(row, False)[0]


def _box_match(mon: dict, row: dict, filt: str) -> bool:
    """Row filter for ;box. Unknown filters are lenient (no filtering)."""
    f = (filt or '').lower().strip()
    if not f or f == 'all':
        return True
    if f in ('fav', 'favs', 'favorites', 'favourite', 'favourites'):
        return bool(mon.get('fav'))
    if f == 'shiny':
        return bool(mon.get('shiny'))
    if f in ('kanto', 'johto', 'hoenn', 'sinnoh'):
        return region_of(mon.get('dex', 0)) == f
    if f in ('common', 'uncommon', 'rare', 'legendary'):
        return rarity_of(row, False)[0] == f
    types = [x.lower() for x in ((row or {}).get('types') or [])]
    if f in types:
        return True
    if f in TYPE_EMOJI:
        return False  # a real type that simply doesn't match
    return True


def _box_slots(mons: list) -> dict:
    """Stable ;mon/;active slot per mon id (box order may be sorted)."""
    return {m['id']: i + 1 for i, m in enumerate(mons)}


def _sec_row(gid, text: str, dex=None):
    """Species row: Section with art thumbnail when the png ships, plain
    TextDisplay otherwise (Section demands an accessory). Returns
    (component, File|None) — caller attaches files to the send."""
    from pathlib import Path
    from discord.ui import Section, TextDisplay, Thumbnail
    import discord
    txt = TextDisplay(text)
    try:
        did = int(dex or 0)
        art = Path(f'assets/emojis/p{did:03d}.png') if 1 <= did <= 493 else None
    except Exception:
        art = None
    if art is not None and art.is_file():
        try:
            thumb = Thumbnail(media=f'attachment://{art.name}')
            return Section(txt, accessory=thumb), discord.File(str(art), filename=art.name)
        except Exception:
            pass
    return txt, None


_TIER_LETTER = {'common': ('letter_c', 'C'), 'uncommon': ('letter_u', 'U'),
                'rare': ('letter_r', 'R'), 'legendary': ('letter_l', 'L')}


def _tier_letter(gid, tier: str) -> str:
    """Rarity letter badge, [X] fallback."""
    name, fb = _TIER_LETTER.get(tier, ('', '?'))
    return em(gid, name, f'[{fb}]') if name else f'[{fb}]'


def _match_mon(mons: list, species_of, query: str):
    """Find a mon by nickname or species name. Returns (mon|None, n_matches).
    Tier order: nick exact, species exact, nick prefix, species prefix,
    nick contains, species contains. Pure."""
    q = (query or '').lower().strip()
    if not q:
        return None, 0
    names = [(m, (m.get('nick') or '').lower(), (species_of(m.get('dex', 0)) or '').lower())
             for m in mons]
    for tier in range(6):
        hits = []
        for m, nick, spec in names:
            if tier == 0 and nick and nick == q:
                hits.append(m)
            elif tier == 1 and spec and spec == q:
                hits.append(m)
            elif tier == 2 and nick and nick.startswith(q):
                hits.append(m)
            elif tier == 3 and spec and spec.startswith(q):
                hits.append(m)
            elif tier == 4 and nick and q in nick:
                hits.append(m)
            elif tier == 5 and spec and q in spec:
                hits.append(m)
        if hits:
            return hits[0], len(hits)
    return None, 0


def _evo_text(gid, row: dict) -> str:
    """Honest evolution line mirroring the evolve command rules."""
    to = (row or {}).get('evo_to') or 0
    if not to:
        return t(gid, 'eco.pk_evo_max')
    nm = (_dex_row(to).get('name') or f'#{to}').capitalize()
    lv = (row or {}).get('evo_level') or 0
    if not lv:
        return t(gid, 'eco.pk_evo_stone', name=nm)
    return t(gid, 'eco.pk_evo_at', name=nm, level=lv)


def _box_cid(viewer, owner, action: str, pg: int, mode: str, filt: str) -> str:
    """Box button id. The action is part of the id so FIRST/LAST can never
    collide with BACK/NEXT landing on the same page (Discord 400s those)."""
    return f'pkbox:{viewer}:{owner}:{action}:{pg}:{mode}:{filt or "-"}'


def _box_parse(cid: str):
    try:
        _, viewer, owner, action, pg, mode, filt = cid.split(':', 6)
        return int(viewer), int(owner), action, int(pg or 1), mode, ('' if filt == '-' else filt)
    except Exception:
        return None


def _species_emoji_name(dex) -> str:
    """Fleet name for a species mini (p001-p809 + p888), or '' out of range."""
    try:
        d = int(dex)
        if 1 <= d <= 809 or d == 888:
            return f'p{d:03d}'
        return ''
    except Exception:
        return ''


def _species_btn_emoji(gid, dex):
    """Species face for buttons; None when unresolvable (label carries the name)."""
    name = _species_emoji_name(dex)
    return _btn_emoji(gid, name) if name else None


def _box_stacks(entries: list) -> dict:
    """Group box entries by species (+shiny): key (dex, shiny) -> [(mon, row)].
    Insertion order preserved — the 'new' baseline. Pure."""
    stacks: dict = {}
    for m, r in entries:
        stacks.setdefault((m.get('dex', 0), bool(m.get('shiny'))), []).append((m, r))
    return stacks


def _box_sort_stacks(stacks: dict, mode: str) -> list:
    """Sorted (key, group) pairs. rarity = tier, top level, dex; level = best
    level desc; dex = species asc (shiny first); new = newest catch desc. Pure."""
    items = list(stacks.items())

    def top(group):
        return max(m['level'] for m, _ in group)

    def newest(group):
        return max(m['id'] for m, _ in group)

    if mode == 'level':
        items.sort(key=lambda kv: (-top(kv[1]), kv[0][0]))
    elif mode == 'dex':
        items.sort(key=lambda kv: (kv[0][0], 0 if kv[0][1] else 1))
    elif mode == 'new':
        items.sort(key=lambda kv: -newest(kv[1]))
    else:
        items.sort(key=lambda kv: (_BOX_RANK[_box_tier(kv[1][0][0], kv[1][0][1])],
                                   -top(kv[1]), kv[0][0]))
    return items


def xp_bar(xp: int, nxt: int, gid=0, width: int = 12) -> str:
    frac = max(0.0, min(1.0, (xp or 0) / max(1, nxt or 1)))
    fill = int(frac * width)
    full = em(gid, 'xp_full') or '#'
    empty = em(gid, 'xp_empty') or '-'
    return full * fill + empty * (width - fill)


def _sky_grass(d, rnd, W, H, weather, horizon=300):
    """Bright daytime Pokemon terrain shared by battle + hunt cards."""
    if weather == 'rain':
        top, bot = (105, 128, 160), (172, 192, 210)
    elif weather == 'sun':
        top, bot = (120, 185, 240), (255, 224, 170)
    else:
        top, bot = (110, 182, 232), (200, 233, 250)
    for y in range(horizon):
        tt = y / max(1, horizon)
        d.line([(0, y), (W, y)],
               fill=(int(top[0] + (bot[0] - top[0]) * tt),
                     int(top[1] + (bot[1] - top[1]) * tt),
                     int(top[2] + (bot[2] - top[2]) * tt)))
    # sun with glow
    sun_c = (255, 236, 170) if weather != 'rain' else (215, 220, 230)
    d.ellipse([748, 14, 842, 108], fill=(255, 244, 200, 90))
    d.ellipse([762, 28, 828, 94], fill=sun_c)
    # puffy white clouds
    for cx, cy, w in ((150, 66, 130), (430, 42, 100), (650, 116, 84)):
        for ox, oy, r in ((-w // 3, 4, 22), (0, -6, 28), (w // 3, 4, 22)):
            d.ellipse([cx + ox - r, cy + oy - r // 2, cx + ox + r, cy + oy + r // 2],
                      fill=(255, 255, 255))
            d.ellipse([cx + ox - r, cy + oy, cx + ox + r, cy + oy + r // 2],
                      fill=(225, 235, 245))
    # distant soft hills for depth
    d.ellipse([-140, horizon - 70, 340, horizon + 40], fill=(140, 205, 140))
    d.ellipse([560, horizon - 88, 1040, horizon + 40], fill=(130, 198, 135))
    # vibrant grass
    for y in range(horizon, H):
        tt = (y - horizon) / max(1, (H - horizon))
        d.line([(0, y), (W, y)],
               fill=(int(104 - 34 * tt), int(192 - 28 * tt), int(104 - 26 * tt)))
    d.line([(0, horizon), (W, horizon)], fill=(86, 170, 88), width=3)
    for _ in range(52):
        x, y = rnd.randint(6, W - 6), rnd.randint(horizon + 6, H - 8)
        d.line([(x, y), (x + rnd.choice([-3, 3]), y - rnd.randint(5, 10))],
               fill=(52, 138, 60), width=2)
    for _ in range(10):  # tiny meadow flowers
        x, y = rnd.randint(10, W - 10), rnd.randint(horizon + 10, H - 10)
        d.ellipse([x - 3, y - 3, x + 3, y + 3],
                  fill=rnd.choice([(255, 255, 255), (255, 235, 150), (255, 200, 210)]))
    if weather == 'rain':
        for _ in range(70):
            x, y = rnd.randint(0, W), rnd.randint(0, horizon)
            d.line([(x, y), (x - 5, y + 12)], fill=(150, 175, 210), width=2)


def _platform(d, x0, y0, x1, y1):
    """Sandy game-style battle platform with shadow + highlight."""
    d.ellipse([x0, y0 + 8, x1, y1 + 8], fill=(52, 120, 58))
    d.ellipse([x0, y0, x1, y1], fill=(236, 214, 168), outline=(172, 142, 100), width=3)
    d.ellipse([x0 + 14, y0 + 5, x1 - 14, y1 - 8], fill=(246, 230, 188))


def _plain_name(name: str) -> str:
    """Strip <:emoji:id> tokens for PIL text (Discord never parses those)."""
    import re as _re
    return _re.sub(r'<:[A-Za-z0-9_]+:\d+>', '', str(name or '')).strip()


_TACKLE = {'name': 'tackle', 'power': 35, 'ptype': 'normal', 'acc': 100}


def _safe_moves(mon: dict) -> list:
    """Never-empty moveset (empty learnsets used to IndexError mid-turn,
    killing the interaction with no message)."""
    mv = (mon or {}).get('moves') or []
    return mv if mv else [dict(_TACKLE)]


def _turn_safe(fn):
    """Turn armor: log the traceback, tell the player, keep battle state.
    Silent interaction deaths used to strand battles with no winner."""
    import functools as _ft
    import traceback as _tb

    @_ft.wraps(fn)
    async def wrapper(self, ix, *args, **kwargs):
        try:
            return await fn(self, ix, *args, **kwargs)
        except Exception as e:
            gid = getattr(getattr(ix, 'guild', None), 'id', 0)
            print(f'[pkturn] {fn.__name__} failed: {type(e).__name__}: {e}')
            _tb.print_exc()
            try:
                await ix.followup.send(t(gid, 'eco.pk_turn_broke'), ephemeral=True)
            except Exception:
                try:
                    await ix.response.send_message(t(gid, 'eco.pk_turn_broke'), ephemeral=True)
                except Exception:
                        pass
    return wrapper


def _turn_lock(fn):
    """One turn at a time per battle/duel: a double-tapped button resolves
    once; the stale tap lands on the already-updated card and is ignored
    (the button callback already deferred, so silence is correct here).
    Stack OUTSIDE @_turn_safe so the flag always releases."""
    import functools as _ft

    @_ft.wraps(fn)
    async def wrapper(self, ix, gid, *args, **kwargs):
        key = kwargs.get('key')
        if key is None:
            for a in args:
                # duel key: tuple of str ids. Switch/move payloads like
                # ('switch', 123) fail the all-str check and are skipped.
                if isinstance(a, tuple) and len(a) >= 2 \
                        and all(isinstance(x, str) for x in a):
                    key = a
                    break
        if key is None and args:
            try:
                key = (str(gid), str(int(getattr(args[0], 'id', args[0]))))
            except Exception:
                key = None
        st = self._battle.get(key) if key is not None else None
        if st is None:
            return await fn(self, ix, gid, *args, **kwargs)
        if st.get('busy') or st.get('done') or st.get('starting'):
            return
        st['busy'] = True
        try:
            return await fn(self, ix, gid, *args, **kwargs)
        finally:
            if self._battle.get(key) is st:
                st['busy'] = False
    return wrapper


ARENAS = ('meadow', 'forest', 'cave')


def _arena_forest(d, rnd, W, H):
    """Dark enchanted forest: deep teal canopy, fireflies, mossy ground."""
    for y in range(290):
        tt = y / 290
        top = (16, 42, 54)
        bot = (34, 84, 78)
        d.line([(0, y), (W, y)],
               fill=(int(top[0] + (bot[0] - top[0]) * tt),
                     int(top[1] + (bot[1] - top[1]) * tt),
                     int(top[2] + (bot[2] - top[2]) * tt)))
    for _ in range(9):  # canopy blobs
        x, w = rnd.randint(0, W), rnd.randint(90, 200)
        d.ellipse([x - w, -40, x + w, 150], fill=(22, 66, 52))
    for _ in range(5):
        x = rnd.randint(0, W)
        d.line([(x, 150), (x, 300)], fill=(28, 52, 44), width=10)
    for _ in range(26):  # fireflies
        x, y = rnd.randint(0, W), rnd.randint(20, 300)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(190, 255, 150))
    for y in range(290, H):
        tt = (y - 290) / (H - 290)
        d.line([(0, y), (W, y)],
               fill=(int(44 - 12 * tt), int(104 - 26 * tt), int(62 - 12 * tt)))
    d.line([(0, 290), (W, 290)], fill=(30, 80, 44), width=3)
    for _ in range(40):
        x, y = rnd.randint(6, W - 6), rnd.randint(296, H - 8)
        d.line([(x, y), (x + rnd.choice([-3, 3]), y - rnd.randint(5, 10))],
               fill=(30, 90, 44), width=2)


def _arena_cave(d, rnd, W, H):
    """Ember cave: violet dark, glowing crystals, stalactites, ash floor."""
    for y in range(300):
        tt = y / 300
        top = (24, 18, 44)
        bot = (64, 44, 96)
        d.line([(0, y), (W, y)],
               fill=(int(top[0] + (bot[0] - top[0]) * tt),
                     int(top[1] + (bot[1] - top[1]) * tt),
                     int(top[2] + (bot[2] - top[2]) * tt)))
    for _ in range(7):  # stalactites
        x, w, h = rnd.randint(0, W), rnd.randint(24, 60), rnd.randint(50, 130)
        d.polygon([(x - w // 2, 0), (x + w // 2, 0), (x, h)], fill=(46, 34, 72))
    for _ in range(8):  # glowing crystals
        x, y = rnd.randint(20, W - 20), rnd.randint(200, 320)
        s = rnd.randint(8, 18)
        col = rnd.choice([(120, 220, 255), (255, 150, 220), (255, 200, 120)])
        d.polygon([(x, y - s), (x + s // 2, y), (x, y + s), (x - s // 2, y)], fill=col)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 255))
    for y in range(300, H):
        tt = (y - 300) / (H - 300)
        d.line([(0, y), (W, y)],
               fill=(int(52 - 10 * tt), int(46 - 8 * tt), int(72 - 10 * tt)))
    d.line([(0, 300), (W, 300)], fill=(40, 34, 56), width=3)


def battle_image(p1_img: bytes, p2_img: bytes, p1: dict, p2: dict,
                 weather=None, arena: str = 'meadow') -> bytes:
    """Game-style battle: meadow / dark forest / ember cave arenas, sandy
    platforms, grounded sprites (contact shadows), cream status boxes."""
    import io as _io
    import random as _r
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, H = 900, 420
    if arena not in ARENAS:
        arena = 'meadow'
    seed = (p1.get('name', '') + p2.get('name', '') + arena)
    rnd = _r.Random(sum(map(ord, seed)) if seed else 7)
    img = _Img.new('RGB', (W, H), (110, 182, 232))
    d = _Dr.Draw(img)
    if arena == 'forest':
        _arena_forest(d, rnd, W, H)
    elif arena == 'cave':
        _arena_cave(d, rnd, W, H)
    else:
        _sky_grass(d, rnd, W, H, weather)
    _platform(d, 40, 292, 400, 320)
    _platform(d, 500, 200, 860, 228)
    if weather == 'rain':
        for _ in range(30):
            x, y = rnd.randint(0, W), rnd.randint(300, H)
            d.line([(x, y), (x - 4, y + 10)], fill=(140, 165, 200), width=2)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_mid = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 20)
        f_hp = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 17)
    except Exception:
        f_mid = f_hp = _F.load_default()

    def _paste(raw, x, y, s, shadow):
        try:
            sp = _Img.open(_io.BytesIO(raw)).convert('RGBA').resize((s, s), _Img.NEAREST)
            canvas = img.convert('RGBA')
            canvas.paste(sp, (x, y), sp)
            img.paste(canvas.convert('RGB'))
            d2 = _Dr.Draw(img)
            sx, sw = x + 20, s - 40  # contact shadow grounds the sprite
            d2.ellipse([sx, shadow, sx + sw, shadow + 16], fill=(20, 30, 22))
            canvas2 = img.convert('RGBA')
            canvas2.paste(sp, (x, y), sp)
            img.paste(canvas2.convert('RGB'))
            return True
        except Exception:
            return False

    if p1_img:
        _paste(p1_img, 105, 87, 205, 294)
    else:
        d.ellipse([110, 96, 310, 296], outline=(90, 90, 98), width=3)
    if p2_img:
        _paste(p2_img, 600, 9, 195, 202)
    else:
        d.ellipse([600, 8, 790, 198], outline=(90, 90, 98), width=3)

    def _statusbox(x, y, name, level, frac, hp_txt):
        bw, bh = 300, 80
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=10, fill=(250, 246, 230),
                            outline=(122, 92, 62), width=2)
        d.text((x + 14, y + 8), f'{_plain_name(name)[:15]}  Lv{level}', font=f_mid, fill=(48, 40, 32))
        bx, by, bw2 = x + 14, y + 38, bw - 28
        d.rounded_rectangle([bx, by, bx + bw2, by + 14], radius=7, fill=(200, 190, 170))
        fw = max(12, int(bw2 * max(0.0, min(1.0, frac))))
        col = (87, 200, 110) if frac > 0.5 else ((240, 190, 60) if frac > 0.2 else (235, 90, 90))
        d.rounded_rectangle([bx, by, bx + fw, by + 14], radius=7, fill=col)
        try:
            w = d.textlength(hp_txt, font=f_hp)
            d.text((x + bw - w - 12, y + 56), hp_txt, font=f_hp, fill=(80, 70, 60))
        except Exception:
            pass

    _statusbox(560, 316, p2['name'], p2['level'],
               p2['hp'] / max(1, p2['stats']['maxhp']),
               f"{p2['hp']}/{p2['stats']['maxhp']}")
    _statusbox(40, 316, p1['name'], p1['level'],
               p1['hp'] / max(1, p1['stats']['maxhp']),
               f"{p1['hp']}/{p1['stats']['maxhp']}")
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def wild_image(sprite_bytes: bytes = None, weather=None, mystery: bool = False,
               arena: str = 'meadow', bare: bool = False) -> bytes:
    """Encounter scene: meadow / dark forest / ember cave arena, platform,
    big centered sprite (or anime-yellow mystery silhouette). bare=True
    renders landscape only (sprite comes from the gallery GIF)."""
    import io as _io
    import random as _r
    from PIL import Image as _Img, ImageDraw as _Dr
    W, H = 900, 420
    if arena not in ARENAS:
        arena = 'meadow'
    rnd = _r.Random(11 if not mystery else 99)
    img = _Img.new('RGB', (W, H), (110, 182, 232))
    d = _Dr.Draw(img)
    if arena == 'forest':
        _arena_forest(d, rnd, W, H)
    elif arena == 'cave':
        _arena_cave(d, rnd, W, H)
    else:
        _sky_grass(d, rnd, W, H, weather)
    _platform(d, 250, 250, 650, 292)
    try:
        if sprite_bytes and not bare:
            sp = _Img.open(_io.BytesIO(sprite_bytes)).convert('RGBA')
            sp = sp.resize((300, 300), _Img.NEAREST)
            if mystery:
                alpha = sp.split()[3].point(lambda a: 255 if a > 20 else 0)
                sil = _Img.new('RGBA', sp.size, (255, 205, 40, 255))
                sil.putalpha(alpha)
                sp = sil
            canvas = img.convert('RGBA')
            canvas.paste(sp, (300, -10), sp)
            img.paste(canvas.convert('RGB'))
            d = _Dr.Draw(img)
    except Exception:
        pass
    # tall foreground grass so the mon sits IN the meadow
    for _ in range(40):
        x = rnd.randint(0, W)
        y = rnd.randint(H - 70, H - 6)
        h = rnd.randint(18, 42)
        d.line([(x, y), (x - 4, y - h)], fill=(46, 128, 54), width=3)
        d.line([(x + 6, y), (x + 10, y - h + 8)], fill=(60, 150, 66), width=3)
    if mystery:
        try:
            from PIL import ImageFont as _F
            from pathlib import Path as _P
            _a = _P(__file__).parent.parent / 'assets'
            f = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 44)
        except Exception:
            f = None
        d.text((W // 2, H - 34), '? ? ?', fill=(255, 255, 255), anchor='mm', font=f,
               stroke_width=2, stroke_fill=(46, 90, 50))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class Pokemon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._hunt_cd = {}
        self._enc = {}    # (gid, uid) -> encounter dict
        self._battle = {}  # (gid, uid) -> battle state
        self._chatxp_cd = {}
        self._spawn_count = {}  # gid -> messages since last wild spawn
        self._wild = {}   # (gid, cid) -> shared channel encounter
        self._box_sort = {}  # (gid, viewer) -> sort mode

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        """PokeTwo-style chat XP: the active mon grows as you talk (1/min)."""
        if not message.guild or message.author.bot:
            return
        try:
            import database as _db
            pfx = _db.get_prefix(message.guild.id) or '.'
        except Exception:
            pfx = '.'
        content = message.content or ''
        if content.startswith(pfx) or content.startswith(';'):
            return
        key = (str(message.guild.id), str(message.author.id))
        if int(time.time()) - self._chatxp_cd.get(key, 0) < 60:
            return
        self._chatxp_cd[key] = int(time.time())
        try:
            mons = my_mons(message.guild.id, message.author.id)
            act = next((m for m in mons if m['active']), None)
            if not act:
                return
            msgs = await self._gain_xp(message.guild.id, message.author.id,
                                       act['id'], random.randint(8, 20))
            if msgs:
                await message.channel.send('\n'.join(msgs))
        except Exception:
            pass
        # PokeTwo core loop: chat activity spawns a shared wild mon
        try:
            await self._maybe_autospawn(message)
        except Exception:
            pass

    async def _maybe_autospawn(self, message: discord.Message):
        gid = str(message.guild.id)
        cid = str(message.channel.id)
        if (gid, cid) in self._wild:
            if self._wild[(gid, cid)].get('exp', 0) < int(time.time()):
                self._wild.pop((gid, cid), None)
            else:
                return
        n = self._spawn_count.get(gid, 0) + 1
        if n < 25:
            self._spawn_count[gid] = n
            return
        self._spawn_count[gid] = 0
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 809)
                row = await dex_get(s, dex)
                if not row:
                    continue
                r = random.random()
                is_leg = bool(row.get('legendary'))
                if (is_leg and r < 0.15) or (not is_leg and r < 0.9):
                    break
            else:
                return
            level = random.randint(5, 40)
            shiny = random.randint(1, SHINY_ODDS) == 1
            stats = calc_stats(row, level)
            self._wild[(gid, cid)] = {'dex': dex, 'level': level, 'shiny': shiny,
                                      'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                                      'exp': int(time.time()) + ENC_TTL}
            raw = await fetch_sprite(s, pix_url(dex, shiny))
            import io as _bio
            try:
                png = wild_image(raw, weather=None, mystery=True)
                view = self._encounter_layout(
                    message.guild.id, t(message.guild.id, 'eco.pk_wild_title', level=level),
                    t(message.guild.id, 'eco.pk_autospawn',
                      types=types_str(gid, row["types"])), extra=['attachment://hunt.png'])
                await message.channel.send(
                    view=view, file=discord.File(_bio.BytesIO(png), 'hunt.png'))
            except Exception:
                self._wild.pop((gid, cid), None)

    def _get_wild(self, gid, cid):
        key = (str(gid), str(cid))
        e = self._wild.get(key)
        if not e or e.get('exp', 0) < int(time.time()):
            self._wild.pop(key, None)
            return None
        return e

    # ----- shared presentation -----

    def _layout(self, gid, title, desc, img=None, accent: int = 0xFFFFFF):
        from cogs.gamble import _game_layout
        return _game_layout(title, desc, img, accent)

    async def _mage(self, gid, title, desc, sprite: str, accent: int = 0xFFFFFF):
        """Message layout with remote sprite image (or no image)."""
        if sprite:
            return self._layout(gid, title, desc, sprite, accent)
        return self._layout(gid, title, desc, None, accent)

    def _encounter_layout(self, gid, title, desc, accent: int = 0x58CC02, extra=None):
        """PokeMeow-style encounter card: bold banner, description block,
        sprite media (remote URL or attachment://), footer. Encounter
        buttons are attached to the container afterwards."""
        from discord.ui import LayoutView, Container, TextDisplay, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        from cogs.gamble import foot
        layout = LayoutView(timeout=300)
        box = Container(accent_color=accent)
        box.add_item(TextDisplay(f'**{title}**\n{desc}'))
        items = [MediaGalleryItem(media=m) for m in (extra or []) if m]
        if items:
            box.add_item(MediaGallery(*items))
        try:
            box.add_item(TextDisplay(f'-# {foot()}'))
        except Exception:
            pass
        layout.add_item(box)
        return layout

    # ----- starters / collection -----


    async def _grant_starter(self, dest, gid, user, dex):
        """Insert starter (active) + set as buddy + balls + cards.
        dest is a commands.Context or (deferred) Interaction."""
        import aiohttp
        is_ix = isinstance(dest, discord.Interaction)

        async def _say(view=None, ephemeral=False):
            if is_ix:
                return await dest.followup.send(view=view, ephemeral=ephemeral)
            if ephemeral:
                try:
                    return await dest.reply(view=view, ephemeral=True)
                except Exception:
                    return await dest.reply(view=view)
            return await dest.reply(view=view, mention_author=False)

        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, dex)
        if not row:
            return await _say(view=self._layout(gid, t(gid, 'eco.pk_starter_title'),
                                                t(gid, 'eco.pk_api')), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,0,0,"",1,?, '')', (str(gid), str(user.id), dex, 5, _roll_ivs()))
            mid = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id',
                               (str(gid), str(user.id))).fetchone()
            if mid:
                conn.execute('INSERT OR REPLACE INTO pk_buddy (guild_id, user_id, mid, hearts) '
                             'VALUES (?,?,?,COALESCE((SELECT hearts FROM pk_buddy WHERE guild_id=? AND user_id=?),0))',
                             (str(gid), str(user.id), mid['id'], str(gid), str(user.id)))
        balls_add(gid, user.id, 'poke', 10)
        spr = (row.get('sprite') or '').split('|')[0]
        rk, re, accent = rarity_of(row, False, gid)
        await _say(view=await self._mage(
            gid, f'{re} ' + t(gid, 'eco.pk_starter_title'),
            t(gid, 'eco.pk_starter', name=row['name'].capitalize())
            + '\n' + _rarity_line(gid, row, False), spr, accent))
        await _say(view=self._layout(gid, t(gid, 'eco.pk_guide_title'),
                                     t(gid, 'eco.pk_guide_body',
                                       name=row['name'].capitalize())),
                   ephemeral=True)

    def _mk_starter_btn(self, gid, uid, dex: int):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            if my_mons(gid, ix.user.id):
                return await ix.response.send_message(t(gid, 'eco.pk_has'), ephemeral=True)
            await ix.response.defer()
            pick = dex or random.choice(list(STARTERS.values()))
            await self._grant_starter(ix, gid, ix.user, pick)
            try:
                row = _dex_row(pick)
                await ix.edit_original_response(
                    content=t(gid, 'eco.pk_starter',
                              name=(row.get('name') or f'#{pick}').capitalize()),
                    view=None)
            except Exception:
                pass
        return _cb

    @commands.command(name='starter', description='Wybierz startera')
    async def starter(self, ctx, name: str = ''):
        gid = ctx.guild.id
        if my_mons(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'eco.pk_has'), ephemeral=True)
        pick = (name or '').lower().strip()
        if pick:
            if pick not in STARTERS:
                return await ctx.reply(t(gid, 'eco.pk_starters'), ephemeral=True)
            return await self._grant_starter(ctx, gid, ctx.author, STARTERS[pick])
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow
        layout = LayoutView(timeout=60)
        box = Container(accent_color=0x58CC02)
        box.add_item(TextDisplay(f"## {t(gid, 'eco.pk_pick_title')}\n{t(gid, 'eco.pk_pick_body')}"))
        order = ['bulbasaur', 'charmander', 'squirtle', 'pikachu', 'eevee', 'turtwig']
        row = None
        for i, key in enumerate(order):
            if i % 3 == 0:
                row = ActionRow()
                box.add_item(row)
            dex = STARTERS[key]
            spe = _species_btn_emoji(gid, dex)
            b = discord.ui.Button(label=key.capitalize()[:80], style=discord.ButtonStyle.secondary,
                                  custom_id=f'pkstart:{ctx.author.id}:{dex}',
                                  emoji=spe if spe else _btn_emoji(gid, 'pokeball'))
            b.callback = self._mk_starter_btn(gid, ctx.author.id, dex)
            row.add_item(b)
        rrow = ActionRow()
        box.add_item(rrow)
        rb = discord.ui.Button(label='RANDOM', style=discord.ButtonStyle.primary,
                               custom_id=f'pkstart:{ctx.author.id}:0')
        rb.callback = self._mk_starter_btn(gid, ctx.author.id, 0)
        rrow.add_item(rb)
        layout.add_item(box)
        await ctx.reply(view=layout, mention_author=False)

    @commands.command(name='hunt', description='Poluj i walcz z dzikimi')
    async def hunt(self, ctx):
        """Daily hunt — casual quest. Auto-assigned each day, no choosing."""
        import random as _rnd
        gid = ctx.guild.id
        today = int(time.time()) // 86400
        with db.conn_ctx() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS pk_hunt_daily (guild_id TEXT, user_id TEXT, target INT, day INT, PRIMARY KEY (guild_id, user_id))')
            h = conn.execute('SELECT target, day FROM pk_hunt_daily WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id))).fetchone()
            # also fetch shiny streak for display (from pk_hunt)
            sh = conn.execute('SELECT streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                              (str(gid), str(ctx.author.id))).fetchone()
            streak = (sh['streak'] if sh else 0) or 0
            if not h or not h['target'] or (h['day'] or 0) != today:
                rnd = random.Random(f"{gid}:{ctx.author.id}:{today}")
                dex = rnd.randint(1, 809)
                row_try = _dex_row(dex)
                if row_try and row_try.get('legendary') and rnd.random() < 0.7:
                    dex = rnd.randint(1, 809)
                conn.execute('INSERT OR REPLACE INTO pk_hunt_daily (guild_id, user_id, target, day) VALUES (?,?,?,?)',
                             (str(gid), str(ctx.author.id), dex, today))
                h = {'target': dex, 'day': today}
            else:
                h = dict(h)
        row = _dex_row(h['target'])
        rk, re, _ = rarity_of(row, False, gid)
        spe = em(gid, _species_emoji_name(h['target'])) or ''
        tgt_emo = em(gid, 'hunt_target') or ''
        tname = (row.get('name') or '#' + str(h['target'])).capitalize()
        title = f"{tgt_emo + ' ' if tgt_emo else ''}Your hunt today is {re} {spe + ' ' if spe else ''}{tname}!"
        streak_txt = (f"Streak: **{streak}**\n"
                      f"When you do `;p` you have a chance of finding this specific Pokemon!\n"
                      f"-# Check `;help hunt` for streak bonuses. Resets tomorrow.")
        return await ctx.reply(view=self._layout(gid, title, streak_txt), ephemeral=True)

    @commands.command(name='p', description='Spotkaj dzikiego (tylko łapanie)')
    async def poke_encounter(self, ctx):
        """Catch-only encounters: balls, no FIGHT row. Also handles hunt target chance."""
        return await self._hunt_core(ctx, 'catch')

    async def _hunt_core(self, ctx, mode: str):
        import aiohttp
        gid = ctx.guild.id
        key = (str(gid), str(ctx.author.id))
        if not my_mons(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        wait = HUNT_CD - (int(time.time()) - self._hunt_cd.get(key, 0))
        if wait > 0:
            return await ctx.reply(t(gid, 'eco.pk_wait', s=wait), ephemeral=True)
        self._hunt_cd[key] = int(time.time())
        await ctx.typing()
        inc = incense_active(gid, ctx.author.id)
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 809)
                row = await dex_get(s, dex)
                if not row:
                    continue
                r = random.random()
                is_leg = bool(row.get('legendary'))
                leg_odds = 0.5 if inc else 0.25
                if (is_leg and r < leg_odds) or (not is_leg and r < 0.9):
                    break
            else:
                return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
            # daily hunt (casual) + shiny hunt 15% chance each in ;p
            try:
                with db.conn_ctx() as conn:
                    dd = conn.execute('SELECT target FROM pk_hunt_daily WHERE guild_id=? AND user_id=?',
                                      (str(gid), str(ctx.author.id))).fetchone()
                    ht_daily = (dd['target'] or 0) if dd else 0
                    if ht_daily and random.random() < 0.15:
                        trow = await dex_get(s, ht_daily)
                        if trow:
                            dex, row = ht_daily, trow
                    else:
                        hh = conn.execute('SELECT target FROM pk_hunt WHERE guild_id=? AND user_id=?',
                                          (str(gid), str(ctx.author.id))).fetchone()
                        ht = (hh['target'] or 0) if hh else 0
                        if ht and random.random() < 0.15:
                            trow = await dex_get(s, ht)
                            if trow:
                                dex, row = ht, trow
            except Exception:
                pass
        mons = my_mons(gid, ctx.author.id)
        avg_lv = sum(m['level'] for m in mons) / max(1, len(mons))
        level = max(3, min(70, int(random.gauss(avg_lv, 6))))
        repelled = repel_active(gid, ctx.author.id)
        if repelled:
            level = max(3, min(100, int(level * 1.5)))
        # shiny odds: base 1/256, hunt target chains it down to 1/32, incense rolls twice
        with db.conn_ctx() as conn:
            h = conn.execute('SELECT target, streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id))).fetchone()
        target = (h['target'] or 0) if h else 0
        streak = (h['streak'] or 0) if h else 0
        denom = max(32, SHINY_ODDS - streak * 4) if target == dex else SHINY_ODDS
        shiny = random.randint(1, denom) == 1 or (inc and random.randint(1, denom) == 1)
        stats = calc_stats(row, level)
        mystery = random.random() < 0.35
        arena = random.choice(ARENAS)
        self._enc[key] = {'dex': dex, 'level': level, 'shiny': shiny,
                          'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                          'mystery': mystery, 'mode': mode, 'arena': arena,
                          'exp': int(time.time()) + ENC_TTL}
        pix = pix_url(dex, shiny)
        spe_emo = em(gid, _species_emoji_name(dex))
        if mystery:
            found = t(gid, 'eco.pk_found_mystery', user=ctx.author.display_name)
            wild_line = (t(gid, 'eco.pk_mystery', types=types_str(gid, row["types"])) +
                         ((f"\n{em(gid, 'item_incense') or '+'} " + t(gid, 'eco.pk_incensed')) if inc else ''))
        else:
            disp = ((em(gid, 'rarity_shiny') or '*') + ' ' if shiny else '') + row['name'].capitalize()
            found = t(gid, 'eco.pk_found', user=ctx.author.display_name, emo=spe_emo, name=disp)
            flags = ''
            if inc:
                flags += f"\n{em(gid, 'item_incense') or '+'} " + t(gid, 'eco.pk_incensed')
            if repelled:
                flags += f"\n{em(gid, 'item_repel') or '-'} " + t(gid, 'eco.pk_repelled')
            if mode == 'catch':
                wild_line = (t(gid, 'eco.pk_wild_catch', name=disp,
                               types=types_str(gid, row["types"])) + flags)
            else:
                rate_emo = em(gid, 'rate_up')
                wild_line = (t(gid, 'eco.pk_wild', name=disp,
                               types=types_str(gid, row["types"]),
                               hint=t(gid, 'eco.pk_wild_hint')) + flags
                             + '\n' + (rate_emo + ' ' if rate_emo else '')
                             + t(gid, 'eco.pk_odds', pct=int(catch_chance(
                                 row.get('rate', 45), level, 1.0, BALLS['ultra'][1]) * 100)))
        slot_of = {m['id']: i + 1 for i, m in enumerate(mons)}
        act = next((m for m in mons if m.get('active')), mons[0])
        if mode == 'fight':
            hint = (f"FIGHT: {mon_name(act, gid)} Lv{act['level']} — "
                    f"`;active {slot_of.get(act['id'], 1)}` / `;battle {slot_of.get(act['id'], 1)}` to change")
        else:
            hint = "`;catch <ball>` or tap a ball below — one throw!"
        st = streak_get(gid, ctx.author.id)
        flame = em(gid, 'streak_flame')
        streak = (flame + ' ' if flame else '') + t(gid, 'eco.pk_streak_line',
                                                    n=st['catch_streak'], b=st['best_streak'])
        bcounts = balls_get(gid, ctx.author.id)
        balls_block = (f"===== Balls left =====\n"
                       f"Pokeballs: {bcounts.get('poke',0)}  •  Greatballs: {bcounts.get('great',0)}\n"
                       f"Ultraballs: {bcounts.get('ultra',0)}  •  Masterballs: {bcounts.get('master',0)}")
        wild_emo = em(gid, 'catch_reticle' if mystery else 'encounter_wild')
        title = (wild_emo + ' ' if wild_emo else '') + t(gid, 'eco.pk_wild_title', level=level)
        rarity = ''
        tgt_line = ''
        is_daily = False
        try:
            with db.conn_ctx() as conn:
                dd = conn.execute('SELECT target FROM pk_hunt_daily WHERE guild_id=? AND user_id=?',
                                  (str(gid), str(ctx.author.id))).fetchone()
                is_daily = bool(dd and (dd['target'] or 0) == dex)
        except Exception:
            pass
        if mystery:
            accent = 0x3A3F4B
        else:
            rk, re_, accent = rarity_of(row, shiny, gid)
            rarity = _rarity_line(gid, row, shiny) + '\n'
            if (target and target == dex) or is_daily:
                tgt_emo = em(gid, 'hunt_target')
                tgt_line = f"\n{tgt_emo + ' ' if tgt_emo else ''}TARGET"
        desc = (f'{found}\n{rarity}{wild_line}{tgt_line}\n'
                f'-# {hint}\n{streak}\n{balls_block}')
        gif = '' if mystery else showdown_gif(row.get('name', ''), shiny)
        if gif:
            try:
                to = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=to) as s3:
                    async with s3.head(gif) as r:
                        if r.status != 200:
                            gif = ''
            except Exception:
                gif = ''
        media = ([gif] if gif else []) or ([pix] if not mystery else [])
        view = self._encounter_layout(gid, title, desc, accent, media)
        self._attach_enc_buttons(view, gid, ctx.author.id, mode, is_hunt=bool(((target and target == dex) or is_daily) and not mystery))
        # Catch-mode resolves by EDITING this message (one throw, win or gone):
        # remember it, and strip the dead buttons off any encounter this replaces
        # (stale buttons would otherwise throw at the NEW encounter).
        old = self._enc.get(key)
        sent = await ctx.reply(view=view, mention_author=False)
        try:
            if old and old.get('mid'):
                # Strip wherever the old card lives (possibly another
                # channel): stale buttons must never outlive their encounter.
                try:
                    och = None
                    try:
                        och = ctx.guild.get_channel(int(old.get('cid') or 0))
                    except Exception:
                        och = None
                    prev = await (och or ctx.channel).fetch_message(int(old['mid']))
                    await prev.edit(view=None)
                except Exception:
                    pass
            cur = self._enc.get(key)
            if cur is not None and getattr(sent, 'id', None):
                cur['mid'] = sent.id
                cur['cid'] = ctx.channel.id
        except Exception:
            pass

    def _attach_enc_buttons(self, view, gid, uid, mode: str = 'fight', is_hunt: bool = False):
        from discord.ui import ActionRow
        row = ActionRow()
        fight = discord.ui.Button(label='FIGHT', style=discord.ButtonStyle.danger,
                                  custom_id=f'pkfight:{uid}',
                                  emoji=_btn_emoji(gid, 'btn_fight'))

        async def _fight(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            mons = my_mons(gid, ix.user.id)
            if len(mons) > 1:
                return await ix.followup.send(
                    view=self._fighter_picker(gid, ix.user.id), ephemeral=True)
            await self._start_battle(ix, gid, ix.user)

        fight.callback = _fight
        # p is catch-only, but hunt target via p can also be fought (spec)
        if mode == 'fight' or (mode == 'catch' and is_hunt):
            row.add_item(fight)
        counts = balls_get(gid, uid)
        for ball in ('poke', 'great', 'ultra', 'master'):
            qty = counts.get(ball, 0)
            bb = discord.ui.Button(label='',
                                   style=discord.ButtonStyle.success if qty
                                   else discord.ButtonStyle.secondary,
                                   custom_id=f'pkball:{uid}:{ball}',
                                   disabled=(qty <= 0),
                                   emoji=_btn_emoji(gid, BALL_FLEET[ball]))
            bb.callback = self._mk_enc_ball(gid, uid, ball)
            row.add_item(bb)
        for child in view.children:
            if type(child).__name__ == 'Container':
                child.add_item(row)
                return
        view.add_item(row)

    def _mk_enc_ball(self, gid, uid, ball: str):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            try:
                await ix.response.defer()
            except Exception as ex:
                print(f'[pkthrow] defer failed: {type(ex).__name__}: {ex}')
                try:
                    return await ix.response.send_message(
                        t(gid, 'eco.pk_turn_broke'), ephemeral=True)
                except Exception:
                    return
            try:
                await self._throw(ix, gid, ix.user, ball)
            except Exception as ex:
                # Last-resort net: surfacing THIS is more useful than a
                # traceback nobody watches. State was already consumed.
                print(f'[pkthrow] _throw crashed: {type(ex).__name__}: {ex}')
                try:
                    await ix.followup.send(
                        f'{t(gid, "eco.pk_turn_broke")} (`{type(ex).__name__}`)',
                        ephemeral=True)
                except Exception:
                    pass
        return _cb

    def _get_enc(self, gid, uid):
        key = (str(gid), str(uid))
        e = self._enc.get(key)
        if not e or e.get('exp', 0) < int(time.time()):
            self._enc.pop(key, None)
            return None
        return e

    async def _edit_enc_message(self, ctx, gid, e, layout) -> bool:
        """Edit the stored encounter message (catch-mode resolves in place).
        Returns False when the message is unknown/gone — caller falls back
        to a fresh reply."""
        try:
            mid, cid = e.get('mid'), e.get('cid')
            if not mid or int(cid or 0) != int(ctx.channel.id):
                return False
            msg = await ctx.channel.fetch_message(int(mid))
            await msg.edit(view=layout)
            return True
        except Exception as ex:
            print(f'[pkthrow] edit_enc_message failed: {type(ex).__name__}: {ex}')
            return False

    def _congrats_layout(self, gid, display_name: str, avatar_url: str,
                         msg: str, gif: str, accent: int):
        """Caught card (no buttons) — shared by the command, button and
        fallback paths so the result looks identical everywhere."""
        from discord.ui import LayoutView, Container, TextDisplay, Section, Thumbnail, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        layout = LayoutView(timeout=60)
        box = Container(accent_color=accent)
        head = f"## Congratulations, {display_name}!\n{msg}"
        if avatar_url:
            try:
                box.add_item(Section(TextDisplay(head), accessory=Thumbnail(media=avatar_url)))
            except Exception:
                box.add_item(TextDisplay(head))
        else:
            box.add_item(TextDisplay(head))
        if gif:
            try:
                box.add_item(MediaGallery(MediaGalleryItem(media=gif)))
            except Exception:
                pass
        layout.add_item(box)
        return layout

    def _catch_result_layout(self, gid, uid, e, *, ok: bool, msg: str, gif: str,
                             user_name: str, avatar_url: str = ''):
        """Final card for a finished catch-mode (`;p`) encounter: no buttons,
        one throw only — caught or gone."""
        if ok:
            rk, re, accent = rarity_of(_dex_row(e['dex']), bool(e.get('shiny')), gid)
            return self._congrats_layout(gid, user_name, avatar_url, msg, gif, accent)
        disp = (_dex_row(e['dex']).get('name') or f"#{e['dex']}").capitalize()
        if e.get('shiny'):
            disp = ((em(gid, 'rarity_shiny') or '*') + ' ' + disp)
        bust = em(gid, 'catch_burst')
        title = f"{bust + ' ' if bust else ''}{disp} escaped..."
        return self._encounter_layout(
            gid, title, f'{msg}\n{_balls_left_line(gid, uid)}', 0x3A3F4B)

    @commands.command(name='catch', description='Rzuć ball')
    async def catch(self, ctx, ball: str = ''):
        gid = ctx.guild.id
        ball = (ball or '').lower() or best_ball(gid, ctx.author.id)
        if ball not in BALLS:
            return await ctx.reply(t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        if not ball:
            return await ctx.reply(t(gid, 'eco.pk_noball', ball='—') + '\n' +
                                   t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        e = self._get_enc(gid, ctx.author.id)
        if not e:
            return await ctx.reply(t(gid, 'eco.pk_noenc'), ephemeral=True)
        one_shot = e.get('mode') == 'catch'
        if one_shot:
            # ;p is one throw: consume up front so a double-tap can't double-spend.
            self._enc.pop((str(gid), str(ctx.author.id)), None)
        await ctx.typing()
        try:
            ok, msg, gif, disp = await self._do_catch(gid, ctx.author.id, e, ball, one_shot=one_shot)
        except Exception as ex:
            print(f'[pkthrow] _do_catch failed: {type(ex).__name__}: {ex}')
            try:
                balls_add(gid, ctx.author.id, ball, 1)
            except Exception:
                pass
            if one_shot:
                self._enc[(str(gid), str(ctx.author.id))] = e
            return await ctx.reply(t(gid, 'eco.pk_turn_broke'), ephemeral=True)
        if ok and not one_shot:
            self._enc.pop((str(gid), str(ctx.author.id)), None)
        if one_shot:
            try:
                av = str(ctx.author.display_avatar.with_size(64).url)
            except Exception:
                av = ''
            try:
                layout = self._catch_result_layout(
                    gid, ctx.author.id, e, ok=ok, msg=msg, gif=gif,
                    user_name=ctx.author.display_name, avatar_url=av)
            except Exception as ex:
                print(f'[pkthrow] result layout failed: {type(ex).__name__}: {ex}')
                layout = None
            if layout is not None:
                try:
                    if await self._edit_enc_message(ctx, gid, e, layout):
                        return
                except Exception as ex:
                    print(f'[pkthrow] edit failed ({type(ex).__name__}: {ex}) — fresh fallback')
            else:
                print('[pkthrow] result layout failed — text fallback')
            # original message gone (or card unbuildable): fall back to a
            # fresh message — text if even that failed to build.
            try:
                if layout is not None:
                    return await ctx.reply(view=layout, mention_author=False)
                return await ctx.reply(msg, mention_author=False)
            except Exception as ex:
                print(f'[pkthrow] fallback send failed: {type(ex).__name__}: {ex}')
                return
        if ok:
            # spec: Congratulations, y4qs! + pfp + caught line + image + rarity/streak/roll/balls/coins
            rk, re, accent = rarity_of(_dex_row(e['dex']), bool(e['shiny']), gid)
            try:
                av = str(ctx.author.display_avatar.with_size(64).url)
            except Exception:
                av = ''
            await ctx.reply(view=self._congrats_layout(
                gid, ctx.author.display_name, av, msg, gif, accent), mention_author=False)
            return
        else:
            view, files = self._mini(gid, disp, msg, e.get('dex'), 0x3A3F4B)
            await ctx.reply(view=view, files=files or None, mention_author=False)

    async def _throw(self, ix: discord.Interaction, gid, user, ball: str):
        key = (str(gid), str(user.id))
        e = self._get_enc(gid, user.id)
        if not e:
            # NOTE: the button wrapper defers first, so answer via followup
            # (response.send_message here always raises InteractionResponded).
            try:
                return await ix.followup.send(t(gid, 'eco.pk_noenc'), ephemeral=True)
            except Exception:
                return
        try:
            clicked = getattr(getattr(ix, 'message', None), 'id', None)
        except Exception:
            clicked = None
        if e.get('mid') and clicked and int(clicked) != int(e['mid']):
            # Stale card (superseded encounter, or another channel): never
            # spend the throw — resolving here edits a card nobody watches
            # while eating the CURRENT encounter. Tell, don't touch.
            # (Wrapper usually deferred already: prefer followup.)
            try:
                if ix.response.is_done():
                    return await ix.followup.send(t(gid, 'eco.pk_enc_old'), ephemeral=True)
                return await ix.response.send_message(t(gid, 'eco.pk_enc_old'), ephemeral=True)
            except Exception:
                try:
                    return await ix.followup.send(t(gid, 'eco.pk_enc_old'), ephemeral=True)
                except Exception:
                    return
        one_shot = e.get('mode') == 'catch'
        if one_shot:
            self._enc.pop(key, None)
        # NOTE: no defer here — the button wrapper already deferred. A second
        # defer raises InteractionResponded and eats the throw (bug 26ab922).
        try:
            ok, msg, gif, disp = await self._do_catch(gid, user.id, e, ball, one_shot=one_shot)
        except Exception as ex:
            # The throw itself blew up: log it, hand the ball back, restore
            # the encounter — losing all three silently is the worst outcome.
            print(f'[pkthrow] _do_catch failed: {type(ex).__name__}: {ex}')
            try:
                balls_add(gid, user.id, ball, 1)
            except Exception:
                pass
            if one_shot:
                self._enc[key] = e
            try:
                return await ix.followup.send(t(gid, 'eco.pk_turn_broke'), ephemeral=True)
            except Exception:
                return
        if ok and not one_shot:
            self._enc.pop(key, None)
        if one_shot:
            try:
                av = str(user.display_avatar.with_size(64).url)
            except Exception:
                av = ''
            try:
                layout = self._catch_result_layout(
                    gid, user.id, e, ok=ok, msg=msg, gif=gif,
                    user_name=user.display_name, avatar_url=av)
            except Exception as ex:
                print(f'[pkthrow] result layout failed: {type(ex).__name__}: {ex}')
                layout = None
            if layout is None:
                # Card unbuildable: the text result still beats silence.
                try:
                    return await ix.followup.send(msg)
                except Exception as ex:
                    print(f'[pkthrow] text fallback failed: {type(ex).__name__}: {ex}')
                    return
            # Direct message edit first (no interaction-response machinery
            # involved), then the deferred-response edit, then a fresh card.
            # Every failure is printed: silent resolves are unacceptable.
            try:
                src = ix.message
                if src is None:
                    raise RuntimeError('no source message')
                await src.edit(view=layout)
                return
            except Exception as ex:
                print(f'[pkthrow] src edit failed ({type(ex).__name__}: {ex})')
            try:
                return await ix.edit_original_response(view=layout)
            except Exception as ex:
                print(f'[pkthrow] edit failed ({type(ex).__name__}: {ex}) — fresh fallback')
            try:
                return await ix.followup.send(view=layout)
            except Exception as ex:
                print(f'[pkthrow] fallback send failed: {type(ex).__name__}: {ex}')
                return
        if ok:
            rk, re, accent = rarity_of(_dex_row(e['dex']), bool(e['shiny']), gid)
            try:
                av = str(user.display_avatar.with_size(64).url)
            except Exception:
                av = ''
            await ix.followup.send(view=self._congrats_layout(
                gid, user.display_name, av, msg, gif, accent))
            return
        else:
            view, files = self._mini(gid, disp, msg, e.get('dex'), 0x3A3F4B)
            await ix.followup.send(view=view, files=files or None)

    async def _do_catch(self, gid, uid, e, ball: str, one_shot: bool = False):
        import aiohttp
        if not balls_take(gid, uid, ball):
            return False, t(gid, 'eco.pk_noball', ball=ball), '', ''
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, e['dex'])
        mult = BALLS[ball][1]
        if mult is None:  # master ball: it never fails, skips pity math entirely
            p = 1.0
        else:
            p = catch_chance(row.get('rate', 45), e['level'], e['hp'] / max(1, e['maxhp']), mult)
            if e.get('grazz'):
                p = min(0.98, p * 1.6)
            p = min(0.98, p + e.get('pity', 0))
        roll = random.random()
        if roll < p:
            first = not my_mons(gid, uid)
            with db.conn_ctx() as conn:
                conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                                                          'VALUES (?,?,?,?,0,?,?,?,?,?)',
                             (str(gid), str(uid), e['dex'], e['level'],
                              1 if e['shiny'] else 0, '', 1 if first else 0, _roll_ivs(), ''))
            with db.conn_ctx() as conn:
                prev = conn.execute('SELECT count FROM pk_dexcount WHERE guild_id=? AND user_id=? AND dex=?',
                                    (str(gid), str(uid), e['dex'])).fetchone()
            is_new = not prev or not (prev['count'] or 0)
            disp = row['name'].capitalize()
            spe = em(gid, _species_emoji_name(e['dex'])) or ''
            ball_emo = em(gid, BALL_FLEET.get(ball, ''), 'o')
            ball_name = {'poke':'Pokeball','great':'Greatball','ultra':'Ultraball','master':'Masterball'}.get(ball, ball.capitalize())
            check = em(gid, 'check') or '✅'
            trainer = em(gid, 'trainer_brendan') or ''
            msg = f"{check} {trainer + ' ' if trainer else ''}You caught a {spe + ' ' if spe else ''}{disp} with a {ball_emo + ' ' if ball_emo else ''}{ball_name}!"
            msg += '\n' + _rarity_line(gid, row, bool(e['shiny']))
            streak_bump(gid, uid, True)
            st = streak_get(gid, uid)
            flame = em(gid, 'streak_flame')
            msg += '\n' + (flame + ' ' if flame else '') + t(gid, 'eco.pk_streak_line',
                                                             n=st['catch_streak'], b=st['best_streak'])
            if is_new:
                msg += '\n' + t(gid, 'eco.pk_newdex')
            for extra in await self._catch_progress(gid, uid, e['dex'], e['shiny']):
                msg += '\n' + extra
            msg += '\n' + self._catch_meta(gid, uid, e['dex'], bool(e['shiny']))
            msg += '\n' + t(gid, 'eco.pk_roll_line', roll=int(roll * 100), rate=int(p * 100))
            msg += '\n' + _balls_left_line(gid, uid)
            from cogs.gamble import bal, set_cash
            gain = e['level'] * CATCH_COINS_LVL + (CATCH_COINS_NEW if is_new else 0) \
                + (CATCH_COINS_SHINY if e['shiny'] else 0)
            b = bal(gid, uid)
            set_cash(gid, uid, b['cash'] + gain)
            msg += '\n' + t(gid, 'eco.pk_coins_earned', win=cshort(gain))
            gif = showdown_gif(row.get('name', ''), bool(e['shiny']))
            if gif:
                try:
                    to = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=to) as s3:
                        async with s3.head(gif) as r:
                            if r.status != 200:
                                gif = ''
                except Exception:
                    gif = ''
            return True, msg, gif, disp
        # broke out: pity grows, next throw is kinder (one-shot ;p has no next throw)
        if not one_shot:
            e['pity'] = min(0.4, e.get('pity', 0) + 0.08)
        streak_bump(gid, uid, False)
        broke = t(gid, 'eco.pk_broke', name=row['name'].capitalize(), ball=ball)
        if not one_shot:
            pity_emo = em(gid, 'pity_token')
            broke += (f"\n{pity_emo + ' ' if pity_emo else ''}Pity +8% (now {int(e['pity'] * 100)}%)")
        return False, broke, '', ''

    def _region_progress(self, gid, uid) -> dict:
        out = {r: 0 for r in REGIONS}
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT dex, count FROM pk_dexcount WHERE guild_id=? AND user_id=?',
                                (str(gid), str(uid))).fetchall()
        for r in rows:
            reg = region_of(r['dex'])
            if reg:
                out[reg] += r['count'] or 0
        return out

    async def _catch_progress(self, gid, uid, dex: int, shiny: bool) -> list:
        """Dex milestones + region quest tiers + shiny-hunt streak. Returns lines."""
        from cogs.gamble import bal, set_cash
        lines = []
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO pk_dexcount (guild_id, user_id, dex, count) '
                         'VALUES (?,?,?,0)', (str(gid), str(uid), dex))
            conn.execute('UPDATE pk_dexcount SET count=count+1 WHERE guild_id=? AND user_id=? AND dex=?',
                         (str(gid), str(uid), dex))
            count = conn.execute('SELECT count FROM pk_dexcount WHERE guild_id=? AND user_id=? AND dex=?',
                                 (str(gid), str(uid), dex)).fetchone()['count']
        if count in DEX_MILESTONES:
            bonus = DEX_MILESTONES[count]
            b = bal(gid, uid)
            set_cash(gid, uid, b['cash'] + bonus)
            lines.append(t(gid, 'eco.pk_dexbonus', count=count, win=cshort(bonus)))
        # region quests
        reg = region_of(dex)
        if reg:
            prog = self._region_progress(gid, uid)[reg]
            with db.conn_ctx() as conn:
                row = conn.execute('SELECT tier FROM pk_quested WHERE guild_id=? AND user_id=? AND track=?',
                                   (str(gid), str(uid), reg)).fetchone()
                claimed = (row['tier'] if row else 0) or 0
            for i, need in enumerate(QUEST_TIERS):
                if prog >= need and claimed <= i:
                    reward = QUEST_REWARDS[i]
                    b = bal(gid, uid)
                    set_cash(gid, uid, b['cash'] + reward)
                    lines.append(t(gid, 'eco.pk_quest', track=reg.title(), need=need,
                                     win=cshort(reward)))
                    # random stone bonus (35% on any quest tier)
                    if random.random() < 0.35:
                        stone = random.choice(list(EVO_STONES.keys()))
                        balls_add(gid, uid, stone, 1)
                        emo = em(gid, EVO_STONES[stone]['icon']) or ''
                        lines.append(t(gid, 'eco.pk_quest_stone',
                                       item=f"{emo + ' ' if emo else ''}{EVO_STONES[stone]['name']}"))
                    claimed = i + 1
                    with db.conn_ctx() as conn:
                        conn.execute('INSERT OR IGNORE INTO pk_quested (guild_id, user_id, track, tier) '
                                     'VALUES (?,?,?,0)', (str(gid), str(uid), reg))
                        conn.execute('UPDATE pk_quested SET tier=? WHERE guild_id=? AND user_id=? AND track=?',
                                     (claimed, str(gid), str(uid), reg))
                    if i == len(QUEST_TIERS) - 1:
                        with db.conn_ctx() as conn:
                            conn.execute('INSERT OR IGNORE INTO achievements '
                                         '(guild_id, user_id, akey, unlocked_at) VALUES (?,?,?,?)',
                                         (str(gid), str(uid), REGION_BADGE[reg], int(time.time())))
                        lines.append(t(gid, 'eco.pk_quest_badge', track=reg.title()))
        # shiny hunt chain
        with db.conn_ctx() as conn:
            h = conn.execute('SELECT target, streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                             (str(gid), str(uid))).fetchone()
            if h and (h['target'] or 0) == dex:
                streak = (h['streak'] or 0) + 1
                if shiny:
                    streak = 0
                    lines.append(t(gid, 'eco.pk_chain_reset'))
                else:
                    lines.append(t(gid, 'eco.pk_chain', n=streak))
                conn.execute('UPDATE pk_hunt SET streak=? WHERE guild_id=? AND user_id=?',
                             (streak, str(gid), str(uid)))
        # daily target + counters + egg cycles
        from cogs.gamble import bal as _bal, set_cash as _set
        d = daily_row(gid, uid)
        if d['target'] == dex and not d['claimed']:
            b = _bal(gid, uid)
            _set(gid, uid, b['cash'] + DAILY_TARGET_REWARD)
            lines.append(t(gid, 'eco.pk_target_done', win=cshort(DAILY_TARGET_REWARD)))
            d['claimed'] = 1
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_daily SET catches=catches+1, claimed=? WHERE guild_id=? AND user_id=?',
                         (d['claimed'], str(gid), str(uid)))
            conn.execute('UPDATE pk_eggs SET cycles=cycles-1 WHERE guild_id=? AND owner_id=?',
                         (str(gid), str(uid)))
            ready = conn.execute('SELECT COUNT(*) c FROM pk_eggs WHERE guild_id=? AND owner_id=? AND cycles<=0',
                                 (str(gid), str(uid))).fetchone()['c']
        if ready:
            lines.append(t(gid, 'eco.pk_egg_ready'))
        return lines

    def _catch_meta(self, gid, uid, dex: int, shiny: bool = False) -> str:
        row = _dex_row(dex)
        streak = leg_streak_bump(gid, uid, bool(row.get('legendary')))
        b = balls_get(gid, uid)
        return t(gid, 'eco.pk_catchmeta', rarity=_rarity_line(gid, row, shiny), streak=streak,
                 balls=f"poke {b['poke']} | great {b['great']} | ultra {b['ultra']} | master {b['master']}")

    def _balls_line(self, gid, uid) -> str:
        b = balls_get(gid, uid)
        parts = [f"{k} x{v} ({BALLS[k][0]}$)" for k, v in b.items()]
        pots = potions_get(gid, uid)
        parts += [f"{k} x{v} ({POTIONS[k][0]}$)" for k, v in pots.items() if v]
        with db.conn_ctx() as conn:
            row = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='incense'",
                               (str(gid), str(uid))).fetchone()
            iq = (row['qty'] if row else 0) or 0
        if iq:
            parts.append(f"incense x{iq} ({POKE_SHOP_PRICES['incense'].amount}$)")
        elif incense_active(gid, uid):
            parts.append('incense ON')
        return ' · '.join(parts)

    # ball shop storefront sections
    BALL_SECTIONS = [
        ('Balls', ['poke', 'great', 'ultra', 'master']),
        ('Battle', ['potion', 'superpotion', 'grazz', 'candy']),
        ('Special', ['egg', 'incense']),
        ('Held', list(HELD_ORDER)),  # appended last: ids 1-10 stay stable
        ('Stones', list(STONE_ORDER)),
        ('Items', list(SHOP_EXTRA_ORDER)),
        ('Buddy', list(SHOP_BUDDY_ORDER)),
    ]
    BALL_EMOJI = {'poke': 'pokeball', 'great': 'greatball', 'ultra': 'ultraball',
                  'master': 'masterball', 'potion': 'potion', 'superpotion': 'potion',
                  'candy': 'candy', 'egg': 'egg', 'grazz': 'item_grazz',
                  'incense': 'fire', 'repel': 'item_repel',
                  **{k: v['icon'] for k, v in HELD_ITEMS.items()},
                  **{k: v['icon'] for k, v in EVO_STONES.items()},
                  **{k: v['icon'] for k, v in SHOP_EXTRAS.items()}}
    PK_NAMES = {'poke': 'Pokeball', 'great': 'Greatball', 'ultra': 'Ultraball',
                'master': 'Masterball', 'potion': 'Potion', 'superpotion': 'Super Potion',
                'candy': 'Rare Candy', 'grazz': 'Golden Razz', 'egg': 'Pokemon Egg',
                'incense': 'Shiny Incense',
                **{k: v['name'] for k, v in HELD_ITEMS.items()},
                **{k: v['name'] for k, v in EVO_STONES.items()},
                **{k: v['name'] for k, v in SHOP_EXTRAS.items()}}
    BALL_SECTION_EMOJI = {'Balls': 'btn_ball', 'Battle': 'btn_fight', 'Special': 'egg',
                          'Held': 'rate_up', 'Stones': 'evo_burst', 'Items': 'box_box', 'Buddy': 'buddy_ribbon'}

    @classmethod
    def ball_ids(cls) -> dict:
        """Stable buy-by-id numbers: global across BALL_SECTIONS order."""
        out, n = {}, 0
        for _s, keys in cls.BALL_SECTIONS:
            for k in keys:
                n += 1
                out[n] = k
        return out

    @staticmethod
    def _pk_price(item: str) -> int:
        if item in BALLS:
            return BALLS[item][0]
        if item in POTIONS:
            return POTIONS[item][0]
        if item == 'candy':
            return POKE_SHOP_PRICES['candy'].amount
        if item == 'grazz':
            return POKE_SHOP_PRICES['grazz'].amount
        if item == 'egg':
            return POKE_SHOP_PRICES['egg'].amount
        if item == 'incense':
            return POKE_SHOP_PRICES['incense'].amount
        if item in HELD_ITEMS:
            return HELD_ITEMS[item]['price']
        if item in EVO_STONES:
            return EVO_STONES[item]['price']
        if item in SHOP_EXTRAS:
            return SHOP_EXTRAS[item]['price']
        return 0

    @staticmethod
    def _pk_desc(gid, item: str) -> str:
        if item in BALLS:
            mult = BALLS[item][1]
            return t(gid, 'eco.pk_shop_ball', mult='∞' if mult is None else f'x{mult:g}')
        if item in POTIONS:
            return t(gid, 'eco.pk_shop_potion', pct=int(POTIONS[item][1] * 100))
        if item == 'candy':
            return t(gid, 'eco.pk_shop_candy')
        if item == 'grazz':
            return t(gid, 'eco.pk_shop_grazz')
        if item == 'egg':
            return t(gid, 'eco.pk_shop_egg')
        if item == 'incense':
            return t(gid, 'eco.pk_shop_incense')
        it = HELD_ITEMS.get(item)
        if it:
            eff = 'ev' if it.get('effect') == 'ev' else it.get('effect', '')
            return t(gid, f'eco.pk_shop_held_{eff}',
                     stat=EV_LABEL.get(it.get('stat', ''), ''), n=POWER_EV)
        st = EVO_STONES.get(item)
        if st:
            mons = ', '.join(str(_dex_row(d).get('name','?').capitalize()) for d in st['mons'].values())
            return t(gid, 'eco.pk_shop_stone', mons=mons or 'Eevee')
        ex = SHOP_EXTRAS.get(item)
        if ex:
            return f"{ex['name']} — {ex['price']:,} PokeCoins"
        return ''

    @commands.group(name='balls', aliases=['pokeshop', 'pshop', 'pkshop'],
                       description='Balle', invoke_without_command=True)
    async def balls(self, ctx):
        from cogs.gamble import bal
        gid = ctx.guild.id
        layout = self._balls_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                    owner_name=ctx.author.display_name)
        await ctx.reply(view=layout, ephemeral=True)

    def _balls_layout(self, gid, uid, cash: int, filt=None, owner_name: str = ''):
        """PokeMeow-style storefront: wallet header, category overview OR one
        filtered section, buy instructions, category buttons + Main shop.

        NOTE: Discord caps a LayoutView at 4000 display chars TOTAL
        (not per Container), so the full 37-item dump is never sent —
        filt=None renders a 7-line overview, buttons drill into sections."""
        from utils.shopui import catalog
        entries = {k: {'name': self.PK_NAMES.get(k, k), 'price': self._pk_price(k),
                       'emo': self.BALL_EMOJI.get(k, ''), 'desc': self._pk_desc(gid, k)}
                   for k in self.PK_NAMES}
        num_of = {k: i for i, k in self.ball_ids().items()}
        if filt is None:
            secs, overview = [], '\n'.join(
                f'`[{min(num_of[k] for k in ks)}-{max(num_of[k] for k in ks)}]`'
                f' __**{s}**__ — {len(ks)} items'
                for s, ks in self.BALL_SECTIONS)
        else:
            secs = [(s, ks) for s, ks in self.BALL_SECTIONS if s == filt]
            overview = ''
        poke = em(gid, 'coin') or '🪙'
        tagline = "Buy some items for your adventure!"
        coins_line = f"{owner_name}'s PokeCoins: {poke} {cash:,}"
        layout, _ids = catalog(
            gid, uid,
            tagline=tagline,
            coins_line=coins_line,
            cash=cash,
            sections=secs, all_sections=self.BALL_SECTIONS, entries=entries,
            accent=0xFF4655, cmd='balls',
            tip="`;balls info <name>` for description & usage (`;shop pokemon` shows this here)",
            buy_title="TO BUY AN ITEM",
            buy_1="`;balls buy {itemname} {amount}` OR `;shop buy {itemname} {amount}`",
            buy_2="`;balls buy {id #} {amount}`  (e.g. `;balls buy questscroll 1` / `;balls buy 6 1`)",
            ex_label="Example",
            ex1='questscroll 1', ex2=f'{num_of.get("questscroll", 6)} 1',
            foot="Also: `;pokeshop` / `;pshop` / `;shop pokemon`. Usage: `;balls info <name>`",
            section_emos=self.BALL_SECTION_EMOJI,
            on_section=self._balls_section_cb(gid, uid),
            extra_head=self._balls_line(gid, uid),
            overview=overview)
        return layout

    def _balls_section_cb(self, gid, uid):
        """Category buttons: re-render filtered (idx) or overview (idx -1)."""
        def factory(idx):
            async def _cb(ix: discord.Interaction):
                set_ctx_lang(ix.user)
                if ix.user.id != int(uid):
                    return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
                from cogs.gamble import bal
                member = ix.guild.get_member(int(uid)) if ix.guild else None
                name = member.display_name if member else f'User {uid}'
                filt = None if idx < 0 else self.BALL_SECTIONS[idx][0]
                layout = self._balls_layout(gid, int(uid), bal(gid, int(uid))['cash'],
                                            filt=filt, owner_name=name)
                await ix.response.edit_message(view=layout)
            return _cb
        return factory

    @balls.command(name='info', description='Opis przedmiotu')
    async def balls_info(self, ctx, item: str = ''):
        gid = ctx.guild.id
        key = (item or '').lower().strip()
        if key.isdigit():
            key = self.ball_ids().get(int(key), '')
        if key not in self.PK_NAMES:
            return await ctx.reply(t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        num_of = {k: i for i, k in self.ball_ids().items()}
        item_emo = em(gid, self.BALL_EMOJI.get(key, ''))
        head = (f'{item_emo + " " if item_emo else ""}'
                f'**{self.PK_NAMES[key]}** — {self._pk_price(key):,} {em(gid, "coin", "$")}')
        sec = next((s for s, ks in self.BALL_SECTIONS if key in ks), '?')
        await ctx.reply(view=self._layout(gid, self.PK_NAMES[key],
                                          f'{head}\n{self._pk_desc(gid, key)}'
                                          f'\n-# `[{num_of.get(key, "?")}]` {sec}'), ephemeral=True)

    @balls.command(name='buy', description='Kup balle')
    async def balls_buy(self, ctx, ball: str = '', n: int = 1):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        ball = (ball or '').lower()
        if ball.isdigit():
            ball = self.ball_ids().get(int(ball), '')
        if ball == 'incense':
            b = bal(gid, ctx.author.id)
            if POKE_SHOP_PRICES['incense'].amount > b['cash']:
                return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            set_cash(gid, ctx.author.id, b['cash'] - POKE_SHOP_PRICES['incense'].amount)
            balls_add(gid, ctx.author.id, 'incense', 1)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_balls SET expires=? WHERE guild_id=? AND user_id=? AND ball=?',
                             (int(time.time()) + INCENSE_SECONDS, str(gid), str(ctx.author.id),
                              'incense'))
            return await ctx.reply(t(gid, 'eco.pk_incense_on'), ephemeral=True)
        catalog = {**{k: v[0] for k, v in BALLS.items()},
                   **{k: v[0] for k, v in POTIONS.items()},
                   'candy': POKE_SHOP_PRICES['candy'].amount,
                   'egg': POKE_SHOP_PRICES['egg'].amount,
                   'grazz': POKE_SHOP_PRICES['grazz'].amount,
                   **{k: v['price'] for k, v in HELD_ITEMS.items()}}
        if ball not in catalog:
            return await ctx.reply(t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        n = max(1, min(99, n or 1))
        if ball == 'egg':
            with db.conn_ctx() as conn:
                owned = conn.execute('SELECT COUNT(*) c FROM pk_eggs WHERE guild_id=? AND owner_id=?',
                                     (str(gid), str(ctx.author.id))).fetchone()['c']
                if owned >= 3:
                    return await ctx.reply(t(gid, 'eco.pk_eggs_full'), ephemeral=True)
                n = min(n, 3 - owned)
            cost = catalog[ball] * n
        else:
            cost = catalog[ball] * n
        b = bal(gid, ctx.author.id)
        if cost > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - cost)
        if ball == 'egg':
            with db.conn_ctx() as conn:
                for _ in range(n):
                    conn.execute('INSERT INTO pk_eggs (guild_id, owner_id, cycles) VALUES (?,?,?)',
                                 (str(gid), str(ctx.author.id), EGG_CYCLES))
        else:
            balls_add(gid, ctx.author.id, ball, n)
        title_txt = held_label(gid, ball)
        if not title_txt:
            ball_emo = em(gid, {'poke': 'pokeball', 'great': 'greatball', 'ultra': 'ultraball',
                                'master': 'masterball'}.get(ball, ''))
            title_txt = f"{ball_emo + ' ' if ball_emo else ''}{ball}"
        view, files = self._mini(gid, title_txt,
                                 t(gid, 'eco.pk_balls_bought', n=n,
                                   ball=self.PK_NAMES.get(ball, ball)), None, 0x57F287)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    def _box_view(self, gid, viewer: int, owner: int, owner_name: str,
                  filt: str = '', page: int = 1, per: int = 10, avatar: str = ''):
        """Box card, PokeMeow-style: avatar + title header, command help,
        species stacks ([dex] letter sprite Name xN), page footer, 5 nav
        buttons. Fleet sprites render inline — no attachments."""
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Section, Thumbnail
        mons = my_mons(gid, owner)
        entries = [(m, _dex_row(m['dex'])) for m in mons]
        entries = [e for e in entries if _box_match(e[0], e[1], filt)]
        mode = self._box_sort.get((str(gid), str(viewer)), 'rarity')
        if mode not in BOX_SORTS:
            mode = 'rarity'
        stacks = _box_sort_stacks(_box_stacks(entries), mode)
        total = max(1, (len(stacks) + per - 1) // per)
        page = min(max(1, page), total)
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0x99AAB5)
        head = Section(TextDisplay(f"**{t(gid, 'eco.pk_box_title', user=owner_name)}**"),
                       accessory=Thumbnail(media=str(avatar or DEFAULT_AVATAR)))
        box.add_item(head)
        box.add_item(TextDisplay(
            f"{em(gid, 'star') or '*'} **Favorite Pokemon:** `;fav <slot|name>`\n"
            f"{em(gid, 'dex_book') or '[book]'} **View by region:** `;box <kanto|johto|hoenn|sinnoh>`\n"
            f"{em(gid, 'hunt_target') or '[target]'} **View by rarity/type:** `;box <common|shiny|fire|...>`\n"
            f"{em(gid, 'trade_swap') or '[trade]'} **View another trainer:** `;box @user [filter]`\n"
            f"**Page {page}**"))
        buddy_mid = (buddy_get(gid, owner) or {}).get('mid')
        rows = stacks[(page - 1) * per:page * per]
        if not rows:
            box.add_item(TextDisplay(t(gid, 'eco.pk_box_empty')))
        for key, group in rows:
            m0, r0 = group[0]
            dex = key[0]
            if key[1]:
                letter = rarity_of(r0, True, gid)[1] or 'S'
            else:
                letter = _tier_letter(gid, _box_tier(m0, r0))
            spe = em(gid, _species_emoji_name(dex))
            name = (r0.get('name') or f'#{dex}').capitalize()
            parts = [f'`[{dex}]`', letter, spe, f'{name} x{len(group)}']
            tail = ''
            if any(m.get('active') for m, _ in group):
                tail += em(gid, 'slot_active') or ''
            if any(m.get('fav') for m, _ in group):
                tail += em(gid, 'star') or ''
            if any(m.get('id') == buddy_mid for m, _ in group):
                tail += em(gid, 'buddy_ribbon') or ''
            if any((r.get('evo_to') or 0) and m['level'] >= (r.get('evo_level') or 999)
                   for m, r in group):
                tail += em(gid, 'up') or ''
            line = ' '.join(p for p in parts if p)
            if tail.strip():
                line += ' ' + tail
            box.add_item(TextDisplay(line))
        box.add_item(TextDisplay(
            t(gid, 'eco.pk_box_page', page=page, total=total, n=len(entries))
            + f' • Sorted by: {mode.title()}'
            + (f' • Filter: {filt}' if filt else '')))
        row = ActionRow()
        for label, emo, action, pg, dis in (
                ('', 'nav_first', 'first', 1, page <= 1),
                ('Back', 'nav_back', 'back', page - 1, page <= 1),
                ('Next', 'nav_next', 'next', page + 1, page >= total),
                ('', 'nav_last', 'last', total, page >= total)):
            lab = label or ('' if _btn_emoji(gid, emo) else ('<<' if action == 'first' else '>>'))
            b = discord.ui.Button(label=lab, style=discord.ButtonStyle.secondary,
                                  custom_id=_box_cid(viewer, owner, action, pg, mode, filt),
                                  disabled=dis, emoji=_btn_emoji(gid, emo))
            b.callback = self._mk_box_btn(gid, viewer, owner)
            row.add_item(b)
        sb = discord.ui.Button(label='Sort', style=discord.ButtonStyle.secondary,
                               custom_id=_box_cid(viewer, owner, 'sort', page, mode, filt),
                               emoji=_btn_emoji(gid, 'nav_sort'))
        sb.callback = self._mk_box_btn(gid, viewer, owner)
        row.add_item(sb)
        box.add_item(row)
        layout.add_item(box)
        return layout, []

    def _mk_box_btn(self, gid, viewer: int, owner: int):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(viewer):
                try:
                    return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
                except Exception:
                    return await ix.followup.send(t(gid, 'eco.not_yours'), ephemeral=True)
            parsed = _box_parse(ix.data.get('custom_id', ''))
            if not parsed:
                try:
                    return await ix.response.send_message(t(gid, 'eco.pk_box_page',
                         page=1, total=1, n=0), ephemeral=True)
                except Exception:
                    return await ix.followup.send(t(gid, 'eco.pk_box_page',
                         page=1, total=1, n=0), ephemeral=True)
            _, owner, action, pg, mode, filt = parsed
            if action == 'sort':
                cur = self._box_sort.get((str(gid), str(viewer)), 'rarity')
                mode = BOX_SORTS[(BOX_SORTS.index(cur) + 1) % len(BOX_SORTS)] if cur in BOX_SORTS else 'rarity'
                self._box_sort[(str(gid), str(viewer))] = mode
            # defer early to avoid 3s timeout on large boxes / slow fleet lookups
            try:
                if not ix.response.is_done():
                    await ix.response.defer()
            except Exception:
                pass
            try:
                member = ix.guild.get_member(int(owner)) if ix.guild else None
                user = member or self.bot.get_user(int(owner))
                name = member.display_name if member else (user.name if user else f'User {owner}')
                avatar = str(user.display_avatar.url) if user and hasattr(user, 'display_avatar') else ''
                view, files = self._box_view(gid, viewer, int(owner), name, filt, pg,
                                             avatar=avatar)
                if ix.response.is_done():
                    await ix.edit_original_response(view=view, attachments=files or None)
                else:
                    await ix.response.edit_message(view=view, attachments=files or None)
            except Exception as e:
                # log and show first page instead of cryptic 2/1 0
                try:
                    print(f"[box] page {pg} failed for {viewer}/{owner} filt={filt!r} mode={mode!r}: {e}")
                except Exception:
                    pass
                try:
                    member = ix.guild.get_member(int(owner)) if ix.guild else None
                    user = member or self.bot.get_user(int(owner))
                    name = member.display_name if member else (user.name if user else f'User {owner}')
                    avatar = str(user.display_avatar.url) if user and hasattr(user, 'display_avatar') else ''
                    view, files = self._box_view(gid, viewer, int(owner), name, filt, 1,
                                                 avatar=avatar)
                    if ix.response.is_done():
                        await ix.edit_original_response(view=view, attachments=files or None)
                    else:
                        await ix.response.edit_message(view=view, attachments=files or None)
                except Exception:
                    try:
                        if ix.response.is_done():
                            await ix.followup.send(t(gid, 'eco.pk_box_page', page=1, total=1, n=0), ephemeral=True)
                        else:
                            await ix.response.send_message(t(gid, 'eco.pk_box_page', page=1, total=1, n=0), ephemeral=True)
                    except Exception:
                        pass
        return _cb

    @commands.command(name='box', description='Twoje pokemony')
    async def box(self, ctx, *args):
        import re as _re
        gid = ctx.guild.id
        owner, owner_name, page, filt = ctx.author.id, ctx.author.display_name, 1, ''
        for a in (args or ()):
            m = _re.fullmatch(r'<@!?(\d+)>', str(a))
            if m:
                owner = int(m.group(1))
                member = ctx.guild.get_member(owner)
                owner_name = member.display_name if member else f'User {owner}'
            elif str(a).isdigit():
                page = max(1, int(a))
            elif not filt:
                filt = str(a).lower()[:24]
        if not my_mons(gid, owner):
            if owner == ctx.author.id:
                return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
            return await ctx.reply(t(gid, 'eco.pk_box_empty_other'), ephemeral=True)
        member = ctx.guild.get_member(owner) if ctx.guild else None
        avatar = str((member or ctx.author).display_avatar.url)
        view, files = self._box_view(gid, ctx.author.id, owner, owner_name, filt, page,
                                     avatar=avatar)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='fav', description='Oznacz/odznacz ulubieńca')
    async def fav(self, ctx, *, arg: str = ''):
        gid = ctx.guild.id
        arg = (arg or '').strip()
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        else:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()  # noqa: E731
            m, _n = _match_mon(mons, spec_of, arg)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        new = 0 if m.get('fav') else 1
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET fav=? WHERE id=?', (new, m['id']))
        key = 'eco.pk_fav_on' if new else 'eco.pk_fav_off'
        view, files = self._mini(gid, mon_name(m, gid),
                                 t(gid, key, name=mon_name(m, gid)), m.get('dex'), 0xFFD43B)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='mon', description='Staty pokemona')
    async def info(self, ctx, *, arg: str = ''):
        import aiohttp
        gid = ctx.guild.id
        arg = (arg or '').strip()
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        else:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()  # noqa: E731
            m, _n = _match_mon(mons, spec_of, arg)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, m['dex'])
            moves = await moveset_for(s, m['dex'], m['level'])
        stats = calc_stats(row, m['level'], _ivs_of(m), None, _evs_of(m))
        form = form_of(m)
        if form:
            row = dict(row, name=form['name'].lower(), types=list(form['types']),
                       hp=form['stats']['hp'], atk=form['stats']['atk'],
                       dfn=form['stats']['dfn'], spa=form['stats']['spa'],
                       spd=form['stats']['spd'], spe=form['stats']['spe'],
                       legendary=1)
            stats = calc_stats(row, m['level'], _ivs_of(m), None, _evs_of(m))
        nxt = XP_NEXT(m['level'])
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if m['shiny'] and len(spr) > 1 else spr[0]
        if form:
            img = form['shiny_sprite'] if m['shiny'] else form['sprite']
        rk, re, accent = rarity_of(row, bool(m['shiny']), gid)
        statline = ' '.join(f"{em(gid, n) or n} {v}" for n, v in (
            ('stat_hp', stats['maxhp']), ('stat_atk', stats['atk']), ('stat_def', stats['dfn']),
            ('stat_spa', stats['spa']), ('stat_spdef', stats['spd']), ('stat_speed', stats['spe'])))
        desc = (f'{re} **{rk.upper()}** · {types_str(gid, row["types"])} · Lv{m["level"]} '
                f'· IV {_iv_pct(m)}% · {_ev_line(gid, m)}\n'
                f'{xp_bar(m["xp"], nxt, gid)} {m["xp"]}/{nxt} XP\n'
                f'{statline}\n'
                f'{_held_line(gid, m)}\n'
                + t(gid, 'eco.pk_info', level=m['level'], types=types_str(gid, row["types"]),
                    hp=stats['maxhp'], atk=stats['atk'], dfn=stats['dfn'],
                    spa=stats['spa'], spd=stats['spd'], spe=stats['spe'],
                    xp=m['xp'], nxt=nxt,
                    moves=', '.join(move_str(gid, x) for x in moves)))
        await ctx.reply(view=await self._mage(gid, f'{re} {mon_name(m, gid)}', desc, img, accent))

    @commands.command(name='active', description='Wybierz wojownika (;box po numery)')
    async def active(self, ctx, slot: int = 0):
        gid = ctx.guild.id
        if not slot:
            mons = my_mons(gid, ctx.author.id)
            if not mons:
                return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
            if len(mons) == 1:
                return await ctx.reply(t(gid, 'eco.pk_active', name=mon_name(mons[0], gid)),
                                       ephemeral=True)
            return await ctx.reply(view=self._fighter_picker(gid, ctx.author.id, 'active'),
                                   ephemeral=True)
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET active=0 WHERE guild_id=? AND owner_id=?',
                         (str(gid), str(ctx.author.id)))
            conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (m['id'],))
        view, files = self._mini(gid, mon_name(m, gid),
                                         t(gid, 'eco.pk_active', name=mon_name(m, gid)),
                                         m.get('dex'), 0x58CC02)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='name', description='Przezwij pokemona')
    async def nick(self, ctx, slot: int, *, name: str = ''):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET nick=? WHERE id=?', (name[:24], m['id']))
        view, files = self._mini(gid, mon_name(m, gid),
                                         t(gid, 'eco.pk_nicked', name=(name[:24] or mon_name(m, gid))),
                                         m.get('dex'), 0x58CC02)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='release', description='Wypuść pokemona')
    async def release(self, ctx, *, arg: str = ''):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        arg = (arg or '').strip()
        m = None
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        if not m and arg:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            m, _ = _match_mon(mons, spec_of, arg.lower())
            if not m:
                m = next((x for x in mons if (x.get('nick') or '').lower() == arg.lower()), None)
        if not m:
            # also try slot fallback via name with spaces? already
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m, gid)), ephemeral=True)
        name = mon_name(m, gid)
        val = release_value(m)
        b = bal(gid, ctx.author.id)
        set_cash(gid, ctx.author.id, b['cash'] + val)
        hk = held_key(m)
        if hk:
            balls_add(gid, ctx.author.id, hk, 1)  # the item drops back in the bag
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        view, files = self._mini(gid, name,
                                         t(gid, 'eco.pk_released_cash', name=name, win=cshort(val)),
                                         m.get('dex'), 0xE0A800)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='releaseall', description='Wypuść cały gatunek')
    async def releaseall(self, ctx, *, name: str = ''):
        import aiohttp
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        target = (name or '').lower().strip()
        if not target:
            return await ctx.reply(t(gid, 'eco.pk_releaseall_use'), ephemeral=True)
        ids, label, skipped_n = [], '', 0
        for m in mons:
            row = _dex_row(m['dex'])
            nm = (row.get('name') or '').lower()
            if target in (nm, (m.get('nick') or '').lower()) or nm.startswith(target):
                if m.get('locked'):
                    skipped_n += 1
                    continue
                ids.append(m['id'])
                label = row.get('name', '?').capitalize()
        if not ids:
            msg = t(gid, 'eco.pk_releaseall_none', name=target[:24])
            if skipped_n:
                msg += ' ' + t(gid, 'eco.pk_releaseall_skip', n=skipped_n)
            return await ctx.reply(msg, ephemeral=True)
        from cogs.gamble import bal as _b2, set_cash as _s2
        total, back = 0, {}
        with db.conn_ctx() as conn:
            for mid in ids:
                mm = conn.execute('SELECT * FROM pk_mons WHERE id=?', (mid,)).fetchone()
                if mm:
                    mm = dict(mm)
                    total += release_value(mm)
                    if mm.get('held'):
                        back[mm['held']] = back.get(mm['held'], 0) + 1
            conn.execute(f"DELETE FROM pk_mons WHERE id IN ({','.join('?' * len(ids))})", ids)
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        for hk, hn in back.items():
            balls_add(gid, ctx.author.id, hk, hn)  # held items come back in the bag
        b = _b2(gid, ctx.author.id)
        _s2(gid, ctx.author.id, b['cash'] + total)
        msg = t(gid, 'eco.pk_released_cash', name=f'{len(ids)}x {label}', win=cshort(total))
        if skipped_n:
            msg += ' ' + t(gid, 'eco.pk_releaseall_skip', n=skipped_n)
        view, files = self._mini(gid, f'{len(ids)}x {label}', msg, None, 0xE0A800)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='trainer', description='Statystyki trenera')
    async def stats(self, ctx, member: discord.Member = None):
        gid = ctx.guild.id
        member = member or ctx.author
        mons = my_mons(gid, member.id)
        with db.conn_ctx() as conn:
            catches = conn.execute('SELECT COALESCE(SUM(count),0) c FROM pk_dexcount '
                                   'WHERE guild_id=? AND user_id=?', (str(gid), str(member.id))).fetchone()['c']
            dex = conn.execute('SELECT COUNT(DISTINCT dex) c FROM pk_mons WHERE guild_id=? AND owner_id=?',
                               (str(gid), str(member.id))).fetchone()['c']
            st = conn.execute('SELECT duels_won, duels_lost FROM pk_stats WHERE guild_id=? AND user_id=?',
                              (str(gid), str(member.id))).fetchone()
        shinies = sum(1 for m in mons if m.get('shiny'))
        total_lv = sum(m['level'] for m in mons)
        with db.conn_ctx() as conn:
            above = conn.execute('SELECT COUNT(*) c FROM (SELECT owner_id, SUM(level) lv FROM pk_mons '
                                 'WHERE guild_id=? GROUP BY owner_id HAVING lv > ?)',
                                 (str(gid), total_lv)).fetchone()['c']
        rank = above + 1 if mons else '—'
        medal = em(gid, {1: 'medal_gold', 2: 'medal_silver', 3: 'medal_bronze'}.get(rank, ''))
        from discord.ui import LayoutView, Container, TextDisplay, Section, Thumbnail
        try:
            av = str(member.display_avatar.with_size(128).url)
        except Exception:
            av = ''
        tlayout = LayoutView(timeout=60)
        tbox = Container(accent_color=0xFFD43B)
        tbox.add_item(TextDisplay(
            f"## {(medal + ' ' if medal else '')}"
            f"{t(gid, 'eco.pk_stats_title', user=member.display_name)}"))
        tbody = (t(gid, 'eco.pk_stats', n=len(mons), lv=total_lv, dex=dex, catches=catches,
                   shinies=shinies, w=(st['duels_won'] if st else 0), l=(st['duels_lost'] if st else 0))
                 + f"\nServer rank: **#{rank}**")
        if av:
            try:
                tbox.add_item(Section(TextDisplay(tbody), accessory=Thumbnail(media=av)))
            except Exception:
                tbox.add_item(TextDisplay(tbody))
        else:
            tbox.add_item(TextDisplay(tbody))
        tlayout.add_item(tbox)
        await ctx.reply(view=tlayout, ephemeral=True)

    @commands.command(name='trainers', description='Top trenerów')
    async def top(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT owner_id, SUM(level) lv, COUNT(*) n FROM pk_mons '
                                'WHERE guild_id=? GROUP BY owner_id ORDER BY lv DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_top_empty'), ephemeral=True)
        medals = {1: 'medal_gold', 2: 'medal_silver', 3: 'medal_bronze'}
        lines = []
        for i, r in enumerate(rows, 1):
            m = ctx.guild.get_member(int(r['owner_id']))
            nm = m.display_name if m else r['owner_id']
            medal = em(gid, medals[i]) + ' ' if i in medals and em(gid, medals[i]) else ''
            lines.append(f"`{i}` {medal}**{nm}** — Lv{r['lv']} ({r['n']})")
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_top_title'), '\n'.join(lines)),
                        ephemeral=True)

    def _dex_view(self, gid, viewer: int, owner: int, page: int = 1, per: int = 8):
        """Pokedex browser: thumbnail sections per species, paged."""
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Section
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT DISTINCT dex FROM pk_mons WHERE guild_id=? AND owner_id=?',
                                (str(gid), str(owner))).fetchall()
            total = conn.execute('SELECT COUNT(*) c FROM pk_dex').fetchone()['c']
        caught = sorted({r['dex'] for r in rows})
        pages = max(1, (len(caught) + per - 1) // per)
        page = min(max(1, page), pages)
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0x3498DB)
        dex_emo = em(gid, 'dex_book')
        box.add_item(TextDisplay(
            f"## {dex_emo + ' ' if dex_emo else ''}{t(gid, 'eco.pk_dex_title', n=len(caught))}"))
        files = []
        for dex in caught[(page - 1) * per:page * per]:
            r = _dex_row(dex)
            rk2, re2, _ = rarity_of(r, False, gid)
            spe = em(gid, _species_emoji_name(dex))
            sec, f = _sec_row(
                gid, f"{_tier_letter(gid, rk2)} {spe + ' ' if spe else ''}{re2} "
                      f"`#{dex}` {(r.get('name') or f'#{dex}').capitalize()} "
                      f"· {types_str(gid, r.get('types') or ['?'])}", dex)
            if f:
                files.append(f)
            box.add_item(sec)
        box.add_item(TextDisplay(
            t(gid, 'eco.pk_dex', names=f'Page {page}/{pages}', total=total)))
        row = ActionRow()
        for label, emo, pg, dis in (('BACK', 'nav_back', page - 1, page <= 1),
                                    ('NEXT', 'nav_next', page + 1, page >= pages)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'pkdex:{viewer}:{owner}:{pg}',
                                  disabled=dis,
                                  emoji=_btn_emoji(gid, emo))
            b.callback = self._mk_dex_btn(gid, viewer, owner)
            row.add_item(b)
        box.add_item(row)
        layout.add_item(box)
        return layout, files

    def _mk_dex_btn(self, gid, viewer: int, owner: int):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(viewer):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            try:
                pg = int((ix.data.get('custom_id', '').split(':') + ['1'])[2])
            except Exception:
                pg = 1
            view, files = self._dex_view(gid, viewer, owner, pg)
            await ix.response.edit_message(view=view, attachments=files or None)
        return _cb

    @commands.command(name='dex', description='Pokedex')
    async def dex(self, ctx):
        gid = ctx.guild.id
        view, files = self._dex_view(gid, ctx.author.id, ctx.author.id, 1)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='guess', description='Zgadnij tajemniczego')
    async def guess(self, ctx, *, name: str = ''):
        """PokeTwo-style: name a mystery encounter to catch it free."""
        import aiohttp
        gid = ctx.guild.id
        e = self._get_enc(gid, ctx.author.id)
        wild = None
        if not e or not e.get('mystery'):
            wild = self._get_wild(gid, ctx.channel.id)
            if not wild:
                return await ctx.reply(
                    t(gid, 'eco.pk_noenc' if not e else 'eco.pk_nomystery'), ephemeral=True)
            e, wild = wild, True
        if not my_mons(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, e['dex'])
        if (name or '').lower().strip() != (row.get('name') or '').lower():
            return await ctx.reply(t(gid, 'eco.pk_wrong', guess=(name or '?')[:24]),
                                   ephemeral=True)
        first = not my_mons(gid, ctx.author.id)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                                                  'VALUES (?,?,?,?,0,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), e['dex'], e['level'],
                          1 if e['shiny'] else 0, '', 1 if first else 0, _roll_ivs(), ''))
        if wild:
            self._wild.pop((str(gid), str(ctx.channel.id)), None)
        else:
            self._enc.pop((str(gid), str(ctx.author.id)), None)
        msg = t(gid, 'eco.pk_guessed', name=((em(gid, 'rarity_shiny') or '*') if e['shiny'] else '') + row['name'].capitalize())
        streak_bump(gid, ctx.author.id, True)
        for extra in await self._catch_progress(gid, ctx.author.id, e['dex'], e['shiny']):
            msg += '\n' + extra
        msg += '\n' + self._catch_meta(gid, ctx.author.id, e['dex'], bool(e['shiny']))
        gif = showdown_gif(row.get('name', ''), bool(e['shiny']))
        if gif:
            try:
                import aiohttp as _aio2
                to = _aio2.ClientTimeout(total=10)
                async with _aio2.ClientSession(timeout=to) as s3:
                    async with s3.head(gif) as r:
                        if r.status != 200:
                            gif = ''
            except Exception:
                gif = ''
        if gif:
            rk, re, accent = rarity_of(row, bool(e['shiny']), gid)
            burst = em(gid, 'catch_burst')
            title = (f'{burst + " " if burst else ""}{re} '
                     + t(gid, 'eco.pk_caught_title', name=row['name'].capitalize()))
            if not wild and e.get('mode') == 'catch':
                # guessed a ;p mystery: resolve the encounter card in place
                if await self._edit_enc_message(
                        ctx, gid, e, self._encounter_layout(
                            gid, title, msg, accent, [gif])):
                    return
            await ctx.reply(view=self._layout(
                gid, title, msg, gif, accent), mention_author=False)
        else:
            spr = (row.get('sprite') or '').split('|')
            img = spr[1] if e['shiny'] and len(spr) > 1 else spr[0]
            if not wild and e.get('mode') == 'catch':
                rk, re, accent = rarity_of(row, bool(e['shiny']), gid)
                burst = em(gid, 'catch_burst')
                if await self._edit_enc_message(
                        ctx, gid, e, self._encounter_layout(
                            gid, f'{burst + " " if burst else ""}{re} '
                            + t(gid, 'eco.pk_caught_title', name=row['name'].capitalize()),
                            msg, accent, [img] if img else [])):
                    return
            await ctx.reply(view=await self._mage(
                gid, t(gid, 'eco.pk_caught_title', name=row['name'].capitalize()), msg, img),
                mention_author=False)

    @commands.command(name='hint', description='Podpowiedź do tajemniczego')
    async def hint(self, ctx):
        import aiohttp
        gid = ctx.guild.id
        e = self._get_enc(gid, ctx.author.id)
        if not e or not e.get('mystery'):
            return await ctx.reply(t(gid, 'eco.pk_nohint'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, e['dex'])
        nm = row.get('name', '???')
        inds = [i for i, ch in enumerate(nm) if ch.isalpha()]
        blanks = set(random.sample(inds, len(inds) // 2)) if inds else set()
        view, files = self._mini(
            gid, 'Mystery hint',
            t(gid, 'eco.pk_hint',
              hint=''.join('_' if i in blanks else ch for i, ch in enumerate(nm))),
            e.get('dex'), 0x3A3F4B)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='shinyhunt', description='Łów shiny łańcuchem')
    async def shinyhunt(self, ctx, *, name: str = ''):
        import aiohttp
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            h = conn.execute('SELECT target, streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id))).fetchone()
        if not (name or '').strip():
            if not h or not h['target']:
                return await ctx.reply(t(gid, 'eco.pk_hunt_none'), ephemeral=True)
            row = _dex_row(h['target'])
            denom = max(32, SHINY_ODDS - (h['streak'] or 0) * 4)
            sec, f = _sec_row(
                gid, t(gid, 'eco.pk_hunt_status',
                       name=(row.get('name') or '?').capitalize(),
                       streak=h['streak'] or 0, denom=denom),
                h['target'])
            _, _, accent = rarity_of(row, False, gid)
            tgt_emo = em(gid, 'hunt_target')
            return await ctx.reply(
                view=self._wrap_sec(gid, f"{tgt_emo + ' ' if tgt_emo else ''}"
                                         + t(gid, 'eco.pk_hunt_title'), sec, accent),
                files=[f] if f else None, ephemeral=True)
        # resolve name -> dex via cache, else API
        dex = 0
        with db.conn_ctx() as conn:
            r = conn.execute('SELECT dex FROM pk_dex WHERE name=?',
                             ((name or '').lower().strip(),)).fetchone()
            if r:
                dex = r['dex']
        if not dex:
            async with aiohttp.ClientSession() as s:
                row = await dex_get(s, (name or '').lower().strip())
                dex = row.get('dex', 0) if row else 0
        if not dex:
            return await ctx.reply(t(gid, 'eco.pk_hunt_unknown', name=(name or '?')[:24]),
                                   ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO pk_hunt (guild_id, user_id, target, streak) '
                         'VALUES (?,?,?,0)', (str(gid), str(ctx.author.id), dex))
            conn.execute('UPDATE pk_hunt SET target=?, streak=0 WHERE guild_id=? AND user_id=?',
                         (dex, str(gid), str(ctx.author.id)))
        row = _dex_row(dex)
        view, files = self._mini(
            gid, (row.get('name') or f'#{dex}').capitalize(),
            t(gid, 'eco.pk_hunt_set', name=(row.get('name') or f'#{dex}').capitalize()),
            dex, 0xB45CFF)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='quests', description='Misje regionów')
    async def quests(self, ctx):
        gid = ctx.guild.id
        prog = self._region_progress(gid, ctx.author.id)
        with db.conn_ctx() as conn:
            claimed = {r['track']: (r['tier'] or 0) for r in conn.execute(
                'SELECT track, tier FROM pk_quested WHERE guild_id=? AND user_id=?',
                (str(gid), str(ctx.author.id))).fetchall()}
        lines = []
        region_badge = {'kanto': 'region_kanto', 'johto': 'region_johto',
                        'hoenn': 'region_hoenn', 'sinnoh': 'region_sinnoh'}
        for track in REGIONS:
            p = prog[track]
            done = claimed.get(track, 0)
            nxt = next((i for i, need in enumerate(QUEST_TIERS) if p < need), None)
            badge = em(gid, region_badge.get(track, ''))
            if nxt is None:
                bar, info = (em(gid, 'xp_full') or '#') * 10, t(gid, 'eco.pk_quest_done')
            else:
                need = QUEST_TIERS[nxt]
                fill = min(10, int(p / need * 10))
                bar = (em(gid, 'xp_full') or '#') * fill \
                    + (em(gid, 'xp_empty') or '-') * (10 - fill)
                info = t(gid, 'eco.pk_quest_next', p=p, need=need,
                         win=cshort(QUEST_REWARDS[nxt]))
            lines.append(f"{badge + ' ' if badge else ''}**{track.title()}** {bar} {info}")
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_quests_title'), '\n'.join(lines)),
                        ephemeral=True)

    @commands.command(name='sell', description='Wystaw na targ')
    async def sell(self, ctx, *, args: str = ''):
        gid = ctx.guild.id
        # parse: ;sell <name|slot> <price>  — price is last token
        parts = (args or '').strip().split()
        if not parts:
            return await ctx.reply("Use: `;sell <name|slot> <price>`", ephemeral=True)
        # last token is price if numeric
        price = 0
        ident = ''
        if parts[-1].isdigit():
            price = int(parts[-1])
            ident = ' '.join(parts[:-1]).strip()
        else:
            # no price? treat whole as ident and expect price missing
            ident = ' '.join(parts).strip()
        if not ident:
            return await ctx.reply("Use: `;sell <name|slot> <price>`", ephemeral=True)
        # resolve mon by slot or name
        m = None
        if ident.isdigit():
            m = get_mon(gid, ctx.author.id, int(ident))
        if not m:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            # try exact name or nick match
            m, _ = _match_mon(mons, spec_of, ident.lower())
            if not m:
                # try nick directly
                m = next((x for x in mons if (x.get('nick') or '').lower() == ident.lower()), None)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m, gid)), ephemeral=True)
        if (price or 0) < 100:
            return await ctx.reply(t(gid, 'eco.pk_market_min'), ephemeral=True)
        hk = held_key(m)
        if hk:
            balls_add(gid, ctx.author.id, hk, 1)  # listings carry no held item
        with db.conn_ctx() as conn:
            n = conn.execute('SELECT COUNT(*) c FROM pk_market WHERE guild_id=? AND seller_id=?',
                             (str(gid), str(ctx.author.id))).fetchone()['c']
            if n >= 3:
                return await ctx.reply(t(gid, 'eco.pk_market_full'), ephemeral=True)
            conn.execute('INSERT INTO pk_market (guild_id, seller_id, seller_name, dex, level, xp, '
                         'shiny, nick, price, created, ivs, evs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), ctx.author.display_name[:24],
                          m['dex'], m['level'], m['xp'], m['shiny'], m.get('nick') or '',
                          price, int(time.time()), m.get('ivs') or '', m.get('evs') or ''))
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
            lid = conn.execute('SELECT last_insert_rowid() i').fetchone()['i']
        view, files = self._mini(gid, mon_name(m, gid),
                                         t(gid, 'eco.pk_listed', name=mon_name(m, gid),
                                           price=cshort(price), lid=lid),
                                         m.get('dex'), 0xE0A800)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    def _wrap_sec(self, gid, title: str, sec, accent: int = 0xFFFFFF):
        """Layout around one prebuilt section (thumbnail rows, single art)."""
        from discord.ui import LayoutView, Container, TextDisplay
        layout = LayoutView(timeout=60)
        box = Container(accent_color=accent)
        box.add_item(TextDisplay(f'## {title}'))
        box.add_item(sec)
        layout.add_item(box)
        return layout

    def _mini(self, gid, title: str, body: str, dex=None, accent: int = 0xFFFFFF):
        """One-shot result card: species thumbnail section when art ships."""
        from discord.ui import LayoutView, Container, TextDisplay, Section
        layout = LayoutView(timeout=60)
        box = Container(accent_color=accent)
        files = []
        if dex:
            sec, f = _sec_row(gid, f'## {title}\n{body}', dex)
            if f:
                files.append(f)
            box.add_item(sec)
        else:
            box.add_item(TextDisplay(f'## {title}\n{body}'))
        layout.add_item(box)
        return layout, files

    def _market_view(self, gid, viewer: int, page: int = 1, per: int = 8):
        """Market browser: stall header, thumbnail rows, price tags, pages."""
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM pk_market WHERE guild_id=? ORDER BY id DESC LIMIT 40',
                (str(gid),)).fetchall()]
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0xE0A800)
        stall = em(gid, 'market_stall')
        box.add_item(TextDisplay(
            f"## {stall + ' ' if stall else ''}{t(gid, 'eco.pk_market_title')}"))
        files = []
        total = max(1, (len(rows) + per - 1) // per)
        page = min(max(1, page), total)
        if not rows:
            box.add_item(TextDisplay(t(gid, 'eco.pk_market_empty')))
        for r in rows[(page - 1) * per:page * per]:
            tag = em(gid, 'price_tag')
            sec, f = _sec_row(
                gid, f"`{r['id']}` {mon_name(r, gid)} Lv{r['level']} — "
                      f"**{cshort(r['price'])}** {tag}({r['seller_name'] or r['seller_id']})",
                r.get('dex'))
            if f:
                files.append(f)
            box.add_item(sec)
        box.add_item(TextDisplay(
            t(gid, 'eco.pk_market_page', page=page, total=total)
            + f' · `;buy <id>`'))
        row = ActionRow()
        for label, emo, pg, dis in (('BACK', 'nav_back', page - 1, page <= 1),
                                    ('NEXT', 'nav_next', page + 1, page >= total)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'pkmkt:{viewer}:{pg}',
                                  disabled=dis,
                                  emoji=_btn_emoji(gid, emo))
            b.callback = self._mk_market_btn(gid, viewer)
            row.add_item(b)
        box.add_item(row)
        layout.add_item(box)
        return layout, files

    def _mk_market_btn(self, gid, viewer: int):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(viewer):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            try:
                pg = int((ix.data.get('custom_id', '').split(':') + ['1'])[2])
            except Exception:
                pg = 1
            view, files = self._market_view(gid, viewer, pg)
            await ix.response.edit_message(view=view, attachments=files or None)
        return _cb

    @commands.command(name='market', description='Targ pokemonów')
    async def market(self, ctx, page: int = 1):
        gid = ctx.guild.id
        view, files = self._market_view(gid, ctx.author.id, max(1, page or 1))
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='buy', description='Kup z targu')
    async def buy(self, ctx, listing: str = None):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        # NOTE: listing is str (not int) on purpose — `;buy` bare or
        # `;buy <shop item>` used to 400 BadArgument spam in logs.
        # Give usage instead: market ids here, shop items via ;balls buy.
        if listing is None:
            view, files = self._market_view(gid, ctx.author.id, 1)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        try:
            lid = int((listing or '').strip())
        except (ValueError, AttributeError):
            return await ctx.reply(
                t(gid, 'eco.pk_market_page', page=1, total=1)
                + f' · `;buy <id>` — for items use `;balls buy {listing} 1`',
                ephemeral=True)
        with db.conn_ctx() as conn:
            r = conn.execute('SELECT * FROM pk_market WHERE id=? AND guild_id=?',
                             (lid or 0, str(gid))).fetchone()
            if not r:
                return await ctx.reply(t(gid, 'eco.pk_market_gone'), ephemeral=True)
            r = dict(r)
        if str(r['seller_id']) == str(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.pk_market_own'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if r['price'] > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        first = not my_mons(gid, ctx.author.id)
        fee = r['price'] * 5 // 100
        set_cash(gid, ctx.author.id, b['cash'] - r['price'])
        sb = bal(gid, r['seller_id'])
        set_cash(gid, r['seller_id'], sb['cash'] + r['price'] - fee)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), r['dex'], r['level'], r['xp'],
                          r['shiny'], r['nick'], 1 if first else 0, r.get('ivs') or '', r.get('evs') or ''))
            conn.execute('DELETE FROM pk_market WHERE id=?', (r['id'],))
        row = _dex_row(r['dex'])
        view, files = self._mini(
            gid, (r['nick'] or (row.get('name') or '?').capitalize()),
            t(gid, 'eco.pk_market_bought',
              name=(r['nick'] or (row.get('name') or '?').capitalize()),
              price=cshort(r['price'])), r.get('dex'), 0x57F287)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='unlist', description='Zdejmij z targu')
    async def unlist(self, ctx, listing: int):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            r = conn.execute('SELECT * FROM pk_market WHERE id=? AND guild_id=? AND seller_id=?',
                             (listing or 0, str(gid), str(ctx.author.id))).fetchone()
            if not r:
                return await ctx.reply(t(gid, 'eco.pk_market_gone'), ephemeral=True)
            r = dict(r)
            first = not my_mons(gid, ctx.author.id)
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), r['dex'], r['level'], r['xp'],
                          r['shiny'], r['nick'], 1 if first else 0, r.get('ivs') or '', r.get('evs') or ''))
            conn.execute('DELETE FROM pk_market WHERE id=?', (r['id'],))
        row = _dex_row(r['dex'])
        view, files = self._mini(
            gid, (r['nick'] or (row.get('name') or '?').capitalize()),
            t(gid, 'eco.pk_unlisted'), r.get('dex'), 0x8A8F98)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='keep', description='Zabezpiecz pokemona')
    async def lock(self, ctx, *, arg: str = ''):
        gid = ctx.guild.id
        arg = (arg or '').strip()
        m = None
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        if not m and arg:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            m, _ = _match_mon(mons, spec_of, arg.lower())
            if not m:
                m = next((x for x in mons if (x.get('nick') or '').lower() == arg.lower()), None)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET locked=? WHERE id=?',
                         (0 if m.get('locked') else 1, m['id']))
        view, files = self._mini(gid, mon_name(m, gid),
                                         t(gid, 'eco.pk_unlocked' if m.get('locked') else 'eco.pk_locked2',
                                           name=mon_name(m, gid)),
                                         m.get('dex'), 0x8A8F98)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    def _set_buddy(self, gid, uid, mid: int):
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO pk_buddy (guild_id, user_id, mid, hearts) '
                         'VALUES (?,?,?,COALESCE((SELECT hearts FROM pk_buddy WHERE guild_id=? AND user_id=?),0))',
                         (str(gid), str(uid), mid, str(gid), str(uid)))

    @commands.command(name='buddy', description='Twój buddy')
    async def buddy(self, ctx, *, arg: str = ''):
        """Bare: buddy card. `set <name|slot>`: choose buddy. `<slot> [nick]`:
        legacy set (+rename). `<text>`: rename current buddy."""
        import aiohttp
        gid = ctx.guild.id
        uid = ctx.author.id
        parts = (arg or '').strip().split(None, 1)
        head = (parts[0].lower() if parts else '')
        rest = (parts[1] if len(parts) > 1 else '')
        if head == 'set':
            if not rest:
                return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
            if rest.strip().isdigit():
                m = get_mon(gid, uid, int(rest.strip()))
                if not m:
                    return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
                self._set_buddy(gid, uid, m['id'])
                view, files = self._mini(gid, mon_name(m, gid),
                                         t(gid, 'eco.pk_buddy_set', name=mon_name(m, gid)),
                                         m.get('dex'), 0xFF6FB5)
                return await ctx.reply(view=view, files=files or None, ephemeral=True)
            mons = my_mons(gid, uid)
            if not mons:
                return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()  # noqa: E731
            m, n = _match_mon(mons, spec_of, rest)
            if not m:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
            self._set_buddy(gid, uid, m['id'])
            msg = t(gid, 'eco.pk_buddy_set', name=mon_name(m, gid))
            if n > 1:
                slot_of = _box_slots(mons)
                msg += '\n' + t(gid, 'eco.pk_buddy_multi',
                                list=f'{n} match — #{slot_of.get(m["id"], "?")} taken')
            view, files = self._mini(gid, mon_name(m, gid), msg, m.get('dex'), 0xFF6FB5)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        if head.isdigit():
            m = get_mon(gid, uid, int(head))
            if not m:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
            self._set_buddy(gid, uid, m['id'])
            if rest.strip():
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE pk_mons SET nick=? WHERE id=?',
                                 (rest.strip()[:24], m['id']))
            extra = t(gid, 'eco.pk_nicked', name=rest.strip()[:24]) if rest.strip() else ''
            return await ctx.reply(t(gid, 'eco.pk_buddy_set', name=mon_name(m, gid))
                                   + (('\n' + extra) if extra else ''), ephemeral=True)
        if head:
            b = buddy_get(gid, uid)
            if not b.get('mid'):
                return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET nick=? WHERE id=?',
                             (arg.strip()[:24], b['mid']))
                cur = conn.execute('SELECT * FROM pk_mons WHERE id=?', (b['mid'],)).fetchone()
            m = dict(cur) if cur else {}
            return await ctx.reply(t(gid, 'eco.pk_nicked', name=arg.strip()[:24])
                                   + f" ({mon_name(m, gid)})", ephemeral=True)
        b = buddy_get(gid, uid)
        if not b.get('mid'):
            return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
        with db.conn_ctx() as conn:
            m = conn.execute('SELECT * FROM pk_mons WHERE id=?', (b['mid'],)).fetchone()
        if not m:
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM pk_buddy WHERE guild_id=? AND user_id=?',
                             (str(gid), str(uid)))
            return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
        m = dict(m)
        row = _dex_row(m['dex'])
        nat = nature_of(m)
        nat_txt = (f"**{nat[0]}** (+{STAT_LABEL[nat[1]]} / -{STAT_LABEL[nat[2]]})"
                   if nat[1] and nat[2] else f"**{nat[0]}** (neutral)")
        stats = calc_stats(row, m['level'], _ivs_of(m), nat, _evs_of(m))
        nxt = XP_NEXT(m['level'])
        rk, re, accent = rarity_of(row, bool(m['shiny']), gid)
        hearts = b.get('hearts', 0) or 0
        total_exp = sum(XP_NEXT(l) for l in range(max(0, m['level']))) + m['xp']
        to_next = max(1, nxt - m['xp'])
        if m['dex'] == 133:
            evo_line = ' · '.join(EEVEE_BRANCHES) + ' (stone evolutions)'
        else:
            evo_line = _evo_text(gid, row)
        ivs = _ivs_of(m) or {k: 31 for k in IV_KEYS}

        def _stars(k):
            iv = ivs.get(k, 31)
            n = 3 if iv >= 31 else (2 if iv >= 25 else (1 if iv >= 15 else 0))
            return '*' * n + '.' * (3 - n)

        def _stat(k):
            e = em(gid, STAT_EMO[k], k.upper())
            v = stats['maxhp'] if k == 'hp' else stats[k]
            return f"{e} {STAT_LABEL.get(k, k.upper())} {v}"

        iv_lines = '\n'.join(f"**{ka.upper()}** {_stars(ka)}   **{kb.upper()}** {_stars(kb)}"
                             for ka, kb in (('atk', 'dfn'), ('hp', 'spe'), ('spa', 'spd')))
        stat_lines = '\n'.join(f"{_stat(ka)}   {_stat(kb)}"
                               for ka, kb in (('atk', 'dfn'), ('hp', 'spe'), ('spa', 'spd')))
        desc = (f"Level: **{m['level']}**\n"
                f"Nature: {nat_txt}\n"
                f"Type: {types_str(gid, row.get('types') or ['?'])}\n"
                f"Friendship: {(em(gid, 'heart') or 'v') * min(10, hearts)} ({hearts})\n"
                f"Evolution: {evo_line}\n"
                f"**{to_next:,} EXP to Level {m['level'] + 1}**\n"
                f"{xp_bar(m['xp'], nxt, gid)} {m['xp']}/{nxt} XP\n"
                f"__**Pokemon IVs**__\n{iv_lines}\n"
                f"__**Pokemon Stats**__\n{stat_lines}")
        gif = showdown_gif(row.get('name', ''), bool(m['shiny']))
        if gif:
            try:
                to = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=to) as s3:
                    async with s3.head(gif) as r:
                        if r.status != 200:
                            gif = ''
            except Exception:
                gif = ''
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if m['shiny'] and len(spr) > 1 else spr[0]
        if form_of(m):
            img = form_sprite(m, bool(m['shiny'])) or img
        from discord.ui import LayoutView, Container, TextDisplay, MediaGallery, ActionRow, Section, Thumbnail
        from discord.ui.media_gallery import MediaGalleryItem
        layout = LayoutView(timeout=180)
        box = Container(accent_color=accent)
        title_txt = f"**{t(gid, 'eco.pk_buddy_title', user=ctx.author.display_name)}** — {mon_name(m, gid)}"
        head_done = False
        if img:
            try:
                box.add_item(Section(TextDisplay(title_txt), accessory=Thumbnail(media=str(img))))
                head_done = True
            except Exception:
                head_done = False
        if not head_done:
            box.add_item(TextDisplay(title_txt))
        box.add_item(TextDisplay(desc))
        if gif:
            try:
                box.add_item(MediaGallery(MediaGalleryItem(media=gif)))
            except Exception:
                pass
        box.add_item(TextDisplay(
            f"-# {mon_name(m, gid)} Level: {m['level']} • Total EXP: {total_exp:,}"
            f" • Buddy bonus: +50% XP share"))
        row1 = ActionRow()
        mv_btn = discord.ui.Button(label='Moves', style=discord.ButtonStyle.secondary,
                                   custom_id=f'pkbmoves:{m["id"]}',
                                   emoji=_btn_emoji(gid, 'btn_fight'))
        mv_btn.callback = self._mk_buddy_moves(gid, uid, m['id'])
        row1.add_item(mv_btn)
        chg = discord.ui.Button(label='Change', style=discord.ButtonStyle.secondary,
                                custom_id=f'pkbchg:{uid}',
                                emoji=_btn_emoji(gid, 'trade_swap'))
        chg.callback = self._mk_buddy_change(gid, uid)
        row1.add_item(chg)
        hlp = discord.ui.Button(label='Help', style=discord.ButtonStyle.secondary,
                                custom_id=f'pkbhelp:{uid}',
                                emoji=_btn_emoji(gid, 'dex_book'))
        hlp.callback = self._mk_buddy_help(gid, uid)
        row1.add_item(hlp)
        box.add_item(row1)
        layout.add_item(box)
        await ctx.reply(view=layout, ephemeral=True)

    def _mk_buddy_moves(self, gid, uid, mid: int):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer(ephemeral=True)
            with db.conn_ctx() as conn:
                r = conn.execute('SELECT * FROM pk_mons WHERE id=?', (mid,)).fetchone()
            if not r:
                return await ix.followup.send(t(gid, 'eco.pk_noslot'), ephemeral=True)
            import aiohttp as _aio
            async with _aio.ClientSession() as s:
                view = await self._moves_view(gid, dict(r), s)
            await ix.followup.send(view=view, ephemeral=True)
        return _cb

    def _mk_buddy_change(self, gid, uid):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.send_message(t(gid, 'eco.pk_buddy_change'), ephemeral=True)
        return _cb

    def _mk_buddy_help(self, gid, uid):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            from lang import get_lang
            en, pl = self.PK_USAGE.get('buddy', (';', ';'))
            await ix.response.send_message(
                view=self._layout(gid, ';buddy', pl if get_lang(gid) == 'pl' else en),
                ephemeral=True)
        return _cb

    @commands.command(name='team', description='Drużyna na pojedynki')
    async def team(self, ctx, *args):
        """;team — show. `;team 1 2 3` — set duel team. `;team give <item> <slot>` — hold item."""
        gid = ctx.guild.id
        # give subcommand: ;team give <item> <slot|dex>
        if args and str(args[0]).lower() == 'give':
            if len(args) < 3:
                return await ctx.reply(t(gid, 'eco.pk_team_give_use'), ephemeral=True)
            item_raw = str(args[1]).lower().replace(' ', '_')
            slot_raw = str(args[2])
            # normalize item
            key = item_raw
            if key not in EVO_STONES and key not in HELD_ITEMS:
                # try name lookup
                low = item_raw.replace('_', ' ')
                if low in EVO_STONE_BY_NAME:
                    key = EVO_STONE_BY_NAME[low]
                elif low in HELD_BY_NAME:
                    key = HELD_BY_NAME[low]
                elif key.replace('_', ' ') in EVO_STONE_BY_NAME:
                    key = EVO_STONE_BY_NAME[key.replace('_', ' ')]
            if key not in EVO_STONES and key not in HELD_ITEMS:
                return await ctx.reply(t(gid, 'eco.pk_held_bad', item=item_raw[:24]), ephemeral=True)
            # resolve mon by slot or dex
            m = None
            if slot_raw.isdigit():
                # slot may be box slot (1=first) or raw dex 133
                m = get_mon(gid, ctx.author.id, int(slot_raw))
                if not m:
                    # try dex fallback
                    try:
                        d = int(slot_raw)
                        mons = my_mons(gid, ctx.author.id)
                        m = next((x for x in mons if int(x['dex']) == d), None)
                    except Exception:
                        pass
            else:
                mons = my_mons(gid, ctx.author.id)
                spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
                m, _ = _match_mon(mons, spec_of, slot_raw.lower())
            if not m:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
            cur = held_key(m)
            if cur == key:
                lab = (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('name') or key
                emo = em(gid, (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('icon','')) 
                lab_str = f"{emo + ' ' if emo else ''}{lab}"
                return await ctx.reply(t(gid, 'eco.pk_team_give_held', item=lab_str), ephemeral=True)
            if not balls_take(gid, ctx.author.id, key):
                lab = (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('name') or key
                emo = em(gid, (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('icon',''))
                lab_str = f"{emo + ' ' if emo else ''}{lab}"
                return await ctx.reply(t(gid, 'eco.pk_team_give_bad', item=lab_str), ephemeral=True)
            if cur:
                balls_add(gid, ctx.author.id, cur, 1)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET held=? WHERE id=?', (key, m['id']))
            lab = (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('name') or key
            emo = em(gid, (HELD_ITEMS.get(key) or EVO_STONES.get(key) or {}).get('icon',''))
            lab_str = f"{emo + ' ' if emo else ''}{lab}"
            view, files = self._mini(gid, t(gid, 'eco.pk_team_title'),
                                     t(gid, 'eco.pk_team_give_ok', item=lab_str, name=mon_name(m,gid)), m.get('dex'), 0x58CC02)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        # numeric team set: ;team 1 2 3
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        # parse numeric args
        nums = []
        for a in args[:3]:
            try:
                nums.append(int(str(a).split()[0]))
            except Exception:
                nums.append(0)
        # pad to 3
        while len(nums) < 3:
            nums.append(0)
        a, b, c = nums[:3]
        if not a:
            team = team_get(gid, ctx.author.id)
            lines = []
            for i, m in enumerate(team, 1):
                spe = em(gid, _species_emoji_name(m.get('dex', 0)))
                lines.append(f"`{i}` {spe + ' ' if spe else ''}{mon_name(m, gid)} — Lv{m['level']}")
            return await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_team_title'),
                                                     '\n'.join(lines) + '\n' +
                                                     t(gid, 'eco.pk_team_use')), ephemeral=True)
        picks = []
        for s in (a, b, c):
            m = get_mon(gid, ctx.author.id, s or 0)
            if m and m['id'] not in picks:
                picks.append(m['id'])
        if not picks:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        picks += [0] * (3 - len(picks))
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO pk_team (guild_id, user_id, s1, s2, s3) VALUES (?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), *picks))
        view, files = self._mini(gid, t(gid, 'eco.pk_team_title'),
                                         t(gid, 'eco.pk_team_set', n=len([p for p in picks if p])),
                                         None, 0xFF4655)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='candy', description='Rare candy +1 level')
    async def candy(self, ctx):
        import aiohttp
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        act = next((m for m in mons if m['active']), None)
        if not act:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        if act['level'] >= 100:
            return await ctx.reply(t(gid, 'eco.pk_candy_max', name=mon_name(act, gid)), ephemeral=True)
        with db.conn_ctx() as conn:
            row = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='candy'",
                               (str(gid), str(ctx.author.id))).fetchone()
            if not row or not row['qty']:
                return await ctx.reply(t(gid, 'eco.pk_candy_none'), ephemeral=True)
            conn.execute("UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball='candy'",
                         (str(gid), str(ctx.author.id)))
        msgs = await self._gain_xp(gid, ctx.author.id, act['id'], act['level'] ** 3 - (act['xp'] or 0))
        body = '\n'.join(msgs) if msgs else t(gid, 'eco.pk_candy_use', name=mon_name(act, gid))
        candy_emo = em(gid, 'candy')
        view, files = self._mini(gid, f"{candy_emo + ' ' if candy_emo else ''}{mon_name(act, gid)}",
                                 body, act.get('dex'), 0xFFD43B)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='eggs', description='Jajka')
    async def eggs(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT id, cycles FROM pk_eggs WHERE guild_id=? AND owner_id=? ORDER BY id',
                (str(gid), str(ctx.author.id))).fetchall()]
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_eggs_none'), ephemeral=True)
        egg_emo = em(gid, 'egg')
        lines = []
        for r in rows:
            ready = r['cycles'] <= 0
            emo = em(gid, 'egg_crack' if ready else 'egg')
            lines.append((f"{emo + ' ' if emo else ''}") + t(gid, 'eco.pk_egg_row', lid=r['id'],
                           state=t(gid, 'eco.pk_egg_ready') if ready
                           else t(gid, 'eco.pk_egg_cycles', n=r['cycles'])))
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_eggs_title'),
                                          '\n'.join(lines) + '\n' + t(gid, 'eco.pk_eggs_hint')),
                        ephemeral=True)

    @commands.command(name='hatch', description='Wykluj jajko')
    async def hatch(self, ctx):
        import aiohttp
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            egg = conn.execute('SELECT id FROM pk_eggs WHERE guild_id=? AND owner_id=? AND cycles<=0 '
                               'ORDER BY id LIMIT 1', (str(gid), str(ctx.author.id))).fetchone()
            if not egg:
                return await ctx.reply(t(gid, 'eco.pk_eggs_none2'), ephemeral=True)
            egg_id = egg['id']
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 809)
                row = await dex_get(s, dex)
                if not row:
                    continue
                if row.get('legendary') and random.random() > 0.1:
                    continue
                break
            else:
                return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        shiny = random.randint(1, 64) == 1
        level = random.randint(1, 10)
        first = not my_mons(gid, ctx.author.id)
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM pk_eggs WHERE id=?', (egg_id,))
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,0,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), dex, level, 1 if shiny else 0, '',
                          1 if first else 0, _roll_ivs(), ''))
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        _, _, accent = rarity_of(row, shiny, gid)
        await ctx.reply(view=await self._mage(
            gid, t(gid, 'eco.pk_hatched_title'),
            t(gid, 'eco.pk_hatched', name=((em(gid, 'rarity_shiny') or '*') if shiny else '') + row['name'].capitalize(),
              level=level), img, accent))

    @commands.command(name='swap', description='Losowa wymiana')
    async def swap(self, ctx, *, arg: str = ''):
        import aiohttp
        gid = ctx.guild.id
        arg = (arg or '').strip()
        m = None
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        if not m and arg:
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            m, _ = _match_mon(mons, spec_of, arg.lower())
            if not m:
                m = next((x for x in mons if (x.get('nick') or '').lower() == arg.lower()), None)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m, gid)), ephemeral=True)
        old = mon_name(m, gid)
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 809)
                row = await dex_get(s, dex)
                if not row:
                    continue
                if row.get('legendary') and random.random() > 0.15:
                    continue
                break
            else:
                return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        shiny = random.randint(1, 100) == 1
        level = max(1, min(100, m['level'] + random.randint(-5, 5)))
        first = len(my_mons(gid, ctx.author.id)) <= 1
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,0,?,?,?,?,'')',
                         (str(gid), str(ctx.author.id), dex, level, 1 if shiny else 0, '',
                          1 if first else 0, _roll_ivs(), ''))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        _, _, accent = rarity_of(row, shiny, gid)
        await ctx.reply(view=await self._mage(
            gid, t(gid, 'eco.pk_swapped_title'),
            t(gid, 'eco.pk_swapped', old=old,
              name=((em(gid, 'rarity_shiny') or '*') if shiny else '') + row['name'].capitalize(), level=level), img, accent))

    @commands.command(name='target', description='Dzienny cel')
    async def target(self, ctx):
        gid = ctx.guild.id
        d = daily_row(gid, ctx.author.id)
        row = _dex_row(d['target'])
        rk, re, accent = rarity_of(row, False, gid) if d['target'] else ('', '', 0xFFFFFF)
        sec, f = _sec_row(
            gid, f"{re + ' ' if re else ''}" + t(
                gid, 'eco.pk_target',
                name=(row.get('name') or '?').capitalize() if d['target'] else '—',
                win=cshort(DAILY_TARGET_REWARD),
                state=t(gid, 'eco.pk_target_done') if d['claimed'] else t(gid, 'eco.pk_target_open')),
            d['target'] or None)
        tgt_emo = em(gid, 'hunt_target')
        await ctx.reply(view=self._wrap_sec(
            gid, f"{tgt_emo + ' ' if tgt_emo else ''}" + t(gid, 'eco.pk_target_title'),
            sec, accent), files=[f] if f else None, ephemeral=True)

    @commands.command(name='checklist', description='Dzienne zadania')
    async def checklist(self, ctx, action: str = ''):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        d = daily_row(gid, ctx.author.id)
        need, have = CHECKLIST_NEED, {'catches': d['catches'], 'battles': d['battles'], 'duels': d['duels']}
        done = all(have[k] >= need[k] for k in need)
        if (action or '').lower() == 'claim':
            if d['checklist']:
                return await ctx.reply(t(gid, 'eco.pk_check_claimed'), ephemeral=True)
            if not done:
                return await ctx.reply(t(gid, 'eco.pk_check_todo'), ephemeral=True)
            b = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, b['cash'] + CHECKLIST_REWARD)
            balls_add(gid, ctx.author.id, 'candy', 2)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_daily SET checklist=1 WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id)))
            return await ctx.reply(t(gid, 'eco.pk_check_done', win=cshort(CHECKLIST_REWARD)),
                                   ephemeral=True)
        lines = [t(gid, 'eco.pk_check_row', what=k, have=have[k], need=need[k],
                   ok=(em(gid, 'box_done') or 'x') if have[k] >= need[k]
                   else (em(gid, 'box_empty') or '-')) for k in need]
        if d['checklist']:
            lines.append(t(gid, 'eco.pk_check_claimed'))
        elif done:
            lines.append(t(gid, 'eco.pk_check_ready'))
        cal = em(gid, 'calendar')
        await ctx.reply(view=self._layout(
            gid, f"{cal + ' ' if cal else ''}" + t(gid, 'eco.pk_check_title'), '\n'.join(lines)),
            ephemeral=True)

    @commands.command(name='pokemon', description='Ręczny spawn')
    async def pokemon(self, ctx):
        """Alias of ;p — same catch encounter.
        (Spec: `;p` aka `;pokemon` they should do the same thing)."""
        return await self._hunt_core(ctx, 'catch')

    @commands.command(name='items', description='Twoje itemy')
    async def items(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=self._items_view(gid, ctx.author.id, ctx.author.display_name),
                        ephemeral=True)

    def _items_view(self, gid, uid, display_name: str = ''):
        """Pokemon bag card (balls, potions, extras, eggs). Shared by
        `;items` and `;inv` on the pokemon prefix."""
        b = balls_get(gid, uid)
        pots = potions_get(gid, uid)
        with db.conn_ctx() as conn:
            extra = {}
            for it in ('candy', 'grazz', 'incense', 'repel', *HELD_ORDER):
                row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                                   (str(gid), str(uid), it)).fetchone()
                extra[it] = (row['qty'] if row else 0) or 0
            eggs = conn.execute('SELECT COUNT(*) c FROM pk_eggs WHERE guild_id=? AND owner_id=?',
                                (str(gid), str(uid))).fetchone()['c']
        icons = {'poke': 'pokeball', 'great': 'greatball', 'ultra': 'ultraball',
                 'master': 'masterball', 'potion': 'potion', 'superpotion': 'potion',
                 'candy': 'candy', 'egg': 'egg', 'grazz': 'item_grazz',
                 'incense': 'item_incense', 'repel': 'item_repel', 'eggs': 'egg',
                 **{k: v['icon'] for k, v in HELD_ITEMS.items()}}

        def _bag(k, v):
            ei = em(gid, icons.get(k, ''))
            lab = (HELD_ITEMS.get(k) or {}).get('name') or k
            return f"• {ei + ' ' if ei else ''}{lab} x{v}"

        lines = ([_bag(k, v) for k, v in b.items() if v]
                 + [_bag(k, v) for k, v in pots.items() if v]
                 + [_bag(k, v) for k, v in extra.items() if v or k == 'incense']
                 + ([_bag('eggs', eggs)] if eggs else []))
        if incense_active(gid, uid):
            lines.append(t(gid, 'eco.pk_items_incense'))
        if repel_active(gid, uid):
            lines.append(t(gid, 'eco.pk_items_repel'))
        return self._layout(
            gid, t(gid, 'eco.pk_items_title', user=display_name),
            '\n'.join(lines) if lines else t(gid, 'eco.pk_items_empty'))

    @commands.command(name='evs', description='Trening EV (z walk)')
    async def evs(self, ctx, *, arg: str = ''):
        """EV panel: where this mon is trained, and how close to the caps."""
        gid = ctx.guild.id
        arg = (arg or '').strip()
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        m = None
        if arg.isdigit():
            m = get_mon(gid, ctx.author.id, int(arg))
        elif arg:
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()  # noqa: E731
            m, _n = _match_mon(mons, spec_of, arg)
        else:
            m = next((x for x in mons if x.get('active')), None) or mons[0]
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        evs = _evs_of(m)
        total = sum(evs.values())
        rows = []
        for k in EV_KEYS:
            ic = em(gid, STAT_EMO.get(k, ''))
            rows.append(f"{ic + ' ' if ic else ''}{EV_LABEL[k]} "
                        f"{xp_bar(evs[k], EV_MAX_STAT, gid)} {evs[k]}/{EV_MAX_STAT}")
        body = (t(gid, 'eco.pk_evs_head', name=mon_name(m, gid), total=total, max=EV_MAX_TOTAL)
                + '\n' + '\n'.join(rows)
                + '\n' + _held_line(gid, m))
        if not total:
            body += '\n' + t(gid, 'eco.pk_evs_none')
        body += '\n-# ' + t(gid, 'eco.pk_evs_hint', p=POWER_EV, cap=EV_MAX_STAT,
                            total=EV_MAX_TOTAL)
        view, files = self._mini(gid, t(gid, 'eco.pk_evs_title'), body, m.get('dex'), 0x58CC02)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='held', description='Trzymany item (equip)')
    async def held(self, ctx, slot: str = '', *, item: str = ''):
        """No args: bag + who holds what. `;held 3 life orb` equips (or `none`)."""
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        want = (item or '').strip().lower()
        slot = (slot or '').strip()
        if not slot:
            bag = held_bag(gid, ctx.author.id)
            owned = [f"{held_label(gid, k)} x{v}" for k, v in bag.items() if v]
            rows = []
            for i, mm in enumerate(mons[:10], start=1):
                lab = held_label(gid, held_key(mm))
                rows.append(f"`{i}` {mon_name(mm, gid)} — "
                            + (t(gid, 'eco.pk_held_line', item=lab) if lab
                               else t(gid, 'eco.pk_held_none')))
            body = (t(gid, 'eco.pk_held_bag',
                      list=' · '.join(owned) or t(gid, 'eco.pk_held_empty'))
                    + '\n' + '\n'.join(rows)
                    + '\n-# ' + t(gid, 'eco.pk_held_use'))
            view, files = self._mini(gid, t(gid, 'eco.pk_held_title'), body, None, 0x58CC02)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        if not slot.isdigit():
            return await ctx.reply(t(gid, 'eco.pk_held_use'), ephemeral=True)
        m = get_mon(gid, ctx.author.id, int(slot))
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        cur = held_key(m)
        if want in ('', 'none', 'off'):
            if not cur:
                return await ctx.reply(t(gid, 'eco.pk_held_none'), ephemeral=True)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET held=? WHERE id=?', ('', m['id']))
            balls_add(gid, ctx.author.id, cur, 1)
            return await ctx.reply(t(gid, 'eco.pk_held_removed', name=mon_name(m, gid),
                                     item=held_label(gid, cur)), ephemeral=True)
        key = HELD_BY_NAME.get(want, want)
        if key not in HELD_ITEMS:
            return await ctx.reply(t(gid, 'eco.pk_held_bad', item=(item or '?')[:24]),
                                   ephemeral=True)
        if cur == key:
            return await ctx.reply(t(gid, 'eco.pk_held_same', item=held_label(gid, key)),
                                   ephemeral=True)
        if not balls_take(gid, ctx.author.id, key):
            return await ctx.reply(t(gid, 'eco.pk_held_noitem', item=held_label(gid, key)),
                                   ephemeral=True)
        if cur:
            balls_add(gid, ctx.author.id, cur, 1)  # swap: the old one goes back in the bag
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET held=? WHERE id=?', (key, m['id']))
        view, files = self._mini(gid, t(gid, 'eco.pk_held_title'),
                                 t(gid, 'eco.pk_held_equipped', name=mon_name(m, gid),
                                   item=held_label(gid, key)), m.get('dex'), 0x58CC02)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='list', description='Shiny i legendy serwera')
    async def plist(self, ctx, what: str = 'shinies'):
        gid = ctx.guild.id
        what = (what or '').lower()
        with db.conn_ctx() as conn:
            if what.startswith('leg'):
                rows = [dict(r) for r in conn.execute(
                    'SELECT owner_id, dex, level FROM pk_mons WHERE guild_id=? ORDER BY id DESC LIMIT 20',
                    (str(gid),)).fetchall()]
                rows = [r for r in rows if (_dex_row(r['dex']) or {}).get('legendary')][:10]
                title = t(gid, 'eco.pk_list_legends')
            else:
                rows = [dict(r) for r in conn.execute(
                    'SELECT owner_id, dex, level FROM pk_mons WHERE guild_id=? AND shiny=1 '
                    'ORDER BY id DESC LIMIT 10', (str(gid),)).fetchall()]
                title = t(gid, 'eco.pk_list_shinies')
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_list_empty'), ephemeral=True)
        lines = []
        for r in rows:
            m = ctx.guild.get_member(int(r['owner_id']))
            nm = m.display_name if m else r['owner_id']
            d = _dex_row(r['dex'])
            spe = em(gid, _species_emoji_name(r['dex']))
            lines.append(f"• {spe + ' ' if spe else ''}{(d.get('name') or '?').capitalize()} "
                         f"Lv{r['level']} — {nm}")
        await ctx.reply(view=self._layout(gid, title, '\n'.join(lines)), ephemeral=True)

    @commands.command(name='grazz', description='Złota malina na encounter')
    async def grazz(self, ctx):
        gid = ctx.guild.id
        e = self._get_enc(gid, ctx.author.id)
        if not e:
            return await ctx.reply(t(gid, 'eco.pk_noenc'), ephemeral=True)
        if e.get('grazz'):
            return await ctx.reply(t(gid, 'eco.pk_grazz_already'), ephemeral=True)
        if not balls_take(gid, ctx.author.id, 'grazz'):
            return await ctx.reply(t(gid, 'eco.pk_grazz_none'), ephemeral=True)
        e['grazz'] = True
        await ctx.reply(t(gid, 'eco.pk_grazz_on'), ephemeral=True)

    @commands.command(name='repel', description='Mocniejsze dzikie 30 min')
    async def repel(self, ctx):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if POKE_SHOP_PRICES['repel'].amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - POKE_SHOP_PRICES['repel'].amount)
        balls_add(gid, ctx.author.id, 'repel', 1)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_balls SET expires=? WHERE guild_id=? AND user_id=? AND ball=?',
                         (int(time.time()) + REPEL_SECONDS, str(gid), str(ctx.author.id), 'repel'))
        await ctx.reply(t(gid, 'eco.pk_repel_on'), ephemeral=True)

    @commands.command(name='evolve', description='Ewoluuj ręcznie')
    async def evolve(self, ctx, slot: str = '', *, stone: str = ''):
        import aiohttp
        gid = ctx.guild.id
        # parse slot + optional stone (e.g. ;evolve 1 water stone)
        slot_n = 0
        s_arg = (stone or '').strip().lower()
        raw_slot = (slot or '').strip()
        if raw_slot.isdigit():
            slot_n = int(raw_slot)
        elif raw_slot:
            # slot may contain "1 water stone" if user omits split
            parts = raw_slot.split()
            if parts[0].isdigit():
                slot_n = int(parts[0])
                if not s_arg and len(parts) > 1:
                    s_arg = ' '.join(parts[1:]).lower()
            else:
                # non-numeric slot -> try name lookup later, keep stone as s_arg
                s_arg = (raw_slot + ' ' + s_arg).strip().lower()
                slot_n = 0
        low_raw = (raw_slot + ' ' + s_arg).strip().lower()
        # subcommands: info / set / buddy / no args -> help list
        if not raw_slot and not s_arg:
            # ;evolve with nothing -> show evolvable list UI (PokeMeow style)
            mons = my_mons(gid, ctx.author.id)
            if not mons:
                return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
            # find evolvables
            evolvables = []
            for mon in mons:
                d = int(mon.get('dex', 0) or 0)
                if d == 133:
                    evolvables.append(mon)
                    continue
                # need dex row for evo check (cached)
                row_e = _dex_row(d)
                if not row_e or not row_e.get('evo_to'):
                    continue
                # level threshold or stone? require level
                lvl_need = row_e.get('evo_level') or 0
                if lvl_need and mon['level'] >= lvl_need:
                    evolvables.append(mon)
                elif not lvl_need and row_e.get('evo_to'):
                    # stone evo without level (e.g. Eevee handled) -> show as evolvable if have stone or held
                    evolvables.append(mon)
            # build UI
            from discord.ui import LayoutView, Container, TextDisplay, Section, Thumbnail
            layout = LayoutView(timeout=60)
            box = Container(accent_color=0xFFD43B)
            try:
                av = str(ctx.author.display_avatar.with_size(64).url)
            except Exception:
                av = ''
            head = f"**{ctx.author.display_name}'s Evolvable Pokemon**\n-# The Pokemon below are ready to evolve!"
            help_lines = (
                "`;evolve {pokemonname}` for regular Pokemon\n"
                "`;evolve {shiny or golden} {pokemonname}` for Shinies/Goldens\n"
                "`;evolve buddy` for Mega Evolutions (prompts when you own multiple compatible stones)\n"
                "`;evolve {pokemonname or buddy} {X/Y/Z}` to choose a Mega form directly\n"
                "`;evolve info {pokemonname}` for all evolution stages and requirements\n"
                f"{em(gid,'mega_bracelet') or '🔷'} `;evolve set {{X/Y/Z}}` to highlight your preferred option in Mega prompts"
            )
            if av:
                try:
                    box.add_item(Section(TextDisplay(head), accessory=Thumbnail(media=av)))
                except Exception:
                    box.add_item(TextDisplay(head))
            else:
                box.add_item(TextDisplay(head))
            box.add_item(TextDisplay(help_lines))
            if not evolvables:
                box.add_item(TextDisplay("*No Pokemon ready to evolve right now.*"))
            else:
                for idx, mon in enumerate(evolvables[:10], start=1):
                    spe = em(gid, _species_emoji_name(mon.get('dex',0))) or ''
                    row_e = _dex_row(mon['dex'])
                    name = (row_e.get('name') or f"#{mon['dex']}").capitalize() if row_e else f"#{mon['dex']}"
                    # friendship hearts? buddy hearts /5
                    buddy = buddy_get(gid, ctx.author.id)
                    hearts = 0
                    if buddy.get('mid') == mon['id']:
                        hearts = buddy.get('hearts',0) or 0
                    hf = f"{em(gid,'heart') or '♥'} {hearts/2:.1f} / 5" if hearts else f"{em(gid,'heart') or '♥'} 0.5 / 5"
                    # exp
                    nxt = 1
                    try:
                        nxt = mon.get('xp',0)
                    except Exception:
                        pass
                    line = f"`{idx:02d}` {spe + ' ' if spe else ''}{name} | Lvl. {mon['level']} | EXP. {mon.get('xp',0):,} | {hf}"
                    box.add_item(TextDisplay(line))
            foot = (
                "Evolve a Pokemon by reaching its required level (if it has one)!\n"
                f"Some Pokemon require 5 {em(gid,'heart') or '♥'}'s, an {em(gid,'evolutionstone') or em(gid,'evo_burst') or '🔷'} , or a {em(gid,'mega') or '🔷'} Mega Stone to evolve.\n"
                "Higher IVs, levels + exp, and friendship are always inherited by the evolved form.\n"
                "Moves are inherited if you don't own the evolution or if you own the evolution and it has less moves than the mon you are evolving.\n"
                "[`;buddy help`] for more buddy info"
            )
            box.add_item(TextDisplay(foot))
            layout.add_item(box)
            return await ctx.reply(view=layout, ephemeral=True)
        if low_raw.startswith('info '):
            # ;evolve info <name>
            q = low_raw.split(' ',1)[1].strip()
            # try find mon by name or dex row
            mons = my_mons(gid, ctx.author.id)
            target_row = None
            target_mon = None
            if q.isdigit():
                target_row = _dex_row(int(q))
            else:
                # try mon name
                spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
                target_mon, _ = _match_mon(mons, spec_of, q)
                if target_mon:
                    target_row = _dex_row(target_mon['dex'])
                else:
                    # try dex by name via cache search
                    for d in range(1,494):
                        r = _dex_row(d)
                        if r and (r.get('name') or '').lower() == q:
                            target_row = r
                            break
            if not target_row:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
            # show evolution line
            line = _evo_text(gid, target_row)
            return await ctx.reply(view=self._layout(gid, f"Evo info — {(target_row.get('name') or q).capitalize()}", line), ephemeral=True)
        if low_raw.startswith('set '):
            pref = low_raw.split(' ',1)[1].strip().upper()[:1]
            if pref not in ('X','Y','Z'):
                return await ctx.reply("Use: `;evolve set {X/Y/Z}`", ephemeral=True)
            # store preference per user (simple db)
            with db.conn_ctx() as conn:
                conn.execute('INSERT OR REPLACE INTO pk_buddy (guild_id, user_id, mid, hearts) VALUES (?,?,COALESCE((SELECT mid FROM pk_buddy WHERE guild_id=? AND user_id=?),0), COALESCE((SELECT hearts FROM pk_buddy WHERE guild_id=? AND user_id=?),0))',
                             (str(gid), str(ctx.author.id), str(gid), str(ctx.author.id), str(gid), str(ctx.author.id)))
                # piggyback on hearts? store pref in separate table or ignore - just acknowledge
            return await ctx.reply(f"{em(gid,'mega_bracelet') or ''} Preferred Mega set to **{pref}**.", ephemeral=True)
        if low_raw in ('buddy', 'buddy info'):
            return await ctx.reply(view=self._layout(gid, "Mega Evolutions", "Mega evolutions use your buddy. `;buddy set <name>` then `;evolve buddy` (stub)."), ephemeral=True)
        if not slot_n and not s_arg:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        # resolve mon by slot or name
        m = get_mon(gid, ctx.author.id, slot_n) if slot_n else None
        if not m and s_arg and not slot_n:
            # try name lookup for cases like ;evolve eevee water_stone
            mons = my_mons(gid, ctx.author.id)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            m, _ = _match_mon(mons, spec_of, s_arg.split()[0])
            # if found by name, stone arg is remainder
            if m:
                s_arg = ' '.join(s_arg.split()[1:]).lower()
        if not m:
            # fallback: if slot_n failed, try name from raw_slot
            if raw_slot and not raw_slot.isdigit():
                mons = my_mons(gid, ctx.author.id)
                spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
                m, _ = _match_mon(mons, spec_of, raw_slot.split()[0].lower())
            if not m:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, m['dex'])
        # Eevee branching — stone required
        if int(m['dex']) == 133:
            # normalize stone arg: allow "water stone", "water_stone", "water"
            key = (s_arg or '').replace(' ', '_').lower()
            # also allow bare "water" -> water_stone
            if key and not key.endswith('_stone') and key + '_stone' in EVO_STONES:
                key = key + '_stone'
            if key in EVO_STONE_BY_NAME:
                key = EVO_STONE_BY_NAME[key]
            bag = balls_get(gid, ctx.author.id)
            held = held_key(m)
            def _have(k):
                return 1 if held == k else int(bag.get(k, 0) or 0)
            if not key or key not in EVO_STONES:
                # show all 8 requirements
                lines = [f"❌ Could not evolve your {em(gid, 'p133') or ''} Eevee.".strip()]
                for sk in STONE_ORDER:
                    info = EVO_STONES[sk]
                    target = info['mons'][133]
                    have = _have(sk)
                    emo = em(gid, info['icon']) or ''
                    # target name via dex row
                    trow = _dex_row(target)
                    tname = (trow.get('name') or f'#{target}').capitalize()
                    spe = em(gid, _species_emoji_name(target)) or ''
                    lines.append(f"You need a {emo + ' ' if emo else ''}{info['name']} to evolve into {spe + ' ' if spe else ''}{tname}. You have {have}.")
                lines.append(f"The required item can be held by the Pokemon as well: `;team give {{item_name}} {slot_n or 133}`.")
                lines.append(f"Ways to get these items: Obtained randomly as a `;quest` reward (all quests) or `;balls buy <stone>`.")
                return await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_evolve_title', name=mon_name(m, gid)),
                                                          '\n'.join(lines)), ephemeral=True)
            # stone specified — validate it is for Eevee
            if 133 not in EVO_STONES[key]['mons']:
                return await ctx.reply(t(gid, 'eco.pk_evolve_stone_bad', stone=(EVO_STONES.get(key, {}).get('name') or key)), ephemeral=True)
            have = _have(key)
            if not have:
                info = EVO_STONES[key]
                emo = em(gid, info['icon']) or ''
                trow = _dex_row(info['mons'][133])
                tname = (trow.get('name') or f'#{info["mons"][133]}').capitalize()
                spe = em(gid, _species_emoji_name(info['mons'][133])) or ''
                return await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_evolve_title', name=mon_name(m, gid)),
                    f"You need a {emo + ' ' if emo else ''}{info['name']} to evolve into {spe + ' ' if spe else ''}{tname}. You have 0."), ephemeral=True)
            # consume stone: held first, else bag
            if held == key:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE pk_mons SET held=? WHERE id=?', ('', m['id']))
            else:
                balls_take(gid, ctx.author.id, key)
            target = EVO_STONES[key]['mons'][133]
            async with aiohttp.ClientSession() as s:
                nxt = await dex_get(s, target)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET dex=? WHERE id=?', (target, m['id']))
            burst = em(gid, 'evo_burst')
            view, files = self._mini(
                gid, f"{burst + ' ' if burst else ''}{((em(gid, 'rarity_shiny') or '*') if m.get('shiny') else '')}" + nxt['name'].capitalize(),
                t(gid, 'eco.pk_evolve', old=mon_name(m, gid),
                  new=((em(gid, 'rarity_shiny') or '*') if m.get('shiny') else '') + nxt['name'].capitalize()),
                target, 0xFFD43B)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        if not row.get('evo_to'):
            return await ctx.reply(t(gid, 'eco.pk_evolve_max', name=mon_name(m, gid)), ephemeral=True)
        if not row.get('evo_level'):
            return await ctx.reply(t(gid, 'eco.pk_evolve_stone', name=mon_name(m, gid)), ephemeral=True)
        if m['level'] < row['evo_level']:
            return await ctx.reply(t(gid, 'eco.pk_evolve_soon', name=mon_name(m, gid),
                                     level=row['evo_level']), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            nxt = await dex_get(s, row['evo_to'])
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET dex=? WHERE id=?', (row['evo_to'], m['id']))
        burst = em(gid, 'evo_burst')
        view, files = self._mini(
            gid, f"{burst + ' ' if burst else ''}{((em(gid, 'rarity_shiny') or '*') if m.get('shiny') else '')}" + nxt['name'].capitalize(),
            t(gid, 'eco.pk_evolve', old=mon_name(m, gid),
              new=((em(gid, 'rarity_shiny') or '*') if m.get('shiny') else '') + nxt['name'].capitalize()),
            row['evo_to'], 0xFFD43B)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    @commands.command(name='streaks', description='Twoje serie')
    async def streaks(self, ctx, member: discord.Member = None):
        gid = ctx.guild.id
        member = member or ctx.author
        with db.conn_ctx() as conn:
            r = conn.execute('SELECT catch_streak, best_streak FROM pk_stats WHERE guild_id=? AND user_id=?',
                             (str(gid), str(member.id))).fetchone()
            h = conn.execute('SELECT target, streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                             (str(gid), str(member.id))).fetchone()
        cs = (r['catch_streak'] if r else 0) or 0
        best = (r['best_streak'] if r else 0) or 0
        ht = ''
        if h and h['target']:
            row = _dex_row(h['target'])
            denom = max(32, SHINY_ODDS - (h['streak'] or 0) * 4)
            ht = '\n' + t(gid, 'eco.pk_hunt_status', name=(row.get('name') or '?').capitalize(),
                          streak=h['streak'] or 0, denom=denom)
        flame = em(gid, 'streak_flame')
        await ctx.reply(view=self._layout(
            gid, f"{flame + ' ' if flame else ''}"
                 + t(gid, 'eco.pk_streaks_title', user=member.display_name),
            t(gid, 'eco.pk_streaks', cur=cs, best=best) + ht), ephemeral=True)

    @commands.command(name='code', description='Kody eventowe')
    async def code(self, ctx, sub: str = '', a: str = '', b: str = ''):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        sub = (sub or '').lower()
        if sub == 'create':
            if not db.is_house(ctx.author.id):
                return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
            kind = (a or '').lower()
            try:
                amount = int(b or 0)
            except Exception:
                amount = 0
            if kind not in ('cash', 'balls') or amount <= 0:
                return await ctx.reply(t(gid, 'eco.pk_code_use'), ephemeral=True)
            import secrets as _s
            c = 'BABKA-' + _s.token_hex(3).upper()
            with db.conn_ctx() as conn:
                conn.execute('INSERT OR REPLACE INTO pk_codes (code, kind, amount, uses_left) '
                             'VALUES (?,?,?,?)', (c, kind, amount, 1))
            return await ctx.reply(t(gid, 'eco.pk_code_made', code=c), ephemeral=True)
        c = (sub or '').upper()
        if not c:
            return await ctx.reply(t(gid, 'eco.pk_code_use'), ephemeral=True)
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM pk_codes WHERE code=?', (c,)).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'eco.pk_code_bad'), ephemeral=True)
            row = dict(row)
            if (row['uses_left'] or 0) <= 0:
                return await ctx.reply(t(gid, 'eco.pk_code_empty'), ephemeral=True)
            got = conn.execute('SELECT 1 FROM pk_redeemed WHERE code=? AND user_id=?',
                               (c, str(ctx.author.id))).fetchone()
            if got:
                return await ctx.reply(t(gid, 'eco.pk_code_used'), ephemeral=True)
            conn.execute('UPDATE pk_codes SET uses_left=uses_left-1 WHERE code=?', (c,))
            conn.execute('INSERT INTO pk_redeemed (code, user_id) VALUES (?,?)', (c, str(ctx.author.id)))
        if row['kind'] == 'balls':
            balls_add(gid, ctx.author.id, 'poke', row['amount'])
            view, files = self._mini(gid, t(gid, 'eco.pk_code_title'),
                                     t(gid, 'eco.pk_code_balls', n=row['amount']), None, 0xFFD43B)
            return await ctx.reply(view=view, files=files or None, ephemeral=True)
        x = bal(gid, ctx.author.id)
        set_cash(gid, ctx.author.id, x['cash'] + row['amount'])
        view, files = self._mini(gid, t(gid, 'eco.pk_code_title'),
                                 t(gid, 'eco.pk_code_cash', win=cshort(row['amount'])), None, 0x57F287)
        await ctx.reply(view=view, files=files or None, ephemeral=True)

    PK_USAGE = {
        'starter': ('Pick your first pokemon. `;starter` opens the picker, `;starter charmander` picks directly.',
                    'Wybierz pierwszego pokemona. `;starter` pokazuje wybór, `;starter charmander` wybiera od razu.'),
        'hunt': ('Fight-first wild encounter (60s cooldown). FIGHT it or throw balls.',
                 'Starcie z dzikim (60s cooldown). FIGHT albo rzuć ball.'),
        'p': ('Catch-only wild encounter (60s cooldown). Balls only, no FIGHT row.',
              'Spotkanie tylko do łapania (60s cooldown). Same balle, bez FIGHT.'),
        'pokemon': ('Manual shared spawn for the channel. Guess mystery ones with `;guess`.',
                    'Ręczny spawn dla kanału. Tajemnicze zgaduj przez `;guess`.'),
        'catch': ('Throw at your active encounter. `;catch ultra` picks the ball.',
                  'Rzuć w aktywne spotkanie. `;catch ultra` wybiera ball.'),
        'guess': ('Name a mystery encounter to catch it free. Exact species name.',
                  'Nazwij tajemnicze spotkanie, żeby złapać za darmo. Dokładna nazwa.'),
        'hint': ('Reveals half the letters of a mystery encounter.',
                 'Odsłania połowę liter tajemniczego spotkania.'),
        'balls': ('Item shop: balls, potions, candy, eggs, lures. `;balls buy <name|id> [n]`, `;balls info <name|id>`.',
                  'Sklep: balle, potiony, candy, jajka, lury. `;balls buy <nazwa|nr> [n]`, `;balls info <nazwa|nr>`.'),
        'box': ('Your collection. Sorts by rarity; filters: region, tier, type, fav. `;box 2`, `;box hoenn`.',
                'Twoja kolekcja. Sortuje po rzadkości; filtry: region, tier, typ, fav. `;box 2`, `;box hoenn`.'),
        'mon': ('Detail card: stats, IV, moves, evolution. `;mon <slot>`.',
                'Karta: staty, IV, ruchy, ewolucja. `;mon <slot>`.'),
        'active': ('Set your fighter. `;active` opens the picker, `;active 3` picks directly.',
                   'Ustaw wojownika. `;active` pokazuje wybór, `;active 3` wybiera od razu.'),
        'fav': ('Toggle favorite: never auto-sold or released. `;fav <slot>`.',
                'Oznacz ulubieńca: ochrona przed sprzedażą. `;fav <slot>`.'),
        'name': ('Nickname a mon. `;name <slot> <nick>`.',
                 'Nazwij pokemona. `;name <slot> <nick>`.'),
        'dex': ('Pokedex completion with cash milestones.',
                'Postęp Pokedeksu z nagrodami.'),
        'release': ('Release one mon back for cash. `;release <slot>`.',
                    'Wypuść pokemona za kasę. `;release <slot>`.'),
        'releaseall': ('Release every mon matching a name. `;releaseall pikachu`.',
                       'Wypuść wszystkie o danej nazwie. `;releaseall pikachu`.'),
        'keep': ('Lock a mon against release and market. `;keep <slot>`.',
                 'Zabezpiecz pokemona. `;keep <slot>`.'),
        'buddy': ('Buddy card + XP share. `;buddy set <name|slot>`.',
                  'Karta buddy + XP. `;buddy set <nazwa|slot>`.'),
        'battle': ('Fight your active encounter. `;battle [slot]` picks the lead.',
                   'Walcz z aktywnym spotkaniem. `;battle [slot]` wybiera prowadzącego.'),
        'duel': ('PvP against a trainer. `;duel @user [wager]`.',
                 'PvP z trenerem. `;duel @typ [stawka]`.'),
        'npc': ('Ladder: joey, finn, cyntia. `;npc joey`.',
                'Drabinka: joey, finn, cyntia. `;npc joey`.'),
        'team': ('Your 3-mon duel team. `;team 1 2 3` or `;team give <stone|item> <slot>` to hold.', 
                 'Drużyna 3 na pojedynki. `;team 1 2 3` lub `;team give <kamień|item> <slot>`.'),
        'moves': ('Learnset of a mon. `;moves <slot>`.',
                  'Ruchy pokemona. `;moves <slot>`.'),
        'move': ('Move lookup. `;move <name>`.',
                 'Opis ruchu. `;move <nazwa>`.'),
        'evolve': ('Evolve a ready mon. `;evolve <slot>` (level) or `;evolve <slot> <stone>` for Eevee → Vaporeon etc. Stones via `;balls` or `;team give`.',
                   'Ewoluuj gotowego. `;evolve <slot>` (level) lub `;evolve <slot> <kamień>` dla Eevee. Kamienie `;balls` lub `;team give`.'),
        'candy': ('Rare candy: +1 level now. `;candy <slot>` / `;rarecandy`.',
                  'Rzadki cukierek: +1 level od razu. `;candy <slot>` / `;rarecandy`.'),
        'eggs': ('Your incubating eggs (max 3).',
                 'Twoje jajka (max 3).'),
        'hatch': ('Hatch a ready egg. `;hatch`.',
                  'Wykluj gotowe jajko. `;hatch`.'),
        'trade': ('Direct trade. `;trade @user <yours> <theirs>`.',
                  'Wymiana. `;trade @typ <twój> <jego>`.'),
        'market': ('Player listings browser.',
                   'Przeglądarka ofert graczy.'),
        'sell': ('List a mon. `;sell <slot> <price>`.',
                 'Wystaw pokemona. `;sell <slot> <cena>`.'),
        'buy': ('Buy a listing. `;buy <id>`.',
                'Kup ofertę. `;buy <id>`.'),
        'unlist': ('Take down your listing. `;unlist <id>`.',
                   'Zdejmij ofertę. `;unlist <id>`.'),
        'swap': ('Random exchange for one of yours. `;swap <slot>`.',
                 'Losowa wymiana. `;swap <slot>`.'),
        'quests': ('Three quest slots with cash rewards.',
                   'Trzy questy z nagrodami.'),
        'target': ('Shiny-hunt target: streak shortens odds. `;target <name>`.',
                   'Cel shiny: seria skraca szanse. `;target <nazwa>`.'),
        'shinyhunt': ('Show target and streak.',
                      'Pokaż cel i serię.'),
        'checklist': ('Daily tasks for a cash bonus.',
                      'Dzienne zadania za bonus.'),
        'streaks': ('Catch and battle streaks.',
                    'Serie łapania i walk.'),
        'trainer': ('Your trainer stats card.',
                    'Karta twoich statystyk.'),
        'trainers': ('Server leaderboards.',
                     'Rankingi serwera.'),
        'items': ('Your bag: balls, potions, lures.',
                  'Plecak: balle, potiony, lury.'),
        'evs': ('EV training: what battles train, caps and power items. `;evs <slot|name>`.',
                'Trening EV: co dają walki, limity i Power itemy. `;evs <slot|nazwa>`.'),
        'held': ('Held items: equip, swap or drop one. `;held <slot> <item|none>`.',
                 'Trzymane itemy: założ, zmień lub zdejmij. `;held <slot> <item|none>`.'),
        'list': ('Server shiny and legendary showcase.',
                 'Gablota shiny i legend serwera.'),
        'grazz': ('Meadow lure: buy and use for grass spawns.',
                  'Przynęta łąkowa: kup i użyj.'),
        'repel': ('Repel: stronger encounters for 30 min.',
                  'Odstraszacz: mocniejsze spotkania 30 min.'),
        'code': ('Redeem an event code. `;code <code>`.',
                 'Zrealizuj kod eventu. `;code <kod>`.'),
        'phelp': ('This hub. `;phelp <command>` for usage.',
                  'Ten poradnik. `;phelp <komenda>` po użycie.'),
        'pokedex': ('Alias of ;dex.', 'Alias ;dex.'),
        'quest': ('Alias of ;quests.', 'Alias ;quests.'),
        'rarecandy': ('Alias of ;candy.', 'Alias ;candy.'),
        'catchbot': ('Autospawn catchbot status.', 'Status catchbota.'),
        'autospawn': ('Autospawn channels. `;autospawn add #ch`.', 'Kanały autospawnu. `;autospawn add #ch`.'),
        'vote': ('Vote for rewards. `;vote`.', 'Głosuj po nagrody. `;vote`.'),
        'news': ('Bot news.', 'Aktualności.'),
        'lootbox': ('Lootbox info.', 'Lootbox.'),
        'incense': ('Shiny incense 30 min. `;incense`.', 'Incense 30 min. `;incense`.'),
        'time': ('Server time.', 'Czas serwera.'),
        'invite': ('Bot invite link.', 'Link zaproszenia.'),
        'lottery': ('Lottery pot.', 'Loteria.'),
        'clan': ('Clan system.', 'Klan.'),
        'pokelure': ('Poke Lure 15 min.', 'Poke Lure 15 min.'),
        'mistyslure': ('Misty Lure 15 min.', 'Misty Lure 15 min.'),
        'seaflute': ('Sea Flute 15 min.', 'Morski flet 15 min.'),
        'fish': ('Fish water spawns. `;fish`.', 'Wędkowanie. `;fish`.'),
        'exclusives': ('Server exclusives.', 'Ekskluzywne.'),
        'research': ('Research tasks.', 'Badania.'),
        'balldex': ('Your ball & item dex. `;balldex`.', 'Twój balldex. `;balldex`.'),
        'faction': ('Faction.', 'Frakcja.'),
        'grs': ('Global Ranking System.', 'Globalny ranking.'),
        'channels': ('Spawn channels.', 'Kanały spawnu.'),
        'lastseen': ('Last seen. `;lastseen @user`.', 'Ostatnio widziany. `;lastseen @user`.'),
        'safarizone': ('Safari Zone.', 'Safari.'),
        'berry': ('Berry usage. `;berry <name> <slot>`.', 'Jagody. `;berry <nazwa> <slot>`.'),
        'vivillon': ('Vivillon patterns.', 'Wzory Vivillon.'),
        'serverdex': ('Server dex progress. `;serverdex`.', 'Postęp serwera. `;serverdex`.'),
        'gym': ('Gyms. `;gym [name]`.', 'Sale. `;gym [nazwa]`.'),
        'elitefour': ('Elite Four.', 'Elite Four.'),
        'champion': ('Champion.', 'Champion.'),
        'challenges': ('Challenges.', 'Wyzwania.'),
        'megachamber': ('Mega Chamber.', 'Komnata Mega.'),
        'battletower': ('Battle Tower.', 'Wieża walk.'),
        'worldboss': ('World Boss.', 'World Boss.'),
        'powerstation': ('Power Station.', 'Elektrownia.'),
        'battlefrontier': ('Battle Frontier.', 'Frontier walk.'),
        'coins': ('Alias of ;bal.', 'Alias ;bal.'),
        'give': ('Alias of ;pay.', 'Alias ;pay.'),
        'highscores': ('Highscores overview.', 'Wyniki.'),
        'perks': ('Perks & bonuses.', 'Perki.'),
        'patreon': ('Patreon.', 'Patreon.'),
        'contests': ('Contests.', 'Konkursy.'),
        'sync': ('Sync.', 'Sync.'),
        'unlocks': ('Unlocks.', 'Odblokowania.'),
        'privacy': ('Privacy.', 'Prywatność.'),
        'claim': ('Claim checklist. `;claim`.', 'Odbierz `;claim`.'),
        'counter': ('Mon counter. `;counter`.', 'Licznik. `;counter`.'),
        'auction': ('Auction alias market.', 'Aukcja alias market.'),
        'collection': ('Alias of ;box.', 'Alias ;box.'),
        'token': ('Token.', 'Token.'),
        'captcha': ('Captcha.', 'Captcha.'),
        'tcg': ('TCG packs.', 'TCG.'),
        'notifications': ('Notif toggle.', 'Powiadomienia.'),
        'events': ('Events.', 'Wydarzenia.'),
        'promo': ('Promo code alias. `;promo <code>`.', 'Kod promo alias. `;promo <kod>`.'),
        'appeal': ('Appeal.', 'Odwołanie.'),
        'explore': ('Explore.', 'Eksploruj.'),
        'rps': ('Rock Paper Scissors. `;rps <choice>`.', 'Papier kamień nożyce. `;rps <wybór>`.'),
        'wtp': ('Who is that Pokemon. `;wtp`.', 'Kto to pokemon. `;wtp`.'),
        'gif': ('Gif level 10+.', 'Gif poziom 10+.'),
    }

    @commands.command(name='phelp', description='Pomoc pokemon')
    async def phelp(self, ctx, *, cmd: str = ''):
        gid = ctx.guild.id
        q = (cmd or '').lower().strip().lstrip(';.!')
        if q:
            from lang import get_lang
            use_pl = get_lang(gid) == 'pl'
            if q in self.PK_USAGE:
                en, pl = self.PK_USAGE[q]
                return await ctx.reply(view=self._layout(
                    gid, f';{q}', (pl if use_pl else en)), ephemeral=True)
            return await ctx.reply(
                t(gid, 'eco.pk_h_unknown', q=q[:24]), ephemeral=True)
        secs = [
            ('btn_ball', t(gid, 'eco.pk_h_catch'),
             'starter · hunt · p · pokemon · catch · guess · hint · balls · pokedex · berry'),
            ('box_box', t(gid, 'eco.pk_h_box'),
             'box · mon · active · fav · name · dex · pokedex · release · releaseall · keep · buddy · collection · counter'),
            ('btn_fight', t(gid, 'eco.pk_h_battle'),
             'battle · duel · npc · team · moves · move · evolve · candy · rarecandy · eggs · hatch · gym · elitefour · champion · battletower · worldboss · powerstation · battlefrontier · megachamber · challenges'),
            ('trade_swap', t(gid, 'eco.pk_h_trade'),
             'trade · market · sell · buy · unlist · swap · auction · fish · pokelure · mistyslure · seaflute · safarizone'),
            ('quest_scroll', t(gid, 'eco.pk_h_prog'),
             'quests · quest · target · shinyhunt · hunt · checklist · streaks · trainer · trainers · research · balldex · serverdex · vivillon · exclusives · grs · faction · channels · lastseen · time · incense · repel · grazz'),
            ('dex_book', t(gid, 'eco.pk_h_extra'),
             'items · evs · held · list · catchbot · autospawn · vote · news · lootbox · lottery · clan · coins · give · highscores · perks · patreon · contests · sync · unlocks · privacy · claim · invite · events · promo · bonuses · code · phelp · rps · wtp · gif · explore · tcg · token · captcha · notifications · appeal'),
        ]
        parts = []
        for icon, title, cmds in secs:
            ei = em(gid, icon)
            chips = ' '.join(f'`;{c}`' for c in cmds.split(' · '))
            parts.append(f'{ei + " " if ei else ""}**{title}**\n{chips}')
        from discord.ui import LayoutView, Container, TextDisplay, Section, Thumbnail
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0x58CC02)
        try:
            ava = str(self.bot.user.display_avatar.url)
        except Exception:
            ava = ''
        head_txt = f"**{t(gid, 'eco.pk_h_title')}**\n-# {t(gid, 'eco.pk_h_sub')}"
        if ava:
            box.add_item(Section(TextDisplay(head_txt), accessory=Thumbnail(media=ava)))
        else:
            box.add_item(TextDisplay(head_txt))
        for part in parts:
            box.add_item(TextDisplay(part))
        box.add_item(TextDisplay(
            f"-# {t(gid, 'eco.pk_h_note')}\n-# {t(gid, 'eco.pk_h_note2')}"))
        layout.add_item(box)
        await ctx.reply(view=layout, ephemeral=True)

    # ----- battles -----

    async def _fighter(self, session, mon: dict, level: int = None, gid=0):
        row = await dex_get(session, mon['dex'] if 'dex' in mon else mon)
        form = form_of(mon)
        if form:
            row = dict(row, name=form['name'].lower(), types=list(form['types']),
                       hp=form['stats']['hp'], atk=form['stats']['atk'],
                       dfn=form['stats']['dfn'], spa=form['stats']['spa'],
                       spd=form['stats']['spd'], spe=form['stats']['spe'],
                       legendary=1)
        lv = mon.get('level', level or 5)
        nature = nature_of(mon) if isinstance(mon, dict) and mon.get('id') else None
        stats = calc_stats(row, lv, _ivs_of(mon), nature, _evs_of(mon))
        hk = held_key(mon)
        if HELD_ITEMS.get(hk, {}).get('effect') == 'ev':
            stats['spe'] = max(1, stats['spe'] // 2)  # power items cost speed
        return {'name': mon_name(mon, gid) if 'owner_id' in mon or 'nick' in mon else row['name'].capitalize(),
                'dex': mon.get('dex', mon), 'level': lv, 'types': row.get('types') or ['normal'],
                'stats': stats, 'hp': stats['maxhp'], 'held': hk,
                'moves': await moveset_for(session, mon.get('dex', mon), lv,
                                           seed=(mon.get('id') if isinstance(mon, dict) and mon.get('id') else None)),
                'shiny': mon.get('shiny', 0), 'row': row, 'form': (mon.get('form') or '')}

    def _fighter_picker(self, gid, uid, mode: str = 'battle'):
        """Team picker. mode='battle': pick lead + start. mode='active': just set active."""
        from discord.ui import ActionRow, Container, TextDisplay
        from discord.ui import LayoutView
        mons = my_mons(gid, uid)[:10]
        layout = LayoutView(timeout=120)
        box = Container(accent_color=0xFF4655)
        lines = []
        for i, m in enumerate(mons, start=1):
            star = (em(gid, 'slot_active') or '*') if m.get('active') else ''
            spe = em(gid, _species_emoji_name(m.get('dex', 0)))
            base = rarity_of(_dex_row(m['dex']), False)[0]
            fav = ' (fav)' if m.get('fav') else ''
            evo = ''
            _erow = _dex_row(m['dex'])
            if (_erow.get('evo_to') or 0) and m['level'] >= (_erow.get('evo_level') or 999):
                evo = (em(gid, 'up') or '') + ' ' if em(gid, 'up') else ''
            lines.append(f"`{i}` #{m['dex']} {_tier_letter(gid, base)} {spe + ' ' if spe else ''}"
                         f"{mon_name(m, gid)} — Lv{m['level']}{star}{fav} {evo}".rstrip())
        hint = ('\n-# Tap a button, or use `;battle <number>` / `;active <number>`'
                if mode == 'battle' else '\n-# Tap a button, or use `;active <number>`')
        box.add_item(TextDisplay('## Choose your fighter\n' + '\n'.join(lines) + hint))
        row = None
        for i, m in enumerate(mons):
            if i % 5 == 0:
                row = ActionRow()
                box.add_item(row)
            spe = _species_btn_emoji(gid, m.get('dex', 0))
            nm = mon_name(m)
            label = f"{i + 1} Lv{m['level']}" if spe else f"{i + 1}. {nm[:14]} Lv{m['level']}"[:80]
            b = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if m.get('active')
                else discord.ButtonStyle.secondary,
                custom_id=f'pklead:{uid}:{m["id"]}',
                emoji=spe if spe else _btn_emoji(gid, _picker_emoji_name(m, bool(m.get('active')))))
            b.callback = self._mk_lead_btn(gid, uid, m['id'], mode)
            row.add_item(b)
        layout.add_item(box)
        return layout

    def _mk_lead_btn(self, gid, uid, mid: int, mode: str = 'battle'):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            # Picker is single-use: strip it so a second tap can't start
            # (or re-set) over the choice that is already resolving.
            try:
                await ix.message.edit(view=None)
            except Exception:
                pass
            await ix.response.defer()
            if mode == 'active':
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE pk_mons SET active=0 WHERE guild_id=? AND owner_id=?',
                                 (str(gid), str(ix.user.id)))
                    conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (mid,))
                mons = my_mons(gid, ix.user.id)
                m = next((x for x in mons if x['id'] == mid), None)
                return await ix.followup.send(
                    t(gid, 'eco.pk_active', name=mon_name(m, gid) if m else '?'), ephemeral=True)
            await self._start_battle(ix, gid, ix.user, lead_mid=mid)
        return _cb

    async def _start_battle(self, ix_or_ctx, gid, user, lead_mid: int = 0):
        import aiohttp
        is_ix = isinstance(ix_or_ctx, discord.Interaction)
        e = self._get_enc(gid, user.id)
        if not e:
            msg = t(gid, 'eco.pk_noenc')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        mons = my_mons(gid, user.id)
        act = None
        if lead_mid:
            act = next((m for m in mons if m['id'] == lead_mid), None)
        if not act:
            act = next((m for m in mons if m['active']), None) or (mons[0] if mons else None)
        if not act:
            msg = t(gid, 'eco.pk_need_starter')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        key = (str(gid), str(user.id))
        if key in self._battle and not self._battle[key].get('starting'):
            msg = t(gid, 'eco.pk_busy')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        # Claim the slot before slow sprite/learnset fetches: a double FIGHT
        # used to overwrite (orphaning the first card) or interleave state.
        self._battle[key] = {'starting': True}
        try:
            async with aiohttp.ClientSession() as s:
                me = await self._fighter(s, act, gid=gid)
                wild = await self._fighter(s, {'dex': e['dex'], 'level': e['level'],
                                               'shiny': e['shiny'], 'nick': ''}, gid=gid)
                me_spr = await fetch_sprite(
                    s, form_sprite(act, bool(me['shiny']))
                    or pix_url(me['dex'], bool(me['shiny']), back=True))
                wild_spr = await fetch_sprite(s, pix_url(e['dex'], bool(wild['shiny'])))
        except Exception:
            if self._battle.get(key, {}).get('starting'):
                self._battle.pop(key, None)
            msg = t(gid, 'eco.pk_api')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        wild['hp'] = e['hp']
        # picked lead becomes the active mon so bench display stays correct
        try:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET active=0 WHERE guild_id=? AND owner_id=?',
                             (str(gid), str(user.id)))
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (act['id'],))
        except Exception:
            pass
        key = (str(gid), str(user.id))
        self._battle[key] = {'me': me, 'wild': wild, 'mid': act['id'], 'log': [],
                             'me_spr': me_spr, 'wild_spr': wild_spr,
                             'weather': roll_weather(), 'fainted': set(),
                             'arena': random.choice(ARENAS),
                             'sent': False}
        try:
            await self._send_battle(ix_or_ctx, is_ix, gid, user.id)
        except Exception:
            # Card never landed: don't strand a battle with no message.
            self._battle.pop(key, None)
            msg = t(gid, 'eco.pk_api')
            try:
                if is_ix:
                    await ix_or_ctx.followup.send(msg, ephemeral=True)
                else:
                    await ix_or_ctx.reply(msg, ephemeral=True)
            except Exception:
                pass

    def _team_line(self, gid, name: str, hp: int, maxhp: int, fainted: bool = False,
                   mark: str = '') -> str:
        return f"{hp_dot(hp / max(1, maxhp), fainted, gid)} {name} HP {hp}/{maxhp}{mark}"

    def _vs_wild_body(self, gid, uid, st) -> str:
        me, wild, log = st['me'], st['wild'], st['log']
        who = f'<@{uid}>'
        body = t(gid, 'eco.pk_vs', a=who, b=t(gid, 'eco.pk_wild_foe', name=wild['name']))
        body += '\n' + t(gid, 'eco.pk_sentout', ball=em(gid, 'mark_me') or 'R', who=who, name=me['name'])
        body += '\n' + t(gid, 'eco.pk_sentout_wild', ball=em(gid, 'mark_foe') or 'B', name=wild['name'])
        if st.get('weather') in WEATHER_LINE:
            body += '\n' + _weather_line(gid, st['weather'])
        body += '\n' + t(gid, 'eco.pk_team_of', user=who)
        for m in my_mons(gid, uid)[:6]:
            spe = em(gid, _species_emoji_name(m.get('dex', 0)))
            if m['id'] == st.get('mid'):
                body += '\n' + self._team_line(gid, me['name'], me['hp'], me['stats']['maxhp'],
                                               mark=held_mark(gid, me))
            else:
                mark = em(gid, 'switch_dot') or '-'
                body += f"\n{mark} {spe + ' ' if spe else ''}{mon_name(m, gid)} Lv{m['level']}"
        if log:
            body += '\n' + '\n'.join(log[-4:])
        return body

    def _vs_duel_body(self, gid, st) -> str:
        a, b = self._duel_pair(st)
        ua, ub = f"<@{st['u1']}>", (st['u2'] if isinstance(st['u2'], str) and st['u2'].startswith('npc:')
                                    else f"<@{st['u2']}>")
        if st.get('npc'):
            ub = st['npc']
        body = t(gid, 'eco.pk_vs', a=ua, b=ub)
        body += '\n' + t(gid, 'eco.pk_sentout', ball=em(gid, 'mark_me') or 'R', who=ua, name=a['name'])
        body += '\n' + t(gid, 'eco.pk_sentout', ball=em(gid, 'mark_foe') or 'B', who=ub, name=b['name'])
        if st.get('weather') in WEATHER_LINE:
            body += '\n' + _weather_line(gid, st['weather'])
        for tag, team, mids, idx in (('pa', st['t1'], st['m1'], st['i1']),
                                     ('pb', st['t2'], st['m2'], st['i2'])):
            owner = ua if tag == 'pa' else ub
            body += '\n' + t(gid, 'eco.pk_team_of', user=owner)
            for j, (f, mid) in enumerate(zip(team, mids)):
                fainted = f['hp'] <= 0 or mid in st.get('fainted', set())
                spe = em(gid, _species_emoji_name(f.get('dex', 0)))
                dot = em(gid, 'faint_dot') if fainted else (em(gid, 'switch_dot') or '-')
                body += f"\n{dot} {spe + ' ' if spe else ''}" + self._team_line(
                    gid, f['name'], f['hp'], f['stats']['maxhp'], fainted,
                    mark=held_mark(gid, f)).split(' ', 1)[1]
        if st['log']:
            body += '\n' + '\n'.join(st['log'][-4:])
        return body

    def _scene_file(self, st):
        import io as _bio
        try:
            png = battle_image(st.get('me_spr'), st.get('wild_spr'),
                               st['me'], st['wild'], st.get('weather'),
                               st.get('arena', 'meadow'))
            return discord.File(_bio.BytesIO(png), 'battle.png')
        except Exception:
            return None

    def _arm_battle_timeout(self, st, view):
        """Idle battles must not strand state (the busy-guard would soft-lock
        later fights): when a card WITH live buttons times out, pop its battle
        and strip the dead buttons. Result cards (no buttons) are left alone.
        Generation-guarded — only the latest render of a battle may clean up."""
        try:
            if not view.is_dispatchable():
                return
            st['_gen'] = int(st.get('_gen') or 0) + 1
            gen, msg = st['_gen'], st.get('msg')

            async def _timeout_pop():
                if st.get('_gen') != gen:
                    return  # superseded render; a newer card owns this battle
                for k, v in list(self._battle.items()):
                    if v is st:
                        self._battle.pop(k, None)
                try:
                    if msg is not None and st.get('msg') is msg:
                        await msg.edit(view=None)
                except Exception:
                    pass

            view.on_timeout = _timeout_pop
        except Exception:
            pass

    async def _show_battle(self, ix_or_ctx, is_new_ctx: bool, st, view, f):
        """Send the battle message once, edit it on later turns."""
        if st.get('msg') is not None:
            try:
                if f:
                    await st['msg'].edit(view=view, attachments=[f])
                else:
                    await st['msg'].edit(view=view)
                self._arm_battle_timeout(st, view)
                return
            except Exception:
                pass
        is_ix = isinstance(ix_or_ctx, discord.Interaction)
        if is_ix:
            if f:
                st['msg'] = await ix_or_ctx.followup.send(view=view, file=f)
            else:
                st['msg'] = await ix_or_ctx.followup.send(view=view)
        else:
            if f:
                st['msg'] = await ix_or_ctx.reply(view=view, file=f, mention_author=False)
            else:
                st['msg'] = await ix_or_ctx.reply(view=view, mention_author=False)
        self._arm_battle_timeout(st, view)

    async def _send_battle(self, ix_or_ctx, is_ix, gid, uid):
        st = self._battle.get((str(gid), str(uid)))
        if not st:
            return
        view = self._battle_view(gid, uid, st)
        await self._show_battle(ix_or_ctx, True, st, view, self._scene_file(st))

    def _battle_view(self, gid, uid, st):
        from discord.ui import ActionRow, Container, TextDisplay, MediaGallery
        from discord.ui import LayoutView
        from discord.ui.media_gallery import MediaGalleryItem
        me = st['me']
        wild = st['wild']
        _, _, me_ac = rarity_of(me.get('row'), bool(me.get('shiny')))
        _, _, w_ac = rarity_of(wild.get('row'), bool(wild.get('shiny')))
        accent = 0xFF4655
        if RARITY_SHINY[3] in (me_ac, w_ac):
            accent = RARITY_SHINY[3]
        elif RARITY_LEGENDARY[3] in (me_ac, w_ac):
            accent = RARITY_LEGENDARY[3]
        layout = LayoutView(timeout=180)
        box = Container(accent_color=accent)
        box.add_item(TextDisplay(self._vs_wild_body(gid, uid, st)))
        box.add_item(MediaGallery(MediaGalleryItem(media='attachment://battle.png')))
        b = balls_get(gid, uid)
        box.add_item(TextDisplay(
            f"-# {em(gid, 'btn_ball') or 'o'} Balls: poke x{b['poke']} · great x{b['great']} · ultra x{b['ultra']} · master x{b['master']}"))
        moves = (me.get('moves') or [])[:4]
        row = ActionRow()
        for i, mv in enumerate(moves):
            b = discord.ui.Button(
                label=f"{mv['name'][:16]} {mv['power']}"[:80],
                style=discord.ButtonStyle.primary, custom_id=f'pkmv:{uid}:{i}',
                emoji=_btn_emoji(gid, _move_emoji_name(mv)))
            b.callback = self._mk_battle_btn(gid, uid, ('move', i))
            row.add_item(b)
        box.add_item(row)
        row2 = ActionRow()
        pots = potions_get(gid, uid)
        plabel = f"POTION ({pots['potion'] + pots['superpotion']})"
        for label, cid, emo in ((plabel, 'pk_potion', 'btn_potion'),
                                 ('BALL', 'pk_ball', 'btn_ball'),
                                 ('RUN', 'pk_run', 'btn_run')):
            b = discord.ui.Button(label=label[:80],
                                  style=discord.ButtonStyle.secondary if label != plabel
                                  else discord.ButtonStyle.success,
                                  custom_id=f'{cid}:{uid}',
                                  emoji=_btn_emoji(gid, emo))
            b.callback = self._mk_battle_btn(gid, uid, cid)
            row2.add_item(b)
        box.add_item(row2)
        row3 = ActionRow()
        for m in my_mons(gid, uid)[:5]:
            nm = mon_name(m)[:16]
            cur = (m['id'] == st.get('mid'))
            dead = m['id'] in st.get('fainted', set())
            spe = _species_btn_emoji(gid, m.get('dex', 0))
            b = discord.ui.Button(label=f"Lv{m['level']}" if spe else nm[:80],
                                  style=discord.ButtonStyle.success if cur
                                  else discord.ButtonStyle.secondary,
                                  custom_id=f'pksw:{uid}:{m["id"]}',
                                  disabled=cur,
                                  emoji=spe if spe else _btn_emoji(gid, _switch_emoji_name(cur, dead)))
            b.callback = self._mk_battle_btn(gid, uid, ('switch', m['id']))
            row3.add_item(b)
            if len(row3.children) >= 5:
                break
        if row3.children:
            box.add_item(row3)
        layout.add_item(box)
        return layout

    def _mk_battle_btn(self, gid, uid, what):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._battle_turn(ix, gid, ix.user, what)
        return _cb

    @_turn_lock
    @_turn_safe
    async def _battle_turn(self, ix: discord.Interaction, gid, user, what):
        key = (str(gid), str(user.id))
        st = self._battle.get(key)
        if not st:
            return await ix.followup.send(t(gid, 'eco.pk_nobattle'), ephemeral=True)
        me, wild = st['me'], st['wild']
        log = st['log']
        if what == 'pk_run':
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            fled = self._layout(gid, t(gid, 'eco.pk_fled_title'), t(gid, 'eco.pk_fled'))
            # Resolve on the battle card instead of spawning a second message.
            if st.get('msg') is not None:
                try:
                    await st['msg'].edit(view=fled)
                    return
                except Exception:
                    pass
            return await ix.followup.send(view=fled)
        if what == 'pk_ball':
            e = self._get_enc(gid, user.id)
            if e:
                e['hp'] = wild['hp']
            ok, msg, gif, disp = await self._do_catch(gid, user.id, e or {'dex': 0}, 'ultra'
                                                      if balls_get(gid, user.id).get('ultra', 0) else
                                                      'great' if balls_get(gid, user.id).get('great', 0) else 'poke')
            if ok:
                self._battle.pop(key, None)
                self._enc.pop(key, None)
                try:
                    av = str(user.display_avatar.with_size(64).url)
                except Exception:
                    av = ''
                rk, re, accent = rarity_of(_dex_row((e or {}).get('dex', 0)), False, gid)
                layout = self._congrats_layout(gid, user.display_name, av, msg, gif, accent)
                if st.get('msg') is not None:
                    try:
                        await st['msg'].edit(view=layout)
                        return
                    except Exception:
                        pass
                return await ix.followup.send(view=layout)
            return await ix.followup.send(msg)
        if what == 'pk_potion':
            pots = potions_get(gid, user.id)
            use = ('superpotion' if me['hp'] < me['stats']['maxhp'] * 0.5 and pots['superpotion']
                   else 'potion' if pots['potion'] else 'superpotion' if pots['superpotion'] else None)
            if not use:
                log.append(t(gid, 'eco.pk_no_potion'))
            elif not balls_take(gid, user.id, use):
                log.append(t(gid, 'eco.pk_no_potion'))
            else:
                heal = int(me['stats']['maxhp'] * POTIONS[use][1])
                me['hp'] = min(me['stats']['maxhp'], me['hp'] + heal)
                log.append(t(gid, 'eco.pk_healed', name=me['name'], hp=heal))
            # potion costs the turn: wild strikes
            await self._wild_strike(gid, me, wild, log, st.get('weather'))
            return await self._after_turn(ix, gid, user, key, st)
        if isinstance(what, tuple) and what[0] == 'switch':
            import aiohttp
            nm = None
            with db.conn_ctx() as conn:
                nm = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                                  (what[1], str(gid), str(user.id))).fetchone()
            if not nm:
                return await ix.followup.send(t(gid, 'eco.pk_noslot'), ephemeral=True)
            nm = dict(nm)
            if nm['id'] == st.get('mid'):
                # Same bench slot: refresh the card in place, don't duplicate it.
                return await self._show_battle(ix, False, st,
                                               self._battle_view(gid, user.id, st), None)
            async with aiohttp.ClientSession() as s:
                me2 = await self._fighter(s, nm, gid=gid)
                me2_spr = await fetch_sprite(
                    s, form_sprite(nm, bool(nm['shiny']))
                    or pix_url(nm['dex'], bool(nm['shiny']), back=True))
            mem = st.setdefault('hp_mem', {})
            mem[st.get('mid')] = me['hp']
            if nm['id'] in mem:
                me2['hp'] = max(1, min(me2['stats']['maxhp'], mem[nm['id']]))
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET active=0 WHERE guild_id=? AND owner_id=?',
                             (str(gid), str(user.id)))
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (nm['id'],))
            st['me'], st['mid'], st['me_spr'] = me2, nm['id'], me2_spr
            me = me2
            log.append(t(gid, 'eco.pk_switched', name=mon_name(nm, gid)))
            await self._wild_strike(gid, me, wild, log, st.get('weather'))
            return await self._after_turn(ix, gid, user, key, st)
        # chosen move (or fallback): faster strikes first
        mv = None
        if isinstance(what, tuple) and what[0] == 'move':
            idx = what[1]
            if 0 <= idx < len(me.get('moves') or []):
                mv = me['moves'][idx]
        order = []
        if me['stats']['spe'] >= wild['stats']['spe']:
            order = [('me', mv), ('wild', None)]
        else:
            order = [('wild', None), ('me', mv)]
        for side, chosen in order:
            if side == 'me':
                if wild['hp'] <= 0:
                    break
                use_mv = chosen or random.choice(_safe_moves(me))
                await self._strike(gid, me, wild, use_mv, True, log, st.get('weather'))
            else:
                if me['hp'] <= 0:
                    break
                await self._strike(gid, wild, me, random.choice(_safe_moves(wild)), False, log,
                                   st.get('weather'))
        return await self._after_turn(ix, gid, user, key, st)

    async def _strike(self, gid, att, dfn, mv, is_me: bool, log: list, weather=None):
        dmg, crit = damage(att['level'], mv, att['stats'], dfn['stats'],
                           att['types'], dfn['types'], weather)
        dmg = held_strike(att, dfn, dmg)
        dfn['hp'] = max(0, dfn['hp'] - dmg)
        eff = effectiveness(mv.get('ptype', 'normal'), dfn['types'])
        tag = _hit_tag(gid, eff, crit, dmg)
        who = t(gid, 'eco.pk_you') if is_me else t(gid, 'eco.pk_foe')
        log.append(t(gid, 'eco.pk_hit', who=who, move=_mv_name(gid, mv), dmg=dmg) + tag)

    async def _wild_strike(self, gid, me, wild, log: list, weather=None):
        await self._strike(gid, wild, me, random.choice(_safe_moves(wild)), False, log, weather)

    async def _after_turn(self, ix: discord.Interaction, gid, user, key, st):
        me, wild, log = st['me'], st['wild'], st['log']
        if wild['hp'] <= 0:
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            gain = held_xp(me, wild['level'] * 10)
            msgs = [t(gid, 'eco.pk_ko', name=wild['name'], xp=gain)]
            ev = await self._gain_party_xp(gid, user.id, st['mid'], gain)
            ev.extend(evs_win(gid, st['mid'], wild.get('row') or {}, me))
            msgs.extend(ev)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_daily SET battles=battles+1 WHERE guild_id=? AND user_id=?',
                             (str(gid), str(user.id)))
            self._buddy_heart(gid, user.id)
            view = self._layout(gid, t(gid, 'eco.pk_win_title'), '\n'.join(log[-4:] + msgs),
                                'attachment://battle.png')
            st['done'] = True
            await self._show_battle(ix, False, st, view, self._scene_file(st))
            return
        if me['hp'] <= 0:
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            view = self._layout(gid, t(gid, 'eco.pk_lose_title'),
                                '\n'.join(log[-4:] + [t(gid, 'eco.pk_blackout')]),
                                'attachment://battle.png')
            st['done'] = True
            await self._show_battle(ix, False, st, view, self._scene_file(st))
            return
        _held_heal(gid, me, log)
        view = self._battle_view(gid, user.id, st)
        await self._show_battle(ix, False, st, view, self._scene_file(st))

    async def _gain_party_xp(self, gid, uid, active_mid: int, gain: int) -> list:
        """Full XP to the battler, 25% to the bench, 50% extra to the buddy."""
        msgs = await self._gain_xp(gid, uid, active_mid, gain)
        share = gain // 4
        buddy = buddy_get(gid, uid).get('mid', 0)
        if share > 0:
            for m in my_mons(gid, uid):
                if m['id'] != active_mid:
                    msgs.extend(await self._gain_xp(gid, uid, m['id'], share))
                    if len(msgs) > 5:
                        break
        if buddy:
            msgs.extend(await self._gain_xp(gid, uid, buddy, gain // 2))
        return msgs

    def _buddy_heart(self, gid, uid):
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_buddy SET hearts=hearts+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))

    async def _gain_xp(self, gid, uid, mid: int, gain: int) -> list:
        """Add XP, level up, evolve. Returns announcement lines."""
        import aiohttp
        msgs = []
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM pk_mons WHERE id=?', (mid,)).fetchone()
            if not row:
                return msgs
            m = dict(row)
        lv, xp = m['level'], (m['xp'] or 0) + gain
        ups = 0
        while lv < 100 and xp >= lv ** 3:
            xp -= lv ** 3
            lv += 1
            ups += 1
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, m['dex'])
        if ups:
            msgs.append(t(gid, 'eco.pk_levelup', name=mon_name(m, gid), level=lv))
        if row.get('evo_to') and row.get('evo_level') and lv >= row['evo_level']:
            async with aiohttp.ClientSession() as s:
                nxt = await dex_get(s, row['evo_to'])
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET dex=?, level=?, xp=? WHERE id=?',
                             (row['evo_to'], lv, xp, mid))
            msgs.append(t(gid, 'eco.pk_evolve', old=mon_name(m, gid),
                          new=((em(gid, 'rarity_shiny') or '*') if m.get('shiny') else '') + nxt['name'].capitalize()))
        else:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET level=?, xp=? WHERE id=?', (lv, xp, mid))
        return msgs

    @commands.command(name='battle', description='Walcz z dzikim (slot z ;box)')
    async def battle(self, ctx, slot: int = 0):
        gid = ctx.guild.id
        if slot:
            m = get_mon(gid, ctx.author.id, slot)
            if not m:
                return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
            return await self._start_battle(ctx, gid, ctx.author, lead_mid=m['id'])
        if len(my_mons(gid, ctx.author.id)) > 1 and not self._battle.get((str(gid), str(ctx.author.id))):
            return await ctx.reply(view=self._fighter_picker(gid, ctx.author.id), ephemeral=True)
        await self._start_battle(ctx, gid, ctx.author)

    # ----- PvP duels (interactive, alternating turns) -----

    @commands.command(name='duel', description='Pojedynek trenerów')
    async def duel(self, ctx, member: discord.Member, wager: int = 0):
        from cogs.gamble import bal
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pk_duel_self'), ephemeral=True)
        me_mons = my_mons(gid, ctx.author.id)
        fo_mons = my_mons(gid, member.id)
        if not any(m['active'] for m in me_mons) or not any(m['active'] for m in fo_mons):
            return await ctx.reply(t(gid, 'eco.pk_duel_need'), ephemeral=True)
        wager = max(0, wager or 0)
        if wager:
            if bal(gid, ctx.author.id)['cash'] < wager or bal(gid, member.id)['cash'] < wager:
                return await ctx.reply(t(gid, 'eco.pk_duel_cash'), ephemeral=True)
        from discord.ui import ActionRow, Container, TextDisplay
        from discord.ui import LayoutView
        layout = LayoutView(timeout=90)
        box = Container(accent_color=0xFF4655)
        # Ping must live inside the layout: content + Components V2 = HTTP 400.
        box.add_item(TextDisplay(f'<@{member.id}>'))
        box.add_item(TextDisplay(t(gid, 'eco.pk_duel_challenge', a=ctx.author.display_name,
                                   b=member.display_name,
                                   wager=(cshort(wager) if wager else '—'))))
        lead = next((m for m in me_mons if m.get('active')), me_mons[0] if me_mons else None)
        chal_files = []
        if lead:
            sec, f = _sec_row(gid, mon_name(lead, gid), lead.get('dex'))
            if f:
                chal_files.append(f)
            box.add_item(sec)
        row = ActionRow()
        ok_b = discord.ui.Button(label='ACCEPT', style=discord.ButtonStyle.success,
                                   emoji=_btn_emoji(gid, 'box_done'))
        no_b = discord.ui.Button(label='DECLINE', style=discord.ButtonStyle.danger,
                                 emoji=_btn_emoji(gid, 'check_cross'))

        _claimed = {'done': False}  # double-ACCEPT guard for the challenge below
        async def _ok(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            if _claimed['done']:
                return await ix.response.send_message(t(gid, 'eco.pk_gone'), ephemeral=True)
            _claimed['done'] = True
            # Strip the challenge buttons first: a double ACCEPT can't start twice.
            try:
                await ix.message.edit(view=None)
            except Exception:
                pass
            await ix.response.defer()
            await self._duel_start(ix, gid, ctx.author, member, wager)

        async def _no(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            _claimed['done'] = True
            try:
                await ix.message.edit(view=None)
            except Exception:
                pass
            await ix.response.send_message(t(gid, 'eco.pk_declined'))

        ok_b.callback = _ok
        no_b.callback = _no
        row.add_item(ok_b)
        row.add_item(no_b)
        box.add_item(row)
        layout.add_item(box)
        await ctx.reply(view=layout, files=chal_files or None, mention_author=False)

    async def _duel_start(self, ix: discord.Interaction, gid, u1, u2, wager: int):
        import aiohttp
        t1 = team_get(gid, u1.id)[:3]
        t2 = team_get(gid, u2.id)[:3]
        if not t1 or not t2:
            return await ix.followup.send(t(gid, 'eco.pk_duel_need'), ephemeral=True)
        for _uid in (u1.id, u2.id):
            if any(str(_uid) in k[1:] for k in self._battle):
                return await ix.followup.send(t(gid, 'eco.pk_busy'), ephemeral=True)
        # Wager escrow: both sides pay into the pot up front, so cash spent
        # mid-duel can't inflate/deflate the payout (old code split live
        # balances at settle = money creation).
        pot = 0
        if wager:
            from cogs.gamble import bal, take_cash, set_cash
            if bal(gid, u1.id)['cash'] < wager or bal(gid, u2.id)['cash'] < wager:
                return await ix.followup.send(t(gid, 'eco.pk_duel_cash'), ephemeral=True)
            ok1 = take_cash(gid, u1.id, wager)
            ok2 = take_cash(gid, u2.id, wager)
            if not (ok1 and ok2):
                if ok1:
                    set_cash(gid, u1.id, bal(gid, u1.id)['cash'] + wager)
                if ok2:
                    set_cash(gid, u2.id, bal(gid, u2.id)['cash'] + wager)
                return await ix.followup.send(t(gid, 'eco.pk_duel_cash'), ephemeral=True)
            pot = wager * 2
        try:
            async with aiohttp.ClientSession() as s:
                f1, f2, s1, s2, m1, m2 = [], [], [], [], [], []
                for m in t1:
                    # gid=0: team names land in switch-button labels (token-free until button patch)
                    f = await self._fighter(s, m, gid=0)
                    f1.append(f)
                    m1.append(m['id'])
                    s1.append(await fetch_sprite(
                        s, form_sprite(f, bool(f['shiny']))
                        or pix_url(f['dex'], bool(f['shiny']), back=True)))
                for m in t2:
                    # gid=0: team names land in switch-button labels (token-free until button patch)
                    f = await self._fighter(s, m, gid=0)
                    f2.append(f)
                    m2.append(m['id'])
                    s2.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']))))
        except Exception:
            if pot:
                from cogs.gamble import bal as _bal, set_cash as _set
                _set(gid, u1.id, _bal(gid, u1.id)['cash'] + wager)
                _set(gid, u2.id, _bal(gid, u2.id)['cash'] + wager)
            return await ix.followup.send(t(gid, 'eco.pk_api'), ephemeral=True)
        key = (str(gid), str(u1.id), str(u2.id))
        self._battle[key] = {'duel': True, 't1': f1, 't2': f2, 'm1': m1, 'm2': m2,
                             'i1': 0, 'i2': 0, 's1': s1, 's2': s2,
                             'u1': u1.id, 'u2': u2.id,
                             'wager': wager, 'pot': pot, 'turn': u1.id, 'log': [],
                             'weather': roll_weather(), 'fainted': set(),
                             'arena': random.choice(ARENAS),
                             'sent': False}
        await self._duel_render(ix, gid, key)

    def _duel_pair(self, st):
        a = st['t1'][st['i1']]
        b = st['t2'][st['i2']]
        return a, b

    def _duel_view(self, gid, key, st):
        from discord.ui import ActionRow, Container, TextDisplay, MediaGallery
        from discord.ui import LayoutView
        from discord.ui.media_gallery import MediaGalleryItem
        a, b = self._duel_pair(st)
        turn_side = a if st['turn'] == st['u1'] else b
        layout = LayoutView(timeout=120)
        box = Container(accent_color=0xFF4655)
        box.add_item(TextDisplay(self._vs_duel_body(gid, st)))
        box.add_item(MediaGallery(MediaGalleryItem(media='attachment://battle.png')))
        bb = balls_get(gid, st['turn'])
        box.add_item(TextDisplay(
            f"-# {em(gid, 'btn_ball') or 'o'} Balls: poke x{bb['poke']} · great x{bb['great']} · ultra x{bb['ultra']} · master x{bb['master']}"))
        row = ActionRow()
        for i, mv in enumerate((turn_side.get('moves') or [])[:4]):
            b = discord.ui.Button(label=f"{mv['name'][:14]} {mv['power']}"[:80],
                                  style=discord.ButtonStyle.primary,
                                  custom_id=f'pkd:{key[1]}:{i}',
                                  emoji=_btn_emoji(gid, _move_emoji_name(mv)))
            b.callback = self._mk_duel_btn(gid, key, ('move', i))
            row.add_item(b)
        box.add_item(row)
        row2 = ActionRow()
        for label, cid, emo in (('POTION', 'potion', 'btn_potion'),
                                  ('FORFEIT', 'forfeit', '')):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'pkd:{key[1]}:{cid}',
                                  emoji=_btn_emoji(gid, emo))
            b.callback = self._mk_duel_btn(gid, key, cid)
            row2.add_item(b)
        box.add_item(row2)
        # switch row: own team, current/fainted disabled
        mine = 't1' if st['turn'] == st['u1'] else 't2'
        row3 = ActionRow()
        for j, (f, mid) in enumerate(zip(st[mine], st['m1' if mine == 't1' else 'm2'])):
            cur = (st['i1' if mine == 't1' else 'i2'] == j)
            dead = f['hp'] <= 0 or mid in st.get('fainted', set())
            spe = _species_btn_emoji(gid, f.get('dex', 0))
            b = discord.ui.Button(label=f"Lv{f.get('level', '?')}" if spe else f['name'][:14],
                                  style=discord.ButtonStyle.success if cur
                                  else discord.ButtonStyle.secondary,
                                  custom_id=f'pkdsw:{key[1]}:{mid}',
                                  disabled=(cur or dead),
                                  emoji=spe if spe else _btn_emoji(gid, _switch_emoji_name(cur, dead)))
            b.callback = self._mk_duel_btn(gid, key, ('switch', mid))
            row3.add_item(b)
            if len(row3.children) >= 5:
                break
        if row3.children:
            box.add_item(row3)
        layout.add_item(box)
        return layout

    def _mk_duel_btn(self, gid, key, what):
        async def _cb(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            st = self._battle.get(key)
            if not st or not st.get('duel'):
                return await ix.response.send_message(t(gid, 'eco.pk_nobattle'), ephemeral=True)
            if ix.user.id != st['turn']:
                return await ix.response.send_message(t(gid, 'eco.pk_duel_wait'), ephemeral=True)
            await ix.response.defer()
            await self._duel_turn(ix, gid, key, what)
        return _cb

    @_turn_lock
    @_turn_safe
    async def _duel_turn(self, ix: discord.Interaction, gid, key, what):
        from cogs.gamble import bal, set_cash
        st = self._battle.get(key)
        if not st:
            return await ix.followup.send(t(gid, 'eco.pk_nobattle'), ephemeral=True)
        side = 't1' if st['turn'] == st['u1'] else 't2'
        foe = 't2' if side == 't1' else 't1'
        if isinstance(what, tuple) and what[0] == 'switch':
            mids = st['m1' if side == 't1' else 'm2']
            if what[1] in mids:
                j = mids.index(what[1])
                cur = st['i1' if side == 't1' else 'i2']
                tgt = st[side][j]
                if j != cur and tgt['hp'] > 0 and what[1] not in st.get('fainted', set()):
                    if side == 't1':
                        st['i1'] = j
                    else:
                        st['i2'] = j
                    log.append(t(gid, 'eco.pk_switched', name=tgt['name']))
                    fo_now = st[foe][st['i2' if side == 't1' else 'i1']]
                    mv = random.choice(_safe_moves(fo_now))
                    dmg, crit = damage(fo_now['level'], mv, fo_now['stats'], tgt['stats'],
                                       fo_now['types'], tgt['types'], st.get('weather'))
                    dmg = held_strike(fo_now, tgt, dmg)
                    tgt['hp'] = max(0, tgt['hp'] - dmg)
                    eff = effectiveness(mv.get('ptype', 'normal'), tgt['types'])
                    tag = _hit_tag(gid, eff, crit, dmg)
                    log.append(t(gid, 'eco.pk_hit', who=fo_now['name'], move=_mv_name(gid, mv), dmg=dmg) + tag)
                    if tgt['hp'] <= 0:
                        st.setdefault('fainted', set()).add(what[1])
                        log.append(t(gid, 'eco.pk_duel_faint', name=tgt['name']))
                        st['turn'] = st['u2'] if side == 't1' else st['u1']
                        st['log'] = log
                        await self._duel_render(ix, gid, key)
                        return
            st['turn'] = st['u2'] if side == 't1' else st['u1']
            st['log'] = log
            await self._duel_render(ix, gid, key)
            return
        me, fo = st[side][st['i1' if side == 't1' else 'i2']], st[foe][st['i2' if side == 't1' else 'i1']]
        log = st['log']
        if what == 'forfeit':
            self._battle.pop(key, None)
            return await self._duel_settle(ix, gid, key, st, foe, forfeit=True)
        if what == 'potion':
            uid = st['turn']
            pots = potions_get(gid, uid)
            use = ('superpotion' if me['hp'] < me['stats']['maxhp'] * 0.5 and pots['superpotion']
                   else 'potion' if pots['potion'] else 'superpotion' if pots['superpotion'] else None)
            if not use:
                return await ix.followup.send(t(gid, 'eco.pk_no_potion'), ephemeral=True)
            if not balls_take(gid, uid, use):
                return await ix.followup.send(t(gid, 'eco.pk_no_potion'), ephemeral=True)
            heal = int(me['stats']['maxhp'] * POTIONS[use][1])
            me['hp'] = min(me['stats']['maxhp'], me['hp'] + heal)
            log.append(t(gid, 'eco.pk_healed', name=me['name'], hp=heal))
        else:
            mv = None
            if isinstance(what, tuple) and what[0] == 'move':
                idx = what[1]
                if 0 <= idx < len(me.get('moves') or []):
                    mv = me['moves'][idx]
            mv = mv or random.choice(_safe_moves(me))
            dmg, crit = damage(me['level'], mv, me['stats'], fo['stats'], me['types'], fo['types'],
                               st.get('weather'))
            dmg = held_strike(me, fo, dmg)
            fo['hp'] = max(0, fo['hp'] - dmg)
            eff = effectiveness(mv.get('ptype', 'normal'), fo['types'])
            tag = _hit_tag(gid, eff, crit, dmg)
            log.append(t(gid, 'eco.pk_hit', who=me['name'], move=_mv_name(gid, mv), dmg=dmg) + tag)
        if fo['hp'] <= 0:
            log.append(t(gid, 'eco.pk_duel_faint', name=fo['name']))
            if side == 't1':
                st.setdefault('fainted', set()).add((st['m2'] or [None])[st['i2']])
                st['i2'] += 1
                if st['i2'] >= len(st['t2']):
                    self._battle.pop(key, None)
                    return await self._duel_settle(ix, gid, key, st, 't1')
                log.append(t(gid, 'eco.pk_duel_send', name=st['t2'][st['i2']]['name']))
            else:
                st.setdefault('fainted', set()).add((st['m1'] or [None])[st['i1']])
                st['i1'] += 1
                if st['i1'] >= len(st['t1']):
                    self._battle.pop(key, None)
                    return await self._duel_settle(ix, gid, key, st, 't2')
                log.append(t(gid, 'eco.pk_duel_send', name=st['t1'][st['i1']]['name']))
        nxt = st['u2'] if side == 't1' else st['u1']
        if isinstance(nxt, str) and nxt.startswith('npc:'):
            # NPC acts instantly: random strike, no potions, no mercy
            a, b = (st['t2'][st['i2']], st['t1'][st['i1']]) if side == 't1' \
                else (st['t1'][st['i1']], st['t2'][st['i2']])
            mv = random.choice(_safe_moves(a))
            dmg, crit = damage(a['level'], mv, a['stats'], b['stats'], a['types'], b['types'],
                               st.get('weather'))
            dmg = held_strike(a, b, dmg)
            b['hp'] = max(0, b['hp'] - dmg)
            eff = effectiveness(mv.get('ptype', 'normal'), b['types'])
            tag = _hit_tag(gid, eff, crit, dmg)
            log.append(t(gid, 'eco.pk_hit', who=a['name'], move=_mv_name(gid, mv), dmg=dmg) + tag)
            if b['hp'] <= 0:
                log.append(t(gid, 'eco.pk_duel_faint', name=b['name']))
                if side == 't1':
                    st['i1'] += 1
                    if st['i1'] >= len(st['t1']):
                        self._battle.pop(key, None)
                        return await self._duel_settle(ix, gid, key, st, 't2')
                    log.append(t(gid, 'eco.pk_duel_send', name=st['t1'][st['i1']]['name']))
                else:
                    st['i2'] += 1
                    if st['i2'] >= len(st['t2']):
                        self._battle.pop(key, None)
                        return await self._duel_settle(ix, gid, key, st, 't1')
                    log.append(t(gid, 'eco.pk_duel_send', name=st['t2'][st['i2']]['name']))
            st['turn'] = st['u1'] if side == 't1' else st['u2']
        else:
            st['turn'] = nxt
        _held_heal(gid, me, log)
        st['log'] = log
        await self._duel_render(ix, gid, key)

    async def _duel_render(self, ix: discord.Interaction, gid, key):
        st = self._battle.get(key)
        if not st:
            return
        import io as _bio
        view = self._duel_view(gid, key, st)
        a, b = self._duel_pair(st)
        f = None
        try:
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b, st.get('weather'),
                                st.get('arena', 'meadow'))
            f = discord.File(_bio.BytesIO(png), 'battle.png')
        except Exception:
            pass
        await self._show_battle(ix, False, st, view, f)

    async def _duel_settle(self, ix: discord.Interaction, gid, key, st, winner_side, forfeit=False):
        from cogs.gamble import bal, set_cash
        won1 = winner_side == 't1'
        is_npc = bool(st.get('npc'))
        uwin = st['u1'] if won1 else st['u2']
        wteam, wmid, lteam = ((st['t1'], st['m1'], st['t2']) if won1
                              else (st['t2'], st['m2'], st['t1']))
        widx = min(st['i1'] if won1 else st['i2'], len(wteam) - 1)
        lidx = min(st['i2'] if won1 else st['i1'], len(lteam) - 1)
        fin = wteam[max(0, widx)]
        fin_mid = (wmid + [0])[max(0, widx)]
        foe = lteam[max(0, lidx)]
        if is_npc and not won1:
            # NPC takes no prisoners and no prizes. Resolve on the duel card.
            await self._show_battle(ix, False, st, self._layout(
                gid, t(gid, 'eco.pk_duel_title'),
                '\n'.join(st['log'][-6:] + [t(gid, 'eco.pk_npc_lose', name=st['npc'])]),
                'attachment://battle.png'), self._duel_final_file(st))
            return
        gain = held_xp(fin, foe['level'] * 12)
        ev = await self._gain_party_xp(gid, uwin, fin_mid, gain)
        ev.extend(evs_win(gid, fin_mid, foe.get('row') or {}, fin))
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_daily SET duels=duels+1, battles=battles+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uwin)))
        self._buddy_heart(gid, uwin)
        line = t(gid, 'eco.pk_duel_win', name=f'<@{uwin}>', xp=gain)
        if forfeit:
            line += '\n' + t(gid, 'eco.pk_duel_forfeit')
        if is_npc and won1:
            import time as _t
            prize = st.get('prize', 0)
            today = int(_t.time()) // 86400
            with db.conn_ctx() as conn:
                already = conn.execute('SELECT day FROM pk_npc WHERE guild_id=? AND user_id=? AND npc=?',
                                       (str(gid), str(uwin), st.get('npckey', ''))).fetchone()
                # Double-settle guard: prize + badge only if unclaimed today.
                if not already or (already['day'] or 0) != today:
                    b = bal(gid, uwin)
                    set_cash(gid, uwin, b['cash'] + prize)
                    line += '\n' + t(gid, 'eco.pk_npc_win', prize=cshort(prize))
                    conn.execute('INSERT OR REPLACE INTO pk_npc (guild_id, user_id, npc, day) VALUES (?,?,?,?)',
                                 (str(gid), str(uwin), st.get('npckey', ''), today))
                    if st.get('npckey') == 'cyntia':
                        with db.conn_ctx() as c2:
                            c2.execute('INSERT OR IGNORE INTO achievements (guild_id, user_id, akey, unlocked_at) '
                                       'VALUES (?,?,?,?)',
                                       (str(gid), str(uwin), 'npc_champ', int(_t.time())))
                        line += '\n' + t(gid, 'eco.pk_npc_badge')
        wager = st.get('wager', 0)
        pot = st.get('pot', 0)
        if pot:
            # Escrowed at duel start: winner takes the pot, loser pays nothing
            # more (old live-balance split created money when spent mid-duel).
            winner = st['u1'] if won1 else st['u2']
            w = bal(gid, winner)
            set_cash(gid, winner, w['cash'] + pot)
            line += '\n' + t(gid, 'eco.pk_duel_wager', win=cshort(wager))
        elif wager:
            w1 = bal(gid, st['u1'])
            w2 = bal(gid, st['u2'])
            if won1:
                set_cash(gid, st['u1'], w1['cash'] + wager)
                set_cash(gid, st['u2'], max(0, w2['cash'] - wager))
            else:
                set_cash(gid, st['u2'], w2['cash'] + wager)
                set_cash(gid, st['u1'], max(0, w1['cash'] - wager))
            line += '\n' + t(gid, 'eco.pk_duel_wager', win=cshort(wager))
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO pk_stats (guild_id, user_id, duels_won, duels_lost) '
                         'VALUES (?,?,0,0)', (str(gid), str(uwin)))
            conn.execute('UPDATE pk_stats SET duels_won=duels_won+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uwin)))
            loser = st['u2'] if won1 else st['u1']
            conn.execute('INSERT OR IGNORE INTO pk_stats (guild_id, user_id, duels_won, duels_lost) '
                         'VALUES (?,?,0,0)', (str(gid), str(loser)))
            conn.execute('UPDATE pk_stats SET duels_lost=duels_lost+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(loser)))
        await self._show_battle(ix, False, st, self._layout(
            gid, t(gid, 'eco.pk_duel_title'),
            '\n'.join(st['log'][-6:] + ev + [line]),
            'attachment://battle.png'), self._duel_final_file(st))

    def _duel_final_file(self, st):
        import io as _bio
        try:
            a, b = self._duel_pair(st)
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b, st.get('weather'),
                                st.get('arena', 'meadow'))
            return discord.File(_bio.BytesIO(png), 'battle.png')
        except Exception:
            return None

    @commands.command(name='move', description='Info o ruchu')
    async def move(self, ctx, *, name: str = ''):
        import aiohttp
        gid = ctx.guild.id
        if not (name or '').strip():
            return await ctx.reply(t(gid, 'eco.pk_move_use'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            mv = await move_get(s, (name or '').lower().strip().replace(' ', '-'))
        if not mv:
            return await ctx.reply(t(gid, 'eco.pk_move_no', name=(name or '?')[:24]), ephemeral=True)
        te = em(gid, TYPE_EMOJI.get(mv.get('ptype', ''), ''))
        await ctx.reply(view=self._layout(
            gid, (te + ' ' if te else '') + t(gid, 'eco.pk_move_title', name=mv['name']),
            t(gid, 'eco.pk_move', power=mv['power'], ptype=mv['ptype'], acc=mv['acc'])),
            ephemeral=True)

    async def _moves_view(self, gid, mon: dict, session):
        """A mon's actual battle moveset as a layout card."""
        ms = await moveset_for(session, mon['dex'], mon['level'], seed=mon.get('id'))
        mrows = []
        for x in ms:
            te = em(gid, TYPE_EMOJI.get(x.get('ptype', ''), ''))
            mrows.append(f"{te + ' ' if te else ''}" + t(gid, 'eco.pk_move_row', name=x['name'],
                         power=x['power'], ptype=x['ptype'], acc=x['acc']))
        return self._layout(gid, t(gid, 'eco.pk_moves_title', name=mon_name(mon, gid)),
                            '\n'.join(mrows))

    @commands.command(name='moves', description='Ruchy pokemona')
    async def moves(self, ctx, slot: int):
        import aiohttp
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            view = await self._moves_view(gid, dict(m), s)
        await ctx.reply(view=view, ephemeral=True)

    @commands.command(name='npc', description='Walcz z NPC')
    async def npc(self, ctx, who: str = ''):
        gid = ctx.guild.id
        team = team_get(gid, ctx.author.id)
        if not team:
            return await ctx.reply(t(gid, 'eco.pk_duel_need'), ephemeral=True)
        if not (who or '').strip():
            import time as _t
            today = int(_t.time()) // 86400
            lines = []
            npc_icon = {'joey': 'npc_cap', 'finn': 'npc_net', 'cyntia': 'trophy'}
            with db.conn_ctx() as conn:
                for key, name, lv, size, prize in NPCS:
                    row = conn.execute('SELECT day FROM pk_npc WHERE guild_id=? AND user_id=? AND npc=?',
                                       (str(gid), str(ctx.author.id), key)).fetchone()
                    done = row and (row['day'] or 0) == today
                    ei = em(gid, npc_icon.get(key, ''))
                    lines.append(f"{ei + ' ' if ei else ''}" + t(
                        gid, 'eco.pk_npc_row', n=key, name=name, lv=lv, size=size,
                        prize=cshort(prize),
                        state=t(gid, 'eco.pk_npc_done') if done else t(gid, 'eco.pk_npc_open')))
            return await ctx.reply(view=self._layout(
                gid, t(gid, 'eco.pk_npc_title'),
                '\n'.join(lines) + '\n' + t(gid, 'eco.pk_npc_use')), ephemeral=True)
        key = (who or '').lower().strip()
        npc = next((x for x in NPCS if x[0] == key or x[0].startswith(key)), None)
        if not npc:
            return await ctx.reply(t(gid, 'eco.pk_npc_unknown'), ephemeral=True)
        import time as _t2
        today = int(_t2.time()) // 86400
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT day FROM pk_npc WHERE guild_id=? AND user_id=? AND npc=?',
                               (str(gid), str(ctx.author.id), npc[0])).fetchone()
            if row and (row['day'] or 0) == today:
                return await ctx.reply(t(gid, 'eco.pk_npc_cool'), ephemeral=True)
        bkey = (str(gid), str(ctx.author.id), f'npc:{npc[0]}')
        if bkey in self._battle and not self._battle[bkey].get('starting'):
            return await ctx.reply(t(gid, 'eco.pk_busy'), ephemeral=True)
        # Claim before the slow team/sprite fetches so a spammed `;npc`
        # can't start (and later double-pay) the same ladder twice.
        self._battle[bkey] = {'starting': True}
        try:
            await self._npc_start(ctx, gid, ctx.author, npc)
        except Exception:
            if self._battle.get(bkey, {}).get('starting'):
                self._battle.pop(bkey, None)
            return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)

    async def _npc_start(self, ctx, gid, user, npc):
        import aiohttp
        key, name, lv, size, prize = npc
        team = team_get(gid, user.id)[:3]
        async with aiohttp.ClientSession() as s:
            f1, s1, m1 = [], [], []
            for m in team:
                # gid=0: team names land in switch-button labels (token-free until button patch)
                f = await self._fighter(s, m, gid=0)
                f1.append(f)
                m1.append(m['id'])
                s1.append(await fetch_sprite(
                    s, form_sprite(f, bool(f['shiny']))
                    or pix_url(f['dex'], bool(f['shiny']), back=True)))
            f2, s2, awaited = [], [], None
            for _ in range(size):
                for _try in range(12):
                    dex = random.randint(1, 809)
                    row = await dex_get(s, dex)
                    if not row:
                        continue
                    if row.get('legendary') and key != 'cyntia':
                        continue
                    break
                else:
                    continue
                f = await self._fighter(
                    s, {'dex': dex, 'level': max(1, lv + random.randint(-3, 3)),
                        'shiny': 0, 'nick': ''}, gid=gid)
                f2.append(f)
                s2.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']))))
        if not f2:
            bkey = (str(gid), str(user.id), f'npc:{key}')
            if self._battle.get(bkey, {}).get('starting'):
                self._battle.pop(bkey, None)
            return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        bkey = (str(gid), str(user.id), f'npc:{key}')
        self._battle[bkey] = {'duel': True, 'npc': name, 'prize': prize, 'npckey': key,
                              't1': f1, 't2': f2, 'm1': m1, 'm2': [],
                              'i1': 0, 'i2': 0, 's1': s1, 's2': s2,
                              'u1': user.id, 'u2': f'npc:{key}',
                              'wager': 0, 'turn': user.id, 'log': [
                                  t(gid, 'eco.pk_npc_start', name=name)],
                              'weather': roll_weather(), 'fainted': set(),
                             'arena': random.choice(ARENAS),
                              'sent': False}
        await self._duel_render_ctx(ctx, gid, bkey)

    async def _duel_render_ctx(self, ctx, gid, key):
        import io as _bio
        st = self._battle.get(key)
        if not st:
            return
        view = self._duel_view(gid, key, st)
        f = None
        try:
            a, b = self._duel_pair(st)
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b, st.get('weather'),
                                st.get('arena', 'meadow'))
            f = discord.File(_bio.BytesIO(png), 'battle.png')
        except Exception:
            pass
        if f:
            st['msg'] = await ctx.reply(view=view, file=f, mention_author=False)
        else:
            st['msg'] = await ctx.reply(view=view, mention_author=False)

    @commands.command(name='trade', description='Wymień pokemona')
    async def trade(self, ctx, member: discord.Member, yours: str = '', theirs: str = ''):
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pk_duel_self'), ephemeral=True)
        def _resolve(uid, ident):
            if not ident:
                return None
            s = str(ident).strip()
            if s.isdigit():
                m = get_mon(gid, uid, int(s))
                if m:
                    return m
            mons = my_mons(gid, uid)
            spec_of = lambda d: ((_dex_row(d).get('name')) or '').lower()
            m, _ = _match_mon(mons, spec_of, s.lower())
            if m:
                return m
            return next((x for x in mons if (x.get('nick') or '').lower() == s.lower()), None)
        mine = _resolve(ctx.author.id, yours)
        want = _resolve(member.id, theirs)
        if not mine or not want:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if mine.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(mine, gid)), ephemeral=True)
        from discord.ui import ActionRow, Container, TextDisplay
        from discord.ui import LayoutView
        layout = LayoutView(timeout=90)
        box = Container(accent_color=0x57F287)
        # Ping must live inside the layout: content + Components V2 = HTTP 400.
        box.add_item(TextDisplay(f'<@{member.id}>'))
        box.add_item(TextDisplay(t(gid, 'eco.pk_trade_offer', a=ctx.author.display_name,
                                   m1=mon_name(mine, gid), b=member.display_name, m2=mon_name(want, gid))))
        row = ActionRow()
        ok_b = discord.ui.Button(label='ACCEPT', style=discord.ButtonStyle.success,
                                   emoji=_btn_emoji(gid, 'box_done'))
        no_b = discord.ui.Button(label='DECLINE', style=discord.ButtonStyle.danger,
                                 emoji=_btn_emoji(gid, 'check_cross'))

        m1_id, m2_id = mine['id'], want['id']
        _tclaimed = {'done': False}  # double-ACCEPT guard for the offer below
        async def _ok(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            if _tclaimed['done']:
                return await ix.response.send_message(t(gid, 'eco.pk_gone'), ephemeral=True)
            _tclaimed['done'] = True
            # Strip the offer first: a double ACCEPT can't swap twice.
            try:
                await ix.message.edit(view=None)
            except Exception:
                pass
            await ix.response.defer()
            with db.conn_ctx() as conn:
                r1 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                                  (m1_id, str(gid), str(ctx.author.id))).fetchone()
                r2 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                                  (m2_id, str(gid), str(member.id))).fetchone()
                if not r1 or not r2:
                    return await ix.followup.send(t(gid, 'eco.pk_gone'), ephemeral=True)
                m1, m2 = dict(r1), dict(r2)
            if m1.get('locked'):
                return await ix.followup.send(t(gid, 'eco.pk_locked', name=mon_name(m1, gid)),
                                              ephemeral=True)
            with db.conn_ctx() as conn:
                c1 = conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=? AND guild_id=? AND owner_id=?',
                                  (str(member.id), m1_id, str(gid), str(ctx.author.id)))
                c2 = conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=? AND guild_id=? AND owner_id=?',
                                  (str(ctx.author.id), m2_id, str(gid), str(member.id)))
                if (c1.rowcount or 0) != 1 or (c2.rowcount or 0) != 1:
                    return await ix.followup.send(t(gid, 'eco.pk_gone'), ephemeral=True)
                for uid in (str(ctx.author.id), str(member.id)):
                    r = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? '
                                     'ORDER BY id LIMIT 1', (str(gid), uid)).fetchone()
                    if r:
                        conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (r['id'],))
            view, files = self._mini(gid, t(gid, 'eco.pk_traded_title'),
                                     t(gid, 'eco.pk_traded', m1=mon_name(m1, gid), m2=mon_name(m2, gid)),
                                     None, 0x57F287)
            await ix.followup.send(view=view, files=files or None)

        async def _no(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            _tclaimed['done'] = True
            try:
                await ix.message.edit(view=None)
            except Exception:
                pass
            await ix.response.send_message(t(gid, 'eco.pk_declined'))

        ok_b.callback = _ok
        no_b.callback = _no
        row.add_item(ok_b)
        row.add_item(no_b)
        box.add_item(row)
        layout.add_item(box)
        await ctx.reply(view=layout, mention_author=False)


async def setup(bot):
    await bot.add_cog(Pokemon(bot))

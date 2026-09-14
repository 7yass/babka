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

POKEAPI = 'https://pokeapi.co/api/v2'
HUNT_CD = 60
ENC_TTL = 300
SHINY_ODDS = 256
XP_NEXT = staticmethod(lambda lv: lv ** 3)

BALLS = {
    'poke': (500, 1.0),
    'great': (1500, 1.5),
    'ultra': (4000, 2.0),
    'master': (50000, None),
}
POTIONS = {
    'potion': (1000, 0.5),
    'superpotion': (3000, 1.0),
}
CANDY_PRICE = 8000
EGG_PRICE = 10000
EGG_CYCLES = 20
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
                d = _r.randint(1, 493)
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


def rarity_of(dex: int) -> str:
    row = _dex_row(dex)
    if row.get('legendary'):
        return 'Legendary'
    rate = row.get('rate') or 45
    if rate <= 20:
        return 'Rare'
    if rate <= 100:
        return 'Uncommon'
    return 'Common'


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

# PokeTwo-style regions + quest tracks (catch milestones per region)
REGIONS = {'kanto': (1, 151), 'johto': (152, 251),
           'hoenn': (252, 386), 'sinnoh': (387, 493)}
QUEST_TIERS = [10, 30, 75, 150, 300]
QUEST_REWARDS = [2000, 6000, 15000, 40000, 100000]
REGION_BADGE = {'kanto': 'region_kanto', 'johto': 'region_johto',
                'hoenn': 'region_hoenn', 'sinnoh': 'region_sinnoh'}
# dex milestones: count -> cash
DEX_MILESTONES = {1: 500, 10: 2000, 50: 10000, 100: 50000}

INCENSE_PRICE = 25000
INCENSE_SECONDS = 1800


def region_of(dex: int):
    for name, (lo, hi) in REGIONS.items():
        if lo <= dex <= hi:
            return name
    return None


def silhouette_image(sprite_bytes: bytes) -> bytes:
    """Black mystery silhouette from artwork bytes."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    try:
        sp = _Img.open(_io.BytesIO(sprite_bytes)).convert('RGBA')
        sp = sp.resize((360, 360))
        alpha = sp.split()[3].point(lambda a: 255 if a > 20 else 0)
        black = _Img.new('RGBA', sp.size, (0, 0, 0, 255))
        black.putalpha(alpha)
        img = _Img.new('RGB', (420, 420), (12, 12, 15))
        img.paste(black, (30, 30), black)
        d = _Dr.Draw(img)
        d.text((210, 392), '? ? ?', fill=(150, 150, 158), anchor='mm')
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
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM pk_dex WHERE dex=?', (dex,)).fetchone()
        return dict(row) if row else {}


async def dex_get(session, dex: int) -> dict:
    """Full species data, cached. Fills evolution info on first fetch."""
    row = _dex_row(dex)
    if row and row.get('name'):
        import json as _j
        row['types'] = _j.loads(row.get('types') or '[]')
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


async def moveset_for(session, dex: int) -> list:
    """Up to 4 damaging moves: STAB first, then strongest coverage (cached)."""
    p = await _api(session, f'{POKEAPI}/pokemon/{dex}')
    cands = []
    try:
        for entry in (p or {}).get('moves', [])[:14]:
            mv = await move_get(session, (entry.get('move') or {}).get('name', ''))
            if mv and mv.get('power') and all(mv['name'] != c['name'] for c in cands):
                cands.append(mv)
    except Exception:
        pass
    if not cands:
        return [{'name': 'tackle', 'power': 35, 'ptype': 'normal', 'acc': 100}]
    types = [t['type']['name'] for t in (p or {}).get('types', [])]
    stab = sorted([m for m in cands if m['ptype'] in types],
                  key=lambda m: -m['power'])[:2]
    rest = sorted([m for m in cands if m['ptype'] not in types],
                  key=lambda m: -m['power'])
    seen, cover = set(), []
    for m in rest:
        if m['ptype'] not in seen:
            seen.add(m['ptype'])
            cover.append(m)
        if len(cover) >= 2:
            break
    moves, seen_names = [], set()
    for m in stab + cover + sorted(cands, key=lambda m: -m['power']):
        if m['name'] not in seen_names:
            seen_names.add(m['name'])
            moves.append(m)
        if len(moves) >= 4:
            break
    return moves


def calc_stats(base: dict, level: int) -> dict:
    def _s(b):
        return (2 * b + 31) * level // 100 + 5
    return {'maxhp': (2 * base.get('hp', 50) + 31) * level // 100 + level + 10,
            'atk': _s(base.get('atk', 50)), 'dfn': _s(base.get('dfn', 50)),
            'spa': _s(base.get('spa', 50)), 'spd': _s(base.get('spd', 50)),
            'spe': _s(base.get('spe', 50))}


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


def hp_dot(frac: float, fainted: bool = False) -> str:
    if fainted:
        return '⚪'
    if frac > 0.5:
        return '🟢'
    if frac > 0.2:
        return '🟡'
    return '🔴'


def catch_chance(rate: int, level: int, hp_frac: float, ball_mult) -> float:
    if ball_mult is None:
        return 1.0
    p = (rate / 255) * ball_mult * (1.2 - 0.9 * max(0.0, min(1.0, hp_frac)))
    p *= max(0.25, 1 - level / 150)
    return max(0.01, min(0.95, p))


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


def mon_name(mon: dict) -> str:
    if not mon:
        return '?'
    row = _dex_row(mon['dex'])
    base = (row.get('name') or f"#{mon['dex']}").capitalize()
    name = (mon.get('nick') or base)
    if mon.get('shiny'):
        name = '✨' + name
    return name


def balls_get(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT ball, qty FROM pk_balls WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
    out = {b: 0 for b in BALLS}
    for r in rows:
        out[r['ball']] = r['qty'] or 0
    return out


def balls_add(gid, uid, ball: str, n: int):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) VALUES (?,?,?,0)',
                     (str(gid), str(uid), ball))
        conn.execute('UPDATE pk_balls SET qty=qty+? WHERE guild_id=? AND user_id=? AND ball=?',
                     (n, str(gid), str(uid), ball))


def balls_take(gid, uid, ball: str) -> bool:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                           (str(gid), str(uid), ball)).fetchone()
        if not row or (row['qty'] or 0) <= 0:
            return False
        conn.execute('UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball=?',
                     (str(gid), str(uid), ball))
        return True


def potions_get(gid, uid) -> dict:
    out = {p: 0 for p in POTIONS}
    with db.conn_ctx() as conn:
        for p in POTIONS:
            row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                               (str(gid), str(uid), p)).fetchone()
            out[p] = (row['qty'] if row else 0) or 0
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


def battle_image(p1_img: bytes, p2_img: bytes, p1: dict, p2: dict) -> bytes:
    """Game-style pixel battle: grass arena, platforms, nearest-neighbor
    pixel sprites, classic status boxes with HP bars."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, H = 900, 420
    img = _Img.new('RGB', (W, H), (24, 28, 40))
    d = _Dr.Draw(img)
    for y in range(300):
        tt = y / 300
        d.line([(0, y), (W, y)],
               fill=(int(24 + 10 * tt), int(28 + 26 * tt), int(40 + 30 * tt)))
    for y in range(300, H):
        tt = (y - 300) / (H - 300)
        d.line([(0, y), (W, y)],
               fill=(int(52 - 12 * tt), int(110 - 24 * tt), int(66 - 14 * tt)))
    d.ellipse([40, 292, 400, 318], fill=(38, 84, 50), outline=(30, 66, 40), width=3)
    d.ellipse([500, 200, 860, 226], fill=(38, 84, 50), outline=(30, 66, 40), width=3)
    d.ellipse([52, 296, 388, 312], fill=(48, 100, 60))
    d.ellipse([512, 204, 848, 220], fill=(48, 100, 60))
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_mid = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 20)
        f_hp = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 17)
    except Exception:
        f_mid = f_hp = _F.load_default()

    def _paste(raw, x, y, s):
        try:
            sp = _Img.open(_io.BytesIO(raw)).convert('RGBA').resize((s, s), _Img.NEAREST)
            canvas = img.convert('RGBA')
            canvas.paste(sp, (x, y), sp)
            img.paste(canvas.convert('RGB'))
            return True
        except Exception:
            return False

    if p1_img:
        _paste(p1_img, 110, 96, 200)
    else:
        d.ellipse([110, 96, 310, 296], outline=(90, 90, 98), width=3)
    if p2_img:
        _paste(p2_img, 600, 8, 190)
    else:
        d.ellipse([600, 8, 790, 198], outline=(90, 90, 98), width=3)

    def _statusbox(x, y, name, level, frac, hp_txt):
        bw, bh = 300, 78
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=10, fill=(16, 18, 24),
                            outline=(250, 200, 60), width=2)
        d.text((x + 14, y + 8), f'{name[:15]}  Lv{level}', font=f_mid, fill=(255, 255, 255))
        bx, by, bw2 = x + 14, y + 38, bw - 28
        d.rounded_rectangle([bx, by, bx + bw2, by + 14], radius=7, fill=(66, 44, 40))
        fw = max(12, int(bw2 * max(0.0, min(1.0, frac))))
        col = (87, 242, 135) if frac > 0.5 else ((250, 200, 60) if frac > 0.2 else (255, 90, 90))
        d.rounded_rectangle([bx, by, bx + fw, by + 14], radius=7, fill=col)
        try:
            w = d.textlength(hp_txt, font=f_hp)
            d.text((x + bw - w - 12, y + 56), hp_txt, font=f_hp, fill=(220, 220, 225))
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


class Pokemon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._hunt_cd = {}
        self._enc = {}    # (gid, uid) -> encounter dict
        self._battle = {}  # (gid, uid) -> battle state
        self._chatxp_cd = {}
        self._spawn_count = {}  # gid -> messages since last wild spawn
        self._wild = {}   # (gid, cid) -> shared channel encounter

    @commands.Cog.listener()
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
                dex = random.randint(1, 493)
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
            sil = silhouette_image(raw) if raw else None
            import io as _bio
            try:
                if sil:
                    view = self._layout(
                        message.guild.id, t(message.guild.id, 'eco.pk_wild_title', level=level),
                        t(message.guild.id, 'eco.pk_autospawn',
                          types='/'.join(row['types'] or ['?'])),
                        'attachment://who.png')
                    await message.channel.send(
                        view=view, file=discord.File(_bio.BytesIO(sil), 'who.png'))
                else:
                    view = self._layout(
                        message.guild.id, t(message.guild.id, 'eco.pk_wild_title', level=level),
                        t(message.guild.id, 'eco.pk_autospawn',
                          types='/'.join(row['types'] or ['?'])))
                    await message.channel.send(view=view)
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

    def _layout(self, gid, title, desc, img=None):
        from cogs.gamble import _game_layout
        return _game_layout(title, desc, img)

    async def _mage(self, gid, title, desc, sprite: str):
        """Message layout with remote sprite image (or no image)."""
        if sprite:
            return self._layout(gid, title, desc, sprite)
        return self._layout(gid, title, desc)

    # ----- starters / collection -----


    @commands.command(name='starter', description='Wybierz startera')
    async def starter(self, ctx, name: str = ''):
        import aiohttp
        gid = ctx.guild.id
        if my_mons(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'eco.pk_has'), ephemeral=True)
        pick = (name or '').lower().strip()
        dex = STARTERS.get(pick) or random.choice([1, 4, 7])
        if pick and pick not in STARTERS:
            return await ctx.reply(t(gid, 'eco.pk_starters'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, dex)
        if not row:
            return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,0,0,"",1)', (str(gid), str(ctx.author.id), dex, 5))
        balls_add(gid, ctx.author.id, 'poke', 10)
        spr = (row.get('sprite') or '').split('|')[0]
        await ctx.reply(view=await self._mage(
            gid, t(gid, 'eco.pk_starter_title'),
            t(gid, 'eco.pk_starter', name=row['name'].capitalize()), spr))

    @commands.command(name='hunt', description='Poluj na dzikie')
    async def hunt(self, ctx):
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
                dex = random.randint(1, 493)
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
        mons = my_mons(gid, ctx.author.id)
        avg_lv = sum(m['level'] for m in mons) / max(1, len(mons))
        level = max(3, min(70, int(random.gauss(avg_lv, 6))))
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
        self._enc[key] = {'dex': dex, 'level': level, 'shiny': shiny,
                          'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                          'mystery': mystery,
                          'exp': int(time.time()) + ENC_TTL}
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        pix = pix_url(dex, shiny)
        if mystery:
            raw = await fetch_sprite(s, pix)
            sil = silhouette_image(raw) if raw else None
            if sil:
                import io as _bio
                view = self._layout(gid, t(gid, 'eco.pk_wild_title', level=level),
                                    t(gid, 'eco.pk_mystery',
                                      types='/'.join(row['types'] or ['?'])) +
                                    (('\n🧪 ' + t(gid, 'eco.pk_incensed')) if inc else ''),
                                    'attachment://who.png')
                self._attach_enc_buttons(view, gid, ctx.author.id)
                return await ctx.reply(
                    view=view,
                    file=discord.File(_bio.BytesIO(sil), 'who.png'),
                    mention_author=False)
        name = ('✨' if shiny else '') + row['name'].capitalize()
        view = await self._mage(gid, t(gid, 'eco.pk_wild_title', level=level),
                                t(gid, 'eco.pk_wild', name=name,
                                  types='/'.join(row['types'] or ['?']),
                                  hint=t(gid, 'eco.pk_wild_hint')) +
                                (('\n🧪 ' + t(gid, 'eco.pk_incensed')) if inc else ''), pix)
        self._attach_enc_buttons(view, gid, ctx.author.id)
        await ctx.reply(view=view, mention_author=False)

    def _attach_enc_buttons(self, view, gid, uid):
        from discord.ui import ActionRow
        row = ActionRow()
        fight = discord.ui.Button(label='FIGHT', style=discord.ButtonStyle.danger,
                                  custom_id=f'pkfight:{uid}')
        ball = discord.ui.Button(label='THROW BALL', style=discord.ButtonStyle.success,
                                 custom_id=f'pkball:{uid}')

        async def _fight(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._start_battle(ix, gid, ix.user)

        async def _ball(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._throw(ix, gid, ix.user, 'poke')

        fight.callback = _fight
        ball.callback = _ball
        row.add_item(fight)
        row.add_item(ball)
        for child in view.children:
            if type(child).__name__ == 'Container':
                child.add_item(row)
                return
        view.add_item(row)

    def _get_enc(self, gid, uid):
        key = (str(gid), str(uid))
        e = self._enc.get(key)
        if not e or e.get('exp', 0) < int(time.time()):
            self._enc.pop(key, None)
            return None
        return e

    @commands.command(name='catch', description='Rzuć ball')
    async def catch(self, ctx, ball: str = 'poke'):
        gid = ctx.guild.id
        ball = (ball or 'poke').lower()
        if ball not in BALLS:
            return await ctx.reply(t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        e = self._get_enc(gid, ctx.author.id)
        if not e:
            return await ctx.reply(t(gid, 'eco.pk_noenc'), ephemeral=True)
        await ctx.typing()
        ok, msg = await self._do_catch(gid, ctx.author.id, e, ball)
        if ok:
            self._enc.pop((str(gid), str(ctx.author.id)), None)
        await ctx.reply(msg, mention_author=False)

    async def _throw(self, ix: discord.Interaction, gid, user, ball: str):
        e = self._get_enc(gid, user.id)
        if not e:
            return await ix.followup.send(t(gid, 'eco.pk_noenc'), ephemeral=True)
        ok, msg = await self._do_catch(gid, user.id, e, ball)
        if ok:
            self._enc.pop((str(gid), str(user.id)), None)
        await ix.followup.send(msg)

    async def _do_catch(self, gid, uid, e, ball: str):
        import aiohttp
        if not balls_take(gid, uid, ball):
            return False, t(gid, 'eco.pk_noball', ball=ball)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, e['dex'])
        mult = BALLS[ball][1]
        p = catch_chance(row.get('rate', 45), e['level'], e['hp'] / max(1, e['maxhp']), mult)
        if random.random() < p:
            first = not my_mons(gid, uid)
            with db.conn_ctx() as conn:
                conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                             'VALUES (?,?,?,?,0,?,?,?)',
                             (str(gid), str(uid), e['dex'], e['level'],
                              1 if e['shiny'] else 0, '', 1 if first else 0))
            msg = t(gid, 'eco.pk_caught', name=('✨' if e['shiny'] else '') + row['name'].capitalize(),
                    ball=ball, level=e['level'])
            for extra in await self._catch_progress(gid, uid, e['dex'], e['shiny']):
                msg += '\n' + extra
            msg += '\n' + self._catch_meta(gid, uid, e['dex'])
            return True, msg
        # break out with a bit of damage? no — it just stares back
        return False, t(gid, 'eco.pk_broke', name=row['name'].capitalize(), ball=ball)

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

    def _catch_meta(self, gid, uid, dex: int) -> str:
        row = _dex_row(dex)
        streak = leg_streak_bump(gid, uid, bool(row.get('legendary')))
        b = balls_get(gid, uid)
        return t(gid, 'eco.pk_catchmeta', rarity=rarity_of(dex), streak=streak,
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
            parts.append(f"incense x{iq} ({INCENSE_PRICE}$)")
        elif incense_active(gid, uid):
            parts.append('incense ON')
        return ' · '.join(parts)

    @commands.group(name='balls', description='Balle')
    async def balls(self, ctx):
        await ctx.reply(t(ctx.guild.id, 'eco.pk_balls', have=self._balls_line(ctx.guild.id, ctx.author.id)),
                        ephemeral=True)

    @balls.command(name='buy', description='Kup balle')
    async def balls_buy(self, ctx, ball: str, n: int = 1):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        ball = (ball or '').lower()
        if ball == 'incense':
            b = bal(gid, ctx.author.id)
            if INCENSE_PRICE > b['cash']:
                return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            set_cash(gid, ctx.author.id, b['cash'] - INCENSE_PRICE)
            balls_add(gid, ctx.author.id, 'incense', 1)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_balls SET expires=? WHERE guild_id=? AND user_id=? AND ball=?',
                             (int(time.time()) + INCENSE_SECONDS, str(gid), str(ctx.author.id)))
            return await ctx.reply(t(gid, 'eco.pk_incense_on'), ephemeral=True)
        catalog = {**{k: v[0] for k, v in BALLS.items()},
                   **{k: v[0] for k, v in POTIONS.items()},
                   'candy': CANDY_PRICE, 'egg': EGG_PRICE}
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
        await ctx.reply(t(gid, 'eco.pk_balls_bought', n=n, ball=ball), ephemeral=True)

    @commands.command(name='box', description='Twoje pokemony')
    async def box(self, ctx, page: int = 1):
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        per, page = 10, max(1, page or 1)
        total = (len(mons) + per - 1) // per
        page = min(page, total)
        lines = []
        for i, m in enumerate(mons[(page - 1) * per:page * per], start=(page - 1) * per + 1):
            star = '⭐' if m['active'] else ''
            lines.append(f"`{i}` {mon_name(m)} — Lv{m['level']}{star}")
        lines.append(t(gid, 'eco.pk_box_page', page=page, total=total, n=len(mons)))
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_box_title', user=ctx.author.display_name),
                                          '\n'.join(lines)), ephemeral=True)

    @commands.command(name='mon', description='Staty pokemona')
    async def info(self, ctx, slot: int):
        import aiohttp
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            row = await dex_get(s, m['dex'])
            moves = await moveset_for(s, m['dex'])
        stats = calc_stats(row, m['level'])
        nxt = XP_NEXT(m['level'])
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if m['shiny'] and len(spr) > 1 else spr[0]
        desc = t(gid, 'eco.pk_info', level=m['level'], types='/'.join(row['types'] or ['?']),
                 hp=stats['maxhp'], atk=stats['atk'], dfn=stats['dfn'],
                 spa=stats['spa'], spd=stats['spd'], spe=stats['spe'],
                 xp=m['xp'], nxt=nxt,
                 moves=', '.join(f"{x['name']}({x['power']})" for x in moves))
        await ctx.reply(view=await self._mage(gid, mon_name(m), desc, img))

    @commands.command(name='active', description='Wybierz wojownika')
    async def active(self, ctx, slot: int):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET active=0 WHERE guild_id=? AND owner_id=?',
                         (str(gid), str(ctx.author.id)))
            conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (m['id'],))
        await ctx.reply(t(gid, 'eco.pk_active', name=mon_name(m)), ephemeral=True)

    @commands.command(name='name', description='Przezwij pokemona')
    async def nick(self, ctx, slot: int, *, name: str = ''):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET nick=? WHERE id=?', (name[:24], m['id']))
        await ctx.reply(t(gid, 'eco.pk_nicked', name=(name[:24] or mon_name(m))), ephemeral=True)

    @commands.command(name='release', description='Wypuść pokemona')
    async def release(self, ctx, slot: int):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m)), ephemeral=True)
        name = mon_name(m)
        val = release_value(m)
        b = bal(gid, ctx.author.id)
        set_cash(gid, ctx.author.id, b['cash'] + val)
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        await ctx.reply(t(gid, 'eco.pk_released_cash', name=name, win=cshort(val)), ephemeral=True)

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
        total = 0
        with db.conn_ctx() as conn:
            for mid in ids:
                mm = conn.execute('SELECT * FROM pk_mons WHERE id=?', (mid,)).fetchone()
                if mm:
                    total += release_value(dict(mm))
            conn.execute(f"DELETE FROM pk_mons WHERE id IN ({','.join('?' * len(ids))})", ids)
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        b = _b2(gid, ctx.author.id)
        _s2(gid, ctx.author.id, b['cash'] + total)
        msg = t(gid, 'eco.pk_released_cash', name=f'{len(ids)}x {label}', win=cshort(total))
        if skipped_n:
            msg += ' ' + t(gid, 'eco.pk_releaseall_skip', n=skipped_n)
        await ctx.reply(msg, ephemeral=True)

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
        await ctx.reply(view=self._layout(
            gid, t(gid, 'eco.pk_stats_title', user=member.display_name),
            t(gid, 'eco.pk_stats', n=len(mons), lv=total_lv, dex=dex, catches=catches,
              shinies=shinies, w=(st['duels_won'] if st else 0), l=(st['duels_lost'] if st else 0))),
            ephemeral=True)

    @commands.command(name='trainers', description='Top trenerów')
    async def top(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT owner_id, SUM(level) lv, COUNT(*) n FROM pk_mons '
                                'WHERE guild_id=? GROUP BY owner_id ORDER BY lv DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_top_empty'), ephemeral=True)
        lines = []
        for i, r in enumerate(rows, 1):
            m = ctx.guild.get_member(int(r['owner_id']))
            nm = m.display_name if m else r['owner_id']
            lines.append(f"`{i}` **{nm}** — Lv{r['lv']} ({r['n']})")
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_top_title'), '\n'.join(lines)),
                        ephemeral=True)

    @commands.command(name='dex', description='Pokedex')
    async def dex(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT DISTINCT dex FROM pk_mons WHERE guild_id=? AND owner_id=?',
                                (str(gid), str(ctx.author.id))).fetchall()
            total = conn.execute('SELECT COUNT(*) c FROM pk_dex').fetchone()['c']
        caught = {r['dex'] for r in rows}
        names = []
        for dex in sorted(caught)[:12]:
            r = _dex_row(dex)
            names.append((r.get('name') or f'#{dex}').capitalize())
        await ctx.reply(view=self._layout(
            gid, t(gid, 'eco.pk_dex_title', n=len(caught)),
            t(gid, 'eco.pk_dex', names=', '.join(names) if names else '—', total=total)),
            ephemeral=True)

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
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,0,?,?,?)',
                         (str(gid), str(ctx.author.id), e['dex'], e['level'],
                          1 if e['shiny'] else 0, '', 1 if first else 0))
        if wild:
            self._wild.pop((str(gid), str(ctx.channel.id)), None)
        else:
            self._enc.pop((str(gid), str(ctx.author.id)), None)
        msg = t(gid, 'eco.pk_guessed', name=('✨' if e['shiny'] else '') + row['name'].capitalize())
        for extra in await self._catch_progress(gid, ctx.author.id, e['dex'], e['shiny']):
            msg += '\n' + extra
        msg += '\n' + self._catch_meta(gid, ctx.author.id, e['dex'])
        await ctx.reply(msg, mention_author=False)

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
        await ctx.reply(t(gid, 'eco.pk_hint',
                          hint=''.join('_' if i in blanks else ch for i, ch in enumerate(nm))),
                        ephemeral=True)

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
            return await ctx.reply(t(gid, 'eco.pk_hunt_status',
                                     name=(row.get('name') or '?').capitalize(),
                                     streak=h['streak'] or 0, denom=denom), ephemeral=True)
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
        await ctx.reply(t(gid, 'eco.pk_hunt_set',
                          name=(row.get('name') or f'#{dex}').capitalize()), ephemeral=True)

    @commands.command(name='quests', description='Misje regionów')
    async def quests(self, ctx):
        gid = ctx.guild.id
        prog = self._region_progress(gid, ctx.author.id)
        with db.conn_ctx() as conn:
            claimed = {r['track']: (r['tier'] or 0) for r in conn.execute(
                'SELECT track, tier FROM pk_quested WHERE guild_id=? AND user_id=?',
                (str(gid), str(ctx.author.id))).fetchall()}
        lines = []
        for track in REGIONS:
            p = prog[track]
            done = claimed.get(track, 0)
            nxt = next((i for i, need in enumerate(QUEST_TIERS) if p < need), None)
            if nxt is None:
                bar, info = '▰' * 10, t(gid, 'eco.pk_quest_done')
            else:
                need = QUEST_TIERS[nxt]
                fill = min(10, int(p / need * 10))
                bar = '▰' * fill + '▱' * (10 - fill)
                info = t(gid, 'eco.pk_quest_next', p=p, need=need,
                         win=cshort(QUEST_REWARDS[nxt]))
            lines.append(f"**{track.title()}** {bar} {info}")
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_quests_title'), '\n'.join(lines)),
                        ephemeral=True)

    @commands.command(name='sell', description='Wystaw na targ')
    async def sell(self, ctx, slot: int, price: int):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m)), ephemeral=True)
        if (price or 0) < 100:
            return await ctx.reply(t(gid, 'eco.pk_market_min'), ephemeral=True)
        with db.conn_ctx() as conn:
            n = conn.execute('SELECT COUNT(*) c FROM pk_market WHERE guild_id=? AND seller_id=?',
                             (str(gid), str(ctx.author.id))).fetchone()['c']
            if n >= 3:
                return await ctx.reply(t(gid, 'eco.pk_market_full'), ephemeral=True)
            conn.execute('INSERT INTO pk_market (guild_id, seller_id, seller_name, dex, level, xp, '
                         'shiny, nick, price, created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), ctx.author.display_name[:24],
                          m['dex'], m['level'], m['xp'], m['shiny'], m.get('nick') or '',
                          price, int(time.time())))
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
            lid = conn.execute('SELECT last_insert_rowid() i').fetchone()['i']
        await ctx.reply(t(gid, 'eco.pk_listed', name=mon_name(m), price=cshort(price), lid=lid),
                        ephemeral=True)

    @commands.command(name='market', description='Targ pokemonów')
    async def market(self, ctx, page: int = 1):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM pk_market WHERE guild_id=? ORDER BY id DESC LIMIT 40',
                (str(gid),)).fetchall()]
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_market_empty'), ephemeral=True)
        per, page = 8, max(1, page or 1)
        total = (len(rows) + per - 1) // per
        page = min(page, total)
        lines = []
        for r in rows[(page - 1) * per:page * per]:
            row = _dex_row(r['dex'])
            nm = (r['nick'] or (row.get('name') or f"#{r['dex']}").capitalize())
            if r['shiny']:
                nm = '✨' + nm
            lines.append(f"`{r['id']}` {nm} Lv{r['level']} — **{cshort(r['price'])}** ({r['seller_name'] or r['seller_id']})")
        lines.append(t(gid, 'eco.pk_market_page', page=page, total=total))
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_market_title'), '\n'.join(lines)),
                        ephemeral=True)

    @commands.command(name='buy', description='Kup z targu')
    async def buy(self, ctx, listing: int):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            r = conn.execute('SELECT * FROM pk_market WHERE id=? AND guild_id=?',
                             (listing or 0, str(gid))).fetchone()
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
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), r['dex'], r['level'], r['xp'],
                          r['shiny'], r['nick'], 1 if first else 0))
            conn.execute('DELETE FROM pk_market WHERE id=?', (r['id'],))
        row = _dex_row(r['dex'])
        await ctx.reply(t(gid, 'eco.pk_market_bought',
                          name=(r['nick'] or (row.get('name') or '?').capitalize()),
                          price=cshort(r['price'])), ephemeral=True)

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
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,?,?,?,?)',
                         (str(gid), str(ctx.author.id), r['dex'], r['level'], r['xp'],
                          r['shiny'], r['nick'], 1 if first else 0))
            conn.execute('DELETE FROM pk_market WHERE id=?', (r['id'],))
        await ctx.reply(t(gid, 'eco.pk_unlisted'), ephemeral=True)

    @commands.command(name='keep', description='Zabezpiecz pokemona')
    async def lock(self, ctx, slot: int):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET locked=? WHERE id=?',
                         (0 if m.get('locked') else 1, m['id']))
        await ctx.reply(t(gid, 'eco.pk_unlocked' if m.get('locked') else 'eco.pk_locked2',
                          name=mon_name(m)), ephemeral=True)

    @commands.command(name='buddy', description='Kumpel')
    async def buddy(self, ctx, slot: int = 0, *, name: str = ''):
        """No buddy: shows. Slot: sets buddy. Extra text: renames buddy."""
        gid = ctx.guild.id
        b = buddy_get(gid, uid := ctx.author.id)
        if not slot:
            if not b.get('mid'):
                return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
            with db.conn_ctx() as conn:
                m = conn.execute('SELECT * FROM pk_mons WHERE id=?', (b['mid'],)).fetchone()
            if not m:
                with db.conn_ctx() as c2:
                    c2.execute('DELETE FROM pk_buddy WHERE guild_id=? AND user_id=?',
                               (str(gid), str(uid)))
                return await ctx.reply(t(gid, 'eco.pk_buddy_none'), ephemeral=True)
            m = dict(m)
            row = _dex_row(m['dex'])
            return await ctx.reply(t(gid, 'eco.pk_buddy_info', name=mon_name(m),
                                     hearts='❤' * min(10, b.get('hearts', 0)) or '—',
                                     level=m['level'],
                                     types='/'.join(row.get('types') or ['?'])), ephemeral=True)
        m = get_mon(gid, uid, slot)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO pk_buddy (guild_id, user_id, mid, hearts) '
                         'VALUES (?,?,?,COALESCE((SELECT hearts FROM pk_buddy WHERE guild_id=? AND user_id=?),0))',
                         (str(gid), str(uid), m['id'], str(gid), str(uid)))
            if name.strip():
                conn.execute('UPDATE pk_mons SET nick=? WHERE id=?', (name.strip()[:24], m['id']))
        extra = t(gid, 'eco.pk_nicked', name=name.strip()[:24]) if name.strip() else ''
        await ctx.reply(t(gid, 'eco.pk_buddy_set', name=mon_name(m)) + (('\n' + extra) if extra else ''),
                        ephemeral=True)

    @commands.command(name='team', description='Drużyna na pojedynki')
    async def team(self, ctx, a: int = 0, b: int = 0, c: int = 0):
        """;team — show. `;team 1 2 3` — set duel team by box slots."""
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        if not mons:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        if not a:
            team = team_get(gid, ctx.author.id)
            lines = [f"`{i}` {mon_name(m)} — Lv{m['level']}" for i, m in enumerate(team, 1)]
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
        await ctx.reply(t(gid, 'eco.pk_team_set', n=len([p for p in picks if p])), ephemeral=True)

    @commands.command(name='candy', description='Rare candy +1 level')
    async def candy(self, ctx):
        import aiohttp
        gid = ctx.guild.id
        mons = my_mons(gid, ctx.author.id)
        act = next((m for m in mons if m['active']), None)
        if not act:
            return await ctx.reply(t(gid, 'eco.pk_need_starter'), ephemeral=True)
        if act['level'] >= 100:
            return await ctx.reply(t(gid, 'eco.pk_candy_max', name=mon_name(act)), ephemeral=True)
        with db.conn_ctx() as conn:
            row = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='candy'",
                               (str(gid), str(ctx.author.id))).fetchone()
            if not row or not row['qty']:
                return await ctx.reply(t(gid, 'eco.pk_candy_none'), ephemeral=True)
            conn.execute("UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball='candy'",
                         (str(gid), str(ctx.author.id)))
        msgs = await self._gain_xp(gid, ctx.author.id, act['id'], act['level'] ** 3 - (act['xp'] or 0))
        await ctx.reply('\n'.join(msgs) if msgs else t(gid, 'eco.pk_candy_use', name=mon_name(act)))

    @commands.command(name='eggs', description='Jajka')
    async def eggs(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT id, cycles FROM pk_eggs WHERE guild_id=? AND owner_id=? ORDER BY id',
                (str(gid), str(ctx.author.id))).fetchall()]
        if not rows:
            return await ctx.reply(t(gid, 'eco.pk_eggs_none'), ephemeral=True)
        lines = [t(gid, 'eco.pk_egg_row', lid=r['id'],
                   state=t(gid, 'eco.pk_egg_ready') if r['cycles'] <= 0
                   else t(gid, 'eco.pk_egg_cycles', n=r['cycles'])) for r in rows]
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
                dex = random.randint(1, 493)
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
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,0,?,?,?)',
                         (str(gid), str(ctx.author.id), dex, level, 1 if shiny else 0, '',
                          1 if first else 0))
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        await ctx.reply(view=await self._mage(
            gid, t(gid, 'eco.pk_hatched_title'),
            t(gid, 'eco.pk_hatched', name=('✨' if shiny else '') + row['name'].capitalize(),
              level=level), img))

    @commands.command(name='swap', description='Losowa wymiana')
    async def swap(self, ctx, slot: int):
        import aiohttp
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if m.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(m)), ephemeral=True)
        old = mon_name(m)
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 493)
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
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active) '
                         'VALUES (?,?,?,?,0,?,?,?)',
                         (str(gid), str(ctx.author.id), dex, level, 1 if shiny else 0, '',
                          1 if first else 0))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        await ctx.reply(view=await self._mage(
            gid, t(gid, 'eco.pk_swapped_title'),
            t(gid, 'eco.pk_swapped', old=old,
              name=('✨' if shiny else '') + row['name'].capitalize(), level=level), img))

    @commands.command(name='target', description='Dzienny cel')
    async def target(self, ctx):
        gid = ctx.guild.id
        d = daily_row(gid, ctx.author.id)
        row = _dex_row(d['target'])
        await ctx.reply(view=self._layout(
            gid, t(gid, 'eco.pk_target_title'),
            t(gid, 'eco.pk_target',
              name=(row.get('name') or '?').capitalize() if d['target'] else '—',
              win=cshort(DAILY_TARGET_REWARD),
              state=t(gid, 'eco.pk_target_done') if d['claimed'] else t(gid, 'eco.pk_target_open'))),
            ephemeral=True)

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
                   ok='✅' if have[k] >= need[k] else '⬜') for k in need]
        if d['checklist']:
            lines.append(t(gid, 'eco.pk_check_claimed'))
        elif done:
            lines.append(t(gid, 'eco.pk_check_ready'))
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_check_title'), '\n'.join(lines)),
                        ephemeral=True)

    # ----- battles -----

    async def _fighter(self, session, mon: dict, level: int = None):
        row = await dex_get(session, mon['dex'] if 'dex' in mon else mon)
        lv = mon.get('level', level or 5)
        stats = calc_stats(row, lv)
        return {'name': mon_name(mon) if 'owner_id' in mon or 'nick' in mon else row['name'].capitalize(),
                'dex': mon.get('dex', mon), 'level': lv, 'types': row.get('types') or ['normal'],
                'stats': stats, 'hp': stats['maxhp'],
                'moves': await moveset_for(session, mon.get('dex', mon)),
                'shiny': mon.get('shiny', 0), 'row': row}

    async def _start_battle(self, ix_or_ctx, gid, user):
        import aiohttp
        is_ix = isinstance(ix_or_ctx, discord.Interaction)
        e = self._get_enc(gid, user.id)
        if not e:
            msg = t(gid, 'eco.pk_noenc')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        mons = my_mons(gid, user.id)
        act = next((m for m in mons if m['active']), None) or (mons[0] if mons else None)
        if not act:
            msg = t(gid, 'eco.pk_need_starter')
            if is_ix:
                return await ix_or_ctx.followup.send(msg, ephemeral=True)
            return await ix_or_ctx.reply(msg, ephemeral=True)
        async with aiohttp.ClientSession() as s:
            me = await self._fighter(s, act)
            wild = await self._fighter(s, {'dex': e['dex'], 'level': e['level'],
                                           'shiny': e['shiny'], 'nick': ''})
            me_spr = await fetch_sprite(s, pix_url(me['dex'], bool(me['shiny']), back=True))
            wild_spr = await fetch_sprite(s, pix_url(e['dex'], bool(wild['shiny'])))
        wild['hp'] = e['hp']
        key = (str(gid), str(user.id))
        self._battle[key] = {'me': me, 'wild': wild, 'mid': act['id'], 'log': [],
                             'me_spr': me_spr, 'wild_spr': wild_spr,
                             'weather': roll_weather(), 'fainted': set(),
                             'sent': False}
        await self._send_battle(ix_or_ctx, is_ix, gid, user.id)

    def _team_line(self, gid, name: str, hp: int, maxhp: int, fainted: bool = False) -> str:
        return f"{hp_dot(hp / max(1, maxhp), fainted)} {name} HP {hp}/{maxhp}"

    def _vs_wild_body(self, gid, uid, st) -> str:
        me, wild, log = st['me'], st['wild'], st['log']
        who = f'<@{uid}>'
        body = t(gid, 'eco.pk_vs', a=who, b=t(gid, 'eco.pk_wild_foe', name=wild['name']))
        body += '\n' + t(gid, 'eco.pk_sentout', ball='🔴', who=who, name=me['name'])
        body += '\n' + t(gid, 'eco.pk_sentout_wild', ball='🔵', name=wild['name'])
        if st.get('weather') in WEATHER_LINE:
            body += '\n' + t(gid, WEATHER_LINE[st['weather']])
        body += '\n' + t(gid, 'eco.pk_team_of', user=who)
        for m in team_get(gid, uid)[:3]:
            if m['id'] == st.get('mid'):
                body += '\n' + self._team_line(gid, me['name'], me['hp'], me['stats']['maxhp'])
            else:
                row = _dex_row(m['dex'])
                nm = (m.get('nick') or (row.get('name') or '?').capitalize())
                if m.get('shiny'):
                    nm = '✨' + nm
                body += '\n' + self._team_line(gid, nm, 1, 1)
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
        body += '\n' + t(gid, 'eco.pk_sentout', ball='🔴', who=ua, name=a['name'])
        body += '\n' + t(gid, 'eco.pk_sentout', ball='🔵', who=ub, name=b['name'])
        if st.get('weather') in WEATHER_LINE:
            body += '\n' + t(gid, WEATHER_LINE[st['weather']])
        for tag, team, mids, idx in (('pa', st['t1'], st['m1'], st['i1']),
                                     ('pb', st['t2'], st['m2'], st['i2'])):
            owner = ua if tag == 'pa' else ub
            body += '\n' + t(gid, 'eco.pk_team_of', user=owner)
            for j, (f, mid) in enumerate(zip(team, mids)):
                fainted = f['hp'] <= 0 or mid in st.get('fainted', set())
                body += '\n' + self._team_line(gid, f['name'], f['hp'], f['stats']['maxhp'], fainted)
        if st['log']:
            body += '\n' + '\n'.join(st['log'][-4:])
        return body

    def _scene_file(self, st):
        import io as _bio
        try:
            png = battle_image(st.get('me_spr'), st.get('wild_spr'),
                               st['me'], st['wild'])
            return discord.File(_bio.BytesIO(png), 'battle.png')
        except Exception:
            return None

    async def _show_battle(self, ix_or_ctx, is_new_ctx: bool, st, view, f):
        """Send the battle message once, edit it on later turns."""
        if st.get('msg') is not None:
            try:
                if f:
                    await st['msg'].edit(view=view, attachments=[f])
                else:
                    await st['msg'].edit(view=view)
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
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0xFF4655)
        box.add_item(TextDisplay(self._vs_wild_body(gid, uid, st)))
        box.add_item(MediaGallery(MediaGalleryItem(media='attachment://battle.png')))
        b = balls_get(gid, uid)
        box.add_item(TextDisplay(
            f"-# ⚪ Balls: poke x{b['poke']} · great x{b['great']} · ultra x{b['ultra']} · master x{b['master']}"))
        moves = (me.get('moves') or [])[:4]
        row = ActionRow()
        for i, mv in enumerate(moves):
            b = discord.ui.Button(
                label=f"{mv['name'][:16]} {mv['power']}"[:80],
                style=discord.ButtonStyle.primary, custom_id=f'pkmv:{uid}:{i}')
            b.callback = self._mk_battle_btn(gid, uid, ('move', i))
            row.add_item(b)
        box.add_item(row)
        row2 = ActionRow()
        pots = potions_get(gid, uid)
        plabel = f"POTION ({pots['potion'] + pots['superpotion']})"
        for label, cid in ((plabel, 'pk_potion'), ('BALL', 'pk_ball'), ('RUN', 'pk_run')):
            b = discord.ui.Button(label=label[:80],
                                  style=discord.ButtonStyle.secondary if label != plabel
                                  else discord.ButtonStyle.success,
                                  custom_id=f'{cid}:{uid}')
            b.callback = self._mk_battle_btn(gid, uid, cid)
            row2.add_item(b)
        box.add_item(row2)
        row3 = ActionRow()
        for m in team_get(gid, uid)[:5]:
            rowm = _dex_row(m['dex'])
            nm = (m.get('nick') or (rowm.get('name') or '?').capitalize())[:16]
            if m.get('shiny'):
                nm = '✨' + nm
            cur = (m['id'] == st.get('mid'))
            b = discord.ui.Button(label=('▶ ' if cur else '') + nm[:80],
                                  style=discord.ButtonStyle.success if cur
                                  else discord.ButtonStyle.secondary,
                                  custom_id=f'pksw:{uid}:{m["id"]}',
                                  disabled=cur)
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
            return await ix.followup.send(view=self._layout(
                gid, t(gid, 'eco.pk_fled_title'), t(gid, 'eco.pk_fled')))
        if what == 'pk_ball':
            e = self._get_enc(gid, user.id)
            if e:
                e['hp'] = wild['hp']
            ok, msg = await self._do_catch(gid, user.id, e or {'dex': 0}, 'ultra'
                                           if balls_get(gid, user.id).get('ultra', 0) else
                                           'great' if balls_get(gid, user.id).get('great', 0) else 'poke')
            if ok:
                self._battle.pop(key, None)
                self._enc.pop(key, None)
            return await ix.followup.send(msg)
        if what == 'pk_potion':
            pots = potions_get(gid, user.id)
            use = ('superpotion' if me['hp'] < me['stats']['maxhp'] * 0.5 and pots['superpotion']
                   else 'potion' if pots['potion'] else 'superpotion' if pots['superpotion'] else None)
            if not use:
                log.append(t(gid, 'eco.pk_no_potion'))
            else:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball=?',
                                 (str(gid), str(user.id), use))
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
                return await ix.followup.send(view=self._battle_view(gid, user.id, st))
            async with aiohttp.ClientSession() as s:
                me2 = await self._fighter(s, nm)
                me2_spr = await fetch_sprite(s, pix_url(nm['dex'], bool(nm['shiny']), back=True))
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
            log.append(t(gid, 'eco.pk_switched', name=mon_name(nm)))
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
                use_mv = chosen or random.choice(me['moves'])
                await self._strike(gid, me, wild, use_mv, True, log, st.get('weather'))
            else:
                if me['hp'] <= 0:
                    break
                await self._strike(gid, wild, me, random.choice(wild['moves']), False, log,
                                   st.get('weather'))
        return await self._after_turn(ix, gid, user, key, st)

    async def _strike(self, gid, att, dfn, mv, is_me: bool, log: list, weather=None):
        dmg, crit = damage(att['level'], mv, att['stats'], dfn['stats'],
                           att['types'], dfn['types'], weather)
        dfn['hp'] = max(0, dfn['hp'] - dmg)
        eff = effectiveness(mv.get('ptype', 'normal'), dfn['types'])
        tag = ' 💥' if eff > 1 else (' 🛡' if eff < 1 else '')
        if crit:
            tag += ' ✨CRIT'
        who = t(gid, 'eco.pk_you') if is_me else t(gid, 'eco.pk_foe')
        log.append(t(gid, 'eco.pk_hit', who=who, move=mv['name'], dmg=dmg) + tag)

    async def _wild_strike(self, gid, me, wild, log: list, weather=None):
        await self._strike(gid, wild, me, random.choice(wild['moves']), False, log, weather)

    async def _after_turn(self, ix: discord.Interaction, gid, user, key, st):
        me, wild, log = st['me'], st['wild'], st['log']
        if wild['hp'] <= 0:
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            gain = wild['level'] * 10
            msgs = [t(gid, 'eco.pk_ko', name=wild['name'], xp=gain)]
            ev = await self._gain_party_xp(gid, user.id, st['mid'], gain)
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
            msgs.append(t(gid, 'eco.pk_levelup', name=mon_name(m), level=lv))
        if row.get('evo_to') and row.get('evo_level') and lv >= row['evo_level']:
            async with aiohttp.ClientSession() as s:
                nxt = await dex_get(s, row['evo_to'])
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET dex=?, level=?, xp=? WHERE id=?',
                             (row['evo_to'], lv, xp, mid))
            msgs.append(t(gid, 'eco.pk_evolve', old=mon_name(m),
                          new=('✨' if m.get('shiny') else '') + nxt['name'].capitalize()))
        else:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET level=?, xp=? WHERE id=?', (lv, xp, mid))
        return msgs

    @commands.command(name='battle', description='Walcz z dzikim')
    async def battle(self, ctx):
        await self._start_battle(ctx, ctx.guild.id, ctx.author)

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
        box.add_item(TextDisplay(t(gid, 'eco.pk_duel_challenge', a=ctx.author.display_name,
                                   b=member.display_name,
                                   wager=(cshort(wager) if wager else '—'))))
        row = ActionRow()
        ok_b = discord.ui.Button(label='ACCEPT', style=discord.ButtonStyle.success)
        no_b = discord.ui.Button(label='DECLINE', style=discord.ButtonStyle.danger)

        async def _ok(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._duel_start(ix, gid, ctx.author, member, wager)

        async def _no(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.send_message(t(gid, 'eco.pk_declined'))

        ok_b.callback = _ok
        no_b.callback = _no
        row.add_item(ok_b)
        row.add_item(no_b)
        box.add_item(row)
        layout.add_item(box)
        await ctx.reply(f'<@{member.id}>', view=layout, mention_author=False)

    async def _duel_start(self, ix: discord.Interaction, gid, u1, u2, wager: int):
        import aiohttp
        t1 = team_get(gid, u1.id)[:3]
        t2 = team_get(gid, u2.id)[:3]
        if not t1 or not t2:
            return await ix.followup.send(t(gid, 'eco.pk_duel_need'), ephemeral=True)
        if wager:
            from cogs.gamble import bal
            if bal(gid, u1.id)['cash'] < wager or bal(gid, u2.id)['cash'] < wager:
                return await ix.followup.send(t(gid, 'eco.pk_duel_cash'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            f1, f2, s1, s2, m1, m2 = [], [], [], [], [], []
            for m in t1:
                f = await self._fighter(s, m)
                f1.append(f)
                m1.append(m['id'])
                s1.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']), back=True)))
            for m in t2:
                f = await self._fighter(s, m)
                f2.append(f)
                m2.append(m['id'])
                s2.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']))))
        key = (str(gid), str(u1.id), str(u2.id))
        self._battle[key] = {'duel': True, 't1': f1, 't2': f2, 'm1': m1, 'm2': m2,
                             'i1': 0, 'i2': 0, 's1': s1, 's2': s2,
                             'u1': u1.id, 'u2': u2.id,
                             'wager': wager, 'turn': u1.id, 'log': [],
                             'weather': roll_weather(), 'fainted': set(),
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
            f"-# ⚪ Balls: poke x{bb['poke']} · great x{bb['great']} · ultra x{bb['ultra']} · master x{bb['master']}"))
        row = ActionRow()
        for i, mv in enumerate((turn_side.get('moves') or [])[:4]):
            b = discord.ui.Button(label=f"{mv['name'][:14]} {mv['power']}"[:80],
                                  style=discord.ButtonStyle.primary,
                                  custom_id=f'pkd:{key[1]}:{i}')
            b.callback = self._mk_duel_btn(gid, key, ('move', i))
            row.add_item(b)
        box.add_item(row)
        row2 = ActionRow()
        for label, cid in (('POTION', 'potion'), ('FORFEIT', 'forfeit')):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'pkd:{key[1]}:{cid}')
            b.callback = self._mk_duel_btn(gid, key, cid)
            row2.add_item(b)
        box.add_item(row2)
        # switch row: own team, current/fainted disabled
        mine = 't1' if st['turn'] == st['u1'] else 't2'
        row3 = ActionRow()
        for j, (f, mid) in enumerate(zip(st[mine], st['m1' if mine == 't1' else 'm2'])):
            cur = (st['i1' if mine == 't1' else 'i2'] == j)
            dead = f['hp'] <= 0 or mid in st.get('fainted', set())
            b = discord.ui.Button(label=('▶ ' if cur else '') + f['name'][:14],
                                  style=discord.ButtonStyle.success if cur
                                  else discord.ButtonStyle.secondary,
                                  custom_id=f'pkdsw:{key[1]}:{mid}',
                                  disabled=(cur or dead))
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
                    mv = random.choice(fo_now['moves'])
                    dmg, crit = damage(fo_now['level'], mv, fo_now['stats'], tgt['stats'],
                                       fo_now['types'], tgt['types'], st.get('weather'))
                    tgt['hp'] = max(0, tgt['hp'] - dmg)
                    eff = effectiveness(mv.get('ptype', 'normal'), tgt['types'])
                    tag = ' 💥' if eff > 1 else (' 🛡' if eff < 1 else '')
                    if crit:
                        tag += ' ✨CRIT'
                    log.append(t(gid, 'eco.pk_hit', who=fo_now['name'], move=mv['name'], dmg=dmg) + tag)
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
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_balls SET qty=qty-1 WHERE guild_id=? AND user_id=? AND ball=?',
                             (str(gid), str(uid), use))
            heal = int(me['stats']['maxhp'] * POTIONS[use][1])
            me['hp'] = min(me['stats']['maxhp'], me['hp'] + heal)
            log.append(t(gid, 'eco.pk_healed', name=me['name'], hp=heal))
        else:
            mv = None
            if isinstance(what, tuple) and what[0] == 'move':
                idx = what[1]
                if 0 <= idx < len(me.get('moves') or []):
                    mv = me['moves'][idx]
            mv = mv or random.choice(me['moves'])
            dmg, crit = damage(me['level'], mv, me['stats'], fo['stats'], me['types'], fo['types'],
                               st.get('weather'))
            fo['hp'] = max(0, fo['hp'] - dmg)
            eff = effectiveness(mv.get('ptype', 'normal'), fo['types'])
            tag = ' 💥' if eff > 1 else (' 🛡' if eff < 1 else '')
            if crit:
                tag += ' ✨CRIT'
            log.append(t(gid, 'eco.pk_hit', who=me['name'], move=mv['name'], dmg=dmg) + tag)
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
            mv = random.choice(a['moves'])
            dmg, crit = damage(a['level'], mv, a['stats'], b['stats'], a['types'], b['types'],
                               st.get('weather'))
            b['hp'] = max(0, b['hp'] - dmg)
            eff = effectiveness(mv.get('ptype', 'normal'), b['types'])
            tag = ' 💥' if eff > 1 else (' 🛡' if eff < 1 else '')
            if crit:
                tag += ' ✨CRIT'
            log.append(t(gid, 'eco.pk_hit', who=a['name'], move=mv['name'], dmg=dmg) + tag)
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
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b)
            f = discord.File(_bio.BytesIO(png), 'duel.png')
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
            # NPC takes no prisoners and no prizes
            await ix.followup.send(view=self._layout(
                gid, t(gid, 'eco.pk_duel_title'),
                '\n'.join(st['log'][-6:] + [t(gid, 'eco.pk_npc_lose', name=st['npc'])]),
                'attachment://battle.png'),
                file=self._duel_final_file(st))
            return
        gain = foe['level'] * 12
        ev = await self._gain_party_xp(gid, uwin, fin_mid, gain)
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
            b = bal(gid, uwin)
            set_cash(gid, uwin, b['cash'] + prize)
            line += '\n' + t(gid, 'eco.pk_npc_win', prize=cshort(prize))
            with db.conn_ctx() as conn:
                conn.execute('INSERT OR REPLACE INTO pk_npc (guild_id, user_id, npc, day) VALUES (?,?,?,?)',
                             (str(gid), str(uwin), st.get('npckey', ''), int(_t.time()) // 86400))
            if st.get('npckey') == 'cyntia':
                with db.conn_ctx() as c2:
                    c2.execute('INSERT OR IGNORE INTO achievements (guild_id, user_id, akey, unlocked_at) '
                               'VALUES (?,?,?,?)',
                               (str(gid), str(uwin), 'npc_champ', int(_t.time())))
                line += '\n' + t(gid, 'eco.pk_npc_badge')
        wager = st.get('wager', 0)
        if wager:
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
        await ix.followup.send(view=self._layout(
            gid, t(gid, 'eco.pk_duel_title'),
            '\n'.join(st['log'][-6:] + ev + [line]),
            'attachment://battle.png'),
            file=self._duel_final_file(st))

    def _duel_final_file(self, st):
        import io as _bio
        try:
            a, b = self._duel_pair(st)
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b)
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
        await ctx.reply(view=self._layout(
            gid, t(gid, 'eco.pk_move_title', name=mv['name']),
            t(gid, 'eco.pk_move', power=mv['power'], ptype=mv['ptype'], acc=mv['acc'])),
            ephemeral=True)

    @commands.command(name='moves', description='Ruchy pokemona')
    async def moves(self, ctx, slot: int):
        import aiohttp
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        async with aiohttp.ClientSession() as s:
            ms = await moveset_for(s, m['dex'])
        await ctx.reply(view=self._layout(
            gid, t(gid, 'eco.pk_moves_title', name=mon_name(m)),
            '\n'.join(t(gid, 'eco.pk_move_row', name=x['name'], power=x['power'],
                         ptype=x['ptype'], acc=x['acc']) for x in ms)), ephemeral=True)

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
            with db.conn_ctx() as conn:
                for key, name, lv, size, prize in NPCS:
                    row = conn.execute('SELECT day FROM pk_npc WHERE guild_id=? AND user_id=? AND npc=?',
                                       (str(gid), str(ctx.author.id), key)).fetchone()
                    done = row and (row['day'] or 0) == today
                    lines.append(t(gid, 'eco.pk_npc_row', n=key, name=name, lv=lv, size=size,
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
        await self._npc_start(ctx, gid, ctx.author, npc)

    async def _npc_start(self, ctx, gid, user, npc):
        import aiohttp
        key, name, lv, size, prize = npc
        team = team_get(gid, user.id)[:3]
        async with aiohttp.ClientSession() as s:
            f1, s1, m1 = [], [], []
            for m in team:
                f = await self._fighter(s, m)
                f1.append(f)
                m1.append(m['id'])
                s1.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']), back=True)))
            f2, s2, awaited = [], [], None
            for _ in range(size):
                for _try in range(12):
                    dex = random.randint(1, 493)
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
                        'shiny': 0, 'nick': ''})
                f2.append(f)
                s2.append(await fetch_sprite(s, pix_url(f['dex'], bool(f['shiny']))))
        if not f2:
            return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        bkey = (str(gid), str(user.id), f'npc:{key}')
        self._battle[bkey] = {'duel': True, 'npc': name, 'prize': prize, 'npckey': key,
                              't1': f1, 't2': f2, 'm1': m1, 'm2': [],
                              'i1': 0, 'i2': 0, 's1': s1, 's2': s2,
                              'u1': user.id, 'u2': f'npc:{key}',
                              'wager': 0, 'turn': user.id, 'log': [
                                  t(gid, 'eco.pk_npc_start', name=name)],
                              'weather': roll_weather(), 'fainted': set(),
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
            png = battle_image(st['s1'][st['i1']], st['s2'][st['i2']], a, b)
            f = discord.File(_bio.BytesIO(png), 'duel.png')
        except Exception:
            pass
        if f:
            st['msg'] = await ctx.reply(view=view, file=f, mention_author=False)
        else:
            st['msg'] = await ctx.reply(view=view, mention_author=False)

    @commands.command(name='trade', description='Wymień pokemona')
    async def trade(self, ctx, member: discord.Member, yours: int, theirs: int):
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pk_duel_self'), ephemeral=True)
        mine = get_mon(gid, ctx.author.id, yours or 0)
        want = get_mon(gid, member.id, theirs or 0)
        if not mine or not want:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        if mine.get('locked'):
            return await ctx.reply(t(gid, 'eco.pk_locked', name=mon_name(mine)), ephemeral=True)
        from discord.ui import ActionRow, Container, TextDisplay
        from discord.ui import LayoutView
        layout = LayoutView(timeout=90)
        box = Container(accent_color=0x57F287)
        box.add_item(TextDisplay(t(gid, 'eco.pk_trade_offer', a=ctx.author.display_name,
                                   m1=mon_name(mine), b=member.display_name, m2=mon_name(want))))
        row = ActionRow()
        ok_b = discord.ui.Button(label='ACCEPT', style=discord.ButtonStyle.success)
        no_b = discord.ui.Button(label='DECLINE', style=discord.ButtonStyle.danger)

        async def _ok(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            m1 = get_mon(gid, ctx.author.id, yours or 0)
            m2 = get_mon(gid, member.id, theirs or 0)
            if not m1 or not m2:
                return await ix.response.send_message(t(gid, 'eco.pk_gone'), ephemeral=True)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=?',
                             (str(member.id), m1['id']))
                conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=?',
                             (str(ctx.author.id), m2['id']))
                for uid in (str(ctx.author.id), str(member.id)):
                    r = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? '
                                     'ORDER BY id LIMIT 1', (str(gid), uid)).fetchone()
                    if r:
                        conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (r['id'],))
            await ix.response.send_message(t(gid, 'eco.pk_traded', m1=mon_name(m1), m2=mon_name(m2)))

        async def _no(ix: discord.Interaction):
            set_ctx_lang(ix.user)
            if ix.user.id != member.id:
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.send_message(t(gid, 'eco.pk_declined'))

        ok_b.callback = _ok
        no_b.callback = _no
        row.add_item(ok_b)
        row.add_item(no_b)
        box.add_item(row)
        layout.add_item(box)
        await ctx.reply(f'<@{member.id}>', view=layout, mention_author=False)


async def setup(bot):
    await bot.add_cog(Pokemon(bot))

"""Babka Pokemon: wild hunts, catching, collection, levels, evolution,
turn-based wild battles, auto PvP duels with wagers, trading, shinies.
Data from PokeAPI (cached in DB). `.pk` for the menu."""
import random
import time

import discord
from discord.ext import commands

import database as db
from lang import t
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
STARTERS = {'bulbasaur': 1, 'charmander': 4, 'squirtle': 7,
            'pikachu': 25, 'eevee': 133}

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
           att_types: list, dfn_types: list) -> int:
    a = atk_stats['atk'] if atk_stats['atk'] >= atk_stats['spa'] else atk_stats['spa']
    d = dfn_stats['dfn'] if atk_stats['atk'] >= atk_stats['spa'] else dfn_stats['spd']
    if random.random() * 100 > (move.get('acc') or 100):
        return 0
    stab = 1.5 if move.get('ptype') in att_types else 1.0
    eff = effectiveness(move.get('ptype', 'normal'), dfn_types)
    base = ((2 * att_level / 5 + 2) * (move.get('power') or 40) * max(1, a) / max(1, d)) / 50 + 2
    return max(1, int(base * stab * eff * random.uniform(0.85, 1.0)))


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


class Pokemon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._hunt_cd = {}
        self._enc = {}    # (gid, uid) -> encounter dict
        self._battle = {}  # (gid, uid) -> battle state

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

    @commands.group(name='pk', description='Pokemony Babki')
    async def pk(self, ctx):
        await ctx.reply('.pk starter / hunt / catch / box / info / dex / balls / battle / duel / trade', ephemeral=True)

    @pk.command(name='starter', description='Wybierz startera')
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

    @pk.command(name='hunt', description='Poluj na dzikie')
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
        async with aiohttp.ClientSession() as s:
            for _ in range(12):
                dex = random.randint(1, 493)
                row = await dex_get(s, dex)
                if not row:
                    continue
                r = random.random()
                is_leg = bool(row.get('legendary'))
                if (is_leg and r < 0.25) or (not is_leg and r < 0.9):
                    break
            else:
                return await ctx.reply(t(gid, 'eco.pk_api'), ephemeral=True)
        mons = my_mons(gid, ctx.author.id)
        avg_lv = sum(m['level'] for m in mons) / max(1, len(mons))
        level = max(3, min(70, int(random.gauss(avg_lv, 6))))
        shiny = random.randint(1, SHINY_ODDS) == 1
        stats = calc_stats(row, level)
        self._enc[key] = {'dex': dex, 'level': level, 'shiny': shiny,
                          'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                          'exp': int(time.time()) + ENC_TTL}
        spr = (row.get('sprite') or '').split('|')
        img = spr[1] if shiny and len(spr) > 1 else spr[0]
        name = ('✨' if shiny else '') + row['name'].capitalize()
        view = await self._mage(gid, t(gid, 'eco.pk_wild_title', level=level),
                                t(gid, 'eco.pk_wild', name=name,
                                  types='/'.join(row['types'] or ['?']),
                                  hint=t(gid, 'eco.pk_wild_hint')), img)
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
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._start_battle(ix, gid, ix.user)

        async def _ball(ix: discord.Interaction):
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

    @pk.command(name='catch', description='Rzuć ball')
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
            return True, t(gid, 'eco.pk_caught', name=('✨' if e['shiny'] else '') + row['name'].capitalize(),
                           ball=ball, level=e['level'])
        # break out with a bit of damage? no — it just stares back
        return False, t(gid, 'eco.pk_broke', name=row['name'].capitalize(), ball=ball)

    def _balls_line(self, gid, uid) -> str:
        b = balls_get(gid, uid)
        return ' · '.join(f"{k} x{v} ({BALLS[k][0]}$)" for k, v in b.items())

    @pk.group(name='balls', description='Balle')
    async def balls(self, ctx):
        await ctx.reply(t(ctx.guild.id, 'eco.pk_balls', have=self._balls_line(ctx.guild.id, ctx.author.id)),
                        ephemeral=True)

    @balls.command(name='buy', description='Kup balle')
    async def balls_buy(self, ctx, ball: str, n: int = 1):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        ball = (ball or '').lower()
        if ball not in BALLS:
            return await ctx.reply(t(gid, 'eco.pk_balls', have=self._balls_line(gid, ctx.author.id)),
                                   ephemeral=True)
        n = max(1, min(99, n or 1))
        cost = BALLS[ball][0] * n
        b = bal(gid, ctx.author.id)
        if cost > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - cost)
        balls_add(gid, ctx.author.id, ball, n)
        await ctx.reply(t(gid, 'eco.pk_balls_bought', n=n, ball=ball), ephemeral=True)

    @pk.command(name='box', description='Twoje pokemony')
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

    @pk.command(name='info', description='Staty pokemona')
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

    @pk.command(name='active', description='Wybierz wojownika')
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

    @pk.command(name='nick', description='Przezwij pokemona')
    async def nick(self, ctx, slot: int, *, name: str = ''):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_mons SET nick=? WHERE id=?', (name[:24], m['id']))
        await ctx.reply(t(gid, 'eco.pk_nicked', name=(name[:24] or mon_name(m))), ephemeral=True)

    @pk.command(name='release', description='Wypuść pokemona')
    async def release(self, ctx, slot: int):
        gid = ctx.guild.id
        m = get_mon(gid, ctx.author.id, slot or 0)
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        name = mon_name(m)
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
            left = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                                (str(gid), str(ctx.author.id))).fetchone()
            if left:
                conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (left['id'],))
        await ctx.reply(t(gid, 'eco.pk_released', name=name), ephemeral=True)

    @pk.command(name='dex', description='Pokedex')
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
        wild['hp'] = e['hp']
        key = (str(gid), str(user.id))
        self._battle[key] = {'me': me, 'wild': wild, 'mid': act['id'], 'log': []}
        view = self._battle_view(gid, user.id, me, wild, [])
        if is_ix:
            await ix_or_ctx.followup.send(view=view)
        else:
            await ix_or_ctx.reply(view=view, mention_author=False)

    def _battle_view(self, gid, uid, me, wild, log):
        from discord.ui import ActionRow, Container, TextDisplay
        from discord.ui import LayoutView
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0xFF4655)
        body = (f"## {me['name']} Lv{me['level']} ({me['hp']}/{me['stats']['maxhp']}) "
                f"vs {wild['name']} Lv{wild['level']} ({wild['hp']}/{wild['stats']['maxhp']})")
        if log:
            body += '\n' + '\n'.join(log[-4:])
        box.add_item(TextDisplay(body))
        row = ActionRow()
        for label, cid in (('ATTACK', 'pk_atk'), ('BALL', 'pk_ball'), ('RUN', 'pk_run')):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'{cid}:{uid}')
            b.callback = self._mk_battle_btn(gid, uid, cid)
            row.add_item(b)
        box.add_item(row)
        layout.add_item(box)
        return layout

    def _mk_battle_btn(self, gid, uid, what: str):
        async def _cb(ix: discord.Interaction):
            if ix.user.id != int(uid):
                return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
            await ix.response.defer()
            await self._battle_turn(ix, gid, ix.user, what)
        return _cb

    async def _battle_turn(self, ix: discord.Interaction, gid, user, what: str):
        import aiohttp
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
        # attack: faster strikes first
        async with aiohttp.ClientSession():
            pass
        order = [(me, wild, True), (wild, me, False)] if me['stats']['spe'] >= wild['stats']['spe'] \
            else [(wild, me, False), (me, wild, True)]
        for att, dfn, is_me in order:
            mv = random.choice(att['moves'])
            dmg = damage(att['level'], mv, att['stats'], dfn['stats'], att['types'], dfn['types'])
            dfn['hp'] = max(0, dfn['hp'] - dmg)
            eff = effectiveness(mv.get('ptype', 'normal'), dfn['types'])
            tag = ' 💥' if eff > 1 else (' 🛡' if eff < 1 else '')
            who = t(gid, 'eco.pk_you') if is_me else t(gid, 'eco.pk_foe')
            log.append(t(gid, 'eco.pk_hit', who=who, move=mv['name'], dmg=dmg) + tag)
            if dfn['hp'] <= 0:
                break
        if wild['hp'] <= 0:
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            gain = wild['level'] * 10
            msgs = [t(gid, 'eco.pk_ko', name=wild['name'], xp=gain)]
            ev = await self._gain_xp(gid, user.id, st['mid'], gain)
            msgs.extend(ev)
            return await ix.followup.send(view=self._layout(
                gid, t(gid, 'eco.pk_win_title'), '\n'.join(log[-4:] + msgs)))
        if me['hp'] <= 0:
            self._battle.pop(key, None)
            self._enc.pop(key, None)
            return await ix.followup.send(view=self._layout(
                gid, t(gid, 'eco.pk_lose_title'),
                '\n'.join(log[-4:] + [t(gid, 'eco.pk_blackout')])))
        await ix.followup.send(view=self._battle_view(gid, user.id, me, wild, log))

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

    @pk.command(name='battle', description='Walcz z dzikim')
    async def battle(self, ctx):
        await self._start_battle(ctx, ctx.guild.id, ctx.author)

    # ----- PvP duels -----

    @pk.command(name='duel', description='Pojedynek trenerów')
    async def duel(self, ctx, member: discord.Member, wager: int = 0):
        import aiohttp
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pk_duel_self'), ephemeral=True)
        me_mons = my_mons(gid, ctx.author.id)
        fo_mons = my_mons(gid, member.id)
        me = next((m for m in me_mons if m['active']), None)
        fo = next((m for m in fo_mons if m['active']), None)
        if not me or not fo:
            return await ctx.reply(t(gid, 'eco.pk_duel_need'), ephemeral=True)
        wager = max(0, wager or 0)
        if wager:
            if bal(gid, ctx.author.id)['cash'] < wager or bal(gid, member.id)['cash'] < wager:
                return await ctx.reply(t(gid, 'eco.pk_duel_cash'), ephemeral=True)
        await ctx.typing()
        async with aiohttp.ClientSession() as s:
            a = await self._fighter(s, me)
            b = await self._fighter(s, fo)
        log = [t(gid, 'eco.pk_duel_start', a=a['name'], b=b['name'])]
        first, second = (a, b) if a['stats']['spe'] >= b['stats']['spe'] else (b, a)
        winner = None
        for _ in range(60):
            for att, dfn in ((first, second), (second, first)):
                mv = random.choice(att['moves'])
                dmg = damage(att['level'], mv, att['stats'], dfn['stats'], att['types'], dfn['types'])
                dfn['hp'] = max(0, dfn['hp'] - dmg)
                if len(log) < 9:
                    log.append(t(gid, 'eco.pk_hit', who=att['name'], move=mv['name'], dmg=dmg))
                if dfn['hp'] <= 0:
                    winner = att
                    break
            if winner:
                break
        if winner is None:
            winner = a if a['hp'] >= b['hp'] else b
        won_me = winner is a
        gain = b['level'] * 12 if won_me else a['level'] * 12
        ev = await self._gain_xp(gid, (ctx.author.id if won_me else member.id),
                                (me if won_me else fo)['id'], gain)
        line = t(gid, 'eco.pk_duel_win',
                 name=ctx.author.display_name if won_me else member.display_name,
                 xp=gain)
        if wager:
            w = bal(gid, ctx.author.id)
            v = bal(gid, member.id)
            if won_me:
                set_cash(gid, ctx.author.id, w['cash'] + wager)
                set_cash(gid, member.id, max(0, v['cash'] - wager))
            else:
                set_cash(gid, member.id, v['cash'] + wager)
                set_cash(gid, ctx.author.id, max(0, w['cash'] - wager))
            line += '\n' + t(gid, 'eco.pk_duel_wager', win=cshort(wager))
        await ctx.reply(view=self._layout(gid, t(gid, 'eco.pk_duel_title'),
                                          '\n'.join(log + ev + [line])), mention_author=False)

    # ----- trading -----

    @pk.command(name='trade', description='Wymień pokemona')
    async def trade(self, ctx, member: discord.Member, yours: int, theirs: int):
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pk_duel_self'), ephemeral=True)
        mine = get_mon(gid, ctx.author.id, yours or 0)
        want = get_mon(gid, member.id, theirs or 0)
        if not mine or not want:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
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

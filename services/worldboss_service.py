"""World boss service (first new system after the services layer).

Discord-free: no ctx, no interactions, no views. One boss, one active
event per guild. Attacks resolve through services.battle_service (no
duplicated damage math); the boss only provides combat state,
contribution policy and reward settlement.

Shared persistent state in two minimal tables (created on demand, no
init_db change). Revision-guarded HP writes + claim-guarded rewards,
so concurrent attackers and double-claims settle exactly once.
"""
import time

import database as db
from lang import t

ATTACK_CD = 60         # seconds between attacks per user
MIN_DAMAGE = 50        # reward threshold
REWARD_BASE = 1000     # participation payout
REWARD_TOP = 5000      # top-contributor bonus
DEFAULT_DURATION = 3600

BOSS = {
    'key': 'dreadmaw',
    'name': 'Dreadmaw',
    'dex': 248,
    'level': 70,
    'types': ['rock', 'dark'],
    'stats': {'atk': 120, 'spa': 100, 'dfn': 110, 'spd': 90, 'spe': 30,
              'maxhp': 10 ** 9},
    'moves': [{'name': 'Crunch', 'power': 80, 'acc': 100, 'ptype': 'dark'},
              {'name': 'Stone Edge', 'power': 100, 'acc': 80, 'ptype': 'rock'}],
    'max_hp': 5000,
    'duration': DEFAULT_DURATION,
}

BOSSES = {BOSS['key']: BOSS}


def ensure_tables() -> None:
    with db.conn_ctx() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS world_boss (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            boss_key TEXT NOT NULL, max_hp INTEGER NOT NULL,
            hp INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
            revision INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            defeated_at INTEGER DEFAULT 0)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS world_boss_parts (
            boss_id INTEGER NOT NULL, guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL, damage INTEGER DEFAULT 0,
            attacks INTEGER DEFAULT 0, joined_at INTEGER DEFAULT 0,
            last_action_at INTEGER DEFAULT 0, reward_claimed INTEGER DEFAULT 0,
            PRIMARY KEY (boss_id, user_id))''')


def _sweep(conn, gid, now) -> None:
    """Mark overdue ACTIVE bosses EXPIRED. Guarded; harmless if already done."""
    conn.execute("UPDATE world_boss SET status='EXPIRED' WHERE guild_id=? "
                 "AND status='ACTIVE' AND expires_at<=?", (str(gid), now))


def _active_row(conn, gid):
    return conn.execute("SELECT * FROM world_boss WHERE guild_id=? AND status='ACTIVE' "
                        "ORDER BY id DESC LIMIT 1", (str(gid),)).fetchone()


def start_boss(gid, boss_key: str, now: int = None, max_hp: int = None,
               duration: int = None) -> dict:
    """Staff: open a boss event. One active per guild."""
    ensure_tables()
    now = int(now if now is not None else time.time())
    cfg = BOSSES.get((boss_key or '').lower())
    if not cfg:
        return {'ok': False, 'code': 'UNKNOWN_BOSS',
                'message': t(gid, 'eco.wb_unknown')}
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        if _active_row(conn, gid):
            return {'ok': False, 'code': 'ALREADY_ACTIVE',
                    'message': t(gid, 'eco.wb_active')}
        hp = int(max_hp or cfg['max_hp'])
        dur = int(duration or cfg['duration'])
        cur = conn.execute(
            'INSERT INTO world_boss (guild_id, boss_key, max_hp, hp, status, '
            'revision, started_at, expires_at) VALUES (?,?,?,?,?,?,?,?)',
            (str(gid), cfg['key'], hp, hp, 'ACTIVE', 0, now, now + dur))
        bid = cur.lastrowid
    return {'ok': True, 'code': 'BOSS_STARTED',
            'message': t(gid, 'eco.wb_started', name=cfg['name'], hp=hp),
            'boss_id': bid, 'boss_key': cfg['key'], 'max_hp': hp}


def boss_status(gid, now: int = None) -> dict | None:
    """View data for the status card, or None when no live event."""
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return None
        b = dict(row)
        parts = [dict(r) for r in conn.execute(
            'SELECT user_id, damage, attacks FROM world_boss_parts '
            'WHERE boss_id=? ORDER BY damage DESC', (b['id'],)).fetchall()]
    top = [{'user_id': p['user_id'], 'damage': p['damage'],
            'attacks': p['attacks']} for p in parts[:3]]
    return {'boss_id': b['id'], 'boss_key': b['boss_key'],
            'name': BOSSES.get(b['boss_key'], {}).get('name', b['boss_key']),
            'hp': b['hp'], 'max_hp': b['max_hp'], 'revision': b['revision'],
            'ends_in': max(0, b['expires_at'] - now),
            'participants': len(parts), 'top': top,
            'by_user': {p['user_id']: p for p in parts}}


def join_boss(gid, uid, now: int = None) -> dict:
    """Explicit join (attacks auto-join too). Idempotent."""
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        conn.execute('INSERT OR IGNORE INTO world_boss_parts '
                     '(boss_id, guild_id, user_id, joined_at) VALUES (?,?,?,?)',
                     (b['id'], str(gid), str(uid), now))
    return {'ok': True, 'code': 'JOINED',
            'message': t(gid, 'eco.wb_joined',
                         name=BOSSES.get(b['boss_key'], {}).get('name', b['boss_key']))}


def _player_fighter(gid, uid):
    """Lightweight combatant from the active mon. No network: STAB strike
    derived from species types, so the flow stays offline-capable."""
    from cogs.pokemon import _dex_row, calc_stats, my_mons
    mons = my_mons(gid, uid)
    if not mons:
        return None
    act = next((m for m in mons if m.get('active')), mons[0])
    row = _dex_row(act['dex'])
    stats = calc_stats(row, act['level'])
    types = list(row.get('types') or ['normal']) or ['normal']
    ptype = types[0]
    return {'name': f"Lv{act['level']} {(row.get('name') or 'mon').capitalize()}",
            'level': act['level'], 'hp': stats['maxhp'], 'stats': stats,
            'types': types, 'held': '',
            'moves': [{'name': f'{ptype.capitalize()} Strike', 'power': 60,
                       'acc': 100, 'ptype': ptype},
                      {'name': 'Tackle', 'power': 40, 'acc': 100,
                       'ptype': 'normal'}]}


def _boss_fighter(hp: int) -> dict:
    return {'name': BOSS['name'], 'level': BOSS['level'], 'hp': hp,
            'stats': dict(BOSS['stats']), 'types': list(BOSS['types']),
            'held': '', 'moves': [dict(m) for m in BOSS['moves']]}


def attack_boss(gid, uid, now: int = None, rng=None) -> dict:
    """One attack: cooldown, resolve via battle_service, guarded HP write,
    contribution. Loser of a revision race gets STALE (retry)."""
    from services import battle_service as _bt
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        conn.execute('INSERT OR IGNORE INTO world_boss_parts '
                     '(boss_id, guild_id, user_id, joined_at) VALUES (?,?,?,?)',
                     (b['id'], str(gid), str(uid), now))
        part = conn.execute('SELECT attacks, damage, last_action_at FROM world_boss_parts '
                            'WHERE boss_id=? AND user_id=?',
                            (b['id'], str(uid))).fetchone()
        part = dict(part)
        wait = ATTACK_CD - (now - (part['last_action_at'] or 0))
        if wait > 0:
            return {'ok': False, 'code': 'COOLDOWN',
                    'message': t(gid, 'eco.wb_cooldown', s=wait)}
    me = _player_fighter(gid, uid)
    if not me:
        return {'ok': False, 'code': 'NO_MON',
                'message': t(gid, 'eco.pk_need_starter')}
    st = {'me': me, 'wild': _boss_fighter(b['hp']), 'log': [], 'weather': None}
    _bt.resolve_turn(st, gid, 0, rng)
    dealt = max(0, b['hp'] - st['wild']['hp'])
    with db.conn_ctx() as conn:
        cur = conn.execute('UPDATE world_boss SET hp=max(0, hp-?), revision=revision+1 '
                           'WHERE id=? AND status=\'ACTIVE\' AND revision=?',
                           (dealt, b['id'], b['revision']))
        if (cur.rowcount or 0) != 1:
            return {'ok': False, 'code': 'STALE',
                    'message': t(gid, 'eco.wb_stale')}
        conn.execute('UPDATE world_boss_parts SET damage=damage+?, attacks=attacks+1, '
                     'last_action_at=? WHERE boss_id=? AND user_id=?',
                     (dealt, now, b['id'], str(uid)))
        left = conn.execute('SELECT hp, max_hp FROM world_boss WHERE id=?',
                            (b['id'],)).fetchone()
        left = dict(left)
        defeated = left['hp'] <= 0
        if defeated:
            conn.execute('UPDATE world_boss SET status=\'COMPLETED\', defeated_at=? '
                         'WHERE id=? AND status=\'ACTIVE\'', (now, b['id']))
    if defeated:
        return {'ok': True, 'code': 'DEFEATED', 'damage': dealt,
                'boss_hp': 0, 'max_hp': left['max_hp'],
                'message': t(gid, 'eco.wb_defeated', dmg=dealt)}
    return {'ok': True, 'code': 'ATTACK_OK', 'damage': dealt,
            'boss_hp': left['hp'], 'max_hp': left['max_hp'],
            'message': t(gid, 'eco.wb_hit', dmg=dealt, hp=left['hp'],
                         max_hp=left['max_hp'])}


def claim_rewards(gid, uid, now: int = None) -> dict:
    """Idempotent reward claim: base participation + top-contributor bonus.
    Claim guard pays exactly once; thresholds enforced."""
    from services._common import credit_cash
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = conn.execute('SELECT * FROM world_boss WHERE guild_id=? AND status=\'COMPLETED\' '
                           'ORDER BY id DESC LIMIT 1', (str(gid),)).fetchone()
        if not row:
            return {'ok': False, 'code': 'NO_REWARD',
                    'message': t(gid, 'eco.wb_no_reward')}
        b = dict(row)
        part = conn.execute('SELECT damage, attacks, reward_claimed FROM world_boss_parts '
                            'WHERE boss_id=? AND user_id=?', (b['id'], str(uid))).fetchone()
        if not part or not (part['attacks'] or 0) or (part['damage'] or 0) < MIN_DAMAGE:
            return {'ok': False, 'code': 'NO_REWARD',
                    'message': t(gid, 'eco.wb_no_reward')}
        top = conn.execute('SELECT user_id FROM world_boss_parts WHERE boss_id=? '
                           'ORDER BY damage DESC LIMIT 1', (b['id'],)).fetchone()
        is_top = bool(top and str(top['user_id']) == str(uid))
        amount = REWARD_BASE + (REWARD_TOP if is_top else 0)
        cur = conn.execute('UPDATE world_boss_parts SET reward_claimed=1 '
                           'WHERE boss_id=? AND user_id=? AND reward_claimed=0',
                           (b['id'], str(uid)))
        if (cur.rowcount or 0) != 1:
            return {'ok': False, 'code': 'ALREADY_CLAIMED',
                    'message': t(gid, 'eco.wb_already')}
        credit_cash(conn, gid, uid, amount)
    return {'ok': True, 'code': 'REWARD_CLAIMED', 'amount': amount,
            'message': t(gid, 'eco.wb_claimed', amount=amount)}

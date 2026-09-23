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
HEAL_COST = 1000       # coin cost, once per raid per user
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
        conn.execute('''CREATE TABLE IF NOT EXISTS world_boss_combat (
            boss_id INTEGER NOT NULL, guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL, mid INTEGER NOT NULL,
            hp INTEGER NOT NULL, max_hp INTEGER NOT NULL,
            healed INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0,
            PRIMARY KEY (boss_id, user_id))''')


def _sweep(conn, gid, now) -> None:
    """Mark overdue ACTIVE bosses EXPIRED + drop their combat snapshots
    (parts stay for post-raid claims). Guarded; harmless if already done."""
    conn.execute("UPDATE world_boss SET status='EXPIRED' WHERE guild_id=? "
                 "AND status='ACTIVE' AND expires_at<=?", (str(gid), now))
    conn.execute('DELETE FROM world_boss_combat WHERE boss_id IN '
                 '(SELECT id FROM world_boss WHERE guild_id=? AND status<>?)',
                 (str(gid), 'ACTIVE'))


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


def _fighter_from_mon(gid, mon: dict, hp: int | None = None) -> dict:
    """Lightweight combatant from a collection row. Synthetic moveset
    (STAB strike + Tackle): no network, deterministic. Permanent row
    is never written — hp lives in the combat snapshot."""
    from cogs.pokemon import _dex_row, calc_stats
    row = _dex_row(mon['dex'])
    stats = calc_stats(row, mon['level'])
    types = list(row.get('types') or ['normal']) or ['normal']
    ptype = types[0]
    full = stats['maxhp']
    return {'name': f"Lv{mon['level']} {(row.get('name') or 'mon').capitalize()}",
            'level': mon['level'], 'hp': full if hp is None else hp,
            'stats': stats, 'types': types, 'held': '',
            'moves': [{'name': f'{ptype.capitalize()} Strike', 'power': 60,
                       'acc': 100, 'ptype': ptype},
                      {'name': 'Tackle', 'power': 40, 'acc': 100,
                       'ptype': 'normal'}], '_maxhp': full}


def _player_fighter(gid, uid):
    """Legacy full-HP builder (kept for tests/back-compat)."""
    from cogs.pokemon import my_mons
    mons = my_mons(gid, uid)
    if not mons:
        return None
    act = next((m for m in mons if m.get('active')), mons[0])
    return _fighter_from_mon(gid, act)


def _load_combat(conn, gid, uid, boss_id):
    """Snapshot row + live mon row. Rebuilds the snapshot when the pinned
    mon is gone (traded/released mid-raid); None when the box is empty."""
    from cogs.pokemon import my_mons
    snap = conn.execute('SELECT * FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                        (boss_id, str(uid))).fetchone()
    snap = dict(snap) if snap else None
    mons = my_mons(gid, uid)
    if not mons:
        return None, None
    live = next((m for m in mons if str(m['id']) == str((snap or {}).get('mid'))), None)
    if snap and not live:
        conn.execute('DELETE FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                     (boss_id, str(uid)))
        snap = None
    if not snap:
        act = next((m for m in mons if m.get('active')), mons[0])
        full = _fighter_from_mon(gid, act)['_maxhp']
        conn.execute('INSERT OR REPLACE INTO world_boss_combat '
                     '(boss_id, guild_id, user_id, mid, hp, max_hp, healed, updated_at) '
                     'VALUES (?,?,?,?,?,?,?,?)',
                     (boss_id, str(gid), str(uid), act['id'], full, full, 0,
                      int(time.time())))
        snap = {'mid': act['id'], 'hp': full, 'max_hp': full, 'healed': 0}
        live = act
    return snap, live


def _boss_fighter(hp: int) -> dict:
    return {'name': BOSS['name'], 'level': BOSS['level'], 'hp': hp,
            'stats': dict(BOSS['stats']), 'types': list(BOSS['types']),
            'held': '', 'moves': [dict(m) for m in BOSS['moves']]}


def attack_boss(gid, uid, now: int = None, rng=None) -> dict:
    """One attack: cooldown, snapshot gate, resolve via battle_service,
    persist boss HP + snapshot HP + contribution in one guarded txn.
    Boss defeat skips retaliation (engine short-circuit); a fainted
    snapshot must switch before acting again."""
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
        snap, live = _load_combat(conn, gid, uid, b['id'])
        if not snap or not live:
            return {'ok': False, 'code': 'NO_MON',
                    'message': t(gid, 'eco.pk_need_starter')}
        if (snap['hp'] or 0) <= 0:
            from cogs.pokemon import mon_name
            return {'ok': False, 'code': 'FAINTED',
                    'message': t(gid, 'eco.wb_fainted', name=mon_name(live, gid))}
        me = _fighter_from_mon(gid, live, snap['hp'])
    st = {'me': me, 'wild': _boss_fighter(b['hp']), 'log': [], 'weather': None}
    _bt.resolve_turn(st, gid, 0, rng, strict_faint=True)
    dealt = max(0, b['hp'] - st['wild']['hp'])
    left_hp = max(0, st['me']['hp'])
    with db.conn_ctx() as conn:
        cur = conn.execute('UPDATE world_boss SET hp=max(0, hp-?), revision=revision+1 '
                           'WHERE id=? AND status=\'ACTIVE\' AND revision=?',
                           (dealt, b['id'], b['revision']))
        if (cur.rowcount or 0) != 1:
            return {'ok': False, 'code': 'STALE',
                    'message': t(gid, 'eco.wb_stale')}
        conn.execute('UPDATE world_boss_combat SET hp=?, updated_at=? '
                     'WHERE boss_id=? AND user_id=?',
                     (left_hp, now, b['id'], str(uid)))
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
    tail = t(gid, 'eco.wb_fainted', name=me['name']) if left_hp <= 0 \
        else t(gid, 'eco.wb_self_hp', name=me['name'], hp=left_hp, max_hp=me['_maxhp'])
    if defeated:
        return {'ok': True, 'code': 'DEFEATED', 'damage': dealt,
                'boss_hp': 0, 'max_hp': left['max_hp'],
                'message': t(gid, 'eco.wb_defeated', dmg=dealt) + '\n' + tail}
    return {'ok': True, 'code': 'ATTACK_OK', 'damage': dealt,
            'boss_hp': left['hp'], 'max_hp': left['max_hp'],
            'message': t(gid, 'eco.wb_hit', dmg=dealt, hp=left['hp'],
                         max_hp=left['max_hp']) + '\n' + tail}


def switch_mon(gid, uid, mon_id: int, now: int = None) -> dict:
    """Send out another mon (full snapshot HP). Same-mon switch is a
    no-op — otherwise it would be a free heal."""
    from cogs.pokemon import mon_name
    from services._common import get_owned_pokemon
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        mon = get_owned_pokemon(conn, gid, uid, mon_id)
        if not mon:
            return {'ok': False, 'code': 'NOT_FOUND',
                    'message': t(gid, 'eco.pk_noslot')}
        snap = conn.execute('SELECT mid FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                            (b['id'], str(uid))).fetchone()
        if snap and str(snap['mid']) == str(mon['id']):
            return {'ok': False, 'code': 'SAME_MON',
                    'message': t(gid, 'eco.wb_same', name=mon_name(mon, gid))}
        full = _fighter_from_mon(gid, mon)['_maxhp']
        conn.execute('INSERT OR REPLACE INTO world_boss_combat '
                     '(boss_id, guild_id, user_id, mid, hp, max_hp, healed, updated_at) '
                     'VALUES (?,?,?,?,?,?,COALESCE((SELECT healed FROM world_boss_combat '
                     'WHERE boss_id=? AND user_id=?),0),?)',
                     (b['id'], str(gid), str(uid), mon['id'], full, full,
                      b['id'], str(uid), now))
    return {'ok': True, 'code': 'SWITCHED',
            'message': t(gid, 'eco.wb_switched', name=mon_name(mon, gid))}


def heal_fighter(gid, uid, now: int = None) -> dict:
    """Top up the current snapshot to full: costs coins, once per raid,
    never on a fainted mon (switch instead)."""
    from services._common import cash_of, debit_cash
    from utils.cards import short as cshort
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        snap = conn.execute('SELECT hp, max_hp, healed FROM world_boss_combat '
                            'WHERE boss_id=? AND user_id=?', (b['id'], str(uid))).fetchone()
        if not snap:
            return {'ok': False, 'code': 'NO_MON',
                    'message': t(gid, 'eco.pk_need_starter')}
        snap = dict(snap)
        if (snap['hp'] or 0) <= 0:
            return {'ok': False, 'code': 'FAINTED',
                    'message': t(gid, 'eco.wb_fainted_switch')}
        if (snap['hp'] or 0) >= (snap['max_hp'] or 0):
            return {'ok': False, 'code': 'FULL_HP',
                    'message': t(gid, 'eco.wb_full')}
        if snap['healed']:
            return {'ok': False, 'code': 'HEAL_USED',
                    'message': t(gid, 'eco.wb_healed_used')}
        if cash_of(conn, gid, uid) < HEAL_COST:
            return {'ok': False, 'code': 'INSUFFICIENT_FUNDS',
                    'message': t(gid, 'eco.broke',
                                 cash=cshort(cash_of(conn, gid, uid)))}
        debit_cash(conn, gid, uid, HEAL_COST)
        conn.execute('UPDATE world_boss_combat SET hp=max_hp, healed=1, updated_at=? '
                     'WHERE boss_id=? AND user_id=?', (now, b['id'], str(uid)))
    return {'ok': True, 'code': 'HEALED',
            'message': t(gid, 'eco.wb_healed', cost=HEAL_COST)}


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

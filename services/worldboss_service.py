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
from game.bosses.models import (BossDefinition, definition_from_snapshot,
                                snapshot_definition)
from game.bosses.registry import registry
from lang import t

HEAL_COST = 1000       # coin cost, once per raid per user (format-wide)


def resolve_boss_phase(phases, hp: int, max_hp: int):
    """Phase from HP ratio over the DEFINITION's phases. Exactly-at-
    threshold counts as the lower phase. Pure + deterministic."""
    ratio = (hp or 0) / max(1, max_hp or 1)
    for phase in phases:
        if ratio > phase.min_hp_ratio:
            return phase
    return phases[-1]


def _definition(row) -> BossDefinition:
    """Live snapshot from the event row (back-compat: pre-snapshot rows
    resolve from the registry). Never silently substitutes another boss."""
    raw = row['config_json'] if 'config_json' in row.keys() else ''
    if raw:
        import json as _json
        return definition_from_snapshot(_json.loads(raw))
    defn = registry.get(row['boss_key'])
    if defn is None:
        raise RuntimeError(f"unknown boss {row['boss_key']!r}")
    return defn


def ensure_tables() -> None:
    with db.conn_ctx() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS world_boss (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            boss_key TEXT NOT NULL, max_hp INTEGER NOT NULL,
            hp INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
            revision INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            defeated_at INTEGER DEFAULT 0, config_json TEXT DEFAULT '',
            max_phase TEXT DEFAULT '')''')
        try:
            conn.execute('ALTER TABLE world_boss ADD COLUMN config_json TEXT DEFAULT \'\'')
        except Exception:
            pass  # already migrated
        try:
            conn.execute('ALTER TABLE world_boss ADD COLUMN max_phase TEXT DEFAULT \'\'')
        except Exception:
            pass  # already migrated
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
        conn.execute('''CREATE TABLE IF NOT EXISTS world_boss_rewards (
            boss_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            reward_json TEXT NOT NULL, claimed_at INTEGER NOT NULL,
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
    """Staff: open a boss event. One active per guild. The definition is
    snapshotted onto the row: later balance edits affect future events."""
    import dataclasses as _dc
    ensure_tables()
    now = int(now if now is not None else time.time())
    cfg = registry.get(boss_key)
    if cfg is None:
        return {'ok': False, 'code': 'UNKNOWN_BOSS',
                'message': t(gid, 'eco.wb_unknown')}
    if not cfg.enabled:
        return {'ok': False, 'code': 'DISABLED',
                'message': t(gid, 'eco.wb_disabled')}
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        if _active_row(conn, gid):
            return {'ok': False, 'code': 'ALREADY_ACTIVE',
                    'message': t(gid, 'eco.wb_active')}
        hp = int(max_hp or cfg.max_hp)
        dur = int(duration or cfg.duration_seconds)
        snap = snapshot_definition(_dc.replace(
            cfg, max_hp=hp, duration_seconds=dur))
        cur = conn.execute(
            'INSERT INTO world_boss (guild_id, boss_key, max_hp, hp, status, '
            'revision, started_at, expires_at, config_json) VALUES (?,?,?,?,?,?,?,?,?)',
            (str(gid), cfg.key, hp, hp, 'ACTIVE', 0, now, now + dur, snap))
        bid = cur.lastrowid
    return {'ok': True, 'code': 'BOSS_STARTED',
            'message': t(gid, 'eco.wb_started', name=cfg.display_name, hp=hp),
            'boss_id': bid, 'boss_key': cfg.key, 'max_hp': hp}


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
    defn = _definition(b)
    phase = resolve_boss_phase(defn.phases, b['hp'], b['max_hp'])
    reached = (b.get('max_phase') or '')
    reached_name = next((p.display_name for p in defn.phases if p.key == reached), None)
    return {'boss_id': b['id'], 'boss_key': b['boss_key'],
            'name': defn.display_name,
            'hp': b['hp'], 'max_hp': b['max_hp'], 'revision': b['revision'],
            'phase': phase.key, 'phase_name': phase.display_name,
            'weather': _phase_weather(phase),
            'max_phase': reached or None, 'max_phase_name': reached_name,
            'reward_threshold': defn.participation_damage,
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
                         name=_definition(b).display_name)}


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


def _phase_weather(phase) -> str | None:
    """Effect resolver: only engine-known weather passes through.
    The service never interprets what rain/sun DO — that's the engine's
    damage math. Unknown/future keys resolve to no weather."""
    from game.bosses.models import KNOWN_PHASE_EFFECTS
    key = (phase.effect_key or '') if phase else ''
    return key if key in KNOWN_PHASE_EFFECTS else None


def _boss_fighter(defn, hp: int, phase=None) -> dict:
    phase = phase or defn.phases[0]
    return {'name': defn.display_name, 'level': defn.level, 'hp': hp,
            'stats': dict(defn.base_stats), 'types': list(defn.types),
            'held': '', 'moves': [dict(m) for m in phase.moves]}


def scale_incoming(phase, raw: int) -> int:
    """Phase hide: incoming strike damage after the engine resolved it.
    Retaliation never passes through here (damage_mult is separate).
    A non-zero strike always lands at least 1 — without the floor a
    0.85 hide could never finish a 1-HP boss (int(1*0.85) == 0)."""
    try:
        mult = float((phase or {}).get('defense_mult', 1.0)
                     if isinstance(phase, dict)
                     else getattr(phase, 'defense_mult', 1.0))
    except Exception:
        mult = 1.0
    if not (mult > 0):
        mult = 1.0
    raw = int(raw or 0)
    if raw <= 0:
        return 0
    return max(1, int(raw * mult))


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
        defn = _definition(b)
        wait = defn.attack_cooldown_seconds - (now - (part['last_action_at'] or 0))
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
        # Phase from PRE-attack HP for the whole turn: the shared engine
        # resolves atomically, so a mid-turn pool swap would fork turn order
        # into the service (or push boss concepts into the engine). The
        # crossing hit still announces + commits in the same txn below.
        phase = resolve_boss_phase(defn.phases, b['hp'], b['max_hp'])
        weather = _phase_weather(phase)
    st = {'me': me, 'wild': _boss_fighter(defn, b['hp'], phase), 'log': [],
          'weather': weather}
    _bt.resolve_turn(st, gid, 0, rng, strict_faint=True, foe_mult=phase.damage_mult)
    raw = max(0, b['hp'] - st['wild']['hp'])
    dealt = scale_incoming(phase, raw)
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
        # fought-phase memory: only when the boss SURVIVED into the phase.
        # A one-shot from full HP never fought it — no bonus. Same txn.
        new_phase = resolve_boss_phase(defn.phases, left['hp'], left['max_hp'])
        if left['hp'] > 0 and new_phase.key != defn.phases[0].key:
            conn.execute('UPDATE world_boss SET max_phase=? WHERE id=?',
                         (new_phase.key, b['id']))
    tail = t(gid, 'eco.wb_fainted', name=me['name']) if left_hp <= 0 \
        else t(gid, 'eco.wb_self_hp', name=me['name'], hp=left_hp, max_hp=me['_maxhp'])
    crossed = (new_phase.key != phase.key and (new_phase.display_name or ''))
    ping = ('\n' + t(gid, 'eco.wb_phase', name=defn.display_name,
                     phase=new_phase.display_name)) if crossed else ''
    if defeated:
        return {'ok': True, 'code': 'DEFEATED', 'damage': dealt,
                'boss_hp': 0, 'max_hp': left['max_hp'],
                'message': t(gid, 'eco.wb_defeated', dmg=dealt) + ping + '\n' + tail}
    return {'ok': True, 'code': 'ATTACK_OK', 'damage': dealt,
            'boss_hp': left['hp'], 'max_hp': left['max_hp'],
            'message': t(gid, 'eco.wb_hit', dmg=dealt, hp=left['hp'],
                         max_hp=left['max_hp']) + ping + '\n' + tail}


def expire_event(gid, now: int = None) -> dict:
    """Scheduler hook: expire one overdue ACTIVE event, sweep its combat
    snapshots, keep parts/claims. Guarded: second call is a NOOP."""
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        row = conn.execute("SELECT id FROM world_boss WHERE guild_id=? AND status='ACTIVE' "
                           "AND expires_at<=? ORDER BY id DESC LIMIT 1",
                           (str(gid), now)).fetchone()
        if not row:
            return {'ok': False, 'code': 'NOOP'}
        cur = conn.execute("UPDATE world_boss SET status='EXPIRED' WHERE id=? AND status='ACTIVE'",
                           (row['id'],))
        if (cur.rowcount or 0) != 1:
            return {'ok': False, 'code': 'NOOP'}
        conn.execute('DELETE FROM world_boss_combat WHERE boss_id=?', (row['id'],))
    return {'ok': True, 'code': 'EXPIRED', 'boss_id': row['id']}


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


def heal_fighter(gid, uid, now: int = None, use_charm: bool = False) -> dict:
    """Top up the current snapshot to full: costs coins, once per raid,
    never on a fainted mon (switch instead). With use_charm, a
    volcanic_charm is consumed instead of coins — same once-per-raid
    budget, same guards (checked before anything is taken)."""
    from services._common import (InsufficientItemError, cash_of, debit_cash,
                                  remove_item)
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
        if use_charm:
            try:
                remove_item(conn, gid, uid, 'volcanic_charm', 1)
            except InsufficientItemError:
                return {'ok': False, 'code': 'NO_CHARM',
                        'message': t(gid, 'eco.wb_no_charm', item='volcanic_charm')}
        else:
            if cash_of(conn, gid, uid) < HEAL_COST:
                return {'ok': False, 'code': 'INSUFFICIENT_FUNDS',
                        'message': t(gid, 'eco.broke',
                                     cash=cshort(cash_of(conn, gid, uid)))}
            debit_cash(conn, gid, uid, HEAL_COST)
        conn.execute('UPDATE world_boss_combat SET hp=max_hp, healed=1, updated_at=? '
                     'WHERE boss_id=? AND user_id=?', (now, b['id'], str(uid)))
    if use_charm:
        return {'ok': True, 'code': 'HEALED', 'charm': True,
                'message': t(gid, 'eco.wb_healed_charm')}
    return {'ok': True, 'code': 'HEALED',
            'message': t(gid, 'eco.wb_healed', cost=HEAL_COST)}


def reset_cooldown(gid, uid, now: int = None) -> dict:
    """Consume 1 tidal_charm to end the attack cooldown now. Guards run
    before the charm is taken: nothing to clear means the charm is kept."""
    from services._common import InsufficientItemError, remove_item
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        defn = _definition(b)
        part = conn.execute('SELECT attacks, last_action_at FROM world_boss_parts '
                            'WHERE boss_id=? AND user_id=?',
                            (b['id'], str(uid))).fetchone()
        if not part or not (part['attacks'] or 0):
            return {'ok': False, 'code': 'NO_PARTICIPATION',
                    'message': t(gid, 'eco.wb_use_noentry')}
        elapsed = now - int(part['last_action_at'] or 0)
        if elapsed >= defn.attack_cooldown_seconds:
            return {'ok': False, 'code': 'COOLDOWN_READY',
                    'message': t(gid, 'eco.wb_cleared_ready')}
        snap = conn.execute('SELECT hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                            (b['id'], str(uid))).fetchone()
        if snap and (snap['hp'] or 0) <= 0:
            return {'ok': False, 'code': 'FAINTED',
                    'message': t(gid, 'eco.wb_fainted_switch')}
        try:
            remove_item(conn, gid, uid, 'tidal_charm', 1)
        except InsufficientItemError:
            return {'ok': False, 'code': 'NO_CHARM',
                    'message': t(gid, 'eco.wb_no_charm', item='tidal_charm')}
        conn.execute('UPDATE world_boss_parts SET last_action_at=0 '
                     'WHERE boss_id=? AND user_id=?', (b['id'], str(uid)))
    return {'ok': True, 'code': 'CLEARED',
            'message': t(gid, 'eco.wb_cleared')}


def use_raid_charm(gid, uid, now: int = None) -> dict:
    """Consume 1 raid_charm: full heal AND cooldown clear at once. Shares
    the once-per-raid heal budget with coin/charm heals — no double dip."""
    from services._common import InsufficientItemError, remove_item
    ensure_tables()
    now = int(now if now is not None else time.time())
    with db.conn_ctx() as conn:
        _sweep(conn, gid, now)
        row = _active_row(conn, gid)
        if not row:
            return {'ok': False, 'code': 'NO_BOSS', 'message': t(gid, 'eco.wb_no_boss')}
        b = dict(row)
        defn = _definition(b)
        snap = conn.execute('SELECT hp, max_hp, healed FROM world_boss_combat '
                            'WHERE boss_id=? AND user_id=?', (b['id'], str(uid))).fetchone()
        if not snap:
            return {'ok': False, 'code': 'NO_MON',
                    'message': t(gid, 'eco.wb_use_noentry')}
        snap = dict(snap)
        if (snap['hp'] or 0) <= 0:
            return {'ok': False, 'code': 'FAINTED',
                    'message': t(gid, 'eco.wb_fainted_switch')}
        if snap['healed']:
            return {'ok': False, 'code': 'HEAL_USED',
                    'message': t(gid, 'eco.wb_healed_used')}
        part = conn.execute('SELECT last_action_at FROM world_boss_parts '
                            'WHERE boss_id=? AND user_id=?',
                            (b['id'], str(uid))).fetchone()
        cd_ready = True
        if part and (part['last_action_at'] or 0):
            cd_ready = (now - int(part['last_action_at'])) >= defn.attack_cooldown_seconds
        if (snap['hp'] or 0) >= (snap['max_hp'] or 0) and cd_ready:
            return {'ok': False, 'code': 'NOTHING_TO_RESTORE',
                    'message': t(gid, 'eco.wb_rallied_idle')}
        try:
            remove_item(conn, gid, uid, 'raid_charm', 1)
        except InsufficientItemError:
            return {'ok': False, 'code': 'NO_CHARM',
                    'message': t(gid, 'eco.wb_no_charm', item='raid_charm')}
        conn.execute('UPDATE world_boss_combat SET hp=max_hp, healed=1, updated_at=? '
                     'WHERE boss_id=? AND user_id=?', (now, b['id'], str(uid)))
        conn.execute('UPDATE world_boss_parts SET last_action_at=0 '
                     'WHERE boss_id=? AND user_id=?', (b['id'], str(uid)))
    return {'ok': True, 'code': 'RALLIED',
            'message': t(gid, 'eco.wb_rallied')}


def roll_boss_loot(table, damage: int, is_top: bool,
                   rng=None) -> list:
    """Deterministic weighted rolls: 1 base + 1 for top contributor.
    Entries below their min_damage are ineligible. Returns [(item, qty)]."""
    import random as _rr
    rng = rng or _rr
    eligible = [e for e in table.entries if damage >= e.min_damage and e.weight > 0]
    if not eligible:
        return []
    out = []
    for _ in range(table.rolls + (1 if is_top else 0)):
        total = sum(e.weight for e in eligible)
        pick = rng.random() * total
        acc = 0
        chosen = eligible[-1]
        for e in eligible:
            acc += e.weight
            if pick < acc:
                chosen = e
                break
        span = max(0, chosen.quantity_max - chosen.quantity_min)
        qty = chosen.quantity_min + (int(rng.random() * (span + 1)) if span else 0)
        out.append((chosen.item_id, max(1, qty)))
    return out


def claim_rewards(gid, uid, now: int = None, rng=None) -> dict:
    """Idempotent reward claim: coins (unchanged math) + rolled loot.
    Claim guard first; the rolled outcome is persisted before commit so a
    retry can never reroll. One transaction throughout."""
    import json as _json
    import random as _rr
    from services._common import credit_cash, upsert_item
    rng = rng or _rr
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
        defn = _definition(b)
        part = conn.execute('SELECT damage, attacks, reward_claimed FROM world_boss_parts '
                            'WHERE boss_id=? AND user_id=?', (b['id'], str(uid))).fetchone()
        if not part or not (part['attacks'] or 0) \
                or (part['damage'] or 0) < defn.participation_damage:
            return {'ok': False, 'code': 'NO_REWARD',
                    'message': t(gid, 'eco.wb_no_reward')}
        top = conn.execute('SELECT user_id FROM world_boss_parts WHERE boss_id=? '
                           'ORDER BY damage DESC LIMIT 1', (b['id'],)).fetchone()
        is_top = bool(top and str(top['user_id']) == str(uid))
        amount = defn.participation_coins + (defn.top_bonus_coins if is_top else 0)
        cur = conn.execute('UPDATE world_boss_parts SET reward_claimed=1 '
                           'WHERE boss_id=? AND user_id=? AND reward_claimed=0',
                           (b['id'], str(uid)))
        if (cur.rowcount or 0) != 1:
            return {'ok': False, 'code': 'ALREADY_CLAIMED',
                    'message': t(gid, 'eco.wb_already')}
        table = defn.loot_table
        loot = roll_boss_loot(table, part['damage'], is_top, rng) if table else []
        # phase bonus: the raid must have FOUGHT the phase (recorded on
        # the event); one-shots from full HP leave max_phase empty.
        phase_bonus = None
        reached = (b.get('max_phase') or '')
        if reached:
            hit = next((p for p in defn.phases if p.key == reached), None)
            if hit is not None and hit.phase_reward is not None:
                phase_bonus = (hit.phase_reward.item_id,
                               max(1, hit.phase_reward.quantity_min))
        items = [[i, q] for i, q in loot]
        if phase_bonus is not None:
            items.append([phase_bonus[0], phase_bonus[1]])
        reward = {'coins': amount, 'items': items,
                  'phase': reached or None}
        conn.execute('INSERT OR REPLACE INTO world_boss_rewards '
                     '(boss_id, user_id, reward_json, claimed_at) VALUES (?,?,?,?)',
                     (b['id'], str(uid), _json.dumps(reward), now))
        credit_cash(conn, gid, uid, amount)
        for item_id, qty in items:
            upsert_item(conn, gid, uid, item_id, qty)
    lines = ''.join(f'\n+{q}x {i}' for i, q in loot)
    if phase_bonus is not None:
        lines += '\n' + t(gid, 'eco.wb_phase_loot', qty=phase_bonus[1],
                          item=phase_bonus[0])
    return {'ok': True, 'code': 'REWARD_CLAIMED', 'amount': amount,
            'loot': loot, 'phase_loot': list(phase_bonus) if phase_bonus else None,
            'message': t(gid, 'eco.wb_claimed', amount=amount) + lines}

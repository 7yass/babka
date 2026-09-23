"""World-boss history: read-only, Discord-free reporting over the live
service's durable rows.

Durability contract (verified, not assumed):
  world_boss         kept forever (ACTIVE/COMPLETED/EXPIRED + snapshot)
  world_boss_parts   kept after expiry/defeat (damage, attacks, claims)
  world_boss_rewards kept (persisted outcome JSON per claim)
  world_boss_combat  swept on finish — history NEVER reads it.

Read-only means: SELECTs (+ meta_get) only. No INSERT/UPDATE/DELETE,
no DDL, no scheduler/service calls. A failed lookup returns None/empty
and changes nothing.
"""
from dataclasses import dataclass, field

import database as db

HISTORY_PAGE_SIZE = 5
LEADERBOARD_PAGE_SIZE = 10
TOP_SHOWN = 5

VALID_METRICS = ('damage', 'wins', 'participation')


@dataclass(frozen=True)
class TopEntry:
    user_id: str
    damage: int
    attacks: int


@dataclass(frozen=True)
class WorldBossHistory:
    event_id: int
    guild_id: str
    boss_key: str
    name: str
    status: str            # ACTIVE / COMPLETED / EXPIRED (raw)
    outcome: str           # active / defeated / expired
    started_at: int
    end_at: int | None     # None while active
    duration_s: int | None
    max_hp: int
    hp: int
    participants: int
    total_attacks: int
    total_damage: int
    top: tuple = ()
    claims: int = 0
    loot_guaranteed: tuple = ()   # ((item, qty)) — phase bonus only
    loot_weighted: tuple = ()     # ((item, qty)) — table rolls only
    loot_total: tuple = ()        # ((item, qty)) — delivered sum
    phase_reached: str | None = None
    phase_name: str | None = None
    scheduler_note: str | None = None


@dataclass(frozen=True)
class LeaderboardEntry:
    rank: int
    user_id: str
    value: int
    extra: int


@dataclass(frozen=True)
class LeaderboardPage:
    metric: str
    page: int
    per_page: int
    total_users: int
    total_pages: int
    entries: tuple = ()


@dataclass(frozen=True)
class WorldBossStats:
    total: int = 0
    active: int = 0
    defeated: int = 0
    expired: int = 0
    defeat_rate: float = 0.0
    avg_duration_s: int | None = None
    avg_participants: float = 0.0
    avg_damage_per_attack: float = 0.0
    total_claims: int = 0
    most_used_boss: str | None = None
    most_common_phase: str | None = None
    loot_guaranteed: tuple = ()
    loot_weighted: tuple = ()
    loot_total: tuple = ()


def _tables_present(conn) -> bool:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name IN ('world_boss','world_boss_parts','world_boss_rewards')").fetchall()
        return len({r['name'] for r in rows}) == 3
    except Exception:
        return False


def _display_name(boss_key: str, config_json: str) -> str:
    if config_json:
        try:
            import json as _json
            data = _json.loads(config_json)
            if data.get('display_name'):
                return data['display_name']
        except Exception:
            pass
    try:
        from game.bosses.registry import registry
        defn = registry.get(boss_key)
        if defn is not None:
            return defn.display_name
    except Exception:
        pass
    return boss_key or '?'


def _phase_name(boss_key: str, config_json: str, phase_key: str) -> str | None:
    if not phase_key:
        return None
    if config_json:
        try:
            import json as _json
            for p in (_json.loads(config_json).get('phases') or ()):
                if p.get('key') == phase_key:
                    return p.get('display_name') or phase_key
        except Exception:
            pass
    try:
        from game.bosses.registry import registry
        defn = registry.get(boss_key)
        if defn is not None:
            for p in defn.phases:
                if p.key == phase_key:
                    return p.display_name or phase_key
    except Exception:
        pass
    return phase_key


def _expected_bonus_item(boss_key: str, config_json: str,
                         phase_key: str) -> str | None:
    """Item id of the fought-phase bonus, or None. Snapshot first so a
    later balance edit can't rewrite old raids."""
    if not phase_key:
        return None
    if config_json:
        try:
            import json as _json
            for p in (_json.loads(config_json).get('phases') or ()):
                if p.get('key') == phase_key:
                    pr = p.get('phase_reward')
                    return pr.get('item_id') if pr else None
        except Exception:
            pass
    try:
        from game.bosses.registry import registry
        defn = registry.get(boss_key)
        if defn is not None:
            for p in defn.phases:
                if p.key == phase_key:
                    return p.phase_reward.item_id if p.phase_reward else None
    except Exception:
        pass
    return None


def _split_loot(items: list, has_phase: bool,
                bonus_item: str | None) -> tuple:
    """Split a persisted items list into (guaranteed, weighted).

    claim_rewards appends exactly one [bonus_item, qty] LAST when the
    raid fought the phase — so the split is positional, never a guess
    by id. A weighted roll of the same item stays weighted.
    """
    items = [tuple(i) for i in (items or [])]
    if has_phase and bonus_item and items and items[-1][0] == bonus_item:
        return (items[-1:], items[:-1])
    return ((), items)


def _sum_items(rows: list) -> tuple:
    agg: dict = {}
    for item, qty in rows:
        agg[item] = agg.get(item, 0) + int(qty or 0)
    return tuple(sorted(agg.items()))


def _scheduler_note_for(gid, event_id) -> str | None:
    """Latest scheduler action text, only when it names this event.
    Stored as 'ts|STARTED:key:id' / 'ts|EXPIRED:id' — older events get
    None rather than someone else's lifecycle line."""
    try:
        raw = db.meta_get(f'wb_{gid}_sched_action', '') or ''
    except Exception:
        return None
    if not raw or f':{event_id}' not in raw:
        return None
    try:
        return raw.split('|', 1)[1]
    except Exception:
        return raw


def _row_to_history(b, parts, rewards, sched_note) -> WorldBossHistory:
    status = b.get('status') or 'ACTIVE'
    outcome = {'COMPLETED': 'defeated', 'EXPIRED': 'expired'}.get(status, 'active')
    defeated_at = int(b.get('defeated_at') or 0)
    expires_at = int(b.get('expires_at') or 0)
    started_at = int(b.get('started_at') or 0)
    if status == 'COMPLETED':
        end_at = defeated_at or None
    elif status == 'EXPIRED':
        end_at = expires_at or None
    else:
        end_at = None
    duration = (end_at - started_at) if end_at is not None else None
    ordered = sorted(parts, key=lambda p: (-int(p[1] or 0), str(p[0])))
    top = tuple(TopEntry(user_id=str(u), damage=int(d or 0), attacks=int(a or 0))
                for u, d, a in ordered[:TOP_SHOWN])
    claims = len(rewards)
    g_rows, w_rows = [], []
    for r in rewards:
        items = r[0] if isinstance(r, (list, tuple)) else []
        phase = r[1] if isinstance(r, (list, tuple)) and len(r) > 1 else None
        g, w = _split_loot(list(items or []), bool(phase), r[2] if len(r) > 2 else None)
        g_rows.extend(g)
        w_rows.extend(w)
    reached = (b.get('max_phase') or '') or None
    return WorldBossHistory(
        event_id=int(b['id']), guild_id=str(b.get('guild_id') or ''),
        boss_key=b.get('boss_key') or '',
        name=_display_name(b.get('boss_key') or '', b.get('config_json') or ''),
        status=status, outcome=outcome, started_at=started_at, end_at=end_at,
        duration_s=duration, max_hp=int(b.get('max_hp') or 0),
        hp=int(b.get('hp') or 0), participants=len(parts),
        total_attacks=sum(int(p[2] or 0) for p in parts),
        total_damage=sum(int(p[1] or 0) for p in parts),
        top=top, claims=claims,
        loot_guaranteed=_sum_items(g_rows), loot_weighted=_sum_items(w_rows),
        loot_total=_sum_items(g_rows + w_rows),
        phase_reached=reached,
        phase_name=_phase_name(b.get('boss_key') or '', b.get('config_json') or '',
                               reached or ''),
        scheduler_note=sched_note)


def _load_event(conn, gid, event_id):
    row = conn.execute('SELECT * FROM world_boss WHERE id=? AND guild_id=?',
                       (int(event_id), str(gid))).fetchone()
    if not row:
        return None
    b = dict(row)
    parts = [(r['user_id'], r['damage'], r['attacks']) for r in conn.execute(
        'SELECT user_id, damage, attacks FROM world_boss_parts WHERE boss_id=?',
        (b['id'],)).fetchall()]
    rew = []
    for r in conn.execute('SELECT reward_json FROM world_boss_rewards WHERE boss_id=?',
                          (b['id'],)).fetchall():
        try:
            import json as _json
            data = _json.loads(r['reward_json'])
        except Exception:
            continue
        rew.append((data.get('items') or [], data.get('phase'),
                    _expected_bonus_item(b.get('boss_key') or '',
                                         b.get('config_json') or '',
                                         data.get('phase') or '')))
    return _row_to_history(b, parts, rew, _scheduler_note_for(gid, b['id']))


def count_events(guild_id) -> int:
    """Total event rows for the guild. Read-only; 0 when tables are absent."""
    with db.conn_ctx() as conn:
        if not _tables_present(conn):
            return 0
        try:
            row = conn.execute('SELECT COUNT(*) c FROM world_boss WHERE guild_id=?',
                               (str(guild_id),)).fetchone()
            return int(row['c'] or 0) if row else 0
        except Exception:
            return 0


def recent_events(guild_id, limit: int = 10, offset: int = 0) -> list:
    """Newest first, guild-scoped, ACTIVE included. Empty list when the
    tables don't exist yet — never creates them (read-only)."""
    limit = max(0, min(100, int(limit or 0)))
    offset = max(0, int(offset or 0))
    gid = str(guild_id)
    with db.conn_ctx() as conn:
        if not _tables_present(conn):
            return []
        rows = conn.execute(
            'SELECT * FROM world_boss WHERE guild_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
            (gid, limit, offset)).fetchall()
        out = []
        for row in rows:
            b = dict(row)
            parts = [(r['user_id'], r['damage'], r['attacks']) for r in conn.execute(
                'SELECT user_id, damage, attacks FROM world_boss_parts WHERE boss_id=?',
                (b['id'],)).fetchall()]
            rew = []
            for r in conn.execute(
                    'SELECT reward_json FROM world_boss_rewards WHERE boss_id=?',
                    (b['id'],)).fetchall():
                try:
                    import json as _json
                    data = _json.loads(r['reward_json'])
                except Exception:
                    continue
                rew.append((data.get('items') or [], data.get('phase'),
                            _expected_bonus_item(b.get('boss_key') or '',
                                                 b.get('config_json') or '',
                                                 data.get('phase') or '')))
            out.append(_row_to_history(b, parts, rew, _scheduler_note_for(gid, b['id'])))
    return out


def event_details(guild_id, event_id) -> WorldBossHistory | None:
    """One guild-scoped event, or None (other guild / missing / no tables)."""
    try:
        eid = int(event_id)
    except Exception:
        return None
    with db.conn_ctx() as conn:
        if not _tables_present(conn):
            return None
        return _load_event(conn, str(guild_id), eid)


def leaderboard(guild_id, metric: str = 'damage', page: int = 0,
                per_page: int = LEADERBOARD_PAGE_SIZE) -> LeaderboardPage:
    """Guild-scoped, stable pagination. Ties break by secondary value,
    then user_id ascending — the same order every call.

    damage:       total damage (extra: total attacks)
    wins:         COMPLETED events topped (extra: total damage)
    participation: events joined (extra: total attacks)
    """
    metric = (metric or 'damage').lower()
    if metric not in VALID_METRICS:
        metric = 'damage'
    page = max(0, int(page or 0))
    per_page = max(1, min(50, int(per_page or LEADERBOARD_PAGE_SIZE)))
    gid = str(guild_id)
    with db.conn_ctx() as conn:
        if not _tables_present(conn):
            return LeaderboardPage(metric, page, per_page, 0, 0, ())
        parts = [dict(r) for r in conn.execute(
            'SELECT p.user_id, p.damage, p.attacks, p.boss_id, b.status '
            'FROM world_boss_parts p JOIN world_boss b ON b.id=p.boss_id '
            'WHERE b.guild_id=?', (gid,)).fetchall()]
    if metric == 'damage':
        agg: dict = {}
        for p in parts:
            a = agg.setdefault(str(p['user_id']), [0, 0])
            a[0] += int(p['damage'] or 0)
            a[1] += int(p['attacks'] or 0)
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
        rows = [(u, d, a) for u, (d, a) in ranked]
    elif metric == 'participation':
        agg = {}
        for p in parts:
            a = agg.setdefault(str(p['user_id']), [set(), 0])
            a[0].add(int(p['boss_id']))
            a[1] += int(p['attacks'] or 0)
        ranked = sorted(agg.items(), key=lambda kv: (-len(kv[1][0]), -kv[1][1], kv[0]))
        rows = [(u, len(s), a) for u, (s, a) in ranked]
    else:  # wins — top contributor (damage DESC, user_id ASC) of COMPLETED events
        by_event: dict = {}
        for p in parts:
            if p.get('status') != 'COMPLETED':
                continue
            by_event.setdefault(int(p['boss_id']), []).append(p)
        wins: dict = {}
        dmg: dict = {}
        for p in parts:
            dmg[str(p['user_id'])] = dmg.get(str(p['user_id']), 0) + int(p['damage'] or 0)
        for plist in by_event.values():
            top = sorted(plist, key=lambda p: (-int(p['damage'] or 0), str(p['user_id'])))[0]
            u = str(top['user_id'])
            wins[u] = wins.get(u, 0) + 1
        ranked = sorted(wins.items(), key=lambda kv: (-kv[1], -dmg.get(kv[0], 0), kv[0]))
        rows = [(u, w, dmg.get(u, 0)) for u, w in ranked]
    total = len(rows)
    pages = (total + per_page - 1) // per_page if total else 0
    if page >= pages:
        return LeaderboardPage(metric, page, per_page, total, pages, ())
    slice_ = rows[page * per_page:(page + 1) * per_page]
    entries = tuple(LeaderboardEntry(rank=page * per_page + i + 1, user_id=u,
                                     value=int(v), extra=int(x))
                    for i, (u, v, x) in enumerate(slice_))
    return LeaderboardPage(metric, page, per_page, total, pages, entries)


def statistics(guild_id, period: int | None = None,
               now: int | None = None) -> WorldBossStats:
    """All-time when period is None, else events with started_at >= now-period."""
    import time as _t
    gid = str(guild_id)
    now = int(now if now is not None else _t.time())
    with db.conn_ctx() as conn:
        if not _tables_present(conn):
            return WorldBossStats()
        if period is None:
            events = [dict(r) for r in conn.execute(
                'SELECT * FROM world_boss WHERE guild_id=?', (gid,)).fetchall()]
        else:
            try:
                cutoff = now - max(0, int(period))
            except Exception:
                cutoff = now
            events = [dict(r) for r in conn.execute(
                'SELECT * FROM world_boss WHERE guild_id=? AND started_at>=?',
                (gid, cutoff)).fetchall()]
        if not events:
            return WorldBossStats()
        ids = [e['id'] for e in events]
        q = ','.join('?' * len(ids))
        parts = [dict(r) for r in conn.execute(
            f'SELECT boss_id, damage, attacks FROM world_boss_parts WHERE boss_id IN ({q})',
            ids).fetchall()]
        rew = []
        for r in conn.execute(
                f'SELECT boss_id, reward_json FROM world_boss_rewards WHERE boss_id IN ({q})',
                ids).fetchall():
            try:
                import json as _json
                data = _json.loads(r['reward_json'])
            except Exception:
                continue
            rew.append((dict(r)['boss_id'], data.get('items') or [], data.get('phase')))
    by_id = {e['id']: e for e in events}
    defeated = sum(1 for e in events if e.get('status') == 'COMPLETED')
    expired = sum(1 for e in events if e.get('status') == 'EXPIRED')
    active = sum(1 for e in events if e.get('status') == 'ACTIVE')
    finished = [e for e in events if e.get('status') in ('COMPLETED', 'EXPIRED')]
    defeat_rate = (defeated / len(finished)) if finished else 0.0
    durs = [int(e.get('defeated_at') or e.get('expires_at') or 0) - int(e.get('started_at') or 0)
            for e in finished]
    durs = [d for d in durs if d >= 0]
    avg_dur = int(sum(durs) / len(durs)) if durs else None
    per_event_parts: dict = {}
    for p in parts:
        per_event_parts.setdefault(int(p['boss_id']), []).append(p)
    counts = [len(per_event_parts.get(e['id'], ())) for e in events]
    avg_parts = sum(counts) / len(counts) if counts else 0.0
    atk = sum(int(p['attacks'] or 0) for p in parts)
    dmg = sum(int(p['damage'] or 0) for p in parts)
    avg_dpa = (dmg / atk) if atk else 0.0
    use: dict = {}
    for e in events:
        use[e.get('boss_key') or ''] = use.get(e.get('boss_key') or '', 0) + 1
    most_used = max(sorted(use.items()), key=lambda kv: kv[1])[0] if use else None
    ph: dict = {}
    for e in events:
        k = (e.get('max_phase') or '')
        if k:
            ph[k] = ph.get(k, 0) + 1
    most_phase = max(sorted(ph.items()), key=lambda kv: kv[1])[0] if ph else None
    g_rows, w_rows = [], []
    for bid, items, phase in rew:
        b = by_id.get(bid) or {}
        bonus = _expected_bonus_item(b.get('boss_key') or '', b.get('config_json') or '',
                                     phase or '')
        g, w = _split_loot(list(items or []), bool(phase), bonus)
        g_rows.extend(g)
        w_rows.extend(w)
    return WorldBossStats(
        total=len(events), active=active, defeated=defeated, expired=expired,
        defeat_rate=round(defeat_rate, 4), avg_duration_s=avg_dur,
        avg_participants=round(avg_parts, 2),
        avg_damage_per_attack=round(avg_dpa, 2), total_claims=len(rew),
        most_used_boss=most_used, most_common_phase=most_phase,
        loot_guaranteed=_sum_items(g_rows), loot_weighted=_sum_items(w_rows),
        loot_total=_sum_items(g_rows + w_rows))

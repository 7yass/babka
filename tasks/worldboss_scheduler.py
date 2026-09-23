"""World-boss scheduler: idempotent expiry + rotation auto-start.

Pure logic over guild objects (id + get_channel) with an injected clock —
no Discord imports, fully testable. The WorldBoss cog runs tick() on a
loop. The task never settles rewards or sends messages; it only discovers
due work and calls services.worldboss_service (expiry + start).

Per-guild settings live in the existing meta table (see DEFAULTS).
Lifecycle footprint per guild (also meta, namespaced):
  wb_<gid>_sched_tick    last pass timestamp
  wb_<gid>_sched_action  "ts|CODE:boss:event" of the last real action
  wb_<gid>_sched_error   "ts|message" of the last failure (cleared on action)
"""
import time as _time
from dataclasses import dataclass

import database as db

DEFAULTS = {'enabled': '1', 'auto': '0', 'rotation': 'dreadmaw,tidecaller',
            'interval': '21600', 'duration': '0', 'channel': ''}


def _bool_norm(v: str):
    low = str(v).lower()
    if low in ('1', 'on', 'true', 'yes'):
        return '1', True
    if low in ('0', 'off', 'false', 'no'):
        return '0', True
    return v, False


def _validate(name: str, value: str):
    """(ok, normalized). Used by set_setting and staff commands."""
    v = str(value or '').strip()
    if name in ('enabled', 'auto'):
        norm, ok = _bool_norm(v)
        return ok, norm
    if name == 'rotation':
        keys = [k.strip().lower() for k in v.replace(',', ' ').split() if k.strip()]
        if not keys:
            return False, v
        from game.bosses.registry import registry
        bad = [k for k in keys if not registry.exists(k)]
        return (not bad), ' '.join(keys)
    if name == 'interval':
        secs = _parse_duration(v)
        return (secs is not None and secs >= 60), str(secs or v)
    if name == 'duration':
        if v in ('', '0', 'off', 'none', 'default'):
            return True, '0'
        secs = _parse_duration(v)
        return (secs is not None and secs >= 60), str(secs or v)
    if name == 'channel':
        digits = ''.join(c for c in v if c.isdigit())
        if v.lower() in ('', 'off', 'none', 'clear'):
            return True, ''
        return (bool(digits), digits)
    return False, v


def _parse_duration(v: str):
    """'6h'/'30m' -> seconds, bare numbers are already seconds.
    None when unparseable."""
    s = str(v or '').strip().lower()
    try:
        if s.endswith('h'):
            return int(float(s[:-1]) * 3600)
        if s.endswith('m'):
            return int(float(s[:-1]) * 60)
        return int(float(s))
    except Exception:
        return None


@dataclass
class SchedulerAction:
    guild_id: str
    action: str       # EXPIRED / STARTED / SKIPPED / FAILED
    ok: bool
    code: str         # EXPIRED, STARTED, AUTO_DISABLED, CHANNEL_MISSING,
                      # INTERVAL_NOT_REACHED, ROTATION_EMPTY, ACTIVE,
                      # NO_ACTIVE_EVENT, INVALID_CONFIGURATION, FAILED
    boss_key: str | None = None
    event_id: int | None = None
    error: str | None = None
    duration_ms: int = 0


def _settings(gid) -> dict:
    return {name: db.meta_get(f'wb_{gid}_{name}', default)
            for name, default in DEFAULTS.items()}


def get_settings(gid) -> dict:
    """Resolved settings with types: ints for interval/duration."""
    import re as _re
    s = _settings(gid)
    try:
        interval = max(60, int(s.get('interval') or 0))
    except Exception:
        interval = 21600
    try:
        duration = int(s.get('duration') or 0) or None
    except Exception:
        duration = None
    return {'enabled': str(s.get('enabled', '1')) == '1',
            'auto': str(s.get('auto', '0')) == '1',
            'rotation': tuple(k.strip().lower() for k in
                              _re.split(r'[\s,]+', str(s.get('rotation') or '')) if k.strip()),
            'interval': interval, 'duration': duration,
            'channel': str(s.get('channel') or '').strip()}


def set_setting(gid, name: str, value: str) -> bool:
    """Staff write path. Validates first; False leaves state unchanged."""
    if name not in DEFAULTS:
        return False
    ok, norm = _validate(name, value)
    if not ok:
        return False
    db.meta_set(f'wb_{gid}_{name}', norm)
    return True


def _last_event(conn, gid):
    return conn.execute('SELECT boss_key, status, defeated_at, expires_at FROM world_boss '
                        'WHERE guild_id=? ORDER BY id DESC LIMIT 1', (str(gid),)).fetchone()


def _last_end(row) -> int | None:
    """When the last event ended (None = never ran = due now)."""
    if not row:
        return None
    d = dict(row)
    if d.get('status') == 'COMPLETED':
        return int(d.get('defeated_at') or 0)
    return int(d.get('expires_at') or 0)


def _next_in_rotation(rotation, last_key):
    """First enabled registry boss after the last one, wrapping."""
    from game.bosses.registry import registry
    if not rotation:
        return None
    try:
        start = list(rotation).index((last_key or '').lower()) + 1
    except ValueError:
        start = 0
    order = list(rotation)
    for i in range(len(order)):
        cand = registry.get(order[(start + i) % len(order)])
        if cand is not None and cand.enabled:
            return cand
    return None


def _record(gid, now: int, kind: str, text: str = '') -> None:
    """Lifecycle footprint. Tick stamps every pass; actions overwrite the
    last action and clear errors; failures record (never raise here)."""
    try:
        if kind == 'tick':
            db.meta_set(f'wb_{gid}_sched_tick', str(now))
        elif kind == 'action':
            db.meta_set(f'wb_{gid}_sched_action', f'{now}|{text}')
            db.meta_set(f'wb_{gid}_sched_error', '')
        elif kind == 'error':
            db.meta_set(f'wb_{gid}_sched_error', f'{now}|{text}')
    except Exception:
        pass


def lifecycle_info(gid) -> dict:
    """Last tick/action/error for status output. Never raises."""
    def _get(name):
        try:
            return db.meta_get(f'wb_{gid}_sched_{name}', '') or ''
        except Exception:
            return ''
    out = {}
    try:
        out['tick_at'] = int(_get('tick') or 0) or None
    except Exception:
        out['tick_at'] = None
    out['action'] = _get('action')
    out['error'] = _get('error')
    return out


def last_event_info(gid):
    """(boss_key, end_ts|None, status) of the latest event. For next-time math."""
    try:
        with db.conn_ctx() as conn:
            row = _last_event(conn, gid)
        if not row:
            return None, None, None
        d = dict(row)
        return d.get('boss_key'), _last_end(row), d.get('status')
    except Exception:
        return None, None, None


def claims_open(gid) -> bool:
    """A finished event with unclaimed rewards still exists."""
    try:
        with db.conn_ctx() as conn:
            row = conn.execute("SELECT id FROM world_boss WHERE guild_id=? AND status='COMPLETED' "
                               "ORDER BY id DESC LIMIT 1", (str(gid),)).fetchone()
            if not row:
                return False
            left = conn.execute('SELECT COUNT(*) c FROM world_boss_parts '
                                'WHERE boss_id=? AND attacks>0 AND reward_claimed=0',
                                (row['id'],)).fetchone()['c']
            return (left or 0) > 0
    except Exception:
        return False


def _err(where: str, e: Exception) -> str:
    """Compact failure tag: phase, type and a short message (no payloads)."""
    try:
        msg = str(e)[:160].replace('\n', ' ')
    except Exception:
        msg = ''
    return f'{where}:{type(e).__name__}:{msg}' if msg else f'{where}:{type(e).__name__}'


def _act(gid, action, ok, code, boss_key=None, event_id=None, error=None,
         t0=None) -> SchedulerAction:
    ms = 0
    try:
        ms = int((_time.monotonic() - (t0 or _time.monotonic())) * 1000)
    except Exception:
        pass
    return SchedulerAction(guild_id=str(gid), action=action, ok=bool(ok), code=code,
                           boss_key=boss_key, event_id=event_id, error=error,
                           duration_ms=ms)


def process_guild(gid, guild, now: int):
    """One guild, one pass: (legacy summary dict, [SchedulerAction]).
    Never raises. Legacy shape is frozen for old callers/tests."""
    from services import worldboss_service as _wb
    gid = str(gid)
    t0 = _time.monotonic()
    out = {'expired': False, 'started': None, 'skipped': None}
    actions = []
    _record(gid, now, 'tick')
    try:
        exp = _wb.expire_event(gid, now)
        out['expired'] = bool(exp['ok'])
        if exp['ok']:
            actions.append(_act(gid, 'EXPIRED', True, 'EXPIRED', None, exp.get('boss_id'), None, t0))
            _record(gid, now, 'action', f"EXPIRED:{exp.get('boss_id')}")
    except Exception as e:
        err = _err('expire', e)
        actions.append(_act(gid, 'FAILED', False, 'FAILED', None, None, err, t0))
        _record(gid, now, 'error', err)
        out['error'] = err
        return out, actions

    def _skip(reason, code):
        out['skipped'] = reason
        actions.append(_act(gid, 'SKIPPED', True, code, None, None, None, t0))
        return out, actions

    def _fail(where, e):
        err = _err(where, e)
        actions.append(_act(gid, 'FAILED', False, 'FAILED', None, None, err, t0))
        _record(gid, now, 'error', err)
        out['error'] = err
        return out, actions

    try:
        try:
            s = get_settings(gid)
        except Exception:
            actions.append(_act(gid, 'SKIPPED', False, 'INVALID_CONFIGURATION', None, None, None, t0))
            out['skipped'] = 'config-unreadable'
            return out, actions
        if not s['enabled'] or not s['auto']:
            return _skip('auto-off', 'AUTO_DISABLED')
        if not s['channel']:
            return _skip('no-channel', 'CHANNEL_MISSING')
        try:
            channel_ok = guild is not None and guild.get_channel(int(s['channel'])) is not None
        except Exception:
            channel_ok = False
        if not channel_ok:
            return _skip('bad-channel', 'CHANNEL_MISSING')
        with db.conn_ctx() as conn:
            active = conn.execute("SELECT id FROM world_boss WHERE guild_id=? AND status='ACTIVE'",
                                  (gid,)).fetchone()
            if active:
                return _skip('active', 'ACTIVE')
            last = _last_event(conn, gid)
        end = _last_end(last)
        if end is not None and now - end < s['interval']:
            return _skip('interval', 'INTERVAL_NOT_REACHED')
        last_key = (dict(last).get('boss_key') if last else '') or ''
        cand = _next_in_rotation(s['rotation'], last_key)
        if cand is None:
            return _skip('no-candidate', 'ROTATION_EMPTY')
        res = _wb.start_boss(gid, cand.key, now, duration=s['duration'])
        if res['ok']:
            out['started'] = cand.key
            actions.append(_act(gid, 'STARTED', True, 'STARTED', cand.key,
                                res.get('boss_id'), None, t0))
            _record(gid, now, 'action', f"STARTED:{cand.key}:{res.get('boss_id')}")
        else:
            out['skipped'] = res.get('code', 'start-failed')
            actions.append(_act(gid, 'SKIPPED', True, res.get('code', 'START_FAILED'),
                                cand.key, None, None, t0))
        return out, actions
    except Exception as e:
        return _fail('start', e)


def run_pass(guilds, now: int):
    """Structured pass: (legacy tick dict, all actions). New callers use this."""
    tick_out, acts = {}, []
    for g in guilds or []:
        try:
            gid = str(g.id)
        except Exception:
            gid = '?'
        try:
            out, actions = process_guild(gid, g, int(now))
            tick_out[gid] = out
            acts.extend(actions)
        except Exception as e:
            err = _err('tick', e)
            tick_out[gid] = {'expired': False, 'started': None,
                             'skipped': None, 'error': err}
            acts.append(_act(gid, 'FAILED', False, 'FAILED', None, None, err))
    return tick_out, acts


def tick(guilds, now: int) -> dict:
    """Legacy adapter: all guilds, each isolated. Shape frozen."""
    out, _ = run_pass(guilds, now)
    return out

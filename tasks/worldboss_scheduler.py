"""World-boss scheduler: idempotent expiry + rotation auto-start.

Pure logic over guild objects (id + get_channel) with an injected clock —
no Discord imports, fully testable. The WorldBoss cog runs tick() on a
loop. The task never settles rewards or sends messages; it only discovers
due work and calls services.worldboss_service (expiry + start).

Per-guild settings live in the existing meta table:
  wb_<gid>_enabled      1/0, default 1
  wb_<gid>_auto         1/0, default 0 (staff opts in)
  wb_<gid>_rotation     "dreadmaw,tidecaller", default both
  wb_<gid>_interval     seconds between events, default 21600 (6h)
  wb_<gid>_duration     event seconds, default 0 = definition default
  wb_<gid>_channel      auto-start announcement gate, default '' = never
"""
import database as db

DEFAULTS = {'enabled': '1', 'auto': '0', 'rotation': 'dreadmaw,tidecaller',
            'interval': '21600', 'duration': '0', 'channel': ''}


def _settings(gid) -> dict:
    return {name: db.meta_get(f'wb_{gid}_{name}', default)
            for name, default in DEFAULTS.items()}


def get_settings(gid) -> dict:
    """Resolved settings with types: ints for interval/duration."""
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
                              str(s.get('rotation') or '').split(',') if k.strip()),
            'interval': interval, 'duration': duration,
            'channel': str(s.get('channel') or '').strip()}


def set_setting(gid, name: str, value: str) -> bool:
    """Staff write path. Returns False for unknown setting names."""
    if name not in DEFAULTS:
        return False
    db.meta_set(f'wb_{gid}_{name}', str(value))
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


def process_guild(gid, guild, now: int) -> dict:
    """One guild, one pass: expire, then maybe auto-start. Never raises."""
    from services import worldboss_service as _wb
    gid = str(gid)
    out = {'expired': False, 'started': None, 'skipped': None}
    try:
        exp = _wb.expire_event(gid, now)
        out['expired'] = bool(exp['ok'])
    except Exception as e:
        return {'expired': False, 'started': None,
                'skipped': None, 'error': f'expire:{type(e).__name__}'}
    try:
        s = get_settings(gid)
        if not s['enabled'] or not s['auto']:
            out['skipped'] = 'auto-off'
            return out
        if not s['channel']:
            out['skipped'] = 'no-channel'
            return out
        try:
            channel_ok = guild is not None and guild.get_channel(int(s['channel'])) is not None
        except Exception:
            channel_ok = False
        if not channel_ok:
            out['skipped'] = 'bad-channel'
            return out
        with db.conn_ctx() as conn:
            active = conn.execute("SELECT id FROM world_boss WHERE guild_id=? AND status='ACTIVE'",
                                  (gid,)).fetchone()
            if active:
                out['skipped'] = 'active'
                return out
            last = _last_event(conn, gid)
        end = _last_end(last)
        if end is not None and now - end < s['interval']:
            out['skipped'] = 'interval'
            return out
        last_key = (dict(last).get('boss_key') if last else '') or ''
        cand = _next_in_rotation(s['rotation'], last_key)
        if cand is None:
            out['skipped'] = 'no-candidate'
            return out
        res = _wb.start_boss(gid, cand.key, now, duration=s['duration'])
        if res['ok']:
            out['started'] = cand.key
        else:
            out['skipped'] = res.get('code', 'start-failed')
        return out
    except Exception as e:
        out['error'] = f'start:{type(e).__name__}'
        return out


def tick(guilds, now: int) -> dict:
    """All guilds, each isolated: one guild's failure never stops the rest."""
    out = {}
    for g in guilds or []:
        try:
            gid = str(getattr(g, 'id', 0))
            out[gid] = process_guild(gid, g, int(now))
        except Exception as e:
            try:
                out[str(getattr(g, 'id', '?'))] = {
                    'expired': False, 'started': None, 'skipped': None,
                    'error': f'tick:{type(e).__name__}'}
            except Exception:
                pass
    return out

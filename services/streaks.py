"""Streaks: the cheapest legible progression there is.

A streak costs a player nothing but attention and it pays every single day.
The multiplier steps up every STREAK_STEP days and is capped, so a streak is
worth chasing but not worth breaking your life over.

The state lives in its own table (eco_streaks) rather than in the legacy
eco.daily_streak column, so it survives a restart cleanly, works for more
than one kind of streak, and can be measured.

Grace: missing a day within STREAK_GRACE does not break the chain. Nobody
who plays at 11pm should lose a week of progress to a 23:55 login.
"""
import time

import database as db


def mult(count: int) -> float:
    """Reward multiplier for a streak of `count` days."""
    from utils.economy import (STREAK_STEP, STREAK_MULT_PER_STEP,
                               STREAK_MAX_MULT)
    if count <= 1:
        return 1.0
    steps = (int(count) - 1) // STREAK_STEP
    return min(1.0 + steps * STREAK_MULT_PER_STEP, STREAK_MAX_MULT)


def get(gid, uid, kind: str = 'daily') -> dict:
    """Current streak state. Never raises."""
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO eco_streaks '
                     '(guild_id,user_id,kind,count,best,last_at) VALUES (?,?,?,0,0,0)',
                     (str(gid), str(uid), kind))
        row = conn.execute('SELECT count, best, last_at FROM eco_streaks '
                           'WHERE guild_id=? AND user_id=? AND kind=?',
                           (str(gid), str(uid), kind)).fetchone()
    count = int(row['count'] or 0) if row else 0
    best = int(row['best'] or 0) if row else 0
    last = int(row['last_at'] or 0) if row else 0
    return {'count': count, 'best': max(best, count), 'last_at': last,
            'mult': mult(count), 'active': _is_live(last)}


def _is_live(last_at: int) -> bool:
    """True when the chain is still alive (within the grace window)."""
    from utils.economy import STREAK_GRACE
    if not last_at:
        return False
    return (int(time.time()) - int(last_at)) <= STREAK_GRACE


def bump(gid, uid, kind: str = 'daily', conn=None) -> dict:
    """Advance a streak. Returns the new state.

    The first claim of a chain starts it. A claim inside the grace window
    continues it. A claim outside it restarts the chain at 1.
    """
    now = int(time.time())
    from utils.economy import STREAK_GRACE
    if conn is not None:
        return _bump_conn(conn, gid, uid, kind, now, STREAK_GRACE)
    with db.conn_ctx() as c:
        return _bump_conn(c, gid, uid, kind, now, STREAK_GRACE)


def _bump_conn(conn, gid, uid, kind, now, grace) -> dict:
    conn.execute('INSERT OR IGNORE INTO eco_streaks '
                 '(guild_id,user_id,kind,count,best,last_at) VALUES (?,?,?,0,0,0)',
                 (str(gid), str(uid), kind))
    row = conn.execute('SELECT count, best, last_at FROM eco_streaks '
                       'WHERE guild_id=? AND user_id=? AND kind=?',
                       (str(gid), str(uid), kind)).fetchone()
    count = int(row['count'] or 0) if row else 0
    best = int(row['best'] or 0) if row else 0
    last = int(row['last_at'] or 0) if row else 0

    if last and (now - last) <= grace:
        count += 1
    else:
        count = 1
    best = max(best, count)
    conn.execute('UPDATE eco_streaks SET count=?, best=?, last_at=? '
                 'WHERE guild_id=? AND user_id=? AND kind=?',
                 (count, best, now, str(gid), str(uid), kind))
    return {'count': count, 'best': best, 'last_at': now, 'mult': mult(count),
            'active': True}


def leaderboard(gid, kind: str = 'daily', limit: int = 10) -> list:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT user_id, count, best FROM eco_streaks '
                            'WHERE guild_id=? AND kind=? ORDER BY count DESC LIMIT ?',
                            (str(gid), kind, int(limit))).fetchall()
    return [dict(r) for r in rows]

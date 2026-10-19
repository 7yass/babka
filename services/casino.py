"""Casino tickets — the single gate on gambling.

One ticket buys one play on any casino game. They arrive from `.drop`
(CASINO_TIX_DROP_CHANCE per claim) and `.daily` (CASINO_TIX_DAILY
guaranteed), and bank to CASINO_TIX_MAX so nobody hoards an endless stack.

This replaces the old 15-plays-per-hour counter: tickets are the only
limiter, so players have one number to watch instead of two.

The house ID never pays for a play and never loses the coin flip.
"""
import random

import database as db
from utils.economy import (CASINO_TIX_DROP_CHANCE, CASINO_TIX_DAILY,
                           CASINO_TIX_MAX)

# House ID: plays are free and it keeps its existing payout tilt.
GOD_ID = '1270782781605154922'


def is_free(uid) -> bool:
    return str(uid) == GOD_ID


def casino_tix(gid, uid) -> int:
    """Current ticket count (0 for the house ID)."""
    if is_free(uid):
        return CASINO_TIX_MAX
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT tickets FROM casino_tix WHERE guild_id=? AND user_id=?',
            (str(gid), str(uid))).fetchone()
    return int(row['tickets']) if row and row['tickets'] is not None else 0


def add_casino_tix(gid, uid, n: int) -> int:
    """Add n tickets, clamped to CASINO_TIX_MAX. Returns the new total."""
    if is_free(uid) or n <= 0:
        return casino_tix(gid, uid)
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO casino_tix (guild_id, user_id, tickets) VALUES (?,?,0)',
            (str(gid), str(uid)))
        conn.execute(
            'UPDATE casino_tix SET tickets = MIN(?, tickets + ?) '
            'WHERE guild_id=? AND user_id=?',
            (CASINO_TIX_MAX, int(n), str(gid), str(uid)))
        row = conn.execute(
            'SELECT tickets FROM casino_tix WHERE guild_id=? AND user_id=?',
            (str(gid), str(uid))).fetchone()
    return int(row['tickets']) if row and row['tickets'] is not None else 0


def take_casino_tix(gid, uid) -> bool:
    """Spend one ticket. False means the player is out — callers must refuse.

    The decrement is guarded by `tickets > 0` in the WHERE clause, so two
    concurrent clicks can never both succeed. House ID always succeeds.
    """
    if is_free(uid):
        return True
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO casino_tix (guild_id, user_id, tickets) VALUES (?,?,0)',
            (str(gid), str(uid)))
        cur = conn.execute(
            'UPDATE casino_tix SET tickets = tickets - 1 '
            'WHERE guild_id=? AND user_id=? AND tickets > 0',
            (str(gid), str(uid)))
        return bool(cur.rowcount)


def roll_drop_ticket(gid, uid) -> int:
    """Called on every `.drop` claim: chance-based ticket. Returns 0 or 1."""
    if is_free(uid):
        return 0
    if random.random() >= CASINO_TIX_DROP_CHANCE:
        return 0
    return 1 if add_casino_tix(gid, uid, 1) > 0 else 0


def grant_daily(gid, uid) -> int:
    """Called on `.daily`: guaranteed tickets. Returns how many were added."""
    before = casino_tix(gid, uid)
    after = add_casino_tix(gid, uid, CASINO_TIX_DAILY)
    return max(0, after - before)

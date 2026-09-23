"""Shared asset primitives for market + trade (rework: common layer).

Narrow on purpose: only operations both services already perform with
identical semantics. Quirks stay OUT (locked/fav/active validation lives
in each service's policy; held-item stripping is market-only; offer
state is trade-only).

Transaction-neutral: every helper takes an open connection and never
commits or rolls back. The caller owns the boundary — combine several
helpers in one `conn_ctx` for atomic settlement. Failures raise the
typed errors below; services translate them into result codes + text.
No Discord imports here, ever.
"""
import database as db


class InsufficientCashError(Exception):
    def __init__(self, gid, uid, amount, balance):
        super().__init__('insufficient cash')
        self.gid, self.uid, self.amount, self.balance = gid, uid, amount, balance


class InsufficientItemError(Exception):
    def __init__(self, gid, uid, item, quantity, have):
        super().__init__('insufficient items')
        self.gid, self.uid, self.item, self.quantity, self.have = gid, uid, item, quantity, have


class OwnershipError(Exception):
    def __init__(self, gid, uid, pokemon_id):
        super().__init__('not owned')
        self.gid, self.uid, self.pokemon_id = gid, uid, pokemon_id


def ensure_eco(conn, gid, uid) -> None:
    """Make sure the wallet row exists (START_CASH default, like bal())."""
    from cogs.gamble import START_CASH
    conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,?)',
                 (str(gid), str(uid), START_CASH))


def cash_of(conn, gid, uid) -> int:
    row = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                       (str(gid), str(uid))).fetchone()
    return int(row['cash']) if row and row['cash'] is not None else 0


def debit_cash(conn, gid, uid, amount: int) -> int:
    """Subtract amount, return resulting balance. Raises InsufficientCashError."""
    ensure_eco(conn, gid, uid)
    bal = cash_of(conn, gid, uid)
    if bal < amount:
        raise InsufficientCashError(gid, uid, amount, bal)
    conn.execute('UPDATE eco SET cash=cash-? WHERE guild_id=? AND user_id=?',
                 (int(amount), str(gid), str(uid)))
    return bal - int(amount)


def credit_cash(conn, gid, uid, amount: int) -> int:
    """Add amount, return resulting balance."""
    ensure_eco(conn, gid, uid)
    conn.execute('UPDATE eco SET cash=cash+? WHERE guild_id=? AND user_id=?',
                 (int(amount), str(gid), str(uid)))
    return cash_of(conn, gid, uid)


def upsert_item(conn, gid, uid, item: str, qty: int) -> int:
    """Add qty of an item (pk_balls doubles as the item table, as today).
    Returns resulting qty. qty must be positive."""
    if not item or int(qty) <= 0:
        raise ValueError('qty must be positive')
    conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) VALUES (?,?,?,0)',
                 (str(gid), str(uid), item))
    conn.execute('UPDATE pk_balls SET qty=qty+? WHERE guild_id=? AND user_id=? AND ball=?',
                 (int(qty), str(gid), str(uid), item))
    row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                       (str(gid), str(uid), item)).fetchone()
    return int(row['qty'] or 0)


def remove_item(conn, gid, uid, item: str, qty: int) -> int:
    """Guarded take (mirrors balls_take): single UPDATE, zero rows kept.
    Raises InsufficientItemError instead of returning False."""
    if not item or int(qty) <= 0:
        raise ValueError('qty must be positive')
    conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) VALUES (?,?,?,0)',
                 (str(gid), str(uid), item))
    cur = conn.execute('UPDATE pk_balls SET qty=qty-? WHERE guild_id=? AND user_id=? AND ball=? AND qty>=?',
                       (int(qty), str(gid), str(uid), item, int(qty)))
    if (cur.rowcount or 0) != 1:
        row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                           (str(gid), str(uid), item)).fetchone()
        have = int(dict(row)['qty'] or 0) if row else 0
        raise InsufficientItemError(gid, uid, item, qty, have)
    row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                       (str(gid), str(uid), item)).fetchone()
    return int(row['qty'] or 0)


def get_owned_pokemon(conn, gid, uid, pokemon_id) -> dict | None:
    """Owned mon row or None. No validation beyond ownership."""
    row = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                       (pokemon_id, str(gid), str(uid))).fetchone()
    return dict(row) if row else None


def transfer_pokemon(conn, gid, pokemon_id, from_uid, to_uid) -> None:
    """Trade-shaped move: ownership UPDATE + active cleared, guarded by the
    current owner. Raises OwnershipError when the row is gone/stale.
    Availability checks (locked/fav/...) are the SERVICE's job."""
    cur = conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=? AND guild_id=? AND owner_id=?',
                       (str(to_uid), pokemon_id, str(gid), str(from_uid)))
    if (cur.rowcount or 0) != 1:
        raise OwnershipError(gid, from_uid, pokemon_id)


def reassign_active(conn, gid, uid):
    """First-by-id mon becomes active, unconditionally (market-create +
    trade shape). Returns the id or None when the box is empty."""
    row = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id LIMIT 1',
                       (str(gid), str(uid))).fetchone()
    if row:
        conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (row['id'],))
        return row['id']
    return None

"""The money spine.

Before this module, money moved by raw `UPDATE eco SET cash=...` scattered
across ten files, and nothing recorded WHY. The bot could tell you how much
a user had; it could not tell you how they got it, where it went, or
whether the economy was healthy. Fifteen currencies, zero audit trail.

Two rules, and the whole economy leans on them:

  1. The balance change and its ledger row commit in the SAME transaction.
     A ledger that can disagree with the wallet is worse than no ledger.
  2. Nothing outside this module writes to a wallet column.

Callers name a `reason` — a stable machine key like 'daily' or
'casino_slots_win'. If they forget, the reason is inferred from the call
stack, so a missed call site still produces a usable ledger row instead of
a blank one. That inference is what let 82 existing call sites keep working
untouched while still becoming measurable.

Every function takes an open `conn` so the caller owns the transaction
boundary. `apply()` is the convenience wrapper for callers that do not.
"""
import sys
import time

import database as db

# Functions that sit between the real caller and the ledger. The reason
# inference skips these, otherwise everything would be "gamble.add_cash".
_WRAPPERS = {
    'adjust', 'apply', 'balance', 'credit', 'debit', 'take', 'record',
    '_infer_reason', 'ensure_row', 'set_balance', '_spec', '_ledger',
    'add_cash', 'take_cash', 'take_cash_upto', 'set_cash', 'credit_cash',
    'debit_cash', 'cash_of', 'ensure_eco',
}


class InsufficientFunds(Exception):
    """Raised when a guarded spend cannot be covered."""

    def __init__(self, gid, uid, amount, balance, currency='coins'):
        super().__init__('insufficient funds')
        self.gid, self.uid = gid, uid
        self.amount, self.balance = amount, balance
        self.currency = currency


class Currency:
    """Where one currency lives and how to make sure the row exists."""

    __slots__ = ('key', 'table', 'column', 'row_sql', 'row_args')

    def __init__(self, key, table, column, row_sql='', row_args=()):
        self.key = key
        self.table = table
        self.column = column
        self.row_sql = row_sql
        self.row_args = tuple(row_args)


def _start_cash() -> int:
    try:
        from utils.economy import START_CASH
        return int(START_CASH)
    except Exception:
        return 1000


# The currency registry. Adding a currency here is the only step needed to
# make it ledgerable everywhere.
CURRENCIES = {
    'coins': Currency('coins', 'eco', 'cash',
                      'INSERT OR IGNORE INTO eco (guild_id,user_id,cash) VALUES(?,?,?)',
                      (_start_cash(),)),
    'bank': Currency('bank', 'eco', 'bank',
                     'INSERT OR IGNORE INTO eco (guild_id,user_id,cash) VALUES(?,?,?)',
                     (_start_cash(),)),
    'casino_tickets': Currency('casino_tickets', 'casino_tix', 'tickets',
                               'INSERT OR IGNORE INTO casino_tix (guild_id,user_id,tickets) VALUES(?,?,0)'),
    'card_tickets': Currency('card_tickets', 'card_tickets', 'tickets',
                             'INSERT OR IGNORE INTO card_tickets (guild_id,user_id) VALUES(?,?)'),
    'ssr_tickets': Currency('ssr_tickets', 'card_tickets', 'ssr_tickets',
                            'INSERT OR IGNORE INTO card_tickets (guild_id,user_id) VALUES(?,?)'),
    'dust': Currency('dust', 'anime_dust', 'dust',
                     'INSERT OR IGNORE INTO anime_dust (guild_id,user_id,dust) VALUES(?,?,0)'),
}
# NOTE: the guild treasury is NOT here. It lives in guild_treasury, which has
# no user_id column, so the wallet's generic (guild_id, user_id) shape does
# not fit it. services/treasury.py owns that table and writes its own
# ledger rows with user_id = 'guild:<id>'.


def _spec(currency: str) -> Currency:
    c = CURRENCIES.get(currency)
    if c is None:
        raise ValueError(f'unknown currency: {currency!r}')
    return c


def _infer_reason() -> str:
    """Stable machine key for the caller's function.

    Walks up past the wallet's own plumbing and the legacy cash wrappers
    and names the first real caller, e.g. 'gamble.slots_cmd'. Explicit
    reasons always win; this only fills the gaps.
    """
    try:
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 14:
            name = frame.f_code.co_name
            mod = (frame.f_globals or {}).get('__name__', '') or ''
            if name not in _WRAPPERS:
                if mod.startswith('cogs.') or mod.startswith('services.'):
                    return f'{mod.split(".")[-1]}.{name}'
                if name == '<module>':
                    # top-level script code: name it after the module
                    return (mod or 'script').split('.')[-1]
                if not name.startswith('<'):
                    return name
            frame = frame.f_back
            depth += 1
    except Exception:
        pass
    return 'misc'


# ---- primitives ------------------------------------------------------
def ensure_row(conn, gid, uid, currency: str = 'coins') -> None:
    s = _spec(currency)
    if not s.row_sql:
        return
    args = (str(gid), str(uid)) + s.row_args
    if s.table == 'guild_treasury':
        args = (str(gid), int(time.time()))
    conn.execute(s.row_sql, args)


def balance(conn, gid, uid, currency: str = 'coins') -> int:
    s = _spec(currency)
    ensure_row(conn, gid, uid, currency)
    row = conn.execute(
        f'SELECT {s.column} FROM {s.table} WHERE guild_id=? AND user_id=?',
        (str(gid), str(uid))).fetchone()
    return int(row[s.column] or 0) if row else 0


def record(conn, gid, uid, currency: str, delta: int, bal: int,
           reason: str, ref: str = '') -> None:
    """Append one ledger row. Same transaction as the balance change."""
    conn.execute(
        'INSERT INTO eco_ledger (guild_id,user_id,currency,delta,balance,reason,ref,at) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (str(gid), str(uid), currency, int(delta), int(bal),
         reason or 'misc', str(ref or '')[:120], int(time.time())))


def _cap_headroom(conn, gid, uid, currency: str) -> int:
    """How much more this player can hold in `currency` without breaking
    the coin ceiling.

    Cash and the bank share ONE ceiling. The number a player can hold is
    the number they can spend, so splitting the cap across two columns
    would let a player hold twice the ceiling without either column
    looking wrong.
    """
    from utils.economy import COIN_CAP
    if currency not in ('coins', 'bank'):
        return COIN_CAP
    row = conn.execute('SELECT cash, bank FROM eco WHERE guild_id=? AND user_id=?',
                       (str(gid), str(uid))).fetchone()
    if not row:
        return COIN_CAP
    held = int(row['cash'] or 0) + int(row['bank'] or 0)
    return max(0, COIN_CAP - held)


def adjust(conn, gid, uid, delta: int, reason: str = None, ref: str = '',
           currency: str = 'coins', floor: int = None) -> int:
    """Signed change. Returns the new balance.

    floor=None lets the balance go negative (the legacy `add_cash` shape,
    which several call sites rely on). floor=0 clamps at zero.

    A CREDIT is clamped to the coin ceiling. The overflow is not lost
    silently: it is written to the ledger as a 'coin_cap' row, so the
    flow analytics can see the ceiling doing its job.
    """
    delta = int(delta)
    s = _spec(currency)
    ensure_row(conn, gid, uid, currency)
    if delta == 0:
        return balance(conn, gid, uid, currency)
    if delta > 0:
        room = _cap_headroom(conn, gid, uid, currency)
        if delta > room:
            lost = delta - room
            delta = room
            record(conn, gid, uid, currency, -lost, 0, 'coin_cap', ref)
    if floor is not None and delta < 0:
        cur_bal = balance(conn, gid, uid, currency)
        if cur_bal + delta < floor:
            delta = floor - cur_bal
            if delta == 0:
                return cur_bal
    conn.execute(
        f'UPDATE {s.table} SET {s.column}={s.column}+? WHERE guild_id=? AND user_id=?',
        (delta, str(gid), str(uid)))
    bal = balance(conn, gid, uid, currency)
    record(conn, gid, uid, currency, delta, bal, reason or _infer_reason(), ref)
    return bal


def take(conn, gid, uid, amount: int, reason: str = None, ref: str = '',
         currency: str = 'coins') -> int:
    """Guarded spend. Raises InsufficientFunds when it cannot be covered."""
    amount = int(amount)
    s = _spec(currency)
    ensure_row(conn, gid, uid, currency)
    cur = conn.execute(
        f'UPDATE {s.table} SET {s.column}={s.column}-? '
        f'WHERE guild_id=? AND user_id=? AND {s.column}>=?',
        (amount, str(gid), str(uid), amount))
    if (cur.rowcount or 0) != 1:
        raise InsufficientFunds(str(gid), str(uid), amount,
                                balance(conn, gid, uid, currency), currency)
    bal = balance(conn, gid, uid, currency)
    record(conn, gid, uid, currency, -amount, bal, reason or _infer_reason(), ref)
    return bal


def set_balance(conn, gid, uid, new_value: int, reason: str = 'set',
                ref: str = '', currency: str = 'coins') -> int:
    """Set a balance outright. The delta is ledgered so the trail is complete."""
    s = _spec(currency)
    ensure_row(conn, gid, uid, currency)
    old = balance(conn, gid, uid, currency)
    new_value = int(new_value)
    if currency in ('coins', 'bank'):
        room = _cap_headroom(conn, gid, uid, currency)
        if new_value - old > room:
            new_value = old + room
    conn.execute(
        f'UPDATE {s.table} SET {s.column}=? WHERE guild_id=? AND user_id=?',
        (new_value, str(gid), str(uid)))
    record(conn, gid, uid, currency, new_value - old, new_value, reason, ref)
    return new_value


def transfer(conn, gid, from_uid, to_uid, amount: int, reason: str = 'transfer',
             ref: str = '') -> int:
    """Move money between two wallets. Both sides are ledgered."""
    amount = int(amount)
    take(conn, gid, from_uid, amount, reason=reason, ref=ref)
    adjust(conn, gid, to_uid, amount, reason=reason, ref=ref)
    return amount


# ---- convenience wrappers (own their own transaction) ----------------
def _guard_nested():
    """Refuse to open a second connection while a write is already open.

    SQLite allows exactly one writer. A second connection writing while the
    caller still holds a write transaction blocks on busy_timeout for 30
    SECONDS and then raises 'database is locked' -- and because this runs
    on the event loop, the whole bot freezes for those 30 seconds. It has
    already happened three times (`.rob` bounty, `.pardon`, the pokemon NPC
    duel prize). Fail loudly and instantly instead.
    """
    if db.write_lock_held():
        raise RuntimeError(
            'wallet: a write transaction is already open. Pass conn= and let '
            'the wallet use the connection the caller already holds - opening '
            'a second one would block on the SQLite write lock for 30 seconds.')


def apply(gid, uid, delta: int, reason: str = None, ref: str = '',
          currency: str = 'coins', floor: int = None, conn=None) -> int:
    if conn is not None:
        return adjust(conn, gid, uid, delta, reason=reason, ref=ref,
                      currency=currency, floor=floor)
    _guard_nested()
    with db.conn_ctx() as c:
        return adjust(c, gid, uid, delta, reason=reason, ref=ref,
                      currency=currency, floor=floor)


def spend(gid, uid, amount: int, reason: str = None, ref: str = '',
          currency: str = 'coins', conn=None) -> int:
    if conn is not None:
        return take(conn, gid, uid, amount, reason=reason, ref=ref, currency=currency)
    _guard_nested()
    with db.conn_ctx() as c:
        return take(c, gid, uid, amount, reason=reason, ref=ref, currency=currency)


def get(gid, uid, currency: str = 'coins') -> int:
    with db.conn_ctx() as conn:
        return balance(conn, gid, uid, currency)

"""The guild treasury: server-wide progression.

An economy made only of per-player wallets gives a player no reason to care
about the server beyond their own balance. The treasury gives the guild its
own track: members pay in (a tax on grind windfalls, or a donation), the
guild buys upgrades, and every member gets the buff.

Two deliberate decisions:

  * The treasury is NOT a wallet row, so it lives in its own table and
    writes its own ledger rows with user_id = 'guild:<id>'. The ledger
    still sees every coin, so the flow analytics cover the guild pool too.
  * The tax never touches casino wins. The casino already carries a
    carefully balanced house edge (tools/economy_rtp.py guards the RTP band
    at 86-95%); a rake on top would be double-dipping and would silently
    push the realized RTP out of band. The tax applies to grind windfalls
    instead, which is where an extra sink is actually needed.
"""
import time

import database as db

GUILD_KEY = 'guild'


def _row(gid):
    return f'guild:{gid}'


def ensure(conn, gid) -> None:
    conn.execute('INSERT OR IGNORE INTO guild_treasury (guild_id, balance, created) '
                 'VALUES (?,0,?)', (str(gid), int(time.time())))


def balance(gid) -> int:
    with db.conn_ctx() as conn:
        ensure(conn, gid)
        row = conn.execute('SELECT balance FROM guild_treasury WHERE guild_id=?',
                           (str(gid),)).fetchone()
    return int(row['balance'] or 0) if row else 0


def _ledger(conn, gid, delta: int, bal: int, reason: str, ref: str = '') -> None:
    conn.execute(
        'INSERT INTO eco_ledger (guild_id,user_id,currency,delta,balance,reason,ref,at) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (str(gid), _row(gid), 'treasury', int(delta), int(bal),
         reason or 'treasury', str(ref or '')[:120], int(time.time())))


def deposit(conn, gid, amount: int, reason: str = 'treasury_in', ref: str = '') -> int:
    """Add to the pool. The caller owns the transaction."""
    amount = int(amount)
    if amount <= 0:
        return balance(gid)
    ensure(conn, gid)
    conn.execute('UPDATE guild_treasury SET balance=balance+? WHERE guild_id=?',
                 (amount, str(gid)))
    bal = int(conn.execute('SELECT balance FROM guild_treasury WHERE guild_id=?',
                           (str(gid),)).fetchone()['balance'] or 0)
    _ledger(conn, gid, amount, bal, reason, ref)
    return bal


def spend(conn, gid, amount: int, reason: str = 'treasury_out', ref: str = '') -> int:
    """Take out of the pool. The caller owns the transaction."""
    amount = int(amount)
    if amount <= 0:
        return balance(gid)
    ensure(conn, gid)
    cur = conn.execute('UPDATE guild_treasury SET balance=balance-? '
                       'WHERE guild_id=? AND balance>=?', (amount, str(gid), amount))
    if (cur.rowcount or 0) != 1:
        raise ValueError('treasury cannot cover that')
    bal = int(conn.execute('SELECT balance FROM guild_treasury WHERE guild_id=?',
                           (str(gid),)).fetchone()['balance'] or 0)
    _ledger(conn, gid, -amount, bal, reason, ref)
    return bal


def tax_on(amount: int) -> int:
    """How much tax a windfall of `amount` owes. Zero below the threshold."""
    from utils.economy import TAX_THRESHOLD, TAX_RATE, TAX_CAP
    amount = int(amount)
    if amount <= TAX_THRESHOLD:
        return 0
    return min(int(amount * TAX_RATE), TAX_CAP)


def collect(gid, uid, amount: int, reason: str = 'tax') -> int:
    """Take the tax on a windfall and move it into the guild pool.

    Returns the tax actually charged (0 when the windfall is too small, or
    when the player cannot cover it — a tax must never create a debt).
    """
    from services.wallet import take
    owed = tax_on(amount)
    if owed <= 0:
        return 0
    with db.conn_ctx() as conn:
        try:
            take(conn, gid, uid, owed, reason=f'{reason}_tax', ref=str(gid))
        except Exception:
            return 0
        deposit(conn, gid, owed, reason='tax_in', ref=reason)
    return owed


def levels(gid) -> dict:
    """Current level of every upgrade."""
    from utils.economy import GUILD_UPGRADES
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT key, level FROM guild_upgrades WHERE guild_id=?',
                            (str(gid),)).fetchall()
    have = {r['key']: int(r['level'] or 0) for r in rows}
    return {k: have.get(k, 0) for k in GUILD_UPGRADES}


def next_cost(gid, key: str):
    """Cost of the next level of an upgrade, or None when it is maxed."""
    from utils.economy import GUILD_UPGRADES
    spec = GUILD_UPGRADES.get(key)
    if not spec:
        return None
    lv = levels(gid).get(key, 0)
    if lv >= spec['max']:
        return None
    return spec['costs'][lv]


def buy(gid, key: str, pay_from: str = 'treasury'):
    """Buy the next level of an upgrade.

    pay_from='treasury' spends the guild pool; pay_from='wallet:<uid>'
    charges one member personally (so a member can buy the upgrade solo).
    Returns (ok, message_key, extra).
    """
    from utils.economy import GUILD_UPGRADES
    spec = GUILD_UPGRADES.get(key)
    if not spec:
        return False, 'treasury.no_upgrade', {}
    cost = next_cost(gid, key)
    if cost is None:
        return False, 'treasury.maxed', {}
    lv = levels(gid).get(key, 0)
    with db.conn_ctx() as conn:
        try:
            if pay_from == 'treasury':
                spend(conn, gid, cost, reason='upgrade', ref=key)
            else:
                from services.wallet import take
                take(conn, gid, pay_from.split(':', 1)[1], cost,
                     reason='upgrade_buy', ref=key)
                deposit(conn, gid, cost, reason='upgrade_buy', ref=key)
        except Exception:
            return False, 'treasury.poor', {'cost': cost}
        conn.execute('INSERT INTO guild_upgrades (guild_id,key,level,at) VALUES(?,?,?,?) '
                     'ON CONFLICT(guild_id,key) DO UPDATE SET level=?, at=?',
                     (str(gid), key, lv + 1, int(time.time()), lv + 1, int(time.time())))
    return True, 'treasury.bought', {'cost': cost, 'level': lv + 1}


def bonuses(gid) -> dict:
    """The buff every member currently gets from the guild's upgrades."""
    from utils.economy import GUILD_UPGRADES
    lv = levels(gid)
    out = {'chance': 0.0, 'interest': 0.0, 'daily_mult': 0.0}
    for key, spec in GUILD_UPGRADES.items():
        effect = spec.get('effect')
        if effect in out:
            out[effect] += lv.get(key, 0) * float(spec.get('per_level', 0))
    return out


def summary(gid) -> dict:
    """Everything the guild treasury panel needs, in one call."""
    from utils.economy import GUILD_UPGRADES
    lv = levels(gid)
    return {
        'balance': balance(gid),
        'levels': lv,
        'bonuses': bonuses(gid),
        'next': {k: next_cost(gid, k) for k in GUILD_UPGRADES},
        'specs': GUILD_UPGRADES,
    }

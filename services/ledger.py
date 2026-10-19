"""The ledger's read side: statements, flow analytics, net worth.

The write side lives in services/wallet.py. This module only ever asks
questions, and every question is answered from the ledger rather than from
a guess about what the economy "should" look like.

`flow()` is the important one. It answers the question an economy designer
actually has to answer — is money being created or destroyed? — and
tools/economy_balance.py turns that answer into a build-time guard rail the
same way tools/economy_rtp.py guards the casino.

Internal transfers (cash to bank and back) carry a reason starting with
'xfer_' and are excluded from every inflow/outflow figure. Counting them
would make a player who banks their money look like they are burning it,
and would make the guild economy look like it is collapsing.
"""
import time

import database as db


def _since(days: int) -> int:
    return int(time.time()) - int(days) * 86400


def recent(gid, uid, limit: int = 15, currency: str = None) -> list:
    """The last N ledger rows for one wallet, newest first."""
    sql = ('SELECT id, currency, delta, balance, reason, ref, at FROM eco_ledger '
           'WHERE guild_id=? AND user_id=?')
    args = [str(gid), str(uid)]
    if currency:
        sql += ' AND currency=?'
        args.append(currency)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(int(limit))
    with db.conn_ctx() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def reasons(gid, uid, days: int = 30, limit: int = 12) -> list:
    """Where the money came from and went, grouped by reason."""
    with db.conn_ctx() as conn:
        rows = conn.execute(
            'SELECT reason, '
            'SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS inflow, '
            'SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS outflow, '
            'COUNT(*) AS n '
            'FROM eco_ledger WHERE guild_id=? AND user_id=? AND at>=? '
            "AND reason NOT LIKE 'xfer_%' "
            'GROUP BY reason ORDER BY (inflow+outflow) DESC LIMIT ?',
            (str(gid), str(uid), _since(days), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['net'] = int(d['inflow'] or 0) - int(d['outflow'] or 0)
        out.append(d)
    return out


def statement(gid, uid, days: int = 30) -> dict:
    """A personal statement: money in, money out, net, and the top reasons."""
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT '
            'SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS inflow, '
            'SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS outflow, '
            'COUNT(*) AS moves '
            'FROM eco_ledger WHERE guild_id=? AND user_id=? AND at>=? '
            "AND reason NOT LIKE 'xfer_%'",
            (str(gid), str(uid), _since(days))).fetchone()
        xfer = conn.execute(
            'SELECT SUM(delta) AS d FROM eco_ledger '
            "WHERE guild_id=? AND user_id=? AND at>=? AND reason LIKE 'xfer_%'",
            (str(gid), str(uid), _since(days))).fetchone()
    inflow = int(row['inflow'] or 0) if row else 0
    outflow = int(row['outflow'] or 0) if row else 0
    return {
        'days': int(days),
        'inflow': inflow,
        'outflow': outflow,
        'net': inflow - outflow,
        'moves': int(row['moves'] or 0) if row else 0,
        'transfers': int(xfer['d'] or 0) if xfer else 0,
        'reasons': reasons(gid, uid, days),
    }


def flow(gid, days: int = 30) -> dict:
    """Guild-wide faucet vs sink.

    Positive net means the economy is inflating (money is being printed);
    negative means it is deflating (money is being hoarded or burned).
    Both are survivable; a large drift in either direction is a bug.
    """
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT '
            'SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS inflow, '
            'SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS outflow, '
            'COUNT(*) AS moves, COUNT(DISTINCT user_id) AS users '
            'FROM eco_ledger WHERE guild_id=? AND at>=? '
            "AND reason NOT LIKE 'xfer_%'",
            (str(gid), _since(days))).fetchone()
        top = conn.execute(
            'SELECT reason, '
            'SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS inflow, '
            'SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS outflow '
            'FROM eco_ledger WHERE guild_id=? AND at>=? '
            "AND reason NOT LIKE 'xfer_%' GROUP BY reason "
            'ORDER BY (inflow+outflow) DESC LIMIT 10',
            (str(gid), _since(days))).fetchall()
    inflow = int(row['inflow'] or 0) if row else 0
    outflow = int(row['outflow'] or 0) if row else 0
    return {
        'days': int(days),
        'inflow': inflow,
        'outflow': outflow,
        'net': inflow - outflow,
        'ratio': (inflow / outflow) if outflow else (float('inf') if inflow else 1.0),
        'moves': int(row['moves'] or 0) if row else 0,
        'users': int(row['users'] or 0) if row else 0,
        'reasons': [dict(r) for r in top],
    }


def net_worth(gid, uid) -> dict:
    """Everything a player owns, valued in one number.

    Cash alone was never the truth: a player sitting on 300 000 in bank and
    a UR card was richer than one with 300 000 in cash and nothing else.

    CARDS ARE NOT MONEY. A card is prestige, and pricing it in coins
    would put a nine-digit number straight back on the screen. The
    collection is reported as a COUNT and a rarity rail instead, and the
    money total covers only what can actually be spent.
    """
    from utils.economy import (TICKET_VALUE, CARD_TICKET_VALUE,
                               SSR_TICKET_VALUE)
    g, u = str(gid), str(uid)
    out = {'cash': 0, 'bank': 0, 'stocks': 0, 'tickets': 0,
           'cards': 0, 'card_rarity': {}}
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT cash, bank FROM eco WHERE guild_id=? AND user_id=?',
                           (g, u)).fetchone()
        if row:
            out['cash'] = int(row['cash'] or 0)
            out['bank'] = int(row['bank'] or 0)
        try:
            rows = conn.execute(
                'SELECT p.symbol AS s, p.qty AS q, st.price AS px '
                'FROM portfolio p LEFT JOIN stocks st '
                'ON st.guild_id=p.guild_id AND st.symbol=p.symbol '
                'WHERE p.guild_id=? AND p.user_id=?', (g, u)).fetchall()
            for r in rows:
                out['stocks'] += int(r['q'] or 0) * int(r['px'] or 0)
        except Exception:
            pass
        try:
            rows = conn.execute(
                'SELECT a.rarity AS rar, COUNT(DISTINCT o.card_id) AS n '
                'FROM anime_collection o JOIN anime_cards a ON a.id=o.card_id '
                'WHERE o.guild_id=? AND o.user_id=? GROUP BY a.rarity', (g, u)).fetchall()
            for r in rows:
                rar = (r['rar'] or 'R').upper()
                n = int(r['n'] or 0)
                out['cards'] += n
                out['card_rarity'][rar] = n
        except Exception:
            pass
        try:
            row = conn.execute('SELECT tickets FROM casino_tix WHERE guild_id=? AND user_id=?',
                               (g, u)).fetchone()
            if row:
                out['tickets'] += int(row['tickets'] or 0) * TICKET_VALUE
        except Exception:
            pass
        try:
            row = conn.execute('SELECT tickets, ssr_tickets FROM card_tickets '
                               'WHERE guild_id=? AND user_id=?', (g, u)).fetchone()
            if row:
                out['tickets'] += (int(row['tickets'] or 0) * CARD_TICKET_VALUE
                                   + int(row['ssr_tickets'] or 0) * SSR_TICKET_VALUE)
        except Exception:
            pass
    out['total'] = out['cash'] + out['bank'] + out['stocks'] + out['tickets']
    return out


def top_holders(gid, limit: int = 10) -> list:
    """Cash leaderboard. For net worth see services/ledger.net_worth()."""
    with db.conn_ctx() as conn:
        rows = conn.execute(
            'SELECT user_id, cash FROM eco WHERE guild_id=? ORDER BY cash DESC LIMIT ?',
            (str(gid), int(limit))).fetchall()
    return [dict(r) for r in rows]


def top_net_worth(gid, limit: int = 25) -> list:
    """Net worth for many users, computed with a handful of queries.

    net_worth() per user would be 5 queries each; on a leaderboard that is
    125 queries on the event loop, which is exactly the kind of thing that
    makes a shared host freeze. This does one query per component and
    stitches the results in Python.

    Returns [{'user_id', 'cash', 'bank', 'stocks', 'cards', 'tickets',
    'dust', 'total'}] sorted by total descending.
    """
    from utils.economy import (TICKET_VALUE, CARD_TICKET_VALUE,
                               SSR_TICKET_VALUE)
    g = str(gid)
    acc = {}

    def slot(uid):
        return acc.setdefault(str(uid), {'user_id': str(uid), 'cash': 0, 'bank': 0,
                                         'stocks': 0, 'cards': 0, 'tickets': 0,
                                         'total': 0})

    with db.conn_ctx() as conn:
        for r in conn.execute('SELECT user_id, cash, bank FROM eco WHERE guild_id=?', (g,)):
            s = slot(r['user_id'])
            s['cash'] = int(r['cash'] or 0)
            s['bank'] = int(r['bank'] or 0)
        try:
            for r in conn.execute(
                    'SELECT p.user_id AS u, SUM(p.qty * COALESCE(st.price,0)) AS v '
                    'FROM portfolio p LEFT JOIN stocks st '
                    'ON st.guild_id=p.guild_id AND st.symbol=p.symbol '
                    'WHERE p.guild_id=? GROUP BY p.user_id', (g,)):
                slot(r['u'])['stocks'] = int(r['v'] or 0)
        except Exception:
            pass
        try:
            for r in conn.execute(
                    'SELECT c.user_id AS u, a.rarity AS rar, COUNT(DISTINCT c.card_id) AS n '
                    'FROM anime_collection c JOIN anime_cards a ON a.id=c.card_id '
                    'WHERE c.guild_id=? GROUP BY c.user_id, a.rarity', (g,)):
                slot(r['u'])['cards'] += int(r['n'] or 0)
        except Exception:
            pass
        try:
            for r in conn.execute('SELECT user_id, tickets FROM casino_tix WHERE guild_id=?', (g,)):
                slot(r['user_id'])['tickets'] += int(r['tickets'] or 0) * TICKET_VALUE
        except Exception:
            pass
        try:
            for r in conn.execute('SELECT user_id, tickets, ssr_tickets FROM card_tickets '
                                  'WHERE guild_id=?', (g,)):
                s = slot(r['user_id'])
                s['tickets'] += (int(r['tickets'] or 0) * CARD_TICKET_VALUE
                                 + int(r['ssr_tickets'] or 0) * SSR_TICKET_VALUE)
        except Exception:
            pass
        try:
            for r in conn.execute('SELECT user_id, dust FROM anime_dust WHERE guild_id=?', (g,)):
                pass        # dust is card progression, not money
        except Exception:
            pass
    for s in acc.values():
        s['total'] = (s['cash'] + s['bank'] + s['stocks'] + s['tickets'])
    return sorted(acc.values(), key=lambda s: -s['total'])[:int(limit)]


def movement_count(gid, uid, days: int = 1) -> int:
    """How many ledger rows one player produced. A spam guard for the table."""
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM eco_ledger WHERE guild_id=? AND user_id=? AND at>=?',
            (str(gid), str(uid), _since(days))).fetchone()
    return int(row['n'] or 0) if row else 0

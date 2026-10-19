"""The ledger's read side: statements, flow analytics, net worth.

The write side lives in services/wallet.py. This module only ever asks
questions, and every question is answered from the ledger rather than from
a guess about what the economy "should" look like.

`flow()` is the important one. It answers the question an economy designer
actually has to answer — is money being created or destroyed? — and
tools/economy_balance.py turns that answer into a build-time guard rail the
same way tools/economy_rtp.py guards the casino.
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
            'FROM eco_ledger WHERE guild_id=? AND user_id=? AND at>=?',
            (str(gid), str(uid), _since(days))).fetchone()
    inflow = int(row['inflow'] or 0) if row else 0
    outflow = int(row['outflow'] or 0) if row else 0
    return {
        'days': int(days),
        'inflow': inflow,
        'outflow': outflow,
        'net': inflow - outflow,
        'moves': int(row['moves'] or 0) if row else 0,
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
            'FROM eco_ledger WHERE guild_id=? AND at>=?',
            (str(gid), _since(days))).fetchone()
        top = conn.execute(
            'SELECT reason, '
            'SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS inflow, '
            'SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS outflow '
            'FROM eco_ledger WHERE guild_id=? AND at>=? GROUP BY reason '
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
    Every holding now has a conservative shadow price so `.bal` and the
    leaderboard can compare people who play differently.
    """
    from utils.economy import (CARD_VALUE, TICKET_VALUE, CARD_TICKET_VALUE,
                               SSR_TICKET_VALUE, DUST_RATE)
    g, u = str(gid), str(uid)
    out = {'cash': 0, 'bank': 0, 'stocks': 0, 'cards': 0, 'tickets': 0, 'dust': 0}
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
                'SELECT a.rarity AS rar, SUM(c.qty) AS n '
                'FROM anime_collection c JOIN anime_cards a ON a.id=c.card_id '
                'WHERE c.guild_id=? AND c.user_id=? GROUP BY a.rarity', (g, u)).fetchall()
            for r in rows:
                out['cards'] += CARD_VALUE.get((r['rar'] or 'R').upper(), 0) * int(r['n'] or 0)
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
        try:
            row = conn.execute('SELECT dust FROM anime_dust WHERE guild_id=? AND user_id=?',
                               (g, u)).fetchone()
            if row:
                out['dust'] = int(int(row['dust'] or 0) * DUST_RATE)
        except Exception:
            pass
    out['total'] = (out['cash'] + out['bank'] + out['stocks']
                    + out['cards'] + out['tickets'] + out['dust'])
    return out


def top_holders(gid, limit: int = 10) -> list:
    """Cash leaderboard. For net worth see services/ledger.net_worth()."""
    with db.conn_ctx() as conn:
        rows = conn.execute(
            'SELECT user_id, cash FROM eco WHERE guild_id=? ORDER BY cash DESC LIMIT ?',
            (str(gid), int(limit))).fetchall()
    return [dict(r) for r in rows]


def movement_count(gid, uid, days: int = 1) -> int:
    """How many ledger rows one player produced. A spam guard for the table."""
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM eco_ledger WHERE guild_id=? AND user_id=? AND at>=?',
            (str(gid), str(uid), _since(days))).fetchone()
    return int(row['n'] or 0) if row else 0

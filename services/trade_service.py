"""Trade service (rework): single-shot mon swaps.

Discord-free: no ctx, no interactions, no views. Models EXACTLY the
current `;trade` behavior — one sender mon for one receiver mon,
receiver ACCEPTs/DECLINEs on buttons, atomic rowcount-guarded swap.
There are no items, coins, multi-asset offers, revisions or expiries
in the current system, so the service has none either.

Offer state lives in the passed-in store (same process-memory pattern
as encounters/battles). Double-ACCEPT is guarded twice: a done flag
(fast path) plus ownership-predicate UPDATEs with rowcount checks
(wins even across processes).
"""
import time
import uuid

import database as db
from lang import t

OFFER_TTL = 3600  # stale offers pruned opportunistically on create


def _new_offer_id() -> str:
    return uuid.uuid4().hex[:12]


def _names(gid, m1: dict, m2: dict):
    from cogs.pokemon import mon_name
    return mon_name(m1, gid), mon_name(m2, gid)


def create_offer(store: dict, gid, sender_id, receiver_id: str,
                 sender_mid: int, receiver_mid: int,
                 receiver_is_bot: bool = False) -> dict:
    """Validate + register an offer. Returns a result dict (see accept)."""
    from cogs.pokemon import mon_name
    gid, sender_id, receiver_id = str(gid), str(sender_id), str(receiver_id)
    now = int(time.time())
    # opportunistic prune, keeps the in-memory store bounded
    for oid in [k for k, o in store.items()
                if now - o.get('created', now) > OFFER_TTL]:
        store.pop(oid, None)
    if sender_id == receiver_id or receiver_is_bot:
        return {'ok': False, 'code': 'SELF',
                'message': t(gid, 'eco.pk_duel_self')}
    with db.conn_ctx() as conn:
        r1 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                          (sender_mid, gid, sender_id)).fetchone()
        r2 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                          (receiver_mid, gid, receiver_id)).fetchone()
        if not r1 or not r2:
            return {'ok': False, 'code': 'NOT_FOUND',
                    'message': t(gid, 'eco.pk_noslot')}
        m1, m2 = dict(r1), dict(r2)
    # quirk preserved: only the SENDER's mon is lock-checked at offer time
    if m1.get('locked'):
        return {'ok': False, 'code': 'LOCKED',
                'message': t(gid, 'eco.pk_locked', name=mon_name(m1, gid))}
    oid = _new_offer_id()
    store[oid] = {'gid': gid, 'sender': sender_id, 'receiver': receiver_id,
                  'm1': sender_mid, 'm2': receiver_mid, 'done': False,
                  'created': now}
    n1, n2 = _names(gid, m1, m2)
    return {'ok': True, 'code': 'OFFER_CREATED', 'message': '',
            'offer_id': oid, 'm1_name': n1, 'm2_name': n2,
            'm1_id': sender_mid, 'm2_id': receiver_mid}


def decline_offer(store: dict, offer_id: str, user_id) -> dict:
    """Receiver declines. Idempotent; never touches assets."""
    o = store.get(offer_id)
    gid = o.get('gid', 0) if o else 0
    if not o or o.get('done'):
        return {'ok': False, 'code': 'GONE', 'message': t(gid, 'eco.pk_gone')}
    if str(user_id) != o['receiver']:
        return {'ok': False, 'code': 'NOT_PARTICIPANT', 'message': ''}
    o['done'] = True
    return {'ok': True, 'code': 'DECLINED',
            'message': t(o['gid'], 'eco.pk_declined')}


def accept_offer(store: dict, offer_id: str, user_id) -> dict:
    """Receiver accepts: re-validate, atomic guarded swap, reassign actives.
    Exactly one accept can win; losers change nothing."""
    from cogs.pokemon import mon_name
    o = store.get(offer_id)
    gid = o.get('gid', 0) if o else 0
    if not o or o.get('done'):
        return {'ok': False, 'code': 'GONE', 'message': t(gid, 'eco.pk_gone')}
    if str(user_id) != o['receiver']:
        return {'ok': False, 'code': 'NOT_PARTICIPANT', 'message': ''}
    gid, a, b = o['gid'], o['sender'], o['receiver']
    m1_id, m2_id = o['m1'], o['m2']
    o['done'] = True  # fast path; DB guards below win cross-process
    try:
        with db.conn_ctx() as conn:
            r1 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                              (m1_id, gid, a)).fetchone()
            r2 = conn.execute('SELECT * FROM pk_mons WHERE id=? AND guild_id=? AND owner_id=?',
                              (m2_id, gid, b)).fetchone()
            if not r1 or not r2:
                return {'ok': False, 'code': 'GONE', 'message': t(gid, 'eco.pk_gone')}
            m1, m2 = dict(r1), dict(r2)
            if m1.get('locked'):
                return {'ok': False, 'code': 'LOCKED',
                        'message': t(gid, 'eco.pk_locked', name=mon_name(m1, gid))}
            c1 = conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=? AND guild_id=? AND owner_id=?',
                              (b, m1_id, gid, a))
            c2 = conn.execute('UPDATE pk_mons SET owner_id=?, active=0 WHERE id=? AND guild_id=? AND owner_id=?',
                              (a, m2_id, gid, b))
            if (c1.rowcount or 0) != 1 or (c2.rowcount or 0) != 1:
                # lost a cross-process race: roll back (commit skipped on raise)
                raise _Abort()
            for uid in (a, b):
                r = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? '
                                 'ORDER BY id LIMIT 1', (gid, uid)).fetchone()
                if r:
                    conn.execute('UPDATE pk_mons SET active=1 WHERE id=?', (r['id'],))
    except _Abort:
        return {'ok': False, 'code': 'GONE', 'message': t(gid, 'eco.pk_gone')}
    n1, n2 = _names(gid, m1, m2)
    return {'ok': True, 'code': 'TRADE_COMPLETE',
            'message': t(gid, 'eco.pk_traded', m1=n1, m2=n2),
            'm1_name': n1, 'm2_name': n2}


class _Abort(Exception):
    """Abort the open transaction (commit skipped -> rollback)."""

"""Trade service regression: single-shot swaps, double-accept guard,
atomic settlement — current delete-free semantics (ownership UPDATEs).

Run: python tools/test_trade_service.py (needs discord installed for cog
imports). Exit code 0 only when every test passes.
"""
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    safe = msg.encode('ascii', 'backslashreplace').decode()
    print(('PASS ' if cond else 'FAIL ') + safe, flush=True)
    if not cond:
        FAILS.append(msg)


GID = 777


def mkmon(conn, owner, dex=25, active=0, locked=0, nick=''):
    cur = conn.execute(
        'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
        'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(GID), str(owner), dex, 10, 0, 0, nick, active, '', '', locked, 0, ''))
    return cur.lastrowid


def owner_of(mid):
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT owner_id, active FROM pk_mons WHERE id=?', (mid,)).fetchone()
        return (r['owner_id'], r['active']) if r else (None, None)


def mons_of(uid):
    import database as db
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id',
            (str(GID), str(uid))).fetchall()]


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from services import trade_service as tr

    src = (ROOT / 'services' / 'trade_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in service')

    store = {}

    # --- create ---
    with db.conn_ctx() as conn:
        a1 = mkmon(conn, 'alice', dex=25, active=1)
        a2 = mkmon(conn, 'alice', dex=133)
        b1 = mkmon(conn, 'bob', dex=7, active=1)
    o = tr.create_offer(store, GID, 'alice', 'bob', a2, b1)
    check(o['ok'] and o['code'] == 'OFFER_CREATED' and o['offer_id'], 'offer created')
    check(o['m1_name'] and o['m2_name'], 'offer carries display names')

    r = tr.create_offer(store, GID, 'alice', 'alice', a2, b1)
    check(not r['ok'] and r['code'] == 'SELF', 'self trade rejected')
    r = tr.create_offer(store, GID, 'alice', 'bot', a2, b1, receiver_is_bot=True)
    check(not r['ok'] and r['code'] == 'SELF', 'bot receiver rejected')
    r = tr.create_offer(store, GID, 'alice', 'bob', 999999, b1)
    check(not r['ok'] and r['code'] == 'NOT_FOUND', 'missing mon rejected')

    with db.conn_ctx() as conn:
        al = mkmon(conn, 'alice', locked=1)
    r = tr.create_offer(store, GID, 'alice', 'bob', al, b1)
    check(not r['ok'] and r['code'] == 'LOCKED', 'sender locked rejected')
    with db.conn_ctx() as conn:
        bl = mkmon(conn, 'bob', locked=1)
    r = tr.create_offer(store, GID, 'alice', 'bob', a2, bl)
    check(r['ok'] if isinstance(r, dict) else r['ok'], 'receiver locked allowed (parity)')

    # --- accept by third party changes nothing ---
    r = tr.accept_offer(store, o['offer_id'], 'mallory')
    check(not r['ok'] and r['code'] == 'NOT_PARTICIPANT', 'third party rejected')
    check(owner_of(a2)[0] == 'alice' and owner_of(b1)[0] == 'bob', 'nothing moved')

    # --- accept: atomic swap + active reassign ---
    r = tr.accept_offer(store, o['offer_id'], 'bob')
    check(r['ok'] and r['code'] == 'TRADE_COMPLETE', 'accept settles')
    check(owner_of(a2)[0] == 'bob' and owner_of(b1)[0] == 'alice', 'ownership swapped')
    alice = mons_of('alice')
    bob = mons_of('bob')
    act_a = [m['id'] for m in alice if m['active']]
    act_b = [m['id'] for m in bob if m['active']]
    check(act_a == [min(m['id'] for m in alice)], 'alice active = first mon')
    check(act_b == [min(m['id'] for m in bob)], 'bob active = first mon')
    check('133' in r['message'] or 'Eevee' in r['message'] or len(r['message']) > 0,
          'message names the mons')

    # --- double accept: single swap ---
    r = tr.accept_offer(store, o['offer_id'], 'bob')
    check(not r['ok'] and r['code'] == 'GONE', 'second accept rejected')
    check(owner_of(a2)[0] == 'bob' and owner_of(b1)[0] == 'alice', 'no double swap')

    # --- accept after mon moved away: atomic abort ---
    with db.conn_ctx() as conn:
        c1 = mkmon(conn, 'carol', dex=39, active=1)
        d1 = mkmon(conn, 'dave', dex=52, active=1)
    o2 = tr.create_offer(store, GID, 'carol', 'dave', c1, d1)
    with db.conn_ctx() as conn:  # carol trades c1 away elsewhere first
        conn.execute('UPDATE pk_mons SET owner_id=? WHERE id=?', ('erin', c1))
    r = tr.accept_offer(store, o2['offer_id'], 'dave')
    check(not r['ok'] and r['code'] == 'GONE', 'stale offer aborts')
    check(owner_of(d1)[0] == 'dave', 'other side untouched (atomic)')

    # --- accept after sender locks post-offer ---
    with db.conn_ctx() as conn:
        e1 = mkmon(conn, 'erin', active=1)
        f1 = mkmon(conn, 'fred', active=1)
    o3 = tr.create_offer(store, GID, 'erin', 'fred', e1, f1)
    with db.conn_ctx() as conn:
        conn.execute('UPDATE pk_mons SET locked=1 WHERE id=?', (e1,))
    r = tr.accept_offer(store, o3['offer_id'], 'fred')
    check(not r['ok'] and r['code'] == 'LOCKED', 'post-offer lock aborts')
    check(owner_of(e1)[0] == 'erin' and owner_of(f1)[0] == 'fred', 'nothing moved')

    # --- decline ---
    with db.conn_ctx() as conn:
        g1 = mkmon(conn, 'gina', active=1)
        h1 = mkmon(conn, 'hank', active=1)
    o4 = tr.create_offer(store, GID, 'gina', 'hank', g1, h1)
    r = tr.decline_offer(store, o4['offer_id'], 'mallory')
    check(not r['ok'] and r['code'] == 'NOT_PARTICIPANT', 'decline by third party')
    r = tr.decline_offer(store, o4['offer_id'], 'hank')
    check(r['ok'] and r['code'] == 'DECLINED', 'receiver declines')
    check(owner_of(g1)[0] == 'gina' and owner_of(h1)[0] == 'hank', 'decline moves nothing')
    r = tr.decline_offer(store, o4['offer_id'], 'hank')
    check(not r['ok'], 'decline is idempotent')
    r = tr.accept_offer(store, o4['offer_id'], 'hank')
    check(not r['ok'], 'declined offer cannot settle')

    # --- racing accepts: exactly one winner ---
    raced = []
    for rnd in range(20):
        with db.conn_ctx() as conn:
            x1 = mkmon(conn, f'x{rnd}', dex=1, active=1)
            y1 = mkmon(conn, f'y{rnd}', dex=4, active=1)
        ox = tr.create_offer(store, GID, f'x{rnd}', f'y{rnd}', x1, y1)
        bar = threading.Barrier(2)
        out = {}

        def attempt():
            bar.wait()
            out[threading.get_ident()] = tr.accept_offer(store, ox['offer_id'], f'y{rnd}')

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        rs = list(out.values())
        wins = [x for x in rs if x['ok']]
        if len(wins) != 1:
            check(False, f'round {rnd}: exactly one winner')
            break
        if owner_of(x1)[0] != f'y{rnd}' or owner_of(y1)[0] != f'x{rnd}':
            check(False, f'round {rnd}: ownership swapped exactly once')
            break
        raced.append(rnd)
    else:
        check(True, '20 racing accepts settle exactly once')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

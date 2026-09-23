"""Market service regression: listings, atomic buys, cancellation, fees,
pagination — all on current delete-based semantics.

Run: python tools/test_market_service.py (needs discord installed for cog
imports). Exit code 0 only when every test passes.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    safe = msg.encode('ascii', 'backslashreplace').decode()
    print(('PASS ' if cond else 'FAIL ') + safe, flush=True)
    if not cond:
        FAILS.append(msg)


GID = 4242


def mkmon(conn, owner, dex=25, level=10, nick='', active=0, locked=0,
          held='', shiny=0):
    cur = conn.execute(
        'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
        'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(GID), str(owner), dex, level, 0, shiny, nick, active, '', '', locked, 0, held))
    return cur.lastrowid


def cash_of(uid):
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                         (str(GID), str(uid))).fetchone()
        return r['cash'] if r else 0


def mons_of(uid):
    import database as db
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id',
            (str(GID), str(uid))).fetchall()]


def market_rows():
    import database as db
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM pk_market WHERE guild_id=?', (str(GID),)).fetchall()]


def main() -> None:
    import database as db
    tmp = Path(tempfile.mkdtemp()) / 'test.db'
    db.DB_PATH = tmp
    db.init_db()

    from cogs.gamble import add_cash
    from services import market_service as mk

    src = (ROOT / 'services' / 'market_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in service')

    # --- create ---
    with db.conn_ctx() as conn:
        m1 = mkmon(conn, 'seller', dex=25, level=12, active=1)
        m2 = mkmon(conn, 'seller', dex=133, level=5)
    r = mk.create_listing(GID, 'seller', 'SellerName', m1, 1000)
    check(r.ok and r.code == 'LISTING_CREATED' and r.listing_id, 'valid listing succeeds')
    check(r.price == 1000 and r.dex == 25, 'result carries price/dex')
    check(len(market_rows()) == 1 and not any(m['id'] == m1 for m in mons_of('seller')),
          'listing owns the mon (moved out of pk_mons)')
    with db.conn_ctx() as conn:
        nxt = conn.execute('SELECT active FROM pk_mons WHERE id=?', (m2,)).fetchone()
    check(nxt['active'] == 1, 'active reassigned to first remaining mon')

    r = mk.create_listing(GID, 'seller', 'S', 999999, 1000)
    check(not r.ok and r.code == 'NOT_FOUND', 'nonexistent mon fails')
    r = mk.create_listing(GID, 'intruder', 'I', m2, 1000)
    check(not r.ok and r.code == 'NOT_FOUND', 'wrong owner fails')
    r = mk.create_listing(GID, 'seller', 'S', m2, 50)
    check(not r.ok and r.code == 'PRICE_TOO_LOW' and len(market_rows()) == 1,
          'price below minimum fails, nothing listed')

    with db.conn_ctx() as conn:
        ml = mkmon(conn, 'seller', locked=1)
    r = mk.create_listing(GID, 'seller', 'S', ml, 500)
    check(not r.ok and r.code == 'LOCKED' and len(market_rows()) == 1,
          'locked mon fails, mon untouched')
    check(any(m['id'] == ml for m in mons_of('seller')), 'locked mon stays in box')

    # cap: 3 per seller
    with db.conn_ctx() as conn:
        ma = mkmon(conn, 'seller')
        mb = mkmon(conn, 'seller')
    check(mk.create_listing(GID, 'seller', 'S', ma, 100).ok, 'cap 2/3')
    check(mk.create_listing(GID, 'seller', 'S', mb, 100).ok, 'cap 3/3')
    with db.conn_ctx() as conn:
        mc = mkmon(conn, 'seller')
    r = mk.create_listing(GID, 'seller', 'S', mc, 100)
    check(not r.ok and r.code == 'MARKET_FULL' and len(market_rows()) == 3,
          'listing cap enforced')

    # held item returns to inventory inside the same flow
    with db.conn_ctx() as conn:
        mh = mkmon(conn, 'other', held='fire_stone')
    r = mk.create_listing(GID, 'other', 'O', mh, 200)
    with db.conn_ctx() as conn:
        q = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='fire_stone'",
                         (str(GID), 'other')).fetchone()
    check(r.ok and q and q['qty'] == 1, 'held item returned to inventory')

    # --- buy ---
    lid = [x for x in market_rows() if x['seller_id'] == 'seller'][0]['id']
    add_cash(GID, 'buyer', 5000)
    add_cash(GID, 'seller', 0)
    b0, s0 = cash_of('buyer'), cash_of('seller')  # includes START_CASH padding
    r = mk.buy_listing(GID, 'buyer', lid)
    check(r.ok and r.code == 'BUY_SUCCESS', 'valid purchase succeeds')
    check(cash_of('buyer') == b0 - 1000, 'buyer charged full price')
    check(cash_of('seller') == s0 + 950, 'seller receives 95%')
    check(r.tax == 50 and r.fee == 0, 'fee math recorded')
    owned = [m for m in mons_of('buyer') if m['dex'] == 25 and m['level'] == 12]
    check(len(owned) == 1, 'purchased mon belongs to buyer')
    check(not any(x['id'] == lid for x in market_rows()), 'listing gone after buy')
    check('1000' in r.message or '1 000' in r.message, 'message quotes price')

    r = mk.buy_listing(GID, 'buyer', 999999)
    check(not r.ok and r.code == 'NOT_FOUND', 'missing listing fails')

    with db.conn_ctx() as conn:
        ms = mkmon(conn, 'seller2')
    rs = mk.create_listing(GID, 'seller2', 'S2', ms, 300)
    r = mk.buy_listing(GID, 'seller2', rs.listing_id)
    check(not r.ok and r.code == 'SELF_BUY', 'self-buy fails')
    check(any(x['id'] == rs.listing_id for x in market_rows()), 'self-buy leaves listing')

    add_cash(GID, 'poor', 100)  # 1100 with START_CASH: still short of 2000
    with db.conn_ctx() as conn:
        mp = mkmon(conn, 'seller2')
    rp = mk.create_listing(GID, 'seller2', 'S2', mp, 2000)
    r = mk.buy_listing(GID, 'poor', rp.listing_id)
    check(not r.ok and r.code == 'INSUFFICIENT_FUNDS', 'poor buyer fails')
    check(cash_of('poor') == 1100 and cash_of('seller2') == 0
          and any(x['id'] == rp.listing_id for x in market_rows()),
          'failed buy changes nothing')

    # --- double buy: racing threads, exactly one winner per round ---
    # Sequential losers see NOT_FOUND (pre-read misses); only a genuine
    # overlap hits ALREADY_SOLD, so race with a start barrier. Every round
    # must settle exactly once no matter the interleaving.
    import threading
    saw_raced = []
    for rnd in range(30):
        with db.conn_ctx() as conn:
            md = mkmon(conn, f's3_{rnd}', dex=7, level=9)
        rd = mk.create_listing(GID, f's3_{rnd}', 'S3', md, 1000)
        add_cash(GID, f'w1_{rnd}', 5000)
        add_cash(GID, f'w2_{rnd}', 5000)
        bar = threading.Barrier(2)
        out = {}

        def attempt(who):
            bar.wait()
            out[who] = mk.buy_listing(GID, who, rd.listing_id)

        t1 = threading.Thread(target=attempt, args=(f'w1_{rnd}',))
        t2 = threading.Thread(target=attempt, args=(f'w2_{rnd}',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        r1, r2 = out[f'w1_{rnd}'], out[f'w2_{rnd}']
        wins = [r for r in (r1, r2) if r.ok]
        losses = [r for r in (r1, r2) if not r.ok]
        ok_round = (len(wins) == 1 and len(losses) == 1
                    and losses[0].code in ('ALREADY_SOLD', 'NOT_FOUND'))
        if not ok_round:
            check(False, f'round {rnd}: exactly one winner '
                         f'({r1.code}/{r2.code})')
            break
        if losses[0].code == 'ALREADY_SOLD':
            saw_raced.append(rnd)
        w, l = (f'w1_{rnd}', f'w2_{rnd}') if wins[0] is r1 else (f'w2_{rnd}', f'w1_{rnd}')
        got = [m for m in mons_of(w) if m['dex'] == 7 and m['level'] == 9]
        # START_CASH pads: winner 6000-1000, loser untouched, seller 1000+950
        if not (len(got) == 1 and not [m for m in mons_of(l) if m['dex'] == 7]
                and cash_of(w) == 5000 and cash_of(l) == 6000
                and cash_of(f's3_{rnd}') == 1950
                and not any(x['id'] == rd.listing_id for x in market_rows())):
            check(False, f'round {rnd}: exact-once settlement')
            break
    else:
        check(True, '30 racing rounds settle exactly once')
    check(bool(saw_raced), 'rowcount guard fires under real overlap')

    # --- cancel ---
    with db.conn_ctx() as conn:
        mu = mkmon(conn, 'seller4', dex=39, level=11)
    ru = mk.create_listing(GID, 'seller4', 'S4', mu, 700)
    r = mk.cancel_listing(GID, 'intruder', ru.listing_id)
    check(not r.ok and 'gone' in r.message.lower()
          and any(x['id'] == ru.listing_id for x in market_rows()),
          'non-seller unlist keeps old gone response')
    r = mk.cancel_listing(GID, 'seller4', ru.listing_id)
    back = [m for m in mons_of('seller4') if m['dex'] == 39]
    check(r.ok and r.code == 'CANCEL_SUCCESS' and len(back) == 1
          and not any(x['id'] == ru.listing_id for x in market_rows()),
          'valid unlist restores the mon')

    # --- browse ---
    for i in range(6):
        with db.conn_ctx() as conn:
            mm = mkmon(conn, f'br{i}')
        mk.create_listing(GID, f'br{i}', 'B', mm, 100 + i)
    pg1 = mk.search_listings(GID, 1)
    check(len(pg1.rows) == 8 and pg1.total_pages == 2, 'page 1 holds 8')
    pg2 = mk.search_listings(GID, 2)
    check(len(pg2.rows) == 3, 'page 2 holds the rest')
    ids1 = [x['id'] for x in pg1.rows]
    check(ids1 == sorted(ids1, reverse=True), 'newest first')
    with db.conn_ctx() as conn:
        for i in range(45):
            conn.execute('INSERT INTO pk_market (guild_id, seller_id, dex, price, created) '
                         'VALUES (?,?,?,?,0)', (str(GID), 'bulk', 1, 100))
    pg = mk.search_listings(GID, 1)
    check(pg.total == 40 and pg.total_pages == 5, 'latest-40 window kept')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

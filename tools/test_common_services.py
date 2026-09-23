"""Shared asset primitives regression: wallet, inventory, ownership,
active reassignment — plus full-suite reruns live in CI order here.

Run: python tools/test_common_services.py (needs discord installed for
cog imports). Exit code 0 only when every test passes.
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


GID = 31337


def mkmon(conn, owner, active=0):
    cur = conn.execute(
        'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
        'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(GID), str(owner), 25, 10, 0, 0, '', active, '', '', 0, 0, ''))
    return cur.lastrowid


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from services import _common as c

    src = (ROOT / 'services' / '_common.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in common')
    check('commit()' not in src and 'rollback()' not in src,
          'helpers never commit/rollback')

    # --- wallet ---
    with db.conn_ctx() as conn:
        c.ensure_eco(conn, GID, 'u1')
        check(c.cash_of(conn, GID, 'u1') == 1000, 'ensure seeds START_CASH')
        check(c.credit_cash(conn, GID, 'u1', 500) == 1500, 'credit returns balance')
        check(c.debit_cash(conn, GID, 'u1', 200) == 1300, 'debit returns balance')
        try:
            c.debit_cash(conn, GID, 'u1', 99999)
            check(False, 'debit rejects insufficient')
        except c.InsufficientCashError as e:
            check(e.balance == 1300 and e.amount == 99999, 'insufficient carries figures')
        check(c.cash_of(conn, GID, 'u1') == 1300, 'failed debit changes nothing')

    # --- inventory ---
    with db.conn_ctx() as conn:
        check(c.upsert_item(conn, GID, 'u1', 'poke', 3) == 3, 'upsert creates row')
        check(c.upsert_item(conn, GID, 'u1', 'poke', 2) == 5, 'upsert increments')
        check(c.remove_item(conn, GID, 'u1', 'poke', 2) == 3, 'remove decrements')
        try:
            c.remove_item(conn, GID, 'u1', 'poke', 99)
            check(False, 'remove rejects insufficient')
        except c.InsufficientItemError as e:
            check(e.have == 3, 'insufficient carries have')
        q = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='poke'",
                         (str(GID), 'u1')).fetchone()['qty']
        check(q == 3, 'failed remove keeps qty')
        check(c.remove_item(conn, GID, 'u1', 'poke', 3) == 0, 'remove to zero')
        gone = conn.execute("SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball='poke'",
                            (str(GID), 'u1')).fetchone()['qty']
        check(gone == 0, 'zero-qty rows kept (current behavior)')

    # --- ownership ---
    with db.conn_ctx() as conn:
        m1 = mkmon(conn, 'alice')
        m2 = mkmon(conn, 'alice')
        got = c.get_owned_pokemon(conn, GID, 'alice', m1)
        check(got and got['id'] == m1, 'owned read hits')
        check(c.get_owned_pokemon(conn, GID, 'bob', m1) is None, 'other owner misses')
        check(c.get_owned_pokemon(conn, GID, 'alice', 999999) is None, 'missing misses')
        c.transfer_pokemon(conn, GID, m1, 'alice', 'bob')
        check(c.get_owned_pokemon(conn, GID, 'bob', m1)['id'] == m1, 'transfer moves owner')
        check(c.get_owned_pokemon(conn, GID, 'alice', m1) is None, 'old owner loses it')
        try:
            c.transfer_pokemon(conn, GID, m1, 'alice', 'bob')
            check(False, 'wrong-owner transfer fails')
        except c.OwnershipError as e:
            check(e.pokemon_id == m1, 'ownership error names mon')

    # --- active ---
    with db.conn_ctx() as conn:
        a1 = mkmon(conn, 'carol')
        a2 = mkmon(conn, 'carol')
        check(c.reassign_active(conn, GID, 'carol') == min(a1, a2), 'first-by-id wins')
        act = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? AND active=1',
                           (str(GID), 'carol')).fetchall()
        check(len(act) == 1 and act[0]['id'] == min(a1, a2), 'exactly one active')
        check(c.reassign_active(conn, GID, 'nobody') is None, 'empty box -> None')

    # --- rollback safety: helper failure inside a caller txn restores all ---
    with db.conn_ctx() as conn:
        r1 = mkmon(conn, 'dave')
    try:
        with db.conn_ctx() as conn:
            c.transfer_pokemon(conn, GID, r1, 'dave', 'erin')
            c.debit_cash(conn, GID, 'erin', 999999)
    except c.InsufficientCashError:
        pass
    with db.conn_ctx() as conn:
        still = conn.execute('SELECT owner_id FROM pk_mons WHERE id=?', (r1,)).fetchone()
    check(still['owner_id'] == 'dave', 'caller txn rolls back on helper failure')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

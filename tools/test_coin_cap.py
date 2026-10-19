"""The coin ceiling â€” the thing that gives money its weight.

COIN_CAP is 99,999. It is enforced in the wallet, so no source of income
can print a six-digit balance, and the overflow is written to the ledger
as a 'coin_cap' row so the flow analytics can see the ceiling working.
"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

tmp = Path(tempfile.gettempdir()) / 'cap_test.db'
for s in ('', '-wal', '-shm'):
    Path(str(tmp) + s).unlink(missing_ok=True)

import database as db
db.DB_PATH = tmp

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS eco (guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
        cash INTEGER DEFAULT 1000, bank INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id,user_id))''',
    '''CREATE TABLE IF NOT EXISTS eco_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'coins', delta INTEGER NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT 'misc',
        ref TEXT DEFAULT '', at INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS guild_treasury (guild_id TEXT PRIMARY KEY,
        balance INTEGER DEFAULT 0, created INTEGER DEFAULT 0)''',
]
with db.conn_ctx() as conn:
    for s in SCHEMA:
        conn.execute(s)

from services import wallet, treasury
from utils.economy import COIN_CAP, START_CASH

G, U = '1', 'u1'
fails = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [ok]   {label}')
    else:
        print(f'  [FAIL] {label} {extra}')
        fails.append(label)


print('--- the ceiling holds ---')
wallet.apply(G, U, COIN_CAP * 3, reason='windfall')
bal = wallet.get(G, U)
check('a credit cannot exceed the ceiling', bal == COIN_CAP, bal)

print('--- cash and the bank share ONE ceiling ---')
wallet.apply(G, U, -COIN_CAP, reason='spend')          # back to zero
wallet.apply(G, U, COIN_CAP - 100, reason='cash')
wallet.apply(G, U, 500, reason='bank')
check('the bank cannot push the pair past the cap',
      wallet.get(G, U) + wallet.get(G, U, 'bank') <= COIN_CAP,
      (wallet.get(G, U), wallet.get(G, U, 'bank')))

print('--- the overflow is recorded, not silently swallowed ---')
with db.conn_ctx() as conn:
    rows = conn.execute(
        "SELECT delta, reason FROM eco_ledger WHERE user_id=? AND reason='coin_cap'",
        (U,)).fetchall()
check('the cap wrote a ledger row', len(rows) >= 1, [dict(r) for r in rows])
if rows:
    check('the row records the amount that did not fit', int(rows[0]['delta']) < 0,
          dict(rows[0]))

print('--- a debit is not clamped ---')
wallet.apply(G, U, -COIN_CAP, reason='spend')
wallet.apply(G, U, 10, reason='x')
b = wallet.apply(G, U, -50, reason='legacy_negative')
check('the legacy negative balance still works', b == -40, b)

print('--- set_balance is clamped too ---')
with db.conn_ctx() as conn:
    wallet.set_balance(conn, G, U, COIN_CAP * 10, reason='set')
check('set_balance respects the ceiling', wallet.get(G, U) <= COIN_CAP, wallet.get(G, U))

print('--- the guild pool obeys it too ---')
with db.conn_ctx() as conn:
    treasury.deposit(conn, G, COIN_CAP * 5, reason='tax_in')
check('the treasury is capped', treasury.balance(G) <= COIN_CAP, treasury.balance(G))

print('--- the cap is below the old scale, so the numbers stay small ---')
check('the ceiling is five digits', COIN_CAP == 99_999, COIN_CAP)
check('a fresh wallet is a small number', 0 < START_CASH < 1000, START_CASH)

print()
if fails:
    print(f'FAILURES ({len(fails)}): ' + ', '.join(fails))
    raise SystemExit(1)
print('COIN CEILING: ALL TESTS PASS')


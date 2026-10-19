"""The automod config cache must actually drop.

Regression: `_cfg_drop` used to call ITSELF. That raised RecursionError
inside the caller's `with db.conn_ctx()` block, and an exception there
rolls the transaction back - so `.automod toggle`, `.badword-add`,
`.badword-remove`, `.anti-invite` and `.anti-link` all appeared to work
and silently saved NOTHING.

The native Discord automod mirror reads that same table, so the whole
server-side feature was dead behind it.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

tmp = Path(tempfile.gettempdir()) / 'automod_cfg_test.db'
for s in ('', '-wal', '-shm'):
    Path(str(tmp) + s).unlink(missing_ok=True)

import database as db
db.DB_PATH = tmp
with db.conn_ctx() as conn:
    conn.execute('''CREATE TABLE IF NOT EXISTS automod (
        guild_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,
        anti_invite INTEGER DEFAULT 0, anti_link INTEGER DEFAULT 0,
        badwords TEXT DEFAULT '', whitelist_channels TEXT DEFAULT '',
        whitelist_roles TEXT DEFAULT '')''')

from cogs import automod as am

fails = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [ok]   {label}')
    else:
        print(f'  [FAIL] {label} {extra}')
        fails.append(label)


GID = 4242
print('--- the drop does not recurse ---')
try:
    am._cfg_drop(GID)
    check('_cfg_drop returns without recursing', True)
except RecursionError:
    check('_cfg_drop returns without recursing', False, 'RecursionError')
except Exception as e:
    check('_cfg_drop returns without recursing', False, f'{type(e).__name__}: {e}')

print('--- the cache is actually cleared ---')
am._CFG_CACHE[str(GID)] = (0, {'enabled': 1})
am._cfg_drop(GID)
check('the cached entry is gone', str(GID) not in am._CFG_CACHE, am._CFG_CACHE.keys())

print('--- a setting change survives the round trip ---')
cfg = am._cfg(GID)
check('the config loads', isinstance(cfg, dict) and 'enabled' in cfg, cfg)
with db.conn_ctx() as conn:
    conn.execute('UPDATE automod SET enabled=0 WHERE guild_id=?', (str(GID),))
    am._cfg_drop(GID)
fresh = am._cfg(GID)
check('the change is visible after the drop', not fresh.get('enabled'), fresh)

print('--- the native mirror can read that table ---')
try:
    from cogs import nativeautomod as na
    check('the native mirror imports', True)
    check('the mirror has a table helper', hasattr(na, '_ensure_table'), dir(na)[:6])
except Exception as e:
    check('the native mirror imports', False, f'{type(e).__name__}: {e}')

print()
if fails:
    print(f'FAILURES ({len(fails)}): ' + ', '.join(fails))
    raise SystemExit(1)
print('AUTOMOD CONFIG: ALL TESTS PASS')

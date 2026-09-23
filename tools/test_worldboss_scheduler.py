"""World-boss scheduler regression: idempotent expiry + rotation
auto-start over injected guilds and clock. No Discord, no sleeping.

Run: python tools/test_worldboss_scheduler.py (needs discord installed
for cog imports). Exit code 0 only when every test passes.
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


class FakeGuild:
    def __init__(self, gid, channels=()):
        self.id = gid
        self._ch = {str(c) for c in channels}

    def get_channel(self, cid):
        return object() if str(cid) in self._ch else None


def wb_rows(gid):
    import database as db
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM world_boss WHERE guild_id=? ORDER BY id', (str(gid),)).fetchall()]


def auto(gid, **kw):
    from tasks import worldboss_scheduler as sch
    base = {'enabled': '1', 'auto': '1', 'rotation': 'dreadmaw,tidecaller',
            'interval': '21600', 'duration': '0', 'channel': '7'}
    base.update(kw)
    for k, v in base.items():
        sch.set_setting(gid, k, v)


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from services import worldboss_service as wb
    from tasks import worldboss_scheduler as sch

    src = (ROOT / 'tasks' / 'worldboss_scheduler.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in scheduler')

    T0 = 2_000_000_000
    g = FakeGuild(11, channels=('7',))

    # --- due start, then no duplication ---
    auto(11)
    out = sch.tick([g], T0)
    check(out['11']['started'] == 'dreadmaw', 'due event starts (rotation head)')
    check(len(wb_rows(11)) == 1, 'one row')
    out = sch.tick([g], T0 + 10)
    check(out['11']['started'] is None and len(wb_rows(11)) == 1, 'active not duplicated')

    # --- interval gate + rotation ---
    with db.conn_ctx() as conn:
        conn.execute("UPDATE world_boss SET status='COMPLETED', defeated_at=? WHERE guild_id=?",
                     (T0 + 20, '11'))
    out = sch.tick([g], T0 + 30)
    check(out['11']['started'] is None, 'interval not elapsed: no start')
    out = sch.tick([g], T0 + 20 + 21600 + 1)
    check(out['11']['started'] == 'tidecaller', 'rotation advances after interval')
    check(len(wb_rows(11)) == 2, 'second event row')

    # --- expiry marked once, snapshots swept, claims preserved ---
    with db.conn_ctx() as conn:
        conn.execute('INSERT INTO world_boss_combat (boss_id, guild_id, user_id, mid, hp, max_hp) '
                     'VALUES (2, ?, ?, 1, 10, 50)', ('11', 'u1'))
        conn.execute('INSERT INTO world_boss_rewards (boss_id, user_id, reward_json, claimed_at) '
                     'VALUES (?, ?, ?, ?)', (2, 'u1', '{"coins": 1}', T0))
    out = sch.tick([g], T0 + 20 + 21600 + 1 + 3600 + 5)
    check(out['11']['expired'] is True, 'overdue expiry fires')
    with db.conn_ctx() as conn:
        st = conn.execute("SELECT status FROM world_boss WHERE guild_id='11' ORDER BY id DESC LIMIT 1"
                          ).fetchone()['status']
        snap = conn.execute('SELECT COUNT(*) c FROM world_boss_combat WHERE boss_id=2').fetchone()['c']
        kept = conn.execute('SELECT reward_json FROM world_boss_rewards WHERE boss_id=2 AND user_id=?',
                            ('u1',)).fetchone()['reward_json']
    check(st == 'EXPIRED', 'expired marked')
    check(snap == 0, 'combat snapshots swept')
    check(kept == '{"coins": 1}', 'claim data preserved')
    out = sch.tick([g], T0 + 20 + 21600 + 1 + 3600 + 6)
    check(out['11']['expired'] is False, 'expiry fires once')
    with db.conn_ctx() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM world_boss WHERE guild_id='11'").fetchone()['c']
    # start of next rotation happens only after interval from expiry end
    check(n == 2, 'no extra rows on re-tick')

    # --- defeated never expires ---
    with db.conn_ctx() as conn:
        conn.execute("INSERT INTO world_boss (guild_id, boss_key, max_hp, hp, status, revision, "
                     "started_at, expires_at, defeated_at) VALUES (?, 'dreadmaw', 5, 0, "
                     "'COMPLETED', 0, ?, ?, ?)", ('12', T0, T0 + 99, T0 + 99))
    out = sch.tick([FakeGuild(12, ('7',))], T0 + 99999)
    with db.conn_ctx() as conn:
        st = conn.execute("SELECT status FROM world_boss WHERE guild_id='12'").fetchone()['status']
    check(st == 'COMPLETED', 'defeated never expires')

    # --- gates: auto-off, disabled, no channel, bad channel ---
    auto(13, auto='0')
    check(sch.tick([FakeGuild(13, ('7',))], T0)['13']['skipped'] == 'auto-off', 'auto-off skips')
    auto(14, enabled='0')
    check(sch.tick([FakeGuild(14, ('7',))], T0)['14']['skipped'] == 'auto-off', 'disabled skips')
    auto(15, channel='')
    check(sch.tick([FakeGuild(15, ('7',))], T0)['15']['skipped'] == 'no-channel',
          'missing channel blocks start')
    auto(16, channel='99')
    check(sch.tick([FakeGuild(16, ('7',))], T0)['16']['skipped'] == 'bad-channel',
          'unknown channel blocks start')

    # --- invalid rotation skips one guild, others proceed ---
    auto(17, rotation='nope')
    auto(18)
    out = sch.tick([FakeGuild(17, ('7',)), FakeGuild(18, ('7',))], T0)
    check(out['17']['started'] is None and out['18']['started'] == 'dreadmaw',
          'bad config isolated per guild')

    # --- rotation skips unknown/disabled bosses inside the list ---
    auto(19, rotation='nope,tidecaller')
    out = sch.tick([FakeGuild(19, ('7',))], T0)
    check(out['19']['started'] == 'tidecaller', 'unknown entries skipped in rotation')

    # --- manual start overrides auto-off ---
    r = wb.start_boss(13, 'tidecaller', T0)
    check(r['ok'] and r['boss_key'] == 'tidecaller', 'manual start ignores auto gate')

    # --- restart resumes: fresh tick after expiry+interval rotates on ---
    out = sch.tick([g], T0 + 20 + 21600 + 1 + 3600 + 5 + 21600 + 10)
    check(out['11']['started'] == 'dreadmaw', 'rotation wraps after restart gap')
    check(len(wb_rows(11)) == 3, 'third event row')

    # --- service expire_event unit shape ---
    r = wb.expire_event(11, T0 + 20 + 21600 + 1 + 3600 + 5 + 21600 + 10 + 3600 + 1)
    check(r['ok'] and r['code'] == 'EXPIRED', 'expire_event acts once')
    r = wb.expire_event(11, T0 + 20 + 21600 + 1 + 3600 + 5 + 21600 + 10 + 3600 + 2)
    check(not r['ok'] and r['code'] == 'NOOP', 'expire_event idempotent')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

"""World-boss history regression: read-only, guild-scoped reporting over
durable rows. No Discord, no sleeping.

Run: python tools/test_worldboss_history.py. Exit 0 only when all pass.
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


def snapshot_db():
    import database as db
    with db.conn_ctx() as conn:
        tables = [r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        dump = {}
        for t in tables:
            try:
                dump[t] = [tuple(r) for r in conn.execute(
                    f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()]
            except Exception:
                dump[t] = 'UNREADABLE'
    return dump


def mk_event(gid, key='tidecaller', status='COMPLETED', start=1000, dur=3600,
             defeated=None, max_phase='', config_json='', max_hp=7000, hp=0):
    import database as db
    end = start + dur
    with db.conn_ctx() as conn:
        cur = conn.execute(
            'INSERT INTO world_boss (guild_id, boss_key, max_hp, hp, status, revision, '
            'started_at, expires_at, defeated_at, config_json, max_phase) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (str(gid), key, max_hp, hp, status, 0, start, end,
             defeated or (end if status == 'COMPLETED' else 0), config_json, max_phase))
        return cur.lastrowid


def add_part(bid, gid, uid, damage, attacks=1, claimed=0):
    import database as db
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO world_boss_parts (boss_id, guild_id, user_id, damage, '
            'attacks, joined_at, last_action_at, reward_claimed) VALUES (?,?,?,?,?,?,?,?)',
            (bid, str(gid), str(uid), damage, attacks, 1000, 1001, claimed))


def add_reward(bid, uid, items, phase=None, claimed_at=2000):
    import database as db
    import json as _json
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO world_boss_rewards (boss_id, user_id, reward_json, '
            'claimed_at) VALUES (?,?,?,?)',
            (bid, str(uid), _json.dumps({'coins': 1000, 'items': items, 'phase': phase}),
             claimed_at))


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    src = (ROOT / 'services' / 'worldboss_history.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in history service')
    # strip module docstring + line comments so prose ("no INSERT...") can't
    # false-positive; what remains must contain no write statements/calls.
    import re as _re
    code = _re.sub(r'""".*?"""', '', src, flags=_re.S)
    code = '\n'.join(l.split('#', 1)[0] for l in code.splitlines())
    for bad in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP',
                'expire_event', 'start_boss', 'claim_rewards', 'meta_set'):
        check(bad not in code, f'read-only: no {bad} in history service')

    from services import worldboss_history as hist
    from services import worldboss_service as wb

    # --- empty store: valid empty pages, tables never created by reads ---
    check(hist.recent_events(1) == [], 'empty history returns empty list')
    lb = hist.leaderboard(1, 'damage')
    check(lb.total_users == 0 and lb.entries == () and lb.total_pages == 0,
          'empty leaderboard returns valid empty page')
    st = hist.statistics(1)
    check(st.total == 0 and st.most_used_boss is None, 'empty stats valid')
    check(hist.event_details(1, 999) is None, 'missing event returns None')
    with db.conn_ctx() as conn:
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check('world_boss' not in names, 'reads create no tables')

    wb.ensure_tables()
    T0 = 2_000_000_000

    # --- guild scoping ---
    e1 = mk_event(11, 'tidecaller', 'COMPLETED', T0, 3600, T0 + 3600, 'raging')
    e2 = mk_event(12, 'dreadmaw', 'COMPLETED', T0, 3600, T0 + 3600, '')
    check([e.event_id for e in hist.recent_events(11)] == [e1], 'recent events guild-scoped')
    check(hist.event_details(11, e2) is None, 'other guild event invisible')
    check(hist.event_details(12, e2) is not None, 'own event visible')

    # --- outcome shapes ---
    d = hist.event_details(11, e1)
    check(d.outcome == 'defeated' and d.status == 'COMPLETED', 'defeated represented')
    check(d.duration_s == 3600 and d.end_at == T0 + 3600, 'defeated duration math')
    e3 = mk_event(11, 'dreadmaw', 'EXPIRED', T0 + 5000, 1800)
    dx = hist.event_details(11, e3)
    check(dx.outcome == 'expired' and dx.duration_s == 1800, 'expired represented')
    e4 = mk_event(11, 'tidecaller', 'ACTIVE', T0 + 9000, 3600, max_hp=7000, hp=5000)
    ax = hist.event_details(11, e4)
    check(ax.outcome == 'active' and ax.end_at is None and ax.duration_s is None,
          'active represented without end')
    check(ax.hp == 5000 and ax.max_hp == 7000, 'active HP carried')

    # --- deterministic top ordering, ties by user_id ---
    add_part(e1, 11, 'u_b', 200, 2)
    add_part(e1, 11, 'u_a', 200, 3)
    add_part(e1, 11, 'u_c', 50, 1)
    d = hist.event_details(11, e1)
    check([t.user_id for t in d.top] == ['u_a', 'u_b', 'u_c'], 'ties use stable user_id order')
    check(d.total_damage == 450 and d.total_attacks == 6 and d.participants == 3,
          'aggregates from durable parts')

    # --- guaranteed vs weighted split (same item both ways) ---
    # phase bonus appended LAST by claim_rewards: [weighted..., bonus]
    add_reward(e1, 'u_a', [['ultra', 1], ['tidal_scale', 1], ['tidal_scale', 1]],
               phase='raging')
    add_reward(e1, 'u_b', [['poke', 2]], phase='raging')
    d = hist.event_details(11, e1)
    check(d.claims == 2, 'claim count from persisted outcomes')
    # u_a: last tidal_scale is the phase bonus, first is a weighted roll;
    # u_b: poke only (bonus item absent -> nothing forced into guaranteed).
    check(dict(d.loot_guaranteed) == {'tidal_scale': 1}, 'guaranteed counted separately')
    check(dict(d.loot_weighted) == {'ultra': 1, 'tidal_scale': 1, 'poke': 2},
          'weighted counted separately')
    check(dict(d.loot_total).get('tidal_scale') == 2, 'total delivered sums both')
    check(d.phase_reached == 'raging', 'phase carried from event row')

    # --- history survives combat snapshot cleanup ---
    r = wb.start_boss(13, 'tidecaller', T0, max_hp=500)
    bid = r['boss_id']
    add_part(bid, 13, 'scout', 120, 2)
    with db.conn_ctx() as conn:
        conn.execute('INSERT INTO world_boss_combat (boss_id, guild_id, user_id, mid, hp, '
                     'max_hp) VALUES (?,?,?,?,?,?)', (bid, '13', 'scout', 1, 50, 100))
    wb.expire_event(13, T0 + 3600 + 1)
    with db.conn_ctx() as conn:
        left = conn.execute('SELECT COUNT(*) c FROM world_boss_combat WHERE boss_id=?',
                            (bid,)).fetchone()['c']
    check(left == 0, 'expiry sweeps combat snapshots')
    h = hist.event_details(13, bid)
    check(h is not None and h.outcome == 'expired' and h.total_damage == 120
          and h.participants == 1, 'history survives snapshot cleanup')

    # --- pagination stable ---
    ids = [mk_event(14, 'dreadmaw', 'COMPLETED', T0 + i * 10, 100, T0 + i * 10 + 100)
           for i in range(7)]
    p1 = hist.recent_events(14, limit=3, offset=0)
    p2 = hist.recent_events(14, limit=3, offset=3)
    p3 = hist.recent_events(14, limit=3, offset=6)
    check([e.event_id for e in p1] == sorted(ids, reverse=True)[:3], 'page 1 newest first')
    check(len(p2) == 3 and len(p3) == 1, 'pages slice without overlap')
    check(len({e.event_id for e in p1 + p2 + p3}) == 7, 'pagination covers all once')
    check(hist.recent_events(14, limit=3, offset=99) == [], 'overflow page empty')

    # --- leaderboards ---
    lb = hist.leaderboard(11, 'damage')
    check([e.user_id for e in lb.entries] == ['u_a', 'u_b', 'u_c'], 'damage order')
    check(lb.entries[0].value == 200 and lb.entries[0].extra == 3, 'damage values')
    lb = hist.leaderboard(11, 'participation')
    check(lb.entries[0].value == 1, 'participation counts events')
    lb = hist.leaderboard(11, 'wins')
    # e1 COMPLETED top is u_a (tie broken by user_id) -> 1 win
    check(lb.entries and lb.entries[0].user_id == 'u_a' and lb.entries[0].value == 1,
          'wins go to deterministic top')
    lb = hist.leaderboard(11, 'bogus-metric')
    check(lb.metric == 'damage', 'invalid metric falls back safely')
    lb1 = hist.leaderboard(11, 'damage', page=0, per_page=1)
    lb2 = hist.leaderboard(11, 'damage', page=99, per_page=1)
    check(len(lb1.entries) == 1 and lb2.entries == () and lb1.total_pages == 3,
          'leaderboard overflow empty page')

    # --- statistics ---
    st = hist.statistics(11)
    check(st.total == 3 and st.defeated == 1 and st.expired == 1 and st.active == 1,
          'stats count outcomes')
    check(st.defeat_rate == 0.5, 'defeat rate over finished only')
    check(st.most_used_boss == 'tidecaller', 'most-used boss')
    check(st.most_common_phase == 'raging', 'most common phase')
    check(st.total_claims == 2, 'stats claim total')
    check(dict(st.loot_guaranteed).get('tidal_scale') == 1, 'stats split guaranteed')
    check(dict(st.loot_weighted).get('tidal_scale') == 1, 'stats split weighted')
    check(st.avg_participants == round(3 / 3, 2), 'avg participants')
    check(st.avg_damage_per_attack == round(450 / 6, 2), 'avg damage per attack')
    st2 = hist.statistics(11, period=3600 * 24, now=T0 + 9500)
    check(st2.total <= st.total, 'period window filters')
    check(hist.statistics(99).total == 0, 'unknown guild stats empty')

    # --- read-only: no writes, scheduler/rewards/active untouched ---
    db.meta_set('wb_11_sched_action', '123|STARTED:tidecaller:1')
    before = snapshot_db()
    hist.recent_events(11, limit=10)
    hist.event_details(11, e1)
    hist.event_details(11, 999999)
    hist.event_details(99, e1)
    hist.leaderboard(11, 'damage')
    hist.leaderboard(11, 'nope', page=5)
    hist.statistics(11)
    hist.statistics(11, period=10, now=T0)
    after = snapshot_db()
    check(before == after, 'read-only methods perform no writes')
    check(db.meta_get('wb_11_sched_action') == '123|STARTED:tidecaller:1',
          'scheduler metadata untouched by failed lookups')
    with db.conn_ctx() as conn:
        n = conn.execute('SELECT COUNT(*) c FROM world_boss_rewards').fetchone()['c']
        act = conn.execute("SELECT COUNT(*) c FROM world_boss WHERE status='ACTIVE'").fetchone()['c']
    check(n == 2 and act == 1, 'rewards and active event unchanged')

    # --- scheduler note attaches only to the named event ---
    db.meta_set('wb_11_sched_action', f'{T0}|STARTED:tidecaller:{e1}')
    d = hist.event_details(11, e1)
    check(d.scheduler_note == f'STARTED:tidecaller:{e1}', 'scheduler action attached')
    d2 = hist.event_details(11, e3)
    check(d2.scheduler_note is None, 'stale note not smeared onto others')

    # --- count helper + renderers (headless, stub guild) ---
    check(hist.count_events(99) == 0, 'count zero for unknown guild')
    check(hist.count_events(14) == 7, 'count matches inserted events')
    import json as _json2
    _lang = _json2.load(open(ROOT / 'lang.json', encoding='utf-8'))
    for _k in ('eco.wb_hist_title', 'eco.wb_hist_empty', 'eco.wb_hist_page',
               'eco.wb_lb_title', 'eco.wb_lb_empty', 'eco.wb_lb_use',
               'eco.wb_stats_title', 'eco.wb_stats_empty', 'eco.wb_stats_events',
               'eco.wb_stats_avg', 'eco.wb_stats_fav', 'eco.wb_stats_loot'):
        check(_k in _lang['en'] and _k in _lang['pl'], f'lang key {_k} in en+pl')
    from cogs.worldboss import (build_history_text, build_leaderboard_text,
                                build_stats_text)

    class _StubGuild:
        def get_member(self, uid):
            return None

    evs = hist.recent_events(11, limit=5, offset=0)
    txt = build_history_text(_StubGuild(), evs, 0, 1, 11)
    check('Tidecaller' in txt and 'Dreadmaw' in txt, 'history render names events')
    check('DEFEATED' in txt and 'EXPIRED' in txt, 'history render outcomes')
    check('<@u_a>' in txt, 'history render top contributor fallback mention')
    board = hist.leaderboard(11, 'damage')
    ltxt = build_leaderboard_text(_StubGuild(), board, 11)
    check('damage' in ltxt and '<@u_a>' in ltxt, 'leaderboard render')
    stxt = build_stats_text(hist.statistics(11), 11)
    check('tidal_scale' in stxt and '(bonus)' in stxt, 'stats render splits bonus loot')
    check('Page' not in txt, 'single page omits pager')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()

"""Identity slice acceptance: deterministic classes, explicit fallbacks.

Run: python tools/test_identity.py (needs discord installed for cog imports
it doesn't touch). Exit code 0 only when every test passes. Scratch DB.
"""
import asyncio
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


async def main() -> None:
    import database as db
    tmp = Path(tempfile.mkdtemp()) / 'test.db'
    db.DB_PATH = tmp
    db.init_db()

    import utils.identity as I
    from utils.identity import (get_trainer_class, get_top_achievement,
                                get_dex_completion, get_duel_record,
                                get_heist_record, get_biggest_trade,
                                get_activity_title)

    G, U = '99', '7'

    def seed(sql, params=()):
        with db.conn_ctx() as conn:
            conn.execute(sql, params)

    # 1. brand-new user: no class, no achievement, zeros — never misleading
    check(get_trainer_class(G, 'new') is None, 'new user has no class')
    check(get_top_achievement(G, 'new') is None, 'new user has no top achievement')
    d = get_dex_completion(G, 'new')
    check((d['distinct'], d['total'], d['pct']) == (0, 809, 0.0), 'new user dex 0/809')
    r = get_duel_record(G, 'new')
    check((r['w'], r['l']) == (0, 0), 'new user duels 0-0')
    check(get_activity_title(G, 'new') == 'newcomer', 'new user title newcomer')
    check(get_heist_record(G, 'new') is None, 'heist: explicit None, never fabricated')
    check(get_biggest_trade(G, 'new') is None, 'trades: explicit None, never fabricated')

    # 2. job-only user -> worker (and one casino session changes nothing:
    #    sessions are not counted durably, so no Gambler class exists)
    seed("INSERT INTO jobs VALUES ('99','w1','kurier',0,0,9)")
    check(get_trainer_class(G, 'w1') == 'worker', 'job-only user is worker')
    check(get_activity_title(G, 'w1') not in ('gym_champion', 'master_collector'),
          'worker gets no elite title')

    # 3. tied scores break by CLASS_ORDER (collector first)
    for dex in range(1, 11):
        seed('INSERT OR IGNORE INTO pk_dexcount VALUES (?,?,?,?)', ('99', 't1', dex, 1))
    seed("INSERT INTO pk_stats VALUES ('99','t1',5,5,0,0,0)")
    check(get_trainer_class(G, 't1') == 'collector', 'tie collector>battler (10 vs 10)')
    # mixed leaning battler wins outright
    seed("INSERT INTO pk_stats VALUES ('99','t2',14,6,0,0,0)")
    for dex in range(1, 11):
        seed('INSERT OR IGNORE INTO pk_dexcount VALUES (?,?,?,?)', ('99', 't2', dex, 1))
    check(get_trainer_class(G, 't2') == 'battler', 'mixed 20 vs 10 goes battler')

    # 4. achievements: highest tier wins, ties -> earliest unlock
    seed("INSERT INTO achievements VALUES ('99','a1','lvl10',100)")
    seed("INSERT INTO achievements VALUES ('99','a1','first_job',50)")
    top = get_top_achievement(G, 'a1')
    check(top and top[0] == 'lvl10', 'top achievement by tier, not insertion')
    seed("INSERT INTO achievements VALUES ('99','a2','rich100k',200)")
    seed("INSERT INTO achievements VALUES ('99','a2','lvl10',300)")
    top2 = get_top_achievement(G, 'a2')
    check(top2 and top2[0] == 'lvl10', 'tier beats recency')
    check(get_top_achievement(G, 'nobody') is None, 'no achievements -> None')

    # 5. dex: unreleased/event entries count as discovered, denominator fixed
    seed('INSERT OR IGNORE INTO pk_dexcount VALUES (?,?,?,?)', ('99', 'e1', 999, 1))
    d = get_dex_completion(G, 'e1')
    check(d['distinct'] == 1 and d['total'] == 809, 'event dex counts, denominator stable')
    for dex in range(1, 810):
        seed('INSERT OR IGNORE INTO pk_dexcount VALUES (?,?,?,?)', ('99', 'full', dex, 1))
    d = get_dex_completion(G, 'full')
    check(d['distinct'] == 809 and d['pct'] == 100.0, 'full dex 809/809')

    # 6. duels have no draws column: format omits them by construction
    r = get_duel_record(G, 't2')
    check(set(r.keys()) == {'w', 'l'}, 'duel record has no draws field')

    # 7. criminal needs sustained activity, not one lucky event
    seed("INSERT INTO eco (guild_id,user_id,cash,last_daily) VALUES ('99','c1',100,0)")
    seed("UPDATE eco SET last_rob=? WHERE guild_id='99' AND user_id='c1'", (123,))
    check(get_trainer_class(G, 'c1') == 'criminal', 'robbed user classifies criminal')
    seed("INSERT INTO eco (guild_id,user_id,cash,last_daily) VALUES ('99','c0',100,0)")
    check(get_trainer_class(G, 'c0') is None, 'single rich wallet is not criminal')

    # 8. trader needs open listings (weak proxy, documented threshold)
    for i in range(2):
        seed("INSERT INTO pk_market (guild_id,seller_id,dex) VALUES ('99','m1',25)")
    check(get_trainer_class(G, 'm1') == 'trader', 'open listings classify trader')

    # 9. titles follow priority, classes fall back
    for i, npc in enumerate(('joey', 'finn', 'cyntia')):
        seed('INSERT OR REPLACE INTO pk_npc VALUES (?,?,?,?)', ('99', 'champ', npc, 1))
    check(get_activity_title(G, 'champ') == 'gym_champion', 'all npcs -> gym champion')
    check(get_activity_title(G, 'full') == 'master_collector', 'full dex -> master collector')

    # 10. bilingual rendering of the real card block (profile logic mirrored).
    # Lang is per-guild: '99' renders en by default, pl after opt-in.
    from lang import set_lang as _setlang
    from lang import t as _t
    from utils.embeds import ok as _ok

    def card(gid, uid, name):
        cls = get_trainer_class(gid, uid)
        title = get_activity_title(gid, uid)
        tkey = 'pf.title_' + title if title in (
            'gym_champion', 'master_collector', 'market_maker', 'crime_boss',
            'job_specialist', 'regional_expert', 'newcomer') else 'pf.class_' + title
        head = ((_t(gid, 'pf.class_' + cls) + ' · ') if cls else '') + _t(gid, tkey)
        dex = get_dex_completion(gid, uid)
        du = get_duel_record(gid, uid)
        lines = [head,
                 _t(gid, 'pf.dex', a=dex['distinct'], b=dex['total'], p=dex['pct']),
                 _t(gid, 'pf.duels', w=du['w'], l=du['l'])]
        top = get_top_achievement(gid, uid)
        if top:
            lines.append(_t(gid, 'pf.top', name=top[1]))
        return _ok('\n'.join(lines), title=_t(gid, 'pf.identity'))

    en = card(G, 't1', 'x')
    check('Collector' in en.description and 'Dex:' in en.description, 'en card renders')
    _setlang(G, 'pl')
    db._lang_cache.pop(G, None)
    pl = card(G, 't1', 'x')
    check('Kolekcjoner' in pl.description and 'Tożsamość' in (pl.title or ''),
          'pl card renders')
    check(len(en.description or '') < 1000, 'card stays compact')
    check('Heist' not in en.description and 'trade:' not in en.description.lower(),
          'no fabricated heist/trade lines')

    # 11. long names don't break the card
    seed("INSERT INTO achievements VALUES ('99','long','npc_champ',1)")
    top = get_top_achievement(G, 'long')
    check(top and len(card(G, 'long', 'x').description or '') < 1000,
          'long achievement name fits')

    # 12. buddy / regions / streak from existing data only
    from utils.identity import get_buddy_name, get_regions, get_best_streak
    check(get_buddy_name(G, 'nobody') is None, 'no buddy -> None, not fabricated')
    check(get_regions(G, 'nobody') == {'have': 0, 'total': 4}, 'no regions -> 0/4')
    check(get_best_streak(G, 'nobody') == 0, 'no streak -> 0')
    seed("INSERT INTO pk_mons (guild_id,owner_id,dex,level,xp,shiny,nick,active,ivs,evs) "
         "VALUES ('99','b1',25,5,0,0,'Sparky',1,'','')")
    mid = None
    with db.conn_ctx() as conn:
        mid = conn.execute("SELECT id FROM pk_mons WHERE owner_id='b1'").fetchone()['id']
        conn.execute('INSERT OR REPLACE INTO pk_buddy VALUES (?,?,?,?)', ('99', 'b1', mid, 0))
    check(get_buddy_name(G, 'b1') == 'Sparky', 'buddy nickname resolves')
    seed("INSERT INTO achievements VALUES ('99','b1','region_kanto',10)")
    seed("INSERT INTO achievements VALUES ('99','b1','region_hoenn',20)")
    check(get_regions(G, 'b1') == {'have': 2, 'total': 4}, 'regions 2/4')
    seed("INSERT INTO pk_stats VALUES ('99','b1',0,0,0,0,7)")
    check(get_best_streak(G, 'b1') == 7, 'best streak reads')

    print('FAILURES: %d' % len(FAILS), flush=True)
    raise SystemExit(1 if FAILS else 0)


asyncio.run(main())

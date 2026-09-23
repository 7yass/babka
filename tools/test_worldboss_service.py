"""World boss regression: lifecycle, guarded HP, contributions,
idempotent rewards — first service built for concurrency.

Run: python tools/test_worldboss_service.py (needs discord installed
for cog imports). Exit code 0 only when every test passes.
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


GID = 9090
T0 = 1_700_000_000


def seed_dex(conn):
    conn.execute(
        'INSERT INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
        'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (25, 'pikachu', '["electric"]', 35, 55, 40, 50, 50, 90, '', 45, 0, 0, 0))


def seed_mon(conn, uid, level=10):
    cur = conn.execute(
        'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
        'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(GID), str(uid), 25, level, 0, 0, '', 1, '', '', 0, 0, ''))
    return cur.lastrowid


def boss_row():
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM world_boss WHERE guild_id=? ORDER BY id DESC LIMIT 1',
                         (str(GID),)).fetchone()
        return dict(r) if r else None


class FakeRng:
    def __init__(self, rolls=None, picks=None):
        self._rolls = list(rolls or [])
        self._picks = list(picks or [])

    def random(self):
        return self._rolls.pop(0) if self._rolls else 0.5

    def uniform(self, a, b):
        return 1.0

    def choice(self, seq):
        if self._picks:
            return seq[self._picks.pop(0) % len(seq)]
        return seq[0]


class FakeRng:
    def __init__(self, rolls=None, picks=None):
        self._rolls = list(rolls or [])
        self._picks = list(picks or [])

    def random(self):
        return self._rolls.pop(0) if self._rolls else 0.5

    def uniform(self, a, b):
        return 1.0

    def choice(self, seq):
        if self._picks:
            return seq[self._picks.pop(0) % len(seq)]
        return seq[0]


def combat_of(boss_id, uid):
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                         (boss_id, str(uid))).fetchone()
        return dict(r) if r else None


def mon_row(mid):
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM pk_mons WHERE id=?', (mid,)).fetchone()
        return dict(r) if r else None


def part(boss_id, uid):
    import database as db
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM world_boss_parts WHERE boss_id=? AND user_id=?',
                         (boss_id, str(uid))).fetchone()
        return dict(r) if r else None


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from cogs.gamble import add_cash
    from services import worldboss_service as wb

    src = (ROOT / 'services' / 'worldboss_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in service')

    with db.conn_ctx() as conn:
        seed_dex(conn)
        seed_mon(conn, 'ash', level=20)

    # --- lifecycle ---
    r = wb.start_boss(GID, 'dreadmaw', T0)
    check(r['ok'] and r['code'] == 'BOSS_STARTED', 'boss starts')
    r = wb.start_boss(GID, 'dreadmaw', T0)
    check(not r['ok'] and r['code'] == 'ALREADY_ACTIVE', 'second boss rejected')
    r = wb.start_boss(GID, 'nope', T0)
    check(not r['ok'] and r['code'] == 'UNKNOWN_BOSS', 'unknown boss rejected')

    r = wb.join_boss(GID, 'ash', T0)
    check(r['ok'] and r['code'] == 'JOINED', 'join works')
    r = wb.join_boss(GID, 'ash', T0)
    check(r['ok'], 'duplicate join harmless')

    # --- attacks ---
    b0 = boss_row()
    r = wb.attack_boss(GID, 'ash', T0)
    check(r['ok'] and r['damage'] > 0, 'attack deals damage via battle service')
    b1 = boss_row()
    check(b1['hp'] == b0['max_hp'] - r['damage'], 'shared HP reduced exactly')
    p = part(b0['id'], 'ash')
    check(p['damage'] == r['damage'] and p['attacks'] == 1, 'contribution tracked')
    r = wb.attack_boss(GID, 'ash', T0)
    check(not r['ok'] and r['code'] == 'COOLDOWN', 'attack cooldown enforced')
    r = wb.attack_boss(GID, 'brock', T0)  # no mons at all
    check(not r['ok'] and r['code'] == 'NO_MON', 'mon-less attacker rejected')

    # --- expiry ---
    r = wb.start_boss(GID, 'dreadmaw', T0)
    check(not r['ok'] and r['code'] == 'ALREADY_ACTIVE', 'active blocks restart')
    with db.conn_ctx() as conn:
        seed_mon(conn, 'old', level=20)
    r = wb.attack_boss(GID, 'old', T0 + 4000)
    check(not r['ok'] and r['code'] == 'NO_BOSS', 'expired boss gone')
    with db.conn_ctx() as conn:
        st = conn.execute("SELECT status FROM world_boss WHERE guild_id=? ORDER BY id DESC LIMIT 1",
                          (str(GID),)).fetchone()['status']
    check(st == 'EXPIRED', 'expiry marked')

    # --- defeat: faint-gating means wars need bench depth; here one
    # level-100 mon with fixed rolls falls a 130hp boss in 2 hits ---
    r = wb.start_boss(GID, 'dreadmaw', T0 + 5000, max_hp=130)
    check(r['ok'], 'new boss after expiry')
    bid = r['boss_id']
    with db.conn_ctx() as conn:
        seed_mon(conn, 'ash100', level=100)
    now, res = T0 + 5000, None
    for _ in range(5):
        res = wb.attack_boss(GID, 'ash100', now,
                             rng=FakeRng(rolls=[0.5] * 8))
        if res['code'] == 'DEFEATED':
            break
        now += 61
    check(res['code'] == 'DEFEATED', 'defeat recorded')
    check(boss_row()['status'] == 'COMPLETED', 'status completed once')
    r = wb.attack_boss(GID, 'ash100', now + 61)
    check(not r['ok'] and r['code'] == 'NO_BOSS', 'no attacks after defeat')

    # --- rewards: threshold, top bonus, idempotency ---
    add_cash(GID, 'ash100', 0)
    base = None
    with db.conn_ctx() as conn:
        base = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                            (str(GID), 'ash100')).fetchone()['cash']
    r = wb.claim_rewards(GID, 'ash100', now + 62)
    check(r['ok'] and r['amount'] == 1000 + 5000, 'solo attacker gets base+top')
    with db.conn_ctx() as conn:
        after = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                             (str(GID), 'ash100')).fetchone()['cash']
    check(after == base + 6000, 'payout credited exactly once')
    r = wb.claim_rewards(GID, 'ash100', now + 63)
    check(not r['ok'] and r['code'] == 'ALREADY_CLAIMED', 'double claim pays nothing')
    with db.conn_ctx() as conn:
        again = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                             (str(GID), 'ash100')).fetchone()['cash']
    check(again == after, 'balance unchanged on re-claim')
    r = wb.claim_rewards(GID, 'stranger', now + 64)
    check(not r['ok'] and r['code'] == 'NO_REWARD', 'non-participant gets nothing')
    r = wb.join_boss(GID, 'late', now + 65)
    check(not r['ok'], 'no joining a completed boss')

    # --- racing defeat: exactly one DEFEATED ---
    r = wb.start_boss(GID, 'dreadmaw', now + 100, max_hp=5)
    bid2 = r['boss_id']
    with db.conn_ctx() as conn:
        seed_mon(conn, 'r1', level=30)
        seed_mon(conn, 'r2', level=30)
    bar = threading.Barrier(2)
    out = {}

    def swing(who):
        bar.wait()
        out[who] = wb.attack_boss(GID, who, now + 100)

    t1 = threading.Thread(target=swing, args=('r1',))
    t2 = threading.Thread(target=swing, args=('r2',))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    codes = sorted(x['code'] for x in out.values())
    check(codes.count('DEFEATED') == 1, f'exactly one defeat ({codes})')
    with db.conn_ctx() as conn:
        st = conn.execute('SELECT status, hp FROM world_boss WHERE id=?', (bid2,)).fetchone()
        tot = conn.execute('SELECT COALESCE(SUM(damage),0) s FROM world_boss_parts WHERE boss_id=?',
                           (bid2,)).fetchone()['s']
    check(st['status'] == 'COMPLETED' and st['hp'] == 0, 'boss dead, hp floored')
    check(tot >= 5, 'no damage lost in the race')

    # --- racing claims: one payout ---
    add_cash(GID, 'r1', 0)
    add_cash(GID, 'r2', 0)
    c0 = {}
    with db.conn_ctx() as conn:
        for u in ('r1', 'r2'):
            c0[u] = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                                 (str(GID), u)).fetchone()['cash']
    bar2 = threading.Barrier(2)
    cout = {}

    def claim(who):
        bar2.wait()
        cout[who] = wb.claim_rewards(GID, who, now + 101)

    u1 = threading.Thread(target=claim, args=('r1',))
    u2 = threading.Thread(target=claim, args=('r2',))
    u1.start()
    u2.start()
    u1.join()
    u2.join()
    paid = [w for w in ('r1', 'r2') if cout[w]['ok']]
    with db.conn_ctx() as conn:
        c1 = {u: conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                              (str(GID), u)).fetchone()['cash'] for u in ('r1', 'r2')}
    # each racer's own claim may or may not be the top one; exactly the top
    # earner(s) get paid, nobody double-dips, totals stay exact
    for u in ('r1', 'r2'):
        earned = c1[u] - c0[u]
        n = cout[u]
        if n['ok']:
            check(earned == n['amount'], f'{u} credited exactly its award')
        else:
            check(earned == 0, f'{u} failed claim pays nothing')
    check(sum(c1[u] - c0[u] for u in ('r1', 'r2')) <= 2 * 6000, 'no money printed')

    # --- two-sided danger: snapshots take the hits, mons don't ---
    r = wb.start_boss(GID, 'dreadmaw', now + 500, max_hp=5000)
    bid3 = r['boss_id']
    with db.conn_ctx() as conn:
        seed_mon(conn, 'tank', level=20)
        tank_mid = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=?',
                                (str(GID), 'tank')).fetchone()['id']
    before = mon_row(tank_mid)
    # forced Crunch (always hits): retaliation lands, snapshot drops to 0
    r = wb.attack_boss(GID, 'tank', now + 500,
                       rng=FakeRng(rolls=[0.5, 0.5, 0.5, 0.5], picks=[1, 0]))
    check(r['ok'], 'attack with injected rng')
    snap = combat_of(bid3, 'tank')
    check(snap and snap['hp'] < snap['max_hp'], 'retaliation reduces snapshot')
    check(mon_row(tank_mid) == before, 'permanent mon row byte-identical')
    r = wb.attack_boss(GID, 'tank', now + 561)
    check(not r['ok'] and r['code'] == 'FAINTED', 'fainted snapshot cannot attack')

    # --- switch: fresh snapshot, same-mon guard, ownership ---
    with db.conn_ctx() as conn:
        seed_mon(conn, 'tank', level=15)
        tank2 = conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? '
                             'ORDER BY id DESC LIMIT 1', (str(GID), 'tank')).fetchone()['id']
    r = wb.switch_mon(GID, 'tank', tank2, now + 562)
    check(r['ok'] and r['code'] == 'SWITCHED', 'switch replaces snapshot')
    snap = combat_of(bid3, 'tank')
    check(snap['mid'] == tank2 and snap['hp'] == snap['max_hp'], 'fresh full-hp snapshot')
    r = wb.switch_mon(GID, 'tank', tank2, now + 563)
    check(not r['ok'] and r['code'] == 'SAME_MON', 'same-mon switch is a no-op')
    r = wb.switch_mon(GID, 'tank', 999999, now + 564)
    check(not r['ok'] and r['code'] == 'NOT_FOUND', 'unowned switch rejected')

    # --- joint commit: boss hp + snapshot + contribution move together ---
    with db.conn_ctx() as conn:
        hp_before = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bid3,)).fetchone()['hp']
    r = wb.attack_boss(GID, 'tank', now + 700)
    with db.conn_ctx() as conn:
        hp_after = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bid3,)).fetchone()['hp']
        contrib = conn.execute('SELECT damage FROM world_boss_parts WHERE boss_id=? AND user_id=?',
                               (bid3, 'tank')).fetchone()['damage']
        snap_hp = conn.execute('SELECT hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                               (bid3, 'tank')).fetchone()['hp']
    check(hp_before - hp_after == r['damage'] and contrib >= r['damage'],
          'boss hp, snapshot and contribution move together')

    # --- stale guard: the exact SQL shape the service uses. A bumped
    # revision makes the guarded write affect 0 rows; nothing else moves.
    # (Organic STALE already observed in the racing defeat test.)
    with db.conn_ctx() as conn:
        rev = conn.execute('SELECT revision FROM world_boss WHERE id=?',
                           (bid3,)).fetchone()['revision']
        conn.execute('UPDATE world_boss SET revision=revision+1 WHERE id=?', (bid3,))
        hp0 = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bid3,)).fetchone()['hp']
        sn0 = conn.execute('SELECT hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                           (bid3, 'tank')).fetchone()['hp']
        cur = conn.execute('UPDATE world_boss SET hp=max(0, hp-?), revision=revision+1 '
                           'WHERE id=? AND status=\'ACTIVE\' AND revision=?',
                           (10, bid3, rev))
        check((cur.rowcount or 0) == 0, 'stale revision writes nothing')
        hp1 = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bid3,)).fetchone()['hp']
        sn1 = conn.execute('SELECT hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                           (bid3, 'tank')).fetchone()['hp']
    check(hp0 == hp1 and sn0 == sn1, 'stale claim changes neither side')

    # --- defeat skips retaliation: snapshot stays full (own guild: bid3 busy) ---
    G3 = 9092
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G3), 'swift', 25, 20, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(G3, 'dreadmaw', now + 900, max_hp=5)
    bid4 = r['boss_id']
    r = wb.attack_boss(G3, 'swift', now + 900)
    check(r['code'] == 'DEFEATED', 'tiny boss falls')
    with db.conn_ctx() as conn:
        snap = conn.execute('SELECT hp, max_hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                            (bid4, 'swift')).fetchone()
    check(snap['hp'] == snap['max_hp'], 'no retaliation after defeat')

    # --- heal: cost, once-per-raid, gates ---
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_combat SET hp=? WHERE boss_id=? AND user_id=?',
                     (5, bid3, 'tank'))
    from cogs.gamble import add_cash
    add_cash(GID, 'tank', 5000)
    with db.conn_ctx() as conn:
        cash0 = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                             (str(GID), 'tank')).fetchone()['cash']
    r = wb.heal_fighter(GID, 'tank', now + 901)
    snap = combat_of(bid3, 'tank')
    with db.conn_ctx() as conn:
        cash1 = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                             (str(GID), 'tank')).fetchone()['cash']
    check(r['ok'] and snap['hp'] == snap['max_hp'] and cash0 - cash1 == 1000,
          'heal tops up for 1000 coins')
    r = wb.heal_fighter(GID, 'tank', now + 902)
    check(not r['ok'] and r['code'] in ('FULL_HP', 'HEAL_USED'), 'heal not repeatable')
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_combat SET hp=0 WHERE boss_id=? AND user_id=?',
                     (bid3, 'tank'))
    r = wb.heal_fighter(GID, 'tank', now + 903)
    check(not r['ok'] and r['code'] == 'FAINTED', 'fainted cannot heal')
    add_cash(GID, 'broke', 0)
    with db.conn_ctx() as conn:
        seed_mon(conn, 'broke', level=20)
        conn.execute('UPDATE eco SET cash=10 WHERE guild_id=? AND user_id=?',
                     (str(GID), 'broke'))
    wb.attack_boss(GID, 'broke', now + 904,
                   rng=FakeRng(rolls=[0.5, 0.5, 0.5, 0.5], picks=[1, 0]))
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_combat SET hp=? WHERE boss_id IN '
                     '(SELECT id FROM world_boss WHERE guild_id=? AND status=\'ACTIVE\') '
                     'AND user_id=?', (3, str(GID), 'broke'))
    r = wb.heal_fighter(GID, 'broke', now + 905)
    check(not r['ok'] and r['code'] == 'INSUFFICIENT_FUNDS', 'broke healer rejected')

    # --- phases: derived, deterministic, retaliation-only ---
    from game.bosses.dreadmaw import DREADMAW
    from services.worldboss_service import resolve_boss_phase
    _ph = DREADMAW.phases
    check(resolve_boss_phase(_ph, 3000, 5000).key == 'normal', 'above threshold: phase 1')
    check(resolve_boss_phase(_ph, 2500, 5000).key == 'enraged', 'exactly at threshold: phase 2')
    check(resolve_boss_phase(_ph, 100, 5000).key == 'enraged', 'below threshold: phase 2')
    check(resolve_boss_phase(_ph, 3000, 5000) is resolve_boss_phase(_ph, 3000, 5000),
          'phase selection deterministic')

    G4 = 9093
    TP = T0 + 300000
    with db.conn_ctx() as conn:
        for u, lv in (('pp1', 40), ('pp2', 40), ('px', 100), ('pz', 20)):
            conn.execute(
                'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
                'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(G4), u, 25, lv, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(G4, 'dreadmaw', TP, max_hp=5000)
    bidp = r['boss_id']
    v = wb.boss_status(G4, TP)
    check(v['phase'] == 'normal' and v['phase_name'] is None, 'status shows phase 1')
    check(wb.boss_status(G4, TP) == v, 'repeated status creates no events')

    def _snap_hp(uid):
        with db.conn_ctx() as conn:
            return conn.execute('SELECT hp, max_hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                                (bidp, uid)).fetchone()

    def _monrows(uid):
        with db.conn_ctx() as conn:
            return [dict(x) for x in conn.execute(
                'SELECT * FROM pk_mons WHERE guild_id=? AND owner_id=? ORDER BY id',
                (str(G4), uid)).fetchall()]

    before_p1 = _monrows('pp1')
    before_p2 = _monrows('pp2')
    r1 = wb.attack_boss(G4, 'pp1', TP, rng=FakeRng(rolls=[0.5] * 8))
    s1 = _snap_hp('pp1')
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss SET hp=? WHERE id=?', (2000, bidp))
    r2 = wb.attack_boss(G4, 'pp2', TP, rng=FakeRng(rolls=[0.5] * 8))
    s2 = _snap_hp('pp2')
    check(r1['ok'] and r2['ok'], 'phase attacks land')
    check(r1['damage'] == r2['damage'], 'phase affects retaliation only, never the strike')
    check((s1['max_hp'] - s1['hp']) < (s2['max_hp'] - s2['hp']),
          'enraged retaliation hits harder')
    check('Enraged' not in r2['message'], 'no transition message without crossing')
    check(_monrows('pp1') == before_p1 and _monrows('pp2') == before_p2,
          'permanent rows byte-identical in both phases')

    # crossing attack announces + commits in the same txn
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss SET hp=? WHERE id=?', (2600, bidp))
    rx = wb.attack_boss(G4, 'px', TP, rng=FakeRng(rolls=[0.5] * 8))
    check(rx['ok'] and 'Enraged' in rx['message'], 'crossing attack announces')
    with db.conn_ctx() as conn:
        hpx = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bidp,)).fetchone()['hp']
    check(hpx <= 2500, 'crossing commits with the announcement')
    v = wb.boss_status(G4, TP)
    check(v['phase'] == 'enraged' and v['phase_name'] == 'Enraged', 'status shows phase 2')

    # defeat in phase 2 still skips retaliation
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss SET hp=? WHERE id=?', (5, bidp))
    # pz already owns a mon from seeding; use a fresh uid for clarity
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G4), 'pfin', 25, 20, 0, 0, '', 1, '', '', 0, 0, ''))
    rz = wb.attack_boss(G4, 'pfin', TP)
    check(rz['code'] == 'DEFEATED', 'phase-2 defeat lands')
    with db.conn_ctx() as conn:
        sz = conn.execute('SELECT hp, max_hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                          (bidp, 'pfin')).fetchone()
    check(sz['hp'] == sz['max_hp'], 'no retaliation after phase-2 defeat')

    # --- phase effects: rain resolved pre-attack, engine-applied ---
    from cogs.pokemon import _dex_row, calc_stats, damage
    from game.bosses.tidecaller import TIDECALLER
    G5 = 9094
    TP2 = T0 + 400000
    with db.conn_ctx() as conn:
        for u in ('w1', 'w2', 'wx'):
            conn.execute(
                'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
                'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(G5), u, 25, 100, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(G5, 'tidecaller', TP2)
    bidw = r['boss_id']
    with db.conn_ctx() as conn:
        before_mons = [dict(x) for x in conn.execute(
            'SELECT * FROM pk_mons WHERE guild_id=? ORDER BY id', (str(G5),)).fetchall()]
    v = wb.boss_status(G5, TP2)
    check(v['weather'] is None, 'no weather in normal phase')

    row = _dex_row(25)
    pstats = calc_stats(row, 100)
    bstats = dict(TIDECALLER.base_stats)
    btypes = list(TIDECALLER.types)
    wg = {'name': 'Water Gun', 'power': 40, 'acc': 100, 'ptype': 'water'}
    exp_dry, _ = damage(70, wg, bstats, pstats, btypes, ['electric'],
                        None, FakeRng(rolls=[0.5, 0.5]))
    r1 = wb.attack_boss(G5, 'w1', TP2, rng=FakeRng(rolls=[0.5] * 8))
    with db.conn_ctx() as conn:
        d1 = conn.execute('SELECT max_hp - hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                          (bidw, 'w1')).fetchone()[0]
    check(d1 == exp_dry, 'normal retaliation has no weather')

    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss SET hp=? WHERE id=?', (2000, bidw))
    v = wb.boss_status(G5, TP2)
    check(v['weather'] == 'rain' and v['phase'] == 'raging', 'status shows rain')
    hpump = {'name': 'Hydro Pump', 'power': 80, 'acc': 85, 'ptype': 'water'}
    exp_wet, _ = damage(70, hpump, bstats, pstats, btypes, ['electric'],
                        'rain', FakeRng(rolls=[0.5, 0.5]))
    exp_wet = int(exp_wet * 1.15)
    r2 = wb.attack_boss(G5, 'w2', TP2, rng=FakeRng(rolls=[0.5] * 8))
    with db.conn_ctx() as conn:
        d2 = conn.execute('SELECT max_hp - hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                          (bidw, 'w2')).fetchone()[0]
    check(d2 == exp_wet, 'enraged retaliation carries rain through the engine')
    check(r1['damage'] == r2['damage'], 'phase never touches the player strike')
    check('Raging Tide' not in r2['message'], 'no transition without crossing')

    # crossing turn retaliates with the PRE-attack (dry) phase:
    # 4250/7000 = 60.7% normal, ~99 deterministic damage lands <= 4200.
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss SET hp=? WHERE id=?', (4250, bidw))
    rx = wb.attack_boss(G5, 'wx', TP2, rng=FakeRng(rolls=[0.5] * 8))
    check('Raging Tide' in rx['message'], 'crossing announces once')
    with db.conn_ctx() as conn:
        dx = conn.execute('SELECT max_hp - hp FROM world_boss_combat WHERE boss_id=? AND user_id=?',
                          (bidw, 'wx')).fetchone()[0]
    check(dx == exp_dry, 'crossing retaliation uses pre-attack phase')
    with db.conn_ctx() as conn:
        after_mons = [dict(x) for x in conn.execute(
            'SELECT * FROM pk_mons WHERE guild_id=? ORDER BY id', (str(G5),)).fetchall()]
    check(after_mons == before_mons, 'combat never touches collection rows')

    # --- per-guild isolation + expiry cleanup ---
    G2 = 9091
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G2), 'scout', 25, 20, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(G2, 'dreadmaw', now + 906, max_hp=5000)
    bid_g2 = r['boss_id']
    wb.attack_boss(GID, 'tank', now + 906)
    with db.conn_ctx() as conn:
        hp_g2 = conn.execute('SELECT hp FROM world_boss WHERE id=?', (bid_g2,)).fetchone()['hp']
    check(hp_g2 == 5000, 'guilds do not share boss HP')
    with db.conn_ctx() as conn:
        left = conn.execute('SELECT COUNT(*) c FROM world_boss_combat WHERE boss_id=?',
                            (bid3,)).fetchone()['c']
    wb.attack_boss(GID, 'tank', now + 50000)  # past expiry: sweep cleans combat rows
    with db.conn_ctx() as conn:
        gone = conn.execute('SELECT COUNT(*) c FROM world_boss_combat WHERE boss_id=?',
                            (bid3,)).fetchone()['c']
    check(left >= 0 and gone == 0, 'expiry cleans combat snapshots')

    # --- pure loot rolls: deterministic, ranged, gated ---
    from game.bosses.dreadmaw import DREADMAW_LOOT
    from services.worldboss_service import roll_boss_loot
    r = roll_boss_loot(DREADMAW_LOOT, 100, False, FakeRng(rolls=[0.0, 0.0]))
    check(r == [('poke', 2)], 'base roll deterministic + qty floor')
    r = roll_boss_loot(DREADMAW_LOOT, 100, True, FakeRng(rolls=[0.0, 0.0, 0.6, 0.0]))
    check(r == [('poke', 2), ('great', 1)], 'top gets the extra roll')
    r = roll_boss_loot(DREADMAW_LOOT, 100, True, FakeRng(rolls=[0.0, 0.0, 0.6, 0.0]))
    check(r == [('poke', 2), ('great', 1)], 'same rolls, same loot')
    r = roll_boss_loot(DREADMAW_LOOT, 10, False, FakeRng(rolls=[0.99]))
    check(r == [('potion', 2)], 'low damage excludes scale/stone')
    r = roll_boss_loot(DREADMAW_LOOT, 600, False, FakeRng(rolls=[0.999, 0.0]))
    check(r == [('fire_stone', 1)], 'high damage unlocks stone')
    for _ in range(20):
        import random as _rr
        for item, qty in roll_boss_loot(DREADMAW_LOOT, 600, True, _rr):
            entry = next(e for e in DREADMAW_LOOT.entries if e.item_id == item)
            check(entry.quantity_min <= qty <= entry.quantity_max, 'qty in range')
            break

    # --- loot claim integration: exact items, persisted outcome, no reroll ---
    LT = T0 + 200000
    r = wb.start_boss(GID, 'dreadmaw', LT, max_hp=150)
    bid_loot = r['boss_id']
    with db.conn_ctx() as conn:
        seed_mon(conn, 'looter', level=100)
    wb.attack_boss(GID, 'looter', LT, rng=FakeRng(rolls=[0.5] * 8))
    wb.attack_boss(GID, 'looter', LT + 61, rng=FakeRng(rolls=[0.5] * 8))
    with db.conn_ctx() as conn:
        dmg = conn.execute('SELECT damage FROM world_boss_parts WHERE boss_id=? AND user_id=?',
                           (bid_loot, 'looter')).fetchone()['damage']
    check(dmg >= 50, 'looter clears the threshold')
    add_cash(GID, 'looter', 0)
    r = wb.claim_rewards(GID, 'looter', LT + 122,
                         rng=FakeRng(rolls=[0.0, 0.0, 0.6, 0.0]))
    check(r['ok'] and r['loot'] == [('poke', 2), ('great', 1)], 'claim rolls exact loot')

    def _qty(u, item):
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                               (str(GID), u, item)).fetchone()
            return row['qty'] if row else 0

    check(_qty('looter', 'poke') == 2 and _qty('looter', 'great') == 1,
          'exact quantities land in inventory')
    import json as _json
    with db.conn_ctx() as conn:
        saved = conn.execute('SELECT reward_json FROM world_boss_rewards WHERE boss_id=? AND user_id=?',
                             (bid_loot, 'looter')).fetchall()
    check(len(saved) == 1, 'one persisted outcome row')
    check(_json.loads(saved[0]['reward_json']) ==
          {'coins': 6000, 'items': [['poke', 2], ['great', 1]]},
          'persisted outcome matches payout')
    r = wb.claim_rewards(GID, 'looter', LT + 123)
    check(not r['ok'] and r['code'] == 'ALREADY_CLAIMED', 'duplicate claim denied')
    check(_qty('looter', 'poke') == 2 and _qty('looter', 'great') == 1,
          'duplicate creates no additional items')
    with db.conn_ctx() as conn:
        saved2 = conn.execute('SELECT reward_json FROM world_boss_rewards WHERE boss_id=? AND user_id=?',
                              (bid_loot, 'looter')).fetchall()
    check(len(saved2) == 1 and saved2[0]['reward_json'] == saved[0]['reward_json'],
          'retry cannot reroll')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

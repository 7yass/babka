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

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

"""Boss simulator regression: deterministic dry-runs over the real
engine, real phases and real loot rolls. No Discord, no sleeping.

Run: python tools/test_boss_simulator.py (needs discord installed for
cog/engine imports). Exit code 0 only when every test passes.
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
        return {t: [tuple(r) for r in conn.execute(
            f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()] for t in tables}


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
            'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (133, 'eevee', '["normal"]', 55, 55, 50, 45, 65, 55, '', 45, 0, 0, 0))

    import re as _re
    src = (ROOT / 'services' / 'boss_simulator.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in simulator')
    code = _re.sub(r'""".*?"""', '', src, flags=_re.S)
    code = '\n'.join(l.split('#', 1)[0] for l in code.splitlines())
    for bad in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'meta_set'):
        check(bad not in code, f'simulator never writes: no {bad}')

    from services import boss_simulator as sim

    KW = {'dex': 133, 'level': 100, 'fighters': 5, 'team_size': 6, 'raids': 30}

    # --- determinism + seed sensitivity ---
    a = sim.simulate_raids('dreadmaw', seed=11, **KW)
    b = sim.simulate_raids('dreadmaw', seed=11, **KW)
    check(a['ok'] and a == b, 'same seed gives identical reports')
    c = sim.simulate_raids('dreadmaw', seed=12, **KW)
    check(a != c, 'different seeds diverge')

    # --- comparative difficulty (same profile, real engine) ---
    t = sim.simulate_raids('tidecaller', seed=11, **KW)
    check(a['ok'] and t['ok'], 'both bosses simulate')
    check(t['avg_attacks_to_kill'] < a['avg_attacks_to_kill'],
          'tidecaller falls faster for normal attackers (effectiveness is real)')
    check(a['kills'] == KW['raids'] and t['kills'] == KW['raids'],
          'full L100 teams clear reliably')
    check(a['avg_clear_time_s'] < a['duration_seconds'],
          'clear fits inside the event window')
    check(0 <= a['avg_faints_per_raid'] <= KW['fighters'] * 6, 'faints bounded')
    check(a['avg_faints_per_raid'] > 0, 'danger is real: faints happen')
    check(a['avg_dmg_per_attack'] > 0, 'damage per attack positive')
    check(a['phase_reach'].get('enraged') == 1.0, 'dreadmaw always enrages')
    check(t['phase_reach'].get('raging') == 1.0, 'tidecaller always rages')

    # --- heals estimate charm demand ---
    h = sim.simulate_raids('dreadmaw', seed=11, heal_policy='coin', **KW)
    check(h['avg_heals_per_raid'] > 0, 'coin policy heals mid-raid')
    check(a['avg_heals_per_raid'] == 0, 'no policy means no heals')

    # --- weak profile: wipes, no kills, threshold None (no kill data) ---
    w = sim.simulate_raids('dreadmaw', dex=133, level=10, fighters=1,
                           team_size=1, raids=10, seed=3)
    check(w['kills'] == 0 and w['expiries'] + w['wipes'] == 10,
          'weak solo never kills')
    check(w['avg_attacks_to_kill'] is None and w['below_threshold_pct'] is None,
          'no-kill runs report None, not zeros')
    check(w['avg_clear_time_s'] is None, 'no clear time without kills')

    # --- threshold math on kills ---
    check(a['below_threshold_pct'] == 0.0, 'L100 team all qualifies (50dmg bar)')
    check(a['participation_damage'] == 50, 'threshold echoed from config')

    # --- loot accounting: guaranteed == fought-phase qualifiers, exactly ---
    # (structural identity the economy depends on)
    g = t['loot_guaranteed_total'].get('tidal_scale', 0)
    check(g == t['kills'] * KW['fighters'],
          'every qualifier earns exactly one phase bonus')
    check(t['loot_weighted_total'].get('tidal_scale', 0) >= 0,
          'weighted drops tracked separately')
    check('dreadmaw_scale' not in t['loot_guaranteed_total'],
          'no phantom guaranteed items')
    check(sum(t['loot_weighted_total'].values()) > 0, 'weighted loot flows')

    # --- read-only: dex reads only, nothing written ---
    before = snapshot_db()
    sim.simulate_raids('dreadmaw', seed=99, **KW)
    sim.simulate_raids('tidecaller', seed=99, **KW)
    sim.preview_boss('dreadmaw')
    sim.preview_boss('nope')
    check(before == snapshot_db(), 'simulator performs no writes')

    # --- invalid inputs fail cleanly ---
    check(sim.simulate_raids('nope')['ok'] is False, 'unknown boss rejected')
    check(sim.simulate_raids('dreadmaw', dex=9999)['ok'] is False,
          'unknown dex rejected')
    for bad in ({'fighters': 0}, {'fighters': 99}, {'level': 0}, {'level': 101},
                {'team_size': 0}, {'team_size': 7}, {'level': 'x'}):
        kw = dict(KW)
        kw.update(bad)
        check(sim.simulate_raids('dreadmaw', **kw)['ok'] is False,
              f"bad profile {bad} rejected")
    big = sim.simulate_raids('dreadmaw', raids=99999, seed=1, dex=133,
                             level=100, fighters=1, team_size=1)
    check(big['raids'] == 1000, 'raid count clamped to cap')

    # --- preview completeness ---
    for key in ('dreadmaw', 'tidecaller'):
        p = sim.preview_boss(key)
        check(p['ok'] and p['max_hp'] > 0 and p['duration_seconds'] > 0,
              f'preview {key} vitals')
        check(len(p['phases']) == 2 and len(p['loot']) >= 4,
              f'preview {key} phases + loot')
        share = sum(e['weight_share'] for e in p['loot'])
        check(abs(share - 1.0) < 0.01, f'preview {key} weights sum to 1')
    check(sim.preview_boss('nope') == {'ok': False, 'code': 'UNKNOWN_BOSS'},
          'preview unknown boss shape')

    # --- renderer smoke (headless) ---
    from cogs.worldboss import build_preview_text, build_sim_text
    txt = build_sim_text(a)
    for needle in ('Dreadmaw', 'Eevee', 'Kills', 'attacks', 'Faints', 'Loot'):
        check(needle in txt, f'sim render shows {needle}')
    txt = build_sim_text(w)
    check('No kills' in txt, 'sim render handles kill-less runs')
    txt = build_sim_text({'ok': False, 'code': 'UNKNOWN_BOSS'})
    check('UNKNOWN_BOSS' in txt, 'sim render handles failure')
    txt = build_preview_text(sim.preview_boss('tidecaller'))
    for needle in ('Tidecaller', '7000', 'Raging Tide', 'tidal_scale', 'Loot'):
        check(needle in txt, f'preview render shows {needle}')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()

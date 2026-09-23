"""Boss configuration framework regression: registry, validation,
snapshots, second-boss raids — same service, two configs.

Run: python tools/test_boss_config.py (needs discord installed for cog
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


GID = 61616
T0 = 1_800_000_000


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


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from game.bosses.dreadmaw import DREADMAW
    from game.bosses.models import (BossDefinition, BossLootTable,
                                    BossPhase, LootEntry,
                                    definition_from_snapshot,
                                    snapshot_definition, validate_definition)
    from game.bosses.registry import BOSSES, registry
    from game.bosses.tidecaller import TIDECALLER

    src = (ROOT / 'services' / 'worldboss_service.py').read_text(encoding='utf-8')
    for token in ("'dreadmaw'", "'tidecaller'"):
        check(token not in src, f'no hardcoded {token} in generic service')

    # --- registry ---
    check(registry.get('dreadmaw') is DREADMAW, 'dreadmaw resolves')
    check(registry.get('TIDECALLER') is TIDECALLER, 'case-insensitive resolve')
    check(registry.get('nope') is None, 'unknown returns None')
    check(registry.exists('dreadmaw') and not registry.exists('nope'), 'exists()')
    check(set(d.key for d in registry.list_available()) == {'dreadmaw', 'tidecaller'},
          'available lists enabled bosses')
    check('dreadmaw' in BOSSES and 'tidecaller' in BOSSES, 'mapping holds both')

    # --- validation rejects nonsense ---
    def bad(**kw):
        base = dict(key='x', display_name='X', max_hp=100, duration_seconds=60,
                    attack_cooldown_seconds=10,
                    phases=(BossPhase(key='a', min_hp_ratio=0.0,
                                      moves=({'name': 'M', 'power': 10, 'acc': 100,
                                              'ptype': 'normal'},)),))
        base.update(kw)
        return BossDefinition(**base)

    for label, kwargs in [
        ('empty key', {'key': ''}),
        ('no name', {'display_name': ''}),
        ('bad hp', {'max_hp': 0}),
        ('bad duration', {'duration_seconds': -1}),
        ('bad cooldown', {'attack_cooldown_seconds': 0}),
        ('no phases', {'phases': ()}),
        ('ratio out of range', {'phases': (BossPhase(key='a', min_hp_ratio=1.5,
                                                     moves=({'name': 'M', 'power': 10, 'acc': 100,
                                                             'ptype': 'normal'},)),)}),
        ('unordered phases', {'phases': (
            BossPhase(key='a', min_hp_ratio=0.0,
                      moves=({'name': 'M', 'power': 10, 'acc': 100, 'ptype': 'normal'},)),
            BossPhase(key='b', min_hp_ratio=0.5,
                      moves=({'name': 'M', 'power': 10, 'acc': 100, 'ptype': 'normal'},)))}),
        ('empty moves', {'phases': (BossPhase(key='a', min_hp_ratio=0.0, moves=()),)}),
        ('bad move', {'phases': (BossPhase(key='a', min_hp_ratio=0.0,
                                           moves=({'name': '', 'power': 0, 'acc': 0,
                                                   'ptype': ''},)),)}),
        ('loot wrong boss', {'loot_table': BossLootTable(
            boss_key='other', rolls=1, entries=(LootEntry('poke', 1, 1, 1),))}),
        ('bad loot entry', {'loot_table': BossLootTable(
            boss_key='x', rolls=1, entries=(LootEntry('poke', 5, 1, 1),))}),
        ('bad phase effect', {'phases': (BossPhase(
            key='a', min_hp_ratio=0.0,
            moves=({'name': 'M', 'power': 10, 'acc': 100, 'ptype': 'normal'},),
            effect_key='lava'),)}),
    ]:
        try:
            validate_definition(bad(**kwargs))
            check(False, f'validation rejects: {label}')
        except ValueError:
            check(True, f'validation rejects: {label}')
    validate_definition(DREADMAW)
    validate_definition(TIDECALLER)
    check(True, 'shipped definitions validate')

    # --- snapshot roundtrip + row persistence ---
    snap = snapshot_definition(DREADMAW)
    check(definition_from_snapshot(__import__('json').loads(snap)) == DREADMAW,
          'snapshot roundtrips exactly')
    snap2 = snapshot_definition(TIDECALLER)
    check(definition_from_snapshot(__import__('json').loads(snap2)) == TIDECALLER,
          'second snapshot roundtrips')
    check(all(p.effect_key is None for p in DREADMAW.phases),
          'dreadmaw stays effect-free')
    check(TIDECALLER.phases[1].effect_key == 'rain', 'tidecaller enraged rains')
    legacy = __import__('json').loads(snap2)
    for p in legacy['phases']:
        del p['effect_key']
    check(all(p.effect_key is None
              for p in definition_from_snapshot(legacy).phases),
          'pre-effect snapshots load with safe default')

    # --- disabled bosses cannot start ---
    off = BossDefinition(key='off', display_name='Off', max_hp=100,
                         duration_seconds=60, attack_cooldown_seconds=10,
                         phases=(BossPhase(key='a', min_hp_ratio=0.0,
                                           moves=({'name': 'M', 'power': 10, 'acc': 100,
                                                   'ptype': 'normal'},)),),
                         enabled=False)
    validate_definition(off)
    BOSSES['off'] = off
    try:
        from services import worldboss_service as wb
        r = wb.start_boss(GID, 'off', T0)
        check(not r['ok'] and r['code'] == 'DISABLED', 'disabled boss cannot start')
    finally:
        del BOSSES['off']

    # --- second boss full raid through the same service ---
    from cogs.gamble import add_cash
    from services import worldboss_service as wb
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
            'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (25, 'pikachu', '["electric"]', 35, 55, 40, 50, 50, 90, '', 45, 0, 0, 0))
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(GID), 'diver', 25, 100, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(GID, 'tidecaller', T0, max_hp=120)
    check(r['ok'] and r['boss_key'] == 'tidecaller', 'tidecaller starts')
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT config_json FROM world_boss WHERE id=?',
                           (r['boss_id'],)).fetchone()
    check(bool(row['config_json']), 'config snapshot stored on the row')
    check(definition_from_snapshot(__import__('json').loads(row['config_json'])).max_hp == 120,
          'snapshot carries effective values')
    v = wb.boss_status(GID, T0)
    check(v['name'] == 'Tidecaller', 'status uses configured name')
    now, res = T0, None
    for _ in range(4):
        res = wb.attack_boss(GID, 'diver', now, rng=FakeRng(rolls=[0.5] * 8))
        if res['code'] == 'DEFEATED':
            break
        now += 61
    check(res['code'] == 'DEFEATED', 'tidecaller falls')
    add_cash(GID, 'diver', 0)
    r = wb.claim_rewards(GID, 'diver', now + 62, rng=FakeRng(rolls=[0.0] * 4))
    check(r['ok'] and r['amount'] == 6000, 'tidecaller pays configured rewards')
    check(r['loot'] == [('poke', 2), ('poke', 2)], 'tidecaller loot table used')

    # --- two guilds, two definitions, independent ---
    G2 = 61617
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G2), 'scout', 25, 100, 0, 0, '', 1, '', '', 0, 0, ''))
    r = wb.start_boss(G2, 'dreadmaw', T0, max_hp=5000)
    check(r['ok'], 'second guild runs dreadmaw in parallel')
    v1 = wb.boss_status(G2, T0)
    check(v1['name'] == 'Dreadmaw' and v1['max_hp'] == 5000, 'definitions stay separate')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

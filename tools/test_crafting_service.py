"""Crafting regression: transactional boss-material sink + raid usable
hooks. No sleeping; threads only for the integer-race test.

Run: python tools/test_crafting_service.py (needs discord installed for
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


class FakeRng:
    def __init__(self, rolls=None):
        self._rolls = list(rolls or [])

    def random(self):
        return self._rolls.pop(0) if self._rolls else 0.5

    def uniform(self, a, b):
        return 1.0

    def choice(self, seq):
        return seq[0]


def qty(gid, uid, item):
    import database as db
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                           (str(gid), str(uid), item)).fetchone()
        return int(row['qty'] or 0) if row else 0


def give(gid, uid, item, n):
    import database as db
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO pk_balls (guild_id, user_id, ball, qty) '
                     'VALUES (?,?,?,0)', (str(gid), str(uid), item))
        conn.execute('UPDATE pk_balls SET qty=? WHERE guild_id=? AND user_id=? AND ball=?',
                     (n, str(gid), str(uid), item))


def expect_value_error(fn, msg):
    try:
        fn()
    except ValueError:
        check(True, msg)
    except Exception as e:
        check(False, f'{msg} (wrong error: {type(e).__name__})')
    else:
        check(False, f'{msg} (no error raised)')


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    src = (ROOT / 'services' / 'crafting_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in crafting service')

    from services import crafting_service as cs

    GID, U = 41, 'crafter'

    # --- listing ---
    views = cs.service.list_recipes(GID, U)
    check([v.key for v in views] == ['volcanic_charm', 'tidal_charm', 'raid_charm'],
          'recipe listing works')
    check(all(not v.can_afford for v in views), 'broke crafter affords nothing')
    give(GID, U, 'dreadmaw_scale', 3)
    views = cs.service.list_recipes(GID, U)
    by_key = {v.key: v for v in views}
    check(by_key['volcanic_charm'].can_afford, 'affordable flagged')
    check(not by_key['tidal_charm'].can_afford and not by_key['raid_charm'].can_afford,
          'unaffordable flagged')

    # --- unknown / disabled / quantity ---
    r = cs.service.craft(GID, U, 'nope')
    check(not r.ok and r.code == 'UNKNOWN_RECIPE', 'unknown recipe fails')
    check(qty(GID, U, 'dreadmaw_scale') == 3, 'failed craft writes nothing')
    disabled = cs.CraftingService(recipes=(
        cs.Recipe(key='x', inputs=(cs.ItemCost('dreadmaw_scale', 1),),
                  outputs=(cs.ItemReward('volcanic_charm', 1),), enabled=False),))
    r = disabled.craft(GID, U, 'x')
    check(not r.ok and r.code == 'DISABLED', 'disabled recipe fails')
    check(qty(GID, U, 'dreadmaw_scale') == 3, 'disabled craft writes nothing')
    for bad_qty in (0, -2, 100, 'many'):
        r = cs.service.craft(GID, U, 'volcanic_charm', bad_qty)
        check(not r.ok and r.code == 'BAD_QUANTITY', f'quantity {bad_qty!r} rejected')
    check(qty(GID, U, 'dreadmaw_scale') == 3, 'bad quantity writes nothing')

    # --- missing ingredient: no partial changes ---
    r = cs.service.craft(GID, U, 'tidal_charm')
    check(not r.ok and r.code == 'INSUFFICIENT_MATERIALS', 'missing ingredient fails')
    check(r.missing == (('tidal_scale', 3, 0),), 'shortfall reported exactly')
    check(qty(GID, U, 'dreadmaw_scale') == 3 and qty(GID, U, 'tidal_charm') == 0,
          'missing craft changes nothing')

    # --- single craft: exact accounting ---
    r = cs.service.craft(GID, U, 'volcanic_charm')
    check(r.ok and r.code == 'CRAFTED', 'single craft succeeds')
    check(r.consumed == (('dreadmaw_scale', 3),), 'consumed reported exactly')
    check(r.produced == (('volcanic_charm', 1),), 'outputs reported exactly')
    check(qty(GID, U, 'dreadmaw_scale') == 0, 'ingredients removed exactly')
    check(qty(GID, U, 'volcanic_charm') == 1, 'outputs added exactly')

    # --- duplicate request cannot create extra outputs ---
    r = cs.service.craft(GID, U, 'volcanic_charm')
    check(not r.ok and r.code == 'INSUFFICIENT_MATERIALS', 'second craft denied')
    check(qty(GID, U, 'volcanic_charm') == 1, 'no extra outputs on retry')

    # --- multi craft scales every cost ---
    give(GID, U, 'tidal_scale', 9)
    r = cs.service.craft(GID, U, 'tidal_charm', 3)
    check(r.ok and r.consumed == (('tidal_scale', 9),), 'multi craft scales costs')
    check(r.produced == (('tidal_charm', 3),), 'multi craft scales outputs')
    check(qty(GID, U, 'tidal_scale') == 0 and qty(GID, U, 'tidal_charm') == 3,
          'multi craft inventory exact')
    r = cs.service.craft(GID, U, 'tidal_charm', 2)
    check(not r.ok, 'oversized multi craft fails')
    check(qty(GID, U, 'tidal_charm') == 3, 'oversized multi craft writes nothing')

    # --- atomic on failure (multi-input: one leg missing) ---
    give(GID, U, 'volcanic_charm', 1)
    give(GID, U, 'tidal_charm', 0)
    r = cs.service.craft(GID, U, 'raid_charm')
    check(not r.ok and r.code == 'INSUFFICIENT_MATERIALS', 'raid charm needs both')
    check(r.missing == (('tidal_charm', 1, 0),), 'missing leg reported')
    check(qty(GID, U, 'volcanic_charm') == 1
          and qty(GID, U, 'tidal_charm') == 0 and qty(GID, U, 'raid_charm') == 0,
          'atomic on failure: no leg consumed')
    give(GID, U, 'tidal_charm', 1)
    r = cs.service.craft(GID, U, 'raid_charm')
    check(r.ok and r.consumed == (('volcanic_charm', 1), ('tidal_charm', 1)),
          'raid charm consumes both legs')
    check(qty(GID, U, 'raid_charm') == 1
          and qty(GID, U, 'volcanic_charm') == 0 and qty(GID, U, 'tidal_charm') == 0,
          'raid charm inventory exact')

    # --- definition validation ---
    expect_value_error(
        lambda: cs.validate_recipe_definitions(
            [cs.Recipe(key='a', inputs=(cs.ItemCost('dreadmaw_scale', 1),),
                       outputs=(cs.ItemReward('volcanic_charm', 1),)),
             cs.Recipe(key='a', inputs=(cs.ItemCost('dreadmaw_scale', 1),),
                       outputs=(cs.ItemReward('volcanic_charm', 1),))]),
        'duplicate keys rejected')
    expect_value_error(
        lambda: cs.validate_recipe_definitions(
            [cs.Recipe(key='b', inputs=(cs.ItemCost('dreadmaw_scale', 0),),
                       outputs=(cs.ItemReward('volcanic_charm', 1),))]),
        'zero input qty rejected')
    expect_value_error(
        lambda: cs.validate_recipe_definitions(
            [cs.Recipe(key='c', inputs=(cs.ItemCost('dreadmaw_scale', 1),),
                       outputs=(cs.ItemReward('volcanic_charm', 0),))]),
        'zero output qty rejected')
    expect_value_error(
        lambda: cs.validate_recipe_definitions(
            [cs.Recipe(key='d', inputs=(cs.ItemCost('unobtanium', 1),),
                       outputs=(cs.ItemReward('volcanic_charm', 1),))]),
        'unknown input id rejected')
    expect_value_error(
        lambda: cs.validate_recipe_definitions(
            [cs.Recipe(key='e', inputs=(cs.ItemCost('dreadmaw_scale', 1),),
                       outputs=(cs.ItemReward('unobtanium', 1),))]),
        'unknown output id rejected')

    # --- concurrent crafts: no overspend, outputs == successes ---
    import threading as _th
    G2, racers = 42, 4
    give(G2, 'r', 'dreadmaw_scale', 7)
    bar = _th.Barrier(racers)
    cout = {}

    def _race(i):
        bar.wait()
        cout[i] = cs.service.craft(G2, 'r', 'volcanic_charm')

    threads = [_th.Thread(target=_race, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = sum(1 for v in cout.values() if v.ok)
    check(wins == 2, 'only affordable crafts succeed')
    check(qty(G2, 'r', 'dreadmaw_scale') == 1, 'consumed never exceeds balance')
    check(qty(G2, 'r', 'volcanic_charm') == 2, 'outputs equal successful crafts')

    # --- raid usable hooks (worldboss_service, additive branches) ---
    from services import worldboss_service as wb
    G3, F = 43, 'fighter'
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
            'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (25, 'pikachu', '["electric"]', 35, 55, 40, 50, 50, 90, '', 45, 0, 0, 0))
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G3), F, 25, 100, 0, 0, '', 1, '', '', 0, 0, ''))
    T = 3_000_000_000
    r = wb.start_boss(G3, 'dreadmaw', T, max_hp=5000)
    check(r['ok'], 'hook fixture boss starts')
    bid = r['boss_id']
    a = wb.attack_boss(G3, F, T, rng=FakeRng(rolls=[0.5] * 8))
    check(a['ok'], 'hook fixture attack lands')

    def _snap():
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT hp, max_hp, healed FROM world_boss_combat '
                               'WHERE boss_id=? AND user_id=?', (bid, F)).fetchone()
            return dict(row)

    def _hurt(dmg=30):
        with db.conn_ctx() as conn:
            conn.execute('UPDATE world_boss_combat SET hp=max(1, hp-?) '
                         'WHERE boss_id=? AND user_id=?', (dmg, bid, F))

    # charm heal with no charm: denied, snapshot untouched
    _hurt()
    hp_before = _snap()['hp']
    h = wb.heal_fighter(G3, F, T + 5, use_charm=True)
    check(not h['ok'] and h['code'] == 'NO_CHARM', 'charm heal needs the charm')
    check(_snap()['hp'] == hp_before, 'denied charm heal changes nothing')

    # charm heal works, shares the once-per-raid budget
    give(G3, F, 'volcanic_charm', 1)
    h = wb.heal_fighter(G3, F, T + 6, use_charm=True)
    s = _snap()
    check(h['ok'] and h['code'] == 'HEALED', 'charm heal succeeds')
    check(s['hp'] == s['max_hp'] and s['healed'] == 1, 'charm heal tops up + flags')
    check(qty(G3, F, 'volcanic_charm') == 0, 'charm consumed')
    _hurt()
    h = wb.heal_fighter(G3, F, T + 7)
    check(not h['ok'] and h['code'] == 'HEAL_USED', 'coin heal blocked after charm heal')
    give(G3, F, 'volcanic_charm', 1)
    h = wb.heal_fighter(G3, F, T + 8, use_charm=True)
    check(not h['ok'] and h['code'] == 'HEAL_USED', 'second charm heal blocked')
    check(qty(G3, F, 'volcanic_charm') == 1, 'blocked charm heal keeps the charm')

    # cooldown reset: needs participation + active cooldown + charm
    c = wb.reset_cooldown(G3, 'stranger', T + 9)
    check(not c['ok'] and c['code'] == 'NO_PARTICIPATION', 'reset needs participation')
    c = wb.reset_cooldown(G3, F, T + 10)
    check(not c['ok'] and c['code'] == 'NO_CHARM', 'reset needs the charm')
    give(G3, F, 'tidal_charm', 1)
    c = wb.reset_cooldown(G3, F, T + 10)
    check(c['ok'] and c['code'] == 'CLEARED', 'cooldown reset succeeds')
    check(qty(G3, F, 'tidal_charm') == 0, 'tidal charm consumed')
    with db.conn_ctx() as conn:
        la = conn.execute('SELECT last_action_at FROM world_boss_parts WHERE boss_id=? '
                          'AND user_id=?', (bid, F)).fetchone()['last_action_at']
    check(la == 0, 'cooldown cleared to zero')
    give(G3, F, 'tidal_charm', 1)
    c = wb.reset_cooldown(G3, F, T + 200)
    check(not c['ok'] and c['code'] == 'COOLDOWN_READY', 'ready cooldown keeps charm')
    check(qty(G3, F, 'tidal_charm') == 1, 'idle reset consumes nothing')

    # raid charm: heal + clear together, same heal budget
    _hurt(40)
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_parts SET last_action_at=? WHERE boss_id=? '
                     'AND user_id=?', (T + 200, bid, F))
    u = wb.use_raid_charm(G3, F, T + 201)
    check(not u['ok'] and u['code'] == 'HEAL_USED', 'rally blocked by spent heal budget')
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_combat SET healed=0 WHERE boss_id=? AND user_id=?',
                     (bid, F))
    u = wb.use_raid_charm(G3, F, T + 201)
    check(not u['ok'] and u['code'] == 'NO_CHARM', 'rally needs the charm')
    give(G3, F, 'raid_charm', 1)
    u = wb.use_raid_charm(G3, F, T + 201)
    s = _snap()
    check(u['ok'] and u['code'] == 'RALLIED', 'rally succeeds')
    check(s['hp'] == s['max_hp'] and s['healed'] == 1, 'rally heals + flags')
    check(qty(G3, F, 'raid_charm') == 0, 'raid charm consumed')
    with db.conn_ctx() as conn:
        la = conn.execute('SELECT last_action_at FROM world_boss_parts WHERE boss_id=? '
                          'AND user_id=?', (bid, F)).fetchone()['last_action_at']
    check(la == 0, 'rally clears cooldown too')

    # coin heal path unchanged (regression: fresh fighter, own boss)
    G4, W = 44, 'rich'
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(G4), W, 25, 100, 0, 0, '', 1, '', '', 0, 0, ''))
    from services._common import credit_cash
    with db.conn_ctx() as conn:
        credit_cash(conn, G4, W, 5000)
    wb.start_boss(G4, 'dreadmaw', T, max_hp=5000)
    wb.attack_boss(G4, W, T, rng=FakeRng(rolls=[0.5] * 8))
    with db.conn_ctx() as conn:
        conn.execute('UPDATE world_boss_combat SET hp=10 WHERE guild_id=? AND user_id=?',
                     (str(G4), W))
    h = wb.heal_fighter(G4, W, T + 5)
    check(h['ok'] and h['code'] == 'HEALED' and 'charm' not in h, 'coin heal unchanged')

    # --- cog render smoke (headless) ---
    from cogs.crafting import (ConfirmView, build_confirm_text, build_recipe_detail_text,
                               build_recipe_list_text, build_result_text)
    views = cs.service.list_recipes(GID, U)
    ltxt = build_recipe_list_text(GID, views)
    check('Volcanic Charm' in ltxt and 'Tidal Charm' in ltxt and 'Raid Charm' in ltxt,
          'craft list renders all recipes')
    check('Dreadmaw Scale' in ltxt, 'craft list shows costs')
    dtxt = build_recipe_detail_text(GID, views[0])
    check('Volcanic Charm' in dtxt, 'craft detail renders')
    ctxt = build_confirm_text(GID, views[0], 3)
    check('3' in ctxt and 'Dreadmaw Scale' in ctxt, 'confirm scales preview')
    rtxt = build_result_text(GID, cs.service.craft(GID, 'nobody', 'volcanic_charm'))
    check('Missing' in rtxt or 'missing' in rtxt or 'Missing materials' in rtxt,
          'result renders shortfall')
    import json as _json
    _lang = _json.load(open(ROOT / 'lang.json', encoding='utf-8'))
    for _k in ('eco.craft_title', 'eco.craft_done', 'eco.craft_missing',
               'eco.craft_confirm', 'eco.craft_no_recipe', 'eco.craft_bad_qty',
               'eco.craft_disabled', 'eco.wb_healed_charm', 'eco.wb_cleared',
               'eco.wb_rallied', 'eco.wb_no_charm', 'eco.wb_use_unknown',
               'eco.craft_name_raid_charm', 'eco.craft_desc_raid_charm'):
        check(_k in _lang['en'] and _k in _lang['pl'], f'lang key {_k} in en+pl')
    view = ConfirmView(GID, U, 'volcanic_charm', 1)
    check(len(view.children) == 2, 'confirm view has forge + cancel')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()

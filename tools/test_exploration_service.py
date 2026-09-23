"""Exploration service regression: travel state, location pools,
pool-fed spawning — first consumer of the services layer.

Run: python tools/test_exploration_service.py (needs discord installed
for cog imports). Exit code 0 only when every test passes.
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


GID = 5150
UID = 'trainer1'

DEX = [
    (25, 'pikachu', '["electric"]', 0),
    (1, 'bulbasaur', '["grass", "poison"]', 0),
    (7, 'squirtle', '["water"]', 0),
    (4, 'charmander', '["fire"]', 0),
    (74, 'geodude', '["rock", "ground"]', 0),
    (129, 'magikarp', '["water"]', 0),
    (150, 'mewtwo', '["psychic"]', 1),
]


def main() -> None:
    import database as db
    db.DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
    db.init_db()

    from services import exploration_service as ex
    from services.encounter_service import spawn_encounter

    src = (ROOT / 'services' / 'exploration_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context'):
        check(bad not in src, f'no {bad} in service')

    with db.conn_ctx() as conn:
        for dex, name, types, leg in DEX:
            conn.execute(
                'INSERT INTO pk_dex (dex, name, types, hp, atk, dfn, spa, spd, spe, '
                'sprite, rate, legendary, evo_to, evo_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (dex, name, types, 50, 50, 50, 50, 50, 50, '', 45, leg, 0, 0))
        conn.execute(
            'INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
            'active, ivs, evs, locked, fav, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(GID), UID, 25, 10, 0, 0, '', 1, '', '', 0, 0, ''))

    # --- travel state ---
    check(ex.get_location(GID, UID) == 'meadow', 'default location is meadow')
    check(ex.set_location(GID, UID, 'Forest') is True, 'travel accepts names case-insensitively')
    check(ex.get_location(GID, UID) == 'forest', 'location persists normalized')
    check(ex.set_location(GID, UID, 'moon') is False, 'unknown location rejected')
    check(ex.get_location(GID, UID) == 'forest', 'failed travel keeps state')

    # --- pools from pk_dex types ---
    check(ex.resolve_pool(GID, 'meadow') is None, 'meadow = anywhere (uniform)')
    check(ex.resolve_pool(GID, 'forest') == [1], 'forest pool')
    check(sorted(ex.resolve_pool(GID, 'lake')) == [7, 129], 'lake pool')
    check(ex.resolve_pool(GID, 'volcano') == [4], 'volcano pool')
    check(ex.resolve_pool(GID, 'cave') == [74], 'cave pool')
    check(ex.resolve_pool(GID, 'moon') is None, 'unknown location falls back')

    # --- pool-fed spawn stays inside the pool ---
    store, cds = {}, {}

    async def spawn(loc):
        pool = ex.resolve_pool(GID, loc)
        return await spawn_encounter(GID, UID, 'Tester', 'catch',
                                     store=store, cooldowns=dict(cds), dex_pool=pool)

    for loc, want in (('forest', {1}), ('lake', {7, 129}), ('volcano', {4})):
        res = asyncio.run(spawn(loc))
        check(res.ok, f'{loc} spawn succeeds')
        if res.ok:
            check(store[(str(GID), UID)]['dex'] in want, f'{loc} dex inside pool')
            check(len(res.desc) > 50, f'{loc} description built')
            check(isinstance(res.media, list), f'{loc} media bundle')

    # meadow (pool None) still spawns like the old uniform path
    res = asyncio.run(spawn('meadow'))
    check(res.ok and store[(str(GID), UID)]['dex'] >= 1, 'meadow uniform spawn')

    # empty dex table never dead-ends
    with db.conn_ctx() as conn:
        conn.execute('DELETE FROM pk_dex')
    check(ex.resolve_pool(GID, 'forest') is None, 'empty dex falls back to uniform')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

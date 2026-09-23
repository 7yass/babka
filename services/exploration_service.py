"""Exploration service (first new system on services): locations + travel.

A location filters the species pool that feeds spawn_encounter; travel
state lives in the existing meta key/value table (zero migration).
Rewards, catching, cooldowns and rendering all reuse the current
pipeline — exploration only decides WHERE you hunt.
"""
import database as db

DEFAULT_LOCATION = 'meadow'

# key -> (display name, blurb, pk_dex types filter). Empty types = anywhere.
LOCATIONS = {
    'meadow': ('Meadow', 'Open fields. Anything can appear.', ()),
    'forest': ('Forest', 'Tall grass and buzzing wings.', ('grass', 'bug')),
    'cave': ('Cave', 'Dark tunnels. Rock and ground types.', ('rock', 'ground')),
    'lake': ('Lake', 'Still water. Water types gather here.', ('water',)),
    'volcano': ('Volcano', 'Scorched rock. Fire types.', ('fire',)),
}


def _meta_key(gid, uid) -> str:
    return f'explore:{gid}:{uid}'


def get_location(gid, uid) -> str:
    loc = db.meta_get(_meta_key(gid, uid), DEFAULT_LOCATION)
    return loc if loc in LOCATIONS else DEFAULT_LOCATION


def set_location(gid, uid, key: str) -> bool:
    """Travel. Returns False for unknown locations (state unchanged)."""
    key = (key or '').lower().strip()
    if key not in LOCATIONS:
        return False
    db.meta_set(_meta_key(gid, uid), key)
    return True


def location_info(key: str):
    key = (key or '').lower().strip()
    return LOCATIONS.get(key)


def resolve_pool(gid, location: str) -> list | None:
    """Dex pool for a location from pk_dex types. None = anywhere
    (uniform 1-809, exactly the old behavior) — also the fallback when
    the dex table is empty (fresh DB) so exploration never dead-ends."""
    info = location_info(location)
    if not info or not info[2]:
        return None
    clauses = ' OR '.join('types LIKE ?' for _ in info[2])
    with db.conn_ctx() as conn:
        try:
            rows = conn.execute(
                f'SELECT dex FROM pk_dex WHERE {clauses} ORDER BY dex',
                tuple(f'%{t}%' for t in info[2])).fetchall()
        except Exception:
            return None
    pool = [int(r['dex']) for r in rows if (r['dex'] or 0) > 0]
    return pool or None

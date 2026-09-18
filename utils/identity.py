"""Player identity: read-only summaries over existing data. NO WRITES.

Every function here performs SELECTs only — the profile card consumes them
but nothing here may INSERT/UPDATE/DELETE. New tables are out of scope.

Deliberate gaps (documented, not papered over):
- Heist record: the heists table holds only the RUNNING heist (deleted on
  resolve), bounties only OPEN contracts. No lifetime heist history exists,
  so there is no heist line until something records it.
- Biggest trade: market listings are deleted on purchase and direct swaps
  are unrecorded. No completed-trade history exists.
- Gambler class: casino sessions are not counted durably (gamble_n resets
  hourly; god_pity only ticks for the house). No Gambler class until a
  sessions counter exists.
- Draws: pk_stats has no draws column, so duel records omit them.

Denominator: DEX_TOTAL = 809 (the catchable pool: hunts roll 1-809).
Shinies, forms and event mons never inflate it — numerator counts
DISTINCT dex discovered.
"""
import database as db

DEX_TOTAL = 809

# (class, minimum bar) evaluated in CLASS_ORDER. One isolated session can
# never classify anyone: every rule needs sustained activity.
CLASS_ORDER = ('collector', 'battler', 'trader', 'worker', 'criminal')

# Explicit achievement tiers (highest first); ties break by earliest unlock.
# BADGES entries are (icon, short, display, desc) — no tier field, so the
# order lives here, deterministically.
ACHIEVEMENT_TIERS = (
    'npc_champ',
    'rich5m',
    'lvl50',
    'rich1m',
    'lvl25',
    'region_kanto', 'region_johto', 'region_hoenn', 'region_sinnoh',
    'grinder',
    'highroller',
    'lifer',
    'famous',
    'lvl10',
    'rich100k',
    'first_job',
)

# Title priority (first match wins). Class titles are the fallback.
TITLE_PRIORITY = (
    'gym_champion',      # all 3 NPCS beaten at least once
    'master_collector',  # dex distinct >= 100
    'market_maker',      # 5+ open listings
    'crime_boss',        # criminal score >= 6
    'job_specialist',   # 50+ career shifts
    'regional_expert',   # any region badge
)


def _one(query, params=()):
    with db.conn_ctx() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else {}


def get_dex_completion(gid, uid) -> dict:
    """{'distinct': int, 'total': 809, 'pct': float}. Shinies don't inflate."""
    r = _one('SELECT COUNT(DISTINCT dex) n FROM pk_dexcount '
             'WHERE guild_id=? AND user_id=?', (str(gid), str(uid)))
    n = r.get('n') or 0
    return {'distinct': n, 'total': DEX_TOTAL,
            'pct': round(100.0 * n / DEX_TOTAL, 1)}


def get_duel_record(gid, uid) -> dict:
    """{'w': int, 'l': int}. No draws column exists — omitted by design."""
    r = _one('SELECT duels_won, duels_lost FROM pk_stats WHERE guild_id=? AND user_id=?',
             (str(gid), str(uid)))
    return {'w': r.get('duels_won') or 0, 'l': r.get('duels_lost') or 0}


def get_top_achievement(gid, uid):
    """(akey, display_name, unlocked_at) of the highest-tier completed
    achievement, earliest unlock breaking ties. None when empty."""
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT akey, unlocked_at FROM achievements '
                            'WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
    done = {r['akey']: (r['unlocked_at'] or 0) for r in rows}
    if not done:
        return None
    try:
        from cogs.achievements import BADGES
    except Exception:
        BADGES = {}
    rank = {k: i for i, k in enumerate(ACHIEVEMENT_TIERS)}
    best = min(done, key=lambda k: (rank.get(k, len(rank)), done[k], k))
    meta = BADGES.get(best) or ()
    name = meta[2] if len(meta) > 2 else best
    return best, name, done[best]


def _scores(gid, uid) -> dict:
    """Raw activity signals. All thresholds live in get_trainer_class."""
    dex = get_dex_completion(gid, uid)['distinct']
    shiny = (_one('SELECT COUNT(*) n FROM pk_mons WHERE guild_id=? AND owner_id=? AND shiny=1',
                  (str(gid), str(uid))).get('n') or 0)
    duels = get_duel_record(gid, uid)
    npc = (_one('SELECT COUNT(*) n FROM pk_npc WHERE guild_id=? AND user_id=?',
                (str(gid), str(uid))).get('n') or 0)
    listings = (_one('SELECT COUNT(*) n FROM pk_market WHERE guild_id=? AND seller_id=?',
                     (str(gid), str(uid))).get('n') or 0)
    shifts = (_one('SELECT shifts FROM jobs WHERE guild_id=? AND user_id=?',
                   (str(gid), str(uid))).get('shifts') or 0)
    eco = _one('SELECT last_rob FROM eco WHERE guild_id=? AND user_id=?',
               (str(gid), str(uid)))
    robbed = bool((eco.get('last_rob') or 0) > 0)
    posted = (_one('SELECT COUNT(*) n FROM bounties WHERE guild_id=? AND by_id=?',
                   (str(gid), str(uid))).get('n') or 0)
    return {'collector': dex + 5 * shiny,
            'battler': (duels['w'] + duels['l']) + 3 * npc,
            'trader': listings,
            'worker': shifts,
            'criminal': (2 if robbed else 0) + min(posted, 3),
            '_dex': dex, '_duels': duels['w'] + duels['l'], '_npc': npc,
            '_shifts': shifts}


def get_trainer_class(gid, uid):
    """Deterministic class or None. Thresholds defeat one-off events;
    CLASS_ORDER breaks ties (collector first, criminal last)."""
    s = _scores(gid, uid)
    ok = {
        'collector': s['_dex'] >= 10,
        'battler': s['_duels'] >= 5,
        'trader': s['trader'] >= 2,
        'worker': s['_shifts'] >= 5,
        'criminal': s['criminal'] >= 2,
    }
    cands = [(s[k], CLASS_ORDER.index(k), k) for k in CLASS_ORDER if ok[k]]
    if not cands:
        return None
    cands.sort(key=lambda t: (-t[0], t[1]))
    return cands[0][2]


def get_activity_title(gid, uid):
    """Primary title key (see TITLE_* lang keys) or 'newcomer'. Same verified
    metrics as the class, kept separate so titles can evolve alone."""
    s = _scores(gid, uid)
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT akey FROM achievements WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
    has = {r['akey'] for r in rows}
    if s['_npc'] >= 3:
        return 'gym_champion'
    if s['_dex'] >= 100:
        return 'master_collector'
    if s['trader'] >= 5:
        return 'market_maker'
    if s['criminal'] >= 6:
        return 'crime_boss'
    if s['_shifts'] >= 50:
        return 'job_specialist'
    if has & {'region_kanto', 'region_johto', 'region_hoenn', 'region_sinnoh'}:
        return 'regional_expert'
    cls = get_trainer_class(gid, uid)
    return cls if cls in ('collector', 'battler', 'trader', 'worker', 'criminal') else 'newcomer'


# Heist record and biggest trade have no durable source (see module docs).
# They stay OUT of the card until something records them — no proxies.
def get_heist_record(gid, uid):
    """Always None: no heist history table exists. Explicit, not missing."""
    return None


def get_biggest_trade(gid, uid):
    """Always None: no completed-trade history exists. Explicit, not missing."""
    return None

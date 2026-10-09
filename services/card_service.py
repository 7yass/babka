"""Anime card service — scalable to 2000+ cards.

Discord-free: no ctx, no views. Handles pack opening, pity, print numbers, dust.
Reuses DB tables added in database.py: anime_sets, anime_cards, anime_collection, anime_pity, anime_dust.

Design for 2000+:
- Rarity pools queried once per pack, not per card (indexed)
- Print number assigned via MAX(print_no) inside transaction (atomic)
- Pity counter per guild+user, reset on SR+ pull
"""
import random
import time

import database as db

# Pack definitions: (pulls, guaranteed_min_rarity)
# Rarity order C < R < SR < LR < UR
PACKS = {
    'card_pack_std': {'pulls': 3, 'guaranteed': None, 'weights': {'C': 70, 'R': 22, 'SR': 7, 'LR': 0.8, 'UR': 0.2}},
    'card_pack_mono': {'pulls': 3, 'guaranteed': 'SR', 'weights': {'C': 60, 'R': 25, 'SR': 12, 'LR': 2.5, 'UR': 0.5}},
    'card_pack_animated': {'pulls': 2, 'guaranteed': None, 'animated_one': True, 'weights': {'C': 50, 'R': 30, 'SR': 15, 'LR': 4, 'UR': 1}},
}

RARITY_ORDER = ['C', 'R', 'SR', 'LR', 'UR']
DUST_VALUE = {'C': 100, 'R': 400, 'SR': 1500, 'LR': 5000, 'UR': 10000}
PITY_THRESHOLD = 10  # packs without SR+ -> next pack guaranteed SR


def _choose_rarity(weights: dict, rng) -> str:
    total = sum(weights.values())
    roll = rng.random() * total
    acc = 0
    for rar, w in weights.items():
        acc += w
        if roll < acc:
            return rar
    return 'C'


def _pick_card(pool, rng):
    if not pool:
        return None
    return rng.choice(pool)


def open_pack(gid, uid, pack_key: str, rng=None) -> dict:
    """Open one pack. Returns {cards: [...], dust_earned: int, pity: int}"""
    gid, uid = str(gid), str(uid)
    cfg = PACKS.get(pack_key)
    if not cfg:
        return {'cards': [], 'error': 'unknown_pack'}
    rng = rng or random

    with db.conn_ctx() as conn:
        # Load pools once
        pools = {}
        counts = {}
        for rar in RARITY_ORDER:
            rows = conn.execute('SELECT * FROM anime_cards WHERE rarity=?', (rar,)).fetchall()
            # animated filter
            if cfg.get('animated_one'):
                # for animated pack, separate pools but for now same logic
                pass
            pools[rar] = [dict(r) for r in rows]
            counts[rar] = len(pools[rar])

        # If DB empty (no cards imported yet), return placeholder
        total_cards = sum(counts.values())
        if total_cards == 0:
            return {'cards': [], 'empty': True}

        # Pity check
        row = conn.execute('SELECT pulls_without_sr FROM anime_pity WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        pity = int(row['pulls_without_sr'] or 0) if row else 0

        pulls = cfg['pulls']
        guaranteed = cfg.get('guaranteed')
        # Pity triggers SR guarantee
        if pity >= PITY_THRESHOLD and not guaranteed:
            guaranteed = 'SR'

        cards = []
        got_sr = False
        for i in range(pulls):
            # last pull guaranteed
            need = guaranteed if (i == pulls - 1 and guaranteed) else None
            if need:
                rar = need if pools.get(need) and pools[need] else 'SR' if pools.get('SR') else 'R'
                # if need rarity has no cards (e.g. no LR), fall back to highest available
                if not pools.get(rar) or not pools[rar]:
                    for fb in reversed(RARITY_ORDER):
                        if pools.get(fb) and pools[fb]:
                            rar = fb
                            break
            else:
                rar = _choose_rarity(cfg['weights'], rng)
                # fallback if no cards of that rarity
                if not pools.get(rar) or not pools[rar]:
                    # pick closest available rarity
                    for fb in ['R', 'C', 'SR', 'LR']:
                        if pools.get(fb) and pools[fb]:
                            rar = fb
                            break

            card = _pick_card(pools.get(rar, []), rng)
            if not card:
                continue
            if rar in ('SR', 'LR', 'UR'):
                got_sr = True

            # Animated variant handling
            is_animated = bool(card['is_animated'])
            if cfg.get('animated_one') and i == 0:
                # force animated if available, else keep as is
                anim_pool = [c for c in pools.get(rar, []) if c['is_animated']]
                if anim_pool:
                    card = rng.choice(anim_pool)
                    is_animated = True

            # Print number for limited editions
            print_no = 0
            print_total = int(card['print_total'] or 0)
            if print_total > 0:
                # Check sold out
                cnt = conn.execute('SELECT COUNT(*) c FROM anime_collection WHERE guild_id=? AND card_id=?', (gid, card['id'])).fetchone()['c']
                if cnt >= print_total:
                    # sold out -> reroll within same rarity excluding sold out
                    avail = [c for c in pools[rar] if int(c['print_total'] or 0) == 0 or conn.execute('SELECT COUNT(*) c FROM anime_collection WHERE guild_id=? AND card_id=?', (gid, c['id'])).fetchone()['c'] < int(c['print_total'] or 1)]
                    if avail:
                        card = rng.choice(avail)
                        print_total = int(card['print_total'] or 0)
                    else:
                        # all sold out in this rarity, fallback
                        continue
                # assign next print number (global per guild+card)
                mx = conn.execute('SELECT MAX(print_no) m FROM anime_collection WHERE guild_id=? AND card_id=?', (gid, card['id'])).fetchone()['m']
                print_no = (int(mx or 0) + 1)
                if print_no > print_total:
                    continue

            # Insert into collection
            existing = conn.execute('SELECT qty FROM anime_collection WHERE guild_id=? AND user_id=? AND card_id=? AND print_no=?', (gid, uid, card['id'], print_no)).fetchone()
            if existing:
                conn.execute('UPDATE anime_collection SET qty=qty+1 WHERE guild_id=? AND user_id=? AND card_id=? AND print_no=?', (gid, uid, card['id'], print_no))
            else:
                conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, obtained_at, qty) VALUES (?,?,?,?,?,1)', (gid, uid, card['id'], print_no, int(time.time())))
            tot = conn.execute('SELECT SUM(qty) q FROM anime_collection WHERE guild_id=? AND user_id=? AND card_id=?',
                               (gid, uid, card['id'])).fetchone()['q'] or 0

            cards.append({
                'id': card['id'], 'code': card['code'], 'rarity': card['rarity'],
                'name': card['name'], 'set_id': card['set_id'],
                'print_no': print_no, 'print_total': print_total,
                'is_animated': is_animated, 'image_url': card['image_url'],
                'is_new': tot <= 1,
            })

        # Update pity
        new_pity = 0 if got_sr else pity + 1
        conn.execute('INSERT OR REPLACE INTO anime_pity (guild_id, user_id, pulls_without_sr) VALUES (?,?,?)', (gid, uid, new_pity))

    invalidate_perks(gid, uid)  # collection changed: set-completion may flip
    return {'cards': cards, 'pity': new_pity, 'got_sr': got_sr}


def get_collection(gid, uid, set_id=None, page=0, per_page=50, owned_only=False) -> dict:
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        if owned_only:
            # Only cards the user actually holds, rarest first (the fun view).
            filt = 'AND k.set_id=?' if set_id else ''
            args = [gid, uid] + ([set_id] if set_id else [])
            total = conn.execute(
                f'SELECT COUNT(DISTINCT k.id) c FROM anime_cards k '
                f'JOIN (SELECT DISTINCT card_id FROM anime_collection '
                f'WHERE guild_id=? AND user_id=?) o ON o.card_id=k.id '
                f'WHERE 1=1 {filt}', args).fetchone()['c']
            cards = conn.execute(
                f'SELECT k.* FROM anime_cards k '
                f'JOIN (SELECT DISTINCT card_id FROM anime_collection '
                f'WHERE guild_id=? AND user_id=?) o ON o.card_id=k.id '
                f'WHERE 1=1 {filt} '
                f"ORDER BY CASE k.rarity WHEN 'UR' THEN 0 WHEN 'LR' THEN 1 "
                f"WHEN 'SR' THEN 2 WHEN 'R' THEN 3 ELSE 4 END, k.code "
                f'LIMIT ? OFFSET ?', args + [per_page, page * per_page]).fetchall()
        elif set_id:
            total = conn.execute('SELECT COUNT(*) c FROM anime_cards WHERE set_id=?', (set_id,)).fetchone()['c']
            cards = conn.execute('SELECT * FROM anime_cards WHERE set_id=? ORDER BY code LIMIT ? OFFSET ?', (set_id, per_page, page*per_page)).fetchall()
        else:
            total = conn.execute('SELECT COUNT(*) c FROM anime_cards').fetchone()['c']
            cards = conn.execute('SELECT * FROM anime_cards ORDER BY set_id, code LIMIT ? OFFSET ?', (per_page, page*per_page)).fetchall()
        owned = {r['card_id']: r for r in conn.execute('SELECT card_id, SUM(qty) qty FROM anime_collection WHERE guild_id=? AND user_id=? GROUP BY card_id', (gid, uid)).fetchall()}
        sets = conn.execute('SELECT * FROM anime_sets').fetchall()
        grand = conn.execute('SELECT COUNT(*) c FROM anime_cards').fetchone()['c'] or 0
        dust_row = conn.execute('SELECT dust FROM anime_dust WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        dust = int(dust_row['dust'] or 0) if dust_row else 0
    return {
        'cards': [dict(c) for c in cards],
        'owned': {k: int(v['qty'] or 0) for k, v in owned.items()},
        'total': total,
        'grand_total': grand,
        'sets': [dict(s) for s in sets],
        'dust': dust,
        'page': page,
        'per_page': per_page,
    }


def set_progress(gid, uid) -> list:
    """[(set_id, owned_distinct, total)] — one grouped query for the header."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        rows = conn.execute(
            'SELECT s.id sid, s.total_cards tot, COUNT(DISTINCT c.card_id) own '
            'FROM anime_sets s '
            'LEFT JOIN anime_cards k ON k.set_id=s.id '
            'LEFT JOIN anime_collection c ON c.card_id=k.id '
            'AND c.guild_id=? AND c.user_id=? '
            'GROUP BY s.id ORDER BY s.id',
            (gid, uid)).fetchall()
    return [(r['sid'], int(r['own'] or 0), int(r['tot'] or 0)) for r in rows]


RAR_COLORS = {'C': 0x9AA0A6, 'R': 0x3498DB, 'SR': 0x9B59B6, 'LR': 0xF1C40F, 'UR': 0xE74C3C}
RAR_EMOJI = {'C': '⚪', 'R': '🔵', 'SR': '🟣', 'LR': '🟡', 'UR': '🔴'}


def get_user_stats(gid, uid) -> dict:
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        total = conn.execute('SELECT COUNT(*) c FROM anime_cards').fetchone()['c']
        owned_distinct = conn.execute('SELECT COUNT(DISTINCT card_id) c FROM anime_collection WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()['c']
        owned_total = conn.execute('SELECT SUM(qty) c FROM anime_collection WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()['c']
        dust_row = conn.execute('SELECT dust FROM anime_dust WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        pity_row = conn.execute('SELECT pulls_without_sr FROM anime_pity WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
    return {
        'total_cards': total or 0,
        'owned_distinct': owned_distinct or 0,
        'owned_total': owned_total or 0,
        'dust': int(dust_row['dust'] or 0) if dust_row else 0,
        'pity': int(pity_row['pulls_without_sr'] or 0) if pity_row else 0,
    }


def convert_duplicates(gid, uid) -> dict:
    """Convert duplicate qty>1 into dust. Returns dust earned."""
    gid, uid = str(gid), str(uid)
    earned = 0
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT card_id, print_no, qty FROM anime_collection WHERE guild_id=? AND user_id=? AND qty>1', (gid, uid)).fetchall()
        for r in rows:
            card = conn.execute('SELECT rarity FROM anime_cards WHERE id=?', (r['card_id'],)).fetchone()
            if not card:
                continue
            extra = int(r['qty'] or 0) - 1
            if extra <= 0:
                continue
            dust_per = DUST_VALUE.get(card['rarity'], 100)
            earned += extra * dust_per
            conn.execute('UPDATE anime_collection SET qty=1 WHERE guild_id=? AND user_id=? AND card_id=? AND print_no=?', (gid, uid, r['card_id'], r['print_no']))
        if earned:
            conn.execute('INSERT OR IGNORE INTO anime_dust (guild_id, user_id, dust) VALUES (?,?,0)', (gid, uid))
            conn.execute('UPDATE anime_dust SET dust=dust+? WHERE guild_id=? AND user_id=?', (earned, gid, uid))
    invalidate_perks(gid, uid)
    return {'dust_earned': earned}


def reroll_with_dust(gid, uid, cost=10000) -> dict:
    """Spend dust to get random missing card."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        dust_row = conn.execute('SELECT dust FROM anime_dust WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        dust = int(dust_row['dust'] or 0) if dust_row else 0
        if dust < cost:
            return {'ok': False, 'code': 'no_dust', 'have': dust, 'need': cost}
        # find missing cards
        missing = conn.execute('SELECT id FROM anime_cards WHERE id NOT IN (SELECT card_id FROM anime_collection WHERE guild_id=? AND user_id=?)', (gid, uid)).fetchall()
        if not missing:
            return {'ok': False, 'code': 'complete'}
        pick = random.choice(missing)['id']
        card = conn.execute('SELECT * FROM anime_cards WHERE id=?', (pick,)).fetchone()
        conn.execute('UPDATE anime_dust SET dust=dust-? WHERE guild_id=? AND user_id=?', (cost, gid, uid))
        # insert with print_no handling
        print_total = int(card['print_total'] or 0)
        print_no = 0
        if print_total > 0:
            mx = conn.execute('SELECT MAX(print_no) m FROM anime_collection WHERE guild_id=? AND card_id=?', (gid, pick)).fetchone()['m']
            print_no = (int(mx or 0) + 1)
            if print_no > print_total:
                return {'ok': False, 'code': 'sold_out'}
        conn.execute('INSERT OR REPLACE INTO anime_collection (guild_id, user_id, card_id, print_no, obtained_at, qty) VALUES (?,?,?,?,?,1)', (gid, uid, pick, print_no, int(time.time())))
    invalidate_perks(gid, uid)
    return {'ok': True, 'card': dict(card), 'print_no': print_no}


# ---------- buddy + set-completion perks ----------
# Buddy: one equipped owned card. Set bonus: owning every card in a series.
# Both are computed lazily and cached briefly — user_boost() runs on every
# XP tick, so this must never become N queries per message.
BUDDY_XP_PCT = {'C': 1, 'R': 2, 'SR': 3, 'LR': 5, 'UR': 8}
BUDDY_DAILY = {'C': 0, 'R': 50, 'SR': 150, 'LR': 400, 'UR': 1000}
SET_DAILY_EACH = 250  # flat daily coins per completed series set
_PERK_CACHE = {}
_PERK_TTL = 300


def _perk_cache_key(gid, uid):
    return (str(gid), str(uid))


def invalidate_perks(gid=None, uid=None):
    """Drop cached perk math (call after pack opens/dust/reroll/buddy change)."""
    if gid is None:
        _PERK_CACHE.clear()
    else:
        _PERK_CACHE.pop(_perk_cache_key(gid, uid), None)


def set_buddy(gid, uid, code: str) -> dict:
    """Equip an owned card as buddy. Returns {ok, card} or {ok, code}."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        card = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                            (code, code)).fetchone()
        if not card:
            return {'ok': False, 'code': 'no_card'}
        c = dict(card)
        own = conn.execute('SELECT SUM(qty) q FROM anime_collection '
                           'WHERE guild_id=? AND user_id=? AND card_id=?',
                           (gid, uid, c['id'])).fetchone()['q'] or 0
        if not own:
            return {'ok': False, 'code': 'not_owned'}
        conn.execute('INSERT OR REPLACE INTO card_buddy (guild_id, user_id, card_id) '
                     'VALUES (?,?,?)', (gid, uid, c['id']))
    invalidate_perks(gid, uid)
    return {'ok': True, 'card': c}


def clear_buddy(gid, uid) -> None:
    with db.conn_ctx() as conn:
        conn.execute('DELETE FROM card_buddy WHERE guild_id=? AND user_id=?',
                     (str(gid), str(uid)))
    invalidate_perks(gid, uid)


def get_buddy(gid, uid):
    """Equipped card row, or None (missing / no longer owned)."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT card_id FROM card_buddy WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        if not row:
            return None
        card = conn.execute('SELECT * FROM anime_cards WHERE id=?', (row['card_id'],)).fetchone()
        if not card:
            return None
        own = conn.execute('SELECT SUM(qty) q FROM anime_collection '
                           'WHERE guild_id=? AND user_id=? AND card_id=?',
                           (gid, uid, row['card_id'])).fetchone()['q'] or 0
        if not own:
            return None
        return dict(card)


def set_completion(gid, uid) -> dict:
    """{completed: [set_ids], count} — a set is complete when the user owns
    every distinct card_id in it."""
    gid, uid = str(gid), str(uid)
    done = []
    with db.conn_ctx() as conn:
        sets = [r['id'] for r in conn.execute('SELECT id FROM anime_sets').fetchall()]
        for sid in sets:
            total = conn.execute('SELECT COUNT(*) c FROM anime_cards WHERE set_id=?',
                                 (sid,)).fetchone()['c'] or 0
            if not total:
                continue
            owned = conn.execute('SELECT COUNT(DISTINCT card_id) c FROM anime_collection '
                                 'WHERE guild_id=? AND user_id=? AND card_id IN '
                                 '(SELECT id FROM anime_cards WHERE set_id=?)',
                                 (gid, uid, sid)).fetchone()['c'] or 0
            if owned >= total:
                done.append(sid)
    return {'completed': done, 'count': len(done)}


def card_perks(gid, uid) -> dict:
    """{xp_pct, daily, buddy (row|None), sets_done} — cached 5 min."""
    key = _perk_cache_key(gid, uid)
    import time as _t
    hit = _PERK_CACHE.get(key)
    if hit and _t.time() - hit[0] < _PERK_TTL:
        return hit[1]
    buddy = get_buddy(gid, uid)
    comp = set_completion(gid, uid)
    rar = (buddy or {}).get('rarity', '')
    out = {
        'xp_pct': BUDDY_XP_PCT.get(rar, 0),
        'daily': BUDDY_DAILY.get(rar, 0) + SET_DAILY_EACH * comp['count'],
        'buddy': buddy,
        'sets_done': comp['completed'],
    }
    _PERK_CACHE[key] = (_t.time(), out)
    return out


def card_daily_bonus(gid, uid) -> int:
    try:
        return int(card_perks(gid, uid)['daily'])
    except Exception:
        return 0

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
    'card_pack_std': {'pulls': 3, 'guaranteed': None, 'weights': {'C': 70, 'R': 22, 'SR': 7, 'LR': 1}},
    'card_pack_mono': {'pulls': 3, 'guaranteed': 'SR', 'weights': {'C': 60, 'R': 25, 'SR': 12, 'LR': 3}},
    'card_pack_animated': {'pulls': 2, 'guaranteed': None, 'animated_one': True, 'weights': {'C': 50, 'R': 30, 'SR': 15, 'LR': 5}},
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

            cards.append({
                'id': card['id'], 'code': card['code'], 'rarity': card['rarity'],
                'name': card['name'], 'set_id': card['set_id'],
                'print_no': print_no, 'print_total': print_total,
                'is_animated': is_animated, 'image_url': card['image_url']
            })

        # Update pity
        new_pity = 0 if got_sr else pity + 1
        conn.execute('INSERT OR REPLACE INTO anime_pity (guild_id, user_id, pulls_without_sr) VALUES (?,?,?)', (gid, uid, new_pity))

    return {'cards': cards, 'pity': new_pity, 'got_sr': got_sr}


def get_collection(gid, uid, set_id=None, page=0, per_page=50) -> dict:
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        if set_id:
            total = conn.execute('SELECT COUNT(*) c FROM anime_cards WHERE set_id=?', (set_id,)).fetchone()['c']
            cards = conn.execute('SELECT * FROM anime_cards WHERE set_id=? ORDER BY code LIMIT ? OFFSET ?', (set_id, per_page, page*per_page)).fetchall()
        else:
            total = conn.execute('SELECT COUNT(*) c FROM anime_cards').fetchone()['c']
            cards = conn.execute('SELECT * FROM anime_cards ORDER BY set_id, code LIMIT ? OFFSET ?', (per_page, page*per_page)).fetchall()
        owned = {r['card_id']: r for r in conn.execute('SELECT card_id, SUM(qty) qty FROM anime_collection WHERE guild_id=? AND user_id=? GROUP BY card_id', (gid, uid)).fetchall()}
        sets = conn.execute('SELECT * FROM anime_sets').fetchall()
        dust_row = conn.execute('SELECT dust FROM anime_dust WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        dust = int(dust_row['dust'] or 0) if dust_row else 0
    return {
        'cards': [dict(c) for c in cards],
        'owned': {k: int(v['qty'] or 0) for k, v in owned.items()},
        'total': total,
        'sets': [dict(s) for s in sets],
        'dust': dust,
        'page': page,
        'per_page': per_page,
    }


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
    return {'ok': True, 'card': dict(card), 'print_no': print_no}

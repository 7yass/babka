"""Anime card service — scalable to 2000+ cards.

Discord-free: no ctx, no views. Ticket loop (drop/wish/burn), buddy +
set-completion perks, dust/reroll.
Reuses DB tables added in database.py: anime_sets, anime_cards,
anime_collection, anime_pity, anime_dust, card_buddy, card_tickets.

Design for 2000+:
- Rarity pools queried per wish (indexed), never full-loaded
- Perk math cached 5 min (user_boost runs on every XP tick)
"""
import random
import time

import database as db


RARITY_ORDER = ['C', 'R', 'SR', 'LR', 'UR']
DUST_VALUE = {'C': 100, 'R': 400, 'SR': 1500, 'LR': 5000, 'UR': 10000}
SET_NAMES = {'naruto': 'Naruto', 'rezero': 'Re:ZERO', 'bleach': 'Bleach',
             'dragonball': 'Dragon Ball', 'onepiece': 'One Piece',
             'demonslayer': 'Demon Slayer', 'jojo': "JoJo's Bizarre Adventure",
             'bluelock': 'Blue Lock', 'fairytail': 'Fairy Tail'}


def set_name(sid: str) -> str:
    return SET_NAMES.get(sid, (sid or '').replace('_', ' ').title() or 'Unknown')


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

# Fleet custom emoji (assets/emojis: rare/sr/ssr/ur/lr/tix/ssrtix.png,
# deployed with `.emojisetup confirm`). Unicode fallback until uploaded.
CUSTOM_FALLBACK = {'rare': '🔵', 'sr': '🟣', 'ssr': '🟪', 'ur': '🔴', 'lr': '🟡',
                   'tix': '🎟', 'ssrtix': '✨'}


def cem(gid, name: str) -> str:
    """`<:name:id>` from the emoji fleet, else the unicode fallback.
    Never raises; safe before the fleet upload."""
    try:
        from utils.emojis import em as _em
        return _em(gid, name, CUSTOM_FALLBACK.get(name, ''))
    except Exception:
        return CUSTOM_FALLBACK.get(name, '')


def rar_em(gid, rar: str) -> str:
    return cem(gid, {'C': 'rare', 'R': 'rare', 'SR': 'sr', 'LR': 'lr', 'UR': 'ur'}.get(rar, 'rare'))


def tix_em(gid) -> str:
    return cem(gid, 'tix')


def ssr_em(gid) -> str:
    return cem(gid, 'ssrtix')


# ---------- ticket economy (wish loop) ----------
# Tickets replace coin packs: .drop accrues them, .wish spends them.
# Pools are R/LR/UR only (that is all the art that exists).
DROP_CD = 300          # one drop accrues per 5 min
DROP_STACK_MAX = 3     # unclaimed drops bank up to 3
WISH_ODDS = {'R': 97.0, 'LR': 2.2, 'UR': 0.8}
SSR_ODDS = {'LR': 95.0, 'UR': 5.0}   # milestone ticket: guaranteed LR+
MILESTONE_EVERY = 200  # tickets spent -> 1 LR+ ticket
BURN_TICKET_EVERY = 10  # burns -> 1 ticket
WISH_MULTI_MAX = 50
DAILY_TICKETS = 3


def ticket_state(gid, uid) -> dict:
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM card_tickets WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        if not row:
            conn.execute('INSERT INTO card_tickets (guild_id, user_id) VALUES (?,?)', (gid, uid))
            return {'guild_id': gid, 'user_id': uid, 'tickets': 0, 'ssr_tickets': 0,
                    'drop_stack': 0, 'last_drop': 0, 'milestone': 0, 'burns': 0}
        return dict(row)


def add_tickets(gid, uid, n: int) -> dict:
    ticket_state(gid, uid)
    with db.conn_ctx() as conn:
        conn.execute('UPDATE card_tickets SET tickets=tickets+? WHERE guild_id=? AND user_id=?',
                     (int(n), str(gid), str(uid)))
    return ticket_state(gid, uid)


def claim_drop(gid, uid, now: int = None) -> dict:
    """Accrue 5-min drops (banked to 3), then hand over ONE ticket."""
    import time as _t
    gid, uid = str(gid), str(uid)
    now = int(now if now is not None else _t.time())
    st = ticket_state(gid, uid)
    stack, last = int(st['drop_stack'] or 0), int(st['last_drop'] or 0)
    if not last:
        stack, last = DROP_STACK_MAX, now  # first touch: full bank
    else:
        add = (now - last) // DROP_CD
        if add > 0:
            stack = min(DROP_STACK_MAX, stack + add)
            last = now if stack >= DROP_STACK_MAX else last + add * DROP_CD
    if stack <= 0:
        wait = DROP_CD - (now - last)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE card_tickets SET drop_stack=?, last_drop=? '
                         'WHERE guild_id=? AND user_id=?', (stack, last, gid, uid))
        return {'ok': False, 'wait': max(1, wait), 'stack': 0,
                'tickets': st['tickets'], 'ssr_tickets': st['ssr_tickets'],
                'milestone': st['milestone'], 'burns': st['burns']}
    stack -= 1
    with db.conn_ctx() as conn:
        conn.execute('UPDATE card_tickets SET drop_stack=?, last_drop=?, tickets=tickets+1 '
                     'WHERE guild_id=? AND user_id=?', (stack, last, gid, uid))
    st = ticket_state(gid, uid)
    st.update(ok=True, granted=1, stack=stack)
    try:
        from services.quests import bump
        bump(gid, uid, 'dropper')
    except Exception:
        pass
    return st


def _roll_rarity(weights: dict, rng) -> str:
    total = sum(weights.values())
    roll = rng.uniform(0, total)
    acc = 0.0
    for rar, w in weights.items():
        acc += w
        if roll < acc:
            return rar
    return next(iter(weights))


def _grant_wish_card(conn, gid, uid, card: dict) -> bool:
    """Insert one pulled card (ours have no print limits). Returns is_new."""
    row = conn.execute('SELECT qty FROM anime_collection WHERE guild_id=? AND user_id=? '
                       'AND card_id=? AND print_no=0', (gid, uid, card['id'])).fetchone()
    if row:
        conn.execute('UPDATE anime_collection SET qty=qty+1 WHERE guild_id=? AND user_id=? '
                     'AND card_id=? AND print_no=0', (gid, uid, card['id']))
    else:
        import time as _t
        conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, '
                     'obtained_at, qty) VALUES (?,?,?,?,?,1)',
                     (gid, uid, card['id'], 0, int(_t.time())))
    tot = conn.execute('SELECT SUM(qty) q FROM anime_collection WHERE guild_id=? AND user_id=? '
                       'AND card_id=?', (gid, uid, card['id'])).fetchone()['q'] or 0
    return tot <= 1


def wish(gid, uid, n: int = 1, use_ssr: bool = False, rng=None) -> dict:
    """Spend tickets, pull cards. Normal: 1 ticket/pull. SSR: 1 LR+ ticket."""
    import random as _r
    gid, uid = str(gid), str(uid)
    rng = rng or _r
    n = max(1, min(WISH_MULTI_MAX, int(n or 1)))
    st = ticket_state(gid, uid)
    if use_ssr:
        if (st['ssr_tickets'] or 0) < 1:
            return {'ok': False, 'code': 'no_ssr', 'state': st}
    elif (st['tickets'] or 0) < n:
        return {'ok': False, 'code': 'no_tickets', 'need': n,
                'have': st['tickets'] or 0, 'state': st}
    weights = SSR_ODDS if use_ssr else WISH_ODDS
    feat, _ = banner_set()
    pulls = []
    with db.conn_ctx() as conn:
        for _ in range(n):
            rar = _roll_rarity(weights, rng)
            pool = conn.execute('SELECT * FROM anime_cards WHERE rarity=?', (rar,)).fetchall()
            if not pool:  # empty tier: fall back to R so the ticket never fizzles
                pool = conn.execute("SELECT * FROM anime_cards WHERE rarity='R'").fetchall()
                rar = 'R'
            if not pool:
                continue
            # Featured banner: 15% of pulls redirect into this week's set.
            if rng.random() < BANNER_FEATURE_CHANCE:
                fb = [c for c in pool if (c['set_id'] or '') == feat]
                if fb:
                    pool = fb
            card = dict(rng.choice(pool))
            is_new = _grant_wish_card(conn, gid, uid, card)
            pulls.append({'id': card['id'], 'code': card.get('code') or card['id'],
                          'name': card.get('name') or card['id'], 'rarity': rar,
                          'set_id': card.get('set_id', ''), 'image_url': card.get('image_url', ''),
                          'is_new': is_new})
        if use_ssr:
            conn.execute('UPDATE card_tickets SET ssr_tickets=ssr_tickets-1 '
                         'WHERE guild_id=? AND user_id=?', (gid, uid))
        else:
            conn.execute('UPDATE card_tickets SET tickets=tickets-?, milestone=milestone+? '
                         'WHERE guild_id=? AND user_id=?', (n, n, gid, uid))
    invalidate_perks(gid, uid)
    st = ticket_state(gid, uid)
    earned = 0
    if not use_ssr:
        # Milestone: every 200 spent -> 1 guaranteed LR+ ticket.
        while (st['milestone'] or 0) >= MILESTONE_EVERY:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE card_tickets SET milestone=milestone-?, ssr_tickets=ssr_tickets+1 '
                             'WHERE guild_id=? AND user_id=?', (MILESTONE_EVERY, gid, uid))
            earned += 1
            st = ticket_state(gid, uid)
    try:
        from services.quests import bump
        bump(gid, uid, 'wisher', len(pulls))
    except Exception:
        pass
    return {'ok': True, 'pulls': pulls, 'state': st, 'ssr_earned': earned,
            'guaranteed': use_ssr}


def burn_cards(gid, uid, code: str, count: int = 1) -> dict:
    """Burn owned copies -> dust + burn progress (10 burns = 1 ticket)."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        card = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                            (code, code)).fetchone()
        if not card:
            return {'ok': False, 'code': 'no_card'}
        c = dict(card)
        bud = conn.execute('SELECT card_id FROM card_buddy WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        if bud and bud['card_id'] == c['id']:
            return {'ok': False, 'code': 'is_buddy'}
        locked = conn.execute('SELECT 1 FROM card_locks WHERE guild_id=? AND user_id=? AND card_id=?',
                              (gid, uid, c['id'])).fetchone()
        if locked:
            return {'ok': False, 'code': 'locked'}
        rows = conn.execute('SELECT print_no, qty FROM anime_collection WHERE guild_id=? AND user_id=? '
                            'AND card_id=? ORDER BY print_no', (gid, uid, c['id'])).fetchall()
        owned = sum(int(r['qty'] or 0) for r in rows)
        if owned <= 0:
            return {'ok': False, 'code': 'not_owned'}
        n = max(1, min(int(count or 1), owned))
        left = n
        for r in rows:
            if left <= 0:
                break
            take = min(int(r['qty'] or 0), left)
            left -= take
            newq = int(r['qty'] or 0) - take
            if newq <= 0:
                conn.execute('DELETE FROM anime_collection WHERE guild_id=? AND user_id=? '
                             'AND card_id=? AND print_no=?', (gid, uid, c['id'], r['print_no']))
            else:
                conn.execute('UPDATE anime_collection SET qty=? WHERE guild_id=? AND user_id=? '
                             'AND card_id=? AND print_no=?', (newq, gid, uid, c['id'], r['print_no']))
        dust = DUST_VALUE.get(c['rarity'], 100) * n
        conn.execute('INSERT OR IGNORE INTO anime_dust (guild_id, user_id, dust) VALUES (?,?,0)', (gid, uid))
        conn.execute('UPDATE anime_dust SET dust=dust+? WHERE guild_id=? AND user_id=?', (dust, gid, uid))
        conn.execute('INSERT OR IGNORE INTO card_tickets (guild_id, user_id) VALUES (?,?)', (gid, uid))
        before = (conn.execute('SELECT burns FROM card_tickets WHERE guild_id=? AND user_id=?',
                               (gid, uid)).fetchone()['burns'] or 0) // BURN_TICKET_EVERY
        conn.execute('UPDATE card_tickets SET burns=burns+? WHERE guild_id=? AND user_id=?',
                     (n, gid, uid))
        after = (conn.execute('SELECT burns FROM card_tickets WHERE guild_id=? AND user_id=?',
                              (gid, uid)).fetchone()['burns'] or 0) // BURN_TICKET_EVERY
        tickets_earned = after - before
        if tickets_earned:
            conn.execute('UPDATE card_tickets SET tickets=tickets+? WHERE guild_id=? AND user_id=?',
                         (tickets_earned, gid, uid))
    invalidate_perks(gid, uid)
    try:
        from services.quests import bump
        bump(gid, uid, 'burner', n)
    except Exception:
        pass
    return {'ok': True, 'card': c, 'burned': n, 'dust': dust,
            'tickets_earned': tickets_earned, 'state': ticket_state(gid, uid)}


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


def is_locked(gid, uid, card_id: str) -> bool:
    with db.conn_ctx() as conn:
        return bool(conn.execute('SELECT 1 FROM card_locks WHERE guild_id=? AND user_id=? AND card_id=?',
                                 (str(gid), str(uid), card_id)).fetchone())


def set_locked(gid, uid, card_id: str, locked: bool) -> None:
    with db.conn_ctx() as conn:
        if locked:
            conn.execute('INSERT OR IGNORE INTO card_locks (guild_id, user_id, card_id) VALUES (?,?,?)',
                         (str(gid), str(uid), card_id))
        else:
            conn.execute('DELETE FROM card_locks WHERE guild_id=? AND user_id=? AND card_id=?',
                         (str(gid), str(uid), card_id))


def wish_pool_counts() -> dict:
    """Live card counts per rarity (for /rates math)."""
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT rarity, COUNT(*) c FROM anime_cards GROUP BY rarity').fetchall()
    return {r['rarity']: int(r['c'] or 0) for r in rows}


def _check_tradable(conn, gid, uid, card_ids) -> tuple:
    """All-or-nothing ownership + flag check. Returns (ok, code, cards)."""
    from collections import Counter
    need = Counter(card_ids)
    bud = conn.execute('SELECT card_id FROM card_buddy WHERE guild_id=? AND user_id=?',
                       (gid, uid)).fetchone()
    buddy_id = bud['card_id'] if bud else None
    cards = {}
    for cid, n in need.items():
        card = conn.execute('SELECT * FROM anime_cards WHERE id=?', (cid,)).fetchone()
        if not card:
            return False, 'no_card', {}
        if cid == buddy_id:
            return False, 'is_buddy', {}
        if conn.execute('SELECT 1 FROM card_locks WHERE guild_id=? AND user_id=? AND card_id=?',
                        (gid, uid, cid)).fetchone():
            return False, 'locked', {}
        have = conn.execute('SELECT SUM(qty) q FROM anime_collection WHERE guild_id=? AND user_id=? '
                            'AND card_id=?', (gid, uid, cid)).fetchone()['q'] or 0
        if have < n:
            return False, 'not_owned', {}
        cards[cid] = dict(card)
    return True, '', cards


def _move_card(conn, gid, frm, to, cid):
    """Move one copy frm -> to (print_no 0 everywhere in our sets)."""
    import time as _t
    row = conn.execute('SELECT qty FROM anime_collection WHERE guild_id=? AND user_id=? '
                       'AND card_id=? AND print_no=0', (gid, frm, cid)).fetchone()
    left = int(row['qty'] or 0) - 1
    if left <= 0:
        conn.execute('DELETE FROM anime_collection WHERE guild_id=? AND user_id=? '
                     'AND card_id=? AND print_no=0', (gid, frm, cid))
    else:
        conn.execute('UPDATE anime_collection SET qty=? WHERE guild_id=? AND user_id=? '
                     'AND card_id=? AND print_no=0', (left, gid, frm, cid))
    got = conn.execute('SELECT qty FROM anime_collection WHERE guild_id=? AND user_id=? '
                       'AND card_id=? AND print_no=0', (gid, to, cid)).fetchone()
    if got:
        conn.execute('UPDATE anime_collection SET qty=qty+1 WHERE guild_id=? AND user_id=? '
                     'AND card_id=? AND print_no=0', (gid, to, cid))
    else:
        conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, '
                     'obtained_at, qty) VALUES (?,?,?,?,?,1)', (gid, to, cid, 0, int(_t.time())))


def swap_cards(gid, a_uid, a_cards, b_uid, b_cards) -> dict:
    """Atomic two-way swap (live trades). Nothing moves unless all checks pass."""
    gid, a_uid, b_uid = str(gid), str(a_uid), str(b_uid)
    if a_uid == b_uid:
        return {'ok': False, 'code': 'self'}
    with db.conn_ctx() as conn:
        oka, ca, _ = _check_tradable(conn, gid, a_uid, list(a_cards))
        if not oka:
            return {'ok': False, 'code': ca, 'side': 'a'}
        okb, cb, _ = _check_tradable(conn, gid, b_uid, list(b_cards))
        if not okb:
            return {'ok': False, 'code': cb, 'side': 'b'}
        for cid in a_cards:
            _move_card(conn, gid, a_uid, b_uid, cid)
        for cid in b_cards:
            _move_card(conn, gid, b_uid, a_uid, cid)
    invalidate_perks(gid, a_uid)
    invalidate_perks(gid, b_uid)
    return {'ok': True}


OFFER_TTL = 3 * 86400


def create_offer(gid, frm, to, give, want) -> dict:
    """Async offer: frm gives card ids, optionally wants codes. Validated now AND at accept."""
    import time as _t
    gid, frm, to = str(gid), str(frm), str(to)
    if frm == to:
        return {'ok': False, 'code': 'self'}
    give, want = list(give or []), list(want or [])
    if not give:
        return {'ok': False, 'code': 'empty'}
    with db.conn_ctx() as conn:
        oka, ca, _ = _check_tradable(conn, gid, frm, give)
        if not oka:
            return {'ok': False, 'code': ca}
        # want codes must exist (ownership checked at accept, they may pull meanwhile)
        for code in want:
            if not conn.execute('SELECT 1 FROM anime_cards WHERE code=? OR id=?',
                                (code, code)).fetchone():
                return {'ok': False, 'code': 'no_card'}
        now = int(_t.time())
        cur = conn.execute('INSERT INTO trade_offers (guild_id, from_id, to_id, give_json, want_json, '
                           'created, expires, status) VALUES (?,?,?,?,?,?,?,?)',
                           (gid, frm, to, __import__('json').dumps(give),
                            __import__('json').dumps(want), now, now + OFFER_TTL, 'open'))
    return {'ok': True, 'id': cur.lastrowid}


def _expire_due(conn, now: int):
    conn.execute("UPDATE trade_offers SET status='expired' WHERE status='open' AND expires<?", (now,))


def get_offer(oid: int):
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM trade_offers WHERE id=?', (int(oid),)).fetchone()
        return dict(row) if row else None


def list_offers(gid, uid) -> dict:
    import time as _t
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        _expire_due(conn, int(_t.time()))
        recv = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_offers WHERE guild_id=? AND to_id=? AND status='open' ORDER BY id",
            (gid, uid)).fetchall()]
        sent = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_offers WHERE guild_id=? AND from_id=? AND status='open' ORDER BY id",
            (gid, uid)).fetchall()]
        hist = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_offers WHERE guild_id=? AND (from_id=? OR to_id=?) "
            "AND status!='open' ORDER BY id DESC LIMIT 5", (gid, uid, uid)).fetchall()]
    return {'received': recv, 'sent': sent, 'history': hist}


def accept_offer(gid, uid, oid: int) -> dict:
    """Recipient accepts: re-validates everything, swaps atomically."""
    import json as _j
    import time as _t
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        _expire_due(conn, int(_t.time()))
        o = conn.execute('SELECT * FROM trade_offers WHERE id=? AND guild_id=?', (oid, gid)).fetchone()
        if not o:
            return {'ok': False, 'code': 'no_offer'}
        o = dict(o)
        if o['status'] != 'open':
            return {'ok': False, 'code': o['status']}
        if o['to_id'] != uid:
            return {'ok': False, 'code': 'not_yours'}
        give = _j.loads(o['give_json'] or '[]')
        want_codes = _j.loads(o['want_json'] or '[]')
        # resolve wanted codes -> card ids (recipient's copies)
        want = []
        for code in want_codes:
            row = conn.execute('SELECT id FROM anime_cards WHERE code=? OR id=?',
                               (code, code)).fetchone()
            if not row:
                return {'ok': False, 'code': 'no_card'}
            want.append(row['id'])
        oka, ca, _ = _check_tradable(conn, gid, o['from_id'], give)
        if not oka:
            conn.execute('UPDATE trade_offers SET status=? WHERE id=?',
                         ('cancelled' if ca in ('locked', 'is_buddy') else 'stale', oid))
            return {'ok': False, 'code': 'giver_' + ca}
        okb, cb, _ = _check_tradable(conn, gid, uid, want)
        if not okb:
            return {'ok': False, 'code': 'taker_' + cb}
        for cid in give:
            _move_card(conn, gid, o['from_id'], uid, cid)
        for cid in want:
            _move_card(conn, gid, uid, o['from_id'], cid)
        conn.execute("UPDATE trade_offers SET status='filled' WHERE id=?", (oid,))
    invalidate_perks(gid, o['from_id'])
    invalidate_perks(gid, uid)
    return {'ok': True, 'offer': o, 'give': give, 'want': want}


def close_offer(gid, uid, oid: int, status: str) -> dict:
    """Decline (recipient) or cancel (sender)."""
    gid, uid = str(gid), str(uid)
    assert status in ('declined', 'cancelled')
    with db.conn_ctx() as conn:
        o = conn.execute('SELECT * FROM trade_offers WHERE id=? AND guild_id=?', (oid, gid)).fetchone()
        if not o:
            return {'ok': False, 'code': 'no_offer'}
        o = dict(o)
        if o['status'] != 'open':
            return {'ok': False, 'code': o['status']}
        if status == 'declined' and o['to_id'] != uid:
            return {'ok': False, 'code': 'not_yours'}
        if status == 'cancelled' and o['from_id'] != uid:
            return {'ok': False, 'code': 'not_yours'}
        conn.execute('UPDATE trade_offers SET status=? WHERE id=?', (status, oid))
    return {'ok': True}


def card_daily_bonus(gid, uid) -> int:
    try:
        return int(card_perks(gid, uid)['daily'])
    except Exception:
        return 0


# ---------- featured banner (weekly rotation, real effect) ----------
BANNER_SETS = ['naruto', 'rezero', 'bleach', 'dragonball', 'onepiece',
               'demonslayer', 'jojo', 'bluelock', 'fairytail']
BANNER_FEATURE_CHANCE = 0.15  # wishes redirecting into the featured set


def banner_set(now: int = None):
    """(set_id, ends_ts): deterministic ISO-week rotation over the 8 sets."""
    import datetime as _dt
    now = int(now if now is not None else __import__('time').time())
    d = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    iso_year, iso_week, _ = d.isocalendar()
    sid = BANNER_SETS[(iso_year * 53 + iso_week) % len(BANNER_SETS)]
    days_ahead = 7 - d.isoweekday()
    end = int((_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc)
               + _dt.timedelta(days=days_ahead + 1)).timestamp())
    return sid, end


# ---------- wishlist ----------
def wl_list(gid, uid) -> list:
    with db.conn_ctx() as conn:
        return [r['term'] for r in conn.execute(
            'SELECT term FROM card_wishlist WHERE guild_id=? AND user_id=? ORDER BY term',
            (str(gid), str(uid))).fetchall()]


def wl_set(gid, uid, term: str, add: bool) -> None:
    term = (term or '').strip()[:60]
    if not term:
        return
    with db.conn_ctx() as conn:
        if add:
            conn.execute('INSERT OR IGNORE INTO card_wishlist (guild_id, user_id, term) VALUES (?,?,?)',
                         (str(gid), str(uid), term))
        else:
            conn.execute('DELETE FROM card_wishlist WHERE guild_id=? AND user_id=? AND term=?',
                         (str(gid), str(uid), term))


def wl_matches(gid, uid, limit: int = 10) -> list:
    """Owned cards matching any wishlist term (name or set)."""
    terms = wl_list(gid, uid)
    if not terms:
        return []
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        owned = conn.execute(
            'SELECT k.id, k.code, k.name, k.rarity, k.set_id FROM anime_cards k '
            'JOIN (SELECT DISTINCT card_id FROM anime_collection WHERE guild_id=? AND user_id=?) o '
            'ON o.card_id=k.id', (gid, uid)).fetchall()
    out = []
    for r in owned:
        hay = f"{r['name'] or ''} {r['set_id'] or ''}".lower()
        if any(t.lower() in hay for t in terms):
            out.append(dict(r))
        if len(out) >= limit:
            break
    return out


# ---------- album (9 favorite slots) ----------
ALBUM_SLOTS = 9


def album_get(gid, uid) -> dict:
    """slot -> card row."""
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT slot, card_id FROM card_album WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
        if not rows:
            return {}
        cards = {r['id']: dict(r) for r in conn.execute(
            f"SELECT * FROM anime_cards WHERE id IN ({','.join('?' for _ in rows)})",
            [r['card_id'] for r in rows]).fetchall()}
    return {int(r['slot']): cards.get(r['card_id']) for r in rows if r['card_id'] in cards}


def album_set(gid, uid, code: str, slot: int) -> dict:
    gid, uid = str(gid), str(uid)
    slot = max(1, min(ALBUM_SLOTS, int(slot or 1)))
    with db.conn_ctx() as conn:
        card = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                            (code, code)).fetchone()
        if not card:
            return {'ok': False, 'code': 'no_card'}
        own = conn.execute('SELECT SUM(qty) q FROM anime_collection WHERE guild_id=? AND user_id=? '
                           'AND card_id=?', (gid, uid, card['id'])).fetchone()['q'] or 0
        if not own:
            return {'ok': False, 'code': 'not_owned'}
        conn.execute('INSERT OR REPLACE INTO card_album (guild_id, user_id, slot, card_id) '
                     'VALUES (?,?,?,?)', (gid, uid, slot, card['id']))
    return {'ok': True, 'card': dict(card), 'slot': slot}


def album_clear(gid, uid, slot: int = 0) -> None:
    with db.conn_ctx() as conn:
        if slot:
            conn.execute('DELETE FROM card_album WHERE guild_id=? AND user_id=? AND slot=?',
                         (str(gid), str(uid), int(slot)))
        else:
            conn.execute('DELETE FROM card_album WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))


# ---------- auctions (BIN, 24h, 5% fee, lazy expiry) ----------
AUC_TTL = 86400
AUC_FEE_PCT = 5
AUC_MIN_PRICE = 100


def _sweep_auctions(conn, now: int):
    """Return expired escrow to sellers."""
    import time as _t
    rows = conn.execute("SELECT id, guild_id, seller_id, card_id FROM auctions "
                        "WHERE status='open' AND expires<?", (now,)).fetchall()
    for r in rows:
        conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, '
                     'obtained_at, qty) VALUES (?,?,?,?,?,1)',
                     (r['guild_id'], r['seller_id'], r['card_id'], 0, int(_t.time())))
        conn.execute("UPDATE auctions SET status='expired' WHERE id=?", (r['id'],))


def auc_sell(gid, uid, code: str, price: int) -> dict:
    import time as _t
    gid, uid = str(gid), str(uid)
    price = max(AUC_MIN_PRICE, int(price or 0))
    with db.conn_ctx() as conn:
        _sweep_auctions(conn, int(_t.time()))
        card = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                            (code, code)).fetchone()
        if not card:
            return {'ok': False, 'code': 'no_card'}
        c = dict(card)
        oka, ca, _ = _check_tradable(conn, gid, uid, [c['id']])
        if not oka:
            return {'ok': False, 'code': ca}
        row = conn.execute('SELECT qty FROM anime_collection WHERE guild_id=? AND user_id=? '
                           'AND card_id=? AND print_no=0', (gid, uid, c['id'])).fetchone()
        if int(row['qty'] or 0) - 1 <= 0:
            conn.execute('DELETE FROM anime_collection WHERE guild_id=? AND user_id=? '
                         'AND card_id=? AND print_no=0', (gid, uid, c['id']))
        else:
            conn.execute('UPDATE anime_collection SET qty=qty-1 WHERE guild_id=? AND user_id=? '
                         'AND card_id=? AND print_no=0', (gid, uid, c['id']))
        now = int(_t.time())
        cur = conn.execute('INSERT INTO auctions (guild_id, seller_id, card_id, price, created, '
                           'expires, status) VALUES (?,?,?,?,?,?,?)',
                           (gid, uid, c['id'], price, now, now + AUC_TTL, 'open'))
    invalidate_perks(gid, uid)
    return {'ok': True, 'id': cur.lastrowid, 'card': c, 'price': price}


def auc_list(gid, limit: int = 10) -> list:
    import time as _t
    with db.conn_ctx() as conn:
        _sweep_auctions(conn, int(_t.time()))
        rows = conn.execute(
            'SELECT a.id, a.seller_id, a.price, a.expires, k.code, k.name, k.rarity, k.set_id '
            'FROM auctions a JOIN anime_cards k ON k.id=a.card_id '
            "WHERE a.guild_id=? AND a.status='open' ORDER BY a.id DESC LIMIT ?",
            (str(gid), limit)).fetchall()
    return [dict(r) for r in rows]


def auc_buy(gid, uid, oid: int) -> dict:
    import time as _t
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        _sweep_auctions(conn, int(_t.time()))
        o = conn.execute('SELECT * FROM auctions WHERE id=? AND guild_id=?', (oid, gid)).fetchone()
        if not o or o['status'] != 'open':
            return {'ok': False, 'code': 'gone'}
        o = dict(o)
        if o['seller_id'] == uid:
            return {'ok': False, 'code': 'self'}
        fee = max(1, o['price'] * AUC_FEE_PCT // 100)
        gain = o['price'] - fee
        eco = conn.execute('SELECT cash FROM eco WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        if (eco['cash'] if eco else 1000) < o['price']:
            return {'ok': False, 'code': 'broke'}
        conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,1000)', (gid, uid))
        conn.execute('UPDATE eco SET cash=cash-? WHERE guild_id=? AND user_id=?',
                     (o['price'], gid, uid))
        conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,1000)',
                     (gid, o['seller_id']))
        conn.execute('UPDATE eco SET cash=cash+? WHERE guild_id=? AND user_id=?',
                     (gain, gid, o['seller_id']))
        conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, '
                     'obtained_at, qty) VALUES (?,?,?,?,?,1)',
                     (gid, uid, o['card_id'], 0, int(_t.time())))
        conn.execute("UPDATE auctions SET status='sold' WHERE id=?", (oid,))
    invalidate_perks(gid, uid)
    invalidate_perks(gid, o['seller_id'])
    return {'ok': True, 'price': o['price'], 'fee': fee, 'seller': o['seller_id']}


def auc_cancel(gid, uid, oid: int) -> dict:
    import time as _t
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        _sweep_auctions(conn, int(_t.time()))
        o = conn.execute('SELECT * FROM auctions WHERE id=? AND guild_id=?', (oid, gid)).fetchone()
        if not o or o['status'] != 'open' or o['seller_id'] != uid:
            return {'ok': False, 'code': 'nope'}
        o = dict(o)
        conn.execute('INSERT INTO anime_collection (guild_id, user_id, card_id, print_no, '
                     'obtained_at, qty) VALUES (?,?,?,?,?,1)',
                     (gid, uid, o['card_id'], 0, int(_t.time())))
        conn.execute("UPDATE auctions SET status='cancelled' WHERE id=?", (oid,))
    invalidate_perks(gid, uid)
    return {'ok': True}


# ---------- wheel (12h fortune spin) ----------
WHEEL_CD = 12 * 3600
WHEEL_PRIZES = [
    ('tickets', 1, 30), ('tickets', 2, 12), ('coins', 500, 22), ('coins', 1500, 10),
    ('dust', 300, 14), ('dust', 1000, 6), ('card_r', 1, 4), ('card_lr', 1, 2),
]


def wheel_state(gid, uid) -> dict:
    import time as _t
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT last_spin FROM wheel_spins WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    last = int(row['last_spin'] or 0) if row else 0
    left = WHEEL_CD - (int(_t.time()) - last)
    return {'ready': left <= 0, 'wait': max(0, left)}


def wheel_spin(gid, uid, rng=None) -> dict:
    import random as _r
    import time as _t
    gid, uid = str(gid), str(uid)
    rng = rng or _r
    st = wheel_state(gid, uid)
    if not st['ready']:
        return {'ok': False, 'wait': st['wait']}
    total = sum(w for _, _, w in WHEEL_PRIZES)
    roll = rng.uniform(0, total)
    acc, pick = 0.0, WHEEL_PRIZES[0]
    for kind, amt, w in WHEEL_PRIZES:
        acc += w
        if roll < acc:
            pick = (kind, amt, w)
            break
    kind, amt, _ = pick
    detail = ''
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO wheel_spins (guild_id, user_id, last_spin) VALUES (?,?,?)',
                     (gid, uid, int(_t.time())))
        if kind == 'tickets':
            conn.execute('INSERT OR IGNORE INTO card_tickets (guild_id, user_id) VALUES (?,?)', (gid, uid))
            conn.execute('UPDATE card_tickets SET tickets=tickets+? WHERE guild_id=? AND user_id=?',
                         (amt, gid, uid))
            detail = f'+{amt} 🎟'
        elif kind == 'coins':
            conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,1000)', (gid, uid))
            conn.execute('UPDATE eco SET cash=cash+? WHERE guild_id=? AND user_id=?', (amt, gid, uid))
            detail = f'+{amt} coins'
        elif kind == 'dust':
            conn.execute('INSERT OR IGNORE INTO anime_dust (guild_id, user_id, dust) VALUES (?,?,0)', (gid, uid))
            conn.execute('UPDATE anime_dust SET dust=dust+? WHERE guild_id=? AND user_id=?', (amt, gid, uid))
            detail = f'+{amt} dust'
        else:
            rar = 'R' if kind == 'card_r' else 'LR'
            pool = conn.execute('SELECT * FROM anime_cards WHERE rarity=?', (rar,)).fetchall()
            if pool:
                card = dict(rng.choice(pool))
                _grant_wish_card(conn, gid, uid, card)
                detail = f"{card.get('code')} [{rar}]"
            else:
                detail = '+1 🎟'
                conn.execute('INSERT OR IGNORE INTO card_tickets (guild_id, user_id) VALUES (?,?)', (gid, uid))
                conn.execute('UPDATE card_tickets SET tickets=tickets+1 WHERE guild_id=? AND user_id=?',
                             (gid, uid))
    invalidate_perks(gid, uid)
    return {'ok': True, 'kind': kind, 'amount': amt, 'detail': detail}


# ---------- drop reminders (one-shot DMs when the stack fills) ----------
def rm_arm(gid, uid) -> bool:
    """Returns False if already armed."""
    import time as _t
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT 1 FROM reminders WHERE guild_id=? AND user_id=? AND kind=?',
                           (str(gid), str(uid), 'drop')).fetchone()
        if row:
            return False
        conn.execute('INSERT INTO reminders (guild_id, user_id, kind, created) VALUES (?,?,?,?)',
                     (str(gid), str(uid), 'drop', int(_t.time())))
    return True


def rm_due() -> list:
    """Reminder rows whose drop stack is full (fires once, then disarms)."""
    import time as _t
    out = []
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT guild_id, user_id FROM reminders WHERE kind=?', ('drop',)).fetchall()
        for r in rows:
            gid, uid = r['guild_id'], r['user_id']
            t = conn.execute('SELECT drop_stack, last_drop FROM card_tickets WHERE guild_id=? AND user_id=?',
                             (gid, uid)).fetchone()
            stack = int(t['drop_stack'] or 0) if t else 0
            last = int(t['last_drop'] or 0) if t else 0
            if last:
                stack = min(DROP_STACK_MAX, stack + (int(_t.time()) - last) // DROP_CD)
            if stack >= DROP_STACK_MAX:
                out.append((gid, uid))
                conn.execute('DELETE FROM reminders WHERE guild_id=? AND user_id=? AND kind=?',
                             (gid, uid, 'drop'))
    return out

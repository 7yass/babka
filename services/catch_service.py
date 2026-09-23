"""Catch service (rework phase 3): the catch attempt as pure game logic.

Discord-free: no ctx, no interactions, no views. Lang/emoji lookups are
pure string functions, so message text is built here and returned — the
caller renders it into layouts/cards. The encounter dict is mutated in
place (pity), exactly like the old in-cog path.

Return contract matches the legacy `_do_catch`: (ok, msg, gif, disp).
"""
import random

import database as db
from lang import t


async def attempt_catch(gid, uid, e: dict, ball: str, one_shot: bool = False,
                        progress_fn=None, meta_fn=None):
    """Run one throw. progress_fn/meta_fn are the collection hooks
    (bound cog methods) until they move into services too."""
    import aiohttp
    from cogs.gamble import add_cash, bal
    from cogs.pokemon import (BALLS, BALL_FLEET, CATCH_COINS_LVL,
                              CATCH_COINS_NEW, CATCH_COINS_SHINY, balls_take,
                              catch_chance, dex_get, my_mons, rarity_of,
                              showdown_gif, streak_bump, streak_get,
                              _balls_left_line, _rarity_line, _roll_ivs,
                              _species_emoji_name)
    from utils.cards import short as cshort
    from utils.emojis import em
    if not balls_take(gid, uid, ball):
        return False, t(gid, 'eco.pk_noball', ball=ball), '', ''
    async with aiohttp.ClientSession() as s:
        row = await dex_get(s, e['dex'])
    base = BALLS[ball][1]
    if base is None:  # master ball: it never fails, skips pity math entirely
        p = 1.0
    else:
        try:
            from cogs.levels import get_user as _gu
            trainer_lv = _gu(gid, uid).get('level', 0) or 0
        except Exception:
            trainer_lv = 0
        p = catch_chance(ball, rarity_of(row, False)[0], e['level'],
                         e['hp'] / max(1, e['maxhp']), trainer_lv,
                         bool(e.get('grazz')))
        p = min(0.98, p + e.get('pity', 0))
    roll = random.random()
    if roll < p:
        first = not my_mons(gid, uid)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, active, ivs, evs) '
                         'VALUES (?,?,?,?,0,?,?,?,?,?)',
                         (str(gid), str(uid), e['dex'], e['level'],
                          1 if e['shiny'] else 0, '', 1 if first else 0, _roll_ivs(), ''))
        with db.conn_ctx() as conn:
            prev = conn.execute('SELECT count FROM pk_dexcount WHERE guild_id=? AND user_id=? AND dex=?',
                                (str(gid), str(uid), e['dex'])).fetchone()
        is_new = not prev or not (prev['count'] or 0)
        disp = row['name'].capitalize()
        spe = em(gid, _species_emoji_name(e['dex'])) or ''
        ball_emo = em(gid, BALL_FLEET.get(ball, ''), 'o')
        ball_name = {'poke': 'Pokeball', 'great': 'Greatball',
                     'ultra': 'Ultraball', 'master': 'Masterball'}.get(ball, ball.capitalize())
        check = em(gid, 'check') or '✅'
        trainer = em(gid, 'trainer_brendan') or ''
        msg = f"{check} {trainer + ' ' if trainer else ''}You caught a {spe + ' ' if spe else ''}{disp} with a {ball_emo + ' ' if ball_emo else ''}{ball_name}!"
        msg += '\n' + _rarity_line(gid, row, bool(e['shiny']))
        streak_bump(gid, uid, True)
        st = streak_get(gid, uid)
        flame = em(gid, 'streak_flame')
        msg += '\n' + (flame + ' ' if flame else '') + t(gid, 'eco.pk_streak_line',
                                                         n=st['catch_streak'], b=st['best_streak'])
        if is_new:
            msg += '\n' + t(gid, 'eco.pk_newdex')
        if progress_fn is not None:
            for extra in await progress_fn(gid, uid, e['dex'], e['shiny']):
                msg += '\n' + extra
        if meta_fn is not None:
            msg += '\n' + meta_fn(gid, uid, e['dex'], bool(e['shiny']))
        msg += '\n' + t(gid, 'eco.pk_roll_line', roll=int(roll * 100), rate=int(p * 100))
        msg += '\n' + _balls_left_line(gid, uid)
        gain = e['level'] * CATCH_COINS_LVL + (CATCH_COINS_NEW if is_new else 0) \
            + (CATCH_COINS_SHINY if e['shiny'] else 0)
        b = bal(gid, uid)
        add_cash(gid, uid, gain)
        msg += '\n' + t(gid, 'eco.pk_coins_earned', win=cshort(gain))
        gif = showdown_gif(row.get('name', ''), bool(e['shiny']))
        if gif:
            try:
                to = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=to) as s3:
                    async with s3.head(gif) as r:
                        if r.status != 200:
                            gif = ''
            except Exception:
                gif = ''
        return True, msg, gif, disp
    # broke out: pity grows, next throw is kinder (one-shot ;p has no next throw)
    if not one_shot:
        e['pity'] = min(0.4, e.get('pity', 0) + 0.08)
    streak_bump(gid, uid, False)
    broke = t(gid, 'eco.pk_broke', name=row['name'].capitalize(), ball=ball)
    if not one_shot:
        pity_emo = em(gid, 'pity_token')
        broke += (f"\n{pity_emo + ' ' if pity_emo else ''}Pity +8% (now {int(e['pity'] * 100)}%)")
    return False, broke, '', ''

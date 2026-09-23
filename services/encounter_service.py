"""Encounter service (rework phase 3): spawning as pure game logic.

Discord-free: no ctx, no interactions, no views. Builds the encounter,
stores it in the passed-in store (same dict the cog has always used),
and returns everything the Discord layer needs to render + send.
Message text uses pure string lookups (lang/emoji), like catch_service.

Shared future pipeline for ;p, auto-spawns, ;explore, safari, fishing,
world bosses and events — callers only differ in how they present it.
"""
import random
import time
from dataclasses import dataclass, field

import database as db
from lang import t


@dataclass
class EncounterResult:
    ok: bool
    reply_text: str = ''      # early-exit message (starter/cooldown/api)
    ephemeral: bool = False
    title: str = ''
    desc: str = ''
    accent: int = 0x58CC02
    media: list = field(default_factory=list)
    mode: str = 'catch'
    is_hunt: bool = False
    key: tuple = None


async def spawn_encounter(gid, uid, display_name: str, mode: str = 'catch',
                          *, store: dict, cooldowns: dict) -> EncounterResult:
    """Run one spawn: validate, select, roll, persist, describe.
    store/cooldowns are the cog's live dicts (injected, not owned)."""
    import aiohttp
    from cogs.pokemon import (ARENAS, ENC_TTL, HUNT_CD, SHINY_ODDS, balls_get,
                              calc_stats, catch_chance, dex_get, em, mon_name,
                              my_mons, pix_url, rarity_of, repel_active,
                              showdown_gif, streak_get, types_str,
                              incense_active, _rarity_line,
                              _species_emoji_name)
    key = (str(gid), str(uid))
    if not my_mons(gid, uid):
        return EncounterResult(ok=False, reply_text=t(gid, 'eco.pk_need_starter'),
                               ephemeral=True)
    wait = HUNT_CD - (int(time.time()) - cooldowns.get(key, 0))
    if wait > 0:
        return EncounterResult(ok=False, reply_text=t(gid, 'eco.pk_wait', s=wait),
                               ephemeral=True)
    cooldowns[key] = int(time.time())
    inc = incense_active(gid, uid)
    async with aiohttp.ClientSession() as s:
        for _ in range(12):
            dex = random.randint(1, 809)
            row = await dex_get(s, dex)
            if not row:
                continue
            r = random.random()
            is_leg = bool(row.get('legendary'))
            leg_odds = 0.5 if inc else 0.25
            if (is_leg and r < leg_odds) or (not is_leg and r < 0.9):
                break
        else:
            return EncounterResult(ok=False, reply_text=t(gid, 'eco.pk_api'),
                                   ephemeral=True)
        # daily hunt (casual) + shiny hunt 15% chance each in ;p
        try:
            with db.conn_ctx() as conn:
                dd = conn.execute('SELECT target FROM pk_hunt_daily WHERE guild_id=? AND user_id=?',
                                  (str(gid), str(uid))).fetchone()
                ht_daily = (dd['target'] or 0) if dd else 0
                if ht_daily and random.random() < 0.15:
                    trow = await dex_get(s, ht_daily)
                    if trow:
                        dex, row = ht_daily, trow
                else:
                    hh = conn.execute('SELECT target FROM pk_hunt WHERE guild_id=? AND user_id=?',
                                      (str(gid), str(uid))).fetchone()
                    ht = (hh['target'] or 0) if hh else 0
                    if ht and random.random() < 0.15:
                        trow = await dex_get(s, ht)
                        if trow:
                            dex, row = ht, trow
        except Exception:
            pass
    mons = my_mons(gid, uid)
    avg_lv = sum(m['level'] for m in mons) / max(1, len(mons))
    level = max(3, min(70, int(random.gauss(avg_lv, 6))))
    repelled = repel_active(gid, uid)
    if repelled:
        level = max(3, min(100, int(level * 1.5)))
    # shiny odds: base 1/256, hunt target chains it down to 1/32, incense rolls twice
    with db.conn_ctx() as conn:
        h = conn.execute('SELECT target, streak FROM pk_hunt WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid))).fetchone()
    target = (h['target'] or 0) if h else 0
    streak = (h['streak'] or 0) if h else 0
    denom = max(32, SHINY_ODDS - streak * 4) if target == dex else SHINY_ODDS
    shiny = random.randint(1, denom) == 1 or (inc and random.randint(1, denom) == 1)
    stats = calc_stats(row, level)
    mystery = random.random() < 0.35
    arena = random.choice(ARENAS)
    store[key] = {'dex': dex, 'level': level, 'shiny': shiny,
                  'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                  'mystery': mystery, 'mode': mode, 'arena': arena,
                  'exp': int(time.time()) + ENC_TTL}
    pix = pix_url(dex, shiny)
    spe_emo = em(gid, _species_emoji_name(dex))
    if mystery:
        found = t(gid, 'eco.pk_found_mystery', user=display_name)
        wild_line = (t(gid, 'eco.pk_mystery', types=types_str(gid, row["types"])) +
                     ((f"\n{em(gid, 'item_incense') or '+'} " + t(gid, 'eco.pk_incensed')) if inc else ''))
    else:
        disp = ((em(gid, 'rarity_shiny') or '*') + ' ' if shiny else '') + row['name'].capitalize()
        found = t(gid, 'eco.pk_found', user=display_name, emo=spe_emo, name=disp)
        flags = ''
        if inc:
            flags += f"\n{em(gid, 'item_incense') or '+'} " + t(gid, 'eco.pk_incensed')
        if repelled:
            flags += f"\n{em(gid, 'item_repel') or '-'} " + t(gid, 'eco.pk_repelled')
        if mode == 'catch':
            wild_line = (t(gid, 'eco.pk_wild_catch', name=disp,
                           types=types_str(gid, row["types"])) + flags)
        else:
            rate_emo = em(gid, 'rate_up')
            try:
                from cogs.levels import get_user as _gu
                _tlv = _gu(gid, uid).get('level', 0) or 0
            except Exception:
                _tlv = 0
            wild_line = (t(gid, 'eco.pk_wild', name=disp,
                           types=types_str(gid, row["types"]),
                           hint=t(gid, 'eco.pk_wild_hint')) + flags
                         + '\n' + (rate_emo + ' ' if rate_emo else '')
                         + t(gid, 'eco.pk_odds', pct=int(catch_chance(
                             'ultra', rarity_of(row, False)[0], level, 1.0,
                             _tlv) * 100)))
    slot_of = {m['id']: i + 1 for i, m in enumerate(mons)}
    act = next((m for m in mons if m.get('active')), mons[0])
    if mode == 'fight':
        hint = (f"FIGHT: {mon_name(act, gid)} Lv{act['level']} — "
                f"`;active {slot_of.get(act['id'], 1)}` / `;battle {slot_of.get(act['id'], 1)}` to change")
    else:
        hint = "`;catch <ball>` or tap a ball below — one throw!"
    st = streak_get(gid, uid)
    flame = em(gid, 'streak_flame')
    streak = (flame + ' ' if flame else '') + t(gid, 'eco.pk_streak_line',
                                                n=st['catch_streak'], b=st['best_streak'])
    bcounts = balls_get(gid, uid)
    balls_block = (f"===== Balls left =====\n"
                   f"Pokeballs: {bcounts.get('poke',0)}  •  Greatballs: {bcounts.get('great',0)}\n"
                   f"Ultraballs: {bcounts.get('ultra',0)}  •  Masterballs: {bcounts.get('master',0)}")
    wild_emo = em(gid, 'catch_reticle' if mystery else 'encounter_wild')
    title = (wild_emo + ' ' if wild_emo else '') + t(gid, 'eco.pk_wild_title', level=level)
    rarity = ''
    tgt_line = ''
    is_daily = False
    try:
        with db.conn_ctx() as conn:
            dd = conn.execute('SELECT target FROM pk_hunt_daily WHERE guild_id=? AND user_id=?',
                              (str(gid), str(uid))).fetchone()
            is_daily = bool(dd and (dd['target'] or 0) == dex)
    except Exception:
        pass
    if mystery:
        accent = 0x3A3F4B
    else:
        rk, re_, accent = rarity_of(row, shiny, gid)
        rarity = _rarity_line(gid, row, shiny) + '\n'
        if (target and target == dex) or is_daily:
            tgt_emo = em(gid, 'hunt_target')
            tgt_line = f"\n{tgt_emo + ' ' if tgt_emo else ''}TARGET"
    desc = (f'{found}\n{rarity}{wild_line}{tgt_line}\n'
            f'-# {hint}\n{streak}\n{balls_block}')
    gif = '' if mystery else showdown_gif(row.get('name', ''), shiny)
    if gif:
        try:
            to = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=to) as s3:
                async with s3.head(gif) as r:
                    if r.status != 200:
                        gif = ''
        except Exception:
            gif = ''
    media = ([gif] if gif else []) or ([pix] if not mystery else [])
    is_hunt = bool(((target and target == dex) or is_daily) and not mystery)
    return EncounterResult(ok=True, title=title, desc=desc, accent=accent,
                           media=media, mode=mode, is_hunt=is_hunt, key=key)

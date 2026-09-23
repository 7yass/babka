"""Battle turn service (rework phase 3): wild-battle turn resolution.

Discord-free: no ctx, no interactions, no views. Mutates the passed-in
battle state in place (hp + shared log, exactly like the old in-cog
path) and returns the appended lines as structured events for future
logs/replays — the Discord layer keeps rendering them as today.

Scope is deliberately the WILD kernel only (order + strikes + faint
short-circuits). `_after_turn` (XP/DB/render/sends), potion/switch
branches and the duel inline math stay in the cog for a later pass.

RNG injection: pass any object with random()/uniform()/choice()
(FakeRng in tests). Production defaults to the global random module,
so all existing callers behave identically.
"""
import random as _random
from dataclasses import dataclass, field

from lang import t


@dataclass
class TurnResult:
    events: list = field(default_factory=list)  # log lines appended this turn
    me_hp: int = 0
    wild_hp: int = 0
    me_fainted: bool = False
    wild_fainted: bool = False


def strike(gid, att: dict, dfn: dict, mv: dict, is_me: bool, log: list,
           weather=None, rng=None) -> None:
    """One strike: damage, held-item tweak, hp, effectiveness tag, log line."""
    from cogs.pokemon import (_hit_tag, _mv_name, damage, effectiveness,
                              held_strike)
    dmg, crit = damage(att['level'], mv, att['stats'], dfn['stats'],
                       att['types'], dfn['types'], weather, rng)
    dmg = held_strike(att, dfn, dmg)
    dfn['hp'] = max(0, dfn['hp'] - dmg)
    eff = effectiveness(mv.get('ptype', 'normal'), dfn['types'])
    tag = _hit_tag(gid, eff, crit, dmg)
    who = t(gid, 'eco.pk_you') if is_me else t(gid, 'eco.pk_foe')
    log.append(t(gid, 'eco.pk_hit', who=who, move=_mv_name(gid, mv), dmg=dmg) + tag)


def wild_strike(gid, me: dict, wild: dict, log: list, weather=None,
                rng=None) -> None:
    """Wild retaliation with a random safe move."""
    from cogs.pokemon import _safe_moves
    _rng = rng or _random
    strike(gid, wild, me, _rng.choice(_safe_moves(wild)), False, log,
           weather, rng)


def resolve_turn(st: dict, gid, move_idx=None, rng=None) -> TurnResult:
    """Wild-battle turn: faster strikes first, faint short-circuits.
    move_idx into me['moves'] (None/invalid = random safe fallback).
    Mutates st in place; returns what happened."""
    from cogs.pokemon import _safe_moves
    _rng = rng or _random
    me, wild, log = st['me'], st['wild'], st['log']
    weather = st.get('weather')
    mv = None
    if move_idx is not None:
        moves = me.get('moves') or []
        if 0 <= move_idx < len(moves):
            mv = moves[move_idx]
    before = len(log)
    if me['stats']['spe'] >= wild['stats']['spe']:
        order = [('me', mv), ('wild', None)]
    else:
        order = [('wild', None), ('me', mv)]
    for side, chosen in order:
        if side == 'me':
            if wild['hp'] <= 0:
                break
            use_mv = chosen or _rng.choice(_safe_moves(me))
            strike(gid, me, wild, use_mv, True, log, weather, rng)
        else:
            if me['hp'] <= 0:
                break
            strike(gid, wild, me, _rng.choice(_safe_moves(wild)), False,
                   log, weather, rng)
    return TurnResult(events=log[before:], me_hp=me['hp'], wild_hp=wild['hp'],
                      me_fainted=me['hp'] <= 0, wild_fainted=wild['hp'] <= 0)

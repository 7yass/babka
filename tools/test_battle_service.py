"""Battle turn service regression: order, accuracy, crit, STAB, fainting,
determinism, Discord-free module.

Run: python tools/test_battle_service.py (needs discord installed for cog
imports). Exit code 0 only when every test passes.
"""
import sys
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
    """Deterministic rng: queued random() rolls, fixed uniform, queued picks."""

    def __init__(self, rolls=None, uniform_v=1.0, picks=None):
        self._rolls = list(rolls or [])
        self.uniform_v = uniform_v
        self._picks = list(picks or [])

    def random(self):
        return self._rolls.pop(0) if self._rolls else 0.5

    def uniform(self, a, b):
        return self.uniform_v

    def choice(self, seq):
        if self._picks:
            return seq[self._picks.pop(0) % len(seq)]
        return seq[0]


def mon(name, spe, hp, power=50, acc=100, ptype='normal', types=('normal',),
        move_name='Tackle'):
    return {'name': name, 'level': 10, 'hp': hp, 'held': '',
            'stats': {'atk': 50, 'spa': 50, 'dfn': 50, 'spd': 50,
                      'spe': spe, 'maxhp': hp},
            'types': list(types),
            'moves': [{'name': move_name, 'power': power, 'acc': acc,
                       'ptype': ptype}]}


def state(me, wild):
    return {'me': me, 'wild': wild, 'log': [], 'weather': None}


def main() -> None:
    import services.battle_service as bs

    # Discord-free module: no discord/client/context imports.
    src = (ROOT / 'services' / 'battle_service.py').read_text(encoding='utf-8')
    for bad in ('import discord', 'from discord', 'Interaction', 'Context',
                'commands.Context'):
        check(bad not in src, f'no {bad} in service')

    # order: faster acts first; a mon KO'd mid-turn still strikes back,
    # but an already-fainted target is never struck (corpse rule).
    st = state(mon('ME', 100, 50, move_name='Quick'),
               mon('WILD', 10, 50, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng())
    check(len(res.events) == 2 and 'Quick' in res.events[0]
          and 'Slow' in res.events[1], 'faster acts first')
    st = state(mon('ME', 100, 50, move_name='Quick'),
               mon('WILD', 10, 0, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng())
    check(len(res.events) == 0, 'fainted target is skipped, no crash')
    st = state(mon('ME', 100, 50, move_name='Quick'),
               mon('WILD', 10, 1, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.5, 0.5]))
    check(len(res.events) == 2 and res.wild_fainted
          and st['wild']['hp'] == 0, 'KO sets faint flags + hp floor')

    # invalid move idx falls back to a random safe move, both sides strike.
    st = state(mon('ME', 100, 50), mon('WILD', 10, 50))
    res = bs.resolve_turn(st, 0, 99, FakeRng())
    check(len(res.events) == 2, 'bad move idx falls back, turn completes')

    # miss: acc roll above accuracy deals nothing, MISS tag, turn continues.
    # (acc=0 means never-miss in this codebase: `move.get('acc') or 100`.)
    st = state(mon('ME', 100, 50, acc=50), mon('WILD', 10, 50))
    hp0 = (st['me']['hp'], st['wild']['hp'])
    res = bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.9]))
    check('MISS' in res.events[0] and st['wild']['hp'] == hp0[1],
          'miss tagged, no damage')
    check(len(res.events) == 2 and st['me']['hp'] < hp0[0],
          'turn continues, retaliation lands')

    # crit applies (forced roll) and shows.
    st = state(mon('ME', 100, 50), mon('WILD', 10, 500))
    res = bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.5, 0.0]))
    check('CRIT' in res.events[0], 'forced crit tagged')

    # STAB once: same defender, matching ptype hits harder.
    a = state(mon('ME', 50, 200, power=60, ptype='normal'),
              mon('WILD', 10, 200, types=('fire',)))
    b = state(mon('ME', 50, 200, power=60, ptype='normal', types=('fire',)),
              mon('WILD', 10, 200, types=('fire',)))
    ra = bs.resolve_turn(a, 0, 0, FakeRng())
    rb = bs.resolve_turn(b, 0, 0, FakeRng())
    check((200 - a['wild']['hp']) > (200 - b['wild']['hp']), 'STAB applies once')

    # determinism: identical rolls, identical outcome.
    def run_once():
        s = state(mon('ME', 60, 120, power=55), mon('WILD', 55, 120, power=55))
        bs.resolve_turn(s, 0, 0, FakeRng(rolls=[0.1, 0.2, 0.3, 0.4]))
        return s['me']['hp'], s['wild']['hp'], list(s['log'])
    check(run_once() == run_once(), 'deterministic with injected rng')

    # hp never negative even on massive overkill.
    st = state(mon('ME', 100, 50, power=9999), mon('WILD', 10, 5))
    bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.5, 0.5]))
    check(st['wild']['hp'] == 0, 'hp floored at 0')

    # strict_faint (world-boss mode): a side KO'd mid-turn does not act.
    st = state(mon('ME', 100, 50, move_name='Quick'),
               mon('WILD', 10, 1, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.5, 0.5]), True)
    check(len(res.events) == 1 and 'Quick' in res.events[0]
          and res.wild_fainted, 'strict: fainted foe stays down')
    st = state(mon('ME', 100, 0, move_name='Quick'),
               mon('WILD', 10, 50, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng(), True)
    check(len(res.events) == 0, 'strict: fainted lead aborts the turn')
    # ...while default keeps the legacy retaliation quirk.
    st = state(mon('ME', 100, 50, move_name='Quick'),
               mon('WILD', 10, 1, move_name='Slow'))
    res = bs.resolve_turn(st, 0, 0, FakeRng(rolls=[0.5, 0.5] * 2))
    check(len(res.events) == 2, 'default: KO still retaliates (legacy)')

    # foe_mult scales retaliation only; default keeps legacy numbers.
    def _pair():
        return state(mon('ME', 100, 200, move_name='Quick'),
                     mon('WILD', 10, 200, move_name='Slow'))
    s1, s2 = _pair(), _pair()
    bs.resolve_turn(s1, 0, 0, FakeRng(rolls=[0.5] * 4))
    bs.resolve_turn(s2, 0, 0, FakeRng(rolls=[0.5] * 4), foe_mult=2.0)
    check(s1['wild']['hp'] == s2['wild']['hp'], 'mult spares the player strike')
    check(s2['me']['hp'] < s1['me']['hp'], 'mult scales retaliation')

    print('FAILS: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


main()

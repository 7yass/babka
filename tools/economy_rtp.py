"""Casino RTP simulator — the guard rail for the economy rework.

Every casino table is simulated here and checked against the house-edge band
in utils.economy (CASINO_RTP_MIN..CASINO_RTP_MAX). Slots, coinflip and
roulette are exact closed-form; blackjack and poker are Monte-Carlo with a
decent (not optimal) strategy, which is what real players do.

Run:  py tools/economy_rtp.py
Exits non-zero if any game falls outside the band.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from utils.economy import (CASINO_SLOTS_TABLE, CASINO_SLOTS_TABLE_GOD,
                           CASINO_SLOTS_GATE, CASINO_COINFLIP_RETURN,
                           CASINO_ROU_SCALE, CASINO_POKER_SCALE,
                           CASINO_BJ_SCALE, CASINO_BJ_PAYOUT,
                           CASINO_RTP_MIN, CASINO_RTP_MAX)

N = 400_000


def slots_rtp(table):
    """Exact, INCLUDING the gate.

    `_win_chance` used to discard ~77% of spins before the table ran, so the
    table's own loss row only applied to the rest. Measuring the table alone
    reported 89.6% while the shipped game actually paid 61.8%. Multiplying by
    the gate keeps that class of error impossible to hide.
    """
    return CASINO_SLOTS_GATE * sum(p * ret for p, ret in table)


def coinflip_rtp():
    return 0.5 * CASINO_COINFLIP_RETURN


def roulette_rtp(kind):
    if kind == 'even':
        return (18 / 37) * (1 + 1 * CASINO_ROU_SCALE)
    if kind == 'dozen':
        return (12 / 37) * (1 + 2 * CASINO_ROU_SCALE)
    return (1 / 37) * (1 + 35 * CASINO_ROU_SCALE)      # straight-up


# ---------------------------------------------------------------- blackjack
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']


def shoe(k=6):
    cards = [((r, 10) if r in ('10', 'J', 'Q', 'K') else (r, int(r) if r != 'A' else 11))
             for r in RANKS for _ in range(k)]
    random.shuffle(cards)
    return cards


def hand_total(h):
    t = sum(v for _, v in h)
    aces = sum(1 for r, _ in h if r == 'A')
    while t > 21 and aces:
        t -= 10
        aces -= 1
    return t


def blackjack_rtp():
    """Player hits to 17 and stands; dealer draws to 17+.

    Returns (P(win), P(push)). A win returns bet + bet*BJ_SCALE; a push
    returns the stake back. Hands are appended to the hand list itself
    (never read back out of the shoe) so the totals actually advance.
    """
    hands = 60_000
    wins = pushes = 0
    for _ in range(hands):
        deck = shoe()
        ph = [deck.pop() for _ in range(2)]
        dh = [deck.pop() for _ in range(2)]
        pt = hand_total(ph)
        while pt < 17 and len(deck) > 15:
            ph.append(deck.pop())
            pt = hand_total(ph)
        if pt > 21:
            continue                                   # bust: house takes it
        dt = hand_total(dh)
        while dt < 17 and len(deck) > 2:
            dh.append(deck.pop())
            dt = hand_total(dh)
        if dt > 21 or pt > dt:
            wins += 1
        elif pt == dt:
            pushes += 1
    return wins / hands, pushes / hands


# ------------------------------------------------------------------- poker
POKER_PAY = {'ROYAL': 250, 'STRAIGHT_FLUSH': 50, 'FOUR': 25, 'FULL_HOUSE': 9,
             'FLUSH': 6, 'STRAIGHT': 4, 'THREE': 3, 'TWO_PAIR': 2, 'JACKS': 1}


def _cat(cards):
    vals = sorted((v for _, v in cards), reverse=True)
    rk = sorted((r for r, _ in cards), key=lambda r: RANKS.index(r))
    flush = len({r for r, _ in cards}) == 1
    uniq = sorted(set(rk), key=lambda r: RANKS.index(r))
    straight = len(uniq) == 5 and RANKS.index(uniq[-1]) - RANKS.index(uniq[0]) == 4
    if flush and straight:
        return ('ROYAL', POKER_PAY['ROYAL']) if rk[-1] == 'A' else ('STRAIGHT_FLUSH', POKER_PAY['STRAIGHT_FLUSH'])
    cnt = {}
    for r in rk:
        cnt[r] = cnt.get(r, 0) + 1
    groups = sorted(cnt.values(), reverse=True)
    if groups[0] == 4:
        return 'FOUR', POKER_PAY['FOUR']
    if groups[:2] == [3, 2]:
        return 'FULL_HOUSE', POKER_PAY['FULL_HOUSE']
    if flush:
        return 'FLUSH', POKER_PAY['FLUSH']
    if straight:
        return 'STRAIGHT', POKER_PAY['STRAIGHT']
    if groups[0] == 3:
        return 'THREE', POKER_PAY['THREE']
    if groups[:2] == [2, 2]:
        return 'TWO_PAIR', POKER_PAY['TWO_PAIR']
    for want in ('J', 'Q', 'K', 'A'):
        if cnt.get(want):
            return 'JACKS', POKER_PAY['JACKS']
    return 'NONE', 0


def poker_rtp():
    """Greedy hold-the-best-five strategy, 3 discards; scales the base table.

    Deck is never mutated during evaluation: each candidate hold draws from a
    positional copy, so the real draw order stays intact.
    """
    from itertools import combinations
    total = 0.0
    hands = 20_000
    deck = shoe()
    for _ in range(hands):
        if len(deck) < 5:
            deck = shoe()
        deal = [deck.pop() for _ in range(5)]
        for _ in range(3):
            if len(deck) < 8:
                deck = shoe()
            best, bestkeep = -1, None
            for r in range(1, 6):
                for keep in combinations(range(5), r):
                    need = 5 - r
                    cand = [deal[i] for i in keep] + [deck[j] for j in range(need)]
                    v = _cat(cand)[1]
                    if v > best:
                        best, bestkeep = v, list(keep)
            if bestkeep is None:
                break
            keepset = set(bestkeep)
            dropped = [deal[i] for i in range(5) if i not in keepset]
            deal = [deal[i] for i in bestkeep]
            for c in dropped:
                deck.append(c)
            deal += [deck.pop() for _ in range(len(dropped))]
        total += _cat(deal)[1] * CASINO_POKER_SCALE
    return total / hands


def main():
    bj_win, bj_push = blackjack_rtp()
    bj = bj_win * (1 + CASINO_BJ_PAYOUT * CASINO_BJ_SCALE) + bj_push
    rows = [
        ('slots (player)', slots_rtp(CASINO_SLOTS_TABLE)),
        ('slots (god)', slots_rtp(CASINO_SLOTS_TABLE_GOD)),
        ('coinflip', coinflip_rtp()),
        ('roulette even', roulette_rtp('even')),
        ('roulette dozen', roulette_rtp('dozen')),
        ('roulette straight', roulette_rtp('straight')),
        ('blackjack', bj),
        ('poker (jacks+)', poker_rtp()),
    ]
    print(f'{"game":20s} {"RTP":>8s} {"edge":>8s}')
    print('-' * 38)
    bad = []
    for name, rtp in rows:
        edge = (1 - rtp) * 100
        flag = ''
        if name != 'slots (god)' and not (CASINO_RTP_MIN <= rtp <= CASINO_RTP_MAX):
            flag = '  <-- OUT OF BAND'
            bad.append(name)
        print(f'{name:20s} {rtp * 100:7.1f}% {edge:7.1f}%{flag}')
    print('-' * 38)
    print(f'band: {CASINO_RTP_MIN * 100:.0f}%-{CASINO_RTP_MAX * 100:.0f}% '
          f'({(1 - CASINO_RTP_MAX) * 100:.0f}%-{(1 - CASINO_RTP_MIN) * 100:.0f}% edge)')
    if CASINO_SLOTS_GATE != 1.0:
        print(f'FAIL: CASINO_SLOTS_GATE={CASINO_SLOTS_GATE} stacks a second house '
              f'edge on top of the paytable loss row')
        bad.append('slots gate')
    if bad:
        print('FAIL:', bad)
        return 1
    print('OK: every game inside the band')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

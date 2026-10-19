"""Economy flow guard rail — is the faucet balanced against the sink?

tools/economy_rtp.py guards the casino: every table must sit inside the
house-edge band. This guards the whole economy: a modelled player-day must
not drift far from neutral.

Why this exists: the bot had fifteen currencies and no way to see money
appearing or disappearing. Now every movement is in the ledger, and this
tool models what an ordinary active player's day looks like and checks the
faucet against the sink. Double a faucet, delete a sink, or let a payout
scale slip and this fails.

It also reads the live ledger when a database is present, so the model can
be compared against what actually happened.

Run:  py tools/economy_balance.py [--live]
Exits non-zero when the modelled net flow leaves the band.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from utils.economy import (BALANCE_NET_MAX, BALANCE_NET_MIN, CASINO_RTP_MAX,
                           CASINO_RTP_MIN, DAILY_CASH, STREAK_MAX_MULT,
                           STREAK_MULT_PER_STEP, STREAK_STEP, TAX_CAP,
                           TAX_RATE, TAX_THRESHOLD)

# ---------------------------------------------------------------- model
# A representative active player-day. These are BEHAVIOUR assumptions,
# not money, so they live here and not in utils/economy.py.
DAILY_CLAIMS = 1          # one daily per day
WORK_SHIFTS = 6           # shifts a reasonably active player works
CASINO_PLAYS = 4          # plays a player gets through on tickets
CASINO_STAKE = 3000       # a typical stake
SHOP_SPEND_PROB = 0.5     # days on which a player buys something
SHOP_SPEND_AVG = 30000    # average basket when they do
CARD_PACKS_PER_DAY = 0.4  # card pulls are the economy's main sink
CARD_PACK_AVG = 50000
TRIBUTE_AVG = 1500        # tips given per day
ROB_EXPECT = 900          # expected robbery loot per day
TRANSFER_AVG = 20000      # player-to-player transfers per day

# The average daily multiplier a player sits at: most people keep a chain
# of a few days, some keep it forever. Weighted toward the low end.
AVG_STREAK = 4.0


def avg_streak_mult() -> float:
    steps = (int(AVG_STREAK) - 1) // STREAK_STEP
    return min(1.0 + steps * STREAK_MULT_PER_STEP, STREAK_MAX_MULT)


def avg_job_pay() -> float:
    """Average pay per shift, from the real job table when available."""
    try:
        from cogs.jobs import JOBS, LADDER
        pays = []
        for spec in JOBS.values():
            lo, hi = spec.get('base', (0, 0))
            pays.append((int(lo) + int(hi)) / 2)
        if not pays:
            return 0
        avg_base = sum(pays) / len(pays)
        mults = [m for _, _, m in LADDER]
        return avg_base * (sum(mults) / len(mults))
    except Exception as e:
        print(f'  (job table unavailable: {e}) - using the fallback')
        return 0


def casino_rtp() -> float:
    """Blended RTP across the tables, using the same formulas as the
    casino guard rail so the two tools cannot disagree."""
    try:
        from tools.economy_rtp import (blackjack_rtp, coinflip_rtp, poker_rtp,
                                       roulette_rtp, slots_rtp)
        from utils.economy import (CASINO_BJ_PAYOUT, CASINO_BJ_SCALE,
                                   CASINO_SLOTS_TABLE)
        bj_win, bj_push = blackjack_rtp()
        bj = bj_win * (1 + CASINO_BJ_PAYOUT * CASINO_BJ_SCALE) + bj_push
        rtp = [slots_rtp(CASINO_SLOTS_TABLE), coinflip_rtp(),
               roulette_rtp('even'), roulette_rtp('dozen'), bj, poker_rtp()]
        return sum(rtp) / len(rtp)
    except Exception as e:
        print(f'  (RTP simulation unavailable: {e}) - using the band midpoint')
        return (CASINO_RTP_MIN + CASINO_RTP_MAX) / 2


def main(argv):
    live = '--live' in argv

    daily = DAILY_CASH * avg_streak_mult() * DAILY_CLAIMS
    work = avg_job_pay() * WORK_SHIFTS
    casino_staked = CASINO_PLAYS * CASINO_STAKE
    rtp = casino_rtp()
    casino_back = casino_staked * rtp
    casino_edge = casino_staked - casino_back
    shop = SHOP_SPEND_PROB * SHOP_SPEND_AVG
    cards = CARD_PACKS_PER_DAY * CARD_PACK_AVG
    tribute = TRIBUTE_AVG
    rob = ROB_EXPECT
    transfers = TRANSFER_AVG
    # the transfer fee is a sink on player-to-player money
    transfer_tax = min(int(transfers * TAX_RATE), TAX_CAP) if transfers > TAX_THRESHOLD else 0

    faucet = daily + work + casino_back + rob
    sink = casino_edge + shop + cards + tribute + transfer_tax
    gross = faucet + sink
    net = faucet - sink
    frac = (net / gross) if gross else 0.0

    print('modelled active player-day')
    print('-' * 52)
    print(f'{"FAUCET":28s} {"amount":>12s}')
    for label, val in (('daily', daily), ('work shifts', work),
                        ('casino returns', casino_back), ('robbery loot', rob)):
        print(f'  {label:26s} {val:12,.0f}')
    print(f'{"  faucet total":28s} {faucet:12,.0f}')
    print('-' * 52)
    print(f'{"SINK":28s} {"amount":>12s}')
    for label, val in (('casino house edge', casino_edge), ('shop purchases', shop),
                        ('card pulls', cards), ('tribute', tribute),
                        ('transfer fees', transfer_tax)):
        print(f'  {label:26s} {val:12,.0f}')
    print(f'  sink total                    {sink:12,.0f}')
    print('-' * 52)
    print(f'{"NET":28s} {net:12,.0f}   ({frac:+.1%} of gross)')
    print(f'band: {BALANCE_NET_MIN:+.0%} to {BALANCE_NET_MAX:+.0%}')

    bad = []
    if not (BALANCE_NET_MIN <= frac <= BALANCE_NET_MAX):
        bad.append('modelled net flow outside the band')
    if rtp < CASINO_RTP_MIN or rtp > CASINO_RTP_MAX:
        bad.append(f'blended casino RTP {rtp:.1%} outside the house-edge band')

    if live:
        print('-' * 52)
        try:
            from services import ledger
            import database as db
            rows = db.conn_ctx()
            with rows as conn:
                guilds = [r['guild_id'] for r in
                          conn.execute('SELECT DISTINCT guild_id FROM eco_ledger').fetchall()]
            for g in guilds:
                f = ledger.flow(g, 30)
                if not f['moves']:
                    continue
                ratio = f['ratio']
                state = 'healthy'
                if ratio == float('inf'):
                    state = 'INFLATING'
                elif ratio > 1.35:
                    state = 'inflating'
                elif ratio < 0.7:
                    state = 'deflating'
                print(f'  guild {g}: in {f["inflow"]:,.0f}  out {f["outflow"]:,.0f}  '
                      f'net {f["net"]:,.0f}  ratio {ratio:.2f}  {state}')
        except Exception as e:
            print(f'  (live ledger unavailable: {e})')

    print('-' * 52)
    if bad:
        print('FAIL: ' + '; '.join(bad))
        return 1
    print('OK: the economy is inside the band')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

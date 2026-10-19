"""Authoritative economy values — single source of truth.

RULE: commands read from here; help text is verified against here
(`.health commands`). Never duplicate these numbers as literals elsewhere.

Only MONEY lives here: prices, bets, payouts, stakes, rewards, fees.
Non-money numbers stay with their own systems: levels, cooldowns,
pagination, damage, team sizes, item quantities, percentage chances
(unless a chance directly sets a payout).

Naming: DOMAIN_WHAT_DETAIL (SHOP_PRICES['nick'], CASINO_BJ_PAYOUT).
Never generic PRICE_1 / VALUE_A — those rot invisibly.

Catalogs (SHOP_PRICES, POKE_SHOP_PRICES) are the canonical monetary
representation: command catalogs (ITEMS, BALLS, HELD_ITEMS, ...) consume
`.amount` and keep only names/icons/effects. Keys are each catalog's own
item keys — the economy 'lootbox' (75k gamble box) and the pokemon
'lootbox' (15k item) are different things that happen to share a word.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ShopPrice:
    """Canonical monetary row. Catalogs consume .amount; nothing else
    stores item prices."""
    item_id: str
    amount: int
    currency: str = "coins"


# ---- economy board (`.shop`): keys are ITEMS keys ----
SHOP_PRICES: dict[str, ShopPrice] = {
    'cookie': ShopPrice('cookie', 7500),
    'scratch': ShopPrice('scratch', 10000),
    'lootbox': ShopPrice('lootbox', 75000),
    'nick': ShopPrice('nick', 125000),
    'shield': ShopPrice('shield', 175000),
    'xpboost': ShopPrice('xpboost', 200000),
    'pardon': ShopPrice('pardon', 300000),
    'curse': ShopPrice('curse', 400000),
    'megabox': ShopPrice('megabox', 400000),
    'force': ShopPrice('force', 500000),
    'highroller': ShopPrice('highroller', 2000000),
    'bail': ShopPrice('bail', 40000),
    'vip': ShopPrice('vip', 5000000),
    'card_pack_std': ShopPrice('card_pack_std', 25000),
    'card_pack_mono': ShopPrice('card_pack_mono', 75000),
    'card_pack_animated': ShopPrice('card_pack_animated', 150000),
}

# ---- pokemon adventure shop (`;shop`, `;balls`): keys are the pokemon
# catalog keys (BALLS / POTIONS / HELD_ITEMS / EVO_STONES / SHOP_EXTRAS
# plus scalar items). ----
POKE_SHOP_PRICES: dict[str, ShopPrice] = {
    'poke': ShopPrice('poke', 500),
    'great': ShopPrice('great', 1500),
    'ultra': ShopPrice('ultra', 4000),
    'master': ShopPrice('master', 50000),
    'potion': ShopPrice('potion', 1000),
    'superpotion': ShopPrice('superpotion', 3000),
    'candy': ShopPrice('candy', 10000),
    'grazz': ShopPrice('grazz', 4000),
    'egg': ShopPrice('egg', 12000),
    'incense': ShopPrice('incense', 25000),
    'repel': ShopPrice('repel', 5000),
    'leftovers': ShopPrice('leftovers', 15000),
    'lifeorb': ShopPrice('lifeorb', 20000),
    'focusband': ShopPrice('focusband', 18000),
    'luckyegg': ShopPrice('luckyegg', 22000),
    'pow_hp': ShopPrice('pow_hp', 12000),
    'pow_atk': ShopPrice('pow_atk', 12000),
    'pow_dfn': ShopPrice('pow_dfn', 12000),
    'pow_spa': ShopPrice('pow_spa', 12000),
    'pow_spd': ShopPrice('pow_spd', 12000),
    'pow_spe': ShopPrice('pow_spe', 12000),
    'water_stone': ShopPrice('water_stone', 15000),
    'thunder_stone': ShopPrice('thunder_stone', 15000),
    'fire_stone': ShopPrice('fire_stone', 15000),
    'sun_stone': ShopPrice('sun_stone', 15000),
    'moon_stone': ShopPrice('moon_stone', 15000),
    'leaf_stone': ShopPrice('leaf_stone', 15000),
    'ice_stone': ShopPrice('ice_stone', 15000),
    'shiny_stone': ShopPrice('shiny_stone', 15000),
    'amuletcoin': ShopPrice('amuletcoin', 200000),
    'questscroll': ShopPrice('questscroll', 15000),
    'shinycharm': ShopPrice('shinycharm', 75000),
    'lootbox': ShopPrice('lootbox', 15000),
    'wailmer_pail': ShopPrice('wailmer_pail', 1000000),
    'expshare': ShopPrice('expshare', 2500000),
    'evolutionstone': ShopPrice('evolutionstone', 30000),
    'mega_bracelet': ShopPrice('mega_bracelet', 150000),
}

# ---- casino ----------------------------------------------------------
# Gambling is GATED BY TICKETS, not an hourly counter. One ticket buys one
# play on any game. Tickets arrive from `.drop` (CASINO_TIX_DROP_CHANCE per
# claim) and `.daily` (CASINO_TIX_DAILY guaranteed). CASINO_TIX_MAX is the
# hoard ceiling so the stack can't be banked indefinitely.
#
# HOUSE EDGE: every game pays its profit through a scale factor. Scales are
# chosen so realized RTP lands in the 88-93% band (7-12% edge) — see
# `tools/economy_rtp.py`, which simulates each table and fails the build if a
# game drifts outside CASINO_RTP_MIN..CASINO_RTP_MAX.
CASINO_TIX_DROP_CHANCE = 0.25   # per .drop claim
CASINO_TIX_DAILY = 2           # per .daily
CASINO_TIX_MAX = 50            # hoard ceiling

CASINO_BASE_MAX_BET = 12000
CASINO_MAX_BET_PER_LEVEL = 1500
CASINO_MAX_BET_CAP = 60000

# Per-game profit scales (1.0 = paytable as written).
CASINO_ROU_SCALE = 0.90        # even 92.4% / dozen 90.8% / straight 87.8%
CASINO_POKER_SCALE = 0.88
CASINO_BJ_SCALE = 0.92

CASINO_BJ_PAYOUT = 1.0          # blackjack natural, before BJ_SCALE
CASINO_BJ_GOD_PAYOUT = 1.3      # house edge, kept deniable
CASINO_COINFLIP_RETURN = 1.78   # fair 50/50 coin: 0.5 * 1.78 = 89% RTP
CASINO_COIN_CAP_MULT = 1

# Win caps scale with stake so big bets matter: profit = min(raw,
# max(FLAT_CAP, STAKE_MULT * bet)). Poker HR mult doubles the scaling.
CASINO_POKER_CAP_MULT = 5
CASINO_POKER_HR_CAP_MULT = 10
CASINO_SLOTS_CAP_MULT = 4
CASINO_ROU_CAP_MULT = 5
CASINO_BJ_CAP_MULT = 2

# ---- slots paytable --------------------------------------------------
# (probability, TOTAL RETURN multiplier). A losing spin returns 0 — it does
# NOT return the stake, so the loss row must be 0.0 and not "profit 0".
# RTP is the expected value of return: sum(p * ret).
# These give 89.6% (10.4% edge), down from the old table's 271%, where a
# "pair" paid 2x on 72% of spins and the machine lost 171 per 100 wagered.
CASINO_SLOTS_TABLE = (
    (0.02, 8.0),   # jackpot: three 7s      (profit 7)
    (0.10, 4.0),   # triple, any non-7     (profit 3)
    (0.28, 1.2),   # pair                  (profit 0.2)
    (0.60, 0.0),   # three distinct: lose  (profit 0)
)
# The god branch: still tilted, still deniable.
CASINO_SLOTS_TABLE_GOD = (
    (0.03, 8.0),
    (0.13, 4.0),
    (0.34, 1.2),
    (0.50, 0.0),
)

# The paytable is the ONLY loss gate. This used to sit at ~0.30 (log-scaled
# by stake) in front of the table, so the table's own loss row was applied to
# only ~23% of spins and real RTP landed at 61.8%. Anything below 1.0 here
# silently stacks a second house edge; tools/economy_rtp.py fails if it moves.
CASINO_SLOTS_GATE = 1.0

# Simulated RTP guard rails enforced by tools/economy_rtp.py.
CASINO_RTP_MIN = 0.86
CASINO_RTP_MAX = 0.95

# ---- crime ----
# Stakes and multipliers per heist target. Chances (base 0.30 + 0.07/crew,
# god 0.90), JOIN_WINDOW and jail durations are NOT money and live in code.


@dataclass(frozen=True)
class HeistTarget:
    key: str
    stake: int
    payout_mult: float


CRIME_HEIST_TARGETS: dict[str, HeistTarget] = {
    'bank': HeistTarget('bank', stake=300, payout_mult=2.0),
    'kasyno': HeistTarget('kasyno', stake=600, payout_mult=2.4),
    'muzeum': HeistTarget('muzeum', stake=1000, payout_mult=2.8),
}


@dataclass(frozen=True)
class RobTuning:
    """Robbery formula parameters. The formula itself (loot = cut of victim
    cash, fine = cut paid back + jail) stays in code; only its monetary
    bounds live here. Chances stay out (not money)."""
    loot_min_pct: float
    loot_max_pct: float
    loot_min: int
    loot_cap: int
    fine_min: int
    fine_pct: float
    fine_cap: int


CRIME_ROB = RobTuning(loot_min_pct=0.08, loot_max_pct=0.20, loot_min=10,
                      loot_cap=25000, fine_min=100, fine_pct=0.20, fine_cap=15000)

CRIME_BOUNTY_MIN = 100


# ---- wallet seed & daily --------------------------------------------
# What a fresh wallet holds, and what the daily hands out. These lived in
# cogs/gamble.py as literals; they are money, so they live HERE.
START_CASH = 1000
DAILY_CASH = 1500
DAILY_CD = 86400

# ---- streaks ---------------------------------------------------------
# The cheapest legible progression there is: it costs a player nothing but
# attention and it pays every single day. The multiplier steps up every
# STREAK_STEP days and is capped, so a streak is worth chasing but not
# worth breaking your life over.
STREAK_KINDS = ('daily', 'work')
STREAK_STEP = 6              # days per multiplier step
STREAK_MULT_PER_STEP = 0.1   # +0.1x per step
STREAK_MAX_MULT = 2.0
STREAK_GRACE = 172800        # 48h: one missed day does NOT kill the streak

# ---- bank interest ---------------------------------------------------
# Interest on the banked balance. The flat cap is gone: the cap and the
# rate both scale with the player's level, so a level-1 account and a
# level-80 account do not earn the same 500 forever. The bank is a real
# money sink for players who do not want to gamble it.
BANK_INTEREST_RATE = 0.02          # per day, on the banked balance
BANK_INTEREST_LEVEL_BONUS = 0.002  # +0.2% of the rate per level above 1
BANK_INTEREST_MIN = 1
BANK_INTEREST_CAP_BASE = 500       # cap at level 1
BANK_INTEREST_CAP_PER_LEVEL = 60   # +60 cap per level
BANK_INTEREST_MAX_CAP = 5000

# ---- tax -------------------------------------------------------------
# An economy needs sinks as well as faucets. Windfalls above the threshold
# pay a small tax into the guild treasury, which buys upgrades that benefit
# every member. Capped so a big win is never punished into nothing.
# The threshold sits below a normal tribute so the fee actually bites
# instead of only ever catching whale-sized transfers.
TAX_THRESHOLD = 10000
TAX_RATE = 0.03
TAX_CAP = 5000

# ---- valuation (net worth only — NOT tradable) -----------------------
# Shadow prices used to turn non-coin holdings into a single number. They
# are deliberately conservative and never leave the net-worth screen.
TICKET_VALUE = 2500        # one casino ticket
CARD_TICKET_VALUE = 1500   # one card-pull ticket
SSR_TICKET_VALUE = 20000   # one guaranteed LR+ ticket
CARD_VALUE = {'C': 100, 'R': 400, 'SR': 1500, 'LR': 5000, 'UR': 10000}
DUST_RATE = 0.1            # dust is grind currency: 10 000 dust ~= one card

# ---- flow balance band ----------------------------------------------
# The modelled player-day (tools/economy_balance.py) must not drift far
# from neutral. A strongly positive net means the economy inflates - money
# piles up and every price drifts with it. A strongly negative net means it
# deflates - players hoard, and a new player hits a wall they cannot
# afford to climb.
#
# The band is deliberately asymmetric. Mild deflation is the healthier
# failure: prices drift down, new players catch up, and a rich player
# always has somewhere to spend. Inflation is the one that quietly ruins
# an economy, so it gets the tighter bound.
BALANCE_NET_MIN = -0.40     # net as a fraction of gross flow
BALANCE_NET_MAX = 0.15

# ---- guild upgrades --------------------------------------------------
# The server-wide progression track. Members pay into the guild treasury,
# the guild buys upgrades, and every member gets the buff. Costs are
# per-level and the effect is per-member, so a single whale cannot make
# the upgrade worthless for everyone else.
GUILD_UPGRADES = {
    'luck': {
        'name': 'luck', 'max': 3,
        'costs': (60000, 180000, 450000),
        'effect': 'chance', 'per_level': 0.01,
    },
    'vault': {
        'name': 'vault', 'max': 3,
        'costs': (50000, 150000, 400000),
        'effect': 'interest', 'per_level': 0.01,
    },
    'hearth': {
        'name': 'hearth', 'max': 3,
        'costs': (40000, 120000, 320000),
        'effect': 'daily_mult', 'per_level': 0.1,
    },
}
GUILD_UPGRADE_TAX_SHARE = 1.0   # fraction of a tax that funds upgrades

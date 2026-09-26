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

# ---- casino ----
CASINO_BASE_MAX_BET = 15000
CASINO_MAX_BET_PER_LEVEL = 2000
CASINO_MAX_BET_CAP = 100000
CASINO_BJ_PAYOUT = 1.0          # blackjack pays even money
CASINO_BJ_GOD_PAYOUT = 1.3      # house edge, kept deniable
CASINO_COINFLIP_RETURN = 1.9    # fair 50/50 coin, 1.9x back: the 5% gap
                                # is the whole edge, stated in help
# Win caps scale with stake so big bets matter: profit = min(raw,
# max(FLAT_CAP, STAKE_MULT * bet)). Poker HR mult doubles the scaling.
CASINO_POKER_CAP_MULT = 5
CASINO_POKER_HR_CAP_MULT = 10
CASINO_SLOTS_CAP_MULT = 4
CASINO_ROU_CAP_MULT = 5
CASINO_BJ_CAP_MULT = 2

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

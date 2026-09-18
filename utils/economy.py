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
CASINO_MAX_BET_PER_LEVEL = 1500
CASINO_MAX_BET_CAP = 100000
CASINO_BJ_PAYOUT = 1.2          # blackjack pays 6:5
CASINO_BJ_GOD_PAYOUT = 1.3      # house edge, kept deniable

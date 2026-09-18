"""Authoritative economy values — single source of truth.

RULE: commands read from here; help text is verified against here
(`.health commands`). Never duplicate these numbers as literals elsewhere.

Only MONEY lives here: prices, bets, payouts, stakes, rewards, fees.
Non-money numbers stay with their own systems: levels, cooldowns,
pagination, damage, team sizes, item quantities, percentage chances
(unless a chance directly sets a payout).

Naming: DOMAIN_WHAT_DETAIL (SHOP_NICK_COST, CASINO_BJ_PAYOUT).
Never generic PRICE_1 / VALUE_A — those rot invisibly.
"""
from dataclasses import dataclass

# ---- economy board: shop ----
SHOP_NICK_COST = 125000

# ---- pokemon consumables ----
POKE_GRAZZ_COST = 4000

# ---- casino ----
CASINO_BASE_MAX_BET = 15000
CASINO_MAX_BET_PER_LEVEL = 1500
CASINO_MAX_BET_CAP = 100000
CASINO_BJ_PAYOUT = 1.2          # blackjack pays 6:5
CASINO_BJ_GOD_PAYOUT = 1.3      # house edge, kept deniable


@dataclass(frozen=True)
class ShopPrice:
    """Typed price row for future shop catalog migrations."""
    key: str
    amount: int

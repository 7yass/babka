"""Economy config regression: help text and command math read utils.economy.

Run: python tools/test_economy_config.py (needs discord installed for cog
imports). Exit code 0 only when every test passes.
"""
import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    safe = msg.encode('ascii', 'backslashreplace').decode()
    print(('PASS ' if cond else 'FAIL ') + safe, flush=True)
    if not cond:
        FAILS.append(msg)


async def main() -> None:
    import database as db
    tmp = Path(tempfile.mkdtemp()) / 'test.db'
    db.DB_PATH = tmp
    db.init_db()

    from utils import economy as ECO
    from utils.economy import ShopPrice
    import dataclasses
    check(isinstance(ShopPrice(key='x', amount=1), ShopPrice)
          and ShopPrice.__dataclass_params__.frozen,
          'ShopPrice typed row exists frozen for next slice')
    from lang import t

    # 1. help text quotes config (en; case-insensitive, k-format)
    def short(n: int) -> str:
        return f'{n // 1000}k' if n % 1000 == 0 else f'{n:,}'

    s = json.load(io.open(ROOT / 'lang.json', encoding='utf-8'))['en']
    meta = json.load(io.open(ROOT / 'helpmeta.json', encoding='utf-8'))['meta']

    def text(key: str) -> str:
        if key in s:
            return s[key]
        if key.startswith('hc_d_'):
            return meta.get(key[5:], [''])[1]
        return ''
    check(short(ECO.SHOP_NICK_COST).lower() in text('hc_d_nick').lower(),
          'help nick quotes SHOP_NICK_COST')
    check(short(ECO.CASINO_BASE_MAX_BET).lower() in text('hc_d_blackjack').lower(),
          'help blackjack quotes CASINO_BASE_MAX_BET')
    check(short(ECO.POKE_GRAZZ_COST).lower() in text('eco.pk_grazz_none').lower(),
          'help grazz quotes POKE_GRAZZ_COST')

    # 2. command math uses the same values
    from cogs.shop import Shop, ITEMS
    check(ITEMS['nick']['price'] == ECO.SHOP_NICK_COST,
          'shop nick price == config')
    from cogs.pokemon import Pokemon
    check(Pokemon._pk_price('grazz') == ECO.POKE_GRAZZ_COST,
          'pokemon grazz price == config')
    from cogs.gamble import max_bet_for
    check(max_bet_for(0) == ECO.CASINO_BASE_MAX_BET,
          'bet base == config')
    check(max_bet_for(1) == ECO.CASINO_BASE_MAX_BET + ECO.CASINO_MAX_BET_PER_LEVEL,
          'bet level 1 == config')
    check(max_bet_for(10) == ECO.CASINO_BASE_MAX_BET + 10 * ECO.CASINO_MAX_BET_PER_LEVEL,
          'bet curve == config')
    check(max_bet_for(999) == ECO.CASINO_MAX_BET_CAP,
          'bet cap == config')

    # 3. end-to-end: economy buy + pokemon buy resolve from config values
    from cogs.gamble import bal, set_cash
    shop = Shop.__new__(Shop)
    shop.bot = MagicMock()

    def mkctx(item, n, prefix):
        ctx = MagicMock()
        ctx.guild.id = 99
        ctx.author.id = 7
        ctx.author.display_name = 't'
        ctx.prefix = prefix
        ctx.invoked_with = 'buy'
        ctx.invoked_parents = ['shop']
        ctx.reply = AsyncMock()
        return ctx

    set_cash(99, 7, 10_000_000)
    ctx = mkctx('nick', '1', '.')
    await Shop.buy.callback(shop, ctx, 'nick', '1')
    args, _ = ctx.reply.call_args
    check('Nick Token' in (args[0] if args else ''), 'economy nick purchase works')
    spent = 10_000_000 - bal(99, 7)['cash']
    check(spent == ECO.SHOP_NICK_COST, 'nick charged config amount')

    pcog = Pokemon.__new__(Pokemon)
    pcog.bot = MagicMock()
    shop.bot.get_cog = MagicMock(return_value=pcog)
    set_cash(99, 7, 10_000_000)
    ctx2 = mkctx('grazz', '2', ';')
    await Shop.buy.callback(shop, ctx2, 'grazz', '2')
    spent2 = 10_000_000 - bal(99, 7)['cash']
    check(spent2 == 2 * ECO.POKE_GRAZZ_COST, 'grazz charged config amount')

    print('FAILURES: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


asyncio.run(main())

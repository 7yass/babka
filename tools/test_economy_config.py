"""Economy config regression: catalogs, help text and purchase math all read
utils.economy. Buyback/refund behavior is out of scope (no shop buyback
exists; market fee is formula-based, not a literal).

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


def short(n: int) -> str:
    return f'{n // 1000}k' if n % 1000 == 0 else f'{n:,}'


async def main() -> None:
    import database as db
    tmp = Path(tempfile.mkdtemp()) / 'test.db'
    db.DB_PATH = tmp
    db.init_db()

    from utils.economy import SHOP_PRICES, POKE_SHOP_PRICES, ShopPrice
    import dataclasses
    check(isinstance(ShopPrice(item_id='x', amount=1), ShopPrice)
          and ShopPrice.__dataclass_params__.frozen,
          'ShopPrice typed row exists frozen')
    _ = dataclasses

    s = json.load(io.open(ROOT / 'lang.json', encoding='utf-8'))['en']
    meta = json.load(io.open(ROOT / 'helpmeta.json', encoding='utf-8'))['meta']

    def text(key: str) -> str:
        if key in s:
            return s[key]
        if key.startswith('hc_d_'):
            return meta.get(key[5:], [''])[1]
        return ''

    # 1. help output quotes config
    check(short(SHOP_PRICES['nick'].amount).lower() in text('hc_d_nick').lower(),
          'help nick quotes config')
    check('125' in text('hc_d_nick'), 'help nick shows 125k')
    from cogs.gamble import max_bet_for, CASINO_BASE_MAX_BET
    check(short(CASINO_BASE_MAX_BET).lower() in text('hc_d_blackjack').lower(),
          'help blackjack quotes config')
    check(short(POKE_SHOP_PRICES['grazz'].amount).lower()
          in text('eco.pk_grazz_none').lower(), 'help grazz quotes config')

    # 2. catalogs consume the rows (full sweep, both storefronts)
    from cogs.shop import Shop, ITEMS
    for k, row in SHOP_PRICES.items():
        check(ITEMS[k]['price'] == row.amount, 'eco catalog row %s' % k)
    from cogs.pokemon import Pokemon
    for k, row in POKE_SHOP_PRICES.items():
        check(Pokemon._pk_price(k) == row.amount, 'poke catalog row %s' % k)

    # 3. purchases: normal, multi-qty invariant, broke, unknown
    from cogs.gamble import bal, set_cash
    shop = Shop.__new__(Shop)
    shop.bot = MagicMock()

    def mkctx(item, n, prefix):
        ctx = MagicMock()
        ctx.guild.id = 99
        ctx.guild.roles = []
        ctx.author.id = 7
        ctx.author.display_name = 't'
        ctx.author.roles = []
        ctx.prefix = prefix
        ctx.invoked_with = 'buy'
        ctx.invoked_parents = ['shop']
        ctx.reply = AsyncMock()
        return ctx

    async def buy(item, n, prefix, cash=10_000_000):
        set_cash(99, 7, cash)
        ctx = mkctx(item, n, prefix)
        await Shop.buy.callback(shop, ctx, item, n)
        assert ctx.reply.call_count == 1
        args, _ = ctx.reply.call_args
        return (args[0] if args else ''), bal(99, 7)['cash']

    msg, cash = await buy('nick', '1', '.')
    check('Nick Token' in msg and cash == 10_000_000 - SHOP_PRICES['nick'].amount,
          'normal purchase charges catalog price')
    msg, cash = await buy('shield', '3', '.')
    check(cash == 10_000_000 - 3 * SHOP_PRICES['shield'].amount,
          'charged == price x quantity')
    msg, cash = await buy('vip', '1', '.', cash=1000)
    check('Not enough cash' in msg or 'biedny' in msg,
          'insufficient balance refused')
    check(cash == 1000, 'insufficient balance charges nothing')
    msg, cash = await buy('nope_item', '1', '.')
    check('don' in msg.lower() or 'nie mamy' in msg.lower(), 'unknown item refused')
    check(cash == 10_000_000, 'unknown item charges nothing')

    # 4. alias/group path: balls_buy handler + pokemon prefix routing
    pcog = Pokemon.__new__(Pokemon)
    pcog.bot = MagicMock()
    shop.bot.get_cog = MagicMock(return_value=pcog)
    set_cash(99, 7, 10_000_000)
    bctx = mkctx('ultra', '2', ';')
    await Pokemon.balls_buy.callback(pcog, bctx, 'ultra', 2)
    spent = 10_000_000 - bal(99, 7)['cash']
    check(spent == 2 * POKE_SHOP_PRICES['ultra'].amount,
          'balls_buy charges catalog price x qty')
    set_cash(99, 7, 10_000_000)
    msg, cash = await buy('8', '1', ';')
    check(cash == 10_000_000 - POKE_SHOP_PRICES['candy'].amount,
          'pokemon id routing charges catalog price')

    # 5. pokemon shop output shows catalog prices (filtered section view;
    # the default overview lists categories, never item rows, by design)
    view = pcog._balls_layout(99, 7, 100, filt='Balls', owner_name='t')
    blob = ' '.join(c.content for c in view.walk_children()
                    if type(c).__name__ == 'TextDisplay')
    check('1,500' in blob and '50,000' in blob, 'pokemon shop renders catalog prices')
    view0 = pcog._balls_layout(99, 7, 100, owner_name='t')
    blob0 = ' '.join(c.content for c in view0.walk_children()
                     if type(c).__name__ == 'TextDisplay')
    check('[1-4]' in blob0 and '1,500' not in blob0, 'overview stays item-free')

    print('FAILURES: %d' % len(FAILS), flush=True)
    sys.exit(1 if FAILS else 0)


asyncio.run(main())

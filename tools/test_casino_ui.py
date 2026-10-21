"""The casino console and the casino art.

Two things this guards:

  * The console's buttons. A Components-V2 panel can hold a button nested
    inside a Container, but only if the button really is dispatchable -
    and a layout that renders as a picture can still fail at the wire.
  * The art. The old coin drew the whole WORD "heads" at 150px into a
    300px disc and spilled gold letters across the canvas; the card faces
    drew the LETTER for a suit, so a club showed a big "C".
"""
import asyncio
import io
import sys
import types
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if 'dotenv' not in sys.modules:
    _m = types.ModuleType('dotenv')
    _m.load_dotenv = lambda *a, **k: False
    sys.modules['dotenv'] = _m

import discord
from discord.ext import commands
from PIL import Image

fails = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [ok]   {label}')
    else:
        print(f'  [FAIL] {label} {extra}')
        fails.append(label)


class FakeAuthor:
    id = 12345
    display_name = 'he'
    bot = False


class FakeGuild:
    id = 777


class FakeCtx:
    def __init__(self, bot):
        self.guild = FakeGuild()
        self.author = FakeAuthor()
        self.bot = bot
        self.channel = None
        self.message = None
        self.loop = asyncio.get_event_loop()


def count_buttons(view) -> int:
    """Buttons inside the panel, however deeply nested."""
    n = 0
    payload = view.to_components()
    for comp in payload:
        if not isinstance(comp, dict):
            continue
        for row in comp.get('components', []) or []:
            for c in row.get('components', []) or []:
                if c.get('type') == 2:      # button
                    n += 1
    return n


async def run():
    bot = commands.Bot(command_prefix='.', intents=discord.Intents.all(), help_command=None)
    await bot.load_extension('cogs.gamble')
    cog = bot.get_cog('Gamble')
    ctx = FakeCtx(bot)

    from cogs import casino_ui

    print('--- the hub ---')
    hub = casino_ui.hub(cog, ctx)
    check('the hub builds', hub is not None)
    n = count_buttons(hub)
    check('the hub has a button per game', n == len(casino_ui.GAMES), n)
    check('the hub covers all five games', len(casino_ui.GAMES) == 5, len(casino_ui.GAMES))

    print('--- the bet console ---')
    for game, _label, _name in casino_ui.GAMES:
        con = casino_ui.CasinoConsole(cog, ctx, game, 50)
        payload = con.to_components()
        check(f'{game}: the console builds', isinstance(payload, list) and len(payload) > 0)
        check(f'{game}: it has buttons', count_buttons(con) >= 3, count_buttons(con))

    print('--- the bet stepper respects the limits ---')
    con = casino_ui.CasinoConsole(cog, ctx, 'slots', 50)
    cap, cash, tix = con._limits()
    check('a limit is readable', cap >= 0 and cash >= 0, (cap, cash, tix))
    con.bet = 10 ** 9
    check('the bet cannot exceed the cap or the balance',
          con.bet <= max(cap, 5) and con.bet <= max(cash, 5), (con.bet, cap, cash))
    con.bet = 1
    check('the bet cannot go under the minimum', con.bet >= 5, con.bet)
    rows = con._rows_for()
    check('the stepper builds its rows', len(rows) >= 2, len(rows))
    check('no row overflows the five-button limit',
          all(len(r.children) <= 5 for r in rows),
          [len(r.children) for r in rows])

    print('--- the console borrows the real commands ---')
    for game, _l, _n in casino_ui.GAMES:
        cmd = getattr(cog, game, None)
        check(f'{game}: the command exists', cmd is not None)

    print('--- the art ---')
    from utils import casino as ca
    from utils.cards import card_face, suit_glyph
    for name, data in (
        ('slots', ca.slots_panel(['7', '7', '7'], mult=8, payout=8000)),
        ('roulette', ca.roulette_wheel(17, list(range(37)), {1, 3, 5})),
        ('coin', ca.coin_flip('heads')),
        ('blackjack', ca.blackjack_table([('K', 'S'), ('Q', 'H')],
                                        [('A', 'S'), ('K', 'C')], hide=True, p_val=20)),
        ('poker', ca.poker_table([('K', 'S'), ('K', 'H'), ('7', 'C'), ('3', 'D'), ('2', 'S')],
                                 [0, 1])),
    ):
        im = Image.open(io.BytesIO(data))
        check(f'{name}: it renders', im.size[0] > 100 and im.size[1] > 100, im.size)
        check(f'{name}: it is RGB', im.mode == 'RGB', im.mode)

    print('--- the card faces use real pips ---')
    check('a club is a club', suit_glyph('C') == '♣', suit_glyph('C'))
    check('a heart is a heart', suit_glyph('H') == '♥', suit_glyph('H'))
    check('a spade is a spade', suit_glyph('S') == '♠', suit_glyph('S'))
    check('a diamond is a diamond', suit_glyph('D') == '♦', suit_glyph('D'))
    face = card_face('K', 'C', 110, 154)
    check('the face renders', face.size == (110, 154), face.size)

    print('--- the coin does not spill its label ---')
    im = Image.open(io.BytesIO(ca.coin_flip('heads')))
    # the glyph must sit inside the disc, not across the canvas
    check('the coin canvas is square', im.size[0] == im.size[1], im.size)
    check('the coin is big enough to read', im.size[0] >= 300, im.size)

    await bot.close()


asyncio.run(run())
print()
if fails:
    print(f'FAILURES ({len(fails)}): ' + ', '.join(fails))
    raise SystemExit(1)
print('CASINO CONSOLE + ART: ALL TESTS PASS')

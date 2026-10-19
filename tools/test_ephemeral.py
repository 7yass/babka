"""Does `ephemeral=True` now do something in a prefix command?

Before: discord.py dropped the flag whenever the context had no
interaction, so every "private" reply in the bot was a PUBLIC message
that stayed up forever.

This drives the real BabkaContext against a fake message and asserts the
four paths.
"""
import asyncio
import sys
import types
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import discord
from discord.ext import commands

# dotenv is not installed in this environment; main.py only needs the
# symbol to exist.
if 'dotenv' not in sys.modules:
    _stub = types.ModuleType('dotenv')
    _stub.load_dotenv = lambda *a, **k: False
    sys.modules['dotenv'] = _stub

import main as botmain

fails = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [ok]   {label}')
    else:
        print(f'  [FAIL] {label} {extra}')
        fails.append(label)


class FakeState:
    def __init__(self):
        self.http = types.SimpleNamespace(token='x')
        self.user = types.SimpleNamespace(id=1)
        self.guilds = {}
        self.private_channels = {}
        self.voice_clients = {}
        self.max_messages = 1000
        self._get_private_channel_by_name = lambda *a, **k: None
        self._get_guild = lambda *a, **k: None


class FakeMsg:
    def __init__(self, guild=True):
        self._state = FakeState()
        self.guild = types.SimpleNamespace(id=1) if guild else None
        self.channel = types.SimpleNamespace(id=2, type=discord.ChannelType.text,
                                             _state=self._state, guild=self.guild)
        self.author = types.SimpleNamespace(id=3, bot=False, display_name='he')
        self.content = '.bal'
        self.id = 99
        self.mentions = []
        self.reactions = []


class FakeBot(commands.Bot):
    """A bot whose send path records the kwargs it was given."""

    def __init__(self):
        super().__init__(command_prefix='.', intents=discord.Intents.all(), help_command=None)
        self.calls = []

    async def get_context(self, origin, /, *, cls=botmain.BabkaContext):
        return await super().get_context(origin, cls=cls)


async def run():
    bot = FakeBot()
    # the bot is not logged in, so give it an identity
    try:
        bot._connection.user = types.SimpleNamespace(id=1)
    except Exception:
        pass

    # patch the underlying send so nothing hits the network
    async def fake_send(self, content=None, **kwargs):
        bot.calls.append(kwargs)
        return types.SimpleNamespace(kwargs=kwargs, delete=lambda **k: None)

    discord.abc.Messageable.send = fake_send

    ctx = await bot.get_context(FakeMsg(guild=True))
    check('context is a BabkaContext', isinstance(ctx, botmain.BabkaContext), type(ctx))

    # 1. plain ephemeral in a prefix command -> auto-deleted
    bot.calls.clear()
    await ctx.send('secret', ephemeral=True)
    kw = bot.calls[-1] if bot.calls else {}
    check('prefix ephemeral gets a delete_after',
          'delete_after' in kw, kw)
    check('delete_after is the short TTL',
          kw.get('delete_after') == botmain.EPHEMERAL_TTL, kw.get('delete_after'))
    check('mentions are suppressed in the visible window',
          isinstance(kw.get('allowed_mentions'), discord.AllowedMentions), kw.get('allowed_mentions'))
    check('no mention of the author', not kw.get('mention_author', False), kw.get('mention_author'))

    # 2. ephemeral WITH a view -> long TTL so the buttons survive
    bot.calls.clear()
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='x', custom_id='x'))
    await ctx.send('panel', view=view, ephemeral=True)
    kw = bot.calls[-1] if bot.calls else {}
    check('view reply uses the long TTL',
          kw.get('delete_after') == botmain.EPHEMERAL_VIEW_TTL, kw.get('delete_after'))

    # 3. a caller that sets its own delete_after is respected
    bot.calls.clear()
    await ctx.send('x', ephemeral=True, delete_after=7)
    kw = bot.calls[-1] if bot.calls else {}
    check('caller delete_after wins', kw.get('delete_after') == 7, kw.get('delete_after'))

    # 4. NON-ephemeral replies are untouched (delete_after stays None)
    bot.calls.clear()
    await ctx.send('public thing')
    kw = bot.calls[-1] if bot.calls else {}
    check('plain reply is not auto-deleted', not kw.get('delete_after'), kw.get('delete_after'))

    # 5. a DM is already private -> left alone
    bot.calls.clear()
    dmctx = await bot.get_context(FakeMsg(guild=False))
    await dmctx.send('dm secret', ephemeral=True)
    kw = bot.calls[-1] if bot.calls else {}
    check('DM reply is not auto-deleted', not kw.get('delete_after'), kw.get('delete_after'))

    # 6. an interaction context takes the native path (truly ephemeral)
    recorded = {}

    async def spy_send(self, content=None, **kwargs):
        recorded.update(kwargs)
        return types.SimpleNamespace(kwargs=kwargs)

    orig_send = commands.Context.send
    commands.Context.send = spy_send
    try:
        ix = types.SimpleNamespace()
        ix.is_expired = lambda: False
        ctx.interaction = ix
        await ctx.send('slash secret', ephemeral=True)
    finally:
        commands.Context.send = orig_send
        ctx.interaction = None
    check('interaction path passes ephemeral=True through',
          recorded.get('ephemeral') is True, recorded)
    check('interaction path is NOT auto-deleted',
          'delete_after' not in recorded, recorded)

    await bot.close()


asyncio.run(run())
print()
if fails:
    print(f'FAILURES ({len(fails)}): ' + ', '.join(fails))
    raise SystemExit(1)
print('EPHEMERAL BEHAVIOUR: ALL TESTS PASS')

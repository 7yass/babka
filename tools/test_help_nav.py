"""Help navigation acceptance: front door, categories, start page, targets.

Run: python tools/test_help_nav.py (needs discord installed).
Exit code 0 only when every test passes. tmp DB for the Polish pass.
"""
import asyncio
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))  # tools/ is sys.path[0] when run as a script

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    safe = msg.encode('ascii', 'backslashreplace').decode()
    print(('PASS ' if cond else 'FAIL ') + safe, flush=True)
    if not cond:
        FAILS.append(msg)


def body_text(layout) -> str:
    return ' '.join(getattr(c, 'content', '') for c in layout.walk_children()
                    if type(c).__name__ == 'TextDisplay')


async def load_bot():
    import discord
    from discord.ext import commands
    intents = discord.Intents.all()
    bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)
    src = open(ROOT / 'main.py', encoding='utf-8').read()
    cogs = re.findall(r"'([^']+)'", src.split('COGS = [', 1)[1].split(']', 1)[0])
    for c in cogs:
        await bot.load_extension(c)
    return bot


async def main() -> None:
    import database as db
    tmp = Path(tempfile.mkdtemp()) / 'test.db'
    db.DB_PATH = tmp
    db.init_db()
    from lang import set_lang
    set_lang(1, 'pl')

    from cogs.help import (HELP_GROUPS, resolve_command, nav_layout,
                           nav_front_layout, start_layout, command_layout)
    bot = await load_bot()

    # 1. .help and .start exist
    check(bot.get_command('help') is not None, '.help exists')
    check(bot.get_command('start') is not None, '.start exists')

    # alias map for the alias-target test
    alias_owners = {}
    for _q, _c in [(_c.name, _c) for _c in bot.commands]:
        for _a in (_c.aliases or []):
            alias_owners.setdefault(str(_a).lower(), set()).add(_c.name)

    # 2-5. every group: renders, primaries canonical, related resolve, no alias targets
    for key, grp in HELP_GROUPS.items():
        for gid, lang in ((0, 'en'), (1, 'pl')):
            try:
                lay = nav_layout(bot, gid, key, '.')
                txt = body_text(lay)
                assert txt.strip(), 'empty'
            except Exception as e:
                check(False, '%s renders %s (%r)' % (key, lang, e))
            else:
                check(True, '%s renders %s' % (key, lang))
        for dotted in grp['primary']:
            node = resolve_command(bot, dotted)
            check(node is not None, 'primary resolves: %s' % dotted)
            first = dotted.split()[0].lower()
            owners = alias_owners.get(first, set())
            check(not owners or first in {c.name.lower() for c in [node]},
                  'primary not an alias shadow: %s' % dotted)
        for rel in grp['related']:
            check(rel in HELP_GROUPS, 'related page exists: %s -> %s' % (key, rel))

    # 6. front door + start, both languages
    for gid, lang in ((0, 'en'), (1, 'pl')):
        front = body_text(nav_front_layout(bot, gid, '.'))
        check(all(k in HELP_GROUPS for k in HELP_GROUPS)
              and len(front) > 100, 'front door renders %s' % lang)
        start = body_text(start_layout(gid, 'Babka'))
        check('.daily' in start and ';p' in start and '.profile' in start,
              'start page renders %s' % lang)
    pl_front = body_text(nav_front_layout(bot, 1, '.'))
    check('Wybierz system' in pl_front, 'front door is Polish when configured')

    # 7. unknown category -> guidance (no crash, nope embed sent)
    from cogs.help import Help
    cog = bot.get_cog('Help')
    ctx = MagicMock()
    ctx.guild.id = 0
    ctx.author.id = 5
    ctx.reply = AsyncMock()
    await Help.help_cmd.callback(cog, ctx, query='frobnicate')
    assert ctx.reply.call_count == 1
    _, kw = ctx.reply.call_args
    check(kw.get('embed') is not None, 'unknown category returns guidance')

    # 8. legacy aliases still functional
    check(bot.get_command('h') is not None, "alias 'h' works")
    check(bot.get_command('pshop') is not None, "alias 'pshop' works")
    check(bot.get_command('dep') is not None, "alias 'dep' works")

    # 9. existing detail behavior unchanged
    _author = MagicMock()
    _author.name = 'tester'
    det = body_text(command_layout(0, _author, 'shop', '.'))
    check('shop' in det.lower(), 'command detail still renders')

    print('FAILURES: %d' % len(FAILS), flush=True)
    raise SystemExit(1 if FAILS else 0)


asyncio.run(main())

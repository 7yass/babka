"""Load EVERY cog exactly the way main.py does, and report failures.

This is the check that matters before a deploy. A cog that fails to load
takes all of its commands with it, and the bot says so in the boot log -
but only if anyone reads it. This makes it loud and exit-coded.

It also catches the alias trap: discord.py raises CommandRegistrationError
when a command's NAME or ALIAS is already claimed, and one bad alias can
take down a 48-command cog.

Run:  py tools/load_check.py
Exits non-zero if any cog fails to load.
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import discord
from discord.ext import commands


def cog_list():
    """The COGS list from main.py, read from the source so the two cannot
    drift apart."""
    import ast
    src = (REPO / 'main.py').read_text(encoding='utf-8-sig')
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'COGS' for t in node.targets):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


async def main():
    cogs = cog_list()
    bot = commands.Bot(command_prefix='.', intents=discord.Intents.all(), help_command=None)
    # main.py loads cogs inside 'async with bot:', which logs in first and
    # therefore has an event loop available. A cog that touches bot.loop at
    # load time (cogs/commitlog does) needs the same here or it looks broken.
    bot.loop = asyncio.get_running_loop()
    failed = []
    loaded = []
    for cog in cogs:
        try:
            await bot.load_extension(cog)
            loaded.append(cog)
        except Exception as e:
            failed.append((cog, f'{type(e).__name__}: {e}'))

    # A subcommand's bare name ('buy' in '.shop buy' and '.stock buy') is
    # NOT global - only the qualified name is. Top-level names and aliases
    # ARE global, and that is where a clash actually kills a cog.
    claimed = {}
    clashes = []
    for cmd in bot.walk_commands():
        name = str(getattr(cmd, 'name', '') or '')
        if not name:
            continue
        owner = cmd.cog.qualified_name if cmd.cog else '?'
        top = getattr(cmd, 'parent', None) is None
        keys = []
        if top:
            keys.append((f'.{name}', 'name'))
            keys += [(f'.{a}', 'alias') for a in (getattr(cmd, 'aliases', None) or [])]
        else:
            q = str(getattr(cmd, 'qualified_name', '') or name)
            keys.append((q, 'subcommand'))
        for key, kind in keys:
            k = key.lower()
            if k in claimed and claimed[k][0] != owner:
                clashes.append((k, claimed[k][0], owner, kind))
            claimed.setdefault(k, (owner, kind))

    print(f'cogs listed:  {len(cogs)}')
    print(f'cogs loaded:  {len(loaded)}')
    print(f'cogs failed:  {len(failed)}')
    for c, e in failed:
        print(f'  [-] {c}: {e}')
    print(f'commands:     {len(list(bot.walk_commands()))}')

    if clashes:
        print('\nNAME/ALIAS SHADOWING (first claim wins, the other is dead):')
        for k, a, b, kind in sorted(clashes):
            print(f'  .{k}: {a} claims the {kind}, {b} is shadowed')
    else:
        print('shadowing:    none')

    await bot.close()
    if failed:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

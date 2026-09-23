import asyncio
import os
import sys
import time

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db

# Hosted panels pipe stdout, and block-buffered pipes get flushed in bursts:
# the panel then stamps every line with the flush time, so minutes of output
# look like they happened in one second. Line-buffer so timestamps are real.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', 0) or 0)


def dynamic_prefix(bot, message):
    """Guild prefix + ';' for Pokemon. Gated in before_invoke."""
    if not message.guild:
        return ('.', ';')
    try:
        return (db.get_prefix(message.guild.id), ';')
    except Exception:
        return ('.', ';')


intents = discord.Intents.all()
bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

COGS = [
    'cogs.levels',
    'cogs.moderation',
    'cogs.antiraid',
    'cogs.automod',
    'cogs.voice',
    'cogs.tiktok',
    'cogs.info',
    'cogs.rules',
    'cogs.roles',
    'cogs.welcome',
    'cogs.setup',
    'cogs.language',
    'cogs.help',
    'cogs.extramod',
    'cogs.afk',
    'cogs.lookups',
    'cogs.gamble',
    'cogs.embed',
    'cogs.giveaway',
    'cogs.polls',
    'cogs.reactroles',
    'cogs.tickets',
    'cogs.verify',
    'cogs.whitelist',
    'cogs.babka',
    'cogs.fitcheck',
    'cogs.counting',
    'cogs.wordle',
    'cogs.langroles',
    'cogs.serverstats',
    'cogs.youtube',
    'cogs.activity',
    'cogs.jobs',
    'cogs.suggest',
    'cogs.shop',
    'cogs.crime',
    'cogs.profile',
    'cogs.achievements',
    'cogs.stocks',
    'cogs.valorant',
    'cogs.pokemon',
    'cogs.emojis',
    'cogs.ping',
    'cogs.health',
    'cogs.commitlog',
]


@bot.before_invoke
async def _pre_invoke(ctx):
    # NOTE: discord.py keeps a SINGLE before_invoke hook — everything lives here.
    try:
        g = getattr(getattr(ctx, 'message', None), 'guild', None) or getattr(ctx, 'guild', None)
        au = getattr(ctx, 'author', None)
        # Holder servers only store emojis: commands run on main or for house.
        # CheckFailure is silent (see on_command_error) — hint first so the
        # user never sees plain nothing.
        if g is not None and not db.is_main_guild(getattr(g, 'id', 0)) \
                and not db.is_house(getattr(au, 'id', 0)):
            try:
                await ctx.reply('Commands run on the main server.', ephemeral=True,
                                delete_after=10)
            except Exception:
                pass
            raise commands.CheckFailure()
    except commands.CheckFailure:
        raise
    except Exception:
        pass
    try:
        from lang import set_ctx_lang
        set_ctx_lang(getattr(ctx, 'author', None))
    except Exception:
        pass
    try:
        cog = getattr(getattr(ctx, 'cog', None), 'qualified_name', '')
        prefix = ctx.prefix or ''
        is_pk = cog == 'Pokemon' or (cog == 'Help' and prefix == ';')
        # `shop`/`inv` are shared: economy store/bag on the guild prefix,
        # pokemon storefront/bag on ';' (PokeMeow-style). Let the Shop cog's
        # trees through on ';' — their callbacks branch on the prefix itself.
        # NOTE: for `;shop buy ...` invoked_with is 'buy', so match the
        # whole invocation chain, not just the leaf.
        invoked = [(ctx.invoked_with or '').lower()] + \
            [str(p or '').lower() for p in (getattr(ctx, 'invoked_parents', None) or [])]
        if cog == 'Shop' and prefix == ';' and ('shop' in invoked or 'inv' in invoked):
            is_pk = True
        if prefix == ';' and not is_pk:
            try:
                gp = db.get_prefix(getattr(getattr(ctx, 'guild', None), 'id', 0)) or '.'
                await ctx.reply(f'That lives on `{gp}` — only pokemon after `;`.',
                                ephemeral=True, delete_after=10)
            except Exception:
                pass
            raise commands.CheckFailure()
        if (ctx.prefix or '') != ';' and cog == 'Pokemon':
            try:
                await ctx.reply('Pokémon moved to `;` — try `;hunt ...`', delete_after=10)
            except Exception:
                pass
            raise commands.CheckFailure()
    except commands.CheckFailure:
        raise
    except Exception:
        pass


@bot.after_invoke
async def _unpin_user_lang(ctx):
    try:
        from lang import reset_ctx_lang
        reset_ctx_lang()
    except Exception:
        pass


@tasks.loop(hours=24)
async def _daily_backup():
    try:
        import datetime as _dt
        from pathlib import Path as _P
        _P('backups').mkdir(exist_ok=True)
        try:
            pruned = db.prune()
        except Exception:
            pruned = {}
        stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        db.backup_to(str(_P('backups') / f'data-{stamp}.db'))
        snaps = sorted(_P('backups').glob('data-*.db'))
        for old in snaps[:-7]:
            try:
                old.unlink()
            except Exception:
                pass
        print(f'[+] DB backup done ({len(snaps[:7])} kept, pruned {pruned})')
    except Exception as e:
        print(f'[-] DB backup failed: {e}')


# READY + loop-lag bookkeeping (module scope: on_ready and the watchdog read it).
_READY = {'n': 0, 'first': 0.0, 'synced': False, 'warned': False}
_LAG = {'last_warn': 0.0}


@bot.event
async def on_ready():
    import time as _t
    _READY['n'] += 1
    _READY['first'] = _READY['first'] or _t.time()
    print(f'[+] Babka Danka: {bot.user} ({bot.user.id}) · ready #{_READY["n"]}')
    # Slash sync is a REST PUT per guild. Doing it on EVERY ready turned a flaky
    # gateway into a rate-limit hammer (log: 1486 syncs in 80 minutes), so sync
    # once per process — retry on the next ready only if it actually failed.
    if not _READY['synced']:
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                print(f'[+] Slash synced to guild {GUILD_ID}')
            else:
                await bot.tree.sync()
                print('[+] Slash synced globally')
            _READY['synced'] = True
        except Exception as e:
            print(f'[-] Sync failed: {e}')
    if _READY['n'] == 1:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name='Jestem prawdziwą babcią Matrofa osły'))
    # on_ready fires again whenever the session could NOT be resumed. Once or
    # twice after a blip is normal; a sustained storm is not — the usual cause
    # is a second process on the same token (panel + local run), because each
    # login invalidates the other's session. That is also what kills
    # interactions (404 code 10062) and drops whole commands.
    if _READY['n'] > 6 and not _READY['warned'] and _t.time() - _READY['first'] < 900:
        _READY['warned'] = True
        print('[!] READY fired %d times in %ds — the gateway session keeps being '
              'invalidated. Check for a SECOND bot instance on the same token '
              '(panel + your PC); kill one or commands keep dying with '
              '"Unknown interaction".' % (_READY['n'], int(_t.time() - _READY['first'])))


@_daily_backup.before_loop
async def _before_backup():
    # The loop body runs immediately at startup by default. A full DB copy
    # on a slow shared disk stalls the gateway handshake, so let the bot
    # get online first.
    await asyncio.sleep(180)


@tasks.loop(seconds=15)
async def _loop_lag():
    """Process-freeze watchdog. Every PIL render already runs in an executor and
    sqlite calls are sub-millisecond, so when the loop misses its schedule by
    seconds it means no code ran at all: the process lost CPU. That's when
    heartbeats die (session invalidated -> reconnect storm) and interactions
    expire (10062). One line per minute at most."""
    import time as _t
    expected = _t.monotonic() + 15
    await asyncio.sleep(15)
    lag = _t.monotonic() - expected
    if lag > 1.5 and _t.time() - _LAG['last_warn'] > 60:
        _LAG['last_warn'] = _t.time()
        # PIL renders already run in executors and sqlite calls are sub-ms, so
        # a multi-second stall means the PROCESS got no CPU — host throttling/
        # freeze, or the machine sleeping if this runs locally. While frozen,
        # gateway heartbeats die (session invalidated -> reconnect storm) and
        # every interaction expires (404 code 10062).
        print(f'[!] no code ran for {lag:.1f}s — the process was frozen. On a '
              f'shared host that is CPU throttling; locally it is the PC '
              f'sleeping. Reconnect storms and "Unknown interaction" follow.')


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Users keep typing the legacy PokeMeow/Node names that helpmeta still
        # lists as aliases. Pure silence reads as "bot is dead"; a hint only
        # fires for names helpmeta actually knows, everything else stays quiet.
        try:
            from cogs.help import suggest_command
            gid = ctx.guild.id if ctx.guild else None
            word = str(getattr(ctx, 'invoked_with', '') or '').split()[0]
            hint = suggest_command(gid, word, ctx.prefix or '.', ctx.bot)
            if hint:
                await ctx.reply(hint, delete_after=12)
        except Exception:
            pass
        return
    if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
        return  # checks.py already replied
    if isinstance(error, commands.CommandOnCooldown):
        return
    # Unwrap CommandInvokeError -> original HTTPException (e.g. oversized
    # components, deleted invoking message). Never log-spam these; tell
    # the user instead of showing nothing.
    orig = getattr(error, 'original', None)
    for e in (error, orig):
        if isinstance(e, discord.HTTPException) and getattr(e, 'code', None) == 50035:
            try:
                await ctx.reply('Too big to display — try a category view.', ephemeral=True)
            except Exception:
                pass
            return
    if isinstance(error, (commands.MissingRequiredArgument,
                          commands.BadArgument,
                          commands.UserInputError)):
        # Never leave the user with nothing: show invocation usage.
        # (The command body never ran, so no cog could reply inline.)
        try:
            name = getattr(getattr(ctx, 'command', None), 'qualified_name', None) or 'command'
            sig = f' {getattr(ctx.command, "signature", "")}' if getattr(ctx, 'command', None) else ''
            await ctx.reply(f'Usage: `{ctx.prefix}{name}{sig}`',
                            ephemeral=True, delete_after=15)
        except Exception:
            pass
        return  # keep logs clean
    print(f'[!] Command error: {ctx.command} in #{ctx.channel} by {ctx.author}: {error}')


def _banner() -> str:
    try:
        from pathlib import Path
        art = (Path(__file__).parent / 'assets' / 'reaper.txt').read_text(encoding='utf-8')
        return art + '\n  B A B K A   D A N K A   |   rosol jest juz na gazie'
    except Exception:
        return 'BABKA DANKA'


BAR_W = 30


def _bar(done: int, total: int) -> str:
    f = int(BAR_W * done / max(1, total))
    return '█' * f + '░' * (BAR_W - f)


async def main():
    db.init_db()
    bot.boot_at = time.time()
    print(_banner())
    import logging as _lg
    try:
        # Slow-callback tripwire: asyncio logs any callback hogging the loop
        # longer than this. The watchdog only says the loop starved; this
        # names the culprit (e.g. a sync DB call on slow shared storage).
        bot.loop.slow_callback_duration = 2.0
        _h = _lg.StreamHandler(sys.stdout)
        _h.setFormatter(_lg.Formatter('[asyncio] %(message)s'))
        _alog = _lg.getLogger('asyncio')
        _alog.addHandler(_h)
        _alog.propagate = False
    except Exception:
        pass
    _daily_backup.start()
    _loop_lag.start()
    async with bot:
        failed = []
        loaded = []
        for i, cog in enumerate(COGS, start=1):
            try:
                await bot.load_extension(cog)
                loaded.append(cog)
                print(f'\r  [{_bar(i, len(COGS))}] {i}/{len(COGS)} {cog}', end='', flush=True)
            except Exception as e:
                failed.append((cog, e))
        bot.cog_report = {'loaded': loaded,
                          'failed': [(c, f'{type(e).__name__}: {e}') for c, e in failed]}
        print()
        for cog, e in failed:
            print(f'  [-] Failed {cog}: {e}')
        print(f'  [+] {len(COGS) - len(failed)}/{len(COGS)} cogs online. babka :3')
        for attempt in (1, 2, 3):
            try:
                await bot.start(TOKEN)
                break
            except discord.errors.LoginFailure:
                raise
            except discord.errors.DiscordServerError as e:
                # Edge/Cloudflare blips (and flagged host IPs) 500 the login.
                # Bounded retries ride out the transient case; a sticky IP flag
                # still fails loud after 3 tries instead of crash-looping.
                print(f'  [-] login 5xx (attempt {attempt}/3): {e.status}. '
                      f'{"retrying..." if attempt < 3 else "giving up."}')
                if attempt == 3:
                    raise
                await asyncio.sleep(10 * attempt)


asyncio.run(main())

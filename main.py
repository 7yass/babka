import asyncio
import os

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db

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
    'cogs.clown',
    'cogs.ship',
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
]


@bot.before_invoke
async def _pre_invoke(ctx):
    # NOTE: discord.py keeps a SINGLE before_invoke hook — everything lives here.
    try:
        g = getattr(getattr(ctx, 'message', None), 'guild', None) or getattr(ctx, 'guild', None)
        au = getattr(ctx, 'author', None)
        # Holder servers only store emojis: commands run on main or for house.
        # CheckFailure is silent (see on_command_error).
        if g is not None and not db.is_main_guild(getattr(g, 'id', 0)) \
                and not db.is_house(getattr(au, 'id', 0)):
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
        # `shop` is shared: economy store on the guild prefix, pokemon
        # storefront on ';' (PokeMeow-style). Let the Shop cog's shop tree
        # through on ';' — its callback branches on the prefix itself.
        if cog == 'Shop' and prefix == ';' and (ctx.invoked_with or '').lower() == 'shop':
            is_pk = True
        if prefix == ';' and not is_pk:
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
        stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        db.backup_to(str(_P('backups') / f'data-{stamp}.db'))
        snaps = sorted(_P('backups').glob('data-*.db'))
        for old in snaps[:-7]:
            try:
                old.unlink()
            except Exception:
                pass
        print(f'[+] DB backup done ({len(snaps[:7])} kept)')
    except Exception as e:
        print(f'[-] DB backup failed: {e}')


@bot.event
async def on_ready():
    print(f'[+] Babka Danka: {bot.user} ({bot.user.id})')
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f'[+] Slash synced to guild {GUILD_ID}')
        else:
            await bot.tree.sync()
            print('[+] Slash synced globally')
    except Exception as e:
        print(f'[-] Sync failed: {e}')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name='Jestem prawdziwą babcią Matrofa osły'))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
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
        return  # cogs now reply with usage inline; keep logs clean
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
    print(_banner())
    _daily_backup.start()
    async with bot:
        failed = []
        for i, cog in enumerate(COGS, start=1):
            try:
                await bot.load_extension(cog)
                print(f'\r  [{_bar(i, len(COGS))}] {i}/{len(COGS)} {cog}', end='', flush=True)
            except Exception as e:
                failed.append((cog, e))
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

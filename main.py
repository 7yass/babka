import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import database as db

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', 0) or 0)


def dynamic_prefix(bot, message):
    if not message.guild:
        return '.'
    try:
        return db.get_prefix(message.guild.id)
    except Exception:
        return '.'


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
    'cogs.shop',
    'cogs.crime',
]


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
    if isinstance(error, discord.HTTPException) and getattr(error, 'code', None) == 50035:
        return  # invoking message was deleted before the reply landed
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
        await bot.start(TOKEN)


asyncio.run(main())

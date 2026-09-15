"""Babka Danka, minimal edition: short dry replies on mention. Nothing else."""
import random
import time

import discord
from discord.ext import commands

import database as db

CD = 20

LINES = [
    'Spadaj , robie zupe',
    'w czym moge ci pomóc?',
    'mój wnuk jest zajęty, wez napisz potem',
    'jeśli szukasz pomocy, to nie umnie',
    'przestań mnie pingować',
    'nie mam czasu na twoje pierdoły',
    'mam cie w dupie',
    'nie mam ochoty na rozmowe z tobą',
]


class Babka(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cd: dict = {}

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not self.bot.user or self.bot.user not in message.mentions:
            return
        if message.content.startswith(db.get_prefix(message.guild.id)):
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._cd.get(key, 0) < CD:
            return
        self._cd[key] = now
        try:
            await message.reply(random.choice(LINES), mention_author=False)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Babka(bot))

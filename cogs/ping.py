"""Ping: gateway latency + message round trip."""
import time

import discord
from discord.ext import commands

from lang import t


class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name='ping', description='Ping pong')
    async def ping(self, ctx):
        gid = ctx.guild.id if ctx.guild else 0
        gw = round((self.bot.latency or 0) * 1000)
        t0 = time.perf_counter()
        msg = await ctx.reply('🏓 Pong!')
        rt = round((time.perf_counter() - t0) * 1000)
        try:
            await msg.edit(content=t(gid, 'eco.ping_pong', gw=gw, rt=rt))
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Ping(bot))

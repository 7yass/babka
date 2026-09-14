"""Ping: gateway latency + message round trip + event-loop health."""
import asyncio
import time

import discord
from discord.ext import commands, tasks

from lang import t


class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.loop_lag_ms = 0
        try:
            if not self._lagwatch.is_running():
                self._lagwatch.start()
        except Exception:
            pass

    def cog_unload(self):
        try:
            self._lagwatch.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=5)
    async def _lagwatch(self):
        t0 = self.bot.loop.time()
        await asyncio.sleep(5)
        drift = (self.bot.loop.time() - t0 - 5) * 1000
        self.bot.loop_lag_ms = round(max(0, drift))

    @commands.hybrid_command(name='ping', description='Ping pong')
    async def ping(self, ctx):
        gid = ctx.guild.id if ctx.guild else 0
        gw = round((self.bot.latency or 0) * 1000)
        lag = getattr(self.bot, 'loop_lag_ms', 0) or 0
        t0 = time.perf_counter()
        msg = await ctx.reply('🏓 Pong!')
        rt = round((time.perf_counter() - t0) * 1000)
        try:
            await msg.edit(content=t(gid, 'eco.ping_pong', gw=gw, rt=rt, lag=lag))
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Ping(bot))

"""Exploration: travel between locations, hunt location encounters.

First consumer of the services layer: locations filter the species pool
that feeds spawn_encounter, travel state lives in meta, rendering goes
through the Pokemon cog's shared send_spawn. Catch/battle/quests resolve
exactly like ;p encounters (same store, same cooldowns).
"""
import discord
from discord.ext import commands

from lang import t


class Exploration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _pk(self):
        cog = self.bot.get_cog('Pokemon')
        if cog is None:
            raise RuntimeError('pokemon cog missing')
        return cog

    @commands.command(name='travel', description='Podróżuj między lokacjami')
    async def travel(self, ctx, *, where: str = ''):
        from services import exploration_service as _ex
        gid = ctx.guild.id
        where = (where or '').strip()
        if not where:
            cur = _ex.get_location(gid, ctx.author.id)
            lines = []
            for key, (name, blurb, _types) in _ex.LOCATIONS.items():
                mark = '📍' if key == cur else '·'
                lines.append(f'{mark} `{key}` — **{name}**: {blurb}')
            return await ctx.reply('\n'.join(lines), ephemeral=True)
        if not _ex.set_location(gid, ctx.author.id, where):
            return await ctx.reply(t(gid, 'eco.pk_travel_bad'), ephemeral=True)
        name = _ex.location_info(where)[0]
        return await ctx.reply(t(gid, 'eco.pk_traveled', name=name), ephemeral=True)

    @commands.command(name='explore', description='Poluj w swojej lokacji')
    async def explore(self, ctx):
        from services import exploration_service as _ex
        from services.encounter_service import spawn_encounter
        pk = self._pk()
        gid = ctx.guild.id
        await ctx.typing()
        loc = _ex.get_location(gid, ctx.author.id)
        pool = _ex.resolve_pool(gid, loc)
        res = await spawn_encounter(
            gid, ctx.author.id, ctx.author.display_name, 'catch',
            store=pk._enc, cooldowns=pk._hunt_cd, dex_pool=pool)
        if not res.ok:
            return await ctx.reply(res.reply_text, ephemeral=res.ephemeral)
        return await pk.send_spawn(ctx, res)


async def setup(bot):
    await bot.add_cog(Exploration(bot))

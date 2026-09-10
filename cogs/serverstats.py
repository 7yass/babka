"""Server stats: locked category on top with live member/boost counters."""
import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or


class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    @staticmethod
    def counts(guild: discord.Guild) -> tuple:
        humans = [m for m in guild.members if not m.bot]
        members = len(humans) or (guild.member_count or 0)
        return members, guild.premium_subscription_count or 0

    async def _apply(self, guild: discord.Guild):
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM stats_cfg WHERE guild_id=?', (str(guild.id),)).fetchone()
        if not row:
            return
        members, boosts = self.counts(guild)
        for key, name in (('members_ch', f'Members: {members:,}'.replace(',', ' ')),
                          ('boosts_ch', f'Boosts: {boosts}')):
            if not row[key]:
                continue
            ch = guild.get_channel(int(row[key]))
            if ch and ch.name != name:
                try:
                    await ch.edit(name=name)
                except Exception:
                    pass

    @tasks.loop(minutes=30)
    async def refresh_loop(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self._apply(guild)
            except Exception:
                continue

    @commands.hybrid_group(name='stats', description='Statystyki serwera')
    async def stats(self, ctx):
        await ctx.reply('/stats setup / refresh', ephemeral=True)

    @stats.command(name='setup', description='Stwórz kategorię statystyk')
    @staff_or('manage_guild')
    async def setup_cmd(self, ctx):
        gid = str(ctx.guild.id)
        await ctx.defer(ephemeral=True)
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT * FROM stats_cfg WHERE guild_id=?', (gid,)).fetchone()
            if old and old['category_id']:
                try:
                    await ctx.reply(t(ctx.guild.id, 'ss.exists'), ephemeral=True)
                    return
                except Exception:
                    pass
        try:
            cat = await ctx.guild.create_category('Server Stats', reason='server stats')
            await cat.move(beginning=True)
            deny = discord.PermissionOverwrite(connect=False, send_messages=False)
            members, boosts = self.counts(ctx.guild)
            mc = await ctx.guild.create_voice_channel(
                f'Members: {members:,}'.replace(',', ' '), category=cat,
                overwrites={ctx.guild.default_role: deny}, reason='server stats')
            bc = await ctx.guild.create_voice_channel(
                f'Boosts: {boosts}', category=cat,
                overwrites={ctx.guild.default_role: deny}, reason='server stats')
        except Exception:
            return await ctx.reply(t(ctx.guild.id, 'ss.fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO stats_cfg (guild_id, category_id, members_ch, boosts_ch)'
                         ' VALUES (?,?,?,?)', (gid, str(cat.id), str(mc.id), str(bc.id)))
        await ctx.reply(embed=ok(t(ctx.guild.id, 'ss.done')), ephemeral=True)

    @stats.command(name='refresh', description='Odśwież liczniki')
    @staff_or('manage_guild')
    async def refresh(self, ctx):
        await self._apply(ctx.guild)
        await ctx.reply(embed=ok(t(ctx.guild.id, 'ss.refreshed')), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await self._apply(member.guild)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            await self._apply(member.guild)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.premium_subscription_count != after.premium_subscription_count:
            try:
                await self._apply(after)
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(ServerStats(bot))

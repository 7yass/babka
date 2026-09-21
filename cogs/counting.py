"""Counting channel: one number at a time, no doubles, records + XP milestones."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or


class Counting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='counting', description='Kanał liczenia')
    async def counting(self, ctx):
        await ctx.reply('.counting set / off', ephemeral=True)

    @counting.command(name='set', description='Ustaw kanał liczenia')
    @staff_or('manage_guild')
    async def set_ch(self, ctx, channel: discord.TextChannel):
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO counting_cfg (guild_id, channel_id, current, record) VALUES (?,?,0,0)',
                         (str(ctx.guild.id), str(channel.id)))
        await ctx.reply(t(ctx.guild.id, 'ct.set', ch=channel.mention), ephemeral=True)

    @counting.command(name='off', description='Wyłącz liczenie')
    @staff_or('manage_guild')
    async def off(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM counting_cfg WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'ct.off'), ephemeral=True)

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM counting_cfg WHERE guild_id=?',
                               (str(message.guild.id),)).fetchone()
        if not cfg or str(message.channel.id) != (cfg['channel_id'] or ''):
            return
        if message.content.startswith(db.get_prefix(message.guild.id)):
            return
        try:
            num = int(message.content.strip().split()[0])
        except (ValueError, IndexError):
            try:
                await message.delete()
            except Exception:
                pass
            return
        gid = message.guild.id
        expected = (cfg['current'] or 0) + 1
        if num == expected and str(message.author.id) != (cfg['last_user'] or ''):
            with db.conn_ctx() as conn:
                conn.execute('UPDATE counting_cfg SET current=?, last_user=?, record=MAX(record, ?) WHERE guild_id=?',
                             (num, str(message.author.id), num, str(gid)))
            try:
                await message.add_reaction('✅')
            except Exception:
                pass
            if num % 100 == 0:
                from cogs.levels import add_xp
                from cogs.gamble import bal, set_cash, add_cash
                add_xp(gid, message.author.id, 250)
                b = bal(gid, message.author.id)
                add_cash(gid, message.author.id, 500)
                try:
                    await message.channel.send(
                        embed=ok(t(gid, 'ct.milestone', n=num, user=message.author.mention)))
                except Exception:
                    pass
            return
        # wrong: delete, shame, reset (keep record)
        try:
            await message.delete()
        except Exception:
            pass
        rec = cfg['record'] or 0
        with db.conn_ctx() as conn:
            conn.execute('UPDATE counting_cfg SET current=0, last_user=NULL WHERE guild_id=?', (str(gid),))
        try:
            warn = await message.channel.send(
                embed=ok(t(gid, 'ct.ruined', user=message.author.mention, want=expected,
                             got=num, record=rec)))
            await warn.delete(delay=8)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Counting(bot))

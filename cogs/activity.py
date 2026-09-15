"""Activity tracking: messages, joins, leaves. Powers the panel /activity page."""
import datetime

import discord
from discord.ext import commands

import database as db


def _today() -> str:
    return datetime.date.today().isoformat()


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        try:
            with db.conn_ctx() as conn:
                conn.execute('''INSERT INTO msg_stats (guild_id, day, user_id, count) VALUES (?,?,?,1)
                    ON CONFLICT(guild_id, day, user_id) DO UPDATE SET count=count+1''',
                             (str(message.guild.id), _today(), str(message.author.id)))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            with db.conn_ctx() as conn:
                conn.execute('''INSERT INTO day_events (guild_id, day, joins, leaves) VALUES (?,?,1,0)
                    ON CONFLICT(guild_id, day) DO UPDATE SET joins=joins+1''',
                             (str(member.guild.id), _today()))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            with db.conn_ctx() as conn:
                conn.execute('''INSERT INTO day_events (guild_id, day, joins, leaves) VALUES (?,?,0,1)
                    ON CONFLICT(guild_id, day) DO UPDATE SET leaves=leaves+1''',
                             (str(member.guild.id), _today()))
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Activity(bot))

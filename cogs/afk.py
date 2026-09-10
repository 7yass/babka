"""AFK: set reason, auto-clear on return, mention notices. Port of example bot."""
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, WHITE
from utils.checks import staff_or  # noqa: F401  (everyone may use afk)

_cache: dict = {}


def _load():
    with db.conn_ctx() as conn:
        for r in conn.execute('SELECT guild_id, user_id, reason, set_at FROM afk').fetchall():
            _cache.setdefault(int(r['guild_id']), {})[int(r['user_id'])] = (r['reason'], float(r['set_at'] or 0))


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        _load()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        gid, uid = message.guild.id, message.author.id
        prefix = db.get_prefix(gid)
        if not message.content.startswith(prefix):
            entry = _cache.get(gid, {}).get(uid)
            if entry and time.time() - entry[1] > 5:
                reason, set_at = entry
                with db.conn_ctx() as conn:
                    conn.execute('DELETE FROM afk WHERE guild_id=? AND user_id=?', (str(gid), str(uid)))
                _cache.get(gid, {}).pop(uid, None)
                label = reason if reason and reason.lower() != 'afk' else t(gid, 'afk.label')
                try:
                    await message.channel.send(
                        embed=build(t(gid, 'afk.back', user=message.author.mention, label=label,
                                              ts=f'<t:{int(set_at)}:R>'), color=WHITE),
                        allowed_mentions=discord.AllowedMentions(users=True), delete_after=30)
                except Exception:
                    pass
                return
        for mentioned in message.mentions:
            if mentioned.bot or mentioned.id == uid:
                continue
            entry = _cache.get(gid, {}).get(mentioned.id)
            if entry:
                reason, set_at = entry
                label = reason if reason and reason.lower() != 'afk' else t(gid, 'afk.label')
                try:
                    await message.channel.send(
                        embed=build(t(gid, 'afk.mentioned', user=mentioned.mention, label=label,
                                              ts=f'<t:{int(set_at)}:R>'), color=WHITE),
                        allowed_mentions=discord.AllowedMentions(users=True), delete_after=10)
                except Exception:
                    pass

    @commands.hybrid_command(name='afk', description='Zaznacz AFK (z powodem lub bez)')
    async def afk(self, ctx, *, reason: str = ''):
        gid = ctx.guild.id if ctx.guild else None
        if len(reason) > 200:
            return await ctx.reply(t(gid, 'afk.too_long'), ephemeral=True)
        stored = reason or t(gid, 'afk.default')
        ts = time.time()
        _cache.setdefault(gid, {})[ctx.author.id] = (stored, ts)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO afk (guild_id, user_id, reason, set_at) VALUES (?,?,?,?)',
                         (str(gid), str(ctx.author.id), stored, ts))
        try:
            await ctx.message.delete()
        except Exception:
            pass
        try:
            await ctx.channel.send(
                embed=build(t(gid, 'afk.set', user=ctx.author.mention, reason=stored), color=WHITE),
                allowed_mentions=discord.AllowedMentions(users=True), delete_after=30)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(AFK(bot))

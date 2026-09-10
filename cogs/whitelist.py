"""Channel whitelist: exempt channels from automod and/or level (GIF) restrictions."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or

SYSTEMS = ('automod', 'levels')


def is_exempt(guild_id, channel_id, system: str) -> bool:
    with db.conn_ctx() as conn:
        return bool(conn.execute('SELECT 1 FROM whitelist WHERE guild_id=? AND channel_id=? AND system=?',
                                 (str(guild_id), str(channel_id), system)).fetchone())


class Whitelist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name='whitelist', description='Kanały wolne od limitów')
    @staff_or('manage_guild')
    async def whitelist(self, ctx):
        await ctx.reply('/whitelist add / remove / list', ephemeral=True)

    @whitelist.command(name='add', description='Zwolnij kanał (automod / levels / all)')
    @staff_or('manage_guild')
    async def add(self, ctx, channel: discord.TextChannel, system: str = 'all'):
        gid = ctx.guild.id
        system = (system or 'all').lower()
        targets = list(SYSTEMS) if system == 'all' else ([system] if system in SYSTEMS else [])
        if not targets:
            return await ctx.reply(t(gid, 'wl.bad_sys'), ephemeral=True)
        with db.conn_ctx() as conn:
            for s in targets:
                conn.execute('INSERT OR IGNORE INTO whitelist (guild_id, channel_id, system) VALUES (?,?,?)',
                             (str(gid), str(channel.id), s))
        await ctx.reply(t(gid, 'wl.added', ch=channel.mention, sys=', '.join(targets)), ephemeral=True)

    @whitelist.command(name='remove', description='Cofnij zwolnienie kanału')
    @staff_or('manage_guild')
    async def remove(self, ctx, channel: discord.TextChannel, system: str = 'all'):
        gid = ctx.guild.id
        system = (system or 'all').lower()
        with db.conn_ctx() as conn:
            if system == 'all':
                conn.execute('DELETE FROM whitelist WHERE guild_id=? AND channel_id=?',
                             (str(gid), str(channel.id)))
            elif system in SYSTEMS:
                conn.execute('DELETE FROM whitelist WHERE guild_id=? AND channel_id=? AND system=?',
                             (str(gid), str(channel.id), system))
            else:
                return await ctx.reply(t(gid, 'wl.bad_sys'), ephemeral=True)
        await ctx.reply(t(gid, 'wl.removed', ch=channel.mention), ephemeral=True)

    @whitelist.command(name='list', description='Zwolnione kanały')
    @staff_or('manage_guild')
    async def list_w(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT channel_id, system FROM whitelist WHERE guild_id=? ORDER BY channel_id',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'wl.empty'), ephemeral=True)
        by_ch: dict = {}
        for r in rows:
            by_ch.setdefault(r['channel_id'], []).append(r['system'])
        lines = [f"• <#{cid}> — {', '.join(sorted(sys))}" for cid, sys in by_ch.items()]
        await ctx.reply(embed=ok(t(gid, 'wl.title') + '\n' + '\n'.join(lines[:25])), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Whitelist(bot))

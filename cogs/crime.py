"""Underworld: crew heists, bounties, jail time."""
import asyncio
import json
import random
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok

TARGETS = {
    'bank': {'stake': 200, 'mult': 2.5},
    'kasyno': {'stake': 400, 'mult': 3.0},
    'muzeum': {'stake': 700, 'mult': 4.0},
}
JOIN_WINDOW = 60


class Crime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name='heist', description='Napad ekipÄ…')
    async def heist(self, ctx):
        await ctx.reply('/heist start / join', ephemeral=True)

    @heist.command(name='start', description='Zacznij napad (bank/kasyno/muzeum)')
    async def heist_start(self, ctx, target: str):
        from cogs.gamble import bal, set_cash
        gid = str(ctx.guild.id)
        target = (target or '').lower()
        if target not in TARGETS:
            return await ctx.reply(t(ctx.guild.id, 'crime.targets'), ephemeral=True)
        with db.conn_ctx() as conn:
            cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
            if cur and int(cur['ends_at'] or 0) > int(time.time()):
                return await ctx.reply(t(ctx.guild.id, 'crime.running'), ephemeral=True)
        stake = TARGETS[target]['stake']
        b = bal(gid, ctx.author.id)
        if b['cash'] < stake:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - stake)
        ends = int(time.time()) + JOIN_WINDOW
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO heists (guild_id, target, stake, crew, ends_at, channel_id)'
                         ' VALUES (?,?,?,?,?,?)',
                         (gid, target, stake, json.dumps([str(ctx.author.id)]), ends, str(ctx.channel.id)))
        await ctx.reply(t(gid, 'crime.open', target=target, stake=stake))
        await asyncio.sleep(JOIN_WINDOW + 2)
        await self._resolve(ctx.guild, gid)

    @heist.command(name='join', description='DoÅ‚Ä…cz do napadu')
    async def heist_join(self, ctx):
        from cogs.gamble import bal, set_cash
        gid = str(ctx.guild.id)
        with db.conn_ctx() as conn:
            cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
        if not cur or int(cur['ends_at'] or 0) < int(time.time()) + 5:
            return await ctx.reply(t(gid, 'crime.none'), ephemeral=True)
        crew = json.loads(cur['crew'] or '[]')
        if str(ctx.author.id) in crew:
            return await ctx.reply(t(gid, 'crime.in_crew'), ephemeral=True)
        if len(crew) >= 5:
            return await ctx.reply(t(gid, 'crime.full'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < cur['stake']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - cur['stake'])
        crew.append(str(ctx.author.id))
        with db.conn_ctx() as conn:
            conn.execute('UPDATE heists SET crew=? WHERE guild_id=?', (json.dumps(crew), gid))
        await ctx.reply(t(gid, 'crime.joined', n=len(crew)))

    async def _resolve(self, guild: discord.Guild, gid: str):
        from cogs.gamble import bal, set_cash
        with db.conn_ctx() as conn:
            cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
            if not cur:
                return
            conn.execute('DELETE FROM heists WHERE guild_id=?', (gid,))
        crew = json.loads(cur['crew'] or '[]')
        if len(crew) < 2:
            for uid in crew:
                b = bal(gid, uid)
                set_cash(gid, uid, b['cash'] + cur['stake'])
            ch = guild.get_channel(int(cur['channel_id'])) if cur['channel_id'] else None
            if ch:
                try:
                    await ch.send(embed=ok(t(guild.id, 'crime.solo', stake=cur['stake'])))
                except Exception:
                    pass
            return
        chance = 0.35 + 0.1 * len(crew)
        pot = int(cur['stake'] * len(crew) * TARGETS[cur['target']]['mult'])
        ch = guild.get_channel(int(cur['channel_id'])) if cur['channel_id'] else None
        if random.random() < chance:
            share = pot // len(crew)
            for uid in crew:
                b = bal(gid, uid)
                set_cash(gid, uid, b['cash'] + share)
            msg = t(guild.id, 'crime.win', pot=pot, share=share,
                    crew=', '.join(f'<@{u}>' for u in crew))
        else:
            for uid in crew:
                db.jail(gid, uid, 15)
            msg = t(guild.id, 'crime.fail', crew=', '.join(f'<@{u}>' for u in crew))
        if ch:
            try:
                await ch.send(embed=ok(msg))
            except Exception:
                pass

    @commands.command(name='bounty', description='Nagroda za gÅ‚owÄ™')
    async def bounty(self, ctx, member: discord.Member, amount: int):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'crime.bounty_no'), ephemeral=True)
        if amount < 100:
            return await ctx.reply(t(gid, 'crime.bounty_min'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - amount)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO bounties (guild_id, target_id, amount, by_id) VALUES (?,?,?,?)',
                         (str(gid), str(member.id), amount, str(ctx.author.id)))
        await ctx.reply(t(gid, 'crime.bounty_set', user=member.display_name, amount=amount))

    @commands.command(name='bounties', description='Lista nagrÃ³d')
    async def bounties(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT target_id, SUM(amount) a FROM bounties WHERE guild_id=? GROUP BY target_id',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'crime.bounty_empty'), ephemeral=True)
        lines = []
        for r in rows:
            m = ctx.guild.get_member(int(r['target_id']))
            lines.append(f"â€¢ {(m.display_name if m else '?')} â€” **{r['a']}**")
        await ctx.reply(embed=ok('\n'.join(lines)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Crime(bot))

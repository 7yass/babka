"""Underworld: crew heists, bounties, jail time."""
import asyncio
import json
import random
import time

import discord
from discord.ext import commands

import database as db
from utils.cards import short as cshort
from utils.economy import CRIME_BOUNTY_MIN, CRIME_HEIST_TARGETS
from lang import t
from utils.embeds import ok

JOIN_WINDOW = 60  # seconds: join window. Duration, not money — stays local.


class Crime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._open_msg = {}  # gid -> (channel_id, message_id) of the join opener

    @commands.group(name='heist', description='Napad ekipą: bank/kasyno/muzeum')
    async def heist(self, ctx):
        await ctx.reply('.heist start <bank|kasyno|muzeum> — crew joins with `.heist join` (2–5 people, 60s window)',
                        ephemeral=True)

    @heist.command(name='start', description='Zacznij napad (bank/kasyno/muzeum)')
    async def heist_start(self, ctx, target: str):
        from cogs.gamble import bal, take_cash, set_cash
        gid = str(ctx.guild.id)
        target = (target or '').lower()
        if target not in CRIME_HEIST_TARGETS:
            return await ctx.reply(t(ctx.guild.id, 'crime.targets'), ephemeral=True)
        stake = CRIME_HEIST_TARGETS[target].stake
        ends = int(time.time()) + JOIN_WINDOW
        crew0 = json.dumps([str(ctx.author.id)])
        # Atomic slot claim: a racing second `.heist start` loses the
        # INSERT instead of REPLACE-wiping the first crew (stakes burned).
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM heists WHERE guild_id=? AND ends_at<=?',
                         (gid, int(time.time())))
            cur = conn.execute(
                'INSERT INTO heists (guild_id, target, stake, crew, ends_at, channel_id)'
                ' SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM heists WHERE guild_id=?)',
                (gid, target, stake, crew0, ends, str(ctx.channel.id), gid))
            if (cur.rowcount or 0) != 1:
                return await ctx.reply(t(ctx.guild.id, 'crime.running'), ephemeral=True)
        if not take_cash(gid, ctx.author.id, stake):
            # Lost the money race: release the slot only if still ours.
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM heists WHERE guild_id=? AND crew=?', (gid, crew0))
            b = bal(gid, ctx.author.id)
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        opener = await ctx.reply(t(gid, 'crime.open', target=target, stake=stake))
        try:
            self._open_msg[gid] = (opener.channel.id, opener.id)
        except Exception:
            pass
        await asyncio.sleep(JOIN_WINDOW + 2)
        await self._resolve(ctx.guild, gid)

    @heist.command(name='join', description='DoÅ‚Ä…cz do napadu')
    async def heist_join(self, ctx):
        from cogs.gamble import bal, take_cash
        gid = str(ctx.guild.id)
        me = str(ctx.author.id)
        # Compare-and-swap append: concurrent joins serialize on the crew
        # value instead of both appending to the same stale read.
        for _ in range(3):
            with db.conn_ctx() as conn:
                cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
                if not cur or int(cur['ends_at'] or 0) < int(time.time()) + 5:
                    return await ctx.reply(t(gid, 'crime.none'), ephemeral=True)
                crew = json.loads(cur['crew'] or '[]')
                if me in crew:
                    return await ctx.reply(t(gid, 'crime.in_crew'), ephemeral=True)
                if len(crew) >= 5:
                    return await ctx.reply(t(gid, 'crime.full'), ephemeral=True)
                stake = cur['stake']
                new_crew = json.dumps(crew + [me])
                upd = conn.execute('UPDATE heists SET crew=? WHERE guild_id=? AND crew=?',
                                   (new_crew, gid, cur['crew']))
                if (upd.rowcount or 0) != 1:
                    continue  # lost the race: re-read and retry
            if not take_cash(gid, ctx.author.id, stake):
                # Roll back our append (exact match, so other joins stay intact).
                with db.conn_ctx() as conn:
                    old = conn.execute('SELECT crew FROM heists WHERE guild_id=?', (gid,)).fetchone()
                    if old:
                        try:
                            fixed = [u for u in json.loads(old['crew'] or '[]') if u != me]
                        except Exception:
                            fixed = []
                        conn.execute('UPDATE heists SET crew=? WHERE guild_id=? AND crew=?',
                                     (json.dumps(fixed), gid, old['crew']))
                b = bal(gid, ctx.author.id)
                return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            return await ctx.reply(t(gid, 'crime.joined', n=len(crew) + 1))
        return await ctx.reply(t(gid, 'crime.none'), ephemeral=True)

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
            result = t(guild.id, 'crime.solo', stake=cur['stake'])
        else:
            from cogs.gamble import GOD_IDS
            chance = 0.90 if any(str(u) in GOD_IDS for u in crew) else 0.30 + 0.07 * len(crew)
            pot = int(cur['stake'] * len(crew)
                      * CRIME_HEIST_TARGETS[cur['target']].payout_mult)
            if random.random() < chance:
                share = pot // len(crew)
                for uid in crew:
                    b = bal(gid, uid)
                    set_cash(gid, uid, b['cash'] + share)
                result = t(guild.id, 'crime.win', pot=pot, share=share,
                           crew=', '.join(f'<@{u}>' for u in crew))
            else:
                for uid in crew:
                    db.jail(gid, uid, 15)
                result = t(guild.id, 'crime.fail', crew=', '.join(f'<@{u}>' for u in crew))
        # Resolve on the opener card; fall back to a fresh message.
        edited = False
        try:
            cid, mid = (self._open_msg or {}).pop(gid, (None, None))
            if mid:
                ch0 = guild.get_channel(int(cid)) if cid else None
                if ch0:
                    opener = await ch0.fetch_message(int(mid))
                    await opener.edit(embed=ok(result))
                    edited = True
        except Exception as e:
            print(f'[crime] opener edit failed: {e}')
        if not edited:
            ch = guild.get_channel(int(cur['channel_id'])) if cur['channel_id'] else None
            if ch:
                try:
                    await ch.send(embed=ok(result))
                except Exception as e:
                    print(f'[crime] resolve send failed: {e}')

    @commands.command(name='bounty', description='Nagroda za głowę')
    async def bounty(self, ctx, member: discord.Member, amount: int):
        """Post a bounty. ESCROWED: the amount leaves the poster's wallet
        immediately and sits in the bounties table until a successful rob
        claims it. No expiry — an unrobbed target locks the poster's money
        indefinitely (by design: check `.bounties` before posting big)."""
        from cogs.gamble import bal, take_cash
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'crime.bounty_no'), ephemeral=True)
        if amount < CRIME_BOUNTY_MIN:
            return await ctx.reply(t(gid, 'crime.bounty_min', min=CRIME_BOUNTY_MIN), ephemeral=True)
        if not take_cash(gid, ctx.author.id, amount):
            b = bal(gid, ctx.author.id)
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO bounties (guild_id, target_id, amount, by_id) VALUES (?,?,?,?)',
                         (str(gid), str(member.id), amount, str(ctx.author.id)))
        await ctx.reply(t(gid, 'crime.bounty_set', user=member.display_name, amount=cshort(amount)))

    @commands.command(name='bounties', description='Lista nagród')
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
            lines.append(f"• {(m.display_name if m else '?')} — **{r['a']}**")
        await ctx.reply(embed=ok('\n'.join(lines)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Crime(bot))

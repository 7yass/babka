"""Shop: nick tokens, force-nick scrolls, XP boosts, rob shields. Plus bail."""
import asyncio
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok

ITEMS = {
    'nick': {'price': 500, 'use': 'shop.u_nick'},
    'force': {'price': 1500, 'use': 'shop.u_force'},
    'xpboost': {'price': 2000, 'use': 'shop.u_xpboost'},
    'shield': {'price': 1200, 'use': 'shop.u_shield'},
}
BAIL_COST = 500


def inv_add(gid, uid, item: str, qty=1, expires=0):
    with db.conn_ctx() as conn:
        conn.execute('''INSERT INTO inventory (guild_id, user_id, item, qty, expires) VALUES (?,?,?,?,?)
            ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty=qty+excluded.qty,
            expires=MAX(expires, excluded.expires)''', (str(gid), str(uid), item, qty, expires))


def inv_take(gid, uid, item: str) -> bool:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item=?',
                           (str(gid), str(uid), item)).fetchone()
        if not row or (row['qty'] or 0) <= 0:
            return False
        conn.execute('UPDATE inventory SET qty=qty-1 WHERE guild_id=? AND user_id=? AND item=?',
                     (str(gid), str(uid), item))
        return True


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='shop', description='Sklep')
    async def shop(self, ctx):
        lines = [f"• **{k}** — {v['price']}$ — {t(ctx.guild.id, v['use'])}" for k, v in ITEMS.items()]
        await ctx.reply(embed=ok(t(ctx.guild.id, 'shop.title') + '\n' + '\n'.join(lines)), ephemeral=True)

    @shop.command(name='buy', description='Kup przedmiot')
    async def buy(self, ctx, item: str):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        item = (item or '').lower()
        if item not in ITEMS:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        price = ITEMS[item]['price']
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        exp = 0
        if item in ('xpboost', 'shield'):
            exp = int(time.time()) + 24 * 3600
        inv_add(gid, ctx.author.id, item, 1, exp)
        await ctx.reply(t(gid, 'shop.bought', item=item), ephemeral=True)

    @commands.command(name='inv', description='Twoje graty')
    async def inv(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT item, qty, expires FROM inventory WHERE guild_id=? AND user_id=?',
                                (str(gid), str(ctx.author.id))).fetchall()
        rows = [dict(r) for r in rows if (r['qty'] or 0) > 0]
        if not rows:
            return await ctx.reply(t(gid, 'shop.empty'), ephemeral=True)
        lines = []
        for r in rows:
            tail = ''
            if r['expires']:
                left = max(0, int(r['expires']) - int(time.time()))
                tail = f" ({left // 3600}h left)" if left else ' (expired)'
            lines.append(f"• **{r['item']}** x{r['qty']}{tail}")
        await ctx.reply(embed=ok('\n'.join(lines)), ephemeral=True)

    @commands.command(name='nick', description='Użyj token zmiany nicku')
    async def nick(self, ctx, *, newname: str):
        gid = ctx.guild.id
        if not inv_take(gid, ctx.author.id, 'nick'):
            return await ctx.reply(t(gid, 'shop.no_token'), ephemeral=True)
        try:
            await ctx.author.edit(nick=newname[:32], reason='nick token')
        except Exception:
            inv_add(gid, ctx.author.id, 'nick')
            return await ctx.reply(t(gid, 'shop.nick_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'shop.nick_ok', name=newname[:32]))

    @commands.command(name='nickbomb', description='Zmień komuś nick na 1h')
    async def forcenick(self, ctx, member: discord.Member, *, newname: str):
        gid = ctx.guild.id
        if not inv_take(gid, ctx.author.id, 'force'):
            return await ctx.reply(t(gid, 'shop.no_scroll'), ephemeral=True)
        old = member.nick
        try:
            await member.edit(nick=newname[:32], reason=f'forced by {ctx.author}')
        except Exception:
            inv_add(gid, ctx.author.id, 'force')
            return await ctx.reply(t(gid, 'shop.nick_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'shop.forced', user=member.display_name))
        await asyncio.sleep(3600)
        try:
            cur = ctx.guild.get_member(member.id)
            if cur and cur.nick == newname[:32]:
                await cur.edit(nick=old, reason='force expired')
        except Exception:
            pass

    @commands.command(name='bail', description='Wykup się z pudła (500)')
    async def bail(self, ctx):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        left = db.jail_left(gid, ctx.author.id)
        if not left:
            return await ctx.reply(t(gid, 'shop.not_jailed'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < BAIL_COST:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - BAIL_COST)
        db.unjail(gid, ctx.author.id)
        await ctx.reply(t(gid, 'shop.free'))


async def setup(bot):
    await bot.add_cog(Shop(bot))

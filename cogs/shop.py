"""Shop: nick tokens, force-nick scrolls, XP boosts, rob shields. Plus bail."""
import asyncio
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok

ITEMS = {
    'cookie': {'price': 2000, 'use': 'shop.u_cookie'},
    'scratch': {'price': 3000, 'use': 'shop.u_scratch'},
    'nick': {'price': 10000, 'use': 'shop.u_nick'},
    'lootbox': {'price': 20000, 'use': 'shop.u_lootbox'},
    'coffee': {'price': 25000, 'use': 'shop.u_coffee'},
    'force': {'price': 30000, 'use': 'shop.u_force'},
    'shield': {'price': 40000, 'use': 'shop.u_shield'},
    'xpboost': {'price': 50000, 'use': 'shop.u_xpboost'},
    'paint': {'price': 50000, 'use': 'shop.u_paint'},
    'luckyglove': {'price': 60000, 'use': 'shop.u_luckyglove'},
    'famestar': {'price': 75000, 'use': 'shop.u_famestar'},
    'pardon': {'price': 80000, 'use': 'shop.u_pardon'},
    'curse': {'price': 100000, 'use': 'shop.u_curse'},
    'megabox': {'price': 100000, 'use': 'shop.u_megabox'},
    'xpbomb': {'price': 120000, 'use': 'shop.u_xpbomb'},
    'greatshield': {'price': 150000, 'use': 'shop.u_greatshield'},
    'titanboost': {'price': 180000, 'use': 'shop.u_titanboost'},
    'gigabox': {'price': 500000, 'use': 'shop.u_gigabox'},
    'vip': {'price': 1000000, 'use': 'shop.u_vip'},
}
# buy -> (inventory item, duration seconds) for stashable goods.
BUY_MAP = {
    'nick': ('nick', 0),
    'force': ('force', 0),
    'xpboost': ('xpboost', 24 * 3600),
    'shield': ('shield', 24 * 3600),
    'greatshield': ('shield', 7 * 24 * 3600),
    'titanboost': ('xpboost', 7 * 24 * 3600),
    'luckyglove': ('luckyglove', 0),
    'paint': ('paint', 0),
    'curse': ('curse', 0),
}
BAIL_COST = 15000
VIP_ROLE = 'Babka VIP'


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
        import random as _rnd
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        item = (item or '').lower()
        if item not in ITEMS:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        price = ITEMS[item]['price']
        if item in ('lootbox', 'megabox', 'gigabox'):
            return await self._open_box(ctx, item, price)
        if item == 'vip':
            return await self._buy_vip(ctx, price)
        if item == 'scratch':
            return await self._scratch(ctx, price)
        if item == 'cookie':
            return await self._cookie(ctx, price)
        if item == 'xpbomb':
            return await self._xpbomb(ctx, price)
        if item == 'coffee':
            return await self._coffee(ctx, price)
        if item == 'famestar':
            return await self._famestar(ctx, price)
        if item == 'pardon':
            return await self._pardon(ctx, price)
        if item == 'paint':
            return await self._paint(ctx, price)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        inv_item, dur = BUY_MAP[item]
        exp = int(time.time()) + dur if dur else 0
        inv_add(gid, ctx.author.id, inv_item, 1, exp)
        await ctx.reply(t(gid, 'shop.bought', item=item), ephemeral=True)

    # (cash_lo, cash_hi, weight) normal prizes per box; then item/jackpot rolls
    BOX_TABLES = {
        'lootbox': {'cash': (8000, 40000, 0.55), 'big': (60000, 100000, 0.08),
                    'jackpot': 200000, 'jackpot_w': 0.05,
                    'items': [('xpboost', 24 * 3600, 0.20), ('shield', 24 * 3600, 0.12)]},
        'megabox': {'cash': (50000, 140000, 0.45), 'big': (250000, 500000, 0.13),
                    'jackpot': 1000000, 'jackpot_w': 0.05,
                    'items': [('xpboost', 7 * 24 * 3600, 0.25), ('shield', 7 * 24 * 3600, 0.12)]},
        'gigabox': {'cash': (250000, 600000, 0.50), 'big': (800000, 1600000, 0.20),
                    'jackpot': 8000000, 'jackpot_w': 0.01,
                    'items': [('xpboost', 7 * 24 * 3600, 0.15), ('shield', 7 * 24 * 3600, 0.14)]},
    }

    async def _open_box(self, ctx, box: str, price: int):
        """Instant-open gambling box driven by BOX_TABLES."""
        import random as _rnd
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        cfg = self.BOX_TABLES[box]
        now = int(time.time())
        roll = _rnd.random()
        if roll < cfg['jackpot_w']:
            win = cfg['jackpot']
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=win), ephemeral=True)
        acc = cfg['jackpot_w'] + cfg['cash'][2]
        if roll < acc:
            win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=win), ephemeral=True)
        acc += cfg['big'][2]
        if roll < acc:
            win = _rnd.randint(cfg['big'][0], cfg['big'][1])
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=win), ephemeral=True)
        for inv_item, dur, w in cfg['items']:
            acc += w
            if roll < acc:
                inv_add(gid, ctx.author.id, inv_item, 1, now + dur)
                return await ctx.reply(t(gid, 'shop.loot_item', item=inv_item), ephemeral=True)
        win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
        set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
        return await ctx.reply(t(gid, 'shop.loot_cash', win=win), ephemeral=True)

    async def _scratch(self, ctx, price: int):
        """3k scratchcard: mostly dust, rarely a fortune."""
        import random as _rnd
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        roll = _rnd.random()
        if roll < 0.01:
            win = 600000
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=win), ephemeral=True)
        if roll < 0.10:
            win = _rnd.randint(45000, 90000)
        elif roll < 0.40:
            win = _rnd.randint(6000, 18000)
        else:
            win = _rnd.randint(0, 1500)
        if win:
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=win), ephemeral=True)
        return await ctx.reply(t(gid, 'shop.scratch_lose'), ephemeral=True)

    async def _cookie(self, ctx, price: int):
        """Babka's cookie: always tasty, sometimes profitable."""
        import random as _rnd
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        win = _rnd.randint(0, 5000)
        if win:
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
        return await ctx.reply(t(gid, 'shop.cookie_win', win=win), ephemeral=True)

    async def _xpbomb(self, ctx, price: int):
        """Instant +5000 XP."""
        from cogs.gamble import bal, set_cash
        from cogs.levels import add_xp
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        res = add_xp(gid, ctx.author.id, 5000)
        await ctx.reply(t(gid, 'shop.xp_ok', xp=5000, level=res['level']), ephemeral=True)

    async def _coffee(self, ctx, price: int):
        """Reset the .work cooldown instantly."""
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_work=0 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(ctx.author.id)))
        await ctx.reply(t(gid, 'shop.coffee_ok'), ephemeral=True)

    async def _famestar(self, ctx, price: int):
        """Instant +5000 fame fans."""
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        with db.conn_ctx() as conn:
            conn.execute('''INSERT INTO jobs (guild_id, user_id, job, fans, tier)
                VALUES (?,?, '',0,0) ON CONFLICT(guild_id, user_id) DO NOTHING''',
                         (str(gid), str(ctx.author.id)))
            conn.execute('UPDATE jobs SET fans=fans+5000 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(ctx.author.id)))
        await ctx.reply(t(gid, 'shop.fame_ok'), ephemeral=True)

    async def _pardon(self, ctx, price: int):
        """Wipe your latest warn."""
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT id FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1',
                               (str(gid), str(ctx.author.id))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'shop.pardon_none'), ephemeral=True)
            b = bal(gid, ctx.author.id)
            if b['cash'] < price:
                return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
            set_cash(gid, ctx.author.id, b['cash'] - price)
            conn.execute('DELETE FROM warns WHERE id=?', (row['id'],))
        await ctx.reply(t(gid, 'shop.pardon_ok'), ephemeral=True)

    async def _paint(self, ctx, price: int):
        """Personal colored name role (re-rolls color on re-buy)."""
        import random as _rnd
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        try:
            name = f'Paint • {ctx.author.id}'
            role = discord.utils.find(lambda r: r.name == name, ctx.guild.roles)
            color = discord.Color.from_rgb(_rnd.randint(40, 255), _rnd.randint(40, 255),
                                           _rnd.randint(40, 255))
            if not role:
                role = await ctx.guild.create_role(name=name, color=color, reason='paint purchase')
            else:
                await role.edit(color=color, reason='paint re-roll')
            if isinstance(ctx.author, discord.Member) and role not in ctx.author.roles:
                await ctx.author.add_roles(role, reason='paint purchase')
        except Exception:
            inv_add(gid, ctx.author.id, 'paint')
            return await ctx.reply(t(gid, 'shop.nick_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'shop.paint_ok'), ephemeral=True)

    @commands.command(name='curse', description='Zdejmij komuś tarczę')
    async def curse(self, ctx, member: discord.Member):
        """Spend a curse scroll to strip someone's rob shield."""
        import time as _t
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'shop.curse_self'), ephemeral=True)
        if not db.has_shield(gid, member.id):
            return await ctx.reply(t(gid, 'shop.curse_none', user=member.display_name),
                                   ephemeral=True)
        if not inv_take(gid, ctx.author.id, 'curse'):
            return await ctx.reply(t(gid, 'shop.no_curse'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute("UPDATE inventory SET expires=0 WHERE guild_id=? AND user_id=? AND item='shield'",
                         (str(gid), str(member.id)))
        await ctx.reply(t(gid, 'shop.curse_ok', user=member.display_name))

    async def _buy_vip(self, ctx, price: int):
        """One-time prestige role purchase."""
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        member = ctx.author
        role = discord.utils.find(lambda r: r.name == VIP_ROLE, ctx.guild.roles)
        if role and isinstance(member, discord.Member) and role in member.roles:
            return await ctx.reply(t(gid, 'shop.vip_owned'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        try:
            if not role:
                role = await ctx.guild.create_role(
                    name=VIP_ROLE, color=discord.Color.gold(), reason='VIP purchase')
            if isinstance(member, discord.Member):
                await member.add_roles(role, reason='VIP purchase')
        except Exception:
            return await ctx.reply(t(gid, 'shop.nick_fail'), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        await ctx.reply(t(gid, 'shop.vip_ok'), ephemeral=True)

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

    @commands.command(name='bail', description='Wykup się z pudła (15000)')
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

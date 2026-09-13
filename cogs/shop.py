"""Shop: nick tokens, force-nick scrolls, XP boosts, rob shields. Plus bail."""
import asyncio
import time

import discord
from discord.ext import commands

import database as db
from utils.cards import short as cshort
from lang import t
from utils.embeds import ok

ITEMS = {
    'cookie': {'price': 5000, 'use': 'shop.u_cookie'},
    'scratch': {'price': 10000, 'use': 'shop.u_scratch'},
    'lootbox': {'price': 75000, 'use': 'shop.u_lootbox'},
    'nick': {'price': 100000, 'use': 'shop.u_nick'},
    'shield': {'price': 150000, 'use': 'shop.u_shield'},
    'xpboost': {'price': 200000, 'use': 'shop.u_xpboost'},
    'pardon': {'price': 300000, 'use': 'shop.u_pardon'},
    'curse': {'price': 400000, 'use': 'shop.u_curse'},
    'megabox': {'price': 400000, 'use': 'shop.u_megabox'},
    'force': {'price': 500000, 'use': 'shop.u_force'},
    'bail': {'price': 40000, 'use': 'shop.u_bail'},
    'vip': {'price': 5000000, 'use': 'shop.u_vip'},
}
# buy -> (inventory item, duration seconds) for stashable goods.
BUY_MAP = {
    'nick': ('nick', 0),
    'force': ('force', 0),
    'xpboost': ('xpboost', 24 * 3600),
    'shield': ('shield', 24 * 3600),
    'curse': ('curse', 0),
}
BAIL_COST = 40000
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


class _IxCtx:
    """Minimal Context shim so purchase handlers work from button clicks."""
    def __init__(self, interaction: discord.Interaction):
        self._ix = interaction
        self.guild = interaction.guild
        self.author = interaction.user

    async def reply(self, content=None, **kwargs):
        kwargs.pop('mention_author', None)
        try:
            await self._ix.followup.send(content, ephemeral=True, **kwargs)
        except Exception:
            pass


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # category -> item keys, in display order
    SHOP_SECTIONS = [
        ('Cheap thrills', ['cookie', 'scratch']),
        ('Identity', ['nick', 'force', 'pardon']),
        ('Protection', ['shield', 'curse']),
        ('Power', ['xpboost']),
        ('Boxes', ['lootbox', 'megabox']),
        ('Freedom', ['bail']),
        ('Prestige', ['vip']),
    ]

    def _shop_layout(self, gid, uid, cash: int):
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Separator
        layout = LayoutView(timeout=180)
        box = Container(accent_color=0xFAC43C)
        box.add_item(TextDisplay(f'# 🛒 {t(gid, "shop.title")}\n'
                                 f'-# {t(gid, "shop.wallet", cash=cshort(cash))}'))
        for sec, keys in self.SHOP_SECTIONS:
            box.add_item(Separator(visible=False))
            lines = '\n'.join(
                f"• **{k}** — {cshort(ITEMS[k]['price'])} — {t(gid, ITEMS[k]['use'])}"
                for k in keys)
            box.add_item(TextDisplay(f'**{sec}**\n{lines}'))
        # buy buttons, 5 per row
        row = ActionRow()
        for sec, keys in self.SHOP_SECTIONS:
            for k in keys:
                if len(row.children) >= 5:
                    box.add_item(row)
                    row = ActionRow()
                b = discord.ui.Button(label=f'{k} · {cshort(ITEMS[k]["price"])}',
                                      style=discord.ButtonStyle.secondary,
                                      custom_id=f'shopbuy:{uid}:{k}')
                b.callback = self._mk_buy(gid, uid, k)
                row.add_item(b)
        if row.children:
            box.add_item(row)
        layout.add_item(box)
        return layout

    def _mk_buy(self, gid, uid, item: str):
        async def _cb(interaction: discord.Interaction):
            if interaction.user.id != int(uid):
                return await interaction.response.send_message(
                    t(gid, 'eco.not_yours'), ephemeral=True)
            from cogs.gamble import bal
            price = ITEMS[item]['price']
            cash = bal(gid, uid)['cash']
            layout = self._confirm_layout(gid, item, price, cash)
            await interaction.response.send_message(view=layout, ephemeral=True)
        return _cb

    def _confirm_layout(self, gid, item: str, price: int, cash: int):
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow
        layout = LayoutView(timeout=120)
        box = Container(accent_color=0xFAC43C)
        left = cash - price
        box.add_item(TextDisplay(
            f'## {item} — {cshort(price)}\n'
            f'{t(gid, ITEMS[item]["use"])}\n'
            f'-# {t(gid, "shop.confirm", cash=cshort(cash), left=cshort(left))}'))
        row = ActionRow()
        yes = discord.ui.Button(label=t(gid, 'shop.yes'), style=discord.ButtonStyle.success,
                                custom_id='shop_yes')
        no = discord.ui.Button(label=t(gid, 'shop.no'), style=discord.ButtonStyle.danger,
                               custom_id='shop_no')

        async def _yes(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            await self.buy(_IxCtx(interaction), item)

        async def _no(interaction: discord.Interaction):
            try:
                await interaction.response.edit_message(
                    content=t(gid, 'shop.cancelled'), view=None)
            except Exception:
                pass

        yes.callback = _yes
        no.callback = _no
        row.add_item(yes)
        row.add_item(no)
        box.add_item(row)
        layout.add_item(box)
        return layout

    @commands.group(name='shop', description='Sklep')
    async def shop(self, ctx):
        from cogs.gamble import bal
        gid = ctx.guild.id
        layout = self._shop_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'])
        await ctx.reply(view=layout, ephemeral=True)

    @shop.command(name='buy', description='Kup przedmiot')
    async def buy(self, ctx, item: str):
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_gate
        gid = ctx.guild.id
        item = (item or '').lower()
        if item not in ITEMS:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        price = ITEMS[item]['price']
        if item in self.GAMBLE_ITEMS:
            wait = _gamble_gate(gid, ctx.author.id)
            if wait is not None:
                return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        if item in ('lootbox', 'megabox'):
            return await self._open_box(ctx, item, price)
        if item == 'vip':
            return await self._buy_vip(ctx, price)
        if item == 'scratch':
            return await self._scratch(ctx, price)
        if item == 'cookie':
            return await self._cookie(ctx, price)
        if item == 'pardon':
            return await self._pardon(ctx, price)
        if item == 'bail':
            return await self._buy_bail(ctx, price)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        inv_item, dur = BUY_MAP[item]
        exp = int(time.time()) + dur if dur else 0
        inv_add(gid, ctx.author.id, inv_item, 1, exp)
        await ctx.reply(t(gid, 'shop.bought', item=item), ephemeral=True)

    # (cash_lo, cash_hi, weight) normal prizes per box; then item/jackpot rolls.
    # Tuned so expected value stays well under the price (house edge).
    BOX_TABLES = {
        'lootbox': {'cash': (10000, 40000, 0.60), 'big': (80000, 150000, 0.08),
                    'jackpot': 400000, 'jackpot_w': 0.02,
                    'items': [('xpboost', 24 * 3600, 0.10), ('shield', 24 * 3600, 0.07)]},
        'megabox': {'cash': (100000, 300000, 0.45), 'big': (400000, 800000, 0.13),
                    'jackpot': 2000000, 'jackpot_w': 0.02,
                    'items': [('xpboost', 7 * 24 * 3600, 0.15), ('shield', 7 * 24 * 3600, 0.12)]},
    }
    # instant shop gambles share the hourly play limit with casino games
    GAMBLE_ITEMS = {'lootbox', 'megabox', 'scratch', 'cookie'}

    async def _open_box(self, ctx, box: str, price: int):
        """Instant-open gambling box driven by BOX_TABLES."""
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_use
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        _gamble_use(gid, ctx.author.id)
        cfg = self.BOX_TABLES[box]
        now = int(time.time())
        roll = _rnd.random()
        if roll < cfg['jackpot_w']:
            win = cfg['jackpot']
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=cshort(win)), ephemeral=True)
        acc = cfg['jackpot_w'] + cfg['cash'][2]
        if roll < acc:
            win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        acc += cfg['big'][2]
        if roll < acc:
            win = _rnd.randint(cfg['big'][0], cfg['big'][1])
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        for inv_item, dur, w in cfg['items']:
            acc += w
            if roll < acc:
                inv_add(gid, ctx.author.id, inv_item, 1, now + dur)
                return await ctx.reply(t(gid, 'shop.loot_item', item=inv_item), ephemeral=True)
        win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
        set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
        return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)

    async def _scratch(self, ctx, price: int):
        """10k scratchcard: mostly dust, rarely a fortune."""
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_use
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        _gamble_use(gid, ctx.author.id)
        roll = _rnd.random()
        if roll < 0.01:
            win = 300000
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=win), ephemeral=True)
        if roll < 0.10:
            win = _rnd.randint(15000, 30000)
        elif roll < 0.40:
            win = _rnd.randint(2000, 6000)
        else:
            win = _rnd.randint(0, 2000)
        if win:
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        return await ctx.reply(t(gid, 'shop.scratch_lose'), ephemeral=True)

    async def _cookie(self, ctx, price: int):
        """Babka's cookie: always tasty, usually a donation to Babka."""
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_use
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        _gamble_use(gid, ctx.author.id)
        win = _rnd.randint(0, 2500)
        if win:
            set_cash(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'] + win)
        return await ctx.reply(t(gid, 'shop.cookie_win', win=cshort(win)), ephemeral=True)

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
                return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            set_cash(gid, ctx.author.id, b['cash'] - price)
            conn.execute('DELETE FROM warns WHERE id=?', (row['id'],))
        await ctx.reply(t(gid, 'shop.pardon_ok'), ephemeral=True)

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
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
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

    @commands.command(name='nickbomb', description='Zmień komuś nick na 24h')
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
        await asyncio.sleep(86400)
        try:
            cur = ctx.guild.get_member(member.id)
            if cur and cur.nick == newname[:32]:
                await cur.edit(nick=old, reason='force expired')
        except Exception:
            pass

    @commands.command(name='bail', description='Wykup się z pudła (40000)')
    async def bail(self, ctx):
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        left = db.jail_left(gid, ctx.author.id)
        if not left:
            return await ctx.reply(t(gid, 'shop.not_jailed'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < BAIL_COST:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - BAIL_COST)
        db.unjail(gid, ctx.author.id)
        await ctx.reply(t(gid, 'shop.free'))

    async def _buy_bail(self, ctx, price: int):
        """Buy your way out through the shop (same as .bail)."""
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        if not db.jail_left(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'shop.not_jailed'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - price)
        db.unjail(gid, ctx.author.id)
        await ctx.reply(t(gid, 'shop.free'))


async def setup(bot):
    await bot.add_cog(Shop(bot))

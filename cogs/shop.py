"""Shop: nick tokens, force-nick scrolls, XP boosts, rob shields. Plus bail."""
import asyncio
import time

import discord
from discord.ext import commands

import database as db
from utils.cards import short as cshort
from utils.economy import SHOP_PRICES
from lang import t, set_ctx_lang
from utils.embeds import card
from utils.emojis import em

ITEMS = {
    'cookie': {'price': SHOP_PRICES['cookie'].amount, 'use': 'shop.u_cookie'},
    'scratch': {'price': SHOP_PRICES['scratch'].amount, 'use': 'shop.u_scratch'},
    'lootbox': {'price': SHOP_PRICES['lootbox'].amount, 'use': 'shop.u_lootbox'},
    'nick': {'price': SHOP_PRICES['nick'].amount, 'use': 'shop.u_nick'},
    'shield': {'price': SHOP_PRICES['shield'].amount, 'use': 'shop.u_shield'},
    'xpboost': {'price': SHOP_PRICES['xpboost'].amount, 'use': 'shop.u_xpboost'},
    'pardon': {'price': SHOP_PRICES['pardon'].amount, 'use': 'shop.u_pardon'},
    'curse': {'price': SHOP_PRICES['curse'].amount, 'use': 'shop.u_curse'},
    'megabox': {'price': SHOP_PRICES['megabox'].amount, 'use': 'shop.u_megabox'},
    'force': {'price': SHOP_PRICES['force'].amount, 'use': 'shop.u_force'},
    'highroller': {'price': SHOP_PRICES['highroller'].amount, 'use': 'shop.u_highroller'},
    'bail': {'price': SHOP_PRICES['bail'].amount, 'use': 'shop.u_bail'},
    'vip': {'price': SHOP_PRICES['vip'].amount, 'use': 'shop.u_vip'},
}
# buy -> (inventory item, duration seconds) for stashable goods.
BUY_MAP = {
    'nick': ('nick', 0),
    'force': ('force', 0),
    'xpboost': ('xpboost', 24 * 3600),
    'shield': ('shield', 24 * 3600),
    'curse': ('curse', 0),
    'highroller': ('highroller', 10 * 60),
}
BAIL_COST = SHOP_PRICES['bail'].amount
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


# storefront display names + fleet emoji per item key
DISPLAY = {
    'cookie': 'Cookie', 'scratch': 'Scratcher', 'lootbox': 'Lootbox',
    'nick': 'Nick Token', 'shield': 'Rob Shield', 'xpboost': 'XP Boost',
    'pardon': 'Pardon', 'curse': 'Curse Scroll', 'megabox': 'Megabox',
    'force': 'Force Scroll', 'highroller': 'High Roller', 'bail': 'Bail',
    'vip': 'VIP',
}
ITEM_EMOJI = {
    'cookie': 'candy', 'scratch': 'price_tag', 'lootbox': 'box_box',
    'nick': 'lock', 'shield': 'shield', 'xpboost': 'bolt',
    'pardon': 'check', 'curse': 'check_cross', 'megabox': 'box_done',
    'force': 'quest_scroll', 'highroller': 'medal_gold', 'bail': 'lock_open',
    'vip': 'trophy',
}
SECTION_EMOJI = {
    'Cheap thrills': 'candy', 'Identity': 'lock', 'Protection': 'shield',
    'Power': 'bolt', 'Boxes': 'box_box', 'Freedom': 'lock_open',
    'Prestige': 'trophy',
}


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
        ('Prestige', ['highroller', 'vip']),
    ]

    @classmethod
    def id_map(cls) -> dict:
        """Stable buy-by-id numbers: global across SHOP_SECTIONS order."""
        out, n = {}, 0
        for _s, keys in cls.SHOP_SECTIONS:
            for k in keys:
                n += 1
                out[n] = k
        return out

    def _shop_layout(self, gid, uid, cash: int, filt=None, owner_name: str = ''):
        """PokeMeow-style storefront: wallet header, numbered sections,
        buy instructions, category buttons (filtered views) + Main shop."""
        from utils.shopui import catalog
        entries = {k: {'name': DISPLAY[k], 'price': ITEMS[k]['price'],
                       'emo': ITEM_EMOJI.get(k, ''), 'desc': t(gid, ITEMS[k]['use'])}
                   for k in ITEMS}
        secs = self.SHOP_SECTIONS if filt is None else \
            [(s, ks) for s, ks in self.SHOP_SECTIONS if s == filt]
        num_of = {k: i for i, k in self.id_map().items()}
        p = db.get_prefix(gid) or '.'
        layout, _ids = catalog(
            gid, uid,
            tagline=t(gid, 'shop.tagline'),
            coins_line=t(gid, 'shop.coins', user=owner_name),
            cash=cash,
            sections=secs, all_sections=self.SHOP_SECTIONS, entries=entries,
            accent=0xFAC43C, cmd='shop', prefix=p,
            tip=t(gid, 'shop.tip', cmd='shop', p=p),
            buy_title=t(gid, 'shop.buy_title'),
            buy_1=t(gid, 'shop.buy_1', cmd='shop', p=p),
            buy_2=t(gid, 'shop.buy_2', cmd='shop', p=p),
            ex_label=t(gid, 'shop.ex_label'),
            ex1='nick 1', ex2=f'{num_of.get("nick", 3)} 1',
            foot=t(gid, 'shop.foot', cmd='shop', p=p),
            section_emos=SECTION_EMOJI,
            on_section=self._section_cb(gid, uid))
        return layout

    def _section_cb(self, gid, uid):
        """Category buttons: re-render filtered (idx) or full (idx -1)."""
        def factory(idx):
            async def _cb(ix: discord.Interaction):
                set_ctx_lang(ix.user)
                if ix.user.id != int(uid):
                    return await ix.response.send_message(t(gid, 'eco.not_yours'), ephemeral=True)
                from cogs.gamble import bal
                member = ix.guild.get_member(int(uid)) if ix.guild else None
                name = member.display_name if member else f'User {uid}'
                filt = None if idx < 0 else self.SHOP_SECTIONS[idx][0]
                layout = self._shop_layout(gid, int(uid), bal(gid, int(uid))['cash'],
                                           filt=filt, owner_name=name)
                await ix.response.edit_message(view=layout)
            return _cb
        return factory

    @commands.group(name='shop', description='Sklep', invoke_without_command=True)
    async def shop(self, ctx):
        from cogs.gamble import bal
        gid = ctx.guild.id
        if (ctx.prefix or '') == ';':
            # Pokemon storefront on the pokemon prefix (PokeMeow-style).
            # Economy store lives on the guild prefix (`.shop`).
            pcog = self.bot.get_cog('Pokemon')
            if pcog is None:
                return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
            layout = pcog._balls_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                        owner_name=ctx.author.display_name)
            return await ctx.reply(view=layout, ephemeral=True)
        layout = self._shop_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                   owner_name=ctx.author.display_name)
        await ctx.reply(view=layout, ephemeral=True)

    async def _buy_pokemon(self, ctx, key: str, n: int):
        """Pokemon-catalog purchase for `;shop buy` on the ';' prefix.
        Same catalog/ids as `;balls buy` (eggs + incense handled properly)."""
        import time as _t
        from cogs.gamble import bal, set_cash, add_cash
        from utils.cards import short as cshort
        gid = ctx.guild.id
        if not key:
            pcog = self.bot.get_cog('Pokemon')
            if pcog is None:
                return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
            layout = pcog._balls_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                        owner_name=ctx.author.display_name)
            return await ctx.reply(view=layout, ephemeral=True)
        try:
            from cogs.pokemon import Pokemon as _Pk, balls_add, EGG_CYCLES, INCENSE_SECONDS
        except Exception:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        pk_by_name = {k.lower(): k for k in _Pk.PK_NAMES}
        for k, v in _Pk.PK_NAMES.items():
            pk_by_name[v.lower()] = k
            pk_by_name[v.lower().replace(' ', '_')] = k
            pk_by_name[v.lower().replace(' ', '')] = k
        if key.isdigit():
            key = _Pk.ball_ids().get(int(key), '')
            if key in pk_by_name:
                key = pk_by_name[key]
        elif key in pk_by_name:
            key = pk_by_name[key]
        if key not in _Pk.PK_NAMES:
            pcog = self.bot.get_cog('Pokemon')
            have = pcog._balls_line(gid, ctx.author.id) if pcog else ''
            return await ctx.reply(t(gid, 'eco.pk_balls', have=have), ephemeral=True)
        n = max(1, min(99, n or 1))
        if key == 'egg':
            with db.conn_ctx() as conn:
                owned = conn.execute('SELECT COUNT(*) c FROM pk_eggs WHERE guild_id=? AND owner_id=?',
                                     (str(gid), str(ctx.author.id))).fetchone()['c']
                if owned >= 3:
                    return await ctx.reply(t(gid, 'eco.pk_eggs_full'), ephemeral=True)
                n = min(n, 3 - owned)
        price = _Pk._pk_price(key)
        if not price:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        total = price * n
        if b['cash'] < total:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        add_cash(gid, ctx.author.id, -total)
        if key == 'egg':
            with db.conn_ctx() as conn:
                for _ in range(n):
                    conn.execute('INSERT INTO pk_eggs (guild_id, owner_id, cycles) VALUES (?,?,?)',
                                 (str(gid), str(ctx.author.id), EGG_CYCLES))
        else:
            balls_add(gid, ctx.author.id, key, n)
        if key == 'incense':
            with db.conn_ctx() as conn:
                conn.execute('UPDATE pk_balls SET expires=? WHERE guild_id=? AND user_id=? AND ball=?',
                             (int(_t.time()) + INCENSE_SECONDS, str(gid), str(ctx.author.id),
                              'incense'))
        return await ctx.reply(t(gid, 'shop.bought_n', n=n, item=_Pk.PK_NAMES.get(key, key)),
                               ephemeral=True)

    async def _buy_economy(self, ctx, key: str, n: int):
        """Economy-catalog purchase (`.shop` board's own numbers/names)."""
        from cogs.gamble import bal, set_cash, _gamble_gate, add_cash
        from utils.cards import short as cshort
        gid = ctx.guild.id
        n = max(1, min(99, n or 1))
        price = ITEMS[key]['price']
        if key in self.GAMBLE_ITEMS:
            wait = _gamble_gate(gid, ctx.author.id)
            if wait is not None:
                return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        if key in ('lootbox', 'megabox'):
            return await self._open_box(ctx, key, price)
        if key == 'vip':
            return await self._buy_vip(ctx, price)
        if key == 'scratch':
            return await self._scratch(ctx, price)
        if key == 'cookie':
            return await self._cookie(ctx, price)
        if key == 'pardon':
            return await self._pardon(ctx, price)
        if key == 'bail':
            return await self._buy_bail(ctx, price)
        b = bal(gid, ctx.author.id)
        total = price * n
        if b['cash'] < total:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        add_cash(gid, ctx.author.id, -total)
        inv_item, dur = BUY_MAP[key]
        exp = int(time.time()) + dur if dur else 0
        inv_add(gid, ctx.author.id, inv_item, n, exp)
        try:
            from cogs.achievements import maybe_award
            maybe_award(gid, ctx.author.id)
        except Exception:
            pass
        await ctx.reply(t(gid, 'shop.bought_n', n=n, item=DISPLAY.get(key, key)), ephemeral=True)

    @shop.command(name='buy', description='Kup przedmiot')
    async def buy(self, ctx, item: str = '', n: str = '1'):
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_gate, add_cash
        from utils.cards import short as cshort
        gid = ctx.guild.id
        key = (item or '').lower().strip()
        try:
            n = max(1, min(99, int(n or 1)))
        except (ValueError, TypeError):
            n = 1
        if (ctx.prefix or '') == ';':
            # Pokemon storefront only on the pokemon prefix.
            return await self._buy_pokemon(ctx, key, n)
        if not key:
            layout = self._shop_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                       owner_name=ctx.author.display_name)
            return await ctx.reply(view=layout, ephemeral=True)
        # Economy first on the guild prefix: the `.shop` board's own
        # numbers/names win. Pokemon ids overlap numerically, so resolving
        # pokemon first hijacked e.g. `.shop buy 8` (xpboost) into candy.
        ekey = self.id_map().get(int(key), '') if key.isdigit() else key
        if ekey not in ITEMS:
            flat = key.replace(' ', '').replace('_', '')
            ekey = {v.lower().replace(' ', ''): k for k, v in DISPLAY.items()}.get(flat, ekey)
        if ekey in ITEMS:
            return await self._buy_economy(ctx, ekey, n)
        # ...otherwise the pokemon catalog (names + its own ids).
        # allow pokemon shop via ;shop buy <pokeball etc> — delegate to balls
        try:
            from cogs.pokemon import Pokemon as _Pk
            # normalize pokemon key: allow names and ids
            pk_by_name = {k.lower(): k for k in _Pk.PK_NAMES}
            for k, v in _Pk.PK_NAMES.items():
                pk_by_name[v.lower()] = k
                pk_by_name[v.lower().replace(' ', '_')] = k
                pk_by_name[v.lower().replace(' ', '')] = k
            # also allow ball ids via pokemon shop
            if key.isdigit():
                key = _Pk.ball_ids().get(int(key), key)
            if key in pk_by_name:
                key = pk_by_name[key]
            if key in _Pk.PK_NAMES:
                # pokemon purchase
                n = max(1, min(99, n or 1))
                price = _Pk._pk_price(key)
                if not price:
                    return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
                b = bal(gid, ctx.author.id)
                total = price * n
                if b['cash'] < total:
                    return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
                add_cash(gid, ctx.author.id, -total)
                # add to pokemon balls
                from cogs.pokemon import balls_add
                balls_add(gid, ctx.author.id, key, n)
                # handle repel/incense expire etc. via balls already
                return await ctx.reply(t(gid, 'shop.bought_n', n=n, item=_Pk.PK_NAMES.get(key, key)), ephemeral=True)
        except Exception:
            pass
        return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)

    @shop.command(name='pokemon', aliases=['balls', 'poke', 'pk'],
                     description='Sklep pokemon')
    async def shop_pokemon(self, ctx):
        """Pokemon adventure shop (same as ;balls / ;pokeshop).
        `;shop` itself is the economy store; `;shop buy` already buys
        from both stores."""
        from cogs.gamble import bal
        gid = ctx.guild.id
        pcog = self.bot.get_cog('Pokemon')
        if pcog is None:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        layout = pcog._balls_layout(gid, ctx.author.id, bal(gid, ctx.author.id)['cash'],
                                    owner_name=ctx.author.display_name)
        await ctx.reply(view=layout, ephemeral=True)

    @shop.command(name='info', description='Opis przedmiotu')
    async def item_info(self, ctx, item: str = ''):
        gid = ctx.guild.id
        key = (item or '').lower().strip()
        if (ctx.prefix or '') == ';':
            # Pokemon item lookup on the pokemon prefix.
            try:
                from cogs.pokemon import Pokemon as _Pk
            except Exception:
                return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
            if key.isdigit():
                key = _Pk.ball_ids().get(int(key), '')
            else:
                low = {k.lower(): k for k in _Pk.PK_NAMES}
                for k, v in _Pk.PK_NAMES.items():
                    low[v.lower()] = k
                key = low.get(key, key)
            if key not in _Pk.PK_NAMES:
                return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
            num_of = {k: i for i, k in _Pk.ball_ids().items()}
            item_emo = em(gid, _Pk.BALL_EMOJI.get(key, ''))
            sec = next((s for s, ks in _Pk.BALL_SECTIONS if key in ks), '?')
            return await ctx.reply(view=card(
                f'{item_emo + " " if item_emo else ""}**{_Pk.PK_NAMES[key]}**',
                f'{_Pk._pk_price(key):,} {em(gid, "coin", "$")}\n'
                f'{_Pk._pk_desc(gid, key)}\n'
                f'-# `[{num_of.get(key, "?")}]` {sec}'), ephemeral=True)
        if key.isdigit():
            key = self.id_map().get(int(key), '')
        if key not in ITEMS:
            return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
        num_of = {k: i for i, k in self.id_map().items()}
        item_emo = em(gid, ITEM_EMOJI.get(key, ''))
        sec = next((s for s, ks in self.SHOP_SECTIONS if key in ks), '?')
        await ctx.reply(view=card(
            f'{item_emo + " " if item_emo else ""}**{DISPLAY[key]}**',
            f'{ITEMS[key]["price"]:,} {em(gid, "coin", "$")}\n'
            f'{t(gid, ITEMS[key]["use"])}\n'
            f'-# `[{num_of.get(key, "?")}]` {sec}'), ephemeral=True)

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
        from cogs.gamble import bal, set_cash, _gamble_use, add_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        add_cash(gid, ctx.author.id, -price)
        _gamble_use(gid, ctx.author.id)
        cfg = self.BOX_TABLES[box]
        now = int(time.time())
        roll = _rnd.random()
        if roll < cfg['jackpot_w']:
            win = cfg['jackpot']
            add_cash(gid, ctx.author.id, win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=cshort(win)), ephemeral=True)
        acc = cfg['jackpot_w'] + cfg['cash'][2]
        if roll < acc:
            win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
            add_cash(gid, ctx.author.id, win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        acc += cfg['big'][2]
        if roll < acc:
            win = _rnd.randint(cfg['big'][0], cfg['big'][1])
            add_cash(gid, ctx.author.id, win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        for inv_item, dur, w in cfg['items']:
            acc += w
            if roll < acc:
                inv_add(gid, ctx.author.id, inv_item, 1, now + dur)
                return await ctx.reply(t(gid, 'shop.loot_item', item=inv_item), ephemeral=True)
        win = _rnd.randint(cfg['cash'][0], cfg['cash'][1])
        add_cash(gid, ctx.author.id, win)
        return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)

    async def _scratch(self, ctx, price: int):
        """10k scratchcard: mostly dust, rarely a fortune."""
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_use, add_cash
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        add_cash(gid, ctx.author.id, -price)
        _gamble_use(gid, ctx.author.id)
        roll = _rnd.random()
        if roll < 0.01:
            win = 300000
            add_cash(gid, ctx.author.id, win)
            return await ctx.reply(t(gid, 'shop.loot_jackpot', win=win), ephemeral=True)
        if roll < 0.10:
            win = _rnd.randint(15000, 30000)
        elif roll < 0.40:
            win = _rnd.randint(2000, 6000)
        else:
            win = _rnd.randint(0, 2000)
        if win:
            add_cash(gid, ctx.author.id, win)
            return await ctx.reply(t(gid, 'shop.loot_cash', win=cshort(win)), ephemeral=True)
        return await ctx.reply(t(gid, 'shop.scratch_lose'), ephemeral=True)

    async def _cookie(self, ctx, price: int):
        """Babka's cookie: always tasty, usually a donation to Babka.
        Rationed to 3 per day (house exempt)."""
        import random as _rnd
        from cogs.gamble import bal, set_cash, _gamble_use, GOD_IDS, add_cash
        gid = ctx.guild.id
        if str(ctx.author.id) not in GOD_IDS:
            import time as _t
            today = int(_t.time()) // 86400
            with db.conn_ctx() as conn:
                row = conn.execute('SELECT cookie_n, cookie_day FROM eco WHERE guild_id=? AND user_id=?',
                                   (str(gid), str(ctx.author.id))).fetchone()
                d = dict(row) if row else {}
                if d.get('cookie_day') != today:
                    conn.execute('UPDATE eco SET cookie_n=0, cookie_day=? WHERE guild_id=? AND user_id=?',
                                 (today, str(gid), str(ctx.author.id)))
                    d = {'cookie_n': 0, 'cookie_day': today}
                if (d.get('cookie_n') or 0) >= 3:
                    return await ctx.reply(t(gid, 'shop.cookie_limit'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        add_cash(gid, ctx.author.id, -price)
        _gamble_use(gid, ctx.author.id)
        if str(ctx.author.id) not in GOD_IDS:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE eco SET cookie_n=cookie_n+1 WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id)))
        win = _rnd.randint(0, 2500)
        if win:
            add_cash(gid, ctx.author.id, win)
        return await ctx.reply(t(gid, 'shop.cookie_win', win=cshort(win)), ephemeral=True)

    async def _pardon(self, ctx, price: int):
        """Wipe your latest warn."""
        from cogs.gamble import bal, set_cash, add_cash
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT id FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1',
                               (str(gid), str(ctx.author.id))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'shop.pardon_none'), ephemeral=True)
            b = bal(gid, ctx.author.id)
            if b['cash'] < price:
                return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            add_cash(gid, ctx.author.id, -price)
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
        from cogs.gamble import bal, set_cash, add_cash
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
        add_cash(gid, ctx.author.id, -price)
        await ctx.reply(t(gid, 'shop.vip_ok'), ephemeral=True)

    @commands.command(name='inv', description='Twoje graty')
    async def inv(self, ctx):
        gid = ctx.guild.id
        if (ctx.prefix or '') == ';':
            # Pokemon bag on the pokemon prefix (same as `;items`).
            pcog = self.bot.get_cog('Pokemon')
            if pcog is None:
                return await ctx.reply(t(gid, 'shop.no_item'), ephemeral=True)
            return await ctx.reply(view=pcog._items_view(
                gid, ctx.author.id, ctx.author.display_name), ephemeral=True)
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
        await ctx.reply(view=card(
            t(gid, 'shop.inv_title', user=ctx.author.display_name),
            '\n'.join(lines)), ephemeral=True)

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
        from cogs.gamble import bal, set_cash, add_cash
        gid = ctx.guild.id
        left = db.jail_left(gid, ctx.author.id)
        if not left:
            return await ctx.reply(t(gid, 'shop.not_jailed'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < BAIL_COST:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        add_cash(gid, ctx.author.id, -BAIL_COST)
        db.unjail(gid, ctx.author.id)
        await ctx.reply(t(gid, 'shop.free'))

    async def _buy_bail(self, ctx, price: int):
        """Buy your way out through the shop (same as .bail)."""
        from cogs.gamble import bal, set_cash, add_cash
        gid = ctx.guild.id
        if not db.jail_left(gid, ctx.author.id):
            return await ctx.reply(t(gid, 'shop.not_jailed'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if b['cash'] < price:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        add_cash(gid, ctx.author.id, -price)
        db.unjail(gid, ctx.author.id)
        await ctx.reply(t(gid, 'shop.free'))


async def setup(bot):
    await bot.add_cog(Shop(bot))

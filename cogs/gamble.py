"""Gambling economy: balance, daily, blackjack, slots, coinflip, rob, rich. Hybrid."""
import random
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import foot

START_CASH, DAILY_CASH, DAILY_CD = 1000, 500, 86400
ROB_CD = 3600
SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
SLOTS = ['7', '★', '♦', '♣', '●']


def bal(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM eco WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
        if not row:
            conn.execute('INSERT INTO eco (guild_id, user_id, cash) VALUES (?,?,?)',
                         (str(gid), str(uid), START_CASH))
            return {'cash': START_CASH, 'last_daily': 0}
        return dict(row)


def set_cash(gid, uid, cash: int):
    bal(gid, uid)
    with db.conn_ctx() as conn:
        conn.execute('UPDATE eco SET cash=? WHERE guild_id=? AND user_id=?',
                     (cash, str(gid), str(uid)))


def hand_value(cards) -> int:
    total, aces = 0, 0
    for r, _ in cards:
        if r == 'A':
            aces += 1
            total += 11
        elif r in ('J', 'Q', 'K'):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def fmt_hand(cards, hide_first=False) -> str:
    shown = ['??'] + [f'{r}{s}' for r, s in cards[1:]] if hide_first and cards else [f'{r}{s}' for r, s in cards]
    return ' '.join(f'`{c}`' for c in shown)


def slots_image(reels) -> bytes:
    """3 casino cells with big symbols."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, cw, ch, gap, pad = 720, 180, 200, 24, 30
    H = ch + pad * 2
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    try:
        f = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 110)
    except Exception:
        f = _F.load_default()
    for i, s in enumerate(reels):
        x = pad + i * (cw + gap)
        d.rounded_rectangle([x, pad, x + cw, pad + ch], radius=18, fill=(34, 34, 39),
                            outline=(70, 70, 78), width=3)
        try:
            w = d.textlength(s, font=f)
            d.text((x + (cw - w) / 2, pad + 28), s, font=f,
                   fill=(250, 200, 60) if s == '7' else (240, 240, 245))
        except Exception:
            d.text((x + 60, pad + 60), s, font=f, fill=(240, 240, 245))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def bj_table_image(phand, dhand, hide=True) -> bytes:
    """Felt table with real cards. Dealer top, player bottom."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    CW, CHH = 110, 154
    try:
        f_r = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 34)
        f_s = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans.ttf'), 52)
    except Exception:
        f_r = f_s = _F.load_default()

    def card(rank, suit, back=False):
        c = _Img.new('RGB', (CW, CHH), (232, 232, 236))
        d = _Dr.Draw(c)
        d.rounded_rectangle([0, 0, CW - 1, CHH - 1], radius=12, outline=(120, 120, 128), width=3)
        if back:
            d.rounded_rectangle([12, 12, CW - 13, CHH - 13], radius=8, fill=(40, 40, 46))
            for x in range(20, CW - 20, 16):
                d.line([(x, 20), (x, CHH - 20)], fill=(70, 70, 78), width=3)
            return c
        col = (180, 40, 40) if suit in ('♥', '♦') else (25, 25, 30)
        d.text((10, 6), rank, font=f_r, fill=col)
        try:
            w = d.textlength(suit, font=f_s)
            d.text(((CW - w) / 2, 52), suit, font=f_s, fill=col)
        except Exception:
            d.text((40, 60), suit, font=f_r, fill=col)
        return c

    def row(cards, hide_first=False):
        n = max(1, len(cards))
        strip = _Img.new('RGB', (n * (CW + 14), CHH), (22, 22, 26))
        for i, (r, s) in enumerate(cards):
            strip.paste(card(r, s, back=(hide_first and i == 0)), (i * (CW + 14), 0))
        return strip

    prows, drows = row(phand), row(dhand, hide_first=hide)
    W = max(prows.width, drows.width) + 60
    H = CHH * 2 + 150
    img = _Img.new('RGB', (W, H), (22, 22, 26))
    d = _Dr.Draw(img)
    try:
        f_t = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 30)
    except Exception:
        f_t = f_r
    pv, dv = hand_value(phand), '?' if hide else str(hand_value(dhand))
    d.text((30, 14), f'DEALER  {dv}', font=f_t, fill=(160, 160, 168))
    img.paste(drows, (30, 52))
    d.text((30, 52 + CHH + 12), f'YOU  {pv}', font=f_t, fill=(255, 255, 255))
    img.paste(prows, (30, 52 + CHH + 50))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def coin_image(side: str) -> bytes:
    """Big coin: O (orzeł) / R (reszka)."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    S = 300
    img = _Img.new('RGB', (S, S), (16, 16, 19))
    d = _Dr.Draw(img)
    d.ellipse([15, 15, S - 15, S - 15], fill=(34, 34, 39), outline=(250, 200, 60), width=8)
    d.ellipse([35, 35, S - 35, S - 35], outline=(90, 90, 98), width=2)
    try:
        f = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 150)
    except Exception:
        f = _F.load_default()
    try:
        w = d.textlength(side, font=f)
        d.text(((S - w) / 2, 55), side, font=f, fill=(250, 200, 60))
    except Exception:
        d.text((110, 90), side, font=f, fill=(250, 200, 60))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _game_layout(title: str, desc: str, image_url: str = None):
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow, MediaGallery
    from discord.ui.media_gallery import MediaGalleryItem
    layout = LayoutView(timeout=120)
    box = Container(accent_color=0xFFFFFF)
    box.add_item(TextDisplay(f'## {title}\n{desc}'))
    if image_url:
        box.add_item(MediaGallery(MediaGalleryItem(media=image_url)))
    try:
        box.add_item(TextDisplay(f'-# {foot()}'))
    except Exception:
        pass
    layout.add_item(box)
    return layout


def _jailed(gid, uid):
    jl = db.jail_left(gid, uid)
    if jl:
        return t(gid, 'eco.jailed', m=max(1, jl // 60))
    return None


def _wallet_line(gid, uid) -> str:
    try:
        return '\n' + t(gid, 'eco.balance_line', cash=bal(gid, uid)['cash'])
    except Exception:
        return ''


def wallet_card(name: str, cash: int, streak: int, avatar_bytes: bytes = None) -> bytes:
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    from utils.cards import paste_avatar as _paste
    W, H, AV = 800, 220, 150
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=18, outline=(250, 200, 60), width=3)
    if avatar_bytes:
        _paste(img, avatar_bytes, (35, 35, AV))
        d.ellipse([35, 35, 35 + AV, 35 + AV], outline=(250, 200, 60), width=4)
    try:
        f_big = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 64)
        f_mid = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans.ttf'), 32)
    except Exception:
        f_big = f_mid = _F.load_default()
    d.text((220, 35), f'{cash:,}'.replace(',', ' '), font=f_big, fill=(250, 200, 60))
    d.text((220, 115), name[:20], font=f_mid, fill=(255, 255, 255))
    if streak and streak > 1:
        d.text((220, 155), f'{streak}-day streak', font=f_mid, fill=(150, 150, 158))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class BJView(discord.ui.LayoutView):
    def __init__(self, cog, player_id: int, bet: int, deck, phand, dhand, gid):
        super().__init__(timeout=120)
        self.cog, self.player_id, self.bet = cog, player_id, bet
        self.deck, self.phand, self.dhand, self.gid = deck, phand, dhand, gid
        self.done = False
        self._build(True)

    def _build(self, hide=True, extra='', image=True):
        from discord.ui import Container, TextDisplay, ActionRow, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        self.clear_items()
        pv = hand_value(self.phand)
        dv_txt = '?' if hide else str(hand_value(self.dhand))
        cash = bal(self.gid, self.player_id)['cash']
        box = Container(accent_color=0xFFFFFF)
        txt = (f'## {t(self.gid, "eco.bj_title", bet=self.bet)}\n'
               + t(self.gid, 'eco.bj_board', bet=self.bet, phand=fmt_hand(self.phand),
                   pv=pv, dhand=fmt_hand(self.dhand, hide_first=hide), dv=dv_txt, cash=cash))
        if extra:
            txt += f'\n{extra}'
        box.add_item(TextDisplay(txt))
        if image:
            box.add_item(MediaGallery(MediaGalleryItem(media='attachment://bj.png')))
        row = ActionRow()
        for key, lab in (('hit', t(self.gid, 'ui.hit')), ('stand', t(self.gid, 'ui.stand')),
                         ('double', t(self.gid, 'ui.double'))):
            b = discord.ui.Button(label=lab, style=discord.ButtonStyle.grey,
                                  custom_id=f'bj_{key}', disabled=self.done)
            b.callback = getattr(self, f'_cb_{key}')
            row.add_item(b)
        box.add_item(row)
        self.add_item(box)

    async def _table_file(self, hide=True):
        import asyncio
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(None, bj_table_image, list(self.phand), list(self.dhand), hide)
        return discord.File(__import__('io').BytesIO(png), 'bj.png')

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(t(self.gid, 'eco.not_yours'), ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction):
        self.done = True
        pv = hand_value(self.phand)
        while hand_value(self.dhand) < 17:
            self.dhand.append(self.deck.pop())
        dv = hand_value(self.dhand)
        b = bal(self.gid, self.player_id)
        if pv > 21:
            msg = t(self.gid, 'eco.bj_bust', pv=pv)
        elif dv > 21 or pv > dv:
            profit = int(self.bet * 1.5) if pv == 21 and len(self.phand) == 2 else self.bet
            set_cash(self.gid, self.player_id, b['cash'] + self.bet + profit)
            msg = t(self.gid, 'eco.bj_win', pv=pv, dv=dv, win=profit)
        elif pv == dv:
            set_cash(self.gid, self.player_id, b['cash'] + self.bet)  # push refunds stake
            msg = t(self.gid, 'eco.bj_push', pv=pv)
        else:
            msg = t(self.gid, 'eco.bj_lose', pv=pv, dv=dv, bet=self.bet)
        self._build(hide=False, extra=msg)
        await interaction.response.edit_message(view=self, attachments=[await self._table_file(False)])
        self.stop()

    async def _cb_hit(self, interaction: discord.Interaction):
        if not await self._guard(interaction) or self.done:
            return
        self.phand.append(self.deck.pop())
        if hand_value(self.phand) >= 21:
            return await self.finish(interaction)
        self._build(True)
        await interaction.response.edit_message(view=self, attachments=[await self._table_file(True)])

    async def _cb_stand(self, interaction: discord.Interaction):
        if not await self._guard(interaction) or self.done:
            return
        await self.finish(interaction)

    async def _cb_double(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if self.done or len(self.phand) != 2:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        b = bal(self.gid, self.player_id)
        if b['cash'] < self.bet:
            try:
                await interaction.response.send_message(t(self.gid, 'eco.broke', cash=b['cash']), ephemeral=True)
            except Exception:
                pass
            return
        set_cash(self.gid, self.player_id, b['cash'] - self.bet)
        self.bet *= 2
        self.phand.append(self.deck.pop())
        await self.finish(interaction)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            b = bal(self.gid, self.player_id)
            set_cash(self.gid, self.player_id, b['cash'] + self.bet)  # refund


class Gamble(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name='bal', description='Twoja kasa')
    async def balance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        b = bal(ctx.guild.id, member.id)
        try:
            av = await member.display_avatar.with_size(256).read()
        except Exception:
            av = None
        png = await self.bot.loop.run_in_executor(
            None, wallet_card, member.display_name, b['cash'], b.get('daily_streak') or 0, av)
        await ctx.reply(view=_game_layout(t(ctx.guild.id, 'eco.bal_title', user=member.display_name),
                                          t(ctx.guild.id, 'eco.bal', user=member.display_name, cash=b['cash']),
                                          'attachment://wallet.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'wallet.png'),
                        ephemeral=True)

    @commands.hybrid_command(name='daily', description='Dzienne monety')
    async def daily(self, ctx):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        b = bal(gid, ctx.author.id)
        now = int(time.time())
        if now - (b.get('last_daily') or 0) < DAILY_CD:
            left = DAILY_CD - (now - (b.get('last_daily') or 0))
            h, rem = divmod(left, 3600)
            m, _ = divmod(rem, 60)
            return await ctx.reply(t(gid, 'eco.daily_wait', h=h, m=m), ephemeral=True)
        streak = (b.get('daily_streak') or 0) + 1
        bonus = min((streak - 1) * 50, 500)
        set_cash(gid, ctx.author.id, b['cash'] + DAILY_CASH + bonus)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_daily=?, daily_streak=? WHERE guild_id=? AND user_id=?',
                         (now, streak, str(gid), str(ctx.author.id)))
        await ctx.reply(t(gid, 'eco.daily_ok', cash=DAILY_CASH + bonus, streak=streak)
                        + _wallet_line(gid, ctx.author.id), ephemeral=True)

    @commands.hybrid_command(name='pay', description='Przelej kasę')
    async def pay(self, ctx, member: discord.Member, amount: int):
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pay_no'), ephemeral=True)
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        vb = bal(gid, member.id)
        set_cash(gid, ctx.author.id, b['cash'] - amount)
        set_cash(gid, member.id, vb['cash'] + amount)
        await ctx.reply(t(gid, 'eco.pay_ok', user=member.display_name, amount=amount)
                        + _wallet_line(gid, ctx.author.id))

    @staticmethod
    def _bank_accrue(gid, uid) -> tuple:
        """Lazy 2%/day interest, capped +500/day. Returns (bank, earned_now)."""
        b = bal(gid, uid)
        now = int(time.time())
        bank = b.get('bank') or 0
        at = b.get('bank_at') or now
        days = (now - at) // 86400
        earned = 0
        if bank > 0 and days > 0:
            earned = min(int(bank * 0.02 * days), 500 * days)
            bank += earned
            with db.conn_ctx() as conn:
                conn.execute('UPDATE eco SET bank=?, bank_at=? WHERE guild_id=? AND user_id=?',
                             (bank, now, str(gid), str(uid)))
        return bank, earned

    @staticmethod
    def _vault(gid, uid) -> tuple:
        """Resolve joint vault: (owner_uid, partner_name_or_None)."""
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT u1, u2 FROM bank_links WHERE guild_id=? AND (u1=? OR u2=?)',
                               (str(gid), str(uid), str(uid))).fetchone()
        if not row:
            return str(uid), None
        other = row['u2'] if row['u1'] == str(uid) else row['u1']
        return row['u1'], other

    @commands.hybrid_command(name='bank', description='Twój bank')
    async def bank(self, ctx):
        gid = ctx.guild.id
        owner, partner = self._vault(gid, ctx.author.id)
        bank, earned = self._bank_accrue(gid, owner)
        b = bal(gid, ctx.author.id)
        msg = t(gid, 'eco.bank_view', cash=b['cash'], bank=bank)
        if partner:
            m = ctx.guild.get_member(int(partner))
            msg += '\n' + t(gid, 'eco.bank_joint', user=(m.display_name if m else '?'))
        if earned:
            msg += '\n' + t(gid, 'eco.bank_interest', earned=earned)
        await ctx.reply(view=_game_layout(t(gid, 'eco.bank_title'), msg), ephemeral=True)

    @commands.hybrid_command(name='bankshare', description='Wspólny sejf we dwoje')
    async def bankshare(self, ctx, partner: discord.Member = None):
        gid = ctx.guild.id
        me = str(ctx.author.id)
        with db.conn_ctx() as conn:
            cur = conn.execute('SELECT u1, u2 FROM bank_links WHERE guild_id=? AND (u1=? OR u2=?)',
                               (str(gid), me, me)).fetchone()
            if partner is None:
                if not cur:
                    return await ctx.reply(t(gid, 'eco.share_none'), ephemeral=True)
                conn.execute('DELETE FROM bank_links WHERE guild_id=? AND u1=?', (str(gid), cur['u1']))
                return await ctx.reply(t(gid, 'eco.share_end'), ephemeral=True)
            if partner.id == ctx.author.id or partner.bot:
                return await ctx.reply(t(gid, 'eco.share_no'), ephemeral=True)
            busy = conn.execute('SELECT 1 FROM bank_links WHERE guild_id=? AND (u1=? OR u2=? OR u1=? OR u2=?)',
                                (str(gid), me, me, str(partner.id), str(partner.id))).fetchone()
            if busy:
                return await ctx.reply(t(gid, 'eco.share_busy'), ephemeral=True)
            a, b = sorted((me, str(partner.id)))
            conn.execute('INSERT INTO bank_links (guild_id, u1, u2) VALUES (?,?,?)', (str(gid), a, b))
        await ctx.reply(t(gid, 'eco.share_ok', user=partner.display_name), ephemeral=True)

    @commands.hybrid_command(name='deposit', description='Wpłać do banku', aliases=['dep'])
    async def deposit(self, ctx, amount: str):
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        owner, _ = self._vault(gid, ctx.author.id)
        bank, _ = self._bank_accrue(gid, owner)
        if (amount or '').lower() == 'all':
            amount = b['cash']
        else:
            try:
                amount = int(amount)
            except (ValueError, TypeError):
                return await ctx.reply(t(gid, 'eco.dep_use'), ephemeral=True)
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        now = int(time.time())
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET cash=? WHERE guild_id=? AND user_id=?',
                         (b['cash'] - amount, str(gid), str(ctx.author.id)))
            conn.execute('UPDATE eco SET bank=?, bank_at=? WHERE guild_id=? AND user_id=?',
                         (bank + amount, now, str(gid), str(owner)))
        await ctx.reply(t(gid, 'eco.dep_ok', amount=amount, bank=bank + amount)
                        + _wallet_line(gid, ctx.author.id), ephemeral=True)

    @commands.hybrid_command(name='withdraw', description='Wypłać z banku', aliases=['with'])
    async def withdraw(self, ctx, amount: str):
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        owner, _ = self._vault(gid, ctx.author.id)
        bank, _ = self._bank_accrue(gid, owner)
        if (amount or '').lower() == 'all':
            amount = bank
        else:
            try:
                amount = int(amount)
            except (ValueError, TypeError):
                return await ctx.reply(t(gid, 'eco.dep_use'), ephemeral=True)
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        if amount > bank:
            return await ctx.reply(t(gid, 'eco.bank_poor', bank=bank), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET cash=? WHERE guild_id=? AND user_id=?',
                         (b['cash'] + amount, str(gid), str(ctx.author.id)))
            conn.execute('UPDATE eco SET bank=? WHERE guild_id=? AND user_id=?',
                         (bank - amount, str(gid), str(owner)))
        await ctx.reply(t(gid, 'eco.with_ok', amount=amount) + _wallet_line(gid, ctx.author.id),
                        ephemeral=True)

    @commands.hybrid_command(name='tribute', description='Daj babce napiwek')
    async def tribute(self, ctx, amount: int):
        import random
        gid = ctx.guild.id
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - amount)
        with db.conn_ctx() as conn:
            conn.execute('''INSERT INTO tributes (guild_id, user_id, total) VALUES (?,?,?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET total=total+excluded.total''',
                         (str(gid), str(ctx.author.id), amount))
            total = conn.execute('SELECT total FROM tributes WHERE guild_id=? AND user_id=?',
                                 (str(gid), str(ctx.author.id))).fetchone()['total']
        thanks = random.choice(t(gid, 'eco.tribute_lines').split('|'))
        await ctx.reply(view=_game_layout(t(gid, 'eco.tribute_title'),
                                          thanks + '\n' + t(gid, 'eco.tribute_total', total=total)))

    @commands.hybrid_command(name='blackjack', description='Oczko', aliases=['bj'])
    async def blackjack(self, ctx, bet: int):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        if bet <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if bet > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - bet)
        deck = [(r, s) for s in SUITS for r in RANKS]
        random.shuffle(deck)
        phand = [deck.pop(), deck.pop()]
        dhand = [deck.pop(), deck.pop()]
        if hand_value(phand) == 21:
            win = int(bet * 1.5)
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + win)
            view = BJView(self, ctx.author.id, bet, deck, phand, dhand, gid)
            view.done = True
            view._build(hide=False, extra=t(gid, 'eco.bj_natural', win=win))
            return await ctx.reply(view=view, files=[await view._table_file(False)])
        view = BJView(self, ctx.author.id, bet, deck, phand, dhand, gid)
        await ctx.reply(view=view, files=[await view._table_file(True)])

    def _take_bet(self, ctx, bet: int):
        gid = ctx.guild.id
        if bet <= 0:
            return None, t(gid, 'eco.bet_pos')
        b = bal(gid, ctx.author.id)
        if bet > b['cash']:
            return None, t(gid, 'eco.broke', cash=b['cash'])
        set_cash(gid, ctx.author.id, b['cash'] - bet)
        return b, None

    @commands.hybrid_command(name='slots', description='Maszynka')
    async def slots(self, ctx, bet: int):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        reels = [random.choice(SLOTS) for _ in range(3)]
        if reels[0] == reels[1] == reels[2]:
            mult = 12 if reels[0] == '7' else 5
            win = bet * mult
            msg = t(gid, 'eco.slots_jackpot', mult=mult, win=win)
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            win = bet
            msg = t(gid, 'eco.slots_small', win=win)
        else:
            win = 0
            msg = t(gid, 'eco.slots_lose', bet=bet)
        msg += _wallet_line(gid, ctx.author.id)
        if win:
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + win)
        import asyncio as _aio
        spin = await ctx.reply(view=_game_layout(t(gid, 'eco.slots_title', bet=bet),
                                                 t(gid, 'eco.spinning')))
        for _ in range(2):
            await _aio.sleep(0.7)
            try:
                fake = await self.bot.loop.run_in_executor(
                    None, slots_image, [random.choice(SLOTS) for _ in range(3)])
                await spin.edit(view=_game_layout(t(gid, 'eco.slots_title', bet=bet),
                                                  t(gid, 'eco.spinning')),
                                attachments=[discord.File(__import__('io').BytesIO(fake), 'slots.png')])
            except Exception:
                break
        await _aio.sleep(0.7)
        png = await self.bot.loop.run_in_executor(None, slots_image, reels)
        try:
            await spin.edit(view=_game_layout(t(gid, 'eco.slots_title', bet=bet), msg,
                                              'attachment://slots.png'),
                            attachments=[discord.File(__import__('io').BytesIO(png), 'slots.png')])
        except Exception:
            await ctx.reply(view=_game_layout(t(gid, 'eco.slots_title', bet=bet), msg,
                                              'attachment://slots.png'),
                            file=discord.File(__import__('io').BytesIO(png), 'slots.png'))

    @commands.hybrid_command(name='coinflip', description='Orzeł czy reszka', aliases=['moneta'])
    async def coinflip(self, ctx, bet: int, side: str):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        side = (side or '').lower()
        pick = 'O' if side.startswith(('o', 'e', 'h')) else ('R' if side.startswith(('r', 't')) else None)
        if pick is None:
            return await ctx.reply(t(gid, 'eco.cf_use'), ephemeral=True)
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        result = random.choice(['O', 'R'])
        if result == pick:
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet * 2)
            msg = t(gid, 'eco.cf_win', win=bet)
        else:
            msg = t(gid, 'eco.cf_lose', bet=bet)
        msg += _wallet_line(gid, ctx.author.id)
        import asyncio as _aio2
        flip = await ctx.reply(view=_game_layout(t(gid, 'eco.cf_title', bet=bet),
                                                 t(gid, 'eco.flipping')))
        for face in ('O', '|', 'R', '|'):
            await _aio2.sleep(0.45)
            try:
                fake = await self.bot.loop.run_in_executor(None, coin_image, face if face != '|' else 'O')
                await flip.edit(view=_game_layout(t(gid, 'eco.cf_title', bet=bet),
                                                  t(gid, 'eco.flipping'), 'attachment://coin.png'),
                                attachments=[discord.File(__import__('io').BytesIO(fake), 'coin.png')])
            except Exception:
                break
        png = await self.bot.loop.run_in_executor(None, coin_image, result)
        try:
            await flip.edit(view=_game_layout(t(gid, 'eco.cf_title', bet=bet), msg,
                                              'attachment://coin.png'),
                            attachments=[discord.File(__import__('io').BytesIO(png), 'coin.png')])
        except Exception:
            await ctx.reply(view=_game_layout(t(gid, 'eco.cf_title', bet=bet), msg,
                                              'attachment://coin.png'),
                            file=discord.File(__import__('io').BytesIO(png), 'coin.png'))

    @commands.hybrid_command(name='rob', description='Okradnij typa')
    async def rob(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        if member.id == ctx.author.id:
            return await ctx.reply(t(gid, 'eco.rob_self'), ephemeral=True)
        if member.bot:
            return await ctx.reply(t(gid, 'eco.rob_bot'), ephemeral=True)
        now = int(time.time())
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT last_rob FROM eco WHERE guild_id=? AND user_id=?',
                               (str(gid), str(ctx.author.id))).fetchone()
            last = (dict(row).get('last_rob') if row else 0) or 0
            if now - last < ROB_CD:
                m = (ROB_CD - (now - last)) // 60
                return await ctx.reply(t(gid, 'eco.rob_wait', m=m), ephemeral=True)
        vb = bal(gid, member.id)
        if vb['cash'] < 100:
            return await ctx.reply(t(gid, 'eco.rob_poor', user=member.display_name), ephemeral=True)
        if db.has_shield(gid, member.id):
            return await ctx.reply(t(gid, 'eco.rob_shield', user=member.display_name), ephemeral=True)
        win_chance = 0.45
        won = random.random() < win_chance
        if won:
            loot = max(10, int(vb['cash'] * random.uniform(0.1, 0.3)))
            ab = bal(gid, ctx.author.id)
            set_cash(gid, member.id, vb['cash'] - loot)
            set_cash(gid, ctx.author.id, ab['cash'] + loot)
            msg = t(gid, 'eco.rob_win', user=member.display_name, loot=loot)
        else:
            fine = min(bal(gid, ctx.author.id)['cash'], max(50, int(vb['cash'] * 0.15)))
            ab = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, ab['cash'] - fine)
            set_cash(gid, member.id, vb['cash'] + fine)
            db.jail(gid, ctx.author.id, 10)
            msg = t(gid, 'eco.rob_fail_jail', user=member.display_name, fine=fine)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_rob=? WHERE guild_id=? AND user_id=?',
                         (now, str(gid), str(ctx.author.id)))
        if won:
            with db.conn_ctx() as conn:
                bounty = conn.execute('SELECT id, amount FROM bounties WHERE guild_id=? AND target_id=?',
                                      (str(gid), str(member.id))).fetchone()
                if bounty:
                    conn.execute('DELETE FROM bounties WHERE id=?', (bounty['id'],))
                    ab2 = bal(gid, ctx.author.id)
                    set_cash(gid, ctx.author.id, ab2['cash'] + bounty['amount'])
                    msg += '\n' + t(gid, 'eco.bounty_claim', amount=bounty['amount'])
        msg += _wallet_line(gid, ctx.author.id)
        await ctx.reply(view=_game_layout(t(gid, 'eco.rob_title'), msg))

    @commands.command(name='ecoreset')
    async def ecoreset(self, ctx, confirm: str = ''):
        gid = ctx.guild.id
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
        if confirm.lower() != 'yes':
            return await ctx.reply(t(gid, 'eco.reset_warn'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET cash=0, bank=0 WHERE guild_id=?', (str(gid),))
            conn.execute('DELETE FROM bounties WHERE guild_id=?', (str(gid),))
        await ctx.reply(t(gid, 'eco.reset_done'))

    @commands.hybrid_command(name='rich', description='Najbogatsi', aliases=['baltop'])
    async def rich(self, ctx):
        gid = ctx.guild.id
        await ctx.defer()
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT user_id, cash FROM eco WHERE guild_id=? ORDER BY cash DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows or rows[0]['cash'] <= 0:
            return await ctx.reply(t(gid, 'eco.rich_empty'), ephemeral=True)
        top = rows[0]['cash']
        board = []
        for i, r in enumerate(rows, start=1):
            m = ctx.guild.get_member(int(r['user_id']))
            name = m.display_name if m else f"user{r['user_id']}"[:16]
            try:
                av = await m.display_avatar.with_size(128).read() if m else None
            except Exception:
                av = None
            board.append((i, name[:20], '', r['cash'] / top if top else 0,
                          f"{r['cash']:,}".replace(',', ' ') + ' monet', None, av))
        from cogs.fitcheck import board_image as _board
        png = await self.bot.loop.run_in_executor(None, _board, board)
        await ctx.reply(view=_game_layout(t(gid, 'eco.rich_title'),
                                          t(gid, 'eco.rich_sub', n=len(board)), 'attachment://rich.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'rich.png'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Gamble(bot))

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


class BJView(discord.ui.LayoutView):
    def __init__(self, cog, player_id: int, bet: int, deck, phand, dhand, gid):
        super().__init__(timeout=120)
        self.cog, self.player_id, self.bet = cog, player_id, bet
        self.deck, self.phand, self.dhand, self.gid = deck, phand, dhand, gid
        self.done = False
        self._build(True)

    def _build(self, hide=True):
        from discord.ui import Container, TextDisplay, ActionRow
        self.clear_items()
        pv = hand_value(self.phand)
        dv_txt = '?' if hide else str(hand_value(self.dhand))
        cash = bal(self.gid, self.player_id)['cash']
        box = Container(accent_color=0xFFFFFF)
        box.add_item(TextDisplay(
            f'## {t(self.gid, "eco.bj_title", bet=self.bet)}\n'
            + t(self.gid, 'eco.bj_board', bet=self.bet, phand=fmt_hand(self.phand),
                  pv=pv, dhand=fmt_hand(self.dhand, hide_first=hide), dv=dv_txt, cash=cash)))
        row = ActionRow()
        for key, lab in (('hit', t(self.gid, 'ui.hit')), ('stand', t(self.gid, 'ui.stand')),
                         ('double', t(self.gid, 'ui.double'))):
            b = discord.ui.Button(label=lab, style=discord.ButtonStyle.grey,
                                  custom_id=f'bj_{key}', disabled=self.done)
            b.callback = getattr(self, f'_cb_{key}')
            row.add_item(b)
        box.add_item(row)
        self.add_item(box)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(t(self.gid, 'eco.not_yours'), ephemeral=True)
            return False
        return True

    def _extra(self, text: str, hide=False):
        from discord.ui import Container, TextDisplay
        for child in self.children:
            if type(child).__name__ == 'Container':
                for item in child.children:
                    if type(item).__name__ == 'TextDisplay':
                        item.content = item.content + f'\n{text}'
                        return

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
            win = int(self.bet * 1.5) if pv == 21 and len(self.phand) == 2 else self.bet
            set_cash(self.gid, self.player_id, b['cash'] + win)
            msg = t(self.gid, 'eco.bj_win', pv=pv, dv=dv, win=win)
        elif pv == dv:
            msg = t(self.gid, 'eco.bj_push', pv=pv)
        else:
            msg = t(self.gid, 'eco.bj_lose', pv=pv, dv=dv, bet=self.bet)
        self._build(hide=False)
        self._extra(msg)
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _cb_hit(self, interaction: discord.Interaction):
        if not await self._guard(interaction) or self.done:
            return
        self.phand.append(self.deck.pop())
        if hand_value(self.phand) >= 21:
            return await self.finish(interaction)
        self._build(True)
        await interaction.response.edit_message(view=self)

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
        await ctx.reply(t(ctx.guild.id, 'eco.bal', user=member.display_name, cash=b['cash']), ephemeral=True)

    @commands.hybrid_command(name='daily', description='Dzienne monety')
    async def daily(self, ctx):
        gid = ctx.guild.id
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
        await ctx.reply(t(gid, 'eco.daily_ok', cash=DAILY_CASH + bonus, streak=streak), ephemeral=True)

    @commands.hybrid_command(name='work', description='Uczciwa robota')
    async def work(self, ctx):
        gid = ctx.guild.id
        b = bal(gid, ctx.author.id)
        now = int(time.time())
        if now - (b.get('last_work') or 0) < 3600:
            m = (3600 - (now - (b.get('last_work') or 0))) // 60
            return await ctx.reply(t(gid, 'eco.work_wait', m=m), ephemeral=True)
        jobs = t(gid, 'eco.jobs').split('|')
        job = random.choice(jobs).strip()
        pay = random.randint(100, 300)
        set_cash(gid, ctx.author.id, b['cash'] + pay)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_work=? WHERE guild_id=? AND user_id=?',
                         (now, str(gid), str(ctx.author.id)))
        await ctx.reply(t(gid, 'eco.work_done', job=job, pay=pay))

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
        await ctx.reply(t(gid, 'eco.pay_ok', user=member.display_name, amount=amount))

    @commands.hybrid_command(name='blackjack', description='Oczko', aliases=['bj'])
    async def blackjack(self, ctx, bet: int):
        gid = ctx.guild.id
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
            view._build(hide=False)
            view._extra(t(gid, 'eco.bj_natural', win=win))
            return await ctx.reply(view=view)
        view = BJView(self, ctx.author.id, bet, deck, phand, dhand, gid)
        await ctx.reply(view=view)

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
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        reels = [random.choice(SLOTS) for _ in range(3)]
        if reels[0] == reels[1] == reels[2]:
            mult = 10 if reels[0] == '7' else 4
            win = bet * mult
            msg = t(gid, 'eco.slots_jackpot', mult=mult, win=win)
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            win = int(bet * 0.5)
            msg = t(gid, 'eco.slots_small', win=win)
        else:
            win = 0
            msg = t(gid, 'eco.slots_lose', bet=bet)
        if win:
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + win)
        png = await self.bot.loop.run_in_executor(None, slots_image, reels)
        await ctx.reply(view=_game_layout(t(gid, 'eco.slots_title', bet=bet), msg, 'attachment://slots.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'slots.png'))

    @commands.hybrid_command(name='coinflip', description='Orzeł czy reszka', aliases=['moneta'])
    async def coinflip(self, ctx, bet: int, side: str):
        gid = ctx.guild.id
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
        png = await self.bot.loop.run_in_executor(None, coin_image, result)
        await ctx.reply(view=_game_layout(t(gid, 'eco.cf_title', bet=bet), msg, 'attachment://coin.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'coin.png'))

    @commands.hybrid_command(name='rob', description='Okradnij typa')
    async def rob(self, ctx, member: discord.Member):
        gid = ctx.guild.id
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
        if random.random() < 0.45:
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
            msg = t(gid, 'eco.rob_fail', user=member.display_name, fine=fine)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_rob=? WHERE guild_id=? AND user_id=?',
                         (now, str(gid), str(ctx.author.id)))
        await ctx.reply(view=_game_layout(t(gid, 'eco.rob_title'), msg))

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
            board.append((i, name[:20], f"{r['cash']:,}".replace(',', ' '), r['cash'] / top if top else 0,
                          f"{r['cash']:,}".replace(',', ' ') + ' monet', av))
        from cogs.fitcheck import board_image as _board
        png = await self.bot.loop.run_in_executor(None, _board, board)
        await ctx.reply(view=_game_layout(t(gid, 'eco.rich_title'),
                                          t(gid, 'eco.rich_sub', n=len(board)), 'attachment://rich.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'rich.png'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Gamble(bot))

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
# blackjack anti-abuse: no more 100k wins
BJ_MAX_BET = 2000   # gods (house) exempt
BJ_MAX_WIN = 10000  # max profit per hand for mortals
BJ_CD = 180         # seconds between hands for mortals
SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
SLOTS = ['7', '★', '♦', '♣', '●']
# European roulette reds; 0 is green, rest black
ROU_REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
# mortals only win ~3% of the spins they'd fairly win; the house always wins
ROU_RIG = 0.97
# single-zero wheel order (clockwise)
WHEEL_ORDER = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30,
               8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7,
               28, 12, 35, 3, 26]
# bet kind -> profit multiplier
ROU_PAY = {'number': 35, 'dozen1': 2, 'dozen2': 2, 'dozen3': 2,
           'col1': 2, 'col2': 2, 'col3': 2}


def roulette_image(n: int) -> bytes:
    """European wheel with the ball sitting on the winning pocket."""
    import io as _io
    import math as _m
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    S = 520
    img = _Img.new('RGB', (S, S), (16, 16, 19))
    d = _Dr.Draw(img)
    cx = cy = S // 2
    R = S // 2 - 14
    step = 360 / 37
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_n = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 17)
        f_c = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 44)
    except Exception:
        f_n = f_c = _F.load_default()
    d.ellipse([cx - R - 6, cy - R - 6, cx + R + 6, cy + R + 6], outline=(250, 200, 60), width=6)
    try:
        idx = WHEEL_ORDER.index(n)
    except ValueError:
        idx = 0
    for i, num in enumerate(WHEEL_ORDER):
        a0, a1 = i * step - 90, (i + 1) * step - 90
        col = (20, 140, 60) if num == 0 else ((180, 40, 40) if num in ROU_REDS else (25, 25, 30))
        d.pieslice([cx - R, cy - R, cx + R, cy + R], a0, a1, fill=col, outline=(200, 200, 208), width=1)
        mid = _m.radians((a0 + a1) / 2)
        tx, ty = cx + int(_m.cos(mid) * (R - 42)), cy + int(_m.sin(mid) * (R - 42))
        txt = str(num)
        try:
            w = d.textlength(txt, font=f_n)
            d.text((tx - w / 2, ty - 9), txt, font=f_n, fill=(255, 255, 255))
        except Exception:
            d.text((tx - 8, ty - 9), txt, font=f_n, fill=(255, 255, 255))
    # hub
    d.ellipse([cx - 62, cy - 62, cx + 62, cy + 62], fill=(34, 34, 39), outline=(250, 200, 60), width=4)
    try:
        w = d.textlength(str(n), font=f_c)
        d.text((cx - w / 2, cy - 26), str(n), font=f_c, fill=(250, 200, 60))
    except Exception:
        d.text((cx - 14, cy - 24), str(n), font=f_c, fill=(250, 200, 60))
    # ball on the winning pocket
    mid = _m.radians(idx * step + step / 2 - 90)
    bx, by = cx + int(_m.cos(mid) * (R - 16)), cy + int(_m.sin(mid) * (R - 16))
    d.ellipse([bx - 11, by - 11, bx + 11, by + 11], fill=(250, 250, 245), outline=(250, 200, 60), width=3)
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()
# house always wins: these users get ~100% win chance on every game of chance
GOD_IDS = {'1270782781605154922'}


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


POKER_PAY = {
    'ROYAL': 250, 'STRAIGHT_FLUSH': 50, 'FOUR': 25, 'FULL_HOUSE': 9,
    'FLUSH': 6, 'STRAIGHT': 4, 'THREE': 3, 'TWO_PAIR': 2, 'JACKS': 1,
}


def poker_eval(cards) -> tuple:
    """cards = [(rank, suit)]. Returns (key, profit_mult)."""
    def _v(r):
        return {'J': 11, 'Q': 12, 'K': 13, 'A': 14}.get(r, int(r) if str(r).isdigit() else 0)
    vals = sorted([_v(r) for r, _ in cards])
    suits = [s for _, s in cards]
    flush = len(set(suits)) == 1
    straight = vals == list(range(vals[0], vals[0] + 5)) or vals == [2, 3, 4, 5, 14]
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    groups = sorted(counts.values(), reverse=True)
    if flush and vals == [10, 11, 12, 13, 14]:
        return 'ROYAL', POKER_PAY['ROYAL']
    if flush and straight:
        return 'STRAIGHT_FLUSH', POKER_PAY['STRAIGHT_FLUSH']
    if groups[0] == 4:
        return 'FOUR', POKER_PAY['FOUR']
    if groups == [3, 2]:
        return 'FULL_HOUSE', POKER_PAY['FULL_HOUSE']
    if flush:
        return 'FLUSH', POKER_PAY['FLUSH']
    if straight:
        return 'STRAIGHT', POKER_PAY['STRAIGHT']
    if groups[0] == 3:
        return 'THREE', POKER_PAY['THREE']
    if groups[:2] == [2, 2]:
        return 'TWO_PAIR', POKER_PAY['TWO_PAIR']
    if groups[0] == 2:
        pair_val = max(v for v, c in counts.items() if c == 2)
        if pair_val >= 11:
            return 'JACKS', POKER_PAY['JACKS']
    return 'NOTHING', 0


def poker_image(hand, held) -> bytes:
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    CW, CHH = 110, 154
    try:
        f_r = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 32)
        f_s = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans.ttf'), 48)
        f_h = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 26)
    except Exception:
        f_r = f_s = f_h = _F.load_default()
    W = 5 * (CW + 12) + 40
    H = CHH + 90
    img = _Img.new('RGB', (W, H), (22, 22, 26))
    d = _Dr.Draw(img)
    for i, (r, s) in enumerate(hand):
        x = 20 + i * (CW + 12)
        col = (180, 40, 40) if s in ('♥', '♦') else (25, 25, 30)
        card = _Img.new('RGB', (CW, CHH), (232, 232, 236))
        cd = _Dr.Draw(card)
        cd.rounded_rectangle([0, 0, CW - 1, CHH - 1], radius=12,
                             outline=(250, 200, 60) if i in held else (120, 120, 128), width=4)
        cd.text((10, 6), r, font=f_r, fill=col)
        try:
            w = cd.textlength(s, font=f_s)
            cd.text(((CW - w) / 2, 50), s, font=f_s, fill=col)
        except Exception:
            cd.text((38, 58), s, font=f_r, fill=col)
        img.paste(card, (x, 10))
        tag = f'HOLD {i + 1}' if i in held else f'{i + 1}'
        try:
            tw = d.textlength(tag, font=f_h)
            d.text((x + (CW - tw) / 2, 10 + CHH + 8), tag, font=f_h,
                   fill=(250, 200, 60) if i in held else (120, 120, 128))
        except Exception:
            pass
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


def wallet_card(name: str, cash: int, streak: int, avatar_bytes: bytes = None, bank: int = 0) -> bytes:
    """Direction C — minimal/terminal wallet: gold top rule, giant balance,
    quiet streak/bank row, mono avatar right."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, H = 800, 220
    tier_c = (250, 200, 60)
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    d.rectangle([0, 0, W, 3], fill=tier_c)
    if avatar_bytes:
        try:
            ax, ay, s = W - 120 - 44, (H - 120) // 2, 120
            av = _Img.open(_io.BytesIO(avatar_bytes)).convert('L').convert('RGB').resize((s, s))
            mask = _Img.new('L', (s, s), 0)
            _Dr.Draw(mask).ellipse([0, 0, s, s], fill=255)
            img.paste(av, (ax, ay), mask)
            d.ellipse([ax, ay, ax + s, ay + s], outline=(58, 58, 64), width=5)
        except Exception:
            pass
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_big = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 66)
        f_lab = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 18)
        f_mid = _F.truetype(str(_a / 'DejaVuSans.ttf'), 26)
        f_row = _F.truetype(str(_a / 'DejaVuSans.ttf'), 20)
    except Exception:
        try:
            f_big = _F.truetype('arialbd.ttf', 66)
            f_lab = _F.truetype('arialbd.ttf', 18)
            f_mid = _F.truetype('arial.ttf', 26)
            f_row = _F.truetype('arial.ttf', 20)
        except Exception:
            f_big = f_lab = f_mid = f_row = _F.load_default()
    d.text((48, 28), 'WALLET', font=f_lab, fill=(96, 96, 104))
    d.text((48, 58), f'{cash:,}'.replace(',', ' '), font=f_big, fill=tier_c)
    d.text((50, 142), name[:20], font=f_mid, fill=(255, 255, 255))
    parts = []
    if streak and streak > 1:
        parts.append(f'{streak} DAY STREAK')
    if bank:
        parts.append(f'BANK {bank:,}'.replace(',', ' '))
    if parts:
        d.text((50, 182), '   ·   '.join(parts), font=f_row, fill=(150, 150, 158))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _rou_wins(n: int, kind: str, num: int) -> bool:
    if kind == 'number':
        return n == num
    if n == 0:
        return False
    if kind == 'red':
        return n in ROU_REDS
    if kind == 'black':
        return n not in ROU_REDS
    if kind == 'odd':
        return n % 2 == 1
    if kind == 'even':
        return n % 2 == 0
    if kind == 'high':
        return n >= 19
    if kind == 'low':
        return n <= 18
    if kind == 'dozen1':
        return 1 <= n <= 12
    if kind == 'dozen2':
        return 13 <= n <= 24
    if kind == 'dozen3':
        return 25 <= n <= 36
    if kind == 'col1':
        return n % 3 == 1
    if kind == 'col2':
        return n % 3 == 2
    if kind == 'col3':
        return n % 3 == 0
    return False


def _roulette_spin(kind: str, num: int, god: bool) -> int:
    if god:
        # house always lands on a winner
        if kind == 'number':
            return num
        if kind == 'red':
            return random.choice(sorted(ROU_REDS))
        if kind == 'black':
            return random.choice([x for x in range(1, 37) if x not in ROU_REDS])
        if kind == 'odd':
            return random.choice([x for x in range(1, 37, 2)])
        if kind == 'even':
            return random.choice([x for x in range(2, 37, 2)])
        if kind == 'high':
            return random.randint(19, 36)
        if kind == 'low':
            return random.randint(1, 18)
        if kind == 'dozen1':
            return random.randint(1, 12)
        if kind == 'dozen2':
            return random.randint(13, 24)
        if kind == 'dozen3':
            return random.randint(25, 36)
        if kind == 'col1':
            return random.choice([x for x in range(1, 37) if x % 3 == 1])
        if kind == 'col2':
            return random.choice([x for x in range(1, 37) if x % 3 == 2])
        return random.choice([x for x in range(1, 37) if x % 3 == 0])  # col3
    n = random.randint(0, 36)
    if _rou_wins(n, kind, num) and random.random() < ROU_RIG:
        losers = [x for x in range(37) if not _rou_wins(x, kind, num)]
        n = random.choice(losers)
    return n


def _rou_ball(n: int) -> str:
    if n == 0:
        return '🟢 **0**'
    return f"{'🔴' if n in ROU_REDS else '⚫'} **{n}**"


class PokerView(discord.ui.LayoutView):
    def __init__(self, cog, player_id: int, bet: int, deck, hand, gid):
        super().__init__(timeout=120)
        self.cog, self.player_id, self.bet = cog, player_id, bet
        self.deck, self.hand, self.held, self.gid = deck, hand, set(), gid
        self.done = False
        self._build()

    def _build(self, result: str = None):
        from discord.ui import Container, TextDisplay, ActionRow, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        self.clear_items()
        box = Container(accent_color=0xFFFFFF)
        txt = f'## {t(self.gid, "eco.poker_title", bet=self.bet)}'
        if result:
            txt += f'\n{result}'
        box.add_item(TextDisplay(txt))
        box.add_item(MediaGallery(MediaGalleryItem(media='attachment://poker.png')))
        row = ActionRow()
        for i in range(5):
            b = discord.ui.Button(label=f'{i + 1}' + ('*' if i in self.held else ''),
                                  style=discord.ButtonStyle.grey, custom_id=f'pk_{i}',
                                  disabled=self.done)
            b.callback = self._mk_hold(i)
            row.add_item(b)
        box.add_item(row)
        row2 = ActionRow()
        draw = discord.ui.Button(label=t(self.gid, 'eco.poker_draw'), style=discord.ButtonStyle.grey,
                                 custom_id='pk_draw', disabled=self.done)
        draw.callback = self._cb_draw
        row2.add_item(draw)
        box.add_item(row2)
        self.add_item(box)

    def _mk_hold(self, i: int):
        async def _cb(interaction: discord.Interaction):
            if interaction.user.id != self.player_id:
                return await interaction.response.send_message(
                    t(self.gid, 'eco.not_yours'), ephemeral=True)
            if self.done:
                return
            if i in self.held:
                self.held.discard(i)
            else:
                self.held.add(i)
            self._build()
            await interaction.response.edit_message(
                view=self, attachments=[await self._img()])
        return _cb

    async def _img(self):
        import asyncio
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(None, poker_image, list(self.hand), set(self.held))
        return discord.File(__import__('io').BytesIO(png), 'poker.png')

    async def _cb_draw(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_id:
            return await interaction.response.send_message(
                t(self.gid, 'eco.not_yours'), ephemeral=True)
        if self.done:
            return
        self.done = True
        for i in range(5):
            if i not in self.held:
                self.hand[i] = self.deck.pop()
        key, mult = poker_eval(self.hand)
        b = bal(self.gid, self.player_id)
        if mult:
            profit = self.bet * mult
            set_cash(self.gid, self.player_id, b['cash'] + self.bet + profit)
            msg = t(self.gid, 'eco.poker_win', hand=key.replace('_', ' '), win=profit)
        else:
            msg = t(self.gid, 'eco.poker_lose', bet=self.bet)
        msg += _wallet_line(self.gid, self.player_id)
        self._build(msg)
        await interaction.response.edit_message(
            view=self, attachments=[await self._img()])
        self.stop()

    async def on_timeout(self):
        if not self.done:
            self.done = True
            b = bal(self.gid, self.player_id)
            set_cash(self.gid, self.player_id, b['cash'] + self.bet)  # refund


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
        god = str(self.player_id) in GOD_IDS
        pv = hand_value(self.phand)
        while hand_value(self.dhand) < 17:
            self.dhand.append(self.deck.pop())
        dv = hand_value(self.dhand)
        b = bal(self.gid, self.player_id)
        if pv > 21:
            msg = t(self.gid, 'eco.bj_bust', pv=pv)
        elif dv > 21 or pv > dv:
            if pv == 21 and len(self.phand) == 2:
                profit = int(self.bet * (1.5 if god else 1.2))  # mortals get 6:5
            else:
                profit = self.bet
            if not god:
                profit = min(profit, BJ_MAX_WIN)
            set_cash(self.gid, self.player_id, b['cash'] + self.bet + profit)
            msg = t(self.gid, 'eco.bj_win', pv=pv, dv=dv, win=profit)
        elif pv == dv:
            if god:
                set_cash(self.gid, self.player_id, b['cash'] + self.bet)  # push refunds stake
                msg = t(self.gid, 'eco.bj_push', pv=pv)
            else:
                msg = t(self.gid, 'eco.bj_lose', pv=pv, dv=dv, bet=self.bet)  # house wins ties
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
        self._bj_cd = {}   # (gid, uid) -> timestamp of last hand (mortals only)
        self._rou_hist = {}  # gid -> last 8 winning numbers

    @commands.hybrid_command(name='bal', description='Twoja kasa')
    async def balance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        b = bal(ctx.guild.id, member.id)
        try:
            av = await member.display_avatar.with_size(256).read()
        except Exception:
            av = None
        png = await self.bot.loop.run_in_executor(
            None, wallet_card, member.display_name, b['cash'], b.get('daily_streak') or 0, av,
            b.get('bank') or 0)
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
        god = str(ctx.author.id) in GOD_IDS
        if bet <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        if not god:
            if bet > BJ_MAX_BET:
                return await ctx.reply(t(gid, 'eco.bj_maxbet', max=BJ_MAX_BET), ephemeral=True)
            last = self._bj_cd.get((str(gid), str(ctx.author.id)), 0)
            wait = BJ_CD - (int(time.time()) - last)
            if wait > 0:
                m, s = divmod(wait, 60)
                return await ctx.reply(t(gid, 'eco.bj_wait', m=m, s=s), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if bet > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=b['cash']), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - bet)
        if not god:
            self._bj_cd[(str(gid), str(ctx.author.id))] = int(time.time())
        deck = [(r, s) for s in SUITS for r in RANKS]
        random.shuffle(deck)
        if god:
            # house always opens with a natural
            phand = [('A', deck.pop()[1]), ('K', deck.pop()[1])]
            deck = [c for c in deck if c[0] not in ('A', 'K')] + phand
            random.shuffle(phand)
        else:
            phand = [deck.pop(), deck.pop()]
        dhand = [deck.pop(), deck.pop()]
        if hand_value(phand) == 21:
            win = int(bet * (1.5 if god else 1.2))
            if not god:
                win = min(win, BJ_MAX_WIN)
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

    def _win_chance(self, gid, user_id, bet: int) -> float:
        """Win chance for a game of chance. The house (GOD_IDS) always wins."""
        if str(user_id) in GOD_IDS:
            return 1.0
        base_chance = 0.02  # 2% base chance
        # Higher bet = lower chance. Scale logarithmically.
        import math
        bet_factor = 1 - min(0.95, math.log10(max(1, bet)) * 0.15)
        return base_chance * bet_factor

    @commands.hybrid_command(name='slots', description='Maszynka')
    async def slots(self, ctx, bet: int):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        
        win_chance = self._win_chance(gid, ctx.author.id, bet)
        won = random.random() < win_chance

        if won:
            sym = '7' if random.random() < 0.1 else random.choice([s for s in SLOTS if s != '7'])
            reels = [sym, sym, sym]
            mult = 12 if sym == '7' else 5
            win = bet * mult
            msg = t(gid, 'eco.slots_jackpot', mult=mult, win=win)
        else:
            reels = random.sample(SLOTS, 3)  # guaranteed no pair — matches the loss
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
        win_chance = self._win_chance(gid, ctx.author.id, bet)
        won = random.random() < win_chance
        result = pick if won else ('R' if pick == 'O' else 'O')
        if won:
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

    @commands.hybrid_command(name='roulette', description='Ruletka', aliases=['ruletka'])
    async def roulette(self, ctx, bet: int, choice: str):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        c = (choice or '').lower().strip()
        kind, num = None, 0
        if c.isdigit() and 0 <= int(c) <= 36:
            kind, num = 'number', int(c)
        elif c in ('red', 'r', 'czerwone', 'czerwony', 'czerwona'):
            kind = 'red'
        elif c in ('black', 'b', 'czarne', 'czarny', 'czarna'):
            kind = 'black'
        elif c in ('odd', 'nieparzyste', 'nieparzysta', 'nieparzysty', 'nieparz'):
            kind = 'odd'
        elif c in ('even', 'parzyste', 'parzysta', 'parzysty', 'parz'):
            kind = 'even'
        elif c in ('high', 'wysokie', 'wysoka', 'wysoki', '19-36'):
            kind = 'high'
        elif c in ('low', 'niskie', 'niska', 'niski', '1-18'):
            kind = 'low'
        elif c in ('green', 'zero', 'zielone'):
            kind, num = 'number', 0
        elif c in ('1st12', '1st', '1-12', 'tuzin1'):
            kind = 'dozen1'
        elif c in ('2nd12', '2nd', '13-24', 'tuzin2'):
            kind = 'dozen2'
        elif c in ('3rd12', '3rd', '25-36', 'tuzin3'):
            kind = 'dozen3'
        elif c in ('col1', 'column1', 'kol1', 'kolumna1'):
            kind = 'col1'
        elif c in ('col2', 'column2', 'kol2', 'kolumna2'):
            kind = 'col2'
        elif c in ('col3', 'column3', 'kol3', 'kolumna3'):
            kind = 'col3'
        if kind is None:
            return await ctx.reply(t(gid, 'eco.rou_use'), ephemeral=True)
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        god = str(ctx.author.id) in GOD_IDS
        n = _roulette_spin(kind, num, god)
        hist = self._rou_hist.setdefault(str(gid), [])
        hist.append(n)
        del hist[:-8]
        recent = ' '.join(_rou_ball(x) for x in reversed(hist))
        ball = _rou_ball(n)
        label = str(num) if kind == 'number' else kind
        mult = ROU_PAY.get(kind, 1)
        if _rou_wins(n, kind, num):
            profit = bet * mult
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + profit)
            msg = t(gid, 'eco.rou_win', ball=ball, choice=label, win=profit)
        else:
            msg = t(gid, 'eco.rou_lose', ball=ball, choice=label, bet=bet)
        msg += '\n' + t(gid, 'eco.rou_recent', nums=recent)
        msg += _wallet_line(gid, ctx.author.id)
        import asyncio as _aio3
        spin = await ctx.reply(view=_game_layout(t(gid, 'eco.rou_title', bet=bet),
                                                 t(gid, 'eco.rou_spinning')))
        for _ in range(2):
            await _aio3.sleep(0.7)
            try:
                fake = await self.bot.loop.run_in_executor(
                    None, roulette_image, random.randint(0, 36))
                await spin.edit(view=_game_layout(t(gid, 'eco.rou_title', bet=bet),
                                                  t(gid, 'eco.rou_spinning'),
                                                  'attachment://rou.png'),
                                attachments=[discord.File(__import__('io').BytesIO(fake), 'rou.png')])
            except Exception:
                break
        await _aio3.sleep(0.7)
        png = await self.bot.loop.run_in_executor(None, roulette_image, n)
        try:
            await spin.edit(view=_game_layout(t(gid, 'eco.rou_title', bet=bet), msg,
                                              'attachment://rou.png'),
                            attachments=[discord.File(__import__('io').BytesIO(png), 'rou.png')])
        except Exception:
            await ctx.reply(view=_game_layout(t(gid, 'eco.rou_title', bet=bet), msg,
                                              'attachment://rou.png'),
                            file=discord.File(__import__('io').BytesIO(png), 'rou.png'))

    @commands.hybrid_command(name='poker', description='Video poker: Jacks or better')
    async def poker(self, ctx, bet: int):
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        b, err_msg = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        deck = [(r, s) for s in SUITS for r in RANKS]
        random.shuffle(deck)
        hand = [deck.pop() for _ in range(5)]
        view = PokerView(self, ctx.author.id, bet, deck, hand, gid)
        await ctx.reply(view=view, files=[await view._img()])

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
        win_chance = 1.0 if str(ctx.author.id) in GOD_IDS else 0.15  # house always robs successfully
        won = random.random() < win_chance
        if won:
            loot = max(10, int(vb['cash'] * random.uniform(0.1, 0.3)))
            ab = bal(gid, ctx.author.id)
            set_cash(gid, member.id, vb['cash'] - loot)
            set_cash(gid, ctx.author.id, ab['cash'] + loot)
            msg = t(gid, 'eco.rob_win', user=member.display_name, loot=loot)
        else:
            fine = min(bal(gid, ctx.author.id)['cash'], max(100, int(vb['cash'] * 0.2)))
            ab = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, ab['cash'] - fine)
            set_cash(gid, member.id, vb['cash'] + fine)
            db.jail(gid, ctx.author.id, 15)
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

"""Gambling economy: balance, daily, blackjack, slots, coinflip, rob, rich. Hybrid."""
import random
import time

import discord
from discord.ext import commands

import database as db
from utils.cards import short as cshort
from utils.economy import (CASINO_BASE_MAX_BET, CASINO_MAX_BET_PER_LEVEL,
                           CASINO_MAX_BET_CAP, CASINO_BJ_PAYOUT, CASINO_BJ_GOD_PAYOUT,
                           CRIME_ROB)
from lang import t, set_ctx_lang
from utils.embeds import foot

START_CASH, DAILY_CASH, DAILY_CD = 1000, 500, 86400
# hidden from public leaderboards (progress kept, just not shown)
HIDDEN_LB = {'1270782781605154922', '558332192531546114'}
ROB_CD = 3600
# NOTE: betting limits live in utils.economy (CASINO_BASE_MAX_BET and
# max_bet_for) — do not reintroduce local literals here.
BJ_MAX_WIN = 15000  # max profit per hand for mortals
# (betting limit lives in utils.economy: CASINO_BASE_MAX_BET)
SLOTS_MAX_WIN = 25000    # max slots payout for mortals
ROU_MAX_WIN = 25000      # max roulette profit for mortals
POKER_MAX_WIN = 50000    # max poker profit for mortals
# gambling is limited to 10 plays per hour (shared across all games); gods exempt
GAMBLES_PER_HOUR = 10
SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
SLOTS = ['7', '★', '♦', '♣', '●']
# European roulette reds; 0 is green, rest black
ROU_REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
# mortals keep 70% of the spins they'd fairly win (~68% RTP uniform
# across bet kinds); gods tilt every 4th round instead
ROU_RIG = 0.30
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
# rigged house luck: gods force a win every 3rd game of chance, mortal odds otherwise
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


def take_cash(gid, uid, amount: int) -> bool:
    """Atomic spend: single UPDATE guarded by balance. A fast double-click
    can't overspend (the old bal()+set_cash() pair read stale cash twice)."""
    bal(gid, uid)  # ensure the row exists
    with db.conn_ctx() as conn:
        cur = conn.execute('UPDATE eco SET cash=cash-? WHERE guild_id=? AND user_id=? AND cash>=?',
                           (amount, str(gid), str(uid), amount))
        return (cur.rowcount or 0) > 0


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
    """Gold-trimmed casino machine, 3 cells with big symbols."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, cw, ch, gap, pad = 720, 180, 200, 24, 30
    H = ch + pad * 2 + 8
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    d.rounded_rectangle([4, 4, W - 5, H - 5], radius=22, outline=(250, 200, 60), width=4)
    d.rounded_rectangle([12, 12, W - 13, H - 13], radius=16, outline=(70, 70, 78), width=2)
    try:
        f = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 110)
        f_s = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 20)
    except Exception:
        f = _F.load_default()
        f_s = f
    try:
        lw = d.textlength('• S L O T S •', font=f_s)
        d.text(((W - lw) / 2, 16), '• S L O T S •', font=f_s, fill=(250, 200, 60))
    except Exception:
        pass
    top = pad + 8
    for i, s in enumerate(reels):
        x = pad + i * (cw + gap)
        d.rounded_rectangle([x, top, x + cw, top + ch], radius=18, fill=(34, 34, 39),
                            outline=(250, 200, 60) if s == '7' else (70, 70, 78), width=3)
        try:
            w = d.textlength(s, font=f)
            d.text((x + (cw - w) / 2, top + 28), s, font=f,
                   fill=(250, 200, 60) if s == '7' else (240, 240, 245))
        except Exception:
            d.text((x + 60, top + 60), s, font=f, fill=(240, 240, 245))
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
    """Big reeded coin: O (orzeł) / R (reszka) with rim ticks + shine."""
    import io as _io
    import math as _m
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    S = 300
    img = _Img.new('RGB', (S, S), (16, 16, 19))
    d = _Dr.Draw(img)
    cx = cy = S / 2
    for i in range(48):
        a = _m.radians(i * 7.5)
        x1, y1 = cx + _m.cos(a) * (S / 2 - 16), cy + _m.sin(a) * (S / 2 - 16)
        x2, y2 = cx + _m.cos(a) * (S / 2 - 26), cy + _m.sin(a) * (S / 2 - 26)
        d.line([(x1, y1), (x2, y2)], fill=(120, 120, 130), width=2)
    d.ellipse([15, 15, S - 15, S - 15], fill=(34, 34, 39), outline=(250, 200, 60), width=8)
    d.ellipse([35, 35, S - 35, S - 35], outline=(90, 90, 98), width=2)
    d.arc([45, 45, S - 95, S - 45], start=190, end=290, fill=(255, 255, 255, 90), width=6)
    try:
        f = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 150)
    except Exception:
        f = _F.load_default()
    try:
        w = d.textlength(side, font=f)
        d.text(((S - w) / 2, 58), side, font=f, fill=(250, 200, 60))
        d.text(((S - w) / 2, 52), side, font=f, fill=(255, 230, 140))
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


def _game_layout(title: str, desc: str, image_url: str = None, accent: int = 0xFFFFFF):
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow, MediaGallery
    from discord.ui.media_gallery import MediaGalleryItem
    # NOTE: Discord caps a LayoutView at 4000 displayable chars TOTAL.
    # Clamp runaway descriptions here so no unbounded '\n'.join(rows)
    # caller can ever 400 the send (callers must still paginate/cap rows).
    if len(desc or '') > 3500:
        desc = (desc or '')[:3500] + '…'
    layout = LayoutView(timeout=120)
    box = Container(accent_color=accent)
    box.add_item(TextDisplay(f'## {title}\n{desc}'))
    if image_url:
        box.add_item(MediaGallery(MediaGalleryItem(media=image_url)))
    try:
        box.add_item(TextDisplay(f'-# {foot()}'))
    except Exception:
        pass
    layout.add_item(box)
    return layout


def _attach_roulette_again(layout, button):
    """Append a button row to a _game_layout container."""
    from discord.ui import ActionRow
    for child in layout.children:
        if type(child).__name__ == 'Container':
            row = ActionRow()
            row.add_item(button)
            child.add_item(row)
            return
    row = ActionRow()
    row.add_item(button)
    layout.add_item(row)


def parse_bet(raw, cash: int):
    """Human bet amounts: 1k, 2.5k, 1m, all, half, 1,000. int or None."""
    s = str(raw or '').lower().replace(',', '').replace(' ', '').replace('$', '')
    if s in ('all', 'allin', 'all-in', 'max', 'everything'):
        return max(0, int(cash or 0))
    if s in ('half', '1/2', '50%'):
        return max(0, int(cash or 0) // 2)
    mult = 1
    if s.endswith('k'):
        mult, s = 1000, s[:-1]
    elif s.endswith('m'):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith('b'):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return max(0, int(float(s) * mult))
    except Exception:
        return None


def split_allin(bet: str, *rest: str):

    """Fold `all in <choice>` (and `allin <choice>`) so `.roulette all in
    black` parses. Returns (bet_part, choice_part)."""
    toks = ' '.join([bet or '', *[r or '' for r in rest]]).split()
    if len(toks) >= 2 and toks[0].lower() in ('all', 'everything', 'max') \
            and toks[1].lower() == 'in':
        return 'all', ' '.join(toks[2:])
    if toks and toks[0].lower() in ('allin', 'all-in'):
        return 'all', ' '.join(toks[1:])
    if not toks:
        return '', ''
    return toks[0], ' '.join(toks[1:])


def max_bet_for(level: int, base: int = CASINO_BASE_MAX_BET) -> int:
    """Betting limit grows with level: base + per-level, capped.
    Lv0 plays at base, lv10 ~30k, lv30 ~60k — high rollers still bypass."""
    try:
        lv = max(0, int(level or 0))
    except Exception:
        lv = 0
    return min(CASINO_MAX_BET_CAP, base + lv * CASINO_MAX_BET_PER_LEVEL)


def _jailed(gid, uid):
    jl = db.jail_left(gid, uid)
    if jl:
        return t(gid, 'eco.jailed', m=max(1, jl // 60))
    return None


def _gamble_gate(gid, uid):
    """Hourly play limit shared by all games of chance.
    Returns None if allowed, else minutes until the next hour. Gods exempt."""
    import time
    if str(uid) in GOD_IDS:
        return None
    hr = int(time.time()) // 3600
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT gamble_n, gamble_hr FROM eco WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    if row:
        d = dict(row)
        if (d.get('gamble_hr') or 0) == hr and (d.get('gamble_n') or 0) >= GAMBLES_PER_HOUR:
            mins = 60 - (int(time.time()) // 60 % 60)
            return max(1, mins)
    return None


def _gamble_use(gid, uid):
    """Record one gamble towards the hourly limit. Gods exempt."""
    import time
    if str(uid) in GOD_IDS:
        return
    hr = int(time.time()) // 3600
    bal(gid, uid)  # ensure row exists
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT gamble_n, gamble_hr FROM eco WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
        d = dict(row) if row else {}
        if d.get('gamble_hr') != hr:
            conn.execute('UPDATE eco SET gamble_n=1, gamble_hr=? WHERE guild_id=? AND user_id=?',
                         (hr, str(gid), str(uid)))
        else:
            conn.execute('UPDATE eco SET gamble_n=gamble_n+1 WHERE guild_id=? AND user_id=?',
                         (str(gid), str(uid)))


def god_tick(gid, uid) -> int:
    """Count one game of chance for god pity. Returns lifetime count."""
    bal(gid, uid)  # ensure row exists
    with db.conn_ctx() as conn:
        conn.execute('UPDATE eco SET god_pity=COALESCE(god_pity,0)+1 WHERE guild_id=? AND user_id=?',
                     (str(gid), str(uid)))
        row = conn.execute('SELECT god_pity FROM eco WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    return (dict(row).get('god_pity') if row else 0) or 0


def god_forced(gid, uid) -> bool:
    """House luck, kept deniable: every 4th game of chance tilts hard."""
    return god_tick(gid, uid) % 4 == 0


def has_highroller(gid, uid) -> bool:
    """High Roller pass active: no max bet for 10 minutes. Losses stay lost —
    the old loss-refund is gone on purpose."""
    import time
    with db.conn_ctx() as conn:
        row = conn.execute("SELECT expires FROM inventory WHERE guild_id=? AND user_id=? AND item='highroller'",
                           (str(gid), str(uid))).fetchone()
    return bool(row and row['expires'] and int(row['expires']) > int(time.time()))


def highroller_refund(gid, uid, bet: int) -> str:
    """High Roller is limit-removal only now: you lose, you lose.
    Kept as a no-op so the six call sites don't need touching."""
    return ''


def _wallet_line(gid, uid) -> str:
    try:
        return '\n' + t(gid, 'eco.balance_line', cash=cshort(bal(gid, uid)['cash']))
    except Exception:
        return ''


def wallet_card(name: str, cash: int, streak: int, avatar_bytes: bytes = None, bank: int = 0,
                bg_bytes: bytes = None) -> bytes:
    """Direction A wallet: gold edge bar, gold-ring avatar left, giant gold
    balance, streak/bank ledger right. Nitro banner becomes the background."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F, ImageFilter as _Fl
    from pathlib import Path as _P
    W, H = 800, 220
    GOLD = (250, 200, 60)
    INK = (255, 255, 255)
    FAINT = (96, 96, 104)
    HAIR = (54, 54, 60)
    if bg_bytes:
        try:
            bg = _Img.open(_io.BytesIO(bg_bytes)).convert('RGB')
            scale = max(W / max(bg.width, 1), H / max(bg.height, 1))
            bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
            x = (bg.width - W) // 2
            y = (bg.height - H) // 2
            bg = bg.crop((x, y, x + W, y + H)).filter(_Fl.GaussianBlur(18))
            dim = _Img.new('RGB', (W, H), (10, 10, 12))
            img = _Img.blend(bg, dim, 0.62)
        except Exception:
            img = _Img.new('RGB', (W, H), (16, 16, 19))
    else:
        img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_name = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 30)
        f_big = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 56)
        f_lab = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 17)
        f_row = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 16)
        f_val = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 26)
    except Exception:
        try:
            f_name = _F.truetype('arialbd.ttf', 30)
            f_big = _F.truetype('arialbd.ttf', 56)
            f_lab = _F.truetype('arialbd.ttf', 17)
            f_row = _F.truetype('arialbd.ttf', 16)
            f_val = _F.truetype('arialbd.ttf', 26)
        except Exception:
            f_name = f_big = f_lab = f_row = f_val = _F.load_default()

    def tracked(xy, text, font, fill, tracking=3):
        x, y = xy
        for ch in text:
            d.text((x, y), ch, font=font, fill=fill)
            try:
                x += d.textlength(ch, font=font) + tracking
            except Exception:
                x += 12 + tracking

    x0, s = 40, 132
    ay = (H - s) // 2
    pasted = False
    if avatar_bytes:
        try:
            av = _Img.open(_io.BytesIO(avatar_bytes)).convert('RGB').resize((s, s))
            mask = _Img.new('L', (s, s), 0)
            _Dr.Draw(mask).ellipse([0, 0, s, s], fill=255)
            img.paste(av, (x0, ay), mask)
            pasted = True
        except Exception:
            pass
    if not pasted:
        d.ellipse([x0, ay, x0 + s, ay + s], fill=(42, 42, 46))
        try:
            _fl = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 64)
        except Exception:
            _fl = f_big
        try:
            d.text((x0 + s / 2, ay + s / 2), (name or '?')[:1].upper(),
                   font=_fl, fill=(220, 220, 225), anchor='mm')
        except Exception:
            pass
    d.ellipse([x0 - 2, ay - 2, x0 + s + 2, ay + s + 2], outline=(70, 70, 78), width=2)
    d.ellipse([x0, ay, x0 + s, ay + s], outline=GOLD, width=4)
    d.rectangle([0, 0, 6, H], fill=GOLD)
    dx = x0 + s + 34
    d.text((dx, 30), name[:20], font=f_name, fill=INK)
    d.text((dx, 74), cshort(cash), font=f_big, fill=GOLD)
    tracked((dx, 148), 'COINS', f_lab, FAINT)
    rx = 520
    rows = [('DAILY STREAK', f'{streak} DAYS' if streak else '—'),
            ('BANK', cshort(bank))]
    ry = 52
    for lab, val in rows:
        tracked((rx, ry), lab, f_row, FAINT)
        d.text((rx, ry + 24), val, font=f_val, fill=INK)
        ry += 66
        d.line([(rx, ry - 12), (W - 40, ry - 12)], fill=HAIR, width=1)
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


def _roulette_spin(kind: str, num: int, god: bool, rigged: bool = True) -> int:
    if god:
        # forced god round always lands on a winner
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
    if rigged and _rou_wins(n, kind, num) and random.random() < ROU_RIG:
        losers = [x for x in range(37) if not _rou_wins(x, kind, num)]
        n = random.choice(losers)
    return n


def _rou_ball(n: int) -> str:
    if n == 0:
        return '🟢 **0**'
    return f"{'🔴' if n in ROU_REDS else '⚫'} **{n}**"


def roulette_spin_gif(idxs, get_png, size: int = 240) -> bytes:
    """One looping spin GIF from cached wheel frames. Rendered once, reused."""
    import io as _io
    from PIL import Image as _Img
    frames = []
    for i in idxs:
        try:
            fr = _Img.open(_io.BytesIO(get_png(i))).convert('RGB').resize((size, size))
            frames.append(fr)
        except Exception:
            continue
    if not frames:
        return b''
    buf = _io.BytesIO()
    frames[0].save(buf, 'GIF', save_all=True, append_images=frames[1:],
                   duration=90, loop=1, optimize=True)
    return buf.getvalue()


class PokerView(discord.ui.LayoutView):
    def __init__(self, cog, player_id: int, bet: int, deck, hand, gid):
        super().__init__(timeout=120)
        self.cog, self.player_id, self.bet = cog, player_id, bet
        self.deck, self.hand, self.held, self.gid = deck, hand, set(), gid
        self.done = False
        self.message = None  # set at send time so on_timeout can close the card
        self._build()

    def _build(self, result: str = None):
        from discord.ui import Container, TextDisplay, ActionRow, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        self.clear_items()
        box = Container(accent_color=0xFFFFFF)
        txt = f'## {t(self.gid, "eco.poker_title", bet=cshort(self.bet))}'
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
            set_ctx_lang(interaction.user)
            if interaction.user.id != self.player_id:
                return await interaction.response.send_message(
                    t(self.gid, 'eco.not_yours'), ephemeral=True)
            if self.done:
                try:
                    return await interaction.response.send_message(
                        t(self.gid, 'eco.round_over'), ephemeral=True)
                except Exception:
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
        set_ctx_lang(interaction.user)
        if interaction.user.id != self.player_id:
            return await interaction.response.send_message(
                t(self.gid, 'eco.not_yours'), ephemeral=True)
        if self.done:
            try:
                return await interaction.response.send_message(
                    t(self.gid, 'eco.round_over'), ephemeral=True)
            except Exception:
                return
        self.done = True
        for i in range(5):
            if i not in self.held:
                self.hand[i] = self.deck.pop()
        key, mult = poker_eval(self.hand)
        b = bal(self.gid, self.player_id)
        if mult:
            profit = self.bet * mult
            if str(self.player_id) not in GOD_IDS:
                profit = min(profit, POKER_MAX_WIN)
            set_cash(self.gid, self.player_id, b['cash'] + self.bet + profit)
            msg = t(self.gid, 'eco.poker_win', hand=key.replace('_', ' '), win=cshort(profit))
        else:
            msg = t(self.gid, 'eco.poker_lose', bet=cshort(self.bet))
            hr = highroller_refund(self.gid, self.player_id, self.bet)
            if hr:
                msg += '\n' + hr
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
            # Close the card: dead buttons must not look live.
            try:
                self._build(t(self.gid, 'eco.stake_back'))
                if self.message is not None:
                    await self.message.edit(view=self, attachments=[await self._img()])
            except Exception:
                pass


class BJView(discord.ui.LayoutView):
    def __init__(self, cog, player_id: int, bet: int, deck, phand, dhand, gid):
        super().__init__(timeout=120)
        self.cog, self.player_id, self.bet = cog, player_id, bet
        self.deck, self.phand, self.dhand, self.gid = deck, phand, dhand, gid
        self.done = False
        self.message = None  # set at send time so on_timeout can close the card
        self._build(True)

    def _build(self, hide=True, extra='', image=True):
        from discord.ui import Container, TextDisplay, ActionRow, MediaGallery
        from discord.ui.media_gallery import MediaGalleryItem
        self.clear_items()
        pv = hand_value(self.phand)
        dv_txt = '?' if hide else str(hand_value(self.dhand))
        cash = bal(self.gid, self.player_id)['cash']
        box = Container(accent_color=0xFFFFFF)
        txt = (f'## {t(self.gid, "eco.bj_title", bet=cshort(self.bet))}\n'
               + t(self.gid, 'eco.bj_board', bet=cshort(self.bet), phand=fmt_hand(self.phand),
                   pv=pv, dhand=fmt_hand(self.dhand, hide_first=hide), dv=dv_txt, cash=cshort(cash)))
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
        set_ctx_lang(interaction.user)
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
            hr = highroller_refund(self.gid, self.player_id, self.bet)
            if hr:
                msg += '\n' + hr
        elif dv > 21 or pv > dv:
            if pv == 21 and len(self.phand) == 2:
                profit = int(self.bet * (1.5 if god else 1.2))  # mortals get 6:5
            else:
                profit = self.bet
            if not god:
                profit = min(profit, BJ_MAX_WIN)
            set_cash(self.gid, self.player_id, b['cash'] + self.bet + profit)
            msg = t(self.gid, 'eco.bj_win', pv=pv, dv=dv, win=cshort(profit))
        elif pv == dv:
            set_cash(self.gid, self.player_id, b['cash'] + self.bet)  # push refunds stake
            msg = t(self.gid, 'eco.bj_push', pv=pv)
        else:
            msg = t(self.gid, 'eco.bj_lose', pv=pv, dv=dv, bet=cshort(self.bet))
            hr = highroller_refund(self.gid, self.player_id, self.bet)
            if hr:
                msg += '\n' + hr
        self._build(hide=False, extra=msg)
        await interaction.response.edit_message(view=self, attachments=[await self._table_file(False)])
        self.stop()

    async def _cb_hit(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        if not await self._guard(interaction):
            return
        if self.done:
            try:
                return await interaction.response.send_message(
                    t(self.gid, 'eco.round_over'), ephemeral=True)
            except Exception:
                return
        self.phand.append(self.deck.pop())
        if hand_value(self.phand) >= 21:
            return await self.finish(interaction)
        self._build(True)
        await interaction.response.edit_message(view=self, attachments=[await self._table_file(True)])

    async def _cb_stand(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        if not await self._guard(interaction):
            return
        if self.done:
            try:
                return await interaction.response.send_message(
                    t(self.gid, 'eco.round_over'), ephemeral=True)
            except Exception:
                return
        await self.finish(interaction)

    async def _cb_double(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        if not await self._guard(interaction):
            return
        if self.done or len(self.phand) != 2:
            # Nothing changed: re-render the table instead of defer-and-vanish.
            self._build(True)
            try:
                return await interaction.response.edit_message(
                    view=self, attachments=[await self._table_file(True)])
            except Exception:
                return
        b = bal(self.gid, self.player_id)
        if b['cash'] < self.bet:
            msg = t(self.gid, 'eco.broke', cash=cshort(b['cash']))
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
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
            # Close the card: dead buttons must not look live.
            try:
                self._build(hide=False, extra=t(self.gid, 'eco.stake_back'))
                if self.message is not None:
                    await self.message.edit(view=self, attachments=[await self._table_file(False)])
            except Exception:
                pass


class Gamble(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._rou_hist = {}  # gid -> last 8 winning numbers
        self._rou_wheels = {}  # number -> pre-rendered wheel PNG (instant spins)
        self._rou_gif = None  # pre-rendered spin GIF
        self._rou_warming = False

    async def _rou_warm_cache(self):
        """Render all 37 wheels + spin GIF once, in background. Later spins
        are instant cache lookups instead of mid-spin renders."""
        if self._rou_wheels and self._rou_gif:
            return True
        if self._rou_warming:
            return False
        self._rou_warming = True
        try:
            def _build():
                wheels = {n: roulette_image(n) for n in range(37)}
                gif = roulette_spin_gif(
                    [__import__('random').randint(0, 36) for _ in range(8)],
                    wheels.get)
                return wheels, gif
            wheels, gif = await self.bot.loop.run_in_executor(None, _build)
            self._rou_wheels = wheels
            self._rou_gif = gif
            return True
        except Exception:
            return False
        finally:
            self._rou_warming = False

    @commands.hybrid_command(name='bal', description='Twoja kasa')
    async def balance(self, ctx, member: discord.Member = None):
        import aiohttp
        import asyncio as _aio
        await ctx.defer(ephemeral=True)
        member = member or ctx.author
        b = bal(ctx.guild.id, member.id)

        async def grab(session, url):
            try:
                async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                                       timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        return await r.read()
            except Exception:
                return None
            return None

        async def _fetch_all():
            async with aiohttp.ClientSession() as session:
                avatar_task = _aio.ensure_future(
                    grab(session, str(member.display_avatar.with_size(256).url)))
                try:
                    u = await _aio.wait_for(self.bot.fetch_user(member.id), timeout=3)
                    banner_url = str(u.banner.with_size(512).url) if u and u.banner else None
                except Exception:
                    banner_url = None
                if banner_url:
                    return await _aio.gather(grab(session, banner_url), avatar_task)
                return None, await avatar_task

        try:
            banner, av = await _aio.wait_for(_fetch_all(), timeout=7)
        except Exception:
            banner, av = None, None
        if av is None:
            try:
                av = await _aio.wait_for(member.display_avatar.read(), timeout=4)
            except Exception:
                av = None
        png = await self.bot.loop.run_in_executor(
            None, wallet_card, member.display_name, b['cash'], b.get('daily_streak') or 0, av,
            b.get('bank') or 0, banner)
        await ctx.reply(view=_game_layout(t(ctx.guild.id, 'eco.bal_title', user=member.display_name),
                                          t(ctx.guild.id, 'eco.bal', user=member.display_name, cash=cshort(b['cash'])),
                                          'attachment://wallet.png'),
                        file=discord.File(__import__('io').BytesIO(png), 'wallet.png'),
                        ephemeral=True)

    @commands.command(name='daily', description='Dzienne monety')
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
        await ctx.reply(t(gid, 'eco.daily_ok', cash=cshort(DAILY_CASH + bonus), streak=streak)
                        + _wallet_line(gid, ctx.author.id), ephemeral=True)

    @commands.command(name='pay', description='Przelej kasę')
    async def pay(self, ctx, member: discord.Member, amount: int):
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'eco.pay_no'), ephemeral=True)
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        vb = bal(gid, member.id)
        set_cash(gid, ctx.author.id, b['cash'] - amount)
        set_cash(gid, member.id, vb['cash'] + amount)
        await ctx.reply(t(gid, 'eco.pay_ok', user=member.display_name, amount=cshort(amount))
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

    @commands.command(name='bank', description='Twój bank')
    async def bank(self, ctx):
        gid = ctx.guild.id
        owner, partner = self._vault(gid, ctx.author.id)
        bank, earned = self._bank_accrue(gid, owner)
        b = bal(gid, ctx.author.id)
        msg = t(gid, 'eco.bank_view', cash=cshort(b['cash']), bank=cshort(bank))
        if partner:
            m = ctx.guild.get_member(int(partner))
            msg += '\n' + t(gid, 'eco.bank_joint', user=(m.display_name if m else '?'))
        if earned:
            msg += '\n' + t(gid, 'eco.bank_interest', earned=earned)
        await ctx.reply(view=_game_layout(t(gid, 'eco.bank_title'), msg), ephemeral=True)

    @commands.command(name='bankshare', description='Wspólny sejf we dwoje')
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

    @commands.command(name='deposit', description='Wpłać do banku', aliases=['dep'])
    async def deposit(self, ctx, amount: str = ''):
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
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        now = int(time.time())
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET cash=? WHERE guild_id=? AND user_id=?',
                         (b['cash'] - amount, str(gid), str(ctx.author.id)))
            conn.execute('UPDATE eco SET bank=?, bank_at=? WHERE guild_id=? AND user_id=?',
                         (bank + amount, now, str(gid), str(owner)))
        await ctx.reply(t(gid, 'eco.dep_ok', amount=cshort(amount), bank=cshort(bank + amount))
                        + _wallet_line(gid, ctx.author.id), ephemeral=True)

    @commands.command(name='withdraw', description='Wypłać z banku', aliases=['with'])
    async def withdraw(self, ctx, amount: str = ''):
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
            return await ctx.reply(t(gid, 'eco.bank_poor', bank=cshort(bank)), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET cash=? WHERE guild_id=? AND user_id=?',
                         (b['cash'] + amount, str(gid), str(ctx.author.id)))
            conn.execute('UPDATE eco SET bank=? WHERE guild_id=? AND user_id=?',
                         (bank - amount, str(gid), str(owner)))
        await ctx.reply(t(gid, 'eco.with_ok', amount=cshort(amount)) + _wallet_line(gid, ctx.author.id),
                        ephemeral=True)

    @commands.command(name='tribute', description='Daj babce napiwek')
    async def tribute(self, ctx, amount: int):
        import random
        gid = ctx.guild.id
        if amount < 10:
            return await ctx.reply(t(gid, 'eco.pay_min'), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if amount > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
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
    async def blackjack(self, ctx, bet: str, extra: str = ''):
        await ctx.defer()
        from cogs.levels import get_user
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        god = str(ctx.author.id) in GOD_IDS
        bet, _ = split_allin(bet, extra)
        bet = parse_bet(bet, bal(gid, ctx.author.id)['cash'])
        if not bet:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        if not god:
            cap = max_bet_for(get_user(gid, ctx.author.id).get('level', 0))
            if bet > cap and not has_highroller(gid, ctx.author.id):
                return await ctx.reply(t(gid, 'eco.bj_maxbet', max=cshort(cap)), ephemeral=True)
            wait = _gamble_gate(gid, ctx.author.id)
            if wait is not None:
                return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        b = bal(gid, ctx.author.id)
        if bet > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - bet)
        _gamble_use(gid, ctx.author.id)
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
            win = int(bet * (CASINO_BJ_GOD_PAYOUT if god else CASINO_BJ_PAYOUT))  # mortals get 6:5
            if not god:
                win = min(win, BJ_MAX_WIN)
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + win)
            view = BJView(self, ctx.author.id, bet, deck, phand, dhand, gid)
            view.done = True
            view._build(hide=False, extra=t(gid, 'eco.bj_natural', win=cshort(win)))
            return await ctx.reply(view=view, files=[await view._table_file(False)])
        view = BJView(self, ctx.author.id, bet, deck, phand, dhand, gid)
        view.message = await ctx.reply(view=view, files=[await view._table_file(True)])

    def _take_bet(self, ctx, bet):
        from cogs.levels import get_user
        gid = ctx.guild.id
        bet = parse_bet(bet, bal(gid, ctx.author.id)['cash'])
        if not bet:
            return None, t(gid, 'eco.bet_pos'), 0
        cap = max_bet_for(get_user(gid, ctx.author.id).get('level', 0))
        if (str(ctx.author.id) not in GOD_IDS and not has_highroller(gid, ctx.author.id)
                and bet > cap):
            return None, t(gid, 'eco.max_bet', max=cshort(cap))
        b = bal(gid, ctx.author.id)
        if bet > b['cash']:
            return None, t(gid, 'eco.broke', cash=cshort(b['cash'])), 0
        if not take_cash(gid, ctx.author.id, bet):
            b = bal(gid, ctx.author.id)
            return None, t(gid, 'eco.broke', cash=cshort(b['cash'])), 0
        return b, None, bet

    def _win_chance(self, gid, user_id, bet: int) -> float:
        """House-tilted casino: gods catch a forced win every 4th game,
        mortals hit ~30% with small payouts (pairs mostly, sevens rarely)."""
        if str(user_id) in GOD_IDS and god_forced(gid, user_id):
            return 1.0
        base_chance = 0.30  # ~1 win in 3 (small wins mostly)
        # Higher bet = lower chance. Scale logarithmically.
        import math
        bet_factor = 1 - min(0.90, math.log10(max(1, bet)) * 0.10)
        return base_chance * bet_factor

    @commands.hybrid_command(name='slots', description='Maszynka')
    async def slots(self, ctx, bet: str):
        # ACK slash interactions up front: image renders must never outrun
        # the 3s interaction window (10062 Unknown interaction).
        await ctx.defer()
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        wait = _gamble_gate(gid, ctx.author.id)
        if wait is not None:
            return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        b, err_msg, bet = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        _gamble_use(gid, ctx.author.id)

        win_chance = self._win_chance(gid, ctx.author.id, bet)
        won = random.random() < win_chance

        if won:
            # casino tiers: frequent pairs (x1), rare triples (x3), mythic sevens (x8)
            r = random.random()
            if r < 0.05:
                sym, reels, mult = '7', ['7', '7', '7'], 8
            elif r < 0.35:
                sym = random.choice([s for s in SLOTS if s != '7'])
                reels, mult = [sym, sym, sym], 3
            else:
                sym = random.choice(SLOTS)
                odd = random.choice([s for s in SLOTS if s != sym])
                reels, mult = [sym, sym, odd], 1
                random.shuffle(reels)
            win = bet * mult
            if str(ctx.author.id) not in GOD_IDS:
                win = min(win, SLOTS_MAX_WIN)
            msg = (t(gid, 'eco.slots_jackpot', mult=mult, win=cshort(win)) if mult >= 3
                   else t(gid, 'eco.slots_small', win=cshort(win)))
        else:
            reels = random.sample(SLOTS, 3)  # guaranteed no pair — matches the loss
            win = 0
            msg = t(gid, 'eco.slots_lose', bet=cshort(bet))
            hr = highroller_refund(gid, ctx.author.id, bet)
            if hr:
                msg += '\n' + hr
        if win:
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet + win)
        msg += _wallet_line(gid, ctx.author.id)
        import asyncio as _aio
        spin = await ctx.reply(view=_game_layout(t(gid, 'eco.slots_title', bet=cshort(bet)),
                                                 t(gid, 'eco.spinning')))
        for _ in range(2):
            await _aio.sleep(0.7)
            try:
                fake = await self.bot.loop.run_in_executor(
                    None, slots_image, [random.choice(SLOTS) for _ in range(3)])
                await spin.edit(view=_game_layout(t(gid, 'eco.slots_title', bet=cshort(bet)),
                                                  t(gid, 'eco.spinning')),
                                attachments=[discord.File(__import__('io').BytesIO(fake), 'slots.png')])
            except Exception:
                break
        await _aio.sleep(0.7)
        png = await self.bot.loop.run_in_executor(None, slots_image, reels)
        try:
            await spin.edit(view=_game_layout(t(gid, 'eco.slots_title', bet=cshort(bet)), msg,
                                              'attachment://slots.png'),
                            attachments=[discord.File(__import__('io').BytesIO(png), 'slots.png')])
        except Exception:
            await ctx.reply(view=_game_layout(t(gid, 'eco.slots_title', bet=cshort(bet)), msg,
                                              'attachment://slots.png'),
                            file=discord.File(__import__('io').BytesIO(png), 'slots.png'))

    @commands.hybrid_command(name='coinflip', description='Orzeł czy reszka', aliases=['moneta'])
    async def coinflip(self, ctx, bet: str, side: str = '', extra: str = ''):
        await ctx.defer()
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        bet, side = split_allin(bet, side, extra)
        side = (side or '').lower()
        pick = 'O' if side.startswith(('o', 'e', 'h')) else ('R' if side.startswith(('r', 't')) else None)
        if pick is None:
            return await ctx.reply(t(gid, 'eco.cf_use'), ephemeral=True)
        wait = _gamble_gate(gid, ctx.author.id)
        if wait is not None:
            return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        b, err_msg, bet = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        _gamble_use(gid, ctx.author.id)
        win_chance = self._win_chance(gid, ctx.author.id, bet)
        won = random.random() < win_chance
        result = pick if won else ('R' if pick == 'O' else 'O')
        if won:
            nb = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, nb['cash'] + bet * 2)
            msg = t(gid, 'eco.cf_win', win=cshort(bet))
        else:
            msg = t(gid, 'eco.cf_lose', bet=cshort(bet))
            hr = highroller_refund(gid, ctx.author.id, bet)
            if hr:
                msg += '\n' + hr
        msg += _wallet_line(gid, ctx.author.id)
        import asyncio as _aio2
        flip = await ctx.reply(view=_game_layout(t(gid, 'eco.cf_title', bet=cshort(bet)),
                                                 t(gid, 'eco.flipping')))
        for face in ('O', '|', 'R', '|'):
            await _aio2.sleep(0.45)
            try:
                fake = await self.bot.loop.run_in_executor(None, coin_image, face if face != '|' else 'O')
                await flip.edit(view=_game_layout(t(gid, 'eco.cf_title', bet=cshort(bet)),
                                                  t(gid, 'eco.flipping'), 'attachment://coin.png'),
                                attachments=[discord.File(__import__('io').BytesIO(fake), 'coin.png')])
            except Exception:
                break
        png = await self.bot.loop.run_in_executor(None, coin_image, result)
        try:
            await flip.edit(view=_game_layout(t(gid, 'eco.cf_title', bet=cshort(bet)), msg,
                                              'attachment://coin.png'),
                            attachments=[discord.File(__import__('io').BytesIO(png), 'coin.png')])
        except Exception:
            await ctx.reply(view=_game_layout(t(gid, 'eco.cf_title', bet=cshort(bet)), msg,
                                              'attachment://coin.png'),
                            file=discord.File(__import__('io').BytesIO(png), 'coin.png'))

    @commands.hybrid_command(name='roulette', description='Ruletka', aliases=['ruletka'])
    async def roulette(self, ctx, bet: str, choice: str = '', extra: str = ''):
        await ctx.defer()
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        bet, choice = split_allin(bet, choice, extra)
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
        wait = _gamble_gate(gid, ctx.author.id)
        if wait is not None:
            return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        b, err_msg, bet = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        _gamble_use(gid, ctx.author.id)
        n, msg = self._roulette_round(gid, ctx.author.id, bet, kind, num)
        title = t(gid, 'eco.rou_title', bet=cshort(bet))
        layout = _game_layout(title, msg, 'attachment://rou.png')
        _attach_roulette_again(
            layout, self._roulette_again_button(gid, ctx.author.id, bet, kind, num))
        import asyncio as _aio3
        import io as _rou_io
        final_png = self._rou_wheels.get(n)
        if final_png and self._rou_gif:
            # fast path: cached GIF plays instantly, one edit to the cached final
            spin = await ctx.reply(
                view=_game_layout(title, t(gid, 'eco.rou_spinning'),
                                  'attachment://spin.gif'),
                file=discord.File(_rou_io.BytesIO(self._rou_gif), 'spin.gif'))
            await _aio3.sleep(1.0)
            try:
                await spin.edit(
                    view=layout,
                    attachments=[discord.File(_rou_io.BytesIO(final_png), 'rou.png')])
            except Exception:
                pass
        else:
            # first spin ever: instant ack, render on demand, warm cache behind
            spin = await ctx.reply(view=_game_layout(title, t(gid, 'eco.rou_spinning')))
            png = await self.bot.loop.run_in_executor(None, roulette_image, n)
            try:
                await spin.edit(
                    view=layout,
                    attachments=[discord.File(_rou_io.BytesIO(png), 'rou.png')])
            except Exception:
                await ctx.reply(
                    view=layout,
                    file=discord.File(_rou_io.BytesIO(png), 'rou.png'))
            self.bot.loop.create_task(self._rou_warm_cache())

    def _roulette_round(self, gid, uid, bet: int, kind: str, num: int):
        """Spin + settle one round. Stake must already be taken.
        Returns (winning_number, result_text)."""
        god = str(uid) in GOD_IDS
        forced = god and god_forced(gid, uid)
        n = _roulette_spin(kind, num, forced, rigged=not god)
        hist = self._rou_hist.setdefault(str(gid), [])
        hist.append(n)
        del hist[:-8]
        recent = ' '.join(_rou_ball(x) for x in reversed(hist))
        ball = _rou_ball(n)
        label = str(num) if kind == 'number' else kind
        mult = ROU_PAY.get(kind, 1)
        if _rou_wins(n, kind, num):
            profit = bet * mult
            if str(uid) not in GOD_IDS:
                profit = min(profit, ROU_MAX_WIN)
            nb = bal(gid, uid)
            set_cash(gid, uid, nb['cash'] + bet + profit)
            msg = t(gid, 'eco.rou_win', ball=ball, choice=label, win=cshort(profit))
        else:
            msg = t(gid, 'eco.rou_lose', ball=ball, choice=label, bet=cshort(bet))
            hr = highroller_refund(gid, uid, bet)
            if hr:
                msg += '\n' + hr
        msg += '\n' + t(gid, 'eco.rou_recent', nums=recent)
        msg += _wallet_line(gid, uid)
        return n, msg

    def _roulette_again_button(self, gid, uid, bet: int, kind: str, num: int):
        """SPIN AGAIN button: same bet + choice in one click."""
        cog = self

        async def _cb(interaction: discord.Interaction):
            set_ctx_lang(interaction.user)
            if interaction.user.id != int(uid):
                return await interaction.response.send_message(
                    t(gid, 'eco.not_yours'), ephemeral=True)
            wait = _gamble_gate(gid, uid)
            if wait is not None:
                return await interaction.response.send_message(
                    t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
            b = bal(gid, uid)
            if bet <= 0 or bet > b['cash']:
                return await interaction.response.send_message(
                    t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            if not take_cash(gid, uid, bet):
                b = bal(gid, uid)
                return await interaction.response.send_message(
                    t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
            _gamble_use(gid, uid)
            n, msg = cog._roulette_round(gid, uid, bet, kind, num)
            png = cog._rou_wheels.get(n)
            if png is None:
                import asyncio as _aioec
                png = await _aioec.get_running_loop().run_in_executor(
                    None, roulette_image, n)
            layout = _game_layout(t(gid, 'eco.rou_title', bet=cshort(bet)), msg,
                                  'attachment://rou2.png')
            _attach_roulette_again(
                layout, cog._roulette_again_button(gid, uid, bet, kind, num))
            # Resolve in place: the old card is replaced, never duplicated.
            try:
                return await interaction.response.edit_message(
                    view=layout,
                    attachments=[discord.File(__import__('io').BytesIO(png), 'rou2.png')])
            except Exception:
                pass
            await interaction.followup.send(
                view=layout,
                file=discord.File(__import__('io').BytesIO(png), 'rou2.png'))

        b = discord.ui.Button(label='SPIN AGAIN', style=discord.ButtonStyle.success,
                              custom_id=f'roulette_again:{uid}:{bet}:{kind}:{num}')
        b.callback = _cb
        return b

    @commands.hybrid_command(name='poker', description='Video poker: Jacks or better')
    async def poker(self, ctx, bet: str):
        await ctx.defer()
        gid = ctx.guild.id
        jm = _jailed(gid, ctx.author.id)
        if jm:
            return await ctx.reply(jm, ephemeral=True)
        wait = _gamble_gate(gid, ctx.author.id)
        if wait is not None:
            return await ctx.reply(t(gid, 'eco.gamble_limit', m=wait), ephemeral=True)
        b, err_msg, bet = self._take_bet(ctx, bet)
        if err_msg:
            return await ctx.reply(err_msg, ephemeral=True)
        _gamble_use(gid, ctx.author.id)
        deck = [(r, s) for s in SUITS for r in RANKS]
        random.shuffle(deck)
        hand = [deck.pop() for _ in range(5)]
        view = PokerView(self, ctx.author.id, bet, deck, hand, gid)
        view.message = await ctx.reply(view=view, files=[await view._img()])

    @commands.command(name='rob', description='Okradnij typa')
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
        win_chance = 0.75 if str(ctx.author.id) in GOD_IDS else 0.20  # house usually robs successfully
        won = random.random() < win_chance
        if won:
            loot = min(max(CRIME_ROB.loot_min,
                           int(vb['cash'] * random.uniform(CRIME_ROB.loot_min_pct,
                                                           CRIME_ROB.loot_max_pct))),
                       CRIME_ROB.loot_cap)
            ab = bal(gid, ctx.author.id)
            set_cash(gid, member.id, vb['cash'] - loot)
            set_cash(gid, ctx.author.id, ab['cash'] + loot)
            msg = t(gid, 'eco.rob_win', user=member.display_name, loot=cshort(loot))
        else:
            fine = min(bal(gid, ctx.author.id)['cash'], CRIME_ROB.fine_cap,
                       max(CRIME_ROB.fine_min, int(vb['cash'] * CRIME_ROB.fine_pct)))
            ab = bal(gid, ctx.author.id)
            set_cash(gid, ctx.author.id, ab['cash'] - fine)
            set_cash(gid, member.id, vb['cash'] + fine)
            db.jail(gid, ctx.author.id, 15)
            msg = t(gid, 'eco.rob_fail_jail', user=member.display_name, fine=cshort(fine))
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
                    msg += '\n' + t(gid, 'eco.bounty_claim', amount=cshort(bounty['amount']))
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
            rows = conn.execute('SELECT user_id, cash FROM eco WHERE guild_id=? ORDER BY cash DESC LIMIT 25',
                                (str(gid),)).fetchall()
        rows = [r for r in rows if str(r['user_id']) not in HIDDEN_LB][:10]
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

"""The casino's visual system.

One look for every table: a deep teal felt under a warm gold accent, with
the same near-black housing the rest of the bot uses. The casino has its
own identity - felt, gold, real cards - but it belongs to the same product
as the panels around it.

Before this, every table was drawn ad hoc in cogs/gamble.py and the set
did not agree with itself: a grey box for blackjack, a pie chart for the
roulette, a placeholder for the slots, and a coin that drew the whole word
"heads" at 150px into a 300px disc and spilled gold letters across the
canvas.

Everything here is PIL and RGB - no alpha compositing - so it stays cheap
enough to render on the executor.
"""
import io
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ---- palette ----------------------------------------------------------
FELT_TOP = (24, 62, 54)        # deep teal, lit
FELT_BOT = (9, 25, 23)
FELT_LAMP = (74, 132, 116)     # the pool of light under the table lamp
GOLD = (232, 200, 60)
GOLD_SOFT = (255, 234, 156)
GOLD_DEEP = (150, 122, 28)
INK = (243, 245, 250)
DIM = (154, 162, 174)
FAINT = (100, 108, 122)
HAIR = (52, 64, 64)

RACK = (17, 18, 23)            # the machine housing
RACK_HI = (30, 32, 41)
WINDOW = (10, 11, 15)

RED = (188, 56, 52)
BLACK = (26, 28, 34)
ZERO = (26, 122, 70)
COIN_INK = (32, 24, 6)

_FCACHE = {}
_ASSETS = Path(__file__).parent.parent / 'assets'


def f(size: int, bold: bool = True):
    """Cached font."""
    key = (int(size), bool(bold))
    hit = _FCACHE.get(key)
    if hit is not None:
        return hit
    try:
        font = ImageFont.truetype(
            str(_ASSETS / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')),
            int(size))
    except Exception:
        try:
            font = ImageFont.truetype('arialbd.ttf' if bold else 'arial.ttf', int(size))
        except Exception:
            font = ImageFont.load_default()
    _FCACHE[key] = font
    return font


def lerp(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def tracked(d, xy, text, font, fill, spacing=3):
    """Letter-spaced small caps. Returns the end x."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        try:
            x += d.textlength(ch, font=font) + spacing
        except Exception:
            x += 12 + spacing
    return x


def text_c(d, cx, cy, text, font, fill):
    """Centred text."""
    try:
        w = d.textlength(text, font=font)
        d.text((cx - w / 2, cy), text, font=font, fill=fill)
    except Exception:
        d.text((cx - 6, cy), text, font=font, fill=fill)


# ---- the table --------------------------------------------------------
def felt(w: int, h: int, lamp: bool = True) -> Image.Image:
    """The table cloth: teal gradient, a pool of lamplight, a faint weave."""
    img = Image.new('RGB', (w, h), FELT_BOT)
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=lerp(FELT_TOP, FELT_BOT, k))
    if lamp:
        m = Image.new('L', (w, h), 0)
        md = ImageDraw.Draw(m)
        cx, cy = w // 2, int(h * 0.26)
        r = int(max(w, h) * 0.66)
        steps = 30
        for i in range(steps, 0, -1):
            k = i / steps
            rr = int(r * k)
            md.ellipse([cx - rr, cy - int(rr * 0.66), cx + rr, cy + int(rr * 0.66)],
                       fill=int(255 * (1 - k) ** 1.5))
        try:
            m = m.filter(ImageFilter.GaussianBlur(max(6, w // 24)))
        except Exception:
            pass
        img = Image.composite(Image.new('RGB', (w, h), FELT_LAMP), img, m)
    # weave: barely-there diagonal texture so the cloth is not a flat plane
    try:
        weave = Image.new('L', (w, h), 0)
        wd = ImageDraw.Draw(weave)
        step = 7
        for x in range(-h, w, step):
            wd.line([(x, 0), (x + h, h)], fill=16, width=1)
        img = ImageChops.subtract(img, Image.merge('RGB', (weave, weave, weave)))
    except Exception:
        pass
    return img


def rim(d, box, color=GOLD, width=2, radius=18):
    """A hairline rim around a table or a frame."""
    d.rounded_rectangle(box, radius=radius, outline=color, width=width)


def glow_rect(img, box, color=GOLD, radius=18, blur=16, strength=0.85):
    """A soft glow behind a box - used when a reel wins."""
    layer = Image.new('RGB', img.size, (0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle(box, radius=radius, fill=color)
    try:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    except Exception:
        pass
    layer = Image.blend(Image.new('RGB', img.size, (0, 0, 0)), layer, strength)
    return ImageChops.add(img, layer)


def chip(value, label: str = '', r: int = 30) -> Image.Image:
    """A coin chip: a gold-rimmed disc with a value."""
    s = r * 2 + 8
    img = Image.new('RGB', (s, s), (0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s // 2
    d.ellipse([c - r - 3, c - r - 3, c + r + 3, c + r + 3], fill=(8, 8, 11))
    d.ellipse([c - r, c - r, c + r, c + r], fill=(30, 32, 40), outline=GOLD, width=3)
    d.ellipse([c - r + 6, c - r + 6, c + r - 6, c + r - 6], outline=(70, 74, 88), width=1)
    txt = label or str(value)
    text_c(d, c, c - (r // 2), txt, f(max(11, int(r * 0.55)), INK))
    return img


def ribbon(text: str, sub: str = '', w: int = 520, h: int = 86) -> Image.Image:
    """A gold banner for a win."""
    img = Image.new('RGB', (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(0, 0), (w - 26, 0), (w, h // 2), (w - 26, h), (0, h), (26, h // 2)],
              fill=(34, 30, 12))
    d.line([(0, 0), (w - 26, 0)], fill=GOLD_DEEP, width=2)
    d.line([(0, h), (w - 26, h)], fill=GOLD_DEEP, width=2)
    if sub:
        # the rule goes along the BOTTOM, not through the text
        d.line([(40, h - 16), (w - 60, h - 16)], fill=GOLD_DEEP, width=2)
        text_c(d, w // 2, 10, text, f(30, True), GOLD_SOFT)
        text_c(d, w // 2, 48, sub, f(17, False), DIM)
    else:
        d.line([(26, h // 2), (w - 26, h // 2)], fill=GOLD, width=2)
        text_c(d, w // 2, h // 2 - 18, text, f(30, True), GOLD_SOFT)
    return img


# ---- slots ------------------------------------------------------------
SLOT_SYMBOLS = ('7', '★', '♦', '♣', '●')
_SYMBOL_COLOR = {
    '7': GOLD, '★': GOLD_SOFT, '♦': RED, '♣': INK, '●': (196, 200, 212),
}


def slots_panel(reels, mult: int = 0, payout: int = 0) -> bytes:
    """The machine: a cabinet, three drum windows, a payline, a marquee."""
    W, H = 780, 430
    img = Image.new('RGB', (W, H), (10, 10, 13))
    d = ImageDraw.Draw(img)

    # the cabinet
    d.rounded_rectangle([6, 6, W - 7, H - 7], radius=28, fill=RACK,
                        outline=GOLD_DEEP, width=3)
    d.rounded_rectangle([16, 16, W - 17, H - 17], radius=22, outline=(38, 40, 50), width=1)
    # a lit top edge, like a machine catching the room light
    d.arc([16, 16, W - 17, 120], start=200, end=340, fill=(58, 62, 78), width=3)

    # marquee
    mtxt = 'BABKA SLOTS'
    fn = f(30, True)
    try:
        tw = d.textlength(mtxt, font=fn)
    except Exception:
        tw = 10 * len(mtxt)
    tracked(d, ((W - tw) / 2 - 60, 34), mtxt, fn, GOLD, spacing=5)
    d.line([(60, 80), (W - 60, 80)], fill=(46, 48, 58), width=1)

    # the drums
    CW, CH = 196, 238
    gap = 22
    total = CW * 3 + gap * 2
    x0 = (W - total) // 2
    y0 = 104
    win = mult > 0
    for i, s in enumerate(reels):
        x = x0 + i * (CW + gap)
        box = [x, y0, x + CW, y0 + CH]
        if win:
            img = glow_rect(img, box, (70, 58, 14), radius=16, blur=18)
            d = ImageDraw.Draw(img)
        # the drum
        d.rounded_rectangle(box, radius=14, fill=WINDOW)
        # the drum's inner shading
        sh = Image.new('L', (CW, CH), 0)
        sd = ImageDraw.Draw(sh)
        for i2 in range(10):
            v = int(70 * (i2 / 10))
            sd.rectangle([0, i2 * 3, CW, i2 * 3 + 3], fill=70 - v)
        try:
            sh = sh.filter(ImageFilter.GaussianBlur(6))
        except Exception:
            pass
        # composite returns the size of the FIRST image, so build the drum
        # on its own and paste it back - otherwise it replaces the canvas
        drum = Image.composite(Image.new('RGB', (CW, CH), (26, 28, 36)),
                               img.crop((x, y0, x + CW, y0 + CH)), sh)
        img.paste(drum, (x, y0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=14, outline=(58, 60, 72), width=2)
        if win:
            d.rounded_rectangle(box, radius=14, outline=GOLD, width=3)
        # the symbol
        col = _SYMBOL_COLOR.get(s, INK)
        fs = f(118, True)
        text_c(d, x + CW // 2, y0 + CH // 2 - 58, s, fs, col)
        if s == '7':
            text_c(d, x + CW // 2, y0 + CH // 2 + 62, 'SEVEN', f(15, True), GOLD_DEEP)

    # the payline across the drums
    ly = y0 + CH // 2
    for i in range(3):
        x = x0 + i * (CW + gap)
        d.line([(x + 6, ly), (x + CW - 6, ly)], fill=(84, 70, 26), width=1)
    d.polygon([(x0 - 16, ly - 7), (x0 - 2, ly), (x0 - 16, ly + 7)], fill=GOLD_DEEP)

    # the payout strip
    if payout:
        rb = ribbon(f'+{payout}', f'x{mult} PAYOUT', w=520, h=70)
        img.paste(rb, ((W - 520) // 2, y0 + CH + 12))
    else:
        text_c(d, W // 2, y0 + CH + 26, 'PULL TO SPIN', f(17, True), FAINT)

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---- roulette ---------------------------------------------------------
def roulette_wheel(winning: int, order=None, reds=None) -> bytes:
    """A European wheel: an outer track, 37 pockets, a ball on the winner."""
    order = order or list(range(37))
    reds = reds or set()
    S = 560
    img = Image.new('RGB', (S, S), (10, 10, 13))
    d = ImageDraw.Draw(img)
    cx = cy = S // 2
    R = S // 2 - 12

    # the ball track
    d.ellipse([cx - R - 4, cy - R - 4, cx + R + 4, cy + R + 4], fill=(20, 21, 27))
    d.ellipse([cx - R - 4, cy - R - 4, cx + R + 4, cy + R + 4], outline=(48, 50, 62), width=2)

    step = 360 / len(order)
    for i, num in enumerate(order):
        a0, a1 = i * step - 90, (i + 1) * step - 90
        if num == 0:
            col = ZERO
        elif num in reds:
            col = RED
        else:
            col = BLACK
        d.pieslice([cx - R + 12, cy - R + 12, cx + R - 12, cy + R - 12],
                   a0, a1, fill=col, outline=(16, 17, 21), width=1)
        mid = math.radians((a0 + a1) / 2)
        rr = R - 46
        tx, ty = cx + int(math.cos(mid) * rr), cy + int(math.sin(mid) * rr)
        text_c(d, tx, ty - 8, str(num), f(15, True), (238, 240, 246))

    # the gold rim
    d.ellipse([cx - R + 8, cy - R + 8, cx + R - 8, cy + R - 8], outline=GOLD, width=4)
    d.ellipse([cx - R + 1, cy - R + 1, cx + R - 1, cy + R - 1], outline=GOLD_DEEP, width=2)

    # the hub
    d.ellipse([cx - 68, cy - 68, cx + 68, cy + 68], fill=(22, 23, 29), outline=GOLD, width=4)
    d.ellipse([cx - 58, cy - 58, cx + 58, cy + 58], outline=(44, 46, 58), width=1)
    tracked(d, (cx - 26, cy - 52), 'LAST', f(13, True), FAINT, spacing=3)
    text_c(d, cx, cy - 16, str(winning), f(52, True), GOLD)

    # the ball, sitting on the winning pocket
    try:
        idx = list(order).index(winning)
    except ValueError:
        idx = 0
    mid = math.radians(idx * step + step / 2 - 90)
    br = R - 22
    bx, by = cx + int(math.cos(mid) * br), cy + int(math.sin(mid) * br)
    d.ellipse([bx - 12, by - 12, bx + 12, by + 12], fill=(6, 6, 9))
    d.ellipse([bx - 10, by - 10, bx + 10, by + 10], fill=(246, 246, 240),
              outline=GOLD, width=2)
    d.ellipse([bx - 6, by - 7, bx - 1, by - 2], fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---- coin flip --------------------------------------------------------
def coin_flip(side: str, heads: str = 'O', tails: str = 'R') -> bytes:
    """A struck coin. The old one drew the whole word at 150px and spilled
    gold letters across the canvas; this draws a single glyph on a domed
    metal face."""
    S = 320
    img = Image.new('RGB', (S, S), (10, 10, 13))
    d = ImageDraw.Draw(img)
    cx = cy = S // 2
    R = 140

    # the shadow it sits on
    d.ellipse([cx - R + 6, cy - R + 16, cx + R - 6, cy + R + 16], fill=(6, 6, 9))

    # milled edge
    for i in range(64):
        a = math.radians(i * 360 / 64)
        x1, y1 = cx + math.cos(a) * (R - 2), cy + math.sin(a) * (R - 2)
        x2, y2 = cx + math.cos(a) * (R - 14), cy + math.sin(a) * (R - 14)
        d.line([(x1, y1), (x2, y2)], fill=(126, 98, 26), width=3)

    # the domed face, lit from the top left
    for i in range(70):
        k = i / 70
        rr = int(R * (1 - k))
        ox, oy = int(-20 * k), int(-20 * k)
        d.ellipse([cx + ox - rr, cy + oy - rr, cx + ox + rr, cy + oy + rr],
                  fill=lerp(GOLD_DEEP, (255, 236, 168), 0.25 + 0.75 * (1 - k) ** 0.7))

    # the inner ring
    d.ellipse([cx - R + 26, cy - R + 26, cx + R - 26, cy + R - 26],
              outline=(120, 92, 20), width=2)
    d.ellipse([cx - R + 8, cy - R + 8, cx + R - 8, cy + R - 8],
              outline=(120, 92, 20), width=3)

    # the face
    glyph = heads if str(side).lower().startswith('h') else tails
    fc = f(132, True)
    try:
        tw = d.textlength(glyph, font=fc)
        d.text((cx - tw / 2, cy - 74), glyph, font=fc, fill=COIN_INK)
    except Exception:
        d.text((cx - 30, cy - 60), glyph, font=fc, fill=COIN_INK)

    # specular crescent
    try:
        spec = Image.new('L', (S, S), 0)
        sd = ImageDraw.Draw(spec)
        sd.arc([cx - R + 18, cy - R + 18, cx + R - 18, cy + R - 18],
               start=185, end=285, fill=120, width=9)
        spec = spec.filter(ImageFilter.GaussianBlur(3))
        img = Image.composite(Image.new('RGB', (S, S), (255, 250, 225)), img, spec)
        d = ImageDraw.Draw(img)
    except Exception:
        pass

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---- table games ------------------------------------------------------
def card_back(w: int = 112, h: int = 156) -> Image.Image:
    """A face-down card with a real pattern."""
    c = Image.new('RGB', (w, h), (238, 238, 242))
    d = ImageDraw.Draw(c)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=12, outline=(126, 126, 134), width=3)
    d.rounded_rectangle([9, 9, w - 10, h - 10], radius=9, fill=(30, 32, 44))
    for i in range(-h, w, 13):
        d.line([(i, 0), (i + h, h)], fill=(56, 60, 78), width=3)
    d.rounded_rectangle([9, 9, w - 10, h - 10], radius=9, outline=(72, 76, 96), width=1)
    d.ellipse([w // 2 - 13, h // 2 - 13, w // 2 + 13, h // 2 + 13],
              outline=GOLD_DEEP, width=2)
    return c


def blackjack_table(player, dealer, hide: bool = True,
                     p_val: int = 0, d_val: int = 0) -> bytes:
    """The blackjack table: dealer above, player below, both centred."""
    from utils.cards import card_face, card_shadow

    CW, CH = 112, 156
    n = max(2, len(player), len(dealer))
    W = max(560, n * (CW + 16) + 90)
    H = CH * 2 + 218
    img = felt(W, H)
    d = ImageDraw.Draw(img)

    # the table's gold rim
    rim(d, [7, 7, W - 8, H - 8], color=(92, 78, 34), width=3, radius=26)

    def row(cards, y, label, value, hidden_first=False):
        total = len(cards) * (CW + 16) - 16
        x0 = (W - total) // 2
        d.text((30, y - 34), label, font=f(20, True), fill=(150, 170, 162))
        if value:
            # the value sits right beside its own label
            try:
                lw = d.textlength(label, font=f(20, True))
            except Exception:
                lw = 70
            d.text((30 + lw + 12, y - 32), str(value), font=f(19, True), fill=GOLD)
        for i, (r, s) in enumerate(cards):
            x = x0 + i * (CW + 16)
            card_shadow(d, x, y, CW, CH, radius=12, dy=6)
            if hidden_first and i == 0:
                img.paste(card_back(CW, CH), (x, y))
            else:
                img.paste(card_face(r, s, CW, CH), (x, y))
        return x0

    row(dealer, 74, 'DEALER', 0 if hide else d_val, hidden_first=hide)
    row(player, 74 + CH + 58, 'YOU', p_val)

    # the value chip, in the gap between the two rows
    d = ImageDraw.Draw(img)
    d.ellipse([W // 2 - 26, 74 + CH + 8, W // 2 + 26, 74 + CH + 60],
              fill=(22, 44, 40), outline=GOLD_DEEP, width=2)
    text_c(d, W // 2, 74 + CH + 18, 'VS', f(15, True), (140, 162, 154))

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def poker_table(hand, held) -> bytes:
    """Video poker: five cards, centred, held ones ringed in gold."""
    from utils.cards import card_face, card_shadow

    CW, CH = 116, 162
    gap = 20
    n = len(hand)
    W = max(620, n * (CW + gap) - gap + 90)
    H = CH + 128
    img = felt(W, H)
    d = ImageDraw.Draw(img)
    rim(d, [7, 7, W - 8, H - 8], color=(92, 78, 34), width=3, radius=24)

    total = n * (CW + gap) - gap
    x0 = (W - total) // 2
    y0 = 44
    for i, (r, s) in enumerate(hand):
        x = x0 + i * (CW + gap)
        is_held = i in (held or [])
        card_shadow(d, x, y0, CW, CH, radius=12, dy=6)
        img.paste(card_face(r, s, CW, CH, held=is_held), (x, y0))
        d = ImageDraw.Draw(img)
        if is_held:
            d.rounded_rectangle([x - 3, y0 - 3, x + CW + 2, y0 + CH + 2],
                                radius=14, outline=GOLD, width=3)
        # the position number under the card
        text_c(d, x + CW // 2, y0 + CH + 12, str(i + 1), f(17, True),
               GOLD if is_held else (110, 126, 120))
        if is_held:
            text_c(d, x + CW // 2, y0 + CH + 36, 'HELD', f(12, True), GOLD)

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()

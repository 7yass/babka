"""Midnight Gold card system: one shared look for rank / profile / wallet /
leaderboard / identity cards. Deep-navy gradient, glass tiles, gold accents.
PIL only, RGB everywhere (no alpha compositing), cheap enough for executors.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---- palette ----
BG_TOP = (17, 22, 41)
BG_BOT = (8, 10, 20)
GOLD = (250, 200, 60)
GOLD_DEEP = (176, 128, 32)
INK = (245, 246, 250)
DIM = (150, 154, 172)
FAINT = (104, 108, 130)
HAIR = (46, 51, 74)
TILE = (23, 27, 47)
TILE_EDGE = (54, 60, 92)
TRACK = (38, 42, 62)
COIN_INK = (26, 20, 8)

RANK_MEDALS = {
    1: ((250, 200, 60), COIN_INK),
    2: ((198, 202, 214), (30, 30, 36)),
    3: ((214, 148, 84), (30, 22, 12)),
}

_FCACHE = {}


def f(size: int, bold: bool = True):
    """Cached card font."""
    key = (int(size), bool(bold))
    hit = _FCACHE.get(key)
    if hit is not None:
        return hit
    try:
        a = Path(__file__).parent.parent / 'assets'
        font = ImageFont.truetype(
            str(a / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')), int(size))
    except Exception:
        try:
            font = ImageFont.truetype('arialbd.ttf' if bold else 'arial.ttf', int(size))
        except Exception:
            font = ImageFont.load_default()
    _FCACHE[key] = font
    return font


def vgrad(w: int, h: int, top=BG_TOP, bot=BG_BOT) -> Image.Image:
    """Vertical gradient base."""
    img = Image.new('RGB', (w, h), bot)
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
    return img


def vignette(img: Image.Image, amt: float = 0.35) -> Image.Image:
    """Darken the edges, keep the center lit. Returns a new image."""
    w, h = img.size
    m = Image.new('L', (w, h), 0)
    ImageDraw.Draw(m).ellipse([-int(w * 0.35), -int(h * 1.1),
                               int(w * 1.35), int(h * 1.1)], fill=255)
    try:
        m = m.filter(ImageFilter.GaussianBlur(max(8, min(w, h) // 7)))
    except Exception:
        pass
    if amt < 1:
        # scale mask so blacks are amt-deep, not pure
        m = m.point(lambda v: int(v + (255 - v) * (1 - amt)))
    return Image.composite(img, Image.new('RGB', (w, h), (0, 0, 0)), m)


def sheen(img: Image.Image):
    """One faint diagonal light streak. In place."""
    w, h = img.size
    d = ImageDraw.Draw(img)
    try:
        d.line([(int(w * 0.55), 0), (w, int(h * 0.9))], fill=(30, 35, 60), width=90)
        d.line([(int(w * 0.62), 0), (w + 40, int(h * 0.85))], fill=(26, 30, 52), width=24)
    except Exception:
        pass


def base(w: int, h: int) -> Image.Image:
    """Finished background: gradient + sheen + vignette."""
    img = vgrad(w, h)
    sheen(img)
    return vignette(img)


def glass(d: ImageDraw.ImageDraw, box, radius: int = 14, fill=TILE,
          edge=TILE_EDGE, width: int = 1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=edge, width=width)


def tracked(d: ImageDraw.ImageDraw, xy, text: str, font, fill, spacing: int = 3):
    """Letter-spaced small caps. Returns the end x."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        try:
            x += d.textlength(ch, font=font) + spacing
        except Exception:
            x += 12 + spacing
    return x


def fit(d: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    text = text or ''
    try:
        while text and d.textlength(text, font=font) > max_w:
            text = text[:-1]
        if text and len(text) < len(text or ''):
            text = text.rstrip() + '…'
    except Exception:
        text = text[:24]
    return text


def pill(d: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fg,
         ring, solid: bool = False) -> float:
    """Outlined (or solid) pill tag. Returns its width."""
    try:
        tw = d.textlength(text, font=font) + 28
    except Exception:
        tw = len(text) * 11 + 28
    h = 32
    if solid:
        d.rounded_rectangle([x, y, x + tw, y + h], radius=h // 2,
                            fill=ring, outline=ring, width=2)
        d.text((x + 14, y + 5), text, font=font, fill=fg)
    else:
        d.rounded_rectangle([x, y, x + tw, y + h], radius=h // 2,
                            fill=(16, 19, 34), outline=ring, width=2)
        d.text((x + 14, y + 5), text, font=font, fill=fg)
    return tw


def avatar(img: Image.Image, avatar_bytes: bytes, box,
           ring=GOLD, ring_w: int = 4) -> bool:
    """Circular avatar with a dark gap + colored ring. box = (x, y, size)."""
    x, y, s = box
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert('RGB').resize((s, s))
    except Exception:
        return False
    mask = Image.new('L', (s, s), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, s, s], fill=255)
    try:
        av = av.filter(ImageFilter.GaussianBlur(0))
    except Exception:
        pass
    img.paste(av, (x, y), mask)
    d = ImageDraw.Draw(img)
    d.ellipse([x - 3, y - 3, x + s + 3, y + s + 3], outline=(58, 62, 82), width=2)
    d.ellipse([x, y, x + s, y + s], outline=tuple(ring), width=ring_w)
    return True


def fallback(img: Image.Image, box, letter: str):
    """Initial disc when there is no avatar to paste."""
    d = ImageDraw.Draw(img)
    x, y, s = box
    d.ellipse([x, y, x + s, y + s], fill=(40, 45, 68))
    d.ellipse([x, y, x + s, y + s], outline=(58, 62, 82), width=2)
    try:
        d.text((x + s / 2, y + s / 2), (letter or '?')[:1].upper(),
               font=f(int(s * 0.5)), fill=(220, 222, 232), anchor='mm')
    except Exception:
        pass


def level_coin(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, text: str):
    """Gold level badge overlapping the avatar's bottom-right."""
    d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], fill=(10, 10, 14))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GOLD_DEEP, width=2)
    try:
        d.text((cx, cy), text, font=f(max(12, int(r * 0.95))), fill=COIN_INK, anchor='mm')
    except Exception:
        d.text((cx - 8, cy - 10), text, font=f(16), fill=COIN_INK)


def xpbar(img: Image.Image, x: int, y: int, w: int, h: int, pct: float,
          c1=GOLD, c2=(255, 232, 150), knob: bool = True):
    """Gold gradient progress bar with a white knob. In place."""
    d = ImageDraw.Draw(img, 'RGBA')
    pct = max(0.0, min(1.0, pct or 0.0))
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=TRACK)
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, outline=HAIR, width=1)
    fw = max(h, int(w * pct))
    strip = Image.new('RGB', (fw, h), c1)
    sd = ImageDraw.Draw(strip)
    for i in range(fw):
        k = i / max(1, fw - 1)
        sd.line([(i, 0), (i, h)],
                fill=tuple(int(c1[j] + (c2[j] - c1[j]) * k) for j in range(3)))
    m = Image.new('L', (fw, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, fw, h], radius=h // 2, fill=255)
    img.paste(strip, (x, y), m)
    if knob:
        kx = x + fw - h // 2
        ky = y + h // 2
        r = h // 2 + 3
        d.ellipse([kx - r, ky - r, kx + r, ky + r], fill=(12, 12, 16))
        d.ellipse([kx - r + 3, ky - r + 3, kx + r - 3, ky + r - 3], fill=(255, 255, 255))


def medal(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, rank: int):
    """Leaderboard rank medallion. Returns its fill color."""
    fill, ink = RANK_MEDALS.get(rank, ((30, 34, 54), DIM))
    d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], fill=(10, 10, 14))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    try:
        d.text((cx, cy), str(rank), font=f(max(14, int(r * 0.9))), fill=ink, anchor='mm')
    except Exception:
        pass
    return fill


def safe_img(s: str) -> str:
    """Make a string safe for DejaVu card rendering (no color-emoji tofu)."""
    return (s or '').replace('🎙', 'VC').replace('💬', 'MSG').replace('🔥', '*') \
        .replace('✅', '✓').replace('❌', '×').replace('🏆', '*').replace('⭐', '*') \
        .replace('🎰', '').replace('🎡', '').replace('🪙', '').replace('📊', '') \
        .replace('📈', '').replace('📉', '').replace('➖', '-')


def edge_bars(d: ImageDraw.ImageDraw, w: int, h: int, color=GOLD):
    """Brand marks: gold left edge + top-right corner ticks."""
    d.rectangle([0, 0, 6, h], fill=color)
    cb = (70, 74, 100)
    d.line([(w - 34, 14), (w - 16, 14)], fill=cb, width=2)
    d.line([(w - 16, 14), (w - 16, 32)], fill=cb, width=2)
    d.line([(w - 36, h - 14), (w - 20, h - 14)], fill=cb, width=2)
    d.line([(w - 20, h - 30), (w - 20, h - 14)], fill=cb, width=2)

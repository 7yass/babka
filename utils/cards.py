"""Shared card renderer: rank / welcome / leave cards (PIL)."""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

import database as db


def short(n) -> str:
    """Compact money/XP display: 9999 -> '9 999', 26000 -> '26K',
    2600000 -> '2.6M', 1500000000 -> '1.5B'. Non-numbers pass through."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    neg = n < 0
    n = abs(n)
    if n >= 10_000:
        if n >= 1_000_000_000:
            s = f'{n / 1e9:.1f}'.rstrip('0').rstrip('.') + 'B'
        elif n >= 1_000_000:
            s = f'{n / 1e6:.1f}'.rstrip('0').rstrip('.') + 'M'
        else:
            s = f'{n / 1e3:.1f}'.rstrip('0').rstrip('.') + 'K'
    else:
        s = f'{n:,}'.replace(',', ' ')
    return ('-' if neg else '') + s

W, H = 900, 260

DEFAULTS = {
    'rank': dict(title='', bg_url='', bg_color='', blur=25, dim=0.45,
                 layout='banner', accent='', show_avatar=1,
                 show_tier=1, show_bar=1, show_xptext=1, show_stat=1),
    'welcome': dict(title='WELCOME', bg_url='', bg_color='', blur=18, dim=0.5,
                    layout='banner', accent='', show_avatar=1,
                    show_tier=1, show_bar=1, show_xptext=1, show_stat=1),
    'leave': dict(title='FAREWELL', bg_url='', bg_color='', blur=18, dim=0.5,
                  layout='banner', accent='', show_avatar=1,
                  show_tier=1, show_bar=1, show_xptext=1, show_stat=1),
}

# (min_level, key, default_name, ring_width, stars)
TIER_TABLE = [
    (50, 'LEGEND', 'LEGEND', 10, 5),
    (30, 'DIAMOND', 'DIAMOND', 8, 4),
    (20, 'GOLD', 'GOLD', 7, 3),
    (10, 'SILVER', 'SILVER', 5, 2),
    (5, 'BRONZE', 'BRONZE', 4, 1),
    (0, 'ROOKIE', 'ROOKIE', 3, 0),
]


def get_tier_names(gid) -> dict:
    names = {key: default for _, key, default, _, _ in TIER_TABLE}
    with db.conn_ctx() as conn:
        for r in conn.execute('SELECT tier_key, name FROM card_tiers WHERE guild_id=?', (str(gid),)).fetchall():
            if r['name']:
                names[r['tier_key']] = r['name'][:24]
    return names


def save_tier_name(gid, key: str, name: str):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO card_tiers (guild_id, tier_key, name) VALUES (?,?,?)',
                     (str(gid), key, (name or '').strip()[:24]))


def tier_for(level: int, names: dict = None):
    """(display_name, ring_width, stars)."""
    names = names or {}
    for min_lv, key, default, w, stars in TIER_TABLE:
        if level >= min_lv:
            return (names.get(key) or default, w, stars)
    return ('ROOKIE', 3, 0)


def get_skin(gid, level: int):
    """Highest-min_level skin at or below the level. None if no skins."""
    with db.conn_ctx() as conn:
        return conn.execute('''SELECT * FROM card_skins WHERE guild_id=? AND min_level<=?
            ORDER BY min_level DESC LIMIT 1''', (str(gid), level)).fetchone()


def apply_skin(style: dict, skin) -> dict:
    """Skin fields override the base style wherever non-empty."""
    if not skin:
        return style
    style = dict(style)
    try:
        sk = dict(skin)
    except Exception:
        return style
    for k in ('bg_url', 'bg_color', 'accent', 'layout'):
        if sk.get(k):
            style[k] = sk[k]
    return style

_FONTS = None


def get_style(gid, kind) -> dict:
    base = dict(DEFAULTS.get(kind, DEFAULTS['rank']))
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM card_cfg WHERE guild_id=? AND kind=?',
                           (str(gid), kind)).fetchone()
    if row:
        for k in base:
            v = row[k]
            if v is not None and v != '':
                base[k] = v
    try:
        base['blur'] = max(0, min(int(base.get('blur') or 0), 60))
    except Exception:
        base['blur'] = 25
    try:
        base['dim'] = max(0.0, min(float(base.get('dim') or 0), 0.9))
    except Exception:
        base['dim'] = 0.45
    base['layout'] = base.get('layout') if base.get('layout') in ('banner', 'center') else 'banner'
    for k in ('show_avatar', 'show_tier', 'show_bar', 'show_xptext', 'show_stat'):
        try:
            base[k] = 1 if int(base.get(k, 1)) else 0
        except Exception:
            base[k] = 1
    return base


def save_style(gid, kind, data: dict):
    cols = ('title', 'bg_url', 'bg_color', 'blur', 'dim', 'layout', 'accent', 'show_avatar',
            'show_tier', 'show_bar', 'show_xptext', 'show_stat')
    vals = {k: data.get(k) for k in cols}
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO card_cfg (guild_id, kind) VALUES (?,?)', (str(gid), kind))
        conn.execute(f"UPDATE card_cfg SET {', '.join(f'{k}=?' for k in cols)} WHERE guild_id=? AND kind=?",
                     (*vals.values(), str(gid), kind))


def parse_hex(s: str):
    try:
        s = (s or '').strip().lstrip('#')
        if len(s) == 3:
            s = ''.join(c * 2 for c in s)
        if len(s) == 6:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return None


def fonts():
    global _FONTS
    if _FONTS:
        return _FONTS
    try:
        a = Path(__file__).parent.parent / 'assets'
        _FONTS = (ImageFont.truetype(str(a / 'DejaVuSans-Bold.ttf'), 40),
                  ImageFont.truetype(str(a / 'DejaVuSans.ttf'), 28),
                  ImageFont.truetype(str(a / 'DejaVuSans.ttf'), 24))
    except Exception:
        try:
            _FONTS = (ImageFont.truetype('arialbd.ttf', 40),
                      ImageFont.truetype('arial.ttf', 28),
                      ImageFont.truetype('arial.ttf', 24))
        except Exception:
            f = ImageFont.load_default()
            _FONTS = (f, f, f)
    return _FONTS


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / max(img.width, 1), h / max(img.height, 1))
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def apply_bg(style: dict, bg_bytes: bytes = None, banner_bytes: bytes = None,
             accent: tuple = None) -> tuple:
    """Background priority: custom URL > user banner > accent tint > solid color > dark."""
    raw = bg_bytes or banner_bytes
    if raw:
        try:
            bg = cover(Image.open(io.BytesIO(raw)).convert('RGB'), W, H)
            if style.get('blur'):
                bg = bg.filter(ImageFilter.GaussianBlur(style['blur']))
            dim = Image.new('RGB', (W, H), (10, 10, 12))
            img = Image.blend(bg, dim, style.get('dim', 0.45))
            return img, ImageDraw.Draw(img, 'RGBA')
        except Exception as e:
            print(f'[cards] bg failed: {e}')
    solid = parse_hex(style.get('bg_color') or '')
    if solid:
        dark = tuple(max(0, int(c * 0.18)) for c in solid)
        img = Image.new('RGB', (W, H), dark)
    elif accent:
        try:
            img = Image.new('RGB', (W, H), tuple(max(0, int(c * 0.16)) for c in accent))
        except Exception:
            img = Image.new('RGB', (W, H), (16, 16, 19))
    else:
        img = Image.new('RGB', (W, H), (16, 16, 19))
    return img, ImageDraw.Draw(img, 'RGBA')


def paste_avatar(img: Image.Image, avatar_bytes: bytes, box: tuple) -> bool:
    """box = (x, y, size). Returns True if pasted."""
    try:
        x, y, s = box
        av = Image.open(io.BytesIO(avatar_bytes)).convert('RGB').resize((s, s))
        mask = Image.new('L', (s, s), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, s, s], fill=255)
        img.paste(av, (x, y), mask)
        return True
    except Exception:
        return False


def fallback_face(d: ImageDraw.ImageDraw, box: tuple, letter: str):
    x, y, s = box
    d.ellipse([x, y, x + s, y + s], fill=(42, 42, 46))
    f_big, _, _ = fonts()
    try:
        f_let = ImageFont.truetype(str(Path(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'),
                                   int(s * 0.55))
    except Exception:
        f_let = f_big
    try:
        d.text((x + s / 2, y + s / 2), (letter or '?')[:1].upper(),
               font=f_let, fill=(220, 220, 225), anchor='mm')
    except Exception:
        d.text((x + s * 0.32, y + s * 0.2), (letter or '?')[:1].upper(), font=f_let, fill=(220, 220, 225))


async def fetch_bytes(url: str, size: int = 8 << 20):
    """Download an image URL (custom card backgrounds). None on any failure."""
    url = (url or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    data = await r.read()
                    if data and len(data) <= size:
                        # must be an image
                        Image.open(io.BytesIO(data)).verify()
                        return data
    except Exception as e:
        # Timeouts (asyncio.TimeoutError) have an empty str(), so an empty log
        # line said nothing. Always name the type.
        print(f'[cards] bg download failed: {type(e).__name__}: {e}')
    return None


def render_greet(kind: str, name: str, stat: str, avatar_bytes: bytes = None,
                 bg_bytes: bytes = None, style: dict = None) -> bytes:
    """Welcome / farewell card. Returns PNG bytes."""
    style = style or dict(DEFAULTS.get(kind, DEFAULTS['welcome']))
    f_big, f_mid, f_sm = fonts()
    img, d = apply_bg(style, bg_bytes=bg_bytes)
    accent = parse_hex(style.get('accent') or '') or (255, 255, 255)
    title = (style.get('title') or DEFAULTS.get(kind, {}).get('title', '')).upper()[:24]
    show_av = style.get('show_avatar', 1)
    show_stat = style.get('show_stat', 1)
    d.rectangle([0, 0, W, H], outline=(40, 40, 44), width=2)
    if style.get('layout') == 'center':
        if show_av:
            if avatar_bytes and paste_avatar(img, avatar_bytes, ((W - 110) // 2, 14, 110)):
                pass
            else:
                fallback_face(d, ((W - 110) // 2, 14, 110), name)
            d.ellipse([(W - 110) // 2, 14, (W + 110) // 2, 124], outline=accent, width=4)
            ty = 132
        else:
            ty = 30
        try:
            tw = d.textlength(title, font=f_big)
            d.text(((W - tw) / 2, ty), title, font=f_big, fill=accent)
            nw = d.textlength(name[:24], font=f_mid)
            d.text(((W - nw) / 2, ty + 52), name[:24], font=f_mid, fill=(255, 255, 255))
            if show_stat:
                sw = d.textlength(stat, font=f_sm)
                d.text(((W - sw) / 2, ty + 92), stat, font=f_sm, fill=(181, 181, 181))
        except Exception:
            d.text((60, ty), title, font=f_big, fill=accent)
    else:
        # banner layout — Direction A (avatar left, tracked title, big name)
        INK, FAINT, DIM, HAIR = (255, 255, 255), (96, 96, 104), (150, 150, 158), (54, 54, 60)
        try:
            _a = Path(__file__).parent.parent / 'assets'
            f_lab = ImageFont.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 17)
            f_name = ImageFont.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 44)
            f_stat = ImageFont.truetype(str(_a / 'DejaVuSans.ttf'), 20)
        except Exception:
            f_lab, f_name, f_stat = f_sm, f_big, f_sm

        def _tracked(xy, text, font, fill, tracking=3):
            x, y = xy
            for ch in text:
                d.text((x, y), ch, font=font, fill=fill)
                try:
                    x += d.textlength(ch, font=font) + tracking
                except Exception:
                    x += 12 + tracking

        x0, s = 42, 148
        ay = (H - s) // 2
        if show_av:
            if not (avatar_bytes and paste_avatar(img, avatar_bytes, (x0, ay, s))):
                fallback_face(d, (x0, ay, s), name)
            d.ellipse([x0 - 3, ay - 3, x0 + s + 3, ay + s + 3], outline=(70, 70, 78), width=2)
            d.ellipse([x0, ay, x0 + s, ay + s], outline=accent, width=4)
            dx = x0 + s + 38
        else:
            dx = 48
        cb = (64, 64, 72)
        d.line([(W - 34, 14), (W - 16, 14)], fill=cb, width=2)
        d.line([(W - 16, 14), (W - 16, 32)], fill=cb, width=2)
        d.line([(W - 36, H - 14), (W - 20, H - 14)], fill=cb, width=2)
        d.line([(W - 20, H - 30), (W - 20, H - 14)], fill=cb, width=2)
        _tracked((dx, 52), title, f_lab, FAINT, tracking=4)
        d.text((dx, 78), name[:18], font=f_name, fill=INK)
        d.line([(dx, 148), (W - 40, 148)], fill=HAIR, width=1)
        if show_stat:
            d.text((dx, 166), stat[:64], font=f_stat, fill=DIM)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()

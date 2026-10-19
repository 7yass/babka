"""Reborn card presentation.

The art still lives on R2; the look is composited at reveal time. What
changed in the reboot:

  * a rarity SPINE down the left edge of the sharp card — rarity is readable
    at a glance without reintroducing a heavy border he already rejected
  * a real bottom scrim (gradient, not a flat bar) so stars and the card code
    read on top of any artwork, including bright scenes
  * the code is set in the scrim when the name is not baked into the art
  * the bleed is lifted slightly and tinted with the rarity accent, so the
    art separates from its background instead of sinking into it

Still borderless. Still full-bleed. Results cached by card id.
"""
import io
import urllib.request
from collections import OrderedDict

FRAME = {
    'C': (154, 160, 166),
    'R': (52, 152, 219),
    'SR': (155, 89, 182),
    'LR': (241, 196, 15),
    'UR': (231, 76, 60),
}
STARS = {'C': 1, 'R': 2, 'SR': 3, 'SR ': 3, 'LR': 4, 'UR': 5}
W, H = 600, 800
SCRIM_TOP = int(H * 0.74)      # where the bottom scrim starts fading in
SPINE_W = 5

_CACHE = OrderedDict()
_CACHE_MAX = 150
_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def _trim_bars(base):
    """Strip baked-in letterbox bars (many arts ship centred on solid black).

    Bails out if the trim would eat real content, so dark scenes survive.
    """
    try:
        g = base.convert('L')
        px, w, h = g.load(), g.width, g.height
        dark = lambda v: v < 14
        l = next((x for x in range(w)
                  if sum(1 for y in range(h) if dark(px[x, y])) / h < 0.98), 0)
        r = next((x for x in range(w - 1, -1, -1)
                  if sum(1 for y in range(h) if dark(px[x, y])) / h < 0.98), w - 1)
        t = next((y for y in range(h)
                  if sum(1 for x in range(w) if dark(px[x, y])) / w < 0.98), 0)
        b = next((y for y in range(h - 1, -1, -1)
                  if sum(1 for x in range(w) if dark(px[x, y])) / w < 0.98), h - 1)
        l, t = max(0, l - 2), max(0, t - 2)
        r, b = min(w - 1, r + 2), min(h - 1, b + 2)
        if (r - l) * (b - t) >= (base.width * base.height) * 0.5:
            return base.crop((l, t, r + 1, b + 1))
    except Exception:
        pass
    return base


def _scrim(img, height):
    """Gradient scrim pinned to the bottom — text reads on any artwork."""
    from PIL import Image as _Img
    grad = _Img.new('L', (1, height))
    px = grad.load()
    for y in range(height):
        f = y / max(1, height - 1)
        px[0, y] = int(255 * (f ** 1.7))
    grad = grad.resize((img.width, height))
    black = _Img.new('RGB', (img.width, height), (4, 4, 7))
    band = img.crop((0, img.height - height, img.width, img.height))
    img.paste(_Img.composite(black, band, grad), (0, img.height - height))
    return img


def frame_art(art: bytes, name: str, rarity: str, code: str = '') -> bytes:
    """Composite raw art -> reborn card PNG."""
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F, ImageFilter as _Fl
    from pathlib import Path as _P

    col = FRAME.get(rarity, FRAME['R'])
    base = _trim_bars(_Img.open(io.BytesIO(art)).convert('RGB'))

    # --- bleed: cover, blur, tint toward the rarity accent -------------
    scale = max(W / max(1, base.width), H / max(1, base.height))
    img = base.resize((max(1, int(base.width * scale)), max(1, int(base.height * scale))))
    x, y = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((x, y, x + W, y + H)).filter(_Fl.GaussianBlur(28))
    tint = _Img.new('RGB', (W, H), tuple(int(c * 0.18) for c in col))
    img = _Img.blend(img, tint, 0.30)
    img = _Img.blend(img, _Img.new('RGB', (W, H), (6, 6, 10)), 0.22)

    # --- sharp art, full height, centred ------------------------------
    fit = min(W / max(1, base.width), H / max(1, base.height))
    fg = base.resize((max(1, int(base.width * fit)), max(1, int(base.height * fit))))
    fx, fy = (W - fg.width) // 2, (H - fg.height) // 2
    img.paste(fg, (fx, fy))

    # --- rarity spine: the new identity marker -------------------------
    d = _Dr.Draw(img, 'RGBA')
    if fg.height >= H * 0.55:
        d.rectangle([fx, fy, fx + SPINE_W - 1, fy + fg.height - 1], fill=col + (235,))

    # --- bottom scrim --------------------------------------------------
    _scrim(img, H - SCRIM_TOP)

    # --- stars + code --------------------------------------------------
    # Stars sit TOP-LEFT, not bottom-centre: most of this art has the
    # character name baked into the lower third, and a bottom-centred star
    # row lands straight on top of it (the exact complaint that produced the
    # borderless pass). Top-left is the quietest corner across the set.
    try:
        assets = _P(__file__).parent.parent / 'assets'
        f_star = _F.truetype(str(assets / 'DejaVuSans.ttf'), 34)
        f_code = _F.truetype(str(assets / 'DejaVuSans.ttf'), 22)
    except Exception:
        f_star = f_code = _F.load_default()

    stars = '★' * STARS.get(rarity, 2)
    try:
        sw = d.textlength(stars, font=f_star)
    except Exception:
        sw = len(stars) * 22
    sx = fx + SPINE_W + 14
    sy = fy + 16
    # small dark pill so stars read over pale art
    d.rounded_rectangle([sx - 10, sy - 8, sx + sw + 10, sy + 44],
                        radius=12, fill=(0, 0, 0, 132))
    d.text((sx, sy), stars, font=f_star, fill=col, stroke_width=2, stroke_fill=(0, 0, 0))

    if code:
        try:
            cw = d.textlength(code, font=f_code)
        except Exception:
            cw = len(code) * 11
        cx = fx + fg.width - 20 - cw
        d.rounded_rectangle([cx - 12, H - 62, cx + cw + 12, H - 30],
                            radius=10, fill=(0, 0, 0, 150))
        d.text((cx, H - 58), code, font=f_code, fill=(214, 214, 222))

    # hairline top edge so the art has a defined boundary
    d.rectangle([fx, fy, fx + fg.width - 1, fy], fill=(255, 255, 255, 34))

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def get_framed_card(card: dict):
    """Composed PNG for a card row (id/name/rarity/image_url). Cached.
    Returns bytes or None (caller falls back to the raw URL)."""
    cid = (card or {}).get('id')
    url = (card or {}).get('image_url')
    if not cid or not url:
        return None
    hit = _CACHE.get(cid)
    if hit and hit[0] == url:
        _CACHE.move_to_end(cid)
        return hit[1]
    try:
        req = urllib.request.Request(url, headers=_UA)
        art = urllib.request.urlopen(req, timeout=10).read()
        if len(art) < 1024:
            return None
        out = frame_art(art, card.get('name') or cid,
                        card.get('rarity') or 'R', card.get('code') or '')
        _CACHE[cid] = (url, out)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        return out
    except Exception:
        return None


def cache_size() -> int:
    return len(_CACHE)

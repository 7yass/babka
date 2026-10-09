"""Per-rarity card frames (Lumina-style slabs).

Art stays on R2; the frame is composited at reveal time: rarity-colored
border (+ inner pinstripe on LR/UR), bottom name plate with stars.
Framed results are cached in memory by card id — art never re-downloads.
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
STARS = {'C': 1, 'R': 2, 'SR': 3, 'LR': 4, 'UR': 5}
W, H, BORDER = 600, 800, 14
_CACHE = OrderedDict()
_CACHE_MAX = 150
_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def frame_art(art: bytes, name: str, rarity: str) -> bytes:
    """Composite raw art -> framed card PNG."""
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    col = FRAME.get(rarity, FRAME['R'])
    base = _Img.open(io.BytesIO(art)).convert('RGB')
    # cover-crop to the inner area
    iw, ih = W - BORDER * 2, H - BORDER * 2
    scale = max(iw / max(1, base.width), ih / max(1, base.height))
    base = base.resize((max(1, int(base.width * scale)), max(1, int(base.height * scale))))
    x = (base.width - iw) // 2
    y = (base.height - ih) // 2
    base = base.crop((x, y, x + iw, y + ih))
    # dim the bottom for the name plate
    ov = _Img.new('L', (iw, ih), 0)
    _od = _Dr.Draw(ov)
    _od.rectangle([0, ih - 190, iw, ih], fill=200)
    dark = _Img.new('RGB', (iw, ih), (5, 5, 10))
    base = _Img.composite(dark, base, ov)
    img = _Img.new('RGB', (W, H), col)
    img.paste(base, (BORDER, BORDER))
    d = _Dr.Draw(img, 'RGBA')
    if rarity in ('LR', 'UR'):  # premium inner pinstripe + corner pips
        light = tuple(min(255, c + 70) for c in col)
        d.rectangle([BORDER + 6, BORDER + 6, W - BORDER - 7, H - BORDER - 7],
                    outline=light, width=3)
        for cx, cy in ((40, 40), (W - 40, 40), (40, H - 40), (W - 40, H - 40)):
            d.polygon([(cx, cy - 9), (cx + 7, cy), (cx, cy + 9), (cx - 7, cy)], fill=light)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_name = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 44)
        f_star = _F.truetype(str(_a / 'DejaVuSans.ttf'), 30)
    except Exception:
        f_name = f_star = _F.load_default()
    label = (name or '')[:34]
    try:
        nw = d.textlength(label, font=f_name)
    except Exception:
        nw = len(label) * 24
    d.text(((W - nw) / 2, H - 128), label, font=f_name, fill=(250, 250, 250))
    stars = '★' * STARS.get(rarity, 2)
    try:
        sw = d.textlength(stars, font=f_star)
    except Exception:
        sw = len(stars) * 18
    d.text(((W - sw) / 2, H - 72), stars, font=f_star, fill=col)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def get_framed_card(card: dict):
    """Framed PNG for a card row (id/name/rarity/image_url). Cached.
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
        out = frame_art(art, card.get('name') or cid, card.get('rarity') or 'R')
        _CACHE[cid] = (url, out)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        return out
    except Exception:
        return None


def cache_size() -> int:
    return len(_CACHE)

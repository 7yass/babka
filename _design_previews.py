"""Design previews: 3 overlay directions x (rank + wallet). Backgrounds default dark;
one bonus render shows Direction A on a simulated photo bg (custom bg_url stays supported).
Outputs: preview_{A|B|C}_rank.png, preview_{A|B|C}_bal.png, preview_A_rank_photo.png
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).parent
ASSETS = ROOT / 'assets'
OUT = ROOT

INK = (255, 255, 255)
DIM = (150, 150, 158)
FAINT = (96, 96, 104)
BG = (16, 16, 19)
HAIR = (54, 54, 60)
GOLD = (250, 200, 60)

TIER_COLORS = {
    'ROOKIE': (150, 150, 158), 'BRONZE': (205, 127, 50), 'SILVER': (184, 192, 204),
    'GOLD': (250, 200, 60), 'DIAMOND': (120, 190, 255), 'LEGEND': (200, 120, 255),
}

DATA = dict(name='Matrof', level=12, rank=3, xp=710, need=1972, pct=36,
            tier='SILVER', stars=2, cash=12450, streak=5, bank=8200)


def FB(size): return ImageFont.truetype(str(ASSETS / 'DejaVuSans-Bold.ttf'), size)
def FR(size): return ImageFont.truetype(str(ASSETS / 'DejaVuSans.ttf'), size)


def tracked(d, xy, text, font, fill, tracking=0, right=False, anchor_mid=False):
    """Letter-spaced text. Returns drawn width."""
    widths = [d.textlength(c, font=font) for c in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x, y = xy
    if right:
        x -= total
    elif anchor_mid:
        x -= total / 2
    for c, w in zip(text, widths):
        d.text((x, y), c, font=font, fill=fill)
        x += w + tracking
    return total


def avatar_img(size=300, color=(88, 88, 98)):
    img = Image.new('RGB', (size, size), color)
    d = ImageDraw.Draw(img)
    hi = tuple(min(255, int(c * 1.35)) for c in color)
    lo = tuple(max(0, int(c * 0.55)) for c in color)
    d.ellipse([size * 0.08, size * 0.05, size * 0.95, size * 0.85], fill=hi)
    d.ellipse([size * 0.15, size * 0.45, size * 1.05, size * 1.2], fill=lo)
    return img


def paste_circle(img, av, x, y, s):
    av = av.resize((s, s)).convert('RGB')
    mask = Image.new('L', (s, s), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, s, s], fill=255)
    img.paste(av, (x, y), mask)


def ring(d, x, y, s, color, w):
    d.ellipse([x, y, x + s, y + s], outline=color, width=w)


def base_card(w, h, bg=BG):
    img = Image.new('RGB', (w, h), bg)
    return img, ImageDraw.Draw(img, 'RGBA')


def knob_bar(d, img, x, y, w, h, pct, fill, track=(42, 42, 46), diamond=False):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill=track)
    fw = max(h, int(w * max(0.0, min(1.0, pct))))
    bar = Image.new('RGB', (fw, h), fill)
    m = Image.new('L', (fw, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, fw, h], radius=h / 2, fill=255)
    img.paste(bar, (int(x), int(y)), m)
    kx = x + fw - h / 2
    ky = y + h / 2
    if diamond:
        r = h * 0.9
        d.polygon([(kx, ky - r), (kx + r, ky), (kx, ky + r), (kx - r, ky)], fill=fill)
    else:
        r = h * 0.62
        d.ellipse([kx - r, ky - r, kx + r, ky + r], fill=fill, outline=(28, 28, 32), width=2)


def busy_bg(w, h):
    """Simulated blurred photo (custom bg_url stand-in)."""
    img = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(img)
    c1, c2 = (110, 60, 140), (40, 110, 130)
    for x in range(w):
        t = x / w
        d.line([(x, 0), (x, h)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    ov = Image.new('RGB', (w, h), (0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.ellipse([w * 0.55, -h * 0.4, w * 1.2, h * 0.6], fill=(255, 170, 60))
    d.ellipse([-w * 0.2, h * 0.4, w * 0.35, h * 1.3], fill=(60, 40, 160))
    ov = ov.filter(ImageFilter.GaussianBlur(60))
    img = Image.blend(img, ov, 0.55).filter(ImageFilter.GaussianBlur(14))
    dim = Image.new('RGB', (w, h), (10, 10, 12))
    return Image.blend(img, dim, 0.45)

# ---------------------------------------------------------------- Direction A
def rank_A(img, d, av, bg_is_photo=False):
    W, H = img.size
    ac = (240, 240, 246)
    tier_c = TIER_COLORS[DATA['tier']]
    x0, s = 42, 148
    paste_circle(img, av, x0, (H - s) // 2, s)
    ring(d, x0 - 3, (H - s) // 2 - 3, s + 6, (70, 70, 78), 2)
    ring(d, x0, (H - s) // 2, s, ac, 4)
    dx = x0 + s + 38
    d.line([(W - 34, 14), (W - 16, 14)], fill=(64, 64, 72), width=2)
    d.line([(W - 16, 14), (W - 16, 32)], fill=(64, 64, 72), width=2)
    d.line([(W - 36, H - 14), (W - 20, H - 14)], fill=(64, 64, 72), width=2)
    d.line([(W - 20, H - 30), (W - 20, H - 14)], fill=(64, 64, 72), width=2)
    name = DATA['name'].upper()
    d.text((dx, 26), name, font=FB(40), fill=INK)
    nw = d.textlength(name, font=FB(40))
    pill_x, pill_y = dx + nw + 18, 34
    tier_txt = DATA['tier'] + ' ' + '★' * DATA['stars']
    tw = d.textlength(tier_txt, font=FB(19)) + 26
    d.rounded_rectangle([pill_x, pill_y, pill_x + tw, pill_y + 32], radius=16,
                        outline=tier_c, width=2, fill=(22, 22, 26))
    d.text((pill_x + 13, pill_y + 6), tier_txt, font=FB(19), fill=tier_c)
    d.line([(dx, 92), (W - 40, 92)], fill=HAIR, width=1)
    cols = [('LEVEL', str(DATA['level'])), ('RANK', f"#{DATA['rank']}"),
            ('PROGRESS', f"{DATA['pct']}%")]
    cw = (W - dx - 60) / len(cols)
    for i, (lab, val) in enumerate(cols):
        cx = dx + 4 + i * cw
        tracked(d, (cx, 104), lab, FB(17), FAINT, tracking=3)
        d.text((cx, 126), val, font=FB(30), fill=INK)
        if i:
            d.line([(cx - 22, 106), (cx - 22, 158)], fill=HAIR, width=1)
    xp = f"{DATA['xp']} / {DATA['need']} XP"
    d.text((W - 40, 176), xp, font=FR(19), fill=DIM, anchor='ra')
    knob_bar(d, img, dx, 204, W - dx - 40, 13, DATA['xp'] / DATA['need'], ac)


def bal_A(img, d, av):
    W, H = img.size
    x0, s = 40, 132
    paste_circle(img, av, x0, (H - s) // 2, s)
    ring(d, x0 - 2, (H - s) // 2 - 2, s + 4, (70, 70, 78), 2)
    ring(d, x0, (H - s) // 2, s, GOLD, 4)
    d.rectangle([0, 0, 6, H], fill=GOLD)
    dx = x0 + s + 34
    d.text((dx, 30), DATA['name'], font=FB(30), fill=INK)
    d.text((dx, 74), f"{DATA['cash']:,}".replace(',', ' '), font=FB(56), fill=GOLD)
    d.text((dx, 148), 'COINS', font=FB(17), fill=FAINT)
    rx = 520
    rows = [('DAILY STREAK', f"{DATA['streak']} DAYS"),
            ('BANK', f"{DATA['bank']:,}".replace(',', ' '))]
    ry = 52
    for lab, val in rows:
        tracked(d, (rx, ry), lab, FB(16), FAINT, tracking=3)
        d.text((rx, ry + 24), val, font=FB(26), fill=INK)
        ry += 66
        d.line([(rx, ry - 12), (W - 40, ry - 12)], fill=HAIR, width=1)

# ---------------------------------------------------------------- Direction B
def glass(d, box, radius=22, fill=(255, 255, 255, 16), outline=(255, 255, 255, 36), w=2):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=w)


def tier_chip_B(d, img, x, y, text, size=20):
    f = FB(size)
    tw = d.textlength(text, font=f)
    w, h = int(tw + 34), int(size + 18)
    c1 = TIER_COLORS[DATA['tier']]
    c2 = tuple(int(c * 0.55) for c in c1)
    grad = Image.new('RGB', (w, h))
    gd = ImageDraw.Draw(grad)
    for i in range(w):
        t = i / max(1, w - 1)
        gd.line([(i, 0), (i, h)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=255)
    img.paste(grad, (int(x), int(y)), mask)
    d.text((x + 17, y + 4), text, font=f, fill=(18, 18, 22))
    return w


def rank_B(img, d, av, bg_is_photo=False):
    W, H = img.size
    s = 150
    ax, ay = 36, (H - s) // 2
    glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-120, H - 160, 320, H + 120], fill=TIER_COLORS[DATA['tier']] + (46,))
    img.paste(Image.composite(glow.convert('RGB'), img.copy(), glow.split()[3]), (0, 0))
    d = ImageDraw.Draw(img, 'RGBA')
    tile_pad = 14
    glass(d, [ax - tile_pad, ay - tile_pad, ax + s + tile_pad, ay + s + tile_pad], radius=30)
    paste_circle(img, av, ax, ay, s)
    ring(d, ax, ay, s, (255, 255, 255, 70), 3)
    dx = ax + s + 40
    top, bot = 20, H - 20
    glass(d, [dx, top, W - 36, bot], radius=24)
    d.text((dx + 26, top + 18), DATA['name'], font=FB(38), fill=INK)
    tw = d.textlength(DATA['name'], font=FB(38))
    tier_chip_B(d, img, dx + 26 + tw + 16, top + 24, DATA['tier'] + ' ' + '★' * DATA['stars'])
    d = ImageDraw.Draw(img, 'RGBA')
    tiles = [('LEVEL', str(DATA['level'])), ('RANK', f"#{DATA['rank']}"),
             ('PROGRESS', f"{DATA['pct']}%")]
    tw_total = (W - 36) - (dx + 26)
    gap = 12
    tile_w = (tw_total - gap * (len(tiles) - 1)) / len(tiles)
    ty = top + 84
    for i, (lab, val) in enumerate(tiles):
        bx = dx + 26 + i * (tile_w + gap)
        glass(d, [bx, ty, bx + tile_w, ty + 56], radius=16, fill=(0, 0, 0, 90),
              outline=(255, 255, 255, 24), w=1)
        tracked(d, (bx + 14, ty + 7), lab, FB(14), FAINT, tracking=2)
        d.text((bx + 14, ty + 26), val, font=FB(24), fill=INK)
    sy = bot - 52
    d.text((W - 62, sy - 26), f"{DATA['xp']} / {DATA['need']} XP", font=FR(17), fill=DIM, anchor='ra')
    knob_bar(d, img, dx + 26, sy + 8, tw_total - 52, 12, DATA['xp'] / DATA['need'], (255, 255, 255))


def bal_B(img, d, av):
    W, H = img.size
    glass(d, [24, 20, 336, H - 20], radius=26)
    s = 108
    paste_circle(img, av, 60, 36, s)
    ring(d, 60, 36, s, (255, 255, 255, 80), 3)
    d.text((60, 152), DATA['name'], font=FB(26), fill=INK)
    d.rounded_rectangle([60, 188, 178, 214], radius=13, fill=(0, 0, 0, 100),
                        outline=(255, 255, 255, 30), width=1)
    d.text((72, 192), f"{DATA['streak']} DAY", font=FB(15), fill=GOLD)
    glass(d, [360, 20, W - 24, H - 20], radius=26)
    tracked(d, (392, 40), 'BALANCE', FB(15), FAINT, tracking=4)
    d.text((392, 66), f"{DATA['cash']:,}".replace(',', ' '), font=FB(58), fill=GOLD)
    d.line([(392, 150), (W - 56, 150)], fill=(255, 255, 255, 30), width=1)
    tracked(d, (392, 162), 'BANK', FB(15), FAINT, tracking=3)
    d.text((470, 158), f"{DATA['bank']:,}".replace(',', ' '), font=FB(26), fill=INK)

# ---------------------------------------------------------------- Direction C
def rank_C(img, d, av, bg_is_photo=False):
    W, H = img.size
    tier_c = TIER_COLORS[DATA['tier']]
    d.rectangle([0, 0, W, 3], fill=tier_c)
    av_mono = av.convert('L').convert('RGB')
    s = 150
    ax = W - s - 44
    ay = (H - s) // 2 - 4
    paste_circle(img, av_mono, ax, ay, s)
    ring(d, ax - 4, ay - 4, s + 8, (58, 58, 64), 6)
    ring(d, ax, ay, s, tier_c, 2)
    d.text((48, 24), DATA['name'], font=FB(54), fill=INK)
    tier_txt = DATA['tier'] + '   ' + '★' * DATA['stars']
    tracked(d, (50, 100), tier_txt, FB(21), tier_c, tracking=6)
    mid = f"LVL {DATA['level']}   ·   RANK #{DATA['rank']}   ·   {DATA['pct']}%"
    d.text((50, 142), mid, font=FR(24), fill=DIM)
    xp = f"{DATA['xp']} / {DATA['need']} XP"
    d.text((ax - 24, 182), xp, font=FR(18), fill=FAINT, anchor='ra')
    knob_bar(d, img, 50, 212, W - 100, 7, DATA['xp'] / DATA['need'], tier_c,
             track=(38, 38, 44), diamond=True)


def bal_C(img, d, av):
    W, H = img.size
    tier_c = GOLD
    d.rectangle([0, 0, W, 3], fill=tier_c)
    av_mono = av.convert('L').convert('RGB')
    s = 120
    ax = W - s - 44
    paste_circle(img, av_mono, ax, (H - s) // 2, s)
    ring(d, ax, (H - s) // 2, s, (58, 58, 64), 5)
    d.text((48, 28), 'WALLET', font=FB(18), fill=FAINT)
    d.text((48, 58), f"{DATA['cash']:,}".replace(',', ' '), font=FB(66), fill=tier_c)
    d.text((50, 142), DATA['name'], font=FR(26), fill=INK)
    rows = f"{DATA['streak']} DAY STREAK   ·   BANK {DATA['bank']:,}".replace(',', ' ')
    d.text((50, 182), rows, font=FR(20), fill=DIM)


# ---------------------------------------------------------------- main
def main():
    av = avatar_img()
    dirs = {'A': (rank_A, bal_A), 'B': (rank_B, bal_B), 'C': (rank_C, bal_C)}
    for key, (rf, bf) in dirs.items():
        img, d = base_card(900, 260)
        rf(img, d, av)
        img.save(OUT / f'preview_{key}_rank.png')
        img, d = base_card(800, 220)
        bf(img, d, av)
        img.save(OUT / f'preview_{key}_bal.png')
    img = busy_bg(900, 260)
    d = ImageDraw.Draw(img, 'RGBA')
    rank_A(img, d, av, bg_is_photo=True)
    img.save(OUT / 'preview_A_rank_photo.png')
    print('previews written')


if __name__ == '__main__':
    main()


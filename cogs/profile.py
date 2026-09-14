"""Player profile card: level, XP, cash, streak, job, top role, staff badge.
Same Direction A style as the rank card. `.profile [@user]` (alias: profil).
`info` name is taken by the FAQ panels cog, hence `profile`."""
import discord
from discord.ext import commands

from utils.cards import short as cshort

STAFF_ROLE_ID = '1546511055793033226'


def profile_card(name: str, level: int, xp: int, need: int, rank: int,
                 tier_name: str, stars: int, tier_color: tuple,
                 cash: int, bank: int, streak: int,
                 job_txt: str, role_txt: str, is_staff: bool,
                 badges: list = None,
                 avatar_bytes: bytes = None, bg_bytes: bytes = None) -> bytes:
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F, ImageFilter as _Fl
    from pathlib import Path as _P
    W, H = 900, 478
    GOLD = (250, 200, 60)
    INK = (255, 255, 255)
    FAINT = (96, 96, 104)
    DIM = (150, 150, 158)
    HAIR = (54, 54, 60)
    if bg_bytes:
        try:
            bg = _Img.open(_io.BytesIO(bg_bytes)).convert('RGB')
            scale = max(W / max(bg.width, 1), H / max(bg.height, 1))
            bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
            x = (bg.width - W) // 2
            y = (bg.height - H) // 2
            bg = bg.crop((x, y, x + W, y + H)).filter(_Fl.GaussianBlur(22))
            dim = _Img.new('RGB', (W, H), (10, 10, 12))
            img = _Img.blend(bg, dim, 0.60)
        except Exception:
            img = _Img.new('RGB', (W, H), (16, 16, 19))
    else:
        img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_name = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 34)
        f_pill = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 18)
        f_lab = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 15)
        f_val = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 26)
        f_sub = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 22)
        f_badge = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 13)
        f_xp = _F.truetype(str(_a / 'DejaVuSans.ttf'), 17)
    except Exception:
        f_name = f_pill = f_lab = f_val = f_sub = f_xp = _F.load_default()
        f_badge = f_xp

    def tracked(xy, text, font, fill, tracking=3):
        x, y = xy
        for ch in text:
            d.text((x, y), ch, font=font, fill=fill)
            try:
                x += d.textlength(ch, font=font) + tracking
            except Exception:
                x += 12 + tracking
        return x

    def pill(x, y, text, font, color):
        try:
            tw = d.textlength(text, font=font) + 26
        except Exception:
            tw = len(text) * 11 + 26
        d.rounded_rectangle([x, y, x + tw, y + 30], radius=15,
                            outline=color, width=2, fill=(22, 22, 26))
        d.text((x + 13, y + 5), text, font=font, fill=color)
        return tw

    # avatar
    x0, s, ay = 42, 148, 36
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
            _fl = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 72)
        except Exception:
            _fl = f_name
        try:
            d.text((x0 + s / 2, ay + s / 2), (name or '?')[:1].upper(),
                   font=_fl, fill=(220, 220, 225), anchor='mm')
        except Exception:
            pass
    ring_c = GOLD if is_staff else (240, 240, 246)
    d.ellipse([x0 - 3, ay - 3, x0 + s + 3, ay + s + 3], outline=(70, 70, 78), width=2)
    d.ellipse([x0, ay, x0 + s, ay + s], outline=ring_c, width=4)

    # corner brackets
    cb = (64, 64, 72)
    d.line([(W - 34, 14), (W - 16, 14)], fill=cb, width=2)
    d.line([(W - 16, 14), (W - 16, 32)], fill=cb, width=2)
    d.line([(W - 36, H - 14), (W - 20, H - 14)], fill=cb, width=2)
    d.line([(W - 20, H - 30), (W - 20, H - 14)], fill=cb, width=2)

    dx = x0 + s + 38
    d.text((dx, 38), name.upper()[:16], font=f_name, fill=INK)
    px = dx
    tier_txt = tier_name + (' ' + '★' * stars if stars else '')
    px += pill(px, 94, tier_txt, f_pill, tier_color) + 10
    if is_staff:
        pill(px, 94, 'STAFF', f_pill, GOLD)
    d.line([(dx, 140), (W - 40, 140)], fill=HAIR, width=1)

    pct = round(xp / need * 100) if need else 0
    cols = [('LEVEL', str(level)), ('RANK', f'#{rank}'), ('PROGRESS', f'{pct}%'),
            ('CASH', cshort(cash)),
            ('BANK', cshort(bank)),
            ('STREAK', f'{streak} DAYS' if streak else '—')]
    cw = (W - dx - 20) / 3
    for i, (lab, val) in enumerate(cols):
        cx = dx + 4 + (i % 3) * cw
        ry = 152 if i < 3 else 222
        tracked((cx, ry), lab, f_lab, FAINT)
        d.text((cx, ry + 22), val, font=f_val, fill=GOLD if lab in ('CASH', 'BANK') else INK)
        if i % 3:
            d.line([(cx - 20, ry + 2), (cx - 20, ry + 52)], fill=HAIR, width=1)

    def fit(text, font, max_w):
        text = text or '—'
        try:
            while text and d.textlength(text, font=font) > max_w:
                text = text[:-1]
            if text != (text or '—'):
                text = text.rstrip() + '…'
        except Exception:
            text = text[:24]
        return text

    half = (W - dx - 20) / 2
    tracked((dx, 296), 'JOB', f_lab, FAINT)
    d.text((dx, 318), fit(job_txt, f_sub, half - 12), font=f_sub, fill=INK)
    tracked((dx + half, 296), 'TOP ROLE', f_lab, FAINT)
    d.text((dx + half, 318), fit(role_txt, f_sub, W - 40 - (dx + half)), font=f_sub, fill=INK)

    # badges strip
    tracked((dx, 350), 'BADGES', f_lab, FAINT)
    _bx = dx
    for _b in (badges or [])[:5]:
        try:
            _tw = d.textlength(_b, font=f_badge) + 18
        except Exception:
            _tw = len(_b) * 8 + 18
        if _bx + _tw > W - 120:
            break
        d.rounded_rectangle([_bx, 368, _bx + _tw, 368 + 24], radius=12,
                            outline=(250, 200, 60), width=1, fill=(26, 24, 16))
        d.text((_bx + 9, 372), _b, font=f_badge, fill=(250, 200, 60))
        _bx += _tw + 8
    _extra = len(badges or []) - 5
    if _extra > 0:
        d.text((_bx, 372), f'+{_extra}', font=f_badge, fill=DIM)

    # xp bar
    bx, bw, bh, by = dx, W - dx - 40, 12, 420
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(42, 42, 46))
    fw = max(bh, int(bw * max(0.0, min(1.0, xp / need if need else 0))))
    bar = _Img.new('RGB', (fw, bh), (240, 240, 246))
    m = _Img.new('L', (fw, bh), 0)
    _Dr.Draw(m).rounded_rectangle([0, 0, fw, bh], radius=bh // 2, fill=255)
    img.paste(bar, (bx, by), m)
    kx = bx + fw - bh // 2
    d.ellipse([kx - bh // 2, by - 3, kx + bh // 2, by + bh + 3],
              fill=(255, 255, 255), outline=(30, 30, 34), width=2)
    d.text((W - 40, 398), f'{cshort(xp)} / {cshort(need)} XP', font=f_xp, fill=DIM, anchor='ra')

    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name='profile', description='Profil gracza', aliases=['profil'])
    async def profile(self, ctx, member: discord.Member = None):
        import aiohttp
        import asyncio as _aio
        await ctx.defer()
        member = member or ctx.author
        gid = ctx.guild.id
        from cogs.levels import get_user, get_rank, xp_needed, TIER_COLORS, _tier_key
        from cogs.gamble import bal
        from cogs.jobs import get_job, JOBS, JOB_ALIAS, job_title, ladder_of
        from cogs.achievements import badge_shorts, maybe_award
        from utils.cards import tier_for, get_tier_names
        try:
            maybe_award(gid, member.id)
        except Exception:
            pass

        data = get_user(gid, member.id)
        rank = get_rank(gid, member.id)
        need = xp_needed(data['level'])
        tier_name, _, stars = tier_for(data['level'], get_tier_names(gid))
        tier_color = TIER_COLORS.get(_tier_key(data['level']), (240, 240, 246))
        b = bal(gid, member.id)

        j = get_job(gid, member.id)
        jkey = JOB_ALIAS.get(j.get('job'), j.get('job'))
        if jkey in JOBS:
            idx, _, _, _ = ladder_of(data['level'])
            job_txt = f"{JOBS[jkey]['label']} — {job_title(jkey, idx, member.id)}"
        else:
            job_txt = '—'
        roles = [r for r in getattr(member, 'roles', []) if not r.is_default()]
        roles.sort(key=lambda r: r.position, reverse=True)
        role_txt = roles[0].name if roles else '—'
        is_staff = any(str(r.id) == STAFF_ROLE_ID for r in getattr(member, 'roles', []))
        try:
            badges = badge_shorts(gid, member.id)
        except Exception:
            badges = []

        async def grab(session, url):
            try:
                async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                                       timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status == 200:
                        return await r.read()
            except Exception:
                return None
            return None

        async with aiohttp.ClientSession() as session:
            avatar_task = _aio.ensure_future(
                grab(session, str(member.display_avatar.with_size(256).url)))
            try:
                u = await self.bot.fetch_user(member.id)
                banner_url = str(u.banner.with_size(1024).url) if u and u.banner else None
            except Exception:
                banner_url = None
            if banner_url:
                banner, avatar = await _aio.gather(grab(session, banner_url), avatar_task)
            else:
                banner, avatar = None, await avatar_task

        png = await self.bot.loop.run_in_executor(
            None, profile_card, member.display_name,
            data['level'], data['xp'], need, rank,
            tier_name, stars, tier_color,
            b['cash'], b.get('bank') or 0, b.get('daily_streak') or 0,
            job_txt, role_txt, is_staff, badges, avatar, banner)
        await ctx.reply(file=discord.File(__import__('io').BytesIO(png), 'profile.png'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Profile(bot))

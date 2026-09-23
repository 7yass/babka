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
    d.rectangle([0, 0, 6, H], fill=GOLD)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_name = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 34)
        f_pill = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 18)
        f_lab = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 15)
        f_val = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 26)
        f_sub = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 22)
        f_medal = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 22)
        f_badge = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 13)
        f_xp = _F.truetype(str(_a / 'DejaVuSans.ttf'), 17)
    except Exception:
        f_name = f_pill = f_lab = f_val = f_sub = f_medal = f_xp = _F.load_default()
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
    ring_c = GOLD if is_staff else tier_color
    d.ellipse([x0 - 3, ay - 3, x0 + s + 3, ay + s + 3], outline=(70, 70, 78), width=2)
    d.ellipse([x0, ay, x0 + s, ay + s], outline=ring_c, width=4)

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

    # medals strip — CoD-style medallions: metal ring, dark disc, glyph.
    # badges = [(glyph, (r, g, b)), ...] in unlock order.
    tracked((dx, 350), 'MEDALS', f_lab, FAINT)
    _mx, _md, _my = dx, 44, 368
    _shown = 0
    for _item in (badges or [])[:8]:
        try:
            _g, _mc = _item
        except Exception:
            continue
        if _mx + _md > W - 40:
            break
        _cx, _cy = _mx + _md // 2, _my + _md // 2
        d.ellipse([_mx, _my, _mx + _md, _my + _md], fill=(24, 24, 28),
                  outline=(70, 70, 78), width=2)
        d.ellipse([_mx + 3, _my + 3, _mx + _md - 3, _my + _md - 3],
                  outline=tuple(_mc), width=3)
        d.arc([_mx + 8, _my + 5, _mx + _md - 8, _my + _md - 5],
              start=200, end=340, fill=(90, 90, 98), width=2)
        try:
            d.text((_cx, _cy), _g, font=f_medal, fill=tuple(_mc), anchor='mm')
        except Exception:
            d.text((_cx - 8, _cy - 12), _g, font=f_medal, fill=tuple(_mc))
        _mx += _md + 10
        _shown += 1
    _extra = len(badges or []) - _shown
    if _extra > 0:
        d.text((_mx, _my + 12), f'+{_extra}', font=f_badge, fill=DIM)

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


def identity_card(title_label: str, head: str, rows: list,
                  buddy_name: str = None, buddy_bytes: bytes = None,
                  no_buddy_label: str = 'No buddy') -> bytes:
    """Second profile card: identity info left, buddy art big right.
    Same Direction A voice as profile_card. rows = [(label, value)]."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    W, H = 900, 380
    GOLD = (250, 200, 60)
    INK = (255, 255, 255)
    FAINT = (96, 96, 104)
    DIM = (150, 150, 158)
    HAIR = (54, 54, 60)
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    try:
        _a = _P(__file__).parent.parent / 'assets'
        f_head = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 30)
        f_lab = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 15)
        f_row = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 22)
        f_cap = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 20)
    except Exception:
        f_head = f_lab = f_row = f_cap = _F.load_default()

    def tracked(xy, text, font, fill, tracking=3):
        x, y = xy
        for ch in text:
            d.text((x, y), ch, font=font, fill=fill)
            try:
                x += d.textlength(ch, font=font) + tracking
            except Exception:
                x += 12 + tracking

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

    # left gold edge (brand bar); top gold rule kept as the identity marker
    d.rectangle([0, 0, 6, H], fill=(250, 200, 60))
    d.line([(42, 14), (W - 42, 14)], fill=(250, 200, 60), width=2)

    # left: identity info
    tracked((42, 28), (title_label or 'IDENTITY').upper(), f_lab, FAINT)
    d.text((42, 54), fit(head, f_head, 460), font=f_head, fill=INK)
    d.line([(42, 100), (508, 100)], fill=HAIR, width=1)
    y = 114
    for lab, val in (rows or [])[:6]:
        tracked((42, y), str(lab or '').upper(), f_lab, FAINT)
        try:
            lx = 42 + max(d.textlength(str(lab or '').upper(), font=f_lab)
                          + 3 * len(str(lab or '')) + 14, 150)
        except Exception:
            lx = 192
        d.text((lx, y - 4), fit(val, f_row, 508 - lx), font=f_row, fill=INK)
        y += 40

    # divider
    d.line([(534, 28), (534, H - 28)], fill=HAIR, width=1)

    # right: buddy art, big
    cx0, cx1, cy0, cy1 = 560, 858, 28, 300
    pasted = False
    if buddy_bytes:
        try:
            sp = _Img.open(_io.BytesIO(buddy_bytes)).convert('RGBA')
            sp.thumbnail((cx1 - cx0, cy1 - cy0), _Img.LANCZOS)
            ox = cx0 + (cx1 - cx0 - sp.width) // 2
            oy = cy0 + (cy1 - cy0 - sp.height) // 2
            img.paste(sp, (ox, oy), sp)
            pasted = True
        except Exception:
            pass
    if not pasted:
        try:
            d.ellipse([cx0 + 69, cy0 + 51, cx0 + 229, cy0 + 211], fill=(42, 42, 46))
            _fl = _F.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 96)
        except Exception:
            _fl = f_head
        try:
            d.text(((cx0 + cx1) / 2, (cy0 + cy1) / 2), '?', font=_fl,
                   fill=(220, 220, 225), anchor='mm')
        except Exception:
            pass
    try:
        cap = fit(buddy_name or no_buddy_label, f_cap, cx1 - cx0)
        tw = d.textlength(cap, font=f_cap)
        d.text(((cx0 + cx1 - tw) / 2, 312), cap, font=f_cap,
               fill=GOLD if buddy_name else DIM)
    except Exception:
        pass

    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def stack_cards(top_png: bytes, bottom_png: bytes, gap: int = 12) -> bytes:
    """Stack two cards vertically into one image. Discord grids multiple
    attachments side-by-side (clipped); one tall file always renders
    top-over-bottom."""
    import io as _io
    from PIL import Image as _Img
    t = _Img.open(_io.BytesIO(top_png)).convert('RGB')
    b = _Img.open(_io.BytesIO(bottom_png)).convert('RGB')
    w = max(t.width, b.width)
    img = _Img.new('RGB', (w, t.height + gap + b.height), (16, 16, 19))
    img.paste(t, ((w - t.width) // 2, 0))
    img.paste(b, ((w - b.width) // 2, t.height + gap))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _buddy_art_url(self, gid, uid):
        """Official artwork for the buddy (form-aware), pixel fallback."""
        try:
            from cogs.pokemon import form_sprite, _dex_row, pix_url
            from utils.identity import get_buddy_mon
            mon = get_buddy_mon(gid, uid)
            if not mon:
                return None
            shiny = bool(mon.get('shiny'))
            art = form_sprite(mon, shiny)
            if art:
                return art
            spr = ((_dex_row(mon.get('dex', 0)).get('sprite')) or '').split('|')
            if shiny and len(spr) > 1 and spr[1]:
                return spr[1]
            if spr and spr[0]:
                return spr[0]
            return pix_url(mon.get('dex', 0), shiny)
        except Exception:
            return None

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
        from cogs.achievements import badge_medals, maybe_award
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
            badges = badge_medals(gid, member.id)
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

        async def _fetch_all():
            async with aiohttp.ClientSession() as session:
                avatar_task = _aio.ensure_future(
                    grab(session, str(member.display_avatar.with_size(256).url)))
                buddy_task = _aio.ensure_future(
                    grab(session, self._buddy_art_url(gid, member.id)))
                try:
                    u = await _aio.wait_for(self.bot.fetch_user(member.id), timeout=3)
                    banner_url = str(u.banner.with_size(1024).url) if u and u.banner else None
                except Exception:
                    banner_url = None
                if banner_url:
                    _banner, _avatar, _buddy = await _aio.gather(
                        grab(session, banner_url), avatar_task, buddy_task)
                    return _banner, _avatar, _buddy
                _avatar, _buddy = await _aio.gather(avatar_task, buddy_task)
                return None, _avatar, _buddy

        try:
            banner, avatar, buddy_art = await _aio.wait_for(_fetch_all(), timeout=9)
        except Exception:
            banner, avatar, buddy_art = None, None, None
        if avatar is None:
            try:
                avatar = await _aio.wait_for(member.display_avatar.read(), timeout=4)
            except Exception:
                avatar = None

        png = await self.bot.loop.run_in_executor(
            None, profile_card, member.display_name,
            data['level'], data['xp'], need, rank,
            tier_name, stars, tier_color,
            b['cash'], b.get('bank') or 0, b.get('daily_streak') or 0,
            job_txt, role_txt, is_staff, badges, avatar, banner)
        # Identity card: same data as before, rendered as a second image
        # card (buddy art right, info left) stacked UNDER the main card
        # into one file — Discord grids separate attachments side-by-side.
        # Any failure falls back to the main card alone: never break .profile.
        out_png = png
        try:
            from lang import t as _t
            from utils.identity import (get_trainer_class, get_top_achievement,
                                        get_dex_completion, get_duel_record,
                                        get_activity_title, get_buddy_name,
                                        get_regions, get_best_streak)
            _cls = get_trainer_class(gid, member.id)
            _title = get_activity_title(gid, member.id)
            _tkey = f'pf.title_{_title}'
            _thead = _t(gid, _tkey) if _title in (
                'gym_champion', 'master_collector', 'market_maker',
                'crime_boss', 'job_specialist', 'regional_expert',
                'newcomer') else _t(gid, f'pf.class_{_title}')
            _head = f"{_t(gid, f'pf.class_{_cls}') + ' · ' if _cls else ''}{_thead}"
            _dex = get_dex_completion(gid, member.id)
            _du = get_duel_record(gid, member.id)
            _rows = [(_t(gid, 'pf.l_dex'), f"{_dex['distinct']}/{_dex['total']} · {_dex['pct']}%"),
                     (_t(gid, 'pf.l_duels'), f"{_du['w']}W–{_du['l']}L")]
            _top = get_top_achievement(gid, member.id)
            if _top:
                _rows.append((_t(gid, 'pf.l_top'), _top[1]))
            _rg = get_regions(gid, member.id)
            _rows.append((_t(gid, 'pf.l_regions'), f"{_rg['have']}/{_rg['total']}"))
            _streak = get_best_streak(gid, member.id)
            if _streak:
                _rows.append((_t(gid, 'pf.l_streak'), str(_streak)))
            _buddy = get_buddy_name(gid, member.id)
            _ipng = await self.bot.loop.run_in_executor(
                None, identity_card, _t(gid, 'pf.identity'), _head, _rows,
                _buddy, buddy_art, _t(gid, 'pf.no_buddy'))
            out_png = await self.bot.loop.run_in_executor(None, stack_cards, png, _ipng)
        except Exception:
            pass
        await ctx.reply(file=discord.File(__import__('io').BytesIO(out_png), 'profile.png'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Profile(bot))

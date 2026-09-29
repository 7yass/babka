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
    """Midnight Gold profile: header + 6 glass stat tiles + job/role rows +
    medal strip + gold XP bar. Same signature as before."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    from utils import cardstyle as cs
    W, H = 900, 500
    if bg_bytes:
        try:
            from utils.cards import cover as _cover
            from PIL import ImageFilter as _Fl
            bg = _cover(_Img.open(_io.BytesIO(bg_bytes)).convert('RGB'), W, H).filter(_Fl.GaussianBlur(22))
            dim = _Img.new('RGB', (W, H), (10, 12, 22))
            img = _Img.blend(bg, dim, 0.60)
            img = cs.vignette(img)
        except Exception:
            img = cs.base(W, H)
    else:
        img = cs.base(W, H)
    d = _Dr.Draw(img, 'RGBA')
    cs.edge_bars(d, W, H)
    ring_c = cs.GOLD if is_staff else tier_color
    x0, s, ay = 42, 132, 34
    if not (avatar_bytes and cs.avatar(img, avatar_bytes, (x0, ay, s), ring_c)):
        cs.fallback(img, (x0, ay, s), name)
    d = _Dr.Draw(img, 'RGBA')
    dx = x0 + s + 32
    d.text((dx, 34), cs.safe_img(name).upper()[:16], font=cs.f(34), fill=cs.INK)
    px = dx
    tier_txt = tier_name + (' ' + '★' * stars if stars else '')
    px += cs.pill(d, px, 82, tier_txt, cs.f(18), tier_color, tier_color) + 10
    if is_staff:
        cs.pill(d, px, 82, 'STAFF', cs.f(18), cs.COIN_INK, cs.GOLD, solid=True)
    d.line([(dx, 128), (W - 42, 128)], fill=cs.HAIR, width=1)
    pct = round(xp / need * 100) if need else 0
    cols = [('LEVEL', str(level), cs.INK), ('RANK', f'#{rank}', cs.INK), ('PROGRESS', f'{pct}%', cs.INK),
            ('CASH', cshort(cash), cs.GOLD), ('BANK', cshort(bank), cs.GOLD),
            ('STREAK', f'{streak} DAYS' if streak else '—', cs.INK)]
    cw = (W - 42 - dx - 24) / 3
    for i, (lab, val, col) in enumerate(cols):
        cx = dx + (i % 3) * (cw + 12)
        ry = 140 if i < 3 else 212
        cs.glass(d, [cx, ry, cx + cw, ry + 62], radius=12)
        cs.tracked(d, (cx + 14, ry + 8), lab, cs.f(14), cs.FAINT)
        d.text((cx + 14, ry + 28), cs.safe_img(val)[:14], font=cs.f(24), fill=col)
    half = (W - 42 - dx - 12) / 2
    cs.tracked(d, (dx, 288), 'JOB', cs.f(14), cs.FAINT)
    d.text((dx, 308), cs.safe_img(job_txt or '—')[:30], font=cs.f(22), fill=cs.INK)
    cs.tracked(d, (dx + half, 288), 'TOP ROLE', cs.f(14), cs.FAINT)
    d.text((dx + half, 308), cs.safe_img(role_txt or '—')[:22], font=cs.f(22), fill=cs.INK)
    cs.tracked(d, (dx, 344), 'MEDALS', cs.f(14), cs.FAINT)
    _mx, _md, _my = dx, 44, 362
    _shown = 0
    for _item in (badges or [])[:8]:
        try:
            _g, _mc = _item
        except Exception:
            continue
        if _mx + _md > W - 42:
            break
        _cx, _cy = _mx + _md // 2, _my + _md // 2
        d.ellipse([_mx - 2, _my - 2, _mx + _md + 2, _my + _md + 2], fill=(10, 10, 14))
        d.ellipse([_mx, _my, _mx + _md, _my + _md], fill=(16, 19, 34),
                  outline=tuple(_mc), width=3)
        try:
            d.text((_cx, _cy), cs.safe_img(str(_g))[:2] or '•', font=cs.f(22),
                   fill=tuple(_mc), anchor='mm')
        except Exception:
            pass
        _mx += _md + 12
        _shown += 1
    _extra = len(badges or []) - _shown
    if _extra > 0:
        d.text((_mx, _my + 12), f'+{_extra}', font=cs.f(15, False), fill=cs.DIM)
    try:
        d.text((W - 42, 414), f'{cshort(xp)} / {cshort(need)} XP', font=cs.f(17, False),
               fill=cs.DIM, anchor='ra')
    except Exception:
        pass
    cs.xpbar(img, dx, 438, W - 42 - dx, 13, (xp / need if need else 0))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def identity_card(title_label: str, head: str, rows: list,
                  buddy_name: str = None, buddy_bytes: bytes = None,
                  no_buddy_label: str = 'No buddy') -> bytes:
    """Midnight Gold identity card: info left, buddy showcase right."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    from utils import cardstyle as cs
    W, H = 900, 380
    img = cs.base(W, H)
    d = _Dr.Draw(img, 'RGBA')
    cs.edge_bars(d, W, H)
    d.line([(42, 14), (W - 42, 14)], fill=cs.GOLD, width=2)
    cs.tracked(d, (42, 28), cs.safe_img(title_label or 'IDENTITY').upper(), cs.f(15), cs.FAINT)
    d.text((42, 52), cs.safe_img(head)[:40], font=cs.f(30), fill=cs.INK)
    d.line([(42, 98), (508, 98)], fill=cs.HAIR, width=1)
    y = 110
    for lab, val in (rows or [])[:6]:
        cs.tracked(d, (42, y), cs.safe_img(str(lab or '')).upper()[:18], cs.f(15), cs.FAINT)
        try:
            lx = 42 + max(d.textlength(cs.safe_img(str(lab or '')).upper()[:18], font=cs.f(15))
                          + 3 * len(str(lab or '')) + 14, 170)
        except Exception:
            lx = 212
        d.text((lx, y - 4), cs.safe_img(val)[:30], font=cs.f(22), fill=cs.INK)
        y += 40
    d.line([(534, 28), (534, H - 28)], fill=cs.HAIR, width=1)
    cs.glass(d, [560, 28, 858, 300], radius=18)
    cx0, cx1, cy0, cy1 = 560, 858, 28, 300
    pasted = False
    if buddy_bytes:
        try:
            sp = _Img.open(_io.BytesIO(buddy_bytes)).convert('RGBA')
            sp.thumbnail((cx1 - cx0 - 24, cy1 - cy0 - 24), _Img.LANCZOS)
            ox = cx0 + (cx1 - cx0 - sp.width) // 2
            oy = cy0 + (cy1 - cy0 - sp.height) // 2
            img.paste(sp, (ox, oy), sp)
            pasted = True
            d = _Dr.Draw(img, 'RGBA')
        except Exception:
            pass
    if not pasted:
        try:
            d.ellipse([cx0 + 119, cy0 + 56, cx0 + 179, cy0 + 116], fill=(40, 45, 68))
            d.text(((cx0 + cx1) / 2, (cy0 + cy1) / 2), '?', font=cs.f(96),
                   fill=(220, 222, 232), anchor='mm')
        except Exception:
            pass
    try:
        cap = cs.fit(d, buddy_name or no_buddy_label, cs.f(20), cx1 - cx0 - 24)
        tw = d.textlength(cap, font=cs.f(20))
        d.text(((cx0 + cx1 - tw) / 2, 310), cap, font=cs.f(20),
               fill=cs.GOLD if buddy_name else cs.DIM)
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
        if not is_staff:
            try:
                from utils.checks import get_staff_roles
                _sr = set(get_staff_roles(gid))
                is_staff = any(str(r.id) in _sr for r in getattr(member, 'roles', []))
            except Exception:
                pass
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

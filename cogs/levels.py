"""Levels: text+voice XP, rank card, rewards. Hybrid = prefix + slash in one."""
import io
import random
import re
import time

import discord
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import database as db
from lang import t, get_lang
from utils.embeds import build, err, foot, ok, say, WHITE, GREEN
from utils.checks import staff_or

TEXT_MIN, TEXT_MAX, COOLDOWN = 15, 25, 60
VOICE_PER_MIN = 8
STREAM_MULT = 1.5
MIN_GIF_LEVEL = 10
GIF_CD = 20  # seconds between GIFs per user
_GIF_LAST: dict = {}
# house rule: this one always earns a little extra
HOUSE_BOOST_ID = '1270782781605154922'
HOUSE_BOOST_MULT = 2.0
_BG_CACHE = {}  # bg_url -> (fetched_at, bytes|None)

GIF_RE = re.compile(r'(tenor\.com|giphy\.com|gifer\.com|klipy\.com|\.gif(\?|#|$|\s))', re.I)
URL_RE = re.compile(r'https?://\S+', re.I)


def is_gif_url(url: str) -> bool:
    """Known gif hosts, .gif files, or 'gif' in the link path (domain excluded
    so giftshop.com doesn't sneak through)."""
    u = url or ''
    if GIF_RE.search(u):
        return True
    try:
        from urllib.parse import urlsplit
        return 'gif' in urlsplit(u).path.lower()
    except Exception:
        return False


def _has_gif(message: discord.Message) -> bool:
    for a in message.attachments:
        if (a.content_type or '').startswith('image/gif'):
            return True
        if (a.filename or '').lower().endswith('.gif'):
            return True
    for s in message.stickers:
        if s.format in (discord.StickerFormatType.apng, discord.StickerFormatType.lottie):
            return True
    if GIF_RE.search(message.content or ''):
        return True
    if any(is_gif_url(u) for u in URL_RE.findall(message.content or '')):
        return True
    return False


def xp_needed(level: int) -> int:
    # Steeper than linear, gentler than exponential: L0=100, L5=600,
    # L10=1500, L20=4500, L50=23100. Early levels fly, late ones grind.
    level = max(0, level)
    return 8 * level * level + 60 * level + 100


def user_boost(guild_id, user_id) -> float:
    """Personal XP multiplier: guild row wins, '*' row is global fallback."""
    if str(user_id) == HOUSE_BOOST_ID:
        return HOUSE_BOOST_MULT
    mult = 1.0
    with db.conn_ctx() as conn:
        for gid in (str(guild_id), '*'):
            row = conn.execute('SELECT mult FROM xp_boosts WHERE guild_id=? AND user_id=?',
                               (gid, str(user_id))).fetchone()
            if row and (row['mult'] or 0) > 0:
                mult = float(row['mult'])
                break
    try:
        if db.boost_left(guild_id, user_id) > 0:
            mult *= 2.0
    except Exception:
        pass
    return mult


def get_user(guild_id, user_id) -> dict:
    gid, uid = str(guild_id), str(user_id)
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM levels WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        if not row:
            conn.execute('INSERT INTO levels (guild_id, user_id) VALUES (?, ?)', (gid, uid))
            return {'guild_id': gid, 'user_id': uid, 'xp': 0, 'level': 0, 'last_text_xp': 0, 'voice_minutes': 0}
        return dict(row)


def add_xp(guild_id, user_id, amount: float):
    u = get_user(guild_id, user_id)
    xp, level = u['xp'] + int(amount), u['level']
    up = False
    while xp >= xp_needed(level):
        xp -= xp_needed(level)
        level += 1
        up = True
    with db.conn_ctx() as conn:
        conn.execute('UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?',
                     (xp, level, str(guild_id), str(user_id)))
    return {'xp': xp, 'level': level, 'leveledUp': up}


def get_rank(guild_id, user_id) -> int:
    with db.conn_ctx() as conn:
        me = conn.execute('SELECT level, xp FROM levels WHERE guild_id=? AND user_id=?',
                          (str(guild_id), str(user_id))).fetchone()
        if not me:
            return 1
        c = conn.execute('''SELECT COUNT(*) c FROM levels WHERE guild_id=?
            AND (level > ? OR (level = ? AND xp > ?))''',
                         (str(guild_id), me['level'], me['level'], me['xp'])).fetchone()['c']
        return c + 1


async def apply_rewards(member: discord.Member, level: int):
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT * FROM level_rewards WHERE guild_id=? ORDER BY level ASC',
                            (str(member.guild.id),)).fetchall()
    if not rows:
        return []
    settings = db.get_settings(member.guild.id)
    earned = [r for r in rows if r['level'] <= level]
    added = []
    # never strip these no matter what the reward table says
    protected = set()
    try:
        with db.conn_ctx() as conn:
            vrow = conn.execute('SELECT role_id FROM verify_cfg WHERE guild_id=?',
                                (str(member.guild.id),)).fetchone()
            if vrow and vrow['role_id']:
                protected.add(str(vrow['role_id']))
            for srow in conn.execute('SELECT role_id FROM staff_roles WHERE guild_id=?',
                                     (str(member.guild.id),)).fetchall():
                protected.add(str(srow['role_id']))
    except Exception:
        pass
    try:
        if settings.get('stack_rewards'):
            for r in earned:
                role = member.guild.get_role(int(r['role_id']))
                if role and role not in member.roles:
                    await member.add_roles(role, reason='level reward')
                    added.append(r)
        elif earned:
            top = earned[-1]
            for r in rows:
                if str(r['role_id']) == str(top['role_id']):
                    continue
                if str(r['role_id']) in protected:
                    continue
                if (r['level'] or 0) <= 0:
                    continue  # baseline grants are kept forever, never stripped
                role = member.guild.get_role(int(r['role_id']))
                if role and role in member.roles:
                    await member.remove_roles(role, reason='level reward')
            role = member.guild.get_role(int(top['role_id']))
            if role and role not in member.roles:
                await member.add_roles(role, reason='level reward')
                added.append(top)
    except Exception:
        pass
    return added


def rank_tier(level: int):
    """(name, ring_width, stars) — card evolves every bunch of levels."""
    from utils.cards import TIER_TABLE
    for min_lv, _key, default, w, stars in TIER_TABLE:
        if level >= min_lv:
            return (default, w, stars)
    return ('ROOKIE', 3, 0)


def _bar(img, bx, by, bw, bh, pct):
    d = ImageDraw.Draw(img, 'RGBA')
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=16, fill=(42, 42, 46))
    if pct > 0:
        fw = max(32, int(bw * pct))
        # monochrome: white -> grey gradient
        bar = Image.new('RGB', (fw, bh))
        bd = ImageDraw.Draw(bar)
        for x in range(fw):
            k = x / max(fw - 1, 1)
            bd.line([(x, 0), (x, bh)], fill=(int(255 - 105 * k), int(255 - 105 * k), int(255 - 100 * k)))
        m = Image.new('L', (fw, bh), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, fw, bh], radius=16, fill=255)
        img.paste(bar, (bx, by), m)


def _ctext(d, cx, y, s, font, fill):
    try:
        w = d.textlength(s, font=font)
        d.text((cx - w / 2, y), s, font=font, fill=fill)
    except Exception:
        d.text((cx - len(s) * 7, y), s, font=font, fill=fill)


def rank_card(member: discord.Member, data: dict, rank: int, lang: str = 'en', avatar_bytes: bytes = None,
              banner_bytes: bytes = None, accent: tuple = None, style: dict = None,
              custom_bg: bytes = None, tier_names: dict = None) -> discord.File:
    from lang import STR
    from utils.cards import apply_bg, parse_hex, paste_avatar, fallback_face, tier_for
    L = lambda k, fb: (STR.get(lang) or {}).get(k, fb)
    W, H = 900, 260
    style = style or {'blur': 25, 'dim': 0.45, 'layout': 'banner', 'show_avatar': 1, 'accent': '',
                      'show_tier': 1, 'show_bar': 1, 'show_xptext': 1}
    layout = style.get('layout', 'banner')
    show_av = style.get('show_avatar', 1)
    show_tier = style.get('show_tier', 1)
    show_bar = style.get('show_bar', 1)
    show_xp = style.get('show_xptext', 1)
    ring = parse_hex(style.get('accent') or '') or (255, 255, 255)
    img, d = apply_bg(style, bg_bytes=custom_bg, banner_bytes=banner_bytes, accent=accent)
    d.rectangle([0, 0, W, H], outline=(40, 40, 44), width=2)
    try:
        from pathlib import Path as _P
        _a = _P(__file__).parent.parent / 'assets'
        f_big, f_mid = ImageFont.truetype(str(_a / 'DejaVuSans-Bold.ttf'), 40), ImageFont.truetype(str(_a / 'DejaVuSans.ttf'), 28)
        try:
            f_sm = ImageFont.truetype(str(_a / 'DejaVuSans.ttf'), 24)
        except Exception:
            f_sm = f_mid
    except Exception:
        try:
            f_big, f_mid, f_sm = ImageFont.truetype('arialbd.ttf', 40), ImageFont.truetype('arial.ttf', 28), ImageFont.truetype('arial.ttf', 24)
        except Exception:
            f_big = f_mid = f_sm = ImageFont.load_default()
    pasted = False
    if show_av and avatar_bytes:
        pasted = paste_avatar(img, avatar_bytes, (50, 50, 160))
    tier_name, ring_w, stars = tier_for(data['level'], tier_names)
    if show_av and not pasted:
        # fallback: initial letter instead of an empty ring
        fallback_face(d, (50, 50, 160), member.display_name)
    if show_av:
        # tier ring evolves with level
        d.ellipse([50, 50, 210, 210], outline=ring, width=ring_w)
    if data['level'] >= 20:
        d.rectangle([6, 6, W - 6, H - 6], outline=(120, 120, 128), width=2)
    if data['level'] >= 50:
        d.rectangle([12, 12, W - 12, H - 12], outline=(200, 200, 208), width=1)
    need = xp_needed(data['level'])
    pct = min(1, data['xp'] / need if need else 0)
    tier_txt = tier_name + (' ' + '★' * stars if stars else '')
    lvl_txt = f"{L('cv.level', 'Level {n}').replace('{n}', str(data['level']))}   •   {L('cv.rank', 'Rank #{n}').replace('{n}', str(rank))}"
    xp_txt = L('cv.xp', '{xp} / {need} XP • {pct}%').replace('{xp}', str(data['xp'])).replace('{need}', str(need)).replace('{pct}', str(round((data['xp'] / need * 100) if need else 0)))
    if layout == 'center':
        _ctext(d, W / 2, 22, member.display_name[:22], f_big, (255, 255, 255))
        if show_tier:
            _ctext(d, W / 2, 74, tier_txt, f_mid, ring)
        _ctext(d, W / 2, 112 if show_tier else 78, lvl_txt, f_mid, (181, 181, 181))
        if show_bar:
            _bar(img, 150, 152, 600, 30, pct)
            if show_xp:
                _ctext(d, W / 2, 192, xp_txt, f_sm, (255, 255, 255))
        elif show_xp:
            _ctext(d, W / 2, 158, xp_txt, f_sm, (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        buf.seek(0)
        return discord.File(buf, 'rank.png')
    d.text((240, 55), member.display_name[:18], font=f_big, fill=(255, 255, 255))
    if show_tier:
        try:
            tw = d.textlength(tier_txt, font=f_mid)
        except Exception:
            tw = len(tier_txt) * 14
        d.text((W - 30 - tw, 55), tier_txt, font=f_mid, fill=(200, 200, 208))
    d.text((240, 105), lvl_txt, font=f_mid, fill=(181, 181, 181))
    if show_bar:
        _bar(img, 240, 165, 600, 32, pct)
    if show_xp:
        d.text((240, 205 if show_bar else 168), xp_txt, font=f_sm, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return discord.File(buf, 'rank.png')


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_tick.start()

    def cog_unload(self):
        self.voice_tick.cancel()

    # ---- events ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        # GIF gate: level 10+ only (staff bypass). Runs before XP so blocked
        # GIFs grant nothing.
        member = message.author
        if (isinstance(member, discord.Member) and not member.guild_permissions.manage_messages
                and _has_gif(message)):
            from cogs.whitelist import is_exempt
            if not is_exempt(message.guild.id, message.channel.id, 'levels'):
                lvl = get_user(message.guild.id, member.id).get('level', 0)
                if lvl < MIN_GIF_LEVEL:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    try:
                        note = await say(message.channel,
                            t(message.guild.id, 'lvl.gif_block', user=member.mention, need=MIN_GIF_LEVEL, n=lvl))
                        await note.delete(delay=8)
                    except Exception:
                        pass
                    return
                import time as _t
                key = (message.guild.id, member.id)
                last = _GIF_LAST.get(key, 0)
                if _t.time() - last < GIF_CD:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    try:
                        note = await say(message.channel,
                            t(message.guild.id, 'lvl.gif_cd', user=member.mention, s=int(GIF_CD - (_t.time() - last))))
                        await note.delete(delay=6)
                    except Exception:
                        pass
                    return
                _GIF_LAST[key] = _t.time()
        settings = db.get_settings(message.guild.id)
        try:
            import json
            if message.channel.id in json.loads(settings.get('noxp_channels') or '[]'):
                return
        except Exception:
            pass
        if len(message.content or '') < 5:
            return
        u = get_user(message.guild.id, message.author.id)
        now = int(time.time() * 1000)
        if now - (u.get('last_text_xp') or 0) < COOLDOWN * 1000:
            return
        with db.conn_ctx() as conn:
            conn.execute('UPDATE levels SET last_text_xp=? WHERE guild_id=? AND user_id=?',
                         (now, str(message.guild.id), str(message.author.id)))
        amount = (TEXT_MIN + random.random() * (TEXT_MAX - TEXT_MIN)) * (settings.get('xp_multiplier') or 1)
        amount *= user_boost(message.guild.id, message.author.id)
        res = add_xp(message.guild.id, message.author.id, amount)
        if res['leveledUp']:
            gid = message.guild.id
            rewards = await apply_rewards(message.author, res['level'])
            rw = t(gid, 'msg.level_rw', roles=' '.join(f'<@&{r["role_id"]}>' for r in rewards)) if rewards else ''
            embed = discord.Embed(
                description=t(gid, 'msg.level_up', user=message.author.mention, level=res['level'], rewards=rw),
                color=WHITE)
            embed.set_footer(text=foot())
            try:
                embed.set_thumbnail(url=str(message.author.display_avatar.with_size(128).url))
            except Exception:
                pass
            msg = {'embed': embed}
            if settings.get('levelup_channel'):
                ch = message.guild.get_channel(int(settings['levelup_channel']))
                if ch:
                    await ch.send(embed=embed)
            else:
                await message.channel.send(embed=embed)
            if settings.get('levelup_dm'):
                try:
                    await message.author.send(t(gid, 'msg.level_dm', level=res['level'], server=message.guild.name))
                except Exception:
                    pass

    @tasks.loop(minutes=1)
    async def voice_tick(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self._voice_guild(guild)
            except Exception as e:
                print(f'[voice] tick failed for {guild.id}: {e}')

    async def _voice_guild(self, guild: discord.Guild):
        settings = db.get_settings(guild.id)
        try:
            import json
            noxp = set(json.loads(settings.get('noxp_channels') or '[]'))
        except Exception:
            noxp = set()
        mult = settings.get('xp_multiplier') or 1
        for ch in guild.voice_channels:
            if guild.afk_channel and ch.id == guild.afk_channel.id:
                continue
            if str(ch.id) in noxp or ch.id in noxp:
                continue
            for m in ch.members:
                if m.bot:
                    continue
                vs = m.voice
                # muted or deafened in any way = no XP. sitters earn nothing.
                if not vs or vs.mute or vs.deaf or vs.self_mute or vs.self_deaf:
                    continue
                try:
                    rate = VOICE_PER_MIN * mult
                    if vs.self_stream:
                        rate *= STREAM_MULT
                    rate *= user_boost(guild.id, m.id)
                    res = add_xp(guild.id, m.id, rate)
                    with db.conn_ctx() as conn:
                        conn.execute('UPDATE levels SET voice_minutes = voice_minutes + 1 WHERE guild_id=? AND user_id=?',
                                     (str(guild.id), str(m.id)))
                except Exception as e:
                    print(f'[voice] xp failed for {m.id}: {e}')
                    continue
                if res['leveledUp']:
                    try:
                        await apply_rewards(m, res['level'])
                    except Exception as e:
                        print(f'[voice] rewards failed for {m.id}: {e}')
                    if settings.get('levelup_channel'):
                        dest = guild.get_channel(int(settings['levelup_channel']))
                        if dest:
                            try:
                                emb = discord.Embed(
                                    description=t(guild.id, 'msg.voice_level', user=m.mention, level=res['level']),
                                    color=WHITE)
                                emb.set_footer(text=foot())
                                await dest.send(embed=emb)
                            except Exception as e:
                                print(f'[voice] announce failed: {e}')

    # ---- commands ----
    @commands.hybrid_command(name='rank', description='Karta gracza')
    async def rank(self, ctx, member: discord.Member = None):
        import aiohttp
        import asyncio as _aio
        await ctx.defer()
        member = member or ctx.author
        data = get_user(ctx.guild.id, member.id)

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
                try:
                    u = await self.bot.fetch_user(member.id)
                    banner_url = str(u.banner.with_size(512).url) if u and u.banner else None
                    accent = u.accent_color.to_rgb() if u and u.accent_color else None
                except Exception as e:
                    print(f'[rank] fetch_user failed: {e}')
                    banner_url, accent = None, None
                if banner_url:
                    banner, avatar = await _aio.gather(grab(session, banner_url), avatar_task)
                else:
                    banner, avatar = None, await avatar_task
                return avatar, banner, accent

        avatar, banner, accent = await _fetch_all()
        from utils.cards import get_style, fetch_bytes, get_skin, apply_skin, get_tier_names
        style = apply_skin(get_style(ctx.guild.id, 'rank'), get_skin(ctx.guild.id, data['level']))
        names = get_tier_names(ctx.guild.id)
        # nitro banner wins; server style bg is the fallback
        custom_bg = banner
        if not custom_bg and style.get('bg_url'):
            if style['bg_url'] not in _BG_CACHE or time.time() - _BG_CACHE[style['bg_url']][0] > 3600:
                _BG_CACHE[style['bg_url']] = (time.time(), await fetch_bytes(style['bg_url']))
            custom_bg = _BG_CACHE[style['bg_url']][1]
        card = await self.bot.loop.run_in_executor(
            None, rank_card, member, data, get_rank(ctx.guild.id, member.id),
            __import__('lang').get_lang(ctx.guild.id), avatar, None, accent, style, custom_bg, names)
        await ctx.reply(file=card, mention_author=False)

    @commands.hybrid_command(name='leaderboard', description='Ranking XP', aliases=['lb', 'top'])
    async def leaderboard(self, ctx):
        from lang import t as _t
        await ctx.defer()
        embeds, files, view = await self._lb_render(ctx.guild, 'xp', 5)
        if not embeds:
            return await ctx.reply(_t(ctx.guild.id, 'lb.empty'), ephemeral=True)
        await ctx.reply(embeds=embeds, files=files, view=view, mention_author=False)

    async def _lb_render(self, guild: discord.Guild, metric: str, n: int):
        """Fetch avatars, render ONE board image, assemble embed + file + view."""
        built = self._lb_build(guild, metric, n)
        if not built:
            return None, None, None
        head, view, specs = built
        rows = []
        for idx, name, level, pct, sub, m in specs:
            av = await self._lb_avatar(m) if m else None
            rows.append((idx, name, level, pct, av))
        png = await self.bot.loop.run_in_executor(None, self._lb_board_image, rows)
        head.set_image(url='attachment://leaderboard.png')
        # drop footer thumbnail clash: keep icon thumb, image below
        return [head], [discord.File(__import__('io').BytesIO(png), 'leaderboard.png')], view

    @staticmethod
    def _lb_total(level: int, xp: int) -> int:
        return sum(xp_needed(l) for l in range(max(0, level))) + max(0, xp)

    def _lb_rows(self, guild: discord.Guild, metric: str, limit: int):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT user_id, level, xp, voice_minutes FROM levels WHERE guild_id=?',
                                (str(guild.id),)).fetchall()
        scored = []
        for r in rows:
            total = self._lb_total(r['level'], r['xp'])
            key = total if metric == 'xp' else (r['level'] if metric == 'level' else (r['voice_minutes'] or 0))
            scored.append((key, total, dict(r)))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return scored[:limit]

    @staticmethod
    def _lb_bar(pct: float, w: int = 14) -> str:
        f = max(0, min(w, round(pct * w)))
        return '█' * f + '░' * (w - f)

    @staticmethod
    def _lb_board_image(rows) -> bytes:
        """One compact board image: avatar + rank line + thin bar per row.
        rows = [(rank, name, level, pct, avatar_bytes)]."""
        import io as _io
        from PIL import Image as _Img, ImageDraw as _Dr
        from utils.cards import fonts as _fonts, cover as _cover
        _, f_mid, f_sm = _fonts()
        try:
            from pathlib import Path as _P
            f_rank = __import__('PIL.ImageFont', fromlist=['truetype']).truetype(
                str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 30)
        except Exception:
            f_rank = f_mid
        W, RH, PAD, AV, GAP = 800, 88, 14, 60, 10
        H = len(rows) * (RH + GAP) - GAP + PAD * 2
        img = _Img.new('RGB', (W, H), (16, 16, 19))
        d = _Dr.Draw(img)
        RANK_COLS = {1: (250, 200, 60), 2: (195, 195, 203), 3: (210, 140, 80)}
        for k, (rank, name, level, pct, av_bytes) in enumerate(rows):
            y0 = PAD + k * (RH + GAP)
            # row card
            d.rounded_rectangle([PAD // 2, y0, W - PAD // 2, y0 + RH], radius=10, fill=(30, 30, 35))
            if av_bytes:
                try:
                    av = _cover(_Img.open(_io.BytesIO(av_bytes)).convert('RGB'), AV, AV)
                    mask = _Img.new('L', (AV, AV), 0)
                    _Dr.Draw(mask).rounded_rectangle([0, 0, AV, AV], radius=12, fill=255)
                    img.paste(av, (PAD + 4, y0 + (RH - AV) // 2), mask)
                except Exception:
                    pass
            x = PAD + 4 + AV + 14
            ty = y0 + 10
            rank_col = RANK_COLS.get(rank, (150, 150, 158))
            segs = [(f'#{rank}', f_rank, rank_col), (' • ', f_mid, (110, 110, 116)),
                    (name, f_mid, (255, 255, 255)), (' • ', f_mid, (110, 110, 116)),
                    (f'LVL: {level}', f_mid, (150, 150, 158))]
            try:
                maxw = W - x - PAD
                nm = name
                while d.textlength(f'#{rank} • {nm} • LVL: {level}', font=f_mid) > maxw and len(nm) > 4:
                    nm = nm[:-2]
                if nm != name:
                    segs[2] = (nm + '…', f_mid, (255, 255, 255))
                cx = x
                for txt, font, col in segs:
                    d.text((cx, ty), txt, font=font, fill=col)
                    cx += d.textlength(txt, font=font)
            except Exception:
                d.text((x, ty), f'#{rank} • {name[:14]} • LVL: {level}', font=f_mid,
                       fill=(255, 255, 255))
            bx, by, bw, bh = x, y0 + RH - 26, W - x - PAD, 5
            d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=(58, 58, 64))
            fw = max(6, int(bw * max(0.0, min(1.0, pct))))
            d.rounded_rectangle([bx, by, bx + fw, by + bh], radius=2,
                                fill=RANK_COLS.get(rank, (168, 168, 176)))
        buf = _io.BytesIO()
        img.save(buf, 'PNG')
        return buf.getvalue()

    def _lb_build(self, guild: discord.Guild, metric: str, n: int):
        from lang import t as _t
        if metric not in ('xp', 'level', 'voice'):
            metric = 'xp'
        n = 10 if n >= 10 else 5
        rows = self._lb_rows(guild, metric, n)
        if not rows:
            return None
        head = discord.Embed(title=guild.name, color=WHITE)
        head.set_footer(text=foot())
        specs = []
        for i, (_key, total, r) in enumerate(rows, start=1):
            m = guild.get_member(int(r['user_id']))
            name = m.display_name if m else f'user{r["user_id"]}'[:16]
            need = xp_needed(r['level'])
            pct = min(1, r['xp'] / need if need else 0)
            if metric == 'voice':
                mins = r['voice_minutes'] or 0
                sub = f'{mins // 60}h {mins % 60}m'
            else:
                sub = f'{total:,} XP'.replace(',', ' ')
            specs.append((i, name[:20], r['level'], pct, sub, m))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(custom_id='lbm', placeholder=_t(guild.id, 'lb.m_xp'),
                                min_values=1, max_values=1, options=[
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_xp'), value='xp',
                                                         default=(metric == 'xp')),
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_level'), value='level',
                                                         default=(metric == 'level')),
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_voice'), value='voice',
                                                         default=(metric == 'voice'))])
        view.add_item(sel)
        view.add_item(discord.ui.Button(label=_t(guild.id, 'lb.less' if n >= 10 else 'lb.more'),
                                        style=discord.ButtonStyle.grey,
                                        custom_id=f'lbmore:{metric}:{n}'))
        return head, view, specs

    async def _lb_avatar(self, member):
        try:
            return await member.display_avatar.with_size(128).read()
        except Exception:
            return None

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        metric, n = None, 5
        if cid == 'lbm':
            vals = interaction.data.get('values') or []
            metric = vals[0] if vals and vals[0] in ('xp', 'level', 'voice') else 'xp'
        elif cid.startswith('lbmore:'):
            try:
                _, metric, n = cid.split(':')
                n = 5 if int(n) >= 10 else 10
            except Exception:
                return
        else:
            return
        from lang import t as _t
        embeds, files, view = await self._lb_render(interaction.guild, metric, n)
        if not embeds:
            return await interaction.response.send_message(_t(interaction.guild_id, 'lb.empty'), ephemeral=True)
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=view)

    @commands.hybrid_group(name='levels', description='Levele (admin)')
    async def levels_grp(self, ctx):
        await ctx.reply('levels reward-add / reward-remove / reward-list / multiplier / noxp / levelup-channel / add-xp / stack',
                        ephemeral=True)

    @levels_grp.command(name='reward-add', description='Rola za poziom')
    @staff_or('manage_guild')
    async def reward_add(self, ctx, level: int, role: discord.Role):
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?)',
                         (str(ctx.guild.id), level, str(role.id)))
        from lang import t as _t
        await ctx.reply(_t(ctx.guild.id, 'lvl.reward_set', level=level, role=role.mention), ephemeral=True)

    @levels_grp.command(name='reward-remove', description='UsuÅ„ nagrodÄ™')
    @staff_or('manage_guild')
    async def reward_remove(self, ctx, level: int):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM level_rewards WHERE guild_id=? AND level=?', (str(ctx.guild.id), level))
        from lang import t as _t
        await ctx.reply(_t(ctx.guild.id, 'lvl.reward_del', level=level), ephemeral=True)

    @levels_grp.command(name='reward-list', description='Lista nagród')
    async def reward_list(self, ctx):
        from lang import t as _t
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM level_rewards WHERE guild_id=? ORDER BY level ASC',
                                (str(ctx.guild.id),)).fetchall()
        if not rows:
            return await ctx.reply(_t(ctx.guild.id, 'lvl.no_rewards'), ephemeral=True)
        await ctx.reply(embed=ok('\n'.join(f"Lv **{r['level']}** → <@&{r['role_id']}>" for r in rows)), ephemeral=True)

    @levels_grp.command(name='multiplier', description='MnoÅ¼nik XP')
    @staff_or('manage_guild')
    async def multiplier(self, ctx, value: float):
        from lang import t as _t
        db.get_settings(ctx.guild.id)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET xp_multiplier=? WHERE guild_id=?', (value, str(ctx.guild.id)))
        await ctx.reply(_t(ctx.guild.id, 'lvl.mult', v=value), ephemeral=True)

    @levels_grp.command(name='noxp', description='XP na kanale wÅ‚./wyÅ‚.')
    @staff_or('manage_guild')
    async def noxp(self, ctx, channel: discord.TextChannel):
        import json
        from lang import t as _t
        s = db.get_settings(ctx.guild.id)
        arr = set(json.loads(s.get('noxp_channels') or '[]'))
        off = str(channel.id) in arr or channel.id in arr
        arr = {str(x) for x in arr}
        if off:
            arr.discard(str(channel.id))
        else:
            arr.add(str(channel.id))
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET noxp_channels=? WHERE guild_id=?', (json.dumps(sorted(arr)), str(ctx.guild.id)))
        await ctx.reply(_t(ctx.guild.id, 'lvl.noxp_on' if off else 'lvl.noxp_off', ch=channel.mention), ephemeral=True)

    @levels_grp.command(name='levelup-channel', description='OgÅ‚oszenia level-upów')
    @staff_or('manage_guild')
    async def levelup_channel(self, ctx, channel: discord.TextChannel = None):
        from lang import t as _t
        db.get_settings(ctx.guild.id)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET levelup_channel=? WHERE guild_id=?',
                         (str(channel.id) if channel else None, str(ctx.guild.id)))
        await ctx.reply(_t(ctx.guild.id, 'lvl.lvl_set', ch=channel.mention) if channel else _t(ctx.guild.id, 'lvl.lvl_same'),
                        ephemeral=True)

    @levels_grp.command(name='add-xp', description='Dosyp XP')
    @staff_or('manage_guild')
    async def add_xp_cmd(self, ctx, member: discord.Member, amount: int):
        from lang import t as _t
        res = add_xp(ctx.guild.id, member.id, amount)
        await ctx.reply(_t(ctx.guild.id, 'lvl.addxp', n=amount, user=member.mention, level=res['level'], xp=res['xp']),
                        ephemeral=True)

    @levels_grp.command(name='stack', description='Kumulacja ról wÅ‚./wyÅ‚.')
    @staff_or('manage_guild')
    async def stack(self, ctx):
        from lang import t as _t
        s = db.get_settings(ctx.guild.id)
        nxt = 0 if s.get('stack_rewards') else 1
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET stack_rewards=? WHERE guild_id=?', (nxt, str(ctx.guild.id)))
        await ctx.reply(_t(ctx.guild.id, 'lvl.stack_on' if nxt else 'lvl.stack_off'), ephemeral=True)

    @levels_grp.command(name='voice-state', description='Kto teraz zbiera XP z gÅ‚osówki')
    @staff_or('manage_guild')
    async def voice_state(self, ctx):
        import json
        s = db.get_settings(ctx.guild.id)
        try:
            noxp = set(json.loads(s.get('noxp_channels') or '[]'))
        except Exception:
            noxp = set()
        lines = [f"multiplier: **{s.get('xp_multiplier') or 1}** (+{int(VOICE_PER_MIN * (s.get('xp_multiplier') or 1))}/min)"]
        for ch in ctx.guild.voice_channels:
            if ctx.guild.afk_channel and ch.id == ctx.guild.afk_channel.id:
                lines.append(f'• #{ch.name}: AFK channel — skipped')
                continue
            if str(ch.id) in noxp or ch.id in noxp:
                lines.append(f'• #{ch.name}: no-XP channel — skipped')
                continue
            for m in ch.members:
                if m.bot:
                    continue
                vs = m.voice
                if not vs or vs.deaf or vs.self_deaf or vs.mute:
                    lines.append(f'• {m.display_name} (#{ch.name}): skipped (deaf/muted)')
                else:
                    tags = []
                    if vs.self_stream:
                        tags.append(f'x{STREAM_MULT} streaming')
                    b = user_boost(ctx.guild.id, m.id)
                    if b != 1 and str(m.id) != HOUSE_BOOST_ID:
                        tags.append(f'x{b:g} boost')
                    lines.append(f'• {m.display_name} (#{ch.name}): earning' + (f" ({', '.join(tags)})" if tags else ''))
        await ctx.reply(embed=ok('\n'.join(lines[:25]) or '(nobody in voice)'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Levels(bot))

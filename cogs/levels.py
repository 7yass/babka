"""Levels: text+voice XP, rank card, rewards. Hybrid = prefix + slash in one."""
import io
import random
import re
import time

import discord
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import database as db
from utils.cards import short as cshort
from lang import t, get_lang
from utils.embeds import build, err, foot, ok, say, WHITE, GREEN
from utils.checks import staff_or

TEXT_MIN, TEXT_MAX, COOLDOWN = 8, 12, 60
VOICE_PER_MIN = 4
STREAM_MULT = 1.5
MIN_GIF_LEVEL = 10
GIF_CD = 20  # seconds between GIFs per user
_GIF_LAST: dict = {}
_XP_FAST: dict = {}
# house rule: this one always earns a little extra
HOUSE_BOOST_ID = '1270782781605154922'
# hidden from public leaderboards (progress kept, just not shown)
HIDDEN_LB = {'1270782781605154922', '558332192531546114', '908532356397285460'}
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
    try:
        # Card buddy: a small XP aura from the equipped card (cached 5 min).
        from services.card_service import card_perks
        mult *= 1.0 + card_perks(guild_id, user_id)['xp_pct'] / 100.0
    except Exception:
        pass
    return mult


def get_user(guild_id, user_id) -> dict:
    gid, uid = str(guild_id), str(user_id)
    with db.conn_ctx() as conn:
        try:
            conn.execute('ALTER TABLE levels ADD COLUMN text_messages INTEGER DEFAULT 0')
        except Exception:
            pass
        row = conn.execute('SELECT * FROM levels WHERE guild_id=? AND user_id=?', (gid, uid)).fetchone()
        if not row:
            conn.execute('INSERT INTO levels (guild_id, user_id) VALUES (?, ?)', (gid, uid))
            return {'guild_id': gid, 'user_id': uid, 'xp': 0, 'level': 0, 'last_text_xp': 0, 'voice_minutes': 0, 'text_messages': 0}
        d = dict(row)
        d.setdefault('text_messages', 0)
        d.setdefault('voice_minutes', 0)
        return d


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
            AND user_id NOT IN ('1270782781605154922','558332192531546114','908532356397285460')
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


def _bar_knob(img, bx, by, bw, bh, pct, fill=(255, 255, 255)):
    d = ImageDraw.Draw(img, 'RGBA')
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(42, 42, 46))
    fw = max(bh, int(bw * max(0.0, min(1.0, pct))))
    bar = Image.new('RGB', (fw, bh), fill)
    m = Image.new('L', (fw, bh), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, fw, bh], radius=bh // 2, fill=255)
    img.paste(bar, (bx, by), m)
    kx = bx + fw - bh // 2
    d.ellipse([kx - bh // 2, by - 3, kx + bh // 2, by + bh + 3], fill=(255, 255, 255),
              outline=(30, 30, 34), width=2)


def _pill(d, x, y, text, font, fg, outline):
    try:
        tw = d.textlength(text, font=font)
    except Exception:
        tw = len(text) * 13
    pad, h = 18, 38
    d.rounded_rectangle([x, y, x + tw + pad * 2, y + h], radius=h // 2,
                        fill=(24, 24, 28), outline=outline, width=2)
    d.text((x + pad, y + 5), text, font=font, fill=fg)
    return tw + pad * 2


def _ctext(d, cx, y, s, font, fill):
    try:
        w = d.textlength(s, font=font)
        d.text((cx - w / 2, y), s, font=font, fill=fill)
    except Exception:
        d.text((cx - len(s) * 7, y), s, font=font, fill=fill)


TIER_COLORS = {
    'ROOKIE': (150, 150, 158), 'BRONZE': (205, 127, 50), 'SILVER': (184, 192, 204),
    'GOLD': (250, 200, 60), 'DIAMOND': (120, 190, 255), 'LEGEND': (200, 120, 255),
}


def _tier_key(level: int) -> str:
    from utils.cards import TIER_TABLE
    for min_lv, key, *_ in TIER_TABLE:
        if level >= min_lv:
            return key
    return 'ROOKIE'


def _mono_avatar(img, avatar_bytes: bytes, box: tuple) -> bool:
    """Paste a desaturated circular avatar. box = (x, y, size)."""
    x, y, s = box
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert('L').convert('RGB').resize((s, s))
        mask = Image.new('L', (s, s), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, s, s], fill=255)
        img.paste(av, (x, y), mask)
        return True
    except Exception:
        return False


def _tracked(d, xy, text, font, fill, tracking=0):
    """Letter-spaced text (Direction C tier line)."""
    x, y = xy
    for c in text:
        d.text((x, y), c, font=font, fill=fill)
        try:
            x += d.textlength(c, font=font) + tracking
        except Exception:
            x += 12 + tracking


def _bar_diamond(img, bx, by, bw, bh, pct, fill):
    """Ultra-slim progress bar with a diamond marker (Direction C)."""
    d = ImageDraw.Draw(img, 'RGBA')
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(38, 38, 44))
    fw = max(bh, int(bw * max(0.0, min(1.0, pct))))
    bar = Image.new('RGB', (fw, bh), fill)
    m = Image.new('L', (fw, bh), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, fw, bh], radius=bh // 2, fill=255)
    img.paste(bar, (bx, by), m)
    kx = bx + fw - bh // 2
    ky = by + bh // 2
    r = max(3, int(bh * 0.9))
    d.polygon([(kx, ky - r), (kx + r, ky), (kx, ky + r), (kx - r, ky)], fill=fill)


def rank_card(member: discord.Member, data: dict, rank: int, lang: str = 'en', avatar_bytes: bytes = None,
              banner_bytes: bytes = None, accent: tuple = None, style: dict = None,
              custom_bg: bytes = None, tier_names: dict = None) -> discord.File:
    """Midnight Gold rank card: gradient + glass tiles, avatar with level
    coin, tier pill, gold XP bar. Same flags/behaviour as before."""
    from lang import STR
    from utils.cards import apply_bg, parse_hex, tier_for
    from utils import cardstyle as cs
    L = lambda k, fb: (STR.get(lang) or {}).get(k, fb)
    W, H = 900, 260
    style = style or {'blur': 25, 'dim': 0.45, 'layout': 'banner', 'show_avatar': 1, 'accent': '',
                      'show_tier': 1, 'show_bar': 1, 'show_xptext': 1}
    layout = style.get('layout', 'banner')
    show_av = 1 if int(style.get('show_avatar', 1) or 0) else 0
    show_tier = 1 if int(style.get('show_tier', 1) or 0) else 0
    show_bar = 1 if int(style.get('show_bar', 1) or 0) else 0
    show_xp = 1 if int(style.get('show_xptext', 1) or 0) else 0
    need = xp_needed(data['level'])
    pct = min(1, data['xp'] / need if need else 0)
    tier_name, ring_w, stars = tier_for(data['level'], tier_names)
    tier_c = parse_hex(style.get('accent') or '') or TIER_COLORS.get(_tier_key(data['level']), (240, 240, 246))
    name = member.display_name[:18]
    lvl_num = str(data['level'])
    rank_num = f'#{rank}'
    pct_num = str(round((data['xp'] / need * 100) if need else 0))
    total_xp = sum(xp_needed(l) for l in range(max(0, data['level']))) + max(0, data['xp'])
    try:
        import datetime as _dt
        _ja = getattr(member, 'joined_at', None)
        _days = max(0, (_dt.datetime.now(_dt.timezone.utc) - _ja).days) if _ja else None
        since_txt = f'{_days} DAYS' if _days is not None else '—'
    except Exception:
        since_txt = '—'
    xp_txt = L('cv.xp', '{xp} / {need} XP • {pct}%').replace('{xp}', cshort(data['xp'])).replace('{need}', cshort(need)).replace('{pct}', pct_num)
    if layout == 'center':
        if custom_bg or style.get('bg_url') or style.get('bg_color'):
            img, d = apply_bg(style, bg_bytes=custom_bg, banner_bytes=banner_bytes, accent=accent)
        else:
            img = cs.base(W, H)
            d = ImageDraw.Draw(img, 'RGBA')
        cs.edge_bars(d, W, H)
        try:
            tw = d.textlength(member.display_name[:22], font=cs.f(54))
            d.text(((W - tw) / 2, 24), member.display_name[:22], font=cs.f(54), fill=cs.INK)
        except Exception:
            pass
        y = 96
        if show_tier:
            tier_txt = tier_name + ('  ' + '★' * stars if stars else '')
            try:
                tw = d.textlength(tier_txt, font=cs.f(24))
            except Exception:
                tw = len(tier_txt) * 13
            cs.pill(d, (W - tw - 28) / 2, y, tier_txt, cs.f(24), tier_c, tier_c)
            y += 46
        cs.tracked(d, ((W - 300) / 2, y), f'LVL {lvl_num}   •   RANK {rank_num}   •   {pct_num}%',
                   cs.f(21), cs.DIM, spacing=3)
        y += 36
        if show_bar:
            cs.xpbar(img, 150, y, 600, 14, pct, c1=tier_c)
            y += 28
        if show_xp:
            try:
                xw = d.textlength(xp_txt, font=cs.f(18, False))
                d.text(((W - xw) / 2, y), xp_txt, font=cs.f(18, False), fill=cs.DIM)
            except Exception:
                pass
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        buf.seek(0)
        return discord.File(buf, 'rank.png')
    # banner layout
    if custom_bg or style.get('bg_url') or style.get('bg_color'):
        img, d = apply_bg(style, bg_bytes=custom_bg, banner_bytes=banner_bytes, accent=accent)
    else:
        img = cs.base(W, H)
        d = ImageDraw.Draw(img, 'RGBA')
    cs.edge_bars(d, W, H)
    x0, s = 44, 148
    ay = (H - s) // 2
    if show_av:
        if not (avatar_bytes and cs.avatar(img, avatar_bytes, (x0, ay, s), tier_c, max(3, ring_w // 2))):
            cs.fallback(img, (x0, ay, s), name)
        cs.level_coin(d, x0 + s - 14, ay + s - 14, 26, lvl_num)
        d = ImageDraw.Draw(img, 'RGBA')
        dx = x0 + s + 36
    else:
        dx = 50
    d.text((dx, 26), name.upper()[:16], font=cs.f(40), fill=cs.INK)
    if show_tier:
        tier_txt = tier_name + (' ' + '★' * stars if stars else '')
        try:
            tw = d.textlength(tier_txt, font=cs.f(19)) + 28
        except Exception:
            tw = len(tier_txt) * 12 + 28
        px = W - 44 - tw
        d.rounded_rectangle([px, 34, px + tw, 34 + 32], radius=16,
                            outline=tier_c, width=2, fill=(16, 19, 34))
        d.text((px + 14, 40), tier_txt, font=cs.f(19), fill=tier_c)
    d.line([(dx, 92), (W - 44, 92)], fill=cs.HAIR, width=1)
    cols = [('IN SERVER', since_txt), ('RANK', rank_num), ('PROGRESS', f'{pct_num}%')]
    tw_all = W - dx - 44
    cw, gap = (tw_all - 24) / 3, 12
    for i, (lab, val) in enumerate(cols):
        cx = dx + i * (cw + gap)
        cs.glass(d, [cx, 100, cx + cw, 160], radius=12)
        cs.tracked(d, (cx + 14, 108), lab, cs.f(15), cs.FAINT)
        d.text((cx + 14, 126), val, font=cs.f(28), fill=cs.INK)
    if show_xp:
        try:
            d.text((W - 44, 168), f"{cshort(data['xp'])} / {cshort(need)} XP", font=cs.f(17, False),
                   fill=cs.DIM, anchor='ra')
        except Exception:
            pass
    if show_bar:
        cs.xpbar(img, dx, 200, W - dx - 44, 14, pct, c1=tier_c)
        d = ImageDraw.Draw(img, 'RGBA')
    if data['level'] >= 20:
        d.rounded_rectangle([8, 8, W - 8, H - 8], radius=14, outline=(120, 122, 140), width=2)
    if data['level'] >= 50:
        d.rounded_rectangle([14, 14, W - 14, H - 14], radius=10, outline=(200, 200, 210), width=1)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return discord.File(buf, 'rank.png')


def _fmt_vc(mins: int) -> str:
    mins = max(0, int(mins or 0))
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f'{d}d {h}h {m}m'
    if h:
        return f'{h}h {m}m'
    return f'{m}m'


def _backfill_msgs(guild_id) -> None:
    """One-time migration: seed text_messages from msg_stats (30d window)
    for users who have no lifetime count yet."""
    try:
        with db.conn_ctx() as conn:
            try:
                conn.execute('ALTER TABLE levels ADD COLUMN text_messages INTEGER DEFAULT 0')
            except Exception:
                pass
            rows = conn.execute(
                'SELECT user_id, SUM(count) s FROM msg_stats WHERE guild_id=? GROUP BY user_id',
                (str(guild_id),)).fetchall()
            for r in rows:
                try:
                    conn.execute(
                        'UPDATE levels SET text_messages=? WHERE guild_id=? AND user_id=? '
                        'AND COALESCE(text_messages,0)=0',
                        (int(r['s'] or 0), str(guild_id), str(r['user_id'])))
                except Exception:
                    continue
    except Exception:
        pass


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_tick.start()

    def cog_unload(self):
        self.voice_tick.cancel()

    # ---- events ----
    @commands.Cog.listener()
    @db.main_guild_only
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
        # lifetime message counter for the leaderboard (counts even on XP cooldown)
        try:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE levels SET text_messages=COALESCE(text_messages,0)+1 WHERE guild_id=? AND user_id=?',
                             (str(message.guild.id), str(message.author.id)))
        except Exception:
            pass
        # quest + activity-ticket tick (own txn, never breaks the XP path)
        try:
            from services.quests import message_tick
            message_tick(message.guild.id, message.author.id)
        except Exception:
            pass
        now_ms = int(time.time() * 1000)
        fast_key = (message.guild.id, message.author.id)
        if now_ms - _XP_FAST.get(fast_key, 0) < COOLDOWN * 1000:
            return  # hot path: zero DB hits for chatters on cooldown
        u = get_user(message.guild.id, message.author.id)
        now = now_ms
        if now - (u.get('last_text_xp') or 0) < COOLDOWN * 1000:
            _XP_FAST[fast_key] = now
            return
        with db.conn_ctx() as conn:
            conn.execute('UPDATE levels SET last_text_xp=? WHERE guild_id=? AND user_id=?',
                         (now, str(message.guild.id), str(message.author.id)))
        _XP_FAST[fast_key] = now
        if len(_XP_FAST) > 10000:
            _cut = now - COOLDOWN * 1000
            for _k in [k for k, v in _XP_FAST.items() if v < _cut]:
                del _XP_FAST[_k]
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
            if not db.is_main_guild(getattr(guild, 'id', 0)):
                continue  # holder servers only store emojis
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
                    try:
                        from services.quests import voice_tick
                        voice_tick(guild.id, m.id)
                    except Exception:
                        pass
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
                    u = await _aio.wait_for(self.bot.fetch_user(member.id), timeout=3)
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

        try:
            avatar, banner, accent = await _aio.wait_for(_fetch_all(), timeout=7)
        except Exception:
            avatar, banner, accent = None, None, None
        if avatar is None:
            try:
                avatar = await _aio.wait_for(member.display_avatar.read(), timeout=4)
            except Exception:
                avatar = None
        from utils.cards import get_style, fetch_bytes, get_skin, apply_skin, get_tier_names
        style = apply_skin(get_style(ctx.guild.id, 'rank'), get_skin(ctx.guild.id, data['level']))
        names = get_tier_names(ctx.guild.id)
        # nitro banner wins; server style bg is the fallback
        custom_bg = banner
        if not custom_bg and style.get('bg_url'):
            if style['bg_url'] not in _BG_CACHE or time.time() - _BG_CACHE[style['bg_url']][0] > 3600:
                _BG_CACHE[style['bg_url']] = (time.time(), await fetch_bytes(style['bg_url']))
                # cap: custom backgrounds are up to 8MB each — never hoard them
                while len(_BG_CACHE) > 20:
                    _BG_CACHE.pop(next(iter(_BG_CACHE)))
            custom_bg = _BG_CACHE[style['bg_url']][1]
        card = await self.bot.loop.run_in_executor(
            None, rank_card, member, data, get_rank(ctx.guild.id, member.id),
            __import__('lang').cur_lang(ctx.guild.id), avatar, None, accent, style, custom_bg, names)
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
            rows.append((idx, name, level, pct, sub, av))
        png = await self.bot.loop.run_in_executor(None, self._lb_board_image, rows)
        head.set_image(url='attachment://leaderboard.png')
        # drop footer thumbnail clash: keep icon thumb, image below
        return [head], [discord.File(__import__('io').BytesIO(png), 'leaderboard.png')], view

    @staticmethod
    def _lb_total(level: int, xp: int) -> int:
        return sum(xp_needed(l) for l in range(max(0, level))) + max(0, xp)

    def _lb_rows(self, guild: discord.Guild, metric: str, limit: int):
        _backfill_msgs(guild.id)
        with db.conn_ctx() as conn:
            try:
                rows = conn.execute(
                    'SELECT user_id, level, xp, voice_minutes, '
                    'COALESCE(text_messages,0) AS text_messages FROM levels WHERE guild_id=?',
                    (str(guild.id),)).fetchall()
            except Exception:
                rows = conn.execute('SELECT user_id, level, xp, voice_minutes FROM levels WHERE guild_id=?',
                                    (str(guild.id),)).fetchall()
        scored = []
        for r in rows:
            if str(r['user_id']) in HIDDEN_LB:
                continue
            d = dict(r)
            d.setdefault('text_messages', 0)
            total = self._lb_total(d['level'], d['xp'])
            if metric == 'voice':
                key = d.get('voice_minutes') or 0
            elif metric == 'messages':
                key = d.get('text_messages') or 0
            elif metric == 'level':
                key = d['level']
            else:
                key = total
            scored.append((key, total, d))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return scored[:limit]

    @staticmethod
    def _lb_bar(pct: float, w: int = 14) -> str:
        f = max(0, min(w, round(pct * w)))
        return '█' * f + '░' * (w - f)

    @staticmethod
    def _lb_board_image(rows) -> bytes:
        """Midnight Gold board: rank medallion + avatar + name/level + VC/MSG
        sub-line + gold progress bar.
        rows = [(rank, name, level, pct, sub, avatar_bytes)]."""
        import io as _io
        from PIL import Image as _Img, ImageDraw as _Dr
        from utils import cardstyle as cs
        from utils.cards import cover as _cover
        W, RH, PAD, AV, GAP = 900, 116, 16, 64, 12
        H = len(rows) * (RH + GAP) - GAP + PAD * 2
        img = cs.base(W, H)
        d = _Dr.Draw(img, 'RGBA')
        for k, row in enumerate(rows):
            rank, name, level = row[0], row[1], row[2]
            pct, sub = row[3], cs.safe_img(row[4] if len(row) > 4 else '')
            av_bytes = row[5] if len(row) > 5 else None
            y0 = PAD + k * (RH + GAP)
            cs.glass(d, [PAD // 2, y0, W - PAD // 2, y0 + RH], radius=16)
            # medal
            mx, my = PAD + 44, y0 + RH // 2
            mfill = cs.medal(d, mx, my, 26, rank)
            # avatar
            ax = PAD + 84
            if av_bytes:
                try:
                    av = _cover(_Img.open(_io.BytesIO(av_bytes)).convert('RGB'), AV, AV)
                    mask = _Img.new('L', (AV, AV), 0)
                    _Dr.Draw(mask).rounded_rectangle([0, 0, AV, AV], radius=16, fill=255)
                    img.paste(av, (ax, y0 + (RH - AV) // 2), mask)
                    d = _Dr.Draw(img, 'RGBA')
                except Exception:
                    pass
            else:
                d.rounded_rectangle([ax, y0 + (RH - AV) // 2, ax + AV, y0 + (RH + AV) // 2],
                                    radius=16, fill=(40, 45, 68))
            x = ax + AV + 18
            try:
                nm = cs.fit(d, name, cs.f(30), W - x - 150)
                d.text((x, y0 + 12), f'{nm}', font=cs.f(30), fill=cs.INK)
                lw = d.textlength(nm, font=cs.f(30))
                cs.pill(d, x + lw + 14, y0 + 14, f'LVL {level}', cs.f(17), mfill, mfill)
            except Exception:
                d.text((x, y0 + 12), f'{name[:16]}', font=cs.f(30), fill=cs.INK)
            try:
                d.text((x, y0 + 52), sub[:90], font=cs.f(19, False), fill=cs.DIM)
            except Exception:
                pass
            cs.xpbar(img, x, y0 + RH - 26, W - x - 26, 8, pct, c1=mfill, knob=False)
            d = _Dr.Draw(img, 'RGBA')
        buf = _io.BytesIO()
        img.save(buf, 'PNG')
        return buf.getvalue()

    def _lb_build(self, guild: discord.Guild, metric: str, n: int):
        from lang import t as _t
        if metric not in ('xp', 'level', 'voice', 'messages'):
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
            vc = _fmt_vc(r.get('voice_minutes') or 0)
            msgs = f"{int(r.get('text_messages') or 0):,}".replace(',', ' ')
            sub = f'{total:,} XP'.replace(',', ' ') + f' • VC {vc} • MSG {msgs}'
            specs.append((i, name[:20], r['level'], pct, sub, m))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(custom_id='lbm', placeholder=_t(guild.id, 'lb.m_xp'),
                                min_values=1, max_values=1, options=[
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_xp'), value='xp',
                                                         default=(metric == 'xp')),
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_level'), value='level',
                                                         default=(metric == 'level')),
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_voice'), value='voice',
                                                         default=(metric == 'voice')),
                                    discord.SelectOption(label=_t(guild.id, 'lb.m_messages'), value='messages',
                                                         default=(metric == 'messages'))])
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
            metric = vals[0] if vals and vals[0] in ('xp', 'level', 'voice', 'messages') else 'xp'
        elif cid.startswith('lbmore:'):
            try:
                _, metric, n = cid.split(':')
                n = 5 if int(n) >= 10 else 10
            except Exception:
                # Stale/foreign board button: answer, don't interaction-fail.
                from lang import t as _t
                try:
                    return await interaction.response.send_message(
                        _t(interaction.guild_id, 'lb.empty'), ephemeral=True)
                except Exception:
                    return
        else:
            return
        # Acknowledge first: a bigger board fetches 10 avatars + renders an
        # image before it has anything to show.
        from utils.interactions import ack, finish
        mode = await ack(interaction)
        from lang import t as _t
        embeds, files, view = await self._lb_render(interaction.guild, metric, n)
        if not embeds:
            return await finish(interaction, mode, content=_t(interaction.guild_id, 'lb.empty'),
                                ephemeral=True)
        await finish(interaction, mode, embeds=embeds, attachments=files, view=view)

    @commands.group(name='levels', description='Levele (admin)')
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

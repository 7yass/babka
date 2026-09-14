"""Fit checks: auto-react fire/trash on pics + hardest-fit leaderboard."""
import discord
from discord.ext import commands

import database as db
from lang import t

FIT_CHANNEL = '1546599670074310716'
POS = {'❤', '🔥', '👽'}
NEG = {'🗑'}
ALLOWED = {'🔥', '🗑'}
TITLES = {1: 'FIRE', 2: 'HARDEST', 3: 'CLEAN'}


def _norm(e: str) -> str:
    return str(e).replace('\ufe0f', '')


def _is_pic(m: discord.Message) -> bool:
    for a in m.attachments:
        ct = (a.content_type or '').lower()
        if ct.startswith('image/') or a.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            return True
    return False


def _emoji_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype('C:/Windows/Fonts/seguiemj.ttf', size)
    except Exception:
        return None


def board_image(rows) -> bytes:
    """rows = [(rank, name, title, pct, pos, neg, avatar_bytes)]."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    from utils.cards import fonts as _fonts, cover as _cover
    _, f_mid, _ = _fonts()
    f_emoji = _emoji_font(26)
    W, RH, PAD, AV, GAP = 800, 96, 14, 64, 10
    H = len(rows) * (RH + GAP) - GAP + PAD * 2
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    RANK_COLS = {1: (250, 200, 60), 2: (195, 195, 203), 3: (210, 140, 80)}
    for k, (rank, name, title, pct, pos, neg, av_bytes) in enumerate(rows):
        y0 = PAD + k * (RH + GAP)
        d.rounded_rectangle([PAD // 2, y0, W - PAD // 2, y0 + RH], radius=10, fill=(30, 30, 35))
        if av_bytes:
            try:
                av = _cover(_Img.open(_io.BytesIO(av_bytes)).convert('RGB'), AV, AV)
                mask = _Img.new('L', (AV, AV), 0)
                _Dr.Draw(mask).rounded_rectangle([0, 0, AV, AV], radius=12, fill=255)
                img.paste(av, (PAD + 4, y0 + (RH - AV) // 2), mask)
            except Exception:
                pass
        x, ty = PAD + 4 + AV + 14, y0 + 8
        rank_col = RANK_COLS.get(rank, (150, 150, 158))
        head_parts = [(f'#{rank}', rank_col), (' • ', (110, 110, 116)), (name, (255, 255, 255))]
        if title:
            head_parts += [(' • ', (110, 110, 116)), (title, rank_col)]
        try:
            maxw = W - x - PAD
            nm = name
            while d.textlength(f'#{rank} • {nm}' + (f' • {title}' if title else ''), font=f_mid) > maxw and len(nm) > 4:
                nm = nm[:-2]
            if nm != name:
                head_parts[2] = (nm + '…', (255, 255, 255))
            cx = x
            for txt, col in head_parts:
                d.text((cx, ty), txt, font=f_mid, fill=col)
                cx += d.textlength(txt, font=f_mid)
        except Exception:
            d.text((x, ty), f'#{rank} • {name[:14]}', font=f_mid, fill=(255, 255, 255))
        # counts line: heart pos, trash neg — or plain text when neg is None
        cy = ty + 34
        try:
            if neg is None:
                d.text((x, cy), str(pos)[:60], font=f_mid, fill=(150, 150, 158))
            else:
                parts = []
                if f_emoji:
                    parts += [('❤', f_emoji)]
                parts += [(f' {pos}   ', f_mid)]
                if f_emoji:
                    parts += [('🗑', f_emoji)]
                parts += [(f' {neg}', f_mid)]
                cx = x
                for txt, font in parts:
                    d.text((cx, cy), txt, font=font, fill=(150, 150, 158))
                    cx += d.textlength(txt, font=font)
        except Exception:
            d.text((x, cy), f'{pos} fire / {neg} trash', font=f_mid, fill=(150, 150, 158))
        bx, by, bw, bh = x, y0 + RH - 20, W - x - PAD, 5
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=(58, 58, 64))
        fw = max(6, int(bw * max(0.0, min(1.0, pct))))
        d.rounded_rectangle([bx, by, bx + fw, by + bh], radius=2,
                            fill=RANK_COLS.get(rank, (168, 168, 176)))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class FitCheck(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if str(message.channel.id) != FIT_CHANNEL or not _is_pic(message):
            return
        for emoji in ('🔥', '🗑️'):
            try:
                await message.add_reaction(emoji)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # fit channel: only fire + trash survive, everything else gets wiped
        if not payload.guild_id or str(payload.channel_id) != FIT_CHANNEL:
            return
        if _norm(payload.emoji) in ALLOWED:
            return
        try:
            guild = self.bot.get_guild(payload.guild_id)
            member = payload.member or (guild.get_member(payload.user_id) if guild else None)
            if not member or member.bot:
                return
            ch = guild.get_channel(payload.channel_id)
            msg = await ch.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    @commands.hybrid_command(name='fitleader', description='Najtrwadsze fity')
    async def fitleader(self, ctx):
        await ctx.defer()
        ch = ctx.guild.get_channel(int(FIT_CHANNEL))
        if not ch:
            return await ctx.reply('No fit channel.', ephemeral=True)
        totals: dict = {}
        try:
            async for m in ch.history(limit=300):
                if m.author.bot or not _is_pic(m):
                    continue
                pos = sum(r.count for r in m.reactions if _norm(r.emoji) in POS)
                neg = sum(r.count for r in m.reactions if _norm(r.emoji) in NEG)
                t = totals.setdefault(str(m.author.id), {'pos': 0, 'neg': 0, 'm': m.author})
                t['pos'] += pos
                t['neg'] += neg
        except Exception:
            pass
        ranked = sorted(totals.values(), key=lambda t: (-t['pos'], t['neg']))
        # merge linked alts into one entry
        with db.conn_ctx() as conn:
            links = conn.execute('SELECT u1, u2 FROM fit_links WHERE guild_id=?',
                                 (str(ctx.guild.id),)).fetchall()
        partner = {}
        for r in links:
            partner[r['u1']] = r['u2']
            partner[r['u2']] = r['u1']
        merged: dict = {}
        for t in ranked:
            uid = str(t['m'].id)
            key = min(uid, partner.get(uid, uid))
            e = merged.setdefault(key, {'pos': 0, 'neg': 0, 'm': t['m']})
            e['pos'] += t['pos']
            e['neg'] += t['neg']
            if t['pos'] > 0 and (e['m'].id != t['m'].id):
                pass  # keep first-seen member for name/avatar
        ranked = sorted(merged.values(), key=lambda t: (-t['pos'], t['neg']))[:10]
        if not ranked or ranked[0]['pos'] == 0:
            return await ctx.reply('No voted fits yet — post pics and vote.', ephemeral=True)
        top = max(t['pos'] for t in ranked)
        rows = []
        for i, t in enumerate(ranked, start=1):
            m = t['m']
            member = ctx.guild.get_member(int(m.id)) if not isinstance(m, discord.Member) else m
            name = member.display_name if member else getattr(m, 'display_name', '?')
            try:
                av = await m.display_avatar.with_size(128).read()
            except Exception:
                av = None
            rows.append((i, name[:20], TITLES.get(i, ''), t['pos'] / top if top else 0,
                         t['pos'], t['neg'], av))
        png = await self.bot.loop.run_in_executor(None, board_image, rows)
        emb = discord.Embed(title='HARDEST FITS', color=0xFFFFFF)
        emb.set_footer(text='Babka Danka')
        emb.set_image(url='attachment://fits.png')
        await ctx.reply(embed=emb, file=discord.File(__import__('io').BytesIO(png), 'fits.png'),
                        mention_author=False)


    @commands.command(name='fitlink', description='PoÅ‚Ä…cz dwa konta w jedne fity')
    async def fitlink(self, ctx, other: discord.Member):
        gid, me = str(ctx.guild.id), str(ctx.author.id)
        if other.id == ctx.author.id or other.bot:
            return await ctx.reply(t(ctx.guild.id, 'fit.link_no'), ephemeral=True)
        with db.conn_ctx() as conn:
            busy = conn.execute('SELECT * FROM fit_links WHERE guild_id=? AND (u1=? OR u2=? OR u1=? OR u2=?)',
                                (gid, me, me, str(other.id), str(other.id))).fetchone()
            if busy:
                return await ctx.reply(t(ctx.guild.id, 'fit.link_busy'), ephemeral=True)
            a, b = sorted((me, str(other.id)))
            conn.execute('INSERT INTO fit_links (guild_id, u1, u2) VALUES (?,?,?)', (gid, a, b))
        await ctx.reply(t(ctx.guild.id, 'fit.linked', user=other.display_name), ephemeral=True)

    @commands.command(name='fitunlink', description='RozÅ‚Ä…cz konta')
    async def fitunlink(self, ctx):
        gid, me = str(ctx.guild.id), str(ctx.author.id)
        from utils.checks import is_staff
        with db.conn_ctx() as conn:
            if is_staff(ctx.author):
                conn.execute('DELETE FROM fit_links WHERE guild_id=? AND (u1=? OR u2=?)', (gid, me, me))
            else:
                conn.execute('DELETE FROM fit_links WHERE guild_id=? AND (u1=? OR u2=?)', (gid, me, me))
        await ctx.reply(t(ctx.guild.id, 'fit.unlinked'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(FitCheck(bot))

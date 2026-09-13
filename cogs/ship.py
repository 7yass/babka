"""Ship: stable love % between two users + couple card. No reroll spam."""
import hashlib

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import foot


def pair_pct(gid, a: int, b: int) -> int:
    u1, u2 = sorted((str(a), str(b)))
    h = hashlib.sha256(f'{gid}:{u1}:{u2}:babka'.encode()).hexdigest()
    return int(h, 16) % 101


def verdict(pct: int) -> str:
    if pct >= 90:
        return 'ship.s90'
    if pct >= 70:
        return 'ship.s70'
    if pct >= 50:
        return 'ship.s50'
    if pct >= 30:
        return 'ship.s30'
    return 'ship.s10'


def ship_card(pct: int, av1: bytes = None, av2: bytes = None) -> bytes:
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr, ImageFont as _F
    from pathlib import Path as _P
    from utils.cards import cover as _cover
    W, H, AV = 900, 260, 170
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img, 'RGBA')
    try:
        f = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans-Bold.ttf'), 90)
        f_sm = _F.truetype(str(_P(__file__).parent.parent / 'assets' / 'DejaVuSans.ttf'), 30)
    except Exception:
        f = f_sm = _F.load_default()
    for av_bytes, x in ((av1, 60), (av2, W - 60 - AV)):
        if av_bytes:
            try:
                av = _cover(_Img.open(_io.BytesIO(av_bytes)).convert('RGB'), AV, AV)
                mask = _Img.new('L', (AV, AV), 0)
                _Dr.Draw(mask).ellipse([0, 0, AV, AV], fill=255)
                img.paste(av, (x, (H - AV) // 2), mask)
                continue
            except Exception:
                pass
        d.ellipse([x, (H - AV) // 2, x + AV, (H + AV) // 2], fill=(42, 42, 46))
    txt = f'{pct}%'
    try:
        w = d.textlength(txt, font=f)
        d.text(((W - w) / 2, 55), txt, font=f, fill=(250, 200, 210))
        bw, bh, bx, by = 300, 22, (W - 300) / 2, 185
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=11, fill=(42, 42, 46))
        fw = max(24, int(bw * pct / 100))
        bar = _Img.new('RGB', (fw, bh), (250, 200, 210))
        m = _Img.new('L', (fw, bh), 0)
        _Dr.Draw(m).rounded_rectangle([0, 0, fw, bh], radius=11, fill=255)
        img.paste(bar, (int(bx), by), m)
    except Exception:
        d.text((380, 90), txt, font=f, fill=(250, 200, 210))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class Ship(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='ship', description='Ile % miłości?')
    async def ship(self, ctx, first: discord.Member, second: discord.Member = None):
        gid = ctx.guild.id
        await ctx.defer()
        second = second or ctx.author
        if first.id == second.id:
            return await ctx.reply(t(gid, 'ship.self'), ephemeral=True)
        if first.bot or second.bot:
            return await ctx.reply(t(gid, 'ship.bot'), ephemeral=True)
        pct = pair_pct(gid, first.id, second.id)
        import time
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT pct FROM ships WHERE guild_id=? AND u1=? AND u2=?',
                               (str(gid), *sorted((str(first.id), str(second.id))))).fetchone()
            if not row:
                conn.execute('INSERT INTO ships (guild_id, u1, u2, pct, at) VALUES (?,?,?,?,?)',
                             (str(gid), *sorted((str(first.id), str(second.id))), pct, int(time.time())))
            else:
                pct = row['pct']
        v = t(gid, verdict(pct))
        e = discord.Embed(
            title=t(gid, 'ship.title', a=first.display_name, b=second.display_name),
            description=f'**{pct}%** — {v}', color=0xFFFFFF)
        e.set_footer(text=foot())
        try:
            a1 = await first.display_avatar.with_size(256).read()
        except Exception:
            a1 = None
        try:
            a2 = await second.display_avatar.with_size(256).read()
        except Exception:
            a2 = None
        png = await self.bot.loop.run_in_executor(None, ship_card, pct, a1, a2)
        e.set_image(url='attachment://ship.png')
        await ctx.reply(embed=e, file=discord.File(__import__('io').BytesIO(png), 'ship.png'))

    @commands.command(name='shiplb', description='Top pary', aliases=['ships'])
    async def shiplb(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT u1, u2, pct FROM ships WHERE guild_id=? ORDER BY pct DESC LIMIT 10',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'ship.empty'), ephemeral=True)
        lines = []
        for i, r in enumerate(rows, start=1):
            m1 = ctx.guild.get_member(int(r['u1']))
            m2 = ctx.guild.get_member(int(r['u2']))
            n1 = m1.display_name if m1 else '?'
            n2 = m2.display_name if m2 else '?'
            lines.append(f"**#{i}** {n1} + {n2} — **{r['pct']}%**")
        e = discord.Embed(title=t(gid, 'ship.lb'), description='\n'.join(lines), color=0xFFFFFF)
        e.set_footer(text=foot())
        await ctx.reply(embed=e)


async def setup(bot):
    await bot.add_cog(Ship(bot))

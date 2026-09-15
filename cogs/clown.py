"""Clown wall: a channel where the dumbest moments live. Everything posted
there gets an auto clown react; /clownleader ranks the top moments."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or


def wall_channel(guild) -> discord.TextChannel | None:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT channel_id FROM clown_cfg WHERE guild_id=?',
                           (str(guild.id),)).fetchone()
    if row and row['channel_id']:
        ch = guild.get_channel(int(row['channel_id']))
        if isinstance(ch, discord.TextChannel):
            return ch
    for ch in guild.text_channels:
        if 'clown' in (ch.name or '').lower():
            return ch
    return None


def clown_count(m: discord.Message) -> int:
    n = 0
    for r in m.reactions:
        if str(r.emoji) == '🤡':
            n += r.count
    return n


def snippet(m: discord.Message) -> str:
    txt = (m.content or '').strip().replace('\n', ' ')
    if txt:
        return txt[:60]
    if m.attachments:
        return f"[{len(m.attachments)} attachment(s)]"
    if m.embeds:
        return '[embed]'
    return '(no text)'


def board_image(rows) -> bytes:
    """rows = [(rank, name, title, pct, sub, avatar_bytes)]."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    from utils.cards import fonts as _fonts, cover as _cover
    _, f_mid, _ = _fonts()
    W, RH, PAD, AV, GAP = 800, 96, 14, 64, 10
    H = len(rows) * (RH + GAP) - GAP + PAD * 2
    img = _Img.new('RGB', (W, H), (16, 16, 19))
    d = _Dr.Draw(img)
    RANK_COLS = {1: (250, 200, 60), 2: (195, 195, 203), 3: (210, 140, 80)}
    for k, (rank, name, title, pct, sub, av_bytes) in enumerate(rows):
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
        try:
            maxw = W - x - PAD
            nm = name
            while d.textlength(f'#{rank} • {nm} • {title}', font=f_mid) > maxw and len(nm) > 4:
                nm = nm[:-2]
            cx = x
            for txt, col in ((f'#{rank}', rank_col), (' • ', (110, 110, 116)),
                             (nm + ('…' if nm != name else ''), (255, 255, 255)),
                             (' • ', (110, 110, 116)), (title, rank_col)):
                d.text((cx, ty), txt, font=f_mid, fill=col)
                cx += d.textlength(txt, font=f_mid)
        except Exception:
            d.text((x, ty), f'#{rank} • {name[:14]}', font=f_mid, fill=(255, 255, 255))
        d.text((x, ty + 36), sub[:60], font=f_mid, fill=(150, 150, 158))
        bx, by, bw, bh = x, y0 + RH - 18, W - x - PAD, 5
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=(58, 58, 64))
        fw = max(6, int(bw * max(0.0, min(1.0, pct))))
        d.rounded_rectangle([bx, by, bx + fw, by + bh], radius=2,
                            fill=RANK_COLS.get(rank, (168, 168, 176)))
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class Clown(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        wall = wall_channel(message.guild)
        if not wall or message.channel.id != wall.id:
            return
        try:
            await message.add_reaction('🤡')
        except Exception:
            pass

    @commands.group(name='clownwall', description='Ściana clownów')
    async def clownwall(self, ctx):
        await ctx.reply('.clownwall set / off', ephemeral=True)

    @clownwall.command(name='set', description='Ustaw kanał ściany')
    @staff_or('manage_guild')
    async def set_ch(self, ctx, channel: discord.TextChannel):
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO clown_cfg (guild_id, channel_id) VALUES (?,?)',
                         (str(ctx.guild.id), str(channel.id)))
        await ctx.reply(t(ctx.guild.id, 'cw.set', ch=channel.mention), ephemeral=True)

    @clownwall.command(name='off', description='Wyłącz ścianę')
    @staff_or('manage_guild')
    async def off(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM clown_cfg WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'cw.off'), ephemeral=True)

    @commands.command(name='clownleader', description='Top momentów', aliases=['clowns'])
    async def clownleader(self, ctx):
        wall = wall_channel(ctx.guild)
        if not wall:
            return await ctx.reply(t(ctx.guild.id, 'cw.none'), ephemeral=True)
        await ctx.defer()
        scored = []
        try:
            async for m in wall.history(limit=200):
                n = clown_count(m)
                if n > 0:
                    scored.append((n, m))
        except Exception:
            pass
        scored.sort(key=lambda x: -x[0])
        scored = scored[:10]
        if not scored:
            return await ctx.reply(t(ctx.guild.id, 'cw.empty'), ephemeral=True)
        top = scored[0][0]
        board = []
        links = []
        for i, (n, m) in enumerate(scored, start=1):
            name = m.author.display_name if m.author else '?'
            try:
                av = await m.author.display_avatar.with_size(128).read()
            except Exception:
                av = None
            word = 'clown' if n == 1 else 'clownów'
            board.append((i, name[:20], f'{n} {word}', n / top if top else 0, snippet(m), av))
            if i <= 3:
                links.append(f"[#{i} — {name}]({m.jump_url})")
        png = await self.bot.loop.run_in_executor(None, board_image, board)
        emb = discord.Embed(title='ŚCIANA CLOWNÓW', description='\n'.join(links), color=0xFFFFFF)
        emb.set_footer(text='babka :3')
        emb.set_image(url='attachment://clowns.png')
        await ctx.reply(embed=emb, file=discord.File(__import__('io').BytesIO(png), 'clowns.png'),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Clown(bot))

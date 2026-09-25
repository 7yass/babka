"""Anime cards — collection display, pack opening via shop, dust.

Scalable to 2000+ cards: paginated queries, indexed, no full loads.
"""
import discord
from discord.ext import commands

from lang import t

PAGE_SIZE = 24  # 4x6 grid like screenshot


class Cards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='cards', aliases=['collection', 'animecards'], invoke_without_command=True)
    async def cards(self, ctx, set_name: str = '', page: str = ''):
        """Collection view. ;cards [set] [page]"""
        from services.card_service import get_collection
        gid, uid = ctx.guild.id, ctx.author.id
        # parse page if set_name is digit
        if set_name.isdigit() and not page:
            page = set_name
            set_name = ''
        try:
            p = max(0, int(page or 1) - 1)
        except:
            p = 0
        data = get_collection(gid, uid, set_id=(set_name or None), page=p, per_page=PAGE_SIZE)
        if not data['cards'] and p == 0 and data['total'] == 0:
            return await ctx.reply("No cards yet — packs coming soon. Check `.shop` → Cards.", ephemeral=True)
        if not data['cards']:
            return await ctx.reply("No more cards on this page.", ephemeral=True)
        # Build simple embed grid description
        lines = []
        for c in data['cards']:
            owned = data['owned'].get(c['id'], 0)
            mark = "✅" if owned else "⬜"
            pn = f" #{c['id']}" if c['code'] else ""
            lines.append(f"{mark} **{c['code'] or c['id']}** [{c['rarity']}] x{owned}" + (f" LR {c['print_total']}" if c['rarity']=='LR' else ""))
        total_pages = max(1, (data['total'] + PAGE_SIZE -1)//PAGE_SIZE)
        header = f"Cards {p+1}/{total_pages} — Dust: {data['dust']} — Owned {len([k for k,v in data['owned'].items() if v])}/{data['total']}"
        if data['sets']:
            header += f" | Sets: {', '.join(s['id'] for s in data['sets'][:5])}"
        await ctx.reply(f"**{header}**\n" + "\n".join(lines[:PAGE_SIZE]), ephemeral=True)

    @commands.command(name='cardinfo', description='Card detail')
    async def cardinfo(self, ctx, code: str = ''):
        from services.card_service import get_collection
        import database as db
        gid = ctx.guild.id
        if not code:
            return await ctx.reply("Use: `.cardinfo MONO-2026-008`", ephemeral=True)
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?', (code, code)).fetchone()
            if not row:
                return await ctx.reply("No such card.", ephemeral=True)
            c = dict(row)
            owned = conn.execute('SELECT SUM(qty) c FROM anime_collection WHERE guild_id=? AND card_id=?', (str(gid), c['id'])).fetchone()['c'] or 0
        text = f"**{c['code']}** [{c['rarity']}] — {c['name'] or c['id']}\nSet: {c['set_id']} | Owned: x{owned}"
        if c['print_total']:
            left = conn.execute('SELECT COUNT(*) c FROM anime_collection WHERE guild_id=? AND card_id=?', (str(gid), c['id'])).fetchone()['c'] if 'conn' in dir() else 0
            text += f"\nPrint: {c['print_total']} total"
        if c['image_url']:
            text += f"\n{c['image_url']}"
        await ctx.reply(text, ephemeral=True)

    @commands.command(name='dust', description='Convert duplicates to dust')
    async def dust(self, ctx):
        from services.card_service import convert_duplicates
        res = convert_duplicates(ctx.guild.id, ctx.author.id)
        if res['dust_earned'] == 0:
            return await ctx.reply("No duplicates to convert.", ephemeral=True)
        await ctx.reply(f"Converted duplicates → **+{res['dust_earned']} dust**.", ephemeral=True)

    @commands.command(name='reroll', description='Reroll missing card with dust')
    async def reroll(self, ctx):
        from services.card_service import reroll_with_dust
        res = reroll_with_dust(ctx.guild.id, ctx.author.id)
        if not res['ok']:
            if res['code'] == 'no_dust':
                return await ctx.reply(f"Need {res['need']} dust, you have {res['have']}.", ephemeral=True)
            if res['code'] == 'complete':
                return await ctx.reply("Collection complete!", ephemeral=True)
            return await ctx.reply(f"Reroll failed: {res['code']}", ephemeral=True)
        c = res['card']
        await ctx.reply(f"Rerolled → **{c['code']}** [{c['rarity']}]!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Cards(bot))

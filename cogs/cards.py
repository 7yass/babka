"""Anime cards — collection display, pack opening via shop, dust.

Scalable to 2000+ cards: paginated queries, indexed, no full loads.
"""
import asyncio

import discord
from discord.ext import commands

from lang import t, set_ctx_lang

PAGE_SIZE = 15  # checklist rows per page (owned view is a binder now)


class CardBinderView(discord.ui.View):
    """One full-art card per page with ◀ ▶ flipping, rarest first.
    Personal (ephemeral) message, owner-only buttons, 5-min timeout."""

    def __init__(self, author_id: int, cards: list, owned: dict, buddy_id,
                 head: str, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.cards = cards
        self.owned = owned
        self.buddy_id = buddy_id
        self.head = head
        self.idx = 0
        self.message = None
        self._lock = asyncio.Lock()
        for label, cid, cb in (('◀', 'binder_prev', self._cb_prev),
                               ('▶', 'binder_next', self._cb_next)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.grey,
                                  custom_id=cid)
            b.callback = cb
            self.add_item(b)

    def _embed(self) -> discord.Embed:
        from services.card_service import RAR_COLORS, RAR_EMOJI, DUST_VALUE
        c = self.cards[self.idx]
        star = ' ⭐' if c['id'] == self.buddy_id else ''
        emb = discord.Embed(
            title=f"{RAR_EMOJI.get(c['rarity'], '')} {c['name'] or c['code']}{star}",
            description=self.head,
            color=RAR_COLORS.get(c['rarity'], 0x9AA0A6))
        if c.get('image_url'):
            emb.set_image(url=c['image_url'])
        emb.add_field(name='Rarity', value=f"**{c['rarity']}**", inline=True)
        emb.add_field(name='Owned', value=f"x{self.owned.get(c['id'], 0)}", inline=True)
        emb.add_field(name='Dust', value=str(DUST_VALUE.get(c['rarity'], 100)), inline=True)
        if c.get('print_total'):
            emb.add_field(name='Print', value=f"{c['print_total']} total", inline=True)
        emb.set_footer(text=f"{self.idx + 1}/{len(self.cards)} • {c.get('code')} · {c.get('set_id')}")
        return emb

    async def _flip(self, interaction: discord.Interaction, step: int):
        from utils.interactions import ack, finish
        set_ctx_lang(interaction.user)
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                'Not your collection.', ephemeral=True)
        async with self._lock:
            mode = await ack(interaction)
            self.idx = (self.idx + step) % len(self.cards)
            await finish(interaction, mode, embed=self._embed(), view=self)

    async def _cb_prev(self, interaction: discord.Interaction):
        await self._flip(interaction, -1)

    async def _cb_next(self, interaction: discord.Interaction):
        await self._flip(interaction, +1)

    async def on_timeout(self):
        try:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                await self.message.edit(embed=self._embed(), view=self)
        except Exception:
            pass
        self.stop()


class Cards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='cards', aliases=['collection', 'animecards', 'lc'], invoke_without_command=True)
    async def cards(self, ctx, set_name: str = '', page: str = ''):
        """`.cards [set] [card#]` — flip through your cards, rarest first.
        `.cards all [set]` — full checklist incl. unowned."""
        from services.card_service import get_collection, set_progress, RAR_EMOJI, card_perks
        gid, uid = ctx.guild.id, ctx.author.id
        # parse page if set_name is digit
        if set_name.isdigit() and not page:
            page = set_name
            set_name = ''
        try:
            p = max(0, int(page or 1) - 1)
        except:
            p = 0
        checklist = set_name.lower() == 'all'
        sid = None if checklist or not set_name else set_name
        if not checklist:
            # Binder: whole owned list (small in practice), start at card #p.
            data = get_collection(gid, uid, set_id=sid, page=0, per_page=1000,
                                  owned_only=True)
            perks = card_perks(gid, uid)
            buddy = (perks['buddy'] or {}).get('code')
            prog = ' · '.join(f'{s} {o}/{t_}' for s, o, t_ in set_progress(gid, uid))
            owned_n = sum(1 for v in data['owned'].values() if v)
            head = (f"Owned {owned_n}/{data['grand_total']} — Dust: {data['dust']}"
                    + (f" — Buddy: ⭐{buddy}" if buddy else "")
                    + (f"\n{prog}" if prog else ""))
            if not data['cards']:
                return await ctx.reply(f'**🃏 Cards**\n{head}\nNo cards yet — buy a pack in `.shop` → Cards.',
                                       ephemeral=True)
            view = CardBinderView(ctx.author.id, data['cards'], data['owned'],
                                  (perks['buddy'] or {}).get('id'), head)
            view.idx = min(p, len(data['cards']) - 1)
            view.message = await ctx.reply(embed=view._embed(), view=view, ephemeral=True)
            return
        data = get_collection(gid, uid, set_id=sid, page=p, per_page=PAGE_SIZE,
                              owned_only=False)
        total_pages = max(1, (data['total'] + PAGE_SIZE - 1) // PAGE_SIZE)
        header = f'🃏 Checklist {p + 1}/{total_pages}'
        if not data['cards']:
            return await ctx.reply(f'**{header}**\nNo more pages.', ephemeral=True)
        lines = []
        for c in data['cards']:
            owned = data['owned'].get(c['id'], 0)
            mark = '✅' if owned else '⬜'
            lines.append(f"{mark} {RAR_EMOJI.get(c['rarity'], '')} **{c['name'] or c['code']}** [{c['rarity']}]"
                         + (f" x{owned}" if owned else ""))
        await ctx.reply(f'**{header}**\n' + '\n'.join(lines[:PAGE_SIZE]), ephemeral=True)

    @commands.command(name='cardinfo', aliases=['lv'], description='Card detail')
    async def cardinfo(self, ctx, code: str = ''):
        from services.card_service import card_perks, DUST_VALUE, RAR_COLORS, RAR_EMOJI
        import database as db
        gid = ctx.guild.id
        if not code:
            return await ctx.reply('Use: `.cardinfo NARUTO-5556`', ephemeral=True)
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?', (code, code)).fetchone()
            if not row:
                return await ctx.reply('No such card.', ephemeral=True)
            c = dict(row)
            owned = conn.execute('SELECT SUM(qty) c FROM anime_collection WHERE guild_id=? AND card_id=?', (str(gid), c['id'])).fetchone()['c'] or 0
        buddy = (card_perks(gid, ctx.author.id)['buddy'] or {}).get('id') == c['id']
        emb = discord.Embed(
            title=f"{RAR_EMOJI.get(c['rarity'], '')} {c['name'] or c['code']}",
            description=f"**{c['code']}** [{c['rarity']}] · Set: {c['set_id']}"
                        + (' · ⭐ YOUR BUDDY' if buddy else ''),
            color=RAR_COLORS.get(c['rarity'], 0x9AA0A6))
        emb.add_field(name='Owned', value=f'x{owned}', inline=True)
        if c['print_total']:
            emb.add_field(name='Print', value=f'{c["print_total"]} total', inline=True)
        emb.add_field(name='Dust value', value=str(DUST_VALUE.get(c['rarity'], 100)), inline=True)
        if c['image_url']:
            emb.set_image(url=c['image_url'])
        await ctx.reply(embed=emb, ephemeral=True)

    @commands.command(name='dust', description='Convert duplicates to dust')
    async def dust(self, ctx):
        from services.card_service import convert_duplicates
        res = convert_duplicates(ctx.guild.id, ctx.author.id)
        if res['dust_earned'] == 0:
            return await ctx.reply("No duplicates to convert.", ephemeral=True)
        await ctx.reply(f"Converted duplicates → **+{res['dust_earned']} dust**.", ephemeral=True)

    @cards.command(name='buddy', description='Equip an owned card for buffs')
    async def cards_buddy(self, ctx, code: str = ''):
        """`.cards buddy NARUTO-5556` — equip. `.cards buddy clear` — unequip.
        Buddy rarity gives +XP (C1/R2/SR3/LR5/UR8%) and +daily coins."""
        from services.card_service import set_buddy, clear_buddy, card_perks
        gid = ctx.guild.id
        if not code or code.lower() == 'clear':
            if code.lower() == 'clear':
                clear_buddy(gid, ctx.author.id)
                return await ctx.reply('Buddy unequipped.', ephemeral=True)
            p = card_perks(gid, ctx.author.id)
            b = p['buddy']
            if not b:
                return await ctx.reply('No buddy equipped. `.cards buddy <code>` — buffs: '
                                       'C +1% XP, R +2%, SR +3%, LR +5%, UR +8% (+daily too).',
                                       ephemeral=True)
            return await ctx.reply(f"Buddy: **{b['code']}** [{b['rarity']}] — "
                                   f"+{p['xp_pct']}% XP, +{p['daily']} daily "
                                   f"({len(p['sets_done'])} sets complete).", ephemeral=True)
        res = set_buddy(gid, ctx.author.id, code)
        if not res['ok']:
            if res['code'] == 'no_card':
                return await ctx.reply('No such card.', ephemeral=True)
            return await ctx.reply('You don\'t own that card yet — pull it from a pack first.',
                                   ephemeral=True)
        c = res['card']
        await ctx.reply(f"Buddy equipped: **{c['code']}** [{c['rarity']}] {c['name']}.", ephemeral=True)

    @cards.command(name='sync', description='Import R2 card manifest (house)')
    async def cards_sync(self, ctx):
        """House-only: load data/anime_cards_r2.json into anime_cards.
        Inserts missing cards, backfills image_url on existing ones —
        never touches collections, dust or pity."""
        import json
        import database as db
        from pathlib import Path
        if not db.is_house(ctx.author.id):
            return await ctx.reply('House only.', ephemeral=True)
        path = Path(__file__).parent.parent / 'data' / 'anime_cards_r2.json'
        if not path.exists():
            return await ctx.reply('No manifest (data/anime_cards_r2.json).', ephemeral=True)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            return await ctx.reply(f'Manifest unreadable: {e}', ephemeral=True)
        ins = upd = 0
        with db.conn_ctx() as conn:
            sets = {}
            for c in data:
                sets[c.get('set_id', 'unknown')] = sets.get(c.get('set_id', 'unknown'), 0) + 1
            for sid, cnt in sets.items():
                conn.execute('INSERT OR IGNORE INTO anime_sets (id,name,total_cards) VALUES (?,?,?)',
                             (sid, sid, cnt))
                conn.execute('UPDATE anime_sets SET total_cards=? WHERE id=?', (cnt, sid))
            for c in data:
                row = conn.execute('SELECT id FROM anime_cards WHERE id=?', (c['id'],)).fetchone()
                if row:
                    conn.execute('UPDATE anime_cards SET image_url=? WHERE id=?',
                                 (c.get('image_url', ''), c['id']))
                    upd += 1
                else:
                    conn.execute('INSERT INTO anime_cards (id,set_id,code,rarity,print_total,'
                                 'image_url,is_animated,name) VALUES (?,?,?,?,?,?,?,?)',
                                 (c['id'], c['set_id'], c.get('code', ''), c.get('rarity', 'C'),
                                  int(c.get('print_total', 0)), c.get('image_url', ''),
                                  int(bool(c.get('is_animated', 0))), c.get('name', '')))
                    ins += 1
        await ctx.reply(f'Cards synced: **{ins}** new, **{upd}** image URLs refreshed.', ephemeral=True)

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

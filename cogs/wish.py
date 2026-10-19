"""Ticket loop: .drop earns, .wish spends, .burn recycles, .cd shows timers."""
import asyncio
import io

import discord
from discord.ext import commands, tasks

from lang import t, set_ctx_lang


def _ticket_line(gid, st) -> str:
    from services.card_service import tix_em, ssr_em
    return f"{tix_em(gid)} x{st['tickets'] or 0} · {ssr_em(gid)} x{st['ssr_tickets'] or 0}"


def _mile_footer(gid, st) -> str:
    from services.card_service import ssr_em
    return (f"🎖 {st['milestone'] or 0}/200 · Milestone · SSR Ticket "
            f"({ssr_em(gid)} x{st['ssr_tickets'] or 0})")


class WishRecapView(discord.ui.View):
    """Paginated multi-wish results (20/page). Owner-only, 5-min timeout."""

    def __init__(self, author_id: int, pulls: list, state: dict, gid=None, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.pulls = pulls
        self.state = state
        self.gid = gid
        self.page = 0
        self.message = None
        self._lock = asyncio.Lock()
        for label, cid, cb in (('Prev', 'wish_prev', self._cb_prev),
                               ('Next', 'wish_next', self._cb_next)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.grey,
                                  custom_id=cid)
            b.callback = cb
            self.add_item(b)

    def _embed(self) -> discord.Embed:
        from services.card_service import RAR_COLORS, rar_em
        per, n = 20, len(self.pulls)
        pages = max(1, (n + per - 1) // per)
        self.page = max(0, min(self.page, pages - 1))
        chunk = self.pulls[self.page * per:(self.page + 1) * per]
        top = max((p['rarity'] for p in self.pulls), key=lambda r: {'R': 0, 'LR': 1, 'UR': 2}.get(r, 0))
        emb = discord.Embed(title='Multi-Wish Results', color=RAR_COLORS.get(top, 0x9AA0A6))
        if not any(p['rarity'] in ('LR', 'UR') for p in self.pulls):
            emb.description = 'No LR / UR pulled.\n'
        else:
            emb.description = ''
        base = self.page * per
        lines = []
        for i, p in enumerate(chunk, start=base):
            lines.append(f"{i} - {rar_em(self.gid, p['rarity'])} {p['rarity']} · "
                         f"{p['name']}" + (' ✨' if p.get('is_new') else ''))
        emb.description += '\n'.join(lines)
        emb.description += f"\n{_mile_footer(self.gid, self.state)}"
        emb.add_field(name='Remaining Tickets', value=_ticket_line(self.gid, self.state), inline=False)
        emb.set_footer(text=(f'Page {self.page + 1}/{pages}' if pages > 1 else 'Multi-Wish Results'))
        return emb

    async def _flip(self, interaction: discord.Interaction, step: int):
        from utils.interactions import ack, finish
        set_ctx_lang(interaction.user)
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'wi.pulls'), ephemeral=True)
        async with self._lock:
            mode = await ack(interaction)
            self.page += step
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


class Wish(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._rm_loop.start()

    def cog_unload(self):
        try:
            self._rm_loop.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=60)
    async def _rm_loop(self):
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return
        try:
            from services.card_service import rm_due
            due = await self.bot.loop.run_in_executor(None, rm_due)
        except Exception:
            return
        for gid, uid in due[:20]:
            try:
                u = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                await u.send(t(gid, 'wi.drops_full'))
            except Exception:
                continue

    @commands.command(name='drop', aliases=['d', 'ldrop'], description='Claim ticket drops')
    async def drop(self, ctx):
        from services.card_service import claim_drop, DROP_STACK_MAX, tix_em
        gid = ctx.guild.id
        res = claim_drop(gid, ctx.author.id)
        if not res['ok']:
            m = max(1, int(res['wait'] // 60))
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.drop_ready_back', a0=m, a1=res['stack'], a2=DROP_STACK_MAX), ephemeral=True)

        from utils.ui import panel, ACCENT, meter, divider
        rewards = f'{tix_em(gid)} **x1**'
        if res.get('casino_tix'):
            rewards += f'\n🎰 **{t(gid, "eco.tix_word")} x{res["casino_tix"]}**'
        body = (t(gid, 'wi.drop_claimed')
                + '\n' + divider(t(gid, 'wi.drop_rewards'))
                + '\n' + rewards
                + '\n' + divider()
                + '\n' + _ticket_line(gid, res)
                + '\n' + meter(res['stack'], DROP_STACK_MAX, t(gid, 'wi.drop_stacked')))
        await ctx.reply(view=panel(ctx.author.display_name, body, accent=ACCENT['good']),
                        mention_author=False)

    @commands.command(name='cd', aliases=['lcd'], description='Cooldowns + ticket balance')
    async def cd(self, ctx):
        from services.card_service import ticket_state, claim_drop, DROP_CD, DROP_STACK_MAX, BURN_TICKET_EVERY, MILESTONE_EVERY, tix_em
        from services.quests import QUESTS, quests_done
        from cogs.gamble import bal, DAILY_CD
        import time as _t
        gid = ctx.guild.id
        ndone, NTOT = quests_done(gid, ctx.author.id), len(QUESTS)
        st = ticket_state(gid, ctx.author.id)
        now = int(_t.time())
        # Peek without claiming: accrue virtually.
        stack, last = int(st['drop_stack'] or 0), int(st['last_drop'] or 0)
        if not last:
            stack = DROP_STACK_MAX
        else:
            stack = min(DROP_STACK_MAX, stack + (now - last) // DROP_CD)
        drop_txt = '✅ Ready · `drop`' if stack > 0 else f'⏳ in {max(1, (DROP_CD - (now - last)) // 60)} min'
        b = bal(gid, ctx.author.id)
        dl_left = DAILY_CD - (now - (b.get('last_daily') or 0))
        daily_txt = '✅ Ready · `daily`' if dl_left <= 0 else f'⏳ in {dl_left // 3600}h {(dl_left % 3600) // 60}m'
        emb = discord.Embed(title=f'{ctx.author.display_name} · Cooldowns', color=0xFAC43C)
        emb.add_field(name='Drop Cooldown', value=drop_txt, inline=False)
        emb.add_field(name='Stacked Drops', value=f'{tix_em(gid)} {stack}/{DROP_STACK_MAX} · Stacked', inline=False)
        emb.add_field(name='Daily Cooldown', value=daily_txt, inline=False)
        emb.add_field(name='Rewards Progress',
                      value=(f"🔥 {st['burns'] or 0} burns (ticket every {BURN_TICKET_EVERY})\n"
                             f"🎖 {st['milestone'] or 0}/{MILESTONE_EVERY} · Guaranteed LR+ Ticket\n"
                             f"📜 {ndone}/{NTOT} · Daily Quests Done"),
                      inline=False)
        emb.add_field(name='Balance', value=_ticket_line(gid, st), inline=False)
        await ctx.reply(embed=emb, ephemeral=True)

    @commands.command(name='wish', aliases=['w', 'lwish'], description='Wish for cards with tickets')
    async def wish(self, ctx, amount: str = '1'):
        from services.card_service import wish as _wish, WISH_MULTI_MAX
        from services.card_service import RAR_COLORS, rar_em, tix_em, ssr_em, set_name
        await ctx.defer()
        gid = ctx.guild.id
        use_ssr = (amount or '').lower() in ('ssr', 'ssr+ticket', 'guaranteed')
        try:
            n = 1 if use_ssr else max(1, min(WISH_MULTI_MAX, int((amount or '1').replace(',', ''))))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.ssr'), ephemeral=True)
        res = _wish(gid, ctx.author.id, n, use_ssr=use_ssr)
        if not res['ok']:
            if res['code'] == 'no_ssr':
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.guaranteed_tickets_spend', a0=tix_em(gid)),
                                       ephemeral=True)
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.need_a0_a1', a0=res['need'], a1=tix_em(gid), a2=res['have']), ephemeral=True)

        st, pulls = res['state'], res['pulls']
        if not pulls:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.well_dry_cards'), ephemeral=True)
        if res.get('ssr_earned'):
            note = f'\n🎖 Milestone! +{res["ssr_earned"]} guaranteed LR+ ticket(s).'
        else:
            note = ''
        if len(pulls) == 1:
            p = pulls[0]
            new = 'new ' if p.get('is_new') else ''
            emb = discord.Embed(
                title=f'{ctx.author.display_name} just got a {new}card!',
                description=(f"**{p['name']}**\n"
                             f"**Rarity:** {rar_em(gid, p['rarity'])} | **ID:** `{p['code']}`\n"
                             f"**Banner Type**\nPermanent\n"
                             f"**Series**\n{set_name(p['set_id'])}\n"
                             f"Balance\n{_ticket_line(gid, st)}{note}\n"
                             f"{_mile_footer(gid, st)}"),
                color=RAR_COLORS.get(p['rarity'], 0x9AA0A6))
            # Framed slab (cached per card); raw URL when anything fails.
            try:
                from services.cardframe import get_framed_card
                framed = await asyncio.get_running_loop().run_in_executor(
                    None, get_framed_card, p)
            except Exception:
                framed = None
            if framed:
                emb.set_image(url='attachment://card.png')
                return await ctx.reply(embed=emb,
                                       file=discord.File(io.BytesIO(framed), 'card.png'),
                                       mention_author=False)
            if p.get('image_url'):
                emb.set_image(url=p['image_url'])
            return await ctx.reply(embed=emb, mention_author=False)
        view = WishRecapView(ctx.author.id, pulls, st, gid)
        view.message = await ctx.reply(embed=view._embed(), view=view, mention_author=False)
        if note:
            await ctx.send(t(ctx.guild.id if ctx.guild else 0, 'wi.a0_milestone_a1', a0=ctx.author.mention, a1=res['ssr_earned']))

    @commands.command(name='quest', aliases=['lquest'], description='Daily quests')
    async def quest(self, ctx):
        from services.quests import QUESTS, quest_state
        st = quest_state(ctx.guild.id, ctx.author.id)
        lines = []
        for q in QUESTS:
            prog, done = st.get(q['id'], (0, False))
            kind, amt = q['reward']
            rw = f'{amt} 🎟' if kind == 'tickets' else (f'{amt} dust' if kind == 'dust' else f'{amt} coins')
            if done:
                lines.append(f"✅ **{q['label']}** — {rw}")
                continue
            fill = int(prog / max(1, q['goal']) * 10)
            lines.append(f"{'█' * fill}{'░' * (10 - fill)} **{q['label']}** {prog}/{q['goal']} — {rw}")
        n = sum(1 for _, d in st.values() if d)
        emb = discord.Embed(title=f'{ctx.author.display_name} · Daily Quests ({n}/{len(QUESTS)})',
                            description='\n'.join(lines)
                            + '\n\n💬 100 msgs = 1 🎟 · 🔊 20 VC min = 1 🎟 (passive, stacks with quests)',
                            color=0xFAC43C)
        await ctx.reply(embed=emb, ephemeral=True)

    @commands.command(name='rates', aliases=['lrates'], description='Live pull rates')
    async def rates(self, ctx, code: str = ''):
        """`.rates` — pool odds. `.rates NARUTO-5556` — that card's personal odds."""
        from services.card_service import (WISH_ODDS, SSR_ODDS, MILESTONE_EVERY,
                                           BURN_TICKET_EVERY, wish_pool_counts, rar_em)
        import database as db
        gid = ctx.guild.id
        pools = wish_pool_counts()
        lines = ['**🎟 Normal wish (1 ticket)**']
        for rar, rate in WISH_ODDS.items():
            n = pools.get(rar, 0)
            per = f' — ~1/{int(round(n / rate * 100))} per card' if n and rate else ''
            lines.append(f"{rar_em(gid, rar)} {rar}: **{rate}%** ({n} cards{per})")
        lines.append(f'\n**✨ Guaranteed LR+ ticket** (every {MILESTONE_EVERY} 🎟 spent)')
        for rar, rate in SSR_ODDS.items():
            lines.append(f"{rar_em(gid, rar)} {rar}: **{rate}%**")
        lines.append(f'\n🔥 Every {BURN_TICKET_EVERY} burns = 1 🎟 · `.drop` banks 3 · `.daily` +3 🎟')
        if code:
            with db.conn_ctx() as conn:
                row = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                                   (code, code)).fetchone()
            if not row:
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'cd.such_card'), ephemeral=True)
            c = dict(row)
            base = WISH_ODDS.get(c['rarity'], 0)
            n = pools.get(c['rarity'], 0)
            personal = base / max(1, n)
            lines.append(f"\n**{c['code']}** [{c['rarity']}] — **{personal:.4f}%** per wish "
                         f"({base}% ÷ {n} {c['rarity']} cards)")
        await ctx.reply('\n'.join(lines), ephemeral=True)

    @commands.command(name='cbanner', description='Weekly featured set')
    async def banner(self, ctx):
        from services.card_service import banner_set, set_name
        import datetime as _dt
        sid, ends = banner_set()
        dt = _dt.datetime.fromtimestamp(ends, tz=_dt.timezone.utc).strftime('%a %H:%M UTC')
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.featured_banner_a0', a0=set_name(sid), a1=dt),

                        ephemeral=True)

    @commands.command(name='wheel', description='12h fortune spin')
    async def wheel(self, ctx):
        from services.card_service import wheel_spin, wheel_state
        st = wheel_state(ctx.guild.id, ctx.author.id)
        if not st['ready']:
            h, rem = divmod(st['wait'], 3600)
            m, _ = divmod(rem, 60)
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.wheel_back_a0', a0=h, a1=m), ephemeral=True)
        res = wheel_spin(ctx.guild.id, ctx.author.id)
        if not res['ok']:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.wheel_jammed_try'), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.a0_spun_a1', a0=ctx.author.display_name, a1=res['detail']),
                        mention_author=False)

    @commands.command(name='rm', description='Remind me when drops are full')
    async def rm(self, ctx):
        from services.card_service import rm_arm
        if rm_arm(ctx.guild.id, ctx.author.id):
            await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.armed_ll_dm'), ephemeral=True)
        else:
            await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.already_armed'), ephemeral=True)

    @commands.command(name='burn', aliases=['lburn'], description='Burn cards for dust + tickets')
    async def burn(self, ctx, code: str = '', count: str = '1'):
        from services.card_service import burn_cards, tix_em
        gid = ctx.guild.id
        if not code:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.burn_naruto_count', a0=tix_em(gid)), ephemeral=True)
        try:
            n = max(1, int((count or '1').replace(',', '')))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'wi.burn_naruto_count2'), ephemeral=True)
        res = burn_cards(ctx.guild.id, ctx.author.id, code, n)
        if not res['ok']:
            keys = {'no_card': 'wi.such_card', 'not_owned': 'wi.err.not_owned',
                    'is_buddy': 'wi.err.is_buddy', 'locked': 'wi.err.locked'}
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0,
                                     keys.get(res['code'], 'wi.err.burn_failed')), ephemeral=True)
        c = res['card']
        msg = (f"🔥 Burned **{c['code']}** x{res['burned']} → **+{res['dust']} dust**."
               + (f" +{res['tickets_earned']} {tix_em(gid)}!" if res['tickets_earned'] else ''))
        await ctx.reply(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Wish(bot))

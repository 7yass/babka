"""Card trading: live two-party window (.cards trade) + 3-day offers (.offer).

Money never moves mid-trade: live sessions are in-memory (a restart just
ends the window, cards stay put) and swaps are atomic. Offers persist in
the DB so the other side can answer from DMs, online or not.
"""
import asyncio
import time

import discord
from discord.ext import commands

import database as db
from lang import t, set_ctx_lang

SESSIONS = {}   # sid -> live session
USER_SES = {}   # (gid, uid) -> sid
_NEXT_SID = [1]
TRADE_TIMEOUT = 600
CHALLENGE_TIMEOUT = 300
NOTE_MAX = 200


def _my_session(gid, uid):
    sid = USER_SES.get((str(gid), str(uid)))
    return SESSIONS.get(sid) if sid else None


def _names(ids):
    """id -> (code, name, rarity). Missing ids map to ('?', '?', '?')."""
    if not ids:
        return {}
    with db.conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT id, code, name, rarity FROM anime_cards WHERE id IN "
            f"({','.join('?' for _ in ids)})", list(ids)).fetchall()
    out = {str(r['id']): (r['code'], r['name'] or r['code'], r['rarity']) for r in rows}
    for i in ids:
        out.setdefault(str(i), ('?', '?', '?'))
    return out


def trade_embed(ses) -> discord.Embed:
    from services.card_service import RAR_EMOJI
    a, b = ses['a'], ses['b']
    emb = discord.Embed(title=f'🔁 Trade #{ses["id"]}', color=0xFAC43C)
    names = _names(set(ses['give'][a]) | set(ses['give'][b]))
    for uid, tag in ((a, 'offers'), (b, 'offers')):
        cards = ses['give'][uid]
        tick = '✅' if ses['ok'][uid] else '⏳'
        if cards:
            val = '\n'.join(f"{RAR_EMOJI.get(names[c][2], '')} **{names[c][1]}** `{names[c][0]}`"
                            for c in cards)
        else:
            val = '— nothing yet —'
        emb.add_field(name=f'<@{uid}> {tick}', value=val, inline=False)
    if ses['note']:
        emb.add_field(name='📝 Note', value=ses['note'][:NOTE_MAX], inline=False)
    emb.set_footer(text='Add with `.cards trade add CODE` or the buttons · both Accept to swap')
    return emb


class _CodeModal(discord.ui.Modal):
    def __init__(self, sid: int, kind: str, uid: int):
        super().__init__(title={'add': 'Add cards', 'remove': 'Remove cards',
                                'note': 'Trade note'}[kind], timeout=120)
        self.sid, self.kind, self.uid = sid, kind, uid
        self.box = discord.ui.TextInput(
            label='Card codes (comma separated)' if kind != 'note' else 'Note (max 200)',
            style=discord.TextStyle.paragraph if kind == 'note' else discord.TextStyle.short,
            max_length=300 if kind != 'note' else NOTE_MAX)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction):
        from services.card_service import _check_tradable
        set_ctx_lang(interaction.user)
        ses = SESSIONS.get(self.sid)
        if not ses or ses['state'] != 'live':
            return await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.trade_over'), ephemeral=True)
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.side_trade'), ephemeral=True)
        from utils.interactions import ack, finish
        mode = await ack(interaction)
        if self.kind == 'note':
            ses['note'] = str(self.box.value or '')[:NOTE_MAX]
            ses['ok'][ses['a']] = ses['ok'][ses['b']] = False
        else:
            codes = [c.strip() for c in str(self.box.value or '').replace(',', ' ').split() if c.strip()]
            if not codes:
                return await finish(interaction, mode, embed=trade_embed(ses),
                                    view=ses.get('view'))
            with db.conn_ctx() as conn:
                ids = []
                for code in codes:
                    row = conn.execute('SELECT id FROM anime_cards WHERE code=? OR id=?',
                                       (code, code)).fetchone()
                    if not row:
                        return await interaction.followup.send(t(interaction.guild.id if interaction.guild else 0, 'cd.such_card_a0', a0=code),
                                                               ephemeral=True)
                    ids.append(row['id'])
                if self.kind == 'add':
                    ok, code_err, _ = _check_tradable(conn, ses['gid'], self.uid,
                                                      ses['give'][self.uid] + ids)
                    if not ok:
                        return await interaction.followup.send(
                            _trade_err(interaction.guild.id if interaction.guild else 0, code_err), ephemeral=True)
                    ses['give'][self.uid].extend(ids)
                else:
                    for cid in ids:
                        if cid in ses['give'][self.uid]:
                            ses['give'][self.uid].remove(cid)
            ses['ok'][ses['a']] = ses['ok'][ses['b']] = False
        try:
            await finish(interaction, mode, embed=trade_embed(ses), view=ses.get('view'))
        except Exception:
            pass


def _trade_err(gid, code: str) -> str:
    keys = {'no_card': 'tr.err.no_card', 'not_owned': 'tr.err.not_owned',
            'is_buddy': 'tr.err.is_buddy', 'locked': 'tr.err.locked'}
    return t(gid, keys.get(code, 'tr.err.add_failed'))


class TradeView(discord.ui.View):
    def __init__(self, sid: int):
        super().__init__(timeout=TRADE_TIMEOUT)
        self.sid = sid
        self._lock = asyncio.Lock()
        for label, cid, style, cb in (
                ('➕ Add', 'tr_add', discord.ButtonStyle.grey, self._cb_add),
                ('➖ Remove', 'tr_del', discord.ButtonStyle.grey, self._cb_del),
                ('📝 Note', 'tr_note', discord.ButtonStyle.grey, self._cb_note),
                ('✅ Accept', 'tr_ok', discord.ButtonStyle.success, self._cb_accept),
                ('❌ Cancel', 'tr_no', discord.ButtonStyle.red, self._cb_cancel)):
            b = discord.ui.Button(label=label, style=style, custom_id=cid)
            b.callback = cb
            self.add_item(b)

    def _ses(self):
        return SESSIONS.get(self.sid)

    async def _guard(self, interaction) -> bool:
        ses = self._ses()
        if not ses or ses['state'] != 'live':
            await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.trade_over'), ephemeral=True)
            return False
        if interaction.user.id not in (ses['a'], ses['b']):
            await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.re_trade'), ephemeral=True)
            return False
        return True

    async def _cb_add(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(_CodeModal(self.sid, 'add', interaction.user.id))

    async def _cb_del(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(_CodeModal(self.sid, 'remove', interaction.user.id))

    async def _cb_note(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(_CodeModal(self.sid, 'note', interaction.user.id))

    async def _cb_accept(self, interaction: discord.Interaction):
        from utils.interactions import ack, finish
        from services.card_service import swap_cards
        set_ctx_lang(interaction.user)
        if not await self._guard(interaction):
            return
        async with self._lock:
            mode = await ack(interaction)
            ses = self._ses()
            if not ses:
                return
            ses['ok'][interaction.user.id] = True
            if ses['ok'][ses['a']] and ses['ok'][ses['b']]:
                res = swap_cards(ses['gid'], ses['a'], ses['give'][ses['a']],
                                 ses['b'], ses['give'][ses['b']])
                if res['ok']:
                    emb = trade_embed(ses)
                    emb.title += ' — SWAPPED ✓'
                    emb.color = 0x2ECC71
                    _drop_session(ses)
                    await finish(interaction, mode, embed=emb, view=None)
                    self.stop()
                    return
                ses['ok'][ses['a']] = ses['ok'][ses['b']] = False
                emb = trade_embed(ses)
                emb.add_field(name='⚠️ Swap failed',
                              value=_trade_err(ctx.guild.id if ctx.guild else 0, res.get('code', ''))
                              + t(ctx.guild.id if ctx.guild else 0, 'tr.err.fix_and_accept'),
                              inline=False)
                return await finish(interaction, mode, embed=emb, view=self)
            await finish(interaction, mode, embed=trade_embed(ses), view=self)

    async def _cb_cancel(self, interaction: discord.Interaction):
        from utils.interactions import ack, finish
        set_ctx_lang(interaction.user)
        if not await self._guard(interaction):
            return
        async with self._lock:
            mode = await ack(interaction)
            ses = self._ses()
            if ses:
                _drop_session(ses)
            emb = discord.Embed(title='Trade cancelled', color=0xE74C3C)
            await finish(interaction, mode, embed=emb, view=None)
            self.stop()

    async def on_timeout(self):
        ses = self._ses()
        if ses:
            _drop_session(ses)
            try:
                if ses.get('message') is not None:
                    emb = discord.Embed(title='Trade expired (10 min, no swap)',
                                        color=0xE74C3C)
                    await ses['message'].edit(embed=emb, view=None)
            except Exception:
                pass
        self.stop()


class ChallengeView(discord.ui.View):
    def __init__(self, sid: int):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.sid = sid
        for label, cid, style, cb in (
                ('Join trade', 'tr_join', discord.ButtonStyle.success, self._cb_join),
                ('Decline', 'tr_decline', discord.ButtonStyle.grey, self._cb_decline)):
            b = discord.ui.Button(label=label, style=style, custom_id=cid)
            b.callback = cb
            self.add_item(b)

    async def _cb_join(self, interaction: discord.Interaction):
        from utils.interactions import ack, finish
        set_ctx_lang(interaction.user)
        ses = SESSIONS.get(self.sid)
        if not ses or ses['state'] != 'pending':
            return await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.challenge_over'), ephemeral=True)
        if interaction.user.id != ses['b']:
            return await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.challenge_isn'), ephemeral=True)
        mode = await ack(interaction)
        ses['state'] = 'live'
        view = TradeView(self.sid)
        ses['view'] = view
        try:
            await finish(interaction, mode, embed=trade_embed(ses), view=view)
            try:
                ses['message'] = await interaction.original_response()
            except Exception:
                pass
        except Exception:
            pass
        self.stop()

    async def _cb_decline(self, interaction: discord.Interaction):
        ses = SESSIONS.get(self.sid)
        if ses and interaction.user.id == ses['b']:
            _drop_session(ses)
            try:
                await interaction.response.edit_message(
                    embed=discord.Embed(title='Trade declined', color=0xE74C3C), view=None)
            except Exception:
                pass
        else:
            try:
                await interaction.response.send_message(t(interaction.guild.id if interaction.guild else 0, 'tr.challenge_isn'), ephemeral=True)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self):
        ses = SESSIONS.get(self.sid)
        if ses:
            _drop_session(ses)
            try:
                if ses.get('message') is not None:
                    await ses['message'].edit(
                        embed=discord.Embed(title='Trade challenge expired', color=0xE74C3C),
                        view=None)
            except Exception:
                pass
        self.stop()


def _drop_session(ses):
    SESSIONS.pop(ses['id'], None)
    USER_SES.pop((str(ses['gid']), str(ses['a'])), None)
    USER_SES.pop((str(ses['gid']), str(ses['b'])), None)


def start_challenge(gid, a_uid, b_uid) -> dict:
    if _my_session(gid, a_uid) or _my_session(gid, b_uid):
        return {'ok': False, 'code': 'busy'}
    sid = _NEXT_SID[0]
    _NEXT_SID[0] += 1
    ses = {'id': sid, 'gid': str(gid), 'a': a_uid, 'b': b_uid, 'state': 'pending',
           'give': {a_uid: [], b_uid: []}, 'ok': {a_uid: False, b_uid: False},
           'note': '', 'message': None, 'view': None}
    SESSIONS[sid] = ses
    USER_SES[(str(gid), str(a_uid))] = sid
    USER_SES[(str(gid), str(b_uid))] = sid
    return {'ok': True, 'session': ses}


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='offer', aliases=['loffer'], description='3-day card offers',
                    invoke_without_command=True)
    async def offer(self, ctx, member: discord.Member = None, *codes):
        """`.offer @user GIVE1 GIVE2 [want:WANT1,WANT2]` — offer stands 3 days.
        `.offer` alone — your received / sent / history."""
        from services.card_service import create_offer, list_offers
        gid = ctx.guild.id if ctx.guild else None
        if member is None:
            if gid is None:
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_accept_id'), ephemeral=True)
            data = list_offers(gid, ctx.author.id)
            lines = ['**📥 Received**']
            for o in data['received'][:10]:
                left = max(0, (o['expires'] - int(time.time())) // 3600)
                lines.append(f"• #{o['id']} from <@{o['from_id']}> — "
                             f"{len((o['give_json'] or '[]').split(','))} card(s), expires ~{left}h "
                             f"— `.offer accept {o['id']}`")
            lines.append('**📤 Sent**')
            for o in data['sent'][:10]:
                lines.append(f"• #{o['id']} to <@{o['to_id']}> ({o['status']}) — `.offer cancel {o['id']}`")
            if data['history']:
                lines.append('**🕘 History**')
                for o in data['history'][:5]:
                    lines.append(f"• #{o['id']} ({o['status']})")
            return await ctx.reply('\n'.join(lines), ephemeral=True)
        if member.id == ctx.author.id:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.can_offer_yourself'), ephemeral=True)
        if member.bot:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'cd.bots_don_trade'), ephemeral=True)
        give, want = [], []
        for tok in codes:
            if tok.lower().startswith('want:'):
                want.extend(c.strip() for c in tok[5:].split(',') if c.strip())
            else:
                give.append(tok)
        with db.conn_ctx() as conn:
            give_ids = []
            for code in give:
                row = conn.execute('SELECT id FROM anime_cards WHERE code=? OR id=?',
                                   (code, code)).fetchone()
                if not row:
                    return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'cd.such_card_a0', a0=code), ephemeral=True)
                give_ids.append(row['id'])
        res = create_offer(gid, ctx.author.id, member.id, give_ids, want)
        if not res['ok']:
            keys = {'self': 'tr.can_offer_yourself', 'empty': 'tr.oe.empty',
                    'no_card': 'tr.oe.no_card', 'not_owned': 'tr.oe.not_owned',
                    'is_buddy': 'tr.oe.is_buddy', 'locked': 'tr.oe.locked'}
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0,
                                     keys.get(res['code'], 'tr.oe.failed')), ephemeral=True)
        try:
            _g = ctx.guild.id if ctx.guild else 0
            await member.send(t(_g, 'tr.offer_dm_head', a0=ctx.author.id, a1=ctx.guild.name, a2=res['id'])
                              + '\n'.join(f'• `{c}`' for c in give)
                              + (t(_g, 'tr.offer_dm_want', a0=', '.join(f'`{c}`' for c in want)) if want else '')
                              + t(_g, 'tr.offer_dm_actions', a0=res['id']))
            dm = ' (DM sent)'
        except Exception:
            dm = ' (DMs closed — they can still `.offer` to see it)'
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_a0_sent', a0=res['id'], a1=member.display_name, a2=dm),
                        ephemeral=True)

    @offer.command(name='accept', description='Accept an offer')
    async def offer_accept(self, ctx, oid: str = ''):
        from services.card_service import accept_offer
        gid = ctx.guild.id if ctx.guild else None
        try:
            i = int((oid or '').lstrip('#'))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_accept_id'), ephemeral=True)
        if gid is None:
            from services.card_service import get_offer
            o = get_offer(i)
            if not o:
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.such_offer'), ephemeral=True)
            gid = o['guild_id']
        res = accept_offer(gid, ctx.author.id, i)
        if not res['ok']:
            keys = {'no_offer': 'tr.such_offer', 'not_yours': 'tr.acc.not_yours',
                    'expired': 'tr.acc.expired', 'declined': 'tr.acc.declined',
                    'cancelled': 'tr.acc.cancelled', 'filled': 'tr.acc.filled',
                    'stale': 'tr.acc.stale', 'no_card': 'tr.acc.no_card',
                    'giver_locked': 'tr.acc.giver_locked', 'giver_is_buddy': 'tr.acc.giver_is_buddy',
                    'giver_not_owned': 'tr.acc.giver_not_owned', 'taker_locked': 'tr.acc.taker_locked',
                    'taker_is_buddy': 'tr.acc.taker_is_buddy', 'taker_not_owned': 'tr.acc.taker_not_owned'}
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0,
                                     keys.get(res['code'], 'tr.acc.failed')), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_a0_filled', a0=i), ephemeral=True)

    @offer.command(name='decline', description='Decline an offer')
    async def offer_decline(self, ctx, oid: str = ''):
        from services.card_service import close_offer, get_offer
        try:
            i = int((oid or '').lstrip('#'))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_decline_id'), ephemeral=True)
        gid = ctx.guild.id if ctx.guild else None
        if gid is None:
            o = get_offer(i)
            gid = o['guild_id'] if o else None
            if gid is None:
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.such_offer'), ephemeral=True)
        res = close_offer(gid, ctx.author.id, i, 'declined')
        if not res['ok']:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_isn_open'), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_a0_declined', a0=i), ephemeral=True)

    @offer.command(name='cancel', description='Cancel your sent offer')
    async def offer_cancel(self, ctx, oid: str = ''):
        from services.card_service import close_offer, get_offer
        try:
            i = int((oid or '').lstrip('#'))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_cancel_id'), ephemeral=True)
        gid = ctx.guild.id if ctx.guild else None
        if gid is None:
            o = get_offer(i)
            gid = o['guild_id'] if o else None
            if gid is None:
                return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.such_offer'), ephemeral=True)
        res = close_offer(gid, ctx.author.id, i, 'cancelled')
        if not res['ok']:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_isn_yours'), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.offer_a0_cancelled', a0=i), ephemeral=True)

    @commands.group(name='auc', description='Card auction house', invoke_without_command=True)
    async def auc(self, ctx):
        """`.auc` — open listings. `.auc sell CODE PRICE` · `.auc buy ID` · `.auc cancel ID`.
        24h, 5% fee, expired lots return to seller."""
        from services.card_service import auc_list
        import time as _t
        rows = auc_list(ctx.guild.id)
        if not rows:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.open_lots_auc'), ephemeral=True)
        lines = []
        for r in rows:
            left = max(0, (r['expires'] - int(_t.time())) // 3600)
            lines.append(f"• #{r['id']} **{r['name']}** [{r['rarity']}] — **{r['price']}** coins "
                         f"(~{left}h left) — `.auc buy {r['id']}`")
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.auc_house') + '\n'.join(lines[:10]), ephemeral=True)

    @auc.command(name='sell', description='List a card')
    async def auc_sell(self, ctx, code: str = '', price: str = ''):
        from services.card_service import auc_sell
        if not code or not price:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.auc_sell_code'), ephemeral=True)
        try:
            p = max(100, int(price.replace(',', '')))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.price_must_number'), ephemeral=True)
        res = auc_sell(ctx.guild.id, ctx.author.id, code, p)
        if not res['ok']:
            keys = {'no_card': 'tr.such_card', 'not_owned': 'tr.ls.not_owned',
                    'is_buddy': 'tr.ls.is_buddy', 'locked': 'tr.err.locked'}
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0,
                                     keys.get(res['code'], 'tr.ls.failed')), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.listed_a0_a1', a0=res['card']['code'], a1=res['price'], a2=res['id']), ephemeral=True)


    @auc.command(name='buy', description='Buy a lot now')
    async def auc_buy(self, ctx, oid: str = ''):
        from services.card_service import auc_buy
        try:
            i = int((oid or '').lstrip('#'))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.auc_buy_id'), ephemeral=True)
        res = auc_buy(ctx.guild.id, ctx.author.id, i)
        if not res['ok']:
            keys = {'gone': 'tr.buy.gone', 'self': 'tr.buy.self', 'broke': 'tr.buy.broke'}
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0,
                                     keys.get(res['code'], 'tr.buy.failed')), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.sold_check_cards', a0=res['price'] - res['fee']),
                        ephemeral=True)

    @auc.command(name='cancel', description='Pull your lot')
    async def auc_cancel(self, ctx, oid: str = ''):
        from services.card_service import auc_cancel
        try:
            i = int((oid or '').lstrip('#'))
        except Exception:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.auc_cancel_id'), ephemeral=True)
        res = auc_cancel(ctx.guild.id, ctx.author.id, i)
        if not res['ok']:
            return await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.lot_isn_yours'), ephemeral=True)
        await ctx.reply(t(ctx.guild.id if ctx.guild else 0, 'tr.lot_a0_pulled', a0=i), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Trade(bot))

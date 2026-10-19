"""Native slash commands — the bot's first real application-command surface.

Fifty-odd prefix commands and seventeen hybrids already cover the same
ground, but a hybrid is a compromise: one callback, two syntaxes, half the
options, and a text prefix that a phone keyboard hates. This file is the
opposite — real `discord.app_commands` with real option types, real
autocomplete and real ephemeral replies.

House rules (the same ones the rest of the bot keeps):
  * one accent per screen, taken from the shared kit in utils/ui.py
  * every user-facing string goes through t(gid, key) — no hardcoded prose
  * a failure is one short line, never a traceback
  * guild_id is never assumed: DM paths refuse politely instead of crashing

The strings live in lang/slash.json (lang.json belongs to another process);
they are merged into the shared i18n table at import time.
"""
import functools
import json
import random
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import lang
from lang import reset_ctx_lang, set_ctx_lang, t
from utils.cards import get_tier_names, short as cshort, tier_for
from utils.economy import (CASINO_COINFLIP_RETURN, CASINO_COIN_CAP_MULT,
                           CASINO_MAX_BET_CAP, CASINO_ROU_CAP_MULT,
                           CASINO_ROU_SCALE, CASINO_SLOTS_CAP_MULT,
                           CASINO_SLOTS_TABLE, DAILY_CASH, DAILY_CD)
from utils.ui import (ACCENT, RARITY, divider, meter, panel, rail,
                      result_panel, stat)

from cogs.levels import get_rank, get_user, xp_needed
from services.casino import casino_tix, grant_daily, take_casino_tix
from services.card_service import (DAILY_TICKETS, DUST_VALUE, add_tickets,
                                   card_perks, get_collection, rarity_counts,
                                   set_completion, set_name, ticket_state)

# A card code looks like ABC-123. Used as a hint for the user, and by the
# "Card Here" context menu to spot a reveal in a message.
SAMPLE_CODE = 'ABC-123'


# ---- the wallet, borrowed ---------------------------------------------
# The economy, the odds and the level math are the bot's, not this file's:
# the slash surface and the prefix surface must share one wallet, one
# paytable and one set of bet caps, so the helpers are borrowed rather
# than re-implemented.
#
# cogs.gamble is resolved lazily: it is being migrated to the ledger right
# now, and a broken import there must not take the whole slash surface
# down with it. Every call site degrades to one short error line instead.
class _Gamble:
    """A lazy, forgiving view of cogs.gamble."""

    def __init__(self):
        self._mod = None
        self._failed = False

    def _resolve(self):
        if self._mod is None and not self._failed:
            try:
                from cogs import gamble
                self._mod = gamble
            except Exception as e:
                self._failed = True
                print(f'[slash] cogs.gamble unavailable: '
                      f'{type(e).__name__}: {e}', flush=True)
        return self._mod

    @property
    def ok(self) -> bool:
        """True when the wallet helpers can be reached."""
        return self._resolve() is not None

    def __getattr__(self, name):
        mod = self._resolve()
        if mod is None:
            raise RuntimeError('cogs.gamble is unavailable')
        return getattr(mod, name)


GAME = _Gamble()


# ---- string table -----------------------------------------------------
_LANG_FILE = Path(__file__).resolve().parent.parent / 'lang' / 'slash.json'


def _register_strings() -> dict:
    """Hand lang/slash.json to the shared i18n table.

    lang.py already loads every lang/<domain>.json overlay, so this file
    needs no special handling — the merge below is only a safety net for
    the day lang.py loses the overlay loader. Keys that already exist in
    lang.json win; this file never overwrites the main table.
    """
    import lang as _lang
    try:
        raw = json.loads(_LANG_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[slash] could not read {_LANG_FILE}: {e}', flush=True)
        raw = {}
    for loc in ('en', 'pl'):
        table = _lang.STR.setdefault(loc, {})
        for key, text in (raw.get(loc) or {}).items():
            table.setdefault(key, text)
    return raw


_STRINGS = _register_strings()


def _L(key: str) -> app_commands.locale_str:
    """A command/parameter description Discord can show in both languages."""
    en = (_STRINGS.get('en') or {}).get(key) or key
    pl = (_STRINGS.get('pl') or {}).get(key) or en
    return app_commands.locale_str(en, pl=pl, key=key)


class SlashTranslator(app_commands.Translator):
    """Gives Discord the Polish description of every slash command.

    Without a translator Discord only ever sees one description per
    command. With this one it gets the `pl` variant of every string that
    was marked as translatable, and the bot speaks both its languages
    without a second bot.

    The lookup goes through the live language table, so a reloaded
    lang/slash.json reaches Discord at the next sync.
    """

    async def translate(self, string, locale, context):
        try:
            if getattr(locale, 'value', '') != 'pl':
                return None
            extras = getattr(string, 'extras', {}) or {}
            key = extras.get('key')
            if key:
                live = (lang.STR.get('pl') or {}).get(key)
                if live:
                    return live
            return extras.get('pl') or None
        except Exception:
            return None


# ---- small helpers ----------------------------------------------------
async def _send(interaction, body='', *, title='', accent=None, image_url=None,
                public=False, view=None):
    """One panel, one accent. Ephemeral unless the caller wants it public."""
    if view is None:
        view = panel(title, body,
                     accent=ACCENT['default'] if accent is None else accent,
                     image_url=image_url)
    try:
        await interaction.followup.send(view=view, ephemeral=not public)
    except Exception as e:
        print(f'[slash] could not deliver panel: {type(e).__name__}: {e}',
              flush=True)


async def _refuse(interaction, gid, key='slash.msg.guild_only'):
    """Polite refusal for DM / non-guild interactions."""
    try:
        await interaction.response.send_message(t(gid, key), ephemeral=True)
    except Exception:
        pass


async def _fail(interaction, gid):
    """One short line instead of a traceback."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(t(gid, 'slash.msg.error'),
                                            ephemeral=True)
        else:
            await interaction.response.send_message(t(gid, 'slash.msg.error'),
                                                    ephemeral=True)
    except Exception:
        pass


def _guarded(func):
    """Any exception inside a command becomes one short ephemeral line."""
    @functools.wraps(func)
    async def wrapper(self, interaction, *args, **kwargs):
        gid = interaction.guild_id
        try:
            set_ctx_lang(interaction.user)
            return await func(self, interaction, *args, **kwargs)
        except Exception as e:
            print(f'[slash] {func.__name__} failed: {type(e).__name__}: {e}',
                  flush=True)
            await _fail(interaction, gid)
        finally:
            reset_ctx_lang()
    return wrapper


def _cap_profit(raw: int, flat_cap: int, stake_mult: int, bet: int) -> int:
    """Win cap: profit = min(raw, max(flat_cap, stake_mult * bet))."""
    return min(raw, max(flat_cap, stake_mult * bet))


# ---- card lookups (shared with the context menus) ---------------------
def find_card(query: str) -> dict:
    """Exact code first, then a fuzzy hit on name / code / series."""
    q = (query or '').strip()
    if not q:
        return {}
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM anime_cards WHERE code=? OR id=?',
                           (q, q)).fetchone()
        if row:
            return dict(row)
        like = f'%{q}%'
        row = conn.execute(
            'SELECT * FROM anime_cards WHERE name LIKE ? OR code LIKE ? '
            'OR set_id LIKE ? ORDER BY rarity, code LIMIT 1',
            (like, like, like)).fetchone()
        return dict(row) if row else {}


def card_by_code(code: str) -> dict:
    """One card by its exact code (case-insensitive)."""
    c = (code or '').strip()
    if not c:
        return {}
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM anime_cards WHERE UPPER(code)=?',
                           (c.upper(),)).fetchone()
        return dict(row) if row else {}


def owned_qty(gid, uid, card_id) -> int:
    """How many copies of a card the member holds."""
    with db.conn_ctx() as conn:
        row = conn.execute(
            'SELECT SUM(qty) q FROM anime_collection '
            'WHERE guild_id=? AND user_id=? AND card_id=?',
            (str(gid), str(uid), str(card_id))).fetchone()
    return int(row['q'] or 0) if row else 0


def card_body(gid, card: dict, uid=None) -> str:
    """The detail block of one card: code, rarity, series, ownership."""
    rar = (card.get('rarity') or 'C').upper()
    lines = [
        stat(t(gid, 'slash.msg.card.code'), card.get('code') or card.get('id')),
        stat(t(gid, 'slash.msg.card.rarity'), rar),
        stat(t(gid, 'slash.msg.card.series'), set_name(card.get('set_id'))),
    ]
    if uid is not None:
        have = owned_qty(gid, uid, card.get('id'))
        lines.append(stat(t(gid, 'slash.msg.card.owned'), f'x{have}'))
        if not have:
            lines.append(t(gid, 'slash.msg.card.not_owned'))
    if card.get('print_total'):
        lines.append(stat(t(gid, 'slash.msg.card.print'),
                          card.get('print_total')))
    lines.append(stat(t(gid, 'slash.msg.card.animated'),
                      t(gid, 'slash.msg.yes') if card.get('is_animated')
                      else t(gid, 'slash.msg.no')))
    lines.append(stat(t(gid, 'slash.msg.card.dust'),
                      DUST_VALUE.get(rar, 100)))
    return '\n'.join(lines)


def card_panel(gid, card: dict, uid=None):
    """A card as a panel: rarity accent, art if the card has one."""
    rar = (card.get('rarity') or 'C').upper()
    image = (card.get('image_url') or '').strip()
    if not image.lower().startswith(('http://', 'https://')):
        image = None
    title = t(gid, 'slash.msg.card.title',
              name=card.get('name') or card.get('code') or card.get('id'),
              code=card.get('code') or card.get('id'))
    return panel(title, card_body(gid, card, uid),
                 accent=RARITY.get(rar, ACCENT['default']), image_url=image)


def wallet_body(gid, uid) -> str:
    """The economy block: cash, bank, net worth, tickets, streak, level."""
    wallet = GAME.bal(gid, uid)
    cash = int(wallet.get('cash') or 0)
    bank = int(wallet.get('bank') or 0)
    cards = ticket_state(gid, uid)
    level = get_user(gid, uid)
    return '\n'.join([
        stat(t(gid, 'slash.msg.balance.cash'), cshort(cash)),
        stat(t(gid, 'slash.msg.balance.bank'), cshort(bank)),
        stat(t(gid, 'slash.msg.balance.net'), cshort(cash + bank)),
        stat(t(gid, 'slash.msg.balance.casino_tix'), casino_tix(gid, uid)),
        stat(t(gid, 'slash.msg.balance.card_tix'),
             int(cards.get('tickets') or 0)),
        stat(t(gid, 'slash.msg.balance.ssr_tix'),
             int(cards.get('ssr_tickets') or 0)),
        stat(t(gid, 'slash.msg.balance.streak'),
             int(wallet.get('daily_streak') or 0)),
        stat(t(gid, 'slash.msg.balance.level'), int(level.get('level') or 0)),
    ])


def collection_body(gid, uid) -> str:
    """The collection block: count, rarity rail, dust, finished sets."""
    data = get_collection(gid, uid, per_page=1)
    owned = sum(1 for qty in (data.get('owned') or {}).values() if qty)
    total = int(data.get('grand_total') or 0)
    counts = rarity_counts(gid, uid)
    try:
        done = set_completion(gid, uid)['count']
    except Exception:
        done = 0
    if not owned:
        return t(gid, 'slash.msg.collection.empty')
    return '\n'.join([
        t(gid, 'slash.msg.collection.summary', owned=owned, total=total),
        divider(),
        t(gid, 'slash.msg.collection.rail'),
        rail(counts),
        divider(),
        stat(t(gid, 'slash.msg.collection.dust'),
             cshort(int(data.get('dust') or 0))),
        stat(t(gid, 'slash.msg.collection.sets'), done),
    ])


# ---- the cog ----------------------------------------------------------
class Slash(commands.Cog):
    """Native slash surface: economy, cards, casino, level.

    The names avoid the hybrids that are already registered as app
    commands (bal, blackjack, slots, coinflip, roulette, poker, casino,
    rich, rank, leaderboard, kick, ban, fitleader, job, work, ping,
    profile) — two app commands cannot share a name.
    """

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # One translator for the whole tree: Discord then shows the Polish
        # description to Polish members. Harmless for the hybrids — their
        # descriptions are plain strings and stay as they are. If somebody
        # else already installed a translator, theirs wins.
        try:
            if self.bot.tree.translator is None:
                await self.bot.tree.set_translator(SlashTranslator())
        except Exception as e:
            print(f'[slash] translator not installed: {type(e).__name__}: {e}',
                  flush=True)

    # ---- /economy ----------------------------------------------------
    economy = app_commands.Group(name='economy',
                                 description=_L('slash.cmd.economy'))

    @economy.command(name='balance', description=_L('slash.cmd.economy.balance'))
    @app_commands.describe(member=_L('slash.cmd.economy.balance.member'))
    @_guarded
    async def economy_balance(self, interaction: discord.Interaction,
                              member: discord.Member = None):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        if not GAME.ok:
            return await _refuse(interaction, gid, 'slash.msg.error')
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = member or interaction.user
        await _send(interaction, wallet_body(gid, target.id),
                    title=t(gid, 'slash.msg.balance.title',
                            user=target.display_name))

    @economy.command(name='daily', description=_L('slash.cmd.economy.daily'))
    @_guarded
    async def economy_daily(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        if not GAME.ok:
            return await _refuse(interaction, gid, 'slash.msg.error')
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        jailed = db.jail_left(gid, uid)
        if jailed:
            return await _send(interaction,
                               t(gid, 'slash.msg.daily.jailed',
                                 m=max(1, jailed // 60)))
        wallet = GAME.bal(gid, uid)
        now = int(time.time())
        last = int(wallet.get('last_daily') or 0)
        if now - last < DAILY_CD:
            left = DAILY_CD - (now - last)
            hours, rest = divmod(left, 3600)
            return await _send(interaction,
                               t(gid, 'slash.msg.daily.wait', h=hours,
                                 m=rest // 60))
        # A missed day resets the streak; two days in a row and it is over.
        if last and now - last > 2 * DAILY_CD:
            streak = 1
        else:
            streak = int(wallet.get('daily_streak') or 0) + 1
        bonus = min((streak - 1) * 100, 1000)
        try:
            card_bonus = int(card_perks(gid, uid)['daily'])
        except Exception:
            card_bonus = 0
        total = DAILY_CASH + bonus + card_bonus
        GAME.add_cash(gid, uid, total)
        try:
            add_tickets(gid, uid, DAILY_TICKETS)
        except Exception:
            pass
        try:
            grant_daily(gid, uid)
        except Exception:
            pass
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_daily=?, daily_streak=? '
                         'WHERE guild_id=? AND user_id=?',
                         (now, streak, str(gid), str(uid)))
        body = '\n'.join([
            t(gid, 'slash.msg.daily.amount', amount=cshort(total)),
            t(gid, 'slash.msg.daily.breakdown', base=cshort(DAILY_CASH),
              bonus=cshort(bonus), cards=cshort(card_bonus)),
            t(gid, 'slash.msg.daily.next', streak=streak,
              next=cshort(min(streak * 100, 1000))),
        ])
        await _send(interaction, body, title=t(gid, 'slash.msg.daily.title'),
                    accent=ACCENT['gold'])

    @economy.command(name='top', description=_L('slash.cmd.economy.top'))
    @_guarded
    async def economy_top(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        if not GAME.ok:
            return await _refuse(interaction, gid, 'slash.msg.error')
        # The board is the one thing worth sharing — it stays public.
        await interaction.response.defer(ephemeral=False, thinking=True)
        with db.conn_ctx() as conn:
            rows = conn.execute(
                'SELECT user_id, cash + COALESCE(bank, 0) AS total FROM eco '
                'WHERE guild_id=? ORDER BY total DESC LIMIT 25',
                (str(gid),)).fetchall()
        rows = [r for r in rows if str(r['user_id']) not in GAME.HIDDEN_LB][:10]
        title = t(gid, 'slash.msg.top.title')
        if not rows:
            return await _send(interaction, t(gid, 'slash.msg.top.empty'),
                               title=title, public=True)
        guild = interaction.guild
        lines = []
        for i, row in enumerate(rows, start=1):
            member = guild.get_member(int(row['user_id'])) if guild else None
            name = member.display_name if member else f"user{row['user_id']}"
            lines.append(t(gid, 'slash.msg.top.row', rank=i, name=name[:24],
                           amount=cshort(int(row['total'] or 0))))
        await _send(interaction, '\n'.join(lines), title=title, public=True)

    @economy.command(name='grant', description=_L('slash.cmd.economy.grant'))
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member=_L('slash.cmd.economy.grant.member'),
                           amount=_L('slash.cmd.economy.grant.amount'))
    @_guarded
    async def economy_grant(self, interaction: discord.Interaction,
                            member: discord.Member,
                            amount: app_commands.Range[int, 1, 1000000000]):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        if not interaction.permissions.manage_guild:
            return await _refuse(interaction, gid, 'slash.msg.no_permission')
        if not GAME.ok:
            return await _refuse(interaction, gid, 'slash.msg.error')
        await interaction.response.defer(ephemeral=True, thinking=True)
        if member.bot:
            return await _send(interaction, t(gid, 'slash.msg.grant.bot'))
        cash = GAME.add_cash(gid, member.id, int(amount))
        await _send(interaction,
                    t(gid, 'slash.msg.grant.done', amount=cshort(int(amount)),
                      user=member.display_name, cash=cshort(cash)))

    # ---- /cards ------------------------------------------------------
    cards = app_commands.Group(name='cards', description=_L('slash.cmd.cards'))

    @cards.command(name='search', description=_L('slash.cmd.cards.search'))
    @app_commands.describe(card=_L('slash.cmd.cards.search.card'))
    @_guarded
    async def cards_search(self, interaction: discord.Interaction, card: str):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = find_card(card)
        if not found:
            return await _send(interaction,
                               t(gid, 'slash.msg.cards.not_found') + '\n'
                               + t(gid, 'slash.msg.cards.hint',
                                   example=SAMPLE_CODE))
        await _send(interaction, '', view=card_panel(gid, found,
                                                     interaction.user.id))

    @cards_search.autocomplete('card')
    async def cards_search_autocomplete(self, interaction: discord.Interaction,
                                        card: str):
        query = (card or '').strip()
        if not query:
            return []
        try:
            with db.conn_ctx() as conn:
                like = f'%{query}%'
                rows = conn.execute(
                    'SELECT code, name FROM anime_cards WHERE name LIKE ? '
                    'OR code LIKE ? OR set_id LIKE ? ORDER BY code LIMIT 25',
                    (like, like, like)).fetchall()
            return [app_commands.Choice(
                name=f"{r['name'] or r['code']} · {r['code']}"[:100],
                value=r['code']) for r in rows]
        except Exception:
            return []

    @cards.command(name='collection',
                   description=_L('slash.cmd.cards.collection'))
    @_guarded
    async def cards_collection(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _send(interaction, collection_body(gid, interaction.user.id),
                    title=t(gid, 'slash.msg.collection.title'))

    @cards.command(name='info', description=_L('slash.cmd.cards.info'))
    @app_commands.describe(card=_L('slash.cmd.cards.info.card'))
    @_guarded
    async def cards_info(self, interaction: discord.Interaction, card: str):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = find_card(card)
        if not found:
            return await _send(interaction,
                               t(gid, 'slash.msg.cards.not_found') + '\n'
                               + t(gid, 'slash.msg.cards.hint',
                                   example=SAMPLE_CODE))
        await _send(interaction, '', view=card_panel(gid, found,
                                                     interaction.user.id))

    # ---- /games ------------------------------------------------------
    games = app_commands.Group(name='games', description=_L('slash.cmd.games'))

    @games.command(name='play', description=_L('slash.cmd.games.play'))
    @app_commands.choices(game=[
        app_commands.Choice(name=_L('slash.choice.slots'), value='slots'),
        app_commands.Choice(name=_L('slash.choice.coinflip'), value='coinflip'),
        app_commands.Choice(name=_L('slash.choice.roulette'), value='roulette'),
    ])
    @app_commands.describe(game=_L('slash.cmd.games.play.game'),
                           bet=_L('slash.cmd.games.play.bet'))
    @_guarded
    async def games_play(self, interaction: discord.Interaction,
                         game: app_commands.Choice[str],
                         bet: app_commands.Range[int, 1, CASINO_MAX_BET_CAP] = 100):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        if not GAME.ok:
            return await _refuse(interaction, gid, 'slash.msg.error')
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        jailed = db.jail_left(gid, uid)
        if jailed:
            return await _send(interaction,
                               t(gid, 'slash.msg.daily.jailed',
                                 m=max(1, jailed // 60)))
        # One ticket buys one play — the same gate the prefix games use.
        if casino_tix(gid, uid) <= 0:
            return await _send(interaction, t(gid, 'slash.msg.play.no_ticket'))
        level = int(get_user(gid, uid).get('level') or 0)
        cap = GAME.max_bet_for(level)
        stake = max(1, min(int(bet), cap))
        notes = []
        if int(bet) > cap:
            notes.append(t(gid, 'slash.msg.play.capped', cap=cshort(cap)))
        if not GAME.take_cash(gid, uid, stake):
            return await _send(interaction,
                               t(gid, 'slash.msg.play.broke', bet=cshort(stake)))
        take_casino_tix(gid, uid)
        if game == 'slots':
            title, body, won, jackpot = _play_slots(gid, uid, stake)
        elif game == 'coinflip':
            title, body, won, jackpot = _play_coinflip(gid, uid, stake)
        else:
            title, body, won, jackpot = _play_roulette(gid, uid, stake)
        if notes:
            body = '\n'.join(notes) + '\n' + body
        view = result_panel(title, body, won=won, jackpot=jackpot)
        await _send(interaction, '', view=view)

    # ---- /level ------------------------------------------------------
    @app_commands.command(name='level', description=_L('slash.cmd.level'))
    @_guarded
    async def level(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if gid is None:
            return await _refuse(interaction, gid)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        data = get_user(gid, uid)
        lv = int(data.get('level') or 0)
        xp = int(data.get('xp') or 0)
        need = xp_needed(lv)
        total_xp = sum(xp_needed(l) for l in range(lv)) + xp
        try:
            tier = tier_for(lv, get_tier_names(gid))[0]
        except Exception:
            tier = ''
        body = '\n'.join([
            meter(xp, need, t(gid, 'slash.msg.level.xp')),
            t(gid, 'slash.msg.level.need', xp=cshort(max(0, need - xp)),
              next=lv + 1),
            stat(t(gid, 'slash.msg.level.total'), cshort(total_xp)),
            stat(t(gid, 'slash.msg.level.tier'), tier),
            stat(t(gid, 'slash.msg.level.rank'), f'#{get_rank(gid, uid)}'),
        ])
        await _send(interaction, body,
                    title=t(gid, 'slash.msg.level.title', level=lv))


# ---- casino games -----------------------------------------------------
def _play_slots(gid, uid, stake):
    """One spin on the slots. Same paytable as the prefix game."""
    table = CASINO_SLOTS_TABLE
    roll = random.random()
    acc, ret = 0.0, table[-1][1]
    for _p, _ret in table:
        acc += _p
        if roll < acc:
            ret = _ret
            break
    if ret >= 8.0:
        reels, mult = ['7', '7', '7'], 7
    elif ret >= 4.0:
        symbol = random.choice([s for s in GAME.SLOTS if s != '7'])
        reels, mult = [symbol, symbol, symbol], 3
    elif ret >= 1.0:
        symbol = random.choice(GAME.SLOTS)
        other = random.choice([s for s in GAME.SLOTS if s != symbol])
        reels, mult = [symbol, symbol, other], 1
    else:
        reels, mult = random.sample(GAME.SLOTS, 3), 0
    raw = int(stake * (ret - 1.0)) if ret > 0 else 0
    win = _cap_profit(raw, GAME.SLOTS_MAX_WIN, CASINO_SLOTS_CAP_MULT, stake)
    if win:
        GAME.add_cash(gid, uid, stake + win)
    lines = [t(gid, 'slash.msg.play.reels', reels=' | '.join(reels))]
    if mult >= 3:
        lines.append(t(gid, 'slash.msg.play.triple', symbol=reels[0], mult=mult))
    if win:
        lines.append(t(gid, 'slash.msg.play.win', amount=cshort(win)))
    else:
        lines.append(t(gid, 'slash.msg.play.lose', bet=cshort(stake)))
    return t(gid, 'slash.msg.play.title.slots'), '\n'.join(lines), bool(win), mult >= 8


def _play_coinflip(gid, uid, stake):
    """One flip of the house coin."""
    god = str(uid) in GAME.GOD_IDS
    side = random.choice(('O', 'R'))
    won = True if god else random.random() < 0.5
    result = side if won else ('R' if side == 'O' else 'O')
    win = 0
    if won:
        raw = int(stake * (CASINO_COINFLIP_RETURN - 1.0))
        win = _cap_profit(raw, GAME.COIN_MAX_WIN, CASINO_COIN_CAP_MULT, stake)
        GAME.add_cash(gid, uid, stake + win)
    label = t(gid, 'slash.msg.play.coin_result',
              side=t(gid, 'slash.msg.play.pick.red') if result == 'O'
              else t(gid, 'slash.msg.play.pick.black'))
    lines = [label]
    if win:
        lines.append(t(gid, 'slash.msg.play.win', amount=cshort(win)))
    else:
        lines.append(t(gid, 'slash.msg.play.lose', bet=cshort(stake)))
    return t(gid, 'slash.msg.play.title.coinflip'), '\n'.join(lines), bool(won), False


def _play_roulette(gid, uid, stake):
    """One spin of the wheel. Even money on red or black."""
    god = str(uid) in GAME.GOD_IDS
    kind = random.choice(('red', 'black'))
    number = GAME._roulette_spin(kind, 0, god, rigged=not god)
    won = GAME._rou_wins(number, kind, 0)
    win = 0
    if won:
        raw = int(stake * GAME.ROU_PAY.get(kind, 1) * CASINO_ROU_SCALE)
        win = _cap_profit(raw, GAME.ROU_MAX_WIN, CASINO_ROU_CAP_MULT, stake)
        GAME.add_cash(gid, uid, stake + win)
    pick = t(gid, 'slash.msg.play.pick.red' if kind == 'red'
             else 'slash.msg.play.pick.black')
    lines = [
        t(gid, 'slash.msg.play.bet_on', pick=pick),
        t(gid, 'slash.msg.play.wheel', number=number),
    ]
    if win:
        lines.append(t(gid, 'slash.msg.play.win', amount=cshort(win)))
    else:
        lines.append(t(gid, 'slash.msg.play.lose', bet=cshort(stake)))
    return (t(gid, 'slash.msg.play.title.roulette'), '\n'.join(lines),
            bool(won), False)


async def setup(bot):
    await bot.add_cog(Slash(bot))

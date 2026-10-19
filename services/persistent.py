"""Persistent panels: rebuild every panel this bot owns, on every boot.

Discord keeps the MESSAGE after a restart, but the library only knows how
to answer a click if it still holds the View that message was sent with.
A View that was only ever `send()`-ed dies with the process, so every
button on it turns into "Unknown interaction" -- forever, even though the
panel config sits happily in SQLite.

This module is the fix:

  * one View per live panel, rebuilt from SQLite on every boot,
  * a stable custom_id per panel (`kind:row-id`), so the same panel always
    lands on the same id and the cogs and the registry agree,
  * `bot.add_view()` for each of them, so a click finds its handler again,
  * and -- the part that actually matters -- every callback re-reads its row
    from SQLite at click time. A callback that closes over in-memory state
    is dead the moment the process restarts; one that re-reads the DB is
    right no matter when it runs.

Call `register_all(bot)` from `on_ready` (and again after a reconnect).
Cogs that build a panel at runtime call `track(view)` so it works right
away instead of after the next restart.
"""
from __future__ import annotations

import asyncio
import random

import discord

import database as db
from lang import t, set_ctx_lang
from utils.common import log_to_mod
from utils.embeds import WHITE, ok

# --------------------------------------------------------------- custom ids
#
# Every persistent view carries a custom_id built from its DB row, so the
# same panel always registers under the same id:
#
#   info:{panel_id}        info_panels.id
#   rules:{guild_id}       verify_panels.guild_id   (the rules panel)
#   verify:{guild_id}      verify_cfg.guild_id      (the verification gate)
#   langroles:{guild_id}   langroles_cfg.guild_id
#   tickets:{guild_id}     ticket_cfg.guild_id
#
# The ITEM custom_ids inside those views follow the same scheme, so a click
# can be traced back to exactly one row.

KIND_INFO = 'info'
KIND_RULES = 'rules'
KIND_VERIFY = 'verify'
KIND_LANGROLES = 'langroles'
KIND_TICKETS = 'tickets'

# Bookkeeping kinds for the views that keep OLD panels alive. Those items
# still carry the custom_id an earlier build put on the message, so the
# buttons users already have on their screen keep working.
KIND_RULES_OLD = 'rules_old'
KIND_VERIFY_OLD = 'verify_old'
KIND_LANGROLES_OLD = 'langroles_old'

# custom_ids used by builds before the persistent registry existed
LEGACY_VERIFY_BUTTON = 'gate:new'
LEGACY_LANGROLES_SELECT = 'langroles_pick'

# language roles offered by the langroles panel
LANGS = [('Italiano', '\U0001F1EE\U0001F1F9'), ('English', '\U0001F1EC\U0001F1E7'),
         ('Polski', '\U0001F1F5\U0001F1F1')]

# info panel button colours -- the bot is monochrome, so every style is grey
STYLES = {'grey': discord.ButtonStyle.grey, 'green': discord.ButtonStyle.grey,
          'red': discord.ButtonStyle.grey}


def cid(kind: str, ident) -> str:
    """Stable custom_id for a panel: cid('info', 12) -> 'info:12'."""
    return f'{kind}:{ident}'


# ------------------------------------------------------------------ storage

def _rows(sql: str, args=()) -> list:
    """Read rows as dicts. A missing table must not take the bot down."""
    try:
        with db.conn_ctx() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []


def info_buttons(panel_id) -> list:
    return _rows('SELECT * FROM info_buttons WHERE panel_id=? ORDER BY id ASC', (int(panel_id),))


def rules_panel(guild_id):
    """The rules panel row (verify_panels)."""
    rows = _rows('SELECT * FROM verify_panels WHERE guild_id=?', (str(guild_id),))
    return rows[0] if rows else None


def rules_panel_by_message(message_id):
    """A rules panel looked up by the message it was posted as."""
    rows = _rows('SELECT * FROM verify_panels WHERE message_id=?', (str(message_id),))
    return rows[0] if rows else None


def verify_cfg(guild_id) -> dict:
    """The verification gate row (verify_cfg)."""
    rows = _rows('SELECT * FROM verify_cfg WHERE guild_id=?', (str(guild_id),))
    return rows[0] if rows else {}


def langroles_cfg(guild_id) -> dict:
    rows = _rows('SELECT * FROM langroles_cfg WHERE guild_id=?', (str(guild_id),))
    return rows[0] if rows else {}


def ticket_cfg(guild_id) -> dict:
    rows = _rows('SELECT * FROM ticket_cfg WHERE guild_id=?', (str(guild_id),))
    return rows[0] if rows else {}


def ticket_types(guild_id) -> list:
    return _rows('SELECT * FROM ticket_types WHERE guild_id=? ORDER BY id ASC', (str(guild_id),))


# ------------------------------------------------------------------- items

class _DbButton(discord.ui.Button):
    """A button whose handler is a plain function, not a closure over state.

    The handler gets the interaction and re-reads everything it needs from
    SQLite, so the button keeps working after a restart.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self)


class _DbSelect(discord.ui.Select):
    """A select whose handler is a plain function (see _DbButton)."""

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self)


class PanelView(discord.ui.View):
    """Base panel view: never expires and carries a stable custom_id.

    `discord.ui.View` has no custom_id of its own, so we keep one next to
    it: it is the id the registry (and the cogs) talk about a panel with.
    """

    def __init__(self, kind: str, ident):
        super().__init__(timeout=None)
        self.kind = kind
        self.ident = ident
        self.custom_id = cid(kind, ident)


# --------------------------------------------------------------- callbacks

def _ident(interaction: discord.Interaction) -> str:
    """The id part of the clicked item's custom_id."""
    raw = (interaction.data or {}).get('custom_id') or ''
    _, _, ident = raw.partition(':')
    return ident


def _claimed(interaction: discord.Interaction) -> bool:
    """True when somebody already answered this interaction."""
    return bool(getattr(interaction, '_pv_handled', False))


def _claim(interaction: discord.Interaction) -> None:
    setattr(interaction, '_pv_handled', True)


async def handle_info(interaction: discord.Interaction, item=None) -> None:
    """FAQ button: answer with the text stored in info_buttons.

    The row is read again here, so an edited (or deleted) answer is always
    the one the user gets.
    """
    if _claimed(interaction):
        return
    _claim(interaction)
    gid = interaction.guild_id
    bid = _ident(interaction)
    with db.conn_ctx() as conn:
        btn = conn.execute('''SELECT b.*, p.guild_id AS g FROM info_buttons b
            JOIN info_panels p ON p.id = b.panel_id WHERE b.id=?''', (bid,)).fetchone()
    if not btn or str(btn['g']) != str(gid):
        return await interaction.response.send_message(t(gid, 'pv.info.gone'), ephemeral=True)
    await interaction.response.send_message(btn['text'][:2000], ephemeral=True)
    await asyncio.sleep(120)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def _grant_rules_role(interaction: discord.Interaction, panel) -> None:
    """Hand the rules panel's role to the clicker (or say why not)."""
    gid = interaction.guild_id
    if not panel or not panel.get('role_id'):
        return await interaction.response.send_message(t(gid, 'pv.vfy.none'), ephemeral=True)
    role = interaction.guild.get_role(int(panel['role_id']))
    if not role:
        return await interaction.response.send_message(t(gid, 'pv.vfy.no_role'), ephemeral=True)
    member = interaction.user
    if role in member.roles:
        await interaction.response.send_message(t(gid, 'pv.vfy.have'), ephemeral=True)
    else:
        try:
            await member.add_roles(role, reason='verified')
        except Exception:
            pass
        await interaction.response.send_message(t(gid, 'pv.vfy.done', role=role.name), ephemeral=True)
    await asyncio.sleep(120)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def handle_rules(interaction: discord.Interaction, item=None) -> None:
    """Rules panel button: the panel is found by guild, in the DB."""
    if _claimed(interaction):
        return
    _claim(interaction)
    await _grant_rules_role(interaction, rules_panel(interaction.guild_id))


async def handle_rules_old(interaction: discord.Interaction, item=None) -> None:
    """Rules panel button posted by an older build (`verify:<message_id>`)."""
    if _claimed(interaction):
        return
    _claim(interaction)
    panel = rules_panel_by_message(_ident(interaction)) or rules_panel(interaction.guild_id)
    await _grant_rules_role(interaction, panel)


async def grant(member: discord.Member, cfg: dict) -> None:
    """Give the verified role, drop the unverified one, note it in the modlog."""
    gid = member.guild.id
    try:
        if cfg.get('unverified_role'):
            role = member.guild.get_role(int(cfg['unverified_role']))
            if role and role in member.roles:
                await member.remove_roles(role, reason='verified')
        role = member.guild.get_role(int(cfg['role_id']))
        if role and role not in member.roles:
            await member.add_roles(role, reason='verified')
    except Exception:
        pass
    await log_to_mod(member.guild, discord.Embed(
        description=t(gid, 'pv.vf.log', user=member.mention), title=t(gid, 'pv.vf.log_title'),
        color=WHITE))


class CaptchaModal(discord.ui.Modal):
    """Maths check for the captcha verification mode."""

    def __init__(self, gid, a: int, b: int):
        super().__init__(title=t(gid, 'pv.vf.cap_title'))
        self.answer = a + b
        self.gid = gid
        self.q = discord.ui.TextInput(label=t(gid, 'pv.vf.cap_q', a=a, b=b), max_length=10)
        self.add_item(self.q)

    async def on_submit(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        cfg = verify_cfg(self.gid)
        try:
            ok = int(self.q.value.strip()) == self.answer
        except Exception:
            ok = False
        if not ok:
            return await interaction.response.send_message(t(self.gid, 'pv.vf.cap_no'), ephemeral=True)
        await grant(interaction.user, cfg)
        role = interaction.guild.get_role(int(cfg['role_id'])) if cfg.get('role_id') else None
        await interaction.response.send_message(
            t(self.gid, 'pv.vfy.done', role=role.name if role else ''), ephemeral=True)
        await asyncio.sleep(120)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


async def handle_verify(interaction: discord.Interaction, item=None) -> None:
    """Verification gate button: the gate is found by guild, in the DB."""
    if _claimed(interaction):
        return
    _claim(interaction)
    set_ctx_lang(interaction.user)
    gid = interaction.guild_id
    cfg = verify_cfg(gid)
    if not cfg.get('role_id'):
        return await interaction.response.send_message(t(gid, 'pv.vfy.none'), ephemeral=True)
    role = interaction.guild.get_role(int(cfg['role_id']))
    if not role:
        return await interaction.response.send_message(t(gid, 'pv.vfy.no_role'), ephemeral=True)
    member = interaction.user
    if role in member.roles:
        await interaction.response.send_message(t(gid, 'pv.vfy.have'), ephemeral=True)
    elif (cfg.get('method') or 'button') == 'captcha':
        a, b = random.randint(2, 20), random.randint(2, 20)
        await interaction.response.send_modal(CaptchaModal(gid, a, b))
        return
    else:
        await grant(member, cfg)
        await interaction.response.send_message(t(gid, 'pv.vfy.done', role=role.name), ephemeral=True)
    await asyncio.sleep(120)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def handle_langroles(interaction: discord.Interaction, item=None) -> None:
    """Language select: toggle the picked language roles on the member."""
    if _claimed(interaction):
        return
    _claim(interaction)
    member = interaction.user
    got, dropped = [], []
    for rid in (interaction.data.get('values') or []):
        role = interaction.guild.get_role(int(rid)) if rid.isdigit() else None
        if not role:
            continue
        try:
            if role in member.roles:
                await member.remove_roles(role, reason='language roles')
                dropped.append(role.name)
            else:
                await member.add_roles(role, reason='language roles')
                got.append(role.name)
        except Exception:
            pass
    bits = []
    gid = interaction.guild_id
    if got:
        bits.append(t(gid, 'pv.lr.got', roles=', '.join(got)))
    if dropped:
        bits.append(t(gid, 'pv.lr.dropped', roles=', '.join(dropped)))
    await interaction.response.send_message(embed=ok(' '.join(bits) or t(gid, 'pv.lr.nothing')),
                                            ephemeral=True)


async def handle_tickets(interaction: discord.Interaction, item=None) -> None:
    """Ticket panel: open a ticket of the picked type.

    The heavy lifting (and the messages) stays in the tickets cog; the
    framework only makes the panel reachable again after a restart.
    """
    if _claimed(interaction):
        return
    _claim(interaction)
    vals = interaction.data.get('values') or []
    type_id = None
    if vals and str(vals[0]).isdigit():
        type_id = int(vals[0])
    try:
        from cogs.tickets import _safe_open
    except Exception:
        return
    await _safe_open(interaction, type_id)


# ------------------------------------------------------------------- views

def build_info_view(panel_id) -> PanelView:
    """The FAQ panel of one info_panels row (one button per info_buttons row)."""
    view = PanelView(KIND_INFO, int(panel_id))
    for b in info_buttons(panel_id):
        view.add_item(_DbButton(handle_info, label=(b.get('label') or '')[:80],
                                style=STYLES.get(b.get('style'), discord.ButtonStyle.grey),
                                custom_id=cid(KIND_INFO, b['id'])))
    return view


def build_rules_view(guild_id, label=None) -> PanelView:
    """The rules panel of one guild.

    `label` overrides the stored one -- used when the panel is attached to
    a message with a label the user just typed (the row is written after
    the message is edited).
    """
    gid = int(guild_id)
    panel = rules_panel(gid)
    label = label or (panel.get('label') if panel else None) or t(gid, 'pv.rules.def_label')
    view = PanelView(KIND_RULES, gid)
    view.add_item(_DbButton(handle_rules, label=(label or '')[:80],
                            style=discord.ButtonStyle.grey, custom_id=cid(KIND_RULES, gid)))
    return view


def build_rules_old_view(panel) -> PanelView:
    """The rules panel as an older build posted it: `verify:<message_id>`."""
    if not panel or not panel.get('message_id'):
        return None
    mid = panel['message_id']
    view = PanelView(KIND_RULES_OLD, f"{panel.get('guild_id')}.{mid}")
    view.add_item(_DbButton(handle_rules_old, label=(panel.get('label') or 'Verify')[:80],
                            style=discord.ButtonStyle.grey, custom_id=f'{KIND_VERIFY}:{mid}'))
    return view


def build_verify_view(guild_id):
    """The verification gate of one guild (components v2 card, as before)."""
    gid = int(guild_id)
    cfg = verify_cfg(gid)
    title = cfg.get('title') or t(gid, 'pv.vf.def_title')
    desc = cfg.get('description') or ''
    method = cfg.get('method') or 'button'
    label = cfg.get('label') or t(gid, 'pv.vf.def_label')
    if method == 'captcha':
        desc = (desc + '\n\n' if desc else '') + t(gid, 'pv.vf.cap_hint')
    layout = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_color=0xFFFFFF)
    box.add_item(discord.ui.TextDisplay(f'## {title}\n{desc}'))
    row = discord.ui.ActionRow()
    row.add_item(_DbButton(handle_verify, label=(label or '')[:80], style=discord.ButtonStyle.grey,
                            custom_id=cid(KIND_VERIFY, gid)))
    box.add_item(row)
    layout.add_item(box)
    layout.kind = KIND_VERIFY
    layout.ident = gid
    layout.custom_id = cid(KIND_VERIFY, gid)
    return layout


def build_verify_old_view() -> PanelView:
    """The gate as an older build posted it: `gate:new`."""
    view = PanelView(KIND_VERIFY_OLD, 'gate')
    view.add_item(_DbButton(handle_verify, label='Verify', style=discord.ButtonStyle.grey,
                            custom_id=LEGACY_VERIFY_BUTTON))
    return view


def langroles_options(guild, roles=None) -> list:
    """Select options for the language roles (found by name in the guild)."""
    opts = []
    found = roles if roles is not None else []
    if not found and guild is not None:
        found = [discord.utils.find(lambda r, n=name: r.name == n, guild.roles)
                 for name, _flag in LANGS]
    for (name, flag), role in zip(LANGS, found):
        if role is None:
            continue
        opts.append(discord.SelectOption(label=name, value=str(role.id), emoji=flag))
    return opts


def _placeholder_options() -> list:
    """Stand-in options, so the custom_id stays registered even when the
    language roles are gone (the options of the live message are what the
    user actually clicks, not the ones of the registered copy)."""
    return [discord.SelectOption(label=name, value=name) for name, _flag in LANGS]


def build_langroles_view(guild_id, roles=None, guild=None) -> PanelView:
    """The language role panel of one guild."""
    gid = int(guild_id)
    view = PanelView(KIND_LANGROLES, gid)
    opts = langroles_options(guild, roles) or _placeholder_options()
    view.add_item(_DbSelect(handle_langroles, custom_id=cid(KIND_LANGROLES, gid),
                            placeholder=t(gid, 'pv.lr.pick'), min_values=1,
                            max_values=len(opts), options=opts))
    return view


def build_langroles_old_view() -> PanelView:
    """The language panel as an older build posted it: `langroles_pick`."""
    view = PanelView(KIND_LANGROLES_OLD, 'pick')
    view.add_item(_DbSelect(handle_langroles, custom_id=LEGACY_LANGROLES_SELECT,
                            placeholder='Language', min_values=1, max_values=3,
                            options=[discord.SelectOption(label=name, value=name)
                                     for name, _flag in LANGS]))
    return view


def build_tickets_view(guild_id) -> PanelView:
    """The ticket panel of one guild."""
    gid = int(guild_id)
    c = ticket_cfg(gid)
    types = ticket_types(gid)
    view = PanelView(KIND_TICKETS, gid)
    if types:
        opts = [discord.SelectOption(label=ty['label'][:100],
                                     description=(ty.get('description') or '')[:100],
                                     value=str(ty['id'])) for ty in types[:25]]
        view.add_item(_DbSelect(handle_tickets, custom_id=cid(KIND_TICKETS, gid),
                                placeholder=t(gid, 'pv.tix.pick'), min_values=1,
                                max_values=1, options=opts))
    else:
        view.add_item(_DbButton(handle_tickets, label=(c.get('btn_label') or 'Open ticket')[:80],
                                style=discord.ButtonStyle.grey,
                                custom_id=f'{cid(KIND_TICKETS, gid)}:open'))
    return view


# -------------------------------------------------------------- the registry

_VIEWS: dict = {}          # view custom_id -> the registered view
_ITEMS: set = set()        # item custom_ids the framework answers
_BOT = None                # the bot the views were registered with


def owns(custom_id: str) -> bool:
    """True when a registered panel view already answers this custom_id."""
    return bool(custom_id) and custom_id in _ITEMS


def _register(bot, view, replace: bool = True) -> bool:
    """Add one view to the bot. Never registers the same view twice.

    `replace=False` keeps a panel that is already registered untouched --
    that is what makes register_all() idempotent.
    """
    if view is None:
        return False
    key = getattr(view, 'custom_id', None)
    if not key:
        return False
    old = _VIEWS.get(key)
    if old is view:
        return False
    if old is not None and not replace:
        return False
    try:
        bot.add_view(view)
    except Exception as e:
        print(f'[-] persistent view {key} not registered: {e}')
        return False
    _VIEWS[key] = view
    try:
        for item in view.walk_children():
            item_cid = getattr(item, 'custom_id', None)
            if item_cid:
                _ITEMS.add(item_cid)
    except Exception:
        pass
    return True


def _unregister(bot, view) -> bool:
    """Best effort: drop a view from the library's store, so a rebuilt
    panel can take its place under the same custom_id."""
    store = getattr(bot, '_view_store', None)
    if store is None:
        conn = getattr(bot, '_connection', None)
        store = getattr(conn, '_view_store', None)
    if store is None or not hasattr(store, 'remove_view'):
        return False
    try:
        store.remove_view(view)
    except Exception:
        return False
    return True


def track(view, bot=None) -> None:
    """Register a newly-created persistent view so it works immediately.

    Called by the cogs right after they post a panel, so the buttons are
    alive before the next restart. A panel that is already registered is
    replaced, so a changed panel (a new FAQ button, a new label) is live
    right away.
    """
    target = bot or _BOT or getattr(view, 'bot', None)
    if target is None:
        return
    key = getattr(view, 'custom_id', None)
    old = _VIEWS.get(key) if key else None
    if old is view:
        return
    if old is not None:
        _unregister(target, old)
    _register(target, view, replace=True)


def register_all(bot) -> int:
    """Re-create and register every persistent view this bot owns.

    Called from on_ready (and again after a reconnect). Idempotent: the
    second call registers nothing new and returns the same count.

    Returns the number of views registered for this bot.
    """
    global _BOT
    _BOT = bot
    failed = 0

    def add(view):
        nonlocal failed
        if view is None:
            return
        key = getattr(view, 'custom_id', None)
        if key and _VIEWS.get(key) is not None:
            return          # already registered -> nothing to do
        if not _register(bot, view, replace=False):
            failed += 1

    for row in _rows('SELECT id FROM info_panels ORDER BY id'):
        add(build_info_view(row['id']))
    for row in _rows('SELECT * FROM verify_panels'):
        add(build_rules_view(row['guild_id']))
        add(build_rules_old_view(row))
    gates = _rows('SELECT guild_id FROM verify_cfg')
    for row in gates:
        add(build_verify_view(row['guild_id']))
    if gates:
        add(build_verify_old_view())
    langrows = _rows('SELECT guild_id FROM langroles_cfg')
    for row in langrows:
        gid = int(row['guild_id'])
        guild = bot.get_guild(gid) if hasattr(bot, 'get_guild') else None
        add(build_langroles_view(gid, guild=guild))
    if langrows:
        add(build_langroles_old_view())
    for row in _rows('SELECT guild_id FROM ticket_cfg'):
        add(build_tickets_view(row['guild_id']))
    if failed:
        print(f'[!] {failed} persistent panel view(s) could not be registered')
    return len(_VIEWS)


# ----------------------------------------------------------------- fallback

async def handle_fallback(interaction: discord.Interaction) -> bool:
    """Answer a click no registered view owns.

    The registered views are the normal path. This is the safety net for
    the case where the registry was never built (main.py forgot to call
    register_all): it routes the click the way the old raw `on_interaction`
    listeners did, by looking the panel up in the DB.
    """
    if interaction.type != discord.InteractionType.component:
        return False
    if _claimed(interaction):
        return False
    raw = (interaction.data or {}).get('custom_id') or ''
    if not raw or owns(raw):
        return False
    kind, _, ident = raw.partition(':')
    handler = None
    if kind == KIND_INFO:
        handler = handle_info
    elif kind == KIND_RULES:
        handler = handle_rules
    elif kind == KIND_LANGROLES:
        handler = handle_langroles
    elif kind == KIND_TICKETS:
        handler = handle_tickets
    elif kind == KIND_VERIFY:
        # older builds posted the rules panel as verify:<message_id>,
        # the gate itself is verify:<guild_id>
        handler = handle_rules_old if rules_panel_by_message(ident) else handle_verify
    elif raw == LEGACY_VERIFY_BUTTON:
        handler = handle_verify
    elif raw == LEGACY_LANGROLES_SELECT:
        handler = handle_langroles
    if handler is None:
        return False
    await handler(interaction)
    return True

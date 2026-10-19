"""Babka UI kit — the one place the bot's look is defined.

The bot was born with a monochrome, near-black, hairline language (welcome
cards, poker faces, Components-V2 panels) and then grew a second skin of
coloured embeds and emoji rows. Everything here is the first language,
finished: tokens, panels, and the components that were missing.

Rules of the house:
  * one accent per screen, chosen from TOKENS — never a raw hex at a call site
  * hierarchy by weight and grey, not by colour
  * dividers are hairlines, never emoji separators
  * Components-V2 Containers, so panels can hold galleries and buttons

Import `from utils.ui import ...` — do not re-derive these inline.
"""
import discord
from discord.ui import (ActionRow, Container, LayoutView, MediaGallery,
                        Separator, TextDisplay)
from discord.ui.media_gallery import MediaGalleryItem

from utils.embeds import foot

# ---- tokens ---------------------------------------------------------
# Monochrome spine. Accents stay desaturated so rarity can still speak
# without the whole screen turning into a fruit salad.
INK = 0xFFFFFF        # primary text
PAPER = 0xB5B5BD      # secondary text
MUTED = 0x7A7A84      # tertiary / meta
HAIR = 0x3A3A42       # rules and borders

ACCENT = {
    'default': INK,
    'muted': MUTED,
    'good': 0x8FD694,
    'bad': 0xE08A8A,
    'gold': 0xE8C87A,
}

RARITY = {
    'C': 0x9AA0A6,
    'R': 0x6FA8DC,
    'SR': 0xB58AD6,
    'LR': 0xE0C060,
    'UR': 0xE07060,
}

MAX_DESC = 3500        # Discord caps a LayoutView at 4000 display chars


def accent_for(rarity: str, default=INK):
    return RARITY.get((rarity or '').upper(), default)


def safe(text) -> str:
    """Escape user-controlled text before it goes into markdown.

    Trade notes, card names, channel topics and nicknames all end up in
    embed bodies. Unescaped, a user can break the layout with a stray
    underscore or inject a mention. Nothing in the bot escaped anything
    before this.
    """
    import discord.utils
    return discord.utils.escape_markdown(str(text or ''))


def uname(member) -> str:
    """A member's display name, escaped for markdown.

    Nicknames are user-controlled and they turn up in nearly every panel.
    Escaped once here rather than at fifty call sites.
    """
    try:
        name = member.display_name
    except Exception:
        name = getattr(member, 'name', '') or ''
    return safe(name)


def clamp(text: str, limit: int = MAX_DESC) -> str:
    text = text or ''
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


# ---- text primitives -------------------------------------------------
def eyebrow(text: str) -> str:
    """Small tracked label. Used instead of an emoji heading."""
    return f'*{text.upper()}*'


def divider(label: str = '') -> str:
    """Hairline rule, optionally captioned."""
    if not label:
        return '\n-# ─────────'
    return f'\n-# ─── {label} ───'


def stat(label: str, value, *, hint: str = '') -> str:
    """One aligned key/value line. The workhorse of every new panel."""
    out = f'`{label}`  **{value}**'
    if hint:
        out += f'  -# {hint}'
    return out


def bar(current: int, maximum: int, width: int = 14, fill: str = '█',
        empty: str = '░') -> str:
    """Monochrome progress bar. No colour needed to read it."""
    try:
        cur, mx = float(current), float(maximum)
    except Exception:
        return f'{empty * width}'
    if mx <= 0:
        return f'{empty * width}'
    ratio = max(0.0, min(1.0, cur / mx))
    done = int(round(ratio * width))
    return f'{fill * done}{empty * (width - done)}'


def meter(current: int, maximum: int, label: str = '', width: int = 14) -> str:
    """Bar plus a right-aligned percentage."""
    try:
        pct = max(0.0, min(100.0, float(current) / float(maximum) * 100)) if maximum else 0.0
    except Exception:
        pct = 0.0
    line = f'`{label}`  {bar(current, maximum, width)}  **{pct:.0f}%**'
    return line


def rail(counts: dict, order=('UR', 'LR', 'SR', 'R', 'C')) -> str:
    """Rarity rail — the collection's shape at a glance.

    New in the reborn UI: instead of a wall of card codes, the binder shows
    where the depth actually is. Each rarity gets a fixed-width cell sized by
    its share, so a collection deep in R but empty at UR looks different from
    the reverse without reading a single number.
    """
    total = sum(int(v or 0) for v in counts.values()) or 1
    cells = []
    for r in order:
        n = int(counts.get(r) or 0)
        cells.append(f'**{r}** {n}  -# {n / total * 100:.0f}%')
    return '  ·  '.join(cells)


def kv_block(pairs, indent: str = '') -> str:
    """Several stat() lines joined with hairlines between sections."""
    return '\n'.join(f'{indent}{stat(k, v)}' for k, v in pairs)


# ---- containers ------------------------------------------------------
def panel(title: str = '', body: str = '', *, accent: int = ACCENT['default'],
          image_url: str = None, footer_text: str = '', timeout: int = 120,
          media_items=None, title_url: str = None, author: str = None,
          timestamp: str = None, footer=None) -> LayoutView:
    """The core surface: a bordered Container with an optional media gallery.

    LayoutView caps at 4000 display characters total, so the body AND the
    title are clamped here — no caller can 400 a send by joining too many
    rows or by using a title somebody typed in.

    footer=None  -> the default rotating footer
    footer=False -> no footer at all
    footer=str   -> that exact text
    """
    layout = LayoutView(timeout=timeout)
    box = Container(accent_color=accent)
    if title:
        head = clamp(title, 200)
        if title_url:
            box.add_item(TextDisplay(f'## [{head}]({title_url})'))
        else:
            box.add_item(TextDisplay(f'## {head}'))
    if body:
        box.add_item(TextDisplay(clamp(body)))
    if author or timestamp:
        bits = []
        if author:
            bits.append(f'**{clamp(str(author), 120)}**')
        if timestamp:
            bits.append(f'-# {clamp(str(timestamp), 80)}')
        box.add_item(TextDisplay('  ·  '.join(bits)))
    items = list(media_items or [])
    if image_url:
        items.append(MediaGalleryItem(media=image_url))
    if items:
        try:
            box.add_item(MediaGallery(*items[:4]))
        except Exception:
            pass
    if footer is not False:
        text = footer if isinstance(footer, str) and footer else (footer_text or foot())
        try:
            box.add_item(TextDisplay(f'-# {clamp(text, 200)}'))
        except Exception:
            pass
    layout.add_item(box)
    return layout


def result_panel(title: str, body: str = '', *, won: bool = None,
                 jackpot: bool = False, image_url: str = None,
                 timeout: int = 120) -> LayoutView:
    """Win / loss framing. Accent and header come from the outcome alone."""
    if jackpot:
        accent, head = ACCENT['gold'], 'JACKPOT'
    elif won is True:
        accent, head = ACCENT['good'], 'WIN'
    elif won is False:
        accent, head = ACCENT['bad'], 'LOSS'
    else:
        accent, head = ACCENT['default'], None
    if head and head not in (body or ''):
        body = f'**{head}**\n{body}'
    return panel(title, body, accent=accent, image_url=image_url, timeout=timeout)


def attach_button(layout: LayoutView, button) -> LayoutView:
    """Append a button row to the panel's container.

    Components-V2 containers own their action rows, so a row added to the
    LayoutView renders outside the border. This is the supported way to keep
    buttons inside the panel.
    """
    # unpack: attach_rows takes rows as separate arguments. Passing the
    # list itself made Container.add_item receive a list, and
    # discord.py raised 'TypeError: expected Item not list'.
    return attach_rows(layout, *button_row(button))


def attach_rows(layout: LayoutView, *rows):
    """Append one or more ActionRows INSIDE the panel's container.

    The kit needs this: a screen with a select menu plus a button bar has
    two rows, and building that by hand meant copying a helper into every
    cog that wanted it.
    """
    for child in layout.children:
        if type(child).__name__ == 'Container':
            for row in rows:
                child.add_item(row)
            return layout
    for row in rows:
        layout.add_item(row)
    return layout


def button_row(*buttons, per_row: int = 4):
    """Pack buttons into ActionRows inside the container."""
    rows = []
    for i in range(0, len(buttons), per_row):
        row = ActionRow()
        for b in buttons[i:i + per_row]:
            row.add_item(b)
        rows.append(row)
    return rows


def progress_panel(title: str, current: int, maximum: int, label: str,
                   extra: str = '', *, accent: int = ACCENT['default'],
                   image_url: str = None, timeout: int = 120) -> LayoutView:
    """Meter-first panel — the shape every progression surface wants."""
    body = meter(current, maximum, label)
    if extra:
        body += '\n' + extra
    return panel(title, body, accent=accent, image_url=image_url, timeout=timeout)

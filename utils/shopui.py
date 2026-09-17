"""PokeMeow-style shop catalog shared by ;shop and ;balls.

One container: wallet header, numbered section rows ([N] chip, sprite,
name — price + coin), tip line, TO BUY block, category buttons + a
Main shop button. Item numbers are global and stable (buy by id);
a category click re-renders just that section."""
import discord

from utils.emojis import em, emoji_id


def fmt(n) -> str:
    """3,293-style thousands, PokeMeow wallet format."""
    try:
        return f'{int(n):,}'
    except Exception:
        return str(n)


def _pemo(gid, name):
    """PartialEmoji for a fleet emoji, else None (bare label button)."""
    if not name:
        return None
    try:
        eid = emoji_id(name, gid)
        if eid:
            return discord.PartialEmoji(name=name, id=eid)
    except Exception:
        pass
    return None


def catalog(gid, uid, *, tagline, coins_line, cash, sections, all_sections,
            entries, accent, cmd, tip, buy_title, buy_1, buy_2, ex_label,
            ex1, ex2, foot, section_emos=None, on_section=None, coin='coin',
            extra_head=''):
    """Build the storefront. entries: {key: {'name','price','emo','desc'}}.
    sections / all_sections: [(label, [keys])]. Numbering follows
    all_sections order (stable ids). on_section: factory(idx) -> callback,
    idx -1 = full view. Returns (LayoutView, ids) with ids = {n: key}."""
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow
    ids, n = {}, 0
    for _label, keys in all_sections:
        for k in keys:
            n += 1
            ids[n] = k
    num_of = {k: i for i, k in ids.items()}
    coin_emo = em(gid, coin, '$')
    icon = em(gid, 'market_stall', '[shop]')
    layout = LayoutView(timeout=180)
    # NOTE: Discord caps a single Container at 4000 displayable chars.
    # The full ;balls shop (37 items) blows past that, so split the
    # storefront: header / one Container per section / footer+buttons.
    # Each stays well under the limit; filtered single-section views
    # are unchanged (just fewer section containers).
    head_box = Container(accent_color=accent)
    head = (f'{icon} **{tagline}**\n'
            f'**{coins_line}:** {coin_emo} {fmt(cash)}')
    if extra_head:
        head += f'\n-# {extra_head}'
    head_box.add_item(TextDisplay(head))
    layout.add_item(head_box)
    for label, keys in sections:
        lines = []
        for k in keys:
            e = entries.get(k) or {}
            emo = em(gid, e.get('emo', ''), '•')
            name = e.get('name') or k
            lines.append(f'`[{num_of.get(k, "?")}]` {emo} **{name}**'
                         f' — {fmt(e.get("price", 0))} {coin_emo}')
        sec_box = Container(accent_color=accent)
        sec_box.add_item(TextDisplay(f'__**{label}**__\n' + '\n'.join(lines)))
        layout.add_item(sec_box)
    foot_box = Container(accent_color=accent)
    foot_box.add_item(TextDisplay(
        f'-# {tip}\n\n'
        f'__**{buy_title}**__\n'
        f'{buy_1}\n'
        f'{buy_2}\n'
        f'{ex_label} 1) `;{cmd} buy {ex1}`\n'
        f'{ex_label} 2) `;{cmd} buy {ex2}`\n'
        f'-# {foot}'))
    if on_section is not None:
        row = ActionRow()
        buttons = [((section_emos or {}).get(label, ''), label, i)
                   for i, (label, _k) in enumerate(all_sections)]
        buttons.append((coin, 'Main shop', -1))
        for emo_name, label, idx in buttons:
            if len(row.children) >= 5:
                foot_box.add_item(row)
                row = ActionRow()
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                  custom_id=f'{cmd}sec:{uid}:{idx}',
                                  emoji=_pemo(gid, emo_name))
            b.callback = on_section(idx)
            row.add_item(b)
        if row.children:
            foot_box.add_item(row)
    layout.add_item(foot_box)
    return layout, ids

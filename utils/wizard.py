"""Modal-first smart setups: one private popup, everything done. Prefix falls back to args."""
import re

import discord

LINK_RE = re.compile(r'https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)')


def parse_link(link: str):
    m = LINK_RE.match((link or '').strip('<>'))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def parse_role(guild: discord.Guild, raw: str):
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r'<@&(\d+)>', raw)
    if m:
        return guild.get_role(int(m.group(1)))
    if raw.isdigit():
        return guild.get_role(int(raw))
    low = raw.lower()
    for r in guild.roles:
        if r.name.lower() == low:
            return r
    return None


def color_style(raw: str):
    v = (raw or 'grey').strip().lower()
    if v in ('green', 'zielony'):
        return discord.ButtonStyle.green, 'green'
    if v in ('red', 'czerwony'):
        return discord.ButtonStyle.red, 'red'
    return discord.ButtonStyle.grey, 'grey'


async def need_args(ctx, usage_key: str, gid):
    """Prefix fallback when modal can't open: show usage."""
    from lang import t
    await ctx.reply(t(gid, usage_key), ephemeral=True)

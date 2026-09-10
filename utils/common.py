"""Shared: modlog delivery, duration parsing."""
import re

import discord


async def log_to_mod(guild: discord.Guild, embed: discord.Embed):
    import database as db
    settings = db.get_settings(guild.id)
    ch_id = settings.get('modlog_channel')
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id))
    if ch is None:
        return
    try:
        await ch.send(embed=embed)
    except Exception:
        pass


def parse_duration(s: str):
    if not s:
        return None
    m = re.match(r'^\s*(\d+)\s*([smhd])\s*$', s, re.I)
    if not m:
        return None
    return int(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2).lower()]


def fmt_duration(secs: int) -> str:
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f'{d}d')
    if h:
        parts.append(f'{h}h')
    if m:
        parts.append(f'{m}m')
    if s and not d and not h:
        parts.append(f'{s}s')
    return ' '.join(parts) or '0s'

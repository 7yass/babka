"""Embed style: monochrome — white/grey only, no colors, no emojis."""
import random

import discord

WHITE = 0xFFFFFF
RED = 0xFFFFFF
DARK_RED = 0xFFFFFF
GREEN = 0xFFFFFF

FOOTERS = ['babka :3', 'babki <3', 'buźka od babki', 'babka czuwa',
           'no hej', 'smacznego', 'pa pa', 'miłego dnia']


def foot() -> str:
    return random.choice(FOOTERS)


def build(description: str = '', *, title: str = '', color: int = WHITE) -> discord.Embed:
    e = discord.Embed(color=color)
    if title:
        e.title = title
    if description:
        e.description = description
    e.set_footer(text=foot())
    return e


def err(description: str = '') -> discord.Embed:
    return build(description, color=RED)


def ok(description: str = '', *, title: str = '') -> discord.Embed:
    """Public confirmation / list output. Monochrome, footer, no emojis."""
    return build(description, title=title, color=WHITE)


async def say(target, description: str = '', *, title: str = '', delete_after: int = None, **kw):
    """Send a styled embed to a channel or as a ctx reply. Returns the message."""
    em = ok(description, title=title)
    if isinstance(target, (discord.TextChannel, discord.Thread, discord.DMChannel)):
        msg = await target.send(embed=em, **kw)
    else:
        msg = await target.reply(embed=em, mention_author=False, **kw)
    if delete_after:
        await _delete_later_msg(msg, delete_after)
    return msg


async def reply_temp(target, content=None, *, embed=None, delete_after=120):
    """Ephemeral-style temp reply: visible, auto-deletes. Works for ctx + interaction."""
    if isinstance(target, discord.Interaction):
        if content:
            await target.response.send_message(content, ephemeral=True)
        else:
            await target.response.send_message(embed=embed, ephemeral=True)
        await _delete_later(target, delete_after)
    else:
        msg = await target.reply(content=content, embed=embed, mention_author=False)
        await _delete_later_msg(msg, delete_after)


async def _delete_later(interaction: discord.Interaction, delay: int):
    import asyncio
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def _delete_later_msg(msg: discord.Message, delay: int):
    import asyncio
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

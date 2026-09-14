"""Emoji fleet setup: Babka joins your servers, wipes their emoji slots and
uploads her own set (assets/emojis/*.png). HOUSE ONLY — destructive.
`.emojisetup` previews, `.emojisetup confirm` executes."""
import asyncio
import json
import time
from pathlib import Path

import discord
from discord.ext import commands

import database as db
from lang import t

EMOJI_GUILDS = [
    1499187861163868222,
    1490674128989192275,
    1370012965427871875,
    1516471753055015154,
    1549124212197818488,
    1310304820409929788,
    1549122779058540706,
    1362198875359678667,
]
IDS_FILE = Path('data/emoji_ids.json')


def _local_set() -> list:
    return sorted((Path('assets/emojis')).glob('*.png'))


class Emojis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='emojisetup', description='Emoji Babki na serwerach')
    async def emojisetup(self, ctx, action: str = ''):
        gid = ctx.guild.id
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
        local = _local_set()
        if (action or '').lower() != 'confirm':
            lines = [t(gid, 'eco.emoji_preview', n=len(local))]
            for g in EMOJI_GUILDS:
                guild = self.bot.get_guild(g)
                if guild:
                    lines.append(t(gid, 'eco.emoji_row', name=guild.name,
                                     n=len(guild.emojis), m=len(local)))
                else:
                    lines.append(t(gid, 'eco.emoji_missing', gid=g))
            lines.append(t(gid, 'eco.emoji_warn'))
            return await ctx.reply('\n'.join(lines), ephemeral=True)
        await ctx.reply(t(gid, 'eco.emoji_go'), ephemeral=True)
        mapping, report, done, total = {}, [], 0, len(EMOJI_GUILDS)
        status = await ctx.reply(t(gid, 'eco.emoji_progress', done=0, total=total))
        for g in EMOJI_GUILDS:
            guild = self.bot.get_guild(g)
            if not guild:
                report.append(t(gid, 'eco.emoji_missing', gid=g))
                done, report = await self._progress(status, gid, report, done, total)
                continue
            me = guild.get_member(self.bot.user.id)
            if not me or not me.guild_permissions.manage_expressions:
                report.append(t(gid, 'eco.emoji_noperm', name=guild.name))
                done, report = await self._progress(status, gid, report, done, total)
                continue
            wiped, added = 0, 0
            for e in list(guild.emojis):
                try:
                    await e.delete(reason='babka emoji fleet')
                    wiped += 1
                    await asyncio.sleep(1)
                except Exception:
                    pass
            guild_map = {}
            for f in local:
                try:
                    em = await guild.create_custom_emoji(
                        name=f.stem, image=f.read_bytes(), reason='babka emoji fleet')
                    guild_map[f.stem] = em.id
                    added += 1
                    await asyncio.sleep(2)
                except Exception:
                    await asyncio.sleep(10)
                    try:
                        em = await guild.create_custom_emoji(
                            name=f.stem, image=f.read_bytes(), reason='babka emoji fleet')
                        guild_map[f.stem] = em.id
                        added += 1
                    except Exception:
                        pass
            mapping[str(g)] = guild_map
            report.append(t(gid, 'eco.emoji_done', name=guild.name, wiped=wiped, added=added))
            done, report = await self._progress(status, gid, report, done, total)
        try:
            IDS_FILE.parent.mkdir(exist_ok=True)
            IDS_FILE.write_text(json.dumps({'updated': int(time.time()),
                                            'guilds': mapping}, indent=2), encoding='utf-8')
        except Exception:
            pass
        await ctx.reply('\n'.join(report))


    async def _progress(self, status, gid, report, done, total):
        done += 1
        try:
            await status.edit(content=t(gid, 'eco.emoji_progress', done=done, total=total)
                                      + '\n' + '\n'.join(report))
        except Exception:
            pass
        return done, report


async def setup(bot):
    await bot.add_cog(Emojis(bot))

"""Emoji fleet setup (SHARDED): 56 emojis spread across 8 guilds (~7 each).
Bots can use fleet emojis cross-server, so no need for 56x8 replicas.
`.emojisetup` previews, `.emojisetup confirm` executes, `.emojistop` aborts."""
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


def _plan() -> dict:
    """Round-robin: emoji i -> guild i % 8. ~7 per guild, 56 total uploads."""
    local = _local_set()
    plan: dict = {str(g): [] for g in EMOJI_GUILDS}
    for i, f in enumerate(local):
        plan[str(EMOJI_GUILDS[i % len(EMOJI_GUILDS)])].append(f)
    return plan


async def _safe_delete(emoji: discord.Emoji, reason: str) -> bool:
    try:
        await emoji.delete(reason=reason)
        return True
    except discord.HTTPException as e:
        if getattr(e, 'status', 0) == 429:
            try:
                await asyncio.sleep(float(getattr(e, 'retry_after', 5)) + 0.5)
            except Exception:
                await asyncio.sleep(5)
            try:
                await emoji.delete(reason=reason)
                return True
            except Exception:
                return False
        return False
    except Exception:
        return False


async def _safe_create(guild: discord.Guild, name: str, data: bytes, reason: str):
    try:
        return await guild.create_custom_emoji(name=name, image=data, reason=reason)
    except discord.HTTPException as e:
        if getattr(e, 'status', 0) == 429:
            try:
                await asyncio.sleep(float(getattr(e, 'retry_after', 5)) + 0.5)
            except Exception:
                await asyncio.sleep(5)
            try:
                return await guild.create_custom_emoji(name=name, image=data, reason=reason)
            except Exception:
                return None
        return None
    except Exception:
        return None


class Emojis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._stop = False

    @commands.command(name='emojistop', description='Abort emoji deploy')
    async def emojistop(self, ctx):
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(ctx.guild.id, 'eco.no_owner'), ephemeral=True)
        self._stop = True
        return await ctx.reply('Stopping emoji deploy after current emoji…', ephemeral=True)

    @commands.command(name='emojisetup', description='Emoji Babki na serwerach')
    async def emojisetup(self, ctx, action: str = ''):
        gid = ctx.guild.id
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
        local = _local_set()
        plan = _plan()
        if (action or '').lower() != 'confirm':
            lines = [f'Fleet setup SHARDED: **{len(local)}** emojis across **{len(EMOJI_GUILDS)}** servers (~{len(local) // max(1, len(EMOJI_GUILDS))} each). `em()` resolves cross-server.']
            for g in EMOJI_GUILDS:
                guild = self.bot.get_guild(g)
                assigned = [f.stem for f in plan[str(g)]]
                if guild:
                    lines.append(f'• {guild.name}: {len(guild.emojis)} now → {len(assigned)} assigned ({", ".join(assigned[:7])})')
                else:
                    lines.append(t(gid, 'eco.emoji_missing', gid=g))
            lines.append(t(gid, 'eco.emoji_warn'))
            return await ctx.reply('\n'.join(lines), ephemeral=True)
        self._stop = False
        await ctx.reply(t(gid, 'eco.emoji_go'), ephemeral=True)
        mapping, report = {}, []
        total = len(EMOJI_GUILDS)
        total_e_all = len(local)
        done_e_all = 0
        status = await ctx.reply(t(gid, 'eco.emoji_progress', done=0, total=total))
        done = 0

        for gi, g in enumerate(EMOJI_GUILDS, start=1):
            if self._stop:
                report.append(f'• STOPPED by user at server {gi}/{total}.')
                break
            guild = self.bot.get_guild(g)
            if not guild:
                try:
                    guild = await self.bot.fetch_guild(g)
                except Exception:
                    guild = None
            if not guild:
                report.append(t(gid, 'eco.emoji_missing', gid=g))
                done = await self._progress(status, gid, report, done, total)
                continue
            me = guild.get_member(self.bot.user.id)
            if me is None:
                try:
                    me = await guild.fetch_member(self.bot.user.id)
                except Exception:
                    me = None
            if not me or not me.guild_permissions.manage_expressions:
                report.append(t(gid, 'eco.emoji_noperm', name=guild.name))
                done = await self._progress(status, gid, report, done, total)
                continue

            try:
                current = list(await guild.fetch_emojis())
            except Exception:
                current = list(guild.emojis)
            existing = {e.name: e for e in current}
            wanted_list = plan[str(g)]
            wanted = {f.stem: f for f in wanted_list}
            wanted_names = list(wanted.keys())

            # Delete anything not assigned to THIS guild (cleans old 56x replicas)
            wiped = 0
            for e in list(current):
                if self._stop:
                    break
                if e.name not in wanted:
                    if await _safe_delete(e, 'babka emoji fleet shard'):
                        wiped += 1
                        existing.pop(e.name, None)
                    await asyncio.sleep(0.5)

            added, skipped = 0, 0
            failed = []
            guild_map = {n: e.id for n, e in existing.items() if n in wanted}
            data_cache: dict = {}

            def _bytes(name: str):
                if name not in data_cache:
                    data_cache[name] = wanted[name].read_bytes()
                return data_cache[name]

            last_edit = 0.0
            for idx, name in enumerate(wanted_names, start=1):
                if self._stop:
                    break
                if name in existing:
                    skipped += 1
                else:
                    try:
                        em = await _safe_create(guild, name, _bytes(name), 'babka emoji fleet shard')
                    except Exception:
                        em = None
                    if em is not None:
                        guild_map[name] = em.id
                        existing[name] = em
                        added += 1
                    else:
                        failed.append(name)
                    await asyncio.sleep(1.0)
                done_e_all += 1
                now = time.monotonic()
                if idx == len(wanted_names) or (now - last_edit) > 2.0:
                    last_edit = now
                    try:
                        await status.edit(content=(
                            f'Deploying emojis… **{done}/{total}** servers, **{done_e_all}/{total_e_all}** emojis.\n'
                            f'Current: **{guild.name}** ({gi}/{total}) — {idx}/{len(wanted_names)} '
                            f'(added {added}, kept {skipped}, wiped {wiped})'
                            + ('\n' + '\n'.join(report) if report else '')
                        ))
                    except Exception:
                        pass

            mapping[str(guild.id)] = guild_map
            try:
                IDS_FILE.parent.mkdir(exist_ok=True)
                IDS_FILE.write_text(json.dumps({'updated': int(time.time()),
                                                'guilds': mapping}, indent=2), encoding='utf-8')
            except Exception:
                pass
            line = t(gid, 'eco.emoji_done', name=guild.name, wiped=wiped, added=added)
            extras = []
            if skipped:
                extras.append(f'kept {skipped}')
            if failed:
                extras.append(f'failed {len(failed)} ({", ".join(failed[:5])}' + ('' if len(failed) <= 5 else '…') + ')')
            if extras:
                line += ' (' + ', '.join(extras) + ')'
            report.append(line)
            done = await self._progress(status, gid, report, done, total)

        if not self._stop:
            try:
                IDS_FILE.parent.mkdir(exist_ok=True)
                IDS_FILE.write_text(json.dumps({'updated': int(time.time()),
                                                'guilds': mapping}, indent=2), encoding='utf-8')
            except Exception:
                pass
        await ctx.reply('\n'.join(report) or 'Nothing to do.')

    async def _progress(self, status, gid, report, done, total):
        done += 1
        try:
            await status.edit(content=t(gid, 'eco.emoji_progress', done=done, total=total)
                                      + '\n' + '\n'.join(report))
        except Exception:
            pass
        return done


async def setup(bot):
    await bot.add_cog(Emojis(bot))

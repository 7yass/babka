"""Emoji fleet setup (SHARDED): local PNGs spread across 8 guilds.
Bots can use fleet emojis cross-server, so no need for full replicas.
`.emojisetup` previews (+ capacity preflight), `.emojisetup confirm`
re-validates then executes, `.emojistop` aborts. Static emojis only:
non-PNG files are ignored, never uploaded."""
import asyncio
import json
import re
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


EMOJI_MAX_BYTES = 256 * 1024  # Discord per-emoji cap
EMOJI_NAME_RE = re.compile(r'^[A-Za-z0-9_]{2,32}$')  # Discord emoji name rules


def _local_set() -> list:
    return sorted((Path('assets/emojis')).glob('*.png'))


def _ignored_files() -> list:
    """Non-PNG files in the emoji dir. The uploader is static-only;
    these are reported, never uploaded."""
    d = Path('assets/emojis')
    try:
        return sorted(p.name for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() != '.png')
    except Exception:
        return []


def _validate_assets(files: list) -> tuple:
    """Split into (valid, [(filename, reason)]). Invalid files are excluded
    from the plan with a warning — they could never be created."""
    valid, invalid = [], []
    for f in files:
        name = f.stem
        if not EMOJI_NAME_RE.match(name):
            invalid.append((f.name, 'bad-name (need 2-32 A-Za-z0-9_)'))
            continue
        try:
            data = f.read_bytes()
        except Exception:
            invalid.append((f.name, 'unreadable'))
            continue
        if len(data) > EMOJI_MAX_BYTES:
            invalid.append((f.name, f'{len(data) // 1024}KB over 256KB'))
            continue
        if data[:8] != b'\x89PNG\r\n\x1a\n':
            invalid.append((f.name, 'not-a-png'))
            continue
        valid.append(f)
    return valid, invalid


def _plan(files: list = None) -> dict:
    """Round-robin: emoji i -> guild i % 8. Only validated files planned."""
    local = _local_set() if files is None else list(files)
    plan: dict = {str(g): [] for g in EMOJI_GUILDS}
    for i, f in enumerate(local):
        plan[str(EMOJI_GUILDS[i % len(EMOJI_GUILDS)])].append(f)
    return plan


def _check_capacity(plan: dict, snapshots: dict) -> dict:
    """Pure capacity preflight. plan: {gid: [wanted stems]} (validated).
    snapshots: {gid: snap | None}; None = unreachable (reported, excluded
    from the verdict — nothing will upload there either way).
    snap: {'name': str, 'limit': int, 'static': [existing names],
           'animated': int}. Animated slots are a separate pool and never
    count against static capacity. Existing same-name emojis are kept,
    never re-uploaded. Returns {'ok': bool, 'guilds': {...}, 'unreachable'}.
    Never mutates its inputs; performs no I/O."""
    guilds, unreachable = {}, []
    for gid in EMOJI_GUILDS:
        key = str(gid)
        snap = snapshots.get(key)
        if not snap:
            unreachable.append(key)
            continue
        try:
            limit = int(snap.get('limit') or 50)
        except Exception:
            limit = 50
        existing = set(snap.get('static') or [])
        wanted = [s for s in (plan.get(key) or [])]
        kept = sorted(n for n in wanted if n in existing)
        new = sorted(n for n in wanted if n not in existing)
        remaining = limit - len(wanted)
        guilds[key] = {
            'name': snap.get('name', key),
            'limit': limit,
            'current': len(existing),
            'animated': int(snap.get('animated') or 0),
            'kept': kept,
            'new': new,
            'remaining': remaining,
            'ok': remaining >= 0,
            'overflow': wanted[limit:] if remaining < 0 else [],
        }
    return {'ok': all(g['ok'] for g in guilds.values()),
            'guilds': guilds, 'unreachable': unreachable}


def _format_preflight(check: dict, invalid: list, ignored: list, total: int) -> list:
    """Human-readable preflight report lines. Pure."""
    n_new = sum(len(g['new']) for g in check['guilds'].values())
    n_kept = sum(len(g['kept']) for g in check['guilds'].values())
    head = (f"Preflight: **{total}** files "
            f"({total - len(invalid)} valid, {len(invalid)} invalid), "
            f"**{n_new}** new uploads, **{n_kept}** kept across "
            f"**{len(check['guilds'])}** servers.")
    lines = [head]
    for gid in EMOJI_GUILDS:
        g = check['guilds'].get(str(gid))
        if not g:
            lines.append(f'• {gid}: unreachable (rechecked on confirm).')
            continue
        status = 'OK' if g['ok'] else f"OVER by {-g['remaining']}: {', '.join(g['overflow'])}"
        lines.append(f"• {g['name']} ({gid}): static {g['current']}/{g['limit']} "
                     f"(+{g['animated']} animated separate), new {len(g['new'])}, "
                     f'kept {len(g["kept"])}, remaining {g["remaining"]} — {status}')
    for gid in check['unreachable']:
        if gid not in check['guilds']:
            lines.append(f'• {gid}: unreachable (rechecked on confirm).')
    if invalid:
        lines.append('Invalid (excluded, would fail at create): '
                     + ', '.join(f'{n} ({r})' for n, r in invalid))
    if ignored:
        lines.append('Ignored (static-only uploader): ' + ', '.join(ignored))
    return lines


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
        valid, invalid = _validate_assets(local)
        ignored = _ignored_files()
        plan = _plan(valid)
        if (action or '').lower() != 'confirm':
            lines = [f'Fleet setup SHARDED: **{len(valid)}** emojis across **{len(EMOJI_GUILDS)}** servers (~{len(valid) // max(1, len(EMOJI_GUILDS))} each). `em()` resolves cross-server.']
            for g in EMOJI_GUILDS:
                guild = self.bot.get_guild(g)
                assigned = [f.stem for f in plan[str(g)]]
                if guild:
                    lines.append(f'• {guild.name}: {len(guild.emojis)} now → {len(assigned)} assigned ({", ".join(assigned[:7])})')
                else:
                    lines.append(t(gid, 'eco.emoji_missing', gid=g))
            snapshots = {}
            for g in EMOJI_GUILDS:
                guild = self.bot.get_guild(g)
                if guild:
                    snapshots[str(g)] = self._snap(guild, list(guild.emojis))
            check = _check_capacity({k: [f.stem for f in v] for k, v in plan.items()},
                                    snapshots)
            lines.append('— preflight (cached counts, rechecked on confirm) —')
            lines.extend(_format_preflight(check, invalid, ignored, len(local)))
            lines.append(t(gid, 'eco.emoji_warn'))
            return await ctx.reply('\n'.join(lines), ephemeral=True)
        self._stop = False
        # Phase 0: resolve guilds (no uploads, deletes, or writes yet).
        work, skip = [], []
        for g in EMOJI_GUILDS:
            guild = self.bot.get_guild(g)
            if not guild:
                try:
                    guild = await self.bot.fetch_guild(g)
                except Exception:
                    guild = None
            if not guild:
                skip.append(t(gid, 'eco.emoji_missing', gid=g))
                continue
            me = guild.get_member(self.bot.user.id)
            if me is None:
                try:
                    me = await guild.fetch_member(self.bot.user.id)
                except Exception:
                    me = None
            if not me or not me.guild_permissions.manage_expressions:
                skip.append(t(gid, 'eco.emoji_noperm', name=guild.name))
                continue
            work.append(guild)
        # Phase 1: fresh snapshots + capacity preflight. Abort = zero mutations.
        snapshots, currents = {}, {}
        for guild in work:
            try:
                current = list(await guild.fetch_emojis())
            except Exception:
                current = list(guild.emojis)
            currents[str(guild.id)] = current
            snapshots[str(guild.id)] = self._snap(guild, current)
        check = _check_capacity({k: [f.stem for f in v] for k, v in plan.items()},
                                snapshots)
        if not check['ok']:
            await ctx.reply('ABORTED: fleet would exceed capacity — '
                            'nothing uploaded, deleted, or saved.\n'
                            + '\n'.join(_format_preflight(check, invalid, ignored, len(local))),
                            ephemeral=True)
            return
        await ctx.reply(t(gid, 'eco.emoji_go'), ephemeral=True)
        mapping, report = {}, list(skip)
        total = len(EMOJI_GUILDS)
        total_e_all = len(valid)
        done_e_all = 0
        status = await ctx.reply(t(gid, 'eco.emoji_progress', done=0, total=total))
        done = 0

        for gi, g in enumerate(EMOJI_GUILDS, start=1):
            if self._stop:
                report.append(f'• STOPPED by user at server {gi}/{total}.')
                break
            guild = next((w for w in work if str(w.id) == str(g)), None)
            if guild is None:
                done = await self._progress(status, gid, report, done, total)
                continue

            current = currents.get(str(g), [])
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

    @staticmethod
    def _snap(guild, current) -> dict:
        """Preflight snapshot from live discord objects. The limit comes
        from the API (emoji_limit), never hardcoded; the animated pool is
        counted separately and never consumes static capacity."""
        try:
            limit = int(getattr(guild, 'emoji_limit', 50) or 50)
        except Exception:
            limit = 50
        static, animated = [], 0
        for e in current or []:
            if getattr(e, 'animated', False):
                animated += 1
            else:
                static.append(getattr(e, 'name', ''))
        return {'name': getattr(guild, 'name', str(getattr(guild, 'id', '?'))),
                'limit': limit, 'static': static, 'animated': animated}

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

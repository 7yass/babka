"""Commit logger: posts new git commits to a logging channel on startup.

One-time job on_ready, idempotent across restarts via a seen-id store.
Follows the bot's conventions: setup(bot), discord.Embed posts, .env config.
"""
import json
import os
import subprocess
from datetime import datetime

import discord
from discord.ext import commands

CHANNEL_ID = int(os.getenv('COMMITLOG_CHANNEL_ID', 1551796397420978298))
REPO_PATH = os.getenv('REPO_PATH') or os.getcwd()
REPO_URL = os.getenv('REPO_URL', '')
SEEN_FILE = os.getenv('COMMITLOG_SEEN_FILE', 'commitlog_seen.json')
MAX_COMMITS = int(os.getenv('COMMITLOG_MAX', 20))
MAX_BODY = 1024


def _git(args):
    return subprocess.run(
        ['git', '-C', REPO_PATH, *args],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()


def _tagged(title: str):
    low = (title or '').lower()
    for k in ('fix', 'repaired', 'bugfix', 'hotfix'):
        if k in low:
            return 'fix', 0x57f085  # green
    for k in ('update', 'upgrade', 'change', 'refactor', 'feat', 'feature', 'chore', 'docs'):
        if k in low:
            return 'update', 0x3498db  # blue
    return 'other', 0x95a5a6  # grey


def _load_seen():
    try:
        with open(SEEN_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(seen):
    try:
        with open(SEEN_FILE, 'w', encoding='utf-8') as f:
            json.dump(sorted(seen)[-MAX_COMMITS * 2:], f)
    except Exception:
        pass


def _parse_commits(limit):
    commits = []
    fmt = '%H%x1f%an%x1f%ad%x1f%s%x1f%b%x1e'
    out = _git(['log', f'-n {limit}', f'--pretty=format:{fmt}', '--date=iso'])
    for rec in out.split('\x1e'):
        if not rec.strip():
            continue
        parts = rec.split('\x1f')
        if len(parts) < 5:
            continue
        cid, author, date, subj, body = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append({'cid': cid, 'author': author, 'date': date, 'subj': subj, 'body': body})
    return commits


async def _run_once(bot):
    try:
        import asyncio
        # _parse_commits shells out to git: blocking. Never run it on the
        # event-loop thread — a slow shared disk stalls heartbeats here.
        commits = await asyncio.get_running_loop().run_in_executor(
            None, _parse_commits, MAX_COMMITS)
        seen = _load_seen()

        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except Exception:
                return

        for c in reversed(commits):  # oldest first
            if c['cid'] in seen:
                continue
            tag, color = _tagged(c['subj'])
            when = ''
            try:
                when = datetime.strptime(c['date'][:19], '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
            except Exception:
                when = c['date']

            emb = discord.Embed(
                title=f"[{tag}] {c['subj'] or '(no subject)'}",
                color=color, timestamp=datetime.utcnow(),
            )
            if REPO_URL:
                emb.url = f"{REPO_URL.rstrip('/')}/commit/{c['cid']}"
            emb.set_author(name=c['author'] or 'unknown')
            emb.set_footer(text=f"commit {c['cid'][:7]}  ·  {when}")
            emb.add_field(name='message', value=(c['body'] or c['subj'] or '-')[:MAX_BODY] or '-', inline=False)

            try:
                await channel.send(embed=emb)
            except Exception:
                return

            seen.add(c['cid'])
            if len(seen) > MAX_COMMITS * 2:
                seen = set(sorted(seen)[-MAX_COMMITS * 2:])

        _save_seen(seen)
    except Exception:
        pass


class CommitLog(commands.Cog):
    """Posts new git commits to a logging channel once, on startup."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.loop.create_task(self._once_after_ready())

    async def _once_after_ready(self):
        try:
            await self.bot.wait_until_ready()
            await _run_once(self.bot)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(CommitLog(bot))
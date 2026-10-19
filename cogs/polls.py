"""Native Discord polls.

The old hand-rolled button board is gone. Discord's own poll object does
the counting now: members vote with real poll buttons, the counts are
real, the poll expires on its own and Discord posts a real poll-result
message when it closes. This cog only starts/stops polls and keeps a thin
index (`native_polls`) so `.poll list` finds them again after a restart.

The legacy `polls`/`pvotes` tables stay readable, so button polls from
before the rewrite still resolve their results.

NOTE: a native poll can never be combined with a Components-V2 panel —
Discord rejects a message that carries both the IS_COMPONENTS_V2 flag
and a poll. The poll therefore goes out as a plain message and every
Babka panel around it stays a separate, ordinary message.
"""
import json
import time
from datetime import timedelta
from pathlib import Path

import discord
from discord.ext import commands, tasks
from discord.poll import Poll, PollLayoutType

import database as db
from lang import STR, t
from utils.checks import staff_or
from utils.common import parse_duration
from utils.ui import ACCENT, divider, panel, stat

LANG_FILE = Path(__file__).resolve().parent.parent / 'lang' / 'discord.json'

# Discord's own limits (Poll Resource): 2-10 answers, 300 chars for the
# question, 55 per answer, duration between 1 and 768 hours.
MIN_HOURS = 1
MAX_HOURS = 768
DEFAULT_HOURS = 24
MAX_OPTIONS = 10
MAX_QUESTION = 300
MAX_OPTION = 55
COOLDOWN = 60

MULTI_WORDS = {'multi', 'many', 'wiele', 'wielokrotne'}

DDL_NATIVE_POLLS = '''
CREATE TABLE IF NOT EXISTS native_polls (
    message_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    question TEXT,
    options TEXT DEFAULT '[]',
    ends_at INTEGER,
    closed INTEGER DEFAULT 0,
    created_at INTEGER,
    creator_id TEXT)'''


def load_extra_lang() -> bool:
    """Hand lang/discord.json to the shared i18n table.

    lang.py only knows lang.json and that file belongs to another
    process, so these strings keep their own file and are merged into
    the same table at import time. Keys that already exist in lang.json
    win — this file never overwrites the main table.
    """
    try:
        with open(LANG_FILE, encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[lang] could not read {LANG_FILE}: {type(e).__name__}: {e}')
        return False
    for code in ('en', 'pl'):
        table = STR.setdefault(code, {})
        chunk = data.get(code)
        if isinstance(chunk, dict):
            for key, text in chunk.items():
                table.setdefault(key, text)
    return True


load_extra_lang()


def ensure_tables() -> None:
    """The index of native polls. Created here, not in database.py."""
    with db.conn_ctx() as conn:
        conn.execute(DDL_NATIVE_POLLS)


# ---------- parsing ----------------------------------------------------

def parse_poll_spec(raw: str) -> dict:
    """`.poll start` payload -> question, options, hours, multiple.

    Format: `question | option | option ... [| 2h] [| multi]`.
    The duration and the multi flag are only read from the tail, and only
    when enough options are left over — "2h" can still be an option.
    """
    parts = [p.strip() for p in (raw or '').split('|')]
    parts = [p for p in parts if p]
    hours = float(DEFAULT_HOURS)
    multiple = False
    notes = []
    while len(parts) >= 4:
        tail = parts[-1].lower()
        if tail in MULTI_WORDS:
            multiple = True
            parts.pop()
            continue
        secs = parse_duration(parts[-1])
        if secs is None:
            break
        hours = secs / 3600
        parts.pop()
    if not parts:
        return {'question': '', 'options': [], 'hours': DEFAULT_HOURS,
                'multiple': multiple, 'notes': notes}
    question = parts[0]
    options = parts[1:]
    if len(question) > MAX_QUESTION:
        question = question[:MAX_QUESTION]
        notes.append('poll.question_trimmed')
    trimmed = []
    for opt in options:
        if len(opt) > MAX_OPTION:
            opt = opt[:MAX_OPTION]
            notes.append('poll.option_trimmed')
        trimmed.append(opt)
    whole = int(round(hours))
    if whole < MIN_HOURS or whole > MAX_HOURS:
        whole = max(MIN_HOURS, min(MAX_HOURS, whole))
        notes.append('poll.duration_clamped')
    return {'question': question, 'options': trimmed, 'hours': whole,
            'multiple': multiple, 'notes': notes}


def build_poll(question: str, options, hours: int, multiple: bool = False) -> Poll:
    """A real Discord poll object. `add_answer` is keyword-only."""
    poll = Poll(question=question, duration=timedelta(hours=hours),
                multiple=multiple, layout_type=PollLayoutType.default)
    for opt in options:
        poll.add_answer(text=opt)
    return poll


# ---------- storage ----------------------------------------------------

def native_rows(gid, channel_id=None, open_only=True, limit=20):
    sql = 'SELECT * FROM native_polls WHERE guild_id=?'
    args = [str(gid)]
    if channel_id is not None:
        sql += ' AND channel_id=?'
        args.append(str(channel_id))
    if open_only:
        sql += ' AND closed=0'
    sql += ' ORDER BY created_at DESC LIMIT ?'
    args.append(int(limit))
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def native_row(gid, message_id):
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM native_polls WHERE message_id=? AND guild_id=?',
                           (str(message_id), str(gid))).fetchone()
    return dict(row) if row else None


def legacy_row(gid, message_id):
    """The old button poll — still readable, so history resolves."""
    try:
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM polls WHERE message_id=? AND guild_id=?',
                               (str(message_id), str(gid))).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def legacy_counts(message_id: str, n_opts: int):
    try:
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT idx, COUNT(*) c FROM pvotes WHERE message_id=? GROUP BY idx',
                                (message_id,)).fetchall()
    except Exception:
        rows = []
    counts = [0] * n_opts
    for r in rows:
        if 0 <= r['idx'] < n_opts:
            counts[r['idx']] = r['c']
    return counts


# ---------- rendering ---------------------------------------------------

def results_body(gid, question, pairs, total, live=True) -> str:
    """`pairs` = [(option, votes)] -> the board of a poll."""
    lines = []
    for i, (opt, n) in enumerate(pairs, start=1):
        pct = round(n / total * 100) if total else 0
        lines.append(t(gid, 'poll.result_line', i=i, opt=opt, n=n, pct=pct))
    body = '\n'.join(lines)
    body += '\n' + t(gid, 'poll.result_total', n=total)
    if live:
        body += '\n-# ' + t(gid, 'poll.result_live')
    return body


class Polls(commands.Cog):
    """Native polls: start, close, list, results."""

    def __init__(self, bot):
        self.bot = bot
        try:
            ensure_tables()
        except Exception as e:
            print(f'[polls] table init failed: {type(e).__name__}: {e}')
        try:
            self.sweeper.start()
        except Exception:
            pass

    def cog_unload(self):
        try:
            self.sweeper.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=1)
    async def sweeper(self):
        """Discord closes the poll by itself — the index just follows."""
        await self.bot.wait_until_ready()
        now = int(time.time())
        try:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE native_polls SET closed=1 WHERE closed=0 '
                             'AND ends_at IS NOT NULL AND ends_at<=?', (now,))
        except Exception:
            pass

    @sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- commands ----------

    @commands.group(name='poll', description='Ankiety (natywne)')
    async def poll(self, ctx):
        await ctx.reply(t(ctx.guild.id, 'poll.help'), ephemeral=True)

    @poll.command(name='start', description='Nowa ankieta (natywna)')
    async def poll_start(self, ctx, *, raw: str = ''):
        gid = ctx.guild.id
        try:
            spec = parse_poll_spec(raw)
            question = spec['question']
            options = spec['options']
            if not question:
                return await ctx.reply(t(gid, 'poll.usage'), ephemeral=True)
            if len(options) < 2:
                return await ctx.reply(t(gid, 'poll.too_few'), ephemeral=True)
            if len(options) > MAX_OPTIONS:
                return await ctx.reply(t(gid, 'poll.too_many', n=MAX_OPTIONS), ephemeral=True)
            with db.conn_ctx() as conn:
                last = conn.execute(
                    'SELECT created_at FROM native_polls WHERE guild_id=? AND creator_id=? '
                    'ORDER BY created_at DESC LIMIT 1',
                    (str(gid), str(ctx.author.id))).fetchone()
            if last and int(time.time()) - int(last['created_at'] or 0) < COOLDOWN:
                return await ctx.reply(t(gid, 'poll.cooldown'), ephemeral=True)
            poll = build_poll(question, options, spec['hours'], spec['multiple'])
            try:
                msg = await ctx.send(poll=poll)
            except discord.HTTPException as e:
                return await ctx.reply(t(gid, 'poll.http_fail', code=e.status,
                                         text=(e.text or '')[:120]), ephemeral=True)
            except Exception:
                return await ctx.reply(t(gid, 'poll.failed'), ephemeral=True)
            ends_at = int(time.time()) + spec['hours'] * 3600
            with db.conn_ctx() as conn:
                conn.execute(
                    'INSERT OR REPLACE INTO native_polls '
                    '(message_id, guild_id, channel_id, question, options, ends_at, closed, '
                    'created_at, creator_id) VALUES (?,?,?,?,?,?,0,?,?)',
                    (str(msg.id), str(gid), str(ctx.channel.id), question,
                     json.dumps(options, ensure_ascii=False), ends_at,
                     int(time.time()), str(ctx.author.id)))
            body = t(gid, 'poll.started', link=msg.jump_url, h=spec['hours'])
            if spec['multiple']:
                body += '\n' + t(gid, 'poll.multi_on')
            for note in dict.fromkeys(spec['notes']):
                body += '\n-# ' + t(gid, note, h=spec['hours'])
            await ctx.reply(body, ephemeral=True)
        except Exception:
            await ctx.reply(t(gid, 'poll.failed'), ephemeral=True)

    @poll.command(name='end', description='Zamknij ankietę')
    @staff_or('manage_guild')
    async def poll_end(self, ctx, message_id: str = ''):
        await self._end_poll(ctx, message_id)

    @poll.command(name='close', description='Zamknij ankietę')
    @staff_or('manage_guild')
    async def poll_close(self, ctx, message_id: str = ''):
        await self._end_poll(ctx, message_id)

    async def _end_poll(self, ctx, message_id: str = ''):
        """End a native poll. Discord finalises it and posts the result."""
        gid = ctx.guild.id
        mid = (message_id or '').strip()
        row = None
        if mid.isdigit():
            row = native_row(gid, mid)
        else:
            rows = native_rows(gid, ctx.channel.id, open_only=True, limit=1)
            row = rows[0] if rows else None
        if not row:
            return await ctx.reply(t(gid, 'poll.end_none'), ephemeral=True)
        channel = ctx.guild.get_channel(int(row['channel_id']))
        if channel is None:
            return await ctx.reply(t(gid, 'poll.end_none'), ephemeral=True)
        try:
            msg = await channel.fetch_message(int(row['message_id']))
        except Exception:
            return await ctx.reply(t(gid, 'poll.end_none'), ephemeral=True)
        try:
            await msg.end_poll()
        except discord.HTTPException as e:
            return await ctx.reply(t(gid, 'poll.http_fail', code=e.status,
                                     text=(e.text or '')[:120]), ephemeral=True)
        except Exception:
            return await ctx.reply(t(gid, 'poll.end_failed'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE native_polls SET closed=1 WHERE message_id=?',
                         (row['message_id'],))
        await ctx.reply(t(gid, 'poll.ended', link=msg.jump_url), ephemeral=True)

    @poll.command(name='list', description='Aktywne ankiety')
    async def poll_list(self, ctx):
        gid = ctx.guild.id
        rows = native_rows(gid, ctx.channel.id, open_only=True, limit=10)
        if not rows:
            return await ctx.reply(t(gid, 'poll.list_empty'), ephemeral=True)
        now = int(time.time())
        lines = []
        total_votes = 0
        for row in rows:
            votes = 0
            left = '—'
            try:
                channel = ctx.guild.get_channel(int(row['channel_id']))
                msg = await channel.fetch_message(int(row['message_id']))
                if msg.poll is not None:
                    votes = msg.poll.total_votes
            except Exception:
                pass
            total_votes += votes
            ends = row.get('ends_at')
            if ends:
                mins = max(0, int((int(ends) - now) // 60))
                left = f'{mins}m'
            lines.append(t(gid, 'poll.list_item', q=row.get('question') or '—',
                           votes=votes, left=left,
                           link=f'https://discord.com/channels/{gid}/{row["channel_id"]}/'
                                f'{row["message_id"]}'))
        body = (stat(t(gid, 'poll.list_count'), len(rows)) + '\n' +
                stat(t(gid, 'poll.vote_count'), total_votes) + divider() + '\n'.join(lines))
        await ctx.reply(view=panel(t(gid, 'poll.list_title'), body, accent=ACCENT['default']),
                        mention_author=False)

    @poll.command(name='results', description='Wyniki ankiety')
    async def poll_results(self, ctx, message_id: str = ''):
        gid = ctx.guild.id
        mid = (message_id or '').strip()
        if not mid.isdigit():
            return await ctx.reply(t(gid, 'poll.results_none'), ephemeral=True)
        row = native_row(gid, mid)
        if row:
            channel = ctx.guild.get_channel(int(row['channel_id']))
            try:
                msg = await channel.fetch_message(int(row['message_id']))
            except Exception:
                msg = None
            if msg is not None and msg.poll is not None:
                poll = msg.poll
                pairs = [(a.text, a.vote_count) for a in poll.answers]
                body = results_body(gid, row.get('question') or poll.question,
                                    pairs, poll.total_votes, live=True)
                await ctx.reply(view=panel(t(gid, 'poll.results_title'), body,
                                            accent=ACCENT['default']), mention_author=False)
                return
        legacy = legacy_row(gid, mid)
        if legacy:
            try:
                options = json.loads(legacy.get('options') or '[]')
            except Exception:
                options = []
            counts = legacy_counts(mid, len(options))
            pairs = list(zip(options, counts))
            body = results_body(gid, legacy.get('question') or '—', pairs, sum(counts),
                                live=False)
            body += '\n-# ' + t(gid, 'poll.result_legacy')
            await ctx.reply(view=panel(t(gid, 'poll.results_title'), body,
                                        accent=ACCENT['default']), mention_author=False)
            return
        await ctx.reply(t(gid, 'poll.results_none'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Polls(bot))

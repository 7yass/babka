"""Button polls: single-vote switching, live counts, auto-close. Hybrid."""
import asyncio
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import WHITE, ok
from utils.checks import staff_or
from utils.common import parse_duration


def board_text(gid, question, options, counts) -> str:
    total = sum(counts)
    lines = []
    for i, opt in enumerate(options):
        n = counts[i] if i < len(counts) else 0
        pct = round(n / total * 100) if total else 0
        bar = '█' * round(pct / 10) + '░' * (10 - round(pct / 10))
        lines.append(f'`{i + 1}.` {opt}\n{bar} **{n}** ({pct}%)')
    return t(gid, 'poll.board', q=question, lines='\n'.join(lines), votes=t(gid, 'poll.votes', n=total))


async def counts_for(message_id: str, n_opts: int):
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT idx, COUNT(*) c FROM pvotes WHERE message_id=? GROUP BY idx',
                            (message_id,)).fetchall()
    counts = [0] * n_opts
    for r in rows:
        if 0 <= r['idx'] < n_opts:
            counts[r['idx']] = r['c']
    return counts


def poll_view(message_id: str, options) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i, opt in enumerate(options[:5]):
        view.add_item(discord.ui.Button(label=f'{i + 1}. {opt[:70]}', style=discord.ButtonStyle.grey,
                                        custom_id=f'poll:{message_id}:{i}'))
    return view


async def close_poll(bot, p: dict):
    import json
    guild = bot.get_guild(int(p['guild_id']))
    ch = guild.get_channel(int(p['channel_id'])) if guild else None
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(p['message_id']))
    except Exception:
        return
    options = json.loads(p['options'] or '[]')
    counts = await counts_for(p['message_id'], len(options))
    gid = p['guild_id']
    try:
        await msg.edit(content=board_text(gid, p['question'], options, counts) + '\n\n' + t(gid, 'poll.closed'),
                       view=None)
    except Exception:
        pass
    with db.conn_ctx() as conn:
        conn.execute('UPDATE polls SET closed=1 WHERE message_id=?', (p['message_id'],))


class Polls(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sweeper.start()

    def cog_unload(self):
        self.sweeper.cancel()

    @tasks.loop(seconds=30)
    async def sweeper(self):
        await self.bot.wait_until_ready()
        now = int(time.time())
        with db.conn_ctx() as conn:
            due = [dict(r) for r in conn.execute(
                'SELECT * FROM polls WHERE closed=0 AND ends_at IS NOT NULL AND ends_at<=?', (now,)).fetchall()]
            for g in due:
                conn.execute('UPDATE polls SET closed=1 WHERE message_id=?', (g['message_id'],))
        for g in due:
            await close_poll(self.bot, g)

    @sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('poll:'):
            return
        _, mid, idx = cid.split(':')
        gid = interaction.guild_id
        import json
        with db.conn_ctx() as conn:
            p = conn.execute('SELECT * FROM polls WHERE message_id=? AND closed=0', (mid,)).fetchone()
            if not p:
                return await interaction.response.send_message(t(gid, 'poll.nope'), ephemeral=True)
            conn.execute('INSERT OR REPLACE INTO pvotes (message_id, user_id, idx) VALUES (?,?,?)',
                         (mid, str(interaction.user.id), int(idx)))
            options = json.loads(p['options'] or '[]')
        counts = await counts_for(mid, len(options))
        await interaction.response.edit_message(
            content=board_text(gid, p['question'], options, counts),
            view=poll_view(mid, options))

    @commands.hybrid_group(name='poll', description='Ankiety')
    async def poll(self, ctx):
        await ctx.reply('/poll create / close / results', ephemeral=True)

    @poll.command(name='create', description='Nowa ankieta (max 5)')
    @staff_or('manage_guild')
    async def create(self, ctx, channel: discord.TextChannel, question: str, option1: str, option2: str,
                     option3: str = None, option4: str = None, option5: str = None, duration: str = None):
        gid = ctx.guild.id
        options = [o for o in (option1, option2, option3, option4, option5) if o]
        ends = None
        if duration:
            secs = parse_duration(duration)
            if secs and secs >= 60:
                ends = int(time.time()) + secs
        try:
            msg = await channel.send(board_text(gid, question, options, [0] * len(options)),
                                     view=poll_view('pending', options))
        except Exception:
            return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO polls (message_id, guild_id, channel_id, question, options, ends_at) VALUES (?,?,?,?,?,?)',
                         (str(msg.id), str(gid), str(channel.id), question, __import__('json').dumps(options), ends))
        try:
            await msg.edit(view=poll_view(str(msg.id), options))
        except Exception:
            pass
        await ctx.reply(t(gid, 'poll.live', ch=channel.mention), ephemeral=True)

    @poll.command(name='close', description='Zamknij + wyniki')
    @staff_or('manage_guild')
    async def close(self, ctx, message_id: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            p = conn.execute('SELECT * FROM polls WHERE message_id=? AND guild_id=? AND closed=0',
                             (message_id, str(gid))).fetchone()
            if not p:
                return await ctx.reply(t(gid, 'poll.nope'), ephemeral=True)
            conn.execute('UPDATE polls SET closed=1 WHERE message_id=?', (message_id,))
        await close_poll(self.bot, dict(p))
        await ctx.reply(t(gid, 'poll.closed'), ephemeral=True)

    @poll.command(name='results', description='Pokaż wyniki')
    async def results(self, ctx, message_id: str):
        import json
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            p = conn.execute('SELECT * FROM polls WHERE message_id=? AND guild_id=?',
                             (message_id, str(gid))).fetchone()
        if not p:
            return await ctx.reply(t(gid, 'poll.nope'), ephemeral=True)
        options = json.loads(p['options'] or '[]')
        counts = await counts_for(message_id, len(options))
        await ctx.reply(embed=ok(t(gid, 'poll.results') + '\n' + board_text(gid, p['question'], options, counts)),
                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Polls(bot))

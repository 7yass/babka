"""Giveaways: timed draws, join button, reroll, auto-end. Hybrid."""
import asyncio
import random
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import WHITE, ok, foot
from utils.checks import staff_or
from utils.common import parse_duration


def card_embed(g: dict, n: int) -> discord.Embed:
    e = discord.Embed(
        description=t(g['guild_id'], 'gw.card', prize=g['prize'], ts=f"<t:{g['ends_at']}:R>",
                      winners=g['winners'], host=f"<@{g['host_id']}>", n=n),
        color=WHITE)
    e.set_footer(text=foot())
    return e


def join_view(gid, ended=False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    b = discord.ui.Button(label=t(gid, 'gw.join_btn'), style=discord.ButtonStyle.grey,
                          custom_id='gw_join:pending', disabled=ended)
    view.add_item(b)
    return view


async def entry_count(message_id: str) -> int:
    with db.conn_ctx() as conn:
        return conn.execute('SELECT COUNT(*) c FROM gentries WHERE message_id=?', (message_id,)).fetchone()['c']


async def draw_winners(bot, g: dict):
    with db.conn_ctx() as conn:
        entries = [r['user_id'] for r in conn.execute(
            'SELECT user_id FROM gentries WHERE message_id=?', (g['message_id'],)).fetchall()]
    guild = bot.get_guild(int(g['guild_id']))
    valid = []
    for uid in entries:
        m = guild.get_member(int(uid)) if guild else None
        if m and not m.bot:
            valid.append(m)
    random.shuffle(valid)
    return valid[:max(1, g['winners'])]


async def close_giveaway(bot, g: dict, reroll=False):
    guild = bot.get_guild(int(g['guild_id']))
    ch = guild.get_channel(int(g['channel_id'])) if guild else None
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(g['message_id']))
    except Exception:
        msg = None
    winners = await draw_winners(bot, g)
    gid = g['guild_id']
    if winners:
        text = t(gid, 'gw.ended', prize=g['prize'],
                 winners=' '.join(m.mention for m in winners), host=f"<@{g['host_id']}>")
    else:
        text = t(gid, 'gw.no_entries')
    if msg:
        try:
            await msg.edit(content=None, embed=ok(text), view=None)
        except Exception:
            try:
                await ch.send(embed=ok(text))
            except Exception:
                pass
    else:
        try:
            await ch.send(embed=ok(text))
        except Exception:
            pass
    with db.conn_ctx() as conn:
        conn.execute('UPDATE giveaways SET closed=1 WHERE message_id=?', (g['message_id'],))


class Giveaway(commands.Cog):
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
                'SELECT * FROM giveaways WHERE closed=0 AND ends_at<=?', (now,)).fetchall()]
        for g in due:
            with db.conn_ctx() as conn:
                conn.execute('UPDATE giveaways SET closed=1 WHERE message_id=? AND closed=0',
                             (g['message_id'],))
            g['closed'] = 1
            await close_giveaway(self.bot, g)

    @sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('gw_join:'):
            return
        gid = interaction.guild_id
        mid = cid.split(':')[1]
        with db.conn_ctx() as conn:
            g = conn.execute('SELECT * FROM giveaways WHERE message_id=? AND closed=0', (mid,)).fetchone()
            if not g:
                return await interaction.response.send_message(t(gid, 'gw.nope'), ephemeral=True)
            has = conn.execute('SELECT 1 FROM gentries WHERE message_id=? AND user_id=?',
                               (mid, str(interaction.user.id))).fetchone()
            if has:
                conn.execute('DELETE FROM gentries WHERE message_id=? AND user_id=?',
                             (mid, str(interaction.user.id)))
                msg = t(gid, 'gw.left')
            else:
                conn.execute('INSERT OR IGNORE INTO gentries (message_id, user_id) VALUES (?,?)',
                             (mid, str(interaction.user.id)))
                msg = t(gid, 'gw.joined')
        await interaction.response.send_message(msg, ephemeral=True)
        # refresh entry count on the card
        try:
            n = await entry_count(mid)
            gd = dict(g)
            await interaction.message.edit(embed=card_embed(gd, n))
        except Exception:
            pass

    @commands.hybrid_group(name='giveaway', description='Rozdania')
    async def giveaway(self, ctx):
        await ctx.reply('/giveaway start / end / reroll / cancel / list', ephemeral=True)

    @giveaway.command(name='start', description='Nowe rozdanie')
    @staff_or('manage_guild')
    async def start(self, ctx, duration: str, winners: int, channel: discord.TextChannel = None, *, prize: str):
        gid = ctx.guild.id
        secs = parse_duration(duration)
        if not secs or secs < 30:
            return await ctx.reply(t(gid, 'mod.temp_use'), ephemeral=True)
        channel = channel or ctx.channel
        ends = int(time.time()) + secs
        view = join_view(gid)
        g = {'guild_id': str(gid), 'prize': prize, 'ends_at': ends,
             'winners': max(1, winners), 'host_id': str(ctx.author.id)}
        try:
            msg = await channel.send(embed=card_embed(g, 0), view=view)
        except Exception:
            return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO giveaways (message_id, guild_id, channel_id, prize, winners, ends_at, host_id) VALUES (?,?,?,?,?,?,?)',
                         (str(msg.id), str(gid), str(channel.id), prize, max(1, winners), ends, str(ctx.author.id)))
        # fix button custom id to the real message
        try:
            v2 = join_view(gid)
            v2.children[0].custom_id = f'gw_join:{msg.id}'
            await msg.edit(view=v2)
        except Exception:
            pass
        await ctx.reply(t(gid, 'gw.live', ch=channel.mention), ephemeral=True)

    async def _get(self, ctx, message_id: str):
        with db.conn_ctx() as conn:
            return conn.execute('SELECT * FROM giveaways WHERE message_id=? AND guild_id=? AND closed=0',
                                (message_id, str(ctx.guild.id))).fetchone()

    @giveaway.command(name='end', description='ZakoÅ„cz i losuj')
    @staff_or('manage_guild')
    async def end(self, ctx, message_id: str):
        g = await self._get(ctx, message_id)
        if not g:
            return await ctx.reply(t(ctx.guild.id, 'gw.nope'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE giveaways SET closed=1 WHERE message_id=?', (message_id,))
        await close_giveaway(self.bot, dict(g))
        await ctx.reply(embed=ok(t(ctx.guild.id, 'gw.ended', prize=g['prize'], winners='…', host=f"<@{g['host_id']}>")),
                        ephemeral=True)

    @giveaway.command(name='reroll', description='Losuj od nowa')
    @staff_or('manage_guild')
    async def reroll(self, ctx, message_id: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            g = conn.execute('SELECT * FROM giveaways WHERE message_id=? AND guild_id=?',
                             (message_id, str(gid))).fetchone()
        if not g:
            return await ctx.reply(t(gid, 'gw.nope'), ephemeral=True)
        winners = await draw_winners(self.bot, dict(g))
        if not winners:
            return await ctx.reply(t(gid, 'gw.no_entries'), ephemeral=True)
        try:
            ch = ctx.guild.get_channel(int(g['channel_id']))
            await ch.send(embed=ok(t(gid, 'gw.rerolled') + ' ' + t(gid, 'gw.ended', prize=g['prize'],
                          winners=' '.join(m.mention for m in winners), host=f"<@{g['host_id']}>")))
        except Exception:
            pass
        await ctx.reply(t(gid, 'gw.rerolled'), ephemeral=True)

    @giveaway.command(name='cancel', description='Anuluj bez losowania')
    @staff_or('manage_guild')
    async def cancel(self, ctx, message_id: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            g = conn.execute('SELECT * FROM giveaways WHERE message_id=? AND guild_id=? AND closed=0',
                             (message_id, str(gid))).fetchone()
            if not g:
                return await ctx.reply(t(gid, 'gw.nope'), ephemeral=True)
            conn.execute('UPDATE giveaways SET closed=1 WHERE message_id=?', (message_id,))
            conn.execute('DELETE FROM gentries WHERE message_id=?', (message_id,))
        try:
            ch = ctx.guild.get_channel(int(g['channel_id']))
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(content=None, embed=ok(t(gid, 'gw.cancelled')), view=None)
        except Exception:
            pass
        await ctx.reply(t(gid, 'gw.cancelled'), ephemeral=True)

    @giveaway.command(name='list', description='Aktywne rozdania')
    async def list_g(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM giveaways WHERE guild_id=? AND closed=0', (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'gw.list_empty'), ephemeral=True)
        await ctx.reply(embed=ok(t(gid, 'gw.list_title') + '\n' + '\n'.join(
            f"• **{r['prize']}** — <#{r['channel_id']}> <t:{r['ends_at']}:R> (`{r['message_id']}`)" for r in rows)),
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaway(bot))

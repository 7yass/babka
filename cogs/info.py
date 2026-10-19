"""Info panels: FAQ buttons with private self-deleting answers. Hybrid.

The panel view is built and registered by services.persistent, so the
buttons keep working after a restart (the answer is read from the DB on
every click, never from a closure).
"""
import asyncio

import discord
from discord.ext import commands

import database as db
from lang import t
from services import persistent
from utils.embeds import build, err, foot, WHITE
from utils.checks import staff_or

STYLES = persistent.STYLES


async def refresh_panel(bot, panel: dict) -> bool:
    with db.conn_ctx() as conn:
        buttons = [dict(r) for r in conn.execute(
            'SELECT * FROM info_buttons WHERE panel_id=? ORDER BY id ASC', (panel['id'],)).fetchall()]
    guild = bot.get_guild(int(panel['guild_id']))
    ch = guild and guild.get_channel(int(panel['channel_id']))
    if not ch:
        return False
    # One button per info_buttons row; the handler re-reads its row on click.
    view = persistent.build_info_view(panel['id'])
    embed = discord.Embed(title=panel.get('title') or 'Info', description=panel.get('description') or '', color=WHITE)
    embed.set_footer(text=foot())
    if panel.get('message_id'):
        try:
            msg = await ch.fetch_message(int(panel['message_id']))
            await msg.edit(embed=embed, view=view if buttons else None)
            if buttons:
                persistent.track(view, bot)
            return True
        except Exception:
            pass
    try:
        msg = await ch.send(embed=embed, view=view if buttons else None)
    except Exception:
        return False
    with db.conn_ctx() as conn:
        conn.execute('UPDATE info_panels SET message_id=? WHERE id=?', (str(msg.id), panel['id']))
    if buttons:
        persistent.track(view, bot)
    return True


class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # Registered panel views answer their own clicks (services/persistent
        # rebuilds them from the DB on every boot). This is only the safety
        # net for panels the registry does not know about.
        await persistent.handle_fallback(interaction)

    @commands.group(name='info', description='Panele FAQ')
    async def info(self, ctx):
        await ctx.reply('.info setup / add / remove / list / delete', ephemeral=True)

    @info.command(name='setup', description='Postaw panel')
    @staff_or('manage_guild')
    async def setup_p(self, ctx, title: str, channel: discord.TextChannel = None, *, description: str = ''):
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            cur = conn.execute('INSERT INTO info_panels (guild_id, channel_id, title, description) VALUES (?,?,?,?)',
                               (str(ctx.guild.id), str(channel.id), title, description))
            pid = cur.lastrowid
        panel = {'id': pid, 'guild_id': str(ctx.guild.id), 'channel_id': str(channel.id),
                 'message_id': None, 'title': title, 'description': description}
        if not await refresh_panel(self.bot, panel):
            return await ctx.reply(t(ctx.guild.id, 'pv.info.post_fail'), ephemeral=True)
        await ctx.reply(t(ctx.guild.id, 'pv.info.live', id=pid, ch=channel.mention), ephemeral=True)

    @info.command(name='add', description='Dodaj guzik')
    @staff_or('manage_guild')
    async def add(self, ctx, panel: int = None, label: str = None, color: str = None, *, text: str = ''):
        gid = ctx.guild.id
        if panel is not None and label and text:
            return await self._do_add(ctx, panel, label, text, color or 'grey')
        if ctx.interaction is None:
            return await ctx.reply(t(gid, 'pv.wiz.args_needed',
                                     use='.info add <panel> <label> [color] <answer...>'), ephemeral=True)
        await ctx.interaction.response.send_modal(_AddModal(self, gid))

    async def _do_add(self, ctx_or_ix, panel: int, label: str, text: str, color: str, followup=None):
        guild = ctx_or_ix.guild
        gid = guild.id

        async def say(content: str):
            if followup is not None:
                try:
                    await followup.send(content, ephemeral=True)
                except Exception:
                    pass
            elif isinstance(ctx_or_ix, discord.Interaction):
                try:
                    await ctx_or_ix.response.send_message(content, ephemeral=True, delete_after=15)
                except Exception:
                    pass
            else:
                try:
                    await ctx_or_ix.reply(content, ephemeral=True)
                except Exception:
                    pass

        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM info_panels WHERE id=? AND guild_id=?', (panel, str(gid))).fetchone()
            if not prow:
                return await say(t(gid, 'pv.info.no_panel'))
            count = conn.execute('SELECT COUNT(*) c FROM info_buttons WHERE panel_id=?', (panel,)).fetchone()['c']
            if count >= 25:
                return await say(t(gid, 'pv.info.full'))
            conn.execute('INSERT INTO info_buttons (panel_id, label, text, style) VALUES (?,?,?,?)',
                         (panel, label, text, color if color in STYLES else 'grey'))
            prow = dict(conn.execute('SELECT * FROM info_panels WHERE id=?', (panel,)).fetchone())
        await refresh_panel(self.bot, prow)
        await say(t(gid, 'pv.info.added', id=panel))

    @info.command(name='remove', description='Zdejmij guzik')
    @staff_or('manage_guild')
    async def remove(self, ctx, panel: int, *, label: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM info_panels WHERE id=? AND guild_id=?', (panel, str(gid))).fetchone()
            if not prow:
                return await ctx.reply(t(gid, 'pv.info.no_panel'), ephemeral=True)
            conn.execute('DELETE FROM info_buttons WHERE panel_id=? AND label=?', (panel, label))
            prow = dict(conn.execute('SELECT * FROM info_panels WHERE id=?', (panel,)).fetchone())
        await refresh_panel(self.bot, prow)
        await ctx.reply(t(gid, 'pv.info.removed', label=label, id=panel), ephemeral=True)

    @info.command(name='list', description='Lista paneli')
    async def list_p(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            panels = conn.execute('SELECT * FROM info_panels WHERE guild_id=?', (str(gid),)).fetchall()
            if not panels:
                return await ctx.reply(t(gid, 'pv.info.empty'), ephemeral=True)
            lines = []
            for p in panels:
                btns = conn.execute('SELECT label FROM info_buttons WHERE panel_id=?', (p['id'],)).fetchall()
                lines.append(f"**#{p['id']}** {p['title']} → <#{p['channel_id']}>\n" +
                             (' '.join(f"`{b['label']}`" for b in btns) or '(no buttons yet)'))
        await ctx.reply('\n\n'.join(lines), ephemeral=True)

    @info.command(name='delete', description='UsuÅ„ panel')
    @staff_or('manage_guild')
    async def delete(self, ctx, panel: int):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            prow = conn.execute('SELECT * FROM info_panels WHERE id=? AND guild_id=?', (panel, str(gid))).fetchone()
            if prow and prow['message_id']:
                try:
                    ch = ctx.guild.get_channel(int(prow['channel_id']))
                    msg = await ch.fetch_message(int(prow['message_id']))
                    await msg.delete()
                except Exception:
                    pass
            conn.execute('DELETE FROM info_buttons WHERE panel_id=?', (panel,))
            conn.execute('DELETE FROM info_panels WHERE id=? AND guild_id=?', (panel, str(gid)))
        await ctx.reply(t(gid, 'pv.info.deleted', id=panel), ephemeral=True)


class _AddModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'pv.info.add_title'))
        self.panel_in = discord.ui.TextInput(label=t(gid, 'pv.info.add_panel'), max_length=10)
        self.label_in = discord.ui.TextInput(label=t(gid, 'pv.info.add_label'), max_length=80)
        self.color_in = discord.ui.TextInput(label=t(gid, 'pv.sr.q_color'), required=False, max_length=10,
                                             placeholder='grey')
        self.text_in = discord.ui.TextInput(label=t(gid, 'pv.info.add_text'),
                                            style=discord.TextStyle.paragraph, max_length=1500)
        for i in (self.panel_in, self.label_in, self.color_in, self.text_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        try:
            panel = int(self.panel_in.value.strip())
        except Exception:
            return await interaction.response.send_message(t(gid, 'pv.info.no_panel'), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        cog = interaction.client.get_cog('Info')
        await cog._do_add(interaction, panel, self.label_in.value.strip(), self.text_in.value,
                          (self.color_in.value or 'grey').strip(), followup=interaction.followup)
        await asyncio.sleep(15)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(Info(bot))

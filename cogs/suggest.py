"""Suggestions: panel with server/bot picker -> form -> private inbox."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.checks import staff_or


class SuggestModal(discord.ui.Modal):
    def __init__(self, gid, category: str):
        super().__init__(title=t(gid, 'sg.form_title', cat=category), custom_id=f'sg_modal:{category}')
        self.what = discord.ui.TextInput(label=t(gid, 'sg.f_title'), max_length=100)
        self.detail = discord.ui.TextInput(label=t(gid, 'sg.f_detail'), style=discord.TextStyle.paragraph,
                                           max_length=1000)
        self.add_item(self.what)
        self.add_item(self.detail)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        category = (interaction.data.get('custom_id', '').split(':') + ['?'])[1]
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT inbox_channel FROM suggest_cfg WHERE guild_id=?',
                               (str(gid),)).fetchone()
        if not cfg or not cfg['inbox_channel']:
            return await interaction.response.send_message(t(gid, 'sg.no_inbox'), ephemeral=True)
        dest = interaction.guild.get_channel(int(cfg['inbox_channel']))
        if not dest:
            return await interaction.response.send_message(t(gid, 'sg.no_inbox'), ephemeral=True)
        emb = discord.Embed(title=self.what.value[:100],
                            description=self.detail.value[:1000], color=0xFFFFFF)
        emb.set_author(name=f'{interaction.user.display_name} ({interaction.user.id})',
                       icon_url=interaction.user.display_avatar.url)
        emb.add_field(name=t(gid, 'sg.category'), value=category, inline=True)
        emb.set_footer(text='babka :3')
        try:
            await dest.send(embed=emb)
        except Exception:
            return await interaction.response.send_message(t(gid, 'info.post_fail'), ephemeral=True)
        await interaction.response.send_message(t(gid, 'sg.sent'), ephemeral=True)


class Suggest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='suggest', description='Propozycje')
    async def suggest(self, ctx):
        await ctx.reply('.suggest setup / off', ephemeral=True)

    @suggest.command(name='setup', description='Panel + tajny kanał')
    @staff_or('manage_guild')
    async def setup(self, ctx, panel: discord.TextChannel, inbox: discord.TextChannel):
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow
        gid = ctx.guild.id
        await ctx.defer(ephemeral=True)
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT panel_message, panel_channel FROM suggest_cfg WHERE guild_id=?',
                               (str(gid),)).fetchone()
            if old and old['panel_message'] and old['panel_channel']:
                try:
                    ch = ctx.guild.get_channel(int(old['panel_channel']))
                    await (await ch.fetch_message(int(old['panel_message']))).delete()
                except Exception:
                    pass
        layout = LayoutView(timeout=None)
        box = Container(accent_color=0xFFFFFF)
        box.add_item(TextDisplay(f"## {t(gid, 'sg.title')}\n{t(gid, 'sg.desc')}"))
        row = ActionRow()
        row.add_item(discord.ui.Select(
            custom_id='sg_pick', placeholder=t(gid, 'sg.pick'), min_values=1, max_values=1,
            options=[discord.SelectOption(label=t(gid, 'sg.server'), value='server'),
                     discord.SelectOption(label=t(gid, 'sg.bot'), value='bot')]))
        box.add_item(row)
        layout.add_item(box)
        try:
            msg = await panel.send(view=layout)
        except Exception:
            return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO suggest_cfg (guild_id, panel_channel, panel_message, inbox_channel)'
                         ' VALUES (?,?,?,?)', (str(gid), str(panel.id), str(msg.id), str(inbox.id)))
        await ctx.reply(t(gid, 'sg.live', panel=panel.mention, inbox=inbox.mention), ephemeral=True)

    @suggest.command(name='off', description='Wyłącz propozycje')
    @staff_or('manage_guild')
    async def off(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM suggest_cfg WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'sg.off'), ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.data.get('custom_id', '') != 'sg_pick':
            return
        vals = interaction.data.get('values') or []
        if not vals:
            return
        cat = t(interaction.guild_id, 'sg.server') if vals[0] == 'server' else t(interaction.guild_id, 'sg.bot')
        await interaction.response.send_modal(SuggestModal(interaction.guild_id, cat))


async def setup(bot):
    await bot.add_cog(Suggest(bot))

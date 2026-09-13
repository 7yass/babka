"""Self-assign role buttons. Hybrid + restart-proof listener."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import WHITE, foot
from utils.checks import staff_or
from utils.wizard import parse_link, parse_role, color_style


class QuickButtonModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'sr.quick_title'))
        self.msg_in = discord.ui.TextInput(label=t(gid, 'sr.q_msg'), placeholder='https://discord.com/channels/.../...',
                                           max_length=120)
        self.label_in = discord.ui.TextInput(label=t(gid, 'sr.q_label'), required=False, max_length=80)
        self.role_in = discord.ui.TextInput(label=t(gid, 'sr.q_role'), placeholder='@role / ID / name',
                                            max_length=100)
        self.color_in = discord.ui.TextInput(label=t(gid, 'sr.q_color'), required=False, max_length=10,
                                             placeholder='grey')
        for i in (self.msg_in, self.label_in, self.role_in, self.color_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        ch_id, msg_id = parse_link(self.msg_in.value)
        if not ch_id:
            return await interaction.response.send_message(t(gid, 'rules.bad_link'), ephemeral=True)
        role = parse_role(interaction.guild, self.role_in.value)
        if not role:
            return await interaction.response.send_message(t(gid, 'wiz.role_bad'), ephemeral=True)
        style, _ = color_style(self.color_in.value)
        label = (self.label_in.value or role.name)[:80]
        ch = interaction.guild.get_channel(ch_id)
        try:
            msg = await ch.fetch_message(msg_id)
        except Exception:
            return await interaction.response.send_message(t(gid, 'rules.bad_link'), ephemeral=True)
        view = discord.ui.View(timeout=None)
        for row in msg.components:
            for comp in row.children:
                if comp.type.name == 'button' and comp.custom_id:
                    view.add_item(discord.ui.Button(label=comp.label, style=comp.style,
                                                    custom_id=comp.custom_id, disabled=comp.disabled,
                                                    emoji=comp.emoji, url=comp.url))
        if len(view.children) >= 25:
            return await interaction.response.send_message(t(gid, 'sr.full'), ephemeral=True)
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=f'sr:{role.id}'))
        try:
            await msg.edit(view=view)
        except Exception:
            return await interaction.response.send_message(t(gid, 'info.post_fail'), ephemeral=True)
        try:
            await interaction.response.send_message(t(gid, 'sr.quick_ok', role=role.mention), ephemeral=True,
                                                    delete_after=10)
        except Exception:
            pass


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('sr:'):
            return
        gid = interaction.guild_id
        role = interaction.guild.get_role(int(cid.split(':')[1]))
        if not role:
            return await interaction.response.send_message(t(gid, 'sr.gone'), ephemeral=True)
        member = interaction.user
        if role in member.roles:
            try:
                await member.remove_roles(role, reason='self role')
            except Exception:
                pass
            await interaction.response.send_message(t(gid, 'sr.removed', role=role.mention), ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason='self role')
            except Exception:
                pass
            await interaction.response.send_message(t(gid, 'sr.added', role=role.mention), ephemeral=True)

    @commands.hybrid_command(name='selfroles', description='Panel ról')
    @staff_or('manage_roles')
    async def selfroles(self, ctx, role1: discord.Role, role2: discord.Role = None, role3: discord.Role = None,
                        role4: discord.Role = None, role5: discord.Role = None, title: str = None):
        gid = ctx.guild.id
        roles = [r for r in (role1, role2, role3, role4, role5) if r]
        embed = discord.Embed(title=title or t(gid, 'sr.title'),
                              description='\n'.join(f'• {r.mention} — click to toggle' for r in roles),
                              color=WHITE)
        embed.set_footer(text=foot())
        view = discord.ui.View(timeout=None)
        for r in roles:
            view.add_item(discord.ui.Button(label=r.name[:80], style=discord.ButtonStyle.grey,
                                            custom_id=f'sr:{r.id}'))
        msg = await ctx.channel.send(embed=embed, view=view)
        import json
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO selfroles (guild_id, message_id, channel_id, roles) VALUES (?,?,?,?)',
                         (str(gid), str(msg.id), str(ctx.channel.id), json.dumps([str(r.id) for r in roles])))
        await ctx.reply(t(gid, 'sr.posted', n=len(roles)), ephemeral=True)

    @commands.hybrid_command(name='sr-add', description='Szybki guzik na wiadomoÅ›Ä‡')
    @staff_or('manage_roles')
    async def sr_add(self, ctx, message_link: str = None, label: str = None, role: str = None, color: str = None):
        gid = ctx.guild.id
        if message_link and role:
            # prefix path: everything inline
            ch_id, msg_id = parse_link(message_link)
            r = parse_role(ctx.guild, role)
            style, _ = color_style(color)
            if not ch_id or not r:
                return await ctx.reply(t(gid, 'wiz.role_bad'), ephemeral=True)
            try:
                ch = ctx.guild.get_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                view = discord.ui.View(timeout=None)
                for row in msg.components:
                    for comp in row.children:
                        if comp.type.name == 'button' and comp.custom_id:
                            view.add_item(discord.ui.Button(label=comp.label, style=comp.style,
                                                            custom_id=comp.custom_id, disabled=comp.disabled,
                                                            emoji=comp.emoji, url=comp.url))
                view.add_item(discord.ui.Button(label=(label or r.name)[:80], style=style,
                                                custom_id=f'sr:{r.id}'))
                await msg.edit(view=view)
            except Exception:
                return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
            return await ctx.reply(t(gid, 'sr.quick_ok', role=r.mention), ephemeral=True)
        if ctx.interaction is None:
            return await ctx.reply(t(gid, 'wiz.args_needed',
                                     use='.sr-add <message-link> [label] <@role> [color]'), ephemeral=True)
        await ctx.interaction.response.send_modal(QuickButtonModal(gid))


async def setup(bot):
    await bot.add_cog(Roles(bot))

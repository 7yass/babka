"""Embed builder: post rich embeds, edit/delete via buttons. Hybrid."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, WHITE
from utils.checks import staff_or


def make_embed(row: dict) -> discord.Embed:
    e = discord.Embed(title=row.get('title') or '', description=row.get('description') or '', color=WHITE)
    if row.get('footer'):
        e.set_footer(text=row['footer'])
    if row.get('image'):
        e.set_image(url=row['image'])
    if row.get('thumb'):
        e.set_thumbnail(url=row['thumb'])
    return e


class EditModal(discord.ui.Modal):
    def __init__(self, embed_id: int, gid):
        super().__init__(title=t(gid, 'emb.edit_title'))
        self.embed_id = embed_id
        self.title_in = discord.ui.TextInput(label=t(gid, 'emb.f_title'), required=False, max_length=200)
        self.desc_in = discord.ui.TextInput(label=t(gid, 'emb.f_desc'), required=False,
                                            style=discord.TextStyle.paragraph, max_length=3000)
        self.add_item(self.title_in)
        self.add_item(self.desc_in)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM embeds WHERE id=? AND guild_id=?',
                               (self.embed_id, str(gid))).fetchone()
            if not row:
                return await interaction.response.send_message(t(gid, 'emb.gone'), ephemeral=True)
            title = self.title_in.value.strip() or row['title']
            desc = self.desc_in.value.strip() or row['description']
            conn.execute('UPDATE embeds SET title=?, description=? WHERE id=?', (title, desc, self.embed_id))
            row = dict(conn.execute('SELECT * FROM embeds WHERE id=?', (self.embed_id,)).fetchone())
        try:
            ch = interaction.guild.get_channel(int(row['channel_id']))
            msg = await ch.fetch_message(int(row['message_id']))
            await msg.edit(embed=make_embed(row))
        except Exception:
            pass
        await interaction.response.send_message(t(gid, 'emb.updated'), ephemeral=True)


class Embeds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not (cid.startswith('emb_edit:') or cid.startswith('emb_del:')):
            return
        gid = interaction.guild_id
        member = interaction.user
        if not (member.guild_permissions.manage_guild or
                __import__('utils.checks', fromlist=['is_staff']).is_staff(member)):
            return await interaction.response.send_message(t(gid, 'staff.only'), ephemeral=True)
        eid = cid.split(':')[1]
        if cid.startswith('emb_edit:'):
            return await interaction.response.send_modal(EditModal(int(eid), gid))
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM embeds WHERE id=? AND guild_id=?', (eid, str(gid))).fetchone()
            if row:
                try:
                    ch = interaction.guild.get_channel(int(row['channel_id']))
                    await (await ch.fetch_message(int(row['message_id']))).delete()
                except Exception:
                    pass
                conn.execute('DELETE FROM embeds WHERE id=?', (eid,))
        await interaction.response.send_message(t(gid, 'emb.deleted'), ephemeral=True)

    @commands.group(name='embed', description='Konstruktor embedów')
    async def embed(self, ctx):
        await ctx.reply('.embed post / edit / delete', ephemeral=True)

    @embed.command(name='post', description='Wyślij embeda')
    @staff_or('manage_guild')
    async def post(self, ctx, channel: discord.TextChannel, title: str, *, rest: str = ''):
        parts = [p.strip() for p in rest.split('|')]
        desc = parts[0] if parts and parts[0] else ''
        footer = parts[1] if len(parts) > 1 else ''
        image = parts[2] if len(parts) > 2 else ''
        with db.conn_ctx() as conn:
            cur = conn.execute('INSERT INTO embeds (guild_id, channel_id, title, description, footer, image) VALUES (?,?,?,?,?,?)',
                               (str(ctx.guild.id), str(channel.id), title, desc, footer, image))
            eid = cur.lastrowid
        row = {'title': title, 'description': desc, 'footer': footer, 'image': image, 'thumb': ''}
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=t(ctx.guild.id, 'ui.edit'), style=discord.ButtonStyle.grey,
                                        custom_id=f'emb_edit:{eid}'))
        view.add_item(discord.ui.Button(label=t(ctx.guild.id, 'ui.delete'), style=discord.ButtonStyle.grey,
                                        custom_id=f'emb_del:{eid}'))
        try:
            msg = await channel.send(embed=make_embed(row), view=view)
        except Exception:
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM embeds WHERE id=?', (eid,))
            return await ctx.reply(t(ctx.guild.id, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE embeds SET message_id=? WHERE id=?', (str(msg.id), eid))
        await ctx.reply(t(ctx.guild.id, 'emb.posted', ch=channel.mention), ephemeral=True)

    @embed.command(name='edit', description='Edytuj embeda po ID')
    @staff_or('manage_guild')
    async def edit_cmd(self, ctx, message_id: str, *, rest: str = ''):
        gid = ctx.guild.id
        parts = [p.strip() for p in rest.split('|')]
        title = parts[0] if parts and parts[0] else None
        desc = parts[1] if len(parts) > 1 else None
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM embeds WHERE message_id=? AND guild_id=?',
                               (message_id, str(gid))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'emb.gone'), ephemeral=True)
            conn.execute('UPDATE embeds SET title=COALESCE(?,title), description=COALESCE(?,description) WHERE id=?',
                         (title or None, desc or None, row['id']))
            row = dict(conn.execute('SELECT * FROM embeds WHERE id=?', (row['id'],)).fetchone())
        try:
            ch = ctx.guild.get_channel(int(row['channel_id']))
            await (await ch.fetch_message(int(row['message_id']))).edit(embed=make_embed(row))
        except Exception:
            pass
        await ctx.reply(t(gid, 'emb.updated'), ephemeral=True)

    @embed.command(name='delete', description='Usuń embeda')
    @staff_or('manage_guild')
    async def delete(self, ctx, message_id: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM embeds WHERE message_id=? AND guild_id=?',
                               (message_id, str(gid))).fetchone()
            if not row:
                return await ctx.reply(t(gid, 'emb.gone'), ephemeral=True)
            try:
                ch = ctx.guild.get_channel(int(row['channel_id']))
                await (await ch.fetch_message(int(row['message_id']))).delete()
            except Exception:
                pass
            conn.execute('DELETE FROM embeds WHERE id=?', (row['id'],))
        await ctx.reply(t(gid, 'emb.deleted'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Embeds(bot))

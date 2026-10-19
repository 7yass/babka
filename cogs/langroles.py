"""Language roles: one command builds channel + 3 roles + select panel.

The select is built and registered by services.persistent, so it keeps
working after a restart (the picked roles are read from the click, the
roles themselves live in the guild).
"""
import discord
from discord.ext import commands

import database as db
from lang import t
from services import persistent
from utils.checks import staff_or

LANGS = persistent.LANGS

# Mathematical Sans-Serif Bold Italic (like 𝙊𝙜𝙤𝙡𝙣𝙮)
_BI_U = {chr(c): chr(0x1D63C + c - 65) for c in range(65, 91)}
_BI_L = {chr(c): chr(0x1D656 + c - 97) for c in range(97, 123)}


def stylish(s: str) -> str:
    out = []
    for ch in s:
        out.append(_BI_U.get(ch) or _BI_L.get(ch) or ch)
    return ''.join(out)


CHANNEL_NAME = '🔢・' + stylish('Role-Reakcyjne')


class LangRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='langroles', description='Role językowe')
    async def langroles(self, ctx):
        await ctx.reply('.langroles setup', ephemeral=True)

    @langroles.command(name='setup', description='Stwórz kanał + role + panel')
    @staff_or('manage_guild')
    async def setup(self, ctx):
        gid = ctx.guild.id
        await ctx.defer(ephemeral=True)
        roles = []
        for name, _flag in LANGS:
            role = discord.utils.find(lambda r: r.name == name, ctx.guild.roles)
            if not role:
                try:
                    role = await ctx.guild.create_role(name=name, reason='language roles')
                except Exception:
                    return await ctx.reply(t(gid, 'pv.lr.fail'), ephemeral=True)
            roles.append(role)
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and 'reakcyjne' in
                                (c.name or '').lower().replace(' ', ''), ctx.guild.text_channels)
        if not ch:
            try:
                ch = await ctx.guild.create_text_channel(CHANNEL_NAME, reason='language roles')
            except Exception:
                return await ctx.reply(t(gid, 'pv.lr.fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT message_id FROM langroles_cfg WHERE guild_id=?',
                               (str(gid),)).fetchone()
            if old and old['message_id']:
                try:
                    old_msg = await ch.fetch_message(int(old['message_id']))
                    await old_msg.delete()
                except Exception:
                    pass
        emb = discord.Embed(title=t(gid, 'pv.lr.title'), description=t(gid, 'pv.lr.desc'), color=0xFFFFFF)
        emb.set_footer(text='babka :3')
        # The select carries a custom_id derived from the guild, so the
        # registry can rebuild it after a restart.
        view = persistent.build_langroles_view(gid, roles=roles)
        try:
            msg = await ch.send(embed=emb, view=view)
        except Exception:
            return await ctx.reply(t(gid, 'pv.lr.fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO langroles_cfg (guild_id, channel_id, message_id) VALUES (?,?,?)',
                         (str(gid), str(ch.id), str(msg.id)))
        persistent.track(view, self.bot)
        await ctx.reply(t(gid, 'pv.lr.done', ch=ch.mention), ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # Registered panel views answer their own clicks (services/persistent
        # rebuilds them from the DB on every boot). This is only the safety
        # net for panels the registry does not know about.
        await persistent.handle_fallback(interaction)


async def setup(bot):
    await bot.add_cog(LangRoles(bot))

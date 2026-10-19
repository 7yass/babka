"""Verification gate: button or captcha, unverified role on join, auto-kick. Hybrid.

The gate view is built and registered by services.persistent, so the button
keeps working after a restart (the role and the method are read from the DB
on every click, never from a closure).
"""
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t, set_ctx_lang
from services import persistent
from utils.checks import staff_or

# The gate itself (button, captcha, role granting) lives in the persistent
# layer, so the same code answers the click whether the view was built at
# runtime or rebuilt from the DB after a restart.
CaptchaModal = persistent.CaptchaModal
grant = persistent.grant


def vcfg(gid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM verify_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        return dict(row) if row else {}


class Verify(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sweeper.start()

    def cog_unload(self):
        self.sweeper.cancel()

    @tasks.loop(minutes=5)
    async def sweeper(self):
        await self.bot.wait_until_ready()
        now = time.time()
        for guild in self.bot.guilds:
            cfg = vcfg(guild.id)
            mins = (cfg.get('kick_minutes') or 0) if cfg else 0
            if not mins or not cfg.get('unverified_role'):
                continue
            role = guild.get_role(int(cfg['unverified_role']))
            if not role:
                continue
            for m in role.members:
                if m.bot or not m.joined_at:
                    continue
                if (now - m.joined_at.timestamp()) > mins * 60:
                    try:
                        try:
                            await m.send(t(guild.id, 'pv.vf.kicked', server=guild.name, mins=mins))
                        except Exception:
                            pass
                        await m.kick(reason='never verified')
                    except Exception:
                        pass

    @sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = vcfg(member.guild.id)
        if cfg.get('unverified_role'):
            try:
                role = member.guild.get_role(int(cfg['unverified_role']))
                if role:
                    await member.add_roles(role, reason='unverified')
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        # Registered panel views answer their own clicks (services/persistent
        # rebuilds them from the DB on every boot). This is only the safety
        # net for panels the registry does not know about.
        await persistent.handle_fallback(interaction)

    @commands.group(name='verify', description='Verification gate')
    async def verify(self, ctx):
        await ctx.reply('.verify setup / off', ephemeral=True)

    @verify.command(name='setup', description='Post the verification panel')
    @staff_or('manage_guild')
    async def setup_v(self, ctx, channel: discord.TextChannel, role: discord.Role,
                      unverified: discord.Role = None, method: str = 'button',
                      kick_minutes: int = 0, title: str = None, label: str = None,
                      *, description: str = ''):
        gid = ctx.guild.id
        method = method.lower()
        if method not in ('button', 'captcha'):
            method = 'button'
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT * FROM verify_cfg WHERE guild_id=?', (str(gid),)).fetchone()
            if old and old['message_id']:
                try:
                    ch = ctx.guild.get_channel(int(old['channel_id']))
                    await (await ch.fetch_message(int(old['message_id']))).delete()
                except Exception:
                    pass
            conn.execute('''INSERT OR REPLACE INTO verify_cfg
                (guild_id, channel_id, role_id, unverified_role, method, kick_minutes, title, description, label)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                         (str(gid), str(channel.id), str(role.id),
                          str(unverified.id) if unverified else None, method, kick_minutes,
                          title or t(gid, 'pv.vf.def_title'), description,
                          label or t(gid, 'pv.vf.def_label')))
        # One view, built by the persistent layer: the same custom_id the
        # registry rebuilds after a restart, so the button never dies.
        layout = persistent.build_verify_view(gid)
        try:
            msg = await channel.send(view=layout)
        except Exception:
            return await ctx.reply(t(gid, 'pv.info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE verify_cfg SET message_id=? WHERE guild_id=?', (str(msg.id), str(gid)))
        persistent.track(layout, self.bot)
        await ctx.reply(t(gid, 'pv.vf.live', ch=channel.mention, role=role.mention, method=method), ephemeral=True)

    @verify.command(name='off', description='Remove verification')
    @staff_or('manage_guild')
    async def off(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM verify_cfg WHERE guild_id=?', (str(gid),)).fetchone()
            if cfg and cfg['message_id']:
                try:
                    ch = ctx.guild.get_channel(int(cfg['channel_id']))
                    await (await ch.fetch_message(int(cfg['message_id']))).delete()
                except Exception:
                    pass
            conn.execute('DELETE FROM verify_cfg WHERE guild_id=?', (str(gid),))
        await ctx.reply(t(gid, 'pv.vf.off'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verify(bot))

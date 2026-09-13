"""Verification gate: button or captcha, unverified role on join, auto-kick. Hybrid."""
import asyncio
import random
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import WHITE, foot
from utils.checks import staff_or
from utils.common import log_to_mod


def vcfg(gid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM verify_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        return dict(row) if row else {}


async def grant(member: discord.Member, cfg: dict):
    gid = member.guild.id
    try:
        if cfg.get('unverified_role'):
            role = member.guild.get_role(int(cfg['unverified_role']))
            if role and role in member.roles:
                await member.remove_roles(role, reason='verified')
        role = member.guild.get_role(int(cfg['role_id']))
        if role and role not in member.roles:
            await member.add_roles(role, reason='verified')
    except Exception:
        pass
    await log_to_mod(member.guild, discord.Embed(
        description=t(gid, 'vf.log', user=member.mention), title=t(gid, 'vf.log_title'), color=WHITE))


class CaptchaModal(discord.ui.Modal):
    def __init__(self, gid, a: int, b: int):
        super().__init__(title=t(gid, 'vf.cap_title'))
        self.answer = a + b
        self.gid = gid
        self.q = discord.ui.TextInput(label=t(gid, 'vf.cap_q', a=a, b=b), max_length=10)
        self.add_item(self.q)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = vcfg(self.gid)
        try:
            ok = int(self.q.value.strip()) == self.answer
        except Exception:
            ok = False
        if not ok:
            return await interaction.response.send_message(t(self.gid, 'vf.cap_no'), ephemeral=True)
        await grant(interaction.user, cfg)
        role = interaction.guild.get_role(int(cfg['role_id'])) if cfg.get('role_id') else None
        await interaction.response.send_message(
            t(self.gid, 'vf.done', role=role.name if role else ''), ephemeral=True)
        await asyncio.sleep(120)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


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
                            await m.send(t(guild.id, 'vf.kicked', server=guild.name, mins=mins))
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
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('gate:'):
            return
        gid = interaction.guild_id
        cfg = vcfg(gid)
        if not cfg.get('role_id'):
            return await interaction.response.send_message(t(gid, 'vfy.none'), ephemeral=True)
        role = interaction.guild.get_role(int(cfg['role_id']))
        if not role:
            return await interaction.response.send_message(t(gid, 'vfy.no_role'), ephemeral=True)
        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message(t(gid, 'vfy.have'), ephemeral=True)
        elif (cfg.get('method') or 'button') == 'captcha':
            a, b = random.randint(2, 20), random.randint(2, 20)
            await interaction.response.send_modal(CaptchaModal(gid, a, b))
            return
        else:
            await grant(member, cfg)
            await interaction.response.send_message(t(gid, 'vfy.done', role=role.name), ephemeral=True)
        await asyncio.sleep(120)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

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
                          title or t(gid, 'vf.def_title'), description,
                          label or t(gid, 'vf.def_label')))
        from discord.ui import LayoutView, Container, TextDisplay, ActionRow
        body = title or t(gid, 'vf.def_title')
        desc = description or ''
        if method == 'captcha':
            desc = (desc + '\n\n' if desc else '') + t(gid, 'vf.cap_hint')
        layout = LayoutView(timeout=None)
        box = Container(accent_color=0xFFFFFF)
        box.add_item(TextDisplay(f'## {body}\n{desc}'))
        row = ActionRow()
        row.add_item(discord.ui.Button(label=label or t(gid, 'vf.def_label'),
                                       style=discord.ButtonStyle.grey, custom_id='gate:new'))
        box.add_item(row)
        layout.add_item(box)
        try:
            msg = await channel.send(view=layout)
        except Exception:
            return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE verify_cfg SET message_id=? WHERE guild_id=?', (str(msg.id), str(gid)))
        await ctx.reply(t(gid, 'vf.live', ch=channel.mention, role=role.mention, method=method), ephemeral=True)

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
        await ctx.reply(t(gid, 'vf.off'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verify(bot))

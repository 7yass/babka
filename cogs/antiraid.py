"""AntiRaid: permaban re-ban, 60-day age gate, burst lockdown, honeypot."""
import asyncio
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, ok, WHITE, DARK_RED, RED
from utils.checks import staff_or
from utils.common import log_to_mod

JOIN_BURST: dict = defaultdict(lambda: deque(maxlen=50))


async def set_lockdown(guild: discord.Guild, locked: bool) -> int:
    failed = 0
    for ch in guild.text_channels:
        try:
            if locked:
                await ch.set_permissions(guild.default_role, send_messages=False)
            else:
                await ch.set_permissions(guild.default_role, send_messages=None)
        except Exception:
            failed += 1
    with db.conn_ctx() as conn:
        conn.execute('UPDATE antiraid SET lockdown=? WHERE guild_id=?', (1 if locked else 0, str(guild.id)))
    return failed


async def alert_admins(guild: discord.Guild, text: str):
    for m in guild.members:
        if m.bot or not m.guild_permissions.administrator:
            continue
        try:
            await m.send(text)
        except Exception:
            pass


class AntiRaid(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- join handling ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gid = member.guild.id
        settings = db.get_settings(gid)
        perm = None
        with db.conn_ctx() as conn:
            perm = conn.execute('SELECT * FROM permabans WHERE guild_id=? AND user_id=?',
                                (str(gid), str(member.id))).fetchone()
        if perm:
            try:
                await member.ban(reason=f"[Permaban] {perm['reason'] or 'banned forever'}", delete_message_days=1)
            except Exception:
                pass
            await log_to_mod(member.guild, build(
                t(gid, 'ar.perma_body', tag=str(member), id=member.id, reason=perm['reason'] or 'banned forever'),
                title=t(gid, 'ar.perma_title'), color=RED))
            return

        cfg = db.get_antiraid(gid)
        age_days = (time.time() - member.created_at.timestamp()) / 86400
        if cfg['enabled'] and age_days < (cfg['min_age_days'] or 60):
            try:
                await member.send(t(gid, 'ar.dm', server=member.guild.name, age=f'{age_days:.0f}', min=cfg['min_age_days']))
                if cfg['action'] == 'ban':
                    await member.ban(reason=f'AntiRaid: age {age_days:.1f}d < {cfg["min_age_days"]}d')
                elif cfg['action'] == 'kick':
                    await member.kick(reason=f'AntiRaid: age {age_days:.1f}d < {cfg["min_age_days"]}d')
            except Exception:
                pass
            await log_to_mod(member.guild, build(
                t(gid, 'ar.young_body', tag=str(member), id=member.id, age=f'{age_days:.1f}',
                  min=cfg['min_age_days'], action=cfg['action']),
                title=t(gid, 'ar.young_title'), color=DARK_RED))
            return

        now = time.time()
        dq = JOIN_BURST[member.guild.id]
        while dq and now - dq[0] > (cfg['burst_seconds'] or 10):
            dq.popleft()
        dq.append(now)
        if cfg['enabled'] and len(dq) >= (cfg['burst_count'] or 5):
            dq.clear()
            failed = await set_lockdown(member.guild, True)
            msg = t(gid, 'ar.burst_body', n=cfg['burst_count'], s=cfg['burst_seconds'],
                    f=t(gid, 'ar.lock_fail', n=failed) if failed else '')
            await log_to_mod(member.guild, build(msg, title=t(gid, 'ar.burst_title'), color=DARK_RED))
            await alert_admins(member.guild, msg)
            return

        if age_days < 7:
            await log_to_mod(member.guild, build(
                t(gid, 'ar.new_body', tag=str(member), id=member.id, age=f'{age_days:.1f}'),
                title=t(gid, 'ar.new_title'), color=WHITE))

    # ---------- honeypot ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        cfg = db.get_antiraid(message.guild.id)
        if not cfg.get('honeypot_channel') or str(message.channel.id) != str(cfg['honeypot_channel']):
            return
        member = message.author
        if isinstance(member, discord.Member) and member.guild_permissions.manage_messages:
            return
        gid = message.guild.id
        try:
            await message.guild.ban(member, reason='[AntiRaid] honeypot', delete_message_days=1)
            body = t(gid, 'ar.honey_body', tag=str(member), id=member.id, ch=message.channel.mention)
            await log_to_mod(message.guild, build(body, title=t(gid, 'ar.honey_title'), color=DARK_RED))
            await alert_admins(message.guild, body)
        except Exception as e:
            print(f'[honeypot] {e}')

    # ---------- commands ----------
    @commands.hybrid_group(name='antiraid', description='Ochrona anty-rajdowa')
    async def antiraid(self, ctx):
        await ctx.reply('/antiraid status / toggle / set-age / set-action / set-burst / honeypot-set / honeypot-clear / lockdown',
                        ephemeral=True)

    @antiraid.command(name='status', description='Pokaż ustawienia')
    async def status(self, ctx):
        gid = ctx.guild.id
        cfg = db.get_antiraid(gid)
        hp = f'<#{cfg["honeypot_channel"]}>' if cfg.get('honeypot_channel') else '(none)'
        await ctx.reply(t(gid, 'ar.status_body', on=t(gid, 'ar.on') if cfg['enabled'] else t(gid, 'ar.off'),
                            days=cfg['min_age_days'], action=cfg['action'], c=cfg['burst_count'], s=cfg['burst_seconds'],
                            ch=hp, lock=t(gid, 'ar.active') if cfg['lockdown'] else t(gid, 'ar.off')),
                        ephemeral=True)

    @antiraid.command(name='toggle', description='Włącz / wyłącz')
    @staff_or('administrator')
    async def toggle(self, ctx):
        gid = ctx.guild.id
        cfg = db.get_antiraid(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET enabled=? WHERE guild_id=?', (0 if cfg['enabled'] else 1, str(gid)))
        await ctx.reply(t(gid, 'ar.toggled', t=t(gid, 'ar.off') if cfg['enabled'] else t(gid, 'ar.on')), ephemeral=True)

    @antiraid.command(name='set-age', description='Minimalny wiek konta (dni)')
    @staff_or('administrator')
    async def set_age(self, ctx, days: int):
        gid = ctx.guild.id
        cfg = db.get_antiraid(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET min_age_days=? WHERE guild_id=?', (days, str(gid)))
        await ctx.reply(t(gid, 'ar.age_off') if days == 0 else t(gid, 'ar.age_set', d=days, a=cfg['action']),
                        ephemeral=True)

    @antiraid.command(name='set-action', description='kick / ban / log')
    @staff_or('administrator')
    async def set_action(self, ctx, action: str):
        gid = ctx.guild.id
        db.get_antiraid(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET action=? WHERE guild_id=?', (action, str(gid)))
        await ctx.reply(t(gid, 'ar.action_set', a=action), ephemeral=True)

    @antiraid.command(name='set-burst', description='Próg nawałnicy')
    @staff_or('administrator')
    async def set_burst(self, ctx, count: int, seconds: int):
        gid = ctx.guild.id
        db.get_antiraid(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET burst_count=?, burst_seconds=? WHERE guild_id=?',
                         (count, seconds, str(gid)))
        await ctx.reply(t(gid, 'ar.burst_set', c=count, s=seconds), ephemeral=True)

    @antiraid.command(name='honeypot-set', description='Kanał-pułapka')
    @staff_or('administrator')
    async def honeypot_set(self, ctx, channel: discord.TextChannel):
        gid = ctx.guild.id
        db.get_antiraid(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET honeypot_channel=? WHERE guild_id=?', (str(channel.id), str(gid)))
        await ctx.reply(embed=ok(t(gid, 'ar.hp_set', ch=channel.mention)))

    @antiraid.command(name='honeypot-clear', description='Usuń honeypota')
    @staff_or('administrator')
    async def honeypot_clear(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('UPDATE antiraid SET honeypot_channel=NULL WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'ar.hp_off'), ephemeral=True)

    @antiraid.command(name='lockdown', description='Lockdown całości')
    @staff_or('administrator')
    async def lockdown(self, ctx):
        gid = ctx.guild.id
        cfg = db.get_antiraid(gid)
        locking = not cfg['lockdown']
        await ctx.defer(ephemeral=True)
        failed = await set_lockdown(ctx.guild, locking)
        if locking:
            await ctx.reply(t(gid, 'ar.locked', f=t(gid, 'ar.lock_fail', n=failed) if failed else ''))
        else:
            await ctx.reply(t(gid, 'ar.unlocked'))

    @commands.hybrid_command(name='lockdown', description='Lockdown całości (alarmowo)')
    @staff_or('administrator')
    async def lockdown_flat(self, ctx):
        await self.lockdown.callback(self, ctx)

    @commands.hybrid_command(name='raid', description='Status ochrony')
    async def raid_flat(self, ctx):
        await self.status.callback(self, ctx)


async def setup(bot):
    await bot.add_cog(AntiRaid(bot))

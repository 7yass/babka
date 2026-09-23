"""World boss: one shared-HP raid per guild. Thin Discord layer over
services.worldboss_service — status card, join/attack/claim, staff start.
"""
import time

import discord
from discord.ext import commands, tasks

from lang import t
from utils.checks import staff_or


def _bar(hp: int, max_hp: int, width: int = 20) -> str:
    frac = max(0.0, min(1.0, hp / max(1, max_hp)))
    full = int(width * frac)
    return '█' * full + '░' * (width - full)


class WorldBoss(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        try:
            if not self._scheduler.is_running():
                self._scheduler.start()
        except Exception:
            pass

    def cog_unload(self):
        try:
            self._scheduler.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=5)
    async def _scheduler(self):
        import time as _t
        try:
            from tasks import worldboss_scheduler as _sched
            summary = await self.bot.loop.run_in_executor(
                None, _sched.tick, list(getattr(self.bot, 'guilds', []) or []),
                int(_t.time()))
        except Exception as e:
            print(f'[wbsched] tick failed: {type(e).__name__}: {e}')
            return
        try:
            acted = {g: v for g, v in summary.items()
                     if v.get('started') or v.get('expired') or v.get('error')}
            if acted:
                print(f'[wbsched] {acted}', flush=True)
        except Exception:
            pass

    def _status_text(self, gid, v: dict, uid=None) -> str:
        title = f"☠️ **{v['name']}**"
        if v.get('phase_name'):
            title += f" — {v['phase_name']}"
        lines = [f"{title}  `{_bar(v['hp'], v['max_hp'])}` "
                 f"**{v['hp']}/{v['max_hp']}**"]
        m, s = divmod(int(v['ends_in']), 60)
        lines.append(f"Ends in {m}m {s}s · {v['participants']} hunters")
        if v.get('weather') == 'rain':
            lines.append(t(gid, 'eco.wb_weather_rain'))
        elif v.get('weather'):
            lines.append(t(gid, 'eco.wb_weather', name=v['weather']))
        if v.get('max_phase_name'):
            lines.append(t(gid, 'eco.wb_phase_reached', phase=v['max_phase_name']))
            if uid is not None:
                mine = (v.get('by_user') or {}).get(str(uid)) or {}
                if (mine.get('damage') or 0) >= (v.get('reward_threshold') or 0):
                    lines.append(t(gid, 'eco.wb_phase_eligible'))
        for i, p in enumerate(v['top'], start=1):
            try:
                mbr = self.bot.get_guild(int(gid)).get_member(int(p['user_id']))
                name = mbr.display_name if mbr else f"<@{p['user_id']}>"
            except Exception:
                name = f"<@{p['user_id']}>"
            lines.append(f"{i}. {name} — {p['damage']} dmg ({p['attacks']}⚔)")
        return '\n'.join(lines)

    @commands.group(name='worldboss', description='Wspólny boss serwera',
                    invoke_without_command=True)
    async def worldboss(self, ctx):
        from services import worldboss_service as _wb
        v = _wb.boss_status(ctx.guild.id)
        if not v:
            return await ctx.reply(t(ctx.guild.id, 'eco.wb_no_boss'))
        await ctx.reply(self._status_text(ctx.guild.id, v, ctx.author.id), mention_author=False)

    @worldboss.command(name='join', description='Dołącz do polowania')
    async def wb_join(self, ctx):
        from services import worldboss_service as _wb
        res = _wb.join_boss(ctx.guild.id, ctx.author.id)
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='attack', description='Atakuj bossa')
    async def wb_attack(self, ctx):
        from services import worldboss_service as _wb
        res = _wb.attack_boss(ctx.guild.id, ctx.author.id)
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='status', description='Status bossa')
    async def wb_status(self, ctx):
        from services import worldboss_service as _wb
        v = _wb.boss_status(ctx.guild.id)
        if not v:
            return await ctx.reply(t(ctx.guild.id, 'eco.wb_no_boss'))
        await ctx.reply(self._status_text(ctx.guild.id, v, ctx.author.id), mention_author=False)

    @worldboss.command(name='rewards', description='Odbierz nagrodę')
    async def wb_rewards(self, ctx):
        from services import worldboss_service as _wb
        res = _wb.claim_rewards(ctx.guild.id, ctx.author.id)
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='switch', description='Zmień wojownika')
    async def wb_switch(self, ctx, slot: str = ''):
        from services import worldboss_service as _wb
        gid = ctx.guild.id
        if not (slot or '').strip().isdigit():
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        from cogs.pokemon import get_mon
        m = get_mon(gid, ctx.author.id, int(slot.strip()))
        if not m:
            return await ctx.reply(t(gid, 'eco.pk_noslot'), ephemeral=True)
        res = _wb.switch_mon(gid, ctx.author.id, m['id'])
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='heal', description='Ulecz wojownika (1/rajd, płatne)')
    async def wb_heal(self, ctx):
        from services import worldboss_service as _wb
        res = _wb.heal_fighter(ctx.guild.id, ctx.author.id)
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='start', description='Wystaw bossa (staff)')
    @staff_or('administrator')
    async def wb_start(self, ctx, boss: str = 'dreadmaw'):
        from services import worldboss_service as _wb
        res = _wb.start_boss(ctx.guild.id, boss, int(time.time()))
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='auto', description='Auto-start rajdu (staff)')
    @staff_or('administrator')
    async def wb_auto(self, ctx, mode: str = ''):
        from tasks import worldboss_scheduler as _sched
        mode = (mode or '').lower()
        if mode not in ('on', 'off'):
            cur = _sched.get_settings(ctx.guild.id)
            return await ctx.reply(
                t(ctx.guild.id, 'eco.wb_auto_state',
                  state='on' if cur['auto'] else 'off'), ephemeral=True)
        _sched.set_setting(ctx.guild.id, 'auto', '1' if mode == 'on' else '0')
        await ctx.reply(t(ctx.guild.id, 'eco.wb_auto_set', state=mode), ephemeral=True)

    @worldboss.command(name='channel', description='Kanał rajdu (staff)')
    @staff_or('administrator')
    async def wb_channel(self, ctx, channel: str = ''):
        from tasks import worldboss_scheduler as _sched
        gid = ctx.guild.id
        arg = (channel or '').strip().lower()
        if not arg:
            cur = _sched.get_settings(gid)
            return await ctx.reply(
                t(gid, 'eco.wb_channel_state',
                  ch=(f'<#{cur["channel"]}>' if cur['channel'] else '—')),
                ephemeral=True)
        if arg in ('off', 'none', 'clear'):
            _sched.set_setting(gid, 'channel', '')
            return await ctx.reply(t(gid, 'eco.wb_channel_cleared'), ephemeral=True)
        cid = ''.join(c for c in arg if c.isdigit())
        try:
            ok = cid and ctx.guild.get_channel(int(cid)) is not None
        except Exception:
            ok = False
        if not ok:
            return await ctx.reply(t(gid, 'eco.wb_channel_bad'), ephemeral=True)
        _sched.set_setting(gid, 'channel', cid)
        await ctx.reply(t(gid, 'eco.wb_channel_set', ch=f'<#{cid}>'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(WorldBoss(bot))

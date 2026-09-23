"""World boss: one shared-HP raid per guild. Thin Discord layer over
services.worldboss_service — status card, join/attack/claim, staff start.
"""
import time

import discord
from discord.ext import commands

from lang import t
from utils.checks import staff_or


def _bar(hp: int, max_hp: int, width: int = 20) -> str:
    frac = max(0.0, min(1.0, hp / max(1, max_hp)))
    full = int(width * frac)
    return '█' * full + '░' * (width - full)


class WorldBoss(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _status_text(self, gid, v: dict) -> str:
        lines = [f"☠️ **{v['name']}**  `{_bar(v['hp'], v['max_hp'])}` "
                 f"**{v['hp']}/{v['max_hp']}**"]
        m, s = divmod(int(v['ends_in']), 60)
        lines.append(f"Ends in {m}m {s}s · {v['participants']} hunters")
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
        await ctx.reply(self._status_text(ctx.guild.id, v), mention_author=False)

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
        await ctx.reply(self._status_text(ctx.guild.id, v), mention_author=False)

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


async def setup(bot):
    await bot.add_cog(WorldBoss(bot))

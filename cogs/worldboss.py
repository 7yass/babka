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


def _ago(ts, now):
    try:
        s = max(0, int(now) - int(ts))
    except Exception:
        return 'never'
    if s < 60:
        return f'{s}s ago'
    m, s = divmod(s, 60)
    if m < 60:
        return f'{m}m ago'
    h, m = divmod(m, 60)
    return f'{h}h {m}m ago'


def build_config_text(gid, now):
    """Pure status dashboard (headless-testable): settings, event,
    next eligible time, claims, tick lifecycle."""
    from services import worldboss_service as _wb
    from tasks import worldboss_scheduler as _sched
    gid, now = str(gid), int(now)
    s = _sched.get_settings(gid)
    life = _sched.lifecycle_info(gid)
    v = _wb.boss_status(gid, now)
    rot = ' → '.join(s['rotation']) if s['rotation'] else '—'
    lines = [f"World-boss scheduler — {'ENABLED' if s['enabled'] else 'DISABLED'}",
             f"Auto: {'on' if s['auto'] else 'off'} · "
             f"Channel: {'<#' + s['channel'] + '>' if s['channel'] else '—'} · "
             f"Interval: {s['interval'] // 3600}h · Rotation: {rot}"]
    if v:
        m, sec = divmod(int(v['ends_in']), 60)
        title = v['name'] + (f" — {v['phase_name']}" if v.get('phase_name') else '')
        lines.append(f"Active: **{title}** {v['hp']}/{v['max_hp']} · "
                     f"ends in {m}m {sec}s · {v['participants']} hunters")
    else:
        lines.append('Active: none')
    _key, end, _status = _sched.last_event_info(gid)
    if end is None:
        lines.append('Next eligible: now')
    else:
        nxt = end + s['interval'] - now
        lines.append('Next eligible: ' + ('now' if nxt <= 0 else f'in {nxt // 3600}h '
                     f'{(nxt % 3600) // 60}m'))
    lines.append(f"Claims open: {'yes' if _sched.claims_open(gid) else 'no'}")
    tick_at = life.get('tick_at')
    lines.append(f"Last tick: {_ago(tick_at, now) if tick_at else 'never'}")
    if life.get('action'):
        lines.append(f"Last action: {life['action']}")
    if life.get('error'):
        lines.append(f"Last error: {life['error']}")
    return '\n'.join(lines)


def _dur(s) -> str:
    try:
        s = max(0, int(s or 0))
    except Exception:
        return '—'
    if s < 60:
        return f'{s}s'
    m, s = divmod(s, 60)
    if m < 60:
        return f'{m}m'
    h, m = divmod(m, 60)
    return f'{h}h {m}m'


def build_sim_text(rep: dict) -> str:
    """Compact balance report (headless-testable)."""
    if not rep.get('ok'):
        return f"Sim failed: {rep.get('code', 'ERROR')}."
    a = rep['assumptions']
    head = (f"**{rep['boss_name']}** sim — {a['fighters']}x {a['fighter']} "
            f"(HP {a['fighter_maxhp']}, teams of {a['team_size']}), "
            f"{a['raids']} raids, seed {a['seed']}, heal {a['heal_policy']}")
    if not rep['kills']:
        return (f"{head}\nNo kills: {rep['expiries']} expiries, "
                f"{rep['wipes']} wipes. Weaker profile or bigger team needed.")
    atk = rep['avg_attacks_to_kill']
    clr = _dur(rep['avg_clear_time_s'])
    lim = _dur(rep['duration_seconds'])
    phases = ', '.join(f'{k} {v * 100:.0f}%' for k, v in rep['phase_reach'].items())
    below = rep['below_threshold_pct']
    below_s = f"{below * 100:.0f}%" if below is not None else '—'
    loot = [f'{k} {v}/raid (bonus)' for k, v in rep['loot_guaranteed_per_raid'].items()]
    loot += [f'{k} {v}/raid' for k, v in rep['loot_weighted_per_raid'].items()]
    return '\n'.join([
        head,
        f"Kills {rep['kill_rate'] * 100:.0f}% ({rep['kills']}/{rep['raids']}) · "
        f"failed {rep['failed']} · avg {atk} attacks/kill "
        f"({rep['avg_attacks_all_raids']} all raids) · ~{clr} clear (limit {lim})",
        f"Faints {rep['avg_faints_per_raid']}/raid · "
        f"heals {rep['avg_heals_per_raid']}/raid · "
        f"{rep['avg_dmg_per_attack']} dmg/hit",
        f"Phase: {phases or '—'} · below {rep['participation_damage']}dmg: {below_s}",
        f"Loot: {'; '.join(loot) or '—'}",
    ])


def build_preview_text(p: dict) -> str:
    """Config dump (headless-testable)."""
    if not p.get('ok'):
        return f"Unknown boss: {p.get('code', 'ERROR')}."
    lines = [f"**{p['name']}** — {p['max_hp']} HP · {_dur(p['duration_seconds'])} · "
             f"{p['attack_cooldown_seconds']}s cooldown · "
             f"threshold {p['participation_damage']} dmg · "
             f"{p['participation_coins']}+{p['top_bonus_coins']} coins"
             + ('' if p['enabled'] else ' · DISABLED')]
    for ph in p['phases']:
        bits = [f">{ph['above_ratio'] * 100:.0f}%", f"x{ph['damage_mult']}"]
        if ph.get('defense_mult', 1.0) != 1.0:
            bits.append(f"hide x{ph['defense_mult']}")
        bits += list(ph['moves'])
        if ph['effect']:
            bits.append(ph['effect'])
        if ph['phase_reward']:
            bits.append(f"+{ph['phase_reward'][1]}x {ph['phase_reward'][0]}")
        lines.append(f"{ph['display_name']}: {', '.join(bits)}")
    loot = [f"{e['item']} {e['qty'][0]}-{e['qty'][1]} "
            f"{e['weight_share'] * 100:.0f}% (min {e['min_damage']}dmg)"
            for e in p['loot']]
    lines.append(f"Loot/roll: {'; '.join(loot)}")
    return '\n'.join(lines)


def _member_name(guild, uid: str) -> str:
    try:
        mbr = guild.get_member(int(uid)) if guild else None
        if mbr:
            return mbr.display_name
    except Exception:
        pass
    return f'<@{uid}>'


def build_history_text(guild, events, page: int, total_pages: int, gid) -> str:
    """Pure-ish history renderer (headless-testable with a stub guild)."""
    lines = [t(gid, 'eco.wb_hist_title')]
    for e in events:
        top = _member_name(guild, e.top[0].user_id) if e.top else '—'
        if e.outcome == 'active':
            lines.append(f"**{e.name}** — ACTIVE, {e.participants} hunters, top {top}")
        else:
            lines.append(f"**{e.name}** — {e.outcome.upper()}, {e.participants} hunters, "
                         f"{_dur(e.duration_s)}, top {top}")
    if total_pages > 1:
        lines.append(t(gid, 'eco.wb_hist_page', page=page + 1, total=total_pages))
    return '\n'.join(lines)


def build_leaderboard_text(guild, page_obj, gid) -> str:
    lines = [t(gid, 'eco.wb_lb_title', metric=page_obj.metric)]
    for e in page_obj.entries:
        lines.append(f"{e.rank}. {_member_name(guild, e.user_id)} — {e.value}")
    if page_obj.total_pages > 1:
        lines.append(t(gid, 'eco.wb_hist_page', page=page_obj.page + 1,
                       total=page_obj.total_pages))
    return '\n'.join(lines)


def build_stats_text(st, gid) -> str:
    avg_dur = _dur(st.avg_duration_s) if st.avg_duration_s is not None else '—'
    loot_bits = []
    for item, qty in (st.loot_guaranteed or ()):
        loot_bits.append(f'{item} (bonus) x{qty}')
    for item, qty in (st.loot_weighted or ()):
        loot_bits.append(f'{item} x{qty}')
    return '\n'.join([
        t(gid, 'eco.wb_stats_title'),
        t(gid, 'eco.wb_stats_events', total=st.total, defeated=st.defeated,
          expired=st.expired, rate=int(round(st.defeat_rate * 100))),
        t(gid, 'eco.wb_stats_avg', dur=avg_dur, parts=f'{st.avg_participants:.1f}',
          dpa=f'{st.avg_damage_per_attack:.1f}', claims=st.total_claims),
        t(gid, 'eco.wb_stats_fav', boss=st.most_used_boss or '—',
          phase=st.most_common_phase or '—'),
        t(gid, 'eco.wb_stats_loot', loot=', '.join(loot_bits) or '—'),
    ])


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

    @worldboss.command(name='rotation', description='Rotacja bossów (staff)')
    @staff_or('administrator')
    async def wb_rotation(self, ctx, *, bosses: str = ''):
        from tasks import worldboss_scheduler as _sched
        from tasks.worldboss_scheduler import _validate
        gid = ctx.guild.id
        if not (bosses or '').strip():
            cur = _sched.get_settings(gid)
            rot = ' '.join(cur['rotation']) if cur['rotation'] else '—'
            return await ctx.reply(t(gid, 'eco.wb_rotation_state', rotation=rot),
                                   ephemeral=True)
        ok, _norm = _validate('rotation', bosses)
        if not ok:
            return await ctx.reply(t(gid, 'eco.wb_rotation_bad'), ephemeral=True)
        _sched.set_setting(gid, 'rotation', bosses)
        cur = _sched.get_settings(gid)
        await ctx.reply(t(gid, 'eco.wb_rotation_set',
                          rotation=' → '.join(cur['rotation'])), ephemeral=True)

    @worldboss.command(name='interval', description='Interwał rajdu (staff)')
    @staff_or('administrator')
    async def wb_interval(self, ctx, span: str = ''):
        from tasks import worldboss_scheduler as _sched
        from tasks.worldboss_scheduler import _validate
        gid = ctx.guild.id
        if not (span or '').strip():
            cur = _sched.get_settings(gid)
            return await ctx.reply(t(gid, 'eco.wb_interval_state',
                                     h=cur['interval'] // 3600), ephemeral=True)
        ok, _norm = _validate('interval', span)
        if not ok:
            return await ctx.reply(t(gid, 'eco.wb_interval_bad'), ephemeral=True)
        _sched.set_setting(gid, 'interval', span)
        cur = _sched.get_settings(gid)
        await ctx.reply(t(gid, 'eco.wb_interval_set', h=cur['interval'] // 3600),
                        ephemeral=True)

    @worldboss.command(name='config', description='Status schedulera (staff)')
    @staff_or('administrator')
    async def wb_config(self, ctx):
        import time as _t
        await ctx.reply(build_config_text(ctx.guild.id, int(_t.time())), ephemeral=True)

    @worldboss.command(name='scheduler', description='Zdrowie schedulera (staff)')
    @staff_or('administrator')
    async def wb_scheduler(self, ctx):
        import time as _t
        from tasks import worldboss_scheduler as _sched
        gid, now = ctx.guild.id, int(_t.time())
        life = _sched.lifecycle_info(gid)
        lines = ['Scheduler tick health']
        tick_at = life.get('tick_at')
        lines.append(f"Last tick: {_ago(tick_at, now) if tick_at else 'never'}")
        lines.append(f"Last action: {life.get('action') or '—'}")
        lines.append(f"Last error: {life.get('error') or '—'}")
        await ctx.reply('\n'.join(lines), ephemeral=True)

    @worldboss.command(name='tick', description='Ręczny przebieg (staff)')
    @staff_or('administrator')
    async def wb_tick(self, ctx):
        import time as _t
        from tasks import worldboss_scheduler as _sched
        _tick, acts = _sched.run_pass([ctx.guild], int(_t.time()))
        bits = []
        for a in acts:
            bit = f"{a.action}:{a.code}"
            if a.boss_key:
                bit += f":{a.boss_key}"
            bits.append(bit)
        await ctx.reply(t(ctx.guild.id, 'eco.wb_tick_done',
                          summary=', '.join(bits) or 'quiet'), ephemeral=True)

    @worldboss.command(name='history', description='Historia rajdów')
    async def wb_history(self, ctx, page: str = ''):
        from services import worldboss_history as _hist
        gid = ctx.guild.id
        try:
            p = max(0, int((page or '1').strip()) - 1)
        except Exception:
            p = 0
        per = _hist.HISTORY_PAGE_SIZE
        total = _hist.count_events(gid)
        total_pages = max(1, (total + per - 1) // per)
        p = min(p, total_pages - 1)
        events = _hist.recent_events(gid, limit=per, offset=p * per)
        if not events:
            return await ctx.reply(t(gid, 'eco.wb_hist_empty'), mention_author=False)
        await ctx.reply(build_history_text(ctx.guild, events, p, total_pages, gid),
                        mention_author=False)

    @worldboss.command(name='stats', description='Statystyki rajdów (staff)')
    @staff_or('administrator')
    async def wb_stats(self, ctx):
        import time as _t
        from services import worldboss_history as _hist
        gid = ctx.guild.id
        st = _hist.statistics(gid, now=int(_t.time()))
        if not st.total:
            return await ctx.reply(t(gid, 'eco.wb_stats_empty'), ephemeral=True)
        await ctx.reply(build_stats_text(st, gid), ephemeral=True)

    @worldboss.command(name='leaderboard', aliases=['lb'],
                       description='Ranking rajdowy')
    async def wb_leaderboard(self, ctx, metric: str = 'damage', page: str = ''):
        from services import worldboss_history as _hist
        gid = ctx.guild.id
        m = (metric or 'damage').lower().strip()
        pg_raw = (page or '').strip()
        # `;worldboss leaderboard 2` means page 2 of the default metric.
        if m.isdigit() and not pg_raw:
            pg_raw, m = m, 'damage'
        if m not in ('damage', 'wins', 'participation'):
            return await ctx.reply(t(gid, 'eco.wb_lb_use'), ephemeral=True)
        try:
            p = max(0, int(pg_raw or '1') - 1)
        except Exception:
            p = 0
        board = _hist.leaderboard(gid, m, page=p)
        if not board.entries:
            return await ctx.reply(t(gid, 'eco.wb_lb_empty'), mention_author=False)
        await ctx.reply(build_leaderboard_text(ctx.guild, board, gid),
                        mention_author=False)

    @worldboss.command(name='use', description='Użyj charm (heal/focus/rally)')
    async def wb_use(self, ctx, *, item: str = ''):
        from services import worldboss_service as _wb
        gid = ctx.guild.id
        key = (item or '').strip().lower().replace(' ', '_').replace('-', '_')
        key = {'volcanic': 'volcanic_charm', 'tidal': 'tidal_charm',
               'raid': 'raid_charm'}.get(key, key)
        if key == 'volcanic_charm':
            res = _wb.heal_fighter(gid, ctx.author.id, use_charm=True)
        elif key == 'tidal_charm':
            res = _wb.reset_cooldown(gid, ctx.author.id)
        elif key == 'raid_charm':
            res = _wb.use_raid_charm(gid, ctx.author.id)
        else:
            return await ctx.reply(t(gid, 'eco.wb_use_unknown'), ephemeral=True)
        await ctx.reply(res['message'], mention_author=False)

    @worldboss.command(name='sim', description='Symulacja balansu (staff)')
    @staff_or('administrator')
    async def wb_sim(self, ctx, boss: str = '', fighters: str = '',
                     level: str = '', raids: str = ''):
        from services import boss_simulator as _sim
        key = (boss or '').strip().lower()
        if not key:
            return await ctx.reply('Usage: `;worldboss sim <boss> [fighters] [level] [raids]` '
                                   '(e.g. `;worldboss sim tidecaller 5 100 200`).',
                                   ephemeral=True)

        def _num(raw, default, lo, hi):
            try:
                return max(lo, min(hi, int((raw or '').strip() or default)))
            except Exception:
                return default

        n, lv, r = _num(fighters, 5, 1, 20), _num(level, 100, 1, 100), _num(raids, 200, 1, 1000)
        try:
            rep = await self.bot.loop.run_in_executor(
                None, _sim.simulate_raids, key, 133, lv, n, 6, r, 7, 'none',
                str(ctx.guild.id))
        except Exception as e:
            return await ctx.reply(f'Sim failed: {type(e).__name__}.', ephemeral=True)
        await ctx.reply(build_sim_text(rep), mention_author=False)

    @worldboss.command(name='preview', description='Podgląd configu bossa (staff)')
    @staff_or('administrator')
    async def wb_preview(self, ctx, boss: str = ''):
        from services import boss_simulator as _sim
        key = (boss or '').strip().lower()
        if not key:
            return await ctx.reply('Usage: `;worldboss preview <boss>`.', ephemeral=True)
        await ctx.reply(build_preview_text(_sim.preview_boss(key)), mention_author=False)

    @worldboss.command(name='forceexpire', description='Zakończ event (staff)')
    @staff_or('administrator')
    async def wb_forceexpire(self, ctx, confirm: str = ''):
        import time as _t
        from services import worldboss_service as _wb
        gid = ctx.guild.id
        if (confirm or '').lower() != 'confirm':
            v = _wb.boss_status(gid, int(_t.time()))
            if not v:
                return await ctx.reply(t(gid, 'eco.wb_force_none'), ephemeral=True)
            return await ctx.reply(t(gid, 'eco.wb_force_warn', name=v['name']),
                                   ephemeral=True)
        res = _wb.expire_event(gid, int(_t.time()))
        if res['ok']:
            return await ctx.reply(t(gid, 'eco.wb_force_done'), ephemeral=True)
        return await ctx.reply(t(gid, 'eco.wb_force_none'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(WorldBoss(bot))

"""Discord Scheduled Events — the world-boss raid, on the guild calendar.

The boss scheduler (tasks/worldboss_scheduler.py) owns the real schedule:
a rotation of bosses, one raid every `interval` hours after the last one
ends. This cog MIRRORS that schedule into real Discord Scheduled Events,
so the guild calendar shows the next raids and Discord itself sends the
"event starting" notification. Staff-created events live in the same
table, and a 15-minute reconciler re-creates whatever disappeared and
clears whatever is over.

The mirror is stored in `sched_events` (discord id -> own event key), so
a restart does not lose the mapping: the reconciler runs at boot and
rebuilds the calendar from the table.
"""
import re
import time
import unicodedata
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.checks import staff_or
from utils.ui import ACCENT, divider, panel, stat

from cogs.polls import load_extra_lang

load_extra_lang()

DDL_SCHED_EVENTS = '''
CREATE TABLE IF NOT EXISTS sched_events (
    guild_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    discord_id TEXT,
    name TEXT,
    description TEXT,
    start_ts INTEGER,
    end_ts INTEGER,
    location TEXT,
    created_at INTEGER,
    PRIMARY KEY (guild_id, event_key))'''

DDL_EVENTS_CFG = '''
CREATE TABLE IF NOT EXISTS events_cfg (
    guild_id TEXT PRIMARY KEY,
    raid_mode TEXT DEFAULT 'auto',
    raid_weekday INTEGER,
    raid_hour INTEGER,
    raid_minute INTEGER,
    created_at INTEGER)'''

# Python weekday: Monday = 0 ... Sunday = 6. English and Polish names.
DAY_MAP = {
    'monday': 0, 'mon': 0, 'poniedzialek': 0, 'pon': 0,
    'tuesday': 1, 'tue': 1, 'wtorek': 1, 'wt': 1,
    'wednesday': 2, 'wed': 2, 'sroda': 2, 'sr': 2,
    'thursday': 3, 'thu': 3, 'czwartek': 3, 'czw': 3,
    'friday': 4, 'fri': 4, 'piatek': 4, 'pt': 4,
    'saturday': 5, 'sat': 5, 'sobota': 5, 'sob': 5,
    'sunday': 6, 'sun': 6, 'niedziela': 6, 'niedz': 6,
}
DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
UNIT = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
RAID_SLOTS = 3


def ensure_tables() -> None:
    """The event mirror and its config. Created here, not in database.py."""
    with db.conn_ctx() as conn:
        conn.execute(DDL_SCHED_EVENTS)
        conn.execute(DDL_EVENTS_CFG)


# ---------- small helpers ----------------------------------------------

def _norm(text: str) -> str:
    """Lowercase, without diacritics — 'środa' -> 'sroda'."""
    out = []
    for ch in (text or '').strip().lower():
        for piece in unicodedata.normalize('NFKD', ch):
            if not unicodedata.combining(piece):
                out.append(piece)
    return ''.join(out)


def local_tz():
    return datetime.now().astimezone().tzinfo


def now_local() -> datetime:
    return datetime.now().astimezone()


def can_manage(guild) -> bool:
    """Does the bot hold Manage Events here?"""
    me = getattr(guild, 'me', None)
    if me is None:
        return False
    try:
        return bool(me.guild_permissions.manage_events)
    except Exception:
        return False


def status_name(ev) -> str:
    return str(getattr(ev, 'status', '') or '').split('.')[-1].lower()


def http_text(e) -> str:
    return (getattr(e, 'text', '') or '')[:120]


# ---------- when parsing -------------------------------------------------

def parse_clock(raw: str):
    """'20:00' / '20.00' / '8pm' / '20h' -> (hour, minute) or None."""
    s = (raw or '').strip().lower().replace('.', ':')
    s = re.sub(r'\s*(godz|h)\s*$', '', s).strip()
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r'^(\d{1,2})\s*(am|pm)?$', s)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
        ap = m.group(2)
        if ap == 'pm' and h < 12:
            h += 12
        if ap == 'am' and h == 12:
            h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def next_weekday(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """The next occurrence of that weekday at that time (local time)."""
    days = (weekday - now.weekday()) % 7
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def _mk(now: datetime, year, month, day, hour, minute):
    try:
        return now.replace(year=int(year), month=int(month), day=int(day),
                           hour=int(hour), minute=int(minute), second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


def parse_when(raw: str, now: datetime = None):
    """'2026-10-25 20:00' | '25.10.2026 20:00' | 'in 3h' | 'sunday 20:00'
    | 'tomorrow 18:00' | '20:00' -> aware datetime (server local) or None."""
    s = ' '.join((raw or '').strip().split())
    if not s:
        return None
    now = now or now_local()
    low = _norm(s)

    m = re.match(r'^(?:in |za |plus |\+)?(\d+)\s*(s|m|h|d|w)$', low)
    if m:
        return now + timedelta(seconds=int(m.group(1)) * UNIT[m.group(2)])

    m = re.match(r'^(tomorrow|jutro)\s+(\d{1,2})[:.](\d{2})$', low)
    if m:
        return (now + timedelta(days=1)).replace(hour=int(m.group(2)),
                                                minute=int(m.group(3)),
                                                second=0, microsecond=0)

    m = re.match(r'^([a-z]{3,12})\s+(\d{1,2}[:.]?\d{0,2}\s*(?:am|pm)?)$', low)
    if m:
        weekday = DAY_MAP.get(m.group(1))
        clock = parse_clock(m.group(2))
        if weekday is None or clock is None:
            return None
        return next_weekday(now, weekday, clock[0], clock[1])

    m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ t](\d{1,2})[:.](\d{2}))?$', s)
    if m:
        return _mk(now, m.group(1), m.group(2), m.group(3), m.group(4) or 0, m.group(5) or 0)

    m = re.match(r'^(\d{1,2})[./](\d{1,2})[./](\d{4})(?:[ ,](\d{1,2})[:.](\d{2}))?$', s)
    if m:
        return _mk(now, m.group(3), m.group(2), m.group(1), m.group(4) or 0, m.group(5) or 0)

    m = re.match(r'^(\d{1,2}[:.]\d{2})$', s)
    if m:
        clock = parse_clock(m.group(1))
        if clock is None:
            return None
        cand = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    return None


# ---------- storage -------------------------------------------------------

def cfg(gid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM events_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        if not row:
            conn.execute('INSERT OR REPLACE INTO events_cfg (guild_id) VALUES (?)', (str(gid),))
            row = conn.execute('SELECT * FROM events_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        return dict(row) if row else {'raid_mode': 'auto'}


def set_cfg(gid, **fields) -> None:
    allowed = ('raid_mode', 'raid_weekday', 'raid_hour', 'raid_minute')
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO events_cfg (guild_id) VALUES (?)', (str(gid),))
        cols = ', '.join(f'{k}=?' for k in sets)
        args = list(sets.values()) + [str(gid)]
        conn.execute(f'UPDATE events_cfg SET {cols} WHERE guild_id=?', tuple(args))


def event_rows(gid):
    with db.conn_ctx() as conn:
        return [dict(r) for r in
                conn.execute('SELECT * FROM sched_events WHERE guild_id=?', (str(gid),)).fetchall()]


def drop_key(gid, key) -> None:
    with db.conn_ctx() as conn:
        conn.execute('DELETE FROM sched_events WHERE guild_id=? AND event_key=?',
                     (str(gid), str(key)))


def drop_discord_id(gid, discord_id) -> None:
    with db.conn_ctx() as conn:
        conn.execute('DELETE FROM sched_events WHERE guild_id=? AND discord_id=?',
                     (str(gid), str(discord_id)))


def set_discord_id(gid, key, discord_id) -> None:
    with db.conn_ctx() as conn:
        conn.execute('UPDATE sched_events SET discord_id=? WHERE guild_id=? AND event_key=?',
                     (str(discord_id), str(gid), str(key)))


def save_event(gid, key, discord_id, name, description, start_ts, end_ts, location) -> None:
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO sched_events (guild_id, event_key, discord_id, name, '
            'description, start_ts, end_ts, location, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (str(gid), str(key), str(discord_id) if discord_id else None, name, description,
             int(start_ts), int(end_ts), location, int(time.time())))


# ---------- the boss schedule -------------------------------------------

def boss_settings(gid) -> dict:
    try:
        from tasks import worldboss_scheduler as sched
        return sched.get_settings(gid)
    except Exception:
        return {'enabled': True, 'auto': True, 'rotation': ('dreadmaw', 'tidecaller'),
                'interval': 21600, 'duration': None, 'channel': ''}


def last_raid(gid):
    try:
        from tasks import worldboss_scheduler as sched
        return sched.last_event_info(gid)
    except Exception:
        return None, None, None


def rotation_defs(rotation):
    try:
        from game.bosses.registry import registry
    except Exception:
        return []
    out = []
    for key in rotation or ():
        defn = registry.get(key)
        if defn is not None:
            out.append(defn)
    return out


def next_defn(defs, last_key):
    """First enabled boss after last_key, wrapping — the same walk the
    scheduler does, so the calendar matches the real raids."""
    if not defs:
        return None
    keys = [d.key for d in defs]
    try:
        start = keys.index((last_key or '').lower()) + 1
    except ValueError:
        start = 0
    for i in range(len(defs)):
        cand = defs[(start + i) % len(defs)]
        if cand.enabled:
            return cand
    return None


def raid_slots(gid, now: datetime, config: dict, count: int = RAID_SLOTS):
    """The next raid slots — [(start, end, boss_key, boss_name)].

    'weekly' mode pins the raid to a weekday and time ('Sunday 20:00');
    'auto' mode follows the boss scheduler (interval after the last
    raid); 'off' schedules nothing.
    """
    mode = (config.get('raid_mode') or 'auto').lower()
    if mode == 'off':
        return []
    settings = boss_settings(gid)
    if mode != 'weekly' and not settings.get('enabled', True):
        return []   # the boss scheduler is switched off — nothing to mirror
    defs = rotation_defs(settings.get('rotation') or ())
    key, end_ts, _status = last_raid(gid)
    duration = settings.get('duration')
    if mode == 'weekly' and config.get('raid_weekday') is not None:
        weekday = int(config['raid_weekday'])
        hour = int(config.get('raid_hour') or 0)
        minute = int(config.get('raid_minute') or 0)
        start = next_weekday(now, weekday, hour, minute)
        step = timedelta(days=7)
    else:
        interval = max(3600, int(settings.get('interval') or 21600))
        base = int(end_ts) + interval if end_ts else int(now.timestamp()) + 300
        if base <= int(now.timestamp()):
            base = int(now.timestamp()) + 300
        start = datetime.fromtimestamp(base, tz=now.tzinfo)
        step = timedelta(seconds=interval)
    slots = []
    last_key = key
    for _ in range(max(1, count)):
        defn = next_defn(defs, last_key)
        if defn is None:
            break
        length = int(duration or defn.duration_seconds)
        slots.append({'start': start, 'end': start + timedelta(seconds=length),
                      'boss_key': defn.key, 'boss_name': defn.display_name})
        last_key = defn.key
        start = start + step
    return slots


class Events(commands.Cog):
    """Scheduled events: the raid mirror plus staff-made events."""

    def __init__(self, bot):
        self.bot = bot
        try:
            ensure_tables()
        except Exception as e:
            print(f'[events] table init failed: {type(e).__name__}: {e}')
        try:
            self.reconciler.start()
        except Exception:
            pass

    def cog_unload(self):
        try:
            self.reconciler.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=15)
    async def reconciler(self):
        """Rebuild the calendar from the table every 15 minutes."""
        await self.bot.wait_until_ready()
        for guild in list(getattr(self.bot, 'guilds', []) or []):
            try:
                await self.reconcile_guild(guild)
            except Exception as e:
                print(f'[events] reconcile failed in {guild.id}: {type(e).__name__}: {e}')

    @reconciler.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- discord calls ----------

    async def _create(self, guild, name, description, start_ts, end_ts, location):
        """Create the real ScheduledEvent. Returns (event, error_text)."""
        tz = local_tz()
        try:
            start = datetime.fromtimestamp(int(start_ts), tz=tz)
            end = datetime.fromtimestamp(int(end_ts), tz=tz) if end_ts \
                else start + timedelta(hours=2)
            ev = await guild.create_scheduled_event(
                name=(name or 'Event')[:100],
                description=(description or '')[:100],
                start_time=start,
                end_time=end,
                entity_type=discord.EntityType.external,
                location=(location or 'Babka Danka')[:100],
                reason='Babka Danka: scheduled event')
            return ev, None
        except discord.HTTPException as e:
            return None, t(getattr(guild, 'id', None), 'events.http_fail',
                           code=e.status, text=http_text(e))
        except Exception as e:
            return None, f'{type(e).__name__}: {e}'

    async def _delete(self, ev) -> bool:
        try:
            await ev.delete(reason='Babka Danka: event removed')
            return True
        except discord.HTTPException:
            return False
        except Exception:
            return False

    async def _live_events(self, guild):
        """The guild's scheduled events — REST first, cache as a fallback.

        The cache can be empty (or stale) when the bot reconnects, and a
        wrong answer here means duplicate events on the calendar.
        """
        try:
            return await guild.fetch_scheduled_events(with_counts=False)
        except Exception:
            try:
                return list(guild.scheduled_events)
            except Exception:
                return []

    def _raid_event_parts(self, guild, gid, slot):
        name = f'World Boss Raid — {slot["boss_name"]}'
        settings = boss_settings(gid)
        channel_id = settings.get('channel') or ''
        channel = None
        if channel_id:
            try:
                channel = guild.get_channel(int(channel_id))
            except Exception:
                channel = None
        where = f'#{channel.name}' if channel is not None else ''
        desc = f'Shared-HP raid. Boss: {slot["boss_name"]}.'
        if where:
            desc += f' Raid channel: {where}.'
        desc += ' Join with .worldboss join.'
        location = t(gid, 'events.default_location')
        return name[:100], desc[:100], location[:100]

    async def reconcile_guild(self, guild) -> dict:
        """Make Discord and the table agree again."""
        gid = guild.id
        out = {'created': 0, 'removed': 0, 'kept': 0, 'skipped': None}
        if not can_manage(guild):
            out['skipped'] = 'no-permission'
            return out
        now = int(time.time())
        rows = event_rows(gid)
        keys = {r['event_key'] for r in rows}
        live = {}
        try:
            for ev in await self._live_events(guild):
                live[int(ev.id)] = ev
        except Exception:
            pass
        for row in rows:
            ev = live.get(int(row['discord_id'])) if row.get('discord_id') else None
            if ev is not None:
                end_ts = int(ev.end_time.timestamp()) if ev.end_time else 0
                if end_ts and end_ts < now:
                    if await self._delete(ev):
                        out['removed'] += 1
                    drop_key(gid, row['event_key'])
                    keys.discard(row['event_key'])
                    continue
                out['kept'] += 1
                continue
            start_ts = int(row.get('start_ts') or 0)
            if start_ts > now:
                ev, err = await self._create(guild, row.get('name'),
                                            row.get('description') or '', start_ts,
                                            int(row.get('end_ts') or 0),
                                            row.get('location') or '')
                if ev is not None:
                    set_discord_id(gid, row['event_key'], ev.id)
                    out['created'] += 1
                    continue
                if err:
                    print(f'[events] re-create failed in {gid}: {err}')
            drop_key(gid, row['event_key'])
            keys.discard(row['event_key'])
            out['removed'] += 1
        # the raid mirror
        for slot in raid_slots(gid, now_local(), cfg(gid)):
            key = f'raid:{int(slot["start"].timestamp())}'
            if key in keys:
                continue
            name, desc, location = self._raid_event_parts(guild, gid, slot)
            ev, err = await self._create(guild, name, desc,
                                        int(slot['start'].timestamp()),
                                        int(slot['end'].timestamp()), location)
            if ev is None:
                if err:
                    print(f'[events] raid event failed in {gid}: {err}')
                continue
            save_event(gid, key, ev.id, name, desc,
                       int(slot['start'].timestamp()), int(slot['end'].timestamp()), location)
            keys.add(key)
            out['created'] += 1
        return out

    # ---------- commands ----------

    @commands.group(name='events', description='Wydarzenia serwera')
    async def events(self, ctx):
        await ctx.reply(t(ctx.guild.id, 'events.help'), ephemeral=True)

    @events.command(name='list', description='Nadchodzące wydarzenia')
    async def events_list(self, ctx):
        gid = ctx.guild.id
        now = now_local()
        items = []
        for ev in await self._live_events(ctx.guild):
            if ev.start_time is None:
                continue
            if ev.start_time < now - timedelta(hours=6):
                continue
            items.append(ev)
        items.sort(key=lambda e: e.start_time)
        if not items:
            return await ctx.reply(t(gid, 'events.list_empty'), ephemeral=True)
        lines = []
        for ev in items:
            status = ('events.status_active' if status_name(ev) == 'active'
                      else 'events.status_scheduled')
            lines.append(t(gid, 'events.list_item', name=ev.name,
                           when=ev.start_time.strftime('%Y-%m-%d %H:%M'),
                           status=t(gid, status), id=ev.id))
        body = stat(t(gid, 'events.list_count'), len(items)) + divider() + '\n'.join(lines)
        await ctx.reply(view=panel(t(gid, 'events.list_title'), body, accent=ACCENT['default']),
                        mention_author=False)

    @events.command(name='create', description='Nowe wydarzenie (staff)')
    @staff_or('manage_guild')
    async def events_create(self, ctx, *, raw: str = ''):
        gid = ctx.guild.id
        try:
            parts = [p.strip() for p in (raw or '').split('|') if p.strip()]
            if len(parts) < 2:
                return await ctx.reply(t(gid, 'events.create_usage'), ephemeral=True)
            name, when = parts[0], parts[1]
            desc = parts[2] if len(parts) > 2 else ''
            location = parts[3] if len(parts) > 3 else t(gid, 'events.default_location')
            start = parse_when(when)
            if start is None:
                return await ctx.reply(t(gid, 'events.when_bad'), ephemeral=True)
            if start <= now_local():
                return await ctx.reply(t(gid, 'events.past'), ephemeral=True)
            if not can_manage(ctx.guild):
                return await ctx.reply(t(gid, 'events.no_perm'), ephemeral=True)
            end = start + timedelta(hours=2)
            ev, err = await self._create(ctx.guild, name, desc,
                                        int(start.timestamp()), int(end.timestamp()),
                                        location)
            if ev is None:
                return await ctx.reply(t(gid, 'events.failed') if not err else err,
                                       ephemeral=True)
            key = f'manual:{int(start.timestamp())}:{ev.id}'
            save_event(gid, key, ev.id, ev.name, desc[:100],
                       int(start.timestamp()), int(end.timestamp()), location[:100])
            await ctx.reply(t(gid, 'events.created', name=ev.name,
                              when=start.strftime('%Y-%m-%d %H:%M'), id=ev.id),
                            ephemeral=True)
        except Exception:
            await ctx.reply(t(gid, 'events.failed'), ephemeral=True)

    @events.command(name='delete', description='Usuń wydarzenie (staff)')
    @staff_or('manage_guild')
    async def events_delete(self, ctx, event_id: str = ''):
        gid = ctx.guild.id
        raw = (event_id or '').strip()
        if not raw:
            return await ctx.reply(t(gid, 'events.delete_none'), ephemeral=True)
        ev = None
        live = await self._live_events(ctx.guild)
        if raw.isdigit():
            for candidate in live:
                if str(candidate.id) == raw:
                    ev = candidate
                    break
        if ev is None:
            for row in event_rows(gid):
                if row['event_key'] == raw or str(row.get('discord_id') or '') == raw:
                    for candidate in live:
                        if str(candidate.id) == str(row.get('discord_id')):
                            ev = candidate
                            break
                    break
        if ev is None:
            return await ctx.reply(t(gid, 'events.delete_none'), ephemeral=True)
        if not can_manage(ctx.guild):
            return await ctx.reply(t(gid, 'events.no_perm'), ephemeral=True)
        try:
            await ev.delete(reason='Babka Danka: event removed')
        except discord.HTTPException as e:
            return await ctx.reply(t(gid, 'events.http_fail', code=e.status,
                                     text=http_text(e)), ephemeral=True)
        except Exception:
            return await ctx.reply(t(gid, 'events.delete_none'), ephemeral=True)
        drop_discord_id(gid, ev.id)
        await ctx.reply(t(gid, 'events.deleted', name=ev.name, id=ev.id), ephemeral=True)

    @events.command(name='raid', description='Slot rajdu (staff)')
    @staff_or('manage_guild')
    async def events_raid(self, ctx, *, args: str = ''):
        gid = ctx.guild.id
        raw = ' '.join((args or '').strip().split())
        config = cfg(gid)
        if not raw:
            mode = (config.get('raid_mode') or 'auto').lower()
            label = t(gid, f'events.mode_{mode}' if mode in ('auto', 'weekly', 'off')
                      else 'events.mode_auto')
            body = t(gid, 'events.raid_state', mode=label)
            slots = raid_slots(gid, now_local(), config)
            if slots:
                for slot in slots:
                    body += '\n' + t(gid, 'events.raid_slot',
                                     when=slot['start'].strftime('%Y-%m-%d %H:%M'),
                                     boss=slot['boss_name'])
            else:
                body += '\n' + t(gid, 'events.raid_none')
            return await ctx.reply(view=panel(t(gid, 'events.raid_title'), body,
                                              accent=ACCENT['default']), ephemeral=True)
        low = _norm(raw)
        if low in ('off', 'wylacz', 'none'):
            set_cfg(gid, raid_mode='off')
            await self.reconcile_guild(ctx.guild)
            return await ctx.reply(t(gid, 'events.raid_off'), ephemeral=True)
        if low in ('auto', 'scheduler', 'harmonogram'):
            set_cfg(gid, raid_mode='auto')
            await self.reconcile_guild(ctx.guild)
            return await ctx.reply(t(gid, 'events.raid_auto'), ephemeral=True)
        parts = raw.split()
        if len(parts) >= 2:
            weekday = DAY_MAP.get(_norm(parts[0]))
            clock = parse_clock(' '.join(parts[1:]))
            if weekday is not None and clock is not None:
                set_cfg(gid, raid_mode='weekly', raid_weekday=weekday,
                        raid_hour=clock[0], raid_minute=clock[1])
                await self.reconcile_guild(ctx.guild)
                return await ctx.reply(t(gid, 'events.raid_set',
                                         day=DAY_NAMES[weekday],
                                         time=f'{clock[0]:02d}:{clock[1]:02d}'),
                                       ephemeral=True)
        await ctx.reply(t(gid, 'events.raid_bad'), ephemeral=True)

    @events.command(name='sync', description='Zsynchronizuj wydarzenia (staff)')
    @staff_or('manage_guild')
    async def events_sync(self, ctx):
        gid = ctx.guild.id
        try:
            result = await self.reconcile_guild(ctx.guild)
        except Exception:
            return await ctx.reply(t(gid, 'events.failed'), ephemeral=True)
        if result.get('skipped') == 'no-permission':
            return await ctx.reply(t(gid, 'events.no_perm'), ephemeral=True)
        await ctx.reply(t(gid, 'events.synced', created=result['created'],
                          removed=result['removed'], kept=result['kept']), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Events(bot))

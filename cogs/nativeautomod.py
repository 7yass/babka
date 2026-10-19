"""Native Discord AutoMod — rules Discord enforces for us, bot online or not.

The hand-rolled automod (cogs/automod.py) only sees messages while the bot
is online. Discord's own AutoMod rules are enforced by Discord's servers, so
they keep working when the bot is down, they cost nothing per message, and
they show up in the server settings where admins expect to find them.

This cog manages those native rules:
  .automod native              list the rules Discord is enforcing
  .automod native add KIND     create one (keyword/preset/mention-spam/harmful-link)
  .automod native remove ID    delete a rule
  .automod native sync         reconcile native rules with the bot's own config

The mapping is explicit and reversible: every rule this cog owns is tracked
in the native_automod table, so `sync` can update or delete exactly what it
created and never touches a rule an admin made by hand.
"""
import asyncio
import datetime
import json
import re
import time

import discord
import discord.automod as da
from discord.ext import commands, tasks

import database as db
from cogs.automod import _apply_timeout, _bump_strike, _reset_strikes, _strikecfg
from lang import t
from utils.checks import staff_or
from utils.common import log_to_mod
from utils.embeds import DARK_RED, build, say
from utils.ui import ACCENT, divider, panel, stat

# The automod config keys that get a native rule each.
MIRROR_KEYS = ('anti_invite', 'anti_link', 'badwords')

# The bot's own invite/link patterns, mirrored as native keyword filters.
INVITE_KEYWORDS = ['discord.gg', 'discordapp.com/invite', 'discord.com/invite']
LINK_KEYWORDS = ['http://', 'https://']

# Discord's own limits for a keyword filter.
KEYWORD_LIMIT = 1000       # max keywords per rule
KEYWORD_CHARS = 60         # max characters per keyword
MENTION_LIMIT_MAX = 50     # max mentions per message
MENTION_LIMIT_DEFAULT = 5  # the bot's own "more than 5 mentions" threshold
PRESET_NAMES = ('profanity', 'sexual_content', 'slurs')

# guild_id, key -> the Discord rule id this cog owns for that automod key.
DDL = """CREATE TABLE IF NOT EXISTS native_automod (
    guild_id TEXT NOT NULL,
    key TEXT NOT NULL,
    rule_id TEXT,
    updated_at INTEGER,
    PRIMARY KEY (guild_id, key)
)"""

_MENTION_RE = re.compile(r'<@!?(\d+)>')
_CHANNEL_RE = re.compile(r'<#(\d+)>')


# ---------- db helpers ----------
def _ensure_table():
    """The cog owns its table (same pattern as cogs/pokemon.py)."""
    with db.conn_ctx() as conn:
        conn.execute(DDL)


def _managed(gid) -> dict:
    """{config key: rule id} for the rules this cog owns in this guild."""
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT key, rule_id FROM native_automod WHERE guild_id=?',
                            (str(gid),)).fetchall()
    return {r['key']: r['rule_id'] for r in rows}


def _set_managed(gid, key, rule_id):
    with db.conn_ctx() as conn:
        conn.execute(
            'INSERT INTO native_automod (guild_id, key, rule_id, updated_at) VALUES (?,?,?,?) '
            'ON CONFLICT(guild_id, key) DO UPDATE SET rule_id=excluded.rule_id, '
            'updated_at=excluded.updated_at',
            (str(gid), key, str(rule_id) if rule_id else None, int(time.time())))


def _clear_managed(gid, rule_id):
    with db.conn_ctx() as conn:
        conn.execute('UPDATE native_automod SET rule_id=NULL, updated_at=? '
                     'WHERE guild_id=? AND rule_id=?', (int(time.time()), str(gid), str(rule_id)))


def _configured_guilds() -> list:
    """Guilds that opted into the native layer (we own a rule slot there)."""
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT DISTINCT guild_id FROM native_automod').fetchall()
    return [int(r['guild_id']) for r in rows]


def _automod_cfg(gid):
    """The bot's own automod config for this guild, read straight from the DB.

    Unlike cogs/automod.py's cached _cfg() this never creates a row, so a
    guild that never touched the hand-rolled automod stays untouched.
    """
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT enabled, anti_invite, anti_link, badwords, '
                            'whitelist_channels, whitelist_roles '
                            'FROM automod WHERE guild_id=?', (str(gid),)).fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg['badwords'] = _json_list(cfg.get('badwords'))
    cfg['whitelist_channels'] = _json_list(cfg.get('whitelist_channels'))
    cfg['whitelist_roles'] = _json_list(cfg.get('whitelist_roles'))
    return cfg


def _json_list(raw) -> list:
    try:
        data = json.loads(raw or '[]')
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x or '').strip()]


def _feature_on(cfg, key) -> bool:
    """Is this automod feature switched on in the bot's own config?"""
    if not cfg:
        return False
    if key == 'badwords':
        return bool(cfg.get('badwords'))
    return bool(cfg.get(key))


# ---------- rule building ----------
def _clean_keywords(words) -> list:
    """Trim a word list to what Discord accepts, keeping the order."""
    out, seen = [], set()
    for raw in words or []:
        w = str(raw or '').strip()
        if not w:
            continue
        w = w[:KEYWORD_CHARS]
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
    return out[:KEYWORD_LIMIT]


def _modlog_channel(gid):
    """The guild's mod-log channel id, if it has one."""
    settings = db.get_settings(gid)
    ch = settings.get('modlog_channel')
    try:
        return int(ch) if ch else None
    except (TypeError, ValueError):
        return None


def _actions(gid, guild, timeout_minutes=None) -> list:
    """The bot's own punishment, expressed as native AutoMod actions.

    block_message  = the bot deleting the message
    send_alert     = the bot writing the hit to the mod-log (and the cue the
                     bot uses to run its own strike flow)
    timeout        = the bot's strike timeout, enforced by Discord itself
    """
    acts = [da.AutoModRuleAction(type=da.AutoModRuleActionType.block_message,
                                 custom_message=t(gid, 'na.block_msg'))]
    ch = _modlog_channel(gid)
    if ch:
        acts.append(da.AutoModRuleAction(type=da.AutoModRuleActionType.send_alert_message,
                                         channel_id=ch))
    if timeout_minutes and _can_timeout(guild):
        acts.append(da.AutoModRuleAction(type=da.AutoModRuleActionType.timeout,
                                         duration=datetime.timedelta(minutes=timeout_minutes)))
    return acts


def _can_timeout(guild) -> bool:
    me = getattr(guild, 'me', None)
    return bool(me and me.guild_permissions.moderate_members)


def _can_manage(guild) -> bool:
    """Managing native rules needs Manage Server on the bot's side."""
    me = getattr(guild, 'me', None)
    return bool(me and me.guild_permissions.manage_guild)


def _exemptions(guild, cfg) -> tuple:
    """(channels, roles) the native rules should not touch.

    Mirrors what the hand-rolled automod skips: channels whitelisted for
    automod and the server's staff roles. (Discord can only exempt whole
    roles, so "has manage_messages" staff is approximated by staff roles.)
    """
    cfg = cfg or {}
    channels, roles = [], []
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT channel_id FROM whitelist WHERE guild_id=? AND system=?',
                            (str(guild.id), 'automod')).fetchall()
        srows = conn.execute('SELECT role_id FROM staff_roles WHERE guild_id=?',
                              (str(guild.id),)).fetchall()
    want_ch = {int(c) for c in cfg.get('whitelist_channels') or [] if str(c).isdigit()}
    want_ch |= {int(r['channel_id']) for r in rows if str(r['channel_id']).isdigit()}
    want_role = {int(r) for r in cfg.get('whitelist_roles') or [] if str(r).isdigit()}
    want_role |= {int(r['role_id']) for r in srows if str(r['role_id']).isdigit()}
    for cid in want_ch:
        ch = guild.get_channel(cid)
        if ch is not None:
            channels.append(ch)
    for rid in want_role:
        role = guild.get_role(rid)
        if role is not None:
            roles.append(role)
    return channels, roles


def _mirror_trigger(key, cfg):
    """The native rule that mirrors one automod feature."""
    if key == 'anti_invite':
        return da.AutoModTrigger(type=da.AutoModRuleTriggerType.keyword,
                                 keyword_filter=list(INVITE_KEYWORDS))
    if key == 'anti_link':
        return da.AutoModTrigger(type=da.AutoModRuleTriggerType.keyword,
                                 keyword_filter=list(LINK_KEYWORDS))
    if key == 'badwords':
        return da.AutoModTrigger(type=da.AutoModRuleTriggerType.keyword,
                                 keyword_filter=_clean_keywords(cfg.get('badwords')))
    return None


def _mirror_name(gid, key) -> str:
    return t(gid, 'na.name_' + key)


def _trigger_sig(trigger):
    """Comparable fingerprint of a trigger, for drift detection."""
    if trigger is None:
        return ()
    ttype = trigger.type
    if ttype == da.AutoModRuleTriggerType.keyword:
        return (ttype, tuple(sorted(trigger.keyword_filter or [])),
                tuple(sorted(trigger.regex_patterns or [])))
    if ttype == da.AutoModRuleTriggerType.keyword_preset:
        return (ttype, tuple(sorted(trigger.presets.to_array())))
    if ttype == da.AutoModRuleTriggerType.mention_spam:
        return (ttype, trigger.mention_limit)
    return (ttype,)


def _action_sig(actions) -> tuple:
    out = []
    for a in actions or []:
        dur = int(a.duration.total_seconds()) if a.duration else 0
        out.append((a.type, a.channel_id, dur, a.custom_message))
    return tuple(sorted(out, key=str))


def _describe_trigger(gid, trigger) -> str:
    ttype = trigger.type
    if ttype == da.AutoModRuleTriggerType.keyword:
        n = len(trigger.keyword_filter or [])
        return f"{t(gid, 'na.trig_keyword')} · {t(gid, 'na.lbl_terms', n=n)}"
    if ttype == da.AutoModRuleTriggerType.harmful_link:
        return t(gid, 'na.trig_harmful_link')
    if ttype == da.AutoModRuleTriggerType.spam:
        return t(gid, 'na.trig_spam')
    if ttype == da.AutoModRuleTriggerType.keyword_preset:
        return t(gid, 'na.trig_preset')
    if ttype == da.AutoModRuleTriggerType.mention_spam:
        return t(gid, 'na.trig_mention_spam')
    if ttype == da.AutoModRuleTriggerType.member_profile:
        return t(gid, 'na.trig_member_profile')
    return str(ttype)


def _describe_action(gid, action) -> str:
    atype = action.type
    if atype == da.AutoModRuleActionType.block_message:
        return t(gid, 'na.act_block')
    if atype == da.AutoModRuleActionType.send_alert_message:
        return t(gid, 'na.act_alert')
    if atype == da.AutoModRuleActionType.timeout:
        return t(gid, 'na.act_timeout')
    if atype == da.AutoModRuleActionType.block_member_interactions:
        return t(gid, 'na.act_interactions')
    return str(atype)


def _rule_lines(gid, rule, managed_key) -> list:
    out = [
        stat(t(gid, 'na.lbl_id'), str(rule.id), hint=rule.name),
        stat(t(gid, 'na.lbl_trigger'), _describe_trigger(gid, rule.trigger)),
        stat(t(gid, 'na.lbl_action'),
             ', '.join(_describe_action(gid, a) for a in rule.actions) or '—'),
        stat(t(gid, 'na.lbl_state'), t(gid, 'na.state_on' if rule.enabled else 'na.state_off')),
    ]
    if managed_key:
        out.append(f"  -# {t(gid, 'na.managed', key=managed_key)}")
    return out


def _attach_native(bot) -> bool:
    """Hang the `native` group under the existing `.automod` group."""
    try:
        parent = bot.get_command('automod')
        if parent is None:
            return False
        if parent.all_commands.get('native') is not None:
            return True
        group = bot.get_command('native')
        if group is None:
            return False
        bot.remove_command('native')
        parent.add_command(group)
        return True
    except Exception:
        return False


class NativeAutoMod(commands.Cog):
    """Discord-native AutoMod rules, mirrored from the bot's own config."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        _ensure_table()
        self.sync_loop.start()
        # bot.loop does not exist before login in discord.py 2.7, so the
        # startup pass is scheduled as a plain task.
        asyncio.create_task(self._startup())

    def cog_unload(self):
        self.sync_loop.cancel()

    async def _startup(self):
        """Attach the group, then run one sync pass over the known guilds."""
        await self.bot.wait_until_ready()
        _attach_native(self.bot)
        await self._sync_all()

    # ---------- background sync ----------
    @tasks.loop(minutes=10)
    async def sync_loop(self):
        await self.bot.wait_until_ready()
        await self._sync_all()

    async def _sync_all(self):
        for gid in _configured_guilds():
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            try:
                await self.sync_guild(guild)
            except discord.HTTPException as exc:
                print(f'[!] native automod: sync failed in {gid}: {exc}', flush=True)
            except Exception as exc:
                print(f'[!] native automod: sync error in {gid}: {exc!r}', flush=True)

    async def sync_guild(self, guild) -> dict:
        """Make the native rules match the bot's own automod config.

        Per feature (anti_invite / anti_link / badwords):
          config on  -> the rule exists, is enabled and matches the config
          config off -> the rule is deleted (and stays deleted until asked again)
        The master automod.enabled switch pauses every managed rule instead of
        deleting it, so flipping it back on is instant.
        """
        gid = guild.id
        cfg = _automod_cfg(gid)
        if cfg is None:
            return {'created': 0, 'updated': 0, 'deleted': 0}
        managed = _managed(gid)
        master = bool(cfg.get('enabled'))
        counts = {'created': 0, 'updated': 0, 'deleted': 0}
        for key in MIRROR_KEYS:
            rule = None
            rule_id = managed.get(key)
            if rule_id:
                try:
                    rule = await guild.fetch_automod_rule(int(rule_id))
                except discord.NotFound:
                    rule = None
            if not _feature_on(cfg, key):
                if rule is not None:
                    await rule.delete(reason='babka automod sync')
                    counts['deleted'] += 1
                _set_managed(gid, key, None)
                continue
            trigger = _mirror_trigger(key, cfg)
            name = _mirror_name(gid, key)
            actions = _actions(gid, guild)
            exempt_channels, exempt_roles = _exemptions(guild, cfg)
            if rule is None:
                rule = await guild.create_automod_rule(
                    name=name,
                    event_type=da.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=master,
                    exempt_channels=exempt_channels,
                    exempt_roles=exempt_roles,
                    reason='babka automod sync')
                _set_managed(gid, key, rule.id)
                counts['created'] += 1
                continue
            changed = (rule.name != name
                       or rule.enabled != master
                       or _trigger_sig(rule.trigger) != _trigger_sig(trigger)
                       or _action_sig(rule.actions) != _action_sig(actions)
                       or {c.id for c in exempt_channels} != set(rule.exempt_channel_ids)
                       or {r.id for r in exempt_roles} != set(rule.exempt_role_ids))
            if changed:
                await rule.edit(name=name, trigger=trigger, actions=actions, enabled=master,
                               exempt_channels=exempt_channels, exempt_roles=exempt_roles,
                               reason='babka automod sync')
                counts['updated'] += 1
        return counts

    # ---------- native alert -> the bot's own strike flow ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        """Turn Discord's automod alert into the bot's own strike flow.

        A native rule blocks the message before the bot ever sees it, so the
        strike counter would never move. Discord's alert lands in the mod-log,
        and that is our cue to punish the way cogs/automod.py does.
        """
        if message.type is not discord.MessageType.auto_moderation_action:
            return
        guild = message.guild
        if guild is None:
            return
        ch = _modlog_channel(guild.id)
        if not ch or message.channel.id != ch:
            return
        member = self._alert_member(message)
        if member is None:
            return
        await self._native_strike(guild, member, message, t(guild.id, 'na.native_hit'))

    @staticmethod
    def _alert_member(message):
        """The member behind an automod alert, if we can name exactly one."""
        guild = message.guild
        found = []
        for user in message.mentions:
            if not user.bot:
                found.append(user.id)
        if not found:
            for text in _alert_texts(message):
                found += [int(x) for x in _MENTION_RE.findall(text)]
        cands = [c for c in dict.fromkeys(found)
                 if c != guild.me.id and guild.get_member(c) is not None]
        return guild.get_member(cands[0]) if len(cands) == 1 else None

    @staticmethod
    def _alert_channel(message):
        """The channel the blocked message came from, if the alert names it."""
        for text in _alert_texts(message):
            m = _CHANNEL_RE.search(text)
            if m:
                ch = message.guild.get_channel(int(m.group(1)))
                if ch is not None:
                    return ch
        return None

    async def _native_strike(self, guild, member, message, reason):
        """Mirror cogs/automod.py's strike flow for a natively blocked message."""
        gid = guild.id
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) '
                         'VALUES (?,?,?,?,?)',
                         (str(gid), str(member.id), 'auto', f'[auto] {reason}', int(time.time())))
        n = _bump_strike(gid, member.id)
        sc = _strikecfg(gid)
        if n <= 2:
            mins = sc['s1_min'] if n == 1 else sc['s2_min']
            ok_to = await _apply_timeout(member, mins, f'strike {n}: {reason}')
            await log_to_mod(guild, build(
                f'{member.mention} strike {n}/3 (timeout {mins}m)\nReason: {reason}',
                title=t(gid, 'as.title'), color=DARK_RED))
            key = 'na.strike_to' if ok_to else 'na.strike_to_fail'
            target = self._alert_channel(message)
            if target is not None:
                try:
                    warn = await say(target, t(gid, key, user=member.mention, n=n, m=mins,
                                               reason=reason))
                    await warn.delete(delay=8)
                except Exception:
                    pass
            return
        _reset_strikes(gid, member.id)
        await log_to_mod(guild, build(f'{member.mention} strike 3/3\nReason: {reason}',
                                      title=t(gid, 'as.title'), color=DARK_RED))
        am = self.bot.get_cog('AutoMod')
        request = getattr(am, '_action_request', None)
        if request is None:
            return
        try:
            await request(guild, member, reason, '', message.channel, sc['s3_action'])
        except Exception:
            pass

    # ---------- commands ----------
    @commands.group(name='native', invoke_without_command=True,
                    description='Native Discord AutoMod rules')
    async def native(self, ctx):
        """Show the native AutoMod rules Discord enforces for this server."""
        gid = ctx.guild.id
        try:
            rules = await ctx.guild.fetch_automod_rules()
        except discord.HTTPException as exc:
            return await ctx.reply(t(gid, 'na.fetch_fail', err=str(exc)), ephemeral=True)
        managed = {str(v): k for k, v in _managed(gid).items() if v}
        if not rules:
            body = t(gid, 'na.empty')
        else:
            chunks = []
            for rule in rules:
                chunks.append('\n'.join(_rule_lines(gid, rule, managed.get(str(rule.id)))))
                chunks.append(divider())
            body = '\n'.join(chunks)
        await ctx.reply(view=panel(t(gid, 'na.title'), body, accent=ACCENT['default']),
                        ephemeral=True)

    @native.command(name='add', description='Add a native AutoMod rule')
    @staff_or('manage_guild')
    async def native_add(self, ctx, kind: str, *, args: str = ''):
        """Create a native AutoMod rule.

        kind: keyword | preset | mention-spam | harmful-link
        """
        gid = ctx.guild.id
        if not _can_manage(ctx.guild):
            return await ctx.reply(t(gid, 'na.need_perm', bot=self._bot_name(ctx.guild)),
                                   ephemeral=True)
        kind = (kind or '').lower().strip()
        try:
            trigger, name = self._build_trigger(gid, kind, args)
        except ValueError as exc:
            key = exc.args[0]
            extra = exc.args[1] if len(exc.args) > 1 else {}
            return await ctx.reply(t(gid, key, **extra), ephemeral=True)
        exempt_channels, exempt_roles = _exemptions(ctx.guild, _automod_cfg(gid))
        try:
            rule = await ctx.guild.create_automod_rule(
                name=name,
                event_type=da.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=_actions(gid, ctx.guild),
                enabled=True,
                exempt_channels=exempt_channels,
                exempt_roles=exempt_roles,
                reason='babka automod')
        except discord.HTTPException as exc:
            print(f'[!] native automod: create failed in {gid}: {exc}', flush=True)
            return await ctx.reply(t(gid, 'na.create_fail', err=str(exc)), ephemeral=True)
        await ctx.reply(t(gid, 'na.added', name=rule.name, id=rule.id), ephemeral=True)

    @native.command(name='remove', description='Delete a native AutoMod rule')
    @staff_or('manage_guild')
    async def native_remove(self, ctx, rule_id: int):
        """Delete a native AutoMod rule by id."""
        gid = ctx.guild.id
        if not _can_manage(ctx.guild):
            return await ctx.reply(t(gid, 'na.need_perm', bot=self._bot_name(ctx.guild)),
                                   ephemeral=True)
        try:
            rule = await ctx.guild.fetch_automod_rule(rule_id)
        except discord.NotFound:
            return await ctx.reply(t(gid, 'na.remove_missing', id=rule_id), ephemeral=True)
        except discord.HTTPException as exc:
            return await ctx.reply(t(gid, 'na.fetch_fail', err=str(exc)), ephemeral=True)
        try:
            await rule.delete(reason='babka automod')
        except discord.HTTPException as exc:
            print(f'[!] native automod: delete failed in {gid}: {exc}', flush=True)
            return await ctx.reply(t(gid, 'na.delete_fail', err=str(exc)), ephemeral=True)
        _clear_managed(gid, rule_id)
        await ctx.reply(t(gid, 'na.removed', id=rule_id), ephemeral=True)

    @native.command(name='sync', description='Mirror the automod config into native rules')
    @staff_or('manage_guild')
    async def native_sync(self, ctx):
        """Make the native rules match the bot's own automod config."""
        gid = ctx.guild.id
        if not _can_manage(ctx.guild):
            return await ctx.reply(t(gid, 'na.need_perm', bot=self._bot_name(ctx.guild)),
                                   ephemeral=True)
        cfg = _automod_cfg(gid)
        if cfg is None:
            return await ctx.reply(t(gid, 'na.sync_no_config'), ephemeral=True)
        try:
            counts = await self.sync_guild(ctx.guild)
        except discord.HTTPException as exc:
            print(f'[!] native automod: sync failed in {gid}: {exc}', flush=True)
            return await ctx.reply(t(gid, 'na.fetch_fail', err=str(exc)), ephemeral=True)
        total = counts['created'] + counts['updated'] + counts['deleted']
        if not total:
            return await ctx.reply(t(gid, 'na.sync_nothing'), ephemeral=True)
        await ctx.reply(t(gid, 'na.synced', created=counts['created'],
                          updated=counts['updated'], deleted=counts['deleted']),
                        ephemeral=True)

    # ---------- helpers ----------
    def _build_trigger(self, gid, kind, args):
        """(trigger, rule name) for a `native add` request."""
        if kind == 'keyword':
            words = _clean_keywords([w for w in (args or '').split(',')])
            if not words:
                raise ValueError('na.need_keywords')
            return (da.AutoModTrigger(type=da.AutoModRuleTriggerType.keyword,
                                      keyword_filter=words),
                    t(gid, 'na.name_keyword'))
        if kind == 'mention-spam':
            raw = (args or '').strip()
            try:
                limit = int(raw) if raw else MENTION_LIMIT_DEFAULT
            except ValueError:
                raise ValueError('na.bad_limit')
            if not 1 <= limit <= MENTION_LIMIT_MAX:
                raise ValueError('na.bad_limit')
            return (da.AutoModTrigger(type=da.AutoModRuleTriggerType.mention_spam,
                                      mention_limit=limit),
                    t(gid, 'na.name_mention_spam'))
        if kind == 'harmful-link':
            return (da.AutoModTrigger(type=da.AutoModRuleTriggerType.harmful_link),
                    t(gid, 'na.name_harmful_link'))
        if kind == 'preset':
            wanted = [p.strip().lower() for p in (args or '').split(',') if p.strip()]
            wanted = wanted or list(PRESET_NAMES)
            for p in wanted:
                if p not in PRESET_NAMES:
                    raise ValueError('na.bad_preset', {'preset': p})
            presets = da.AutoModPresets(**{p: True for p in wanted})
            return (da.AutoModTrigger(type=da.AutoModRuleTriggerType.keyword_preset,
                                      presets=presets),
                    t(gid, 'na.name_preset'))
        raise ValueError('na.bad_kind', {'kind': kind})

    @staticmethod
    def _bot_name(guild) -> str:
        me = getattr(guild, 'me', None)
        return (getattr(me, 'display_name', None) or getattr(me, 'name', None)
                or 'the bot')


def _alert_texts(message) -> list:
    """Every scrap of text on a message (content, title, description, fields)."""
    texts = [message.content or '']
    for emb in message.embeds:
        texts.append(emb.title or '')
        texts.append(emb.description or '')
        for field in emb.fields:
            texts.append(field.value or '')
    return texts


async def setup(bot):
    cog = NativeAutoMod(bot)
    await bot.add_cog(cog)
    _attach_native(bot)

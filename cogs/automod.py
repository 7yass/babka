"""AutoMod: invites, links, bad words, mentions, caps, spam, charspam."""
import json
import re
import time
import unicodedata
from collections import defaultdict, deque

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, err, ok, say, WHITE, DARK_RED
from utils.checks import staff_or
from utils.common import log_to_mod

INVITE_RE = re.compile(r'discord(?:\.gg|app\.com/invite)/[\w-]+', re.I)
LINK_RE = re.compile(r'https?://\S+', re.I)
SPAM = defaultdict(lambda: defaultdict(lambda: deque(maxlen=10)))

# Evasion-proof matching: strip accents, fold leetspeak + Cyrillic/Greek
# lookalikes, drop separators, collapse repeats — so "n1gg3r", "n i g g e r"
# or "nígger" all normalize to the same skeleton as the base word.
LEET = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's',
    '6': 'g', '7': 't', '8': 'b', '9': 'g',
    '@': 'a', '$': 's', '!': 'i', '+': 't', '€': 'e',
})
CONFUSE = {
    'а': 'a', 'е': 'e', 'ё': 'e', 'і': 'i', 'о': 'o', 'р': 'p',
    'с': 'c', 'к': 'k', 'х': 'x', 'м': 'm', 'н': 'h', 'т': 't',
    'у': 'y', 'в': 'b', 'д': 'd', 'л': 'l', 'п': 'n',
    'α': 'a', 'ε': 'e', 'ι': 'i', 'ο': 'o', 'ρ': 'p', 'κ': 'k',
    'μ': 'm', 'ν': 'v', 'τ': 't', 'υ': 'u', 'χ': 'x', 'ζ': 'z',
}
NON_AZ = re.compile(r'[^a-z]')
RUNS = re.compile(r'(.)\1+')

# Ambiguous glyphs: every reading gets checked (capped).
AMB = {'1': ('i', 'l'), '|': ('i', 'l'), '!': ('i', 'l')}
# 'v' doubles as 'u' in PL evasions (kvrwa); symmetric so base forms still match.
VU = str.maketrans({'v': 'u'})


def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', (s or '').lower())
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = ''.join(CONFUSE.get(c, c) for c in s)
    s = s.translate(LEET).replace('vv', 'w').translate(VU)
    return RUNS.sub(r'\1', NON_AZ.sub('', s))


def _readings(s: str) -> list:
    out = ['']
    for ch in (s or '').lower():
        out = [p + o for p in out for o in AMB.get(ch, (ch,))]
        if len(out) > 32:
            return [_norm(s)]
    return [_norm(r) for r in out]


def _raw_variants(s: str) -> list:
    """AMB alternatives without normalization (length-preserving)."""
    out = ['']
    for ch in (s or '').lower():
        out = [p + o for p in out for o in AMB.get(ch, (ch,))]
        if len(out) > 32:
            return [(s or '').lower()]
    return out


def _fold1(s: str) -> str:
    """Length-preserving fold: accents, lookalikes, leet — no stripping."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ''.join(CONFUSE.get(c, c) for c in s).translate(LEET).translate(VU)


WORD_RE = re.compile(r'[^\W\d_]+', re.UNICODE)
# vowel-ish continuations: Polish inflections swap endings for vowels
# ('suki', 'kurwo'), so stem + vowel still counts as the same word.
NEXT_OK = set('aeiouyąęó') | {'l', 'r', 'j'}


def _badword_hit(content: str, badwords: list):
    """Word-aware bad-word match. Splits the message into words BEFORE
    normalization (so separators survive), then matches each entry against
    whole words: exact for tiny words, stem + length cap otherwise.
    Multi-word entries fall back to joined-stream matching."""
    raw_words = WORD_RE.findall(content or '')
    if not raw_words:
        return None
    variants = []
    folds = []
    for w in raw_words:
        for r in _raw_variants(w):
            variants.append(_norm(r))
            folds.append(_fold1(r))
        if len(variants) > 256:
            break
    joined = ''.join(_norm(w) for w in raw_words)
    for w in badwords:
        nw = _norm(w or '')
        if not nw:
            continue
        if ' ' in (w or '').strip():
            if nw in joined:
                return w
            continue
        if len(nw) < 4:
            if any(nw == nm for nm in variants):
                return w
            continue
        bound = len(nw) + 3
        for nm in variants:
            if nm == nw:
                return w
            if nm.startswith(nw) and len(nm) <= bound:
                return w
        for fl in folds:
            if (fl.startswith(nw[:-1]) and len(fl) <= bound
                    and len(fl) > len(nw) - 1 and fl[len(nw) - 1] in NEXT_OK):
                return w
    return None

# Premade severe-profanity seed (EN + PL). No mild words, no slurs —
# add your own via badword-add. Matching is word-based (stem + length cap)
# so 'jujutsu kaisen' never trips 'suka', while inflections still match.
DEFAULT_BADWORDS = [
    # EN
    'fuck', 'fucker', 'fucking', 'motherfucker', 'cocksucker', 'cunt',
    'slut', 'whore', 'twat', 'bitch', 'dick', 'cock', 'pussy', 'asshole',
    'prick', 'douchebag', 'cumslut', 'sex',
    # PL
    'kurwa', 'kurwo', 'chuj', 'chuja', 'chujem', 'pierdole', 'pierdolę',
    'pierdolony', 'jebac', 'jebać', 'jebany', 'jebana', 'suka', 'sukinsyn',
    'skurwysyn', 'dziwka', 'cipa', 'kutas', 'spierdalaj', 'wypierdalaj', 'pizda',
    'Wypierdalaj', 'Odpierdol się', 'cwel',
]


_CFG_CACHE = {}
_CFG_TTL = 30


def _cfg_drop(gid):
    _cfg_drop(gid)




def _cfg(guild_id) -> dict:
    import time as _t
    key = str(guild_id)
    hit = _CFG_CACHE.get(key)
    if hit and _t.time() - hit[0] < _CFG_TTL:
        return dict(hit[1])
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM automod WHERE guild_id = ?', (str(guild_id),)).fetchone()
        if not row:
            conn.execute('INSERT INTO automod (guild_id, badwords) VALUES (?,?)',
                         (str(guild_id), json.dumps(DEFAULT_BADWORDS)))
            row = conn.execute('SELECT * FROM automod WHERE guild_id = ?', (str(guild_id),)).fetchone()
        d = dict(row)
        d['badwords'] = json.loads(d.get('badwords') or '[]')
    _CFG_CACHE[key] = (_t.time(), d)
    return dict(d)


def _strikecfg(guild_id) -> dict:
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO strike_cfg (guild_id) VALUES (?)', (str(guild_id),))
        row = conn.execute('SELECT * FROM strike_cfg WHERE guild_id=?', (str(guild_id),)).fetchone()
        d = dict(row)
        d['s1_min'] = d.get('s1_min') if d.get('s1_min') is not None else 5
        d['s2_min'] = d.get('s2_min') if d.get('s2_min') is not None else 10
        d['s3_action'] = d.get('s3_action') or 'kick'
        return d


def _bump_strike(guild_id, user_id) -> int:
    with db.conn_ctx() as conn:
        conn.execute('''INSERT INTO strikes (guild_id, user_id, count, updated_at) VALUES (?,?,1,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET count=count+1, updated_at=excluded.updated_at''',
                     (str(guild_id), str(user_id), int(time.time())))
        return conn.execute('SELECT count FROM strikes WHERE guild_id=? AND user_id=?',
                            (str(guild_id), str(user_id))).fetchone()['count']


def _reset_strikes(guild_id, user_id):
    with db.conn_ctx() as conn:
        conn.execute('DELETE FROM strikes WHERE guild_id=? AND user_id=?', (str(guild_id), str(user_id)))


async def _apply_timeout(member: discord.Member, minutes: int, reason: str) -> bool:
    import datetime
    try:
        await member.timeout(datetime.timedelta(minutes=max(1, minutes)), reason=reason)
        return True
    except Exception:
        return False


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _strike(self, message: discord.Message, word: str):
        """Bad-word hit (message already deleted): warn + escalating timeouts,
        3rd strike sends an action request to the admin log."""
        gid = message.guild.id
        member = message.author
        reason = t(gid, 'as.word', w=word)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)',
                         (str(gid), str(member.id), 'auto', f'[auto] {reason}', int(time.time())))
        n = _bump_strike(gid, member.id)
        sc = _strikecfg(gid)
        if n == 1:
            mins = sc['s1_min']
            ok_to = await _apply_timeout(member, mins, f'strike 1: {reason}')
            key = 'as.strike_to' if ok_to else 'as.strike_to_fail'
            await log_to_mod(message.guild, build(
                f'{member.mention} strike 1/{3} (timeout {mins}m)\nReason: {reason}\n```{(message.content or "")[:300]}```',
                title=t(gid, 'as.title'), color=DARK_RED))
            try:
                warn = await say(message.channel, t(gid, key, user=member.mention, n=n, m=mins, reason=reason))
                await warn.delete(delay=8)
            except Exception:
                pass
            return
        if n == 2:
            mins = sc['s2_min']
            ok_to = await _apply_timeout(member, mins, f'strike 2: {reason}')
            key = 'as.strike_to' if ok_to else 'as.strike_to_fail'
            await log_to_mod(message.guild, build(
                f'{member.mention} strike 2/3 (timeout {mins}m)\nReason: {reason}\n```{(message.content or "")[:300]}```',
                title=t(gid, 'as.title'), color=DARK_RED))
            try:
                warn = await say(message.channel, t(gid, key, user=member.mention, n=n, m=mins, reason=reason))
                await warn.delete(delay=8)
            except Exception:
                pass
            return
        # 3rd strike: staff decides. Reset the cycle afterwards.
        _reset_strikes(gid, member.id)
        await log_to_mod(message.guild, build(
            f'{member.mention} strike 3/3\nReason: {reason}\n```{(message.content or "")[:300]}```',
            title=t(gid, 'as.title'), color=DARK_RED))
        await self._action_request(message.guild, member, reason, (message.content or '')[:300],
                                   message.channel, sc['s3_action'])
        try:
            warn = await say(message.channel, t(gid, 'as.escalated', user=member.mention, reason=reason))
            await warn.delete(delay=10)
        except Exception:
            pass

    async def _action_request(self, guild: discord.Guild, member: discord.Member, reason: str,
                              sample: str, fallback_ch, default_action: str):
        gid = guild.id
        settings = db.get_settings(gid)
        dest = guild.get_channel(int(settings.get('modlog_channel'))) if settings.get('modlog_channel') else None
        if dest is None:
            dest = fallback_ch
        emb = build(f'{member.mention} (`{member.id}`)\n{reason}\n```{sample}```\n'
                    f'Suggested: **{default_action}** — or pick below.',
                    title=t(gid, 'as.req_title'), color=DARK_RED)
        try:
            emb.set_thumbnail(url=str(member.display_avatar.with_size(128).url))
        except Exception:
            pass
        view = discord.ui.View(timeout=None)
        for label, act in (('Kick', 'kick'), ('Ban', 'ban'), ('Timeout 1h', 'timeout'), ('Dismiss', 'dismiss')):
            style = discord.ButtonStyle.red if act in ('kick', 'ban') else discord.ButtonStyle.grey
            view.add_item(discord.ui.Button(label=label, style=style,
                                            custom_id=f'strike:{act}:{member.id}'))
        try:
            await dest.send(embed=emb, view=view)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if not cid.startswith('strike:'):
            return
        from utils.checks import is_staff
        try:
            _, act, uid = cid.split(':')
        except ValueError:
            return
        need = {'kick': 'kick_members', 'ban': 'ban_members',
                'timeout': 'moderate_members', 'dismiss': 'manage_messages'}
        if act not in need:
            return
        me = interaction.user
        allowed = is_staff(me) or getattr(me.guild_permissions, need[act], False)
        if not allowed:
            return await interaction.response.send_message(
                t(interaction.guild_id, 'as.req_noperm'), ephemeral=True)
        guild = interaction.guild
        target = guild.get_member(int(uid))
        if target is None and act != 'dismiss':
            return await interaction.response.send_message(
                t(interaction.guild_id, 'as.req_gone'), ephemeral=True)
        import datetime
        try:
            if act == 'kick':
                await target.kick(reason=f'strike action by {me}')
            elif act == 'ban':
                await guild.ban(target, reason=f'strike action by {me}', delete_message_days=1)
            elif act == 'timeout':
                await target.timeout(datetime.timedelta(hours=1), reason=f'strike action by {me}')
        except Exception:
            return await interaction.response.send_message(
                t(interaction.guild_id, 'as.kick_fail', user=f'<@{uid}>'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)',
                         (str(guild.id), str(uid), str(me.id), f'[strike-action] {act}', int(time.time())))
        emb = interaction.message.embeds[0] if interaction.message.embeds else None
        if emb:
            emb = emb.copy()
            emb.add_field(name='—', value=t(interaction.guild_id, 'as.req_handled', user=me.mention, act=act),
                          inline=False)
        await interaction.response.edit_message(embed=emb, view=None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member) or member.guild_permissions.manage_messages:
            return
        from cogs.whitelist import is_exempt
        if is_exempt(message.guild.id, message.channel.id, 'automod'):
            return
        cfg = _cfg(message.guild.id)
        if not cfg.get('enabled'):
            return
        gid = message.guild.id
        content = message.content or ''
        verdict = None
        if cfg.get('anti_invite') and INVITE_RE.search(content):
            verdict = ('delete', t(gid, 'as.invite'))
        elif cfg.get('anti_link') and LINK_RE.search(content):
            from cogs.levels import is_gif_url as _is_gif, get_user as _get_user, MIN_GIF_LEVEL
            urls = LINK_RE.findall(content)
            if all(_is_gif(u) for u in urls):
                if _get_user(gid, member.id).get('level', 0) < MIN_GIF_LEVEL:
                    pass  # only GIF links + under level: the GIF gate handles it (with a proper note)
                # else: only GIF links and level is fine — let it through
            else:
                verdict = ('delete', t(gid, 'as.links'))
        if verdict is None:
            hit = _badword_hit(content, cfg['badwords'])
            if hit:
                try:
                    await message.delete()
                except Exception:
                    pass
                await self._strike(message, hit)
                return
            elif len(message.mentions) + len(message.role_mentions) > 5:
                verdict = ('delete', t(gid, 'as.mentions'))
            else:
                letters = re.sub(r'[^a-zA-Z]', '', content)
                if len(letters) >= 10:
                    up = len(re.findall(r'[A-Z]', content))
                    if up / len(letters) * 100 > 70:
                        verdict = ('delete', t(gid, 'as.caps'))
            if not verdict:
                now = time.time()
                dq = SPAM[message.guild.id][message.author.id]
                while dq and now - dq[0] > 5:
                    dq.popleft()
                dq.append(now)
                if len(dq) >= 5:
                    dq.clear()
                    verdict = ('timeout', t(gid, 'as.spam', n=5, s=5))
            if not verdict and re.search(r'(.)\1{9,}', content):
                verdict = ('delete', t(gid, 'as.chars'))
        if not verdict:
            return
        action, reason = verdict
        try:
            await message.delete()
        except Exception:
            pass
        await log_to_mod(message.guild, build(
            f'{member.mention} — {reason}\n```{(message.content or "")[:300]}```',
            title=t(gid, 'as.title'), color=DARK_RED))
        try:
            warn = await say(message.channel, t(gid, 'as.warn', user=member.mention, reason=reason))
            await warn.delete(delay=5)
        except Exception:
            pass
        if action == 'timeout':
            try:
                await member.timeout(60, reason=reason)
            except Exception:
                pass

    # ---------- commands ----------
    @commands.group(name='automod', description='Auto-moderacja')
    async def automod(self, ctx):
        await ctx.reply('.automod status / toggle / badword-add / badword-remove / anti-invite / anti-link',
                        ephemeral=True)

    @automod.command(name='status', description='Pokaż ustawienia')
    async def status(self, ctx):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        on = t(gid, 'ar.on')
        off = t(gid, 'ar.off')
        await ctx.reply(t(gid, 'am.status', on=on if cfg['enabled'] else off,
                            inv=on if cfg['anti_invite'] else off, links=on if cfg['anti_link'] else off,
                            words=', '.join(cfg['badwords']) or '(none)', ch='(none)'), ephemeral=True)

    @automod.command(name='toggle', description='Włącz / wyłącz')
    @staff_or('manage_guild')
    async def toggle(self, ctx):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET enabled=? WHERE guild_id=?', (0 if cfg['enabled'] else 1, str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.toggled', t=t(gid, 'ar.off') if cfg['enabled'] else t(gid, 'ar.on')), ephemeral=True)

    @automod.command(name='badword-add', description='Zablokuj słowo')
    @staff_or('manage_guild')
    async def bw_add(self, ctx, word: str):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        words = set(cfg['badwords']) | {word.lower()}
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET badwords=? WHERE guild_id=?', (json.dumps(sorted(words)), str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.blocked', w=word.lower()), ephemeral=True)

    @automod.command(name='badword-remove', description='Odblokuj słowo')
    @staff_or('manage_guild')
    async def bw_remove(self, ctx, word: str):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        words = [w for w in cfg['badwords'] if w != word.lower()]
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET badwords=? WHERE guild_id=?', (json.dumps(words), str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.unblocked', w=word.lower()), ephemeral=True)

    @automod.command(name='anti-invite', description='Filtr invite wł./wył.')
    @staff_or('manage_guild')
    async def anti_invite(self, ctx):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET anti_invite=? WHERE guild_id=?', (0 if cfg['anti_invite'] else 1, str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.inv', t=t(gid, 'ar.off') if cfg['anti_invite'] else t(gid, 'ar.on')), ephemeral=True)

    @automod.command(name='anti-link', description='Filtr linków wł./wył.')
    @staff_or('manage_guild')
    async def anti_link(self, ctx):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET anti_link=? WHERE guild_id=?', (0 if cfg['anti_link'] else 1, str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.links', t=t(gid, 'ar.off') if cfg['anti_link'] else t(gid, 'ar.on')), ephemeral=True)

    @automod.command(name='strikes', description='Ile warnów ma typ')
    @staff_or('manage_messages')
    async def strikes(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT count FROM strikes WHERE guild_id=? AND user_id=?',
                               (str(gid), str(member.id))).fetchone()
        n = row['count'] if row else 0
        key = 'as.strikes_n' if n else 'as.strikes_none'
        await ctx.reply(t(gid, key, user=member.mention, n=n), ephemeral=True)

    @automod.command(name='strike-reset', description='Wyczyść warny typa')
    @staff_or('manage_messages')
    async def strike_reset(self, ctx, member: discord.Member):
        gid = ctx.guild.id
        _reset_strikes(gid, member.id)
        await ctx.reply(t(gid, 'as.strikes_cleared', user=member.mention), ephemeral=True)

    @automod.command(name='badwords-seed', description='Scal gotową listę słów EN+PL')
    @staff_or('manage_guild')
    async def bw_seed(self, ctx):
        gid = ctx.guild.id
        cfg = _cfg(gid)
        words = sorted(set(cfg['badwords']) | set(DEFAULT_BADWORDS))
        with db.conn_ctx() as conn:
            conn.execute('UPDATE automod SET badwords=? WHERE guild_id=?', (json.dumps(words), str(gid)))
            _cfg_drop(gid)
        await ctx.reply(t(gid, 'am.seeded', n=len(words)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))

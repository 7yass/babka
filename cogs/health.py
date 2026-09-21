"""Command health: read-only audits over the live registry, help coverage,
stale numbers and runtime safety. Admin-only dev tool — never auto-fixes.

Usage: `.health commands [category:<mod>] [issue:<help|aliases|registry|currency|safety|config>]`
"""
import inspect
import json
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import commands

import database as db
from lang import t, set_ctx_lang
from utils.checks import staff_or
from utils.economy import CASINO_BASE_MAX_BET, POKE_SHOP_PRICES, SHOP_PRICES

META = json.loads((Path(__file__).parent.parent / 'helpmeta.json').read_text(encoding='utf-8'))
COMMAND_META = META['meta']
CATEGORIES = META['cats']

# Curated stale-number rules: (helpmeta key, forbidden substrings, required substrings).
# Each is a regression test for a number we already got wrong once.
STALE_RULES = [
    ('wordle', ['6 tries', '.wordle <guess>'], ['channel']),
]


def _short(n: int) -> str:
    """Help-text number format: 125000 -> '125k', 4000 -> '4k'."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n % 1000 == 0:
        return f'{n // 1000}k'
    return f'{n:,}'


def _config_text_rules():
    """(lang key, needles) derived from utils.economy at audit time —
    displayed help must quote the same values commands charge."""
    return [
        ('hc_d_nick', [_short(SHOP_PRICES['nick'].amount)]),
        ('hc_d_blackjack', [_short(CASINO_BASE_MAX_BET)]),
        ('eco.pk_grazz_none', [_short(POKE_SHOP_PRICES['grazz'].amount)]),
    ]

# Source patterns marking runtime impact. Findings are SUSPECTED (static
# scan, not business-logic proof) unless confirmed by review.
# add_cash/take_cash_upto are the atomic writers: still currency-touching, so
# they belong here even though they can't produce a racy absolute balance.
MONEY_PATTERNS = ('set_cash(', 'add_cash(', 'take_cash_upto(', 'take_cash(',
                  'UPDATE eco SET cash', 'UPDATE eco SET bank')
ITEM_PATTERNS = ('balls_add(', 'balls_take(', 'inv_add(', 'inv_take(')
XP_PATTERNS = ('add_xp(', 'UPDATE pk_mons SET level', 'UPDATE pk_mons SET xp')
ROLE_PATTERNS = ('add_roles(', 'remove_roles(', 'create_role(', 'delete_role(')


def _walk(bot):
    """(qualified_name, command) for every top-level command, group and subcommand."""
    out = []

    def _rec(cmd, parent=None):
        qname = f'{parent} {cmd.name}'.strip() if parent else cmd.name
        out.append((qname, cmd))
        if isinstance(cmd, commands.Group):
            for sub in cmd.commands:
                _rec(sub, qname)

    for cmd in bot.commands:
        _rec(cmd)
    return out


def _src(cmd) -> str:
    try:
        return inspect.getsource(cmd.callback)
    except Exception:
        return ''


def _hits(src: str, patterns) -> bool:
    return any(p in src for p in patterns)


def _racy_write(src: str) -> bool:
    """True when a real yield point sits between a balance read and a
    direct set_cash write. Reply/edit/defer lines don't count: in this
    codebase those always sit on early-return branches or after the write,
    never between read and write on the taken path."""
    import re
    code = '\n'.join(l for l in src.splitlines()
                     if not re.search(r'await\s+(ctx|interaction|ix)\.', l))
    for m in re.finditer(r'set_cash\s*\(', code):
        before = code[:m.start()]
        reads = [mm.start() for mm in re.finditer(r'\bbal\s*\(', before)]
        if not reads:
            continue
        if 'await' in before[max(reads):]:
            return True
    return False


def _suggest(name: str, actual_qnames) -> list:
    """Likely current names for a stale help entry ('age' -> 'antiraid set-age')."""
    flat = name.replace('-', '').replace('_', '')
    out = []
    for q in sorted(actual_qnames):
        norm = q.replace('-', '').replace('_', '').replace(' ', '')
        tail = q.split(' ')[-1].replace('-', '').replace('_', '')
        if norm == flat or tail == flat:
            out.append(q)
    return out[:3]


def _required_params(cmd) -> list:
    """Positional/keyword params without defaults (sans self/ctx). [] when none/unknown."""
    import inspect as _insp
    try:
        params = list(_insp.signature(cmd.callback).parameters.values())
    except Exception:
        return []
    if params and params[0].name in ('self', 'cls'):
        params = params[1:]
    if params and params[0].name == 'ctx':
        params = params[1:]
    req = [p.name for p in params
           if p.default is _insp.Parameter.empty
           and p.kind in (_insp.Parameter.POSITIONAL_ONLY,
                          _insp.Parameter.POSITIONAL_OR_KEYWORD)]
    return req


# Reviewed once, by hand: currency-touching commands whose money already
# flows from a single domain table, so there is nothing to migrate.
# Kept explicit so the pending count stays meaningful instead of nagging.
REVIEWED_LOCAL = {
    'work': 'pay bands single-sourced in JOBS (display + calc read one table)',
    'stockbuy': 'market-driven prices; seeds single-sourced in STOCKS',
    'stocksell': 'market-driven prices; seeds single-sourced in STOCKS',
}


def _price_literal_hits():
    """`'price': <digits>` literals in loaded cogs — every one is a shop
    price that must come from utils.economy instead. poke_extras.py is
    skipped (not loaded; manual review)."""
    import re
    hits = []
    cogdir = Path(__file__).parent
    for f in sorted(cogdir.glob('*.py')):
        if f.name == 'poke_extras.py':
            continue
        try:
            text = f.read_text(encoding='utf-8')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if re.search(r"'price':\s*\d+", line):
                hits.append(f'{f.name}:{i}: hardcoded price field')
    return hits


def _crime_literal_hits():
    """Stake/mult/amount-floor/random-bound literals in crime.py — every one
    belongs in utils.economy CRIME_* instead."""
    import re
    hits = []
    try:
        text = Path(__file__).parent.joinpath('crime.py').read_text(encoding='utf-8')
    except Exception:
        return ['crime.py unreadable (manual review)']
    for i, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if re.search(r'''['"](stake|mult)['"]\s*:\s*[\d.]''', line):
            hits.append(f'crime.py:{i}: hardcoded stake/mult')
        elif re.search(r'\bamount\s*<\s*\d+', line):
            hits.append(f'crime.py:{i}: hardcoded amount floor')
        elif re.search(r'uniform\(\s*[\d.]+', line):
            hits.append(f'crime.py:{i}: hardcoded random bound')
    return hits


# Deliberately NOT money (stays in code, never in utils.economy).
CRIME_EXCLUDED = ('JOIN_WINDOW', 'ROB_CD', 'jail minutes', 'heist base chance',
                  'heist god chance', 'rob mortal chance', 'rob god chance')


def _crime_status(bot):
    """(total_values, using, handler_count, hits, formula, notes)."""
    from utils.economy import CRIME_HEIST_TARGETS, CRIME_ROB
    total = 2 * len(CRIME_HEIST_TARGETS) + len(_dc_fields(CRIME_ROB)) + 1
    by_qname = {}
    for q, c in _walk(bot):
        by_qname[q] = c
    handlers = ['heist start', 'heist join', 'bounty', 'rob']
    using, notes = 0, []
    for q in handlers:
        c = by_qname.get(q)
        if c is None:
            notes.append(f'{q}: command missing')
            continue
        src = _src(c)
        # join reads the stake stored at start time (which came from config).
        if 'CRIME_' in src or "cur['stake']" in src:
            using += 1
        else:
            notes.append(f'{q}: not reading shared config')
    hits = _crime_literal_hits()
    formula = len(CRIME_HEIST_TARGETS) + len(_dc_fields(CRIME_ROB))
    return total, using, len(handlers), hits, formula, notes


def _dc_fields(obj):
    import dataclasses as _dc
    try:
        return [f.name for f in _dc.fields(obj)]
    except Exception:
        return []


def audit(bot):
    """Full audit. Returns (summary: dict, details: dict of issue -> [lines])."""
    cmds = _walk(bot)
    top = [(q, c) for q, c in cmds if ' ' not in q]
    subs = [(q, c) for q, c in cmds if ' ' in q]

    actual_names = {c.name for _, c in cmds}
    actual_qnames = {q for q, _ in cmds}
    alias_owners = defaultdict(set)
    for q, c in cmds:
        for a in (getattr(c, 'aliases', None) or []):
            alias_owners[str(a).lower()].add(q)

    meta_names = set(COMMAND_META)
    details = defaultdict(list)

    # ---- registry ----
    missing_impl = sorted(n for n in meta_names
                          if n not in actual_names and n.lower() not in alias_owners)
    for n in missing_impl:
        sug = _suggest(n, actual_qnames)
        hint = f" (did you mean: {', '.join(sug)})" if sug else ''
        details['registry'].append(f'{n}: in help, no implementation{hint}')
    impl_no_help = sorted(
        q for q, c in cmds
        if ' ' not in q and q not in meta_names and q.lower() not in alias_owners
        and not getattr(c, 'hidden', False))
    for q in impl_no_help:
        details['registry'].append(f'{q}: implemented, no help entry')
    top_names = {c.name for _, c in top}
    for al, owners in sorted(alias_owners.items()):
        if len(owners) > 1:
            details['aliases'].append(f"'{al}' claimed by: {', '.join(sorted(owners))}")
    # Real shadowing is same-depth only: a TOP-LEVEL alias on command O
    # while a DIFFERENT top-level canonical owns the name — dispatch then
    # depends on cog load order. Subcommand aliases (e.g. `shop pokemon`)
    # can never collide with top-level names; those are harmless.
    for al, owners in sorted(alias_owners.items()):
        rivals = {o for o in owners if ' ' not in o and o != al}
        if al in top_names and rivals:
            details['aliases'].append(
                f"'{al}' on {', '.join(sorted(rivals))} shadows canonical '{al}'")
    # helpmeta aliases: dead if no top-level command implements them
    # (help lookup can never reach through them).
    for n, v in COMMAND_META.items():
        for a in (v[2] or []):
            alive = any(' ' not in o for o in alias_owners.get(str(a).lower(), set()))
            if not alive:
                details['help'].append(f"help alias '{a}' of '{n}' matches nothing implemented")

    # ---- help coverage (top-level only; subcommands ride on the parent entry) ----
    for q, c in top:
        if q not in meta_names:
            continue
        v = COMMAND_META[q]
        if not v[1]:
            details['help'].append(f'{q}: empty description')
        # Examples only matter when the user must supply arguments.
        if (len(v) < 4 or not v[3]) and _required_params(c):
            details['help'].append(f'{q}: no usage example (takes: {", ".join(_required_params(c))})')
        if v[0] not in CATEGORIES:
            details['help'].append(f'{q}: bad category {v[0]!r}')
    # Help text falls back to helpmeta's desc_en, so English needs no hc_d_*.
    # What Polish users actually miss is the PL string — check that instead of
    # reporting the whole command list as "unlocalized".
    try:
        from lang import STR
        pl_keys = set((STR.get('pl') or {}).keys())
    except Exception:
        pl_keys = set()
    for q, _ in top:
        if q in meta_names and f'hc_d_{q}' not in pl_keys:
            details['help'].append(f'{q}: no Polish help text (hc_d_{q})')

    # ---- stale numbers: shape rules (explicit text expectations) ----
    try:
        from lang import STR as _STR
        descs = {n: ((_STR.get('en') or {}).get(f'hc_d_{n}') or COMMAND_META[n][1])
                 for n in meta_names if n in COMMAND_META}
    except Exception:
        descs = {}
    for key, forbidden, required in STALE_RULES:
        d = descs.get(key, '')
        for bad in forbidden:
            if bad in d:
                details['help'].append(f'{key}: stale text still contains {bad!r}')
        for need in required:
            if need not in d:
                details['help'].append(f'{key}: text missing {need!r}')


    # ---- stale numbers: config-driven (help must quote utils.economy) ----
    # Mirror help rendering: lang key first, helpmeta desc_en fallback.
    try:
        from lang import STR as _STR2
        _en = _STR2.get('en') or {}
    except Exception:
        _en = {}

    def _help_text(tkey: str) -> str:
        if tkey in _en:
            return _en[tkey]
        if tkey.startswith('hc_d_'):
            v = COMMAND_META.get(tkey[5:])
            if v:
                return v[1]
        return ''

    for tkey, needles in _config_text_rules():
        d = _help_text(tkey)
        for need in needles:
            if need.lower() not in d.lower():
                details['help'].append(f'{tkey}: text missing config value {need!r}')

    # ---- navigation targets (must resolve to canonical commands) ----
    try:
        from cogs.help import HELP_GROUPS, resolve_command
    except Exception:
        HELP_GROUPS, resolve_command = {}, None
    if resolve_command is not None:
        for key, grp in HELP_GROUPS.items():
            # resolve_command only matches canonical .name entries, so a
            # resolved primary is canonical by construction — aliases in
            # metadata fail here instead of pointing users at shadows.
            for dotted in grp.get('primary', ()):
                if resolve_command(bot, dotted) is None:
                    details['help'].append(f'nav: {key!r} primary {dotted!r} does not resolve')
            for rel in grp.get('related', ()):
                if rel not in HELP_GROUPS:
                    details['help'].append(f'nav: {key!r} related {rel!r} is not a group')
    # ---- runtime safety (suspected; static scan) ----
    # ---- runtime safety (suspected; static scan) ----
    for q, c in cmds:
        src = _src(c)
        if not src:
            continue
        touches = []
        if _hits(src, MONEY_PATTERNS):
            touches.append('currency')
            # take_cash is atomic by construction. A direct set_cash is only
            # racy with an await between the balance read and the write.
            if 'take_cash(' not in src and _racy_write(src):
                details['safety'].append(f'{q}: suspected non-atomic balance write')
        if _hits(src, ITEM_PATTERNS):
            touches.append('items')
        if _hits(src, XP_PATTERNS):
            touches.append('xp')
        if _hits(src, ROLE_PATTERNS):
            touches.append('roles')
        if touches and 'currency' in touches:
            details['currency'].append(f'{q}: touches {", ".join(touches)} (review logging)')
            # Shared-config coverage: the constant names live in
            # utils.economy, but most call sites read them via catalogs
            # built at module import — so a module importing utils.economy
            # counts its currency commands as migrated.
            try:
                import sys as _sys
                mod_src = inspect.getsource(_sys.modules.get(c.callback.__module__)) \
                    if getattr(c.callback, '__module__', None) else ''
            except Exception:
                mod_src = ''
            if 'utils.economy' in (mod_src or ''):
                details['config'].append(f'{q}: uses shared config')
            elif q in REVIEWED_LOCAL:
                details['config'].append(f'{q}: reviewed local — {REVIEWED_LOCAL[q]}')
            else:
                details['config'].append(f'{q}: hardcoded values (not yet migrated)')
    for hit in _price_literal_hits():
        details['config'].append(f'{hit} (manual review)')
    n_cooldown = sum(1 for _, c in cmds if getattr(c, '_cooldown', None) is not None)
    n_checks = [(q, len(getattr(c, 'checks', None) or [])) for q, c in cmds]
    gated = sorted(q for q, n in n_checks if n > 0)

    summary = {
        'canonical': len(top),
        'subcommands': len(subs),
        'aliases': sum(len(getattr(c, 'aliases', None) or []) for _, c in cmds),
        'missing_impl': len([l for l in details['registry'] if 'no implementation' in l]),
        'unregistered': len([l for l in details['registry'] if 'no help entry' in l]),
        'alias_issues': len(details['aliases']),
        'help_issues': len(details['help']),
        'currency_touching': len(details['currency']),
        'config_using': len([l for l in details['config'] if 'uses shared config' in l]),
        'config_pending': len([l for l in details['config'] if 'not yet migrated' in l]),
        'price_literals': len(_price_literal_hits()),
        'safety_flags': len(details['safety']),
        'with_cooldowns': n_cooldown,
        'gated': len(gated),
    }
    try:
        _ct, _cu, _ch, _chits, _cf, _cnotes = _crime_status(bot)
    except Exception:
        _ct, _cu, _ch, _chits, _cf, _cnotes = 0, 0, 0, [], 0, ['crime audit failed']
    summary.update({
        'crime_values': _ct,
        'crime_using': _cu,
        'crime_handlers': _ch,
        'crime_hardcoded': len(_chits),
        'crime_formula': _cf,
    })
    for line in _chits:
        details['crime'].append(f'{line} (manual review)')
    for note in _cnotes:
        details['crime'].append(note)
    board = _systems_board(cmds, details, summary)
    summary['systems'] = board
    return summary, details


SYSTEM_ORDER = ('moderation', 'protection', 'levels', 'voice', 'info', 'setup',
                'fun', 'economy', 'pokemon')
# Human annotations shown instead of counts. Edit deliberately, rarely.
SYSTEM_NOTES = {'pokemon': 'still working on'}
# Nav help-group -> board row for nav-target failures.
NAV_ROW = {'economy': 'economy', 'pokemon': 'pokemon', 'market': 'economy',
           'casino': 'economy', 'crime': 'economy', 'profile': 'levels',
           'server': 'setup'}
# Loaded-cog filename -> board row for price-literal hits.
PRICE_ROW = {'shop': 'economy', 'pokemon': 'pokemon'}


def _board_top(line: str) -> str:
    """Owning top-level command name for a detail line."""
    import re
    m = re.match(r"help alias '[^']+' of '([^']+)'", line)
    if m:
        return m.group(1)
    m = re.match(r"'([^']+)'", line)
    if m:
        return m.group(1)
    return line.split(':')[0].strip().split(' ')[0]


def _system_for(top: str, by_qname: dict):
    cmd = by_qname.get(top)
    if cmd is not None and getattr(getattr(cmd, 'cog', None), 'qualified_name', '') == 'Pokemon':
        return 'pokemon'
    v = COMMAND_META.get(top)
    if v and v[0] in CATEGORIES:
        return v[0]
    if cmd is not None:
        low = (getattr(getattr(cmd, 'cog', None), 'qualified_name', '') or '').lower()
        if low in CATEGORIES:
            return low
    return None


def _systems_board(cmds, details, summary):
    """Per-system (red, yellow) finding counts. Red = proven-broken or
    dispatch-affecting (real alias collisions, hardcoded prices, dead nav
    targets). Yellow = backlog and suspicion (renames, help gaps, safety
    shapes, pending migrations). Informational lines (shared-config usage,
    reviewed-local, currency inventory) never affect dots."""
    import re
    by_qname = {}
    for q, c in cmds:
        by_qname.setdefault(q, c)
        by_qname.setdefault(q.split(' ')[0], c)
    red = {s: 0 for s in SYSTEM_ORDER}
    yellow = {s: 0 for s in SYSTEM_ORDER}

    def bump(sys, is_red):
        if sys in red:
            if is_red:
                red[sys] += 1
            else:
                yellow[sys] += 1

    for line in details.get('aliases', []):
        bump(_system_for(_board_top(line), by_qname), True)
    for line in details.get('safety', []):
        bump(_system_for(_board_top(line), by_qname), False)
    for line in details.get('registry', []):
        bump(_system_for(_board_top(line), by_qname), False)
    for line in details.get('help', []):
        m = re.match(r"nav: '(\w+)'", line)
        if m:
            bump(NAV_ROW.get(m.group(1)), True)
        else:
            bump(_system_for(_board_top(line), by_qname), False)
    for line in details.get('config', []):
        if 'not yet migrated' in line:
            bump(_system_for(_board_top(line), by_qname), False)
        elif 'manual review' in line and '.py:' in line:
            m = re.search(r'(\w+)\.py:', line)
            if m:
                bump(PRICE_ROW.get(m.group(1)), True)
    for line in details.get('crime', []):
        bump(_system_for(_board_top(line), by_qname), False)
    if summary.get('crime_hardcoded'):
        bump('economy', True)
    out = []
    for s in SYSTEM_ORDER:
        n = red[s] + yellow[s]
        dot = '🔴' if red[s] else ('🟡' if yellow[s] else '🟢')
        note = SYSTEM_NOTES.get(s)
        tail = f' ({note})' if note else (f' ({n} open)' if n else '')
        out.append((s, dot, tail))
    return out


def _report_lines(summary, details, filt_cat=None, filt_issue=None):
    lines = ['Command Health Report', '---------------------']
    lines.append(f"Canonical: {summary['canonical']} · subcommands: {summary['subcommands']} "
                 f"· aliases: {summary['aliases']}")
    lines.append(f"Missing implementation: {summary['missing_impl']} · "
                 f"unregistered: {summary['unregistered']}")
    lines.append(f"Alias issues: {summary['alias_issues']} · help issues: {summary['help_issues']}")
    lines.append(f"Currency-touching: {summary['currency_touching']} · "
                 f"safety flags: {summary['safety_flags']}")
    lines.append(f"Economy config: {summary['config_using']} using shared config · "
                 f"{summary['config_pending']} not yet migrated")
    lines.append(f"Hardcoded shop prices: {summary['price_literals']}")
    lines.append('-# poke_extras.py excluded (unloaded cog): manual review required')
    lines.append(f"Crime Configuration{chr(10)}--------------------")
    lines.append(f"Crime monetary values: {summary['crime_values']}")
    lines.append(f"Using shared config: {summary['crime_using']}/{summary['crime_handlers']}")
    lines.append(f"Hardcoded crime prices: {summary['crime_hardcoded']}")
    lines.append(f"Formula parameters: {summary['crime_formula']}")
    lines.append(f"Chance/duration values excluded: {len(CRIME_EXCLUDED)}")
    lines.append('Manual review: 1 (bounty escrow has no expiry — documented in code)')
    lines.append(f"With cooldowns: {summary['with_cooldowns']} · gated: {summary['gated']}")
    if filt_cat is None and filt_issue is None:
        lines.append('Systems')
        lines.append('-------')
        for s, dot, tail in summary.get('systems', []):
            lines.append(f'{s} {dot}{tail}')
    picked = []
    if filt_issue and filt_issue not in ('registry', 'aliases', 'help', 'currency', 'safety',
                                         'config', 'crime'):
        lines.append(f'\nUnknown issue. Try: registry, aliases, help, currency, safety, config, crime')
        return '\n'.join(lines)
    if filt_issue in ('registry', 'aliases', 'help', 'currency', 'safety', 'config', 'crime'):
        picked = [(filt_issue, l) for l in details.get(filt_issue, [])]
    elif filt_cat:
        for issue, ls in details.items():
            for l in ls:
                name = l.split(':')[0]
                v = COMMAND_META.get(name)
                if v and v[0] == filt_cat:
                    picked.append((issue, l))
    else:
        for issue in ('registry', 'aliases', 'help', 'currency', 'safety'):
            picked.extend((issue, l) for l in details.get(issue, [])[:8])
    if picked:
        lines.append('')
        for issue, l in picked[:30]:
            lines.append(f'[{issue}] {l}')
        if len(picked) > 30:
            lines.append(f'… +{len(picked) - 30} more in this view')
    elif filt_issue or filt_cat:
        lines.append('\nStatus: clean for this filter')
    else:
        n_open = sum(len(details.get(i, [])) for i in ('registry', 'aliases', 'help', 'currency', 'safety'))
        lines.append(f'\nStatus: {n_open} issues require review' if n_open else '\nStatus: clean')
    return '\n'.join(lines)


class Health(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='health', description='Dev health audits')
    @staff_or('administrator')
    async def health(self, ctx):
        await ctx.reply('.health commands [category:<mod>] [issue:<help|aliases|registry|currency|safety|config|crime>]',
                        ephemeral=True)

    @health.command(name='commands', description='Command registry audit')
    @staff_or('administrator')
    async def health_commands(self, ctx, *, args: str = ''):
        set_ctx_lang(ctx.author)
        filt_cat, filt_issue = None, None
        for tok in (args or '').split():
            if tok.startswith('category:'):
                filt_cat = tok.split(':', 1)[1].lower()
            elif tok.startswith('issue:'):
                filt_issue = tok.split(':', 1)[1].lower()
        if filt_cat and filt_cat not in CATEGORIES:
            return await ctx.reply(f'Unknown category. Try: {", ".join(sorted(CATEGORIES))}',
                                   ephemeral=True)
        try:
            summary, details = audit(self.bot)
            text = _report_lines(summary, details, filt_cat, filt_issue)
        except Exception as e:
            return await ctx.reply(f'health broke: {type(e).__name__}: {e}', ephemeral=True)
        if len(text) > 3900:
            text = text[:3900] + '\n… truncated'
        await ctx.reply(f'```{text}```', ephemeral=True)


async def setup(bot):
    await bot.add_cog(Health(bot))

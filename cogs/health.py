"""Command health: read-only audits over the live registry, help coverage,
stale numbers and runtime safety. Admin-only dev tool — never auto-fixes.

Usage: `.health commands [category:<mod>] [issue:<help|aliases|registry|currency|safety>]`
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

META = json.loads((Path(__file__).parent.parent / 'helpmeta.json').read_text(encoding='utf-8'))
COMMAND_META = META['meta']
CATEGORIES = META['cats']

# Curated stale-number rules: (helpmeta key, forbidden substrings, required substrings).
# Each is a regression test for a number we already got wrong once.
STALE_RULES = [
    ('blackjack', ['max 15000'], []),
    ('nick', ['100k'], []),
    ('grazz', ['3k'], ['4k']),
    ('wordle', ['6 tries', '.wordle <guess>'], ['channel']),
]

# Source patterns marking runtime impact. Findings are SUSPECTED (static
# scan, not business-logic proof) unless confirmed by review.
MONEY_PATTERNS = ('set_cash(', 'take_cash(', 'UPDATE eco SET cash', 'UPDATE eco SET bank')
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
    for al, owners in sorted(alias_owners.items()):
        if len(owners) > 1:
            details['aliases'].append(f"'{al}' claimed by: {', '.join(sorted(owners))}")
    for al, owners in sorted(alias_owners.items()):
        if al in actual_names and al not in {o.split(' ')[0] for o in owners}:
            details['aliases'].append(f"'{al}' shadowed: also a canonical command")
    # helpmeta aliases pointing at another canonical name (unreachable via help lookup)
    for n, v in COMMAND_META.items():
        for a in (v[2] or []):
            if str(a).lower() in actual_names and str(a).lower() != n.lower():
                details['aliases'].append(f"help alias '{a}' of '{n}' shadows canonical '{a}'")

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
    try:
        from lang import STR
        en_keys = set((STR.get('en') or {}).keys())
    except Exception:
        en_keys = set()
    for q, _ in top:
        if q in meta_names and f'hc_d_{q}' not in en_keys:
            details['help'].append(f'{q}: no localized help text (hc_d_{q})')

    # ---- stale numbers (curated regression rules) ----
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

    # ---- runtime safety (suspected; static scan) ----
    for q, c in cmds:
        src = _src(c)
        if not src:
            continue
        touches = []
        if _hits(src, MONEY_PATTERNS):
            touches.append('currency')
            # Direct assignment is only racy with an await between read and
            # write (separate events can interleave); atomic take_cash is
            # always safe. Anything else here is suspected, not proven.
            if 'set_cash(' in src and 'take_cash(' not in src and 'await' in src:
                details['safety'].append(f'{q}: suspected non-atomic balance write')
        if _hits(src, ITEM_PATTERNS):
            touches.append('items')
        if _hits(src, XP_PATTERNS):
            touches.append('xp')
        if _hits(src, ROLE_PATTERNS):
            touches.append('roles')
        if touches and 'currency' in touches:
            details['currency'].append(f'{q}: touches {", ".join(touches)} (review logging)')
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
        'safety_flags': len(details['safety']),
        'with_cooldowns': n_cooldown,
        'gated': len(gated),
    }
    return summary, details


def _report_lines(summary, details, filt_cat=None, filt_issue=None):
    lines = ['Command Health Report', '---------------------']
    lines.append(f"Canonical: {summary['canonical']} · subcommands: {summary['subcommands']} "
                 f"· aliases: {summary['aliases']}")
    lines.append(f"Missing implementation: {summary['missing_impl']} · "
                 f"unregistered: {summary['unregistered']}")
    lines.append(f"Alias issues: {summary['alias_issues']} · help issues: {summary['help_issues']}")
    lines.append(f"Currency-touching: {summary['currency_touching']} · "
                 f"safety flags: {summary['safety_flags']}")
    lines.append(f"With cooldowns: {summary['with_cooldowns']} · gated: {summary['gated']}")
    picked = []
    if filt_issue in ('registry', 'aliases', 'help', 'currency', 'safety'):
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
        total = sum(len(details.get(i, [])) for i in ('registry', 'aliases', 'help', 'currency', 'safety'))
        if total > len(picked):
            lines.append(f'… +{total - len(picked)} more (narrow with issue: / category:)')
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
        await ctx.reply('.health commands [category:<mod>] [issue:<help|aliases|registry|currency|safety>]',
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

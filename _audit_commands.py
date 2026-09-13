"""One-off audit: classify every command decorator in cogs/*.py via AST."""
import ast
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).parent
COGS = ROOT / 'cogs'


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


def kwarg(dec, name, default=None):
    try:
        for k in dec.keywords:
            if k.arg == name:
                return ast.literal_eval(k.value)
    except Exception:
        pass
    return default


def classify_cog_file(path):
    src = path.read_text(encoding='utf-8-sig', errors='replace')
    tree = ast.parse(src)
    out = {'file': path.name, 'slash_top': [], 'prefix_top': [],
           'slash_groups': {}, 'prefix_groups': {}, 'slash_subs': [], 'prefix_subs': []}

    def dn_of(dec):
        call = dec if isinstance(dec, ast.Call) else None
        return dotted(dec.func if call is not None else dec), call

    # pass 1: group parents (functions decorated as groups, or Group assignments)
    parents = {}
    group_subs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                dn, _ = dn_of(d)
                if dn.endswith('hybrid_group'):
                    parents[node.name] = 'hybrid'
                elif dn == 'commands.group':
                    parents[node.name] = 'prefix'
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            full = dotted(node.value.func)
            if full.endswith('Group'):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        parents[tgt.id] = 'hybrid' if ('app_commands' in full or 'hybrid' in full) else 'prefix'

    # pass 2: classify every decorated function
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            dn, call = dn_of(d)
            name = (kwarg(call, 'name') if call is not None else None) or node.name
            if dn.endswith('hybrid_command'):
                out['slash_top'].append(name)
            elif dn.endswith('hybrid_group'):
                pass  # group parent; counted via slash_groups with its subs
            elif dn in ('app_commands.command', 'bot.tree.command', 'tree.command'):
                out['slash_top'].append(name)
            elif dn == 'commands.command':
                out['prefix_top'].append(name)
            elif dn == 'commands.group':
                pass
            else:
                m = re.fullmatch(r'([A-Za-z_]\w*)\.(command|group)', dn)
                if m:
                    parent = m.group(1)
                    if parent in parents:
                        key = 'slash' if parents[parent] == 'hybrid' else 'prefix'
                        group_subs.setdefault((parent, key), []).append(name)
                        out[f'{key}_subs'].append(f'{parent} {name}')
                    else:
                        out['prefix_subs'].append(f'{parent} {name}')

    for (g, key), subs in group_subs.items():
        out[f'{key}_groups'][g] = subs
    # assignment-based groups with no subs still count as one entry
    for g, kind in parents.items():
        bucket = out[('slash' if kind == 'hybrid' else 'prefix') + '_groups']
        if g not in bucket:
            bucket[g] = []
    return out


totals = {'slash_top': 0, 'slash_groups': 0, 'slash_subs': 0, 'prefix_top': 0, 'prefix_subs': 0}
report = []
for f in sorted(COGS.glob('*.py')):
    r = classify_cog_file(f)
    if not any([r['slash_top'], r['prefix_top'], r['slash_subs'], r['prefix_subs'], r['slash_groups'], r['prefix_groups']]):
        continue
    report.append(r)
    totals['slash_top'] += len(r['slash_top'])
    totals['slash_groups'] += len(r['slash_groups'])
    totals['slash_subs'] += len(r['slash_subs'])
    totals['prefix_top'] += len(r['prefix_top'])
    totals['prefix_subs'] += len(r['prefix_subs'])
    print(f"== {r['file']}")
    if r['slash_top']:
        print(f"   slash top   ({len(r['slash_top'])}): {', '.join(r['slash_top'])}")
    if r['slash_groups']:
        for g, subs in r['slash_groups'].items():
            print(f"   slash group /{g} ({len(subs)} subs): {', '.join(subs)}")
    if r['slash_subs'] and not r['slash_groups']:
        print(f"   slash subs: {', '.join(r['slash_subs'])}")
    if r['prefix_top']:
        print(f"   prefix top  ({len(r['prefix_top'])}): {', '.join(r['prefix_top'])}")
    if r['prefix_groups']:
        for g, subs in r['prefix_groups'].items():
            print(f"   prefix group .{g} ({len(subs)} subs): {', '.join(subs)}")
    if r['prefix_subs'] and not r['prefix_groups']:
        print(f"   prefix subs: {', '.join(r['prefix_subs'])}")

top_level_slash = totals['slash_top'] + totals['slash_groups']
print('\n---- TOTALS ----')
print(f"top-level slash commands (incl. group parents): {top_level_slash}")
print(f"slash subcommands (do NOT count vs 200):        {totals['slash_subs']}")
print(f"prefix-only top commands:                       {totals['prefix_top']}")
print(f"prefix-only subs:                               {totals['prefix_subs']}")

# helpmeta inventory
try:
    meta = json.loads((ROOT / 'helpmeta.json').read_text(encoding='utf-8-sig'))
    entries = meta.get('meta', meta)
    by_cog = {}
    for name, val in entries.items():
        cog = val[0] if isinstance(val, list) else '?'
        by_cog.setdefault(cog, []).append(name)
    print(f"\nhelpmeta.json: {len(entries)} commands total")
    for cog, names in sorted(by_cog.items()):
        print(f"  {cog}: {len(names)}")
except Exception as e:
    print(f'helpmeta parse failed: {e}')

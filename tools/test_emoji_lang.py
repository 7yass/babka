"""Visual-language conformance, patch 1: rock mapping + emoji resolver.

Stdlib only. No discord/turso/app imports (TYPE_EMOJI is read via ast).
Exit code 0 only when every test passes.
"""
import ast
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import utils.emojis as E

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    safe = msg.encode('ascii', 'backslashreplace').decode()
    print(('PASS ' if cond else 'FAIL ') + safe, flush=True)
    if not cond:
        FAILS.append(msg)


def type_emoji() -> dict:
    """Read TYPE_EMOJI without importing cogs.pokemon (imports discord)."""
    tree = ast.parse((ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') == 'TYPE_EMOJI':
            return ast.literal_eval(node.value)
    raise AssertionError('TYPE_EMOJI not found')


def use_fixture(payload) -> tempfile.TemporaryDirectory:
    """Point the resolver at temp registry data. Payload may be a dict
    (written as JSON) or a raw string (e.g. malformed JSON)."""
    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / 'emoji_ids.json'
    if isinstance(payload, str):
        p.write_text(payload, encoding='utf-8')
    elif payload is None:
        pass  # absent file
    else:
        p.write_text(json.dumps(payload), encoding='utf-8')
    E.IDS_FILE = p
    E._MAP, E._MAP_TS = {}, 0.0
    return tmp


A, B = '111111111111111111', '222222222222222222'
FIRE_A = 333333333333333333
SHINY_B = 444444444444444444
SHARDED = {'guilds': {A: {'fire': FIRE_A}, B: {name: SHINY_B for name in ['rarity_shiny']}}}


def test_rock_mapped() -> None:
    m = type_emoji()
    check(m.get('rock') == 'rock', 'TYPE_EMOJI["rock"] == "rock"')
    check(all(isinstance(v, str) and v for v in m.values()),
          'all TYPE_EMOJI values are non-empty strings')


def test_missing_file_safe() -> None:
    tmp = use_fixture(None)
    try:
        check(E.em(A, 'fire', fallback='fb') == 'fb', 'absent file: em() returns fallback')
        check(E.emoji_id('fire', A) is None, 'absent file: emoji_id() is None')
        check(E.merged_map() == {}, 'absent file: merged_map() is empty')
    finally:
        tmp.cleanup()


def test_malformed_safe() -> None:
    tmp = use_fixture('{not json')
    try:
        check(E.em(A, 'fire', fallback='fb') == 'fb', 'malformed JSON: em() returns fallback')
        check(E.emoji_id('fire', A) is None, 'malformed JSON: emoji_id() is None')
        check(E.merged_map() == {}, 'malformed JSON: merged_map() is empty')
    finally:
        tmp.cleanup()


def test_per_guild_lookup() -> None:
    tmp = use_fixture(SHARDED)
    try:
        check(E.em(A, 'fire') == f'<:fire:{FIRE_A}>', 'per-guild lookup hits own host')
        check(E.emoji_id('fire', A) == FIRE_A, 'emoji_id() per-guild numeric id')
    finally:
        tmp.cleanup()


def test_global_fallback() -> None:
    tmp = use_fixture(SHARDED)
    try:
        check(E.em(A, 'rarity_shiny') == f'<:rarity_shiny:{SHINY_B}>',
              'global fallback finds emoji on another host')
        check(E.emoji_id('rarity_shiny', '999999999999999999') == SHINY_B,
              'emoji_id() falls back across guilds')
        check(E.merged_map().get('fire') == FIRE_A
              and E.merged_map().get('rarity_shiny') == SHINY_B,
              'merged_map() flattens all hosts')
    finally:
        tmp.cleanup()


def test_missing_emoji_fallback() -> None:
    tmp = use_fixture(SHARDED)
    try:
        check(E.em(A, 'nope', fallback='fb') == 'fb', 'unknown name: em() returns fallback')
        check(E.em(A, '', fallback='fb') == 'fb', 'empty name: em() returns fallback')
        check(E.emoji_id('nope', A) is None, 'unknown name: emoji_id() is None')
    finally:
        tmp.cleanup()


def test_id_format() -> None:
    pat = re.compile(r'^<:([a-z0-9_]{2,32}):(\d{17,20})>$')
    tmp = use_fixture(SHARDED)
    try:
        tokens = [E.em(A, 'fire'), E.em(B, 'rarity_shiny')]
        ok = all(pat.match(t) for t in tokens)
        check(ok, 'resolved tokens use 17-20 digit Discord ids')
        check(all(17 <= len(str(E.emoji_id(n, A) or '')) <= 20
                  for n in ('fire', 'rarity_shiny')),
              'emoji_id() values are 17-20 digits')
    finally:
        tmp.cleanup()


_PURE = None


def pure():
    """Exec the DB-free render helpers from cogs.pokemon without importing
    discord: hp_dot, xp_bar, mon_name, _hit_tag (+ _dex_row for names)."""
    global _PURE
    if _PURE is not None:
        return _PURE
    import types
    import database as DB
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    wanted = {'hp_dot', 'xp_bar', 'mon_name', '_hit_tag', '_dex_row',
              '_move_emoji_name', '_switch_emoji_name', '_picker_emoji_name',
              '_weather_line', '_rarity_line', '_tier_word', 'rarity_of', 'showdown_gif',
              'streak_get', '_balls_left_line', 'balls_get', 'region_of',
              '_box_tier', '_box_match', '_box_slots', '_box_sort_entries',
              '_box_cid', '_box_parse', '_species_emoji_name',
              '_match_mon', '_evo_text', 'form_of', 'form_emo', 'form_sprite',
              '_tier_letter', '_plain_name', '_safe_moves', '_turn_safe',
              '_roll_ivs', '_ivs_of', '_iv_pct', 'calc_stats',
              '_arena_forest', '_arena_cave', 'moveset_for', 'wild_image',
              '_sky_grass', '_platform'}
    import lang as LANG
    import random as _rnd
    mod = types.ModuleType('pkpure')
    mod.__dict__['em'] = E.em
    mod.__dict__['db'] = DB
    mod.__dict__['t'] = LANG.t
    mod.__dict__['get_lang'] = LANG.get_lang
    mod.__dict__['random'] = _rnd
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<pkpure>', 'exec'),
                 mod.__dict__)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') in (
                    'TYPE_EMOJI', 'WEATHER_LINE', 'RARITY_RATE', 'BALLS', 'BALL_FLEET',
                    'TIER_WORD', 'RARITY_COMMON', 'RARITY_UNCOMMON', 'RARITY_RARE',
                    'RARITY_LEGENDARY', 'RARITY_SHINY', 'BOX_SORTS', 'REGIONS',
                    '_BOX_RANK', 'FORMS', 'FORM_SUFFIX', '_TIER_LETTER', 'ARENAS',
                    '_TACKLE', 'IV_KEYS'):
            mod.__dict__[node.targets[0].id] = ast.literal_eval(node.value)
    _PURE = mod
    return mod


BANNED = '⭐✨▶🟢🟡🔴⚪▓░💥🛡🧪✅⬜❤🌧☀🔵🟣🔶'


def test_patch2_names_and_markers() -> None:
    gid = '111111111111111111'
    emo_map = {'guilds': {gid: {
        'rarity_shiny': 444444444444444444,
        'hp_full': 555555555555555555, 'hp_mid': 555555555555555556,
        'hp_low': 555555555555555557, 'hp_empty': 555555555555555558,
        'xp_full': 555555555555555559, 'xp_empty': 555555555555555560,
        'hit_super': 555555555555555561, 'shield': 555555555555555562,
        'hit_crit': 555555555555555563, 'hit_miss': 555555555555555564,
    }}}
    tmp = use_fixture(emo_map)
    try:
        p = pure()
        mon = {'dex': 999999, 'nick': 'Testy', 'shiny': 1}
        check(p.mon_name(mon, gid) == '<:rarity_shiny:444444444444444444>Testy',
              'mon_name shiny resolves custom marker')
        check(p.mon_name({'dex': 999999, 'nick': 'Testy', 'shiny': 0}, gid) == 'Testy',
              'mon_name non-shiny unchanged')
        check(p.hp_dot(0.9, False, gid) == '<:hp_full:555555555555555555>'
              and p.hp_dot(0.3, False, gid) == '<:hp_mid:555555555555555556>'
              and p.hp_dot(0.1, False, gid) == '<:hp_low:555555555555555557>'
              and p.hp_dot(0.9, True, gid) == '<:hp_empty:555555555555555558>',
              'hp_dot thresholds resolve customs')
        bar = p.xp_bar(6, 12, gid, width=4)
        check(bar == '<:xp_full:555555555555555559>' * 2 + '<:xp_empty:555555555555555560>' * 2,
              'xp_bar segments resolve customs')
        check(p._hit_tag(gid, 2.0, False, 10) == ' <:hit_super:555555555555555561>',
              'hit tag super-effective')
        check(p._hit_tag(gid, 0.5, False, 10) == ' <:shield:555555555555555562>',
              'hit tag resisted')
        check(p._hit_tag(gid, 1.0, False, 10) == '', 'hit tag neutral is empty')
        check(p._hit_tag(gid, 2.0, True, 10).endswith('<:hit_crit:555555555555555563>'),
              'hit tag crit appended')
        check(p._hit_tag(gid, 2.0, False, 0) == ' <:hit_miss:555555555555555564>',
              'hit tag miss on zero damage')
    finally:
        tmp.cleanup()


def test_patch2_ascii_fallbacks() -> None:
    tmp = use_fixture({'guilds': {}})
    try:
        p = pure()
        gid = '111111111111111111'
        check(p.mon_name({'dex': 999999, 'nick': 'Testy', 'shiny': 1}, gid) == '*Testy',
              'mon_name shiny falls back to ASCII')
        check(p.hp_dot(0.9, False, gid) == 'O' and p.hp_dot(0.3, False, gid) == '-'
              and p.hp_dot(0.1, False, gid) == '!' and p.hp_dot(0.0, True, gid) == 'x',
              'hp_dot ASCII fallbacks')
        check(p.xp_bar(6, 12, gid, width=4) == '##--', 'xp_bar ASCII fallbacks')
        check(p._hit_tag(gid, 2.0, False, 10) == ' !', 'hit super ASCII fallback')
        check(p._hit_tag(gid, 0.5, False, 10) == ' =', 'hit resist ASCII fallback')
        check(p._hit_tag(gid, 1.0, True, 10) == ' CRIT', 'hit crit ASCII fallback')
        check(p._hit_tag(gid, 9.0, False, 0) == ' MISS', 'hit miss ASCII fallback')
        check(all(c not in BANNED for c in
                  p.mon_name({'dex': 1, 'nick': 'X', 'shiny': 1}, gid)
                  + p.hp_dot(0.9, False, gid) + p.xp_bar(1, 2, gid)
                  + p._hit_tag(gid, 2.0, True, 5)),
              'no banned Unicode in helper output')
    finally:
        tmp.cleanup()


def test_button_decisions() -> None:
    gid = '111111111111111111'
    tmp = use_fixture({'guilds': {gid: {'btn_fight': 666666666666666666,
                                        'fire': 333333333333333333}}})
    try:
        p = pure()
        check(p._move_emoji_name({'ptype': 'fire', 'name': 'x', 'power': 1}) == 'fire',
              'move button maps fire type icon')
        check(p._move_emoji_name({'ptype': '???'}) == '', 'unknown move type maps to no icon')
        check(p._switch_emoji_name(True, False) == 'slot_active', 'switch: active marker')
        check(p._switch_emoji_name(False, True) == 'faint_dot', 'switch: fainted marker')
        check(p._switch_emoji_name(False, False) == 'switch_dot', 'switch: benched marker')
        check(p._picker_emoji_name({'dex': 999999}, True) == 'slot_active',
              'picker: active marker')
        check(p._picker_emoji_name({'dex': 999999}, False) == '',
              'picker: unknown dex maps to no icon')
    finally:
        tmp.cleanup()


def test_button_source_hygiene() -> None:
    import re as _re
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    emoji_kwargs = _re.findall(r'emoji\s*=\s*([^\n,)]+)', src)
    check(bool(emoji_kwargs), 'button emoji kwargs exist')
    check(all('<:' not in v and not v.strip().startswith(("'", '"'))
              and '_btn_emoji(' in v for v in emoji_kwargs),
          'every button emoji is a resolver call (never a raw token)')
    live_tokens = [ln for ln in src.splitlines() if '<:' in ln
                   and 're.sub' not in ln and '\\d' not in ln
                   and not ln.strip().startswith(('#', '"', "'"))]
    check(not live_tokens, 'no raw <:name:id> tokens in live code paths')
    label_lines = [ln for ln in src.splitlines() if 'label=' in ln]
    check(all(c not in BANNED for ln in label_lines for c in ln),
          'no banned Unicode in button label lines')
    for cid in ('pkfight:', 'pkball:', 'pklead:', 'pkmv:', "'pk_potion'",
                "'pk_ball'", "'pk_run'", 'pksw:', 'pkd:', 'pkdsw:', 'pkbox:', 'pkstart:'):
        check(cid in src, f'custom_id family intact: {cid}')


def test_partialemoji_shape() -> None:
    try:
        import discord
    except Exception:
        print('SKIP test_partialemoji_shape (discord not installed)', flush=True)
        return
    pe = discord.PartialEmoji(name='btn_fight', id=666666666666666666)
    check(pe.name == 'btn_fight' and pe.id == 666666666666666666,
          'PartialEmoji carries resolved name+id')
    check(str(pe) == '<:btn_fight:666666666666666666>',
          'PartialEmoji renders standard token')
    b = discord.ui.Button(label='FIGHT', emoji=None)
    check(b.emoji is None, 'button without id can be sent bare')


def test_weather_emojis() -> None:
    gid = '111111111111111111'
    tmp = use_fixture({'guilds': {gid: {'weather_rain': 777777777777777777,
                                        'weather_sun': 777777777777777778}}})
    try:
        p = pure()
        rain = p._weather_line(gid, 'rain')
        sun = p._weather_line(gid, 'sun')
        check(rain.startswith('<:weather_rain:777777777777777777> ')
              and rain.endswith('Water moves hit harder.'),
              'rain resolves custom icon + keeps text')
        check(sun.startswith('<:weather_sun:777777777777777778> ')
              and sun.endswith('Fire moves hit harder.'),
              'sun resolves custom icon + keeps text')
        check(p._weather_line(gid, 'hail') == '', 'unknown weather renders nothing')
    finally:
        tmp.cleanup()


def test_weather_plain_fallback() -> None:
    tmp = use_fixture({'guilds': {}})
    try:
        p = pure()
        gid = '111111111111111111'
        rain = p._weather_line(gid, 'rain')
        check(rain == 'Rain! Water moves hit harder.', 'missing rain icon falls back to plain text')
        check(all(c not in '🌧☀' for c in rain + p._weather_line(gid, 'sun')),
              'no weather glyphs in fallback output')
    finally:
        tmp.cleanup()


def test_quest_bars_and_economy() -> None:
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check('▰' not in src and '▱' not in src, 'no ▰▱ bars remain in pokemon.py')
    check('🌧' not in src and '☀' not in src, 'no weather glyphs remain in pokemon.py')
    tree = ast.parse(src)
    vals = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') in ('QUEST_TIERS', 'QUEST_REWARDS'):
            vals[node.targets[0].id] = ast.literal_eval(node.value)
    check(vals.get('QUEST_TIERS') == [10, 30, 75, 150, 300], 'quest tiers unchanged')
    check(vals.get('QUEST_REWARDS') == [2000, 6000, 15000, 40000, 100000],
          'quest rewards unchanged')
    check("em(gid, 'xp_full')" in src and "em(gid, 'xp_empty')" in src,
          'quest bars use xp_full/xp_empty')
    import json as _json
    lang = _json.loads((ROOT / 'lang.json').read_text(encoding='utf-8'))
    for section in ('en', 'pl'):
        check('🌧' not in lang[section]['eco.pk_wx_rain']
              and '☀' not in lang[section]['eco.pk_wx_sun'],
              f'weather strings glyph-free ({section})')


def ach_data():
    """BADGES + PK_FALLBACK + badge_icon from achievements.py without
    importing discord."""
    import types
    src = (ROOT / 'cogs' / 'achievements.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    mod = types.ModuleType('pkach')
    mod.__dict__['em'] = E.em
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') in ('BADGES', 'PK_FALLBACK'):
            mod.__dict__[node.targets[0].id] = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == 'badge_icon':
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<pkach>', 'exec'),
                 mod.__dict__)
    return mod


def test_achievement_icons() -> None:
    gid = '111111111111111111'
    ids = {'star': 888888888888888881, 'trophy': 888888888888888882,
           'region_kanto': 888888888888888883, 'region_johto': 888888888888888884,
           'region_hoenn': 888888888888888885, 'region_sinnoh': 888888888888888886}
    tmp = use_fixture({'guilds': {gid: ids}})
    try:
        a = ach_data()
        for key, eid in ids.items():
            target = [k for k, v in a.BADGES.items() if v[0] == key]
            check(bool(target), f'achievement key uses fleet name: {key}')
            for k in target:
                check(a.badge_icon(gid, k) == f'<:{key}:{eid}>',
                      f'{k} resolves through shared registry')
        expect_fb = {'lvl10': '*', 'lvl25': '*', 'lvl50': '*', 'npc_champ': 'T',
                     'region_kanto': 'K', 'region_johto': 'J',
                     'region_hoenn': 'H', 'region_sinnoh': 'S'}
        check(a.PK_FALLBACK == expect_fb, 'ASCII fallback map exact')
    finally:
        tmp.cleanup()


def test_achievement_fallbacks_and_data() -> None:
    tmp = use_fixture({'guilds': {}})
    try:
        a = ach_data()
        gid = '111111111111111111'
        check(a.badge_icon(gid, 'region_kanto') == 'K'
              and a.badge_icon(gid, 'npc_champ') == 'T'
              and a.badge_icon(gid, 'lvl25') == '*',
              'missing ids use ASCII fallbacks')
        untouched = {
            'first_job': ('💼', 'HIRED', 'First Job', 'Get hired anywhere.'),
            'grinder': ('🏭', 'GRIND', 'Grinder', 'Work 25 shifts.'),
            'lifer': ('⚒️', 'LIFER', 'Lifer', 'Work 100 shifts.'),
            'rich100k': ('💰', '100K', 'Six Figures', 'Hold 100K cash.'),
            'rich1m': ('💎', '1M', 'Millionaire', 'Hold 1M cash.'),
            'rich5m': ('👑', '5M', 'Mogul', 'Hold 5M cash.'),
            'highroller': ('🎲', 'ROLLER', 'High Roller', 'Buy the High Roller pass.'),
            'famous': ('📣', 'FAMOUS', 'Famous', 'Reach 1K fans.'),
        }
        check(all(tuple(a.BADGES[k]) == v for k, v in untouched.items()),
              'non-Pokemon badges byte-identical')
        for k in ('lvl10', 'lvl25', 'lvl50', 'region_kanto', 'region_johto',
                  'region_hoenn', 'region_sinnoh', 'npc_champ'):
            check(a.BADGES[k][1:] == {
                'lvl10': ('LVL10', 'Rising', 'Reach level 10.'),
                'lvl25': ('LVL25', 'Star', 'Reach level 25.'),
                'lvl50': ('LVL50', 'Legend', 'Reach level 50.'),
                'region_kanto': ('KANTO', 'Kanto Master', 'Finish the Kanto quest track.'),
                'region_johto': ('JOHTO', 'Johto Master', 'Finish the Johto quest track.'),
                'region_hoenn': ('HOENN', 'Hoenn Master', 'Finish the Hoenn quest track.'),
                'region_sinnoh': ('SINNOH', 'Sinnoh Master', 'Finish the Sinnoh quest track.'),
                'npc_champ': ('CHAMP', 'Champion Slayer', 'Beat Champion Cyntia.'),
            }[k], f'{k} name/desc/short preserved')
            check(all(c not in '⭐🌟💫🏆🔴🟡🟢🔵' for c in a.BADGES[k][0]),
                  f'{k} icon holds no legacy glyph')
        src = (ROOT / 'cogs' / 'achievements.py').read_text(encoding='utf-8')
        flat = src.replace('_', '')
        for n in (25, 100, 10, 25, 50, 100000, 1000000, 5000000, 1000):
            check(str(n) in flat, f'progression threshold present: {n}')
    finally:
        tmp.cleanup()


def upre():
    """Pure uploader helpers from cogs/emojis.py without importing discord."""
    import re as _re
    import types
    src = (ROOT / 'cogs' / 'emojis.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    mod = types.ModuleType('upure')
    mod.__dict__['re'] = _re
    mod.__dict__['Path'] = Path
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                '_validate_assets', '_plan', '_check_capacity', '_format_preflight',
                '_ignored_files', '_local_set', '_local_anim', '_pool_row'):
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<upure>', 'exec'),
                 mod.__dict__)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') in (
                    'EMOJI_GUILDS', 'EMOJI_MAX_BYTES', 'EMOJI_NAME_RE'):
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<upure>', 'exec'),
                 mod.__dict__)
    return mod


def _guilds(n=8, limit=50, current=7, nanim=2):
    """Snapshot + plan fixtures. Animated pool holds `nanim` names each."""
    u = upre()
    snaps, plan, aplan = {}, {}, {}
    for i, g in enumerate(u.EMOJI_GUILDS[:n]):
        names = [f'e{i:02d}{j}' for j in range(8)]
        anames = [f'a{i:02d}{j}' for j in range(nanim)]
        have = names[:current] if current <= 8 else names + [f'x{i:02d}{j}' for j in range(current - 8)]
        snaps[str(g)] = {'name': f'G{i}', 'limit': limit, 'static': have,
                         'animated': 1, 'animated_names': anames[:1]}
        plan[str(g)] = names
        aplan[str(g)] = anames
    return snaps, plan, aplan


def test_preflight_fits_all_eight() -> None:
    u = upre()
    snaps, plan, aplan = _guilds()
    # guild 0 already hosts 3 static + 1 animated assigned names -> kept
    snaps[str(u.EMOJI_GUILDS[0])]['static'] = plan[str(u.EMOJI_GUILDS[0])][:3]
    check_result = u._check_capacity(plan, aplan, snaps)
    check(check_result['ok'] is True, 'fitting plan passes all eight servers')
    g0 = check_result['guilds'][str(u.EMOJI_GUILDS[0])]
    check(len(g0['kept']) == 3 and len(g0['new']) == 5, 'same-name assets kept, not re-uploaded')
    check(len(g0['akept']) == 1 and len(g0['anew']) == 1, 'animated kept/new split')
    check(g0['remaining'] == 50 - 8 and g0['aremaining'] == 50 - 2, 'remaining exact')
    check(all(v['ok'] for v in check_result['guilds'].values()), 'every guild row ok')


def test_preflight_exceeds_one_server() -> None:
    u = upre()
    snaps, plan, aplan = _guilds()
    bad = str(u.EMOJI_GUILDS[3])
    snaps[bad] = {'name': 'Small', 'limit': 7, 'static': [], 'animated': 0, 'animated_names': []}
    check_result = u._check_capacity(plan, aplan, snaps)
    check(check_result['ok'] is False, 'one over-capacity server fails the plan')
    row = check_result['guilds'][bad]
    check(row['ok'] is False and row['remaining'] == -1, 'overflow row exact (-1)')
    check(row['overflow'] == plan[bad][7:], 'exact overflow files reported')
    check(all(v['ok'] for k, v in check_result['guilds'].items() if k != bad),
          'other seven servers still ok')


def test_preflight_exceeds_several() -> None:
    u = upre()
    snaps, plan, aplan = _guilds()
    bad_keys = [str(u.EMOJI_GUILDS[1]), str(u.EMOJI_GUILDS[6])]
    for k in bad_keys:
        snaps[k] = {'name': 'Tiny', 'limit': 5, 'static': [], 'animated': 0, 'animated_names': []}
    check_result = u._check_capacity(plan, aplan, snaps)
    bad_rows = [k for k, v in check_result['guilds'].items() if not v['ok']]
    check(check_result['ok'] is False and sorted(bad_rows) == sorted(bad_keys),
          'several over-capacity servers all reported')


def test_preflight_animated_pools() -> None:
    u = upre()
    snaps, plan, aplan = _guilds()
    g0 = str(u.EMOJI_GUILDS[0])
    # 42 animated residents are wiped as extras: only wanted counts
    snaps[g0] = {'name': 'G0', 'limit': 8, 'static': plan[g0][:3],
                 'animated': 42, 'animated_names': [f'res{j}' for j in range(42)]}
    check_result = u._check_capacity(plan, aplan, snaps)
    row = check_result['guilds'][g0]
    check(check_result['ok'] is True and row['remaining'] == 0 and row['aremaining'] == 6,
          'residents never consume capacity in either pool')
    # animated wanted over limit while static fits on another guild
    g1 = str(u.EMOJI_GUILDS[1])
    snaps[g1] = {'name': 'G1', 'limit': 50, 'static': [], 'animated': 0,
                 'animated_names': []}
    aplan[g1] = [f'big{j}' for j in range(51)]
    check_result = u._check_capacity(plan, aplan, snaps)
    r1 = check_result['guilds'][g1]
    check(check_result['ok'] is False and r1['ok'] is False
          and len(r1['aoverflow']) == 1 and r1['remaining'] >= 0,
          'animated-only overflow fails just that pool')


def test_preflight_ignored_and_kinds() -> None:
    u = upre()
    tmp = tempfile.TemporaryDirectory()
    try:
        d = Path(tmp.name) / 'assets' / 'emojis'
        d.mkdir(parents=True)
        (d / 'ok.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 100)
        (d / 'movie.gif').write_bytes(b'GIF89a' + b'0' * 100)
        (d / 'notes.txt').write_bytes(b'hi')
        import os
        prev = os.getcwd()
        os.chdir(tmp.name)
        try:
            check(u._ignored_files() == ['notes.txt'], 'only non-deployables ignored')
            valid, invalid = u._validate_assets([d / 'movie.gif'], kind='gif')
            check([p.name for p in valid] == ['movie.gif'], 'gif validates as animated')
            valid, invalid = u._validate_assets([d / 'ok.png'], kind='gif')
            check([n for n, _ in invalid] == ['ok.png'], 'png bytes rejected as gif')
            valid, invalid = u._validate_assets([d / 'movie.gif'], kind='png')
            check([n for n, _ in invalid] == ['movie.gif'], 'gif bytes rejected as png')
        finally:
            os.chdir(prev)
    finally:
        tmp.cleanup()


def test_preflight_empty_and_invalid() -> None:
    u = upre()
    snaps, _, _ = _guilds()
    check_result = u._check_capacity({}, {}, snaps)
    check(check_result['ok'] is True
          and all(len(v['new']) == 0 and len(v['anew']) == 0
                  for v in check_result['guilds'].values()),
          'empty pack passes with zero uploads')
    check(u._plan([]) == {str(g): [] for g in u.EMOJI_GUILDS}, 'empty file list plans nothing')
    tmp = tempfile.TemporaryDirectory()
    try:
        d = Path(tmp.name)
        (d / 'x.png').write_bytes(b'\x89PNG\r\n\x1a\nok')          # 1-char stem
        (d / 'has space.png').write_bytes(b'\x89PNG\r\n\x1a\nok')  # illegal char
        (d / 'big.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * (257 * 1024))
        (d / 'fake.png').write_bytes(b'not a png at all')
        (d / 'gone.png').write_text('', encoding='utf-8')
        (d / 'gone.png').unlink()                                  # missing file
        (d / 'good.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        valid, invalid = u._validate_assets([d / f for f in (
            'x.png', 'has space.png', 'big.png', 'fake.png', 'gone.png', 'good.png')])
        bad = dict(invalid)
        check([p.name for p in valid] == ['good.png'], 'only the valid file is planned')
        check(set(bad) == {'x.png', 'has space.png', 'big.png', 'fake.png', 'gone.png'},
              'every invalid file reported with a reason')
    finally:
        tmp.cleanup()


def test_preflight_abort_safety() -> None:
    import copy
    u = upre()
    snaps, plan, aplan = _guilds()
    snaps[str(u.EMOJI_GUILDS[0])] = {'name': 'Tiny', 'limit': 2, 'static': [],
                                    'animated': 0, 'animated_names': []}
    frozen = (copy.deepcopy(plan), copy.deepcopy(aplan), copy.deepcopy(snaps))
    check_result = u._check_capacity(plan, aplan, snaps)
    check(check_result['ok'] is False, 'failing plan detected')
    check((plan, aplan, snaps) == frozen,
          'checker never mutates plans or snapshots (resume state intact)')
    src = (ROOT / 'cogs' / 'emojis.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    seg = ''
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'emojisetup':
            seg = ast.get_source_segment(src, node) or ''
    guard = seg.find('_check_capacity(')
    check(guard > 0
          and guard < seg.find('_safe_delete(')
          and guard < seg.find('IDS_FILE.write_text'),
          'preflight gate precedes every delete and every state write')
    check("if not check['ok']:" in seg and seg.find('ABORTED') < seg.find("t(gid, 'eco.emoji_go')"),
          'capacity failure returns before confirmation/progress')


def test_no_content_with_layout() -> None:
    """Components V2 rejects content+layout (HTTP 400/50035). No message
    send may pass a string positional together with a live view kwarg."""
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, 'attr', '') or getattr(node.func, 'id', '')
        if name not in ('reply', 'send', 'send_message', 'edit_message'):
            continue
        view_live = any(kw.arg == 'view' and not (
            isinstance(kw.value, ast.Constant) and kw.value.value is None)
            for kw in node.keywords)
        str_positional = (bool(node.args) and isinstance(node.args[0], ast.Constant)
                          and isinstance(node.args[0].value, str))
        if view_live and str_positional:
            bad.append(getattr(node, 'lineno', '?'))
    check(not bad, f'no content+layout sends (lines {bad or "none"})')


def test_hunt_parity_helpers() -> None:
    gid = '111111111111111111'
    tmp = use_fixture({'guilds': {gid: {'rarity_shiny': 444444444444444444,
                                        'pokeball': 555555555555555551}}})
    try:
        p = pure()
        check(p.showdown_gif('Mr. Mime', False)
              == 'https://play.pokemonshowdown.com/sprites/ani/mrmime.gif',
              'showdown slug: Mr. Mime')
        check(p.showdown_gif('Ho-Oh', True)
              == 'https://play.pokemonshowdown.com/sprites/ani-shiny/hooh.gif',
              'showdown slug: shiny Ho-Oh')
        check(p.showdown_gif('', False) == '', 'empty name maps to no art')
        check(set(p.RARITY_RATE) == {'common', 'uncommon', 'rare', 'legendary'}
              and sum(p.RARITY_RATE.values()) == 100,
              'rarity shares cover all tiers and sum to 100')
        fresh = '999999999999999999'
        check(p.streak_get(fresh, fresh) == {'catch_streak': 0, 'best_streak': 0},
              'fresh trainer streaks are zero')
        line = p._rarity_line(gid, {'legendary': 0, 'rate': 190}, False)
        check('30%' in line and 'Common' in line, 'rarity line shows tier + share')
        balls = p._balls_left_line(fresh, fresh)
        check(': 0' in balls and 'Balls left' in balls, 'balls-left block lists stock')
        src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
        for fn in ('def showdown_gif', 'def streak_get', 'def _balls_left_line',
                   'def _rarity_line'):
            seg = src[src.find(fn):src.find(fn) + 1200]
            check(all(c not in BANNED for c in seg), f'no banned glyphs near {fn}')
    finally:
        tmp.cleanup()


def _fx_mon(mid, dex, level, shiny=0, fav=0, active=0, nick=''):
    return {'id': mid, 'dex': dex, 'level': level, 'shiny': shiny,
            'fav': fav, 'active': active, 'nick': nick}


def _fx_row(rate=190, legendary=0, types=None):
    return {'rate': rate, 'legendary': legendary, 'types': types or ['normal']}


def test_box_filter_sort() -> None:
    p = pure()
    m1 = _fx_mon(11, 282, 25)                    # gardevoir-ish psychic rare
    m2 = _fx_mon(7, 133, 5, shiny=1)             # shiny eevee
    m3 = _fx_mon(3, 60, 18, fav=1)               # fav poliwag water common
    r1, r2, r3 = _fx_row(45), _fx_row(45), _fx_row(190, 0, ['water'])
    check(p._box_match(m1, r1, '') and p._box_match(m1, r1, 'all')
          and p._box_match(m1, r1, 'nope'), 'empty/all/unknown filters pass')
    check(p._box_match(m3, r3, 'fav') and not p._box_match(m1, r1, 'fav'), 'fav filter')
    check(p._box_match(m2, r2, 'shiny') and not p._box_match(m1, r1, 'shiny'), 'shiny filter')
    check(p._box_match(m1, r1, 'hoenn') and p._box_match(m3, r3, 'kanto'),
          'region filter accepts home region')
    check(not p._box_match(m1, r1, 'kanto') and not p._box_match(m3, r3, 'hoenn'),
          'region filter rejects away region')
    check(p._box_match(m1, r1, 'rare') and not p._box_match(m3, r3, 'rare'), 'tier filter')
    check(p._box_match(m3, r3, 'water') and not p._box_match(m1, r1, 'water'), 'type filter')
    check(p._box_slots([m1, m2, m3]) == {11: 1, 7: 2, 3: 3}, 'stable true slots')
    entries = [(m1, r1), (m2, r2), (m3, r3)]
    check([e[0]['id'] for e in p._box_sort_entries(entries, 'rarity')] == [7, 11, 3],
          'rarity sort: shiny, rare, common')
    check([e[0]['id'] for e in p._box_sort_entries(entries, 'level')] == [11, 3, 7],
          'level sort descends')
    check([e[0]['id'] for e in p._box_sort_entries(entries, 'dex')] == [3, 7, 11],
          'dex sort ascends')
    check([e[0]['id'] for e in p._box_sort_entries(entries, 'new')] == [11, 7, 3],
          'new sort uses insertion order')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    seg = src[src.find('def _box_view'):src.find('def _mk_box_btn') + 2000]
    check(all(c not in BANNED for c in seg), 'no banned glyphs in box view code')
    for btn in ("'<<'", "'BACK'", "'NEXT'", "'>>'", "'SORT:"):
        check(btn in seg, f'ascii nav button present: {btn}')
    check('pk_mons ADD COLUMN fav' in
          (ROOT / 'database.py').read_text(encoding='utf-8'),
          'fav migration present')


def test_badge_icon_everywhere() -> None:
    """No raw BADGES[][0] reads outside badge_icon itself: converted keys
    hold fleet names, so direct reads would print literal text."""
    import re as _re
    ach = (ROOT / 'cogs' / 'achievements.py').read_text(encoding='utf-8')
    jobs = (ROOT / 'cogs' / 'jobs.py').read_text(encoding='utf-8')
    check('badge_icon(gid, _ak)' in jobs, 'jobs unlock line resolves via badge_icon')
    check(not _re.search(r'\[BADGES\[|_B\[_ak\]\[0\]', jobs), 'no raw badge-icon reads in jobs')
    check('def badge_icon' in ach, 'badge_icon defined once in achievements')


def test_preview_fits_discord() -> None:
    """Worst-case 12-guild preview must fit one 2000-char message."""
    u = upre()
    snaps, plan, aplan = {}, {}, {}
    for i, g in enumerate(u.EMOJI_GUILDS):
        names = [f'very_long_emoji_name_{i:02d}_{j}' for j in range(46)]
        anames = [f'anim_{i:02d}_{j}' for j in range(5)]
        snaps[str(g)] = {'name': f'A Very Long Guild Name Number {i}',
                         'limit': 50, 'static': names[:40], 'animated': 1,
                         'animated_names': anames[:1]}
        plan[str(g)] = names
        aplan[str(g)] = anames
    snaps[str(u.EMOJI_GUILDS[0])] = {'name': 'Tiny', 'limit': 40, 'static': [],
                                    'animated': 0, 'animated_names': []}
    check_result = u._check_capacity(plan, aplan, snaps)
    text = ('Fleet setup SHARDED: **552** static + **5** animated emojis '
            'across **12** servers. `em()` resolves cross-server.\n'
            + '\n'.join(u._format_preflight(
                check_result, [('bad name.png', 'bad-name'), ('big.png', 'over 256KB')],
                ['notes.txt'], 557))
            + '\nType `.emojisetup confirm` to execute. This wipes non-fleet emojis!')
    check(len(text) <= 1900, f'preview fits one message ({len(text)} chars)')
    check(check_result['ok'] is False, 'over-capacity still detected in compact rows')


def test_upsert_no_race() -> None:
    """Guild-row ensure must be a single atomic upsert (concurrent tasks
    on new guilds raced check-then-insert into UNIQUE violations)."""
    src = (ROOT / 'database.py').read_text(encoding='utf-8')
    check('INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)' in src,
          'get_settings uses atomic upsert')
    check('INSERT OR IGNORE INTO antiraid (guild_id) VALUES (?)' in src,
          'get_antiraid uses atomic upsert')
    import re as _re
    bare = [ln.strip() for ln in src.splitlines()
            if _re.search(r'INSERT INTO (guild_settings|antiraid) \(', ln)]
    check(not bare, 'no bare check-then-insert on guild rows')


def test_dead_stream_matcher() -> None:
    import database as DB
    check(DB._is_dead_stream(RuntimeError(
        'Hrana: api error: status=404 Not Found, body={"error":"stream not found: e8f7:fb97"}')),
        'matches stream-not-found')
    check(DB._is_dead_stream(RuntimeError('sync error: invalid local state')),
          'matches sync errors')
    check(not DB._is_dead_stream(RuntimeError('UNIQUE constraint failed: guild_settings.guild_id')),
          'constraint errors are not stream deaths')
    check(not DB._is_dead_stream(RuntimeError('')), 'empty error is not a stream death')


def test_conn_ctx_drops_dead_pool() -> None:
    import database as DB

    class Stub:
        def __init__(self):
            self.closed = 0

        def commit(self):
            raise RuntimeError('Hrana: api error: stream not found: abc')

        def close(self):
            self.closed += 1

    stub, orig_get, orig_conn = Stub(), DB.get_conn, DB._TURSO_CONN
    DB._TURSO_CONN = stub
    DB.get_conn = lambda: DB._TURSO_CONN
    try:
        try:
            with DB.conn_ctx() as c:
                c.commit()
            raised = False
        except RuntimeError:
            raised = True
        check(raised, 'dead-stream error still surfaces to caller')
        check(DB._TURSO_CONN is None and stub.closed >= 1, 'pool dropped after dead stream')
    finally:
        DB.get_conn = orig_get
        DB._TURSO_CONN = orig_conn

    class Fine:
        committed = False

        def commit(self):
            Fine.committed = True

        def close(self):
            pass

    fine = Fine()
    DB._TURSO_CONN = fine
    DB.get_conn = lambda: DB._TURSO_CONN
    try:
        try:
            with DB.conn_ctx() as c:
                raise ValueError('UNIQUE constraint failed: guild_settings.guild_id')
            raised = False
        except ValueError:
            raised = True
        check(raised, 'ordinary errors still surface')
        check(DB._TURSO_CONN is fine, 'healthy pool is never dropped')
    finally:
        DB.get_conn = orig_get
        DB._TURSO_CONN = orig_conn


def test_box_ids_unique() -> None:
    """Box button ids are unique per render: action is part of the id, so
    FIRST/LAST can never collide with BACK/NEXT on edge pages (HTTP 400)."""
    p = pure()
    for total in (1, 2, 3):
        for page in range(1, total + 1):
            ids = [p._box_cid(1, 2, a, pg, 'rarity', '')
                   for a, pg in (('first', 1), ('back', page - 1),
                                 ('next', page + 1), ('last', total),
                                 ('sort', page))]
            check(len(set(ids)) == 5, f'box ids unique (page {page}/{total})')
    parsed = p._box_parse(p._box_cid(111, 222, 'next', 3, 'level', 'hoenn'))
    check(parsed == (111, 222, 'next', 3, 'level', 'hoenn'), 'box id round-trips')
    check(p._box_parse('garbage') is None and p._box_parse('pkbox:1:2') is None,
          'malformed box ids rejected')


def test_species_buttons() -> None:
    p = pure()
    check(p._species_emoji_name(1) == 'p001', 'dex 1 maps p001')
    check(p._species_emoji_name(25) == 'p025', 'dex 25 maps p025')
    check(p._species_emoji_name(493) == 'p493', 'dex 493 maps p493')
    check(all(p._species_emoji_name(x) == '' for x in (0, 494, -1, 'x', None, '')),
          'out-of-range dex maps to no icon')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check(src.count('_species_btn_emoji(') >= 4, 'species faces wired (picker + 2 switch rows)')


def test_main_guild_gate() -> None:
    import asyncio as _aio
    import database as DB
    check(DB.MAIN_GUILD_ID == 1530916477941714974, 'main guild id pinned')
    check(DB.is_main_guild(1530916477941714974)
          and DB.is_main_guild('1530916477941714974')
          and not DB.is_main_guild(1549264681867284581)
          and not DB.is_main_guild(0) and not DB.is_main_guild(None)
          and not DB.is_main_guild('nope'), 'main-guild matching exact')

    class Msg:
        def __init__(self, gid):
            self.guild = type('G', (), {'id': gid})() if gid else None

    ran = []

    class Cog:
        @DB.main_guild_only
        async def on_message(self, message):
            ran.append(message)

    cog = Cog()
    _aio.run(cog.on_message(Msg(1530916477941714974)))
    _aio.run(cog.on_message(Msg(1549264681867284581)))
    _aio.run(cog.on_message(Msg(None)))
    check(len(ran) == 1, 'decorator passes main guild, blocks holder + DM')

    import re as _re
    for f in ('levels', 'automod', 'antiraid', 'counting', 'afk', 'tickets',
              'wordle', 'fitcheck', 'activity', 'babka', 'clown', 'pokemon'):
        src = (ROOT / 'cogs' / f'{f}.py').read_text(encoding='utf-8-sig')
        check(bool(_re.search(r'@commands\.Cog\.listener\(\)\n    @db\.main_guild_only\n'
                              r'    async def on_message', src)),
              f'{f} listener gated with correct order')
    main = (ROOT / 'main.py').read_text(encoding='utf-8')
    check('is_main_guild' in main and 'is_house' in main and 'CheckFailure' in main,
          'global command gate in before_invoke (silent CheckFailure)')
    levels = (ROOT / 'cogs' / 'levels.py').read_text(encoding='utf-8-sig')
    check('is_main_guild' in levels.split('async def voice_tick')[1].split('async def ')[0],
          'voice sweep skips holder servers')


def test_buddy_match_evo() -> None:
    p = pure()
    spec = lambda d: {25: 'pikachu', 133: 'eevee', 4: 'charmander'}.get(d, '')  # noqa: E731
    mons = [
        {'id': 1, 'dex': 25, 'nick': 'Sparky'},
        {'id': 2, 'dex': 133, 'nick': ''},
        {'id': 3, 'dex': 4, 'nick': 'Spark'},
    ]
    m, n = p._match_mon(mons, spec, 'sparky')
    check(m['id'] == 1 and n == 1, 'nick exact wins first tier')
    m, n = p._match_mon(mons, spec, 'eevee')
    check(m['id'] == 2 and n == 1, 'species exact matches')
    m, n = p._match_mon(mons, spec, 'spar')
    check(m['id'] == 1 and n == 2, 'prefix tier with count')
    check(p._match_mon(mons, spec, 'zzz') == (None, 0), 'no match')
    check(p._match_mon(mons, spec, '') == (None, 0), 'empty query')
    evo = p._evo_text(0, {'evo_to': 134, 'evo_level': 25})
    check(('Vaporeon' in evo or '#134' in evo) and '25' in evo,
          'evo line names target (or dex fallback) + level')
    check(p._evo_text(0, {'evo_to': 0, 'evo_level': 0}) != '', 'max evo renders')
    check(p._evo_text(0, {'evo_to': 134, 'evo_level': 0}) != '', 'stone evo renders')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    seg = src[src.find('async def buddy'):src.find('async def buddy') + 9000]
    check(all(c not in BANNED for c in seg), 'no banned glyphs in buddy command')


def test_catchmeta_rarity() -> None:
    """Regression: _catch_meta passed a dex int into row-based rarity_of
    (AttributeError after every catch/guess)."""
    import re as _re
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check(len(_re.findall(r'^def rarity_of', src, flags=_re.M)) == 1,
          'single rarity_of definition (dead shadow removed)')
    seg = src[src.find('def _catch_meta'):src.find('def _catch_meta') + 800]
    check('_rarity_line(' in seg and 'rarity_of(dex)' not in seg,
          '_catch_meta renders through _rarity_line')


def test_anime_silhouette() -> None:
    """Mystery sprites render anime-yellow on show blue, never near-black."""
    import io as _io
    from PIL import Image as _Img
    import types
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    mod = types.ModuleType('pksil')
    mod.__dict__['ARENAS'] = ('meadow', 'forest', 'cave')
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                'wild_image', 'silhouette_image', '_sky_grass', '_platform',
                '_arena_forest', '_arena_cave'):
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<pksil>', 'exec'),
                 mod.__dict__)
    sp = _Img.new('RGBA', (96, 96), (200, 30, 30, 255))
    buf = _io.BytesIO()
    sp.save(buf, 'PNG')
    raw = buf.getvalue()
    meadow = _Img.open(_io.BytesIO(mod.wild_image(raw, mystery=True))).convert('RGB')
    r, g, b = meadow.getpixel((450, 140))
    check(r > 200 and g > 150 and b < 120, f'meadow silhouette is yellow (got {r},{g},{b})')
    card = _Img.open(_io.BytesIO(mod.silhouette_image(raw))).convert('RGB')
    r, g, b = card.getpixel((10, 10))
    check(r < 120 and 60 < g < 160 and b > 150, f'silhouette card bg is show blue (got {r},{g},{b})')
    r, g, b = card.getpixel((210, 180))
    check(r > 200 and g > 150 and b < 120, f'card silhouette is yellow (got {r},{g},{b})')


def test_forms() -> None:
    gid = '111111111111111111'
    tmp = use_fixture({'guilds': {gid: {'form_mega': 999999999999999991,
                                        'rarity_shiny': 444444444444444444}}})
    try:
        p = pure()
        check(set(p.FORMS) == {'mega_rayquaza', 'primal_groudon', 'zacian_crowned'},
              'form registry has the three grants')
        for key, f in p.FORMS.items():
            check(set(f['stats']) == {'hp', 'atk', 'dfn', 'spa', 'spd', 'spe'},
                  f'{key} stats plug into calc_stats')
            check(isinstance(f['types'], list) and f['types'] and f['name']
                  and f['sprite'].startswith('https://') and f['emo'], f'{key} complete')
        check(p.form_of({'form': 'MEGA_RAYQUAZA'})['name'] == 'Mega Rayquaza',
              'form lookup case-insensitive')
        check(p.form_of({'form': 'nope'}) is None and p.form_of({}) is None
              and p.form_of(None) is None, 'unknown forms are None')
        check(p.form_emo(gid, {'form': 'mega_rayquaza'}) == '<:form_mega:999999999999999991>',
              'form marker resolves')
        check(p.form_emo(gid, {'form': 'primal_groudon'}) == '', 'missing form emoji is empty')
        check(p.mon_name({'dex': 384, 'nick': '', 'shiny': 0, 'form': 'mega_rayquaza'}, gid)
              == '<:form_mega:999999999999999991> Mega Rayquaza',
              'formed name shows marker + form name')
        check(p.mon_name({'dex': 384, 'nick': 'Ray', 'shiny': 0, 'form': 'mega_rayquaza'}, gid)
              == '<:form_mega:999999999999999991> Ray',
              'nick still wins over form name')
        check(p.showdown_gif('Rayquaza-Mega', False).endswith('/ani/rayquaza-mega.gif'),
              'forme slugs keep hyphens')
        check(p.showdown_gif('Mr. Mime', False).endswith('/ani/mrmime.gif'),
              'base species still strip punctuation')
        check(p.form_sprite({'form': 'zacian_crowned'}, False).endswith('/10188.png'),
              'form art override resolves')
        check(p.form_sprite({'dex': 25}, False) is None, 'unformed mons use pixel art')
        check("ADD COLUMN form" in (ROOT / 'database.py').read_text(encoding='utf-8'),
              'form migration present')
    finally:
        tmp.cleanup()


def test_master_never_fails() -> None:
    """Regression: the 0.98 cap applied to master balls too (1-in-50
    breakouts). Master must bypass pity math with p == 1.0."""
    import types
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    mod = types.ModuleType('pkcatch')
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'catch_chance':
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<pkcatch>', 'exec'),
                 mod.__dict__)
    check(mod.catch_chance(3, 100, 1.0, None) == 1.0, 'master mult returns 1.0')
    seg = src[src.find('mult = BALLS[ball][1]'):src.find('mult = BALLS[ball][1]') + 600]
    check('if mult is None:' in seg and 'p = 1.0' in seg,
          'master bypasses the 0.98 cap')
    check('min(0.98' in seg, 'other balls still capped')


def test_box_card() -> None:
    gid = '111111111111111111'
    ids = {f'letter_{k}': 777777777777777770 + i for i, k in enumerate('curl')}
    tmp = use_fixture({'guilds': {gid: ids}})
    try:
        p = pure()
        check(p._tier_letter(gid, 'rare') == '<:letter_r:777777777777777772>',
              'rarity letter resolves')
        check(p._tier_letter(gid, 'nope') == '[?]', 'unknown tier falls back')
    finally:
        tmp.cleanup()
    tmp = use_fixture({'guilds': {}})
    try:
        p = pure()
        check(p._tier_letter(gid, 'legendary') == '[L]', 'missing letter falls back ASCII')
    finally:
        tmp.cleanup()
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    seg = src[src.find('def _box_view'):src.find('def _mk_box_btn')]
    for needle in ('Section(', 'Thumbnail(media=', 'attachment://', 'Page ',
                   ';fav <slot>', ';box <kanto', ';box @user', 'nav_first',
                   'nav_back', 'nav_next', 'nav_last', 'nav_sort'):
        check(needle in seg, f'box card contains: {needle}')
    check(all(c not in BANNED for c in seg), 'no banned glyphs in box view')
    for f in ('letter_c', 'letter_u', 'letter_r', 'letter_l', 'nav_first',
              'nav_back', 'nav_next', 'nav_last', 'nav_sort'):
        check((ROOT / 'assets' / 'emojis' / f'{f}.png').is_file(), f'asset on disk: {f}')


def test_dextypes_list() -> None:
    """Regression: _dex_row returned types as a JSON string, so every direct
    consumer (buddy card, box filter) iterated characters."""
    import database as DB
    with DB.conn_ctx() as conn:
        dexes = [r['dex'] for r in conn.execute('SELECT dex FROM pk_dex LIMIT 5')]
    p = pure()
    if dexes:
        for dx in dexes:
            check(isinstance(p._dex_row(dx).get('types'), list), f'dex {dx} types is a list')
    else:
        print('SKIP dex rows (empty local cache)', flush=True)
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check("row['types'] = _j.loads(row.get('types') or '[]')" not in src,
          'no unguarded types parse remains')
    seg = src[src.find('async def buddy'):src.find('async def buddy') + 12000]
    check('form_sprite(m,' in seg, 'buddy shows form art')


def test_plain_arena() -> None:
    from PIL import Image as _Img
    from PIL import ImageDraw as _Dr
    p = pure()
    check(p._plain_name('<:rarity_shiny:123> Arceus') == 'Arceus', 'tokens stripped for PIL')
    check(p._plain_name('Pikachu') == 'Pikachu', 'plain names untouched')
    check(set(p.ARENAS) == {'meadow', 'forest', 'cave'}, 'three arenas')
    import random as _r
    for fn in ('forest', 'cave'):
        im = _Img.new('RGB', (900, 420), (0, 0, 0))
        getattr(p, f'_arena_{fn}')(_Dr.Draw(im), _r.Random(7), 900, 420)
        r, g, b = im.getpixel((450, 30))
        check(r + g + b < 300, f'{fn} canopy reads dark')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check(src.count("'arena': random.choice(ARENAS)") == 3, 'arena stored in all battle states')
    check('st.get(\'arena\', \'meadow\')' in src, 'image calls carry the arena')


def test_turn_armor() -> None:
    import asyncio as _aio
    p = pure()

    class IX:
        def __init__(self):
            self.sent = []
            _self = self

            class Fol:
                async def send(inner, *a, **k):
                    _self.sent.append((a, k))
            self.followup = Fol()
            self.guild = type('G', (), {'id': 0})()

    @p._turn_safe
    async def boom(self, ix):
        raise ValueError('mid-turn kaboom')

    @p._turn_safe
    async def fine(self, ix):
        return 'turn-ok'

    ix = IX()
    check(_aio.run(boom(None, ix)) is None and len(ix.sent) == 1, 'failed turn tells the player')
    check(_aio.run(fine(None, ix)) == 'turn-ok', 'good turns pass through')
    check(p._safe_moves({})[0]['name'] == 'tackle', 'empty moves fall back to tackle')
    check(p._safe_moves({'moves': [{'name': 'x'}]}) == [{'name': 'x'}], 'real moves untouched')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check('@_turn_safe\n    async def _battle_turn' in src
          and '@_turn_safe\n    async def _duel_turn' in src,
          'armor decorates both turn handlers')
    check('random.choice(me[' not in src and 'random.choice(a[' not in src
          and 'random.choice(fo_now[' not in src and 'random.choice(wild[' not in src,
          'no unguarded move picks remain')


def test_movesets_ivs() -> None:
    import inspect as _insp
    import json as _j
    p = pure()
    check('level' in str(_insp.signature(p.moveset_for)), 'moveset takes level')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check('level-up' in src and 'level_learned_at' in src and 'moves = list(learned[:4])' in src,
          'learnsets prefer level-up moves')
    ivs = _j.loads(p._roll_ivs())
    check(set(ivs) == {'hp', 'atk', 'dfn', 'spa', 'spd', 'spe'}
          and all(0 <= v <= 31 for v in ivs.values()), 'rolled IVs valid spread')
    check(p._ivs_of({}) is None and p._ivs_of({'ivs': 'junk'}) is None
          and p._iv_pct({}) == 100, 'legacy mons stay perfect')
    lo = p.calc_stats({'hp': 50, 'atk': 50, 'dfn': 50, 'spa': 50, 'spd': 50, 'spe': 50},
                      20, {k: 0 for k in ('hp', 'atk', 'dfn', 'spa', 'spd', 'spe')})
    hi = p.calc_stats({'hp': 50, 'atk': 50, 'dfn': 50, 'spa': 50, 'spd': 50, 'spe': 50}, 20)
    check(lo['maxhp'] < hi['maxhp'] and lo['atk'] < hi['atk'], 'IVs scale stats')
    check("ADD COLUMN ivs" in (ROOT / 'database.py').read_text(encoding='utf-8'),
          'ivs migrations present')
    check(src.count('nick, active, ivs)') >= 7, 'all mon inserts carry ivs')


def test_hunt_modes() -> None:
    import inspect as _insp
    p = pure()
    sig = str(_insp.signature(p.wild_image))
    check('arena' in sig and 'bare' in sig, 'wild_image paints any arena')
    from PIL import Image as _Img
    import io as _io
    import random as _r
    for arena, dark in (('forest', True), ('cave', True), ('meadow', False)):
        png = p.wild_image(None, arena=arena, bare=True)
        im = _Img.open(_io.BytesIO(png)).convert('RGB')
        r, g, b = im.getpixel((450, 40))
        check((r + g + b < 350) == dark, f'{arena} sky reads right')
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    check("'mode': mode, 'arena': arena" in src, 'encounters store mode + arena')
    check('def _attach_enc_buttons(self, view, gid, uid, mode' in src,
          'encounter buttons take the mode')
    check("if mode == 'fight':" in src, 'fight row only in fight mode')
    check("@commands.command(name='p'" in src, 'catch-only ;p command exists')


def test_phelp_hub() -> None:
    import re as _re
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    usage = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and getattr(sub.targets[0], 'id', '') == 'PK_USAGE':
                    usage = ast.literal_eval(sub.value)
    cmds = set(_re.findall(r"@commands\.(?:command|group)\(name='([^']+)'", src))
    cmds |= {'p'}
    missing = sorted(c for c in cmds if c not in usage)
    check(not missing, f'every command has usage ({len(usage)} covered)')
    check(all(isinstance(v, tuple) and len(v) == 2 and all(v) for v in usage.values()),
          'usage entries are EN/PL pairs')
    check("'eco.pk_h_unknown'" in src, 'unknown-command path exists')
    for f in ('box_box', 'dex_book', 'trade_swap', 'quest_scroll', 'market_stall', 'coin'):
        check((ROOT / 'assets' / 'emojis' / f'{f}.png').is_file(), f'hub icon deployable: {f}')


def test_catch_mode_text() -> None:
    """;p cards must never mention FIGHT or weakening (no FIGHT row)."""
    import json as _j
    src = (ROOT / 'cogs' / 'pokemon.py').read_text(encoding='utf-8')
    start = src.find("if mode == 'catch':")
    branch = src[start:src.find('\n            else:', start)]
    check('pk_wild_catch' in branch and 'pk_odds' not in branch,
          'catch mode uses its own line, no weaken odds')
    lang = _j.loads((ROOT / 'lang.json').read_text(encoding='utf-8'))
    for section in ('en', 'pl'):
        line = lang[section].get('eco.pk_wild_catch', '')
        check(bool(line) and 'FIGHT' not in line and 'WALCZ' not in line,
              f'catch line fight-free ({section})')


TESTS = (test_rock_mapped, test_missing_file_safe, test_malformed_safe,
         test_per_guild_lookup, test_global_fallback, test_missing_emoji_fallback,
         test_id_format, test_patch2_names_and_markers, test_patch2_ascii_fallbacks,
         test_button_decisions, test_button_source_hygiene, test_partialemoji_shape,
         test_weather_emojis, test_weather_plain_fallback, test_quest_bars_and_economy,
         test_achievement_icons, test_achievement_fallbacks_and_data,
         test_preflight_fits_all_eight, test_preflight_exceeds_one_server,
         test_preflight_exceeds_several, test_preflight_animated_pools,
         test_preflight_ignored_and_kinds,
         test_preflight_empty_and_invalid, test_preflight_abort_safety,
         test_no_content_with_layout, test_hunt_parity_helpers,
         test_box_filter_sort, test_badge_icon_everywhere,
         test_preview_fits_discord, test_upsert_no_race,
         test_dead_stream_matcher, test_conn_ctx_drops_dead_pool,
         test_box_ids_unique, test_species_buttons,
         test_main_guild_gate, test_buddy_match_evo,
         test_catchmeta_rarity, test_anime_silhouette, test_forms,
         test_master_never_fails, test_box_card, test_dextypes_list,
         test_plain_arena, test_turn_armor, test_movesets_ivs,
         test_hunt_modes, test_phelp_hub, test_catch_mode_text)

if __name__ == '__main__':
    for t in TESTS:
        try:
            t()
        except Exception as e:  # never let one test mask the rest
            check(False, f'{t.__name__} raised {type(e).__name__}: {e}')
    print(f'{len(TESTS) - len(FAILS)}/{len(TESTS)} passed', flush=True)
    sys.exit(1 if FAILS else 0)

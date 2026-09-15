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
              '_weather_line'}
    import lang as LANG
    mod = types.ModuleType('pkpure')
    mod.__dict__['em'] = E.em
    mod.__dict__['db'] = DB
    mod.__dict__['t'] = LANG.t
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<pkpure>', 'exec'),
                 mod.__dict__)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], 'id', '') in ('TYPE_EMOJI', 'WEATHER_LINE'):
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
    check(all(v.strip().startswith('_btn_emoji(') for v in emoji_kwargs),
          'every button emoji kwarg is a resolver call (never a raw token)')
    check('<:' not in src, 'no raw <:name:id> tokens anywhere in pokemon.py')
    label_lines = [ln for ln in src.splitlines() if 'label=' in ln]
    check(all(c not in BANNED for ln in label_lines for c in ln),
          'no banned Unicode in button label lines')
    for cid in ('pkfight:', 'pkball:', 'pklead:', 'pkmv:', "'pk_potion'",
                "'pk_ball'", "'pk_run'", 'pksw:', 'pkd:', 'pkdsw:'):
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


TESTS = (test_rock_mapped, test_missing_file_safe, test_malformed_safe,
         test_per_guild_lookup, test_global_fallback, test_missing_emoji_fallback,
         test_id_format, test_patch2_names_and_markers, test_patch2_ascii_fallbacks,
         test_button_decisions, test_button_source_hygiene, test_partialemoji_shape,
         test_weather_emojis, test_weather_plain_fallback, test_quest_bars_and_economy,
         test_achievement_icons, test_achievement_fallbacks_and_data)

if __name__ == '__main__':
    for t in TESTS:
        try:
            t()
        except Exception as e:  # never let one test mask the rest
            check(False, f'{t.__name__} raised {type(e).__name__}: {e}')
    print(f'{len(TESTS) - len(FAILS)}/{len(TESTS)} passed', flush=True)
    sys.exit(1 if FAILS else 0)

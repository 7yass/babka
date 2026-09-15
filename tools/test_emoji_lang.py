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
    print(('PASS ' if cond else 'FAIL ') + msg, flush=True)
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


TESTS = (test_rock_mapped, test_missing_file_safe, test_malformed_safe,
         test_per_guild_lookup, test_global_fallback, test_missing_emoji_fallback,
         test_id_format)

if __name__ == '__main__':
    for t in TESTS:
        try:
            t()
        except Exception as e:  # never let one test mask the rest
            check(False, f'{t.__name__} raised {type(e).__name__}: {e}')
    print(f'{len(TESTS) - len(FAILS)}/{len(TESTS)} passed', flush=True)
    sys.exit(1 if FAILS else 0)

"""Build the anime-cards manifest for the 8 R2 series from local filenames.

Filename shape: {Series...}_{Character...}_#{N}_{RARITY}_{ID}.png
Output: data/anime_cards_r2.json — [{id,set_id,code,rarity,print_total,
image_url,is_animated,name}] ready for import_lumina.py / `.cards sync`.

  py tools/build_cards_manifest.py <public-base-url>
e.g. py tools/build_cards_manifest.py https://pub-xxx.r2.dev

Keys keep literal '#' (valid in R2); URLs percent-encode it.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

SRC = r'F:\Files\Documents\!! Projects\babka cards'
SETS = [
    ('naruto', 'NARUTO', ('naruto_', 'boruto_')),
    ('rezero', 'REZERO', ('re_zero_',)),
    ('bleach', 'BLEACH', ('bleach_',)),
    ('dragonball', 'DB', ('dragon_ball_',)),
    ('onepiece', 'OP', ('one_piece_',)),
    ('demonslayer', 'DS', ('demon_slayer_',)),
    ('jojo', 'JOJO', ('jojo_',)),
    ('bluelock', 'BLK', ('blue_lock_',)),
]
PAT = re.compile(r'^(.*)_#(\d+)_(R|SR|LR|UR)_(\d+)\.png$', re.IGNORECASE)
KEY_PREFIX = 'cards/'


def main():
    base = sys.argv[1].rstrip('/')
    cards, seen = [], set()
    for set_id, tag, prefixes in SETS:
        with __import__('os').scandir(SRC) as it:
            paths = sorted(e.path for e in it
                           if e.name.lower().endswith('.png')
                           and e.name.lower().startswith(prefixes))
        for p in paths:
            name = Path(p).name
            m = PAT.match(name)
            if not m:
                print('SKIP (unparsed):', name)
                continue
            head, num, rar, cid = m.group(1), m.group(2), m.group(3).upper(), m.group(4)
            cid_full = f'{tag}-{cid}'
            if cid_full in seen:
                print('SKIP (dup id):', name)
                continue
            seen.add(cid_full)
            cards.append({
                'id': cid_full,
                'set_id': set_id,
                'code': cid_full,
                'rarity': rar,
                'print_total': 0,
                'image_url': f"{base}/{KEY_PREFIX}{quote(name)}",
                'is_animated': 0,
                'name': f"{head.replace('_', ' ')} #{num}",
            })
    out = Path(__file__).parent.parent / 'data' / 'anime_cards_r2.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding='utf-8')
    by_set = {}
    for c in cards:
        by_set.setdefault(c['set_id'], {}).setdefault(c['rarity'], 0)
        by_set[c['set_id']][c['rarity']] += 1
    print(f'wrote {len(cards)} cards -> {out}')
    for sid, r in by_set.items():
        print(f'  {sid}: {r}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: py tools/build_cards_manifest.py <public-base-url>')
    main()

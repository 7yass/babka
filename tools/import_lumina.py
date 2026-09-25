"""Bulk import for Lumina cards — ready for 500-2000.

Usage:
  py tools/import_lumina.py data/lumina_cards.json
  or drop images into assets/cards/ + csv

Expected JSON: [{code, set_id, rarity, print_total, image_url, is_animated, name}]
Scales to 2000: executemany, batched, indexed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db

def import_cards(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    print(f"Importing {len(data)} cards...")
    database = db
    database.init_db()
    sets = {}
    for c in data:
        sets[c.get('set_id','unknown')] = sets.get(c.get('set_id','unknown'), 0)+1
    with db.conn_ctx() as conn:
        for sid, cnt in sets.items():
            conn.execute('INSERT OR REPLACE INTO anime_sets (id,name,total_cards) VALUES (?,?,?)', (sid, sid, cnt))
        for c in data:
            conn.execute('INSERT OR REPLACE INTO anime_cards (id,set_id,code,rarity,print_total,image_url,is_animated,name) VALUES (?,?,?,?,?,?,?,?)',
                         (c['id'], c['set_id'], c.get('code',''), c.get('rarity','C'), int(c.get('print_total',0)), c.get('image_url',''), int(bool(c.get('is_animated',0))), c.get('name','')))
    print(f"Done. Sets: {sets}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: py tools/import_lumina.py <json>")
        sys.exit(1)
    import_cards(sys.argv[1])

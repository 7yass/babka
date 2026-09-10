"""i18n: t(guild_id, key, **vars). Per-server EN/PL, street Polish."""
import json
from pathlib import Path

import database as db

with open(Path(__file__).parent / 'lang.json', encoding='utf-8') as f:
    STR = json.load(f)


def get_lang(guild_id) -> str:
    if not guild_id:
        return 'en'
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT lang FROM lang WHERE guild_id = ?', (str(guild_id),)).fetchone()
        return 'pl' if row and row['lang'] == 'pl' else 'en'


def set_lang(guild_id, lang: str):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO lang (guild_id, lang) VALUES (?, ?)', (str(guild_id), lang))


def t(guild_id, key: str, **vars) -> str:
    lang = get_lang(guild_id)
    s = (STR.get(lang) or {}).get(key, STR['en'].get(key, key))
    for k, v in vars.items():
        s = s.replace('{' + k + '}', str(v))
    return s

"""i18n: t(guild_id, key, **vars). Per-server EN/PL, street Polish.
Per-user override: members holding the EN role get English, PL role gets
Polish, everyone else falls back to the server language. The active user
is carried in a contextvar (set by before_invoke + view callbacks)."""
import contextvars
import json
from pathlib import Path

import database as db

# role id -> language. Neither role = server default.
LANG_ROLES = {
    '1547275835453743146': 'en',
    '1547275838213333002': 'pl',
}

_ctx_member = contextvars.ContextVar('babka_user', default=None)


def set_ctx_lang(member) -> None:
    """Pin the current reply language to this member's roles (or reset)."""
    _ctx_member.set(member)


def reset_ctx_lang() -> None:
    _ctx_member.set(None)


def resolve_lang(guild, member) -> str:
    """Member roles win; otherwise the server language."""
    try:
        roles = getattr(member, 'roles', []) or []
        for r in roles:
            hit = LANG_ROLES.get(str(getattr(r, 'id', '')))
            if hit == 'en':
                return 'en'
        for r in roles:
            if LANG_ROLES.get(str(getattr(r, 'id', ''))) == 'pl':
                return 'pl'
    except Exception:
        pass
    gid = getattr(guild, 'id', guild)
    return get_lang(gid)

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
    member = _ctx_member.get()
    lang = resolve_lang(guild_id, member) if member is not None else get_lang(guild_id)
    s = (STR.get(lang) or {}).get(key, STR['en'].get(key, key))
    for k, v in vars.items():
        s = s.replace('{' + k + '}', str(v))
    return s

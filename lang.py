"""i18n: t(guild_id, key, **vars). Per-server EN/PL, street Polish.
Per-user override: members holding the EN role get English, PL role gets
Polish, everyone else falls back to the server language. The active user
is carried in a contextvar (set by before_invoke + view callbacks)."""
import contextvars
import json
import time as _time
from pathlib import Path

import database as db

# role id -> language. Neither role = server default.
LANG_ROLES = {
    '1547275835453743146': 'en',
    '1547275838213333002': 'pl',
}

# Overlay directory: lang/<domain>.json, each shaped {"en": {...}, "pl": {...}}.
# Overlays win over lang.json, so a new subsystem can ship its own strings
# without anyone hand-editing a 210KB file.
LANG_DIR = Path(__file__).parent / 'lang'
LANG_MAIN = Path(__file__).parent / 'lang.json'

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

def _load_base() -> dict:
    """lang.json plus every lang/*.json overlay."""
    with open(LANG_MAIN, encoding='utf-8') as f:
        data = json.load(f)
    if LANG_DIR.is_dir():
        for p in sorted(LANG_DIR.glob('*.json')):
            try:
                with open(p, encoding='utf-8') as f:
                    overlay = json.load(f)
            except Exception as e:
                print(f'[-] lang overlay {p.name} failed to load: {e}')
                continue
            if not isinstance(overlay, dict):
                continue
            for lang, kv in overlay.items():
                if not isinstance(kv, dict):
                    continue
                data.setdefault(lang, {}).update(kv)
    return data


def _stamp():
    """Fingerprint of every language file, for hot reload."""
    parts = []
    try:
        parts.append((LANG_MAIN.name, LANG_MAIN.stat().st_mtime_ns))
    except Exception:
        pass
    if LANG_DIR.is_dir():
        for p in sorted(LANG_DIR.glob('*.json')):
            try:
                parts.append((p.name, p.stat().st_mtime_ns))
            except Exception:
                pass
    return tuple(parts)


# STR is imported by reference (`from lang import STR`) in help.py and
# health.py, so it must stay THE SAME dict object across reloads. Mutate it
# in place instead of rebinding.
STR: dict = {}
_STR_STAMP = None
_RELOAD_AFTER = 30.0
_reload_checked_at = 0.0


def _load_into(target: dict) -> None:
    target.clear()
    target.update(_load_base())


def refresh(force: bool = False) -> bool:
    """Reload language files if any changed. Returns True if reloaded.

    Translations used to be frozen at import: editing lang.json needed a bot
    restart, and on a panel-hosted bot that means a redeploy. Now the files
    are polled at most every 30s and swapped in place.
    """
    global _STR_STAMP, _reload_checked_at
    now = _time.time()
    if not force and (now - _reload_checked_at) < _RELOAD_AFTER:
        return False
    _reload_checked_at = now
    stamp = _stamp()
    if stamp == _STR_STAMP:
        return False
    try:
        _load_into(STR)
    except Exception as e:
        print(f'[-] language reload failed, keeping old strings: {e}')
        return False
    _STR_STAMP = stamp
    return True


def missing_keys() -> list:
    """Keys present in EN but not in PL (and vice versa). Used by .health."""
    en = set((STR.get('en') or {}).keys())
    pl = set((STR.get('pl') or {}).keys())
    return sorted((en - pl, pl - en))


_load_into(STR)
_STR_STAMP = _stamp()


def get_lang(guild_id) -> str:
    if not guild_id:
        return 'en'
    hit = db._lang_cache.get(str(guild_id))
    if hit:
        import time as _t
        if _t.time() - hit[0] < db._CACHE_TTL:
            return hit[1]
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT lang FROM lang WHERE guild_id = ?', (str(guild_id),)).fetchone()
        lang = 'pl' if row and row['lang'] == 'pl' else 'en'
    import time as _t
    db._lang_cache[str(guild_id)] = (_t.time(), lang)
    return lang


def set_lang(guild_id, lang: str):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO lang (guild_id, lang) VALUES (?, ?)', (str(guild_id), lang))
    db._lang_cache.pop(str(guild_id), None)


def t(guild_id, key: str, **vars) -> str:
    refresh()
    lang = cur_lang(guild_id)
    s = (STR.get(lang) or {}).get(key, STR['en'].get(key, key))
    for k, v in vars.items():
        s = s.replace('{' + k + '}', str(v))
    return s


def has_key(key: str) -> bool:
    """True when the key exists in EN (the fallback language)."""
    refresh()
    return key in (STR.get('en') or {})


def cur_lang(guild_id) -> str:
    """Context-aware language: pinned member roles win, else server lang."""
    member = _ctx_member.get()
    if member is not None:
        return resolve_lang(guild_id, member)
    return get_lang(guild_id)

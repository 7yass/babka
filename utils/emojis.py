"""Custom emoji helper: <:name:id> per guild from the deploy map,
falls back to plain text/unicode when missing."""
import json
from pathlib import Path

IDS_FILE = Path('data/emoji_ids.json')
_MAP, _MAP_TS = {}, 0.0


def _load():
    global _MAP, _MAP_TS
    try:
        ts = IDS_FILE.stat().st_mtime
    except Exception:
        return {}
    if ts != _MAP_TS:
        try:
            _MAP = json.loads(IDS_FILE.read_text(encoding='utf-8')).get('guilds', {})
        except Exception:
            _MAP = {}
        _MAP_TS = ts
    return _MAP


def em(gid, name: str, fallback: str = '') -> str:
    """`<:name:id>` for this guild's deployed set, else fallback."""
    try:
        eid = (_load().get(str(gid)) or {}).get(name)
        if eid:
            return f'<:{name}:{eid}>'
    except Exception:
        pass
    return fallback

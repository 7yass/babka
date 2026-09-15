"""Custom emoji helper: <:name:id> from the fleet map.
SHARDED fleet: each emoji lives on exactly one of the 8 hosts, but bots
can use fleet emojis cross-server — so resolve per-guild first, then
fall back to a global scan of all hosts."""
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
    """`<:name:id>` for this guild's set, else any fleet host, else fallback."""
    if not name:
        return fallback
    try:
        m = _load()
        eid = (m.get(str(gid)) or {}).get(name)
        if eid:
            return f'<:{name}:{eid}>'
        # Cross-server fallback: bot shares all fleet guilds, so an emoji
        # hosted on guild A renders fine in guild B.
        for guild_map in m.values():
            eid = (guild_map or {}).get(name)
            if eid:
                return f'<:{name}:{eid}>'
    except Exception:
        pass
    return fallback

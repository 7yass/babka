"""Boss registry: explicit mapping, validated at import (fail fast).
Unknown keys return None -> service answers UNKNOWN_BOSS, never a
silent Dreadmaw fallback. Disabled bosses resolve but cannot start."""
from game.bosses.dreadmaw import DREADMAW
from game.bosses.models import validate_definition
from game.bosses.tidecaller import TIDECALLER

BOSSES = {
    DREADMAW.key: DREADMAW,
    TIDECALLER.key: TIDECALLER,
}

for _defn in BOSSES.values():
    validate_definition(_defn)
del _defn


class BossRegistry:
    def get(self, boss_key: str):
        """Definition or None (unknown/disabled-safe: caller decides)."""
        return BOSSES.get((boss_key or '').lower())

    def exists(self, boss_key: str) -> bool:
        return (boss_key or '').lower() in BOSSES

    def list_available(self):
        """Enabled definitions only, registry order."""
        return tuple(d for d in BOSSES.values() if d.enabled)


registry = BossRegistry()

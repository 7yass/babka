"""Boss configuration models: immutable definitions, validation,
config snapshots for active events.

Runtime state (hp, revision, participants) lives in the service's
tables; this module only describes WHAT a boss is. Snapshotting means
a balance edit mid-raid affects future events, never the live one.
"""
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class LootEntry:
    item_id: str
    quantity_min: int
    quantity_max: int
    weight: int
    guaranteed: bool = False
    min_damage: int = 0


@dataclass(frozen=True)
class BossLootTable:
    boss_key: str
    entries: tuple
    rolls: int


@dataclass(frozen=True)
class BossPhase:
    key: str
    min_hp_ratio: float       # active while hp/max_hp is ABOVE this
    moves: tuple              # retaliation pool (engine move dicts)
    damage_mult: float = 1.0
    display_name: str | None = None


@dataclass(frozen=True)
class BossDefinition:
    key: str
    display_name: str
    species_dex: int = 0
    level: int = 70
    types: tuple = ()
    base_stats: dict = field(default_factory=dict)
    max_hp: int = 5000
    duration_seconds: int = 3600
    attack_cooldown_seconds: int = 60
    participation_damage: int = 50
    participation_coins: int = 1000
    top_bonus_coins: int = 5000
    phases: tuple = ()
    loot_table: BossLootTable | None = None
    enabled: bool = True


def validate_definition(defn: BossDefinition) -> None:
    """Fail fast on nonsense config. Called by the registry for all bosses."""
    if not defn.key or not isinstance(defn.key, str):
        raise ValueError('boss key must be a non-empty string')
    if not defn.display_name:
        raise ValueError(f'{defn.key}: display_name required')
    if defn.max_hp <= 0:
        raise ValueError(f'{defn.key}: max_hp must be positive')
    if defn.duration_seconds <= 0:
        raise ValueError(f'{defn.key}: duration_seconds must be positive')
    if defn.attack_cooldown_seconds <= 0:
        raise ValueError(f'{defn.key}: attack_cooldown_seconds must be positive')
    if not defn.phases:
        raise ValueError(f'{defn.key}: at least one phase required')
    ratios = []
    for ph in defn.phases:
        if not (0.0 <= ph.min_hp_ratio < 1.0):
            raise ValueError(f'{defn.key}: phase {ph.key!r} ratio outside [0, 1)')
        ratios.append(ph.min_hp_ratio)
        if not ph.moves:
            raise ValueError(f'{defn.key}: phase {ph.key!r} needs moves')
        for mv in ph.moves:
            if not mv.get('name') or (mv.get('power') or 0) <= 0 \
                    or (mv.get('acc') or 0) <= 0 or not mv.get('ptype'):
                raise ValueError(f'{defn.key}: phase {ph.key!r} has a bad move')
    if ratios != sorted(ratios, reverse=True):
        raise ValueError(f'{defn.key}: phases must order thresholds descending')
    if defn.loot_table is not None:
        lt = defn.loot_table
        if lt.boss_key != defn.key:
            raise ValueError(f'{defn.key}: loot table belongs to {lt.boss_key!r}')
        for e in lt.entries:
            if e.weight < 0 or e.quantity_max < e.quantity_min \
                    or e.quantity_min < 0 or e.min_damage < 0:
                raise ValueError(f'{defn.key}: bad loot entry {e.item_id!r}')


def snapshot_definition(defn: BossDefinition) -> str:
    """Compact JSON snapshot stored on the event row at start."""
    return json.dumps(asdict(defn), sort_keys=True)


def definition_from_snapshot(data: dict) -> BossDefinition:
    """Rebuild a definition from snapshot JSON (lists back to tuples)."""
    data = dict(data)
    data['types'] = tuple(data.get('types') or ())
    data['phases'] = tuple(BossPhase(
        key=p['key'], min_hp_ratio=p['min_hp_ratio'],
        moves=tuple(dict(m) for m in p['moves']),
        damage_mult=p.get('damage_mult', 1.0),
        display_name=p.get('display_name')) for p in data.get('phases') or ())
    lt = data.get('loot_table')
    data['loot_table'] = BossLootTable(
        boss_key=lt['boss_key'], rolls=lt.get('rolls', 1),
        entries=tuple(LootEntry(**e) for e in lt.get('entries') or ())) \
        if lt else None
    data['base_stats'] = dict(data.get('base_stats') or {})
    return BossDefinition(**{k: data[k] for k in BossDefinition.__dataclass_fields__
                             if k in data})

"""Tidecaller: second boss, proving the framework runs two ordinary
configs through one service. Water type, roomier HP pool, higher phase
threshold, gentler enrage, own loot (tidal_scale + water_stone)."""
from game.bosses.models import (BossDefinition, BossLootTable, BossPhase,
                                LootEntry)

TIDECALLER_LOOT = BossLootTable(
    boss_key='tidecaller',
    rolls=1,  # +1 for the top contributor
    entries=(
        LootEntry('poke', 2, 5, 450),
        LootEntry('ultra', 1, 2, 300),
        LootEntry('superpotion', 1, 1, 200),
        LootEntry('tidal_scale', 1, 1, 80, min_damage=75),
        LootEntry('water_stone', 1, 1, 25, min_damage=600),
    ),
)

TIDECALLER = BossDefinition(
    key='tidecaller',
    display_name='Tidecaller',
    species_dex=130,
    level=70,
    types=('water', 'flying'),
    base_stats={'atk': 110, 'spa': 120, 'dfn': 100, 'spd': 100, 'spe': 40,
                'maxhp': 10 ** 9},
    max_hp=7000,
    duration_seconds=3600,
    attack_cooldown_seconds=60,
    participation_damage=75,
    participation_coins=1000,
    top_bonus_coins=5000,
    phases=(
        BossPhase(key='normal', min_hp_ratio=0.60,
                  moves=({'name': 'Water Gun', 'power': 40, 'acc': 100,
                          'ptype': 'water'},),
                  damage_mult=1.0, display_name=None),
        BossPhase(key='raging', min_hp_ratio=0.00,
                  moves=({'name': 'Hydro Pump', 'power': 80, 'acc': 85,
                          'ptype': 'water'},),
                  damage_mult=1.15, display_name='Raging Tide'),
    ),
    loot_table=TIDECALLER_LOOT,
    enabled=True,
)

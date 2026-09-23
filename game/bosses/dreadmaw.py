"""Dreadmaw: the original boss, moved verbatim into a definition.
Values match the pre-framework constants (5000 HP, 1h, 60s cooldown,
50 dmg threshold, 1k/5k coins, Tackle -> Ember 1.2x)."""
from game.bosses.models import (BossDefinition, BossLootTable, BossPhase,
                                LootEntry)

DREADMAW_LOOT = BossLootTable(
    boss_key='dreadmaw',
    rolls=1,  # +1 for the top contributor
    entries=(
        LootEntry('poke', 2, 5, 500),
        LootEntry('great', 1, 2, 250),
        LootEntry('potion', 1, 2, 150),
        LootEntry('dreadmaw_scale', 1, 1, 80, min_damage=50),
        LootEntry('fire_stone', 1, 1, 20, min_damage=500),
    ),
)

DREADMAW = BossDefinition(
    key='dreadmaw',
    display_name='Dreadmaw',
    species_dex=248,
    level=70,
    types=('rock', 'dark'),
    base_stats={'atk': 120, 'spa': 100, 'dfn': 110, 'spd': 90, 'spe': 30,
                'maxhp': 10 ** 9},
    max_hp=5000,
    duration_seconds=3600,
    attack_cooldown_seconds=60,
    participation_damage=50,
    participation_coins=1000,
    top_bonus_coins=5000,
    phases=(
        BossPhase(key='normal', min_hp_ratio=0.50,
                  moves=({'name': 'Tackle', 'power': 40, 'acc': 100,
                          'ptype': 'normal'},),
                  damage_mult=1.0, display_name=None),
        BossPhase(key='enraged', min_hp_ratio=0.00,
                  moves=({'name': 'Ember', 'power': 50, 'acc': 100,
                          'ptype': 'fire'},),
                  damage_mult=1.2, display_name='Enraged'),
    ),
    loot_table=DREADMAW_LOOT,
    enabled=True,
)

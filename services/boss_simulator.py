"""Raid balance simulator + config preview: staff dry-run tooling.

Uses the REAL battle engine (services.battle_service.resolve_turn),
REAL phase resolution and REAL loot rolls — only the inputs are stated
assumptions (fighter species/level/count, heal policy), echoed in every
report so numbers are never mistaken for live data.

Discord-free. DB use is SELECT-only (dex rows via the same builders as
production); simulate_* never INSERT/UPDATE/DELETE. Deterministic per
seed: random.Random(seed) threaded through engine + loot.
"""
import random as _random

import database as db
from services import battle_service as _bt
from services import worldboss_service as _wb

MAX_RAIDS = 1000
MAX_FIGHTERS = 20
MAX_ATTACKS_PER_RAID = 20000


def preview_boss(boss_key: str) -> dict:
    """Structured config snapshot for `;worldboss preview`. Read-only."""
    from game.bosses.registry import registry
    key = (boss_key or '').strip().lower()
    defn = registry.get(key)
    if defn is None:
        return {'ok': False, 'code': 'UNKNOWN_BOSS'}
    total_w = sum(e.weight for e in defn.loot_table.entries) or 1
    return {
        'ok': True, 'key': defn.key, 'name': defn.display_name,
        'enabled': bool(defn.enabled), 'max_hp': defn.max_hp,
        'duration_seconds': defn.duration_seconds,
        'attack_cooldown_seconds': defn.attack_cooldown_seconds,
        'participation_damage': defn.participation_damage,
        'participation_coins': defn.participation_coins,
        'top_bonus_coins': defn.top_bonus_coins,
        'phases': tuple({
            'key': p.key, 'display_name': p.display_name or p.key,
            'above_ratio': p.min_hp_ratio, 'damage_mult': p.damage_mult,
            'effect': p.effect_key,
            'moves': tuple(m.get('name') for m in p.moves),
            'phase_reward': ([p.phase_reward.item_id, p.phase_reward.quantity_min]
                             if p.phase_reward else None),
        } for p in defn.phases),
        'loot': tuple({
            'item': e.item_id, 'qty': [e.quantity_min, e.quantity_max],
            'weight_share': round(e.weight / total_w, 4),
            'min_damage': e.min_damage, 'rolls': defn.loot_table.rolls,
        } for e in defn.loot_table.entries),
    }


def _fighter(gid, dex: int, level: int) -> dict | None:
    """Same snapshot production attacks with (full HP). None when the
    species is absent from the local dex."""
    from cogs.pokemon import _dex_row, calc_stats
    row = _dex_row(int(dex))
    if not row or not row.get('name'):
        return None
    stats = calc_stats(row, int(level))
    types = list(row.get('types') or ['normal']) or ['normal']
    ptype = types[0]
    full = stats['maxhp']
    return {'name': f"Lv{level} {(row.get('name') or 'mon').capitalize()}",
            'level': int(level), 'hp': full, 'stats': stats, 'types': types,
            'held': '', '_maxhp': full,
            'moves': [{'name': f'{ptype.capitalize()} Strike', 'power': 60,
                       'acc': 100, 'ptype': ptype},
                      {'name': 'Tackle', 'power': 40, 'acc': 100,
                       'ptype': 'normal'}]}


def _boss_fighter(defn, hp: int, phase) -> dict:
    return {'name': defn.display_name, 'level': defn.level, 'hp': hp,
            'stats': dict(defn.base_stats), 'types': list(defn.types),
            'held': '', 'moves': [dict(m) for m in phase.moves]}


def _one_raid(gid, defn, template: dict, n: int, team: int, cooldown: int,
              duration: int, heal: bool, rng) -> dict:
    """One raid, event-driven on the cooldown clock. Mirrors production:
    pre-attack phase for the whole turn, strict fainting, fought-phase
    memory only when the boss survives into the phase, one heal budget
    per fighter per raid (carried across switches, like the service).
    Fainted fighters send the next mon (switch_mon) until the team runs
    dry — a wipe needs every fighter out of mons."""
    boss_hp = defn.max_hp
    fs = [{'hp': template['_maxhp'], 'mons': team, 'dmg': 0, 'attacks': 0,
           'healed': False, 'out': False,
           'next': i * (cooldown / max(1, n))} for i in range(n)]
    t, attacks, faints, heals, switches = 0.0, 0, 0, 0, 0
    max_phase = defn.phases[0].key
    while boss_hp > 0 and attacks < MAX_ATTACKS_PER_RAID:
        ready = [f for f in fs if not f['out'] and f['next'] <= duration]
        if not ready:
            break
        f = min(ready, key=lambda x: x['next'])
        t = max(t, f['next'])
        if t > duration:
            break
        phase = _wb.resolve_boss_phase(defn.phases, boss_hp, defn.max_hp)
        me = dict(template, hp=f['hp'])
        wild = _boss_fighter(defn, boss_hp, phase)
        _bt.resolve_turn({'me': me, 'wild': wild, 'log': []}, gid, 0, rng,
                         strict_faint=True, foe_mult=phase.damage_mult)
        dealt = max(0, boss_hp - wild['hp'])
        boss_hp = wild['hp']
        f['dmg'] += dealt
        f['attacks'] += 1
        f['next'] = t + cooldown
        attacks += 1
        if me['hp'] <= 0:
            faints += 1
            f['mons'] -= 1
            if f['mons'] > 0:
                f['hp'] = template['_maxhp']
                switches += 1
            else:
                f['out'] = True
        else:
            f['hp'] = me['hp']
            if heal and not f['healed'] and boss_hp > 0 \
                    and f['hp'] <= template['_maxhp'] / 2:
                f['hp'], f['healed'], heals = template['_maxhp'], True, heals + 1
        if boss_hp > 0:
            new_phase = _wb.resolve_boss_phase(defn.phases, boss_hp, defn.max_hp)
            if new_phase.key != defn.phases[0].key:
                max_phase = new_phase.key
        else:
            break
    if boss_hp <= 0:
        outcome = 'kill'
    elif all(f['out'] for f in fs):
        outcome = 'wipe'
    else:
        outcome = 'expiry'
    return {'outcome': outcome, 'attacks': attacks, 'time_s': min(t, duration),
            'damage': [f['dmg'] for f in fs], 'faints': faints, 'heals': heals,
            'switches': switches, 'max_phase': max_phase}


def _raid_loot(defn, damage_list, max_phase, rng) -> tuple:
    """Per-participant loot for one kill, same rule as claim_rewards:
    weighted rolls + exactly one phase bonus for qualifiers when the raid
    fought the phase. Returns (weighted_rows, guaranteed_rows)."""
    hit = next((p for p in defn.phases if p.key == max_phase), None)
    bonus = ([hit.phase_reward.item_id, max(1, hit.phase_reward.quantity_min)]
             if hit is not None and hit.phase_reward is not None else None)
    top_dmg = max(damage_list) if damage_list else 0
    w_rows, g_rows = [], []
    for d in damage_list:
        if d < defn.participation_damage:
            continue
        w_rows.extend(_wb.roll_boss_loot(defn.loot_table, d, d >= top_dmg and d > 0,
                                         rng))
        if bonus is not None:
            g_rows.append(tuple(bonus))
    return w_rows, g_rows


def simulate_raids(boss_key: str, dex: int = 133, level: int = 100,
                   fighters: int = 5, team_size: int = 6, raids: int = 200,
                   seed: int = 0, heal_policy: str = 'none', gid=0) -> dict:
    """Monte-Carlo raid simulation. All caps clamped; bad profiles
    rejected without running. Returns a JSON-safe report dict."""
    from game.bosses.registry import registry
    key = (boss_key or '').strip().lower()
    defn = registry.get(key)
    if defn is None:
        return {'ok': False, 'code': 'UNKNOWN_BOSS'}
    try:
        level, n, team = int(level), int(fighters), int(team_size)
    except Exception:
        return {'ok': False, 'code': 'BAD_PROFILE'}
    if not (1 <= level <= 100 and 1 <= n <= MAX_FIGHTERS and 1 <= team <= 6):
        return {'ok': False, 'code': 'BAD_PROFILE'}
    try:
        raids = max(1, min(MAX_RAIDS, int(raids)))
    except Exception:
        raids = 200
    try:
        dex = int(dex)
    except Exception:
        return {'ok': False, 'code': 'UNKNOWN_DEX'}
    template = _fighter(gid, dex, level)
    if template is None:
        return {'ok': False, 'code': 'UNKNOWN_DEX'}
    heal = str(heal_policy or 'none').lower() == 'coin'
    cooldown, duration = defn.attack_cooldown_seconds, defn.duration_seconds
    outcomes = {'kill': 0, 'expiry': 0, 'wipe': 0}
    atk_to_kill, clear_times, faints, heals, switches = [], [], 0, 0, 0
    all_dmg, all_atk, part_dmgs = 0, 0, []
    phase_hits: dict = {}
    w_agg: dict = {}
    g_agg: dict = {}
    for i in range(raids):
        rng = _random.Random(int(seed) * 1000003 + i)
        r = _one_raid(gid, defn, template, n, team, cooldown, duration, heal, rng)
        outcomes[r['outcome']] += 1
        faints += r['faints']
        heals += r['heals']
        switches += r['switches']
        all_dmg += sum(r['damage'])
        all_atk += r['attacks']
        if r['outcome'] == 'kill':
            part_dmgs.extend(r['damage'])
            atk_to_kill.append(r['attacks'])
            clear_times.append(r['time_s'])
            phase_hits[r['max_phase']] = phase_hits.get(r['max_phase'], 0) + 1
            w_rows, g_rows = _raid_loot(
                defn, r['damage'], r['max_phase'],
                _random.Random(int(seed) * 1000003 + 7919 + i))
            for item, q in w_rows:
                w_agg[item] = w_agg.get(item, 0) + q
            for item, q in g_rows:
                g_agg[item] = g_agg.get(item, 0) + q
    kills = outcomes['kill']
    below = sum(1 for d in part_dmgs if d < defn.participation_damage)
    return {
        'ok': True, 'boss_key': defn.key, 'boss_name': defn.display_name,
        'assumptions': {'dex': dex, 'level': level, 'fighters': n,
                        'team_size': team, 'raids': raids, 'seed': int(seed),
                        'heal_policy': 'coin' if heal else 'none',
                        'fighter': template['name'],
                        'fighter_maxhp': template['_maxhp']},
        'raids': raids, 'kills': kills, 'expiries': outcomes['expiry'],
        'wipes': outcomes['wipe'],
        'kill_rate': round(kills / raids, 4) if raids else 0.0,
        'avg_attacks_to_kill': round(sum(atk_to_kill) / kills, 1) if kills else None,
        'avg_clear_time_s': round(sum(clear_times) / kills, 1) if kills else None,
        'duration_seconds': duration,
        'avg_faints_per_raid': round(faints / raids, 2) if raids else 0.0,
        'avg_heals_per_raid': round(heals / raids, 2) if raids else 0.0,
        'avg_switches_per_raid': round(switches / raids, 2) if raids else 0.0,
        'avg_dmg_per_attack': round(all_dmg / all_atk, 1) if all_atk else 0.0,
        'phase_reach': {k: round(v / kills, 4) for k, v in phase_hits.items()} if kills else {},
        'below_threshold_pct': (round(below / len(part_dmgs), 4)
                                if part_dmgs else None),
        'participation_damage': defn.participation_damage,
        'loot_weighted_per_raid': {k: round(v / raids, 3) for k, v in sorted(w_agg.items())},
        'loot_guaranteed_per_raid': {k: round(v / raids, 3) for k, v in sorted(g_agg.items())},
        'loot_weighted_total': dict(sorted(w_agg.items())),
        'loot_guaranteed_total': dict(sorted(g_agg.items())),
    }

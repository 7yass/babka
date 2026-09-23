"""Boss-material crafting: the sink for dreadmaw_scale / tidal_scale.

Discord-free. One transaction per craft (check all, take all, give all)
through services._common helpers, so zero-qty/row semantics match the
rest of the inventory system. Callers own rendering.

Tradability (deliberate): scales and outputs live in pk_balls, and no
market/trade path moves pk_balls rows (both systems move mons only).
So everything here is untradable by construction — no speculation, no
raid-mandatory power. Revisit only with a real item-transfer design.

Outputs are consumable raid utilities or a trophy: no permanent combat
power until balancing data exists.
"""
from dataclasses import dataclass

import database as db
from services._common import InsufficientItemError, remove_item, upsert_item

MAX_QTY = 99

# Every item id the recipes touch. Materials drop from raids; outputs
# are made here. Display names/descriptions live in lang (name/desc keys)
# so the service never touches Discord or locale state.
CRAFT_ITEMS = {
    'dreadmaw_scale': {'name_key': 'eco.craft_name_dreadmaw_scale',
                       'desc_key': 'eco.craft_desc_dreadmaw_scale'},
    'tidal_scale': {'name_key': 'eco.craft_name_tidal_scale',
                    'desc_key': 'eco.craft_desc_tidal_scale'},
    'volcanic_charm': {'name_key': 'eco.craft_name_volcanic_charm',
                       'desc_key': 'eco.craft_desc_volcanic_charm'},
    'tidal_charm': {'name_key': 'eco.craft_name_tidal_charm',
                    'desc_key': 'eco.craft_desc_tidal_charm'},
    'raid_charm': {'name_key': 'eco.craft_name_raid_charm',
                   'desc_key': 'eco.craft_desc_raid_charm'},
}


@dataclass(frozen=True)
class ItemCost:
    item: str
    qty: int


@dataclass(frozen=True)
class ItemReward:
    item: str
    qty: int


@dataclass(frozen=True)
class Recipe:
    key: str
    inputs: tuple
    outputs: tuple
    enabled: bool = True
    unlock_key: str | None = None


@dataclass(frozen=True)
class RecipeInputView:
    item: str
    qty_each: int
    qty_total: int
    have: int


@dataclass(frozen=True)
class RecipeView:
    key: str
    inputs: tuple
    outputs: tuple
    enabled: bool
    can_afford: bool


@dataclass(frozen=True)
class CraftResult:
    ok: bool
    code: str            # CRAFTED / UNKNOWN_RECIPE / DISABLED /
                         # BAD_QUANTITY / INSUFFICIENT_MATERIALS
    recipe_key: str
    quantity: int
    consumed: tuple = ()   # ((item, qty))
    produced: tuple = ()   # ((item, qty))
    missing: tuple = ()    # ((item, need, have)) when insufficient


RECIPES = (
    Recipe(key='volcanic_charm',
           inputs=(ItemCost('dreadmaw_scale', 3),),
           outputs=(ItemReward('volcanic_charm', 1),)),
    Recipe(key='tidal_charm',
           inputs=(ItemCost('tidal_scale', 3),),
           outputs=(ItemReward('tidal_charm', 1),)),
    Recipe(key='raid_charm',
           inputs=(ItemCost('volcanic_charm', 1), ItemCost('tidal_charm', 1)),
           outputs=(ItemReward('raid_charm', 1),)),
)


def validate_recipe_definitions(recipes=RECIPES, catalog=CRAFT_ITEMS) -> None:
    """Startup validation. Raises ValueError on the first defect."""
    seen = set()
    for r in recipes:
        if not r.key or not isinstance(r.key, str):
            raise ValueError('recipe key must be a non-empty string')
        if r.key in seen:
            raise ValueError(f'duplicate recipe key {r.key!r}')
        seen.add(r.key)
        if not r.inputs or not r.outputs:
            raise ValueError(f'{r.key}: needs at least one input and one output')
        for c in r.inputs:
            if not c.item or int(c.qty) <= 0:
                raise ValueError(f'{r.key}: input quantities must be positive')
            if c.item not in catalog:
                raise ValueError(f'{r.key}: unknown input item {c.item!r}')
        for o in r.outputs:
            if not o.item or int(o.qty) <= 0:
                raise ValueError(f'{r.key}: output quantities must be positive')
            if o.item not in catalog:
                raise ValueError(f'{r.key}: unknown output item {o.item!r}')
        if not isinstance(r.enabled, bool):
            raise ValueError(f'{r.key}: enabled must be bool')


validate_recipe_definitions()


def normalize_key(raw: str) -> str:
    return (raw or '').strip().lower().replace(' ', '_').replace('-', '_')


class CraftingService:
    def __init__(self, recipes=RECIPES):
        self._recipes = {r.key: r for r in recipes}

    def get_recipe(self, recipe_key: str) -> Recipe | None:
        return self._recipes.get(normalize_key(recipe_key))

    def list_recipes(self, gid, uid) -> list:
        """All recipes with per-input holdings and affordability (qty 1)."""
        gid, uid = str(gid), str(uid)
        with db.conn_ctx() as conn:
            have = {}
            for r in self._recipes.values():
                for c in r.inputs:
                    if c.item not in have:
                        row = conn.execute(
                            'SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                            (gid, uid, c.item)).fetchone()
                        have[c.item] = int(row['qty'] or 0) if row else 0
        out = []
        for r in self._recipes.values():
            inputs = tuple(RecipeInputView(item=c.item, qty_each=int(c.qty),
                                           qty_total=int(c.qty),
                                           have=have.get(c.item, 0))
                           for c in r.inputs)
            out.append(RecipeView(
                key=r.key,
                inputs=inputs,
                outputs=tuple((o.item, int(o.qty)) for o in r.outputs),
                enabled=bool(r.enabled),
                can_afford=bool(r.enabled) and all(
                    have.get(c.item, 0) >= int(c.qty) for c in r.inputs)))
        return out

    def craft(self, gid, uid, recipe_key: str, quantity: int = 1) -> CraftResult:
        """One transaction: validate, check all, remove all, upsert all.
        Any shortfall (including a lost race) rolls everything back."""
        gid, uid = str(gid), str(uid)
        recipe = self.get_recipe(recipe_key)
        key = normalize_key(recipe_key)
        if recipe is None:
            return CraftResult(False, 'UNKNOWN_RECIPE', key, 0)
        if not recipe.enabled:
            return CraftResult(False, 'DISABLED', recipe.key, 0)
        try:
            qty = int(quantity)
        except Exception:
            return CraftResult(False, 'BAD_QUANTITY', recipe.key, 0)
        if qty < 1 or qty > MAX_QTY:
            return CraftResult(False, 'BAD_QUANTITY', recipe.key, qty)
        need = [(c.item, int(c.qty) * qty) for c in recipe.inputs]
        try:
            with db.conn_ctx() as conn:
                missing = []
                for item, total in need:
                    row = conn.execute(
                        'SELECT qty FROM pk_balls WHERE guild_id=? AND user_id=? AND ball=?',
                        (gid, uid, item)).fetchone()
                    have = int(row['qty'] or 0) if row else 0
                    if have < total:
                        missing.append((item, total, have))
                if missing:
                    return CraftResult(False, 'INSUFFICIENT_MATERIALS', recipe.key,
                                       qty, missing=tuple(missing))
                for item, total in need:
                    remove_item(conn, gid, uid, item, total)
                made = []
                for o in recipe.outputs:
                    upsert_item(conn, gid, uid, o.item, int(o.qty) * qty)
                    made.append((o.item, int(o.qty) * qty))
        except InsufficientItemError as e:
            # Lost a race between the check and the take: nothing committed.
            return CraftResult(False, 'INSUFFICIENT_MATERIALS', recipe.key, qty,
                               missing=tuple([(e.item, e.quantity, e.have)]))
        return CraftResult(True, 'CRAFTED', recipe.key, qty,
                           consumed=tuple(need), produced=tuple(made))


service = CraftingService()

"""Crafting: boss-material sink. Thin Discord layer over
services.crafting_service — recipe list, detail, confirmation, result.
The Confirm button revalidates through the service (never trusts preview).
"""
import discord
from discord.ext import commands

from lang import t
from services.crafting_service import CRAFT_ITEMS, normalize_key, service


def item_name(gid, item: str) -> str:
    meta = CRAFT_ITEMS.get(item)
    if meta:
        try:
            return t(gid, meta['name_key'])
        except Exception:
            pass
    return item


def item_desc(gid, item: str) -> str:
    meta = CRAFT_ITEMS.get(item)
    if meta:
        try:
            return t(gid, meta['desc_key'])
        except Exception:
            pass
    return ''


def build_recipe_list_text(gid, views) -> str:
    lines = [t(gid, 'eco.craft_title')]
    for v in views:
        if not v.enabled:
            continue
        cost = ' + '.join(f"{item_name(gid, i.item)} x{i.qty_each}" for i in v.inputs)
        makes = ' + '.join(f"{item_name(gid, o)} x{q}" for o, q in v.outputs)
        mark = '✅' if v.can_afford else '—'
        lines.append(f"{mark} **{item_name(gid, v.key)}** (`;craft {v.key}`): {cost} → {makes}")
    lines.append(t(gid, 'eco.craft_hint'))
    return '\n'.join(lines)


def build_recipe_detail_text(gid, view, qty: int = 1) -> str:
    cost = '\n'.join(f"- {item_name(gid, i.item)} x{i.qty_each * qty} "
                     f"({t(gid, 'eco.craft_have', n=i.have)})" for i in view.inputs)
    makes = '\n'.join(f"+ {item_name(gid, o)} x{q * qty}" for o, q in view.outputs)
    head = f"**{item_name(gid, view.key)}**\n{item_desc(gid, view.key)}"
    return (f"{head}\n{t(gid, 'eco.craft_cost')}\n{cost}\n"
            f"{t(gid, 'eco.craft_receive')}\n{makes}")


def build_confirm_text(gid, view, qty: int) -> str:
    return (f"{t(gid, 'eco.craft_confirm', n=qty, name=item_name(gid, view.key))}\n"
            + build_recipe_detail_text(gid, view, qty))


def build_result_text(gid, res) -> str:
    if res.ok:
        got = ', '.join(f"{item_name(gid, o)} x{q}" for o, q in res.produced)
        return t(gid, 'eco.craft_done', items=got)
    if res.code == 'INSUFFICIENT_MATERIALS':
        bits = ', '.join(f"{item_name(gid, i)} x{need} "
                         f"({t(gid, 'eco.craft_have', n=have)})"
                         for i, need, have in (res.missing or ()))
        return t(gid, 'eco.craft_missing', bits=bits)
    if res.code == 'DISABLED':
        return t(gid, 'eco.craft_disabled')
    if res.code == 'BAD_QUANTITY':
        return t(gid, 'eco.craft_bad_qty')
    return t(gid, 'eco.craft_no_recipe')


class ConfirmView(discord.ui.View):
    def __init__(self, gid, uid, recipe_key: str, qty: int):
        super().__init__(timeout=60)
        self.gid, self.uid = gid, uid
        self.recipe_key, self.qty = recipe_key, qty

    async def _finish(self, interaction: discord.Interaction, text: str):
        try:
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(content=text, view=self)
        except Exception:
            try:
                await interaction.followup.send(text, ephemeral=True)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label='✅', style=discord.ButtonStyle.green,
                       custom_id='craft_yes')
    async def yes(self, interaction: discord.Interaction,
                  button: discord.ui.Button):
        from lang import set_ctx_lang
        set_ctx_lang(interaction.user)
        if str(interaction.user.id) != str(self.uid):
            return await interaction.response.send_message(
                t(self.gid, 'eco.not_yours'), ephemeral=True)
        res = service.craft(self.gid, self.uid, self.recipe_key, self.qty)
        await self._finish(interaction, build_result_text(self.gid, res))

    @discord.ui.button(label='✖', style=discord.ButtonStyle.grey,
                       custom_id='craft_no')
    async def no(self, interaction: discord.Interaction,
                 button: discord.ui.Button):
        if str(interaction.user.id) != str(self.uid):
            return await interaction.response.send_message(
                t(self.gid, 'eco.not_yours'), ephemeral=True)
        await self._finish(interaction, t(self.gid, 'eco.craft_cancelled'))


class Craft(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _view_for(self, gid, uid, key: str):
        recipe = service.get_recipe(key)
        if recipe is None:
            return None
        for v in service.list_recipes(gid, uid):
            if v.key == recipe.key:
                return v
        return None

    @commands.group(name='craft', description='Boss crafting',
                    invoke_without_command=True)
    async def craft(self, ctx, recipe: str = '', qty: str = ''):
        """`;craft` lists, `;craft info <key>` details, `;craft <key> [n]`
        asks for confirmation (revalidated on click)."""
        gid, uid = ctx.guild.id, ctx.author.id
        arg = (recipe or '').strip()
        if not arg:
            views = service.list_recipes(gid, uid)
            return await ctx.reply(build_recipe_list_text(gid, views),
                                   mention_author=False)
        if arg.lower() == 'info':
            v = self._view_for(gid, uid, qty)
            if v is None:
                return await ctx.reply(t(gid, 'eco.craft_no_recipe'), ephemeral=True)
            return await ctx.reply(build_recipe_detail_text(gid, v),
                                   mention_author=False)
        key = normalize_key(arg)
        try:
            n = max(1, min(99, int((qty or '1').strip() or 1)))
        except Exception:
            return await ctx.reply(t(gid, 'eco.craft_bad_qty'), ephemeral=True)
        v = self._view_for(gid, uid, key)
        if v is None:
            return await ctx.reply(t(gid, 'eco.craft_no_recipe'), ephemeral=True)
        if not v.enabled:
            return await ctx.reply(t(gid, 'eco.craft_disabled'), ephemeral=True)
        short = [(i.item, i.qty_each * n, i.have) for i in v.inputs
                 if i.have < i.qty_each * n]
        if short:
            bits = ', '.join(f"{item_name(gid, i)} x{need} "
                             f"({t(gid, 'eco.craft_have', n=have)})"
                             for i, need, have in short)
            return await ctx.reply(t(gid, 'eco.craft_missing', bits=bits),
                                   ephemeral=True)
        view = ConfirmView(gid, uid, v.key, n)
        try:
            view.children[0].label = t(gid, 'eco.craft_yes')
            view.children[1].label = t(gid, 'eco.craft_no')
        except Exception:
            pass
        await ctx.reply(build_confirm_text(gid, v, n), view=view,
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(Craft(bot))

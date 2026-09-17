"""Help center: home + module dropdown + command detail. Port of the Python original."""
import asyncio
import json
from pathlib import Path

import discord
from discord.ext import commands

import database as db
from lang import t, STR, get_lang, cur_lang, set_ctx_lang

META = json.loads((Path(__file__).parent.parent / 'helpmeta.json').read_text(encoding='utf-8'))
COMMAND_META = META['meta']       # name -> [module, desc_en, aliases, example, perms, slashonly]
CATEGORIES = META['cats']         # key -> [label_en, desc_en]
WHITE, RED = 0xFFFFFF, 0xFFFFFF
HELP_DELETE, CMD_DELETE = 60, 30

MOD_PL = {'levels': 'Levele', 'voice': 'Głosówka', 'moderation': 'Mody',
          'protection': 'Ochrona', 'info': 'Info', 'setup': 'Start'}
PERM_PL = {'Everyone': 'Wszyscy', 'Manage Server': 'Zarządzanie serwerem', 'Administrator': 'Administrator',
           'Ban Members': 'Banowanie', 'Manage Messages': 'Zarządzanie wiadomościami', 'Kick Members': 'Wyrzucanie',
           'Moderate Members': 'Wyciszanie', 'Manage Channels': 'Zarządzanie kanałami',
           'Manage Nicknames': 'Zmiana nicków', 'Manage Roles': 'Zarządzanie rolami'}


def L(gid, key, fallback):
    lang = cur_lang(gid)
    return (STR.get(lang) or {}).get(key, fallback)


EXAMPLE_FILL = {'<member>': '@{me}', '<amount>': '50', '<duration>': '10m', '<reason>': 'breaking rules',
                '<number>': '5', '<level>': '5', '<days>': '60', '<seconds>': '10', '<joins>': '5',
                '<word>': 'spam', '<username>': 'someone', '<nick>': 'newname', '<0-10>': '2',
                '<user_id>': '123456789012345678', '<subcommand>': 'status', '<command>': 'ban'}


def _box(gid, *blocks):
    from discord.ui import LayoutView, Container, TextDisplay, Separator
    layout = LayoutView(timeout=HELP_DELETE)
    box = Container(accent_color=0xFFFFFF)
    for i, b in enumerate(blocks):
        if i:
            box.add_item(Separator(visible=False))
        box.add_item(TextDisplay(b))
    layout.add_item(box)
    return layout


def home_layout(gid, author, bot_user, prefix):
    head = f'# {t(gid, "hc.help_title", name=bot_user.name)}'
    body = (f'**{t(gid, "hc.how_t")}**\n{t(gid, "hc.how_v")}\n\n'
            f'**{t(gid, "hc.pslash_t")}**\n{t(gid, "hc.pslash_v", p=prefix)}\n\n'
            f'**{t(gid, "hc.legend_t")}**\n{t(gid, "hc.legend_v")}\n\n'
            f'-# {t(gid, "hc.foot", n=len(COMMAND_META), m=len(CATEGORIES), s=HELP_DELETE)}')
    return _box(gid, head, body)


def category_layout(gid, key, prefix):
    label = L(gid, f'hc.c_{key}_t', CATEGORIES[key][0])
    desc = L(gid, f'hc.c_{key}_d', CATEGORIES[key][1])
    cmds = sorted(n for n, v in COMMAND_META.items() if v[0] == key)
    lst = ', '.join(n + '/' if len(v) > 5 and v[5] else n for n, v in ((n, COMMAND_META[n]) for n in cmds))
    # View budget is 4000 chars total: cap a runaway category listing.
    if len(lst) > 3000:
        lst = lst[:3000] + f'… +{len(cmds)}'
    body = (f'# {label}\n{desc}\n{t(gid, "hc.cat_hint", p=prefix)}\n'
            f'```{lst}```\n-# {t(gid, "hc.count", n=len(cmds))}')
    return _box(gid, body)


def command_layout(gid, author, name, prefix):
    module, desc_en, aliases, example, perms = COMMAND_META[name][:5]
    slashonly = len(COMMAND_META[name]) > 5 and COMMAND_META[name][5]
    desc = L(gid, f'hc_d_{name}', desc_en)
    usage = f'/{name}' if slashonly else f'{prefix}{name}'
    if example:
        clean = example
        for k, v in EXAMPLE_FILL.items():
            clean = clean.replace(k, v)
        import re
        clean = re.sub(r'\[.*?\]', '', clean).strip().replace('{me}', author.name)
        code = f"{t(gid, 'hc.syntax')}  {usage} {example}\n" + (f"{t(gid, 'hc.example')} {usage} {clean}" if clean else '')
    else:
        code = f"{t(gid, 'hc.syntax')}  {usage}"
    if cur_lang(gid) == 'pl':
        module = MOD_PL.get(module, module)
        perms = PERM_PL.get(perms, perms)
    else:
        module = module.capitalize()
    body = (f'# {usage}\n{desc}\n'
            f'**{t(gid, "hc.aliases")}** {", ".join(aliases) if aliases else t(gid, "hc.none")}\n'
            f'**{t(gid, "hc.module")}** {module} · **{t(gid, "hc.perms")}** {perms or t(gid, "hc.na")}\n'
            f'```{code}```\n-# {t(gid, "hc.opt")}')
    return _box(gid, body)


class HelpSelect(discord.ui.Select):
    def __init__(self, gid):
        opts = [discord.SelectOption(label=t(gid, 'hc.home'), value='home', description=t(gid, 'hc.home_d'))]
        for key, (label, desc) in CATEGORIES.items():
            opts.append(discord.SelectOption(label=L(gid, f'hc.c_{key}_t', label), value=key,
                                             description=L(gid, f'hc.c_{key}_d', desc)[:100]))
        super().__init__(placeholder=t(gid, 'hc.pick'), options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        try:
            await self._run(interaction)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                await interaction.response.send_message('help broke — admins check console.',
                                                        ephemeral=True)
            except Exception:
                pass

    async def _run(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        invoker_id = getattr(self.view, 'invoker_id', None)
        if interaction.user.id != invoker_id:
            return await interaction.response.send_message(t(interaction.guild_id, 'hc.notyours'), ephemeral=True)
        prefix = db.get_prefix(interaction.guild_id)
        me = interaction.user
        if self.values[0] == 'home':
            layout = home_layout(interaction.guild_id, me, interaction.client.user, prefix)
        else:
            layout = category_layout(interaction.guild_id, self.values[0], prefix)
        layout.invoker_id = invoker_id
        _attach_select(layout, HelpSelect(interaction.guild_id))
        await interaction.response.edit_message(view=layout)


def _attach_select(layout, select):
    from discord.ui import ActionRow
    for child in layout.children:
        if type(child).__name__ == 'Container':
            row = ActionRow()
            row.add_item(select)
            child.add_item(row)
            return
    row = ActionRow()
    row.add_item(select)
    layout.add_item(row)


async def autodelete_view(msg: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def autodelete(msg: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='help', description='Centrum pomocy', aliases=['h', 'commands'])
    async def help_cmd(self, ctx, *, query: str = None):
        gid = ctx.guild.id if ctx.guild else None
        prefix = db.get_prefix(gid) if gid else '.'
        if not query:
            layout = home_layout(gid, ctx.author, self.bot.user, prefix)
            layout.invoker_id = ctx.author.id
            _attach_select(layout, HelpSelect(gid))
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete_view(msg, HELP_DELETE))
            return
        q = query.lower().strip()
        if q in CATEGORIES:
            layout = category_layout(gid, q, prefix)
            layout.invoker_id = ctx.author.id
            _attach_select(layout, HelpSelect(gid))
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete_view(msg, HELP_DELETE))
            return
        name = q if q in COMMAND_META else next(
            (n for n, v in COMMAND_META.items() if q in (v[2] or [])), None)
        if name:
            layout = command_layout(gid, ctx.author, name, prefix)
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete(msg, CMD_DELETE))
            return
        msg = await ctx.reply(embed=discord.Embed(
            description=t(gid, 'hc.nope', q=query, p=prefix), color=RED), mention_author=False)
        asyncio.create_task(autodelete(msg, CMD_DELETE))


async def setup(bot):
    await bot.add_cog(Help(bot))

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


# Navigation systems: the front door. Primary entries MUST be canonical
# command paths ("levels add-xp", never "addxp"); aliases stay functional
# for users but are never navigation targets. Verified by `.health commands`
# and tools/test_help_nav.py — a primary that stops resolving fails loudly
# instead of pointing at a missing command.
HELP_GROUPS = {
    'economy': {
        'primary': ('daily', 'work', 'shop', 'bank', 'pay', 'deposit'),
        'related': ('market', 'casino', 'crime'),
    },
    'pokemon': {
        'primary': ('p', 'hunt', 'catch', 'box', 'team', 'battle', 'quests', 'trade'),
        'related': ('market',),
    },
    'market': {
        'primary': ('market', 'buy', 'sell', 'unlist'),
        'related': ('pokemon', 'economy'),
    },
    'casino': {
        'primary': ('blackjack', 'slots', 'roulette', 'coinflip', 'poker'),
        'related': ('crime', 'economy'),
    },
    'crime': {
        'primary': ('heist', 'rob', 'bounty', 'bail'),
        'related': ('casino', 'economy'),
    },
    'profile': {
        'primary': ('profile', 'rank', 'achievements', 'trainer'),
        'related': ('pokemon', 'economy'),
    },
    'server': {
        'primary': ('setup', 'tickets', 'verify', 'welcome'),
        'related': (),
    },
}


def resolve_command(bot, dotted: str):
    """Find a command by canonical dotted path ('levels add-xp').
    Aliases never match: only .name at each level. Returns the command or None."""
    parts = (dotted or '').split()
    if not parts:
        return None
    level = {c.name: c for c in bot.commands}
    node = None
    for p in parts:
        node = level.get(p)
        if node is None:
            return None
        level = {c.name: c for c in getattr(node, 'commands', [])}
    return node


def implemented_names(bot) -> set:
    """Lowercased canonical names + aliases that exist on the running bot.

    helpmeta.json carries ~100 legacy entries with no implementation behind
    them; without this filter `.help <module>` advertises commands whose only
    answer is silence (main.py swallows CommandNotFound)."""
    if bot is None:
        return set()
    try:
        out = set()
        for c in bot.walk_commands():
            out.add(str(c.name).lower())
            for a in (getattr(c, 'aliases', None) or []):
                out.add(str(a).lower())
        return out
    except Exception:
        return set()


def suggest_command(gid, name: str, prefix: str, bot=None) -> str:
    """Legacy name a user typed -> the canonical command behind it, using
    helpmeta's alias lists (they document the PokeMeow/Node names people
    remember). Returns a hint, or '' when nothing sensible matches."""
    q = (name or '').strip().lower()
    if not q:
        return ''
    impl = implemented_names(bot)
    for n, v in COMMAND_META.items():
        if n.lower() == q:
            continue  # they typed the canonical name — that's a different problem
        if q not in [str(a).lower() for a in (v[2] or [])]:
            continue
        if impl and n.lower() not in impl:
            continue  # never point at a command that isn't there
        use_prefix = _cmd_prefix(bot, gid, prefix, n) if bot is not None else prefix
        s = L(gid, 'hc.cmd_hint', '`{q}` is not a command — try `{p}{n}`')
        return s.replace('{q}', q).replace('{p}', use_prefix).replace('{n}', n)
    return ''


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


def _cmd_prefix(bot, gid, guild_prefix, dotted: str) -> str:
    """Display prefix for a nav entry: ';' for Pokemon-cog commands
    (the prefix gate blocks them on '.'), guild prefix otherwise."""
    try:
        cmd = resolve_command(bot, dotted)
        cog = getattr(getattr(cmd, 'cog', None), 'qualified_name', '')
        if cog == 'Pokemon':
            return ';'
    except Exception:
        pass
    return guild_prefix


def _cmd_ref(bot, gid, guild_prefix, dotted: str) -> str:
    return f'`{_cmd_prefix(bot, gid, guild_prefix, dotted)}{dotted}`'


def nav_layout(bot, gid, key, guild_prefix):
    """One system page: purpose, primary commands, related pages, next hint.
    Unresolvable primaries are omitted (health flags them)."""
    meta = HELP_GROUPS[key]
    title = L(gid, f'hc.nav_{key}_t', key.title())
    desc = L(gid, f'hc.nav_{key}_d', '')
    primaries = [d for d in meta['primary'] if resolve_command(bot, d) is not None]
    body = f'# {title}\n{desc}\n\n**{t(gid, "hc.nav_start")}**\n'
    body += '\n'.join(_cmd_ref(bot, gid, guild_prefix, d) for d in primaries)
    if meta['related']:
        body += (f'\n\n**{t(gid, "hc.nav_related")}**\n'
                 + '  '.join(f'`{guild_prefix}help {r}`' for r in meta['related']
                             if r in HELP_GROUPS))
    if primaries:
        body += f'\n\n**{t(gid, "hc.nav_next")}** {_cmd_ref(bot, gid, guild_prefix, primaries[0])}'
    return _box(gid, body)


def nav_front_layout(bot, gid, guild_prefix):
    """Front door: every system + its first actions."""
    blocks = [f'# {t(gid, "hc.nav_head")}']
    for key in HELP_GROUPS:
        title = L(gid, f'hc.nav_{key}_t', key.title())
        firsts = [d for d in HELP_GROUPS[key]['primary'][:3]
                  if resolve_command(bot, d) is not None]
        line = f'**{title}**  `{guild_prefix}help {key}`'
        if firsts:
            line += '\n' + ' · '.join(_cmd_ref(bot, gid, guild_prefix, d) for d in firsts)
        blocks.append(line)
    return _box(gid, *blocks)


def start_layout(gid, bot_name: str):
    return _box(gid, f'# {t(gid, "hc.start_title", name=bot_name)}\n'
                     f'{t(gid, "hc.start_body", bot=bot_name)}')


def home_layout(gid, author, bot_user, prefix, bot=None):
    head = f'# {t(gid, "hc.help_title", name=bot_user.name)}'
    impl = implemented_names(bot)
    n_cmds = (len([n for n in COMMAND_META if n.lower() in impl]) if impl
              else len(COMMAND_META))
    body = (f'**{t(gid, "hc.how_t")}**\n{t(gid, "hc.how_v")}\n\n'
            f'**{t(gid, "hc.pslash_t")}**\n{t(gid, "hc.pslash_v", p=prefix)}\n\n'
            f'**{t(gid, "hc.legend_t")}**\n{t(gid, "hc.legend_v")}\n\n'
            f'-# {t(gid, "hc.foot", n=n_cmds, m=len(CATEGORIES), s=HELP_DELETE)}')
    return _box(gid, head, body)


def category_layout(gid, key, prefix, bot=None):
    label = L(gid, f'hc.c_{key}_t', CATEGORIES[key][0])
    desc = L(gid, f'hc.c_{key}_d', CATEGORIES[key][1])
    impl = implemented_names(bot)
    # Only commands that actually exist: the module listing used to advertise
    # the whole legacy helpmeta backlog alongside the real ones.
    cmds = sorted(n for n, v in COMMAND_META.items()
                  if v[0] == key and (not impl or n.lower() in impl))
    lst = ', '.join(n + '/' if len(v) > 5 and v[5] else n for n, v in ((n, COMMAND_META[n]) for n in cmds))
    # View budget is 4000 chars total: cap a runaway category listing.
    if len(lst) > 3000:
        lst = lst[:3000] + f'… +{len(cmds)}'
    body = (f'# {label}\n{desc}\n{t(gid, "hc.cat_hint", p=prefix)}\n'
            f'```{lst}```\n-# {t(gid, "hc.count", n=len(cmds))}')
    return _box(gid, body)


def command_layout(gid, author, name, prefix, bot=None):
    module, desc_en, aliases, example, perms = COMMAND_META[name][:5]
    slashonly = len(COMMAND_META[name]) > 5 and COMMAND_META[name][5]
    desc = L(gid, f'hc_d_{name}', desc_en)
    impl = implemented_names(bot)
    # helpmeta lists legacy aliases the cogs never defined; showing them points
    # people at commands that stay silent.
    if impl:
        aliases = [a for a in aliases if str(a).lower() in impl]
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
            layout = home_layout(interaction.guild_id, me, interaction.client.user,
                                 prefix, interaction.client)
        else:
            layout = category_layout(interaction.guild_id, self.values[0], prefix,
                                     interaction.client)
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
            layout = nav_front_layout(self.bot, gid, prefix)
            layout.invoker_id = ctx.author.id
            _attach_select(layout, HelpSelect(gid))
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete_view(msg, HELP_DELETE))
            return
        q = query.lower().strip()
        if q in HELP_GROUPS:
            layout = nav_layout(self.bot, gid, q, prefix)
            layout.invoker_id = ctx.author.id
            _attach_select(layout, HelpSelect(gid))
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete_view(msg, HELP_DELETE))
            return
        if q in CATEGORIES:
            layout = category_layout(gid, q, prefix, self.bot)
            layout.invoker_id = ctx.author.id
            _attach_select(layout, HelpSelect(gid))
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete_view(msg, HELP_DELETE))
            return
        # Only offer a detail page for a command that exists: helpmeta carries
        # 104 entries with no implementation behind them, and a detail page for
        # one of those is a dead end.
        impl = implemented_names(self.bot)
        name = q if (q in COMMAND_META and q.lower() in impl) else next(
            (n for n, v in COMMAND_META.items()
             if q in (v[2] or []) and n.lower() in impl), None)
        if name:
            layout = command_layout(gid, ctx.author, name, prefix, self.bot)
            msg = await ctx.reply(view=layout, mention_author=False)
            asyncio.create_task(autodelete(msg, CMD_DELETE))
            return
        msg = await ctx.reply(embed=discord.Embed(
            description=t(gid, 'hc.nope', q=query, p=prefix), color=RED), mention_author=False)
        asyncio.create_task(autodelete(msg, CMD_DELETE))

    @commands.command(name='start', description='Od czego zacząć')
    async def start_cmd(self, ctx):
        gid = ctx.guild.id if ctx.guild else None
        name = self.bot.user.name if self.bot.user else 'Babka'
        msg = await ctx.reply(view=start_layout(gid, name), mention_author=False)
        asyncio.create_task(autodelete(msg, HELP_DELETE))


async def setup(bot):
    await bot.add_cog(Help(bot))

"""Tickets: typed panel, claim, close+HTML transcript, ratings, archive,
reopen, add-user, rename, stats, auto-close. All hybrid."""
import asyncio
import html
import io
import json
import re
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t, set_ctx_lang
from utils.embeds import WHITE, foot, ok
from utils.checks import staff_or, is_staff


def cfg(gid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM ticket_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        if not row:
            conn.execute('INSERT INTO ticket_cfg (guild_id) VALUES (?)', (str(gid),))
            row = conn.execute('SELECT * FROM ticket_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        return dict(row)


def support_roles(gid):
    c = cfg(gid)
    try:
        return json.loads(c.get('support_roles') or '[]')
    except Exception:
        return []


def is_support(member: discord.Member) -> bool:
    if is_staff(member):
        return True
    roles = support_roles(member.guild.id)
    return any(str(r.id) in roles for r in member.roles)


def clean_name(s: str) -> str:
    s = (s or '').lower().replace(' ', '-')
    return re.sub(r'[^a-z0-9-_]', '', s)[:90] or 'ticket'


def panel_layout(gid, types):
    """Components V2 ticket panel. Falls back to legacy view on any error."""
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow
    c = cfg(gid)
    layout = LayoutView(timeout=None)
    box = Container(accent_color=0xFFFFFF)
    box.add_item(TextDisplay(f"## {c.get('title') or 'Support'}\n{c.get('description') or t(gid, 'tix.pick')}"))
    row = ActionRow()
    if not types:
        row.add_item(discord.ui.Button(label=c.get('btn_label') or 'Open ticket',
                                       style=discord.ButtonStyle.grey, custom_id='tix_open_single'))
    else:
        opts = [discord.SelectOption(label=x['label'][:100], description=(x.get('description') or '')[:100],
                                     value=str(x['id'])) for x in types[:25]]
        row.add_item(_TypeSelect(t(gid, 'tix.pick'), opts))
    box.add_item(row)
    layout.add_item(box)
    return layout


class _TypeSelect(discord.ui.Select):
    # callback intentionally absent: on_interaction routes this (restart-proof,
    # single path — a view callback here would double-open tickets)
    def __init__(self, placeholder, options):
        super().__init__(custom_id='tix_open_select', placeholder=placeholder[:100],
                         options=options, min_values=1, max_values=1)


def control_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, cid in (('Claim', 'tix_claim'), ('Close', 'tix_close'),
                       ('Add', 'tix_add'), ('Transcript', 'tix_transcript')):
        view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.grey, custom_id=cid))
    return view


async def _safe_open(interaction: discord.Interaction, type_id=None):
    set_ctx_lang(interaction.user)
    """open_ticket wrapper: never let Discord see a silent timeout."""
    try:
        return await open_ticket(interaction, type_id)
    except Exception as e:
        print(f'[tickets] open failed: {e}')
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    t(interaction.guild_id, 'tix.open_fail'), ephemeral=True)
        except Exception:
            pass


async def open_ticket(interaction: discord.Interaction, type_id=None):
    set_ctx_lang(interaction.user)
    gid = interaction.guild_id
    c = cfg(gid)
    if not c.get('category_id'):
        return await interaction.response.send_message(t(gid, 'tix.no_panel'), ephemeral=True)
    with db.conn_ctx() as conn:
        n_open = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND owner_id=? AND closed=0',
                              (str(gid), str(interaction.user.id))).fetchone()['n']
        total = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND owner_id=?',
                             (str(gid), str(interaction.user.id))).fetchone()['n']
    if n_open >= (c.get('max_open') or 3):
        return await interaction.response.send_message(
            t(gid, 'tix.max_open', n=c.get('max_open') or 3), ephemeral=True)
    label = t(gid, 'tix.general')
    support_mention = []
    if type_id:
        with db.conn_ctx() as conn:
            ty = conn.execute('SELECT * FROM ticket_types WHERE id=? AND guild_id=?',
                              (type_id, str(gid))).fetchone()
        if ty:
            ty = dict(ty)
            label = ty['label']
            if ty.get('support_role'):
                support_mention.append(f"<@&{ty['support_role']}>")
    if not support_mention:
        support_mention = [f'<@&{r}>' for r in support_roles(gid)]
    name = (c.get('naming') or 'ticket-{user}-{n}').replace(
        '{user}', clean_name(interaction.user.name)).replace(
        '{n}', str(total + 1)).replace('{type}', clean_name(label))
    guild = interaction.guild
    category = guild.get_channel(int(c['category_id']))
    if not category:
        return await interaction.response.send_message(t(gid, 'tix.no_panel'), ephemeral=True)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                read_message_history=True)}
    for rid in support_roles(gid):
        role = guild.get_role(int(rid))
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True, manage_messages=True)
    if type_id:
        with db.conn_ctx() as conn:
            ty = conn.execute('SELECT support_role FROM ticket_types WHERE id=?', (type_id,)).fetchone()
            if ty and ty['support_role']:
                role = guild.get_role(int(ty['support_role']))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                   read_message_history=True)
    try:
        ch = await guild.create_text_channel(clean_name(name), category=category,
                                             overwrites=overwrites,
                                             topic=f'ticket {interaction.user.id} | {label}', reason='ticket open')
    except Exception:
        return await interaction.response.send_message(t(gid, 'info.post_fail'), ephemeral=True)
    now = int(time.time())
    with db.conn_ctx() as conn:
        conn.execute('''INSERT INTO tickets (channel_id, guild_id, owner_id, type_id, created_at, last_msg_at)
            VALUES (?,?,?,?,?,?)''', (str(ch.id), str(gid), str(interaction.user.id), type_id, now, now))
        # Double-click race: both passed the pre-check before either INSERTed.
        again = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND owner_id=? AND closed=0',
                             (str(gid), str(interaction.user.id))).fetchone()['n']
    if again > (c.get('max_open') or 3):
        try:
            await ch.delete(reason='ticket over limit (double open)')
        except Exception:
            pass
        return await interaction.response.send_message(
            t(gid, 'tix.max_open', n=c.get('max_open') or 3), ephemeral=True)
    greet = (c.get('greeting') or t(gid, 'tix.greet')).replace('{user}', interaction.user.mention).replace(
        '{type}', label).replace('{server}', guild.name)
    content = ' '.join(support_mention) if support_mention else None
    greet_emb = discord.Embed(title=f'{label} — {interaction.user.display_name}',
                              description=greet, color=WHITE)
    greet_emb.set_footer(text=foot())
    try:
        await ch.send(content=content, embed=greet_emb,
                      view=control_view(), allowed_mentions=discord.AllowedMentions.all())
    except Exception:
        pass
    await interaction.response.send_message(t(gid, 'tix.created', ch=ch.mention), ephemeral=True)


async def build_transcript(channel: discord.TextChannel):
    msgs = [m async for m in channel.history(limit=1000, oldest_first=True)]
    rows = []
    for m in msgs:
        body = html.escape(m.content or '')
        atts = ''.join(f"<div><a href='{html.escape(a.url)}'>{html.escape(a.filename)}</a></div>"
                       for a in m.attachments)
        av = str(m.author.display_avatar.url) if m.author.display_avatar else ''
        rows.append(
            f"<div class='m'><img src='{av}'/><div><b>{html.escape(m.author.display_name)}</b> "
            f"<span>{m.created_at.strftime('%Y-%m-%d %H:%M')}</span><p>{body or '<i>(embed/sticker)</i>'}</p>{atts}</div></div>")
    doc = ("<html><head><meta charset='utf-8'><style>body{background:#111;color:#eee;font-family:sans-serif}"
           ".m{display:flex;gap:10px;margin:12px}img{width:40px;height:40px;border-radius:50%}"
           "span{color:#888;font-size:12px}p{margin:4px 0}a{color:#9cf}</style></head><body><h2>#"
           + html.escape(channel.name) + "</h2>" + ''.join(rows) + '</body></html>')
    return doc, len(msgs)


async def finish_close(guild: discord.Guild, channel: discord.TextChannel, tl: dict, silent=False):
    gid = guild.id
    c = cfg(gid)
    owner = guild.get_member(int(tl['owner_id'])) if tl.get('owner_id') else None
    if c.get('log_channel'):
        dest = guild.get_channel(int(c['log_channel']))
        if dest:
            try:
                await dest.send(t(gid, 'tix.closed_log', ch=channel.name,
                                  user=owner.mention if owner else tl.get('owner_id', '?')))
            except Exception:
                pass
    with db.conn_ctx() as conn:
        conn.execute('UPDATE tickets SET closed=1 WHERE channel_id=?', (str(channel.id),))
    if owner:
        view = discord.ui.View(timeout=None)
        for n in range(1, 6):
            view.add_item(discord.ui.Button(label=str(n), style=discord.ButtonStyle.grey,
                                            custom_id=f'rate:{channel.id}:{n}'))
        try:
            await owner.send(t(gid, 'tix.rate_ask'), view=view)
        except Exception:
            pass
    if c.get('archive_id'):
        arch = guild.get_channel(int(c['archive_id']))
        if arch:
            try:
                await channel.edit(category=arch)
                if owner:
                    await channel.set_permissions(owner, send_messages=False)
                return
            except Exception:
                pass
    await asyncio.sleep(0 if silent else 5)
    try:
        await channel.delete(reason='ticket closed')
    except Exception:
        pass


class AddModal(discord.ui.Modal):
    def __init__(self, gid):
        super().__init__(title=t(gid, 'tix.add_title'))
        self.uid = discord.ui.TextInput(label=t(gid, 'tix.add_label'), max_length=40)
        self.add_item(self.uid)

    async def on_submit(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        gid = interaction.guild_id
        raw = self.uid.value.strip().strip('<@!>')
        member = interaction.guild.get_member(int(raw)) if raw.isdigit() else None
        if not member:
            return await interaction.response.send_message(t(gid, 'mod.no_member'), ephemeral=True)
        try:
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,
                                                      read_message_history=True)
        except Exception:
            pass
        await interaction.response.send_message(t(gid, 'tix.added', user=member.mention), ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sweeper.start()

    def cog_unload(self):
        self.sweeper.cancel()

    @tasks.loop(minutes=5)
    async def sweeper(self):
        await self.bot.wait_until_ready()
        now = int(time.time())
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM tickets WHERE closed=0').fetchall()]
        for tl in rows:
            c = cfg(tl['guild_id'])
            hours = c.get('auto_close_hours') or 0
            if not hours or now - (tl.get('last_msg_at') or tl.get('created_at') or now) < hours * 3600:
                continue
            guild = self.bot.get_guild(int(tl['guild_id']))
            ch = guild.get_channel(int(tl['channel_id'])) if guild else None
            if not ch:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE tickets SET closed=1 WHERE channel_id=?', (tl['channel_id'],))
                continue
            try:
                await ch.send(embed=ok(t(tl['guild_id'], 'tix.auto_closed')))
            except Exception:
                pass
            await finish_close(guild, ch, tl, silent=True)

    @sweeper.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    @db.main_guild_only
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT 1 FROM tickets WHERE channel_id=? AND closed=0',
                               (str(message.channel.id),)).fetchone()
            if row:
                conn.execute('UPDATE tickets SET last_msg_at=? WHERE channel_id=?',
                             (int(time.time()), str(message.channel.id)))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        gid = interaction.guild_id
        if interaction.type == discord.InteractionType.modal_submit:
            if (interaction.data.get('custom_id') or '') == 'tix_add_modal':
                return  # handled by AddModal callback
            return
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if cid == 'tix_open_single':
            return await _safe_open(interaction)
        if cid == 'tix_open_select':
            vals = interaction.data.get('values') or []
            if not vals:
                return
            return await _safe_open(interaction, int(vals[0]))
        if cid.startswith('rate:'):
            _, chid, n = cid.split(':')
            with db.conn_ctx() as conn:
                cur = conn.execute('SELECT rating FROM tickets WHERE channel_id=?', (chid,)).fetchone()
                if cur and cur['rating'] is not None:
                    # Already rated: last click must not flip stats forever.
                    try:
                        return await interaction.response.send_message(
                            t(gid, 'tix.rate_thanks', n=cur['rating']), ephemeral=True)
                    except Exception:
                        return
                conn.execute('UPDATE tickets SET rating=? WHERE channel_id=?', (int(n), chid))
            try:
                await interaction.response.send_message(t(gid, 'tix.rate_thanks', n=n), ephemeral=True)
            except Exception:
                pass
            return
        if not (cid.startswith('tix_claim') or cid.startswith('tix_close') or
                cid.startswith('tix_add') or cid.startswith('tix_transcript')):
            return
        # must be inside a ticket
        with db.conn_ctx() as conn:
            tl = conn.execute('SELECT * FROM tickets WHERE channel_id=? AND guild_id=?',
                              (str(interaction.channel_id), str(gid))).fetchone()
        if not tl:
            return await interaction.response.send_message(t(gid, 'tix.not_ticket'), ephemeral=True)
        tl = dict(tl)
        if tl.get('closed'):
            return await interaction.response.send_message(t(gid, 'tix.closed'), ephemeral=True)
        member = interaction.user
        if cid == 'tix_claim':
            if not is_support(member):
                return await interaction.response.send_message(t(gid, 'tix.no_perm'), ephemeral=True)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE tickets SET claimed_by=? WHERE channel_id=?',
                             (str(member.id), str(interaction.channel_id)))
            try:
                ch = interaction.channel
                base = re.sub(r'^claimed-[a-z0-9]+-', '', ch.name)
                await ch.edit(name=f"claimed-{clean_name(member.display_name)}-{base}"[:100],
                              reason=f'claimed by {member}')
            except Exception:
                pass
            # Resolve on the card: Claim goes dead so it can't be re-clicked.
            try:
                claimed_view = control_view()
                claimed_view.children[0].disabled = True
                await interaction.response.edit_message(view=claimed_view)
            except Exception:
                pass
            return await interaction.followup.send(t(gid, 'tix.claimed', user=member.mention))
        if cid == 'tix_close':
            owner = str(tl['owner_id']) == str(member.id)
            if not owner and not is_support(member):
                return await interaction.response.send_message(t(gid, 'tix.no_perm'), ephemeral=True)
            view = discord.ui.View(timeout=60)
            view.add_item(discord.ui.Button(label=t(gid, 'tix.yes'), style=discord.ButtonStyle.grey,
                                            custom_id='tix_close_yes'))
            view.add_item(discord.ui.Button(label=t(gid, 'tix.no'), style=discord.ButtonStyle.grey,
                                            custom_id='tix_close_no'))
            return await interaction.response.send_message(t(gid, 'tix.close_ask'), view=view, ephemeral=True)
        if cid == 'tix_close_yes':
            owner = str(tl['owner_id']) == str(member.id)
            if not owner and not is_support(member):
                return await interaction.response.send_message(t(gid, 'tix.no_perm'), ephemeral=True)
            try:
                await interaction.message.delete()
            except Exception:
                pass
            await interaction.response.send_message(t(gid, 'tix.closed'), ephemeral=True)
            await finish_close(interaction.guild, interaction.channel, tl)
            return
        if cid == 'tix_close_no':
            try:
                await interaction.message.delete()
            except Exception:
                pass
            return
        if cid == 'tix_add':
            if not is_support(member):
                return await interaction.response.send_message(t(gid, 'tix.no_perm'), ephemeral=True)
            modal = AddModal(gid)
            modal.custom_id = 'tix_add_modal'
            return await interaction.response.send_modal(modal)
        if cid == 'tix_transcript':
            if not is_support(member):
                return await interaction.response.send_message(t(gid, 'tix.no_perm'), ephemeral=True)
            doc, n = await build_transcript(interaction.channel)
            try:
                await interaction.response.send_message(
                    content=t(gid, 'tix.transcript', ch=interaction.channel.name),
                    file=discord.File(io.BytesIO(doc.encode('utf-8')),
                                      filename=f'transcript-{interaction.channel.name}.html'),
                    ephemeral=True)
            except Exception:
                pass
            return

    # ---------- setup commands ----------
    @commands.group(name='tickets', description='Tickety')
    async def tickets(self, ctx):
        await ctx.reply('.tickets setup / config / panel / type-add / type-remove / types / staff-add / staff-remove / rename / reopen / stats',
                        ephemeral=True)

    @tickets.command(name='setup', description='Nowa konfiguracja')
    @staff_or('manage_guild')
    async def setup_t(self, ctx, panel_channel: discord.TextChannel, category: discord.CategoryChannel,
                      support: discord.Role, log_channel: discord.TextChannel = None,
                      archive: discord.CategoryChannel = None):
        gid = ctx.guild.id
        cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('''UPDATE ticket_cfg SET panel_channel=?, category_id=?, archive_id=?,
                log_channel=?, support_roles=? WHERE guild_id=?''',
                         (str(panel_channel.id), str(category.id),
                          str(archive.id) if archive else None,
                          str(log_channel.id) if log_channel else None,
                          json.dumps([str(support.id)]), str(gid)))
        await ctx.reply(t(gid, 'tix.setup_ok', ch=panel_channel.mention), ephemeral=True)

    @tickets.command(name='quicksetup', description='Szybki setup')
    @staff_or('manage_guild')
    async def quicksetup(self, ctx, panel_channel: discord.TextChannel, category: discord.CategoryChannel,
                         support: discord.Role, log_channel: discord.TextChannel = None):
        gid = ctx.guild.id
        cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('''UPDATE ticket_cfg SET panel_channel=?, category_id=?,
                log_channel=?, support_roles=? WHERE guild_id=?''',
                         (str(panel_channel.id), str(category.id),
                          str(log_channel.id) if log_channel else str(panel_channel.id),
                          json.dumps([str(support.id)]), str(gid)))
        await self.panel.callback(self, ctx)

    @tickets.command(name='config', description='Limity, nazwy, teksty')
    @staff_or('manage_guild')
    async def config(self, ctx, max_open: int = None, auto_close_hours: int = None,
                     naming: str = None, greeting: str = None, button: str = None,
                     title: str = None, description: str = None):
        gid = ctx.guild.id
        c = cfg(gid)
        with db.conn_ctx() as conn:
            conn.execute('''UPDATE ticket_cfg SET max_open=COALESCE(?,max_open),
                auto_close_hours=COALESCE(?,auto_close_hours), naming=COALESCE(?,naming),
                greeting=COALESCE(?,greeting), btn_label=COALESCE(?,btn_label),
                title=COALESCE(?,title), description=COALESCE(?,description) WHERE guild_id=?''',
                         (max_open, auto_close_hours, naming,
                          greeting if greeting else (None if greeting is None else ''),
                          button, title, description, str(gid)))
        await ctx.reply(t(gid, 'tix.cfg_ok'), ephemeral=True)

    @tickets.command(name='staff-add', description='Dodaj support')
    @staff_or('manage_guild')
    async def staff_add(self, ctx, role: discord.Role):
        gid = ctx.guild.id
        roles = set(support_roles(gid)) | {str(role.id)}
        with db.conn_ctx() as conn:
            conn.execute('UPDATE ticket_cfg SET support_roles=? WHERE guild_id=?',
                         (json.dumps(sorted(roles)), str(gid)))
        await ctx.reply(t(gid, 'tix.staff_added', role=role.mention), ephemeral=True)

    @tickets.command(name='staff-remove', description='UsuÅ„ support')
    @staff_or('manage_guild')
    async def staff_remove(self, ctx, role: discord.Role):
        gid = ctx.guild.id
        roles = set(support_roles(gid)) - {str(role.id)}
        with db.conn_ctx() as conn:
            conn.execute('UPDATE ticket_cfg SET support_roles=? WHERE guild_id=?',
                         (json.dumps(sorted(roles)), str(gid)))
        await ctx.reply(t(gid, 'tix.staff_removed', role=role.mention), ephemeral=True)

    @tickets.command(name='type-add', description='Dodaj typ')
    @staff_or('manage_guild')
    async def type_add(self, ctx, label: str, role: discord.Role = None, *, description: str = ''):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO ticket_types (guild_id, label, description, support_role) VALUES (?,?,?,?)',
                         (str(gid), label, description, str(role.id) if role else None))
        await ctx.reply(t(gid, 'tix.type_added', label=label), ephemeral=True)

    @tickets.command(name='type-remove', description='UsuÅ„ typ')
    @staff_or('manage_guild')
    async def type_remove(self, ctx, label: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM ticket_types WHERE guild_id=? AND label=?', (str(gid), label))
        await ctx.reply(t(gid, 'tix.type_removed', label=label), ephemeral=True)

    @tickets.command(name='types', description='Lista typów')
    async def types(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM ticket_types WHERE guild_id=?', (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'tix.type_empty'), ephemeral=True)
        await ctx.reply(embed=ok(t(gid, 'tix.type_title') + '\n' + '\n'.join(
            f"• **{r['label']}**" + (f" — {r['description']}" if r['description'] else '') +
            (f" (<@&{r['support_role']}>) " if r['support_role'] else '') for r in rows)), ephemeral=True)

    @tickets.command(name='panel', description='Postaw panel')
    @staff_or('manage_guild')
    async def panel(self, ctx):
        gid = ctx.guild.id
        c = cfg(gid)
        ch = ctx.guild.get_channel(int(c['panel_channel'])) if c.get('panel_channel') else None
        if not ch:
            return await ctx.reply(t(gid, 'tix.no_panel'), ephemeral=True)
        # drop old panel
        if c.get('panel_message'):
            try:
                await (await ch.fetch_message(int(c['panel_message']))).delete()
            except Exception:
                pass
        with db.conn_ctx() as conn:
            types = [dict(r) for r in conn.execute(
                'SELECT * FROM ticket_types WHERE guild_id=? ORDER BY id ASC', (str(gid),)).fetchall()]
        try:
            msg = await ch.send(view=panel_layout(gid, types))
        except Exception:
            return await ctx.reply(t(gid, 'info.post_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE ticket_cfg SET panel_message=? WHERE guild_id=?', (str(msg.id), str(gid)))
        await ctx.reply(t(gid, 'tix.panel_ok', ch=ch.mention), ephemeral=True)

    @tickets.command(name='rename', description='ZmieÅ„ nazwÄ™')
    @staff_or('manage_guild')
    async def rename(self, ctx, *, name: str):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            tl = conn.execute('SELECT * FROM tickets WHERE channel_id=? AND guild_id=? AND closed=0',
                              (str(ctx.channel.id), str(gid))).fetchone()
        if not tl:
            return await ctx.reply(t(gid, 'tix.not_ticket'), ephemeral=True)
        try:
            await ctx.channel.edit(name=clean_name(name))
        except Exception:
            pass
        await ctx.reply(t(gid, 'tix.rename_ok', name=clean_name(name)), ephemeral=True)

    @tickets.command(name='reopen', description='Otwórz z powrotem')
    @staff_or('manage_guild')
    async def reopen(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            tl = conn.execute('SELECT * FROM tickets WHERE channel_id=? AND guild_id=? AND closed=1',
                              (str(ctx.channel.id), str(gid))).fetchone()
        if not tl:
            return await ctx.reply(t(gid, 'tix.not_ticket'), ephemeral=True)
        c = cfg(gid)
        try:
            cat = ctx.guild.get_channel(int(c['category_id'])) if c.get('category_id') else None
            if cat:
                await ctx.channel.edit(category=cat)
            owner = ctx.guild.get_member(int(tl['owner_id']))
            if owner:
                await ctx.channel.set_permissions(owner, send_messages=True)
        except Exception:
            pass
        with db.conn_ctx() as conn:
            conn.execute('UPDATE tickets SET closed=0, last_msg_at=? WHERE channel_id=?',
                         (int(time.time()), str(ctx.channel.id)))
        await ctx.reply(embed=ok(t(gid, 'tix.reopened')))

    @tickets.command(name='stats', description='Staty ticketów')
    async def stats(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            open_n = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND closed=0',
                                  (str(gid),)).fetchone()['n']
            total = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=?', (str(gid),)).fetchone()['n']
            avg = conn.execute('SELECT AVG(rating) a FROM tickets WHERE guild_id=? AND rating IS NOT NULL',
                               (str(gid),)).fetchone()['a']
            per = conn.execute('''SELECT COALESCE(tt.label, ?) l, COUNT(*) n FROM tickets t
                LEFT JOIN ticket_types tt ON tt.id = t.type_id WHERE t.guild_id=? GROUP BY l''',
                               (t(gid, 'tix.general'), str(gid))).fetchall()
        await ctx.reply(embed=ok(t(gid, 'tix.stats_title') + '\n' + t(
            gid, 'tix.stats_body', open=open_n, total=total,
            avg=round(avg, 1) if avg else '—',
            types='\n'.join(f"• {r['l']}: **{r['n']}**" for r in per) or '—')), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))

"""Voice Master: join-to-create rooms, panel in room chat. Hybrid setup."""
import re

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, ok, WHITE, GREEN
from utils.checks import staff_or

GREEN_STYLE = discord.ButtonStyle.green
GREY_STYLE = discord.ButtonStyle.grey
RED_STYLE = discord.ButtonStyle.red


def panel_embed(channel, owner_mention: str, gid, thumb_url: str = None) -> discord.Embed:
    ow = channel.overwrites_for(channel.guild.default_role)
    states = []
    if ow.connect is False:
        states.append(t(gid, 'vm.st_locked'))
    else:
        states.append(t(gid, 'vm.st_open'))
    if ow.view_channel is False:
        states.append(t(gid, 'vm.st_hidden'))
    limit = channel.user_limit or 0
    states.append(t(gid, 'vm.st_limit', cur=limit, max=99) if limit else t(gid, 'vm.st_nolimit'))
    humans = len([m for m in channel.members if not m.bot])
    states.append(t(gid, 'vm.st_inside', n=humans))
    e = build(f"{owner_mention}\n{' Â· '.join(states)}",
              title=channel.name, color=WHITE)
    try:
        with db.conn_ctx() as conn:
            vmr = conn.execute('SELECT interface_channel_id FROM voicemaster WHERE guild_id=?',
                               (str(channel.guild.id),)).fetchone()
        if vmr and vmr['interface_channel_id']:
            e.description = (e.description or '') + '\n' + t(
                channel.guild.id, 'vm.iface_hint', ch=f"<#{vmr['interface_channel_id']}>")
    except Exception:
        pass
    if thumb_url:
        try:
            e.set_thumbnail(url=thumb_url)
        except Exception:
            pass
    return e


async def refresh_panel(guild: discord.Guild, channel) -> None:
    """Re-render the room panel with live state. Silent on failure."""
    try:
        with db.conn_ctx() as conn:
            rec = conn.execute('SELECT * FROM temp_vcs WHERE channel_id=?', (str(channel.id),)).fetchone()
            if not rec or not rec['panel_id']:
                return
            panel_id = rec['panel_id']
            owner_id = rec['owner_id']
        owner = guild.get_member(int(owner_id))
        thumb = str(guild.icon.with_size(128).url) if guild.icon else None
        msg = await channel.fetch_message(int(panel_id))
        await msg.edit(embed=panel_embed(channel, owner.mention if owner else f'<@{owner_id}>',
                                         guild.id, thumb),
                       view=None)  # room stays clean; controls live on the interface
    except Exception:
        pass


def control_grid(channel_id, gid) -> discord.ui.View:
    """Private control grid — shown ephemerally, never in public."""
    cid = str(channel_id)
    view = discord.ui.View(timeout=300)
    for key in ('lock', 'hide', 'rename', 'plus', 'minus', 'kick', 'trust', 'delete'):
        label = {'lock': t(gid, 'vm.b_lock'), 'hide': t(gid, 'vm.b_hide'), 'rename': t(gid, 'vm.b_rename'),
                 'plus': '+', 'minus': '−', 'kick': t(gid, 'vm.b_kick'), 'trust': t(gid, 'vm.b_trust'),
                 'delete': t(gid, 'vm.b_delete')}[key]
        short = {'lock': 'lock', 'hide': 'hide', 'rename': 'rename', 'plus': 'lplus', 'minus': 'lminus',
                 'kick': 'kick', 'trust': 'trust', 'delete': 'delete'}[key]
        view.add_item(discord.ui.Button(label=label, style=GREY_STYLE, custom_id=f'vm_{short}:{cid}'))
    return view


ICON_KEYS = ('lock', 'hide', 'rename', 'plus', 'minus', 'kick', 'trust', 'delete')


def get_icons(gid) -> dict:
    with db.conn_ctx() as conn:
        return {r['key']: r['emoji'] for r in conn.execute(
            'SELECT key, emoji FROM vm_icons WHERE guild_id=?', (str(gid),)).fetchall()}


def _vmi_button(guild: discord.Guild, icons: dict, labels: dict, key: str):
    em = icons.get(key)
    if em:
        try:
            from discord import PartialEmoji
            return discord.ui.Button(emoji=PartialEmoji.from_str(em), style=GREY_STYLE,
                                     custom_id=f'vmi_{key}')
        except Exception:
            pass
    return discord.ui.Button(label=labels.get(key, key), style=GREY_STYLE, custom_id=f'vmi_{key}')


def interface_view(guild: discord.Guild) -> discord.ui.View:
    """Static jar-style panel. Icons when configured, grey labels otherwise."""
    icons = get_icons(guild.id)
    labels = {'lock': t(guild.id, 'vm.b_lock'), 'hide': t(guild.id, 'vm.b_hide'),
              'rename': t(guild.id, 'vm.b_rename'), 'plus': '+', 'minus': '−',
              'kick': t(guild.id, 'vm.b_kick'), 'trust': t(guild.id, 'vm.b_trust'),
              'delete': t(guild.id, 'vm.b_delete')}
    view = discord.ui.View(timeout=None)
    for key in ICON_KEYS:
        view.add_item(_vmi_button(guild, icons, labels, key))
    return view


def interface_layout(guild: discord.Guild):
    """Components V2 panel: container, inline thumbnail, grouped buttons."""
    from discord.ui import (LayoutView, Container, TextDisplay, Section,
                            Thumbnail, Separator, ActionRow)
    gid = guild.id
    icons = get_icons(gid)
    labels = {'lock': t(gid, 'vm.b_lock'), 'hide': t(gid, 'vm.b_hide'),
              'rename': t(gid, 'vm.b_rename'), 'plus': '+', 'minus': '−',
              'kick': t(gid, 'vm.b_kick'), 'trust': t(gid, 'vm.b_trust'),
              'delete': t(gid, 'vm.b_delete'), 'party': t(gid, 'vm.b_party')}
    view = LayoutView(timeout=None)
    box = Container(accent_color=0xFFFFFF)
    head = TextDisplay(f'## {t(gid, "vm.iface_title")}\n{t(gid, "vm.iface_desc")}')
    try:
        thumb = Thumbnail(str(guild.icon.with_size(128).url)) if guild.icon else None
    except Exception:
        thumb = None
    box.add_item(Section(head, accessory=thumb) if thumb else head)
    box.add_item(Separator(visible=True))
    box.add_item(TextDisplay(t(gid, 'vm.iface_hint2')))
    row1 = ActionRow()
    for key in ('lock', 'hide', 'rename', 'plus', 'minus'):
        row1.add_item(_vmi_button(guild, icons, labels, key))
    row2 = ActionRow()
    for key in ('kick', 'trust', 'delete', 'party'):
        row2.add_item(_vmi_button(guild, icons, labels, key))
    box.add_item(row1)
    box.add_item(row2)
    view.add_item(box)
    return view


class RenameModal(discord.ui.Modal, title='Rename'):
    def __init__(self, channel_id: int, gid):
        super().__init__(title=t(gid, 'vm.rename_title'))
        self.channel_id = channel_id
        self.name_input = discord.ui.TextInput(label=t(gid, 'vm.rename_label'), max_length=100)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        ch = interaction.guild.get_channel(self.channel_id)
        if not ch:
            return await interaction.response.send_message(t(gid, 'vm.vc_gone2'), ephemeral=True)
        if not _owns_channel(interaction.guild, interaction.user.id, self.channel_id):
            return await interaction.response.send_message(t(gid, 'vm.no_owner'), ephemeral=True)
        try:
            await ch.edit(name=self.name_input.value[:100])
        except Exception:
            pass
        await refresh_panel(interaction.guild, ch)
        await interaction.response.send_message(t(gid, 'vm.renamed', name=self.name_input.value[:100]), ephemeral=True)


class TrustModal(discord.ui.Modal, title='Trust'):
    def __init__(self, channel_id: int, gid):
        super().__init__(title=t(gid, 'vm.trust_title'))
        self.channel_id = channel_id
        self.uid = discord.ui.TextInput(label=t(gid, 'vm.trust_label'), max_length=40,
                                        placeholder='@user / user ID')
        self.add_item(self.uid)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        ch = interaction.guild.get_channel(self.channel_id)
        if not ch:
            return await interaction.response.send_message(t(gid, 'vm.vc_gone2'), ephemeral=True)
        if not _owns_channel(interaction.guild, interaction.user.id, self.channel_id):
            return await interaction.response.send_message(t(gid, 'vm.no_owner'), ephemeral=True)
        raw = self.uid.value.strip()
        target = None
        digits = ''.join(c for c in raw if c.isdigit())
        if digits:
            target = interaction.guild.get_member(int(digits))
        if not target:
            low = raw.lstrip('@').lower()
            for m in interaction.guild.members:
                if m.display_name.lower() == low or m.name.lower() == low:
                    target = m
                    break
        if not target:
            return await interaction.response.send_message(t(gid, 'mod.no_member'), ephemeral=True)
        try:
            await ch.set_permissions(target, connect=True, view_channel=True, speak=True)
            # a trusted user must fit: bump the limit when the room is full
            humans = len([m for m in ch.members if not m.bot])
            if (ch.user_limit or 0) > 0 and humans >= ch.user_limit:
                await ch.edit(user_limit=min(99, ch.user_limit + 1))
        except Exception:
            pass
        await refresh_panel(interaction.guild, ch)
        await interaction.response.send_message(t(gid, 'vm.trusted', user=target.mention), ephemeral=True)


def _is_owner(interaction: discord.Interaction, channel_id: int):
    """Only the room owner (or a server Administrator) may touch the panel."""
    gid = interaction.guild_id
    with db.conn_ctx() as conn:
        rec = conn.execute('SELECT * FROM temp_vcs WHERE channel_id=?', (str(channel_id),)).fetchone()
    if not rec:
        return None, t(gid, 'vm.not_temp3')
    if str(rec['owner_id']) != str(interaction.user.id):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.guild_permissions.administrator:
            return None, t(gid, 'vm.no_owner')
    return dict(rec), None


def _owns_channel(guild: discord.Guild, user_id: int, channel_id) -> bool:
    with db.conn_ctx() as conn:
        rec = conn.execute('SELECT owner_id FROM temp_vcs WHERE channel_id=?', (str(channel_id),)).fetchone()
    if not rec:
        return False
    if str(rec['owner_id']) == str(user_id):
        return True
    member = guild.get_member(int(user_id))
    return bool(member and member.guild_permissions.administrator)


class Voice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- join-to-create + cleanup ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        # vault: only the house stays inside, everyone else gets yanked
        try:
            if after.channel and not member.bot and not db.is_house(member.id):
                with db.conn_ctx() as conn:
                    v = conn.execute('SELECT 1 FROM voice_vaults WHERE guild_id=? AND channel_id=?',
                                     (str(guild.id), str(after.channel.id))).fetchone()
                if v:
                    try:
                        await member.move_to(None, reason='vault')
                    except Exception:
                        pass
                    return
        except Exception:
            pass
        with db.conn_ctx() as conn:
            vm = conn.execute('SELECT * FROM voicemaster WHERE guild_id=?', (str(guild.id),)).fetchone()
        if vm and after.channel and str(after.channel.id) == str(vm['master_channel_id']):
            name = (vm['default_name'] or "{user}'s pv").replace('{user}', member.name)
            try:
                temp = await guild.create_voice_channel(
                    name, category=after.channel.category,
                    user_limit=vm['default_limit'] or 0, reason='voice master')
                await temp.set_permissions(guild.default_role, view_channel=True, connect=True)
                await temp.set_permissions(member, manage_channels=True, mute_members=True,
                                           deafen_members=True, move_members=True)
                with db.conn_ctx() as conn:
                    conn.execute('INSERT OR REPLACE INTO temp_vcs (channel_id, guild_id, owner_id) VALUES (?,?,?)',
                                 (str(temp.id), str(guild.id), str(member.id)))
                await member.move_to(temp)
                thumb = str(guild.icon.with_size(128).url) if guild.icon else None
                panel_msg = await temp.send(embed=panel_embed(temp, member.mention, guild.id, thumb))
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE temp_vcs SET panel_id=? WHERE channel_id=?',
                                 (str(panel_msg.id), str(temp.id)))
            except Exception as e:
                print(f'[voice] create failed: {e}')
        if before.channel:
            with db.conn_ctx() as conn:
                rec = conn.execute('SELECT * FROM temp_vcs WHERE channel_id=?', (str(before.channel.id),)).fetchone()
            if rec:
                ch = guild.get_channel(before.channel.id)
                if ch and len(ch.members) == 0:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
                    with db.conn_ctx() as conn:
                        conn.execute('DELETE FROM temp_vcs WHERE channel_id=?', (str(before.channel.id),))

    # ---------- button/select handling (restart-proof via listener) ----------
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.modal_submit and \
                (interaction.data.get('custom_id') or '').startswith('vm_rename_modal:'):
            return  # handled by modal callback
        cid_raw = ''
        if interaction.type == discord.InteractionType.component:
            cid_raw = interaction.data.get('custom_id', '')
            # panel dropdown routes into the same handlers as the old buttons
            if cid_raw.startswith('vm_manage:'):
                picked = (interaction.data.get('values') or [''])[0]
                if picked not in ('lock', 'hide', 'rename', 'lplus', 'lminus', 'kick', 'trust', 'delete'):
                    return
                cid_raw = f'vm_{picked}:' + cid_raw.split(':')[1]
            elif interaction.data.get('component_type') != 2:
                return  # selects have their own callbacks
        if not cid_raw.startswith('vm_') and not cid_raw.startswith('controls:') \
                and not cid_raw.startswith('vmi_'):
            return
        gid = interaction.guild_id
        # static interface: resolve the clicker's OWN room, then run the same handlers
        if cid_raw.startswith('vmi_'):
            action = cid_raw[4:]
            if action not in ('lock', 'hide', 'rename', 'plus', 'minus', 'kick', 'trust', 'delete', 'party'):
                return
            mine = None
            with db.conn_ctx() as conn:
                rows = conn.execute('SELECT channel_id FROM temp_vcs WHERE guild_id=? AND owner_id=?',
                                    (str(gid), str(interaction.user.id))).fetchall()
            for r in rows:
                ch = interaction.guild.get_channel(int(r['channel_id']))
                if ch:
                    mine = ch
                    break
            if not mine:
                return await interaction.response.send_message(t(gid, 'vm.no_room'), ephemeral=True)
            if action == 'party':
                try:
                    await mine.edit(user_limit=0, reason=f'party by {interaction.user}')
                except Exception:
                    return await interaction.response.send_message(t(gid, 'vm.fail'), ephemeral=True)
                return await interaction.response.send_message(t(gid, 'vm.party', ch=mine.mention),
                                                               ephemeral=True)
            cid_raw = f"vm_{'lplus' if action == 'plus' else 'lminus' if action == 'minus' else action}:{mine.id}"
        # the one public tap -> private grid (owner only)
        if cid_raw.startswith('controls:'):
            arg = cid_raw.split(':')[1]
            channel = interaction.guild.get_channel(int(arg)) if arg.isdigit() else None
            if not channel:
                return await interaction.response.send_message(t(gid, 'vm.vc_gone'), ephemeral=True)
            rec, problem = _is_owner(interaction, arg)
            if problem:
                return await interaction.response.send_message(problem, ephemeral=True)
            thumb = str(interaction.guild.icon.with_size(128).url) if interaction.guild.icon else None
            return await interaction.response.send_message(
                embed=panel_embed(channel, interaction.user.mention, gid, thumb),
                view=control_grid(channel.id), ephemeral=True)
        if cid_raw.startswith('vm_rename_modal:'):
            return
        parts = cid_raw.split(':')
        ns, arg = parts[0], parts[1] if len(parts) > 1 else ''
        channel = interaction.guild.get_channel(int(arg)) if arg.isdigit() else None

        if ns == 'vm_claim':
            with db.conn_ctx() as conn:
                rec = conn.execute('SELECT * FROM temp_vcs WHERE channel_id=?', (arg,)).fetchone()
            if not rec:
                return await interaction.response.send_message(t(gid, 'vm.not_temp3'), ephemeral=True)
            if channel and any(str(m.id) == str(rec['owner_id']) for m in channel.members):
                return await interaction.response.send_message(t(gid, 'vm.claim_owner'), ephemeral=True)
            if not channel or interaction.user not in channel.members:
                return await interaction.response.send_message(t(gid, 'vm.claim_join'), ephemeral=True)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE temp_vcs SET owner_id=? WHERE channel_id=?', (str(interaction.user.id), arg))
            try:
                await channel.set_permissions(interaction.user, manage_channels=True, mute_members=True,
                                              deafen_members=True, move_members=True)
            except Exception:
                pass
            return await interaction.response.send_message(t(gid, 'vm.claimed', name=channel.name), ephemeral=True)
            # (claim button retired from panel; handler kept for old panels)

        if not channel or not channel.members and ns != 'vm_delete':
            pass
        if not channel:
            return await interaction.response.send_message(t(gid, 'vm.vc_gone'), ephemeral=True)
        rec, problem = _is_owner(interaction, arg)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)

        if ns == 'vm_lock':
            locked = channel.overwrites_for(interaction.guild.default_role).connect is False
            try:
                await channel.set_permissions(interaction.guild.default_role,
                                              connect=None if locked else False)
            except Exception:
                pass
            await refresh_panel(interaction.guild, channel)
            return await interaction.response.send_message(
                t(gid, 'vm.unlocked') if locked else t(gid, 'vm.locked'), ephemeral=True)
        if ns == 'vm_hide':
            hidden = channel.overwrites_for(interaction.guild.default_role).view_channel is False
            try:
                await channel.set_permissions(interaction.guild.default_role,
                                              view_channel=None if hidden else False)
            except Exception:
                pass
            await refresh_panel(interaction.guild, channel)
            return await interaction.response.send_message(
                t(gid, 'vm.visible') if hidden else t(gid, 'vm.hidden'), ephemeral=True)
        if ns == 'vm_rename':
            return await interaction.response.send_modal(RenameModal(channel.id, gid))
        if ns in ('vm_lplus', 'vm_lminus'):
            cur = channel.user_limit or 0
            new = min(99, cur + 1) if ns == 'vm_lplus' else max(0, cur - 1)
            try:
                await channel.edit(user_limit=new)
            except Exception:
                pass
            await refresh_panel(interaction.guild, channel)
            return await interaction.response.send_message(
                t(gid, 'vm.limit_set', v=t(gid, 'vm.limit_unlimited') if new == 0 else new), ephemeral=True)
        if ns == 'vm_kick':
            cands = [m for m in channel.members if m.id != interaction.user.id and not m.bot][:25]
            if not cands:
                return await interaction.response.send_message(t(gid, 'vm.no_kick'), ephemeral=True)
            view = discord.ui.View(timeout=60)
            view.add_item(_MemberSelect(f'vm_kick_select:{arg}', t(gid, 'vm.kick_who'), cands))
            return await interaction.response.send_message(t(gid, 'vm.kick_who'), view=view, ephemeral=True)
        if ns == 'vm_trust':
            return await interaction.response.send_modal(TrustModal(arg, gid))
        if ns == 'vm_delete':
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM temp_vcs WHERE channel_id=?', (arg,))
            try:
                await channel.delete()
            except Exception:
                pass
            try:
                await interaction.response.send_message(t(gid, 'vm.deleted'), ephemeral=True)
            except Exception:
                pass
            return

    # ---------- commands ----------
    async def _post_interface(self, guild: discord.Guild, channel: discord.TextChannel):
        with db.conn_ctx() as conn:
            old = conn.execute('SELECT interface_message_id FROM voicemaster WHERE guild_id=?',
                               (str(guild.id),)).fetchone()
            if old and old['interface_message_id']:
                try:
                    await (await channel.fetch_message(int(old['interface_message_id']))).delete()
                except Exception:
                    pass
            try:
                msg = await channel.send(view=interface_layout(guild))
            except Exception:
                return None
            conn.execute('UPDATE voicemaster SET interface_channel_id=?, interface_message_id=? WHERE guild_id=?',
                         (str(channel.id), str(msg.id), str(guild.id)))
            return msg

    def _my_room(self, guild: discord.Guild, user_id) -> discord.VoiceChannel | None:
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT channel_id FROM temp_vcs WHERE guild_id=? AND owner_id=?',
                                (str(guild.id), str(user_id))).fetchall()
        for r in rows:
            ch = guild.get_channel(int(r['channel_id']))
            if ch:
                return ch
        return None

    @commands.command(name='party', description='Tryb imprezy: twój pokój bez limitu')
    async def party(self, ctx):
        gid = ctx.guild.id
        room = self._my_room(ctx.guild, ctx.author.id)
        if not room:
            return await ctx.reply(t(gid, 'vm.no_room'), ephemeral=True)
        try:
            await room.edit(user_limit=0, reason=f'party by {ctx.author}')
        except Exception:
            return await ctx.reply(t(gid, 'vm.fail'), ephemeral=True)
        await ctx.reply(t(gid, 'vm.party', ch=room.mention))

    @commands.command(name='bring', description='ÅšciÄ…gnij rolÄ™ na swojÄ… gÅ‚osówkÄ™')
    @staff_or('move_members')
    async def bring(self, ctx, role: discord.Role):
        gid = ctx.guild.id
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.reply(t(gid, 'vm.bring_join'), ephemeral=True)
        dest = ctx.author.voice.channel
        moved, failed = 0, 0
        for m in role.members:
            if m.bot or not m.voice or not m.voice.channel or m.voice.channel.id == dest.id:
                continue
            try:
                await m.move_to(dest, reason=f'bring by {ctx.author}')
                moved += 1
            except Exception:
                failed += 1
        await ctx.reply(t(gid, 'vm.bring_done', n=moved, ch=dest.mention,
                           f=t(gid, 'vm.bring_fail', n=failed) if failed else ''))

    @commands.command(name='v', aliases=['vault'])
    async def vault_cmd(self, ctx):
        gid = ctx.guild.id
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
        vc = ctx.author.voice.channel if ctx.author.voice else None
        if not vc:
            return await ctx.reply(t(gid, 'vm.bring_join'), ephemeral=True)
        try:
            await vc.set_permissions(ctx.guild.default_role, connect=False, view_channel=False)
            for m in list(vc.members):
                if not m.bot and not db.is_house(m.id):
                    try:
                        await m.move_to(None, reason='vault')
                    except Exception:
                        pass
        except Exception:
            return await ctx.reply(t(gid, 'vm.fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO voice_vaults (guild_id, channel_id) VALUES (?,?)',
                         (str(gid), str(vc.id)))
        await ctx.reply(t(gid, 'vm.vault_on', ch=vc.mention))

    @commands.command(name='uv', aliases=['unvault'])
    async def unvault_cmd(self, ctx):
        gid = ctx.guild.id
        if not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'eco.no_owner'), ephemeral=True)
        vc = ctx.author.voice.channel if ctx.author.voice else None
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT channel_id FROM voice_vaults WHERE guild_id=?', (str(gid),)).fetchone()
            conn.execute('DELETE FROM voice_vaults WHERE guild_id=?', (str(gid),))
        ch = vc or (ctx.guild.get_channel(int(row['channel_id'])) if row and row['channel_id'] else None)
        if ch:
            try:
                await ch.set_permissions(ctx.guild.default_role, connect=None, view_channel=None)
            except Exception:
                pass
        await ctx.reply(t(gid, 'vm.vault_off'))

    @commands.command(name='vcpanel', description='Panel pokoju')
    async def vcpanel(self, ctx):
        vc = ctx.author.voice.channel if ctx.author.voice else None
        if not vc:
            return await ctx.reply(t(ctx.guild.id, 'vm.no_vc'), ephemeral=True)
        with db.conn_ctx() as conn:
            rec = conn.execute('SELECT * FROM temp_vcs WHERE channel_id=?', (str(vc.id),)).fetchone()
        if not rec:
            return await ctx.reply(t(ctx.guild.id, 'vm.not_temp'), ephemeral=True)
        if str(rec['owner_id']) != str(ctx.author.id):
            member = ctx.guild.get_member(ctx.author.id)
            if not member or not member.guild_permissions.administrator:
                return await ctx.reply(t(ctx.guild.id, 'vm.no_owner'), ephemeral=True)
        thumb = str(ctx.guild.icon.with_size(128).url) if ctx.guild.icon else None
        embed = panel_embed(vc, ctx.author.mention, ctx.guild.id, thumb)
        # slash -> ephemeral grid (private); prefix -> self-deleting grid
        if ctx.interaction:
            await ctx.reply(embed=embed, view=control_grid(vc.id), ephemeral=True)
        else:
            msg = await ctx.channel.send(embed=embed, view=control_grid(vc.id))
            await msg.delete(delay=120)
            try:
                await ctx.message.delete()
            except Exception:
                pass

    @commands.command(name='vcs', description='Voice za jednym zamachem')
    @staff_or('manage_guild')
    async def vcs(self, ctx, lobby_name: str, interface_name: str = 'interface'):
        """`.vcs Stworz-Kanal interface` — creates both channels, wires everything, done.
        `.vcs off` disables voice rooms."""
        gid = ctx.guild.id
        if lobby_name.strip().lower() == 'off':
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM voicemaster WHERE guild_id=?', (str(gid),))
            return await ctx.reply(t(gid, 'vm.off'), ephemeral=True)
        gid = ctx.guild.id
        lobby_name = lobby_name.strip()[:90]
        slug = re.sub(r'[^a-z0-9-_]', '', interface_name.strip().lower().replace(' ', '-'))[:90] or 'interface'
        if not lobby_name:
            return await ctx.reply(t(gid, 'vm.vcs_use'), ephemeral=True)
        lobby = discord.utils.get(ctx.guild.voice_channels, name=lobby_name)
        if not lobby:
            try:
                lobby = await ctx.guild.create_voice_channel(lobby_name, reason='voice master lobby')
            except Exception:
                return await ctx.reply(t(gid, 'vm.vcs_fail'), ephemeral=True)
        iface = discord.utils.get(ctx.guild.text_channels, name=slug)
        if not iface:
            try:
                iface = await ctx.guild.create_text_channel(slug, reason='voice master interface')
            except Exception:
                return await ctx.reply(t(gid, 'vm.vcs_fail'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('''INSERT INTO voicemaster (guild_id, master_channel_id, default_name)
                VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET master_channel_id=excluded.master_channel_id''',
                         (str(gid), str(lobby.id), "{user}'s pv"))
        await self._post_interface(ctx.guild, iface)
        await ctx.reply(t(gid, 'vm.vcs_ok', lobby=lobby.mention, iface=iface.mention), ephemeral=True)



class _LimitSelect(discord.ui.Select):
    def __init__(self, channel_id: int, options):
        super().__init__(custom_id=f'vm_limit_select:{channel_id}', placeholder=options[0].label if options else '…',
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        ch = interaction.guild.get_channel(int(self.custom_id.split(':')[1]))
        if not ch:
            return await interaction.response.send_message(t(gid, 'vm.vc_gone2'), ephemeral=True)
        try:
            await ch.edit(user_limit=int(self.values[0]))
        except Exception:
            pass
        v = self.values[0]
        await interaction.response.send_message(
            t(gid, 'vm.limit_set', v=t(gid, 'vm.limit_unlimited') if v == '0' else v), ephemeral=True)


class _MemberSelect(discord.ui.Select):
    def __init__(self, custom_id: str, placeholder: str, members):
        super().__init__(custom_id=custom_id, placeholder=placeholder[:100],
                         options=[discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in members],
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        ns, arg = self.custom_id.split(':')
        ch = interaction.guild.get_channel(int(arg))
        if not ch:
            return await interaction.response.send_message(t(gid, 'vm.vc_gone2'), ephemeral=True)
        if not _owns_channel(interaction.guild, interaction.user.id, arg):
            return await interaction.response.send_message(t(gid, 'vm.no_owner'), ephemeral=True)
        uid = self.values[0]
        if ns == 'vm_kick_select':
            target = interaction.guild.get_member(int(uid))
            try:
                if target and target.voice and target.voice.channel and target.voice.channel.id == ch.id:
                    await target.move_to(None)
                await ch.set_permissions(discord.Object(id=int(uid)), connect=False)
            except Exception:
                pass
            return await interaction.response.send_message(t(gid, 'vm.kicked_blocked', user=f'<@{uid}>'), ephemeral=True)
        if ns == 'vm_trust_select':
            try:
                await ch.set_permissions(discord.Object(id=int(uid)), connect=True, view_channel=True)
                humans = len([m for m in ch.members if not m.bot])
                if (ch.user_limit or 0) > 0 and humans >= ch.user_limit:
                    await ch.edit(user_limit=min(99, ch.user_limit + 1))
            except Exception:
                pass
            return await interaction.response.send_message(t(gid, 'vm.trusted', user=f'<@{uid}>'), ephemeral=True)
        if ns == 'vm_transfer_select':
            with db.conn_ctx() as conn:
                conn.execute('UPDATE temp_vcs SET owner_id=? WHERE channel_id=?', (uid, arg))
            try:
                await ch.set_permissions(discord.Object(id=int(uid)), manage_channels=True, mute_members=True,
                                         deafen_members=True, move_members=True)
                await ch.set_permissions(interaction.user, manage_channels=False)
            except Exception:
                pass
            return await interaction.response.send_message(t(gid, 'vm.moved', user=f'<@{uid}>'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Voice(bot))

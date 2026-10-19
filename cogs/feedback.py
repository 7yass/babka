"""Feedback intake — one forum thread per card request, bug report or idea.

`.feedback` opens a small form (category select + details). The form
posts a thread in the configured forum — or, when no forum is set up
yet, a plain message in the channel the command was run in, with a link
back to the author. Every submission is indexed in a local table so
staff can list them without walking the whole forum.

The button is routed through the cog's `on_interaction` listener on
purpose: a view callback would die with the process, the listener keeps
working after a restart.
"""
import functools
import os
import time

import discord
from discord.ext import commands

import database as db
from lang import t, set_ctx_lang
from utils.checks import is_staff
from utils.ui import ACCENT, divider, panel, stat

from cogs.polls import load_extra_lang

load_extra_lang()

# value -> (label key, english tag name)
CATEGORIES = {
    'card': ('fb.cat_card', 'Card request'),
    'bug': ('fb.cat_bug', 'Bug report'),
    'idea': ('fb.cat_idea', 'Idea'),
}
MAX_TEXT = 1000

DDL_FEEDBACK_CFG = '''
CREATE TABLE IF NOT EXISTS feedback_cfg (
    guild_id TEXT PRIMARY KEY,
    forum_channel_id TEXT,
    staff_role_id TEXT,
    created_at INTEGER)'''

DDL_FEEDBACK_SUBS = '''
CREATE TABLE IF NOT EXISTS feedback_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    category TEXT,
    text TEXT,
    thread_id TEXT,
    created INTEGER)'''


def ensure_tables() -> None:
    """The feedback config and the submission index. Created here, not
    in database.py."""
    with db.conn_ctx() as conn:
        conn.execute(DDL_FEEDBACK_CFG)
        conn.execute(DDL_FEEDBACK_SUBS)


def cfg(gid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM feedback_cfg WHERE guild_id=?', (str(gid),)).fetchone()
    return dict(row) if row else {}


def save_cfg(gid, forum_id=None, staff_role_id=None) -> None:
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO feedback_cfg (guild_id) VALUES (?)', (str(gid),))
        conn.execute('UPDATE feedback_cfg SET forum_channel_id=COALESCE(?,forum_channel_id), '
                     'staff_role_id=COALESCE(?,staff_role_id) WHERE guild_id=?',
                     (str(forum_id) if forum_id else None,
                      str(staff_role_id) if staff_role_id else None, str(gid)))


def is_forum(channel) -> bool:
    """Is this a forum channel?"""
    if channel is None:
        return False
    if isinstance(channel, discord.ForumChannel):
        return True
    return getattr(channel, 'type', None) == discord.ChannelType.forum


def forum_for(guild):
    """The configured forum channel, or None."""
    conf = cfg(guild.id)
    if not conf.get('forum_channel_id'):
        return None
    try:
        channel = guild.get_channel(int(conf['forum_channel_id']))
    except Exception:
        return None
    return channel if is_forum(channel) else None


def save_submission(gid, uid, category, text, thread_id='') -> int:
    with db.conn_ctx() as conn:
        cur = conn.execute(
            'INSERT INTO feedback_submissions (guild_id, user_id, category, text, thread_id, '
            'created) VALUES (?,?,?,?,?,?)',
            (str(gid), str(uid), category, text[:MAX_TEXT], str(thread_id or ''), int(time.time())))
        return cur.lastrowid or 0


def recent(gid, limit=15):
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM feedback_submissions WHERE guild_id=? ORDER BY id DESC LIMIT ?',
            (str(gid), int(limit))).fetchall()]


def is_feedback_staff(member, gid) -> bool:
    """Server staff, or the role stored in the feedback config."""
    if member is None:
        return False
    if is_staff(member):
        return True
    role_id = cfg(gid).get('staff_role_id')
    if not role_id:
        return False
    return any(str(r.id) == str(role_id) for r in getattr(member, 'roles', []) or [])


def feedback_staff(func):
    """Staff gate for the feedback commands.

    The check runs inside the wrapper instead of as a discord.py check:
    a failed check would raise CheckFailure before the command body and
    the user would never see why. functools.wraps keeps the original
    signature, so the command still parses its parameters.
    """
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        if not is_feedback_staff(ctx.author, ctx.guild.id):
            await ctx.reply(t(ctx.guild.id, 'fb.staff_only'), ephemeral=True)
            return None
        return await func(self, ctx, *args, **kwargs)
    return wrapper


def category_label(gid, category: str) -> str:
    key = CATEGORIES.get(category or '', (None, 'Idea'))[0]
    return t(gid, key or 'fb.cat_idea')


def jump_link(gid, channel_id, message_id) -> str:
    return f'https://discord.com/channels/{gid}/{channel_id}/{message_id}'


class FeedbackModal(discord.ui.Modal):
    """The form: a category select and the details."""

    def __init__(self, gid, uid, channel_id, message_id):
        super().__init__(title=t(gid, 'fb.form_title'),
                         custom_id=f'fb_modal_{os.urandom(8).hex()}')
        self.gid = int(gid)
        self.uid = int(uid)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)
        self.category = discord.ui.Select(
            custom_id='fb_category',
            placeholder=t(gid, 'fb.cat_label'),
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=t(gid, key), value=value)
                     for value, (key, _en) in CATEGORIES.items()])
        self.details = discord.ui.TextInput(
            label=t(gid, 'fb.text_label'),
            placeholder=t(gid, 'fb.text_placeholder'),
            style=discord.TextStyle.paragraph,
            max_length=MAX_TEXT, required=True)
        self.add_item(self.category)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        gid = self.gid
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass
        # discord.py 2.7 has no Select.value — the picked values live in .values
        picked = self.category.values or []
        category = picked[0] if picked else 'idea'
        text = (self.details.value or '').strip()
        if not text:
            return await self._say(interaction, t(gid, 'fb.empty'))
        guild = interaction.guild
        user = interaction.user
        label = category_label(gid, category)
        body = t(gid, 'fb.thread_body', cat=label, by=t(gid, 'fb.thread_from'),
                 user=user.mention, text=text)
        forum = forum_for(guild)
        if forum is not None:
            try:
                thread = await forum.create_thread(
                    name=f'{label} — {user.display_name}'[:100],
                    content=body[:2000],
                    applied_tags=self._tags(forum, category))
            except discord.HTTPException as e:
                return await self._say(interaction, t(gid, 'fb.failed',
                                                     err=f'{e.status} {e.text or ""}'[:80]))
            except Exception as e:
                return await self._say(interaction, t(gid, 'fb.failed',
                                                     err=f'{type(e).__name__}'))
            save_submission(gid, self.uid, category, text, str(thread.id))
            return await self._say(interaction, t(gid, 'fb.sent_forum', link=thread.jump_url))
        # no forum yet — a plain message in the channel it came from
        dest = guild.get_channel(self.channel_id) or interaction.channel
        link = jump_link(gid, self.channel_id, self.message_id)
        fallback = t(gid, 'fb.fallback_body', cat=label, user=user.mention, text=text, link=link)
        try:
            await dest.send(content=fallback[:2000])
        except Exception as e:
            return await self._say(interaction, t(gid, 'fb.failed',
                                                 err=f'{type(e).__name__}'))
        save_submission(gid, self.uid, category, text, '')
        return await self._say(interaction, t(gid, 'fb.no_forum', ch=dest.mention))

    def _tags(self, forum, category):
        """The forum tag of that category, if the forum has one."""
        wanted = {CATEGORIES.get(category, (None, 'Idea'))[1].lower()}
        for value, (key, en) in CATEGORIES.items():
            if value == category:
                wanted.add(t(self.gid, key).lower())
        for tag in forum.available_tags:
            if (tag.name or '').lower() in wanted:
                return [tag]
        return []

    async def _say(self, interaction, text):
        try:
            if interaction.response.is_done():
                return await interaction.followup.send(text, ephemeral=True)
            return await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            return None


class _OpenButton(discord.ui.Button):
    """Opens the form.

    No callback on purpose: the cog's `on_interaction` routes the click
    (restart-proof, and one single path to the modal). The context of
    the request travels inside the custom id.
    """

    def __init__(self, gid, uid, channel_id, message_id, label):
        super().__init__(label=label, style=discord.ButtonStyle.grey,
                         custom_id=f'fb_open_{gid}_{uid}_{channel_id}_{message_id}_'
                                   f'{os.urandom(4).hex()}')


class Feedback(commands.Cog):
    """Card requests, bug reports and ideas, through a forum."""

    def __init__(self, bot):
        self.bot = bot
        try:
            ensure_tables()
        except Exception as e:
            print(f'[feedback] table init failed: {type(e).__name__}: {e}')

    # ---------- interaction routing ----------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        set_ctx_lang(interaction.user)
        if interaction.type == discord.InteractionType.modal_submit:
            return  # the modal's own on_submit handles it
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id') or ''
        if not cid.startswith('fb_open_'):
            return
        parts = cid.split('_')
        # fb_open_<gid>_<uid>_<channel>_<message>_<token>
        if len(parts) < 6:
            return
        try:
            gid, uid, channel_id, message_id = (int(parts[2]), int(parts[3]),
                                                int(parts[4]), int(parts[5]))
        except (TypeError, ValueError):
            return
        try:
            await interaction.response.send_modal(
                FeedbackModal(gid, uid, channel_id, message_id))
        except Exception:
            try:
                await interaction.response.send_message(
                    t(gid, 'fb.failed', err='modal'), ephemeral=True)
            except Exception:
                pass

    # ---------- commands ----------

    @commands.group(name='feedback', description='Zgłoszenia, prośby o karty, pomysły')
    async def feedback(self, ctx):
        gid = ctx.guild.id
        button = _OpenButton(gid, ctx.author.id, ctx.channel.id, ctx.message.id,
                              t(gid, 'fb.open_btn'))
        view = discord.ui.View(timeout=3600)
        view.add_item(button)
        await ctx.reply(t(gid, 'fb.open_desc') + '\n' + t(gid, 'fb.help'),
                        view=view, ephemeral=True)

    @feedback.command(name='setup', description='Ustaw forum zgłoszeń (staff)')
    @feedback_staff
    async def feedback_setup(self, ctx, forum: discord.abc.GuildChannel = None):
        gid = ctx.guild.id
        if forum is None:
            forum = forum_for(ctx.guild)
        if not is_forum(forum):
            return await ctx.reply(t(gid, 'fb.setup_bad'), ephemeral=True)
        save_cfg(gid, forum_id=forum.id)
        tags = await self._ensure_tags(forum, gid)
        await ctx.reply(t(gid, 'fb.setup_ok', ch=forum.mention) +
                        (' ' + t(gid, 'fb.setup_tags', tags=tags) if tags else ''),
                        ephemeral=True)

    @feedback.command(name='list', description='Ostatnie zgłoszenia (staff)')
    @feedback_staff
    async def feedback_list(self, ctx):
        gid = ctx.guild.id
        rows = recent(gid, limit=15)
        forum = forum_for(ctx.guild)
        if not rows and (forum is None or not forum.threads):
            return await ctx.reply(t(gid, 'fb.list_empty'), ephemeral=True)
        lines = []
        seen_threads = set()
        for row in rows:
            thread = None
            if forum is not None and row.get('thread_id'):
                try:
                    thread = forum.get_thread(int(row['thread_id']))
                except Exception:
                    thread = None
            user = ctx.guild.get_member(int(row['user_id']))
            who = user.display_name if user is not None else f'<@{row["user_id"]}>'
            when = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(row.get('created') or 0)))
            text = (row.get('text') or '')[:120].replace('\n', ' ')
            line = t(gid, 'fb.list_item', cat=category_label(gid, row.get('category')),
                     user=who, when=when, text=text)
            if thread is not None:
                seen_threads.add(thread.id)
                line += '\n' + t(gid, 'fb.list_link', link=thread.jump_url)
            else:
                line += '\n-# ' + t(gid, 'fb.thread_gone')
            lines.append(line)
        if forum is not None:
            for thread in forum.threads:
                if thread.id in seen_threads:
                    continue
                lines.append(t(gid, 'fb.list_item',
                               cat=thread.name[:60],
                               user='—',
                               when=time.strftime('%Y-%m-%d %H:%M',
                                                  time.localtime(int(thread.created_at.timestamp()))),
                               text='-') + '\n' + t(gid, 'fb.list_link', link=thread.jump_url))
        body = stat(t(gid, 'fb.list_count'), len(rows)) + divider() + '\n'.join(lines)
        await ctx.reply(view=panel(t(gid, 'fb.list_title'), body, accent=ACCENT['default']),
                        mention_author=False)

    async def _ensure_tags(self, forum, gid):
        """Create the category tags of the forum, once."""
        existing = {(tag.name or '').lower() for tag in forum.available_tags}
        made = []
        for value, (key, en) in CATEGORIES.items():
            name = t(gid, key)
            if name.lower() in existing or en.lower() in existing:
                continue
            try:
                await forum.create_tag(name=name[:20])
                made.append(name)
            except Exception:
                pass
        return ', '.join(made)


async def setup(bot):
    await bot.add_cog(Feedback(bot))

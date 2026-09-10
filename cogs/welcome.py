"""Greetings + goodbyes. Hybrid setup."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.checks import staff_or
from utils.cards import get_style, render_greet, fetch_bytes

_BG_CACHE = {}


async def _card_bytes(gid, kind: str, member: discord.Member, stat: str):
    import asyncio
    style = get_style(gid, kind)
    bg = None
    if style.get('bg_url'):
        import time
        hit = _BG_CACHE.get(style['bg_url'])
        if not hit or time.time() - hit[0] > 3600:
            _BG_CACHE[style['bg_url']] = (time.time(), await fetch_bytes(style['bg_url']))
            hit = _BG_CACHE[style['bg_url']]
        bg = hit[1]
    try:
        avatar = await member.display_avatar.with_size(256).read()
    except Exception:
        avatar = None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, render_greet, kind, member.display_name, stat, avatar, bg, style)


def fill(text: str, member: discord.Member, guild: discord.Guild) -> str:
    return (text.replace('{user}', member.mention).replace('{name}', member.name)
            .replace('{server}', guild.name).replace('{count}', str(guild.member_count or 0)))


def blocked(guild_id, member: discord.Member) -> bool:
    with db.conn_ctx() as conn:
        if conn.execute('SELECT 1 FROM permabans WHERE guild_id=? AND user_id=?',
                        (str(guild_id), str(member.id))).fetchone():
            return True
        cfg = conn.execute('SELECT * FROM antiraid WHERE guild_id=?', (str(guild_id),)).fetchone()
    if cfg and cfg['enabled']:
        age = (discord.utils.utcnow() - member.created_at).total_seconds() / 86400
        if age < (cfg['min_age_days'] or 60):
            return True
    return False


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(member.guild.id),)).fetchone()
        if not cfg or not cfg['enabled'] or not cfg['channel_id'] or not cfg['join_text']:
            return
        if blocked(member.guild.id, member):
            return
        ch = member.guild.get_channel(int(cfg['channel_id']))
        if not ch:
            return
        text = cfg['join_text']
        if text.startswith('default:'):
            text = t(member.guild.id, 'welcome.join_default')
        files = []
        try:
            if cfg['join_card']:
                stat = f"Member #{member.guild.member_count or 0}"
                files = [discord.File(__import__('io').BytesIO(
                    await _card_bytes(member.guild.id, 'welcome', member, stat)), 'welcome.png')]
        except Exception as e:
            print(f'[welcome] card failed: {e}')
        try:
            await ch.send(fill(text, member, member.guild), files=files or None)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(member.guild.id),)).fetchone()
        if not cfg or not cfg['enabled'] or not cfg['leave_text']:
            return
        ch = member.guild.get_channel(int(cfg['leave_channel_id'] or cfg['channel_id'] or 0) or 0)
        if not ch:
            return
        text = cfg['leave_text']
        if text.startswith('default:'):
            text = t(member.guild.id, 'welcome.leave_default')
        files = []
        try:
            if cfg['leave_card']:
                stat = f"{member.guild.member_count or 0} left"
                files = [discord.File(__import__('io').BytesIO(
                    await _card_bytes(member.guild.id, 'leave', member, stat)), 'farewell.png')]
        except Exception as e:
            print(f'[welcome] leave card failed: {e}')
        try:
            await ch.send(fill(text, member, member.guild), files=files or None)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join_ping(self, member: discord.Member):
        # ping-on-join: ping the newcomer, delete the ping after N seconds
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(member.guild.id),)).fetchone()
        try:
            ping_ch = cfg and cfg['pingjoin_channel']
        except Exception:
            ping_ch = None
        if not ping_ch:
            return
        ch = member.guild.get_channel(int(ping_ch))
        if not ch:
            return
        text = (cfg and cfg['pingjoin_text']) or '{user}'
        secs = 10
        try:
            secs = max(3, min(int((cfg and cfg['pingjoin_delete']) or 10), 300))
        except Exception:
            pass
        try:
            msg = await ch.send(fill(text, member, member.guild),
                                allowed_mentions=discord.AllowedMentions(users=True))
            await msg.delete(delay=secs)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update_boost(self, before: discord.Member, after: discord.Member):
        # boost message: fires when premium_since gets set
        if before.premium_since or not after.premium_since:
            return
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(after.guild.id),)).fetchone()
        try:
            bch = cfg and cfg['boost_channel']
        except Exception:
            bch = None
        if not bch:
            return
        ch = after.guild.get_channel(int(bch))
        if not ch:
            return
        text = (cfg and cfg['boost_text']) or t(after.guild.id, 'wl.boost_default')
        try:
            await ch.send(fill(text, after, after.guild))
        except Exception:
            pass

    @commands.hybrid_group(name='welcome', description='Powitania')
    async def welcome(self, ctx):
        await ctx.reply('/welcome setup / test / disable', ephemeral=True)

    @welcome.command(name='setup', description='Wejścia i wyjścia')
    @staff_or('manage_guild')
    async def setup_w(self, ctx, channel: discord.TextChannel, *, texts: str = ''):
        parts = [p.strip() for p in texts.split('|')]
        join = parts[0] if len(parts) > 0 and parts[0] else 'default:join'
        leave = parts[1] if len(parts) > 1 and parts[1] else 'default:leave'
        with db.conn_ctx() as conn:
            conn.execute("""INSERT INTO welcome_cfg (guild_id, channel_id, join_text, leave_text, enabled)
                VALUES (?,?,?,?,1) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
                join_text=excluded.join_text, leave_text=excluded.leave_text, enabled=1""",
                         (str(ctx.guild.id), str(channel.id), join, leave))
        await ctx.reply(t(ctx.guild.id, 'welcome.setup_ok', channel=channel.mention), ephemeral=True)

    @welcome.command(name='test', description='Podgląd')
    @staff_or('manage_guild')
    async def test(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            cfg = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        if not cfg or not cfg['channel_id']:
            return await ctx.reply(t(gid, 'welcome.not_set'), ephemeral=True)
        jt = cfg['join_text']
        lt = cfg['leave_text']
        if jt.startswith('default:'):
            jt = t(gid, 'welcome.join_default')
        if lt.startswith('default:'):
            lt = t(gid, 'welcome.leave_default')
        await ctx.reply(t(gid, 'welcome.test', join=fill(jt, ctx.author, ctx.guild), leave=fill(lt, ctx.author, ctx.guild)),
                        ephemeral=True)

    @welcome.command(name='disable', description='Wyłącz powitania')
    @staff_or('manage_guild')
    async def disable(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('UPDATE welcome_cfg SET enabled=0 WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'welcome.disabled'), ephemeral=True)

    @welcome.command(name='boost', description='Boosty (albo "off")')
    @staff_or('manage_guild')
    async def boost(self, ctx, channel: discord.TextChannel = None, *, text: str = ''):
        gid = ctx.guild.id
        if channel is None and text.strip().lower() == 'off':
            with db.conn_ctx() as conn:
                conn.execute('UPDATE welcome_cfg SET boost_channel=NULL WHERE guild_id=?', (str(gid),))
            return await ctx.reply(t(gid, 'wl.boost_off'), ephemeral=True)
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            conn.execute("""INSERT INTO welcome_cfg (guild_id, boost_channel, boost_text) VALUES (?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET boost_channel=excluded.boost_channel,
                boost_text=excluded.boost_text""",
                         (str(gid), str(channel.id), text or None))
        await ctx.reply(t(gid, 'wl.boost_ok', ch=channel.mention), ephemeral=True)

    @welcome.command(name='pingjoin', description='Ping na wejściu (albo "off")')
    @staff_or('manage_guild')
    async def pingjoin(self, ctx, channel: discord.TextChannel = None, delete_after: int = 10, *, text: str = ''):
        gid = ctx.guild.id
        if channel is None and (text.strip().lower() == 'off' or not text.strip()):
            with db.conn_ctx() as conn:
                conn.execute('UPDATE welcome_cfg SET pingjoin_channel=NULL WHERE guild_id=?', (str(gid),))
            return await ctx.reply(t(gid, 'wl.ping_off'), ephemeral=True)
        channel = channel or ctx.channel
        with db.conn_ctx() as conn:
            conn.execute("""INSERT INTO welcome_cfg (guild_id, pingjoin_channel, pingjoin_text, pingjoin_delete)
                VALUES (?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET pingjoin_channel=excluded.pingjoin_channel,
                pingjoin_text=excluded.pingjoin_text, pingjoin_delete=excluded.pingjoin_delete""",
                         (str(gid), str(channel.id), text or '{user}', max(3, min(delete_after, 300))))
        await ctx.reply(t(gid, 'wl.ping_ok', ch=channel.mention, s=max(3, min(delete_after, 300))), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))

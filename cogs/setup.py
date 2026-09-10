"""First-run setup: staff roles, log channels, full status checklist. Hybrid."""
import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import build, WHITE
from utils.checks import staff_or, get_staff_roles


def status_embed(guild: discord.Guild) -> discord.Embed:
    gid = guild.id
    settings = db.get_settings(gid)
    raid = db.get_antiraid(gid)
    staff = get_staff_roles(gid)
    with db.conn_ctx() as conn:
        welcome = conn.execute('SELECT * FROM welcome_cfg WHERE guild_id=?', (str(gid),)).fetchone()
        vm = conn.execute('SELECT * FROM voicemaster WHERE guild_id=?', (str(gid),)).fetchone()
        verify = conn.execute('SELECT * FROM verify_panels WHERE guild_id=?', (str(gid),)).fetchone()
        tt = conn.execute('SELECT COUNT(*) c FROM tiktok_watch WHERE guild_id=?', (str(gid),)).fetchone()['c']
        panels = conn.execute('SELECT COUNT(*) c FROM info_panels WHERE guild_id=?', (str(gid),)).fetchone()['c']
        rewards = conn.execute('SELECT COUNT(*) c FROM level_rewards WHERE guild_id=?', (str(gid),)).fetchone()['c']

    def ch(cid):
        return f'<#{cid}>' if cid else t(gid, 'setup.missing')

    lines = [
        t(gid, 'setup.row_prefix', v=db.get_prefix(gid)),
        t(gid, 'setup.row_lang', v='polski' if __import__('lang').get_lang(gid) == 'pl' else 'english'),
        t(gid, 'setup.row_staff', v=' '.join(f'<@&{r}>' for r in staff) if staff else t(gid, 'setup.missing')),
        t(gid, 'setup.row_modlog', v=ch(settings.get('modlog_channel'))),
        t(gid, 'setup.row_levelup', v=ch(settings.get('levelup_channel'))),
        t(gid, 'setup.row_age', v=t(gid, 'setup.age_on', days=raid['min_age_days'], action=raid['action']) if raid['enabled'] else t(gid, 'setup.off')),
        t(gid, 'setup.row_honeypot', v=ch(raid.get('honeypot_channel'))),
        t(gid, 'setup.row_welcome', v=ch(welcome['channel_id']) if welcome and welcome['enabled'] and welcome['channel_id'] else t(gid, 'setup.missing')),
        t(gid, 'setup.row_tiktok', v=str(tt) if tt else t(gid, 'setup.missing')),
        t(gid, 'setup.row_lobby', v=ch(vm['master_channel_id']) if vm and vm['master_channel_id'] else t(gid, 'setup.missing')),
        t(gid, 'setup.row_panels', v=str(panels) if panels else t(gid, 'setup.missing')),
        t(gid, 'setup.row_verify', v=ch(verify['channel_id']) if verify and verify['message_id'] else t(gid, 'setup.missing')),
        t(gid, 'setup.row_rewards', v=str(rewards) if rewards else t(gid, 'setup.missing')),
    ]
    return build('\n'.join(lines) + '\n\n' + t(gid, 'setup.hint'), title=t(gid, 'setup.title'), color=WHITE)


class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name='setup', description='Startowa konfiguracja')
    async def setup(self, ctx):
        await ctx.reply('/setup status / staff-add / staff-remove / set-modlog / set-levelup', ephemeral=True)

    @setup.command(name='status', description='Pełna checklista')
    async def status(self, ctx):
        await ctx.reply(embed=status_embed(ctx.guild), ephemeral=True)

    @setup.command(name='staff-add', description='Rola ekipy')
    @staff_or('manage_guild')
    async def staff_add(self, ctx, role: discord.Role):
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?,?)',
                         (str(ctx.guild.id), str(role.id)))
        await ctx.reply(t(ctx.guild.id, 'setup.staff_added', role=role.mention), ephemeral=True)

    @setup.command(name='staff-remove', description='Usuń rolę ekipy')
    @staff_or('manage_guild')
    async def staff_remove(self, ctx, role: discord.Role):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM staff_roles WHERE guild_id=? AND role_id=?', (str(ctx.guild.id), str(role.id)))
        await ctx.reply(t(ctx.guild.id, 'setup.staff_removed', role=role.mention), ephemeral=True)

    @setup.command(name='set-modlog', description='Kanał logów modów')
    @staff_or('manage_guild')
    async def set_modlog(self, ctx, channel: discord.TextChannel):
        db.get_settings(ctx.guild.id)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET modlog_channel=? WHERE guild_id=?',
                         (str(channel.id), str(ctx.guild.id)))
        await ctx.reply(t(ctx.guild.id, 'setup.modlog_set', channel=channel.mention), ephemeral=True)

    @setup.command(name='set-levelup', description='Kanał level-upów')
    @staff_or('manage_guild')
    async def set_levelup(self, ctx, channel: discord.TextChannel = None):
        db.get_settings(ctx.guild.id)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE guild_settings SET levelup_channel=? WHERE guild_id=?',
                         (str(channel.id) if channel else None, str(ctx.guild.id)))
        await ctx.reply(t(ctx.guild.id, 'setup.levelup_set', channel=channel.mention) if channel
                        else t(ctx.guild.id, 'setup.levelup_set', channel=t(ctx.guild.id, 'setup.same_channel')),
                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Setup(bot))

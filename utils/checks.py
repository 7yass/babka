"""Staff gate: Administrator / Manage Server / staff role passes everything."""
import discord
from discord.ext import commands
import database as db


def get_staff_roles(guild_id) -> list:
    with db.conn_ctx() as conn:
        return [r['role_id'] for r in
                conn.execute('SELECT role_id FROM staff_roles WHERE guild_id = ?', (str(guild_id),)).fetchall()]


def is_staff(member: discord.Member) -> bool:
    if member is None:
        return False
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    roles = get_staff_roles(member.guild.id)
    return any(str(r.id) in roles for r in member.roles)


def staff_or(perm: str):
    """Check decorator: discord perm OR staff role. Usage: @staff_or('manage_messages')."""
    async def predicate(ctx) -> bool:
        from lang import t
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        if member and (getattr(member.guild_permissions, perm, False) or is_staff(member)):
            return True
        gid = ctx.guild.id if ctx.guild else None
        msg = t(gid, 'staff.only')
        try:
            if ctx.interaction and not ctx.interaction.response.is_done():
                await ctx.interaction.response.send_message(msg, ephemeral=True)
            else:
                await ctx.reply(embed=__import__('utils.embeds', fromlist=['err']).err(msg), mention_author=False, delete_after=10)
        except Exception:
            pass
        return False
    return commands.check(predicate)

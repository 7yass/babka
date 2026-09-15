"""Achievements: auto-awarded badges, shown on the profile card.
`.achievements [@user]` lists them. Hooks live in work()/shop buy()/profile."""
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.emojis import em

# key -> (icon, short ASCII for the card, name, description).
# Converted keys hold a fleet emoji NAME in [0] (resolved via badge_icon);
# all other keys keep their legacy glyph untouched.
BADGES = {
    'first_job': ('💼', 'HIRED', 'First Job', 'Get hired anywhere.'),
    'grinder': ('🏭', 'GRIND', 'Grinder', 'Work 25 shifts.'),
    'lifer': ('⚒️', 'LIFER', 'Lifer', 'Work 100 shifts.'),
    'lvl10': ('star', 'LVL10', 'Rising', 'Reach level 10.'),
    'lvl25': ('star', 'LVL25', 'Star', 'Reach level 25.'),
    'lvl50': ('star', 'LVL50', 'Legend', 'Reach level 50.'),
    'rich100k': ('💰', '100K', 'Six Figures', 'Hold 100K cash.'),
    'rich1m': ('💎', '1M', 'Millionaire', 'Hold 1M cash.'),
    'rich5m': ('👑', '5M', 'Mogul', 'Hold 5M cash.'),
    'highroller': ('🎲', 'ROLLER', 'High Roller', 'Buy the High Roller pass.'),
    'famous': ('📣', 'FAMOUS', 'Famous', 'Reach 1K fans.'),
    'region_kanto': ('region_kanto', 'KANTO', 'Kanto Master', 'Finish the Kanto quest track.'),
    'region_johto': ('region_johto', 'JOHTO', 'Johto Master', 'Finish the Johto quest track.'),
    'region_hoenn': ('region_hoenn', 'HOENN', 'Hoenn Master', 'Finish the Hoenn quest track.'),
    'region_sinnoh': ('region_sinnoh', 'SINNOH', 'Sinnoh Master', 'Finish the Sinnoh quest track.'),
    'npc_champ': ('trophy', 'CHAMP', 'Champion Slayer', 'Beat Champion Cyntia.'),
}

# ASCII fallbacks for converted keys (custom emoji missing -> plain text).
PK_FALLBACK = {
    'lvl10': '*', 'lvl25': '*', 'lvl50': '*',
    'region_kanto': 'K', 'region_johto': 'J',
    'region_hoenn': 'H', 'region_sinnoh': 'S',
    'npc_champ': 'T',
}


def badge_icon(gid, key: str) -> str:
    """Render icon for a badge: custom fleet emoji, ASCII fallback,
    or the legacy glyph for non-converted systems."""
    if key in PK_FALLBACK:
        return em(gid, BADGES[key][0]) or PK_FALLBACK[key]
    return BADGES[key][0]


def unlocked(gid, uid) -> set:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT akey FROM achievements WHERE guild_id=? AND user_id=?',
                            (str(gid), str(uid))).fetchall()
    return {r['akey'] for r in rows}


def badge_shorts(gid, uid) -> list:
    """Short ASCII badge names for the profile card, in BADGES order."""
    have = unlocked(gid, uid)
    return [BADGES[k][1] for k in BADGES if k in have]


def maybe_award(gid, uid) -> list:
    """Check every condition, award what's earned. Returns new badge keys."""
    from cogs.levels import get_user
    from cogs.gamble import bal
    from cogs.jobs import get_job
    have = unlocked(gid, uid)
    d = get_user(gid, uid)
    b = bal(gid, uid)
    j = get_job(gid, uid)
    checks = {
        'first_job': bool(j.get('job')),
        'grinder': (j.get('shifts', 0) or 0) >= 25,
        'lifer': (j.get('shifts', 0) or 0) >= 100,
        'lvl10': (d.get('level', 0) or 0) >= 10,
        'lvl25': (d.get('level', 0) or 0) >= 25,
        'lvl50': (d.get('level', 0) or 0) >= 50,
        'rich100k': (b.get('cash', 0) or 0) >= 100_000,
        'rich1m': (b.get('cash', 0) or 0) >= 1_000_000,
        'rich5m': (b.get('cash', 0) or 0) >= 5_000_000,
        'famous': (j.get('fans', 0) or 0) >= 1000,
    }
    with db.conn_ctx() as conn:
        row = conn.execute("SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item='highroller'",
                           (str(gid), str(uid))).fetchone()
        checks['highroller'] = bool(row)
    now = int(time.time())
    new = []
    with db.conn_ctx() as conn:
        for key, earned in checks.items():
            if earned and key not in have:
                conn.execute('INSERT OR IGNORE INTO achievements (guild_id, user_id, akey, unlocked_at) '
                             'VALUES (?,?,?,?)', (str(gid), str(uid), key, now))
                new.append(key)
    return new


class Achievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='achievements', description='Twoje odznaki', aliases=['badges', 'ach'])
    async def achievements(self, ctx, member: discord.Member = None):
        from cogs.gamble import _game_layout
        member = member or ctx.author
        gid = ctx.guild.id
        have = unlocked(gid, member.id)
        if not have:
            return await ctx.reply(t(gid, 'eco.ach_none', user=member.display_name),
                                   ephemeral=True)
        lines = [f"{badge_icon(gid, k)} **{BADGES[k][2]}** — {BADGES[k][3]}"
                 for k in BADGES if k in have]
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.ach_title', user=member.display_name, n=len(have)),
            '\n'.join(lines)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Achievements(bot))

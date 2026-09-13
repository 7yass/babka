"""Language switch. Hybrid."""
from discord.ext import commands

from lang import t, set_lang, get_lang
from utils.checks import staff_or


class Language(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='language', description='Język bota: english / polski')
    @staff_or('manage_guild')
    async def language(self, ctx, lang: str = None):
        gid = ctx.guild.id if ctx.guild else None
        if not lang:
            return await ctx.reply(t(gid, 'lang.current',
                                     lang='polski' if get_lang(gid) == 'pl' else 'english'), ephemeral=True)
        v = lang.lower()
        if v.startswith('pl') or v.startswith('pol'):
            set_lang(gid, 'pl')
        else:
            set_lang(gid, 'en')
        await ctx.reply(t(gid, 'lang.set'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Language(bot))

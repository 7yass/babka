"""Jobs 2.0: careers with fame tiers, gender-aware OnlyFans contracts, CEO house job."""
import random
import time

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok
from utils.checks import staff_or

GIRL_ROLE = '1531037037153747025'
BOY_ROLE = '1531037181270032404'
WORK_CD = 3600

# (min_level, title, pay_mult)
FAME = [(0, 'nikt', 1.0), (5, 'lokals', 1.3), (10, 'znany', 1.7),
        (20, 'gwiazda', 2.2), (30, 'legenda', 3.0)]

BRANDS = ['FitKoks', 'Volt Energy', 'Kebab Sultan', 'BubbleTea Boom', 'Gym Shark PL']
OF_M = ['Stachu Twardy', 'Benon Wielki', 'Koksu Mario']
OF_F = ['Lola Słodka', 'Mia Gorąca', 'Diana Dym']

JOBS = {
    'tiktoker': {'label': 'TikToker', 'base': (120, 260),
                 'shifts': ['nagrywałeś tańce na parkingu', 'robiłeś live z lodówki',
                            'kręciłeś pranka na sąsiedzie', 'montowałeś vloga całą noc']},
    'onlyfans': {'label': 'OnlyFans', 'base': (100, 220),
                 'shifts': ['wrzucałeś ekskluzywną sesję', 'robiłeś Q&A dla fanów',
                            'streamowałeś backstage', 'podpisywałeś fotki dla top fanów']},
    'mcdonalds': {'label': 'McDonalds', 'base': (110, 200),
                  'shifts': ['smażyłeś frytki na nocnej zmianie', 'obsługiwałeś drive-thru w deszczu',
                             'składałeś Big Maci na czas', 'sprzątałeś salę po imprezie klasowej']},
    'kurier': {'label': 'Kurier', 'base': (120, 230),
               'shifts': ['wiozłeś paczki po całym mieście', 'goniłeś z jedzeniem w ulewie',
                          'wdrapywałeś się na 10 piętro bez windy', 'rozwoziłeś prezenty przed świętami']},
    'mechanik': {'label': 'Mechanik', 'base': (130, 250),
                 'shifts': ['wymieniałeś sprzęgło w passacie', 'stawiałeś diagnozę po dźwięku',
                            'robiłeś przegląd przed zimą', 'wyciągałeś auto z rowu']},
    'ceo': {'label': 'Young CEO', 'base': (800, 1500), 'hidden': True,
            'shifts': ['podpisywałeś kontrakty na jachcie', 'zwalniałeś zarząd przez telefon',
                       'kupowałeś kolejną firmę z nudów', 'grałeś w golfa z inwestorami']},
}


def fame_of(level: int):
    idx, title, mult = 0, FAME[0][1], FAME[0][2]
    for i, (ml, ti, mu) in enumerate(FAME):
        if level >= ml:
            idx, title, mult = i, ti, mu
    return idx, title, mult


def get_job(gid, uid) -> dict:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
        return dict(row) if row else {}


async def ensure_job_role(guild: discord.Guild, key: str):
    label = JOBS[key]['label']
    role = discord.utils.find(lambda r: r.name == label, guild.roles)
    if not role:
        try:
            role = await guild.create_role(name=label, reason='job role')
        except Exception:
            return None
    return role


class Jobs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name='job', description='Twoja kariera')
    async def job(self, ctx):
        await ctx.reply('/job list / join / leave / my', ephemeral=True)

    @job.command(name='list', description='Oferty pracy')
    async def job_list(self, ctx):
        lines = []
        for key, j in JOBS.items():
            if j.get('hidden'):
                continue
            lines.append(f"• **{j['label']}** — {j['base'][0]}–{j['base'][1]} / zmianę")
        await ctx.reply(embed=ok(t(ctx.guild.id, 'job.list_title') + '\n' + '\n'.join(lines)), ephemeral=True)

    @job.command(name='join', description='Zatrudnij się')
    async def job_join(self, ctx, name: str):
        gid = ctx.guild.id
        key = (name or '').lower().strip()
        if key == 'ceo' and not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'job.nope'), ephemeral=True)
        if key not in JOBS:
            return await ctx.reply(t(gid, 'job.nope'), ephemeral=True)
        old = get_job(gid, ctx.author.id).get('job')
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO jobs (guild_id, user_id, job, fans, tier) VALUES (?,?,?,?,0)',
                         (str(gid), str(ctx.author.id), key, 0))
        member = ctx.author
        try:
            if old and old != key and old in JOBS:
                r = discord.utils.find(lambda x: x.name == JOBS[old]['label'], ctx.guild.roles)
                if r:
                    await member.remove_roles(r, reason='job change')
            role = await ensure_job_role(ctx.guild, key)
            if role and role not in member.roles:
                await member.add_roles(role, reason='new job')
        except Exception:
            pass
        await ctx.reply(t(gid, 'job.hired', job=JOBS[key]['label']), ephemeral=True)

    @job.command(name='leave', description='Rzuć robotę')
    async def job_leave(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM jobs WHERE guild_id=? AND user_id=?',
                         (str(ctx.guild.id), str(ctx.author.id)))
        await ctx.reply(t(ctx.guild.id, 'job.left'), ephemeral=True)

    @job.command(name='my', description='Twoja kariera i fame')
    async def job_my(self, ctx):
        from cogs.levels import get_user
        gid = ctx.guild.id
        j = get_job(gid, ctx.author.id)
        lv = get_user(gid, ctx.author.id).get('level', 0)
        idx, title, mult = fame_of(lv)
        job = JOBS.get(j.get('job'), {}).get('label', t(gid, 'job.none'))
        await ctx.reply(embed=ok(t(gid, 'job.card', job=job, fame=title, mult=mult,
                                           fans=j.get('fans', 0), level=lv)), ephemeral=True)

    @commands.hybrid_command(name='work', description='Idź do roboty')
    async def work(self, ctx):
        from cogs.levels import get_user
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        jl = db.jail_left(gid, ctx.author.id)
        if jl:
            return await ctx.reply(t(gid, 'eco.jailed', m=max(1, jl // 60)), ephemeral=True)
        b = bal(gid, ctx.author.id)
        now = int(time.time())
        if now - (b.get('last_work') or 0) < WORK_CD:
            m = (WORK_CD - (now - (b.get('last_work') or 0))) // 60
            return await ctx.reply(t(gid, 'eco.work_wait', m=m), ephemeral=True)
        j = get_job(gid, ctx.author.id)
        key = j.get('job') if j.get('job') in JOBS else None
        lv = get_user(gid, ctx.author.id).get('level', 0)
        idx, title, mult = fame_of(lv)
        if key:
            job = JOBS[key]
            flavor = random.choice(job['shifts'])
            lo, hi = job['base']
            pay = int(random.randint(lo, hi) * mult)
            extra = ''
            fans_gain = 0
            if key == 'onlyfans':
                roles = [str(r.id) for r in (ctx.author.roles if isinstance(ctx.author, discord.Member) else [])]
                if GIRL_ROLE in roles:
                    partner = random.choice(OF_M)
                elif BOY_ROLE in roles:
                    partner = random.choice(OF_F)
                else:
                    partner = None
                if partner and random.random() < 0.5:
                    bonus = int(pay * 0.8)
                    pay += bonus
                    fans_gain = random.randint(5, 15) * (idx + 1)
                    extra = t(gid, 'job.of_collab', who=partner, bonus=bonus)
                elif random.random() < 0.4:
                    brand = random.choice(BRANDS)
                    bonus = int(pay * 0.4)
                    pay += bonus
                    fans_gain = random.randint(2, 6) * (idx + 1)
                    extra = t(gid, 'job.of_brand', who=brand, bonus=bonus)
                else:
                    fans_gain = random.randint(1, 4) * (idx + 1)
            elif key == 'tiktoker' and random.random() < 0.2:
                pay *= 3
                extra = t(gid, 'job.viral')
            elif key == 'mcdonalds' and random.random() < 0.25:
                if random.random() < 0.5:
                    tip = int(pay * 0.5)
                    pay += tip
                    extra = t(gid, 'job.tip', tip=tip)
                else:
                    cut = int(pay * 0.3)
                    pay -= cut
                    extra = t(gid, 'job.karen', cut=cut)
            if fans_gain:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE jobs SET fans=fans+? WHERE guild_id=? AND user_id=?',
                                 (fans_gain, str(gid), str(ctx.author.id)))
            old_tier = j.get('tier', 0)
            if idx > old_tier:
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE jobs SET tier=? WHERE guild_id=? AND user_id=?',
                                 (idx, str(gid), str(ctx.author.id)))
                extra += '\n' + t(gid, 'job.promo', fame=title)
            set_cash(gid, ctx.author.id, b['cash'] + pay)
            with db.conn_ctx() as conn:
                conn.execute('UPDATE eco SET last_work=? WHERE guild_id=? AND user_id=?',
                             (now, str(gid), str(ctx.author.id)))
            try:
                role = await ensure_job_role(ctx.guild, key)
                if role and isinstance(ctx.author, discord.Member) and role not in ctx.author.roles:
                    await ctx.author.add_roles(role, reason='job role')
            except Exception:
                pass
            msg = t(gid, 'eco.work_done', job=f"{job['label']}: {flavor}", pay=pay)
            if extra.strip():
                msg += '\n' + extra.strip()
            return await ctx.reply(msg)
        # no job: day labor
        jobs_txt = t(gid, 'eco.jobs').split('|')
        job = random.choice(jobs_txt).strip()
        pay = random.randint(100, 300)
        set_cash(gid, ctx.author.id, b['cash'] + pay)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_work=? WHERE guild_id=? AND user_id=?',
                         (now, str(gid), str(ctx.author.id)))
        await ctx.reply(t(gid, 'eco.work_done', job=job, pay=pay))


async def setup(bot):
    await bot.add_cog(Jobs(bot))

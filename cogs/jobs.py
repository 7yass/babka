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

# career ladder to max level 50: (min_level, title, pay_mult)
LADDER = [(0, 'Pracownik', 1.0), (5, 'Manager', 1.25), (10, 'Executive', 1.55),
          (15, 'Dyrektor', 1.9), (20, 'Wiceprezes', 2.25), (25, 'Starszy Wiceprezes', 2.6),
          (30, 'Dyrektor Operacyjny', 3.0), (35, 'Prezes', 3.4),
          (40, 'Przewodniczący', 3.8), (45, 'Mogul', 4.3)]


def ladder_of(level: int):
    idx, title, mult = 0, LADDER[0][1], LADDER[0][2]
    for i, (ml, ti, mu) in enumerate(LADDER):
        if level >= ml:
            idx, title, mult = i, ti, mu
    nxt = LADDER[idx + 1][0] if idx + 1 < len(LADDER) else None
    return idx, title, mult, nxt


BRANDS = ['FitKoks', 'Volt Energy', 'Kebab Sultan', 'BubbleTea Boom', 'Gym Shark PL']
OF_M = ['Stachu Twardy', 'Benon Wielki', 'Koksu Mario']
OF_F = ['Lola Słodka', 'Mia Gorąca', 'Diana Dym']

JOBS = {
    'tiktoker': {'label': 'TikToker', 'base': (120, 260),
                 'track': ['Widz', 'Początkujący', 'Twórca', 'Regularny', 'Znany Twórca',
                           'Influencer', 'Gwiazda', 'Supergwiazda', 'Ikona', 'Legenda TikToka'],
                 'house_title': 'Król Algorytmu',
                 'shifts': ['nagrywałeś tańce na parkingu', 'robiłeś live z lodówki',
                            'kręciłeś pranka na sąsiedzie', 'montowałeś vloga całą noc']},
    'onlyfans': {'label': 'OnlyFans', 'base': (100, 220),
                 'track': ['Nowy', 'Początkujący', 'Twórca', 'Regularny', 'Popularny',
                           'Top Twórca', 'Gwiazda', 'Supergwiazda', 'Ikona', 'Legenda OF'],
                 'house_title': 'Właściciel Platformy',
                 'shifts': ['wrzucałeś ekskluzywną sesję', 'robiłeś Q&A dla fanów',
                            'streamowałeś backstage', 'podpisywałeś fotki dla top fanów']},
    'mcdonalds': {'label': 'McDonalds', 'base': (110, 200),
                  'track': ['Praktykant', 'Kasjer', 'Kucharz', 'Starszy Kucharz', 'Manager Zmiany',
                            'Kierownik', 'Manager Restauracji', 'Manager Regionalny', 'Dyrektor', 'Prezes Frytek'],
                  'house_title': 'Właściciel Franczyzy',
                  'shifts': ['smażyłeś frytki na nocnej zmianie', 'obsługiwałeś drive-thru w deszczu',
                             'składałeś Big Maci na czas', 'sprzątałeś salę po imprezie klasowej']},
    'kurier': {'label': 'Kurier', 'base': (120, 230),
               'track': ['Nowy', 'Rowerzysta', 'Kierowca', 'Szybki', 'Ekspres',
                         'Veteran', 'Mistrz Trasy', 'Logistyk', 'Koordynator', 'Król Dróg'],
               'house_title': 'Właściciel Floty',
               'shifts': ['wiozłeś paczki po całym mieście', 'goniłeś z jedzeniem w ulewie',
                          'wdrapywałeś się na 10 piętro bez windy', 'rozwoziłeś prezenty przed świętami']},
    'mechanik': {'label': 'Mechanik', 'base': (130, 250),
                 'track': ['Uczeń', 'Pomocnik', 'Mechanik', 'Starszy Mechanik', 'Specjalista',
                           'Diagnosta', 'Mistrz', 'Ekspert', 'Guru', 'Legenda Warsztatu'],
                 'house_title': 'Właściciel Warsztatu',
                 'shifts': ['wymieniałeś sprzęgło w passacie', 'stawiałeś diagnozę po dźwięku',
                            'robiłeś przegląd przed zimą', 'wyciągałeś auto z rowu']},
    'barman': {'label': 'Barman', 'base': (120, 240),
               'track': ['Praktykant', 'Pomocnik', 'Barman', 'Starszy Barman', 'Miksolog',
                         'Szef Zmiany', 'Manager Baru', 'Właściciel', 'Sieciówka', 'Król Nocy'],
               'house_title': 'Właściciel Sieci Barów',
               'shifts': ['mieszałeś drinki na piątkowej zmianie', 'lewałeś piwo szybciej niż spływało',
                          'słuchałeś żali gościa przy barze', 'robiłeś flair z butelkami']},
    'taksowkarz': {'label': 'Taksówkarz', 'base': (115, 235),
                   'track': ['Kursant', 'Kierowca', 'Taksówkarz', 'Stały', 'Nocny Wilk',
                             'Veteran', 'Złoty Kierowca', 'Dyspozytor', 'Właściciel', 'Król Szos'],
                   'house_title': 'Właściciel Korporacji Taxi',
                   'shifts': ['woziłeś ludzi po nocnym mieście', 'stałeś w korku na Wisłostradzie',
                              'słuchałeś historii życia pasażera', 'goniłeś na lotnisko na czas']},
    'fryzjer': {'label': 'Fryzjer', 'base': (125, 245),
                'track': ['Uczeń', 'Pomocnik', 'Fryzjer', 'Stylista', 'Starszy Stylista',
                          'Kolorysta', 'Master', 'Artysta', 'Trendsetter', 'Ikona Stylu'],
                'house_title': 'Właściciel Sieci Salonów',
                'shifts': ['ciniowałeś fade na zero', 'słuchałeś dram klienta godzinę',
                           'prostowałeś grzywki przed sylwestrem', 'goliłeś brody jak chirurg']},
    'programista': {'label': 'Programista', 'base': (200, 380), 'min_level': 10,
                    'track': ['Stażysta', 'Junior', 'Regular', 'Mid', 'Starszy Mid',
                              'Senior', 'Starszy Senior', 'Tech Lead', 'Architect', 'CTO'],
                    'house_title': 'Założyciel Startupu',
                    'shifts': ['debugowałeś produkcję o 3 w nocy', 'pisałeś testy których nikt nie czyta',
                               'tłumaczyłeś menedżerowi czemu nie działa', 'deployowałeś w piątek']},
    'ochroniarz': {'label': 'Ochroniarz', 'base': (150, 280), 'min_level': 5,
                   'track': ['Nowy', 'Bramkarz', 'Ochroniarz', 'Patrol', 'Starszy',
                             'Koordynator', 'Szef Zmiany', 'Manager', 'Dyrektor', 'Szef Ochrony'],
                   'house_title': 'Właściciel Agencji',
                   'shifts': ['stałeś pod klubem całą noc', 'wyrzucałeś zadymiarza za drzwi',
                              'sprawdzałeś listy gości', 'pilnowałeś parkingu']},
    'kucharz': {'label': 'Kucharz', 'base': (170, 300), 'min_level': 8,
                'track': ['Uczeń', 'Pomocnik', 'Kucharz', 'Starszy Kucharz', 'Sous-Chef',
                          'Szef Kuchni', 'MasterChef', 'Restaurator', 'Gwiazdka Michelin', 'Legenda Gastronomii'],
                'house_title': 'Właściciel Restauracji',
                'shifts': ['ogarniałeś serwis na 200 osób', 'kroiłeś cebulę bez płaczu',
                           'wymyślałeś danie dnia', 'gasiłeś pożar na patelni']},
    'fotograf': {'label': 'Fotograf', 'base': (180, 330), 'min_level': 12,
                 'track': ['Amator', 'Początkujący', 'Fotograf', 'Eventowy', 'Portrecista',
                           'Ślubny', 'Komercyjny', 'Modowy', 'Artysta', 'Legenda Obiektywu'],
                 'house_title': 'Właściciel Agencji',
                 'shifts': ['robiłeś ślub do białego rana', 'łapałeś zachód nad Wisłą',
                            'retuszowałeś sesję całą noc', 'kręciłeś teledysk w garażu']},
    'agent': {'label': 'Agent Nieruchomości', 'base': (220, 420), 'min_level': 18,
              'track': ['Stażysta', 'Agent', 'Pośrednik', 'Starszy Agent', 'Ekspert Rynku',
                        'Top Sprzedawca', 'Manager', 'Dyrektor', 'Partner', 'Król Nieruchomości'],
              'house_title': 'Deweloper',
              'shifts': ['sprzedałeś kawalerkę powyżej ceny', 'oprowadzałeś willę z basenem',
                         'podpisywałeś akt u notariusza', 'negocjowałeś prowizję']},
    'prawnik': {'label': 'Prawnik', 'base': (300, 550), 'min_level': 25,
                'track': ['Aplikant', 'Młodszy', 'Prawnik', 'Radca', 'Starszy Radca',
                          'Partner', 'Starszy Partner', 'Mecenas', 'Adwokat', 'Legenda Sali Sądowej'],
                'house_title': 'Właściciel Kancelarii',
                'shifts': ['wygrałeś sprawę w sądzie', 'pisałeś umowę 40 stron',
                           'broniłeś klienta z urzędu', 'liczyłeś godziny na fakturze']},
    'lekarz': {'label': 'Lekarz', 'base': (350, 650), 'min_level': 30,
               'track': ['Stażysta', 'Rezydent', 'Lekarz', 'Specjalista', 'Starszy Specjalista',
                         'Ordynator', 'Zastępca Dyrektora', 'Dyrektor Szpitala', 'Profesor', 'Legenda Medycyny'],
               'house_title': 'Właściciel Kliniki',
               'shifts': ['miałeś dyżur 24h na SORze', 'szyłeś łuk brwiowy po bójce',
                          'wypisywałeś recepty hurtowo', 'uspokajałeś spanikowanych rodziców']},
    'entrepreneur': {'label': 'Entrepreneur', 'base': (600, 1000), 'hidden': True, 'min_level': 20,
                     'track': ['Marzyciel', 'Freelancer', 'Założyciel', 'Startupowiec', 'Biznesmen',
                               'Inwestor', 'Rekin', 'Magnat', 'Miliarder', 'Legenda Biznesu'],
                     'house_title': 'Ten Jedyny',
                     'shifts': ['podpisywałeś kontrakty na jachcie', 'zwalniałeś zarząd przez telefon',
                                'kupowałeś kolejną firmę z nudów', 'grałeś w golfa z inwestorami']},
}

# old secret key -> new (keeps existing hires working)
JOB_ALIAS = {'ceo': 'entrepreneur'}


def job_title(key: str, idx: int, uid=None) -> str:
    """Per-job rank title. House gets its own legend name at the top rungs."""
    track = (JOBS.get(key) or {}).get('track') or []
    if uid is not None and db.is_house(uid) and idx >= 8:
        return (JOBS.get(key) or {}).get('house_title') or 'Ten Jedyny'
    if 0 <= idx < len(track):
        return track[idx]
    return LADDER[idx][1] if 0 <= idx < len(LADDER) else LADDER[-1][1]


def fame_of(level: int):
    idx, title, mult, nxt = ladder_of(level)
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
        from cogs.gamble import _game_layout
        from cogs.levels import get_user
        lv = get_user(ctx.guild.id, ctx.author.id).get('level', 0)
        lines = []
        for key, j in JOBS.items():
            if j.get('hidden'):
                continue
            lock = t(ctx.guild.id, 'job.need_level', level=j['min_level']) if lv < j.get('min_level', 0) else ''
            lines.append(f"• **{j['label']}** — {j['base'][0]}–{j['base'][1]} / zmianę {lock}")
        await ctx.reply(view=_game_layout(t(ctx.guild.id, 'job.list_title'), '\n'.join(lines)),
                        ephemeral=True)

    @job.command(name='join', description='Zatrudnij się')
    async def job_join(self, ctx, name: str):
        from cogs.levels import get_user
        gid = ctx.guild.id
        key = JOB_ALIAS.get((name or '').lower().strip(), (name or '').lower().strip())
        if key in JOBS and JOBS[key].get('hidden') and not db.is_house(ctx.author.id):
            return await ctx.reply(t(gid, 'job.nope'), ephemeral=True)
        if key not in JOBS:
            return await ctx.reply(t(gid, 'job.nope'), ephemeral=True)
        need = JOBS[key].get('min_level', 0)
        if get_user(gid, ctx.author.id).get('level', 0) < need:
            return await ctx.reply(t(gid, 'job.locked', level=need), ephemeral=True)
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
        from cogs.gamble import _game_layout
        gid = ctx.guild.id
        j = get_job(gid, ctx.author.id)
        jkey = JOB_ALIAS.get(j.get('job'), j.get('job'))
        lv = get_user(gid, ctx.author.id).get('level', 0)
        idx, _, mult, nxt = ladder_of(lv)
        title = job_title(jkey, idx, ctx.author.id) if jkey in JOBS else ladder_of(lv)[1]
        job = JOBS.get(jkey, {}).get('label', t(gid, 'job.none'))
        nxt_txt = t(gid, 'job.next', level=nxt) if nxt else t(gid, 'job.top')
        await ctx.reply(view=_game_layout(t(gid, 'job.my_title', user=ctx.author.display_name),
                                          t(gid, 'job.card', job=job, fame=title, mult=mult,
                                            fans=j.get('fans', 0), level=lv, shifts=j.get('shifts', 0),
                                            nxt=nxt_txt)), ephemeral=True)

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
        key = JOB_ALIAS.get(j.get('job'), j.get('job'))
        if key not in JOBS:
            key = None
        lv = get_user(gid, ctx.author.id).get('level', 0)
        idx, _, mult, nxt = ladder_of(lv)
        title = job_title(key, idx, ctx.author.id) if key else ladder_of(lv)[1]
        shifts = j.get('shifts', 0) if key else 0
        senior = min(shifts // 10 * 0.05, 0.5)
        house_edge = 1.5 if db.is_house(ctx.author.id) else 1.0
        if key:
            job = JOBS[key]
            flavor = random.choice(job['shifts'])
            lo, hi = job['base']
            pay = int(random.randint(lo, hi) * mult * (1 + senior) * house_edge)
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
                conn.execute('UPDATE jobs SET shifts=shifts+1 WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id)))
            try:
                role = await ensure_job_role(ctx.guild, key)
                if role and isinstance(ctx.author, discord.Member) and role not in ctx.author.roles:
                    await ctx.author.add_roles(role, reason='job role')
            except Exception:
                pass
            msg = t(gid, 'eco.work_done', job=f"{job['label']}: {flavor}", pay=pay)
            if extra.strip():
                msg += '\n' + extra.strip()
            if senior:
                msg += '\n' + t(gid, 'job.senior', pct=int(senior * 100))
            try:
                from cogs.gamble import bal as _bal, _game_layout
                msg += '\n' + t(gid, 'eco.balance_line', cash=_bal(gid, ctx.author.id)['cash'])
                return await ctx.reply(view=_game_layout(t(gid, 'eco.work_title', job=job['label']), msg))
            except Exception:
                pass
            return await ctx.reply(msg)
        # no job: day labor
        jobs_txt = t(gid, 'eco.jobs').split('|')
        job = random.choice(jobs_txt).strip()
        pay = random.randint(100, 300)
        set_cash(gid, ctx.author.id, b['cash'] + pay)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE eco SET last_work=? WHERE guild_id=? AND user_id=?',
                         (now, str(gid), str(ctx.author.id)))
        try:
            from cogs.gamble import bal as _bal2, _game_layout as _gl2
            extra2 = '\n' + t(gid, 'eco.balance_line', cash=_bal2(gid, ctx.author.id)['cash'])
        except Exception:
            extra2 = ''
            _gl2 = None
        if _gl2:
            return await ctx.reply(view=_gl2(t(gid, 'eco.work_title', job=t(gid, 'job.none')),
                                             t(gid, 'eco.work_done', job=job, pay=pay) + extra2))
        await ctx.reply(t(gid, 'eco.work_done', job=job, pay=pay) + extra2)


async def setup(bot):
    await bot.add_cog(Jobs(bot))

"""Event Wordle: Babka drops a hint round every X minutes, first correct guess wins."""
import asyncio
import random
import time
import unicodedata

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.embeds import ok, foot
from utils.checks import staff_or

# (word, category, hint)
WORDS = [
    # jedzenie
    ('bigos', 'JEDZENIE', 'Musi postać trzy dni'), ('rosół', 'JEDZENIE', 'Niedzielny klasyk'),
    ('pierogi', 'JEDZENIE', 'Ruskie albo z mięsem'), ('schabowy', 'JEDZENIE', 'Z kapustą i ziemniakami'),
    ('sernik', 'JEDZENIE', 'Na zimno albo na ciepło'), ('pączek', 'JEDZENIE', 'Tłusty czwartek'),
    ('kiełbasa', 'JEDZENIE', 'Z grilla najlepsza'), ('ogórek', 'JEDZENIE', 'Kiszony albo małosolny'),
    ('jabłko', 'JEDZENIE', 'Jedno dziennie'), ('marchew', 'JEDZENIE', 'Dobra na wzrok'),
    # zwierzęta
    ('kot', 'ZWIERZĘTA', 'Mruczy'), ('pies', 'ZWIERZĘTA', 'Najlepszy przyjaciel'),
    ('chomik', 'ZWIERZĘTA', 'Chomikuje jedzenie w policzkach'), ('papuga', 'ZWIERZĘTA', 'Powtarza wszystko'),
    ('żółw', 'ZWIERZĘTA', 'Powolny ale długowieczny'), ('niedźwiedź', 'ZWIERZĘTA', 'Śpi zimą'),
    ('wilk', 'ZWIERZĘTA', 'Wyje do księżyca'), ('lis', 'ZWIERZĘTA', 'Chytry rudy'),
    ('sowa', 'ZWIERZĘTA', 'Mądra nocna'), ('królik', 'ZWIERZĘTA', 'Długie uszy'),
    # dom
    ('kanapa', 'DOM', 'Do siedzenia przed TV'), ('lodówka', 'DOM', 'Zimno w środku'),
    ('poduszka', 'DOM', 'Do spania'), ('telewizor', 'DOM', 'Babka ogląda seriale'),
    ('kuchenka', 'DOM', 'Tu stoi rosół'), ('lustro', 'DOM', 'Powiedz kto najpiękniejszy'),
    ('dywan', 'DOM', 'Leży na podłodze'), ('lampa', 'DOM', 'Daje światło'),
    ('fotel', 'DOM', 'U Babci najlepszy'), ('okno', 'DOM', 'Widać przez nie dwór'),
    # szkoła
    ('tablica', 'SZKOŁA', 'Pisze się po niej kredą'), ('plecak', 'SZKOŁA', 'Nosi książki'),
    ('długopis', 'SZKOŁA', 'Lepszy niż ołówek na sprawdzian'), ('zeszyt', 'SZKOŁA', 'W kratkę albo w linie'),
    ('dzwonek', 'SZKOŁA', 'Najlepszy dźwięk'), ('nauczyciel', 'SZKOŁA', 'Sprawdza obecność'),
    ('przerwa', 'SZKOŁA', '10 minut wolności'), ('ocena', 'SZKOŁA', 'Od 1 do 6'),
    ('klasówka', 'SZKOŁA', 'Strach każdego ucznia'), ('podręcznik', 'SZKOŁA', 'Ciężki jak cegła'),
    # sport
    ('piłka', 'SPORT', 'Nożna, ręczna, siatkowa'), ('bramka', 'SPORT', 'Trzeba w nią trafić'),
    ('mecz', 'SPORT', 'Dwie połowy'), ('trener', 'SPORT', 'Krzyczy z ławki'),
    ('stadion', 'SPORT', 'Tysiące kibiców'), ('rower', 'SPORT', 'Dwa kółka'),
    ('basen', 'SPORT', 'Chlor i ręcznik'), ('siatkówka', 'SPORT', 'Gra nad siatką'),
    ('bieganie', 'SPORT', 'Rano najgorsze'), ('medal', 'SPORT', 'Złoty, srebrny, brązowy'),
    # natura
    ('drzewo', 'NATURA', 'Ma liście albo igły'), ('rzeka', 'NATURA', 'Płynie do morza'),
    ('góra', 'NATURA', 'Trzeba na nią wejść'), ('las', 'NATURA', 'Pełen grzybów jesienią'),
    ('kwiat', 'NATURA', 'Pachnie ładnie'), ('deszcz', 'NATURA', 'Weź parasol'),
    ('śnieg', 'NATURA', 'Zimą biało'), ('jezioro', 'NATURA', 'Mazury pełne'),
    ('ptak', 'NATURA', 'Śpiewa rano'), ('grzyb', 'NATURA', 'Babka zna wszystkie'),
    # miasto
    ('autobus', 'MIASTO', 'Spóźnia się zawsze'), ('kino', 'MIASTO', 'Popcorn obowiązkowy'),
    ('sklep', 'MIASTO', 'W niedzielę zamknięte'), ('ulica', 'MIASTO', 'Jeżdżą po niej auta'),
    ('most', 'MIASTO', 'Przez rzekę'), ('park', 'MIASTO', 'Ławki i gołębie'),
    ('szkoła', 'MIASTO', 'Budynek z dzwonkiem'), ('apteka', 'MIASTO', 'Po leki'),
    ('rondo', 'MIASTO', 'Kierowcy się gubią'), ('latarnia', 'MIASTO', 'Świeci nocą'),
    # tech
    ('komputer', 'TECH', 'Siedzisz przed nim'), ('telefon', 'TECH', 'Zawsze w ręce'),
    ('internet', 'TECH', 'Kiedyś piszczał'), ('klawiatura', 'TECH', 'Klikasz w nią'),
    ('myszka', 'TECH', 'Nie ta z serem'), ('ekran', 'TECH', 'Patrzysz w niego za długo'),
    ('słuchawki', 'TECH', 'Muzyka prosto do uszu'), ('konsola', 'TECH', 'Granie na kanapie'),
    ('aplikacja', 'TECH', 'Ściągasz ze sklepu'), ('hasło', 'TECH', 'Nigdy 1234'),
    # ubrania
    ('kapcie', 'UBRANIA', 'Babka każe założyć'), ('czapka', 'UBRANIA', 'Na zimę obowiązkowa'),
    ('sweter', 'UBRANIA', 'Babka robi na drutach'), ('buty', 'UBRANIA', 'Zdejmij w przedpokoju'),
    ('kurtka', 'UBRANIA', 'Jesienna albo zimowa'), ('spodnie', 'UBRANIA', 'Dziurawe modne'),
    ('koszulka', 'UBRANIA', 'Z nadrukiem'), ('szalik', 'UBRANIA', 'Owijasz szyję'),
    ('rękawiczki', 'UBRANIA', 'Żeby łapki nie marzły'), ('sukienka', 'UBRANIA', 'Na wesele'),
    # muzyka
    ('gitara', 'MUZYKA', 'Sześć strun'), ('piosenka', 'MUZYKA', 'Nuci się pod nosem'),
    ('koncert', 'MUZYKA', 'Głośno i na żywo'), ('perkusja', 'MUZYKA', 'Bum cyk bum'),
    ('pianino', 'MUZYKA', 'Czarno-białe klawisze'), ('radio', 'MUZYKA', 'Gra w kuchni u babci'),
    ('taniec', 'MUZYKA', 'Na weselu obowiązkowy'), ('chór', 'MUZYKA', 'Śpiewają razem'),
    ('skrzypce', 'MUZYKA', 'Smyczek potrzebny'), ('disco', 'MUZYKA', 'Polo nie umiera'),
    # ciało
    ('ręka', 'CIAŁO', 'Pięć palców'), ('noga', 'CIAŁO', 'Do chodzenia'),
    ('głowa', 'CIAŁO', 'Myśli w niej'), ('oko', 'CIAŁO', 'Widzisz nim memy'),
    ('ucho', 'CIAŁO', 'Słyszysz bas'), ('nos', 'CIAŁO', 'Wącha bigos'),
    ('włosy', 'CIAŁO', 'Do fryzjera z nimi'), ('ząb', 'CIAŁO', 'Myj dwa razy dziennie'),
    ('serce', 'CIAŁO', 'Bije szybciej na sprawdzianie'), ('palec', 'CIAŁO', 'Klikasz nim'),
    # pogoda
    ('słońce', 'POGODA', 'Opalaj się z głową'), ('wiatr', 'POGODA', 'Czapka z głowy leci'),
    ('burza', 'POGODA', 'Grzmi i błyska'), ('mgła', 'POGODA', 'Nic nie widać'),
    ('tęcza', 'POGODA', 'Po deszczu'), ('mróz', 'POGODA', 'Szyby w szronie'),
    ('upał', 'POGODA', 'Lody ratują'), ('grad', 'POGODA', 'Lodowe kulki z nieba'),
    ('chmura', 'POGODA', 'Płynie po niebie'), ('rosa', 'POGODA', 'Rano na trawie'),
    # zawody
    ('lekarz', 'ZAWODY', 'Biały kitel'), ('strażak', 'ZAWODY', 'Gasi pożary'),
    ('policjant', 'ZAWODY', 'Mandat za prędkość'), ('nauczyciel', 'ZAWODY', 'Sprawdza obecność'),
    ('kucharz', 'ZAWODY', 'Rządzi w kuchni'), ('mechanik', 'ZAWODY', 'Naprawi auto'),
    ('fryzjer', 'ZAWODY', 'Krótko czy długo?'), ('rolnik', 'ZAWODY', 'Traktor i pole'),
    ('programista', 'ZAWODY', 'Kawa i kod'), ('ksiądz', 'ZAWODY', 'Niedzielna msza'),
    # gry
    ('szachy', 'GRY', 'Król i królowa'), ('karty', 'GRY', 'Poker, wojna, makao'),
    ('kości', 'GRY', 'Rzuć dwiema'), ('puzzle', 'GRY', 'Tysiąc elementów'),
    ('piłka', 'GRY', 'Też na podwórku'), ('chowanego', 'GRY', 'Raz, dwa, trzy... kryj się'),
    ('berka', 'GRY', 'Kto goni'), ('klocki', 'GRY', 'Boli jak się stanie'),
    ('gra', 'GRY', 'Za trudna na ten serwer'), ('wyścigi', 'GRY', 'Kto pierwszy na mecie'),
]

WINDOW = 60


EXTRA_FOLD = str.maketrans({'ł': 'l', 'Ł': 'l', 'ø': 'o', 'Ø': 'o', 'đ': 'd', 'Đ': 'd',
                            'ß': 'ss', 'æ': 'ae', 'Æ': 'ae', 'œ': 'oe', 'Œ': 'oe', 'ŋ': 'n'})


def flat(s: str) -> str:
    s = unicodedata.normalize('NFKD', (s or '').lower()).translate(EXTRA_FOLD)
    return ''.join(c for c in s if not unicodedata.combining(c)).strip()


def masked(word: str, revealed: set) -> str:
    return ' '.join(ch if flat(ch) in revealed or ch == ' ' else '_' for ch in word)


class Wordle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active: dict = {}  # gid -> round info
        self.round_loop.start()

    def cog_unload(self):
        self.round_loop.cancel()

    def _cfg(self, gid):
        with db.conn_ctx() as conn:
            return conn.execute('SELECT * FROM wordle_cfg WHERE guild_id=?', (str(gid),)).fetchone()

    @tasks.loop(minutes=1)
    async def round_loop(self):
        await self.bot.wait_until_ready()
        now = time.time()
        for guild in self.bot.guilds:
            try:
                cfg = self._cfg(guild.id)
                if not cfg or not cfg['channel_id']:
                    continue
                gid = str(guild.id)
                if gid in self.active:
                    continue
                nxt = cfg['next_at'] or 0
                if now < nxt:
                    continue
                minutes = cfg['minutes'] or 30
                with db.conn_ctx() as conn:
                    conn.execute('UPDATE wordle_cfg SET next_at=? WHERE guild_id=?',
                                 (now + minutes * 60, gid))
                await self._round(guild, cfg)
            except Exception:
                continue

    async def _round(self, guild: discord.Guild, cfg):
        ch = guild.get_channel(int(cfg['channel_id']))
        if not ch:
            return
        word, cat, hint = random.choice(WORDS)
        gid = str(guild.id)
        info = {'word': word, 'end': time.time() + WINDOW, 'winner': None, 'msg_id': None}
        self.active[gid] = info
        try:
            msg = await ch.send(embed=ok(
                t(guild.id, 'wd.round', cat=cat, mask=masked(word, set()), hint=hint, secs=WINDOW)))
            info['msg_id'] = msg.id
        except Exception:
            self.active.pop(gid, None)
            return
        await asyncio.sleep(WINDOW)
        info = self.active.pop(gid, None)
        if not info or info['winner']:
            return
        try:
            await ch.send(embed=ok(t(guild.id, 'wd.timeout', word=word)))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        gid = str(message.guild.id)
        info = self.active.get(gid)
        if not info or info['winner'] or str(message.channel.id) != str((self._cfg(gid) or {}).get('channel_id') or ''):
            return
        if time.time() > info['end']:
            return
        if flat(message.content) == flat(info['word']):
            info['winner'] = message.author.id
            xp = random.randint(100, 300)
            cash = random.randint(100, 400)
            from cogs.levels import add_xp
            from cogs.gamble import bal, set_cash
            try:
                add_xp(message.guild.id, message.author.id, xp)
                b = bal(message.guild.id, message.author.id)
                set_cash(message.guild.id, message.author.id, b['cash'] + cash)
            except Exception:
                pass
            with db.conn_ctx() as conn:
                conn.execute('''INSERT INTO wordle_wins (guild_id, user_id, wins) VALUES (?,?,1)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1''',
                             (gid, str(message.author.id)))
            try:
                await message.channel.send(embed=ok(t(
                    message.guild.id, 'wd.win', user=message.author.mention,
                    word=info['word'], xp=xp, cash=cash)))
            except Exception:
                pass
            self.active.pop(gid, None)

    @commands.hybrid_group(name='wordle', description='Zgadywanka babki')
    async def wordle(self, ctx):
        await ctx.reply('/wordle set / off / now / top', ephemeral=True)

    @wordle.command(name='set', description='Ustaw kanał i co ile minut')
    @staff_or('manage_guild')
    async def set_ch(self, ctx, channel: discord.TextChannel, minutes: int = 30):
        minutes = max(5, min(minutes, 180))
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO wordle_cfg (guild_id, channel_id, minutes, next_at)'
                         ' VALUES (?,?,?,?)', (str(ctx.guild.id), str(channel.id), minutes, 0))
        await ctx.reply(t(ctx.guild.id, 'wd.set', ch=channel.mention, m=minutes), ephemeral=True)

    @wordle.command(name='off', description='Wyłącz zgadywankę')
    @staff_or('manage_guild')
    async def off(self, ctx):
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM wordle_cfg WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'wd.off'), ephemeral=True)

    @wordle.command(name='now', description='Runda od razu')
    @staff_or('manage_guild')
    async def now(self, ctx):
        cfg = self._cfg(ctx.guild.id)
        if not cfg or not cfg['channel_id']:
            return await ctx.reply(t(ctx.guild.id, 'wd.none'), ephemeral=True)
        if str(ctx.guild.id) in self.active:
            return await ctx.reply(t(ctx.guild.id, 'wd.running'), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE wordle_cfg SET next_at=0 WHERE guild_id=?', (str(ctx.guild.id),))
        await ctx.reply(t(ctx.guild.id, 'wd.coming'), ephemeral=True)

    @wordle.command(name='top', description='Najlepsi zgadywacze')
    async def top(self, ctx):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT user_id, wins FROM wordle_wins WHERE guild_id=? ORDER BY wins DESC LIMIT 10',
                                (str(ctx.guild.id),)).fetchall()
        if not rows:
            return await ctx.reply(t(ctx.guild.id, 'wd.empty'), ephemeral=True)
        lines = []
        for i, r in enumerate(rows, start=1):
            m = ctx.guild.get_member(int(r['user_id']))
            lines.append(f"**#{i}** {(m.display_name if m else '?')} — **{r['wins']}**")
        await ctx.reply(embed=ok('\n'.join(lines)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Wordle(bot))

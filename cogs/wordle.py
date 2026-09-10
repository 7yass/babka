"""Polish Wordle: daily 5-letter word, 6 tries. Win = XP + cash."""
import datetime
import json
import random

import discord
from discord.ext import commands

import database as db
from lang import t
from utils.embeds import ok

WORDS = """adres alarm album aleja arbuz atlas babka balon banan baton beret bigos bilet biuro
blask broda burza chory cichy cwany deska domek drink dymek dywan dzwon elita ekran farba
filet firma flaga fotel front glina gniew gotyk guzik honor igloo ikona imbir jutro kabel
kciuk klasa kokos kolor komar komin koper kotek kozak krzew kufel medal melon metal metro
mleko model morze motyl napis nauka numer obiad obraz ocean omlet oliwa opera orkan palec
palma pasta piano pilot pirat piana plama pobyt pomoc ponad punkt rower rynek rzecz rzeka
samba sanki serek sklep skwer sosna sarna sport strop sufit szafa sznur szopa tabor teatr
temat torba tupet trawa trema trakt tunel ulica uwaga wagon wanna wazon welon wiatr wideo
winda worek wydra zamek zegar zimno zapas""".split()

MAX_TRIES = 6


def today_word() -> str:
    seed = int(datetime.date.today().strftime('%Y%m%d'))
    return WORDS[seed % len(WORDS)]


def score(word: str, guess: str) -> str:
    out = ['⬛'] * 5
    rest = list(word)
    for i in range(5):
        if guess[i] == word[i]:
            out[i] = '🟩'
            rest[i] = None
    for i in range(5):
        if out[i] == '⬛' and guess[i] in rest:
            out[i] = '🟨'
            rest.remove(guess[i])
    return ''.join(out)


class Wordle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name='wordle', description='Dzienne słowo (PL)')
    async def wordle(self, ctx, guess: str = None):
        gid = str(ctx.guild.id)
        uid = str(ctx.author.id)
        day = datetime.date.today().isoformat()
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT * FROM wordle WHERE guild_id=? AND user_id=? AND day=?',
                               (gid, uid, day)).fetchone()
            if not row:
                conn.execute('INSERT INTO wordle (guild_id, user_id, day, word, guesses, done) VALUES (?,?,?,?,?,0)',
                             (gid, uid, day, today_word(), '[]'))
                row = conn.execute('SELECT * FROM wordle WHERE guild_id=? AND user_id=? AND day=?',
                                   (gid, uid, day)).fetchone()
            d = dict(row)
        guesses = json.loads(d.get('guesses') or '[]')
        if guess is None:
            if d['done']:
                return await ctx.reply(embed=ok(t(ctx.guild.id, 'wd.done_today', word=d['word'])), ephemeral=True)
            if not guesses:
                return await ctx.reply(embed=ok(t(ctx.guild.id, 'wd.start')), ephemeral=True)
            return await ctx.reply(embed=ok(self._board(guesses)), ephemeral=True)
        guess = (guess or '').lower().strip()
        if d['done']:
            return await ctx.reply(embed=ok(t(ctx.guild.id, 'wd.done_today', word=d['word'])), ephemeral=True)
        if len(guess) != 5 or not guess.isalpha():
            return await ctx.reply(t(ctx.guild.id, 'wd.bad'), ephemeral=True)
        if guess in [g for g, _ in guesses]:
            return await ctx.reply(t(ctx.guild.id, 'wd.repeat'), ephemeral=True)
        mark = score(d['word'], guess)
        guesses.append([guess, mark])
        done = guess == d['word'] or len(guesses) >= MAX_TRIES
        with db.conn_ctx() as conn:
            conn.execute('UPDATE wordle SET guesses=?, done=? WHERE guild_id=? AND user_id=? AND day=?',
                         (json.dumps(guesses), 1 if done else 0, gid, uid, day))
        board = self._board(guesses)
        if guess == d['word']:
            tries = len(guesses)
            xp = 400 - tries * 40
            cash = 300 + (MAX_TRIES - tries) * 100
            from cogs.levels import add_xp
            from cogs.gamble import bal, set_cash
            add_xp(ctx.guild.id, ctx.author.id, xp)
            b = bal(ctx.guild.id, ctx.author.id)
            set_cash(ctx.guild.id, ctx.author.id, b['cash'] + cash)
            board += '\n' + t(ctx.guild.id, 'wd.win', xp=xp, cash=cash)
        elif done:
            board += '\n' + t(ctx.guild.id, 'wd.lose', word=d['word'])
        await ctx.reply(embed=ok(board))

    @staticmethod
    def _board(guesses) -> str:
        lines = [f'`{g}` {m}' for g, m in guesses]
        while len(lines) < MAX_TRIES:
            lines.append('`_ _ _ _ _`')
        return '\n'.join(lines)


async def setup(bot):
    await bot.add_cog(Wordle(bot))

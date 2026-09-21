"""Fake stock market: random-walking joke stocks, buy low sell high.
`.stocks` board, `.stockbuy <sym> <cash>`, `.stocksell <sym> <qty|all>`,
`.portfolio`. Prices drift every trade + every 15 min in background."""
import random
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.cards import short as cshort

# symbol -> (name, start price)
STOCKS = {
    'BABKA': ('Babka Industries', 500),
    'PIEROG': ('Pierogi Consolidated', 200),
    'ROSOL': ('Rosol Energy', 120),
    'KLAPEK': ('Klapek Enterprises', 80),
    'MALUCH': ('Maluch Motors', 300),
    'BIGOS': ('Bigos Holdings', 60),
}
TICK_CD = 900  # background drift every 15 min
HIST_KEEP = 24


def _now():
    return int(time.time())


def _drift(price: int) -> int:
    change = random.gauss(0, 0.06)
    if random.random() < 0.03:  # babka sneezes: crash or moon
        change += random.choice([-0.35, 0.45])
    return max(5, int(price * (1 + change)))


def _ensure(gid):
    with db.conn_ctx() as conn:
        for sym, (_, start) in STOCKS.items():
            conn.execute('INSERT OR IGNORE INTO stocks (guild_id, symbol, price, updated_at) '
                         'VALUES (?,?,?,?)', (str(gid), sym, start, _now()))


def _prices(gid) -> dict:
    _ensure(gid)
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT symbol, price FROM stocks WHERE guild_id=?',
                            (str(gid),)).fetchall()
    return {r['symbol']: r['price'] for r in rows}


def _tick_symbol(gid, sym):
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT price FROM stocks WHERE guild_id=? AND symbol=?',
                           (str(gid), sym)).fetchone()
        if not row:
            return
        new = _drift(row['price'])
        conn.execute('UPDATE stocks SET price=?, updated_at=? WHERE guild_id=? AND symbol=?',
                     (new, _now(), str(gid), sym))
        conn.execute('INSERT INTO stock_hist (guild_id, symbol, price, ts) VALUES (?,?,?,?)',
                     (str(gid), sym, new, _now()))
        conn.execute('DELETE FROM stock_hist WHERE guild_id=? AND symbol=? AND ts NOT IN '
                     '(SELECT ts FROM stock_hist WHERE guild_id=? AND symbol=? '
                     'ORDER BY ts DESC LIMIT ?)',
                     (str(gid), sym, str(gid), sym, HIST_KEEP))


def _spark(gid, sym) -> str:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT price FROM stock_hist WHERE guild_id=? AND symbol=? '
                            'ORDER BY ts DESC LIMIT 12', (str(gid), sym)).fetchall()
    if len(rows) < 2:
        return ''
    pts = [r['price'] for r in reversed(rows)]
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1
    bars = '▁▂▃▄▅▆▇█'
    return ''.join(bars[min(7, int((p - lo) / span * 7))] for p in pts)


def _pct(gid, sym) -> float:
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT price FROM stock_hist WHERE guild_id=? AND symbol=? '
                            'ORDER BY ts ASC LIMIT 1', (str(gid), sym)).fetchone()
        first = rows['price'] if rows else None
    cur = _prices(gid).get(sym, 0)
    if not first or not cur:
        return 0.0
    return (cur - first) / first * 100


class Stocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticker.start()

    def cog_unload(self):
        self.ticker.cancel()

    @tasks.loop(seconds=TICK_CD)
    async def ticker(self):
        try:
            with db.conn_ctx() as conn:
                gids = [r['guild_id'] for r in conn.execute(
                    'SELECT DISTINCT guild_id FROM stocks').fetchall()]
            for g in gids:
                for sym in STOCKS:
                    _tick_symbol(g, sym)
        except Exception:
            pass

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    def _board(self, gid) -> str:
        from cogs.gamble import _game_layout
        prices = _prices(gid)
        lines = []
        for sym, (name, _) in STOCKS.items():
            p = prices.get(sym, 0)
            ch = _pct(gid, sym)
            arrow = '▲' if ch >= 0 else '▼'
            lines.append(f"• **{sym}** ({name}) — {cshort(p)} {arrow}{abs(ch):.1f}% {_spark(gid, sym)}")
        return _game_layout(t(gid, 'eco.stocks_title'), '\n'.join(lines))

    @commands.command(name='stocks', description='Giełda babki')
    async def stocks(self, ctx):
        for sym in STOCKS:
            _tick_symbol(ctx.guild.id, sym)
        await ctx.reply(view=self._board(ctx.guild.id), ephemeral=True)

    @commands.command(name='stockbuy', description='Kup akcje')
    async def stockbuy(self, ctx, symbol: str, cash: int):
        from cogs.gamble import bal, set_cash, add_cash
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stock_no', syms='/'.join(STOCKS)), ephemeral=True)
        if cash <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        _tick_symbol(gid, sym)
        price = _prices(gid)[sym]
        qty = cash // price
        if qty <= 0:
            return await ctx.reply(t(gid, 'eco.stock_poor', price=cshort(price)), ephemeral=True)
        cost = qty * price
        b = bal(gid, ctx.author.id)
        if cost > b['cash']:
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        add_cash(gid, ctx.author.id, -cost)
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR IGNORE INTO portfolio (guild_id, user_id, symbol, qty, spent) '
                         'VALUES (?,?,?,0,0)', (str(gid), str(ctx.author.id), sym))
            conn.execute('UPDATE portfolio SET qty=qty+?, spent=spent+? '
                         'WHERE guild_id=? AND user_id=? AND symbol=?',
                         (qty, cost, str(gid), str(ctx.author.id), sym))
        from cogs.gamble import _game_layout
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.stock_bought_title'),
            t(gid, 'eco.stock_bought', qty=qty, sym=sym, cost=cshort(cost))), ephemeral=True)

    @commands.command(name='stocksell', description='Sprzedaj akcje')
    async def stocksell(self, ctx, symbol: str, qty: str = 'all'):
        from cogs.gamble import bal, set_cash, add_cash
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stock_no', syms='/'.join(STOCKS)), ephemeral=True)
        with db.conn_ctx() as conn:
            pos = conn.execute('SELECT qty, spent FROM portfolio WHERE guild_id=? AND user_id=? AND symbol=?',
                               (str(gid), str(ctx.author.id), sym)).fetchone()
        if not pos or not pos['qty']:
            return await ctx.reply(t(gid, 'eco.stock_none', sym=sym), ephemeral=True)
        _tick_symbol(gid, sym)
        price = _prices(gid)[sym]
        n = pos['qty'] if str(qty).lower() == 'all' else max(0, int(qty or 0))
        n = min(n, pos['qty'])
        if n <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        gain = n * price
        avg = (pos['spent'] or 0) / max(1, pos['qty'])
        pnl = gain - int(avg * n)
        b = bal(gid, ctx.author.id)
        add_cash(gid, ctx.author.id, gain)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE portfolio SET qty=qty-?, spent=spent-? '
                         'WHERE guild_id=? AND user_id=? AND symbol=?',
                         (n, int(avg * n), str(gid), str(ctx.author.id), sym))
        from cogs.gamble import _game_layout
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.stock_sold_title'),
            t(gid, 'eco.stock_sold', qty=n, sym=sym, gain=cshort(gain), pnl=cshort(pnl))),
            ephemeral=True)

    @commands.command(name='portfolio', description='Twoje akcje', aliases=['portfel'])
    async def portfolio(self, ctx):
        gid = ctx.guild.id
        prices = _prices(gid)
        with db.conn_ctx() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT symbol, qty, spent FROM portfolio WHERE guild_id=? AND user_id=? AND qty>0',
                (str(gid), str(ctx.author.id))).fetchall()]
        if not rows:
            return await ctx.reply(t(gid, 'eco.port_empty'), ephemeral=True)
        from cogs.gamble import _game_layout
        lines, total, invested = [], 0, 0
        for r in rows:
            val = r['qty'] * prices.get(r['symbol'], 0)
            pnl = val - (r['spent'] or 0)
            total += val
            invested += r['spent'] or 0
            arrow = '▲' if pnl >= 0 else '▼'
            lines.append(f"• **{r['symbol']}** x{r['qty']} — {cshort(val)} ({arrow}{cshort(abs(pnl))})")
        lines.append(t(gid, 'eco.port_total', val=cshort(total), pnl=cshort(total - invested)))
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.port_title', user=ctx.author.display_name), '\n'.join(lines)),
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Stocks(bot))

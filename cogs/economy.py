"""The economy's read side: statements, net worth, flow, treasury.

Before this cog, the only things a player could see about their money were
"how much cash do I have" and a leaderboard of the same number. That was
never the truth: a player sitting on a UR card and a fat bank was richer
than one with the same cash and nothing else, and nothing in the bot could
tell you where your money came from.

Everything here is answered from the ledger (services/ledger.py), so the
numbers are measured rather than guessed.
"""
import discord
from discord.ext import commands

import database as db
from lang import t
from services import ledger, streaks, treasury
from utils.ui import ACCENT, divider, meter, panel, stat


class Economy(commands.Cog):
    """Statements, net worth, economy health, guild treasury."""

    # ---- .ledger : where the money came from ------------------------
    @commands.hybrid_command(name='ledger', aliases=['statement', 'stmt'])
    async def ledger_cmd(self, ctx, days: int = 30):
        """Where your money came from, and where it went."""
        gid = ctx.guild.id
        days = max(1, min(int(days or 30), 365))
        st = ledger.statement(gid, ctx.author.id, days)
        if not st['moves']:
            body = t(gid, 'eco.ledger_empty', days=days)
            return await ctx.reply(view=panel(t(gid, 'eco.ledger_title'),
                                              body, accent=ACCENT['muted']),
                                    ephemeral=True)
        net = st['net']
        accent = ACCENT['good'] if net >= 0 else ACCENT['bad']
        body = (stat(t(gid, 'eco.ledger_in'), f'{st["inflow"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.ledger_out'), f'{st["outflow"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.ledger_net'), f'{net:+,}'.replace(',', ' ')
                              .replace('+', '+').replace('-', '-'))
                + '\n' + stat(t(gid, 'eco.ledger_moves'), st['moves'])
                + '\n' + divider(t(gid, 'eco.ledger_reasons')))
        for r in st['reasons'][:6]:
            body += f"\n`{r['reason']}` **{r['inflow'] - r['outflow']:+,}**".replace(',', ' ') \
                + f'  -# {r["n"]}x'
        await ctx.reply(view=panel(t(gid, 'eco.ledger_title'), body, accent=accent),
                        ephemeral=True)

    # ---- .worth : everything you own, in one number ------------------
    @commands.hybrid_command(name='worth', aliases=['networth'])
    async def worth(self, ctx, member: discord.Member = None):
        """Your net worth: cash, bank, stocks, cards, tickets."""
        gid = ctx.guild.id
        member = member or ctx.author
        w = ledger.net_worth(gid, member.id)
        body = (stat(t(gid, 'eco.worth_cash'), f'{w["cash"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.worth_bank'), f'{w["bank"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.worth_stocks'), f'{w["stocks"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.worth_cards'), f'{w["cards"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.worth_tickets'), f'{w["tickets"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.worth_dust'), f'{w["dust"]:,}'.replace(',', ' '))
                + '\n' + divider()
                + '\n' + stat(t(gid, 'eco.worth_total'), f'**{w["total"]:,}**'.replace(',', ' ')))
        await ctx.reply(view=panel(t(gid, 'eco.worth_title', user=member.display_name),
                                   body, accent=ACCENT['default']),
                        ephemeral=True)

    # ---- .flow : is the economy healthy? -----------------------------
    @commands.hybrid_command(name='flow', aliases=['economy'])
    async def flow(self, ctx, days: int = 30):
        """Guild economy health: money created vs money destroyed."""
        gid = ctx.guild.id
        days = max(1, min(int(days or 30), 365))
        f = ledger.flow(gid, days)
        if not f['moves']:
            body = t(gid, 'eco.flow_empty', days=days)
            return await ctx.reply(view=panel(t(gid, 'eco.flow_title'), body,
                                              accent=ACCENT['muted']))
        ratio = f['ratio']
        if ratio == float('inf'):
            verdict, accent = t(gid, 'eco.flow_inflating'), ACCENT['bad']
        elif ratio > 1.35:
            verdict, accent = t(gid, 'eco.flow_inflating'), ACCENT['gold']
        elif ratio < 0.7:
            verdict, accent = t(gid, 'eco.flow_deflating'), ACCENT['gold']
        else:
            verdict, accent = t(gid, 'eco.flow_healthy'), ACCENT['good']
        body = (stat(t(gid, 'eco.flow_in'), f'{f["inflow"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.flow_out'), f'{f["outflow"]:,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.flow_net'), f'{f["net"]:+,}'.replace(',', ' '))
                + '\n' + stat(t(gid, 'eco.flow_users'), f'{f["users"]}')
                + '\n' + divider(t(gid, 'eco.flow_top')))
        for r in f['reasons'][:6]:
            body += (f"\n`{r['reason']}` **{int(r['inflow'] or 0) - int(r['outflow'] or 0):+,}**"
                     .replace(',', ' '))
        body += f'\n\n**{verdict}**'
        await ctx.reply(view=panel(t(gid, 'eco.flow_title'), body, accent=accent))

    # ---- .streak : your chain ---------------------------------------
    @commands.hybrid_command(name='streak', aliases=['streaks'])
    async def streak(self, ctx, member: discord.Member = None):
        """Your daily streak and what it is paying you."""
        gid = ctx.guild.id
        member = member or ctx.author
        s = streaks.get(gid, member.id, 'daily')
        body = (stat(t(gid, 'eco.streak_current'), s['count'])
                + '\n' + stat(t(gid, 'eco.streak_best'), s['best'])
                + '\n' + stat(t(gid, 'eco.streak_mult'), f'**{s["mult"]:.2f}x**')
                + '\n' + divider()
                + '\n' + meter(min(s['count'], 60), 60, t(gid, 'eco.streak_chain'), width=16))
        await ctx.reply(view=panel(t(gid, 'eco.streak_title', user=member.display_name),
                                   body, accent=ACCENT['gold']),
                        ephemeral=True)

    # ---- .treasury : the guild's own progression ---------------------
    @commands.group(name='treasury', aliases=['guild'], invoke_without_command=True)
    async def treasury_cmd(self, ctx):
        """The guild pool and the upgrades it buys."""
        if ctx.invoked_subcommand is not None:
            return
        gid = ctx.guild.id
        s = treasury.summary(gid)
        body = stat(t(gid, 'eco.treasury_pool'), f'**{s["balance"]:,}**'.replace(',', ' '))
        body += '\n' + divider(t(gid, 'eco.treasury_levels'))
        for key, spec in s['specs'].items():
            lv = s['levels'].get(key, 0)
            nxt = s['next'].get(key)
            if nxt is None:
                body += f"\n`{key}` **{lv}/{spec['max']}**  -# {t(gid, 'eco.treasury_maxed')}"
            else:
                body += f"\n`{key}` **{lv}/{spec['max']}**  -# next {nxt:,}".replace(',', ' ')
        body += '\n' + divider(t(gid, 'eco.treasury_bonus'))
        for eff, val in s['bonuses'].items():
            if not val:
                continue
            shown = f'+{val:.0%}' if val < 1 else f'+{val:.1f}x'
            body += f"\n`{eff}` **{shown}**"
        body += f"\n\n{t(gid, 'eco.treasury_hint')}"
        await ctx.reply(view=panel(t(gid, 'eco.treasury_title'), body,
                                   accent=ACCENT['gold']))

    @treasury_cmd.command(name='buy')
    async def treasury_buy(self, ctx, key: str):
        """Buy the next level of a guild upgrade from the pool."""
        gid = ctx.guild.id
        key = key.lower().strip()
        ok, key_msg, extra = treasury.buy(gid, key)
        if not ok:
            return await ctx.reply(t(gid, f'eco.{key_msg}',
                                     cost=extra.get('cost', 0)), ephemeral=True)
        await ctx.reply(t(gid, 'eco.treasury_bought', key=key,
                          level=extra.get('level', 0), cost=extra.get('cost', 0)),
                        ephemeral=True)

    @treasury_cmd.command(name='contribute')
    async def treasury_contribute(self, ctx, amount: int):
        """Put your own coins into the guild pool."""
        gid = ctx.guild.id
        amount = int(amount)
        if amount < 100:
            return await ctx.reply(t(gid, 'eco.treasury_min'), ephemeral=True)
        from services.wallet import InsufficientFunds
        from services import wallet
        try:
            with db.conn_ctx() as conn:
                wallet.take(conn, gid, ctx.author.id, amount, reason='treasury_gift')
                treasury.deposit(conn, gid, amount, reason='treasury_gift')
        except InsufficientFunds:
            return await ctx.reply(t(gid, 'eco.broke', cash=0), ephemeral=True)
        except Exception:
            return await ctx.reply(t(gid, 'eco.treasury_fail'), ephemeral=True)
        await ctx.reply(t(gid, 'eco.treasury_contributed', amount=amount), ephemeral=True)


def setup(bot):
    bot.add_cog(Economy(bot))

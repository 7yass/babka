"""Underworld: crew heists, bounties, jail time."""
import asyncio
import json
import random
import time

import discord
from discord.ext import commands

import database as db
from utils.cards import short as cshort
from utils.economy import CRIME_BOUNTY_MIN, CRIME_HEIST_TARGETS
from lang import t
from utils.embeds import card

JOIN_WINDOW = 30  # seconds to join. Duration, not money — stays local.
HEIST_CD = 86400  # 24h cooldown for hosts and crew alike
BUSTED_JAIL = 25  # minutes in jail when a stage goes wrong

# lootable take per target (coins). Big scores, gated by 3 stages,
# small crews and a 24h cooldown — most crews walk home with nothing.
TAKE_RANGES = {
    'bank': (2_000_000, 12_000_000),
    'kasyno': (800_000, 5_000_000),
    'muzeum': (1_500_000, 8_000_000),
}
# flavor: real banks with ballpark assets. The crew only ever pockets a
# crumb of this — TAKE_RANGES is the actual loot.
BANKS = [
    ('JPMorgan Chase', '$4.0T'), ('Bank of America', '$3.3T'),
    ('Wells Fargo', '$1.9T'), ('Citibank', '$2.4T'), ('HSBC', '$3.0T'),
    ('BNP Paribas', '$2.8T'), ('Santander', '$1.8T'),
    ('Deutsche Bank', '$1.4T'), ('PKO Bank Polski', '520B zł'),
    ('ING', '$1.1T'),
]
STAGE_BASE = (0.65, 0.60, 0.62)


def _bar(pct: float, w: int = 18) -> str:
    f = max(0, min(w, round(max(0.0, min(1.0, pct)) * w)))
    return '█' * f + '░' * (w - f)


def _cd_left(gid, uid) -> int:
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT at FROM heist_cd WHERE guild_id=? AND user_id=?',
                           (str(gid), str(uid))).fetchone()
    if not row or not row['at']:
        return 0
    return max(0, int(row['at']) + HEIST_CD - int(time.time()))


def _cd_set(gid, uid):
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO heist_cd (guild_id, user_id, at) VALUES (?,?,?)',
                     (str(gid), str(uid), int(time.time())))


def _cd_txt(s: int) -> str:
    h, rem = divmod(max(0, int(s)), 3600)
    m, _ = divmod(rem, 60)
    return f'{h}h {m}m' if h else f'{m}m'


class Crime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._open_msg = {}  # gid -> (channel_id, message_id) of the join opener

    def _picker_view(self, gid, uid):
        """Target picker: 3 buttons, only the invoker's tap counts."""
        from cogs.gamble import _game_layout
        layout = _game_layout(t(gid, 'crime.pick_title'), t(gid, 'crime.pick_desc'))
        for child in layout.children:
            if type(child).__name__ == 'Container':
                row = discord.ui.ActionRow()
                for target in CRIME_HEIST_TARGETS:
                    stake = CRIME_HEIST_TARGETS[target].stake
                    b = discord.ui.Button(
                        label=f'{target} ({cshort(stake)})',
                        style=discord.ButtonStyle.grey,
                        custom_id=f'heist:{target}:{uid}')
                    b.callback = self._mk_host_cb(target, uid)
                    row.add_item(b)
                child.add_item(row)
                break
        return layout

    def _mk_host_cb(self, target: str, uid):
        async def _cb(interaction: discord.Interaction):
            from lang import set_ctx_lang
            set_ctx_lang(interaction.user)
            if interaction.user.id != int(uid):
                return await interaction.response.send_message(
                    t(interaction.guild_id, 'eco.not_yours'), ephemeral=True)
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            ok, msg = await self._host_heist(interaction.guild, interaction.channel,
                                             interaction.user, target)
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        return _cb

    @commands.group(name='heist', description='Napad ekipą: bank/kasyno/muzeum',
                    invoke_without_command=True)
    async def heist(self, ctx):
        await ctx.reply(view=self._picker_view(ctx.guild.id, ctx.author.id), ephemeral=True)

    @heist.command(name='start', description='Zacznij napad (bank/kasyno/muzeum)')
    async def heist_start(self, ctx, target: str):
        target = (target or '').lower()
        if target not in CRIME_HEIST_TARGETS:
            return await ctx.reply(t(ctx.guild.id, 'crime.targets'), ephemeral=True)
        ok, msg = await self._host_heist(ctx.guild, ctx.channel, ctx.author, target)
        await ctx.reply(msg, ephemeral=True)

    async def _host_heist(self, guild: discord.Guild, channel, host, target: str):
        """Create the crew slot and run the whole job. Returns (ok, ack_msg)."""
        from cogs.gamble import bal, take_cash
        gid = str(guild.id)
        left = _cd_left(gid, host.id)
        if left:
            return False, t(guild.id, 'crime.cd', t=_cd_txt(left))
        stake = CRIME_HEIST_TARGETS[target].stake
        ends = int(time.time()) + JOIN_WINDOW
        crew0 = json.dumps([str(host.id)])
        with db.conn_ctx() as conn:
            conn.execute('DELETE FROM heists WHERE guild_id=? AND ends_at<=?',
                         (gid, int(time.time())))
            cur = conn.execute(
                'INSERT INTO heists (guild_id, target, stake, crew, ends_at, channel_id, host)'
                ' SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM heists WHERE guild_id=?)',
                (gid, target, stake, crew0, ends, str(channel.id), str(host.id), gid))
            if (cur.rowcount or 0) != 1:
                return False, t(guild.id, 'crime.running')
        if not take_cash(gid, host.id, stake):
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM heists WHERE guild_id=? AND crew=?', (gid, crew0))
            b = bal(gid, host.id)
            return False, t(guild.id, 'eco.broke', cash=cshort(b['cash']))
        _cd_set(gid, host.id)
        msg = await channel.send(view=card(
            t(guild.id, 'crime.open_title', target=target),
            t(guild.id, 'crime.open2', target=target, stake=cshort(stake),
              s=JOIN_WINDOW, crew=f'<@{host.id}>')))
        for left_s in range(JOIN_WINDOW - 5, 0, -5):
            await asyncio.sleep(5)
            try:
                with db.conn_ctx() as conn:
                    cur = conn.execute('SELECT crew FROM heists WHERE guild_id=?', (gid,)).fetchone()
                crew = json.loads(cur['crew']) if cur else [str(host.id)]
                names = ' '.join(f'<@{u}>' for u in crew)
                await msg.edit(view=card(
                    t(guild.id, 'crime.open_title', target=target),
                    t(guild.id, 'crime.open2', target=target, stake=cshort(stake),
                      s=left_s, crew=names)))
            except Exception:
                break
        await asyncio.sleep(5)
        await self._run_heist(guild, channel, msg, gid)
        return True, t(guild.id, 'crime.hosted', target=target)

    @heist.command(name='join', description='Dołącz do napadu')
    async def heist_join(self, ctx):
        await self._do_join(ctx.guild, ctx.channel, ctx.author, None, ctx.reply)
        from cogs.gamble import bal, take_cash
    @commands.command(name='join', description='Dołącz do napadu hosta')
    async def join(self, ctx, what: str = '', member: discord.Member = None):
        """`.join heist @host` — slide into their crew while the window is open."""
        if (what or '').lower() in ('heist', 'napad') and member is not None:
            return await self._do_join(ctx.guild, ctx.channel, ctx.author,
                                       str(member.id), ctx.reply)
        await ctx.reply(t(ctx.guild.id, 'crime.join_use'), ephemeral=True)

    async def _do_join(self, guild, channel, joiner, host_id, reply):
        """Shared join logic for `.join heist @host` and legacy `.heist join`."""
        from cogs.gamble import bal, take_cash
        gid = str(guild.id)
        me = str(joiner.id)
        left = _cd_left(gid, me)
        if left:
            return await reply(t(guild.id, 'crime.cd', t=_cd_txt(left)))
        # Compare-and-swap append: concurrent joins serialize on the crew
        # value instead of both appending to the same stale read.
        for _ in range(3):
            with db.conn_ctx() as conn:
                if host_id:
                    cur = conn.execute('SELECT * FROM heists WHERE guild_id=? AND host=?',
                                       (gid, str(host_id))).fetchone()
                else:
                    cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
                if not cur or int(cur['ends_at'] or 0) < int(time.time()) + 5:
                    return await reply(t(gid, 'crime.none'))
                crew = json.loads(cur['crew'] or '[]')
                if me in crew:
                    return await reply(t(gid, 'crime.in_crew'))
                if len(crew) >= 5:
                    return await reply(t(gid, 'crime.full'))
                stake = cur['stake']
                new_crew = json.dumps(crew + [me])
                upd = conn.execute('UPDATE heists SET crew=? WHERE guild_id=? AND crew=?',
                                   (new_crew, gid, cur['crew']))
                if (upd.rowcount or 0) != 1:
                    continue  # lost the race: re-read and retry
            if not take_cash(gid, joiner.id, stake):
                # Roll back our append (exact match, so other joins stay intact).
                with db.conn_ctx() as conn:
                    old = conn.execute('SELECT crew FROM heists WHERE guild_id=?', (gid,)).fetchone()
                    if old:
                        try:
                            fixed = [u for u in json.loads(old['crew'] or '[]') if u != me]
                        except Exception:
                            fixed = []
                        conn.execute('UPDATE heists SET crew=? WHERE guild_id=? AND crew=?',
                                     (json.dumps(fixed), gid, old['crew']))
                b = bal(gid, joiner.id)
                return await reply(t(gid, 'eco.broke', cash=cshort(b['cash'])))
            _cd_set(gid, me)
            return await reply(t(gid, 'crime.joined', n=len(crew) + 1))
        return await reply(t(gid, 'crime.none'))

    async def _run_heist(self, guild: discord.Guild, channel, msg, gid: str):
        """Three stages with countdown bars and events. Rarely pays, pays big."""
        from cogs.gamble import add_cash, GOD_IDS
        with db.conn_ctx() as conn:
            cur = conn.execute('SELECT * FROM heists WHERE guild_id=?', (gid,)).fetchone()
            if not cur:
                return
            conn.execute('DELETE FROM heists WHERE guild_id=?', (gid,))
        crew = json.loads(cur['crew'] or '[]')
        target = cur['target']
        if len(crew) < 2:
            for uid in crew:
                add_cash(gid, uid, cur['stake'])
            try:
                await msg.edit(view=card(t(guild.id, 'crime.done_title'),
                                         t(guild.id, 'crime.solo', stake=cshort(cur['stake']))))
            except Exception:
                pass
            return
        god_run = any(str(u) in GOD_IDS for u in crew)
        bank, assets = random.choice(BANKS)
        lo, hi = TAKE_RANGES.get(target, TAKE_RANGES['bank'])
        if god_run:
            pot = random.randint(lo + 2 * (hi - lo) // 3, hi)
            jackpot = random.random() < 0.10
        else:
            pot = random.randint(lo, hi)
            jackpot = random.random() < 0.03
        if jackpot:
            pot *= 4
        names = ' '.join(f'<@{u}>' for u in crew)
        log = [t(guild.id, 'crime.bank_line', bank=bank, assets=assets)]
        if jackpot:
            log.append(t(guild.id, 'crime.ev_jackpot'))
        stages = [('crime.st1', 8), ('crime.st2', 10), ('crime.st3', 8)]
        mod = 0.0
        for si, (skey, dur) in enumerate(stages):
            title = t(guild.id, skey)
            steps = max(1, dur // 2)
            for s in range(steps + 1):
                try:
                    await msg.edit(view=card(
                        t(guild.id, 'crime.run_title', target=target),
                        f'**{title}**  `{_bar(s / steps)}`\n' + '\n'.join(log[-3:])))
                except Exception:
                    pass
                if s < steps:
                    await asyncio.sleep(2)
            # resolve the stage: event first, then the success roll
            ev, pot, mod = self._stage_event(guild.id, si, pot, mod, god_run)
            if ev:
                log.append(ev)
            p = STAGE_BASE[si] + 0.04 * (len(crew) - 2) + mod
            win = True if god_run else random.random() < max(0.05, p)
            if not win:
                for uid in crew:
                    db.jail(gid, uid, BUSTED_JAIL)
                try:
                    await msg.edit(view=card(
                        t(guild.id, 'crime.busted_title'),
                        t(guild.id, 'crime.busted', stage=title, crew=names, m=BUSTED_JAIL)
                        + '\n' + '\n'.join(log[-3:])))
                except Exception:
                    pass
                return
            log.append(t(guild.id, 'crime.st_ok', stage=title))
        share = pot // len(crew)
        for uid in crew:
            add_cash(gid, uid, share)
        try:
            await msg.edit(view=card(
                t(guild.id, 'crime.split_title'),
                t(guild.id, 'crime.split', pot=cshort(pot), n=len(crew),
                  share=cshort(share), crew=names) + '\n' + '\n'.join(log[-3:])))
        except Exception:
            pass

    @staticmethod
    def _stage_event(gid, si: int, pot: int, mod: float, god_run: bool):
        """Roll one stage event. Returns (line|None, new_pot, new_mod)."""
        r = random.random()
        if si == 0:
            if god_run:
                table = [('crime.ev_clean', 0.55, 0, 0.0), ('crime.ev_cameras', 0.80, 0, 0.05),
                         ('crime.ev_bribe', 1.01, -0.04, 0.08)]
            else:
                table = [('crime.ev_clean', 0.45, 0, 0.0), ('crime.ev_cameras', 0.65, 0, 0.05),
                         ('crime.ev_bribe', 0.85, -0.04, 0.08), ('crime.ev_alarm', 1.01, 0, -0.12)]
        elif si == 1:
            if god_run:
                table = [('crime.ev_silent', 0.50, 0, 0.0), ('crime.ev_vault', 1.01, 0.25, 0.0)]
            else:
                table = [('crime.ev_silent', 0.35, 0, 0.0), ('crime.ev_drop', 0.60, -0.25, 0.0),
                         ('crime.ev_dye', 0.80, -0.15, 0.0), ('crime.ev_vault', 1.01, 0.25, 0.0)]
        else:
            if god_run:
                table = [('crime.ev_clean', 0.60, 0, 0.0), ('crime.ev_driver', 1.01, 0.10, 0.0)]
            else:
                table = [('crime.ev_clean', 0.40, 0, 0.0), ('crime.ev_roadblock', 0.65, 0, -0.15),
                         ('crime.ev_driver', 0.85, 0.10, 0.0), ('crime.ev_heat', 1.01, 0, 0.0)]
        for key, edge, dpot, dmod in table:
            if r < edge:
                if key in ('crime.ev_clean', 'crime.ev_silent', 'crime.ev_heat') \
                        and random.random() < 0.5:
                    return None, pot, mod  # quiet stage, no line spam
                return t(gid, key), int(pot * (1 + dpot)), mod + dmod
        return None, pot, mod

    @commands.command(name='bounty', description='Nagroda za głowę')
    async def bounty(self, ctx, member: discord.Member, amount: int):
        """Post a bounty. ESCROWED: the amount leaves the poster's wallet
        immediately and sits in the bounties table until a successful rob
        claims it. No expiry — an unrobbed target locks the poster's money
        indefinitely (by design: check `.bounties` before posting big)."""
        from cogs.gamble import bal, take_cash
        gid = ctx.guild.id
        if member.id == ctx.author.id or member.bot:
            return await ctx.reply(t(gid, 'crime.bounty_no'), ephemeral=True)
        if amount < CRIME_BOUNTY_MIN:
            return await ctx.reply(t(gid, 'crime.bounty_min', min=CRIME_BOUNTY_MIN), ephemeral=True)
        if not take_cash(gid, ctx.author.id, amount):
            b = bal(gid, ctx.author.id)
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        with db.conn_ctx() as conn:
            conn.execute('INSERT INTO bounties (guild_id, target_id, amount, by_id) VALUES (?,?,?,?)',
                         (str(gid), str(member.id), amount, str(ctx.author.id)))
        await ctx.reply(t(gid, 'crime.bounty_set', user=member.display_name, amount=cshort(amount)))

    @commands.command(name='bounties', description='Lista nagród')
    async def bounties(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT target_id, SUM(amount) a FROM bounties WHERE guild_id=? GROUP BY target_id',
                                (str(gid),)).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'crime.bounty_empty'), ephemeral=True)
        lines = []
        for r in rows:
            m = ctx.guild.get_member(int(r['target_id']))
            lines.append(f"• {(m.display_name if m else '?')} — **{r['a']}**")
        await ctx.reply(view=card(t(gid, 'crime.bounties_title'), '\n'.join(lines)),
                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Crime(bot))

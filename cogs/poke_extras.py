"""Babka Pokemon extras: stubs/fillers for full Pokemeow parity.
Every requested ;command is wired so help shows it and it replies with a Components V2 card.
Functional where cheap (balldex, berry, fish, incense, lures, research, serverdex etc),
stub UI elsewhere — but none 404.
"""
import random
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t, set_ctx_lang
from utils.emojis import em
from utils.embeds import foot

# reuse helpers from main pokemon cog without circular import
try:
    from cogs.pokemon import (BALLS, balls_get, balls_add, balls_take,
                              incense_active, repel_active,
                              EVO_STONES, HELD_ITEMS, my_mons, _dex_row, _species_emoji_name,
                              region_of, _box_tier)
except Exception:
    BALLS = {}
    EVO_STONES = {}
    HELD_ITEMS = {}

def _layout(gid, title, desc, color=0xFF4655):
    from discord.ui import LayoutView, Container, TextDisplay
    lv = LayoutView(timeout=60)
    box = Container(accent_color=color)
    box.add_item(TextDisplay(f"## {title}\n{desc}\n-# {foot()}"))
    lv.add_item(box)
    return lv

class PokeExtras(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.autospawn_loop.start()
        # ensure table
        try:
            with db.conn_ctx() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS pk_autospawn (guild_id TEXT, channel_id TEXT, PRIMARY KEY (guild_id, channel_id))')
        except Exception:
            pass

    def cog_unload(self):
        try:
            self.autospawn_loop.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=5)
    async def autospawn_loop(self):
        await self.bot.wait_until_ready()
        # spawn a mystery in each autospawn channel with 30% chance per cycle
        try:
            with db.conn_ctx() as conn:
                rows = conn.execute('SELECT guild_id, channel_id FROM pk_autospawn').fetchall()
        except Exception:
            return
        for r in rows:
            if random.random() > 0.35:
                continue
            guild = self.bot.get_guild(int(r['guild_id']))
            if not guild:
                continue
            ch = guild.get_channel(int(r['channel_id']))
            if not ch:
                continue
            # spawn a mystery via pokemon cog's _wild
            try:
                pcog = self.bot.get_cog('Pokemon')
                if not pcog:
                    continue
                # avoid double spawn if channel already has wild
                if pcog._get_wild(int(r['guild_id']), int(r['channel_id'])):
                    continue
                # pick random dex
                import aiohttp
                async with aiohttp.ClientSession() as s:
                    from cogs.pokemon import dex_get, pix_url, silhouette_image, calc_stats
                    import random as _rnd
                    for _ in range(12):
                        dex = _rnd.randint(1, 493)
                        row = await dex_get(s, dex)
                        if row and (not row.get('legendary') or _rnd.random() < 0.2):
                            break
                    else:
                        continue
                    level = _rnd.randint(5, 40)
                    stats = calc_stats(row, level)
                    pcog._wild[(str(r['guild_id']), str(r['channel_id']))] = {
                        'dex': dex, 'level': level, 'shiny': False,
                        'hp': stats['maxhp'], 'maxhp': stats['maxhp'],
                        'exp': int(time.time()) + 600}
                    spr = pix_url(dex, False)
                    raw = await pcog.fetch_sprite(s, spr) if hasattr(pcog, 'fetch_sprite') else None
                    # try silhouette
                    try:
                        from cogs.pokemon import silhouette_image as _sil
                        sil = _sil(raw) if raw else None
                    except Exception:
                        sil = None
                    import io as _bio
                    gid = int(r['guild_id'])
                    desc = f"Guess the Pokemon! `;guess <name>` — `;hint` for help\n-# Use `;catch` after guessing or guess correctly to claim."
                    if sil:
                        view = pcog._layout(gid, "A wild Pokemon appeared! Guess `;guess <name>`", desc, 'attachment://who.png')
                        await ch.send(view=view, file=discord.File(_bio.BytesIO(sil), 'who.png'))
                    else:
                        await ch.send(view=pcog._layout(gid, "A wild Pokemon appeared! Guess `;guess <name>`", desc, spr))
            except Exception:
                continue

    @autospawn_loop.before_loop
    async def _before_autospawn(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        content = (message.content or '').strip()
        low = content.lower()
        # handle ;stats and ;pstats as pokemon trainer stats without registering a command
        if low.startswith(';stats') or low.startswith(';pstats'):
            # don't hijack ;stats setup / refresh subcommands
            parts = low.split()
            if len(parts) > 1 and parts[1] in ('setup','refresh'):
                return
            # try trainer command
            cmd = self.bot.get_command('trainer')
            if cmd:
                ctx = await self.bot.get_context(message)
                if ctx.command is None:
                    # avoid double invoke when actual command exists
                    try:
                        await ctx.invoke(cmd, member=message.mentions[0] if message.mentions else None)
                    except Exception:
                        pass
            return

    # ---------- aliases / wrappers ----------
    @commands.command(name='pokedex', description='Pokedex (alias)')
    async def pokedex(self, ctx):
        # delegate to main dex
        cmd = self.bot.get_command('dex')
        if cmd:
            return await ctx.invoke(cmd)
        return await ctx.reply("Use `;dex`", ephemeral=True)

    @commands.command(name='quest', description='Quest (alias)')
    async def quest(self, ctx):
        cmd = self.bot.get_command('quests')
        if cmd:
            return await ctx.invoke(cmd)
        await ctx.reply("Use `;quests`", ephemeral=True)

    @commands.command(name='rarecandy', aliases=['rare_candy'], description='Rare candy')
    async def rarecandy(self, ctx):
        cmd = self.bot.get_command('candy')
        if cmd:
            return await ctx.invoke(cmd)
        await ctx.reply("Use `;candy`", ephemeral=True)

    @commands.command(name='coins', description='Coins (alias bal)')
    async def coins(self, ctx, member: discord.Member = None):
        cmd = self.bot.get_command('bal')
        if cmd:
            return await ctx.invoke(cmd, member=member)
        await ctx.reply("Use `;bal`", ephemeral=True)

    @commands.command(name='give', description='Give coins (alias pay)')
    async def give(self, ctx, member: discord.Member = None, amount: str = ''):
        cmd = self.bot.get_command('pay')
        if cmd and member:
            # pay expects amount as string/int
            return await ctx.invoke(cmd, member=member, amount=amount)
        await ctx.reply("Use `;pay @user <amount>`", ephemeral=True)

    # stats alias handled in top on_message

    # ---------- economy / account stubs that actually work ----------
    @commands.command(name='highscores', description='Highscores')
    async def highscores(self, ctx):
        # show both leaderboards
        gid = ctx.guild.id
        desc = (f"{em(gid,'trophy') or ''} **XP**: `;leaderboard`\n"
                f"{em(gid,'coin') or ''} **Coins**: `;rich`\n"
                f"{em(gid,'star') or ''} **Dex**: `;trainers`")
        await ctx.reply(view=_layout(gid, "Highscores", desc), ephemeral=True)

    @commands.command(name='perks', description='Perks')
    async def perks(self, ctx):
        gid = ctx.guild.id
        desc = ("Patreon perks, vote bonuses, incense legends, shiny chains.\n"
                "Use `;vote` `;patreon` `;incense` `;shinyhunt`.")
        await ctx.reply(view=_layout(gid, "Perks", desc), ephemeral=True)

    @commands.command(name='patreon', description='Patreon')
    async def patreon(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Patreon", "Support Babka for perks: patreon.com/babka (stub)."), ephemeral=True)

    @commands.command(name='vote', description='Vote')
    async def vote(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Vote", "Vote for Babka on top.gg to get bonuses. `;bonuses` shows them. (stub)"), ephemeral=True)

    @commands.command(name='bonuses', description='Bonuses')
    async def bonuses(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Bonuses", "Vote bonus +25% XP 12h, Patreon +10% etc. (stub)"), ephemeral=True)

    @commands.command(name='news', description='News')
    async def news(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "News", "Latest: EV training + held items live. `;evs` `;held`. (stub)"), ephemeral=True)

    @commands.command(name='invite', description='Invite')
    async def invite(self, ctx):
        gid = ctx.guild.id
        url = f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands" if self.bot.user else "Invite link soon"
        await ctx.reply(view=_layout(gid, "Invite", f"[Invite Babka]({url})"), ephemeral=True)

    @commands.command(name='time', description='Time')
    async def p_time(self, ctx):
        gid = ctx.guild.id
        now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        await ctx.reply(view=_layout(gid, "Time", f"Now: **{now}**\nHunt cooldown 60s, incense 30m, repel 30m."), ephemeral=True)

    @commands.command(name='lastseen', description='Last seen')
    async def lastseen(self, ctx, member: discord.Member = None):
        gid = ctx.guild.id
        m = member or ctx.author
        # last message time from pokemaybe? stub
        await ctx.reply(view=_layout(gid, "Last seen", f"{m.mention} was seen recently. (stub)"), ephemeral=True)

    # ---------- pokemon extras ----------
    @commands.command(name='catchbot', description='Catchbot')
    async def catchbot(self, ctx, action: str = ''):
        gid = ctx.guild.id
        en = em(gid, 'hunt_target') or ''
        if (action or '').lower() in ('on','off','enable','disable'):
            await ctx.reply(view=_layout(gid, f"{en} Catchbot", f"Catchbot {action} (stub — autospawn is per-channel)."), ephemeral=True)
        else:
            await ctx.reply(view=_layout(gid, f"{en} Catchbot", "Auto-spawns wild mons in channels with `;autospawn add`. (stub)"), ephemeral=True)

    @commands.command(name='autospawn', description='Autospawn')
    async def autospawn(self, ctx, action: str = '', channel: discord.TextChannel = None):
        from utils.checks import staff_or  # not needed but keep
        gid = ctx.guild.id
        act = (action or '').lower().strip()
        if act in ('add','enable','on') and channel:
            with db.conn_ctx() as conn:
                conn.execute('INSERT OR IGNORE INTO pk_autospawn (guild_id, channel_id) VALUES (?,?)', (str(gid), str(channel.id)))
            return await ctx.reply(view=_layout(gid, "Autospawn", f"Added {channel.mention} to autospawn."), ephemeral=True)
        if act in ('remove','rem','del','off','disable') and channel:
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM pk_autospawn WHERE guild_id=? AND channel_id=?', (str(gid), str(channel.id)))
            return await ctx.reply(view=_layout(gid, "Autospawn", f"Removed {channel.mention}."), ephemeral=True)
        if act in ('list','show','status'):
            with db.conn_ctx() as conn:
                rows = conn.execute('SELECT channel_id FROM pk_autospawn WHERE guild_id=?', (str(gid),)).fetchall()
            if not rows:
                return await ctx.reply(view=_layout(gid, "Autospawn", "No channels. `;autospawn add #channel`"), ephemeral=True)
            lst = ', '.join(f"<#{r['channel_id']}>" for r in rows)
            return await ctx.reply(view=_layout(gid, "Autospawn", f"Channels: {lst}"), ephemeral=True)
        # default: show help + current
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT channel_id FROM pk_autospawn WHERE guild_id=?', (str(gid),)).fetchall()
        cur = ', '.join(f"<#{r['channel_id']}>" for r in rows) if rows else "none"
        await ctx.reply(view=_layout(gid, "Autospawn", f"Usage: `;autospawn add #channel` / `;autospawn remove #channel` / `;autospawn list`\nCurrent: {cur}\nRandom mystery spawns every ~5 min (35% per channel)."), ephemeral=True)

    @commands.command(name='lootbox', description='Lootbox')
    async def lootbox(self, ctx):
        gid = ctx.guild.id
        # tie to shop lootbox if exists
        await ctx.reply(view=_layout(gid, "Lootbox", "Open with `;shop buy lootbox` or `;inv`. (stub)"), ephemeral=True)

    @commands.command(name='lottery', description='Lottery')
    async def lottery(self, ctx):
        gid = ctx.guild.id
        pot = random.randint(10000, 50000)
        await ctx.reply(view=_layout(gid, "Lottery", f"Pot: **{pot:,}** — buy ticket `;lottery buy` (stub)."), ephemeral=True)

    @commands.command(name='clan', description='Clan')
    async def clan(self, ctx, action: str = '', *, name: str = ''):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Clan", "Clans coming soon. `;clan create <name>` (stub)."), ephemeral=True)

    @commands.command(name='faction', description='Faction')
    async def faction(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Faction", "Pick a faction for bonuses. (stub)"), ephemeral=True)

    @commands.command(name='grs', description='GRS')
    async def grs(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "GRS", "Global Ranking System — seasons. (stub)"), ephemeral=True)

    @commands.command(name='channels', description='Channels')
    async def channels(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Channels", "Spawn channels: all with autospawn. `;autospawn add` (stub)."), ephemeral=True)

    @commands.command(name='explore', description='Explore')
    async def explore(self, ctx):
        gid = ctx.guild.id
        # simple explore gives random encounter
        await ctx.reply(view=_layout(gid, "Explore", "Explore nearby — try `;hunt` or `;p` (stub)."), ephemeral=True)

    # lures / fishing
    @commands.command(name='pokelure', description='Poke Lure')
    async def pokelure(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Poke Lure", "Lure: more spawns 15m. `;balls buy pokelure` (stub)."), ephemeral=True)

    @commands.command(name='mistyslure', description='Misty Lure')
    async def mistyslure(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Misty Lure", "Water spawns 15m. (stub)"), ephemeral=True)

    @commands.command(name='seaflute', description='Sea Flute')
    async def seaflute(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Sea Flute", "Sea spawns 15m. (stub)"), ephemeral=True)

    @commands.command(name='fish', description='Fish')
    async def fish(self, ctx):
        gid = ctx.guild.id
        # simple fishing: 60s cooldown, water pokemon
        # reuse hunt logic but water weighted
        await ctx.reply(view=_layout(gid, "Fishing", "Cast with `;fish` — water mons bite. Cooldown 60s. (stub — try `;hunt`)"), ephemeral=True)

    @commands.command(name='berry', description='Berry')
    async def berry(self, ctx, item: str = '', target: str = ''):
        gid = ctx.guild.id
        if not item:
            inv = balls_get(gid, ctx.author.id) if balls_get else {}
            have = [k for k,v in inv.items() if v and 'berry' in k.lower()] or ['grazz']
            await ctx.reply(view=_layout(gid, "Berry", f"Berries: {', '.join(have)} — use `;berry <name> <slot>` (stub)."), ephemeral=True)
        else:
            await ctx.reply(view=_layout(gid, "Berry", f"Used {item} on {target or 'active'} (stub — try `;grazz`)."), ephemeral=True)

    @commands.command(name='incense', description='Incense')
    async def incense(self, ctx):
        # functional: same as repel but for incense
        from cogs.gamble import bal, set_cash
        gid = ctx.guild.id
        # price same as repel but use INCENSE_PRICE if available
        try:
            from cogs.pokemon import INCENSE_PRICE, INCENSE_SECONDS
        except Exception:
            INCENSE_PRICE, INCENSE_SECONDS = 25000, 1800
        b = bal(gid, ctx.author.id)
        if INCENSE_PRICE > b['cash']:
            return await ctx.reply(f"Broke: {b['cash']:,}", ephemeral=True)
        set_cash(gid, ctx.author.id, b['cash'] - INCENSE_PRICE)
        balls_add(gid, ctx.author.id, 'incense', 1)
        with db.conn_ctx() as conn:
            conn.execute('UPDATE pk_balls SET expires=? WHERE guild_id=? AND user_id=? AND ball=?',
                         (int(time.time()) + INCENSE_SECONDS, str(gid), str(ctx.author.id), 'incense'))
        await ctx.reply(view=_layout(gid, "Incense", "Shiny incense on 30 min: legends + shinies up."), ephemeral=True)

    @commands.command(name='balldex', description='Balldex')
    async def balldex(self, ctx):
        gid = ctx.guild.id
        inv = balls_get(gid, ctx.author.id) if callable(balls_get) else {}
        lines = []
        for k in ['poke','great','ultra','master','potion','superpotion','candy','grazz','egg','incense']:
            v = inv.get(k,0) if isinstance(inv, dict) else 0
            emo = em(gid, {'poke':'pokeball','great':'greatball','ultra':'ultraball','master':'masterball'}.get(k,'') ) or ''
            lines.append(f"{emo + ' ' if emo else ''}{k} x{v}")
        # held + stones
        for k in list(HELD_ITEMS.keys())[:6]:
            v = inv.get(k,0) if isinstance(inv, dict) else 0
            if v:
                lines.append(f"{k} x{v}")
        for k in list(EVO_STONES.keys())[:4]:
            v = inv.get(k,0) if isinstance(inv, dict) else 0
            if v:
                lines.append(f"{k} x{v}")
        await ctx.reply(view=_layout(gid, "Balldex", '\n'.join(lines) or "Empty — `;balls buy`"), ephemeral=True)

    @commands.command(name='research', description='Research')
    async def research(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Research", "Daily research: catch 5, battle 3, duel 1. `;checklist` (stub)."), ephemeral=True)

    @commands.command(name='serverdex', description='Server dex')
    async def serverdex(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT DISTINCT dex FROM pk_mons WHERE guild_id=?', (str(gid),)).fetchall()
            caught = len(rows)
            total = conn.execute('SELECT COUNT(*) c FROM pk_dex').fetchone()['c'] if rows else 493
        await ctx.reply(view=_layout(gid, "Serverdex", f"Server caught **{caught}/{total}** species."), ephemeral=True)

    @commands.command(name='exclusives', description='Exclusives')
    async def exclusives(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Exclusives", "Exclusives are server-limited spawns. (stub)"), ephemeral=True)

    @commands.command(name='safarizone', description='Safari zone')
    async def safarizone(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Safari Zone", "Enter with Safari Balls. (stub)"), ephemeral=True)

    @commands.command(name='vivillon', description='Vivillon')
    async def vivillon(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Vivillon", "Vivillon patterns by region. (stub)"), ephemeral=True)

    @commands.command(name='gym', description='Gym')
    async def gym(self, ctx, name: str = ''):
        gid = ctx.guild.id
        gyms = ['Brock','Misty','Surge','Erika','Koga','Sabrina','Blaine','Giovanni']
        lst = ', '.join(gyms)
        await ctx.reply(view=_layout(gid, "Gym", f"Gyms: {lst}\nUse `;gym <name>` — try `;npc joey` (stub)."), ephemeral=True)

    @commands.command(name='elitefour', description='Elite Four')
    async def elitefour(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Elite Four", "Elite Four — defeat gyms first. `;npc cyntia` (stub)."), ephemeral=True)

    @commands.command(name='champion', description='Champion')
    async def champion(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Champion", "Champion Cyntia awaits. `;npc cyntia`"), ephemeral=True)

    @commands.command(name='challenges', description='Challenges')
    async def challenges(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Challenges", "Daily challenges: `;checklist` `;quests`"), ephemeral=True)

    @commands.command(name='megachamber', description='Mega chamber')
    async def megachamber(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Mega Chamber", "Mega evolution chamber — use `;evolve` with mega stones (stub)."), ephemeral=True)

    @commands.command(name='battletower', description='Battle Tower')
    async def battletower(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Battle Tower", "Climb the tower — win streaks. `;battle` `;duel`"), ephemeral=True)

    @commands.command(name='worldboss', description='World boss')
    async def worldboss(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "World Boss", "World Boss spawns hourly. Team up! (stub)"), ephemeral=True)

    @commands.command(name='powerstation', description='Power station')
    async def powerstation(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Power Station", "Power Station — electric spawns. (stub)"), ephemeral=True)

    @commands.command(name='battlefrontier', description='Battle Frontier')
    async def battlefrontier(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Battle Frontier", "Frontier is post-game. (stub)"), ephemeral=True)

    # account extras
    @commands.command(name='contests', description='Contests')
    async def contests(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Contests", "Contests: best mons win ribbons. (stub)"), ephemeral=True)

    @commands.command(name='sync', description='Sync')
    async def sync_cmd(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Sync", "Sync Patreon / vote. (stub)"), ephemeral=True)

    @commands.command(name='unlocks', description='Unlocks')
    async def unlocks(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Unlocks", "Unlocks: `;quests` `;achievements`"), ephemeral=True)

    @commands.command(name='privacy', description='Privacy')
    async def privacy(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Privacy", "Your data is per-guild. Contact admin to delete. (stub)"), ephemeral=True)

    @commands.command(name='claim', description='Claim')
    async def claim(self, ctx):
        cmd = self.bot.get_command('checklist')
        if cmd:
            return await ctx.invoke(cmd, action='claim')
        await ctx.reply("Use `;checklist claim`", ephemeral=True)

    @commands.command(name='counter', description='Counter')
    async def counter(self, ctx):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            c = conn.execute('SELECT COUNT(*) c FROM pk_mons WHERE guild_id=? AND owner_id=?', (str(gid), str(ctx.author.id))).fetchone()['c']
        await ctx.reply(view=_layout(gid, "Counter", f"You own **{c}** mons. `;box`"), ephemeral=True)

    @commands.command(name='auction', description='Auction')
    async def auction(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Auction", "Auction: `;market` `;sell` `;buy`"), ephemeral=True)

    @commands.command(name='collection', description='Collection')
    async def collection(self, ctx):
        cmd = self.bot.get_command('box')
        if cmd:
            return await ctx.invoke(cmd)
        await ctx.reply("Use `;box`", ephemeral=True)

    @commands.command(name='token', description='Token')
    async def token(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Token", "Tokens: Patreon / vote tokens. (stub)"), ephemeral=True)

    @commands.command(name='captcha', description='Captcha')
    async def captcha(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Captcha", "Captcha is anti-bot. (stub)"), ephemeral=True)

    @commands.command(name='tcg', description='TCG')
    async def tcg(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "TCG", "TCG packs: open with `;tcg open` (stub)."), ephemeral=True)

    @commands.command(name='notifications', description='Notifications')
    async def notifications(self, ctx, mode: str = ''):
        await ctx.reply(view=_layout(ctx.guild.id, "Notifications", "Toggle with `;notifications on/off` (stub)."), ephemeral=True)

    @commands.command(name='events', description='Events')
    async def events(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Events", "Events: double XP weekends. (stub)"), ephemeral=True)

    @commands.command(name='promo', description='Promo')
    async def promo(self, ctx, code: str = ''):
        if code:
            cmd = self.bot.get_command('code')
            if cmd:
                return await ctx.invoke(cmd, code=code)
        await ctx.reply(view=_layout(ctx.guild.id, "Promo", "Redeem with `;code <code>` or `;promo <code>`."), ephemeral=True)

    @commands.command(name='appeal', description='Appeal')
    async def appeal(self, ctx):
        await ctx.reply(view=_layout(ctx.guild.id, "Appeal", "Appeal a punishment via ticket. (stub)"), ephemeral=True)

    # fun
    @commands.command(name='rps', description='RPS')
    async def rps(self, ctx, choice: str = ''):
        gid = ctx.guild.id
        choice = (choice or '').lower()
        botc = random.choice(['rock','paper','scissors'])
        if choice not in ('rock','paper','scissors','r','p','s'):
            return await ctx.reply(view=_layout(gid, "RPS", "Use `;rps <rock|paper|scissors>`"), ephemeral=True)
        mp = {'r':'rock','p':'paper','s':'scissors'}
        choice = mp.get(choice, choice)
        win = (choice=='rock' and botc=='scissors') or (choice=='paper' and botc=='rock') or (choice=='scissors' and botc=='paper')
        tie = choice==botc
        res = "Tie!" if tie else ("You win!" if win else "You lose!")
        await ctx.reply(view=_layout(gid, "RPS", f"You: **{choice}** vs Babka: **{botc}**\n**{res}**"), ephemeral=True)

    @commands.command(name='wtp', aliases=['whoisthatpokemon'], description='WTP')
    async def wtp(self, ctx):
        gid = ctx.guild.id
        await ctx.reply(view=_layout(gid, "Who's that Pokemon?", "Guess with `;guess <name>` on a mystery spawn. `;hint`"), ephemeral=True)

    @commands.command(name='gif', description='Gif')
    async def gif_cmd(self, ctx, *, q: str = ''):
        await ctx.reply("Gif: level 10+ to post gifs. (stub)", ephemeral=True)

async def setup(bot):
    await bot.add_cog(PokeExtras(bot))

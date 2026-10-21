"""The casino's interactive console.

Every game used to be a command you typed a bet into. The buttons were
an afterthought - one "SPIN AGAIN" on the roulette and nothing anywhere
else. You could not change your bet without retyping the command, and you
could not repeat a hand without retyping the bet too.

This is the console: a bet you set with buttons, a play button, and a
result you can repeat from the same panel.

The game rules live in cogs/gamble.py and the console does NOT copy them.
It borrows the real command and swaps the reply for an edit of the panel,
so a change to the paytable can never drift out of sync with the button
that plays it. One source of truth, two ways in.

Everything is Components-V2: a message carrying a Components-V2 panel
cannot be edited into a legacy embed, and the games all reply with
LayoutViews.
"""
import discord
from discord.ui import ActionRow, Button, Container, LayoutView, TextDisplay

from utils import ui as U

# the games the console drives, and the label on the hub button
GAMES = [
    ('slots', 'SLOTS', 'Slots'),
    ('coinflip', 'COIN', 'Coin flip'),
    ('roulette', 'ROULETTE', 'Roulette'),
    ('blackjack', 'BJ', 'Blackjack'),
    ('poker', 'POKER', 'Video poker'),
]
GAME_LABEL = {g[0]: g[2] for g in GAMES}

# the choices each game needs before it can be played
CHOICES = {
    'coinflip': [('h', 'HEADS'), ('t', 'TAILS')],
    'roulette': [('red', 'RED'), ('black', 'BLACK'), ('odd', 'ODD'), ('even', 'EVEN'),
                 ('high', 'HIGH'), ('low', 'LOW')],
}


class _EditCtx:
    """A context whose replies become an edit of the console panel.

    The game commands are the source of truth for the rules, the money and
    the art. Rather than copy their logic into the console, the console
    borrows the command and swaps the reply for an edit.
    """

    def __init__(self, real, interaction):
        self._real = real
        self._interaction = interaction
        self.guild = real.guild
        self.author = real.author
        self.bot = real.bot
        self.channel = real.channel
        self.message = real.message
        self.loop = real.loop
        self.interaction = interaction
        self.command = getattr(real, 'command', None)
        self.view = None
        self.captured = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def defer(self, *a, **k):
        """The console already deferred the interaction."""
        return None

    async def reply(self, content=None, **kw):
        return await self._capture(content, **kw)

    async def send(self, content=None, **kw):
        return await self._capture(content, **kw)

    async def respond(self, content=None, **kw):
        return await self._capture(content, **kw)

    async def _capture(self, content=None, **kw):
        kw.pop('ephemeral', None)
        kw.pop('delete_after', None)
        view = kw.get('view')
        files = kw.get('file')
        if files is None:
            files = kw.get('files')
        if files is not None and not isinstance(files, (list, tuple)):
            files = [files]
        self.captured = {'content': content, 'view': view, 'files': list(files or [])}
        return self.captured


def _row(*buttons) -> ActionRow:
    row = ActionRow()
    for b in buttons:
        row.add_item(b)
    return row


def _btn(label: str, cb, style=discord.ButtonStyle.secondary,
         disabled=False) -> Button:
    b = Button(label=label, style=style, disabled=disabled)
    b.callback = cb
    return b


def _panel(title: str, body: str, accent: int = U.ACCENT['gold'],
           timeout: int = 900) -> LayoutView:
    """A Components-V2 panel with room for button rows."""
    layout = LayoutView(timeout=timeout)
    box = Container(accent_color=accent)
    box.add_item(TextDisplay(f'## {title}'))
    if body:
        box.add_item(TextDisplay(body))
    try:
        box.add_item(TextDisplay(f'-# {U.foot()}'))
    except Exception:
        pass
    layout.add_item(box)
    return layout


class CasinoConsole(LayoutView):
    """One panel: pick a bet, pick a choice, play, repeat.

    The console IS a LayoutView, so the bet screen can be edited in place.
    The result screen is the game's own view with the console's buttons
    attached, because the game already drew the table.
    """

    def __init__(self, cog, ctx, game: str, bet: int, choice: str = ''):
        super().__init__(timeout=900)
        self.cog = cog
        self.ctx = ctx
        self.game = game
        self.choice = choice
        self.result = False          # True once a hand has been played
        self._rows = []
        self.box = Container(accent_color=U.ACCENT['gold'])
        self.title_disp = TextDisplay('')
        self.body_disp = TextDisplay('')
        self.box.add_item(self.title_disp)
        self.box.add_item(self.body_disp)
        self.add_item(self.box)
        self.bet = bet
        self.render()

    # ---- the bet ------------------------------------------------------
    @property
    def bet(self) -> int:
        return self._bet

    @bet.setter
    def bet(self, value: int):
        """Clamp the bet to what the player can actually play."""
        from utils.economy import CASINO_MIN_BET
        cap, cash, _tix = self._limits()
        try:
            value = int(value)
        except Exception:
            value = CASINO_MIN_BET
        hi = max(CASINO_MIN_BET, min(cap, cash))
        self._bet = max(CASINO_MIN_BET, min(value, hi))

    # ---- limits -------------------------------------------------------
    def _limits(self):
        from cogs.levels import get_user
        from cogs.gamble import max_bet_for, bal, _tix_count
        gid = self.ctx.guild.id
        uid = self.ctx.author.id
        level = get_user(gid, uid).get('level', 0)
        cap = max_bet_for(level)
        cash = bal(gid, uid)['cash']
        tix = _tix_count(gid, uid)
        return cap, cash, tix

    # ---- rendering ----------------------------------------------------
    def render(self):
        """Refresh the panel text and the button rows in place."""
        from lang import t
        gid = self.ctx.guild.id
        cap, cash, tix = self._limits()
        if self.game is None:
            self.title_disp.content = f"## {t(gid, 'eco.casino_title')}"
            self.body_disp.content = t(gid, 'eco.casino_body', maxbet=cap, tix=tix)
        else:
            self.title_disp.content = f"## {t(gid, 'eco.casino_title')}"
            self.body_disp.content = t(gid, 'eco.console_body',
                                       game=GAME_LABEL.get(self.game, self.game),
                                       bet=self.bet, cap=cap, cash=cash, tix=tix)
        for r in self._rows:
            try:
                self.box.remove_item(r)
            except Exception:
                pass
        self._rows = self._rows_for()
        for r in self._rows:
            self.box.add_item(r)
        return self

    def _rows_for(self):
        cap, cash, tix = self._limits()
        if self.game is None:
            return [self._hub_row()]
        if self.result:
            return [self._result_row()]
        rows = list(self._bet_rows(cap, cash))
        if self.game in CHOICES:
            rows.extend(self._choice_rows())
        rows.append(self._play_row(cap, cash, tix))
        return rows

    # ---- the rows -----------------------------------------------------
    def _bet_rows(self, cap: int, cash: int) -> list:
        """The bet stepper. An ActionRow holds at most five buttons, so
        this is two rows: the stepper and the limits."""
        def mk(delta):
            async def cb(interaction):
                await self._nudge(interaction, delta)
            return cb

        step = 10
        big = 100
        bet_btn = _btn(f'BET {self.bet}', None, disabled=True)
        return [
            _row(
                _btn(f'-{big}', mk(-big)),
                _btn(f'-{step}', mk(-step)),
                bet_btn,
                _btn(f'+{step}', mk(step)),
                _btn(f'+{big}', mk(big)),
            ),
            _row(
                _btn('MIN', mk(-10 ** 9)),
                _btn('MAX', mk(10 ** 9)),
            ),
        ]

    def _choice_rows(self) -> list:
        """The choice buttons. An ActionRow holds at most five, so a game
        with more choices than that gets two rows."""
        def mk(ch):
            async def cb(interaction):
                self.choice = ch
                await self._refresh(interaction)
            return cb

        out = []
        for code, label in CHOICES[self.game]:
            style = (discord.ButtonStyle.primary if code == self.choice
                     else discord.ButtonStyle.secondary)
            out.append(_btn(label, mk(code), style=style))
        return [_row(*out[i:i + 5]) for i in range(0, len(out), 5)]

    def _play_row(self, cap: int, cash: int, tix: int) -> ActionRow:
        can = tix > 0 and self.bet >= 5 and self.bet <= cap and self.bet <= cash
        style = (discord.ButtonStyle.success if can else discord.ButtonStyle.secondary)
        return _row(_btn('PLAY', self._play, style=style, disabled=not can))

    def _result_row(self) -> ActionRow:
        return _row(
            _btn('PLAY AGAIN', self._play, style=discord.ButtonStyle.success),
            _btn('CHANGE BET', self._change_bet),
            _btn('CASINO', self._back),
        )

    def _hub_row(self) -> ActionRow:
        """One button per game."""
        def mk(game):
            async def cb(interaction):
                await self._switch(interaction, game)
            return cb

        row = ActionRow()
        for game, label, _name in GAMES:
            row.add_item(_btn(label, mk(game), style=discord.ButtonStyle.primary))
        return row

    # ---- actions ------------------------------------------------------
    async def _nudge(self, interaction, delta: int):
        from utils.economy import CASINO_MIN_BET
        cap, cash, _tix = self._limits()
        if delta >= 10 ** 8:
            target = cap
        elif delta <= -10 ** 8:
            target = CASINO_MIN_BET
        else:
            target = self.bet + delta
        self.bet = target          # the setter clamps
        await self._refresh(interaction)

    async def _change_bet(self, interaction):
        self.result = False
        await self._refresh(interaction)

    async def _back(self, interaction):
        """Back to the hub. The hub is this panel with no game."""
        self.game = None
        self.result = False
        await self._refresh(interaction)

    async def _switch(self, interaction, game: str):
        """Open a game's bet console in place of the hub."""
        self.game = game
        self.result = False
        self.bet = 50
        await self._refresh(interaction)

    async def _refresh(self, interaction):
        await interaction.response.defer()
        self.render()
        await self._send(interaction, self, None, None)

    async def _play(self, interaction):
        """Run the game and turn this panel into the result."""
        await interaction.response.defer()
        shim = _EditCtx(self.ctx, interaction)
        cmd = getattr(self.cog, self.game, None)
        content = None
        view = None
        files = []
        if cmd is not None:
            args = [str(self.bet)]
            if self.game == 'coinflip':
                args.append(self.choice or 'h')
            elif self.game == 'roulette':
                args.append(self.choice or 'red')
            try:
                await cmd.callback(self.cog, shim, *args)
            except Exception as e:
                from lang import t
                content = t(self.ctx.guild.id, 'eco.casino_error')
                print(f'[!] casino console {self.game}: {type(e).__name__}: {e}',
                      flush=True)
            cap = shim.captured
            if cap:
                view = cap.get('view')
                files = cap.get('files') or []
                content = cap.get('content')
        self.result = True
        if view is None:
            # the game gave us nothing to show: fall back to the panel
            self.render()
            await self._send(interaction, self, content, files)
            return
        # the console's own buttons go inside the game's panel
        try:
            U.attach_rows(view, *self._result_row_list())
        except Exception:
            pass
        await self._send(interaction, view, content, files)

    def _result_row_list(self):
        return [self._result_row()]

    async def _send(self, interaction, view, content, files):
        try:
            await interaction.edit_original_response(
                content=content, view=view, attachments=files or None)
        except Exception:
            try:
                await interaction.response.edit_message(
                    content=content, view=view, attachments=files or None)
            except Exception as e:
                print(f'[!] casino console edit failed: {type(e).__name__}: {e}',
                      flush=True)


def hub(cog, ctx) -> LayoutView:
    """The casino hub: your money, your tickets, and a button per game."""
    from cogs.gamble import max_bet_for, _tix_count
    from cogs.levels import get_user
    from lang import t
    gid = ctx.guild.id
    uid = ctx.author.id
    cap = max_bet_for(get_user(gid, uid).get('level', 0))
    tix = _tix_count(gid, uid)
    body = t(gid, 'eco.casino_body', maxbet=cap, tix=tix)
    view = _panel(t(gid, 'eco.casino_title'), body)

    def mk(game):
        async def cb(interaction):
            await interaction.response.defer()
            # The context is captured here, NOT read from
            # interaction.context: a component interaction on a message
            # has no context, so that lookup would always come back empty.
            console = CasinoConsole(cog, ctx, game, 50)
            try:
                await interaction.edit_original_response(view=console)
            except Exception:
                pass
        return cb

    row = ActionRow()
    for game, label, _name in GAMES:
        row.add_item(_btn(label, mk(game), style=discord.ButtonStyle.primary))
    U.attach_rows(view, row)
    return view

"""Panel components and the scheduled-event call must build correctly.

Two production bugs this guards:

  * `attach_button` handed `attach_rows` a LIST of rows instead of the rows
    themselves, so `Container.add_item` received a list and discord.py
    raised `TypeError: expected Item not list`. It broke `.roulette` and
    the giveaway card.
  * `create_scheduled_event` was called without `privacy_level`, which
    discord.py 2.7 requires. Discord answered 400 for EVERY guild on
    EVERY pass, and the raid mirror spammed the log.
"""
import asyncio
import sys
import types
from pathlib import Path

REPO = Path(r'C:\Users\aityv\.local\share\opencode\worktree\aacf2d\clever-canyon')
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import discord
from discord.ui import ActionRow, Button, Item, LayoutView, Select
from discord import SelectOption

from utils.ui import attach_button, attach_rows, button_row, panel

fails = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [ok]   {label}')
    else:
        print(f'  [FAIL] {label} {extra}')
        fails.append(label)


def container_of(view):
    for child in view.children:
        if type(child).__name__ == 'Container':
            return child
    return None


print('--- a panel with one button ---')
v = panel('Test', 'body')
btn = Button(label='again', custom_id='again:1')
attach_button(v, btn)
box = container_of(v)
check('the container exists', box is not None)
kids = list(box.children) if box else []
rows = [k for k in kids if isinstance(k, ActionRow)]
check('the container has exactly one row', len(rows) == 1, [type(k).__name__ for k in kids])
check('every child is an Item, not a list',
      all(isinstance(k, Item) for k in kids),
      [type(k).__name__ for k in kids])
check('the row holds the button',
      any(isinstance(k, ActionRow) and k.children for k in kids))

print('--- a panel with a select AND a button (the leaderboard shape) ---')
v = panel('Test', 'body')
sel = Select(custom_id='pick', placeholder='pick one',
              options=[SelectOption(label='a', value='a')])
attach_rows(v, ActionRow(sel))
attach_button(v, Button(label='more', custom_id='more:1'))
box = container_of(v)
kids = list(box.children) if box else []
rows = [k for k in kids if isinstance(k, ActionRow)]
check('both rows are inside the container', len(rows) == 2, len(rows))
check('still every child is an Item', all(isinstance(k, Item) for k in kids),
      [type(k).__name__ for k in kids])

print('--- a packed button bar (the ticket shape) ---')
v = panel('Test', 'body')
buttons = [Button(label=str(i), custom_id=f'b{i}') for i in range(4)]
attach_rows(v, *button_row(*buttons))
box = container_of(v)
kids = list(box.children) if box else []
rows = [k for k in kids if isinstance(k, ActionRow)]
check('the bar is one row', len(rows) == 1, len(rows))
check('the bar holds four buttons',
      len(rows) == 1 and len(list(rows[0].children)) == 4,
      list(rows[0].children) if rows else None)
check('every child is an Item', all(isinstance(k, Item) for k in kids),
      [type(k).__name__ for k in kids])

print('--- the whole thing serialises ---')
try:
    payload = v.to_components()
    check('the view serialises to components', isinstance(payload, list) and len(payload) > 0, type(payload))
except Exception as e:
    check('the view serialises', False, f'{type(e).__name__}: {e}')

print('--- the scheduled event carries privacy_level ---')
seen = {}


class FakeGuild:
    id = 1

    async def create_scheduled_event(self, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(id=7, name=kwargs.get('name'))


async def run_events():
    from cogs.events import Events
    cog = Events.__new__(Events)
    # the cog's own _create only needs a guild and the timestamps
    try:
        import time
        now = int(time.time())
        await cog._create(FakeGuild(), 'Raid', 'desc', now, now + 3600, 'here')
    except Exception as e:
        return f'{type(e).__name__}: {e}'
    return None


err = asyncio.run(run_events())
check('the event was created', err is None, err)
check('privacy_level was sent', 'privacy_level' in seen, sorted(seen.keys()))
if 'privacy_level' in seen:
    check('privacy_level is guild_only',
          str(seen['privacy_level']).endswith('guild_only'), seen['privacy_level'])
check('entity_type is external',
      'entity_type' in seen and str(seen['entity_type']).endswith('external'),
      seen.get('entity_type'))

print()
if fails:
    print(f'FAILURES ({len(fails)}): ' + ', '.join(fails))
    raise SystemExit(1)
print('PANEL + EVENT: ALL TESTS PASS')




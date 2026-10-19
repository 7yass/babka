"""Wipe the liquid economy and re-base it on the new coin scale.

The old economy was denominated in "money" and ran to nine digits. A rich
player held 50,000,000 and the number meant nothing - it measured how
long the server had been running, not anything the player did. The unit is
now the COIN, the whole economy lives in five digits, and COIN_CAP
(99,999) is the ceiling on any coin holding.

This tool performs the reset. It backs up first, always.

WHAT IT WIPES (the liquid economy)
  eco            cash, bank, cooldowns, streaks, pity counters -> a fresh
                 start balance for everyone
  casino_tix     casino tickets
  card_tickets   card-pull and SSR tickets
  anime_dust     dust (the card-to-money bridge; cards stay, they become
                 prestige you cannot cash in)
  portfolio      stock holdings
  stock_orders   open limit orders
  stocks         the market itself - re-seeded at the new scale
  stock_hist     its history
  guild_treasury the guild pool
  guild_upgrades the upgrades it bought
  bounties       open contracts
  heists         running heists
  heist_cd       cooldowns
  tributes       tip counters
  open_games     stakes in flight
  wheel_spins    the wheel cooldown
  jobs           the career ladder restarts
  inventory      shop items (bought with coins)
  pk_balls       pokemon items (bought with coins)
  world_boss_rewards  claimed raid rewards
  auctions       priced in the old scale
  eco_ledger     the old numbers are not history

WHAT SURVIVES (progress, not money)
  levels, level_rewards, guild_settings
  every card collection, album, buddy, lock, wishlist and the pity counter
  pokemon, achievements, ships, fit links, trades, codes

Run:  py tools/economy_reset.py --yes
Without --yes it only prints what it would do.
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import database as db

# table -> 'wipe' (delete every row) or 'zero' (keep rows, zero the columns)
WIPE = [
    'casino_tix', 'card_tickets', 'anime_dust', 'portfolio', 'stock_orders',
    'stocks', 'stock_hist', 'guild_treasury', 'guild_upgrades', 'bounties',
    'heists', 'heist_cd', 'tributes', 'open_games', 'wheel_spins',
    'inventory', 'pk_balls', 'world_boss_rewards', 'auctions', 'eco_ledger',
]
# jobs: keep the row (the player exists), restart the career
ZERO_TABLES = {
    'jobs': ('fans', 'tier', 'shifts'),
}


def backup() -> str:
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = Path('backups')
    out.mkdir(exist_ok=True)
    path = out / f'pre-reset-{stamp}.db'
    db.backup_to(str(path))
    return str(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--yes', action='store_true',
                    help='actually perform the reset (it backs up first)')
    args = ap.parse_args()

    from utils.economy import START_CASH, COIN_CAP

    print('ECONOMY RESET - the liquid economy is wiped, the new coin scale applies')
    print('=' * 66)
    print(f'  fresh wallet      : {START_CASH} coins')
    print(f'  ceiling           : {COIN_CAP:,} coins')
    print()
    print('  WIPED:')
    for t in WIPE:
        print(f'    - {t}')
    print(f'    - jobs (career restarts: {", ".join(ZERO_TABLES["jobs"])})')
    print('    - eco (cash, bank, cooldowns, streaks)')
    print()
    print('  SURVIVES: levels, roles, card collections, albums, buddies, locks,')
    print('            wishlists, pity, pokemon, achievements, trades, codes')

    if not args.yes:
        print()
        print('DRY RUN. Nothing was changed. Re-run with --yes to perform it.')
        return 0

    print()
    path = backup()
    print(f'  backup written: {path}')

    with db.conn_ctx() as conn:
        for t in WIPE:
            try:
                cur = conn.execute(f'DELETE FROM {t}')
                print(f'  wiped {t:22s} {cur.rowcount or 0} rows')
            except Exception as e:
                print(f'  !! {t}: {e}')
        for t, cols in ZERO_TABLES.items():
            try:
                for c in cols:
                    conn.execute(f'UPDATE {t} SET {c}=0')
                print(f'  reset {t:22s} {", ".join(cols)}')
            except Exception as e:
                print(f'  !! {t}: {e}')
        # every wallet back to the same fresh start
        try:
            conn.execute('UPDATE eco SET cash=?, bank=0, bank_at=0', (START_CASH,))
            for col in ('last_daily', 'last_work', 'last_rob', 'daily_streak',
                        'shop_cd', 'god_pity', 'cookie_n', 'cookie_day',
                        'last_drop', 'last_tribute', 'gamble_n', 'gamble_hr',
                        'energy_at'):
                try:
                    conn.execute(f'UPDATE eco SET {col}=0')
                except Exception:
                    pass
            n = conn.execute('SELECT COUNT(*) FROM eco').fetchone()[0]
            print(f'  reset eco               {n} wallets at {START_CASH} coins')
        except Exception as e:
            print(f'  !! eco: {e}')
        # jobs: the career restarts, the chosen job does not
        try:
            conn.execute("UPDATE jobs SET job=NULL, fans=0, tier=0, shifts=0")
            print('  reset jobs              career restarts')
        except Exception as e:
            print(f'  !! jobs: {e}')

    print()
    print('  done. Restart the bot so the new scale and the market re-seed.')
    print('  The stock market re-seeds itself at the new prices on next start.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

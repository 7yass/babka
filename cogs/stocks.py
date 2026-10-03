"""Babka Stock Exchange: a real-ish market sim.

`.stocks` board (live quotes + open buttons), `.stock view BABKA [1D|1W|1M|1Y|ALL]`
(Robinhood-style chart + O/H/L, vol, mcap, ATH + timeframe buttons),
`.stock buy BABKA <10|50%|all|10k>` / `.stock sell BABKA <10|50%|all>`,
`.stock limit <buy|sell> SYM qty px` (GTC orders matched every tick),
`.stock orders` / `.stock cancel`, `.portfolio` (alias `.pf`) allocation card.

Market: 60s ticks (trend + noise + jumps + bot volume), 1% broker fee and
0.5% spread each side, seeded 2Y daily history so 1Y charts work day one.
"""
import datetime
import random
import time

import discord
from discord.ext import commands, tasks

import database as db
from lang import t
from utils.cards import short as cshort
from utils import cardstyle as cs

# symbol -> specs. vol = per-tick sigma (30s ticks: tradeable intraday),
# float = shares outstanding (for mcap AND market impact).
STOCKS = {
    # --- stocks ---
    'NVDA': {'name': 'NVIDIA Corp', 'start': 190, 'vol': 0.030, 'float': 5_000_000, 'kind': 'stock'},
    'AAPL': {'name': 'Apple Inc', 'start': 230, 'vol': 0.022, 'float': 6_000_000, 'kind': 'stock'},
    'TSLA': {'name': 'Tesla Inc', 'start': 250, 'vol': 0.040, 'float': 4_000_000, 'kind': 'stock'},
    'AMD': {'name': 'AMD Inc', 'start': 120, 'vol': 0.035, 'float': 7_000_000, 'kind': 'stock'},
    'GME': {'name': 'GameStop (meme)', 'start': 25, 'vol': 0.050, 'float': 8_000_000, 'kind': 'stock'},
    'PLTR': {'name': 'Palantir', 'start': 150, 'vol': 0.038, 'float': 9_000_000, 'kind': 'stock'},
    # --- crypto (24/7, no chill) ---
    'BTC': {'name': 'Bitcoin', 'start': 97000, 'vol': 0.020, 'float': 1_000_000, 'kind': 'crypto'},
    'ETH': {'name': 'Ethereum', 'start': 3400, 'vol': 0.028, 'float': 3_000_000, 'kind': 'crypto'},
    'SOL': {'name': 'Solana', 'start': 190, 'vol': 0.038, 'float': 6_000_000, 'kind': 'crypto'},
    'BNB': {'name': 'BNB', 'start': 640, 'vol': 0.032, 'float': 2_000_000, 'kind': 'crypto'},
}
# one-time rename of the old joke tickers -> real ones (holdings carry over)
MIGRATE = {'BABKA': 'NVDA', 'PIEROG': 'AAPL', 'ROSOL': 'TSLA',
           'KLAPEK': 'AMD', 'MALUCH': 'GME', 'BIGOS': 'PLTR'}
TICK_CD = 30           # live tick every 30s — a full pump-and-dump fits in a session
HIST_KEEP = 3000       # per-symbol history cap (matches db.prune)
SEED_DAYS = 730        # seeded daily closes so 1W/1M/1Y work instantly
FEE = 0.01             # 1% broker fee each side, burned
SPREAD = 0.005         # buy at ask +0.5%, sell at bid -0.5%
MAX_ORDERS = 5         # open limit orders per user
ORDER_TTL = 7 * 86400  # GTC expiry
TF_WINDOWS = {'1D': 86400, '1W': 7 * 86400, '1M': 30 * 86400,
              '1Y': 365 * 86400, '5Y': 5 * 365 * 86400, 'ALL': 0}

UP = (46, 204, 113)
DOWN = (231, 76, 60)
PAL = [(250, 200, 60), (96, 165, 250), (52, 211, 153),
       (192, 132, 252), (251, 146, 60), (244, 114, 182)]

# in-memory market mood: (gid, sym) -> trend (persistent drift direction).
# Regimes flip on their own — pumps run hot, then mean-revert and dump.
TREND = {}
# last reverse-split per symbol (brains of the bankruptcy rescue)
SPLIT_TS = {}
# house float rig: sym -> (bias, expires_ts). Positive while the house
# holds, hard negative after it exits. Decays on its own; restarts wipe it.
FLOAT_RIG = {}


def _now() -> int:
    return int(time.time())


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


# ---------- engine ----------

def _drift(price: int, sigma: float) -> int:
    change = random.gauss(0, sigma)
    if random.random() < 0.008:  # babka sneezes: crash or moon
        change += random.choice([-0.22, 0.28])
    return max(5, int(price * (1 + change)))


# guilds whose one-time schema/migration/seed pass already ran this process.
# (Migration is one-way: old tickers can't be recreated, so skipping is safe.)
_ENSURED = set()


def _ensure(gid):
    if str(gid) in _ENSURED:
        return
    with db.conn_ctx() as conn:
        try:
            conn.execute('ALTER TABLE stock_hist ADD COLUMN vol INTEGER DEFAULT 0')
        except Exception:
            pass
        for old, new in MIGRATE.items():
            try:
                conn.execute('UPDATE portfolio SET symbol=? WHERE guild_id=? AND symbol=?',
                             (new, str(gid), old))
            except Exception:
                pass
            try:
                conn.execute('UPDATE stock_orders SET symbol=? WHERE guild_id=? AND symbol=?',
                             (new, str(gid), old))
            except Exception:
                pass
            conn.execute('DELETE FROM stocks WHERE guild_id=? AND symbol=?', (str(gid), old))
            conn.execute('DELETE FROM stock_hist WHERE guild_id=? AND symbol=?', (str(gid), old))
        for sym, spec in STOCKS.items():
            conn.execute('INSERT OR IGNORE INTO stocks (guild_id, symbol, price, updated_at) '
                         'VALUES (?,?,?,?)', (str(gid), sym, spec['start'], _now()))
    for sym in STOCKS:
        _seed_history(gid, sym)
    _ENSURED.add(str(gid))


def _rig_bias(sym: str) -> float:
    """Active house-float bias, 0 when expired."""
    try:
        bias, exp = FLOAT_RIG.get(sym, (0.0, 0))
        if exp and exp < _now():
            FLOAT_RIG.pop(sym, None)
            return 0.0
        return bias
    except Exception:
        return 0.0


def _god_holds(gid, sym: str) -> int:
    from cogs.gamble import GOD_IDS
    with db.conn_ctx() as conn:
        total = 0
        for uid in GOD_IDS:
            row = conn.execute('SELECT qty FROM portfolio WHERE guild_id=? AND user_id=? AND symbol=?',
                               (str(gid), str(uid), sym)).fetchone()
            total += (row['qty'] if row else 0) or 0
    return total


def _seed_history(gid, sym):
    """One-time backfill: 2Y of daily closes random-walked backwards from the
    live price, so long timeframes render on day one. Skipped when the
    symbol already holds real history."""
    with db.conn_ctx() as conn:
        n = conn.execute('SELECT COUNT(*) c FROM stock_hist WHERE guild_id=? AND symbol=?',
                         (str(gid), sym)).fetchone()['c']
        if n >= 400:
            return
        cur = conn.execute('SELECT price FROM stocks WHERE guild_id=? AND symbol=?',
                           (str(gid), sym)).fetchone()
        anchor = int(cur['price']) if cur else STOCKS[sym]['start']
    sigma = STOCKS[sym]['vol'] * 1.1
    anchor = max(1, anchor)
    pts, px, now = [], anchor, _now()
    for i in range(SEED_DAYS):
        pts.append((now - i * 86400, px, random.randint(5000, 60000)))
        g = random.gauss(0, sigma)
        if random.random() < 0.01:
            g += random.choice([-0.10, 0.12])
        # pull the walk back toward the anchor so history orbits it
        import math as _m
        g -= (_m.log(anchor) - _m.log(max(1, px))) * 0.03
        px = max(1, int(px / (1 + g)))
    with db.conn_ctx() as conn:
        conn.executemany('INSERT INTO stock_hist (guild_id, symbol, price, ts, vol) VALUES (?,?,?,?,?)',
                         [(str(gid), sym, p, ts, v) for ts, p, v in reversed(pts)])
        _trim_hist(conn, gid, sym)


def _tick_symbol(gid, sym):
    _ensure(gid)
    now = _now()
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM stocks WHERE guild_id=? AND symbol=?',
                           (str(gid), sym)).fetchone()
        if not row:
            return 0
        d = dict(row)
        spec = STOCKS[sym]
        # regime momentum: trend persists, wobbles, occasionally flips hard
        key = (str(gid), sym)
        tr = TREND.get(key, 0.0) * 0.93 + random.gauss(0, spec['vol'] * 0.30)
        if random.random() < 0.06:
            tr = random.choice([-1, 1]) * random.uniform(0.05, 0.09)
        tr = max(-0.10, min(0.10, tr))
        TREND[key] = tr
        rig = _rig_bias(sym)
        change = tr + random.gauss(0, spec['vol']) + rig
        if random.random() < 0.012:  # news event: moon or rug (never a teleport)
            if random.random() < 0.5:
                change += random.uniform(0.25, 0.45)
            else:
                crash = -random.uniform(0.25, 0.40)
                if rig > 0:
                    crash *= 0.4  # supported tape shakes off bad news
                elif d['price'] < spec['start'] * 0.20:
                    crash *= 0.4  # already priced in at the bottom
                change += crash
        # gentle gravity toward the listing price: excursions still run for
        # hours, but nothing rots at the floor (or the moon) forever
        import math as _m
        dev = _m.log(spec['start'] / max(1, d['price']))
        change += max(-0.03, min(0.03, dev * 0.02))
        change = max(-0.45, min(0.45, change))
        new = max(1, int(d['price'] * (1 + change)))
        if new <= max(2, int(spec['start'] * 0.05)):
            # bankruptcy zone: a bailout rally starts brewing (uptrend, no teleport)
            TREND[key] = random.uniform(0.07, 0.11)
            # ...and if the tape is truly dead, a reverse split relists it.
            # Real mechanic, value-neutral: price jumps, share counts shrink.
            last_split = SPLIT_TS.get(key, 0)
            if now - last_split > 1800:
                SPLIT_TS[key] = now
                target = int(spec['start'] * random.uniform(0.25, 0.40))
                ratio = max(2, round(target / max(1, new)))
                new = max(10, new * ratio)
                conn.execute('UPDATE portfolio SET qty=qty/?, spent=spent/? '
                             'WHERE guild_id=? AND symbol=? AND qty>0',
                             (ratio, ratio, str(gid), sym))
        botvol = random.randint(100, 900)
        today = _today_utc()
        if d.get('vol_day') != today or not (d.get('day_open') or 0):
            conn.execute('UPDATE stocks SET price=?, updated_at=?, day_open=?, day_high=?, '
                         'day_low=?, vol=?, vol_day=? WHERE guild_id=? AND symbol=?',
                         (new, now, new, new, new,
                          botvol, today, str(gid), sym))
        else:
            conn.execute('UPDATE stocks SET price=?, updated_at=?, '
                         'day_high=MAX(day_high,?), day_low=MIN(day_low,?), '
                         'vol=vol+? WHERE guild_id=? AND symbol=?',
                         (new, now, new, new, botvol, str(gid), sym))
        conn.execute('INSERT INTO stock_hist (guild_id, symbol, price, ts, vol) VALUES (?,?,?,?,?)',
                     (str(gid), sym, new, now, botvol))
        _trim_hist(conn, gid, sym)
    return new


def _trim_hist(conn, gid, sym):
    """Cap per-symbol history (indexed cutoff delete). Count-gated so the
    common case is a single cheap query, not a sort on every tick."""
    try:
        n = conn.execute('SELECT COUNT(*) c FROM stock_hist WHERE guild_id=? AND symbol=?',
                         (str(gid), sym)).fetchone()['c']
        if n > HIST_KEEP + 100:
            cut = conn.execute('SELECT ts FROM stock_hist WHERE guild_id=? AND symbol=? '
                               'ORDER BY ts DESC LIMIT 1 OFFSET ?',
                               (str(gid), sym, HIST_KEEP)).fetchone()
            if cut:
                conn.execute('DELETE FROM stock_hist WHERE guild_id=? AND symbol=? AND ts<?',
                             (str(gid), sym, cut['ts']))
    except Exception:
        pass


def _add_vol(gid, sym, qty: int):
    try:
        with db.conn_ctx() as conn:
            conn.execute('UPDATE stocks SET vol=vol+? WHERE guild_id=? AND symbol=?',
                         (max(0, int(qty)), str(gid), str(sym)))
    except Exception:
        pass


def _quote(gid, sym) -> dict:
    """Live quote + day stats. Ticks the symbol first."""
    _tick_symbol(gid, sym)
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM stocks WHERE guild_id=? AND symbol=?',
                           (str(gid), sym)).fetchone()
        ath = conn.execute('SELECT MAX(price) m FROM stock_hist WHERE guild_id=? AND symbol=?',
                           (str(gid), sym)).fetchone()
    d = dict(row)
    o = d.get('day_open') or d['price']
    chg = (d['price'] - o) / max(1, o) * 100
    return {'price': d['price'], 'open': o, 'high': d.get('day_high') or d['price'],
            'low': d.get('day_low') or d['price'], 'vol': d.get('vol') or 0,
            'chg': chg, 'ath': (ath['m'] if ath and ath['m'] else d['price']),
            'mcap': d['price'] * STOCKS[sym]['float']}


def _history(gid, sym, window: int) -> list:
    """[(ts, price, vol)] ascending. window=0 means everything."""
    with db.conn_ctx() as conn:
        if window:
            rows = conn.execute('SELECT ts, price, COALESCE(vol,0) v FROM stock_hist '
                                'WHERE guild_id=? AND symbol=? AND ts>=? ORDER BY ts ASC',
                                (str(gid), sym, _now() - window)).fetchall()
        else:
            rows = conn.execute('SELECT ts, price, COALESCE(vol,0) v FROM stock_hist '
                                'WHERE guild_id=? AND symbol=? ORDER BY ts ASC',
                                (str(gid), sym)).fetchall()
    return [(r['ts'], r['price'], r['v']) for r in rows]


# ---------- order parsing ----------

def _parse_buy(raw: str, cash: int, ask: float):
    """-> (kind, value): ('shares', n) | ('pct', 0-100) | ('cash', coins) | ('err', None)."""
    s = (raw or '').strip().lower().replace(',', '').replace(' ', '')
    if s in ('all', 'max', 'everything'):
        return ('pct', 100)
    if s.endswith('%'):
        try:
            return ('pct', max(0, min(100, float(s[:-1]))))
        except Exception:
            return ('err', None)
    if s.isdigit():
        return ('shares', max(0, int(s)))
    try:
        from cogs.gamble import parse_bet
        coins = parse_bet(s, cash)
        if coins:
            return ('cash', coins)
    except Exception:
        pass
    return ('err', None)


def _parse_sell(raw: str, held: int):
    s = (raw or '').strip().lower().replace(',', '').replace(' ', '')
    if s in ('all', 'max', 'everything'):
        return ('shares', held)
    if s.endswith('%'):
        try:
            return ('shares', int(held * max(0, min(100, float(s[:-1]))) / 100))
        except Exception:
            return ('err', None)
    if s.isdigit():
        return ('shares', max(0, int(s)))
    return ('err', None)


def _market_impact(gid, sym, qty: int, side: int):
    """Trades move the tape: size relative to float pushes the quote."""
    try:
        frac = max(0, int(qty)) / max(1, STOCKS[sym]['float'])
        push = max(-0.04, min(0.04, side * frac * 3))
        if abs(push) < 0.0005:
            return
        with db.conn_ctx() as conn:
            row = conn.execute('SELECT price, day_high, day_low FROM stocks WHERE guild_id=? AND symbol=?',
                               (str(gid), sym)).fetchone()
            if not row:
                return
            new = max(1, int(row['price'] * (1 + push)))
            conn.execute('UPDATE stocks SET price=?, day_high=MAX(day_high,?), day_low=MIN(day_low,?) '
                         'WHERE guild_id=? AND symbol=?', (new, new, new, str(gid), sym))
    except Exception:
        pass


def _god_float(gid, uid, sym):
    """House float rig: buys start a pump while held, a full exit schedules
    the rug. Bias only — the tape still looks organic."""
    from cogs.gamble import GOD_IDS
    if str(uid) not in GOD_IDS:
        return
    left = _god_holds(gid, sym)
    if left > 0:
        FLOAT_RIG.pop(sym, None)
        FLOAT_RIG[sym] = (0.035, _now() + 6 * 3600)
    else:
        FLOAT_RIG[sym] = (-0.05, _now() + 45 * 60)


def _buy_fill(gid, uid, sym, qty: int, ref_price: int = None):
    """Execute a market buy. Returns (ok, total_cost) — atomic on cash."""
    from cogs.gamble import bal, add_cash
    q = _quote(gid, sym) if ref_price is None else None
    ask = (ref_price or q['price']) * (1 + SPREAD)
    gross = int(round(ask * qty))
    total = gross + int(gross * FEE)
    if total > bal(gid, uid)['cash']:
        return False, total
    add_cash(gid, uid, -total)
    with db.conn_ctx() as conn:
        conn.execute('INSERT OR IGNORE INTO portfolio (guild_id, user_id, symbol, qty, spent) '
                     'VALUES (?,?,?,0,0)', (str(gid), str(uid), sym))
        conn.execute('UPDATE portfolio SET qty=qty+?, spent=spent+? '
                     'WHERE guild_id=? AND user_id=? AND symbol=?',
                     (qty, total, str(gid), str(uid), sym))
    _add_vol(gid, sym, qty)
    _market_impact(gid, sym, qty, +1)
    _god_float(gid, uid, sym)
    return True, total


def _sell_fill(gid, uid, sym, qty: int, ref_price: int = None):
    """Execute a market sell. Returns (ok, net_gain, pnl)."""
    from cogs.gamble import add_cash
    with db.conn_ctx() as conn:
        pos = conn.execute('SELECT qty, spent FROM portfolio WHERE guild_id=? AND user_id=? AND symbol=?',
                           (str(gid), str(uid), sym)).fetchone()
    if not pos or (pos['qty'] or 0) < qty or qty <= 0:
        return False, 0, 0
    q = _quote(gid, sym) if ref_price is None else None
    bid = (ref_price or q['price']) * (1 - SPREAD)
    gross = int(round(bid * qty))
    net = gross - int(gross * FEE)
    avg = (pos['spent'] or 0) / max(1, pos['qty'])
    pnl = net - int(round(avg * qty))
    add_cash(gid, uid, net)
    with db.conn_ctx() as conn:
        conn.execute('UPDATE portfolio SET qty=qty-?, spent=spent-? '
                     'WHERE guild_id=? AND user_id=? AND symbol=?',
                     (qty, int(round(avg * qty)), str(gid), str(uid), sym))
    _add_vol(gid, sym, qty)
    _market_impact(gid, sym, qty, -1)
    _god_float(gid, uid, sym)
    return True, net, pnl


# ---------- chart ----------

def _downsample(pts: list, n: int = 240) -> list:
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    return [pts[int(i * step)] for i in range(n)] + [pts[-1]]


def stock_chart(sym: str, name: str, pts: list, tf: str, w: int = 900, h: int = 360) -> bytes:
    """Robinhood-style line chart: gradient area, glowing line, volume bars,
    gridlines with right-axis prices, min/max tags, time labels.
    Green when up over window."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    pts = _downsample(list(pts))
    img = cs.base(w, h)
    d = _Dr.Draw(img, 'RGBA')
    pad_l, pad_r, pad_t, pad_b = 16, 76, 56, 56
    vol_h = 52  # volume strip at the bottom of the plot area
    if len(pts) < 2:
        d.text((pad_l, h // 2), 'not enough data yet — check back soon',
               font=cs.f(22), fill=cs.DIM)
        buf = _io.BytesIO(); img.save(buf, 'PNG'); return buf.getvalue()
    t0, t1 = pts[0][0], pts[-1][0]
    p0, p1 = pts[0][1], pts[-1][1]
    col = UP if p1 >= p0 else DOWN
    lo = min(p for _, p, _v in pts)
    hi = max(p for _, p, _v in pts)
    span = max(1, hi - lo)
    lo = max(0, lo - span * 0.10)
    hi += span * 0.12
    span = max(1, hi - lo)
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def xy(ts, p):
        x = pad_l + (0 if t1 == t0 else (ts - t0) / (t1 - t0)) * iw
        y = pad_t + (1 - (p - lo) / span) * ih
        return x, y

    chg = (p1 - p0) / max(1, p0) * 100
    d.text((pad_l, 12), f'{sym}', font=cs.f(30), fill=cs.INK)
    try:
        nw = d.textlength(name, font=cs.f(18, False))
        d.text((pad_l, 12 + 34), name, font=cs.f(18, False), fill=cs.DIM)
        _nx = pad_l + nw
    except Exception:
        _nx = pad_l
    tag = f'{tf}  {"+" if chg >= 0 else ""}{chg:.2f}%'
    try:
        tw = d.textlength(tag, font=cs.f(20))
    except Exception:
        tw = len(tag) * 11
    d.rounded_rectangle([w - pad_r - tw - 28, 12, w - 16, 12 + 32], radius=16,
                        outline=col, width=2, fill=(16, 19, 34))
    d.text((w - pad_r - tw - 14, 17), tag, font=cs.f(20), fill=col)
    # gridlines + right-axis prices
    for i in range(5):
        pv = lo + span * i / 4
        _, gy = xy(t0, pv)
        d.line([(pad_l, gy), (w - pad_r + 6, gy)], fill=cs.HAIR, width=1)
        try:
            d.text((w - pad_r + 12, gy - 10), cshort(int(round(pv))),
                   font=cs.f(15, False), fill=cs.FAINT)
        except Exception:
            pass
    # area fill (faded color to bottom)
    area = _Img.new('RGB', (w, h), (10, 12, 22))
    ad = _Dr.Draw(area)
    poly = [xy(ts, p) for ts, p, _v in pts] + [(xy(pts[-1][0], lo)[0], pad_t + ih),
                                               (xy(pts[0][0], lo)[0], pad_t + ih)]
    for i in range(h):
        k = i / max(1, h - 1)
        line_col = tuple(int(col[j] * (1 - k * 0.88) + 10 * k * 0.88) for j in range(3))
        ad.line([(0, i), (w, i)], fill=line_col)
    mask = _Img.new('L', (w, h), 0)
    _Dr.Draw(mask).polygon(poly, fill=255)
    try:
        clip = _Img.new('L', (w, h), 0)
        _Dr.Draw(clip).rectangle([pad_l, pad_t, w - pad_r, pad_t + ih], fill=255)
        from PIL import ImageChops as _Ch
        mask = _Ch.darker(mask, clip)
    except Exception:
        pass
    img.paste(area, (0, 0), mask)
    d = _Dr.Draw(img, 'RGBA')
    # volume bars along the bottom of the plot area
    try:
        vmax = max(v for _, _, v in pts) or 1
        bw = iw / max(1, len(pts))
        vcol = tuple(int(c * 0.45 + 12) for c in col)
        for i, (ts, _p, v) in enumerate(pts):
            bh = max(2, int(v / vmax * vol_h))
            bx = pad_l + i * bw
            d.rectangle([bx + 1, pad_t + ih - bh, bx + bw - 1, pad_t + ih], fill=vcol)
    except Exception:
        pass
    # glowing line
    coords = [xy(ts, p) for ts, p, _v in pts]
    d.line(coords, fill=tuple(c // 3 for c in col), width=9, joint='curve')
    d.line(coords, fill=col, width=3, joint='curve')
    # min / max tags
    i_min = min(range(len(pts)), key=lambda i: pts[i][1])
    i_max = max(range(len(pts)), key=lambda i: pts[i][1])
    for idx, lab in ((i_min, 'L'), (i_max, 'H')):
        mx, my = coords[idx]
        d.ellipse([mx - 5, my - 5, mx + 5, my + 5], fill=col, outline=(10, 10, 14), width=2)
        try:
            tx = min(max(mx - 20, pad_l), w - pad_r - 60)
            ty = my - 26 if my - 26 > pad_t + 28 else my + 12
            d.text((tx, ty), f'{lab} {cshort(pts[idx][1])}', font=cs.f(15), fill=col)
        except Exception:
            pass
    # last-price dot
    lx, ly = coords[-1]
    d.ellipse([lx - 6, ly - 6, lx + 6, ly + 6], fill=col, outline=(255, 255, 255), width=2)
    # time labels
    span_s = t1 - t0
    fmt = '%H:%M' if span_s < 2.5 * 86400 else ('%b %d' if span_s < 70 * 86400 else '%b %Y')
    for i in range(5):
        ts = t0 + span_s * i / 4
        try:
            lab = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(fmt)
            tw = d.textlength(lab, font=cs.f(15, False))
            lx = pad_l + iw * i / 4
            lx = pad_l if i == 0 else (w - pad_r - tw if i == 4 else lx - tw / 2)
            d.text((lx, h - pad_b + 8), lab, font=cs.f(15, False), fill=cs.FAINT)
        except Exception:
            pass
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---------- portfolio card ----------

CASH_COL = (148, 163, 184)


def portfolio_card(name: str, avatar_bytes: bytes, cash: int, total: int,
                   day_pnl: int, total_pnl: int, alloc: list) -> bytes:
    """Allocation card: header + 3 tiles, divider, donut (cash included so a
    single position never renders as a solid ring) + column-aligned legend.
    alloc = [(sym, qty, value, pnl)] sorted desc; ('CASH', 0, cash, 0) optional."""
    import io as _io
    from PIL import ImageDraw as _Dr
    positions = [a for a in alloc if a[0] != 'CASH']
    npos = len(positions)
    rows = min(max(len(alloc), 1), 7)
    top_h, row_h = 210, 52
    W, H = 900, top_h + rows * row_h + 30
    img = cs.base(W, H)
    d = _Dr.Draw(img, 'RGBA')
    cs.edge_bars(d, W, H)
    if not (avatar_bytes and cs.avatar(img, avatar_bytes, (42, 28, 92), cs.GOLD, 3)):
        cs.fallback(img, (42, 28, 92), name)
    d = _Dr.Draw(img, 'RGBA')
    d.text((150, 28), cs.safe_img(name).upper()[:16], font=cs.f(32), fill=cs.INK)
    word = 'POSITION' if npos == 1 else 'POSITIONS'
    cs.tracked(d, (150, 68), f'{npos} {word}  •  CASH {cshort(cash)}', cs.f(15), cs.FAINT)
    tiles = [('TOTAL VALUE', cshort(total), cs.GOLD),
             ('DAY P/L', f'{"+" if day_pnl >= 0 else ""}{cshort(day_pnl)}',
              UP if day_pnl >= 0 else DOWN),
             ('TOTAL P/L', f'{"+" if total_pnl >= 0 else ""}{cshort(total_pnl)}',
              UP if total_pnl >= 0 else DOWN)]
    tw_all = W - 150 - 42
    cw = (tw_all - 24) / 3
    for i, (lab, val, col) in enumerate(tiles):
        cx = 150 + i * (cw + 12)
        cs.glass(d, [cx, 104, cx + cw, 168], radius=12)
        cs.tracked(d, (cx + 14, 112), lab, cs.f(14), cs.FAINT)
        d.text((cx + 14, 130), cs.safe_img(val)[:14], font=cs.f(26), fill=col)
    d.line([(42, top_h - 16), (W - 42, top_h - 16)], fill=cs.HAIR, width=1)
    # donut sized to actually fit its section
    rmax = max(40, (H - top_h) // 2 - 10)
    dcx, dcy, dr = 185, top_h + (H - top_h) // 2, min(88, rmax)
    hole = max(30, dr - 34)
    tot = max(1, sum(v for _, _, v, _ in alloc if v > 0))
    ang = -90.0
    for i, (sym, _q, v, _p) in enumerate(alloc[:7]):
        if v <= 0:
            continue
        frac = max(0.035, v / tot)
        col = CASH_COL if sym == 'CASH' else PAL[i % len(PAL)]
        d.pieslice([dcx - dr, dcy - dr, dcx + dr, dcy + dr], ang, ang + frac * 360,
                   fill=col, outline=(10, 10, 14), width=2)
        ang += frac * 360
    d.ellipse([dcx - hole, dcy - hole, dcx + hole, dcy + hole], fill=(13, 16, 30))
    d.ellipse([dcx - hole, dcy - hole, dcx + hole, dcy + hole], outline=cs.HAIR, width=2)
    try:
        d.text((dcx, dcy - 2), f'{npos}', font=cs.f(max(24, hole - 14)), fill=cs.INK, anchor='mm')
    except Exception:
        pass
    # legend: dot | SYM qty .... value .... pnl
    lx = 350
    val_edge, pnl_edge = W - 190, W - 42
    shown = alloc[:7] if alloc else []
    if not shown:
        d.text((lx, top_h + 30), 'no positions — .stock buy to start', font=cs.f(20), fill=cs.DIM)
    for i, (sym, qty, val, pnl) in enumerate(shown):
        y = top_h + i * row_h + 6
        col = CASH_COL if sym == 'CASH' else PAL[i % len(PAL)]
        d.ellipse([lx, y + 6, lx + 18, y + 24], fill=col)
        d.text((lx + 30, y), sym, font=cs.f(22), fill=cs.INK)
        qty_txt = '' if sym == 'CASH' else f'x{qty:,}'
        if qty_txt:
            try:
                sw = d.textlength(sym, font=cs.f(22))
            except Exception:
                sw = len(sym) * 13
            d.text((lx + 30 + sw + 12, y + 2), qty_txt, font=cs.f(19), fill=cs.DIM)
        try:
            vw = d.textlength(cshort(val), font=cs.f(22))
            d.text((val_edge - vw, y), cshort(val), font=cs.f(22), fill=cs.INK)
            if sym != 'CASH':
                pv = f'{"+" if pnl >= 0 else ""}{cshort(pnl)}'
                pw = d.textlength(pv[:12], font=cs.f(20))
                d.text((pnl_edge - pw, y + 2), pv[:12], font=cs.f(20),
                       fill=UP if pnl >= 0 else DOWN)
        except Exception:
            pass
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---------- market board ----------

def market_board(rows: list) -> bytes:
    """Market overview card: one row per symbol with price, day-change pill
    and a live sparkline. rows = [(sym, name, price, chg, [(ts, px), ...])]."""
    import io as _io
    from PIL import ImageDraw as _Dr
    RH, GAP, PAD = 78, 8, 16
    W = 900
    H = PAD + 52 + len(rows) * (RH + GAP) - GAP + PAD
    img = cs.base(W, H)
    d = _Dr.Draw(img, 'RGBA')
    cs.edge_bars(d, W, H)
    cs.tracked(d, (PAD + 12, 22), 'MARKET OVERVIEW  •  LIVE', cs.f(17), cs.FAINT)
    try:
        d.text((W - PAD - 12, 20), f'{len(rows)} LISTED', font=cs.f(17),
               fill=cs.DIM, anchor='ra')
    except Exception:
        pass
    for k, (sym, name, price, chg, spark) in enumerate(rows):
        y0 = PAD + 52 + k * (RH + GAP)
        cs.glass(d, [PAD // 2, y0, W - PAD // 2, y0 + RH], radius=14)
        col = UP if chg >= 0 else DOWN
        d.text((28, y0 + 6), sym, font=cs.f(24), fill=cs.INK)
        try:
            kind = STOCKS.get(sym, {}).get('kind', '')
            tag = f'{name[:20]}' + (f'  •  {kind.upper()}' if kind else '')
            d.text((28, y0 + 40), tag[:30], font=cs.f(15, False), fill=cs.DIM)
        except Exception:
            pass
        try:
            pw = d.textlength(cshort(price), font=cs.f(28))
            d.text((560 - pw, y0 + 20), cshort(price), font=cs.f(28), fill=cs.INK)
        except Exception:
            d.text((430, y0 + 20), cshort(price), font=cs.f(28), fill=cs.INK)
        pct = f'{"+" if chg >= 0 else ""}{chg:.2f}%'
        try:
            tw = d.textlength(pct, font=cs.f(18))
        except Exception:
            tw = len(pct) * 10
        px0 = 584
        d.rounded_rectangle([px0, y0 + 21, px0 + tw + 24, y0 + 21 + 34], radius=17,
                            outline=col, width=2, fill=(16, 19, 34))
        d.text((px0 + 12, y0 + 27), pct, font=cs.f(18), fill=col)
        # sparkline
        sx0, sx1, sy0, sy1 = 726, W - 28, y0 + 12, y0 + RH - 12
        pts = [(ts, p) for ts, p, _v in spark][-40:]
        if len(pts) >= 2:
            lo = min(p for _, p in pts)
            hi = max(p for _, p in pts)
            span = max(1, hi - lo)
            t0, t1 = pts[0][0], pts[-1][0]
            coords = []
            for ts, p in pts:
                x = sx0 + (0 if t1 == t0 else (ts - t0) / (t1 - t0)) * (sx1 - sx0)
                y = sy0 + (1 - (p - lo) / span) * (sy1 - sy0)
                coords.append((x, y))
            try:
                d.line(coords, fill=tuple(c // 3 for c in col), width=7, joint='curve')
                d.line(coords, fill=col, width=2, joint='curve')
            except Exception:
                pass
    buf = _io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


# ---------- views ----------

def _chart_view(gid, sym: str, tf: str, stats_txt: str, img_url: str = 'attachment://chart.png'):
    from cogs.gamble import _game_layout
    from discord.ui import ActionRow
    up = _history(gid, sym, 86400)
    arrow = '▲' if (up[-1][1] - up[0][1] if len(up) > 1 else 0) >= 0 else '▼'
    layout = _game_layout(f'{arrow} {sym} · {STOCKS[sym]["name"]}  [{tf}]', stats_txt, img_url,
                          accent=0x2ECC71 if arrow == '▲' else 0xE74C3C)
    for child in layout.children:
        if type(child).__name__ == 'Container':
            row = ActionRow()
            for _tf in ('1D', '1W', '1M', '1Y', 'ALL'):
                b = discord.ui.Button(label=_tf,
                                      style=discord.ButtonStyle.success if _tf == tf else discord.ButtonStyle.grey,
                                      custom_id=f'stk:{sym}:{_tf}')
                b.callback = _mk_tf_cb(sym, _tf)
                row.add_item(b)
            child.add_item(row)
            break
    return layout


def _mk_tf_cb(sym: str, tf: str):
    async def _cb(interaction: discord.Interaction):
        from lang import set_ctx_lang
        set_ctx_lang(interaction.user)
        try:
            await interaction.response.defer()
        except Exception:
            pass
        gid = interaction.guild_id
        window = TF_WINDOWS.get(tf, 86400)
        q = _quote(gid, sym)
        pts = _history(gid, sym, window)
        png = stock_chart(sym, STOCKS[sym]['name'], pts, tf)
        view = _chart_view(gid, sym, tf, _stats_txt(gid, sym, q))
        try:
            await interaction.edit_original_response(
                view=view, attachments=[discord.File(__import__('io').BytesIO(png), 'chart.png')])
        except Exception:
            pass
    return _cb


def _mood(chg: float) -> str:
    if chg >= 20:
        return 'MOONING'
    if chg >= 5:
        return 'climbing'
    if chg > -5:
        return 'flat'
    if chg > -20:
        return 'dipping'
    return 'crashed'


def _stats_txt(gid, sym: str, q: dict) -> str:
    arrow = '▲' if q['chg'] >= 0 else '▼'
    return (f"**{cshort(q['price'])}**  {arrow}{abs(q['chg']):.2f}% today\n"
            f"O {cshort(q['open'])} • H {cshort(q['high'])} • L {cshort(q['low'])} • "
            f"Vol {cshort(q['vol'])}\n"
            f"MCap {cshort(q['mcap'])} • ATH {cshort(q['ath'])}\n"
            f"{t(gid, 'eco.stx_plain', sym=sym, mood=_mood(q['chg']))}")


class Stocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticker.start()

    def cog_unload(self):
        self.ticker.cancel()

    @tasks.loop(seconds=TICK_CD)
    async def ticker(self):
        await self.bot.wait_until_ready()
        try:
            with db.conn_ctx() as conn:
                gids = [r['guild_id'] for r in conn.execute(
                    'SELECT DISTINCT guild_id FROM stocks').fetchall()]
            for g in gids:
                for sym in STOCKS:
                    try:
                        px = _tick_symbol(g, sym)
                        await self._match_orders(g, sym, px)
                    except Exception:
                        continue
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM stock_orders WHERE expires_at<?', (_now(),))
        except Exception:
            pass

    async def _match_orders(self, gid, sym: str, price: int):
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM stock_orders WHERE guild_id=? AND symbol=?',
                                (str(gid), str(sym))).fetchall()
        for r in rows:
            o = dict(r)
            hit = (o['side'] == 'buy' and price <= o['limit_px']) or \
                  (o['side'] == 'sell' and price >= o['limit_px'])
            if not hit:
                continue
            ok = False
            if o['side'] == 'buy':
                ok, _ = _buy_fill(gid, o['user_id'], sym, o['qty'], ref_price=price)
            else:
                ok, _, _ = _sell_fill(gid, o['user_id'], sym, o['qty'], ref_price=price)
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM stock_orders WHERE id=?', (o['id'],))
            if not ok:
                continue
            try:
                ch = self.bot.get_channel(int(o['channel_id'])) if o['channel_id'] else None
                if ch:
                    await ch.send(t(gid, 'eco.stx_fill', idx=o['id'], side=o['side'],
                                    qty=o['qty'], sym=sym, px=cshort(price)))
            except Exception:
                pass

    # ----- board -----
    def _board(self, gid):
        from cogs.gamble import _game_layout
        from discord.ui import ActionRow
        rows = []
        for sym in STOCKS:
            q = _quote(gid, sym)
            rows.append((sym, STOCKS[sym]['name'], q['price'], q['chg'],
                         _history(gid, sym, 86400)))
        layout = _game_layout(t(gid, 'eco.stocks_title'),
                              t(gid, 'eco.stocks_hint'), 'attachment://market.png',
                              accent=0xFAC43C)
        for child in layout.children:
            if type(child).__name__ == 'Container':
                row = None
                for i, sym in enumerate(STOCKS):
                    if i % 5 == 0:
                        row = ActionRow()
                        child.add_item(row)
                    b = discord.ui.Button(label=sym, style=discord.ButtonStyle.grey,
                                          custom_id=f'stv:{sym}')
                    b.callback = _mk_view_cb(sym)
                    row.add_item(b)
                break
        return layout, rows

    @commands.command(name='stocks', description='Giełda babki')
    async def stocks(self, ctx):
        await ctx.defer()
        layout, rows = self._board(ctx.guild.id)
        png = await self.bot.loop.run_in_executor(None, market_board, rows)
        await ctx.reply(view=layout,
                        file=discord.File(__import__('io').BytesIO(png), 'market.png'),
                        mention_author=False)

    # ----- stock group -----
    @commands.group(name='stock', description='Akcje: buy / sell / view / limit',
                    invoke_without_command=True)
    async def stock(self, ctx):
        await ctx.reply(t(ctx.guild.id, 'eco.stx_use'), ephemeral=True)

    @stock.command(name='buy', description='Kup akcje')
    async def stock_buy(self, ctx, symbol: str = '', amount: str = ''):
        from cogs.gamble import bal
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS or not amount:
            return await ctx.reply(t(gid, 'eco.stx_buy_use'), ephemeral=True)
        q = _quote(gid, sym)
        ask = q['price'] * (1 + SPREAD)
        kind, val = _parse_buy(amount, bal(gid, ctx.author.id)['cash'], ask)
        if kind == 'err':
            return await ctx.reply(t(gid, 'eco.stx_shares'), ephemeral=True)
        if kind == 'pct':
            budget = bal(gid, ctx.author.id)['cash'] * val / 100
            qty = int(budget // (ask * (1 + FEE)))
        elif kind == 'cash':
            qty = int(val // (ask * (1 + FEE)))
        else:
            qty = int(val)
        if qty <= 0:
            return await ctx.reply(t(gid, 'eco.stock_poor', price=cshort(q['price'])), ephemeral=True)
        ok, total = _buy_fill(gid, ctx.author.id, sym, qty, ref_price=q['price'])
        if not ok:
            b = bal(gid, ctx.author.id)
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        from cogs.gamble import _game_layout
        fee = total - int(round(ask * qty))
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.stock_bought_title'),
            t(gid, 'eco.stx_bought', qty=qty, sym=sym, px=cshort(q['price']),
              cost=cshort(total), fee=cshort(fee))
            + '\n' + t(gid, 'eco.stx_tip_buy', sym=sym)), ephemeral=True)

    @stock.command(name='sell', description='Sprzedaj akcje')
    async def stock_sell(self, ctx, symbol: str = '', amount: str = 'all'):
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stock_no', syms='/'.join(STOCKS)), ephemeral=True)
        with db.conn_ctx() as conn:
            pos = conn.execute('SELECT qty FROM portfolio WHERE guild_id=? AND user_id=? AND symbol=?',
                               (str(gid), str(ctx.author.id), sym)).fetchone()
        held = (pos['qty'] if pos else 0) or 0
        if not held:
            return await ctx.reply(t(gid, 'eco.stock_none', sym=sym), ephemeral=True)
        kind, val = _parse_sell(amount or 'all', held)
        if kind == 'err':
            return await ctx.reply(t(gid, 'eco.stx_shares'), ephemeral=True)
        n = min(int(val), held)
        if n <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        q = _quote(gid, sym)
        ok, net, pnl = _sell_fill(gid, ctx.author.id, sym, n, ref_price=q['price'])
        if not ok:
            return await ctx.reply(t(gid, 'eco.stock_none', sym=sym), ephemeral=True)
        from cogs.gamble import _game_layout
        fee = int(round(q['price'] * (1 - SPREAD) * n)) - net
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.stock_sold_title'),
            t(gid, 'eco.stx_sold', qty=n, sym=sym, px=cshort(q['price']),
              gain=cshort(net), fee=cshort(fee), pnl=cshort(pnl))
            + '\n' + t(gid, 'eco.stx_tip_sell')), ephemeral=True)

    @stock.command(name='view', description='Wykres akcji')
    async def stock_view(self, ctx, symbol: str = '', tf: str = '1D'):
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stock_no', syms='/'.join(STOCKS)), ephemeral=True)
        tf = (tf or '1D').upper()
        if tf not in TF_WINDOWS:
            tf = '1D'
        await ctx.defer()
        q = _quote(gid, sym)  # tick first: rescues + fresh price land IN this chart
        pts = _history(gid, sym, TF_WINDOWS[tf])
        png = await self.bot.loop.run_in_executor(None, stock_chart, sym,
                                                  STOCKS[sym]['name'], pts, tf)
        await ctx.reply(view=_chart_view(gid, sym, tf, _stats_txt(gid, sym, q)),
                        file=discord.File(__import__('io').BytesIO(png), 'chart.png'),
                        mention_author=False)

    @stock.command(name='limit', description='Zlecenie limit')
    async def stock_limit(self, ctx, side: str = '', symbol: str = '', qty: str = '', px: str = ''):
        gid = ctx.guild.id
        side = (side or '').lower()
        sym = (symbol or '').upper()
        if side not in ('buy', 'sell') or sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stx_limit_use'), ephemeral=True)
        try:
            n = max(1, int((qty or '').replace(',', '')))
            lim = max(1, int((px or '').lower().replace('@', '').replace(',', '')
                             .replace('k', '000')))
        except Exception:
            return await ctx.reply(t(gid, 'eco.stx_limit_use'), ephemeral=True)
        with db.conn_ctx() as conn:
            mine = conn.execute('SELECT COUNT(*) c FROM stock_orders WHERE guild_id=? AND user_id=?',
                                (str(gid), str(ctx.author.id))).fetchone()['c']
        if mine >= MAX_ORDERS:
            return await ctx.reply(t(gid, 'eco.stx_maxorders', n=MAX_ORDERS), ephemeral=True)
        with db.conn_ctx() as conn:
            cur = conn.execute('INSERT INTO stock_orders (guild_id, user_id, symbol, side, qty, '
                               'limit_px, created_at, expires_at, channel_id) VALUES (?,?,?,?,?,?,?,?,?)',
                               (str(gid), str(ctx.author.id), sym, side, n, lim,
                                _now(), _now() + ORDER_TTL, str(ctx.channel.id)))
            oid = cur.lastrowid
        await ctx.reply(t(gid, 'eco.stx_limit_ok', side=side, qty=n, sym=sym,
                            px=cshort(lim), idx=oid), ephemeral=True)

    @stock.command(name='orders', description='Twoje zlecenia')
    async def stock_orders(self, ctx):
        from cogs.gamble import _game_layout
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            rows = conn.execute('SELECT * FROM stock_orders WHERE guild_id=? AND user_id=? ORDER BY id',
                                (str(gid), str(ctx.author.id))).fetchall()
        if not rows:
            return await ctx.reply(t(gid, 'eco.stx_orders_empty'), ephemeral=True)
        lines = [f"• #{dict(r)['id']} {dict(r)['side']} {dict(r)['qty']}x {dict(r)['symbol']} "
                 f"@ {cshort(dict(r)['limit_px'])}" for r in rows]
        await ctx.reply(view=_game_layout(t(gid, 'eco.stx_orders_title'), '\n'.join(lines)),
                        ephemeral=True)

    @stock.command(name='cancel', description='Anuluj zlecenie')
    async def stock_cancel(self, ctx, oid: str = ''):
        gid = ctx.guild.id
        with db.conn_ctx() as conn:
            if (oid or '').lower() == 'all':
                conn.execute('DELETE FROM stock_orders WHERE guild_id=? AND user_id=?',
                             (str(gid), str(ctx.author.id)))
                return await ctx.reply(t(gid, 'eco.stx_cancelled', idx='all'), ephemeral=True)
            try:
                i = int(oid)
            except Exception:
                return await ctx.reply(t(gid, 'eco.stx_cancel_use'), ephemeral=True)
            cur = conn.execute('DELETE FROM stock_orders WHERE id=? AND guild_id=? AND user_id=?',
                               (i, str(gid), str(ctx.author.id)))
            if not (cur.rowcount or 0):
                return await ctx.reply(t(gid, 'eco.stx_no_order', idx=oid), ephemeral=True)
        await ctx.reply(t(gid, 'eco.stx_cancelled', idx=oid), ephemeral=True)

    # ----- legacy wrappers -----
    @commands.command(name='stockbuy', description='Kup akcje')
    async def stockbuy(self, ctx, symbol: str, cash: int):
        from cogs.gamble import bal
        gid = ctx.guild.id
        sym = (symbol or '').upper()
        if sym not in STOCKS:
            return await ctx.reply(t(gid, 'eco.stock_no', syms='/'.join(STOCKS)), ephemeral=True)
        if cash <= 0:
            return await ctx.reply(t(gid, 'eco.bet_pos'), ephemeral=True)
        q = _quote(gid, sym)
        ask = q['price'] * (1 + SPREAD)
        qty = int(cash // (ask * (1 + FEE)))
        if qty <= 0:
            return await ctx.reply(t(gid, 'eco.stock_poor', price=cshort(q['price'])), ephemeral=True)
        ok, total = _buy_fill(gid, ctx.author.id, sym, qty, ref_price=q['price'])
        if not ok:
            b = bal(gid, ctx.author.id)
            return await ctx.reply(t(gid, 'eco.broke', cash=cshort(b['cash'])), ephemeral=True)
        from cogs.gamble import _game_layout
        await ctx.reply(view=_game_layout(
            t(gid, 'eco.stock_bought_title'),
            t(gid, 'eco.stock_bought', qty=qty, sym=sym, cost=cshort(total))), ephemeral=True)

    @commands.command(name='stocksell', description='Sprzedaj akcje')
    async def stocksell(self, ctx, symbol: str, qty: str = 'all'):
        await self.stock_sell(ctx, symbol, qty)

    @commands.command(name='portfolio', description='Twoje akcje', aliases=['portfel', 'pf'])
    async def portfolio(self, ctx, member: discord.Member = None):
        import asyncio as _aio
        member = member or ctx.author
        await ctx.defer()
        gid = ctx.guild.id
        prices = {s: _quote(gid, s)['price'] for s in STOCKS}
        opens = {}
        with db.conn_ctx() as conn:
            for s in STOCKS:
                r = conn.execute('SELECT day_open FROM stocks WHERE guild_id=? AND symbol=?',
                                 (str(gid), s)).fetchone()
                opens[s] = (r['day_open'] if r and r['day_open'] else prices[s])
            rows = [dict(r) for r in conn.execute(
                'SELECT symbol, qty, spent FROM portfolio WHERE guild_id=? AND user_id=? AND qty>0',
                (str(gid), str(member.id))).fetchall()]
        if not rows:
            return await ctx.reply(t(gid, 'eco.port_empty'), ephemeral=True)
        from cogs.gamble import bal
        alloc, total, invested, day_pnl = [], 0, 0, 0
        for r in rows:
            px = prices.get(r['symbol'], 0)
            val = r['qty'] * px
            pnl = val - (r['spent'] or 0)
            day_pnl += r['qty'] * (px - opens.get(r['symbol'], px))
            total += val
            invested += r['spent'] or 0
            alloc.append((r['symbol'], r['qty'], val, pnl))
        alloc.sort(key=lambda a: -a[2])
        cash = bal(gid, member.id)['cash']
        alloc.append(('CASH', 0, cash, 0))
        try:
            av = await _aio.wait_for(member.display_avatar.read(), timeout=4)
        except Exception:
            av = None
        png = await self.bot.loop.run_in_executor(
            None, portfolio_card, member.display_name, av,
            cash, total, day_pnl, total - invested, alloc)
        await ctx.reply(file=discord.File(__import__('io').BytesIO(png), 'portfolio.png'),
                        mention_author=False)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get('custom_id', '')
        if cid.startswith('stv:'):
            sym = cid.split(':', 1)[1]
            if sym not in STOCKS:
                return
            from lang import set_ctx_lang
            set_ctx_lang(interaction.user)
            try:
                await interaction.response.defer()
            except Exception:
                pass
            gid = interaction.guild_id
            q = _quote(gid, sym)
            pts = _history(gid, sym, TF_WINDOWS['1D'])
            png = stock_chart(sym, STOCKS[sym]['name'], pts, '1D')
            view = _chart_view(gid, sym, '1D', _stats_txt(gid, sym, q))
            try:
                await interaction.edit_original_response(
                    view=view, attachments=[discord.File(__import__('io').BytesIO(png), 'chart.png')])
            except Exception:
                pass


def _mk_view_cb(sym: str):
    async def _cb(interaction: discord.Interaction):
        from lang import set_ctx_lang
        set_ctx_lang(interaction.user)
        try:
            await interaction.response.defer()
        except Exception:
            pass
        gid = interaction.guild_id
        q = _quote(gid, sym)
        pts = _history(gid, sym, TF_WINDOWS['1D'])
        png = stock_chart(sym, STOCKS[sym]['name'], pts, '1D')
        view = _chart_view(gid, sym, '1D', _stats_txt(gid, sym, q))
        try:
            await interaction.edit_original_response(
                view=view, attachments=[discord.File(__import__('io').BytesIO(png), 'chart.png')])
        except Exception:
            pass
    return _cb


async def setup(bot):
    await bot.add_cog(Stocks(bot))

"""Market service (rework): listings, atomic purchases, cancellation.

Discord-free: no ctx, no interactions, no views. Message text uses pure
string lookups (lang/emoji); the cog renders cards and sends.

Core invariant: the listing OWNS the Pokemon by moving it from pk_mons
into pk_market (delete-original). There is no status column — a listing
ends by DELETE. Purchases claim via a rowcount-guarded DELETE so exactly
one winner exists even with two processes on one SQLite file.

Current semantics preserved: min price 100, 3 listings per seller,
locked blocked, buyer pays price, seller gets price-5% (sink), no
listing fee, latest-40 browser at 8/page. Favorite/active/buddy/
battle/trade stay lenient unless the validation policy enables them.
"""
from dataclasses import dataclass, field
import time

import database as db
from lang import t
from services._common import (cash_of, credit_cash, debit_cash, ensure_eco,
                              get_owned_pokemon, reassign_active, upsert_item)

MIN_PRICE = 100
MAX_LISTINGS = 3
SALE_TAX_PCT = 5
BROWSE_LIMIT = 40


@dataclass(frozen=True)
class MarketValidationPolicy:
    """Stricter-than-today checks. All default OFF (current behavior);
    enable deliberately, never by accident."""
    reject_locked: bool = True
    reject_favorite: bool = False
    reject_active: bool = False
    reject_buddy: bool = False
    reject_in_battle: bool = False
    reject_in_trade: bool = False


DEFAULT_POLICY = MarketValidationPolicy()


@dataclass
class MarketResult:
    ok: bool
    code: str               # LISTING_CREATED / PRICE_TOO_LOW / NOT_FOUND /
                            # NOT_OWNER / LOCKED / MARKET_FULL / BUY_SUCCESS /
                            # ALREADY_SOLD / SELF_BUY / INSUFFICIENT_FUNDS /
                            # CANCEL_SUCCESS. ALREADY_LISTED + INVALID_PRICE
                            # exist for API completeness (copy-out design
                            # makes duplicates impossible; parsing errors
                            # stay in the cog).
    message: str            # user-facing text, same strings as the old cog
    listing_id: int | None = None
    pokemon_id: int | None = None   # new pk_mons id (buy/cancel) or source id
    buyer_id: int | None = None
    seller_id: int | None = None
    price: int = 0
    fee: int = 0                # buyer fee (0: buyers pay exactly price)
    tax: int = 0                # seller tax (5%, vanishes as a sink)
    balance_after: int | None = None
    dex: int = 0                # for the cog's result card art
    name: str = ''              # display name for the cog's result card
    events: list = field(default_factory=list)


@dataclass
class MarketPage:
    page: int
    total_pages: int
    total: int
    rows: list = field(default_factory=list)


class _Abort(Exception):
    """Roll back the open transaction, return this result instead."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def _mon_name(m: dict, gid) -> str:
    from cogs.pokemon import mon_name
    return mon_name(m, gid)


def _policy_deny(m: dict, gid, uid, policy: MarketValidationPolicy,
                 busy_ids=None) -> str | None:
    """Result code when a policy check rejects, else None. Lenient flags
    only bite when explicitly enabled."""
    if policy.reject_locked and m.get('locked'):
        return 'LOCKED'
    if policy.reject_favorite and m.get('fav'):
        return 'FAVORITE'
    if policy.reject_active and m.get('active'):
        return 'ACTIVE'
    if policy.reject_buddy:
        with db.conn_ctx() as conn:
            b = conn.execute('SELECT mid FROM pk_buddy WHERE guild_id=? AND user_id=?',
                             (str(gid), str(uid))).fetchone()
            if b and str(b['mid']) == str(m['id']):
                return 'BUDDY'
    if (policy.reject_in_battle or policy.reject_in_trade) and busy_ids \
            and str(m['id']) in {str(x) for x in busy_ids}:
        return 'BUSY'
    return None


def create_listing(gid, seller_id, seller_name: str, mon_id: int, price: int,
                   policy: MarketValidationPolicy = DEFAULT_POLICY,
                   busy_ids=None) -> MarketResult:
    """List a mon: held item back to inventory, copy into pk_market,
    delete original, reassign active. One transaction."""
    from cogs.pokemon import held_key
    from utils.cards import short as cshort
    seller_id = str(seller_id)
    with db.conn_ctx() as conn:
        m = get_owned_pokemon(conn, gid, seller_id, mon_id)
        if not m:
            return MarketResult(False, 'NOT_FOUND', t(gid, 'eco.pk_noslot'))
    if price is None or int(price) < MIN_PRICE:
        return MarketResult(False, 'PRICE_TOO_LOW', t(gid, 'eco.pk_market_min'))
    price = int(price)
    denied = _policy_deny(m, gid, seller_id, policy, busy_ids)
    if denied == 'LOCKED':
        return MarketResult(False, 'LOCKED',
                            t(gid, 'eco.pk_locked', name=_mon_name(m, gid)))
    if denied:
        return MarketResult(False, denied, t(gid, 'eco.pk_noslot'))
    hk = held_key(m)
    with db.conn_ctx() as conn:
        n = conn.execute('SELECT COUNT(*) c FROM pk_market WHERE guild_id=? AND seller_id=?',
                         (str(gid), seller_id)).fetchone()['c']
        if n >= MAX_LISTINGS:
            return MarketResult(False, 'MARKET_FULL', t(gid, 'eco.pk_market_full'))
        if hk:
            # listings carry no held item: back to inventory, same txn
            upsert_item(conn, gid, seller_id, hk, 1)
        conn.execute('INSERT INTO pk_market (guild_id, seller_id, seller_name, dex, level, xp, '
                     'shiny, nick, price, created, ivs, evs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (str(gid), seller_id, (seller_name or '')[:24],
                      m['dex'], m['level'], m['xp'], m['shiny'], m.get('nick') or '',
                      price, int(time.time()),
                      m.get('ivs') or '', m.get('evs') or ''))
        conn.execute('DELETE FROM pk_mons WHERE id=?', (m['id'],))
        # quirk preserved: first remaining mon becomes active unconditionally
        reassign_active(conn, gid, seller_id)
        lid = conn.execute('SELECT last_insert_rowid() i').fetchone()['i']
    name = _mon_name(m, gid)
    events = [{'type': 'held_returned', 'item': hk}] if hk else []
    events.append({'type': 'listed', 'listing_id': lid, 'price': price})
    return MarketResult(True, 'LISTING_CREATED',
                        t(gid, 'eco.pk_listed', name=name,
                          price=cshort(price), lid=lid),
                        listing_id=lid, pokemon_id=m['id'], seller_id=seller_id,
                        price=price, dex=m['dex'], name=name, events=events)


def buy_listing(gid, buyer_id, listing_id) -> MarketResult:
    """Atomic purchase: rowcount-guarded claim, then settle, one txn.
    Loser changes nothing. Raises _Abort internally to roll back."""
    from utils.cards import short as cshort
    buyer_id = str(buyer_id)
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM pk_market WHERE id=? AND guild_id=?',
                         (listing_id or 0, str(gid))).fetchone()
        if not r:
            return MarketResult(False, 'NOT_FOUND', t(gid, 'eco.pk_market_gone'))
        r = dict(r)
    if str(r['seller_id']) == buyer_id:
        return MarketResult(False, 'SELF_BUY', t(gid, 'eco.pk_market_own'))
    try:
        with db.conn_ctx() as conn:
            # claim: exactly one winner, even across processes
            cur = conn.execute('DELETE FROM pk_market WHERE id=? AND guild_id=?',
                               (r['id'], str(gid)))
            if (cur.rowcount or 0) != 1:
                raise _Abort('ALREADY_SOLD', t(gid, 'eco.pk_market_gone'))
            ensure_eco(conn, gid, buyer_id)
            ensure_eco(conn, gid, r['seller_id'])
            if cash_of(conn, gid, buyer_id) < r['price']:
                raise _Abort('INSUFFICIENT_FUNDS',
                             t(gid, 'eco.broke', cash=cshort(cash_of(conn, gid, buyer_id))))
            fee = r['price'] * SALE_TAX_PCT // 100
            after = debit_cash(conn, gid, buyer_id, r['price'])
            credit_cash(conn, gid, r['seller_id'], r['price'] - fee)
            has = conn.execute('SELECT COUNT(*) c FROM pk_mons WHERE guild_id=? AND owner_id=?',
                               (str(gid), buyer_id)).fetchone()['c']
            conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
                         'active, ivs, evs) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (str(gid), buyer_id, r['dex'], r['level'], r['xp'],
                          r['shiny'], r['nick'], 1 if not has else 0,
                          r.get('ivs') or '', r.get('evs') or ''))
            mid = conn.execute('SELECT last_insert_rowid() i').fetchone()['i']
    except _Abort as a:
        return MarketResult(False, a.code, a.message, listing_id=r['id'],
                            seller_id=str(r['seller_id']), price=r['price'])
    name = (r['nick'] or _dex_row_of(r['dex']))
    return MarketResult(True, 'BUY_SUCCESS',
                        t(gid, 'eco.pk_market_bought', name=name,
                          price=cshort(r['price'])),
                        listing_id=r['id'], pokemon_id=mid, buyer_id=buyer_id,
                        seller_id=str(r['seller_id']), price=r['price'],
                        fee=0, tax=fee, balance_after=after, dex=r['dex'],
                        name=name,
                        events=[{'type': 'sold', 'listing_id': r['id'],
                                 'price': r['price'], 'tax': fee}])


def cancel_listing(gid, seller_id, listing_id) -> MarketResult:
    """Unlist: mon copied back (active if seller has none), listing deleted.
    Non-sellers get the same 'gone' text as a missing listing."""
    seller_id = str(seller_id)
    with db.conn_ctx() as conn:
        r = conn.execute('SELECT * FROM pk_market WHERE id=? AND guild_id=? AND seller_id=?',
                         (listing_id, str(gid), seller_id)).fetchone()
        if not r:
            return MarketResult(False, 'NOT_OWNER', t(gid, 'eco.pk_market_gone'))
        r = dict(r)
        first = not conn.execute('SELECT id FROM pk_mons WHERE guild_id=? AND owner_id=? LIMIT 1',
                                 (str(gid), seller_id)).fetchone()
        conn.execute('INSERT INTO pk_mons (guild_id, owner_id, dex, level, xp, shiny, nick, '
                     'active, ivs, evs) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (str(gid), seller_id, r['dex'], r['level'], r['xp'],
                      r['shiny'], r['nick'], 1 if first else 0,
                      r.get('ivs') or '', r.get('evs') or ''))
        mid = conn.execute('SELECT last_insert_rowid() i').fetchone()['i']
        conn.execute('DELETE FROM pk_market WHERE id=?', (r['id'],))
    row = _dex_row_of(r['dex'])
    name = (r['nick'] or row)
    return MarketResult(True, 'CANCEL_SUCCESS', t(gid, 'eco.pk_unlisted'),
                        listing_id=r['id'], pokemon_id=mid, seller_id=seller_id,
                        price=r['price'], dex=r['dex'], name=name,
                        events=[{'type': 'unlisted', 'listing_id': r['id']}])


def _dex_row_of(dex: int) -> str:
    from cogs.pokemon import _dex_row
    return (_dex_row(dex).get('name') or '?').capitalize()


def search_listings(gid, page: int = 1, per: int = 8) -> MarketPage:
    """Latest-40 browser window, 8 per page. Pure reads for the cog's view."""
    with db.conn_ctx() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM pk_market WHERE guild_id=? ORDER BY id DESC LIMIT 40',
            (str(gid),)).fetchall()]
    total = max(1, (len(rows) + per - 1) // per)
    page = min(max(1, page or 1), total)
    return MarketPage(page=page, total_pages=total, total=len(rows),
                      rows=rows[(page - 1) * per:page * per])

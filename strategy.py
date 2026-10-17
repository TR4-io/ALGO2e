"""ALGO2e market maker. Run: python strategy.py; inspect: python strategy.py --check.

Uses the local RIT Client REST connection from the course primer/LT3.
Run only one bot on this account; start flat with no open orders.
Defaults are hypotheses to validate in practice, not measured optimal settings.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import signal
import sys
import time

import requests

BASE_URL = os.getenv('RIT_BASE_URL', 'http://localhost:9999/v1')
API_KEY = os.getenv('RIT_API_KEY', 'Rotman')
TICKERS = ('CNR', 'RY', 'AC')
# Positive taker numbers are costs; positive maker numbers are receipts.
TAKER_FEE = {'CNR': 0.0027, 'RY': -0.0014, 'AC': 0.0015}
MAKER_REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}
TICK = 0.01
MAX_ORDER = 5000
TARGET_VOLUME = 110000  # buffer above the 100,000-share ranked requirement
GROSS_BUDGET = 18000   # headroom below the 25,000 case limit
NET_BUDGET = 18000
STOCK_BUDGET = 6500
REDUCE_AT = 260
FLATTEN_AT = 280
END_TICK = 300
POLL_SECONDS = 0.20
MAX_QUOTE_AGE = 4.0
REPRICE_TICKS = 2
MAX_BOOK_AGE = 0.75
LOSS_STOP = 750.0      # dollar loss versus P&L at startup; tune with practice
DRAWDOWN_STOP = 600.0  # activated only after reaching $300 session P&L


class ApiError(RuntimeError):
    pass


class UnknownWrite(ApiError):
    """Do not replay a POST whose outcome is unknown."""


class HttpError(ApiError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f'HTTP {status} on {path}')


class RateLimited(ApiError):
    def __init__(self, wait):
        self.wait = wait
        super().__init__(f'Rate limited: wait {wait:.2f}s, then make a fresh decision')


def integer(value):
    n = float(value)
    if not math.isfinite(n) or n != int(n):
        raise ValueError('Invalid share quantity')
    return int(n)


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'X-API-Key': API_KEY})

    def call(self, method, path, **params):
        try:
            r = self.session.request(method, BASE_URL.rstrip('/') + '/' + path,
                                     params=params, timeout=2, allow_redirects=False)
        except requests.RequestException as exc:
            kind = UnknownWrite if method != 'GET' else ApiError
            raise kind(f'{method} {path}: connection failed ({type(exc).__name__})') from exc
        if r.status_code == 429:
            try:
                wait = float(r.headers.get('Retry-After') or r.json().get('wait', 0.5))
                if not math.isfinite(wait):
                    wait = 0.5
            except (ValueError, TypeError, AttributeError):
                wait = 0.5
            raise RateLimited(max(0.05, wait))
        if method != 'GET' and r.status_code >= 500:
            raise UnknownWrite(f'{method} {path}: HTTP {r.status_code}; outcome unknown')
        if not 200 <= r.status_code < 300:
            raise HttpError(r.status_code, path)
        try:
            return r.json()
        except ValueError as exc:
            kind = UnknownWrite if method != 'GET' else ApiError
            raise kind(f'{path}: unreadable response') from exc


@dataclass
class Order:
    id: int
    ticker: str
    action: str
    quantity: int
    price: float
    born: float
    filled: int = 0
    status: str = 'OPEN'

    @property
    def remaining(self):
        return self.quantity - self.filled if self.status == 'OPEN' else 0


class Ledger:
    """Inventory comes only from confirmed fills, never cancellation assumptions."""
    def __init__(self):
        self.orders = {}
        self.positions = dict.fromkeys(TICKERS, 0)
        self.volume = 0

    def update(self, row):
        order = self.orders[integer(row['order_id'])]
        filled = integer(row['quantity_filled'])
        if (row['ticker'] != order.ticker or row['action'] != order.action
                or integer(row['quantity']) != order.quantity
                or not order.filled <= filled <= order.quantity
                or row['status'] not in ('OPEN', 'CANCELLED', 'TRANSACTED')):
            raise ValueError('Order identity or cumulative fills changed unexpectedly')
        delta = filled - order.filled
        self.positions[order.ticker] += delta if order.action == 'BUY' else -delta
        self.volume += delta
        order.filled, order.status = filled, row['status']

    def active(self, ticker=None):
        return [o for o in self.orders.values() if o.status == 'OPEN'
                and (ticker is None or o.ticker == ticker)]


def risk_bounds(positions, orders):
    """Worst case over all independent combinations of outstanding fills."""
    low, high = dict(positions), dict(positions)
    for o in orders:
        if o.action == 'BUY':
            high[o.ticker] += o.remaining
        else:
            low[o.ticker] -= o.remaining
    gross = sum(max(abs(low[t]), abs(high[t])) for t in TICKERS)
    net = max(abs(sum(low.values())), abs(sum(high.values())))
    return low, high, gross, net


def capacity(positions, orders, ticker, action, wanted, gross_limit, net_limit):
    """Largest safe clip, reserving every remaining open-order share."""
    def safe(q):
        candidate = Order(-1, ticker, action, q, 1, 0)
        low, high, gross, net = risk_bounds(positions, [*orders, candidate])
        return (gross <= gross_limit and net <= net_limit
                and all(max(abs(low[t]), abs(high[t])) <= STOCK_BUDGET for t in TICKERS))
    if not safe(0):
        return 0
    lo, hi = 0, min(MAX_ORDER, wanted)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if safe(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def clean_book(raw, own_ids):
    result = {}
    for side in ('bids', 'asks'):
        levels = {}
        for row in raw[side]:
            if str(row.get('order_id')) in own_ids or row.get('status', 'OPEN') != 'OPEN':
                continue
            qty = integer(row['quantity']) - integer(row.get('quantity_filled', 0))
            price = float(row['price'])
            if qty < 0 or not math.isfinite(price) or price <= 0:
                raise ValueError('Invalid order book')
            if qty:
                levels[price] = levels.get(price, 0) + qty
        result[side] = sorted(levels.items(), reverse=side == 'bids')
    return result


class Market:
    def __init__(self):
        self.mid = None
        self.tick = None
        self.variance = 0.0
        self.trend = 0.0

    def observe(self, book, tick):
        if not book['bids'] or not book['asks']:
            return None
        bid, bqty = book['bids'][0]
        ask, aqty = book['asks'][0]
        if bid >= ask:
            return None
        mid = (bid + ask) / 2
        if self.tick is not None and tick > self.tick:
            dt = tick - self.tick
            weight = 1 - math.exp(-dt / 5.0)
            change = mid - self.mid
            self.variance = (1-weight)*self.variance + weight*change*change/dt
            self.trend = (1-weight)*self.trend + weight*change/dt
        if self.tick is None or tick > self.tick:
            self.mid, self.tick = mid, tick
        spread = ask - bid
        micro = (ask*bqty + bid*aqty)/(bqty+aqty)
        fair = mid + 0.35*(micro-mid) + max(-spread/4, min(spread/4, self.trend))
        return bid, ask, fair, math.sqrt(self.variance)


def quote_prices(ticker, metrics, position, reduce_only=False):
    bid, ask, fair, sigma = metrics
    # Skew both prices down when long, up when short. Never intentionally cross.
    skew = position/STOCK_BUDGET * max(0.025, 2*sigma)
    center = fair-skew
    edge = 0.002 + 0.35*sigma
    rebate = MAKER_REBATE[ticker]
    buy = min(bid + (TICK if ask-bid >= 3*TICK else 0), center + rebate-edge)
    sell = max(ask - (TICK if ask-bid >= 3*TICK else 0), center - rebate+edge)
    if reduce_only:
        buy, sell = bid, ask
    buy = min(math.floor((buy+1e-9)/TICK)*TICK, ask-TICK)
    sell = max(math.ceil((sell-1e-9)/TICK)*TICK, bid+TICK)
    return round(buy, 2), round(sell, 2)


def clip_size(volume, tick, sigma):
    # Aim to qualify by tick 240; do not cross the market just to print volume.
    pace = TARGET_VOLUME * min(tick/240, 1)
    base = 2000 if volume < pace else 1000
    if volume >= TARGET_VOLUME:
        base = 750
    return max(250, int(base/(1+sigma/0.03)/50)*50)


def exit_price(book, action, wanted, urgent):
    levels = book['asks' if action == 'BUY' else 'bids']
    if not levels:
        return None
    best, _ = levels[0]
    budget = 0.50 if urgent else 0.15
    qty, price = 0, best
    for p, available in levels:
        if abs(p-best) > budget + 1e-9:
            break
        qty += min(wanted-qty, available)
        price = p
        if qty >= wanted:
            break
    return qty, price


class Bot:
    def __init__(self, client, log=None):
        self.api = client.call
        self.ledger = Ledger()
        self.markets = {t: Market() for t in TICKERS}
        self.gross_limit, self.net_limit = GROSS_BUDGET, NET_BUDGET
        self.log = log
        self.stopping = False
        self.active_seen = False
        self.round_changed = False
        self.period = None
        self.last_tick = -1
        self.last_report = -10
        self.start_pnl = None
        self.peak = 0.0
        self.reduction = False

    def securities(self):
        rows = self.api('GET', 'securities')
        by_ticker = {r['ticker']: r for r in rows}
        if not set(TICKERS).issubset(by_ticker):
            raise ValueError('Connect to ALGO2e: expected CNR, RY and AC')
        if any(integer(r['position']) for t, r in by_ticker.items() if t not in TICKERS):
            raise ValueError('Unexpected inventory in another security')
        return by_ticker

    def start(self):
        case = self.api('GET', 'case')
        self.period = case.get('period')
        rows = self.securities()
        if any(integer(rows[t]['position']) for t in TICKERS) or self.api('GET', 'orders', status='OPEN'):
            raise ValueError('Start flat with no open orders; do not run another bot/manual trades')
        limits = self.api('GET', 'limits')
        if not isinstance(limits, list) or not limits:
            raise ValueError('Cannot verify live position limits')
        # ALGO2e uses a shared share limit; conservative minimum across returned limits.
        for row in limits:
            g, n = float(row['gross_limit']), float(row['net_limit'])
            if not math.isfinite(g+n) or g <= 0 or n <= 0:
                raise ValueError('Invalid live position limits')
            self.gross_limit = min(self.gross_limit, int(g*0.90))
            self.net_limit = min(self.net_limit, int(n*0.90))
        self.start_pnl = self.pnl(rows)
        print(f'Connected. Risk budgets gross/net: {self.gross_limit}/{self.net_limit}. '
              f'Confirmed-volume target: {TARGET_VOLUME:,}.')
        if self.start_pnl is None:
            print('P&L fields unavailable: dollar loss/drawdown stops disabled; inventory limits remain active.')

    @staticmethod
    def pnl(rows):
        try:
            value = sum(float(rows[t]['realized'])+float(rows[t]['unrealized']) for t in TICKERS)
            return value if math.isfinite(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    def reconcile(self):
        for o in self.ledger.active():
            try:
                row = self.api('GET', f'orders/{o.id}')
            except HttpError as exc:
                if exc.status != 404:
                    raise
                row = None
                for status in ('OPEN', 'TRANSACTED', 'CANCELLED'):
                    row = next((r for r in self.api('GET', 'orders', status=status)
                                if integer(r['order_id']) == o.id), None)
                    if row is not None:
                        break
                if row is None:
                    raise ApiError(f'Order {o.id} missing; not assuming it filled or cancelled')
            self.ledger.update(row)

    def cancel(self, o):
        try:
            self.api('DELETE', f'orders/{o.id}')
        except HttpError as exc:
            if exc.status != 404:
                raise
        # Cancellation response alone does not account for racing fills.
        self.reconcile()

    def cancel_all(self):
        for o in list(self.ledger.active()):
            self.cancel(o)

    def submit(self, ticker, action, qty, price):
        if qty < 1 or price <= 0:
            return
        row = self.api('POST', 'orders', ticker=ticker, action=action,
                       type='LIMIT', quantity=qty, price=price)
        try:
            oid = integer(row['order_id'])
            if oid in self.ledger.orders:
                raise ValueError('Reused order id')
            self.ledger.orders[oid] = Order(oid, ticker, action, qty, price, time.monotonic())
            self.ledger.update(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise UnknownWrite('Order accepted but response cannot be reconciled') from exc

    def book(self, ticker):
        started = time.monotonic()
        book = clean_book(self.api('GET', 'securities/book', ticker=ticker, limit=1000),
                          {str(o.id) for o in self.ledger.active()})
        return book, started

    def report(self, tick, pnl):
        if tick-self.last_report < 5:
            return
        self.last_report = tick
        p = self.ledger.positions
        remaining = max(0, 100000-self.ledger.volume)
        print(f't={tick:5.1f} fills={self.ledger.volume:>7,} need={remaining:>7,} '
              f'positions={p} P&L={pnl if pnl is not None else "unavailable"} '
              f'mode={"REDUCE" if self.reduction else "MAKE"}')
        if self.log:
            self.log.writerow([tick, self.ledger.volume, *(p[t] for t in TICKERS), pnl,
                               len(self.ledger.active()), self.reduction])

    def step(self):
        case = self.api('GET', 'case')
        tick = float(case['tick'])
        if case.get('period') != self.period or tick < self.last_tick:
            self.round_changed = True
            raise ApiError('Case reset detected. Restart for the next round.')
        self.last_tick = tick
        self.reconcile()
        rows = self.securities()
        actual = {t: integer(rows[t]['position']) for t in TICKERS}
        if actual != self.ledger.positions:
            # Fills may have occurred between order reads and the position snapshot.
            # Skip submissions this cycle; persistent mismatch fails closed in main.
            raise ApiError('Position/fill snapshots disagree; waiting for reconciliation')
        open_rows = self.api('GET', 'orders', status='OPEN')
        if any(integer(r['order_id']) not in self.ledger.orders for r in open_rows):
            raise ApiError('Untracked open order: stop other bots/manual trading')
        current_pnl = self.pnl(rows)
        pnl = current_pnl-self.start_pnl if current_pnl is not None and self.start_pnl is not None else None
        if pnl is not None:
            self.peak = max(self.peak, pnl)
            if pnl <= -LOSS_STOP or (self.peak >= 300 and self.peak-pnl >= DRAWDOWN_STOP):
                self.stopping = True
        self.reduction = self.stopping or tick >= REDUCE_AT
        self.report(tick, pnl)
        if case['status'] != 'ACTIVE' or tick >= END_TICK:
            self.cancel_all()
            return not (self.active_seen and case['status'] == 'STOPPED' or tick >= END_TICK or self.stopping)
        self.active_seen = True
        if self.stopping and not any(actual.values()) and not self.ledger.active():
            return False
        # Rotate priority so one stock cannot permanently monopolize the risk budget.
        offset = int(tick) % len(TICKERS)
        for ticker in TICKERS[offset:]+TICKERS[:offset]:
            self.manage(ticker, tick)
        return True

    def manage(self, ticker, tick):
        pos = self.ledger.positions[ticker]
        book, observed = self.book(ticker)
        metrics = self.markets[ticker].observe(book, tick)
        if time.monotonic()-observed > MAX_BOOK_AGE or metrics is None:
            for o in list(self.ledger.active(ticker)):
                self.cancel(o)
            return
        emergency = self.stopping or tick >= FLATTEN_AT
        prices = quote_prices(ticker, metrics, pos, self.reduction)
        allowed = ('SELL',) if self.reduction and pos > 0 else ('BUY',) if self.reduction and pos < 0 else () if self.reduction else ('BUY', 'SELL')
        # Cancel obsolete orders; preserve queue position when target moves <2 cents.
        for o in list(self.ledger.active(ticker)):
            target = prices[0 if o.action == 'BUY' else 1]
            age = time.monotonic()-o.born
            crossing = o.price >= metrics[1] if o.action == 'BUY' else o.price <= metrics[0]
            oversize = self.reduction and o.remaining > abs(pos)
            if (o.action not in allowed or oversize or crossing or emergency and age >= 0.5
                    or not emergency and (age >= MAX_QUOTE_AGE or abs(o.price-target) >= REPRICE_TICKS*TICK-1e-9)):
                self.cancel(o)
        if time.monotonic()-observed > MAX_BOOK_AGE:
            return  # cancel calls may have made the quote stale
        for action in allowed:
            if any(o.action == action for o in self.ledger.active(ticker)):
                continue
            pos = self.ledger.positions[ticker]
            if self.reduction and (pos == 0 or (pos > 0) != (action == 'SELL')):
                continue
            qty = clip_size(self.ledger.volume, tick, metrics[3])
            price = prices[0 if action == 'BUY' else 1]
            if self.reduction:
                qty = min(qty, abs(pos))
            if emergency:
                result = exit_price(book, action, min(MAX_ORDER, abs(pos)), tick >= 292)
                if result is None:
                    continue
                qty, price = result
            qty = capacity(self.ledger.positions, self.ledger.active(), ticker, action, qty,
                           self.gross_limit, self.net_limit)
            # Avoid increasing already large single-stock inventory.
            if not self.reduction and (pos > 3500 and action == 'BUY' or pos < -3500 and action == 'SELL'):
                continue
            if time.monotonic()-observed > MAX_BOOK_AGE:
                return
            opposite = [o.price for o in self.ledger.active(ticker) if o.action != action]
            if opposite:
                # A retained old quote must never trade against its replacement's opposite side.
                price = min(price, min(opposite)-TICK) if action == 'BUY' else max(price, max(opposite)+TICK)
                price = round(price, 2)
            self.submit(ticker, action, qty, price)

    def cleanup(self):
        if self.round_changed:
            print('Case changed: not cancelling old IDs in the new round.')
            return
        try:
            case = self.api('GET', 'case')
            if case.get('period') != self.period or float(case['tick']) < self.last_tick:
                print('Case reset during cleanup; inspect RIT manually.')
                return
            # Includes a potentially accepted POST whose response was lost. Safe only
            # because startup requires exclusive use of an empty trading account.
            for ticker in TICKERS:
                self.api('POST', 'commands/cancel', ticker=ticker)
            self.reconcile()
            remaining = self.api('GET', 'orders', status='OPEN')
            positions = {t: integer(r['position']) for t, r in self.securities().items()}
            print(f'Final confirmed bot volume: {self.ledger.volume:,}; positions: {positions}; open orders: {len(remaining)}')
            print('Volume target met.' if self.ledger.volume >= 100000 else 'WARNING: ranked-run minimum volume NOT met.')
            if remaining or any(positions.values()):
                print('ATTENTION: inspect RIT and manage remaining inventory/orders. Flat closeout is not guaranteed.')
        except (ApiError, ValueError, KeyError, TypeError) as exc:
            print(f'Cleanup could not be confirmed: {exc}. Inspect RIT immediately.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read connection/securities/limits only; place no orders')
    args = parser.parse_args()
    client = Client()
    bot = Bot(client)
    handle = None
    old_handler = None
    started = False
    try:
        bot.start()
        started = True
        if args.check:
            print('Read-only check passed. Run without --check to trade.')
            return 0
        Path('logs').mkdir(exist_ok=True)
        handle = open(Path('logs')/f'run-{datetime.now():%Y%m%d-%H%M%S}.csv', 'w', newline='')
        bot.log = csv.writer(handle)
        bot.log.writerow(['tick', 'confirmed_volume', *TICKERS, 'session_pnl', 'open_orders', 'reduce_only'])
        def stop(signum, frame):
            if bot.stopping:
                raise KeyboardInterrupt
            bot.stopping = True
            print('Stopping quotes and closing inventory. Ctrl+C again exits immediately.')
        old_handler = signal.signal(signal.SIGINT, stop)
        failures = 0
        while True:
            try:
                if not bot.step():
                    break
                failures = 0
            except RateLimited as exc:
                print(exc)
                time.sleep(exc.wait)
                continue
            except UnknownWrite:
                raise
            except ApiError as exc:
                failures += 1
                print(exc)
                if isinstance(exc, HttpError) and exc.status in (401, 403) or failures >= 5:
                    raise
                time.sleep(0.25*failures)
            handle.flush()
            time.sleep(POLL_SECONDS)
        return 0
    except (ApiError, ValueError, KeyError, TypeError) as exc:
        print(f'STOPPED: {exc}. Check local RIT API settings and account state.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Immediate exit requested; remaining positions require manual attention.')
        return 130
    finally:
        if started and not args.check:
            bot.cleanup()
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
        if handle:
            handle.close()
        client.session.close()


if __name__ == '__main__':
    sys.exit(main())

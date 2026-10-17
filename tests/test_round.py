"""Deterministic execution plumbing check, NOT a realistic profitability backtest."""
import contextlib
import io
import unittest
from unittest.mock import patch
import strategy as s


class Exchange:
    def __init__(self):
        self.tick = 0
        self.orders = {}
        self.positions = dict.fromkeys(s.TICKERS, 0)
        self.volume = 0
        self.cash = 0.0

    def fill(self, row, qty):
        qty = min(qty, row['quantity']-row['quantity_filled'])
        row['quantity_filled'] += qty
        delta = qty if row['action'] == 'BUY' else -qty
        self.positions[row['ticker']] += delta
        self.cash -= delta*row['price']
        self.volume += qty
        if row['quantity_filled'] == row['quantity']:
            row['status'] = 'TRANSACTED'

    def advance(self):
        self.tick += 1
        for row in self.orders.values():
            if row['status'] == 'OPEN':
                # Alternating sides with partial fills exercise the fill ledger.
                if row['action'] == ('BUY' if self.tick % 2 else 'SELL'):
                    self.fill(row, 700)

    def call(self, method, path, **p):
        if path == 'case':
            return {'period': 1, 'tick': self.tick, 'status': 'ACTIVE' if self.tick < 300 else 'STOPPED'}
        if path == 'limits':
            return [{'gross_limit': 25000, 'net_limit': 25000}]
        if path == 'securities':
            return [dict(ticker=t, position=q) for t, q in self.positions.items()]
        if path == 'securities/book':
            return {'bids': [dict(price=10, quantity=10000)], 'asks': [dict(price=10.02, quantity=10000)]}
        if path == 'orders' and method == 'GET':
            return [dict(o) for o in self.orders.values() if o['status'] == p['status']]
        if path == 'orders' and method == 'POST':
            assert 0 < p['quantity'] <= 5000
            oid = len(self.orders)+1
            row = dict(p, order_id=oid, quantity_filled=0, status='OPEN')
            self.orders[oid] = row
            if p['action'] == 'BUY' and p['price'] >= 10.02 or p['action'] == 'SELL' and p['price'] <= 10:
                self.fill(row, p['quantity'])
            return dict(row)
        if path.startswith('orders/'):
            row = self.orders[int(path.split('/')[1])]
            if method == 'DELETE' and row['status'] == 'OPEN':
                # A fill races every cancellation.
                self.fill(row, 37)
                if row['status'] == 'OPEN':
                    row['status'] = 'CANCELLED'
            return dict(row)
        if path == 'commands/cancel':
            for row in self.orders.values():
                if row['ticker'] == p['ticker'] and row['status'] == 'OPEN':
                    row['status'] = 'CANCELLED'
            return {}
        raise AssertionError((method, path, p))


class RoundTest(unittest.TestCase):
    def test_full_round_with_partial_and_racing_fills(self):
        exchange = Exchange()
        bot = s.Bot(exchange)
        with contextlib.redirect_stdout(io.StringIO()), patch('strategy.time.monotonic', side_effect=lambda: float(exchange.tick)):
            bot.start()
            while bot.step():
                _, _, gross, net = s.risk_bounds(bot.ledger.positions, bot.ledger.active())
                self.assertLessEqual(gross, bot.gross_limit)
                self.assertLessEqual(net, bot.net_limit)
                self.assertEqual(bot.ledger.volume, exchange.volume)
                exchange.advance()
            bot.cleanup()
        self.assertEqual(bot.ledger.volume, exchange.volume)
        self.assertFalse(any(exchange.positions.values()))
        self.assertFalse(bot.ledger.active())
        self.assertGreater(bot.ledger.volume, 0)


if __name__ == '__main__':
    unittest.main()

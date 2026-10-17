import itertools
import random
import unittest
from unittest.mock import Mock
import requests
import strategy as s


def row(oid=1, ticker='CNR', action='BUY', quantity=1000, filled=0, status='OPEN', price=10):
    return dict(order_id=oid, ticker=ticker, action=action, quantity=quantity,
                quantity_filled=filled, status=status, price=price)


class RiskTests(unittest.TestCase):
    def test_reserves_both_sides_without_netting(self):
        p = dict.fromkeys(s.TICKERS, 0)
        orders = [s.Order(1, 'CNR', 'BUY', 5000, 10, 0), s.Order(2, 'CNR', 'SELL', 5000, 11, 0)]
        low, high, gross, net = s.risk_bounds(p, orders)
        self.assertEqual((low['CNR'], high['CNR'], gross, net), (-5000, 5000, 5000, 5000))
        self.assertEqual(s.capacity(p, orders, 'CNR', 'BUY', 5000, 18000, 18000), 1500)

    def test_gross_is_not_signed_net(self):
        p = {'CNR': 6000, 'RY': -6000, 'AC': 0}
        self.assertEqual(s.capacity(p, [], 'AC', 'BUY', 5000, 13000, 18000), 1000)
        self.assertEqual(s.capacity(p, [], 'RY', 'BUY', 2000, 18000, 18000), 2000)

    def test_random_fill_corners_stay_safe(self):
        rng = random.Random(2026)
        for _ in range(500):
            p = {t: rng.randint(-3000, 3000) for t in s.TICKERS}
            orders = [s.Order(i, t, a, rng.randint(0, 1000), 10, 0)
                      for i, (t, a) in enumerate(itertools.product(s.TICKERS, ('BUY', 'SELL')))]
            t, a = rng.choice(s.TICKERS), rng.choice(('BUY', 'SELL'))
            q = s.capacity(p, orders, t, a, 5000, 15000, 10000)
            _, _, old_gross, old_net = s.risk_bounds(p, orders)
            if old_gross > 15000 or old_net > 10000:
                self.assertEqual(q, 0)
                continue
            orders.append(s.Order(9, t, a, q, 10, 0))
            for fills in itertools.product((0, 1), repeat=len(orders)):
                result = dict(p)
                for o, filled in zip(orders, fills):
                    result[o.ticker] += filled*o.remaining*(1 if o.action == 'BUY' else -1)
                self.assertLessEqual(sum(map(abs, result.values())), 15000)
                self.assertLessEqual(abs(sum(result.values())), 10000)
                self.assertLessEqual(max(map(abs, result.values())), s.STOCK_BUDGET)


class ExecutionTests(unittest.TestCase):
    def test_partial_cancel_counts_fills_once(self):
        ledger = s.Ledger()
        ledger.orders[1] = s.Order(1, 'CNR', 'BUY', 1000, 10, 0)
        ledger.update(row(filled=300))
        ledger.update(row(filled=300))
        ledger.update(row(filled=450, status='CANCELLED'))
        self.assertEqual(ledger.volume, 450)
        self.assertEqual(ledger.positions['CNR'], 450)
        self.assertFalse(ledger.active())

    def test_cancel_racing_fill(self):
        client = Mock()
        bot = s.Bot(client)
        o = s.Order(1, 'CNR', 'BUY', 1000, 10, 0)
        bot.ledger.orders[1] = o
        client.call.side_effect = [{}, row(filled=1000, status='TRANSACTED')]
        bot.cancel(o)
        self.assertEqual(bot.ledger.volume, 1000)
        self.assertEqual(bot.ledger.positions['CNR'], 1000)

    def test_404_reconciles_from_terminal_list(self):
        client = Mock()
        bot = s.Bot(client)
        bot.ledger.orders[1] = s.Order(1, 'CNR', 'BUY', 1000, 10, 0)
        client.call.side_effect = [s.HttpError(404, 'orders/1'), [], [], [row(filled=200, status='CANCELLED')]]
        bot.reconcile()
        self.assertEqual(bot.ledger.volume, 200)

    def test_missing_order_not_assumed_filled(self):
        client = Mock()
        bot = s.Bot(client)
        bot.ledger.orders[1] = s.Order(1, 'CNR', 'BUY', 1000, 10, 0)
        client.call.side_effect = [s.HttpError(404, 'orders/1'), [], [], []]
        with self.assertRaises(s.ApiError):
            bot.reconcile()
        self.assertEqual(bot.ledger.volume, 0)
        self.assertEqual(bot.ledger.active()[0].remaining, 1000)

    def test_timeout_post_not_retried(self):
        c = s.Client()
        c.session.request = Mock(side_effect=requests.Timeout())
        with self.assertRaises(s.UnknownWrite):
            c.call('POST', 'orders', quantity=100)
        self.assertEqual(c.session.request.call_count, 1)
        c.session.close()

    def test_rate_limit_wait_is_honored_without_replay(self):
        c = s.Client()
        c.session.request = Mock(return_value=Mock(status_code=429, headers={'Retry-After': '3'}))
        with self.assertRaises(s.RateLimited) as e:
            c.call('POST', 'orders')
        self.assertEqual(e.exception.wait, 3)
        self.assertEqual(c.session.request.call_count, 1)
        c.session.close()

    def test_reset_prevents_old_id_cleanup(self):
        client = Mock()
        bot = s.Bot(client)
        bot.period, bot.last_tick = 1, 100
        client.call.return_value = {'tick': 0, 'period': 2, 'status': 'ACTIVE'}
        with self.assertRaises(s.ApiError):
            bot.step()
        bot.cleanup()
        self.assertEqual(client.call.call_count, 1)

    def test_position_mismatch_blocks_submission(self):
        client = Mock()
        bot = s.Bot(client)
        bot.period = 1
        rows = [dict(ticker=t, position=100 if t == 'CNR' else 0) for t in s.TICKERS]
        client.call.side_effect = [{'tick': 1, 'period': 1, 'status': 'ACTIVE'}, rows]
        with self.assertRaises(s.ApiError):
            bot.step()
        self.assertTrue(all(c.args[0] == 'GET' for c in client.call.call_args_list))


class QuoteTests(unittest.TestCase):
    def test_prices_passive_and_ry_charges_respected(self):
        metrics = (10.00, 10.01, 10.005, 0)
        for t in s.TICKERS:
            buy, sell = s.quote_prices(t, metrics, 0)
            self.assertLess(buy, metrics[1])
            self.assertGreater(sell, metrics[0])
            self.assertGreaterEqual(metrics[2]-buy+s.MAKER_REBATE[t], .002-1e-9)
            self.assertGreaterEqual(sell-metrics[2]+s.MAKER_REBATE[t], .002-1e-9)
        wider = (10, 10.06, 10.03, 0.02)
        cnr = s.quote_prices('CNR', wider, 0)
        ry = s.quote_prices('RY', wider, 0)
        self.assertLessEqual(ry[0], cnr[0])
        self.assertGreaterEqual(ry[1], cnr[1])

    def test_inventory_skew(self):
        m = (10, 10.04, 10.02, 0.01)
        flat = s.quote_prices('CNR', m, 0)
        long = s.quote_prices('CNR', m, 6000)
        short = s.quote_prices('CNR', m, -6000)
        self.assertLess(long[0], flat[0])
        self.assertGreater(short[1], flat[1])

    def test_book_removes_own_and_used_quantity(self):
        raw = {'bids': [row(oid=1), row(oid=2, filled=400)], 'asks': []}
        self.assertEqual(s.clean_book(raw, {'1'}), {'bids': [(10, 600)], 'asks': []})

    def test_bounded_exit_depth(self):
        b = {'asks': [(10, 400), (10.10, 300), (10.30, 500)], 'bids': []}
        self.assertEqual(s.exit_price(b, 'BUY', 1000, False), (700, 10.10))
        self.assertEqual(s.exit_price(b, 'BUY', 1000, True), (1000, 10.30))
        self.assertIsNone(s.exit_price(b, 'SELL', 100, True))

    def test_volume_pressure_and_volatility(self):
        self.assertGreater(s.clip_size(0, 100, 0), s.clip_size(110000, 100, 0))
        self.assertLess(s.clip_size(0, 100, .10), s.clip_size(0, 100, 0))

    def test_emergency_only_reduces_position(self):
        client = Mock()
        bot = s.Bot(client)
        bot.ledger.positions['CNR'] = 1500
        bot.reduction = True
        bot.book = lambda t: ({'bids': [(10, 3000)], 'asks': [(10.02, 3000)]}, s.time.monotonic())
        bot.submit = Mock()
        bot.manage('CNR', 290)
        bot.submit.assert_called_once_with('CNR', 'SELL', 1500, 10)


if __name__ == '__main__':
    unittest.main()

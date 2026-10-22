import copy
import unittest
from datetime import datetime, timedelta

from stock_option_signals import IST, evaluate_stock_setup


def confirmed_bullish_fixture():
    candles = {key: [] for key in ("timestamp", "open", "high", "low", "close", "volume")}

    def add(dt, opening, high, low, close, volume=1000):
        values = (int(dt.timestamp()), opening, high, low, close, volume)
        for field, value in zip(candles, values):
            candles[field].append(value)

    previous_open = datetime(2026, 10, 21, 9, 15, tzinfo=IST)
    for index in range(75):
        add(previous_open + timedelta(minutes=index * 5), 100, 100.4, 99.6, 100)
    today_open = datetime(2026, 10, 22, 9, 15, tzinfo=IST)
    for index in range(36):
        if index < 3:
            add(today_open + timedelta(minutes=index * 5), 102.4, 103, 101.8, 102.5)
        else:
            add(today_open + timedelta(minutes=index * 5), 102.5, 102.95, 102.15, 102.6)
    add(today_open + timedelta(minutes=180), 102.65, 103.1, 102.55, 103.04)
    add(today_open + timedelta(minutes=185), 103.05, 103.09, 102.99, 103.03)
    add(today_open + timedelta(minutes=190), 103.02, 103.28, 103.0, 103.24, 2000)
    return candles, candles["timestamp"][-1] + 300


class StockSetupTests(unittest.TestCase):
    def setUp(self):
        self.candles, self.as_of = confirmed_bullish_fixture()

    def test_bullish_confirmation(self):
        result = evaluate_stock_setup(self.candles, self.as_of)
        self.assertIsNotNone(result)
        self.assertEqual(result["opt_type"], "CE")
        self.assertEqual(result["signal_timestamp"], self.as_of - 300)
        self.assertAlmostEqual(result["day_change_pct"], 3.24)
        self.assertEqual(result["orb_high"], 103)
        self.assertEqual(result["orb_low"], 101.8)
        self.assertEqual(result["spot_ltp"], 103.24)
        self.assertAlmostEqual(result["relative_volume"], 2.0)
        self.assertGreater(result["signal_quality"], 0)
        self.assertLessEqual(result["signal_quality"], 100)

    def test_bearish_confirmation(self):
        original = copy.deepcopy(self.candles)
        for field, counterpart in (("open", "open"), ("high", "low"), ("low", "high"), ("close", "close")):
            self.candles[field] = [200 - value for value in original[counterpart]]
        result = evaluate_stock_setup(self.candles, self.as_of)
        self.assertIsNotNone(result)
        self.assertEqual(result["opt_type"], "PE")
        self.assertAlmostEqual(result["day_change_pct"], -3.24)

    def test_retest_must_touch_boundary_and_hold(self):
        for low, close in ((102.5, 103.03), (103.13, 103.15), (102.99, 102.99)):
            with self.subTest(low=low, close=close):
                data = copy.deepcopy(self.candles)
                data["low"][-2] = low
                data["close"][-2] = close
                data["open"][-2] = max(data["open"][-2], low)
                data["high"][-2] = max(data["high"][-2], close + 0.01)
                self.assertIsNone(evaluate_stock_setup(data, self.as_of))

    def test_confirmation_requires_volume_expansion(self):
        self.candles["volume"][-1] = 1499
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_zero_volume_and_weak_body_rejected(self):
        for field, value in (("volume", 0), ("open", 103.2), ("close", 103.08)):
            with self.subTest(field=field):
                data = copy.deepcopy(self.candles)
                data[field][-1] = value
                self.assertIsNone(evaluate_stock_setup(data, self.as_of))

    def test_future_and_forming_bars_cannot_change_signal(self):
        expected = evaluate_stock_setup(self.candles, self.as_of)
        # This large future bar would destroy both trend and VWAP if read early.
        for field, value in (("timestamp", self.as_of), ("open", 200), ("high", 400),
                             ("low", 100), ("close", 300), ("volume", 1000000)):
            self.candles[field].append(value)
        self.assertEqual(evaluate_stock_setup(self.candles, self.as_of + 60), expected)

    def test_confirmation_cannot_be_used_before_close(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of - 1))

    def test_stale_confirmation_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of + 301))

    def test_previous_day_signal_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of + 86400))

    def test_gapped_pattern_rejected(self):
        for field in self.candles:
            del self.candles[field][-2]
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_missing_opening_range_bar_rejected(self):
        for field in self.candles:
            del self.candles[field][76]
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_prior_session_close_is_required(self):
        data = {field: values[75:] for field, values in self.candles.items()}
        self.assertIsNone(evaluate_stock_setup(data, self.as_of))

    def test_insufficient_15m_history_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of, ema_period=100))

    def test_threshold_applies_to_closed_day_move(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of, min_day_change_pct=4))

    def test_malformed_data_fails_closed(self):
        for corrupt in (lambda data: data["volume"].pop(),
                        lambda data: data["close"].__setitem__(-1, float("nan")),
                        lambda data: data["low"].__setitem__(-1, 1000)):
            with self.subTest(corrupt=corrupt):
                data = copy.deepcopy(self.candles)
                corrupt(data)
                self.assertIsNone(evaluate_stock_setup(data, self.as_of))


if __name__ == "__main__":
    unittest.main()

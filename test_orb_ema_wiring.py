import unittest
from stock_option_signals import detect_orb_ema_trend_continuation


class OrbEmaTests(unittest.TestCase):
    def test_gap_down_can_use_session_vwap(self):
        closes = [1900]*50 + [1766,1764,1763,1762,1761,1753]
        bars = [{"date": i >= 50, "timestamp": i*300, "open": c+1,
                 "high": c+2, "low": c-1, "close": c, "volume": 10000}
                for i,c in enumerate(closes)]
        bars[-1].update(open=1761, high=1763, low=1752, volume=18000)
        result = detect_orb_ema_trend_continuation(bars, 1785, 1760)
        self.assertIsNone(result["reason"])
        self.assertEqual(result["side"], "PE")
        self.assertGreater(result["ema21_stretch_pct"], 2.2)
        self.assertLess(result["vwap_stretch_pct"], 2.2)
        bars[-1].update(open=1750, high=1754, low=1680, close=1682)
        result = detect_orb_ema_trend_continuation(bars, 1785, 1760)
        self.assertEqual(result["reason"], "Overextended from EMA21 and VWAP")

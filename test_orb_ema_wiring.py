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

class ItmFallbackTests(unittest.TestCase):
    def test_adjacent_listed_strike_and_delta_gate(self):
        from types import SimpleNamespace
        from dhan_dynamic_fno_stock_bot import DynamicStockOptionsUniverse

        for side, neighbor in (("CE", 95), ("PE", 110)):
            with self.subTest(side=side):
                strikes = (90, 95, 100, 110, 120)
                stock = dict(symbol="TEST", strike_step=5, fno_lot_size=100,
                             nearest_expiry="2026-11-23", strikes_map={
                                 f"{strike:.2f}": {side: dict(security_id=strike, trading_symbol=str(strike))}
                                 for strike in strikes
                             })
                sign = 1 if side == "CE" else -1
                chain = {str(strike): {side.lower(): dict(
                    greeks=dict(delta=sign*(0.5 if strike == 100 else 0.6), theta=-1, gamma=0.01),
                    implied_volatility=18, security_id=strike,
                )} for strike in strikes}
                cfg = SimpleNamespace(min_delta=0.3, max_delta=0.7)
                resolve = DynamicStockOptionsUniverse.resolve_atm_option
                result = resolve(stock, 100, side, chain, cfg)
                self.assertEqual(result["itm_fallback_contract"]["strike"], neighbor)
                del chain[str(neighbor)]
                self.assertIsNone(resolve(stock, 100, side, chain, cfg)["itm_fallback_contract"])

    def test_sub_floor_primary_only_tries_adjacent_contract_and_uses_strict_spread(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import dhan_dynamic_fno_stock_bot as bot

        def quote(bid, ask):
            return {"received_epoch": 1000, "depth": {
                "buy": [{"price": bid, "quantity": 100}],
                "sell": [{"price": ask, "quantity": 100}],
            }}

        primary = dict(symbol="TEST", opt_type="CE", strike=385, opt_security_id="atm",
                       lot_size=100, signal_timestamp=600, day_change_pct=7,
                       delta=.5, theta=-1, gamma=.01, atr_5m=5, expiry="1970-02-01",
                       itm_fallback_contract=dict(strike=380, opt_security_id="itm"),
                       alternate_contracts=[dict(strike=375, opt_security_id="other")])
        cfg = SimpleNamespace(max_spread_pct=3.5, max_premium=100, max_lots=1, slippage_pct=.1, min_days_to_expiry=3, rupee_max_risk=950,
                              rupee_target=500, plan=Mock(return_value=None))
        for book, expected in ((quote(9.75, 9.85), "No affordable lot count"),
                               (quote(4.65, 4.75), "outside"),
                               (quote(9.70, 10.10), "above 2.00%"),
                               (quote(0, 9.85), "Unusable bid/ask")):
            with self.subTest(book=book):
                client = Mock()
                client.fetch_option_quotes.return_value = {"atm": quote(5.15, 5.25), "itm": book}
                with patch.object(bot.time, "time", return_value=1000):
                    self.assertEqual(bot.qualify_stock_contracts(client, primary, cfg, 50000, "TEST"), (None, {}))
                calls = client.logger.info.call_args_list
                self.assertEqual([call.args[-2] for call in calls], [385, 380])
                self.assertIn(expected, calls[-1].args[-1])

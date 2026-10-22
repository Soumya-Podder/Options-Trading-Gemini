import argparse
import copy
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import dhan_dynamic_fno_stock_bot as bot


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = bot.DynamicStrategyConfig({}, argparse.Namespace())
        self.now = datetime(2026, 10, 22, 11, 0, 10, tzinfo=bot.IST)
        self.sig = {"symbol": "TEST", "lot_size": 500, "signal_timestamp": self.now.timestamp()-310}
        self.quote = {"last_trade_time": "22/10/2026 11:00:10",
                      "depth": {"buy": [{"price": 99.95, "quantity": 500}],
                                "sell": [{"price": 100, "quantity": 500}]}}

    def qualify(self, quote=None, wallet=60000):
        return bot.qualify_quote(self.sig, quote or self.quote, self.cfg, wallet, self.now.timestamp())

    def test_qualified_quote_and_fixed_exit_plan(self):
        s = self.qualify()
        self.assertIsNotNone(s)
        self.assertEqual(s["hard_target_price"], 103)
        self.assertEqual(s["initial_sl"], 99.6)
        self.assertGreaterEqual(s["expected_net_rr"], 3)

    def test_wide_spread_stale_depth_or_insufficient_size_rejected(self):
        for edit in (lambda q: q["depth"]["buy"][0].update(price=99),
                     lambda q: q["depth"]["sell"][0].update(quantity=499),
                     lambda q: q.update(last_trade_time="22/10/2026 10:59:00")):
            q = copy.deepcopy(self.quote)
            edit(q)
            self.assertIsNone(self.qualify(q))
        self.assertIsNone(self.qualify(wallet=1000))

    def test_stale_confirmation_rejected(self):
        self.sig["signal_timestamp"] -= 300
        self.assertIsNone(self.qualify())

    def test_best_setup_is_not_first_or_largest_mover(self):
        ranked = bot.rank_candidates([{"symbol":"FIRST", "signal_quality":70, "expected_net_rr":4, "day_change_pct":8},
                                      {"symbol":"BEST", "signal_quality":90, "expected_net_rr":3.2, "day_change_pct":2.5}])
        self.assertEqual(ranked[0]["symbol"], "BEST")

    def test_shortlist_preserves_eight_each_side(self):
        quotes = {str(i): {"last_price": 103+i, "ohlc":{"close":100}} for i in range(10)}
        quotes.update({str(i+20): {"last_price": 97-i, "ohlc":{"close":100}} for i in range(10)})
        result = bot.shortlist_movers(quotes, self.cfg)
        self.assertEqual(len(result), 16)
        self.assertEqual(sum(x[0]>0 for x in result), 8)

    def test_old_environment_cannot_increase_daily_cap(self):
        cfg = bot.DynamicStrategyConfig({"STOCK_OPT_MAX_DAILY_TRADES":"4", "STOCK_OPT_MAX_CONCURRENT":"2"}, argparse.Namespace())
        self.assertEqual((cfg.max_daily_trades, cfg.max_concurrent), (1,1))

    def bars(self):
        ts = int(self.now.timestamp())
        return ts, {ts + 300*i: {"open":100, "high":101, "low":99.9, "close":100.5, "strike":1000} for i in range(3)}

    def test_stop_first_when_bar_hits_both(self):
        ts, bars = self.bars()
        bars[ts].update(high=104, low=99)
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertEqual(sim["exit_reason"], "PREMIUM_STOP")
        self.assertLess(sim["exit_price"], sim["initial_sl"])

    def test_gap_stop_uses_open_below_stop(self):
        ts, bars = self.bars()
        bars[ts+300].update(open=98, low=97, close=98, high=99)
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertLess(sim["exit_price"], 98)

    def test_missing_or_different_strike_is_unvalidated(self):
        ts, bars = self.bars()
        del bars[ts+300]
        self.assertIsNone(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg))
        ts, bars = self.bars()
        bars[ts+300]["strike"] = 1010
        self.assertIsNone(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg))

    def test_fixed_target_and_time_exit(self):
        ts, bars = self.bars()
        bars[ts]["high"] = 105
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertEqual(sim["exit_reason"], "PREMIUM_TARGET")
        self.assertLessEqual(sim["exit_price"], sim["entry_price"]*1.03)
        ts, bars = self.bars()
        self.assertEqual(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)["exit_reason"], "TIME_EXIT_15M")

    def test_net_metrics_count_fee_loser_as_loss(self):
        ledger = {"initial_capital":1000, "trades":[{"gross_pnl":10, "net_pnl":-40}, {"gross_pnl":200,"net_pnl":150}]}
        bot.update_ledger_metrics(ledger)
        self.assertEqual(ledger["net_win_to_loss_ratio"], 3.75)
        self.assertEqual(ledger["virtual_balance_net"], 1110)

    def test_ledger_recovery_is_idempotent(self):
        ledger = {"initial_capital":60000, "trades":[]}
        pos = {"symbol":"TEST", "contract":"TEST_CE", "opt_type":"CE", "scanner":"TEST", "qty":500,
               "entry_price":100, "opt_1r":0.5, "entry_ts":"2026-10-22 11:00:00", "entry_id":"unique"}
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, "LEDGER_FILE", str(Path(folder)/"ledger.json")):
            bot.close_paper_position(ledger, pos, 103, "PREMIUM_TARGET", self.now)
            bot.close_paper_position(ledger, pos, 103, "PREMIUM_TARGET", self.now)
            self.assertEqual(len(ledger["trades"]), 1)


if __name__ == "__main__":
    unittest.main()

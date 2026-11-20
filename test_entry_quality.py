import math
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import dhan_bot_engine as index
from dhan_mcx_mini_options_bot import mcx_trade_quality_and_session_gate
from nifty_market_structure import compute_multi_timeframe_sr_and_bounces
from nifty_oi import IST, validate_option_greeks_and_iv
from stock_option_signals import compute_bsm_greeks_for_stock_option, evaluate_smc_institutional_breakout


class EntryQualityTests(unittest.TestCase):
    def node(self, side='CE'):
        return {'greeks': {'delta': .55 if side == 'CE' else -.55, 'gamma': .002, 'theta': -12},
                'implied_volatility': 18, 'top_ask_price': 100}

    def view(self):
        return {'index': 'NIFTY', 'spot': 25000, 'd_spot_5m': 25, 'days_to_expiry': .5,
                'regime_info': {'regime': 'TREND'},
                'market_structure': {'ready': True, 'mtf_sr': {'multi_bounce_zones': [],
                                                              'htf_4h_levels': [25008], 'mtf_15m_levels': []}},
                'oi_analysis': {'nearby_strikes': [dict(strike=25000, side='ce', change_over_5m=-100,
                                                       iv_change_5m=.2, premium_change_over_5m=5)]}}

    def test_index_reservation_blocks_shared_underlying(self):
        for slot, other in (("active_position", "active_channel_position"),
                            ("active_channel_position", "active_position")):
            ledger = {slot: {"index": "NIFTY", "option_type": "CE"}}
            self.assertFalse(index.reserve_index_entry(
                ledger, "2026-11-20", {"index": "NIFTY", "option_type": "PE"}, position_key=other))
            self.assertFalse(index.index_has_open_position("SENSEX", ledger.values()))

    def test_executable_quote_rejects_missing_stale_future_and_crossed(self):
        node = dict(top_bid_price=140, top_ask_price=141, depth_received_epoch=100)
        self.assertIsNone(index.index_entry_quote_block_reason(node, 105))
        for bad, now in (({}, 105), (node, 105.01), (node, 99),
                         ({**node, "top_bid_price": 142}, 105),
                         ({**node, "top_ask_price": float("nan")}, 105)):
            self.assertIsNotNone(index.index_entry_quote_block_reason(bad, now))

    def test_nifty_daily_budget_rejects_second_trade_stop(self):
        day = "2026-11-20"
        ledger = {"trades": [{"index": "NIFTY", "exit_timestamp": day + " 09:47:48", "net_pnl": -630}]}
        self.assertIsNone(index.nifty_daily_entry_risk_reason({}, day, 142.75, 134.95, 1, 65))
        self.assertIn("planned stop", index.nifty_daily_entry_risk_reason(ledger, day, 96.50, 90.50, 1, 65))
        ledger["trades"][0]["net_pnl"] = -850
        self.assertIn("limit", index.nifty_daily_entry_risk_reason(ledger, day, 96.50, 90.50, 1, 65))

    def test_missing_and_wrong_sign_greeks_fail(self):
        for node in ({}, self.node('PE')):
            self.assertFalse(validate_option_greeks_and_iv(node, 'CE')['greek_supported'])

    def test_daily_theta_uses_holding_horizon(self):
        result = validate_option_greeks_and_iv(self.node(), 'CE', expected_spot_move=25, is_expiry_day=True)
        self.assertTrue(result['greek_supported'])
        self.assertAlmostEqual(result['modeled_decay_points'], 12 * 20 / 375, places=4)

    def test_low_delta_and_theta_trap_veto(self):
        for changes in ({'delta': .2}, {'theta': -300}):
            node = self.node()
            node['greeks'].update(changes)
            self.assertFalse(validate_option_greeks_and_iv(node, 'CE', expected_spot_move=10)['greek_supported'])

    def test_iv_crush_matches_selected_strike(self):
        view = self.view()
        self.assertTrue(index.index_contract_quality(view, self.node(), 25000, 'CE')['allowed'])
        view['oi_analysis']['nearby_strikes'][0]['iv_change_5m'] = -.9
        result = index.index_contract_quality(view, self.node(), 25000, 'CE')
        self.assertIn('MATCHED_CONTRACT_IV_CRUSH', result['failure_factors'])
        self.assertIsNone(index.index_contract_quality(view, self.node(), 25050, 'CE')['greeks']['iv_change_5m'])

    def test_opposing_wall_blocks_both_sources(self):
        for channel in (False, True):
            view = self.view()
            if channel:
                view['market_structure']['telegram_call'] = {'source': 'GrowX'}
            view['market_structure']['mtf_sr']['multi_bounce_zones'] = [
                dict(level=25008, touches=2, role='STRONG_RESISTANCE')]
            result = index.index_contract_quality(view, self.node(), 25000, 'CE')
            self.assertIn('OPPOSING_MULTI_TIMEFRAME_WALL', result['failure_factors'])

    def test_missing_structure_cannot_silently_pass(self):
        view = self.view()
        view['market_structure'] = {}
        self.assertIn('MTF_STRUCTURE_NOT_READY', index.index_contract_quality(view, self.node(), 25000, 'CE')['failure_factors'])

    def test_opening_channel_uses_complete_historical_structure_only(self):
        from nifty_market_structure import apply_nifty_level_entry

        start = datetime(2026, 11, 19, 9, 15, tzinfo=IST)
        prices = [24900 + (i % 24) for i in range(75)]
        candles = {
            "timestamp": [(start + timedelta(minutes=5*i)).timestamp() for i in range(75)],
            "open": prices, "close": prices,
            "high": [price + 4 for price in prices],
            "low": [price - 4 for price in prices],
        }
        view = self.view()
        now = (start + timedelta(days=1)).timestamp()
        structure = apply_nifty_level_entry(view, candles, {}, now)
        self.assertFalse(structure["ready"])
        self.assertFalse(structure["allowed"])
        self.assertTrue(structure["historical_mtf_ready"])
        native = index.index_contract_quality(view, self.node(), 25000, "CE")
        channel = index.index_contract_quality(view, self.node(), 25000, "CE", channel_entry=True)
        self.assertIn("MTF_STRUCTURE_NOT_READY", native["failure_factors"])
        self.assertTrue(channel["allowed"], channel["failure_factors"])

        candles = {key: values[:-1] for key, values in candles.items()}
        apply_nifty_level_entry(view, candles, {}, now)
        channel = index.index_contract_quality(view, self.node(), 25000, "CE", channel_entry=True)
        self.assertIn("MTF_STRUCTURE_NOT_READY", channel["failure_factors"])

    def test_bsm_recovers_known_atm_volatility(self):
        s, k, t, iv, rate = 100, 100, 30 / 365, .25, .065
        cdf = lambda x: .5 * (1 + math.erf(x / math.sqrt(2)))
        d1 = (rate + .5 * iv * iv) * t / (iv * math.sqrt(t))
        d2 = d1 - iv * math.sqrt(t)
        premium = s * cdf(d1) - k * math.exp(-rate * t) * cdf(d2)
        result = compute_bsm_greeks_for_stock_option(s, k, premium, 'CE', 30)
        self.assertAlmostEqual(result['iv_pct'], 25, places=5)
        self.assertTrue(result['greek_supported'])

    def test_bsm_invalid_prices_and_expiry_fail(self):
        for price, days in ((101, 30), (-1, 30), (5, 0), (float('nan'), 30)):
            self.assertFalse(compute_bsm_greeks_for_stock_option(100, 100, price, 'CE', days)['greek_supported'])

    def bars(self):
        bars = [dict(open=100, high=101, low=99, close=100, volume=100) for _ in range(11)]
        bars.append(dict(open=102, high=106, low=102, close=105.5, volume=200))
        return bars

    def test_bullish_bearish_breakout_and_trap(self):
        bars = self.bars()
        self.assertTrue(evaluate_smc_institutional_breakout(bars, 'CE')['smc_passed'])
        bearish = [dict(open=200-b['open'], high=200-b['low'], low=200-b['high'],
                        close=200-b['close'], volume=b['volume']) for b in bars]
        self.assertTrue(evaluate_smc_institutional_breakout(bearish, 'PE')['smc_passed'])
        bars[-1].update(open=100, low=99, close=100)
        self.assertTrue(evaluate_smc_institutional_breakout(bars, 'CE')['is_fakeout'])

    def test_no_fvg_or_missing_volume_cannot_pass(self):
        bars = self.bars()
        bars[-1]['low'] = 100
        self.assertFalse(evaluate_smc_institutional_breakout(bars, 'CE')['smc_passed'])
        self.assertFalse(evaluate_smc_institutional_breakout([], 'CE')['smc_passed'])

    def test_mcx_quote_and_chop_fail_closed(self):
        bars = [dict(open=100+i, high=101+i, low=100+i, close=100.8+i) for i in range(12)]
        when = datetime(2026, 11, 18, 18, tzinfo=IST)
        self.assertTrue(mcx_trade_quality_and_session_gate(bars, {'top_bid_price': 100, 'top_ask_price': 101}, 30, 40, when)[0])
        for quote in ({}, {'top_bid_price': 100, 'top_ask_price': 104}):
            self.assertFalse(mcx_trade_quality_and_session_gate(bars, quote, 30, 40, when)[0])
        flat = [dict(open=100, high=101, low=99, close=100) for _ in range(12)]
        self.assertFalse(mcx_trade_quality_and_session_gate(flat, None, 30, 40, when)[0])

    def test_four_hour_reference_requires_48_contiguous_bars(self):
        start = datetime(2026, 11, 18, 9, 15, tzinfo=IST).timestamp()
        bars = [dict(ts=start+i*300, open=100, high=101, low=99, close=100) for i in range(48)]
        self.assertFalse(compute_multi_timeframe_sr_and_bounces(bars[:36], bars[:36], 100)['htf_4h_levels'])
        self.assertEqual(compute_multi_timeframe_sr_and_bounces(bars, bars, 100)['htf_4h_levels'], [99, 101])

    def test_completed_trade_updates_quality_factors(self):
        with tempfile.TemporaryDirectory() as folder:
            brain = index.SelfLearningBrain(logging.getLogger('quality-test'),
                str(Path(folder) / 'brain.json'), str(Path(folder) / 'rules.json'), fresh=True)
            quality = {'factors': {'iv_trend': 'FALLING'}}
            for reward in (1.5, -.5):
                brain.learn_from_trade('2026-11-18', 'test', [0.0]*10, 1, reward, trade_quality=quality)
            result = brain.brain['quality_factor_outcomes']['1|iv_trend|FALLING']
            self.assertEqual((result['trades'], result['wins'], result['net_r_sum']), (2, 1, 1.0))

    def test_stock_theta_uses_hold_window_and_atr(self):
        from stock_option_signals import evaluate_intraday_theta_viability
        self.assertTrue(evaluate_intraday_theta_viability(-9.82, .5, 8, 100)[0])
        self.assertIn("EXCEEDS_ATR", evaluate_intraday_theta_viability(-9.82, .5, 1, 100)[1])
        self.assertIn("EXTREME", evaluate_intraday_theta_viability(-35, .5, 30, 100)[1])

    def test_stock_execution_gate_checks_model_at_actual_ask(self):
        import argparse
        import dhan_dynamic_fno_stock_bot as stock
        now = datetime(2026, 10, 22, 11, 0, 10, tzinfo=IST)
        cfg = stock.DynamicStrategyConfig({'STOCK_OPT_STOP_PCT': '1', 'STOCK_OPT_TARGET_PCT': '10',
            'STOCK_OPT_MIN_NET_RR': '1.2', 'STOCK_OPT_RUPEE_TARGET': '2000',
            'STOCK_OPT_RUPEE_MAX_RISK': '1000', 'STOCK_OPT_MAX_LOTS': '3'}, argparse.Namespace())
        signal = dict(symbol='TEST', lot_size=500, spot_ltp=1000, strike=1000,
                      signal_timestamp=now.timestamp()-310, delta=.6, theta=-1, gamma=.01,
                      iv=20, iv_range_low=15, iv_range_high=30, opt_type='CE',
                      greeks_timestamp=now.timestamp(), expiry='2026-11-26', atr_5m=20)
        quote = {'received_epoch': now.timestamp(), 'last_trade_time': '22/10/2026 11:00:10',
                 'depth': {'buy': [dict(price=99.95, quantity=500)],
                           'sell': [dict(price=100, quantity=500)]}}
        qualified = stock.qualify_quote(signal, quote, cfg, 60000, now.timestamp())
        self.assertEqual(qualified["lots"], 1)
        self.assertLess(qualified["net_profit_target"], cfg.rupee_target)
        self.assertGreaterEqual(qualified["expected_net_rr"], 1.5)
        reasons = []
        near_wall = {**signal, "mtf_liquidity": {"nearest_opposing_zone_pct": .04}}
        self.assertIsNone(stock.qualify_quote(near_wall, quote, cfg, 60000, now.timestamp(), reasons))
        self.assertIn("3x friction", reasons[0])
        with patch.object(stock, 'compute_bsm_greeks_for_stock_option',
                          return_value={'greek_supported': False, 'reasons': ['STOCK_OPT_EXTREME_IV']}) as model:
            reasons = []
            self.assertIsNone(stock.qualify_quote(signal, quote, cfg, 60000, now.timestamp(), reasons))
            self.assertEqual(model.call_args.args[2], 100)
            self.assertIn('BSM quality', reasons[0])


if __name__ == '__main__':
    unittest.main()

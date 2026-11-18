import unittest
from datetime import datetime
from unittest.mock import patch

from nifty_oi import IST, NiftyOITracker
from stock_option_signals import _resample_candles, analyze_mtf_liquidity_and_reversal_zones, evaluate_stock_setup


class HorizonMatrixTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 18, 9, 15, tzinfo=IST).timestamp()

    def chain(self, minute=0):
        return {str(25000 + i*50): {
            side: {'oi': 10000 + minute * (100 if side == 'ce' else 300),
                   'last_price': 100 - minute*.01, 'previous_oi': 10000,
                   'volume': 1000+minute*10, 'implied_volatility': 20+minute*.001,
                   'security_id': f'{i}:{side}',
                   'greeks': {'delta': (.5 if side == 'ce' else -.5) + minute*.0001,
                              'gamma': .002, 'theta': -10}}
            for side in ('ce', 'pe')} for i in range(-4, 5)}

    def test_all_seven_horizons_survive_full_session(self):
        tracker = NiftyOITracker()
        for minute in range(376):
            _, info = tracker.assess(self.chain(minute), 25000, self.start+minute*60, '2026-11-19')
        matrix = info['multi_horizon_matrix']
        self.assertEqual(len(matrix['horizons']), 7)
        self.assertTrue(all(r['available'] for r in matrix['horizons'].values()))
        self.assertEqual(matrix['horizons']['4h']['ce_oi_change'], 7*240*100)
        self.assertEqual(matrix['horizons']['4h']['pe_oi_change'], 7*240*300)
        self.assertAlmostEqual(matrix['horizons']['4h']['atm_ce']['iv_change'], .24)
        self.assertAlmostEqual(matrix['horizons']['4h']['atm_ce']['delta_change'], .024)
        self.assertAlmostEqual(matrix['horizons']['4h']['atm_pe']['absolute_delta_change'], -.024)
        self.assertFalse(matrix['directions']['PE']['multi_horizon_supported'])
        self.assertLessEqual(len(tracker.history), 400)

    def test_polling_replaces_same_minute_and_pins_first_observation(self):
        tracker = NiftyOITracker()
        for second in range(60):
            tracker.assess(self.chain(), 25000, self.start+second, 'expiry')
        self.assertEqual(len(tracker.history), 1)
        self.assertEqual(tracker.morning_snapshot['time'], self.start)
        self.assertEqual(tracker.history[-1]['time'], self.start+59)

    def test_no_substitution_of_short_history_for_long_horizon(self):
        tracker = NiftyOITracker()
        tracker.assess(self.chain(), 25000, self.start+1500, 'expiry')
        _, info = tracker.assess(self.chain(20), 25000, self.start+2700, 'expiry')
        matrix = info['multi_horizon_matrix']
        self.assertFalse(matrix['horizons']['4h']['available'])
        self.assertIsNone(matrix['horizons']['4h']['ce_oi_change'])
        self.assertEqual(matrix['horizons']['morning']['reference_epoch'], self.start+1500)

    def test_security_roll_and_missing_strikes_are_unavailable(self):
        for changed in ('security', 'missing'):
            tracker = NiftyOITracker()
            tracker.assess(self.chain(), 25000, self.start, 'expiry')
            current = self.chain(5)
            if changed == 'security':
                current['25000']['ce']['security_id'] = 'different'
            else:
                del current['25000']['ce']
            _, info = tracker.assess(current, 25000, self.start+300, 'expiry')
            self.assertFalse(info['multi_horizon_matrix']['horizons']['5m']['available'])

    def test_expiry_and_day_changes_reset_matrix(self):
        for change in ('expiry', 'day'):
            tracker = NiftyOITracker()
            tracker.assess(self.chain(), 25000, self.start, 'a')
            when = self.start+300 if change == 'expiry' else self.start+86400
            expiry = 'b' if change == 'expiry' else 'a'
            _, info = tracker.assess(self.chain(5), 25000, when, expiry)
            self.assertFalse(info['multi_horizon_matrix']['horizons']['morning']['available'])

    def test_atm_roll_compares_current_identical_strikes(self):
        tracker = NiftyOITracker()
        tracker.assess(self.chain(), 25000, self.start, 'expiry')
        _, info = tracker.assess(self.chain(5), 25050, self.start+300, 'expiry')
        row = info['multi_horizon_matrix']['horizons']['5m']
        self.assertTrue(row['available'])
        self.assertEqual(row['ce_oi_change'], 3500)

    def test_missing_iv_is_not_flat_iv(self):
        tracker = NiftyOITracker()
        tracker.assess(self.chain(), 25000, self.start, 'expiry')
        current = self.chain(5)
        current['25000']['ce']['implied_volatility'] = 0
        _, info = tracker.assess(current, 25000, self.start+300, 'expiry')
        row = info['multi_horizon_matrix']['horizons']['5m']
        self.assertTrue(row['available'])
        self.assertIsNone(row['atm_ce']['iv_change'])

    def test_matrix_veto_reaches_shared_index_contract_gate(self):
        import dhan_bot_engine as bot
        from test_entry_quality import EntryQualityTests
        tracker = NiftyOITracker()
        for minute in range(31):
            _, info = tracker.assess(self.chain(minute), 25000, self.start+minute*60, 'expiry')
        helper = EntryQualityTests()
        view = helper.view()
        view['oi_analysis'] = info
        with patch.object(bot.time, 'time', return_value=self.start+1800):
            result = bot.index_contract_quality(view, helper.node('PE'), 25000, 'PE')
        self.assertIn('PERSISTENT_OPPOSING_MULTI_HORIZON_OI', result['failure_factors'])
        self.assertFalse(result['allowed'])


class LiquidityZoneTests(unittest.TestCase):
    def bars(self, count=75):
        start = datetime(2026, 11, 18, 9, 15, tzinfo=IST).timestamp()
        return [dict(timestamp=start+i*300, open=100, high=101, low=99,
                     close=100, volume=100) for i in range(count)]

    def test_resampling_excludes_partial_gapped_and_cross_day_groups(self):
        bars = self.bars(75)
        self.assertEqual(len(_resample_candles(bars, 48)), 1)
        self.assertEqual(len(_resample_candles(bars[:47], 48)), 0)
        self.assertEqual(len(_resample_candles(bars[:1]+bars[2:48], 48)), 0)
        tomorrow = [{**b, 'timestamp': b['timestamp']+86400} for b in bars[:25]]
        self.assertEqual(len(_resample_candles(bars[50:]+tomorrow, 48)), 0)

    def test_opposing_equal_highs_block_call_symmetrically(self):
        bars = self.bars(60)
        bars[-1].update(open=100.7, low=100.6, high=100.9, close=100.8, volume=200)
        result = analyze_mtf_liquidity_and_reversal_zones(bars, 'CE', 100.8)
        self.assertTrue(result['available'])
        self.assertFalse(result['liquidity_clear'])
        self.assertEqual(result['blocking_zone']['role'], 'SUPPLY')
        mirrored = [{**b, 'open': 200-b['open'], 'high': 200-b['low'],
                     'low': 200-b['high'], 'close': 200-b['close']} for b in bars]
        other = analyze_mtf_liquidity_and_reversal_zones(mirrored, 'PE', 99.2)
        self.assertFalse(other['liquidity_clear'])

    def test_high_volume_sweep_reclaim_has_distinct_entry_evidence(self):
        bars = self.bars(60)
        bars[-1].update(open=99.2, low=98, high=100, close=99.9, volume=200)
        result = analyze_mtf_liquidity_and_reversal_zones(bars, 'CE', 99.9)
        self.assertTrue(result['liquidity_sweep_bounce'])
        self.assertEqual(result['sweep_evidence'][0]['role'], 'DEMAND')
        bars[-1]['volume'] = 50
        self.assertFalse(analyze_mtf_liquidity_and_reversal_zones(bars, 'CE', 99.9)['liquidity_sweep_bounce'])

    def test_incomplete_history_is_unknown(self):
        result = analyze_mtf_liquidity_and_reversal_zones(self.bars(10), 'CE', 100)
        self.assertFalse(result['available'])
        self.assertFalse(result['liquidity_clear'])

    def test_order_block_is_removed_after_later_mitigation(self):
        bars = self.bars(60)
        for i in range(15, 18):
            bars[i].update(open=101, high=103, low=100, close=102)
        for i in range(18, 21):
            bars[i].update(open=102, high=102, low=95, close=96, volume=300)
        for i in range(21, 59):
            bars[i].update(open=96, high=97, low=95, close=96)
        bars[-1].update(open=100, high=100.5, low=99.8, close=100.3)
        result = analyze_mtf_liquidity_and_reversal_zones(bars, 'CE', 100.3)
        blocks = [z for z in result['zones']['15m'] if z['kind'] == 'UNMITIGATED_ORDER_BLOCK']
        self.assertTrue(blocks)
        self.assertFalse(result['liquidity_clear'])
        bars[25]['high'] = 100.1
        result = analyze_mtf_liquidity_and_reversal_zones(bars, 'CE', 100.3)
        self.assertFalse([z for z in result['zones']['15m'] if z['kind'] == 'UNMITIGATED_ORDER_BLOCK'])

    def test_sweep_path_does_not_require_a_new_day_high_breakout(self):
        from test_stock_option_integration import confirmed_bullish_fixture
        candles, now = confirmed_bullish_fixture()
        close = candles['close'][-1]
        liquidity = {'liquidity_clear': True, 'liquidity_sweep_bounce': True,
                     'sweep_evidence': [{'trigger_level': close-.1}], 'zones': {}}
        with patch('stock_option_signals.analyze_mtf_liquidity_and_reversal_zones', return_value=liquidity):
            signal = evaluate_stock_setup(candles, now, require_breakout=True)
        self.assertIsNotNone(signal)
        self.assertEqual(signal['breakout_confirmation']['entry_path'], 'LIQUIDITY_SWEEP_RECLAIM')
        self.assertTrue(signal['smc_quality']['smc_passed'])


if __name__ == '__main__':
    unittest.main()

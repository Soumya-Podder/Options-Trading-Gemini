import json
import tempfile
import unittest
from pathlib import Path

from bot_observability import publish_bot_observation
from dashboard_data import desk_snapshot, levels, scanner_log


class BotObservabilityTests(unittest.TestCase):
    def candles(self):
        return {'timestamp': [1000, 1300], 'open': [100, 101], 'high': [102, 103],
                'low': [99, 100], 'close': [101, 102], 'volume': [10, 20]}

    def test_records_only_closed_candles_and_excludes_unlisted_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(publish_bot_observation('index', 'NIFTY', self.candles(),
                {'spot': 101, 'access_token': 'private', 'entry_decision': {'action': 'HOLD'}},
                directory=directory, now=1400))
            data = json.loads((Path(directory)/'index_desk_telemetry.json').read_text())
            row = data['symbols']['NIFTY']
            self.assertEqual(len(row['candles']), 1)
            self.assertNotIn('access_token', row['analysis'])

    def test_breakout_event_survives_later_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            publish_bot_observation('stock', 'TEST', self.candles(),
                {'signal_timestamp': 1000, 'opt_type': 'CE',
                 'breakout_confirmation': {'closed_candle': True}}, directory=directory, now=1400)
            publish_bot_observation('stock', 'TEST', self.candles(),
                {'entry_decision': {'action': 'HOLD'}}, directory=directory, now=1406)
            data = json.loads((Path(directory)/'stock_desk_telemetry.json').read_text())
            self.assertEqual(data['symbols']['TEST']['events'][0]['timestamp'], 1000)

    def test_latest_membership_rejection_removes_old_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'dhan_stock_fno.log'
            path.write_text('09:30:00 [STOCK SCANNER MATCH] TEST | {"allowed": true, "opt_type": "CE"}\n09:35:00 [OPTSTK REJECT] TEST | Scanner membership: Volume too low\n')
            self.assertFalse(scanner_log(Path(directory))['TEST']['analysis']['scanner_membership']['allowed'])

    def test_invalid_evidence_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(publish_bot_observation('stock', 'TEST', {}, {}, directory=directory, now=1400))
            self.assertFalse(list(Path(directory).iterdir()))

    def test_multiple_symbols_and_throttle_preserve_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            for symbol, now in (('NIFTY', 1400), ('SENSEX', 1401), ('NIFTY', 1406)):
                publish_bot_observation('index', symbol, self.candles(), {}, directory=directory, now=now)
            data = json.loads((Path(directory)/'index_desk_telemetry.json').read_text())
            self.assertEqual(set(data['symbols']), {'NIFTY', 'SENSEX'})

    def test_archive_keeps_actual_breakout_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'dhan_hist_cache').mkdir()
            row = {'snapshot_epoch': 1400, 'closed_index_candles': self.candles(),
                   'analysis': {'recommended_dir': 'BUY_PE', 'market_structure': {
                       'allowed': True, 'setup': 'BREAKOUT_CONFIRMED', 'trigger_level': 102}}}
            (root/'dhan_hist_cache'/'nifty_entry_audit_2026-11-18.jsonl').write_text(json.dumps(row)+'\n')
            data = desk_snapshot(root)
            nifty = data['engines']['Indices']['NIFTY']
            self.assertEqual(nifty['events'][0]['timestamp'], 1000)
            self.assertEqual(nifty['source'], 'Saved decision audit')

    def test_zone_roles_and_timeframes_are_preserved(self):
        result = levels({'mtf_liquidity': {'zones': {'1h': [
            {'lower': 99, 'upper': 101, 'role': 'DEMAND', 'kind': 'UNMITIGATED_ORDER_BLOCK'}]}}})
        self.assertEqual(result[0]['timeframe'], '1h')
        self.assertEqual(result[0]['role'], 'DEMAND')
        self.assertEqual(result[0]['lower'], 99)


if __name__ == '__main__':
    unittest.main()

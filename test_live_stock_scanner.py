import tempfile
import time
import unittest
from unittest.mock import patch
from live_stock_scanner import LiveStockScanner

class LiveScannerTests(unittest.TestCase):
    def scanner(self):
        s=LiveStockScanner('.')
        s.universe={'1':{'symbol':'TEST'}}
        return s

    def quote(self, now, price=103):
        return {'1':{'received_epoch':now,'last_price':price,'ohlc':{'close':100}}}

    def test_stale_or_missing_quotes_never_show_old_matches(self):
        s=self.scanner()
        self.assertFalse(s.evaluate(self.quote(1),100)['symbols'])
        self.assertFalse(s.evaluate({},100)['symbols'])

    def test_references_load_without_blocking_quotes(self):
        s=self.scanner()
        result=s.evaluate(self.quote(1000),1000)
        self.assertEqual(result['pending_references'],1)
        self.assertFalse(result['complete'])
        self.assertEqual(s.pending,{'1':900})

    @patch('live_stock_scanner.scanx_stock_membership')
    def test_each_cycle_replaces_matches(self, membership):
        s=self.scanner()
        s.references['1']={'boundary':900,'daily':{},'candles':{}}
        membership.return_value={'allowed':True,'day_change_pct':3,'opt_type':'CE'}
        first=s.evaluate(self.quote(1000),1000)
        self.assertIn('TEST',first['symbols'])
        second=s.evaluate(self.quote(1003,101),1003)
        self.assertFalse(second['symbols'])
        self.assertTrue(second['complete'])

    def test_after_hours_snapshot_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner=LiveStockScanner(directory)
            result=scanner.empty('LIVE','Live scanner',observed_epoch=1000,
                                 symbols={'TEST':{'symbol':'TEST'}},complete=True)
            scanner.remember(result,force=True)
            restored=LiveStockScanner(directory)
            closed=restored.closed_snapshot()
            self.assertEqual(closed['status'],'LAST_RECORDED')
            self.assertEqual(closed['observed_epoch'],1000)
            self.assertIn('TEST',closed['symbols'])
            restored.remember(restored.empty('ERROR','Connection failed'),force=True)
            self.assertEqual(restored.closed_snapshot()['observed_epoch'],1000)

    def test_empty_success_replaces_previous_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner=LiveStockScanner(directory)
            scanner.remember(scanner.empty('LIVE','',observed_epoch=1000,symbols={'OLD':{}},complete=True),force=True)
            scanner.remember(scanner.empty('LIVE','',observed_epoch=1003,symbols={},complete=True),force=True)
            self.assertFalse(LiveStockScanner(directory).closed_snapshot()['symbols'])

    def test_incomplete_scan_does_not_replace_saved_close(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner=LiveStockScanner(directory)
            scanner.remember(scanner.empty('LIVE','',observed_epoch=1000,symbols={'VALID':{}},complete=True),force=True)
            scanner.remember(scanner.empty('LIVE','Loading',observed_epoch=1003,symbols={},complete=False),force=True)
            self.assertIn('VALID',LiveStockScanner(directory).closed_snapshot()['symbols'])

    def test_snapshot_drops_stale_matches(self):
        s=self.scanner()
        s.document=s.empty('LIVE','',observed_epoch=time.time()-20,symbols={'OLD':{}},complete=True)
        self.assertEqual(s.snapshot()['status'],'STALE')
        self.assertFalse(s.snapshot()['symbols'])

if __name__=='__main__':unittest.main()

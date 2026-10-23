import math
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock,patch
from datetime import datetime, timedelta

import nifty_research as research
import dhan_bot_engine as bot


def row(strike, close=100):
    return {'open':close,'high':close+1,'low':close-1,'close':close,'strike':strike}


class FixedContractTests(unittest.TestCase):
    def test_enters_analyzer_selected_contract_instead_of_rolling_atm(self):
        analysis={'atm_ce_strike':22050,'atm_pe_strike':22000}
        self.assertEqual(research.selected_entry_strike(analysis,'CALL'),22050)
        self.assertEqual(research.selected_entry_strike(analysis,'PUT'),22000)

    def test_next_day_retry_keeps_observed_statistics(self):
        brain=MagicMock();brain.rules={'loss':{'status':'PRUNED','trades_count':2,'wins_count':0},
                                     'good':{'status':'ACTIVE','trades_count':10,'wins_count':7}}
        research.retry_pruned_next_day(brain)
        self.assertEqual(brain.rules['loss'],{'status':'CANDIDATE','trades_count':2,'wins_count':0})
        self.assertEqual(brain.rules['good']['status'],'ACTIVE')

    def test_tracks_actual_strike_after_atm_changes(self):
        legs={'ATM':{1:row(22000,100),2:row(22050,90)},
              'ATM-1':{1:row(21950,110),2:row(22000,105)}}
        bars=research.fixed_contract_bars(legs,[1,2],22000)
        self.assertEqual(bars['close'],[100,105])
        self.assertEqual(bars['strike'],[22000,22000])

    def test_missing_strike_stays_missing_instead_of_estimated(self):
        bars=research.fixed_contract_bars({'ATM':{1:row(22050)}},[1],22000)
        self.assertTrue(math.isnan(bars['close'][0]))

    def test_conflicting_actual_observations_fail(self):
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            research.fixed_contract_bars({'a':{1:row(22000,100)},'b':{1:row(22000,105)}},[1],22000)

    def test_duplicate_matching_observations_are_accepted(self):
        bars=research.fixed_contract_bars({'a':{1:row(22000)},'b':{1:row(22000)}},[1],22000)
        self.assertEqual(bars['close'],[100])

    def test_stitched_contract_uses_actual_gap_price(self):
        start=int(datetime(2026,10,22,10).timestamp())
        stamps=[start,start+60]
        legs={'ATM':{stamps[0]:row(22000,100),stamps[1]:row(22050,100)},
              'ATM-1':{stamps[1]:row(22000,80)}}
        bars=research.fixed_contract_bars(legs,stamps,22000)
        result=bot.evaluate_real_option_trade_bars(0,True,stamps,bars['open'],bars['high'],bars['low'],
            bars['close'],bars['strike'],6.15,interval_min=1,expected_strike=22000,entry_at_open=True)
        self.assertEqual(result[1],80)


class ResearchValidationTests(unittest.TestCase):
    def data(self):
        return {'timestamp':[1],**{k:[v] for k,v in {'open':100,'high':101,'low':99,'close':100,'oi':1000,'iv':20,'strike':22000,'spot':22000}.items()}}

    def test_misaligned_data_rejected(self):
        data=self.data();data['strike']=[]
        with self.assertRaisesRegex(ValueError,'unaligned'):research.checked_rows(data)

    def test_bad_ohlc_rejected(self):
        data=self.data();data['low']=[105]
        with self.assertRaisesRegex(ValueError,'OHLC'):research.checked_rows(data)

    def test_nonfinite_prices_rejected(self):
        data=self.data();data['close']=[float('nan')]
        with self.assertRaisesRegex(ValueError,'Non-finite'):research.checked_rows(data)

    def days(self,n,gross=1000):
        return [{'day':str(datetime(2026,1,1).date()+timedelta(days=i)),
                 'gross':gross,'net':gross-65,'trades':1,'unvalidated':False} for i in range(n)]

    def test_ten_day_target_and_partial_block(self):
        blocks=research.blocks(self.days(12))
        self.assertTrue(blocks[0]['target_met'])
        self.assertFalse(blocks[1]['target_met'])
        self.assertEqual(blocks[0]['gross'],10000)

    def test_zero_trade_days_are_included(self):
        days=self.days(10);days[-1].update(gross=0,net=0,trades=0)
        block=research.blocks(days)[0]
        self.assertEqual(block['sessions'],10)
        self.assertFalse(block['target_met'])

    def test_unvalidated_outcomes_cannot_pass_target(self):
        days=self.days(10,2000);days[0]['unvalidated']=True
        self.assertFalse(research.blocks(days)[0]['target_met'])

    def bracket(self,bars):
        start=int(datetime(2026,10,22,10).timestamp())
        data={'timestamp':[start+60*i for i in range(len(bars))]}
        data.update({k:[b[j] for b in bars] for j,k in enumerate(('open','high','low','close'))})
        return research.fixed_target_replay(0,data,6.15,20)

    def test_bracket_ambiguous_bar_uses_stop_first(self):
        result=self.bracket([(100,125,90,110)])
        self.assertEqual(result[3],'INITIAL_SL_HIT')
        self.assertLess(result[1],94)

    def test_bracket_opening_gap_uses_actual_worse_price(self):
        result=self.bracket([(100,101,99,100),(80,85,79,82)])
        self.assertEqual(result[1],79.9)

    def test_bracket_target_is_bounded_and_slipped(self):
        result=self.bracket([(100,125,99,120)])
        self.assertEqual(result[3],'SINGLE_LOT_TARGET_20')
        self.assertLess(result[1],120.1)

    def test_bracket_cannot_book_early_end_as_session_close(self):
        with self.assertRaisesRegex(bot.ReplayDataError,'before close'):
            self.bracket([(100,101,99,100)])

    def test_bracket_missing_prices_are_unvalidated(self):
        with self.assertRaises(bot.ReplayDataError):
            self.bracket([(100,float('nan'),99,100)])

    def test_actual_research_trade_cannot_overwrite_production_ledger(self):
        runtime=research.ROOT/'paper_trade_ledger.json'
        before=runtime.read_bytes()
        day='2026-10-22'
        start=int(datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=research.IST).timestamp())
        stamps=[start+300*i for i in range(75)]
        legs={side:{} for side in ('CALL','PUT')}
        for side in legs:
            for label in research.LABELS:
                offset=0 if label=='ATM' else int(label[3:])
                legs[side][label]={t:{**row(22000+50*offset), 'oi':10000000,'iv':20,'spot':22025} for t in stamps}
        index={t:row(22000) for t in stamps}
        brain=MagicMock();brain.select_action.return_value=('BUY_CE','test');brain.compute_lots.return_value=1
        research.WORK.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=research.WORK) as temp:
            out=Path(temp)/'case'
            with patch.object(bot,'SelfLearningBrain',return_value=brain):
                result=research.run_candidate({'name':'isolation','stop':6.15,'entry':'guard'},
                    {day:stamps},legs,index,out,bot.MarketMicrostructureAnalyzer)
            self.assertGreater(len(result['ledger']['trades']),0)
            self.assertTrue((out/'paper_trade_ledger.json').exists())
            self.assertEqual(runtime.read_bytes(),before)


if __name__=='__main__':unittest.main()

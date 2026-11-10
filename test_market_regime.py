import copy
import unittest
from datetime import datetime,timedelta

from market_regime import opening_trend
from stock_option_signals import IST
from stock_option_signals import evaluate_stock_setup
from stock_option_policy import premium_plan, stock_reentry_reason
import dhan_dynamic_fno_stock_bot as stock
import test_nifty_market_structure as level_fixtures
from nifty_market_structure import apply_nifty_level_entry


class OpeningRegimeTests(unittest.TestCase):
    def bars(self):
        start=datetime(2026,10,22,9,15,tzinfo=IST)
        prior={'ts':(start-timedelta(days=1)).timestamp(),'date':(start-timedelta(days=1)).date(),
               'open':100,'high':101,'low':99,'close':100}
        data=[prior]
        for i,values in enumerate(((98,99,97,98),(98,98.5,97.5,98),(98,98.5,97.4,97.8),(97.8,98,96.8,96.9))):
            o,h,l,c=values;data.append(dict(ts=(start+timedelta(minutes=5*i)).timestamp(),date=start.date(),open=o,high=h,low=l,close=c))
        return data

    def test_gap_break_and_bullish_symmetry(self):
        bars=self.bars();result=opening_trend(bars)
        self.assertEqual(result['direction'],'BUY_PE')
        self.assertEqual(result['trigger_level'],97)
        other=copy.deepcopy(bars)
        for b,a in zip(other,bars):
            for k,v in (('open','open'),('high','low'),('low','high'),('close','close')):b[k]=200-a[v]
        self.assertEqual(opening_trend(other)['direction'],'BUY_CE')

    def test_weak_candle_gap_without_break_and_missing_open_do_not_signal(self):
        for mutate in (lambda b:b[-1].update(open=96.95),lambda b:b[-1].update(close=97.5),
                       lambda b:b.pop(1),lambda b:b[0].update(close=98)):
            data=self.bars();mutate(data);self.assertIsNone(opening_trend(data))

    def test_broader_opening_drive_is_not_enabled_by_default(self):
        data=self.bars();data[1].update(open=100,high=101)
        self.assertIsNone(opening_trend(data))
        self.assertEqual(opening_trend(data,allow_drive=True)['setup'],'OPENING_DRIVE_BREAK')

    def stock_fixture(self):
        start=datetime(2026,10,22,9,15,tzinfo=IST)
        d={k:[] for k in ('timestamp','open','high','low','close','volume')}
        values=[(100,100.4,99.6,100,1000)]*75+[(97.6,98,90.5,92.1,1000),
            (92,93,90.6,93,1000),(93,94,91.5,92.9,1000),(92.8,93,90.2,90.3,4000)]
        for i,row in enumerate(values):
            dt=(start-timedelta(days=1)+timedelta(minutes=5*i)) if i<75 else start+timedelta(minutes=5*(i-75))
            for k,v in zip(d,(dt.timestamp(),*row)):d[k].append(v)
        return d,d['timestamp'][-1]+300

    def test_stock_exhaustion_cap_blocks_large_gap_even_with_opening_trend(self):
        data,now=self.stock_fixture()
        for enabled in (False,True):
            reasons=[]
            self.assertIsNone(evaluate_stock_setup(data,now,enable_opening_trend=enabled,reasons=reasons))
            self.assertIn('5.5% exhaustion cap',reasons[0])

    def test_nifty_first_gap_break_requires_oi_and_is_explicit(self):
        fixture=level_fixtures.LevelEntryTests();fixture.setUp()
        for k in fixture.data:fixture.data[k].pop()
        fixture.data['close'][:75]=[22800]*75
        fixture.analysis['spot']=23030
        now=fixture.now-300
        self.assertFalse(apply_nifty_level_entry(fixture.analysis,fixture.data,{},now)['allowed'])
        result=apply_nifty_level_entry(fixture.analysis,fixture.data,{},now,enable_opening_trend=True)
        self.assertTrue(result['allowed'],result)
        self.assertEqual(result['setup'],'OPENING_GAP_BREAK')
        fixture.rows[0]['change_over_5m']=0
        self.assertFalse(apply_nifty_level_entry(fixture.analysis,fixture.data,{},now,enable_opening_trend=True)['allowed'])

    def test_rupee_target_extension_requires_realistic_move_and_funded_size(self):
        kwargs=dict(target_pct=10,stop_pct=5,min_net_rr=1.2,rupee_target=2000,max_lots=3,rupee_max_risk=1000)
        self.assertIsNone(premium_plan(5.4,1825,stock.compute_fno_option_charges,**kwargs))
        plan=premium_plan(5.4,1825,stock.compute_fno_option_charges,**kwargs,extend_rupee_target=True)
        self.assertIsNone(plan)
        kwargs['rupee_max_risk']=2000
        plan=premium_plan(5.4,1825,stock.compute_fno_option_charges,**kwargs,extend_rupee_target=True)
        self.assertEqual(plan['lots'],2)
        self.assertLessEqual(plan['expected_net_risk'],2000)
        self.assertGreaterEqual(plan['expected_net_reward'],2000)
        self.assertEqual(plan['hard_target_price'],plan['rupee_target_price'])
        self.assertLessEqual(plan['hard_target_price'],5.4*1.18)
        self.assertGreater(plan['rupee_target_price'],5.4*1.1)

    def test_extended_target_cannot_override_invalid_input_or_risk_limit(self):
        for kwargs in ({'rupee_max_risk':100},{'target_pct':-1},{'target_pct':float('nan')}):
            args=dict(target_pct=10,stop_pct=5,min_net_rr=1.2,rupee_target=2000,max_lots=3,rupee_max_risk=1000,extend_rupee_target=True)
            args.update(kwargs)
            self.assertIsNone(premium_plan(5.4,1825,stock.compute_fno_option_charges,**args))

    def test_cash_target_selects_smallest_feasible_size_within_move_estimate(self):
        plan=premium_plan(10,500,lambda *_:40,target_pct=10,stop_pct=1,min_net_rr=1.2,
            rupee_target=2000,max_lots=3,rupee_max_risk=1000,extend_rupee_target=True,max_target_price=12.5)
        self.assertEqual(plan['lots'],3)
        self.assertLessEqual(plan['rupee_target_price'],11.8)
        self.assertLessEqual(plan['rupee_target_price'],12.5)
        self.assertLessEqual(plan['expected_net_risk'],1000)
        self.assertGreaterEqual(plan['expected_net_reward'],2000)

    def test_stock_loss_does_not_blacklist_a_fresh_setup_all_day(self):
        trade={'symbol':'TEST','timestamp':'2026-10-23 09:35:00','exit_timestamp':'2026-10-23 09:40:00','net_pnl':-500}
        epoch=lambda h,m:datetime(2026,10,23,h,m,tzinfo=IST).timestamp()
        self.assertIn('cooldown',stock_reentry_reason('TEST',[trade],epoch(9,50)))
        self.assertIsNone(stock_reentry_reason('TEST',[trade],epoch(9,55),epoch(9,50)))
        self.assertIn('new confirmation',stock_reentry_reason('TEST',[trade],epoch(9,55),epoch(9,35)))
        trade['net_pnl']=100
        self.assertIn('Profit',stock_reentry_reason('TEST',[trade],epoch(9,55),epoch(9,50)))

    def test_stock_reentry_never_reads_a_future_trade_result(self):
        trade={'symbol':'TEST','timestamp':'2026-10-23 09:35:00','exit_timestamp':'2026-10-23 10:00:00','net_pnl':object()}
        now=datetime(2026,10,23,9,55,tzinfo=IST).timestamp()
        self.assertIn('not closed',stock_reentry_reason('TEST',[trade],now))


if __name__=='__main__':unittest.main()


class ConsolidationBreakoutTests(unittest.TestCase):
    def sequence(self):
        rows=[(100,100.5,99.5,100,5000)]*3+[
            (102.1,102.6,101.9,102.3,1000),
            (102.3,102.6,102.0,102.4,900),
            (102.4,102.6,102.1,102.5,800),
            (102.5,103.15,102.45,103.1,1500)]
        return [dict(timestamp=i*300,open=o,high=h,low=l,close=c,volume=v)
                for i,(o,h,l,c,v) in enumerate(rows)]

    def test_quiet_base_expansion_and_bearish_symmetry(self):
        from stock_option_signals import confirmed_consolidation_breakout as detect
        bars=self.sequence()
        result=detect(bars,2,True)
        self.assertEqual(result['base_bars'],3)
        self.assertAlmostEqual(result['trigger_level'],102.6)
        self.assertGreater(result['breakout_volume_ratio'],1.2)
        mirror=copy.deepcopy(bars)
        for a,b in zip(bars,mirror):
            b.update(open=200-a['open'],high=200-a['low'],low=200-a['high'],close=200-a['close'])
        self.assertIsNotNone(detect(mirror,2,False))

    def test_chase_weak_volume_distribution_gap_and_nearby_obstacle_rejected(self):
        from stock_option_signals import confirmed_consolidation_breakout as detect
        changes=[lambda b:b[-1].update(volume=700),
                 lambda b:b[-1].update(close=104,high=104.1),
                 lambda b:b[-1].update(close=102.55),
                 lambda b:b[-2].update(volume=3000),
                 lambda b:b[-2].update(low=100),
                 lambda b:b[-1].update(timestamp=9999),
                 lambda b:b[0].update(high=103.5)]
        for change in changes:
            with self.subTest(change=change):
                bars=self.sequence();change(bars)
                self.assertIsNone(detect(bars,2,True))

    def test_full_signal_uses_base_volume_and_ignores_forming_bar(self):
        from unittest.mock import patch
        start=datetime(2026,10,28,9,15,tzinfo=IST)
        data={k:[] for k in ('timestamp','open','high','low','close','volume')}
        rows=[(99,100,98,99,1000)]*75
        bars=self.sequence()
        # A prior high remains ahead, but there is more than one ATR of room.
        bars.insert(3,dict(open=101,high=106,low=101,close=102,volume=4000))
        rows += [(b['open'],b['high'],b['low'],b['close'],b['volume']) for b in bars]
        for i,row in enumerate(rows):
            stamp=(start-timedelta(days=1)+timedelta(minutes=5*i) if i<75
                   else start+timedelta(minutes=5*(i-75)))
            for k,v in zip(data,(stamp.timestamp(),*row)):data[k].append(v)
        now=data['timestamp'][-1]+300
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[100,100.2]):
            signal=evaluate_stock_setup(data,now,require_breakout=True)
            self.assertIsNotNone(signal)
            self.assertEqual(signal['breakout_confirmation']['entry_path'],'CONSOLIDATION_BREAKOUT')
            self.assertEqual(signal['volume_confirmation'],'CONSOLIDATION_VOLUME_EXPANSION')
            blocked=evaluate_stock_setup(data,now,require_breakout=True,
                previous_session={'date':'2026-10-27','high':104,'low':94})
            self.assertIsNone(blocked)
            # Compare at the same decision time, before any future bar exists.
            mirrored=copy.deepcopy(data)
            for field,other in (('open','open'),('high','low'),('low','high'),('close','close')):
                mirrored[field]=[200-v for v in data[other]]
            with patch('stock_option_signals._ema',return_value=[100,99.8]):
                bearish=evaluate_stock_setup(mirrored,now,require_breakout=True)
            self.assertIsNotNone(bearish)
            self.assertEqual(bearish['breakout_confirmation']['entry_path'],'CONSOLIDATION_BREAKOUT')
            self.assertEqual(bearish['opt_type'],'PE')
            for k,v in zip(data,(now,103,110,100,109,100000)):data[k].append(v)
            self.assertEqual(evaluate_stock_setup(data,now+30,require_breakout=True),signal)


class PullbackRetestTests(unittest.TestCase):
    def sequence(self):
        values=[(99.7,100,99.5,99.8,1000)]*3+[(99.9,101.1,99.8,101,3000),
                (101,101.05,100.1,100.3,1000),(100.3,101.25,100.2,101.2,2000)]
        return [dict(timestamp=i*300,open=o,high=h,low=l,close=c,volume=v)
                for i,(o,h,l,c,v) in enumerate(values)]

    def detect(self,bars,bullish=True):
        from stock_option_signals import confirmed_pullback_retest
        return confirmed_pullback_retest(bars,100,100,100,100,2,bullish)

    def test_closed_retest_and_bearish_symmetry(self):
        bars=self.sequence();result=self.detect(bars)
        self.assertEqual(result['setup'],'PULLBACK_RETEST')
        self.assertEqual(result['trigger_level'],100)
        self.assertEqual(result['pullback_bars'],1)
        mirror=copy.deepcopy(bars)
        for a,b in zip(bars,mirror):
            b.update(open=200-a['open'],high=200-a['low'],low=200-a['high'],close=200-a['close'])
        self.assertIsNotNone(self.detect(mirror,False))

    def test_distribution_level_failure_weak_rebound_chase_and_missing_bar_rejected(self):
        mutations=[lambda b:b[-2].update(volume=2900),
                   lambda b:b[-2].update(close=99.7,low=99.5),
                   lambda b:b[-2].update(low=99.4),
                   lambda b:b[-1].update(volume=1100),
                   lambda b:b[-1].update(close=101.0),
                   lambda b:b[-1].update(close=102,high=102.1),
                   lambda b:b[-1].update(timestamp=2000),
                   lambda b:b[3].update(volume=1000),
                   lambda b:b[-2].update(volume=0)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bars=self.sequence();mutate(bars);self.assertIsNone(self.detect(bars))


    def test_full_setup_accepts_retest_without_new_session_high_and_ignores_forming_bar(self):
        from unittest.mock import patch
        start=datetime(2026,10,28,9,15,tzinfo=IST)
        data={k:[] for k in ('timestamp','open','high','low','close','volume')}
        rows=[(99,100,98,99,1000)]*75
        rows += [(b['open'],b['high'],b['low'],b['close'],b['volume']) for b in self.sequence()]
        for i,row in enumerate(rows):
            stamp=((start-timedelta(days=1)+timedelta(minutes=5*i)) if i<75
                   else start+timedelta(minutes=5*(i-75)))
            for k,v in zip(data,(stamp.timestamp(),*row)):data[k].append(v)
        now=data['timestamp'][-1]+300
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[99,99.8]):
            result=evaluate_stock_setup(data,now,require_breakout=True)
            self.assertIsNotNone(result)
            self.assertEqual(result['breakout_confirmation']['entry_path'],'PULLBACK_RETEST')
            self.assertEqual(result['breakout_confirmation']['trigger_level'],100)
            for k,v in zip(data,(now,105,107,100,106,100000)):data[k].append(v)
            self.assertEqual(evaluate_stock_setup(data,now+60,require_breakout=True),result)

import copy
import unittest
from datetime import datetime,timedelta

from market_regime import opening_trend
from stock_option_signals import IST
from stock_option_signals import evaluate_stock_setup
from stock_option_policy import premium_plan, stock_reentry_reason
import dhan_dynamic_fno_stock_bot as stock
import test_nifty_level_entry as level_fixtures
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

    def test_stock_confirmed_gap_break_can_pass_ema_distance_without_bypassing_volume(self):
        d,now=self.stock_fixture()
        reasons=[]
        self.assertIsNone(evaluate_stock_setup(d,now,reasons=reasons))
        self.assertIn('EMA distance',reasons[0])
        signal=evaluate_stock_setup(d,now,enable_opening_trend=True)
        self.assertEqual(signal['opt_type'],'PE')
        self.assertEqual(signal['scanner'],'OPENING_GAP_BREAK_PE')
        expected=copy.deepcopy(signal)
        for k,v in zip(d,(now,200,250,100,200,1000000)):d[k].append(v)
        self.assertEqual(evaluate_stock_setup(d,now,enable_opening_trend=True),expected)
        for values in d.values():values.pop()
        d['volume'][-1]=1000
        self.assertIsNone(evaluate_stock_setup(d,now,enable_opening_trend=True))

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

    def test_rupee_target_extension_uses_one_lot_without_raising_risk(self):
        kwargs=dict(target_pct=10,stop_pct=5,min_net_rr=1.2,rupee_target=2000,max_lots=3,rupee_max_risk=1000)
        self.assertIsNone(premium_plan(5.4,1825,stock.compute_fno_option_charges,**kwargs))
        plan=premium_plan(5.4,1825,stock.compute_fno_option_charges,**kwargs,extend_rupee_target=True)
        self.assertEqual(plan['lots'],1)
        self.assertLessEqual(plan['expected_net_risk'],1000)
        self.assertGreaterEqual(plan['expected_net_reward'],2000)
        self.assertEqual(plan['hard_target_price'],plan['rupee_target_price'])
        self.assertLessEqual(plan['hard_target_price'],10.8)
        self.assertGreater(plan['rupee_target_price'],5.4*1.1)

    def test_extended_target_cannot_override_invalid_input_or_risk_limit(self):
        for kwargs in ({'rupee_max_risk':100},{'target_pct':-1},{'target_pct':float('nan')}):
            args=dict(target_pct=10,stop_pct=5,min_net_rr=1.2,rupee_target=2000,max_lots=3,rupee_max_risk=1000,extend_rupee_target=True)
            args.update(kwargs)
            self.assertIsNone(premium_plan(5.4,1825,stock.compute_fno_option_charges,**args))

    def test_cash_target_selects_smallest_feasible_size_within_move_estimate(self):
        plan=premium_plan(10,500,lambda *_:40,target_pct=10,stop_pct=1,min_net_rr=1.2,
            rupee_target=2000,max_lots=3,rupee_max_risk=1000,extend_rupee_target=True,max_target_price=12.5)
        self.assertEqual(plan['lots'],2)
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

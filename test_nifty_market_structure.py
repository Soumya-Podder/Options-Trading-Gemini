from nifty_oi import NiftyOITracker
from nifty_market_structure import apply_nifty_level_entry
import copy
import logging
import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import dhan_bot_engine as bot
from nifty_market_structure import IST, assess_nifty_structure


class MarketStructureTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,22,9,40,tzinfo=IST).timestamp()
        self.data={k:[] for k in ("timestamp","open","high","low","close")}
        prior=datetime(2026,10,21,9,15,tzinfo=IST)
        for i in range(75):
            self.add(prior+timedelta(minutes=5*i),23000,23100,22800,23000)
        today=datetime(2026,10,22,9,15,tzinfo=IST)
        for i in range(3):
            self.add(today+timedelta(minutes=5*i),23000,23010,22990,23005)
        self.add(today+timedelta(minutes=15),23008,23040,23005,23030)
        self.add(today+timedelta(minutes=20),23028,23050,23020,23045)
        self.chain={"22900.000000":{"pe":{"oi":10000000,"previous_oi":9000000}},
                    "23100.000000":{"ce":{"oi":12000000,"previous_oi":13000000}}}

    def add(self,dt,o,h,l,c):
        for key,value in zip(self.data,(dt.timestamp(),o,h,l,c)):
            self.data[key].append(value)

    def assess(self,side="BUY_CE",spot=23045):
        return assess_nifty_structure(self.data,self.chain,self.now,spot,side)

    def test_bullish_breakout_marks_day_range_and_oi(self):
        result=self.assess()
        self.assertTrue(result["allowed"],result)
        self.assertEqual(result["setup"],"BREAKOUT_HOLD")
        self.assertEqual(result["levels"]["day_high"],23050)
        self.assertEqual(result["levels"]["opening_high"],23010)
        self.assertEqual(result["call_oi_wall"]["change_from_previous_close"],-1000000)
        self.assertGreaterEqual(result["structure_rr"],1.5)

    def test_bearish_breakdown_is_symmetric(self):
        original=copy.deepcopy(self.data)
        for field,other in (("open","open"),("high","low"),("low","high"),("close","close")):
            self.data[field]=[46000-v for v in original[other]]
        self.assertTrue(self.assess("BUY_PE",22955)["allowed"])

    def test_calls_rejected_when_resistance_is_too_close(self):
        self.chain["23055"]={"ce":{"oi":20000000}}
        result=self.assess()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["room_points"],10)

    def test_middle_of_range_does_not_create_entry(self):
        for k,v in (("open",23000),("high",23005),("low",22998),("close",23004)):
            self.data[k][-1]=v
        result=self.assess(spot=23004)
        self.assertFalse(result["allowed"])
        self.assertIn("separate closed-candle",result["reason"])

    def test_future_candles_do_not_change_decision(self):
        expected=self.assess()
        self.add(datetime(2026,10,22,9,40,tzinfo=IST),24000,25000,22000,24500)
        self.assertEqual(self.assess(),expected)

    def test_confirmation_window_and_strong_day_closed_range(self):
        self.assertTrue(assess_nifty_structure(self.data,self.chain,self.now+599,23045,"BUY_CE")["ready"])
        self.assertFalse(assess_nifty_structure(self.data,self.chain,self.now+600,23045,"BUY_CE")["ready"])
        self.assertFalse(assess_nifty_structure(self.data,self.chain,self.now,23045,"BUY_CE",confirm_window_sec=901)["ready"])
        self.data['open'][-1]=23032
        quiet=self.assess()
        self.assertEqual(quiet['reason'],'Weak follow-through candle body or closing location')
        self.data['low'][-5]=22960
        strong=self.assess()
        self.assertEqual(strong['day_range_points'],90)
        self.assertEqual(strong['min_body_fraction'],0.4)
        self.assertEqual(strong['min_close_fraction'],0.6)
        self.assertNotEqual(strong['reason'],'Weak follow-through candle body or closing location')
        # A future candle cannot turn a quiet day into a strong-day exception.
        self.data['low'][-5]=22990
        self.add(datetime.fromtimestamp(self.now,IST),23045,23200,22800,23050)
        self.assertEqual(self.assess()['min_body_fraction'],0.5)

    def test_stale_missing_oi_and_missing_prior_session_fail_closed(self):
        self.assertFalse(assess_nifty_structure(self.data,self.chain,self.now+600,23030,"BUY_CE")["ready"])
        self.assertFalse(assess_nifty_structure(self.data,{},self.now,23030,"BUY_CE")["ready"])
        for key in self.data:
            self.data[key]=self.data[key][75:]
        self.assertFalse(self.assess()["ready"])

    def test_first_break_cannot_enter_before_follow_through_closes(self):
        for values in self.data.values():
            values.pop()
        result=assess_nifty_structure(self.data,self.chain,self.now-300,23030,"BUY_CE")
        self.assertFalse(result["allowed"])

    def test_confirmed_retest_can_enter(self):
        self.data["low"][-1]=23009
        self.data["open"][-1]=23015
        result=self.assess()
        self.assertTrue(result["allowed"],result)
        self.assertEqual(result["setup"],"RETEST_CONFIRMED")

    def test_weak_follow_through_or_chasing_is_rejected(self):
        self.assertFalse(self.assess(spot=23080)["allowed"])
        self.data["open"][-1]=23043
        self.assertFalse(self.assess()["allowed"])

    def test_missing_current_candle_and_nonfinite_price_fail_closed(self):
        self.assertFalse(self.assess(spot=float("nan"))["allowed"])
        for key in self.data:
            del self.data[key][-2]
        self.assertFalse(self.assess()["ready"])

    def test_brain_requires_structure_only_for_nifty(self):
        with patch.object(bot.SelfLearningBrain,"_load_json",side_effect=lambda path,default:default), \
             patch.object(bot,"atomic_write_json"):
            brain=bot.SelfLearningBrain(logging.getLogger("test"))
            view={"index":"NIFTY","exceeds_50_lakhs":True,"recommended_dir":"BUY_CE"}
            self.assertEqual(brain.select_action(view,0),("HOLD",None))

    def test_index_history_request_is_actual_index_ohlc_and_live_cache_expires(self):
        client=bot.DhanV2RestClient("test","test",logging.getLogger("test"))
        client.session=MagicMock()
        client.session.post.return_value.status_code=200
        client.session.post.return_value.json.return_value=self.data
        with patch.object(client,"_wait_hist_rate_limit"), patch.object(bot.time,"time",return_value=100):
            self.assertEqual(client.fetch_nifty_structure_candles("2026-10-12 00:00:00","2026-10-22 09:35:00"),self.data)
            client.fetch_nifty_structure_candles("2026-10-12 00:00:00","2026-10-22 09:35:03")
        self.assertEqual(client.session.post.call_count,1)
        payload=client.session.post.call_args.kwargs["json"]
        self.assertEqual((payload["securityId"],payload["exchangeSegment"],payload["instrument"]),("13","IDX_I","INDEX"))
        client.session.post.return_value.status_code=401
        with patch.object(client,"_wait_hist_rate_limit"),patch.object(bot.time,"time",return_value=131):
            self.assertIsNone(client.fetch_nifty_structure_candles("2026-10-12 00:00:00","2026-10-22 09:35:31"))

    def test_legacy_option_cache_is_trimmed_to_exclusive_end(self):
        import hashlib
        key="NIFTY_ATM_CALL_5m_2026-10-21_2026-10-22"
        suffix=hashlib.md5(key.encode()).hexdigest()[:12]
        start=datetime(2026,10,21,9,15).timestamp()
        outside=datetime(2026,10,22,9,15).timestamp()
        data={"timestamp":[start,outside],"close":[100,200],"oi":[1,2],"volume":[]}
        client=bot.DhanV2RestClient("test","test",logging.getLogger("test"))
        client.session=MagicMock()
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,"CACHE_DIR",folder):
            Path(folder,f"{key}_{suffix}.json").write_text(json.dumps(data))
            result=client.fetch_rolling_option_window("NIFTY","ATM","CALL","2026-10-21","2026-10-22")
        self.assertEqual(result,{"timestamp":[start],"close":[100],"oi":[1],"volume":[]})
        client.session.post.assert_not_called()

    def test_sensex_decision_is_unchanged_by_nifty_gate(self):
        with patch.object(bot.SelfLearningBrain,"_load_json",side_effect=lambda path,default:default), \
             patch.object(bot,"atomic_write_json"):
            brain=bot.SelfLearningBrain(logging.getLogger("test"))
            view={"index":"SENSEX","exceeds_50_lakhs":True,"recommended_dir":"BUY_CE",
                  "atm_ce_ltp":150,"wall_tag":"PUT_SIDE_GT_50L","ce_quadrant":"SHORT_COVERING",
                  "pe_quadrant":"SHORT_BUILDUP","market_controller":"BUYERS","market_dominance":"BULLS",
                  "state_vec":[0]*10}
            self.assertEqual(brain.select_action(view,0)[0],"BUY_CE")

    def test_nifty_rollover_does_not_invent_oi_or_premium_impulse(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        args=dict(idx_name="NIFTY",spot=23000,call_strikes=[23050,23100,23150],
                  put_strikes=[23000,22950,22900],ce_oi=15000000,pe_oi=5000000,
                  ce_prem=300,pe_prem=300,ce_iv=20,pe_iv=20,atm_ce_ltp=100,atm_pe_ltp=100)
        analyzer.evaluate_snapshot(**args)
        args.update(spot=23010,call_strikes=[23100,23150,23200],ce_prem=200,pe_prem=400,
                    ce_oi=25000000,pe_oi=2000000)
        result=analyzer.evaluate_snapshot(**args)
        self.assertEqual(result["recommended_dir"],"HOLD")
        self.assertEqual(result["data_health"],"ATM_ROLLOVER_WAIT")


def chain(oi=10000,price=100):
    return {str(k):{side:{'oi':oi,'last_price':price,'previous_oi':8000,
                         'security_id':f'{k}_{side}'} for side in ('ce','pe')} for k in range(21800,22401,50)}


class OITests(unittest.TestCase):
    def test_wall_direction(self):
        analysis={'recommended_dir':'BUY_CE','oi_analysis':{'ready':True,'room_to_resistance':24}}
        self.assertFalse(bot.SelfLearningBrain.has_wall_room(analysis))
        analysis['oi_analysis']['room_to_resistance']=25
        self.assertTrue(bot.SelfLearningBrain.has_wall_room(analysis))

    def setUp(self):
        self.tracker=NiftyOITracker()
        self.t=datetime(2026,10,23,10,tzinfo=IST).timestamp()

    def test_nearest_building_wall_uses_oi_change_even_when_premium_rises(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        _,info=self.tracker.assess(chain(11000,110),22025,self.t+300,'2026-10-27')
        self.assertEqual(info['room_to_resistance'],25)
        self.assertEqual(info['room_to_support'],25)
        self.assertEqual(info['building_resistance']['strike'],22050)
        self.assertEqual(info['building_support']['strike'],22000)

    def test_startup_requires_actual_five_minutes(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(11000),22025,self.t+299,'2026-10-27')
        self.assertIsNone(ref);self.assertFalse(info['ready'])

    def test_previous_day_and_intraday_changes_are_separate(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(11000),22025,self.t+300,'2026-10-27')
        row=next(r for r in info['nearby_strikes'] if r['strike']==22000 and r['side']=='pe')
        self.assertEqual(ref['ce_oi'],30000)
        self.assertEqual(row['change_from_previous_day'],3000)
        self.assertEqual(row['change_over_5m'],1000)

    def test_atm_shift_compares_same_new_basket(self):
        first=chain()
        for k,sides in first.items():
            for node in sides.values():node['oi']=float(k)
        self.tracker.assess(first,22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22075,self.t+300,'2026-10-27')
        self.assertTrue(info['ready'])
        self.assertEqual(ref['ce_oi'],22100+22150+22200)
        self.assertEqual(ref['atm_ce_strike'],22100)

    def test_expiry_change_resets_reference(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22025,self.t+300,'2026-11-03')
        self.assertIsNone(ref)

    def test_stale_reference_is_rejected(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22025,self.t+391,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_security_id_change_is_rejected(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        changed=chain();changed['22050']['ce']['security_id']='OTHER_CONTRACT'
        ref,info=self.tracker.assess(changed,22025,self.t+300,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_missing_current_node_prevents_signal(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        changed=chain();del changed['22050']['ce']
        ref,info=self.tracker.assess(changed,22025,self.t+300,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_missing_previous_oi_does_not_invent_daily_change(self):
        changed=chain();del changed['22000']['pe']['previous_oi']
        ref,info=self.tracker.assess(changed,22025,self.t,'2026-10-27')
        row=next(r for r in info['nearby_strikes'] if r['strike']==22000 and r['side']=='pe')
        self.assertIsNone(row['change_from_previous_day'])

    def test_unchanged_oi_is_not_short_covering(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        with patch('dhan_bot_engine.time.time',return_value=self.t):
            analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':chain()},'2026-10-27')
        with patch('dhan_bot_engine.time.time',return_value=self.t+300):
            result=analyzer.evaluate_chain('NIFTY',{'last_price':22030,'oc':chain(price=105)},'2026-10-27')
        self.assertEqual(result['ce_quadrant'],'NO_OI_CHANGE')
        self.assertEqual(result['pe_quadrant'],'NO_OI_CHANGE')
        self.assertEqual(result['recommended_dir'],'HOLD')

    def test_real_oi_changes_produce_direction_without_fake_clock(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        first=chain();second=chain()
        for sides in first.values():
            sides['ce']['oi']=10000000;sides['pe']['oi']=1000000
        for sides in second.values():
            sides['ce'].update(oi=11000000,last_price=90)
            sides['pe'].update(oi=2000000,last_price=110)
        analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':first},'2026-10-27',snapshot_time=self.t)
        result=analyzer.evaluate_chain('NIFTY',{'last_price':22010,'oc':second},'2026-10-27',snapshot_time=self.t+300)
        self.assertTrue(result['oi_analysis']['ready'])
        self.assertEqual(result['ce_oi_change_5m'],3000000)
        self.assertEqual(result['pe_oi_change_5m'],3000000)
        self.assertEqual(result['recommended_dir'],'BUY_PE')

    def test_nifty_only_session_never_polls_sensex(self):
        clock=MagicMock()
        clock.now.return_value=datetime(2026,10,23,10)
        client=MagicMock();client.fetch_nearest_expiry.return_value='2026-10-27'
        client.fetch_option_chain.side_effect=[None,KeyboardInterrupt]
        with patch.object(bot,'datetime',clock),patch.object(bot,'load_dotenv_file',return_value={}),\
             patch.object(bot,'DhanV2RestClient',return_value=client),patch.object(bot,'SelfLearningBrain') as brain,\
             patch.object(bot,'TelegramNotifier'):
            brain._load_json.return_value={'trades':[]}
            with self.assertRaises(KeyboardInterrupt):
                bot.run_intraday_market_session(False,logging.getLogger('test'),['NIFTY'])
        client.fetch_nearest_expiry.assert_called_once_with(13,'IDX_I')
        self.assertTrue(all(call.args[0]==13 for call in client.fetch_option_chain.call_args_list))


class LevelEntryTests(unittest.TestCase):
    def setUp(self):
        f=MarketStructureTests();f.setUp()
        self.data,self.now=f.data,f.now
        self.rows=[{'strike':23010.,'side':'pe','oi':10500000.,
                    'change_from_previous_day':1500000.,'change_over_5m':500000.,
                    'premium_change_over_5m':-2.},
                   {'strike':23100.,'side':'ce','oi':12000000.,
                    'change_from_previous_day':-1000000.,'change_over_5m':0.,
                    'premium_change_over_5m':0.}]
        self.analysis={'index':'NIFTY','spot':23045,'recommended_dir':'HOLD',
                       'exceeds_50_lakhs':False,'d_spot_5m':1,
                       'atm_ce_ltp':150,'atm_pe_ltp':150,
                       'wall_tag':'SUB_50L_DIFF','ce_quadrant':'NO_OI_CHANGE',
                       'pe_quadrant':'SHORT_BUILDUP','market_controller':'CONTESTED_CHOP',
                       'market_dominance':'BALANCED_CHOP','state_vec':[0]*10,
                       'oi_analysis':{'ready':True,'reference_age_seconds':300,
                                      'nearby_strikes':self.rows}}

    def run_entry(self):
        return apply_nifty_level_entry(self.analysis,self.data,{},self.now)

    def test_candle_and_exact_strike_oi_generate_direction_without_basket_impulse(self):
        result=self.run_entry()
        self.assertTrue(result['allowed'],result)
        self.assertEqual(self.analysis['recommended_dir'],'BUY_CE')
        self.assertEqual(result['trigger_level'],23010)
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            # Recovery/lunch filters must not reintroduce the old momentum gate.
            self.assertEqual(brain.select_action(self.analysis,1,bot.dtime(12,15))[0],'BUY_CE')
            self.assertEqual(self.analysis['entry_decision']['action'],'BUY_CE')

    def test_daily_buildup_alone_cannot_confirm_entry(self):
        self.rows[0]['change_over_5m']=0
        self.assertFalse(self.run_entry()['allowed'])
        self.assertEqual(self.analysis['recommended_dir'],'HOLD')

    def test_oi_at_other_strike_cannot_confirm_trigger(self):
        self.rows[0]['change_over_5m']=0
        other=copy.deepcopy(self.rows[0]);other.update(strike=22900.,oi=1000.,change_over_5m=500.,change_from_previous_day=0.)
        self.rows.append(other)
        self.assertFalse(self.run_entry()['allowed'])

    def test_missing_reference_or_invalid_strike_change_fails_closed(self):
        self.analysis['oi_analysis']['ready']=False
        self.assertFalse(self.run_entry()['allowed'])
        self.analysis['oi_analysis']['ready']=True
        self.rows[0]['change_over_5m']=float('nan')
        self.assertFalse(self.run_entry()['allowed'])

    def test_opposing_premium_change_is_not_writer_confirmation(self):
        self.rows[0]['premium_change_over_5m']=2
        self.assertFalse(self.run_entry()['allowed'])

    def test_future_or_weak_candle_cannot_confirm(self):
        self.data['open'][-1]=23044
        self.assertFalse(self.run_entry()['allowed'])

    def test_bearish_confirmation_is_symmetric(self):
        original=copy.deepcopy(self.data)
        for field,other in (('open','open'),('high','low'),('low','high'),('close','close')):
            self.data[field]=[46000-v for v in original[other]]
        for row in self.rows:
            row['strike']=46000-row['strike']
            row['side']='ce' if row['side']=='pe' else 'pe'
        self.analysis['spot']=22955
        self.assertTrue(self.run_entry()['allowed'])
        self.assertEqual(self.analysis['recommended_dir'],'BUY_PE')

    def test_buildup_wall_is_separate_from_largest_absolute_oi(self):
        from nifty_market_structure import assess_nifty_structure
        oc={'22900':{'pe':{'oi':10000,'previous_oi':12000}},
            '23010':{'pe':{'oi':8000,'previous_oi':1000}},
            '23100':{'ce':{'oi':12000,'previous_oi':13000}}}
        result=assess_nifty_structure(self.data,oc,self.now,23045,'BUY_CE')
        self.assertEqual(result['put_oi_wall']['strike'],22900)
        self.assertEqual(result['put_oi_buildup']['strike'],23010)
        self.assertIsNone(result['call_oi_buildup'])
        self.assertIn(23010,result['entry_levels'])

    def test_daily_buildup_can_mark_wall_even_when_five_minute_oi_falls(self):
        tracker=NiftyOITracker()
        tracker.assess(chain(11000),22025,self.now,'expiry')
        _,info=tracker.assess(chain(10000),22025,self.now+300,'expiry')
        self.assertEqual(info['building_support']['strike'],22000)
        self.assertEqual(info['building_support']['change_over_5m'],-1000)
        self.assertEqual(info['building_support']['change_from_previous_day'],2000)

    def test_negative_daily_and_intraday_changes_do_not_mark_wall(self):
        tracker=NiftyOITracker()
        tracker.assess(chain(7000),22025,self.now,'expiry')
        _,info=tracker.assess(chain(6000),22025,self.now+300,'expiry')
        self.assertIsNone(info['building_support'])
        self.assertIsNone(info['building_resistance'])

    def test_missing_previous_day_oi_does_not_invent_buildup_wall(self):
        from nifty_market_structure import assess_nifty_structure
        oc={'23010':{'pe':{'oi':10000,'previous_oi':None}},
            '23100':{'ce':{'oi':12000}}}
        result=assess_nifty_structure(self.data,oc,self.now,23045,'BUY_CE')
        self.assertTrue(result['ready'])
        self.assertIsNone(result['put_oi_buildup'])
        self.assertIsNone(result['call_oi_buildup'])

    def test_call_unwinding_at_trigger_also_confirms_bullish_reaction(self):
        self.rows[0].update(side='ce',change_over_5m=-500000.,premium_change_over_5m=2.)
        self.rows[1]['side']='pe'
        self.assertTrue(self.run_entry()['allowed'])
        self.assertEqual(self.analysis['recommended_dir'],'BUY_CE')

    def test_nearby_confirmed_level_not_hidden_by_distant_maximum_oi(self):
        self.rows[0]['change_from_previous_day']=0
        far=copy.deepcopy(self.rows[0])
        far.update(strike=22900.,oi=30000000.,change_from_previous_day=5000000.,change_over_5m=0.)
        self.rows.append(far)
        self.assertFalse(self.run_entry()['allowed']) # Losing replay experiment stays disabled.
        result=apply_nifty_level_entry(self.analysis,self.data,{},self.now,include_nearest=True)
        self.assertTrue(result['allowed'],result)
        self.assertEqual(result['put_oi_wall']['strike'],22900)
        self.assertEqual(result['put_oi_buildup']['strike'],22900)
        self.assertEqual(result['trigger_level'],23010)
        self.assertEqual(result['nearest_put_oi_level']['strike'],23010)

    def test_direction_specific_rejection_reasons_are_retained(self):
        self.rows[0]['change_over_5m']=0
        result=self.run_entry()
        self.assertFalse(result['allowed'])
        self.assertEqual(set(result['direction_checks']),{'BUY_CE','BUY_PE'})
        self.assertEqual(result['direction_checks']['BUY_CE']['oi_confirmed_levels'],[])

    def test_valid_structure_blocked_by_wall_room_has_specific_decision_reason(self):
        self.assertTrue(self.run_entry()['allowed'])
        self.analysis['oi_analysis']['room_to_resistance']=24
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(self.analysis,0),('HOLD',None))
        self.assertEqual(self.analysis['entry_decision']['reason'],'Insufficient room to nearest building OI wall')

if __name__=="__main__":
    unittest.main()

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
from nifty_market_structure import IST, assess_nifty_structure, assess_nifty_opening_reclaim


class OpeningReclaimTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,26,9,16,tzinfo=IST).timestamp()
        def rows(values):
            return [dict(zip(('open','high','low','close','open_interest'),v),timestamp=self.start+60*i)
                    for i,v in enumerate(values)]
        self.index=rows([(22481.2,22517.15,22476.75,22509.65,0),
                         (22510.5,22511.15,22458.3,22463.25,0)])
        self.ce={'side':'CE','strike':22600,'expiry':'2026-10-27','security_id':'call',
                 'candles':rows([(99.15,117.8,98,116.85,6942520),
                                 (115.8,115.8,94.4,95.75,7601880)])}
        self.pe={'side':'PE','strike':22600,'expiry':'2026-10-27','security_id':'51337',
                 'candles':rows([(171.3,173,145.6,147.8,6634875),
                                 (146.15,179.05,146.15,174.95,7134400)])}

    def assess(self, seconds=120):
        return assess_nifty_opening_reclaim(self.index,self.ce,self.pe,self.start+seconds,22463.6)

    def test_recorded_opening_reclaim_detected_only_after_close(self):
        self.assertEqual(self.assess(60)['candidate_dir'],'HOLD')
        result=self.assess()
        self.assertEqual(result['candidate_dir'],'BUY_PE')
        self.assertEqual(result['writer_oi_change'],659360)
        self.assertFalse(result['qualified_for_execution'])
        self.assertFalse(result['execution_enabled'])

    def test_contract_mismatch_and_missing_or_unchanged_oi_rejected(self):
        for edit in (lambda: self.pe.update(strike=22550),
                     lambda: self.pe.update(expiry='2026-11-03'),
                     lambda: self.pe['candles'][1].pop('open_interest'),
                     lambda: self.pe['candles'][1].update(open_interest=6634875)):
            self.setUp()
            edit()
            self.assertEqual(self.assess()['candidate_dir'],'HOLD')

    def test_gaps_stale_and_unconfirmed_option_reclaim_rejected(self):
        self.index[1]['timestamp']+=60
        self.assertEqual(self.assess(180)['candidate_dir'],'HOLD')
        self.setUp()
        self.assertEqual(self.assess(241)['candidate_dir'],'HOLD')
        self.pe['candles'][1]['close']=170
        self.assertEqual(self.assess()['candidate_dir'],'HOLD')

    def test_unclosed_future_observation_does_not_change_signal(self):
        self.index.append({'timestamp':self.start+120,'open':22463,'high':22600,
                           'low':22400,'close':22590})
        self.assertEqual(self.assess()['candidate_dir'],'BUY_PE')


class MarketStructureTests(unittest.TestCase):
    def test_v2_accepts_closed_first_break_without_waiting_for_another_bar(self):
        for key,value in {'open':23028,'high':23050,'low':23025,'close':23045}.items():
            self.data[key][-1]=value
        # Use an explicitly mapped pre-existing level.
        self.chain['23035']={'pe':{'oi':15000000,'previous_oi':14000000}}
        result=assess_nifty_structure(self.data,self.chain,self.now,23045,'BUY_CE',
                                     trigger_levels={23035},enable_breakout_entries=True)
        self.assertTrue(result['allowed'],result)
        self.assertEqual(result['setup'],'BREAKOUT_CONFIRMED')
        self.assertEqual(result['invalidation_level'],23035)

    def test_v2_rejects_price_that_has_lost_the_trigger(self):
        result=assess_nifty_structure(self.data,self.chain,self.now,23005,'BUY_CE',
                                     enable_breakout_entries=True)
        self.assertFalse(result['allowed'])

    def test_nifty_structure_is_required_even_with_aligned_momentum(self):
        base={'index':'NIFTY','recommended_dir':'HOLD','oi_momentum_dir':'BUY_PE',
              'entry_model':'LEVEL_CANDLE_AND_STRIKE_OI_V1','exceeds_50_lakhs':True,
              'd_spot_5m':-25,'diff_ce_minus_pe_lakhs':80,'ce_quadrant':'SHORT_BUILDUP',
              'pe_quadrant':'LONG_BUILDUP','atm_ce_ltp':100,'atm_pe_ltp':100,
              'wall_tag':'CALL_SIDE_GT_50L','market_controller':'BUYERS_AND_CALL_WRITERS_ALIGNED',
              'market_dominance':'BEARS_DOMINATING','state_vec':[0]*10,
              'spot':22000,'session_vwap':22010,'oi_analysis':{'ready':True,'room_to_support':100}}
        for imbalance,reason,ready,expected in (
                (80,'Wait for retest',True,'HOLD'),
                (60,'Wait for retest',True,'HOLD'),
                (60,'Price structure unavailable',True,'HOLD'),
                (80,'Price structure unavailable',False,'HOLD')):
            with self.subTest(imbalance=imbalance,reason=reason,ready=ready),\
                 patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),\
                 patch.object(bot,'atomic_write_json'):
                brain=bot.SelfLearningBrain(logging.getLogger('test'))
                view=copy.deepcopy(base);view['diff_ce_minus_pe_lakhs']=imbalance
                view['oi_analysis']['ready']=ready
                view['market_structure']={'allowed':False,'ready':reason!='Price structure unavailable','reason':reason}
                self.assertEqual(brain.select_action(view,0)[0],expected)

    def test_rollover_uses_verified_contract_changes_and_large_impulse(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        args=dict(idx_name='NIFTY',spot=23000,call_strikes=[23050,23100,23150],
                  put_strikes=[23000,22950,22900],ce_oi=15000000,pe_oi=5000000,
                  ce_prem=300,pe_prem=300,ce_iv=20,pe_iv=20,atm_ce_ltp=100,atm_pe_ltp=100)
        analyzer.evaluate_snapshot(**args)
        args.update(spot=22980,call_strikes=[23000,23050,23100],put_strikes=[22950,22900,22850],
                    ce_oi=18000000,pe_oi=6000000,ce_prem=200,pe_prem=400,
                    require_reference=True,reference_snapshot={'ts':self.now-300,'spot':23000,'atm_ce_strike':23000,
                                 'ce_oi':15000000,'pe_oi':5000000,'ce_prem':215,'pe_prem':380})
        result=analyzer.evaluate_snapshot(**args)
        self.assertEqual(result['recommended_dir'],'BUY_PE')
        self.assertEqual(result['ce_oi_change_5m'],3000000)
        self.assertEqual(result['ce_premium_change_5m'],-15)
        self.assertEqual(result['data_health'],'LIVE_CLEAN')

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

    def test_empty_or_partial_rolling_cache_is_retried_with_complete_ohlc(self):
        import hashlib
        key="NIFTY_ATM+3_CALL_5m_2026-10-21_2026-10-22"
        suffix=hashlib.md5(key.encode()).hexdigest()[:12]
        stamp=datetime(2026,10,21,9,15,tzinfo=IST).timestamp()
        full={"timestamp":[stamp],**{k:[100] for k in ('open','high','low','close','oi','iv','strike','spot')}}
        for old in ({'empty':True,'timestamp':[]},{'timestamp':[stamp],'close':[100]}):
            with self.subTest(cache=old),tempfile.TemporaryDirectory() as folder,patch.object(bot,'CACHE_DIR',folder):
                path=Path(folder,f'{key}_{suffix}.json')
                path.write_text(json.dumps(old))
                client=bot.DhanV2RestClient('test','test',logging.getLogger('test'))
                client.session=MagicMock()
                client.session.post.return_value.status_code=200
                client.session.post.return_value.json.return_value={'data':{'ce':full}}
                with patch.object(client,'_wait_hist_rate_limit'):
                    result=client.fetch_rolling_option_window('NIFTY','ATM+3','CALL','2026-10-21','2026-10-22',require_ohlc=True)
                self.assertEqual(result,full)
                self.assertEqual(json.loads(path.read_text()),full)
                self.assertIn('open',client.session.post.call_args.kwargs['json']['requiredData'])

    def test_nested_index_history_response_is_accepted(self):
        client=bot.DhanV2RestClient('test','test',logging.getLogger('test'))
        client.session=MagicMock()
        client.session.post.return_value.status_code=200
        client.session.post.return_value.json.return_value={'data':self.data}
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'CACHE_DIR',folder),patch.object(client,'_wait_hist_rate_limit'):
            self.assertEqual(client.fetch_nifty_structure_candles('2026-10-12','2026-10-22',historical=True),self.data)

    def test_sensex_decision_is_unchanged_by_nifty_gate(self):
        with patch.object(bot.SelfLearningBrain,"_load_json",side_effect=lambda path,default:default), \
             patch.object(bot,"atomic_write_json"):
            brain=bot.SelfLearningBrain(logging.getLogger("test"))
            view={"index":"SENSEX","exceeds_50_lakhs":True,"recommended_dir":"BUY_CE",
                  "atm_ce_ltp":150,"atm_ce_bid":149.95,"atm_ce_ask":150,"atm_ce_bid_qty":1000,"atm_ce_ask_qty":1000,"wall_tag":"PUT_SIDE_GT_50L","ce_quadrant":"SHORT_COVERING",
                  "pe_quadrant":"SHORT_BUILDUP","market_controller":"BUYERS","market_dominance":"BULLS",
                  "state_vec":[0]*10,"d_spot_5m":40,"diff_ce_minus_pe_lakhs":-60}
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
    def test_single_sided_strikes_keep_puts_when_call_is_absent(self):
        sparse={k:{side:v for side,v in legs.items() if (side=='ce' and float(k)>22025) or
                 (side=='pe' and float(k)<=22025)} for k,legs in chain().items()}
        self.tracker.assess(sparse,22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(sparse,22025,self.t+300,'2026-10-27')
        self.assertTrue(info['ready'])
        self.assertIsNotNone(ref)
        self.assertEqual(ref['pe_oi'],30000)

    def test_wall_direction(self):
        analysis={'recommended_dir':'BUY_CE','oi_analysis':{'ready':True,'room_to_resistance':34}}
        self.assertFalse(bot.SelfLearningBrain.has_wall_room(analysis))
        analysis['oi_analysis']['room_to_resistance']=35
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

    def test_startup_requires_minimum_snapshot_age(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(11000),22025,self.t+269,'2026-10-27')
        self.assertIsNone(ref);self.assertFalse(info['ready'])

    def test_snapshot_jitter_window_boundaries(self):
        for age,accepted in ((269,False),(270,True),(298,True),(392,True),(450,True),(451,False)):
            with self.subTest(age=age):
                tracker=NiftyOITracker()
                tracker.assess(chain(),22025,self.t,'2026-10-27')
                reference,info=tracker.assess(chain(11000),22025,self.t+age,'2026-10-27')
                self.assertEqual(info['ready'],accepted)
                self.assertEqual(reference is not None,accepted)

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
        ref,info=self.tracker.assess(chain(),22025,self.t+451,'2026-10-27')
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
            self.assertEqual(brain.select_action(self.analysis,1,bot.dtime(12,15))[0],'HOLD')
            self.assertEqual(self.analysis['entry_decision']['action'],'HOLD')

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
        self.analysis.update(exceeds_50_lakhs=True,diff_ce_minus_pe_lakhs=-60)
        self.analysis['oi_analysis']['room_to_resistance']=24
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(self.analysis,0),('HOLD',None))
        self.assertEqual(self.analysis['entry_decision']['reason'],'Insufficient room to nearest building OI wall')

class BuyerFilterTests(unittest.TestCase):
    def test_dynamic_premium_boundaries(self):
        for index,base in (('NIFTY',45),('SENSEX',125)):
            for dte,mult in ((0.59,1.45),(0.6,1.2),(1.49,1.2),(1.5,1.08),(2.99,1.08),(3,1)):
                self.assertAlmostEqual(bot.min_premium(index,dte),base*mult)

    def test_expiry_uses_decision_time_and_ist(self):
        now=datetime(2026,10,27,9,30,tzinfo=IST)
        self.assertEqual(bot.days_to_expiry('2026-10-27',now),0.25)
        self.assertEqual(bot.days_to_expiry('2026-10-27',now.replace(tzinfo=None)),0.25)
        self.assertEqual(bot.days_to_expiry('not-a-date',now),5)
        self.assertEqual(bot.days_to_expiry('2026-10-26',now),0)

    def test_observed_edge_threshold_requires_sufficient_move(self):
        for index in ('NIFTY','SENSEX'):
            cfg=bot.INDICES_CONFIG[index]
            self.assertFalse(bot.premium_allows_edge(100,bot.option_stop_points(index,100),cfg['min_spot_impulse'],0.50))
        self.assertTrue(bot.premium_allows_edge(100,10,19,0.5))
        self.assertFalse(bot.premium_allows_edge(100,10,18.99,0.5))

    def test_theta_boundaries_and_no_early_bypass(self):
        self.assertTrue(bot.theta_time_ok(0.2,bot.dtime(10,29),True,0.12))
        self.assertFalse(bot.theta_time_ok(0.2,bot.dtime(10,30),True))
        self.assertFalse(bot.theta_time_ok(0.2,bot.dtime(10),False))
        self.assertFalse(bot.theta_time_ok(0.2,bot.dtime(10),True,0.121))
        self.assertFalse(bot.theta_time_ok(1,bot.dtime(13),False))
        self.assertTrue(bot.theta_time_ok(1,bot.dtime(13),True,0.1))

    def test_spread_quantity_and_nonfinite_values(self):
        self.assertTrue(bot.spread_and_liquidity_ok(99,101,100,130,130,2,65,100))
        self.assertFalse(bot.spread_and_liquidity_ok(99,101,100,65,130,2,65,100))
        self.assertFalse(bot.spread_and_liquidity_ok(49.5,50.5,50,130,130,1,65,50))
        self.assertFalse(bot.spread_and_liquidity_ok(98,102,100,130,130,1,65,100))
        self.assertFalse(bot.spread_and_liquidity_ok(float('nan'),101,100,130,130,1,65,100))

    def test_chain_preserves_side_specific_quotes_and_greeks(self):
        c=chain()
        c['22050']['ce'].update(top_bid_price=99,top_ask_price=100,top_bid_quantity=65,top_ask_quantity=130,
                               greeks={'delta':0.55,'theta':-5})
        c['22000']['pe'].update(top_bid_price=199,top_ask_price=200,top_bid_quantity=260,top_ask_quantity=325,
                               greeks={'delta':-0.6,'theta':-30})
        stamp=datetime(2026,10,27,9,30,tzinfo=IST).timestamp()
        result=bot.MarketMicrostructureAnalyzer().evaluate_chain('NIFTY',{'last_price':22025,'oc':c},'2026-10-27',stamp)
        self.assertEqual(result['days_to_expiry'],0.25)
        self.assertEqual((result['atm_ce_delta'],result['atm_pe_delta']),(0.55,-0.6))
        self.assertEqual((result['atm_ce_bid'],result['atm_pe_bid']),(99,199))

    def test_veto_precedes_rule_creation_and_learning(self):
        view={'index':'SENSEX','exceeds_50_lakhs':True,'recommended_dir':'BUY_PE','atm_pe_ltp':200,
              'd_spot_5m':-30,'days_to_expiry':5}
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json') as save:
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(view,0),('HOLD',None))
            self.assertEqual(view['entry_decision']['reason'],'Premium too expensive vs expected impulse')
            self.assertEqual(brain.rules,{})
            save.assert_not_called()

    def test_selected_put_theta_and_depth_are_hard_vetoes(self):
        base={'index':'SENSEX','exceeds_50_lakhs':True,'recommended_dir':'BUY_PE','atm_pe_ltp':200,
              'd_spot_5m':-80,'days_to_expiry':0.2,'atm_ce_theta':-1,'atm_pe_theta':-30}
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'),patch.object(bot,'premium_allows_edge',return_value=True):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(base,0,bot.dtime(10)),('HOLD',None))
            self.assertEqual(base['entry_decision']['reason'],'Theta / days-to-expiry filter')
            base.update(days_to_expiry=5,atm_pe_theta=-1,atm_pe_bid=199,atm_pe_ask=200,
                        atm_pe_bid_qty=19,atm_pe_ask_qty=20)
            self.assertEqual(brain.select_action(base,0,bot.dtime(10)),('HOLD',None))
            self.assertEqual(base['entry_decision']['reason'],'Spread or liquidity filter')

class StructureQualityGateTests(unittest.TestCase):
    def test_level_put_is_vetoed_against_bullish_put_wall(self):
        view=self.view(room=60,rr=5)
        view.update(recommended_dir='BUY_PE',atm_pe_ltp=100,d_spot_5m=-30,diff_ce_minus_pe_lakhs=-60)
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(view,0)[0],'HOLD')
            self.assertIn('Veto BUY_PE',view['entry_decision']['reason'])

    def test_observer_records_first_observed_oi_and_price_proxy(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        stamp=datetime(2026,10,23,9,15,tzinfo=IST).timestamp()
        analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':chain()},snapshot_time=stamp)
        result=analyzer.evaluate_chain('NIFTY',{'last_price':22030,'oc':chain(11000,101)},snapshot_time=stamp+300)
        self.assertEqual(result['cum_ce_oi_build'],3000)
        self.assertEqual(result['cum_pe_oi_build'],3000)
        self.assertEqual(result['cum_spot_delta'],5)
        self.assertEqual(result['session_high'],22030)
        self.assertEqual(result['session_low'],22025)
        self.assertIn('not volume VWAP',result['session_vwap_basis'])

    def test_session_context_has_ten_features_and_resets_each_day(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        stamp=datetime(2026,10,23,9,15,tzinfo=IST).timestamp()
        first=analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':chain()},snapshot_time=stamp)
        later=analyzer.evaluate_chain('NIFTY',{'last_price':22075,'oc':chain()},snapshot_time=stamp+180*60)
        self.assertEqual(len(later['state_vec']),10)
        self.assertEqual((first['state_vec'][7],first['state_vec'][9]),(0,-1))
        self.assertEqual((later['state_vec'][7],later['state_vec'][9]),(1,0.5))
        next_day=analyzer.evaluate_chain('NIFTY',{'last_price':23000,'oc':chain()},snapshot_time=stamp+86400)
        self.assertEqual(next_day['state_vec'][7],0)
        self.assertEqual(next_day['d_spot_5m'],0)
        self.assertEqual(next_day['session_anchor']['spot'],23000)

    def test_v2_local_oi_replaces_basket_gate_but_keeps_theta_and_price_vetoes(self):
        for theta,impulse,local,expected in ((-20,30,True,'BUY_CE'),(-100,30,True,'HOLD'),
                                             (-20,-5,True,'HOLD'),(-20,30,False,'HOLD')):
            with self.subTest(theta=theta,impulse=impulse,local=local),\
                 patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),\
                 patch.object(bot,'atomic_write_json'):
                view=self.view();view.update(entry_model='LEVEL_CANDLE_AND_STRIKE_OI_V2',
                    exceeds_50_lakhs=False,diff_ce_minus_pe_lakhs=0,atm_ce_theta=theta,
                    days_to_expiry=1.2,d_spot_5m=impulse)
                if not local:view['market_structure']['strike_oi_confirmation']=[]
                action=bot.SelfLearningBrain(logging.getLogger('test')).select_action(view,0,bot.dtime(10))[0]
                self.assertEqual(action,expected)
                if action=='BUY_CE':self.assertEqual(view['market_structure']['max_hold_minutes'],20)

    def test_explicit_maximum_hold_is_applied_in_replay(self):
        start=datetime(2026,10,26,10,tzinfo=IST).timestamp()
        result=bot.evaluate_real_option_trade_bars(0,True,[start+300*i for i in range(6)],
            [100]*6,[101]*6,[99]*6,[100]*6,[22600]*6,8.5,interval_min=5,
            index='NIFTY',lots=1,lot_size=65,entry_at_open=True,max_hold_minutes=20)
        self.assertEqual(result[0],3)
        self.assertEqual(result[3],'MAX_HOLD_TIME')

    def view(self,room=40,rr=1.6,setup='BREAKOUT_HOLD'):
        return {'index':'NIFTY','entry_model':'LEVEL_CANDLE_AND_STRIKE_OI_V1',
                'recommended_dir':'BUY_CE','exceeds_50_lakhs':True,
                'market_structure':{'allowed':True,'strike_oi_confirmation':[{'side':'pe'}],
                                    'room_points':room,'structure_rr':rr,'setup':setup},
                'oi_analysis':{'ready':True,'room_to_resistance':40},'atm_ce_ltp':100,
                'atm_ce_bid':99.95,'atm_ce_ask':100,'atm_ce_bid_qty':1000,'atm_ce_ask_qty':1000,
                'days_to_expiry':5,'d_spot_5m':30,'diff_ce_minus_pe_lakhs':-60,'wall_tag':'wall','ce_quadrant':'ce',
                'pe_quadrant':'pe','market_controller':'buyers','market_dominance':'bulls','state_vec':[0]*10}

    def test_room_setup_and_post_loss_boundaries(self):
        for room,rr,setup,loss,allowed in (
                (24.9,5,'BREAKOUT_HOLD',0,False),(25,1.35,'BREAKOUT_HOLD',0,True),
                (40,1.34,'REJECTION_CONFIRMED',0,False),(40,2.19,'RETEST_CONFIRMED',0,False),
                (40,2.2,'RETEST_CONFIRMED',0,True),(40,2.09,'BREAKOUT_HOLD',1,False),
                (40,2.2,'RETEST_CONFIRMED',1,True),(55,2.0,'RETEST_CONFIRMED',1,True),(54.9,2.0,'RETEST_CONFIRMED',1,False),(float('nan'),4,'BREAKOUT_HOLD',0,False)):
            with self.subTest(room=room,rr=rr,setup=setup,loss=loss),patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
                brain=bot.SelfLearningBrain(logging.getLogger('test'))
                view=self.view(room,rr,setup)
                self.assertEqual(brain.select_action(view,loss,bot.dtime(10))[0]=='BUY_CE',allowed)

    def test_observed_impulse_boundary(self):
        self.assertFalse(bot.premium_allows_edge(100,10,18.99,0.5))
        self.assertTrue(bot.premium_allows_edge(100,10,19,0.5))

class DailyTrendTests(unittest.TestCase):
    def test_structural_entry_requires_wall_and_verified_reference(self):
        base=StructureQualityGateTests().view()
        for edit,allowed in ((lambda x:None,True),
                             (lambda x:x.update(exceeds_50_lakhs=False),False),
                             (lambda x:x.update(diff_ce_minus_pe_lakhs=-49.9),False),
                             (lambda x:x['oi_analysis'].update(ready=False),False)):
            view=copy.deepcopy(base);edit(view)
            with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
                brain=bot.SelfLearningBrain(logging.getLogger('test'))
                self.assertEqual(brain.select_action(view,0,bot.dtime(10))[0]=='BUY_CE',allowed)

    def test_fifteen_minute_reference_uses_time_not_three_fast_polls(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        start=datetime(2026,10,23,9,15,tzinfo=IST).timestamp()
        for offset in (0,3,6,9):
            view=analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':chain()},snapshot_time=start+offset)
            self.assertFalse(view['trend_15m_ready'])
            self.assertEqual(view['d_spot_15m'],0)
        view=analyzer.evaluate_chain('NIFTY',{'last_price':22040,'oc':chain()},snapshot_time=start+900)
        self.assertTrue(view['trend_15m_ready'])
        self.assertEqual(view['d_spot_15m'],15)
        self.assertEqual(view['trend_15m_reference_age_seconds'],900)

    def test_six_point_move_can_generate_daily_direction_with_one_quadrant(self):
        start=datetime(2026,10,23,9,15,tzinfo=IST).timestamp()
        for side in ('CE','PE'):
            analyzer=bot.MarketMicrostructureAnalyzer()
            args=dict(idx_name='NIFTY',spot=22000,call_strikes=[22050,22100,22150],
                      put_strikes=[22000,21950,21900],ce_oi=5000000 if side=='CE' else 8000000,
                      pe_oi=8000000 if side=='CE' else 5000000,ce_prem=300,pe_prem=300,
                      ce_iv=20,pe_iv=20,atm_ce_ltp=100,atm_pe_ltp=100,live_ts=start)
            analyzer.evaluate_snapshot(**args)
            args.update(spot=22006 if side=='CE' else 21994,live_ts=start+300,
                        ce_oi=5100000 if side=='CE' else 9000000,
                        pe_oi=9000000 if side=='CE' else 5100000,
                        ce_prem=315 if side=='CE' else 300,pe_prem=300 if side=='CE' else 315)
            view=analyzer.evaluate_snapshot(**args)
            self.assertEqual(view['recommended_dir'],'HOLD')
            self.assertEqual(view['daily_trend_dir'],'BUY_'+side)

    def test_between_level_trend_cannot_bypass_structural_entry(self):
        base=StructureQualityGateTests().view()
        base.update(recommended_dir='HOLD',daily_trend_dir='BUY_CE',exceeds_50_lakhs=False,
                    spot=22050,session_vwap=22040,d_spot_5m=6,d_spot_15m=24,
                    diff_ce_minus_pe_lakhs=-25,cum_oi_shift=0,ce_quadrant='LONG_BUILDUP',pe_quadrant='NEUTRAL')
        base['market_structure']={'ready':True,'allowed':False,'reason':'No marked level with reaction','room_points':28}
        base['oi_analysis']={'ready':True,'room_to_resistance':30}
        for edit,allowed in ((lambda x:None,False),
                             (lambda x:x['oi_analysis'].pop('room_to_resistance'),False),
                             (lambda x:x['market_structure'].update(reason='Wait for retest'),False),
                             (lambda x:x['market_structure'].update(room_points=27.9),False)):
            view=copy.deepcopy(base);edit(view)
            with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
                brain=bot.SelfLearningBrain(logging.getLogger('test'))
                result=brain.select_action(view,0,bot.dtime(10))
                self.assertEqual(result[0]=='BUY_CE',allowed)

if __name__=="__main__":
    unittest.main()

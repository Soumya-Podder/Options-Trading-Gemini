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


if __name__=="__main__":
    unittest.main()

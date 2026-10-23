import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import MagicMock,patch
import logging
import dhan_bot_engine as bot


class ReplayTests(unittest.TestCase):
    def test_premium_scaled_nifty_stop_is_clamped_and_index_specific(self):
        for premium,expected in ((45,6.15),(100,8.5),(150,12.5),(300,12.5)):
            self.assertEqual(bot.option_stop_points('NIFTY',premium),expected)
        self.assertEqual(bot.option_stop_points('SENSEX',150),20)

    def test_nifty_ratchet_waits_then_locks_and_never_lowers_stop(self):
        self.assertEqual(bot.update_ratchet_sl(100,90,111.5,10),(90,False))
        self.assertEqual(bot.update_ratchet_sl(100,90,115,10),(102.5,True))
        self.assertEqual(bot.update_ratchet_sl(100,90,122,10),(116,True))
        self.assertEqual(bot.update_ratchet_sl(100,90,130,10),(123.5,True))
        self.assertEqual(bot.update_ratchet_sl(100,124,130,10),(124,True))

    def test_current_replay_uses_entry_premium_for_initial_risk(self):
        start=datetime(2026,10,22,10)
        stamps=[int((start+timedelta(minutes=i)).timestamp()) for i in range(2)]
        args=(0,True,stamps,[150,150],[151,151],[140,149],[150,150],[22000,22000],6.15)
        current=bot.evaluate_real_option_trade_bars(*args,interval_min=1,index='NIFTY',
            lots=1,lot_size=65,entry_at_open=True,current_session_policy=True)
        legacy=bot.evaluate_real_option_trade_bars(*args,interval_min=1,index='NIFTY',
            lots=1,lot_size=65,entry_at_open=True,current_session_policy=False)
        self.assertEqual(current[3],'END_OF_WINDOW')
        self.assertEqual(legacy[3],'INITIAL_SL_HIT')

    def test_daily_target_includes_prior_losses_and_partial_profit_once(self):
        position={'entry_price':100,'qty':65,'lots':1}
        self.assertTrue(bot.daily_target_reached(0,position,120))
        self.assertFalse(bot.daily_target_reached(-500,position,120))
        position={'entry_price':100,'qty':65,'lots':2,'exit_fills':[{'price':110,'qty':65}]}
        self.assertTrue(bot.daily_target_reached(0,position,111))
        self.assertFalse(bot.daily_target_reached(0,position,110))

    def replay(self,bars,**kwargs):
        start=datetime(2026,10,22,10)
        stamps=[int((start+timedelta(minutes=5*i)).timestamp()) for i in range(len(bars))]
        strikes=kwargs.pop("strikes",[22000]*len(bars))
        stamps=kwargs.pop("timestamps",stamps)
        fills=[]
        result=bot.evaluate_real_option_trade_bars(0,True,stamps,
            *[[b[i] for b in bars] for i in range(4)],strikes,6,
            index="NIFTY",lots=kwargs.pop("lots",1),lot_size=65,exit_fills=fills,**kwargs)
        return result,fills

    def test_current_daily_target_uses_shared_net_helper(self):
        def check(entry,remaining,fills,bid):
            return bot.daily_target_reached(-400,{'entry_price':entry,'qty':remaining,'lots':2,'exit_fills':fills},bid)
        result,fills=self.replay([(100,100,100,100),(100,120,99,119)],lots=2,
            session_profit_check=check,current_session_policy=True)
        self.assertEqual(result[3],'DAILY_NET_PROFIT_TARGET')
        self.assertEqual(len(fills),2)
        gross=sum((f['price']-100)*f['qty'] for f in fills)
        self.assertGreaterEqual(-400+gross-(40+2*24.5+20),1200)
        self.assertEqual(fills[0]['price'],110)

    def test_current_daily_target_cannot_override_same_bar_stop(self):
        check=lambda e,q,f,b:bot.daily_target_reached(0,{'entry_price':e,'qty':q,'lots':1,'exit_fills':f},b)
        result,_=self.replay([(100,100,100,100),(100,130,90,125)],
            session_profit_check=check,current_session_policy=True)
        self.assertEqual(result[3],'INITIAL_SL_HIT')

    def test_current_time_exit_uses_peak_known_before_bar(self):
        bars=[(100,100.5,99.5,100)]*5+[(101,150,100,140)]
        result,_=self.replay(bars,entry_at_open=True,current_session_policy=True)
        self.assertEqual(result[0],5)
        self.assertEqual(result[1],101)
        self.assertEqual(result[3],'THETA_TIME_STOP')

    def test_gap_through_stop_uses_opening_price(self):
        result,_=self.replay([(100,100,100,100),(80,90,75,85)])
        self.assertEqual(result[1],80)

    def test_strike_change_is_unvalidated_not_synthetic(self):
        with self.assertRaisesRegex(bot.ReplayDataError,"Rolling strike changed"):
            self.replay([(100,100,100,100),(100,110,90,100)],strikes=[22000,22050])

    def test_entry_strike_cannot_change_before_next_bar(self):
        with self.assertRaisesRegex(bot.ReplayDataError,"before executable entry"):
            self.replay([(100,110,99,105)],entry_at_open=True,expected_strike=21950)

    def test_intrabar_high_cannot_create_same_bar_trailing_exit(self):
        result,_=self.replay([(100,100,100,100),(100,120,99,100),(125,126,124,125)])
        self.assertEqual(result[0],2)
        self.assertEqual(result[3],"END_OF_WINDOW")

    def test_next_bar_gap_below_new_trail_uses_worse_open(self):
        result,_=self.replay([(100,100,100,100),(100,120,99,110),(102,110,100,105)])
        self.assertEqual((result[0],result[1]),(2,102))

    def test_open_entry_does_not_use_signal_close_as_fill(self):
        result,_=self.replay([(100,110,99,109)],entry_at_open=True)
        self.assertEqual(result[1],109)
        self.assertEqual(result[2],1.5)

    def test_adverse_slippage_is_applied(self):
        result,_=self.replay([(100,110,99,109)],entry_at_open=True,slippage_pct=0.1)
        self.assertLess(result[1],109)
        self.assertLess(result[2],1.5)

    def test_missing_bar_is_not_a_validated_exit(self):
        start=int(datetime(2026,10,22,10).timestamp())
        with self.assertRaisesRegex(bot.ReplayDataError,"Missing post-entry"):
            self.replay([(100,100,100,100),(100,101,99,100)],timestamps=[start,start+600])

    def test_open_above_first_target_precedes_later_stop(self):
        result,fills=self.replay([(100,100,100,100),(111,115,90,100)],lots=2)
        self.assertEqual([f["price"] for f in fills],[110,94])
        self.assertEqual(result[1],102)

    def test_truncated_session_cannot_be_scored(self):
        with self.assertRaisesRegex(bot.ReplayDataError,"History ends before session close"):
            self.replay([(100,101,99,100)],entry_at_open=True,require_session_end=True)

    def test_1445_cutoff_does_not_need_after_cutoff_option_prices(self):
        start=int(datetime(2026,10,22,14,40).timestamp())
        result,_=self.replay([(100,101,99,100)],timestamps=[start],
                             entry_at_open=True,require_session_end=True)
        self.assertEqual(result[3], 'EOD_SQUAREOFF')

    def test_bar_after_1445_cannot_change_exit_or_invalidate_it(self):
        start=int(datetime(2026,10,22,14,40).timestamp())
        result,_=self.replay([(100,101,99,100),(float('nan'),)*4],
                             timestamps=[start,start+300],entry_at_open=True,require_session_end=True)
        self.assertEqual((result[1],result[3]), (100,'EOD_SQUAREOFF'))

    def test_next_day_open_is_not_an_exit_for_a_missing_session_tail(self):
        start=int(datetime(2026,10,22,10).timestamp())
        tomorrow=int(datetime(2026,10,23,9,15).timestamp())
        with self.assertRaisesRegex(bot.ReplayDataError,"History ends before session close"):
            self.replay([(100,100,100,100),(100,101,99,100)],
                        timestamps=[start,tomorrow],require_session_end=True)

    def test_other_option_intervals_cannot_mix_with_five_minute_nifty_signals(self):
        with self.assertRaisesRegex(ValueError,"five-minute option candles"):
            bot.run_real_historical_backtest(30,["NIFTY"],"15",logging.getLogger("test"))

    def test_unvalidated_backtest_trade_does_not_train_or_create_fake_pnl(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,10,22,12,tzinfo=tz)
        start=datetime(2026,10,16,10)
        data={"timestamp":[int((start+timedelta(minutes=5*i)).timestamp()) for i in range(3)],
              "open":[100]*3,"high":[101]*3,"low":[99]*3,"close":[100]*3,
              "oi":[10000000]*3,"iv":[20]*3,"strike":[22000,22000,22050],"spot":[22000]*3}
        client=MagicMock()
        client.fetch_rolling_option_window.return_value=data
        analyzer=MagicMock()
        analyzer.evaluate_snapshot.return_value={"recommended_dir":"BUY_CE","state_vec":[0]*10,
            "d_spot_5m":10,"diff_ce_minus_pe_lakhs":100,"spot":22000}
        brain=MagicMock()
        brain.select_action.return_value=("BUY_CE","rule")
        brain.compute_lots.return_value=1
        with patch.object(bot,"datetime",Clock),patch.object(bot,"DhanV2RestClient",return_value=client), \
             patch.object(bot,"MarketMicrostructureAnalyzer",return_value=analyzer), \
             patch.object(bot,"SelfLearningBrain",return_value=brain), \
             patch.object(bot,"load_dotenv_file",return_value={"DHAN_CLIENT_ID":"test","DHAN_ACCESS_TOKEN":"test"}), \
             patch.object(bot,"atomic_write_json") as save:
            bot.run_real_historical_backtest(2,["NIFTY"],"5",logging.getLogger("test"))
        ledger=next(c.args[1] for c in save.call_args_list if str(c.args[0]).endswith("paper_trade_ledger.json"))
        self.assertTrue(all(c.args[0] not in ("paper_trade_ledger.json","live_telemetry.json","bot_brain_state.json","learned_rules.json") for c in save.call_args_list))
        self.assertEqual(ledger["total_trades"],0)
        self.assertEqual(ledger["net_profit_after_charges"],0)
        self.assertEqual(len(ledger["unvalidated_trades"]),1)
        self.assertEqual(ledger["backtest_status"],"INCOMPLETE_OPTION_REPLAY")
        brain.learn_from_trade.assert_not_called()


class NiftyTargetTests(unittest.TestCase):
    def replay(self, bars, index="NIFTY", lots=2):
        start = datetime(2026,10,22,10,0)
        stamps = [int((start+timedelta(minutes=5*i)).timestamp()) for i in range(len(bars))]
        fills = []
        result = bot.evaluate_real_option_trade_bars(0,True,stamps,
            [b[0] for b in bars],[b[1] for b in bars],[b[2] for b in bars],
            [b[3] for b in bars],[22000]*len(bars),6,
            index=index,lots=lots,lot_size=65,exit_fills=fills)
        return result,fills

    def test_whole_lot_split_and_scope(self):
        self.assertEqual(bot.nifty_target_quantities("NIFTY",2,65),(65,65))
        self.assertEqual(bot.nifty_target_quantities("NIFTY",3,65),(65,130))
        self.assertEqual(bot.nifty_target_quantities("NIFTY",4,65),(130,130))
        self.assertIsNone(bot.nifty_target_quantities("NIFTY",1,65))
        self.assertIsNone(bot.nifty_target_quantities("SENSEX",2,20))

    def test_two_targets_average_to_fifteen_points(self):
        result,fills=self.replay([(100,100,100,100),(100,110,99,109),(111,120,108,119)])
        self.assertEqual(result[3],"TARGET_2_20_POINTS")
        self.assertEqual(result[1],115)
        self.assertEqual([(f["qty"],f["price"]) for f in fills],[(65,110),(65,120)])

    def test_both_targets_in_one_bar_and_odd_lots(self):
        result,fills=self.replay([(100,100,100,100),(100,125,99,123)],lots=3)
        self.assertEqual([f["qty"] for f in fills],[65,130])
        self.assertAlmostEqual(result[1],116.6666666667)

    def test_stop_before_any_target_on_ambiguous_bar(self):
        result,fills=self.replay([(100,100,100,100),(100,125,90,120)])
        self.assertEqual(result[3],"INITIAL_SL_HIT")
        self.assertEqual(len(fills),1)
        self.assertEqual(fills[0]["qty"],130)

    def test_remaining_quantity_can_stop_after_target_one(self):
        result,fills=self.replay([(100,100,100,100),(100,111,99,110),(110,112,90,95)])
        self.assertEqual([f["price"] for f in fills],[110,94])
        self.assertEqual(result[1],102)

    def test_end_window_closes_only_remaining_quantity(self):
        result,fills=self.replay([(100,100,100,100),(100,111,99,108),(108,112,102,105)])
        self.assertEqual(result[3],"END_OF_WINDOW")
        self.assertEqual(sum(f["qty"] for f in fills),130)
        self.assertEqual(result[1],107.5)

    def test_single_lot_and_sensex_keep_existing_behavior(self):
        bars=[(100,100,100,100),(100,120,99,119),(119,120,110,111)]
        original,_=self.replay(bars,index="",lots=1)
        self.assertEqual(self.replay(bars,lots=1)[0],original)
        result,fills=self.replay(bars,index="SENSEX",lots=2)
        self.assertEqual(result,original)
        self.assertEqual(fills,[])

    def test_partial_is_booked_once_and_final_is_one_trade(self):
        ledger={"initial_capital":30000,"virtual_balance_gross":30000,
                "virtual_balance_net":30000,"trades":[]}
        pos={"entry_price":100,"lots":2,"qty":130,"original_qty":130,
             "target_one_qty":65,"target_one_done":False}
        brain=MagicMock()
        with patch.object(bot,"atomic_write_json"):
            bot.book_target_one(ledger,pos,110,"2026-10-22 10:05:00")
            self.assertEqual(ledger["virtual_balance_gross"],30650)
            self.assertEqual(pos["qty"],65)
            bot.book_target_one(ledger,pos,111,"2026-10-22 10:06:00")
            self.assertEqual(ledger["virtual_balance_gross"],30650)
            fills=pos["exit_fills"]+[{"qty":65,"price":120,"reason":"TARGET_2_20_POINTS"}]
            bot.record_closed_trade(ledger,brain,"2026-10-22","2026-10-22 10:00:00",
                "2026-10-22 10:10:00","NIFTY",22000,"CE",2,130,22000,60,
                100,115,2.5,"TARGET_2_20_POINTS","rule",[],"TEST",
                exit_fills=fills,booked_gross=pos["partial_gross"],booked_net=pos["partial_net"])
        self.assertEqual(ledger["virtual_balance_gross"],31950)
        self.assertEqual(ledger["virtual_balance_net"],31841)
        self.assertEqual(ledger["total_trades"],1)
        self.assertEqual(ledger["trades"][0]["net_pnl"],1841)
        self.assertNotIn("active_position",ledger)
        brain.learn_from_trade.assert_called_once()


class NiftyEntryLimitTests(unittest.TestCase):
    day="2026-10-23"

    def test_eighth_entry_is_persisted_and_restart_cannot_take_ninth(self):
        ledger={"trades":[{"timestamp":self.day+" 10:00:00"} for _ in range(7)]}
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"ledger.json")
            self.assertEqual(bot.MAX_TRADES_PER_DAY,8)
            self.assertTrue(bot.reserve_index_entry(ledger,self.day,{},path))
            restarted=json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(bot.index_entries_today(restarted,self.day),8)
            self.assertFalse(bot.reserve_index_entry(restarted,self.day,{},path))
            restarted.pop("pending_entry")
            restarted["trades"].append({"timestamp":self.day+" 11:00:00"})
            self.assertFalse(bot.reserve_index_entry(restarted,self.day,{},path))

    def test_legacy_open_position_consumes_a_slot(self):
        ledger={"trades":[{"timestamp":self.day} for _ in range(7)],
                "active_position":{"entry_ts":self.day+" 10:00:00","target_one_done":True}}
        self.assertEqual(bot.index_entries_today(ledger,self.day),8)
        self.assertFalse(bot.reserve_index_entry(ledger,self.day,{}))

    def test_interrupted_submission_blocks_duplicate_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"ledger.json")
            ledger={"trades":[]}
            self.assertTrue(bot.reserve_index_entry(ledger,self.day,{},path))
            restarted=json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(bot.index_entries_today(restarted,self.day),1)
            self.assertFalse(bot.reserve_index_entry(restarted,self.day,{},path))

    def test_unpersisted_reservation_fails_before_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"ledger.json")
            with patch.object(bot,"atomic_write_json"):
                with self.assertRaises(OSError):
                    bot.reserve_index_entry({"trades":[]},self.day,{},path)

    def test_previous_day_closed_entries_do_not_use_todays_capacity(self):
        ledger={"trades":[{"timestamp":"2026-10-22 10:00:00"}],
                "daily_entry_counts":{"2026-10-22":8}}
        self.assertEqual(bot.index_entries_today(ledger,self.day),0)

if __name__=="__main__":unittest.main()

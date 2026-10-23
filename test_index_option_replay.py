import unittest
from datetime import datetime,timedelta
from unittest.mock import MagicMock,patch
import logging
import dhan_bot_engine as bot


class ReplayTests(unittest.TestCase):
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


if __name__=="__main__":unittest.main()

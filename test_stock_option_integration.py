import argparse
import copy
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import dhan_dynamic_fno_stock_bot as bot


class IntegrationTests(unittest.TestCase):
    def test_expiry_formats_and_invalid_dates(self):
        for raw in ("2026-10-29", "2026-10-29 14:30:00", "2026-10-29T14:30:00",
                    "29-Oct-2026", "29-October-2026", "29/10/2026", "29-10-2026"):
            self.assertEqual(bot.normalize_expiry(raw),"2026-10-29")
        for raw in ("",None,"31-Feb-2026","unknown"):
            self.assertIsNone(bot.normalize_expiry(raw))

    def test_security_id_alias_still_requires_master_match(self):
        stock={"symbol":"TEST","strike_step":10,"fno_lot_size":100,"nearest_expiry":"2026-11-26",
               "strikes_map":{"100.00":{"CE":{"security_id":"123","trading_symbol":"TEST-CE"}}}}
        chain={"100.000000":{"ce":{"securityId":123,"greeks":{"delta":0.6,"theta":-1,"gamma":0.01},"implied_volatility":20}}}
        self.assertEqual(bot.DynamicStockOptionsUniverse.resolve_atm_option(stock,100,"CE",chain,self.cfg)["opt_security_id"],"123")
        chain["100.000000"]["ce"]["securityId"]=999
        self.assertIsNone(bot.DynamicStockOptionsUniverse.resolve_atm_option(stock,100,"CE",chain,self.cfg))

    def test_paper_target_honors_minimum_rr_profit_requirement(self):
        cfg=bot.DynamicStrategyConfig({},argparse.Namespace())
        pos={**cfg.plan(50,430),"entry_price":50,"entry_epoch":self.now.timestamp(),"peak_bid":50}
        pos.update(current_sl=pos["initial_sl"],trail_jump=0)
        bid=53.5
        quote={"received_epoch":self.now.timestamp(),"depth":{"buy":[{"price":bid,"quantity":pos["qty"]}]}}
        self.assertGreater((bid-50)*pos["qty"]-bot.compute_fno_option_charges(50,bid,pos["qty"]),1200)
        self.assertLess(bid,pos["rupee_target_price"])
        self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))

    def test_follow_through_band_for_calls_and_puts(self):
        from unittest.mock import MagicMock
        stock={"eq_security_id":"1","nearest_expiry":"2026-11-26"}
        client=MagicMock()
        client.fetch_intraday_candles.return_value={"timestamp":[1]}
        for side,direction in (("CE",1),("PE",-1)):
            for advance,allowed in ((-0.31,False),(-0.3,True),(0,True),(1,True),(2,True),(2.01,False)):
                with self.subTest(side=side,advance=advance):
                    client.fetch_batch_ohlc.return_value={"NSE_EQ":{"1":{"last_price":100+direction*advance}}}
                    sig={"opt_type":side,"conf_close":100,"atr_5m":2}
                    with patch.object(bot,"evaluate_stock_setup",return_value=sig), \
                         patch.object(bot.DynamicStockOptionsUniverse,"resolve_atm_option",return_value={"opt_security_id":"9"}):
                        result=bot.verify_live_scanx_and_resolve_stock_option(client,stock,100,3,self.cfg)
                    self.assertEqual(result is not None,allowed)

    def setUp(self):
        self.cfg = bot.DynamicStrategyConfig({"STOCK_OPT_STOP_PCT":"1", "STOCK_OPT_TARGET_PCT":"10",
            "STOCK_OPT_MIN_NET_RR":"1.2", "STOCK_OPT_RUPEE_TARGET":"2000",
            "STOCK_OPT_RUPEE_MAX_RISK":"1000", "STOCK_OPT_MAX_LOTS":"3"}, argparse.Namespace())
        self.now = datetime(2026, 10, 22, 11, 0, 10, tzinfo=bot.IST)
        self.sig = {"symbol": "TEST", "lot_size": 500, "signal_timestamp": self.now.timestamp()-310,
                    "delta": 0.6, "theta": -1, "gamma": 0.01, "iv": 20,
                    "iv_range_low": 15, "iv_range_high": 30, "opt_type": "CE",
                    "greeks_timestamp": self.now.timestamp(), "expiry": "2026-11-26", "atr_5m": 20}
        self.quote = {"received_epoch": datetime(2026,10,22,11,0,10,tzinfo=bot.IST).timestamp(), "last_trade_time": "22/10/2026 11:00:10",
                      "depth": {"buy": [{"price": 99.95, "quantity": 500}],
                                "sell": [{"price": 100, "quantity": 500}]}}

    def qualify(self, quote=None, wallet=60000):
        return bot.qualify_quote(self.sig, quote or self.quote, self.cfg, wallet, self.now.timestamp())

    def test_qualified_quote_and_fixed_exit_plan(self):
        s = self.qualify()
        self.assertIsNotNone(s)
        self.assertEqual(s["hard_target_price"], 110)
        self.assertEqual(s["initial_sl"], 99)
        self.assertGreaterEqual(s["expected_net_rr"], 1.2)
        self.assertAlmostEqual(s["spread_fraction_of_initial_risk"], 0.05)
        self.assertAlmostEqual(s["bid_room_to_initial_stop"], 0.95)

    def test_wide_spread_stale_depth_or_insufficient_size_rejected(self):
        for edit in (lambda q: q["depth"]["buy"][0].update(price=97),
                     lambda q: q["depth"]["sell"][0].update(quantity=499),
                     lambda q: q.update(received_epoch=self.now.timestamp()-31)):
            q = copy.deepcopy(self.quote)
            edit(q)
            self.assertIsNone(self.qualify(q))
        self.assertIsNone(self.qualify(wallet=1000))

    def test_stale_confirmation_rejected(self):
        self.sig["signal_timestamp"] -= 600
        self.assertIsNone(self.qualify())

    def test_best_setup_is_not_first_or_largest_mover(self):
        ranked = bot.rank_candidates([{"symbol":"FIRST", "signal_quality":70, "expected_net_rr":4, "day_change_pct":8},
                                      {"symbol":"BEST", "signal_quality":90, "expected_net_rr":3.2, "day_change_pct":2.5}])
        self.assertEqual(ranked[0]["symbol"], "BEST")

    def test_shortlist_preserves_eight_each_side(self):
        quotes = {str(i): {"last_price": 103+i, "ohlc":{"close":100}} for i in range(10)}
        quotes.update({str(i+20): {"last_price": 97-i, "ohlc":{"close":100}} for i in range(10)})
        result = bot.shortlist_movers(quotes, self.cfg)
        self.assertEqual(len(result), 16)
        self.assertEqual(sum(x[0]>0 for x in result), 8)

    def test_environment_limits_are_validated(self):
        with self.assertRaises(ValueError):
            bot.DynamicStrategyConfig({"STOCK_OPT_MAX_DAILY_TRADES":"4"}, argparse.Namespace())
        cfg = bot.DynamicStrategyConfig({"STOCK_OPT_MAX_DAILY_TRADES":"1", "STOCK_OPT_MAX_CONCURRENT":"1"}, argparse.Namespace())
        self.assertEqual((cfg.max_daily_trades,cfg.max_concurrent), (1,1))

    def test_confirmation_window_boundaries_and_validation(self):
        for age, allowed in ((-1,False),(0,True),(300,True),(599,True),(600,False)):
            with self.subTest(age=age):
                sig={**self.sig,"signal_timestamp":self.now.timestamp()-300-age}
                result=bot.qualify_quote(sig,self.quote,self.cfg,60000,self.now.timestamp())
                self.assertEqual(result is not None,allowed)
        for window in (299,901):
            with self.assertRaises(ValueError):
                bot.DynamicStrategyConfig({"STOCK_OPT_CONFIRM_WINDOW_SEC":str(window)},argparse.Namespace())

    def test_strong_mover_spread_and_iv_still_require_depth_and_risk(self):
        cfg=bot.DynamicStrategyConfig({"STOCK_OPT_RUPEE_MAX_RISK":"1000"},argparse.Namespace())
        sig={**self.sig,"lot_size":100,"day_change_pct":-6,"opt_type":"PE","delta":-0.6,"iv":27.75}
        quote=copy.deepcopy(self.quote);quote['depth']['buy'][0]['price']=96
        good=bot.qualify_quote(sig,quote,cfg,60000,self.now.timestamp())
        self.assertIsNotNone(good)
        self.assertEqual(good['spread_limit_pct'],4.5)
        self.assertLessEqual(good['expected_net_risk'],1000)
        self.assertGreaterEqual(good['expected_net_rr'],1.3)
        self.assertIsNone(bot.qualify_quote({**sig,'day_change_pct':-5.99},quote,cfg,60000,self.now.timestamp()))
        self.assertIsNone(bot.qualify_quote({**sig,'iv':28.5},quote,cfg,60000,self.now.timestamp()))
        quote['depth']['sell'][0]['quantity']=99
        self.assertIsNone(bot.qualify_quote(sig,quote,cfg,60000,self.now.timestamp()))

    def test_strong_mover_expected_gain_floor_including_extended_plan(self):
        for extension in ('false','true'):
            cfg=bot.DynamicStrategyConfig({'STOCK_OPT_EXTEND_RUPEE_TARGET':extension},argparse.Namespace())
            sig={**self.sig,'lot_size':100,'day_change_pct':6,'atr_5m':6}
            result=bot.qualify_quote(sig,self.quote,cfg,60000,self.now.timestamp())
            self.assertIsNotNone(result)
            self.assertEqual(result['expected_gain_fraction'],0.55)
            self.assertGreaterEqual(result['expected_move_gain'],0.55*result['net_profit_target'])
            self.assertLess(result['expected_move_gain'],0.75*result['net_profit_target'])
            self.assertIsNone(bot.qualify_quote({**sig,'day_change_pct':5.99},self.quote,cfg,60000,self.now.timestamp()))
            self.assertIsNone(bot.qualify_quote({**sig,'atr_5m':1},self.quote,cfg,60000,self.now.timestamp()))

    def bars(self):
        ts = int(self.now.timestamp())
        return ts, {ts + 300*i: {"open":100, "high":101, "low":99.9, "close":100.5, "strike":1000} for i in range(self.cfg.max_hold_bars_5m)}

    def test_stop_first_when_bar_hits_both(self):
        ts, bars = self.bars()
        bars[ts].update(high=115, low=98)
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertEqual(sim["exit_reason"], "PREMIUM_STOP")
        self.assertLess(sim["exit_price"], sim["initial_sl"])

    def test_gap_stop_uses_open_below_stop(self):
        ts, bars = self.bars()
        bars[ts+300].update(open=98, low=97, close=98, high=99)
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertLess(sim["exit_price"], 98)

    def test_gap_above_target_precedes_later_same_bar_stop(self):
        ts,bars=self.bars()
        bars[ts+300].update(open=112,high=115,low=98,close=100)
        result=bot.simulate_fixed_premium_trade(ts,bars,500,self.cfg)
        self.assertEqual(result['exit_reason'], 'RUPEE_TARGET')
        self.assertGreater(result['exit_price'],result['entry_price'])

    def test_missing_or_different_strike_is_unvalidated(self):
        ts, bars = self.bars()
        del bars[ts+300]
        self.assertIsNone(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg))
        ts, bars = self.bars()
        bars[ts+300]["strike"] = 1010
        self.assertIsNone(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg))

    def test_fixed_target_and_time_exit(self):
        ts, bars = self.bars()
        bars[ts]["high"] = 115
        sim = bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)
        self.assertEqual(sim["exit_reason"], "RUPEE_TARGET")
        self.assertLessEqual(sim["exit_price"], sim["entry_price"]*1.10)
        ts, bars = self.bars()
        self.assertEqual(bot.simulate_fixed_premium_trade(ts, bars, 500, self.cfg)["exit_reason"], "NO_PROGRESS")

    def test_greek_and_expiry_filters_fail_closed(self):
        for values in ({"delta":0.1}, {"delta":-0.6}, {"theta":-100}, {"gamma":0},
                       {"iv":29}, {"iv":float("nan")}, {"atr_5m":0.1},
                       {"expiry":"2026-10-23"}, {"greeks_timestamp":self.now.timestamp()-121}):
            with self.subTest(values=values):
                original = self.sig.copy()
                self.sig.update(values)
                self.assertIsNone(self.qualify())
                self.sig = original
        del self.sig["delta"]
        self.assertIsNone(self.qualify())

    def test_spread_default_override_and_validation_ceiling(self):
        self.assertEqual(self.cfg.max_spread_pct,3.5)
        for cap in (0.2,2.5,5):
            cfg=bot.DynamicStrategyConfig({"STOCK_OPT_MAX_SPREAD_PCT":str(cap)},argparse.Namespace())
            self.assertEqual(cfg.max_spread_pct,cap)
        for cap in (0,5.01,float("nan")):
            with self.assertRaises(ValueError):
                bot.DynamicStrategyConfig({"STOCK_OPT_MAX_SPREAD_PCT":str(cap)},argparse.Namespace())

    def test_unchanged_paper_state_is_not_written(self):
        from unittest.mock import MagicMock
        gate=MagicMock()
        pos={"entry_id":"saved","opt_security_id":"11","contract":"TEST_CE","current_sl":99,"peak_bid":100,"persisted_state":[99,100]}
        gate.read.side_effect=[{"state":"reserved","details":{"mode":"PAPER","position":pos}},
                               {"state":"closed","details":{}}]
        gate.count_entries.return_value=2
        calls=[]
        def evaluate(position,*args):
            self.assertGreaterEqual(args[1].timestamp(), args[0]['received_epoch'])
            calls.append(1)
            if len(calls)==2:
                position["peak_bid"]=101
            if len(calls)==3:
                return 101,"TIME_EXIT"
            return None
        client=MagicMock()
        def fresh_depth(*args):
            received=clock.now.return_value.replace(second=clock.now.return_value.second+1)
            clock.now.return_value=received
            return {"11":{"received_epoch":received.timestamp()}}
        client.fetch_option_quotes.side_effect=fresh_depth
        with patch.object(bot,"datetime") as clock, \
             patch.object(bot,"load_dotenv_file",return_value={"DHAN_CLIENT_ID":"test","DHAN_ACCESS_TOKEN":"test"}), \
             patch.object(bot,"DhanStockOptionsClient",return_value=client), \
             patch.object(bot.DynamicStockOptionsUniverse,"load_universe",return_value={}), \
             patch.object(bot,"DailyTradeGate",return_value=gate), \
             patch.object(bot,"load_or_init_ledger",return_value={"trades":[]}), \
             patch.object(bot,"paper_exit",side_effect=evaluate), \
             patch.object(bot,"close_paper_position",return_value={"net_pnl":0}), \
             patch.object(bot,"TelegramNotifier",None), patch.object(bot.time,"sleep"):
            clock.now.return_value=self.now
            bot.run_dynamic_stock_options_session(False,False,self.cfg,logging.getLogger("test"))
        self.assertEqual(len(calls),3)
        gate.update_position.assert_called_once_with("2026-10-22",1,pos)
        gate.mark_closed.assert_called_once()
        self.assertEqual(pos["persisted_state"],(99,101))

    def test_detailed_rejection_reason_and_qualified_metrics(self):
        self.assertEqual(self.cfg.min_days_to_expiry,4)
        reasons=[]
        result=bot.qualify_quote(self.sig,self.quote,self.cfg,60000,self.now.timestamp(),reasons)
        self.assertIsNotNone(result)
        self.assertEqual(reasons,[])
        self.assertEqual(result["dte"],35)
        self.assertAlmostEqual(result["theta_pct_per_day"],1)
        self.assertGreaterEqual(result["expected_move_gain"],self.cfg.rupee_target)
        for edits,expected in (({"expiry":"2026-10-25"},"Only 3 days to expiry, need 4"),
                               ({"theta":-7},"Theta 7.00%/day above 6.00%"),
                               ({"atr_5m":0.1},"A 1.8 ATR move nets only Rs."),
                               ({"iv":29},"IV in the top"),
                               ({"delta":float("nan")},"Non-finite broker Greeks")):
            with self.subTest(edits=edits):
                reasons=[]
                self.assertIsNone(bot.qualify_quote({**self.sig,**edits},self.quote,self.cfg,60000,self.now.timestamp(),reasons))
                self.assertEqual(len(reasons),1)
                self.assertTrue(reasons[0].startswith(expected),reasons)
        q=copy.deepcopy(self.quote)
        q["depth"]["buy"][0]["price"]=98.9
        reasons=[]
        self.assertIsNone(bot.qualify_quote(self.sig,q,self.cfg,60000,self.now.timestamp(),reasons))
        self.assertEqual(reasons,["Bid already at or below the planned stop"])

    def test_new_defaults_preserve_rupee_risk_and_rr(self):
        cfg=bot.DynamicStrategyConfig({},argparse.Namespace())
        self.assertEqual((cfg.target_pct,cfg.stop_pct,cfg.rupee_target,cfg.rupee_max_risk,cfg.max_lots),
                         (12,5,900,1800,2))
        self.assertIsNone(cfg.plan(100,500))
        plan=cfg.plan(50,430)
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan["expected_net_risk"],1800)
        self.assertGreaterEqual(plan["expected_net_reward"],900)
        self.assertGreaterEqual(plan["expected_net_rr"],1.3)
        self.assertEqual(plan["initial_sl"],47.5)

    def test_old_last_trade_with_fresh_depth_receipt_can_be_evaluated(self):
        q=copy.deepcopy(self.quote)
        q.update(last_trade_time="21/10/2026 10:00:00",received_epoch=self.now.timestamp())
        result=self.qualify(q)
        self.assertIsNotNone(result)
        self.assertEqual(result["quote_age_seconds"],0)
        self.assertGreater(result["last_trade_age_seconds"],86400)
        q["received_epoch"]-=31
        self.assertIsNone(self.qualify(q))
        q["received_epoch"]=self.now.timestamp()+1
        self.assertIsNone(self.qualify(q))

    def test_paper_exit_uses_fresh_depth_not_last_trade_time(self):
        pos={**self.cfg.plan(100,500),"entry_price":100,"entry_epoch":self.now.timestamp(),"current_sl":99}
        q=copy.deepcopy(self.quote)
        q.update(last_trade_time="21/10/2026 10:00:00",received_epoch=self.now.timestamp())
        q["depth"]["buy"][0]["price"]=106
        self.assertEqual(bot.paper_exit(pos,q,self.now,self.cfg)[1],"RUPEE_TARGET")
        q["received_epoch"]-=31
        self.assertIsNone(bot.paper_exit(pos,q,self.now,self.cfg))

    def test_premium_cap(self):
        q = copy.deepcopy(self.quote)
        q["depth"]["buy"][0]["price"] = 1001
        q["depth"]["sell"][0]["price"] = 1001
        self.assertIsNone(self.qualify(q,wallet=1000000))

    def test_paper_rupee_exit_trailing_and_no_progress(self):
        plan = self.cfg.plan(100,500)
        def position():
            return {**plan,"entry_price":100,"entry_epoch":self.now.timestamp(),"current_sl":plan["initial_sl"]}
        def quote(bid):
            q = copy.deepcopy(self.quote)
            q["depth"]["buy"][0]["price"] = bid
            return q
        pos = position()
        result = bot.paper_exit(pos,quote(106),self.now,self.cfg)
        self.assertEqual(result[1],"RUPEE_TARGET")
        pos = position()
        self.assertIsNone(bot.paper_exit(pos,quote(103),self.now,self.cfg))
        self.assertGreater(pos["current_sl"],pos["initial_sl"])
        self.assertEqual(bot.paper_exit(pos,quote(101.5),self.now,self.cfg)[1],"TRAIL_STOP")
        pos = position()
        pos["entry_epoch"] -= self.cfg.no_progress_bars*300
        self.assertEqual(bot.paper_exit(pos,quote(100),self.now,self.cfg)[1],"NO_PROGRESS")

    def test_paper_position_exits_at_1445_with_fresh_depth(self):
        cutoff=self.now.replace(hour=14,minute=45)
        plan=self.cfg.plan(100,500)
        pos={**plan,'entry_price':100,'entry_epoch':cutoff.timestamp()-60,
             'current_sl':plan['initial_sl']}
        quote=copy.deepcopy(self.quote)
        quote['received_epoch']=cutoff.timestamp()
        decision=bot.paper_exit(pos,quote,cutoff,self.cfg)
        self.assertEqual(decision[1], 'EOD_SQUAREOFF')

    def test_stock_replay_closes_at_1445_without_later_bars(self):
        stamp=self.now.replace(hour=14,minute=40,second=0,microsecond=0).timestamp()
        bars={stamp:{'open':100,'high':100.1,'low':99.9,'close':100,'strike':100}}
        result=bot.simulate_fixed_premium_trade(stamp,bars,500,self.cfg)
        self.assertEqual(result['exit_reason'], 'EOD_SQUAREOFF')
        self.assertTrue(result['exit_timestamp'].endswith('14:45:00'))
        self.assertIsNone(bot.simulate_fixed_premium_trade(stamp+300,bars,500,self.cfg))

    def test_real_delta_selects_strike_instead_of_distance(self):
        stock = {"symbol":"TEST","strike_step":10,"fno_lot_size":500,"nearest_expiry":"2026-11-26",
            "strikes_map":{"100.00":{"CE":{"security_id":"1","trading_symbol":"ATM"}},
                           "90.00":{"CE":{"security_id":"2","trading_symbol":"ITM"}}}}
        chain = {"100.000000":{"ce":{"security_id":1,"greeks":{"delta":0.2,"theta":-1,"gamma":0.01},"implied_volatility":30}},
                 "90.000000":{"ce":{"security_id":2,"greeks":{"delta":0.6,"theta":-1,"gamma":0.01},"implied_volatility":20}}}
        chosen = bot.DynamicStockOptionsUniverse.resolve_atm_option(stock,100,"CE",chain,self.cfg)
        self.assertEqual(chosen["opt_security_id"],"2")
        del chain["90.000000"]["ce"]["greeks"]
        self.assertIsNone(bot.DynamicStockOptionsUniverse.resolve_atm_option(stock,100,"CE",chain,self.cfg))

    def test_session_submits_two_sized_brackets_with_real_trail(self):
        self.run_ranked_session(False)

    def test_rejected_top_candidates_do_not_hide_next_valid_entry(self):
        self.run_ranked_session(True)

    def test_paper_entry_uses_next_valid_candidate_and_persists_position(self):
        self.run_ranked_session(True,live=False)

    def test_long_screen_refreshes_universe_between_candidates(self):
        self.run_ranked_session(False, slow_screen=True)

    def test_market_poll_interval_is_bounded_to_three_to_five_seconds(self):
        self.assertEqual(bot.DynamicStrategyConfig({}, argparse.Namespace()).poll_interval_sec, 4)
        for seconds in (3, 5):
            self.assertEqual(bot.DynamicStrategyConfig({"STOCK_OPT_POLL_INTERVAL_SEC":str(seconds)}, argparse.Namespace()).poll_interval_sec, seconds)
        for seconds in (0, 2.9, 5.1, float("nan")):
            with self.assertRaises(ValueError):
                bot.DynamicStrategyConfig({"STOCK_OPT_POLL_INTERVAL_SEC":str(seconds)}, argparse.Namespace())

    def test_market_request_hook_keeps_quote_rate_limit(self):
        from unittest.mock import MagicMock
        client = bot.DhanStockOptionsClient("test", "test", logging.getLogger("test"))
        client.before_market_request = MagicMock()
        client.last_quote_ts = 10
        with patch.object(bot.time,"monotonic",return_value=10.5), patch.object(bot.time,"sleep") as sleep:
            client._pace_quote()
        client.before_market_request.assert_called_once_with()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.55)

    def run_ranked_session(self, reject_top, live=True, slow_screen=False):
        from unittest.mock import MagicMock
        cfg = self.cfg
        cfg.initial_capital = 200000
        plan = cfg.plan(100,100)
        candidates = [{**plan, "symbol":name,"opt_type":"CE","signal_quality":90,
            "expected_net_rr":3,"eq_security_id":str(i),"opt_security_id":str(i+10),
            "conf_close":100,"atr_5m":20,"strike":100,"trading_symbol":name,
            "opt_ltp":100,"spread_pct":0.05} for i,name in enumerate(("AAA","BBB","CCC"),1)]
        client = MagicMock()
        client.fetch_batch_ohlc.return_value = {"NSE_EQ":{str(i):{"last_price":100} for i in range(1,4)}}
        client.fetch_option_quotes.return_value = {"11":{},"12":{},"13":{}}
        client.place_option_super_order.return_value = "order-id"
        universe = {str(i):{"symbol":name} for i,name in enumerate(("AAA","BBB","CCC"),1)}
        attempts = {}
        elapsed = [100.0]
        def verify(client,stock,*args,**kwargs):
            if slow_screen:
                elapsed[0] += 5
            name=stock["symbol"]
            attempts[name]=attempts.get(name,0)+1
            if reject_top and name in ("AAA","BBB") and attempts[name]>1:
                kwargs["reasons"].append("Setup changed during recheck")
                return None
            return next(x for x in candidates if x["symbol"]==name)
        with tempfile.TemporaryDirectory() as folder:
            cfg.gate_dir = folder
            with patch.object(bot,"datetime") as clock, patch.object(bot,"DhanStockOptionsClient",return_value=client), \
                 patch.object(bot.DynamicStockOptionsUniverse,"load_universe",return_value=universe), \
                 patch.object(bot,"load_dotenv_file",return_value={"DHAN_CLIENT_ID":"test","DHAN_ACCESS_TOKEN":"test"}), \
                 patch.object(bot,"load_or_init_ledger",return_value={"trades":[],"virtual_balance_net":200000}), \
                 patch.object(bot,"shortlist_movers",return_value=[(3,str(i),100) for i in range(1,4)]), \
                 patch.object(bot,"verify_live_scanx_and_resolve_stock_option",side_effect=verify), \
                 patch.object(bot,"qualify_quote",side_effect=lambda sig,*args,**kwargs: sig), \
                 patch.object(bot.time,"monotonic",side_effect=lambda: elapsed[0]), \
                 patch.object(bot.time,"sleep",side_effect=lambda *_: setattr(clock.now,"return_value",self.now.replace(day=23))), \
                 patch.object(bot,"TelegramNotifier",None):
                clock.now.return_value = self.now
                bot.run_dynamic_stock_options_session(live,False,cfg,logging.getLogger("test"))
            expected=1 if reject_top else 2
            if slow_screen:
                universe_refreshes = [call for call in client.fetch_batch_ohlc.call_args_list if call.args[0] == list(universe)]
                self.assertGreaterEqual(len(universe_refreshes), 3)
            self.assertEqual(client.place_option_super_order.call_count,expected if live else 0)
            if reject_top and live:
                self.assertEqual(client.place_option_super_order.call_args.args[0],"CCC")
            for call in client.place_option_super_order.call_args_list:
                self.assertEqual(call.args[2],plan["qty"])
                self.assertEqual(call.args[4],plan["rupee_target_price"])
                self.assertGreater(call.args[6],0)
            self.assertEqual(bot.DailyTradeGate(folder,2).count_entries("2026-10-22"),expected)
            if not live:
                saved=bot.DailyTradeGate(folder,2).read("2026-10-22",1)
                self.assertEqual(saved["details"]["position"]["symbol"],"CCC")

    def test_net_metrics_count_fee_loser_as_loss(self):
        ledger = {"initial_capital":1000, "trades":[{"gross_pnl":10, "net_pnl":-40}, {"gross_pnl":200,"net_pnl":150}]}
        bot.update_ledger_metrics(ledger)
        self.assertEqual(ledger["net_win_to_loss_ratio"], 3.75)
        self.assertEqual(ledger["virtual_balance_net"], 1110)

    def test_ledger_recovery_is_idempotent(self):
        ledger = {"initial_capital":60000, "trades":[]}
        pos = {"symbol":"TEST", "contract":"TEST_CE", "opt_type":"CE", "scanner":"TEST", "qty":500,
               "entry_price":100, "opt_1r":0.5, "entry_ts":"2026-10-22 11:00:00", "entry_id":"unique"}
        pos.update(spread_fraction_of_initial_risk=0.4, bid_room_to_initial_stop=0.3, delta=0.6)
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, "LEDGER_FILE", str(Path(folder)/"ledger.json")):
            bot.close_paper_position(ledger, pos, 103, "PREMIUM_TARGET", self.now)
            bot.close_paper_position(ledger, pos, 103, "PREMIUM_TARGET", self.now)
            self.assertEqual(len(ledger["trades"]), 1)
            self.assertEqual(ledger['trades'][0]['entry_diagnostics']['spread_fraction_of_initial_risk'],0.4)
            self.assertEqual(ledger['trades'][0]['entry_diagnostics']['delta'],0.6)


if __name__ == "__main__":
    unittest.main()

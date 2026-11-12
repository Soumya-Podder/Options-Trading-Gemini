from datetime import timedelta
from stock_option_signals import IST, evaluate_stock_setup
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
    def test_combined_loss_latches_and_excludes_sensex_and_flagged_stock(self):
        day = "2026-11-12"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stock_dir = root / "stock_option_daily_entries"
            stock_dir.mkdir()
            (stock_dir / f"{day}.slot1.json").write_text(json.dumps({
                "state": "closed", "exclude_from_daily_report": True,
                "close_details": {"entry_id": "excluded"},
            }), encoding="utf-8")

            def write_ledger(filename, trades):
                (root / filename).write_text(json.dumps({"mode": "PAPER", "trades": trades}), encoding="utf-8")

            write_ledger("paper_trade_ledger.json", [
                {"index": "NIFTY", "exit_timestamp": day + " 10:01:00", "net_pnl": -1500},
                {"index": "SENSEX", "exit_timestamp": day + " 10:02:00", "net_pnl": -2000},
            ])
            write_ledger("stock_fno_paper_ledger.json", [
                {"entry_id": "excluded", "exit_timestamp": day + " 10:00:00", "net_pnl": -1000},
                {"entry_id": "loss", "exit_timestamp": day + " 10:03:00", "net_pnl": -1100},
                {"entry_id": "late", "exit_timestamp": day + " 10:04:00", "net_pnl": 200},
            ])
            self.assertEqual(bot.combined_paper_loss_state(day, root), (-2400.0, True))
            self.assertEqual(bot.combined_paper_loss_state("2026-11-13", root), (0.0, False))
            self.assertFalse(bot.combined_paper_entry_allowed(day, root))

            write_ledger("stock_fno_paper_ledger.json", [
                {"entry_id": "prior", "exit_timestamp": day + " 10:00:00", "net_pnl": -999},
            ])
            (stock_dir / f"{day}.slot2.json").write_text(json.dumps({
                "state": "reserved", "details": {"position": {"expected_net_risk": 900}},
            }), encoding="utf-8")
            self.assertEqual(bot.combined_paper_loss_state(day, root), (-2499.0, False))
            self.assertTrue(bot.combined_paper_entry_allowed(day, root))

    def test_order_flow_counts_trade_volume_once_and_exposes_missing_coverage(self):
        import struct

        flow = bot.DhanCandleOrderFlow("test", "test", ["42"], logging.getLogger("test"))
        stamp = 1_794_371_400

        def packet(second, price, quantity, volume, bid=100, ask=101):
            data = bytearray(162)
            struct.pack_into("<BHBI", data, 0, 8, 162, 1, 42)
            struct.pack_into("<fHIfI", data, 8, price, quantity, stamp + second, 100.0, volume)
            struct.pack_into("<ff", data, 74, bid, ask)
            return bytes(data)

        flow.ingest(packet(0, 100, 10, 1000), stamp)
        flow.ingest(packet(1, 101, 10, 1010), stamp + 1)
        flow.ingest(packet(1, 101, 10, 1010), stamp + 1.2)
        flow.ingest(packet(2, 100, 5, 1015), stamp + 2)
        flow.ingest(packet(3, 101, 10, 1040), stamp + 3)
        flow.ingest(packet(2, 101, 10, 1050), stamp + 3.1)
        result = flow.snapshot("42", stamp, 60)
        self.assertEqual(result["status"], "ESTIMATED")
        self.assertEqual(result["estimated_buy"], 10)
        self.assertEqual(result["estimated_sell"], 5)
        self.assertEqual(result["estimated_delta"], 5)
        self.assertEqual(result["observed_unclassified"], 25)
        self.assertEqual(result["unclassified_or_unobserved"], 45)
        self.assertEqual(result["classified_fraction"], 0.25)
        self.assertEqual(flow.snapshot("42", stamp, 30)["status"], "VOLUME_MISMATCH")
        self.assertEqual(flow.snapshot("42", stamp - 300, 10)["status"], "UNAVAILABLE")

    def test_full_daily_cap_keeps_scanning_until_1330(self):
        from unittest.mock import MagicMock

        cfg = bot.DynamicStrategyConfig({}, argparse.Namespace())
        now = datetime(2026, 11, 11, 13, 29, tzinfo=IST)
        client, gate = MagicMock(), MagicMock()
        gate.read.return_value = {"state": "closed"}
        gate.count_entries.return_value = cfg.max_daily_trades
        stock = {"symbol": "RADICO"}
        with patch.object(bot, "datetime") as clock, \
             patch.object(bot, "DhanStockOptionsClient", return_value=client), \
             patch.object(bot.DynamicStockOptionsUniverse, "load_universe", return_value={"1": stock}), \
             patch.object(bot, "DailyTradeGate", return_value=gate), \
             patch.object(bot, "load_dotenv_file", return_value={"DHAN_CLIENT_ID": "test", "DHAN_ACCESS_TOKEN": "test"}), \
             patch.object(bot, "load_or_init_ledger", return_value={"trades": [], "virtual_balance_net": 50000}), \
             patch.object(bot, "shortlist_movers", return_value=[(-2.72, "1", 4436.6)]), \
             patch.object(bot, "verify_live_scanx_and_resolve_stock_option", return_value={"allowed": True}) as verify, \
             patch.object(bot.time, "sleep", side_effect=lambda _: setattr(clock.now, "return_value", now.replace(minute=30))), \
             patch.object(bot, "TelegramNotifier", None):
            clock.now.return_value = now
            bot.run_dynamic_stock_options_session(False, False, cfg, logging.getLogger("test"))
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], stock)
        self.assertTrue(verify.call_args.kwargs["membership_only"])
        gate.reserve.assert_not_called()

    def test_confirmed_breakout_never_bypasses_scanner_daily_volume_gate(self):
        from unittest.mock import MagicMock
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_REQUIRE_CONFIRMED_BREAKOUT':'true'},argparse.Namespace())
        client=MagicMock()
        client.fetch_intraday_candles.return_value={'timestamp':[1]}
        client.scanner_previous_session.return_value={'date':'2026-10-27','volume':19084943}
        client.fetch_batch_ohlc.return_value={'NSE_EQ':{'4684':{
            'last_price':871.65,'volume':2827356,'ohlc':{'close':851.85}}}}
        reasons=[]
        rejected={'allowed':False,'reason':'Session volume not above previous full-session volume'}
        with patch.object(bot,'scanx_stock_membership',return_value=rejected) as membership, \
             patch.object(bot,'evaluate_stock_setup') as evaluate:
            result=bot.verify_live_scanx_and_resolve_stock_option(client,
                {'symbol':'SONACOMS','eq_security_id':'4684','nearest_expiry':'2026-11-23'},871.65,2.32,cfg,reasons)
        self.assertIsNone(result)
        self.assertEqual(membership.call_args.args[3:], ())
        self.assertIn('Session volume not above previous full-session volume',reasons[0])
        evaluate.assert_not_called()
        client.fetch_option_chain.assert_not_called()

    def test_single_lot_books_base_target_unless_fresh_runner_supports_holding(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1200','STOCK_OPT_RUNNER_TARGET':'5500',
            'STOCK_OPT_STOP_PCT':'4','STOCK_OPT_TARGET_PCT':'16','STOCK_OPT_MIN_NET_RR':'1.5',
            'STOCK_OPT_EXTEND_RUPEE_TARGET':'true'},argparse.Namespace())
        plan=cfg.plan(50,500)
        self.assertEqual(plan['lots'],1)
        pos={**plan,'entry_price':50,'entry_epoch':self.now.timestamp(),
             'current_sl':plan['initial_sl'],'runner_assessment':{'hold':False}}
        def quote(bid):
            return {'received_epoch':self.now.timestamp(),'depth':{'buy':[{'price':bid,'quantity':500}]}}
        self.assertEqual(bot.paper_exit(copy.deepcopy(pos),quote(plan['rupee_target_price']),self.now,cfg)[1],'RUPEE_TARGET')
        pos['runner_assessment']={'hold':True,'assessed_epoch':self.now.timestamp()}
        self.assertIsNone(bot.paper_exit(pos,quote(plan['rupee_target_price']),self.now,cfg))
        self.assertTrue(pos['base_target_locked'])
        self.assertGreater(pos['current_sl'],50)
        locked=pos['current_sl']
        self.assertEqual(bot.paper_exit(pos,quote(locked-.05),self.now,cfg)[1],'TRAIL_STOP')
        self.assertGreaterEqual(pos['current_sl'],locked)
        self.assertEqual(bot.paper_exit(pos,quote(65),self.now,cfg)[1],'RUNNER_NET_TARGET')
        # A gap can still exceed the intended floor; the cash-loss exit wins.
        self.assertEqual(bot.paper_exit(pos,quote(40),self.now,cfg)[1],'CASH_LOSS_LIMIT')

    def test_order_flow_plan_requires_two_to_one_and_opposing_flow_books_profit(self):
        cfg = bot.DynamicStrategyConfig({
            "STOCK_OPT_RUPEE_TARGET": "900", "STOCK_OPT_STOP_PCT": "4",
            "STOCK_OPT_TARGET_PCT": "16", "STOCK_OPT_MIN_NET_RR": "1.25",
            "STOCK_OPT_EXTEND_RUPEE_TARGET": "true",
        }, argparse.Namespace())
        plan = cfg.plan(50, 500, min_net_rr=2)
        self.assertGreaterEqual(plan["expected_net_rr"], 2)
        now = self.now.timestamp()
        flow = {
            "status": "ESTIMATED", "classified_fraction": 0.75,
            "candle_start_epoch": int(now) // 300 * 300 - 300,
            "estimated_delta": -20_000, "total_candle_volume": 100_000,
        }
        pos = {
            **plan, "opt_type": "CE", "entry_price": 50, "entry_epoch": now - 600,
            "current_sl": plan["initial_sl"],
            "runner_assessment": {"hold": True, "assessed_epoch": now},
            "exit_order_flow": flow,
        }
        quote = {"received_epoch": now, "depth": {"buy": [
            {"price": plan["rupee_target_price"], "quantity": plan["qty"]},
        ]}}
        low_coverage = dict(flow, classified_fraction=0.4)
        self.assertIsNone(bot.paper_exit({**pos, "exit_order_flow": low_coverage}, quote, self.now, cfg))
        self.assertEqual(bot.paper_exit(pos, quote, self.now, cfg)[1], "ORDER_FLOW_PROFIT_EXIT")
        self.assertEqual(bot.order_flow_direction(flow), -1)
        self.assertEqual(bot.order_flow_direction(dict(flow, estimated_delta=20_000)), 1)
        self.assertEqual(bot.order_flow_direction(flow, flow["candle_start_epoch"] + 300), 0)

    def staged_position(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_STAGED_PROFIT':'true','STOCK_OPT_RUPEE_TARGET':'1600',
            'STOCK_OPT_TARGET_PCT':'14','STOCK_OPT_STOP_PCT':'4','STOCK_OPT_MAX_LOTS':'3',
            'STOCK_OPT_MAX_HOLD_BARS_5M':'10'},argparse.Namespace())
        plan=cfg.plan(18,375)
        self.assertEqual(plan['lots'],2)
        pos={**plan,'symbol':'ATHERENERG','index':'ATHERENERG','contract':'ATHERENERG 1660_CE',
             'opt_type':'CE','scanner':'CONFIRMED_BREAKOUT_CE','lot_size':375,'strike':1660,
             'entry_price':18,'entry_epoch':self.now.timestamp()-600,'entry_ts':'2026-10-22 10:50:10',
             'entry_id':'staged','slot':1,'current_sl':plan['initial_sl'],'peak_bid':20.4,
             'runner_assessment':{'hold':True,'assessed_epoch':self.now.timestamp()}}
        quote={'received_epoch':self.now.timestamp(),'depth':{'buy':[{'price':20.4,'quantity':750}],
                    'sell':[{'price':20.45,'quantity':750}]}}
        return cfg,pos,quote

    def test_partial_is_durable_once_and_final_pnl_includes_all_fills(self):
        cfg,pos,quote=self.staged_position()
        with tempfile.TemporaryDirectory() as folder:
            gate=bot.DailyTradeGate(folder,4)
            gate.reserve('2026-10-22',{'mode':'PAPER','position':pos},1)
            partial=bot.book_stock_partial(pos,quote,self.now,cfg,gate,'2026-10-22')
            self.assertEqual(partial['qty'],375)
            self.assertEqual((pos['qty'],pos['lots'],pos['original_qty'],pos['original_lots']),(375,1,750,2))
            recovered=gate.read('2026-10-22',1)['details']['position']
            self.assertIsNone(bot.book_stock_partial(recovered,quote,self.now,cfg,gate,'2026-10-22'))
            self.assertEqual(gate.count_entries('2026-10-22'),1)
            ledger={'initial_capital':50000,'trades':[]}
            with patch.object(bot,'LEDGER_FILE',str(Path(folder,'ledger.json'))):
                rec=bot.close_paper_position(ledger,recovered,23,'TIME_EXIT',self.now)
                bot.close_paper_position(ledger,recovered,23,'TIME_EXIT',self.now)
            self.assertEqual(len(ledger['trades']),1)
            self.assertEqual((rec['qty'],rec['lots'],len(rec['exit_fills'])),(750,2,2))
            expected=(partial['price']-18)*375+(23-18)*375
            self.assertAlmostEqual(rec['gross_pnl'],expected)
            single_fee=bot.compute_fno_option_charges(18,rec['exit_price'],750)
            self.assertAlmostEqual(rec['charges']-single_fee,23.60,places=2)
            self.assertAlmostEqual(ledger['virtual_balance_net'],50000+rec['net_pnl'])

    def test_partial_failure_does_not_mutate_position_and_stale_depth_cannot_book(self):
        from unittest.mock import MagicMock
        cfg,pos,quote=self.staged_position();gate=MagicMock()
        original=copy.deepcopy(pos);gate.update_position.side_effect=OSError('write failed')
        with self.assertRaises(OSError):bot.book_stock_partial(pos,quote,self.now,cfg,gate,'2026-10-22')
        self.assertEqual(pos,original)
        quote['received_epoch']-=9
        self.assertIsNone(bot.book_stock_partial(pos,quote,self.now,cfg,gate,'2026-10-22'))

    def test_staged_profit_protects_breakeven_and_keeps_partial_runner_on_pause(self):
        from unittest.mock import MagicMock
        cfg,pos,quote=self.staged_position()
        pos['peak_bid']=18+1.5*pos['trail_jump']+0.10;pos['runner_assessment']={'hold':False}
        quote['depth']['buy'][0]['price']=pos['peak_bid']
        self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))
        self.assertEqual(pos['current_sl'],pos['initial_sl'])
        pos['runner_assessment']={'hold':True,'assessed_epoch':self.now.timestamp()}
        quote['depth']['buy'][0]['price']=20.4
        partial=bot.book_stock_partial(pos,quote,self.now,cfg,MagicMock(),'2026-10-22')
        self.assertGreater(partial['booked_net'],0)
        self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))
        pos['runner_assessment']={'hold':False}
        self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))
        self.assertGreater(pos['current_sl'],pos['entry_price'])
        quote['depth']['buy'][0]['price']=pos['current_sl']
        self.assertEqual(bot.paper_exit(pos,quote,self.now,cfg)[1],'TRAIL_STOP')
        quote['depth']['buy'][0]['price']=10
        self.assertEqual(bot.paper_exit(pos,quote,self.now,cfg)[1],'CASH_LOSS_LIMIT')

    def test_extended_runner_needs_fresh_volume_and_stays_bounded(self):
        from unittest.mock import MagicMock
        cfg,pos,quote=self.staged_position()
        bot.book_stock_partial(pos,quote,self.now,cfg,MagicMock(),'2026-10-22')
        pos.update(conf_close=100,atr_5m=10,entry_contract_oi=100000,
            entry_epoch=self.now.timestamp()-3600,volume_context={'regular_average':500},
            underlying_observations=[[self.now.timestamp()-300,100,1000]])
        underlying={'last_price':120,'received_epoch':self.now.timestamp(),'volume':3000}
        chain={'1660':{'ce':{'oi':102000,'greeks':{'delta':.6,'theta':-1}}}}
        result=bot.assess_stock_runner(pos,quote,underlying,chain,self.now.timestamp(),cfg)
        self.assertTrue(result['hold'])
        pos['runner_assessment']=result
        self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))
        self.assertFalse(bot.assess_stock_runner(pos,quote,{**underlying,'volume':1100},chain,self.now.timestamp(),cfg)['hold'])
        pos['entry_epoch']=self.now.timestamp()-5400
        self.assertFalse(bot.assess_stock_runner(pos,quote,underlying,chain,self.now.timestamp(),cfg)['hold'])

    def test_fast_runner_accepts_unchanged_oi_but_requires_fresh_volume(self):
        cfg,pos,quote=self.staged_position()
        now=self.now.timestamp()
        pos.update(conf_close=100,atr_5m=10,entry_contract_oi=100000,
            entry_epoch=now-60,volume_context={'regular_average':500},
            underlying_observations=[[now-50,101,1000]])
        underlying={'last_price':120,'received_epoch':now,'volume':3000}
        chain={'1660':{'ce':{'oi':100000,'greeks':{'delta':.6,'theta':-1}}}}
        result=bot.assess_stock_runner(pos,quote,underlying,chain,now,cfg)
        self.assertTrue(result['hold'])
        self.assertEqual(result['oi_regime'],'OI_UNCHANGED')
        self.assertFalse(bot.assess_stock_runner(pos,quote,{**underlying,'volume':1000},chain,now,cfg)['hold'])
        self.assertFalse(bot.assess_stock_runner(pos,quote,{**underlying,'received_epoch':now-9},chain,now,cfg)['hold'])
        pos['entry_epoch']=now-600
        self.assertFalse(bot.assess_stock_runner(pos,quote,underlying,chain,now,cfg)['hold'])

    def test_partial_runner_without_new_assessment_still_honors_target_and_cutoff(self):
        from unittest.mock import MagicMock
        cfg,pos,quote=self.staged_position()
        bot.book_stock_partial(pos,quote,self.now,cfg,MagicMock(),'2026-10-22')
        pos['runner_assessment']={'hold':False}
        pos['runner_net_target']=2000
        quote['depth']['buy'][0]['price']=24
        self.assertEqual(bot.paper_exit(pos,quote,self.now,cfg)[1],'RUNNER_NET_TARGET')
        cfg,pos,quote=self.staged_position()
        bot.book_stock_partial(pos,quote,self.now,cfg,MagicMock(),'2026-10-22')
        pos['runner_assessment']={'hold':False}
        cutoff=self.now.replace(hour=15,minute=15)
        quote['received_epoch']=cutoff.timestamp()
        self.assertEqual(bot.paper_exit(pos,quote,cutoff,cfg)[1],'EOD_SQUAREOFF')

    def test_current_expiry_includes_today_and_cannot_be_pushed_out_by_old_dte_setting(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_MIN_DAYS_TO_EXPIRY':'4'},argparse.Namespace())
        self.assertEqual(cfg.min_days_to_expiry,0)
        sig={**self.sig,'expiry':self.now.date().isoformat()}
        self.assertIsNotNone(bot.qualify_quote(sig,self.quote,self.cfg,60000,self.now.timestamp()))
        sig['expiry']=(self.now-timedelta(days=1)).date().isoformat()
        reasons=[]
        self.assertIsNone(bot.qualify_quote(sig,self.quote,self.cfg,60000,self.now.timestamp(),reasons))
        self.assertEqual(reasons,['Contract already expired'])

    def test_universe_selects_current_expiry_without_later_month_fallback(self):
        import csv,io
        from unittest.mock import MagicMock
        today=bot.datetime.now(bot.IST).date()
        columns=['SEM_EXM_EXCH_ID','SEM_INSTRUMENT_NAME','SEM_TRADING_SYMBOL','SEM_SMST_SECURITY_ID',
                 'SEM_CUSTOM_SYMBOL','SEM_LOT_UNITS','SEM_SERIES','SEM_STRIKE_PRICE','SEM_OPTION_TYPE','SEM_EXPIRY_DATE']
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        writer.writerow({'SEM_EXM_EXCH_ID':'NSE','SEM_INSTRUMENT_NAME':'EQUITY','SEM_TRADING_SYMBOL':'ATHERENERG-EQ',
                         'SEM_SMST_SECURITY_ID':'1','SEM_CUSTOM_SYMBOL':'Ather Energy','SEM_SERIES':'EQ'})
        for days,security in ((28,'200'),(0,'100'),(-1,'90')):
            writer.writerow({'SEM_EXM_EXCH_ID':'NSE','SEM_INSTRUMENT_NAME':'OPTSTK',
                'SEM_TRADING_SYMBOL':f'ATHERENERG-{security}-1660-CE','SEM_SMST_SECURITY_ID':security,
                'SEM_LOT_UNITS':'375','SEM_STRIKE_PRICE':'1660','SEM_OPTION_TYPE':'CE',
                'SEM_EXPIRY_DATE':(today+timedelta(days=days)).isoformat()+' 15:30:00'})
        response=MagicMock();response.text=stream.getvalue()
        with patch.object(bot.os.path,'exists',return_value=False),patch.object(bot.requests,'get',return_value=response),patch.object(bot,'atomic_write_json') as save:
            stock=bot.DynamicStockOptionsUniverse.load_universe(logging.getLogger('test'))['1']
        self.assertEqual(stock['nearest_expiry'],today.isoformat())
        self.assertEqual(stock['strikes_map']['1660.00']['CE']['security_id'],'100')
        self.assertTrue(save.call_args.args[0].endswith('_dte0.json'))
        future_chain={'1660':{'ce':{'security_id':'200','greeks':{'delta':.6,'theta':-1,'gamma':.01},'implied_volatility':20}}}
        self.assertIsNone(bot.DynamicStockOptionsUniverse.resolve_atm_option(stock,1660,'CE',future_chain,self.cfg))
        with self.assertRaisesRegex(ValueError,'Current expiry required'):
            bot.DynamicStockOptionsUniverse.load_universe(logging.getLogger('test'),4)

    def test_strong_setup_can_scale_only_with_cash_depth_and_reward_support(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1600','STOCK_OPT_STOP_PCT':'4',
            'STOCK_OPT_TARGET_PCT':'20','STOCK_OPT_RUPEE_MAX_RISK':'1500',
            'STOCK_OPT_MAX_LOTS':'3'},argparse.Namespace())
        sig={**self.sig,'lot_size':100,'signal_quality':90,'higher_timeframe_aligned':True}
        for wallet,lots in ((15000,1),(25000,2),(40000,3)):
            result=bot.qualify_quote(sig,self.quote,cfg,wallet,self.now.timestamp())
            self.assertIsNotNone(result)
            self.assertEqual(result['lots'],lots)
            self.assertLessEqual(result['expected_net_risk_per_lot'],1500)
        depth=copy.deepcopy(self.quote)
        for side in ('buy','sell'):depth['depth'][side][0]['quantity']=100
        self.assertEqual(bot.qualify_quote(sig,depth,cfg,40000,self.now.timestamp())['lots'],1)
        weak={**sig,'signal_quality':70}
        self.assertEqual(bot.qualify_quote(weak,self.quote,cfg,40000,self.now.timestamp())['lots'],1)

    def test_stock_runner_keeps_base_profit_and_books_upper_target(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1600','STOCK_OPT_STOP_PCT':'4',
            'STOCK_OPT_RUPEE_MAX_RISK':'1500','STOCK_OPT_MAX_LOTS':'3'},argparse.Namespace())
        plan=cfg.plan(10,1250)
        self.assertIsNotNone(plan)
        pos={**plan,'entry_price':10,'entry_epoch':self.now.timestamp()-60,
             'current_sl':plan['initial_sl'],'runner_assessment':{'hold':True}}
        def quote(bid):
            return {'received_epoch':self.now.timestamp(),'depth':{'buy':[{'price':bid,'quantity':pos['qty']}]}}
        self.assertIsNone(bot.paper_exit(pos,quote(11),self.now,cfg))
        self.assertGreater(pos['current_sl'],10)
        self.assertEqual(bot.paper_exit(pos,quote(12.2),self.now,cfg)[1],'RUNNER_NET_TARGET')

    def test_stock_runner_rejects_stale_or_unsupportive_context(self):
        cfg=self.cfg;now=self.now.timestamp()
        pos={'entry_price':10,'entry_epoch':now-60,'qty':2500,'lots':2,'opt_type':'CE',
             'conf_close':400,'atr_5m':5,'strike':400,'entry_contract_oi':100000,'lot_size':1250,
             'underlying_observations':[[now-300,402]]}
        q={'received_epoch':now,'depth':{'buy':[{'price':11,'quantity':2500}],
                                       'sell':[{'price':11.05,'quantity':2500}]}}
        underlying={'received_epoch':now,'last_price':406}
        chain={'400':{'ce':{'oi':103000,'greeks':{'delta':0.6,'theta':-1}}}}
        self.assertTrue(bot.assess_stock_runner(pos,q,underlying,chain,now,cfg)['hold'])
        self.assertFalse(bot.assess_stock_runner(pos,q,{**underlying,'received_epoch':now-9},chain,now,cfg)['hold'])
        self.assertFalse(bot.assess_stock_runner(pos,q,{**underlying,'last_price':399},chain,now,cfg)['hold'])
        self.assertFalse(bot.assess_stock_runner(pos,q,underlying,{},now,cfg)['hold'])

    def test_cash_loss_limit_is_position_exit_not_plan_veto(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_MAX_RISK':'600',
            'STOCK_OPT_RUPEE_TARGET':'2000','STOCK_OPT_MAX_LOTS':'3'},argparse.Namespace())
        plan=cfg.plan(50,200)
        self.assertIsNotNone(plan)
        self.assertGreater(plan['lots'],1)
        self.assertLessEqual(plan['expected_net_risk'],600)
        self.assertLessEqual(plan['expected_net_risk_per_lot'],600)
        self.assertEqual(plan['total_risk_limit'],600)
        self.assertIsNotNone(cfg.plan(100,200))

    def test_cash_loss_exit_uses_total_net_loss_and_keeps_gap_fill(self):
        cfg=bot.DynamicStrategyConfig({},argparse.Namespace())
        plan=cfg.plan(100,500)
        pos={**plan,'entry_price':100,'entry_epoch':self.now.timestamp()-60,
             'current_sl':plan['initial_sl']}
        quote={'received_epoch':self.now.timestamp(),
               'depth':{'buy':[{'price':97,'quantity':plan['qty']}]}}
        decision=bot.paper_exit(pos,quote,self.now,cfg)
        self.assertEqual(decision[1],'CASH_LOSS_LIMIT')
        self.assertLess(decision[0],97)
        ts=int(self.now.timestamp())
        bars={ts:{'open':100,'high':101,'low':90,'close':95,'strike':100},
              ts+300:{'open':90,'high':91,'low':89,'close':90,'strike':100}}
        sim=bot.simulate_fixed_premium_trade(ts,bars,500,cfg)
        self.assertEqual(sim['exit_reason'],'CASH_LOSS_LIMIT')
        self.assertGreater(sim['exit_price'],95)
        bars[ts]={'open':100,'high':101,'low':99,'close':100,'strike':100}
        sim=bot.simulate_fixed_premium_trade(ts,bars,500,cfg)
        self.assertEqual(sim['exit_reason'],'CASH_LOSS_LIMIT')
        self.assertLessEqual(sim['exit_price'],90)

    def test_spread_stop_room_rejects_recorded_recltd_and_allows_tighter_depth(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_STOP_PCT':'4','STOCK_OPT_RUPEE_TARGET':'1600',
             'STOCK_OPT_EXTEND_RUPEE_TARGET':'false','STOCK_OPT_SLIPPAGE_PCT':'0.05'},argparse.Namespace())
        sig={**self.sig,'lot_size':1575,'delta':-0.59413,'theta':-0.14999,
             'gamma':0.01367,'iv':36.396,'iv_range_low':33.43,'iv_range_high':74.61,
             'opt_type':'PE','atr_5m':1.1771,'day_change_pct':-4.66}
        quote={'received_epoch':self.now.timestamp(),'depth':{
             'buy':[{'price':16.1,'quantity':1575}],
             'sell':[{'price':16.4,'quantity':1575}]}}
        reasons=[]
        self.assertIsNone(bot.qualify_quote(sig,quote,cfg,50000,self.now.timestamp(),reasons))
        self.assertEqual(reasons,['Spread consumes over 25% of effective stop distance'])
        quote['depth']['buy'][0]['price']=16.25
        self.assertIsNotNone(bot.qualify_quote(sig,quote,cfg,50000,self.now.timestamp()))

    def test_fresh_momentum_rejects_tata_fade_symmetrically_without_lookahead(self):
        from stock_option_signals import recent_entry_momentum
        start=int(self.now.replace(hour=11,minute=35,second=0).timestamp())
        rows=[(start+120,986.75,990.8,986.65,990.25,87781),
              (start+180,990.35,990.35,988,988.2,15330),
              (start+240,987.7,1000,987.1,999,7710)]
        fields=('timestamp','open','high','low','close','volume')
        for side in ('CE','PE'):
            mirrored=[r if side=='CE' else (r[0],2000-r[1],2000-r[3],2000-r[2],2000-r[4],r[5]) for r in rows]
            candles={k:[r[i] for r in mirrored] for i,k in enumerate(fields)}
            sig={'opt_type':side,'atr_5m':2.7864,'signal_timestamp':start-300}
            spot=988.25 if side=='CE' else 2000-988.25
            result=recent_entry_momentum(candles,sig,spot,start+294)
            self.assertFalse(result['allowed'])
            self.assertEqual(result['reason'],'Recent breakout fading after confirmation')
            # The large favorable forming candle must not rescue the entry.
            self.assertEqual(result['last_closed_minute'],start+180)
            self.assertFalse(recent_entry_momentum(None,sig,spot,start+294)['allowed'])
            held=990 if side=='CE' else 1010
            self.assertTrue(recent_entry_momentum(candles,sig,held,start+294)['allowed'])
            self.assertFalse(recent_entry_momentum(candles,sig,spot,start+600)['allowed'])

    def test_cash_stop_target_uses_effective_risk_before_rr_sizing(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1600','STOCK_OPT_STOP_PCT':'5',
            'STOCK_OPT_EXTEND_RUPEE_TARGET':'true','STOCK_OPT_RUPEE_MAX_RISK':'1500'},argparse.Namespace())
        plan=cfg.plan(100,500)
        self.assertIsNotNone(plan)
        self.assertGreater(plan['planned_premium_stop_risk'],1500)
        self.assertAlmostEqual(plan['net_profit_target'],1950)
        self.assertGreaterEqual(plan['expected_net_reward'],1950)
        self.assertLess(plan['expected_net_reward'],2050)
        self.assertLess(plan['rupee_target_price'],105)

    def test_requested_hold_profile_allows_consolidation_before_time_exits(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_STOP_PCT':'7','STOCK_OPT_TARGET_PCT':'14',
            'STOCK_OPT_MIN_NET_RR':'1.4','STOCK_OPT_SLIPPAGE_PCT':'0.05',
            'STOCK_OPT_RUPEE_TARGET':'1250','STOCK_OPT_RUPEE_MAX_RISK':'1600',
            'STOCK_OPT_MAX_LOTS':'2','STOCK_OPT_NO_PROGRESS_BARS':'8',
            'STOCK_OPT_MAX_HOLD_BARS_5M':'16'},argparse.Namespace())
        plan=cfg.plan(100,100)
        self.assertIsNotNone(plan)
        quote=copy.deepcopy(self.quote)
        quote['depth']['buy'][0]['price']=100
        for elapsed in (15,39):
            pos={**plan,'entry_price':100,'entry_epoch':self.now.timestamp()-elapsed*60,
                 'current_sl':plan['initial_sl']}
            self.assertIsNone(bot.paper_exit(pos,quote,self.now,cfg))
        pos={**plan,'entry_price':100,'entry_epoch':self.now.timestamp()-40*60,
             'current_sl':plan['initial_sl']}
        self.assertEqual(bot.paper_exit(pos,quote,self.now,cfg)[1],'NO_PROGRESS')
        pos={**plan,'entry_price':100,'entry_epoch':self.now.timestamp()-80*60,
             'current_sl':plan['initial_sl'],'peak_bid':100+plan['trail_jump']}
        self.assertEqual(bot.paper_exit(pos,quote,self.now,cfg)[1],'TIME_EXIT')

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
        stock={"symbol":"TEST","eq_security_id":"1","nearest_expiry":"2026-11-26"}
        client=MagicMock()
        client.fetch_intraday_candles.return_value={"timestamp":[1]}
        for side,direction in (("CE",1),("PE",-1)):
            for advance,allowed in ((-0.21,False),(-0.2,True),(0,True),(0.7,True),(1.4,True),(1.41,False)):
                with self.subTest(side=side,advance=advance):
                    client.fetch_batch_ohlc.return_value={"NSE_EQ":{"1":{"last_price":100+direction*advance,"received_epoch":bot.time.time()}}}
                    sig={"opt_type":side,"conf_close":100,"atr_5m":2}
                    with patch.object(bot,"evaluate_stock_setup",return_value=sig), \
                         patch.object(bot,"scanx_stock_membership",return_value={'allowed':True,'opt_type':side,'previous_close':100}), \
                         patch.object(bot.DynamicStockOptionsUniverse,"resolve_atm_option",return_value={"opt_security_id":"9"}):
                        result=bot.verify_live_scanx_and_resolve_stock_option(client,stock,100,3,self.cfg)
                    self.assertEqual(result is not None,allowed)

    def test_final_underlying_refresh_rejects_move_during_option_selection(self):
        from unittest.mock import MagicMock
        stock={'eq_security_id':'1','nearest_expiry':'2026-11-26','symbol':'TEST'}
        for final_price,age,allowed in ((100,0,True),(99.5,0,False),(100,9,False)):
            with self.subTest(final_price=final_price,age=age):
                client=MagicMock()
                client.fetch_intraday_candles.return_value={'timestamp':[1]}
                client.fetch_batch_ohlc.side_effect=[
                    {'NSE_EQ':{'1':{'last_price':100,'received_epoch':bot.time.time()}}},
                    {'NSE_EQ':{'1':{'last_price':final_price,'received_epoch':bot.time.time()-age}}}]
                sig={'opt_type':'CE','conf_close':100,'atr_5m':2}
                reasons=[]
                with patch.object(bot,'evaluate_stock_setup',return_value=sig), \
                     patch.object(bot,'scanx_stock_membership',return_value={'allowed':True,'opt_type':'CE','previous_close':100}), \
                     patch.object(bot.DynamicStockOptionsUniverse,'resolve_atm_option',return_value={'opt_security_id':'9'}):
                    result=bot.verify_live_scanx_and_resolve_stock_option(client,stock,100,3,self.cfg,reasons)
                self.assertEqual(result is not None,allowed)
                if not allowed:self.assertTrue(reasons)

    def test_structural_entry_band_holds_breakout_without_chasing(self):
        from unittest.mock import MagicMock
        for side,direction in (('CE',1),('PE',-1)):
            for advance,allowed in ((-0.4,True),(-0.51,False),(1.4,True),(1.41,False),(-1.1,False)):
                with self.subTest(side=side,advance=advance):
                    client=MagicMock()
                    client.fetch_intraday_candles.return_value={'timestamp':[1]}
                    client.fetch_batch_ohlc.return_value={'NSE_EQ':{'1':{
                        'last_price':100+direction*advance,'received_epoch':bot.time.time()}}}
                    sig={'opt_type':side,'conf_close':100,'conf_low':98,'conf_high':102,'atr_5m':2,
                         'signal_timestamp':bot.time.time()-310,
                         'breakout_confirmation':{'trigger_level':100-direction,'buffer':0.1,
                            'closed_candle':True,'entry_path':'DIRECT_BREAKOUT'}}
                    with patch.object(bot,'evaluate_stock_setup',return_value=sig), \
                         patch.object(bot,'scanx_stock_membership',return_value={'allowed':True,'opt_type':side,'previous_close':100}), \
                         patch.object(bot.DynamicStockOptionsUniverse,'resolve_atm_option',return_value={'opt_security_id':'9'}):
                        result=bot.verify_live_scanx_and_resolve_stock_option(client,
                            {'eq_security_id':'1','nearest_expiry':'2026-11-26','symbol':'TEST'},100,3,self.cfg)
                    self.assertEqual(result is not None,allowed)

    def test_final_selection_rechecks_scanner_and_signal_expiry(self):
        from unittest.mock import MagicMock
        stock={'eq_security_id':'1','nearest_expiry':'2026-11-26','symbol':'TEST'}
        for failure in ('scanner','expiry'):
            with self.subTest(failure=failure):
                client=MagicMock()
                client.fetch_intraday_candles.return_value={'timestamp':[1]}
                client.fetch_batch_ohlc.return_value={'NSE_EQ':{'1':{
                    'last_price':100,'received_epoch':bot.time.time()}}}
                sig={'opt_type':'CE','conf_close':100,'atr_5m':2,'signal_timestamp':bot.time.time()-310}
                def resolve(*args):
                    if failure=='expiry':sig['signal_timestamp']=bot.time.time()-300-self.cfg.confirm_window_sec-1
                    return {'opt_security_id':'9'}
                passed={'allowed':True,'opt_type':'CE','previous_close':100}
                final={'allowed':False,'reason':'Session volume not above previous full-session volume'}
                reasons=[]
                with patch.object(bot,'evaluate_stock_setup',return_value=sig), \
                     patch.object(bot,'scanx_stock_membership',side_effect=[passed,final]), \
                     patch.object(bot.DynamicStockOptionsUniverse,'resolve_atm_option',side_effect=resolve):
                    result=bot.verify_live_scanx_and_resolve_stock_option(client,stock,100,3,self.cfg,reasons)
                self.assertIsNone(result)
                self.assertIn('Final scanner membership' if failure=='scanner' else 'expired',reasons[-1])

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

    def test_shortlist_does_not_drop_stocks_beyond_eight_each_side(self):
        quotes = {str(i): {"last_price": 103+i, "ohlc":{"close":100}} for i in range(10)}
        quotes.update({str(i+20): {"last_price": 97-i, "ohlc":{"close":100}} for i in range(10)})
        result = bot.shortlist_movers(quotes, self.cfg)
        self.assertEqual(len(result), 20)
        self.assertEqual(sum(x[0]>0 for x in result), 10)

    def test_environment_limits_are_validated(self):
        with self.assertRaises(ValueError):
            bot.DynamicStrategyConfig({"STOCK_OPT_MAX_DAILY_TRADES":"5"}, argparse.Namespace())
        cfg = bot.DynamicStrategyConfig({"STOCK_OPT_MAX_DAILY_TRADES":"1", "STOCK_OPT_MAX_CONCURRENT":"1"}, argparse.Namespace())
        self.assertEqual((cfg.max_daily_trades,cfg.max_concurrent), (1,1))

    def test_confirmation_window_boundaries_and_validation(self):
        for age, allowed in ((-1,False),(0,True),(299,True),(300,False),(599,False)):
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
        reasons=[]
        self.assertIsNone(bot.qualify_quote(sig,quote,cfg,60000,self.now.timestamp(),reasons))
        self.assertEqual(reasons,['Spread consumes over 25% of effective stop distance'])
        quote['depth']['buy'][0]['price']=98.9
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
            sig={**self.sig,'lot_size':100,'day_change_pct':6,'atr_5m':7.2}
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
                       {"expiry":"2026-10-21"}, {"greeks_timestamp":self.now.timestamp()-121}):
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
        self.cfg.max_daily_trades=2
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
        def close_position(*args):
            clock.now.return_value = self.now.replace(hour=13, minute=30)
            return {"net_pnl": 0}

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
             patch.object(bot,"close_paper_position",side_effect=close_position), \
             patch.object(bot,"TelegramNotifier",None), patch.object(bot.time,"sleep"):
            clock.now.return_value=self.now
            bot.run_dynamic_stock_options_session(False,False,self.cfg,logging.getLogger("test"))
        self.assertEqual(len(calls),3)
        gate.update_position.assert_called_once_with("2026-10-22",1,pos)
        gate.mark_closed.assert_called_once()
        self.assertEqual(pos["persisted_state"],(99,101))

    def test_detailed_rejection_reason_and_qualified_metrics(self):
        self.assertEqual(self.cfg.min_days_to_expiry,0)
        reasons=[]
        result=bot.qualify_quote(self.sig,self.quote,self.cfg,60000,self.now.timestamp(),reasons)
        self.assertIsNotNone(result)
        self.assertEqual(reasons,[])
        self.assertEqual(result["dte"],35)
        self.assertAlmostEqual(result["theta_pct_per_day"],1)
        self.assertGreaterEqual(result["expected_move_gain"],self.cfg.rupee_target)
        for edits,expected in (({"expiry":"2026-10-21"},"Contract already expired"),
                               ({"theta":-7},"Theta 7.00%/day above 6.00%"),
                               ({"atr_5m":0.1},"A 1.5 ATR move nets only Rs."),
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
                         (12,5,900,1500,2))
        self.assertIsNotNone(cfg.plan(100,500))
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
        self.assertEqual(bot.paper_exit(pos,q,self.now,self.cfg)[1],'RUPEE_TARGET')
        self.assertGreater(pos['current_sl'],99)
        protected=pos['current_sl']
        q["received_epoch"]-=31
        self.assertIsNone(bot.paper_exit(pos,q,self.now,self.cfg))
        self.assertEqual(pos['current_sl'],protected)

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
        self.assertEqual(result[1],'RUPEE_TARGET')
        self.assertGreaterEqual(bot.stock_position_value(pos,result[0])['net'],plan['net_profit_target'])
        pos = position()
        self.assertIsNone(bot.paper_exit(pos,quote(100+2.2*plan['trail_jump']+0.05),self.now,self.cfg))
        self.assertGreater(pos['current_sl'],pos['initial_sl'])
        arm=bot.stock_net_protection_bid(pos,plan['rupee_target_price'],0.86*plan['net_profit_target'],self.cfg)
        self.assertIsNone(bot.paper_exit(pos,quote(arm),self.now,self.cfg))
        self.assertGreater(pos['current_sl'],pos['initial_sl'])
        self.assertEqual(bot.paper_exit(pos,quote(pos['current_sl']-0.05),self.now,self.cfg)[1],"TRAIL_STOP")
        pos = position()
        pos["entry_epoch"] -= self.cfg.no_progress_bars*300
        self.assertEqual(bot.paper_exit(pos,quote(100),self.now,self.cfg)[1],"NO_PROGRESS")

    def test_paper_position_exits_at_1515_with_fresh_depth(self):
        cutoff=self.now.replace(hour=15,minute=15)
        plan=self.cfg.plan(100,500)
        pos={**plan,'entry_price':100,'entry_epoch':cutoff.timestamp()-60,
             'current_sl':plan['initial_sl']}
        quote=copy.deepcopy(self.quote)
        quote['received_epoch']=cutoff.timestamp()
        decision=bot.paper_exit(pos,quote,cutoff,self.cfg)
        self.assertEqual(decision[1], 'EOD_SQUAREOFF')

    def test_stock_replay_closes_at_1515_without_later_bars(self):
        stamp=self.now.replace(hour=15,minute=10,second=0,microsecond=0).timestamp()
        bars={stamp:{'open':100,'high':100.1,'low':99.9,'close':100,'strike':100}}
        result=bot.simulate_fixed_premium_trade(stamp,bars,500,self.cfg)
        self.assertEqual(result['exit_reason'], 'EOD_SQUAREOFF')
        self.assertTrue(result['exit_timestamp'].endswith('15:15:00'))
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

    def test_live_session_fails_before_any_network_or_ledger_mutation(self):
        with patch.object(bot,"DhanStockOptionsClient") as client, self.assertRaisesRegex(RuntimeError,"LIVE disabled"):
            bot.run_dynamic_stock_options_session(True,False,self.cfg,logging.getLogger("test"))
        client.assert_not_called()

    def test_rejected_top_candidates_do_not_hide_next_valid_entry(self):
        self.run_ranked_session(True,live=False)

    def test_paper_entry_uses_next_valid_candidate_and_persists_position(self):
        self.run_ranked_session(True,live=False)

    def test_long_screen_refreshes_universe_between_candidates(self):
        self.run_ranked_session(True,live=False,slow_screen=True)

    def test_live_selection_compares_candidates_before_first_entry(self):
        self.run_ranked_session(False,live=False,prefer_last=True)

    def test_final_recheck_uses_alternate_strike_when_preferred_spread_widens(self):
        self.run_ranked_session(False, live=False, prefer_last=True, use_final_alternate=True)

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

    def run_ranked_session(self, reject_top, live=True, slow_screen=False, prefer_last=False, use_final_alternate=False):
        from unittest.mock import MagicMock
        cfg = self.cfg
        cfg.initial_capital = 200000
        plan = cfg.plan(100,100)
        candidates = [{**plan, "symbol":name,"opt_type":"CE","signal_quality":90,
            "expected_net_rr":3,"eq_security_id":str(i),"opt_security_id":str(i+10),
            "conf_close":100,"atr_5m":20,"strike":100,"trading_symbol":name,
            "opt_ltp":100,"spread_pct":0.05} for i,name in enumerate(("AAA","BBB","CCC"),1)]
        if prefer_last:
            for candidate,quality in zip(candidates,(70,80,99)):candidate['signal_quality']=quality
        client = MagicMock()
        client.fetch_batch_ohlc.return_value = {"NSE_EQ":{str(i):{"last_price":100} for i in range(1,4)}}
        client.fetch_option_quotes.return_value = {"11":{},"12":{},"13":{}}
        if use_final_alternate:
            candidates[-1]["alternate_contracts"] = [{"opt_security_id":"23", "strike":105, "trading_symbol":"CCC105CE"}]
            client.fetch_option_quotes.return_value["23"] = {}
        client.place_option_super_order.return_value = "order-id"
        universe = {str(i):{"symbol":name} for i,name in enumerate(("AAA","BBB","CCC"),1)}
        attempts = {}
        screening_order = []
        elapsed = [100.0]
        def verify(client,stock,*args,**kwargs):
            if slow_screen:
                elapsed[0] += 5
            name=stock["symbol"]
            screening_order.append(name)
            attempts[name]=attempts.get(name,0)+1
            if reject_top and name in ("AAA","BBB") and attempts[name]>1:
                kwargs["reasons"].append("Setup changed during recheck")
                return None
            return next(x for x in candidates if x["symbol"]==name)
        def qualify(candidate, *_args, reasons=None):
            if use_final_alternate and candidate["opt_security_id"] == "13" and attempts.get("CCC",0) > 1:
                reasons.append("Spread consumes over 25% of effective stop distance")
                return None
            return candidate
        with tempfile.TemporaryDirectory() as folder:
            cfg.gate_dir = folder
            with patch.object(bot,"datetime") as clock, patch.object(bot,"DhanStockOptionsClient",return_value=client), \
                 patch.object(bot.DynamicStockOptionsUniverse,"load_universe",return_value=universe), \
                 patch.object(bot,"load_dotenv_file",return_value={"DHAN_CLIENT_ID":"test","DHAN_ACCESS_TOKEN":"test"}), \
                 patch.object(bot,"load_or_init_ledger",return_value={"trades":[],"virtual_balance_net":200000}), \
                 patch.object(bot,"shortlist_movers",return_value=[(3,str(i),100) for i in range(1,4)]), \
                 patch.object(bot,"verify_live_scanx_and_resolve_stock_option",side_effect=verify), \
                 patch.object(bot,"qualify_quote",side_effect=qualify), \
                 patch.object(bot.time,"monotonic",side_effect=lambda: elapsed[0]), \
                 patch.object(bot.time,"sleep",side_effect=lambda *_: setattr(clock.now,"return_value",self.now.replace(day=23))), \
                 patch.object(bot,"TelegramNotifier",None):
                clock.now.return_value = self.now
                bot.run_dynamic_stock_options_session(live,False,cfg,logging.getLogger("test"))
            expected=1 if reject_top else 2
            # Compare a bounded group, then rebuild each selected entry.
            self.assertEqual(screening_order[:3],["AAA","BBB","CCC"])
            if reject_top:
                self.assertEqual(screening_order[:6],["AAA","BBB","CCC","AAA","BBB","CCC"])
            if prefer_last:
                self.assertEqual(screening_order[:4],["AAA","BBB","CCC","CCC"])
                saved_best=bot.DailyTradeGate(folder,2).read('2026-10-22',1)['details']['position']
                self.assertEqual(saved_best['symbol'],'CCC')
                self.assertEqual(len(saved_best['selection_comparison']),3)
                self.assertEqual(saved_best['selection_comparison'][0]['symbol'],'CCC')
                if use_final_alternate:
                    self.assertEqual(saved_best['opt_security_id'], '23')
                    self.assertEqual(saved_best['strike'], 105)
                    self.assertGreaterEqual(client.fetch_option_quotes.call_args_list.count(unittest.mock.call(["13","23"])), 2)
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

    def test_consecutive_loss_breaker_uses_exit_order_for_concurrent_trades(self):
        def trade(hour,net):return {'timestamp':'2026-10-22 09:30:00','exit_timestamp':f'2026-10-22 {hour}:00:00','net_pnl':net}
        self.assertIsNone(bot.stock_daily_risk_reason([trade('12',300),trade('10',-100),trade('11',-100)],[],self.cfg,'2026-10-22'))
        self.assertIn('Consecutive',bot.stock_daily_risk_reason([trade('12',-100),trade('10',300),trade('11',-100)],[],self.cfg,'2026-10-22'))

    def test_daily_risk_counts_excluded_executions_and_open_risk(self):
        gate=bot.DailyTradeGate(self.cfg.gate_dir,self.cfg.max_daily_trades)
        trades=[{'timestamp':'2026-10-22 10:00:00','net_pnl':-1000},
                {'timestamp':'2026-10-22 11:00:00','net_pnl':-1000}]
        self.assertIn('Consecutive',bot.stock_daily_risk_reason(trades,[],self.cfg,'2026-10-22'))
        self.assertIn('Daily loss',bot.stock_daily_risk_reason(trades[:1],[{'expected_net_risk':1500}],self.cfg,'2026-10-22',600))
        with tempfile.TemporaryDirectory() as folder:
            gate=bot.DailyTradeGate(folder,2)
            gate.reserve('2026-10-22',{'mode':'PAPER'},1)
            trade={**trades[0],'entry_id':'excluded','exit_timestamp':'2026-10-22 10:05:00'}
            gate.mark_closed('2026-10-22',trade,1)
            outcomes=bot.executed_stock_outcomes({'trades':[]},gate)
            self.assertEqual(outcomes,[trade])

    def test_partial_option_path_does_not_invent_time_exit(self):
        ts,bars=self.bars()
        self.assertIsNone(bot.simulate_fixed_premium_trade(ts,{ts:bars[ts]},500,self.cfg))

    def test_quote_observation_updates_market_prices_without_changing_entry(self):
        pos={'entry_price':38.85,'current_ltp':38.85}
        quote={'received_epoch':100,'last_price':38.5,'depth':{'buy':[{'price':38.4}],'sell':[{'price':38.7}]}}
        self.assertTrue(bot.update_position_quote(pos,quote,101))
        self.assertEqual(pos['entry_price'],38.85)
        self.assertEqual(pos['current_ltp'],38.5)
        self.assertEqual(pos['current_bid'],38.4)
        self.assertEqual(pos['quote_received_epoch'],100)
        old=copy.deepcopy(pos)
        self.assertFalse(bot.update_position_quote(pos,{**quote,'received_epoch':1},101))
        self.assertEqual(pos,old)
        self.assertFalse(bot.update_position_quote(pos,{**quote,'received_epoch':102},101))
        del quote['last_price']
        self.assertTrue(bot.update_position_quote(pos,quote,101))
        self.assertIsNone(pos['current_ltp'])
        self.assertEqual(pos['current_bid'],38.4)

    def test_runner_requires_recent_continuation(self):
        cfg=self.cfg;now=self.now.timestamp()
        pos={'entry_price':10,'entry_epoch':now-600,'qty':2500,'opt_type':'CE',
             'conf_close':400,'atr_5m':5,'strike':400,'entry_contract_oi':100000,'lot_size':1250,
             'underlying_observations':[[now-300,406]]}
        q={'received_epoch':now,'depth':{'buy':[{'price':11,'quantity':2500}],
             'sell':[{'price':11.05,'quantity':2500}]}}
        underlying={'received_epoch':now,'last_price':406}
        chain={'400':{'ce':{'oi':103000,'greeks':{'delta':0.6,'theta':-1}}}}
        self.assertFalse(bot.assess_stock_runner(pos,q,underlying,chain,now,cfg)['hold'])

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


def confirmed_bullish_fixture():
    candles = {key: [] for key in ("timestamp", "open", "high", "low", "close", "volume")}

    def add(dt, opening, high, low, close, volume=1000):
        values = (int(dt.timestamp()), opening, high, low, close, volume)
        for field, value in zip(candles, values):
            candles[field].append(value)

    previous_open = datetime(2026, 10, 21, 9, 15, tzinfo=IST)
    for index in range(75):
        add(previous_open + timedelta(minutes=index * 5), 100, 100.4, 99.6, 100)
    today_open = datetime(2026, 10, 22, 9, 15, tzinfo=IST)
    for index in range(36):
        if index < 3:
            add(today_open + timedelta(minutes=index * 5), 102.4, 103, 101.8, 102.5)
        else:
            add(today_open + timedelta(minutes=index * 5), 102.5, 102.95, 102.15, 102.6)
    add(today_open + timedelta(minutes=180), 102.65, 103.1, 102.55, 103.04)
    add(today_open + timedelta(minutes=185), 103.05, 103.09, 102.99, 103.03)
    add(today_open + timedelta(minutes=190), 103.02, 103.28, 103.0, 103.24, 2000)
    return candles, candles["timestamp"][-1] + 300


class StockSetupTests(unittest.TestCase):
    def setUp(self):
        self.candles, self.as_of = confirmed_bullish_fixture()

    def opening_spike_fixture(self):
        data={k:v[:75] for k,v in self.candles.items()}
        start=datetime(2026,10,22,9,15,tzinfo=IST)
        rows=((102.4,103,101.8,102.5,4000),(102.5,103.1,102.3,102.8,1800),
              (102.8,103,102.5,102.7,900),(102.7,103.1,102.6,103,1280),
              (103,103.15,102.8,103.1,1190),(103.1,103.2,103,103.15,970),
              (103.15,103.19,102.95,103,450),(103,103.28,102.98,103.26,750))
        for i,row in enumerate(rows):
            for k,v in zip(data,((start+timedelta(minutes=5*i)).timestamp(),*row)):data[k].append(v)
        return data,data['timestamp'][-1]+300

    def test_opening_spike_can_use_strong_rising_volume_continuation(self):
        data,now=self.opening_spike_fixture()
        for bearish in (False,True):
            candidate=copy.deepcopy(data)
            if bearish:
                for field,opposite in (('open','open'),('high','low'),('low','high'),('close','close')):
                    candidate[field]=[200-v for v in data[opposite]]
            with patch('stock_option_signals._ema',return_value=[97.5,97.2] if bearish else [102.5,102.8]),patch('stock_option_signals._atr',return_value=[1]):
                result=evaluate_stock_setup(candidate,now)
            self.assertIsNotNone(result)
            self.assertEqual(result['opt_type'],'PE' if bearish else 'CE')
            self.assertEqual(result['volume_confirmation'],'OPENING_SPIKE_ADJUSTED_STRONG_CONTINUATION')
            self.assertLess(result['relative_volume'],1.2)
            self.assertEqual(result['volume_context']['post_opening_median'],970)
            self.assertEqual(result['volume_context']['required_volume'],727.5)

    def test_opening_adjustment_does_not_admit_thin_or_weak_candles(self):
        original,now=self.opening_spike_fixture()
        for change in ('thin','zero','not_rising','no_opening_spike','weak','no_break','gap'):
            with self.subTest(change=change):
                data=copy.deepcopy(original)
                if change=='thin':data['volume'][-1]=600
                elif change=='zero':data['volume'][-1]=0
                elif change=='not_rising':data['volume'][-2]=900
                elif change=='no_opening_spike':data['volume'][75:78]=[1000]*3
                elif change=='weak':data['open'][-1]=103.23
                elif change=='no_break':data['high'][-2]=103.27
                elif change=='gap':
                    for values in data.values():del values[-3]
                with patch('stock_option_signals._ema',return_value=[102.5,102.8]),patch('stock_option_signals._atr',return_value=[1]):
                    self.assertIsNone(evaluate_stock_setup(data,now))

    def test_opening_adjustment_preserves_vwap_and_extension_guards(self):
        original,now=self.opening_spike_fixture()
        for average in ([104,104.1],[99,99.1]):
            with patch('stock_option_signals._ema',return_value=average),patch('stock_option_signals._atr',return_value=[1]):
                self.assertIsNone(evaluate_stock_setup(original,now))
        data=copy.deepcopy(original)
        for field,value in (('open',105),('high',106),('low',104),('close',105.5)):
            data[field][75:78]=[value]*3
        with patch('stock_option_signals._ema',return_value=[102.5,102.8]),patch('stock_option_signals._atr',return_value=[1]):
            self.assertIsNone(evaluate_stock_setup(data,now))

    def test_opening_adjustment_never_reads_forming_candle(self):
        data,now=self.opening_spike_fixture()
        with patch('stock_option_signals._ema',return_value=[102.5,102.8]),patch('stock_option_signals._atr',return_value=[1]):
            expected=evaluate_stock_setup(data,now)
            for k,v in zip(data,(now,103.3,200,50,150,1000000)):data[k].append(v)
            self.assertEqual(evaluate_stock_setup(data,now+60),expected)

    def test_bullish_confirmation(self):
        result = evaluate_stock_setup(self.candles, self.as_of)
        self.assertIsNotNone(result)
        self.assertEqual(result["opt_type"], "CE")
        self.assertEqual(result["signal_timestamp"], self.as_of - 300)
        self.assertAlmostEqual(result["day_change_pct"], 3.24)
        self.assertEqual(result["orb_high"], 103)
        self.assertEqual(result["orb_low"], 101.8)
        self.assertEqual(result["spot_ltp"], 103.24)
        self.assertAlmostEqual(result["relative_volume"], 2.0)
        self.assertGreater(result["signal_quality"], 0)
        self.assertLessEqual(result["signal_quality"], 100)

    def test_bearish_confirmation(self):
        original = copy.deepcopy(self.candles)
        for field, counterpart in (("open", "open"), ("high", "low"), ("low", "high"), ("close", "close")):
            self.candles[field] = [200 - value for value in original[counterpart]]
        result = evaluate_stock_setup(self.candles, self.as_of)
        self.assertIsNotNone(result)
        self.assertEqual(result["opt_type"], "PE")
        self.assertAlmostEqual(result["day_change_pct"], -3.24)

    def test_bullish_and_bearish_require_vwap_alignment(self):
        # Concentrate early session volume above the later bullish close.
        data=copy.deepcopy(self.candles)
        for i in range(75,78):
            for field,value in (("open",104),("high",105),("low",103.5),("close",104.5),("volume",1000000)):
                data[field][i]=value
        self.assertIsNotNone(evaluate_stock_setup(self.candles,self.as_of))
        self.assertIsNone(evaluate_stock_setup(data,self.as_of))
        original=copy.deepcopy(data)
        for field,counterpart in (("open","open"),("high","low"),("low","high"),("close","close")):
            data[field]=[200-value for value in original[counterpart]]
        self.assertIsNone(evaluate_stock_setup(data,self.as_of))

    def test_retest_is_optional_ranking_bonus(self):
        for low, close in ((102.5, 103.03), (103.13, 103.15), (102.99, 102.99)):
            with self.subTest(low=low, close=close):
                data = copy.deepcopy(self.candles)
                data["low"][-2] = low
                data["close"][-2] = close
                data["open"][-2] = max(data["open"][-2], low)
                data["high"][-2] = max(data["high"][-2], close + 0.01)
                result = evaluate_stock_setup(data, self.as_of)
                self.assertIsNotNone(result)
                self.assertLess(result["signal_quality"], evaluate_stock_setup(self.candles, self.as_of)["signal_quality"])

    def test_confirmation_requires_volume_expansion(self):
        self.candles["volume"][-1] = 1199
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_zero_volume_and_weak_body_rejected(self):
        for field, value in (("volume", 0), ("open", 103.2), ("close", 103.08)):
            with self.subTest(field=field):
                data = copy.deepcopy(self.candles)
                data[field][-1] = value
                self.assertIsNone(evaluate_stock_setup(data, self.as_of))

    def test_future_and_forming_bars_cannot_change_signal(self):
        expected = evaluate_stock_setup(self.candles, self.as_of)
        # This large future bar would destroy both trend and VWAP if read early.
        for field, value in (("timestamp", self.as_of), ("open", 200), ("high", 400),
                             ("low", 100), ("close", 300), ("volume", 1000000)):
            self.candles[field].append(value)
        self.assertEqual(evaluate_stock_setup(self.candles, self.as_of + 60), expected)

    def test_confirmation_cannot_be_used_before_close(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of - 1))

    def test_configured_freshness_window(self):
        self.assertIsNotNone(evaluate_stock_setup(self.candles,self.as_of+599,confirm_window_sec=600))
        self.assertIsNone(evaluate_stock_setup(self.candles,self.as_of+300,confirm_window_sec=300))
        self.assertIsNone(evaluate_stock_setup(self.candles,self.as_of,confirm_window_sec=901))

    def test_stale_confirmation_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of + 600))

    def test_previous_day_signal_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of + 86400))

    def test_gapped_pattern_rejected(self):
        for field in self.candles:
            del self.candles[field][-2]
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_missing_opening_range_does_not_block_momentum(self):
        for field in self.candles:
            del self.candles[field][76]
        self.assertIsNotNone(evaluate_stock_setup(self.candles, self.as_of))

    def test_prior_session_close_is_required(self):
        data = {field: values[75:] for field, values in self.candles.items()}
        self.assertIsNone(evaluate_stock_setup(data, self.as_of))

    def test_insufficient_15m_history_rejected(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of, ema_period=100))

    def test_old_sparse_history_does_not_veto_fresh_confirmation(self):
        for field in self.candles:
            del self.candles[field][1]
        self.assertIsNotNone(evaluate_stock_setup(self.candles,self.as_of))

    def test_partial_current_15m_bucket_is_not_aggregated(self):
        from stock_option_signals import _completed_15m_bars, _closed_bars
        bars=_closed_bars(self.candles,self.as_of)
        bars.append({**bars[-1],"timestamp":bars[-1]["timestamp"]+300})
        aggregates=_completed_15m_bars(bars)
        self.assertTrue(all(x["timestamp"]+900 <= bars[-1]["timestamp"]+300 for x in aggregates))

    def test_threshold_applies_to_closed_day_move(self):
        self.assertIsNone(evaluate_stock_setup(self.candles, self.as_of, min_day_change_pct=4))

    def test_malformed_data_fails_closed(self):
        for corrupt in (lambda data: data["volume"].pop(),
                        lambda data: data["close"].__setitem__(-1, float("nan")),
                        lambda data: data["low"].__setitem__(-1, 1000)):
            with self.subTest(corrupt=corrupt):
                data = copy.deepcopy(self.candles)
                corrupt(data)
                self.assertIsNone(evaluate_stock_setup(data, self.as_of))

class BreakoutContinuationTests(unittest.TestCase):
    def test_fresh_direct_breakout_accepts_seventy_percent_close_strength(self):
        self.data['high'][-1]=103.4
        for now in (self.now,self.now+60):
            reasons=[]
            with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[99.9,100.2]):
                signal=evaluate_stock_setup(self.data,now,min_day_change_pct=2,require_breakout=True,reasons=reasons)
                self.assertIsNotNone(signal)
                self.assertGreaterEqual(signal['breakout_confirmation']['close_strength'],0.70)

    def test_breakout_range_above_one_and_half_atr_is_rejected(self):
        self.data['high'][-1]=103.25
        reasons=[]
        with patch('stock_option_signals._atr',return_value=[1]),patch('stock_option_signals._ema',return_value=[99.9,100.2]):
            self.assertIsNone(evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True,reasons=reasons))
        self.assertIn('overextended',reasons[0])

    def test_individual_breakout_cannot_override_failed_scanner_volume(self):
        from stock_option_signals import scanx_stock_membership
        quote={'last_price':103,'ohlc':{'close':100},'volume':1000,'received_epoch':self.now}
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now)['allowed'])
        with self.assertRaises(TypeError):
            scanx_stock_membership(self.data,quote,self.now,require_daily_volume=False)
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[99.9,100.2]):
            signal=evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True)
            self.assertEqual(signal['scanner'],'CONFIRMED_BREAKOUT_CE')
            self.assertTrue(signal['breakout_confirmation']['closed_candle'])
            # A prior-day obstacle above this close invalidates the breakout.
            reasons=[]
            blocked=evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True,
                previous_session={'date':'2026-10-23','high':104,'low':99},reasons=reasons)
            self.assertIsNone(blocked)
            self.assertIn('No fresh closed breakout',reasons[0])
            # An impressive forming candle must not alter the closed signal.
            for k,v in zip(self.data,(self.now,103,120,102,119,99999)):self.data[k].append(v)
            again=evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True)
            self.assertEqual(again['signal_timestamp'],signal['signal_timestamp'])

    def test_basic_scanner_threshold_and_ema_cannot_be_relaxed(self):
        from stock_option_signals import scanx_stock_membership
        quote={'last_price':101.5,'ohlc':{'close':100},'volume':8000,'received_epoch':self.now}
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now,min_day_change_pct=1)['allowed'])
        quote['last_price']=103
        self.assertTrue(scanx_stock_membership(self.data,quote,self.now)['allowed'])
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now,ema_period=5)['allowed'])

    def test_screenshot_profile_early_volume_baseline_is_symmetric_and_not_a_weak_volume_bypass(self):
        prior=75
        self.data['volume'][prior:prior+3]=[5000]*3
        self.data['volume'][prior+3:prior+5]=[100,100]
        self.data['volume'][-1]=300
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[99.9,100.2]):
            signal=evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True)
            self.assertIsNotNone(signal)
            self.assertEqual(signal['volume_context']['required_volume'],120)
            self.data['volume'][-1]=110
            self.assertIsNone(evaluate_stock_setup(self.data,self.now,min_day_change_pct=2,require_breakout=True))
        self.data['volume'][-1]=300
        mirrored=copy.deepcopy(self.data)
        for i in range(len(mirrored['timestamp'])):
            for field in ('open','close'):mirrored[field][i]=200-self.data[field][i]
            mirrored['high'][i]=200-self.data['low'][i];mirrored['low'][i]=200-self.data['high'][i]
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[100.1,99.8]):
            signal=evaluate_stock_setup(mirrored,self.now,min_day_change_pct=2,require_breakout=True)
        self.assertEqual(signal['scanner'],'CONFIRMED_BREAKOUT_PE')

    def test_breakout_needs_verified_prior_levels(self):
        data=copy.deepcopy(self.data)
        for values in data.values():values.pop(0)
        with patch('stock_option_signals._atr',return_value=[2]),patch('stock_option_signals._ema',return_value=[99.9,100.2]):
            self.assertIsNone(evaluate_stock_setup(data,self.now,min_day_change_pct=2,require_breakout=True))
            reference={'date':'2026-10-23','high':100.2,'low':99.8}
            self.assertIsNotNone(evaluate_stock_setup(data,self.now,min_day_change_pct=2,require_breakout=True,previous_session=reference))
            reference['high']='bad'
            self.assertIsNone(evaluate_stock_setup(data,self.now,min_day_change_pct=2,require_breakout=True,previous_session=reference))

    def test_strong_continuation_does_not_require_beating_previous_volume_spike(self):
        self.data['volume'][-2]=1000
        result=self.signal()
        self.assertIsNotNone(result)
        self.assertEqual(result['volume_confirmation'],'ABOVE_AVERAGE_STRONG_PRICE_CONTINUATION')
        self.data['volume'][-1]=50
        self.assertIsNone(self.signal())

    def test_fresh_later_consolidation_break_can_qualify_without_chasing(self):
        values=(self.now,103,104.2,102.9,104,400)
        for field,value in zip(self.data,values):self.data[field].append(value)
        self.now+=300
        result=self.signal()
        self.assertIsNotNone(result)
        self.assertTrue(result['breakout_continuation']['local_consolidation_break'])
        for field,value in {'open':109,'high':110.2,'low':108.9,'close':110}.items():
            self.data[field][-1]=value
        self.assertIsNone(self.signal())

    def test_scanner_requires_full_session_volume_and_uses_quote_previous_close(self):
        from stock_option_signals import scanx_stock_membership
        quote={'last_price':103,'ohlc':{'close':100},'volume':10000,'received_epoch':self.now}
        result=scanx_stock_membership(self.data,quote,self.now)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['previous_session_volume'],7500)
        self.assertEqual(result['opt_type'],'CE')
        self.assertFalse(scanx_stock_membership(self.data,{**quote,'volume':7000},self.now)['allowed'])
        self.assertFalse(scanx_stock_membership(self.data,{**quote,'volume':7500},self.now)['allowed'])
        self.assertFalse(scanx_stock_membership(self.data,{**quote,'volume':1000},self.now)['allowed'])
        self.assertFalse(scanx_stock_membership(self.data,{**quote,'ohlc':{'close':102}},self.now)['allowed'])

    def test_scanner_rejects_stale_quote_and_partial_previous_session(self):
        from stock_option_signals import scanx_stock_membership
        quote={'last_price':103,'ohlc':{'close':100},'volume':10000,'received_epoch':self.now-31}
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now)['allowed'])
        quote['received_epoch']=self.now
        for values in self.data.values():values.pop(0)
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now)['allowed'])
        quote['previous_session_reference']={'date':'2026-10-23','volume':7500}
        self.assertTrue(scanx_stock_membership(self.data,quote,self.now)['allowed'])
        quote['previous_session_reference']['date']='2026-10-22'
        self.assertFalse(scanx_stock_membership(self.data,quote,self.now)['allowed'])

    def test_incomplete_fifteen_minute_bucket_is_excluded(self):
        from stock_option_signals import _completed_15m_bars
        t=self.now-900
        rows=[{'timestamp':t+i*300,'open':100,'high':101,'low':99,'close':100}
              for i in (0,2)]
        self.assertEqual(_completed_15m_bars(rows),[])

    def setUp(self):
        self.data={k:[] for k in ('timestamp','open','high','low','close','volume')}
        def add(t,o,h,l,c,v):
            for k,value in zip(self.data,(t.timestamp(),o,h,l,c,v)):
                self.data[k].append(value)
        prior=datetime(2026,10,23,9,15,tzinfo=bot.IST)
        for i in range(75):add(prior+timedelta(minutes=5*i),100,100.2,99.8,100,100)
        today=datetime(2026,10,26,9,15,tzinfo=bot.IST)
        for i,c in enumerate((100.5,100.8,101,101.4,101.8)):
            add(today+timedelta(minutes=5*i),c-.3,c+.2,c-.4,c,120)
        add(today+timedelta(minutes=25),101.8,103.2,101.7,103,300)
        self.now=(today+timedelta(minutes=30)).timestamp()

    def signal(self):
        with patch('stock_option_signals._atr',return_value=[2]),\
             patch('stock_option_signals._ema',return_value=[99.9,100.2]):
            return evaluate_stock_setup(self.data,self.now,min_day_change_pct=2)

    def test_first_threshold_cross_can_confirm_a_bounded_breakout(self):
        result=self.signal()
        self.assertIsNotNone(result)
        self.assertEqual(result['scanner'],'BREAKOUT_CONTINUATION_CE')
        self.assertTrue(result['breakout_continuation']['threshold_cross'])

    def test_weak_volume_or_overextended_breakout_still_rejected(self):
        self.data['volume'][-1]=130
        self.assertIsNone(self.signal())
        self.setUp()
        self.data['open'][-1]=105;self.data['high'][-1]=107.2
        self.data['low'][-1]=104.8;self.data['close'][-1]=107
        self.assertIsNone(self.signal())

    def test_not_a_blanket_exception_for_later_trend_candles(self):
        self.data['close'][-2]=102.2;self.data['high'][-2]=102.4
        self.assertIsNone(self.signal())

    def test_unclosed_breakout_is_not_used(self):
        self.now-=1
        self.assertIsNone(self.signal())

    def test_put_breakout_uses_the_same_bounds(self):
        original=copy.deepcopy(self.data)
        for field,source in (('open','open'),('high','low'),('low','high'),('close','close')):
            self.data[field]=[200-v for v in original[source]]
        with patch('stock_option_signals._atr',return_value=[2]),\
             patch('stock_option_signals._ema',return_value=[100.1,99.8]):
            result=evaluate_stock_setup(self.data,self.now,min_day_change_pct=2)
        self.assertIsNotNone(result)
        self.assertEqual(result['scanner'],'BREAKOUT_CONTINUATION_PE')

if __name__ == "__main__":
    unittest.main()


class StockExitProtectionTests(unittest.TestCase):
    def test_reliance_entry_rejected_when_premium_stop_precedes_structural_invalidation(self):
        signal={'opt_type':'CE','spot_ltp':1251.1,'atr_5m':7.0416267040517,'delta':0.56326,
            'breakout_confirmation':{'trigger_level':1246.4,'buffer':0.3520813352,'entry_path':'DIRECT_BREAKOUT'}}
        plan={'initial_sl':27.5,'cash_loss_stop_price':26.5}
        result=bot.stock_entry_structure_check(signal,plan,29.7,29.6)
        self.assertFalse(result['allowed'])
        self.assertIn('inside estimated structural risk',result['reason'])
        self.assertAlmostEqual(result['delta_scenario_risk_points'],2.9456353,places=5)
        self.assertEqual(plan['initial_sl'],27.5)
        # A new near-level entry fits the existing stop; the stop is not widened.
        earlier={**signal,'spot_ltp':1247.1}
        self.assertTrue(bot.stock_entry_structure_check(earlier,plan,29.7,29.6)['allowed'])
        mirrored={**signal,'opt_type':'PE','spot_ltp':2500-signal['spot_ltp'],'delta':-signal['delta'],
            'breakout_confirmation':{**signal['breakout_confirmation'],'trigger_level':2500-1246.4}}
        self.assertFalse(bot.stock_entry_structure_check(mirrored,plan,29.7,29.6)['allowed'])

    def test_structural_entry_uses_retest_invalidation_and_rejects_chase_or_bad_data(self):
        signal={'opt_type':'CE','spot_ltp':101,'atr_5m':2,'delta':.5,
            'breakout_confirmation':{'trigger_level':100,'buffer':.1,'entry_path':'PULLBACK_RETEST',
                'pullback':{'invalidation_level':98}}}
        plan={'initial_sl':9,'cash_loss_stop_price':8.5}
        self.assertFalse(bot.stock_entry_structure_check(signal,plan,10,9.95)['allowed'])
        signal['breakout_confirmation']['pullback']['invalidation_level']=99.5
        self.assertTrue(bot.stock_entry_structure_check(signal,plan,10,9.95)['allowed'])
        self.assertFalse(bot.stock_entry_structure_check({**signal,'spot_ltp':102},plan,10,9.95)['allowed'])
        self.assertFalse(bot.stock_entry_structure_check({**signal,'delta':float('nan')},plan,10,9.95)['allowed'])

    def test_urgent_pass_never_decides_from_old_runner_or_no_progress_assessment(self):
        cfg,pos,now=self.setup_position()
        pos['entry_epoch']=now.timestamp()-cfg.no_progress_bars*300-1
        self.assertIsNone(bot.paper_exit(pos,self.quote(pos['entry_price'],now),now,cfg,urgent_only=True))
        self.assertEqual(bot.paper_exit(pos,self.quote(pos['entry_price'],now),now,cfg)[1],'NO_PROGRESS')
        cfg,pos,now=self.setup_position()
        pos['breakout_failure']={'failed':True,'assessed_epoch':now.timestamp()}
        self.assertIsNone(bot.paper_exit(pos,self.quote(pos['entry_price'],now),now,cfg,urgent_only=True))
        self.assertEqual(bot.paper_exit(pos,self.quote(pos['entry_price'],now),now,cfg)[1],'UNDERLYING_BREAKOUT_FAILED')
        cfg,pos,now=self.setup_position()
        self.assertEqual(bot.paper_exit(pos,self.quote(20,now),now,cfg,urgent_only=True)[1],'CASH_LOSS_LIMIT')

    def setup_position(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_STOP_PCT':'7.5','STOCK_OPT_RUPEE_TARGET':'1200',
            'STOCK_OPT_RUPEE_MAX_RISK':'1800','STOCK_OPT_MIN_NET_RR':'1.5',
            'STOCK_OPT_TARGET_PCT':'16','STOCK_OPT_MAX_LOTS':'1',
            'STOCK_OPT_EXTEND_RUPEE_TARGET':'true','STOCK_OPT_STAGED_PROFIT':'true',
            'STOCK_OPT_SLIPPAGE_PCT':'0.05'},argparse.Namespace())
        now=datetime(2026,10,28,12,50,tzinfo=IST)
        plan=cfg.plan(24.9,950)
        pos={**plan,'entry_price':24.9,'entry_epoch':now.timestamp()-162,
             'peak_bid':24.9,'current_sl':plan['initial_sl'],'opt_type':'CE','atr_5m':3.3908844,
             'breakout_confirmation':{'trigger_level':539}}
        return cfg,pos,now

    def quote(self,bid,now,quantity=950):
        return {'received_epoch':now.timestamp(),'depth':{'buy':[{'price':bid,'quantity':quantity}]}}

    def test_pgel_peak_now_protects_single_lot_after_costs_without_waiting_for_trail(self):
        cfg,pos,now=self.setup_position()
        self.assertEqual(pos['lots'],1)
        self.assertIsNone(bot.paper_exit(pos,self.quote(25.5,now),now,cfg))
        self.assertEqual(pos['current_sl'],pos['initial_sl'])
        self.assertIsNone(bot.paper_exit(pos,self.quote(26.45,now),now,cfg))
        self.assertGreater(pos['current_sl'],pos['entry_price'])
        arm=bot.stock_net_protection_bid(pos,pos['rupee_target_price'],.86*pos['net_profit_target'],cfg)
        self.assertIsNone(bot.paper_exit(pos,self.quote(arm,now),now,cfg))
        floor=pos['current_sl'];self.assertGreater(floor,pos['entry_price'])
        decision=bot.paper_exit(pos,self.quote(floor,now),now,cfg)
        self.assertEqual(decision[1],'TRAIL_STOP')
        self.assertGreaterEqual(bot.stock_position_value(pos,decision[0])['net'],0)
        self.assertEqual(pos['current_sl'],floor)

    def failure_candles(self,now):
        start=now.timestamp()-120
        return {'timestamp':[start,start+60],'open':[539,538.6],'high':[539.1,538.7],
                'low':[538.5,538.3],'close':[538.6,538.4],'volume':[100,120]}

    def test_failure_requires_two_post_entry_closed_minutes_and_is_symmetric(self):
        cfg,pos,now=self.setup_position();candles=self.failure_candles(now)
        result=bot.stock_breakout_failure(pos,candles,now.timestamp())
        self.assertTrue(result['failed'])
        pos['breakout_failure']=result
        decision=bot.paper_exit(pos,self.quote(24.3,now),now,cfg)
        self.assertEqual(decision[1],'UNDERLYING_BREAKOUT_FAILED')
        mirrored=copy.deepcopy(candles)
        for k,source in [('open','open'),('high','low'),('low','high'),('close','close')]:
            mirrored[k]=[1078-v for v in candles[source]]
        self.assertTrue(bot.stock_breakout_failure({**pos,'opt_type':'PE'},mirrored,now.timestamp())['failed'])
        self.assertFalse(bot.stock_breakout_failure(pos,candles,now.timestamp()-1)['failed'])
        self.assertFalse(bot.stock_breakout_failure(pos,candles,now.timestamp()+91)['failed'])
        recovered=copy.deepcopy(candles);recovered['close'][0]=539.05
        self.assertFalse(bot.stock_breakout_failure(pos,recovered,now.timestamp())['failed'])
        self.assertFalse(bot.stock_breakout_failure({**pos,'entry_epoch':now.timestamp()-60},candles,now.timestamp())['failed'])
        # Future/forming recovery cannot alter a completed failure.
        for k,v in zip(candles,(now.timestamp(),540,541,539.5,540.5,1000)):candles[k].append(v)
        self.assertTrue(bot.stock_breakout_failure(pos,candles,now.timestamp()+30)['failed'])

    def test_depth_walk_fills_multiple_bid_levels_and_records_stop_overrun(self):
        cfg,pos,now=self.setup_position();pos['current_sl']=25.3
        quote=self.quote(25.2,now,500);quote['depth']['buy'].append({'price':25.15,'quantity':450})
        price,reason=bot.paper_exit(pos,quote,now,cfg)
        self.assertEqual(reason,'TRAIL_STOP');self.assertEqual(price,25.15)
        self.assertAlmostEqual(pos['exit_diagnostics']['executable_vwap'],(25.2*500+25.15*450)/950)
        cfg,pos,now=self.setup_position()
        price,reason=bot.paper_exit(pos,self.quote(22.7,now),now,cfg)
        self.assertEqual(reason,'CASH_LOSS_LIMIT')
        self.assertGreater(pos['exit_diagnostics']['cash_limit_overrun'],0)
        self.assertAlmostEqual(pos['exit_diagnostics']['net_at_exit'],bot.stock_position_value(pos,price)['net'])

    def test_unusable_depth_is_recorded_and_stale_structural_failure_cannot_exit(self):
        cfg,pos,now=self.setup_position()
        self.assertIsNone(bot.paper_exit(pos,self.quote(24.8,now,100),now,cfg))
        self.assertEqual(pos['exit_monitor']['unusable_checks'],1)
        stale=self.quote(24.8,now);stale['received_epoch']-=31
        self.assertIsNone(bot.paper_exit(pos,stale,now,cfg))
        pos['breakout_failure']={'failed':True,'assessed_epoch':now.timestamp()-91}
        self.assertIsNone(bot.paper_exit(pos,self.quote(24.8,now),now,cfg))
        self.assertEqual(pos['exit_monitor']['unusable_checks'],2)
        for i in range(30):bot.paper_exit(pos,self.quote(24.8,now),now,cfg)
        self.assertEqual(len(pos['exit_monitor']['recent_quotes']),24)


class CashRiskProfitProtectionRegressionTests(unittest.TestCase):
    def test_dixon_recorded_profit_arms_requested_hundred_rupee_net_floor(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_MAX_RISK':'1800','STOCK_OPT_SLIPPAGE_PCT':'0.05'},argparse.Namespace())
        now=datetime(2026,11,2,12,20,tzinfo=IST)
        pos={'entry_price':506.25,'entry_epoch':now.timestamp()-100,'qty':50,'lots':1,
             'initial_sl':468.3,'current_sl':468.3,'peak_bid':503.55,'initial_bid':503.55,
             'trail_jump':14.15,'opt_1r':37.95,'expected_net_risk':1800,'net_profit_target':2700,
             'rupee_target_price':562.8,'hard_target_price':600}
        quote=lambda bid:{'received_epoch':now.timestamp(),'depth':{'buy':[{'price':bid,'quantity':50}]}}
        self.assertLess(531.7-506.25,max(1.5*14.15,.75*37.95))
        self.assertIsNone(bot.paper_exit(pos,quote(531.7),now,cfg))
        self.assertGreater(pos['current_sl'],pos['entry_price'])
        arm=bot.stock_net_protection_bid(pos,562.8,.86*2700,cfg)
        self.assertIsNone(bot.paper_exit(pos,quote(arm),now,cfg))
        self.assertGreater(pos['current_sl'],pos['entry_price'])
        self.assertGreater(pos['peak_net_pnl'],900)
        floor=pos['current_sl']
        decision=bot.paper_exit(pos,quote(floor),now,cfg)
        self.assertEqual(decision[1],'TRAIL_STOP')
        self.assertGreaterEqual(bot.stock_position_value(pos,decision[0])['net'],0)
        self.assertEqual(pos['current_sl'],floor)


class RequestedEarlyEntryRunnerPatchTests(unittest.TestCase):
    def test_fresh_scanner_quote_reused_but_stale_or_future_quote_refetched(self):
        from unittest.mock import MagicMock
        client=MagicMock();now=bot.time.time()
        fresh={'last_price':100,'received_epoch':now-5.9}
        with patch.object(bot.time,'time',return_value=now):
            self.assertEqual(bot.fresh_stock_underlying_quote(client,'1',{'1':fresh}),fresh)
            client.fetch_batch_ohlc.assert_not_called()
            for stamp in (now-6,now+1):
                bot.fresh_stock_underlying_quote(client,'1',{'1':{**fresh,'received_epoch':stamp}})
        self.assertEqual(client.fetch_batch_ohlc.call_count,2)

    def test_recent_candidate_reuses_verification_without_chain_but_rechecks_price(self):
        from unittest.mock import MagicMock
        client=MagicMock();now=datetime(2026,11,5,10,1,tzinfo=IST).timestamp()
        client._stock_verification_context={'1':{'candles':{},'daily_reference':{'volume':100}}}
        cfg=bot.DynamicStrategyConfig({},argparse.Namespace())
        candidate={'eq_security_id':'1','verified_epoch':now-2,'signal_timestamp':now-360,
                   'opt_type':'CE','conf_close':100,'entry_price_validation':{
                   'minimum_advance':-.2,'maximum_advance':1.4,'trigger_level':99.9}}
        quote={'last_price':101.3,'received_epoch':now}
        with patch.object(bot.time,'time',return_value=now),patch.object(bot,'scanx_stock_membership',return_value={'allowed':True,'opt_type':'CE'}):
            self.assertIsNotNone(bot.recheck_recent_stock_candidate(client,candidate,cfg,{'1':quote},[]))
            self.assertIsNone(bot.recheck_recent_stock_candidate(client,candidate,cfg,{'1':{**quote,'last_price':101.5}},[]))
            self.assertIsNone(bot.recheck_recent_stock_candidate(client,{**candidate,'verified_epoch':now-10},cfg,{'1':quote},[]))
        client.fetch_option_chain.assert_not_called();client.fetch_intraday_candles.assert_not_called()
        client.fetch_batch_ohlc.assert_not_called()

    def test_runner_can_hold_on_fresh_trend_without_forecasting_upper_target(self):
        fixture=IntegrationTests();fixture.setUp();cfg,pos,quote=fixture.staged_position()
        now=fixture.now.timestamp()
        pos.update(conf_close=100,atr_5m=10,entry_contract_oi=100000,
                   entry_epoch=now-400,volume_context={'regular_average':500},
                   underlying_observations=[[now-300,102.5,1000]])
        underlying={'last_price':104,'received_epoch':now,'volume':3000}
        chain={'1660':{'ce':{'oi':102000,'greeks':{'delta':.6,'theta':-1}}}}
        result=bot.assess_stock_runner(pos,quote,underlying,chain,now,cfg)
        self.assertLess(result['scenario_gain'],1.2*result['required_premium_gain'])
        self.assertTrue(result['hold'])
        self.assertFalse(bot.assess_stock_runner(pos,quote,{**underlying,'received_epoch':now-9},chain,now,cfg)['hold'])

    def test_runner_locks_requested_tier_without_exiting_at_base(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1200','STOCK_OPT_RUNNER_TARGET':'4500'},argparse.Namespace())
        now=datetime(2026,11,5,12,tzinfo=IST)
        pos={'entry_price':100,'entry_epoch':now.timestamp()-60,'qty':100,'lots':1,
             'initial_sl':94.5,'current_sl':94.5,'peak_bid':100,'trail_jump':4,'opt_1r':5.5,
             'net_profit_target':1200,'rupee_target_price':113,'runner_assessment':{'hold':True,'assessed_epoch':now.timestamp()}}
        quote={'received_epoch':now.timestamp(),'depth':{'buy':[{'price':118,'quantity':100}]}}
        self.assertIsNone(bot.paper_exit(pos,quote,now,cfg))
        floor=pos['current_sl'];self.assertGreater(floor,100);self.assertLess(floor,118)
        quote['depth']['buy'][0]['price']=floor
        decision=bot.paper_exit(pos,quote,now,cfg)
        self.assertEqual(decision[1],'TRAIL_STOP')
        self.assertGreaterEqual(bot.stock_position_value(pos,decision[0])['net'],bot.stock_profit_protection_net(pos['peak_net_pnl'])-1)


class ScannerVolumeBoundaryTests(unittest.TestCase):
    def test_bearish_volume_surge_cannot_bypass_full_session_volume(self):
        from stock_option_signals import scanx_stock_membership
        fixture = BreakoutContinuationTests()
        fixture.setUp()
        data = fixture.data
        for i in range(len(data["timestamp"])):
            data["open"][i] = 200 - data["open"][i]
            data["close"][i] = 200 - data["close"][i]
            data["high"][i], data["low"][i] = 200 - data["low"][i], 200 - data["high"][i]
        data["volume"][-1] = 360
        now = fixture.now
        quote = {"last_price": 97, "ohlc": {"close": 100}, "volume": 1000, "received_epoch": now}
        self.assertFalse(scanx_stock_membership(data, quote, now)["allowed"])
        self.assertFalse(scanx_stock_membership(data, {**quote, "volume": 7500}, now)["allowed"])
        accepted = scanx_stock_membership(data, {**quote, "volume": 7501}, now)
        self.assertTrue(accepted["allowed"])
        self.assertEqual(accepted["opt_type"], "PE")


class RequestedExhaustionAndNetTierTests(unittest.TestCase):
    def test_exact_net_tier_boundaries(self):
        for peak,expected in [(749.99,None),(750,100),(1199,100),(1200,720),(1799,1079.4),(1800,1350),(3000,2250)]:
            with self.subTest(peak=peak):
                result=bot.stock_profit_protection_net(peak)
                if expected is None:self.assertIsNone(result)
                else:self.assertAlmostEqual(result,expected)

    def test_large_moves_remain_visible_on_both_scanner_sides(self):
        from stock_option_signals import scanx_stock_membership
        fixture=BreakoutContinuationTests();fixture.setUp()
        for sign in (1,-1):
            for change in (5.5, 5.5001, 7.4, 13.15):
                quote={'last_price':100*(1+sign*change/100),'ohlc':{'close':100},
                       'volume':10000,'received_epoch':fixture.now}
                with self.subTest(sign=sign,change=change):
                    # EMA remains on the appropriate side for both directions.
                    with patch('stock_option_signals._ema',return_value=[100]*30):
                        result=scanx_stock_membership(fixture.data,quote,fixture.now)
                    self.assertTrue(result['allowed'])

    def test_closed_signal_cannot_bypass_exhaustion_cap(self):
        fixture=BreakoutContinuationTests();fixture.setUp()
        reasons=[]
        self.assertIsNone(evaluate_stock_setup(fixture.data,fixture.now,previous_close=90,
                         enable_opening_trend=True,require_breakout=True,reasons=reasons))
        self.assertIn('5.5% exhaustion cap',reasons[0])

    def test_each_tier_locks_net_after_charges_and_never_lowers_stop(self):
        cfg=bot.DynamicStrategyConfig({'STOCK_OPT_RUPEE_TARGET':'1500','STOCK_OPT_RUNNER_TARGET':'4500',
                                      'STOCK_OPT_RUPEE_MAX_RISK':'1000'},argparse.Namespace())
        now=datetime(2026,11,5,12,tzinfo=IST)
        pos={'entry_price':100,'entry_epoch':now.timestamp()-60,'qty':100,'lots':1,
             'initial_sl':92.5,'current_sl':92.5,'peak_bid':100,'trail_jump':4,'opt_1r':7.5,
             'net_profit_target':1500,'rupee_target_price':116,'runner_assessment':{'hold':True,'assessed_epoch':now.timestamp()}}
        previous_stop=pos['current_sl']
        for net_target in (800,1300,1900):
            bid=bot.stock_net_protection_bid(pos,200,net_target,cfg)
            quote={'received_epoch':now.timestamp(),'depth':{'buy':[{'price':bid,'quantity':100}]}}
            self.assertIsNone(bot.paper_exit(pos,quote,now,cfg))
            locked=bot.stock_profit_protection_net(pos['peak_net_pnl'])
            self.assertGreaterEqual(pos['current_sl'],previous_stop)
            fill=__import__('math').floor(pos['current_sl']*(1-cfg.slippage_pct/100)*20+1e-9)/20
            self.assertGreaterEqual(bot.stock_position_value(pos,fill)['net'],locked-1)
            previous_stop=pos['current_sl']

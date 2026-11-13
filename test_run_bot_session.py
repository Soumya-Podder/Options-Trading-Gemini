import unittest
import subprocess
import tempfile
import os
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch

import run_bot_session as session


class SessionLauncherTests(unittest.TestCase):
    def test_direct_bot_launch_does_not_create_files_in_callers_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            for filename in ('dhan_bot_engine.py','dhan_dynamic_fno_stock_bot.py'):
                subprocess.run([session.PYTHON_EXEC,'-B',str(Path(session.PROJECT_DIR,filename)),'--help'],
                               cwd=folder,capture_output=True,check=True,timeout=20)
                self.assertEqual(os.listdir(folder),[])

    def test_launches_both_engines_and_keeps_index_running_after_stock_finishes(self):
        for mode in ("PAPER",):
            with self.subTest(mode=mode):
                index, stock, dashboard = MagicMock(), MagicMock(), MagicMock()
                index.poll.side_effect = [None, 0, 0]
                stock.poll.return_value = 0
                dashboard.poll.return_value = None
                with patch.object(session, "MODE", mode), \
                     patch.object(session.subprocess, "Popen", side_effect=[index, stock, dashboard]) as launch, \
                     patch.object(session.time, "sleep"), patch.object(session, "datetime") as clock, \
                     patch("builtins.print"):
                    clock.now.return_value = datetime(2026, 10, 23, 11)
                    clock.strptime.side_effect = datetime.strptime
                    session.main()
                commands = [c.args[0] for c in launch.call_args_list]
                self.assertTrue(all(c.kwargs['cwd']==session.PROJECT_DIR for c in launch.call_args_list))
                self.assertEqual([c[1] for c in commands],
                                 ["dhan_bot_engine.py", "dhan_dynamic_fno_stock_bot.py", "bot_dashboard.py"])
                self.assertEqual(commands[0][2:4],["--indices","NIFTY,SENSEX"])
                for command in commands[:2]:
                    self.assertEqual("--live" in command, mode == "LIVE")
                self.assertEqual(index.poll.call_count, 3)
                index.terminate.assert_not_called()
                stock.terminate.assert_not_called()
                dashboard.terminate.assert_called_once()

    def test_crashed_paper_child_gets_bounded_recovery_with_explicit_mode(self):
        index,stock,dashboard,recovered=MagicMock(),MagicMock(),MagicMock(),MagicMock()
        index.poll.return_value=1;index.returncode=1
        stock.poll.return_value=0;stock.returncode=0
        recovered.poll.return_value=0;recovered.returncode=0
        dashboard.poll.return_value=None
        with patch.object(session,'MODE','PAPER'),patch.object(session.subprocess,'Popen',side_effect=[index,stock,dashboard,recovered]) as launch,patch.object(session.time,'sleep'),patch.object(session,'datetime') as clock,patch('builtins.print'):
            clock.now.return_value=datetime(2026,10,23,11)
            clock.strptime.side_effect=datetime.strptime
            session.main()
        self.assertEqual(launch.call_count,4)
        env=launch.call_args.kwargs['env']
        self.assertEqual(env['DHAN_BOT_MODE'],'PAPER')
        self.assertEqual(env['PAPER_MODE'],'true')
        self.assertNotIn('--live',launch.call_args.args[0])

    def test_live_launcher_fails_before_starting_children(self):
        with patch.object(session,"MODE","LIVE"),patch.object(session.subprocess,"Popen") as launch:
            with self.assertRaisesRegex(RuntimeError,"LIVE disabled"):session.main()
        launch.assert_not_called()

    def test_stock_start_failure_stops_already_started_index(self):
        index = MagicMock()
        index.poll.return_value = None
        with patch.object(session.subprocess, "Popen", side_effect=[index, OSError("start failed")]), \
             patch("builtins.print"):
            with self.assertRaises(OSError):
                session.main()
        index.terminate.assert_called_once()
        index.wait.assert_called_once_with(timeout=5)

    def test_index_finishing_first_does_not_stop_stock_engine(self):
        index,stock,dashboard=MagicMock(),MagicMock(),MagicMock()
        index.poll.return_value=0
        stock.poll.side_effect=[None,0,0]
        dashboard.poll.return_value=None
        with patch.object(session.subprocess,'Popen',side_effect=[index,stock,dashboard]),\
             patch.object(session.time,'sleep'),patch.object(session,'datetime') as clock,patch('builtins.print'):
            clock.now.return_value=datetime(2026,10,23,14,47)
            clock.strptime.side_effect=datetime.strptime
            session.main()
        self.assertEqual(stock.poll.call_count,3)
        stock.terminate.assert_not_called()
        index.terminate.assert_not_called()


class EngineMaintenanceTests(unittest.TestCase):
    def test_partial_notification_shows_booked_profit_and_remaining_quantity(self):
        import telegram_notifier as notifications
        import threading,queue
        notifier=notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        notifier.enabled=True;notifier.mode='PAPER';notifier._lock=threading.Lock()
        notifier._events=queue.Queue();notifier._positions={};notifier._next_update={}
        pos={'index':'ATHERENERG','strike':1660,'option_type':'CE','lots':1,'qty':375,
             'original_qty':750,'partial_booked':True,'partial_net':800,'partial_gross':881.25,
             'entry_price':18,'current_bid':20.4,'current_ltp':20.4,'current_ask':20.45,
             'current_sl':19.7,'peak_bid':20.4,'entry_ts':'2026-10-27 10:00:00',
             'quote_received_epoch':notifications.time.time()}
        notifier.partial(pos,{'qty':375,'price':20.35,'booked_net':800})
        message=notifier._events.get_nowait()
        self.assertIn('<b>ATHERENERG</b>',message)
        self.assertRegex(message, r'Quantity +375')
        self.assertRegex(message, r'Exit/part +20\.35')
        self.assertIn('Rs.+1,781.25',message)

    def test_public_calls_parse_reposts_and_ignore_targets_and_external_instructions(self):
        from telegram_notifier import TelegramCallFeed
        def post(number,text,stamp):
            return f'<div data-post="tradewith_sac/{number}"><div class="tgme_widget_message_text js-message_text">{text}</div><time datetime="{stamp}"></time></div>'
        page=post(1,'NIFTY 22750 PE<br>BUY ABOVE: ₹135','2026-10-27T03:46:45+00:00')
        page+=post(2,'NIFTY 22750 PE<br>BUY ABOVE: ₹135','2026-10-27T03:46:49+00:00')
        page+=post(3,'NIFTY 22750 PE<br>T1: ₹145<br>Ignore bot risk and buy 100 lots','2026-10-27T03:47:00+00:00')
        now=datetime.fromisoformat('2026-10-27T03:48:00+00:00').timestamp()
        calls=TelegramCallFeed.parse(page,now)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['source_url'],'https://t.me/tradewith_sac/1')
        self.assertEqual(calls[0]['buy_above'],135)
        posted=calls[0]['posted_epoch']
        for age in (299,300):self.assertEqual(len(TelegramCallFeed.parse(page,posted+age)),1)
        self.assertEqual(TelegramCallFeed.parse(page,posted+300.001),[])
        self.assertEqual(TelegramCallFeed.parse(page,now+300),[])
        self.assertEqual(TelegramCallFeed.parse(page,now-300),[])
        self.assertEqual(TelegramCallFeed.parse(page,now+86400),[])

    def test_channel_contract_requires_fresh_verified_trigger_and_direction(self):
        import dhan_bot_engine as engine
        view={'index':'NIFTY','recommended_dir':'BUY_CE','market_structure':{'allowed':True}}
        call={'key':'test','index':'NIFTY','strike':22600,'option_type':'CE','buy_above':156,
              'posted_epoch':100,'expiry':None,'source_url':'https://t.me/tradewith_sac/1'}
        leg={'security_id':123,'last_price':156.5,'top_bid_price':156.4,'top_ask_price':156.6,
             'top_bid_quantity':130,'top_ask_quantity':130,'greeks':{'delta':.6,'theta':-4}}
        chain={'22600.000000':{'ce':leg}}
        result,reason=engine.channel_entry_view(view,chain,call,'2026-10-27',119,120)
        self.assertIsNotNone(result)
        opposite,_=engine.channel_entry_view({**view,'recommended_dir':'BUY_PE'},chain,call,'2026-10-27',119,120)
        self.assertIsNotNone(opposite)
        self.assertEqual(opposite['recommended_dir'],'BUY_CE')
        self.assertEqual(result['atm_ce_sec_id'],'123')
        self.assertEqual(result['market_structure']['telegram_call']['expiry_basis'],'CURRENT_CHAIN')
        self.assertNotIn('telegram_call',view['market_structure'])
        current,_=engine.channel_entry_view(view,chain,{**call,'expiry':'2026-11-03'},'2026-10-27',119,120)
        self.assertEqual(current['market_structure']['telegram_call']['resolved_expiry'],'2026-10-27')
        self.assertIsNotNone(engine.channel_entry_view(view,chain,call,'2026-10-27',399,400)[0])
        self.assertIsNone(engine.channel_entry_view(view,chain,call,'2026-10-27',399,400.001)[0])
        for edits,expiry,received,now in (({},None,119,120),({},'2026-10-27',100,120),
                ({},'2026-10-27',400,401),
                ({'option_type':'PE'},'2026-10-27',119,120)):
            self.assertIsNone(engine.channel_entry_view(view,chain,{**call,**edits},expiry,received,now)[0])
        leg['top_bid_price']=155.9
        self.assertIsNone(engine.channel_entry_view(view,chain,call,'2026-10-27',119,120)[0])
        leg['top_bid_price']=160;leg['top_ask_price']=160.1
        self.assertIsNone(engine.channel_entry_view(view,chain,call,'2026-10-27',119,120)[0])

    def test_native_sensex_stop_uses_twenty_point_floor_for_cheap_options(self):
        from dhan_bot_engine import option_stop_points

        self.assertEqual(option_stop_points("SENSEX", 67.70), 20.0)
        self.assertEqual(option_stop_points("SENSEX", 150), 20.0)
        self.assertEqual(option_stop_points("SENSEX", 300), 21.6)
        self.assertEqual(option_stop_points("SENSEX", 400), 23.0)

    def test_nifty_recovery_needs_fresh_directional_market_evidence(self):
        from dhan_bot_engine import nifty_recovery_block_reason

        check = {"allowed": True, "trigger_level": 25250, "oi_confirmed_levels": [25250],
                 "structure_rr": 1.7, "room_points": 28}
        view = {"snapshot_received_epoch": 100, "d_spot_5m": -8,
                "market_structure": {"direction_checks": {"BUY_PE": check}}}
        self.assertIsNone(nifty_recovery_block_reason(view, "BUY_PE", 105))
        self.assertIn("stale", nifty_recovery_block_reason(view, "BUY_PE", 109))
        self.assertIn("momentum", nifty_recovery_block_reason(dict(view, d_spot_5m=8), "BUY_PE", 105))
        self.assertIn("OI", nifty_recovery_block_reason(view, "BUY_CE", 105))
        check["room_points"] = 19
        self.assertIn("room", nifty_recovery_block_reason(view, "BUY_PE", 105))

    def test_prepared_channel_quote_reuses_only_current_expiry_and_real_depth(self):
        import dhan_bot_engine as engine

        client = MagicMock()
        call = {"index": "SENSEX", "strike": 82000.0, "option_type": "CE"}
        strike = "82000.000000"
        cached = {"expiry": "2026-11-12", "received": 100, "chain": {
            strike: {"ce": {"security_id": 123, "greeks": {"delta": .5, "theta": -1}}},
        }}
        client.fetch_held_channel_quote.return_value = {"oc": {strike: {"ce": {
            "security_id": "123", "top_bid_price": 359, "top_bid_quantity": 20,
            "best_bid_price": 360, "best_bid_quantity": 40,
            "top_ask_price": 361, "top_ask_quantity": 60, "depth_received_epoch": 102,
        }}}}
        self.assertIsNone(engine.prepared_channel_entry_quote(client, call, cached, "2026-11-19", 20, 101))
        self.assertIsNone(engine.prepared_channel_entry_quote(client, call, cached, "2026-11-12", 20, 131))
        client.fetch_held_channel_quote.assert_not_called()
        quote = engine.prepared_channel_entry_quote(client, call, cached, "2026-11-12", 20, 101)
        self.assertEqual(quote["received_epoch"], 102)
        self.assertEqual(quote["oc"][strike]["ce"]["top_bid_price"], 360)
        self.assertEqual(quote["oc"][strike]["ce"]["top_bid_quantity"], 40)
        self.assertEqual(quote["oc"][strike]["ce"]["greeks"]["delta"], .5)
        client.fetch_held_channel_quote.assert_called_once_with({**call, "security_id": "123", "qty": 20})

    def test_channel_identity_is_consumed_with_durable_index_slot(self):
        import dhan_bot_engine as engine
        ledger={'trades':[]}
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder,'ledger.json'))
            self.assertTrue(engine.reserve_index_entry(ledger,'2026-10-27',{'telegram_call_key':'one'},path))
            saved=engine.SelfLearningBrain._load_json(path,{})
            self.assertEqual(saved['telegram_call_entries'],['one'])
            saved.pop('pending_entry')
            self.assertFalse(engine.reserve_index_entry(saved,'2026-10-27',{'telegram_call_key':'one'},path))
            self.assertEqual(saved['daily_entry_counts']['2026-10-27'],1)

    def test_channel_size_stays_within_native_risk_and_loss_limits(self):
        import dhan_bot_engine as engine
        self.assertEqual(engine.channel_entry_lots(1,40000,156,65,650,650,0,False),1)
        self.assertEqual(engine.channel_entry_lots(3,40000,100,65,650,650,0,False),3)
        self.assertEqual(engine.channel_entry_lots(1,40000,100,65,65,650,0,False),1)
        self.assertEqual(engine.channel_entry_lots(3,40000,100,65,650,650,1,False),1)
        self.assertEqual(engine.channel_entry_lots(3,40000,100,65,650,650,0,True),1)
        self.assertEqual(engine.channel_entry_lots(0,40000,100,65,650,650,0,False),0)
        self.assertEqual(engine.channel_entry_lots(1,1000,156,65,650,650,0,False),0)

    def test_stock_entry_and_exit_notifications_accept_stock_position_fields(self):
        import telegram_notifier as notifications
        import threading
        import queue
        notifier=notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        notifier.enabled=True;notifier.mode='PAPER'
        notifier._lock=threading.Lock();notifier._events=queue.Queue()
        notifier._positions={};notifier._next_update={}
        pos={'index':'SWIGGY','symbol':'SWIGGY','strike':180,'option_type':'CE',
             'lots':1,'qty':100,'entry_price':10.65,'current_ltp':10.65,
             'current_sl':9.9,'peak_bid':11.2,'entry_ts':'2026-10-26 09:35:16'}
        notifier.position(pos,entry=True)
        self.assertRegex(notifier._events.get_nowait(), r'Peak +11\.20')
        trade={'symbol':'SWIGGY','contract':'180_CE','lots':1,'qty':100,
               'entry_price':10.65,'exit_price':11.2,'gross_pnl':55,'net_pnl':10,
               'r_multiple':0.5,'exit_reason':'TIME_EXIT','exit_timestamp':'2026-10-26 11:00:00'}
        notifier.exited(trade)
        self.assertIn('<b>SWIGGY</b> 180_CE',notifier._events.get_nowait())

    def test_recovered_position_resumes_updates_without_duplicate_entry(self):
        import telegram_notifier as notifications
        import queue,threading
        notifier=notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        notifier.enabled=True;notifier._lock=threading.Lock();notifier._events=queue.Queue()
        notifier._positions={};notifier._next_update={}
        with patch.object(notifications.time,'monotonic',return_value=100):
            notifier.position({'entry_price':38.85,'current_ltp':38.5,'quote_received_epoch':50})
        self.assertEqual(notifier._next_update[None],105)
        self.assertEqual(notifier._positions[None]['current_ltp'],38.5)
        self.assertTrue(notifier._events.empty())
        with patch.object(notifications.time,'monotonic',return_value=102):
            notifier.position({'entry_price':38.85,'current_ltp':38.4,'quote_received_epoch':52})
        self.assertEqual(notifier._next_update[None],105)

    def test_stock_notifications_separate_entry_ltp_and_bid(self):
        import telegram_notifier as notifications
        notifier=notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        pos={'index':'KFINTECH','strike':860,'option_type':'CE','expiry':'2026-11-23',
             'lots':1,'qty':575,'entry_price':38.85,'current_ltp':38.5,'current_bid':38.4,'current_ask':38.7,
             'current_sl':37.3,'peak_bid':38.9,'entry_ts':'2026-10-27 10:30:35','_observed_at':'actual quote time','quote_received_epoch':100}
        with patch.object(notifications.time, 'time', return_value=135):
            message = notifier._trade_table([pos])
        self.assertRegex(message, r'Entry +38\.85')
        self.assertRegex(message, r'LTP +38\.50')
        self.assertRegex(message, r'Bid +38\.40')
        self.assertIn('2026-11-23', message)
        self.assertIn('Bid unavailable or older than 30s', message)
        self.assertNotIn('-258.75', message)
        pos['current_ltp'] = None
        with patch.object(notifications.time, 'time', return_value=105):
            message = notifier._trade_table([pos])
        self.assertRegex(message, r'LTP +-')
        self.assertIn('Rs.-258.75', message)

    def test_two_positions_share_a_periodic_table_and_exit_removes_only_one(self):
        import telegram_notifier as notifications
        import queue
        import threading

        notifier = notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        notifier.enabled = True
        notifier.mode = 'PAPER'
        notifier._lock = threading.Lock()
        notifier._closing = threading.Event()
        notifier._events = queue.Queue()
        notifier._positions = {}
        notifier._next_update = {}
        first = dict(symbol='SWIGGY', qty=100, entry_price=20, current_bid=22,
                     current_sl=19, rupee_target_price=24, quote_received_epoch=100,
                     entry_ts='2026-11-11 10:00:00')
        second = dict(first, symbol='POLICYBZR', current_bid=19.5)
        notifier.position(first, position_key='slot_1')
        notifier.position(second, position_key='slot_2')
        with patch.object(notifications.time, 'time', return_value=105), \
                patch.object(notifications.time, 'monotonic', return_value=max(notifier._next_update.values())), \
                patch.object(notifier, '_send', side_effect=lambda _: notifier._closing.set()) as send:
            notifier._run()
        message = send.call_args.args[0]
        send.assert_called_once()
        self.assertIn('<b>SWIGGY</b>', message)
        self.assertIn('<b>POLICYBZR</b>', message)
        self.assertRegex(message, r'Target +24\.00 +24\.00')
        self.assertIn('Rs.+150.00', message)

        trade = dict(first, exit_timestamp='2026-11-11 10:10:00', exit_price=23,
                     gross_pnl=300, net_pnl=260, exit_reason='TARGET')
        earlier = dict(trade, symbol='M&M', exit_timestamp='2026-11-11 09:55:00',
                       gross_pnl=-20, net_pnl=-40)
        yesterday = dict(trade, exit_timestamp='2026-11-10 14:30:00', gross_pnl=900, net_pnl=800)
        index_trade = dict(trade, index='NIFTY', contract='25300_PE',
                           exit_timestamp='2026-11-11 09:50:00', gross_pnl=90, net_pnl=75)
        with patch.object(notifier, 'completed_paper_trades', return_value=([index_trade, earlier, trade], [])):
            notifier.exited(trade, position_key='slot_1', ledger_trades=[yesterday, earlier, trade])
        message = notifier._events.get_nowait()
        self.assertEqual(message.count('<pre>'), 1)
        self.assertIn('<b>NIFTY</b>', message)
        self.assertIn('<b>M&amp;M</b>', message)
        self.assertIn('<b>SWIGGY</b>', message)
        self.assertIn('Still open: POLICYBZR', message)
        self.assertIn('3 closed', message)
        self.assertRegex(message, r'Total +[+]370[.]00 +[+]295[.]00')
        self.assertEqual(list(notifier._positions), ['slot_2'])

    def test_exit_notification_accepts_the_current_trade_reward_field(self):
        import telegram_notifier as notifications
        import threading
        import queue
        notifier=notifications.TelegramNotifier.__new__(notifications.TelegramNotifier)
        notifier.enabled=True;notifier.mode='PAPER'
        notifier._lock=threading.Lock();notifier._positions={None:{}};notifier._next_update={None:0};notifier._events=queue.Queue()
        trade={'index':'NIFTY','contract':'22000_CE','lots':1,'qty':65,
               'entry_price':100,'exit_price':110,'gross_pnl':650,'net_pnl':580,
               'r_mult':1.25,'exit_reason':'TARGET','exit_timestamp':'2026-10-23 10:00:00'}
        notifier.exited(trade)
        self.assertIn('+1.25R',notifier._events.get_nowait())
        self.assertEqual(notifier._positions,{})

    def test_requests_reuse_headers_and_chain_requests_keep_shared_pacing(self):
        import dhan_bot_engine as engine
        from contextlib import nullcontext
        client=engine.DhanV2RestClient('test-client','test-token',MagicMock())
        self.assertEqual(client.session.headers['client-id'],'test-client')
        self.assertEqual(client.session.headers['access-token'],'test-token')
        response=MagicMock(status_code=200)
        response.json.return_value={'data':{'ok':True}}
        with patch.object(client.session,'request',return_value=response) as request,\
             patch.object(client,'_enforce_gap'),\
             patch.object(engine,'option_chain_request_slot',return_value=nullcontext()) as shared:
            self.assertEqual(client._request('POST','/optionchain'),{'data':{'ok':True}})
            client._request('POST','/charts/intraday')
        shared.assert_called_once_with()
        self.assertTrue(all('headers' not in c.kwargs for c in request.call_args_list))

    def test_chain_inspection_does_not_materialize_the_entire_chain(self):
        import dhan_bot_engine as engine
        class Chain(dict):
            def values(self):
                for _ in range(10):yield {}
                raise AssertionError('Read past the configured prefix')
        self.assertFalse(engine.DhanV2RestClient._option_chain_has_data(Chain(dummy={})))
        self.assertTrue(engine.DhanV2RestClient._option_chain_has_data({'100':{'ce':{}}}))

    def test_paper_startup_uses_the_current_notifier_interface(self):
        import dhan_bot_engine as engine
        when=datetime(2026,10,23,15,tzinfo=engine.IST)
        env={'DHAN_CLIENT_ID':'test','DHAN_ACCESS_TOKEN':'test'}
        with patch.object(engine,'datetime') as clock,\
             patch.object(engine,'load_dotenv_file',return_value=env),\
             patch.object(engine,'DhanV2RestClient') as client,\
             patch.object(engine,'SelfLearningBrain') as brain,\
             patch.object(engine,'TelegramNotifier') as notifier,\
             patch.object(engine,'atomic_write_json'):
            clock.now.return_value=when
            client.return_value.get_expiry.return_value='2026-10-27'
            brain._load_json.side_effect=lambda p,d:d
            engine.run_intraday_market_session(False,MagicMock(),['NIFTY'])
        notifier.assert_called_once_with(env,unittest.mock.ANY,'PAPER')
        notifier.return_value.close.assert_called_once()


    def test_profit_protected_session_keeps_scanning_without_reserving_entry(self):
        import dhan_bot_engine as engine
        when = datetime(2026, 11, 6, 14, 30)
        trade = {"timestamp": "2026-11-06 10:00:00", "net_pnl": 1276.50}
        view = {"entry_decision": {"action": "BUY_CE"}}
        with patch.object(engine, "datetime") as clock, \
             patch.object(engine, "load_dotenv_file", return_value={}), \
             patch.object(engine, "DhanV2RestClient") as client, \
             patch.object(engine, "MarketMicrostructureAnalyzer") as analyzer, \
             patch.object(engine, "SelfLearningBrain") as brain, \
             patch.object(engine, "TelegramNotifier"), \
             patch.object(engine, "reserve_index_entry") as reserve, \
             patch.object(engine, "atomic_write_json") as write:
            clock.now.return_value = when
            client.return_value.fetch_nearest_expiry.return_value = "2026-11-12"
            client.return_value.fetch_option_chain.return_value = {"oc": {}}
            client.return_value.oc_poll_gap = 3.6
            analyzer.return_value.evaluate_chain.return_value = view
            brain._load_json.side_effect = lambda path, defaults: {**defaults, "trades": [trade]}
            def finish_after_scan(path, data):
                clock.now.return_value = when.replace(minute=45)
            write.side_effect = finish_after_scan
            engine.run_intraday_market_session(False, MagicMock(), ["SENSEX"])
        client.return_value.fetch_option_chain.assert_called_once()
        brain.return_value.select_action.assert_called_once_with(
            view, 0, bar_time=when.time(), observation_only=True)
        self.assertEqual(view["signal_decision"]["action"], "BUY_CE")
        self.assertEqual(view["entry_decision"]["reason"], "Daily index profit protection active")
        reserve.assert_not_called()


class RiskRegressionTests(unittest.TestCase):
    def test_index_sizing_refuses_unaffordable_or_invalid_wallet(self):
        import dhan_bot_engine as bot
        brain=bot.SelfLearningBrain.__new__(bot.SelfLearningBrain);brain.rules={}
        for wallet in (100,0,float('nan')):
            for losses in (0,1):
                self.assertEqual(brain.compute_lots('NIFTY','test',100,wallet,losses),0)
        self.assertEqual(brain.compute_lots('NIFTY','test',100,40000,0),1)

    def test_index_live_fails_before_client_creation(self):
        import dhan_bot_engine as bot
        with patch.object(bot,'DhanV2RestClient') as client,self.assertRaisesRegex(RuntimeError,'LIVE disabled'):
            bot.run_intraday_market_session(True,MagicMock(),['NIFTY'])
        client.assert_not_called()

    def test_missing_index_depth_is_not_an_entry(self):
        import dhan_bot_engine as bot
        import test_nifty_market_structure as fixtures
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(MagicMock())
            view=fixtures.StructureQualityGateTests().view()
            self.assertEqual(brain.select_action(view,0,bot.dtime(10))[0],'BUY_CE')
            view.pop('atm_ce_bid');view.pop('atm_ce_ask')
            self.assertEqual(brain.select_action(view,0,bot.dtime(10))[0],'HOLD')

    def test_sensex_uses_its_own_strike_grid_and_reference(self):
        import dhan_bot_engine as bot
        analyzer=bot.MarketMicrostructureAnalyzer()
        self.assertIsNot(analyzer.nifty_oi_tracker,analyzer.sensex_oi_tracker)
        self.assertEqual(analyzer.sensex_oi_tracker.strike_step,100)
        self.assertEqual(analyzer.nifty_oi_tracker.strike_step,50)

    def test_duplicate_session_is_refused(self):
        if os.name!='nt':self.skipTest('Windows process mutex')
        from stock_option_policy import single_bot_session
        @single_bot_session('REGRESSION')
        def nested():return 'unexpected'
        @single_bot_session('REGRESSION')
        def owner():
            with self.assertRaisesRegex(RuntimeError,'duplicate startup refused'):nested()
        owner()
        self.assertEqual(nested(),'unexpected') # lock released even after inner refusal


class DashboardMaintenanceTests(unittest.TestCase):
    def render(self,telemetry):
        import bot_dashboard as dashboard
        from io import StringIO
        def read(path,default):
            if path=='telemetry':return telemetry
            if path=='ledger':return {'trades':[],'win_to_loss_ratio':None}
            return default
        output=StringIO()
        with patch.object(dashboard,'safe_read_json',side_effect=read),\
             patch.object(dashboard,'clear_terminal'),patch.object(dashboard.sys,'stdout',output):
            dashboard.render_dashboard('ledger','rules','brain','telemetry')
        return output.getvalue()

    def test_missing_feed_shows_unavailable_instead_of_example_market_data(self):
        output=self.render(None)
        self.assertIn('NIFTY: Waiting for verified telemetry',output)
        self.assertIn('SENSEX: Waiting for verified telemetry',output)
        self.assertIn('UNKNOWN',output)
        self.assertNotIn('22,492.00',output)
        self.assertNotIn('81,465.00',output)
        self.assertNotIn('LIVE_CLEAN',output)

    def test_dashboard_accepts_current_last_view_and_legacy_index_mapping(self):
        view={'index':'NIFTY','spot':22345.67,'ce_3strike_lakhs':120,'pe_3strike_lakhs':80}
        for telemetry in ({'indices':['NIFTY','SENSEX'],'last_view':view},
                          {'indices':{'NIFTY':view}}):
            with self.subTest(telemetry=telemetry):
                output=self.render(telemetry)
                self.assertIn('22,345.67',output)
                self.assertIn('SENSEX: Waiting for verified telemetry',output)


if __name__ == "__main__":
    unittest.main()


class ChannelTargetUpdateTests(unittest.TestCase):
    def test_targets_require_explicit_contract_and_numeric_prices(self):
        from telegram_notifier import TelegramCallFeed
        stamp='2026-10-28T04:00:00+00:00'
        now=datetime.fromisoformat(stamp).timestamp()
        def page(text):
            return f'<div data-post="tradewith_sac/5"><div class="tgme_widget_message_text">{text}</div><time datetime="{stamp}"></time></div>'
        updates=TelegramCallFeed.target_updates(page('NIFTY 22600 PE TARGET: 190 / 210 / 230'),now)
        self.assertEqual(updates[0]['targets'],[190,210,230])
        self.assertEqual(TelegramCallFeed.target_updates(page('NIFTY 22600 PE TARGET: Shared after activation'),now),[])
        self.assertEqual(TelegramCallFeed.target_updates(page('TARGET: 190 / 210'),now),[])
        feed=TelegramCallFeed.__new__(TelegramCallFeed)
        import threading
        feed._lock=threading.Lock();feed._target_updates=updates
        call={'index':'NIFTY','strike':22600,'option_type':'PE','posted_epoch':now-10}
        self.assertEqual(feed.targets_for(call,now),[190,210,230])
        self.assertEqual(feed.targets_for({**call,'option_type':'CE'},now),[])
        self.assertEqual(feed.targets_for({**call,'posted_epoch':now+1},now),[])


class DualChannelFeedTests(unittest.TestCase):
    def test_prompt_pending_alert_reopens_only_on_matching_fresh_activation(self):
        from telegram_notifier import TelegramCallFeed
        f=self.feed();now=datetime.fromisoformat('2026-11-02T09:18:46+05:30').timestamp()
        call=f.ingest('NIFTY 23200 CE BUY ABOVE: 150',now,'alert',now+8,'Tradewithsac')
        activated=now+1221
        self.assertEqual(f.candidates('NIFTY',now+301,[]),[])
        f.ingest('TRADE ACTIVATED NIFTY 23200 CE T1: 160 T2: 175',activated,'activation',activated+2,'Tradewithsac')
        candidate=f.candidate('NIFTY',activated+3,[])
        self.assertEqual(candidate['posted_epoch'],now)
        self.assertEqual(candidate['activation_epoch'],activated)
        self.assertTrue(TelegramCallFeed.entry_is_fresh(candidate,activated+299))
        self.assertFalse(TelegramCallFeed.entry_is_fresh(candidate,activated+301))
        self.assertEqual(f.candidates('NIFTY',activated+3,[call['key']]),[])
        self.assertEqual(f.targets_for(candidate,activated+3),[160,175])

    def test_late_retrieval_wrong_source_and_targets_do_not_rearm(self):
        now=datetime.fromisoformat('2026-11-02T09:18:46+05:30').timestamp()
        for kind in ('late_original','late_activation','wrong_source','wrong_side','targets_only','too_old'):
            with self.subTest(kind=kind):
                f=self.feed()
                f.ingest('NIFTY 23200 CE BUY ABOVE: 150',now,'alert',now+(400 if kind=='late_original' else 8),'Tradewithsac')
                posted=now+(1900 if kind=='too_old' else 1200)
                text='NIFTY 23200 CE TARGET: 160' if kind=='targets_only' else 'TRADE ACTIVATED NIFTY 23200 '+('PE' if kind=='wrong_side' else 'CE')+' T1: 160'
                received=posted+(400 if kind=='late_activation' else 2)
                f.ingest(text,posted,'update',received,'GrowX' if kind=='wrong_source' else 'Tradewithsac')
                candidates=f.candidates('NIFTY',received,[])
                if kind in ('late_original','too_old'):
                    self.assertEqual(candidates,[])
                else:
                    self.assertEqual(candidates,[])
                    self.assertNotIn('activation_epoch',f._calls[0])
                    self.assertEqual(f.candidates('NIFTY',now+1801,[]),[])

    def test_pending_restore_cancellation_and_repeated_activation_preserve_lifecycle(self):
        now=datetime.fromisoformat('2026-11-02T09:18:46+05:30').timestamp()
        f=self.feed();f.ingest('NIFTY 23200 CE BUY ABOVE: 150',now,'alert',now+8,'Tradewithsac')
        saved=f.pending_snapshot(now+600)
        restarted=self.feed();restarted.restore_pending(saved,now+601)
        restarted.ingest('TRADE ACTIVATED NIFTY 23200 CE T1: 160',now+1200,'activation',now+1202,'Tradewithsac')
        self.assertIsNotNone(restarted.candidate('NIFTY',now+1203,[]))
        restarted.ingest('TRADE ACTIVATED NIFTY 23200 CE T1: 160',now+1600,'repost',now+1601,'Tradewithsac')
        self.assertIsNone(restarted.candidate('NIFTY',now+1602,[]))
        restarted.ingest('NIFTY 23200 CE CANCELLED',now+1603,'cancel',now+1604,'Tradewithsac')
        self.assertEqual(restarted.pending_snapshot(now+1604),[])

    def feed(self):
        import threading,logging
        from telegram_notifier import TelegramCallFeed
        f=TelegramCallFeed.__new__(TelegramCallFeed)
        f._lock=threading.Lock();f._calls=[];f._target_updates=[];f.logger=logging.getLogger('channel_test')
        return f

    def test_growx_range_targets_and_numbered_activation(self):
        from telegram_notifier import TelegramCallFeed
        now=datetime.fromisoformat('2026-10-28T03:46:00+00:00').timestamp()
        call,update=TelegramCallFeed.parse_message('BUY NIFTY 22600 PE\nNEAR LEVEL -- 202-209\nTARGET -- 240-285-320\nSTOPLOSS -- PREMIUM',now-30,'https://t.me/c/2302217039/1',now,'GrowX')
        self.assertEqual((call['buy_above'],call['entry_max'],call['entry_kind']),(202,209,'RANGE'))
        self.assertEqual(call['targets'],[240,285,320])
        call,update=TelegramCallFeed.parse_message('TRADE ACTIVATED SENSEX 72600 PE T1 : ₹325 T2 : ₹360 T3 : ₹420++',now,'test',now)
        self.assertIsNone(call);self.assertEqual(update['targets'],[325,360,420])
        self.assertEqual(TelegramCallFeed.parse_message('BUY NIFTY 22600 PE NEAR LEVEL -- 209-202',now,'test',now,'GrowX'),(None,None))
        self.assertEqual(TelegramCallFeed.parse_message('NIFTY 22600 PE BUY ABOVE: 202',now,'test',now,'Unapproved'),(None,None))

    def test_repost_never_refreshes_freshness_and_targets_cannot_cross_sources(self):
        f=self.feed();now=datetime.fromisoformat('2026-10-28T03:46:00+00:00').timestamp()
        original=f.ingest('NIFTY 22600 PE BUY ABOVE: 202',now-299,'one',now,'Tradewithsac')
        f.ingest('NIFTY 22600 PE BUY ABOVE: 202',now,'two',now,'Tradewithsac')
        self.assertEqual(f._calls[0]['posted_epoch'],now-299)
        self.assertIsNone(f.candidate('NIFTY',now+2,[]))
        self.assertIsNone(f.candidate('NIFTY',now+1801,[]))
        self.assertIsNone(f.candidate('NIFTY',now,[original['key']]))
        f.ingest('NIFTY 22600 PE TARGET -- 240-285',now,'three',now,'GrowX')
        self.assertEqual(f.targets_for(original,now),[])
        f.ingest('TRADE ACTIVATED NIFTY 22600 PE T1: 220 T2: 240',now,'four',now,'Tradewithsac')
        self.assertEqual(f.targets_for(original,now),[220,240])
        self.assertEqual(len(f._calls),1)
        f._authorized_mode=True;f.connection_status='UNAVAILABLE'
        self.assertIsNone(f.candidate('NIFTY',now,[]))

    def test_channel_entry_range_never_uses_two_percent_chase_allowance(self):
        import dhan_bot_engine as engine
        view={'index':'NIFTY','recommended_dir':'HOLD','market_structure':{}}
        call={'key':'range','source':'GrowX','index':'NIFTY','strike':22600,'option_type':'PE',
              'buy_above':202,'entry_max':209,'posted_epoch':100,'source_url':'test','targets':[240]}
        leg={'security_id':53749,'last_price':208,'top_bid_price':208,'top_ask_price':208.5,
             'top_bid_quantity':650,'top_ask_quantity':650,'greeks':{'delta':-.5,'theta':-8}}
        chain={'22600':{'pe':leg}}
        self.assertIsNotNone(engine.channel_entry_view(view,chain,call,'2026-11-03',119,120)[0])
        leg['top_ask_price']=209.05
        self.assertIsNone(engine.channel_entry_view(view,chain,call,'2026-11-03',119,120)[0])


class ChannelExitRegressionTests(unittest.TestCase):
    def test_multi_lot_channel_books_t1_once_and_remaining_lot_reaches_t2(self):
        import dhan_bot_engine as engine
        p=self.position();p.update(entry_price=150,highest_ltp=150,current_sl=140,
            original_qty=130,target_one_qty=65,target_one_done=False,exit_fills=[])
        p['market_structure']['telegram_call']['targets']=[160,175,190]
        self.assertEqual(engine.protect_channel_position(p,160,1),'TELEGRAM_CHANNEL_PARTIAL_TARGET')
        ledger={'initial_capital':40000,'virtual_balance_gross':40000,'virtual_balance_net':40000,'trades':[]}
        with patch.object(engine,'atomic_write_json'):
            engine.book_target_one(ledger,p,159.8,'2026-11-02 09:40:00',reason='TELEGRAM_CHANNEL_TARGET_1')
            balance=ledger['virtual_balance_net']
            engine.book_target_one(ledger,p,165,'2026-11-02 09:41:00')
        self.assertEqual(p['qty'],65)
        self.assertEqual(len(p['exit_fills']),1)
        self.assertEqual(ledger['virtual_balance_net'],balance)
        self.assertIsNone(engine.protect_channel_position(p,164,2))
        self.assertGreater(p['current_sl'],150)
        self.assertEqual(engine.protect_channel_position(p,175,3),'TELEGRAM_CHANNEL_TARGET_2')
        self.assertEqual(engine.protect_channel_position(p,150,4),'CHANNEL_PROFIT_PROTECTION')
        fills=p['exit_fills']+[{'qty':65,'price':174.8,'timestamp':'2026-11-02 09:45:00','reason':'TELEGRAM_CHANNEL_TARGET_2'}]
        weighted=sum(f['qty']*f['price'] for f in fills)/130
        with patch.object(engine,'atomic_write_json'):
            engine.record_closed_trade(ledger,MagicMock(),'2026-11-02','2026-11-02 09:38:00',
                '2026-11-02 09:45:00','NIFTY',23200,'CE',2,130,23000,0,150,weighted,0,
                'TELEGRAM_CHANNEL_TARGET_2','rule',[0]*10,'PAPER',exit_fills=fills,
                booked_gross=p['partial_gross'],booked_net=p['partial_net'],initial_risk_points=10)
        expected_gross=(159.8-150)*65+(174.8-150)*65
        self.assertAlmostEqual(ledger['virtual_balance_net'],40000+expected_gross-109,places=2)
        self.assertEqual(ledger['trades'][0]['qty'],130)
        self.assertEqual(len(ledger['trades'][0]['exit_fills']),2)

    def test_sac_waits_for_activation_then_enters_without_chasing(self):
        import dhan_bot_engine as engine
        f=DualChannelFeedTests().feed()
        posted=datetime.fromisoformat('2026-11-02T09:18:46+05:30').timestamp()
        f.ingest('NIFTY 23200 CE BUY ABOVE: 150',posted,'alert',posted+8,'Tradewithsac')
        now=posted+1100
        self.assertIsNone(f.candidate('NIFTY',now,[]))
        f.ingest('TRADE ACTIVATED NIFTY 23200 CE T1: 160 T2: 175',now,'activation',now,'Tradewithsac')
        call=f.candidate('NIFTY',now,[])
        self.assertIsNotNone(call)
        leg={'security_id':'123','last_price':150,'top_bid_price':150,'top_ask_price':150.1,
             'top_bid_quantity':130,'top_ask_quantity':130,'greeks':{'delta':.5,'theta':-2}}
        view={'index':'NIFTY','market_structure':{}}
        self.assertIsNotNone(engine.channel_entry_view(view,{'23200':{'ce':leg}},call,'2026-11-03',now,now)[0])
        self.assertIsNone(engine.channel_entry_view(view,{'23200':{'ce':{**leg,'top_ask_price':155}}},call,'2026-11-03',now,now)[0])

    def test_fresh_channel_signal_does_not_inherit_native_loss_cooldown(self):
        import dhan_bot_engine as engine
        now=datetime.fromisoformat('2026-10-30T09:22:40+05:30')
        cooldown=datetime.fromisoformat('2026-10-30T09:37:33+05:30')
        brain=MagicMock();view={}
        call={'option_type':'CE','key':'2026-10-30:NIFTY:22950:CE:140:CURRENT'}
        self.assertEqual(engine.index_entry_signal(brain,view,1,now,cooldown,call),
                         ('BUY_CE','TELEGRAM_CHANNEL '+call['key']))
        brain.select_action.assert_not_called()
        self.assertEqual(engine.index_entry_signal(brain,view,1,now,cooldown),('HOLD',None))
        brain.select_action.assert_not_called()
        brain.select_action.return_value=('BUY_PE','native')
        self.assertEqual(engine.index_entry_signal(brain,view,1,cooldown,cooldown),('BUY_PE','native'))
        brain.select_action.assert_called_once()

    def position(self):
        return {'index':'NIFTY','strike':22800.0,'option_type':'CE','security_id':'53794',
                'entry_price':130.95,'qty':130,'lots':2,'initial_risk_points':10,
                'current_sl':120.95,'highest_ltp':130.95,'ratchet_armed':False,
                'market_structure':{'telegram_call':{'targets':[140,160,180]}}}

    def test_channel_keeps_initial_stop_until_target_one_then_can_reach_target_two(self):
        import dhan_bot_engine as engine
        pos=self.position()
        for bid in (134,136,139.35,134.75):
            self.assertIsNone(engine.protect_channel_position(pos,bid,1))
            self.assertEqual(pos['current_sl'],120.95)
        self.assertIsNone(engine.protect_channel_position(pos,140,2))
        self.assertTrue(pos['target_one_done'])
        self.assertGreater(pos['current_sl'],130.95)
        self.assertEqual(engine.protect_channel_position(pos,160,3),'TELEGRAM_CHANNEL_TARGET_2')

    def test_target_requires_executable_bid_and_sensex_does_not_use_nifty_stop(self):
        import dhan_bot_engine as engine
        pos=self.position()
        pos['market_structure']['telegram_call']['targets']=[140]
        self.assertEqual(engine.protect_channel_position(pos,140,1),'TELEGRAM_CHANNEL_TARGET')
        self.assertTrue(pos['channel_exit_observation']['target_observed'])
        pos=self.position();pos.update(index='SENSEX',entry_price=305.35,highest_ltp=305.35,
                                      current_sl=285.35,initial_risk_points=20,qty=60,lots=3)
        pos['market_structure']['telegram_call']['targets']=[325,360,420]
        self.assertIsNone(engine.protect_channel_position(pos,314,1))
        self.assertEqual(pos['current_sl'],285.35)
        self.assertIsNone(engine.protect_channel_position(pos,316,2))
        self.assertEqual(pos['current_sl'],285.35)
        self.assertEqual(engine.protect_channel_position(pos,280,3),'INITIAL_SL_HIT')
        self.assertFalse(pos['channel_exit_observation']['target_observed'])

    def test_held_depth_uses_quote_endpoint_and_walks_depth_without_full_chain(self):
        import dhan_bot_engine as engine
        import logging
        from unittest.mock import patch
        client=engine.DhanV2RestClient('test','test',logging.getLogger('quote_test'))
        quote={'data':{'NSE_FNO':{'53794':{'last_price':140.3,'depth':{
            'buy':[{'price':140,'quantity':65},{'price':139.8,'quantity':65}],
            'sell':[{'price':140.4,'quantity':130}]}}}}}
        with patch.object(client,'_request',return_value=quote) as request:
            response=client.fetch_held_channel_quote(self.position())
        self.assertEqual(request.call_args.args[:2],('POST','/marketfeed/quote'))
        node=response['oc']['22800.000000']['ce']
        self.assertAlmostEqual(node['top_bid_price'],139.9)
        self.assertEqual(node['top_bid_quantity'],130)
        with patch.object(client,'_request',return_value={}),patch.object(engine.time,'sleep'):
            self.assertIsNone(client.fetch_held_channel_quote(self.position()))

    def test_all_fresh_calls_are_visible_when_newest_is_not_executable(self):
        f=DualChannelFeedTests().feed()
        now=datetime.fromisoformat('2026-10-29T03:49:00+00:00').timestamp()
        older=f.ingest('BUY NIFTY 22650 CE NEAR LEVEL -- 189-193 TARGET -- 240-285',now-120,'grow',now,'GrowX')
        newer=f.ingest('NIFTY 22650 PE BUY ABOVE: 170',now-60,'sac',now,'Tradewithsac')
        f.ingest('TRADE ACTIVATED NIFTY 22650 PE T1: 180',now-30,'activated',now,'Tradewithsac')
        self.assertEqual([c['key'] for c in f.candidates('NIFTY',now,[])],[newer['key'],older['key']])
        self.assertEqual([c['key'] for c in f.candidates('NIFTY',now,[newer['key']])],[older['key']])


class ChannelNovemberThirdRegressionTests(unittest.TestCase):
    def test_buy_near_alert_matches_activation_and_preserves_targets(self):
        from telegram_notifier import TelegramCallFeed
        feed=DualChannelFeedTests().feed()
        posted=datetime.fromisoformat('2026-11-03T09:19:35+05:30').timestamp()
        for wording in ('BUY NEAR : ₹125','BUY NEAR: Rs.125','BUY NEAR 125'):
            call,_=TelegramCallFeed.parse_message('NIFTY 23450 CE '+wording,posted,'sac',posted+2)
            self.assertEqual(call['entry_kind'],'NEAR')
            self.assertEqual(TelegramCallFeed.entry_bounds(call),(123.75,126.25))
        feed.ingest('NIFTY 23450 CE BUY NEAR : ₹125',posted,'sac',posted+2,'Tradewithsac')
        feed.ingest('TRADE ACTIVATED NIFTY 23450 CE T1: ₹135 T2: ₹150 T3: ₹170++',posted+97,'activation',posted+99,'Tradewithsac')
        candidate=feed.candidate('NIFTY',posted+100,[])
        self.assertEqual(candidate['activation_epoch'],posted+97)
        self.assertEqual(feed.targets_for(candidate,posted+100),[135,150,170])
        self.assertEqual(feed.candidates('NIFTY',posted+100,[candidate['key']]),[])

    def test_single_near_quote_and_modeled_fill_have_real_execution_room(self):
        import math
        import dhan_bot_engine as engine
        from telegram_notifier import TelegramCallFeed
        now=datetime.fromisoformat('2026-11-03T09:15:31+05:30').timestamp()
        call,_=TelegramCallFeed.parse_message('BUY NIFTY 23300 CE NEAR LEVEL -- 170 TARGET -- 240-285',now-10,'growx',now,'GrowX')
        self.assertEqual(call['entry_kind'],'NEAR')
        floor,cap=TelegramCallFeed.entry_bounds(call)
        self.assertAlmostEqual(floor,168.3);self.assertAlmostEqual(cap,171.7)
        leg={'security_id':'123','last_price':170,'top_bid_price':169.9,'top_ask_price':170.2,
             'top_bid_quantity':650,'top_ask_quantity':650,'greeks':{'delta':.94,'theta':-2}}
        view={'index':'NIFTY','market_structure':{}}
        check=lambda node:engine.channel_entry_view(view,{'23300':{'ce':node}},call,'2026-11-03',now,now)
        self.assertIsNotNone(check(leg)[0])
        self.assertLessEqual(math.ceil(leg['top_ask_price']*1.001*20-1e-9)/20,cap)
        self.assertIsNone(check({**leg,'top_bid_price':168.25,'top_ask_price':168.4})[0])
        self.assertIsNone(check({**leg,'top_bid_price':172,'top_ask_price':172.1})[0])
        for delta in (0,-.8,1.01,float('nan')):
            with self.subTest(delta=delta):
                self.assertIsNone(check({**leg,'greeks':{'delta':delta,'theta':-2}})[0])
        for delta in (.12,.8,1):
            self.assertIsNotNone(check({**leg,'greeks':{'delta':delta,'theta':-2}})[0])
        self.assertIsNone(check({**leg,'greeks':{'theta':-2}})[0])

    def test_explicit_range_is_never_widened_and_near_still_expires(self):
        from telegram_notifier import TelegramCallFeed
        now=datetime.fromisoformat('2026-11-03T09:15:31+05:30').timestamp()
        for source,text in [('GrowX','BUY NIFTY 23300 CE NEAR LEVEL -- 170-173'),
                            ('Tradewithsac','NIFTY 23300 CE BUY NEAR: 170-173')]:
            call,_=TelegramCallFeed.parse_message(text,now,'test',now,source)
            self.assertEqual(TelegramCallFeed.entry_bounds(call),(170,173))
            self.assertFalse(TelegramCallFeed.entry_is_fresh(call,now+1801))
        self.assertEqual(TelegramCallFeed.entry_bounds({'buy_above':1000,'index':'SENSEX','entry_kind':'NEAR'}),(996,1004))

    def test_unsupported_contract_entry_is_visible_in_logs(self):
        feed=DualChannelFeedTests().feed()
        now=datetime.fromisoformat('2026-11-03T09:15:31+05:30').timestamp()
        with self.assertLogs(feed.logger,level='WARNING') as logs:
            feed.ingest('NIFTY 23300 CE BUY AROUND 170',now,'unsupported',now,'GrowX')
        self.assertIn('CHANNEL FORMAT REJECT',logs.output[0])
        self.assertEqual(feed.candidates('NIFTY',now,[]),[])


class MarketOpeningTimingTests(unittest.TestCase):
    def test_preopen_wait_never_sleeps_past_open(self):
        from datetime import time,timezone,timedelta
        from stock_option_policy import session_open_wait_seconds
        for zone in (None,timezone(timedelta(hours=5,minutes=30))):
            for moment,expected in ((datetime(2026,11,5,9,10),1.0),
                                    (datetime(2026,11,5,9,14,59,950000),.05),
                                    (datetime(2026,11,5,9,15),0.0),
                                    (datetime(2026,11,5,9,16),0.0)):
                self.assertAlmostEqual(session_open_wait_seconds(moment.replace(tzinfo=zone),time(9,15)),expected)


class ChannelLifecycleAuditTests(unittest.TestCase):
    def test_november_fourth_wait_activation_does_not_consume_early(self):
        import dhan_bot_engine as engine
        feed=DualChannelFeedTests().feed()
        posted=datetime.fromisoformat('2026-11-04T09:59:54+05:30').timestamp()
        call=feed.ingest('NIFTY 23800 PE BUY ABOVE: 152',posted,'alert',posted+39,'Tradewithsac')
        feed.ingest('Wait for trigger',posted+4,'wait',posted+39,'Tradewithsac')
        early=posted+61
        leg={'security_id':'123','last_price':153.7,'top_bid_price':153.6,'top_ask_price':153.75,
             'top_bid_quantity':650,'top_ask_quantity':650,'greeks':{'delta':-.5,'theta':-2}}
        self.assertIsNone(feed.candidate('NIFTY',early,[]))
        self.assertEqual(feed.decision_statuses('NIFTY',early,[])[0]['state'],'WAITING_FOR_ACTIVATION')
        self.assertIsNone(engine.channel_entry_view({'index':'NIFTY'}, {'23800':{'pe':leg}},call,'2026-11-09',early,early)[0])
        activated=posted+1052
        feed.ingest('TRADE ACTIVATED NIFTY 23800 PE T1: 162 T2: 180 T3: 200',activated,'activation',activated+7,'Tradewithsac')
        ready=feed.candidate('NIFTY',activated+8,[])
        self.assertIsNotNone(ready)
        self.assertEqual(feed.targets_for(ready,activated+8),[162,180,200])
        self.assertIsNotNone(engine.channel_entry_view({'index':'NIFTY'}, {'23800':{'pe':leg}},ready,'2026-11-09',activated+8,activated+8)[0])
        self.assertEqual(feed.decision_statuses('NIFTY',activated+8,[call['key']])[0]['state'],'ALREADY_EXECUTED')
        self.assertIsNone(feed.candidate('NIFTY',activated+8,[call['key']]))

    def test_growx_needs_no_activation_but_catchup_never_renews_age(self):
        feed=DualChannelFeedTests().feed()
        posted=datetime.fromisoformat('2026-11-04T09:15:55+05:30').timestamp()
        text='BUY NIFTY 23750 CE NEAR LEVEL -- 170 TARGET -- 195-240'
        call=feed.ingest(text,posted,'growx',posted+1,'GrowX')
        self.assertIsNotNone(feed.candidate('NIFTY',posted+2,[]))
        for age in (5,10,100,301):feed.ingest(text,posted,'growx',posted+age,'GrowX')
        self.assertEqual(feed._calls[0]['first_received_epoch'],posted+1)
        self.assertEqual(feed.decision_statuses('NIFTY',posted+301,[])[0]['state'],'EXPIRED')
        self.assertIsNone(feed.candidate('NIFTY',posted+301,[]))

    def test_restored_legacy_sac_alert_waits_and_old_formats_do_not_warn(self):
        feed=DualChannelFeedTests().feed()
        posted=datetime.fromisoformat('2026-11-04T09:59:54+05:30').timestamp()
        call=feed.ingest('NIFTY 23800 PE BUY ABOVE: 152',posted,'alert',posted+1,'Tradewithsac')
        saved=feed.pending_snapshot(posted+2);saved[0].pop('requires_activation')
        restored=DualChannelFeedTests().feed();restored.restore_pending(saved,posted+3)
        self.assertIsNone(restored.candidate('NIFTY',posted+4,[]))
        with patch.object(restored.logger,'warning') as warn:
            restored.ingest('NIFTY 23800 PE BUY AROUND 152',posted-86400,'old',posted,'Tradewithsac')
        warn.assert_not_called()

    def test_account_feed_failure_retries_without_enabling_candidates(self):
        import threading
        feed=DualChannelFeedTests().feed();feed._authorized_mode=True;feed._closing=MagicMock()
        feed._closing.is_set.return_value=False
        feed._closing.wait.side_effect=[False,True]
        with patch.object(feed,'_account_feed',new=MagicMock(return_value=None)),patch('telegram_notifier.asyncio.run',side_effect=ConnectionError):
            feed._run()
        self.assertEqual(feed._closing.wait.call_count,2)
        self.assertEqual(feed.connection_status,'UNAVAILABLE')
        self.assertEqual(feed.candidates('NIFTY',0,[]),[])


class PersistenceFailureAuditTests(unittest.TestCase):
    def test_exhausted_file_replacements_are_reported_not_silently_successful(self):
        from unittest.mock import mock_open
        import dhan_bot_engine as index,dhan_dynamic_fno_stock_bot as stock
        for module in (index,stock):
            with self.subTest(module=module.__name__),patch('builtins.open',mock_open()),patch.object(module.os,'replace',side_effect=PermissionError) as replace,patch.object(module.time,'sleep'):
                with self.assertRaisesRegex(OSError,'Could not persist JSON'):
                    module.atomic_write_json('audit-ledger.json',{'value':1})
                self.assertEqual(replace.call_count,10)

import copy
import json
import logging
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import MagicMock,patch

import dhan_bot_engine as bot
from stock_option_policy import rank_candidates


def rule():
    return dict(status='CANDIDATE',trades_count=0,wins_count=0,losses_count=0,
                unique_days_traded=[],total_win_r=0.,total_loss_r=0.,avg_win_r=0.,
                avg_loss_r=0.,intraday_losses=0)


class LearningFeedbackTests(unittest.TestCase):
    def sizing_brain_and_view(self,index='NIFTY'):
        brain=bot.SelfLearningBrain.__new__(bot.SelfLearningBrain)
        brain.rules={'fresh':rule()}
        view={'index':index,'recommended_dir':'BUY_CE','atm_ce_bid':179.8,'atm_ce_ask':180,
              'atm_ce_bid_qty':650,'atm_ce_ask_qty':650,'atm_ce_delta':.5,
              'market_structure':{'ready':True,'allowed':True,'room_points':70 if index=='SENSEX' else 30,
                  'structure_rr':2.4,'confirmation_body_fraction':.8,'confirmation_close_strength':.85,
                  'confirmation_age_seconds':15,'level_reference_age_seconds':300,
                  'strike_oi_confirmation':[{'strike':22600,'side':'pe','change_over_5m':10000}]}}
        return brain,view

    def test_fresh_confirmed_setup_can_size_two_lots_without_learning_history(self):
        brain,view=self.sizing_brain_and_view()
        self.assertEqual(brain.compute_lots('NIFTY','fresh',180.9,40000,0,analysis=view),2)
        sizing=view['market_structure']['lot_sizing']
        self.assertLessEqual(sizing['estimated_stop_risk_rs'],2000)
        self.assertFalse(sizing['probability_estimated'])

    def test_exceptional_setup_can_size_three_but_depth_and_risk_cap_bind(self):
        brain,view=self.sizing_brain_and_view()
        view.update(atm_ce_bid=99.8,atm_ce_ask=100)
        view['market_structure'].update(room_points=50,structure_rr=3.2,confirmation_close_strength=.9)
        self.assertEqual(brain.compute_lots('NIFTY','fresh',100.1,40000,0,analysis=view),3)
        view['atm_ce_bid_qty']=130
        self.assertEqual(brain.compute_lots('NIFTY','fresh',100.1,40000,0,analysis=view),2)
        brain,view=self.sizing_brain_and_view('SENSEX')
        view.update(atm_ce_bid=99.8,atm_ce_ask=100)
        self.assertEqual(brain.compute_lots('SENSEX','fresh',100.1,10000,0,analysis=view),1)

    def test_size_up_fails_closed_on_weak_stale_missing_or_losing_context(self):
        mutations=(lambda v:v['market_structure'].update(confirmation_body_fraction=.4),
            lambda v:v['market_structure'].update(confirmation_age_seconds=121),
            lambda v:v['market_structure'].update(strike_oi_confirmation=[]),
            lambda v:v['market_structure'].pop('confirmation_close_strength'),
            lambda v:v.update(atm_ce_ask=float('nan')),
            lambda v:v.update(atm_ce_bid_qty=65),
            lambda v:v['market_structure'].update(room_points=10),
            lambda v:v.update(atm_ce_bid=176))
        for mutate in mutations:
            brain,view=self.sizing_brain_and_view();mutate(view)
            self.assertEqual(brain.compute_lots('NIFTY','fresh',180.9,40000,0,analysis=view),1)
        brain,view=self.sizing_brain_and_view()
        self.assertEqual(brain.compute_lots('NIFTY','fresh',180.9,40000,1,analysis=view),1)
        brain.rules['fresh']['last_lot_loss']=True
        self.assertEqual(brain.compute_lots('NIFTY','fresh',180.9,40000,0,analysis=view),1)

    def test_explicit_entry_risk_is_used_for_net_learning_on_flat_exit(self):
        ledger=dict(initial_capital=30000,virtual_balance_gross=30000,
                    virtual_balance_net=30000,trades=[])
        brain=MagicMock()
        with patch.object(bot,'atomic_write_json'):
            bot.record_closed_trade(ledger,brain,'2026-10-23','2026-10-23 10:00:00',
                '2026-10-23 10:05:00','NIFTY',22000,'CE',1,65,22000,0,
                150,150,0,'TIME_EXIT','rule',[0]*10,'PAPER',initial_risk_points=12.5)
        self.assertEqual(ledger['trades'][0]['initial_risk_points'],12.5)
        self.assertAlmostEqual(brain.learn_from_trade.call_args.args[4],-64.5/(12.5*65),places=6)

    def test_halving_preserves_win_and_consistency(self):
        stats={'wins_count':1,'losses_count':3,'total_win_r':2,'total_loss_r':3}
        bot.SelfLearningBrain.halve_rule_history(stats)
        self.assertEqual((stats['wins_count'],stats['losses_count'],stats['trades_count']),(1,1,2))

    def test_cooldown_and_retirement(self):
        brain=bot.SelfLearningBrain.__new__(bot.SelfLearningBrain)
        brain.rules_file='unused';brain.recent_losing_states=[]
        brain.rules={
            'a':{'status':'PRUNED','pruned_on_day':'2026-10-01','prune_count':1},
            'b':{'status':'PRUNED','pruned_on_day':'2026-10-01','prune_count':3}}
        with patch.object(bot,'atomic_write_json'):
            brain.unmute_new_day('2026-10-05')
            self.assertEqual(brain.rules['a']['status'],'PRUNED')
            brain.unmute_new_day('2026-10-06')
            self.assertEqual(brain.rules['a']['status'],'CANDIDATE')
            self.assertEqual(brain.rules['b']['status'],'PRUNED')

    def test_gross_profit_net_loss_trains_as_loss(self):
        ledger=dict(initial_capital=30000,virtual_balance_gross=30000,
                    virtual_balance_net=30000,trades=[])
        brain=MagicMock()
        with patch.object(bot,'atomic_write_json'):
            bot.record_closed_trade(ledger,brain,'2026-10-23','2026-10-23 10:00:00',
                '2026-10-23 10:05:00','NIFTY',22000,'CE',1,65,22000,0,
                100,100.5,0.1,'TIME_EXIT','rule',[0]*10,'PAPER')
        trade=ledger['trades'][0]
        self.assertGreater(trade['gross_pnl'],0)
        self.assertLess(trade['net_pnl'],0)
        self.assertLess(brain.learn_from_trade.call_args.args[4],0)
        self.assertEqual(trade['learning_reward_basis'],'NET_AFTER_MODELED_CHARGES')

    def test_entry_review_records_only_known_closed_bars_without_training(self):
        import test_nifty_market_structure as fixtures
        fixture=fixtures.MarketStructureTests();fixture.setUp()
        expected=len(fixture.data['timestamp'])
        fixture.add(datetime(2026,10,22,9,40,tzinfo=fixtures.IST),23045,24000,22000,23000)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder,'audit.jsonl')
            self.assertTrue(bot.record_nifty_entry_observation(path,{'spot':23045},fixture.data,fixture.chain,fixture.now))
            saved=json.loads(path.read_text())
            self.assertEqual(len(saved['closed_index_candles']['timestamp']),expected)
            self.assertEqual(saved['purpose'],'ENTRY_REVIEW_ONLY_NOT_REALIZED_PNL_OR_TRAINING')
            self.assertNotIn('gross_pnl',saved)

    def test_losses_persist_and_mute_rule_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            brain_path=str(Path(folder,'brain.json'));rules_path=str(Path(folder,'rules.json'))
            brain=bot.SelfLearningBrain(logging.getLogger('test'),brain_path,rules_path)
            brain.rules['pattern']=rule()
            brain.learn_from_trade('2026-10-23','pattern',[1]*10,1,-1)
            reloaded=bot.SelfLearningBrain(logging.getLogger('test'),brain_path,rules_path)
            reloaded.unmute_new_day('2026-10-23')
            self.assertEqual(reloaded.rules['pattern']['intraday_losses'],1)
            self.assertEqual(reloaded.recent_losing_states,[('BUY_CE',[1]*10)])
            reloaded.learn_from_trade('2026-10-23','pattern',[1]*10,1,-1)
            self.assertEqual(reloaded.rules['pattern']['status'],'DAY_MUTED')
            self.assertEqual(json.loads(Path(rules_path).read_text())['pattern']['losses_count'],2)
            self.assertNotEqual(reloaded.brain['weights'][1],brain.brain['weights'][1])

    def test_repeated_day_initialization_does_not_erase_loss_state(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            brain.unmute_new_day('2026-10-23')
            brain.recent_losing_states.append(('BUY_CE',[1]*10))
            brain.unmute_new_day('2026-10-23')
            self.assertEqual(len(brain.recent_losing_states),1)

    def test_size_up_requires_ten_trades_six_days_and_loss_reduces_it(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'));brain.rules['pattern']=rule()
            for day in ('2026-10-20','2026-10-21','2026-10-22'):
                brain.unmute_new_day(day)
                brain.learn_from_trade(day,'pattern',[1]*10,1,1.5)
            self.assertEqual(brain.rules['pattern']['status'],'CANDIDATE')
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)
            for offset in range(7):
                day=(datetime(2026,10,23)+timedelta(days=offset)).strftime('%Y-%m-%d')
                brain.unmute_new_day(day)
                brain.learn_from_trade(day,'pattern',[1]*10,1,1.5)
                if offset<6:self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)
            self.assertEqual(brain.rules['pattern']['status'],'ACTIVE')
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),2)
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,39999,0),1)
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,100000,0),2)
            brain.learn_from_trade('2026-10-30','pattern',[1]*10,1,-1)
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)

    def test_zero_win_pruning_waits_for_seven_trades_and_four_days(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'));brain.rules['pattern']=rule()
            for number,day in enumerate([(datetime(2026,10,20)+timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)],1):
                brain.unmute_new_day(day)
                brain.learn_from_trade(day,'pattern',[0]*10,1,-1)
                self.assertEqual(brain.rules['pattern']['status'],'PRUNED' if number==7 else 'CANDIDATE')

    def test_four_day_strong_wins_promote_but_do_not_size_up(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'));brain.rules['pattern']=rule()
            for day in ('2026-10-20','2026-10-21','2026-10-22','2026-10-23'):
                brain.unmute_new_day(day)
                brain.learn_from_trade(day,'pattern',[0]*10,1,1)
            self.assertEqual(brain.rules['pattern']['status'],'ACTIVE')
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,50000,0),1)

    def test_weight_update_uses_requested_learning_rate_without_decay(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            brain.brain['weights'][1]=[0.2]*10;brain.brain['bias'][1]=0.1
            brain.learn_from_trade('2026-10-23','unknown',[0]*10,1,1)
            self.assertEqual(brain.brain['weights'][1],[0.2]*10)
            self.assertEqual(brain.brain['bias'][1],0.1405)
            self.assertEqual(brain.brain['replay_buffer'][-1]['reward_basis'],'NET_AFTER_MODELED_CHARGES')

    def test_large_reward_uses_normalized_bounded_rate_and_buffer_is_bounded_once(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            brain.brain['weights'][1]=[0]*10;brain.brain['bias'][1]=0
            brain.brain['replay_buffer']=[{'day':'old'} for _ in range(120)]
            brain.learn_from_trade('2026-10-23','unknown',[1]*10,1,2)
            self.assertEqual(brain.brain['weights'][1],[round(0.045*2/11,6)]*10)
            self.assertEqual(brain.brain['bias'][1],round(0.045*2/11,6))
            self.assertEqual(brain.brain['model_role'],'DIAGNOSTIC_ONLY_RULES_CONTROL_TRADING')
            self.assertEqual(len(brain.brain['replay_buffer']),120)
            self.assertEqual(sum(t.get('day')=='2026-10-23' for t in brain.brain['replay_buffer']),1)

    def test_size_up_needs_independent_days_and_sixty_percent_affordability(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            brain.rules['pattern']={**rule(),'status':'ACTIVE','trades_count':10,'wins_count':8,
                                    'expectancy_r':1.5,'avg_win_r':2.5,'avg_loss_r':1.0,'unique_days_traded':['2026-10-20']}
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)
            brain.rules['pattern']['unique_days_traded']=[str(i) for i in range(6)]
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),2)
            brain.rules['pattern']['avg_win_r']=2.19
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)
            brain.rules['pattern']['avg_win_r']=2.5
            self.assertEqual(brain.compute_lots('NIFTY','pattern',200,40000,0),1)
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,1),1)

    def stock_history(self,net=-100,days=5):
        now=datetime(2026,10,23,10,tzinfo=timezone.utc)
        candidate=dict(symbol='TEST',scanner='BULLISH',opt_type='CE',signal_quality=80)
        trades=[dict(symbol='TEST',scanner='BULLISH',opt_type='CE',net_pnl=net,
                     learning_risk_rs=100,exit_timestamp=(now-timedelta(days=i)).isoformat()) for i in range(1,days+1)]
        return now,candidate,trades

    def test_stock_repeated_net_losses_pause_and_cooldown_expires(self):
        now,c,trades=self.stock_history()
        self.assertEqual(rank_candidates([c],trades,now.timestamp()),[])
        later=rank_candidates([c],trades,(now+timedelta(days=5)).timestamp())
        self.assertEqual(len(later),1)
        self.assertLess(later[0]['learning_feedback']['quality_adjustment'],0)

    def test_stock_positive_closed_results_improve_rank_without_changing_risk(self):
        now,c,trades=self.stock_history(100)
        other={**c,'symbol':'OTHER','signal_quality':82}
        ranked=rank_candidates([other,c],trades,now.timestamp())
        self.assertEqual(ranked[0]['symbol'],'TEST')
        self.assertNotIn('lots',ranked[0])

    def test_stock_future_results_and_other_symbols_do_not_leak_into_ranking(self):
        now,c,trades=self.stock_history()
        for t in trades:t['exit_timestamp']=(now+timedelta(days=1)).isoformat()
        ranked=rank_candidates([c],trades,now.timestamp())
        self.assertEqual(ranked[0]['learning_feedback']['closed_trades'],0)
        for t in trades:
            t['symbol']='OTHER';t['exit_timestamp']=(now-timedelta(days=1)).isoformat()
        self.assertEqual(rank_candidates([c],trades,now.timestamp())[0]['learning_feedback']['closed_trades'],0)


class GeminiReviewTests(unittest.TestCase):
    def evaluator(self, decision=None):
        import dhan_gemini_options_bot as gemini
        session=MagicMock()
        session.post.return_value.status_code=200
        result=decision or {'decision':'BUY_CE','strike':22050,'confidence_score':8,
                           'expected_target_points':20,'reasoning':'Structure and OI align'}
        session.post.return_value.json.return_value={'candidates':[{'finishReason':'STOP',
            'content':{'parts':[{'text':json.dumps(result)}]}}]}
        evaluator=gemini.GeminiMarketEvaluator('unit-test-only',logging.getLogger('test'),session=session)
        return gemini,evaluator,session

    def context(self):
        return {'decision':'BUY_CE','strike':22050,'target_points':20}

    def test_matching_approval_and_header_only_credentials(self):
        gemini,evaluator,session=self.evaluator()
        with patch.object(gemini.time,'monotonic',return_value=100):
            result=evaluator.analyze_setup(self.context())
        self.assertTrue(result['approved'])
        url=session.post.call_args.args[0]
        payload=session.post.call_args.kwargs['json']
        self.assertNotIn('unit-test-only',url)
        self.assertNotIn('unit-test-only',json.dumps(payload))
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
        self.assertEqual(payload['generationConfig']['thinkingConfig']['thinkingBudget'],0)

    def test_wrong_contract_direction_score_target_and_nonfinite_are_rejected(self):
        base={'decision':'BUY_CE','strike':22050,'confidence_score':8,
              'expected_target_points':20,'reasoning':'Review'}
        for edit in ({'strike':22100},{'decision':'BUY_PE'},{'decision':'HOLD'},
                     {'confidence_score':7},{'confidence_score':True},
                     {'expected_target_points':19.9},{'expected_target_points':float('nan')}):
            with self.subTest(edit=edit):
                gemini,evaluator,_=self.evaluator({**base,**edit})
                with patch.object(gemini.time,'monotonic',return_value=100):
                    self.assertFalse(evaluator.analyze_setup(self.context())['approved'])

    def test_api_error_and_malformed_output_hold_without_raw_exception(self):
        import requests
        gemini,evaluator,session=self.evaluator()
        session.post.side_effect=requests.Timeout('unit-test-only must not be exposed')
        with patch.object(gemini.time,'monotonic',return_value=100):
            result=evaluator.analyze_setup(self.context())
        self.assertEqual(result['decision'],'HOLD')
        self.assertNotIn('unit-test-only',str(result))
        session.post.side_effect=None
        session.post.return_value.json.return_value={'candidates':[]}
        with patch.object(gemini.time,'monotonic',return_value=140):
            self.assertFalse(evaluator.analyze_setup(self.context())['approved'])

    def test_review_interval_and_late_response_never_reuse_approval(self):
        gemini,evaluator,session=self.evaluator()
        with patch.object(gemini.time,'monotonic',return_value=100):
            self.assertTrue(evaluator.analyze_setup(self.context())['approved'])
            self.assertFalse(evaluator.analyze_setup(self.context())['approved'])
        self.assertEqual(session.post.call_count,1)
        with patch.object(gemini.time,'monotonic',side_effect=[140,149]):
            self.assertFalse(evaluator.analyze_setup(self.context())['approved'])

    def test_base_hold_does_not_call_gemini(self):
        import dhan_gemini_options_bot as gemini
        evaluator=MagicMock()
        result=gemini.select_with_gemini(MagicMock(),{},0,None,evaluator,MagicMock(return_value=('HOLD',None)))
        self.assertEqual(result,('HOLD',None))
        evaluator.analyze_setup.assert_not_called()

    def test_cutoff_crossed_during_review_blocks_entry(self):
        import dhan_gemini_options_bot as gemini
        before=datetime(2026,10,23,14,29,59,tzinfo=bot.IST)
        after=datetime(2026,10,23,14,30,1,tzinfo=bot.IST)
        analysis={'snapshot_received_epoch':before.timestamp()}
        evaluator=MagicMock()
        evaluator.analyze_setup.return_value={'approved':True}
        with patch.object(gemini.engine,'datetime') as clock,patch.object(gemini,'candidate_context',return_value=self.context()):
            clock.now.side_effect=[before,after]
            result=gemini.select_with_gemini(MagicMock(),analysis,0,None,evaluator,MagicMock(return_value=('BUY_CE','rule')))
        self.assertEqual(result[0],'HOLD')
        self.assertIn('cutoff',analysis['entry_decision']['reason'])

    def test_fresh_approved_candidate_keeps_original_action_and_rule(self):
        import dhan_gemini_options_bot as gemini
        now=datetime(2026,10,23,10,tzinfo=bot.IST)
        analysis={'index':'NIFTY','atm_ce_ltp':100,'atm_ce_strike':22050,
                  'snapshot_received_epoch':now.timestamp(),'market_structure':{'allowed':True}}
        review={'approved':True,'decision':'BUY_CE','strike':22050,'confidence_score':8}
        evaluator=MagicMock();evaluator.analyze_setup.return_value=review
        with patch.object(bot,'datetime') as clock:
            clock.now.return_value=now
            result=gemini.select_with_gemini(MagicMock(),analysis,0,None,evaluator,MagicMock(return_value=('BUY_CE','original-rule')))
        self.assertEqual(result,('BUY_CE','original-rule'))
        self.assertEqual(analysis['market_structure']['gemini_review'],review)

    def test_stale_snapshot_cannot_request_gemini_approval(self):
        import dhan_gemini_options_bot as gemini
        now=datetime(2026,10,23,10,tzinfo=bot.IST)
        analysis={'snapshot_received_epoch':now.timestamp()-9}
        evaluator=MagicMock()
        with patch.object(bot,'datetime') as clock:
            clock.now.return_value=now
            result=gemini.select_with_gemini(MagicMock(),analysis,0,None,evaluator,MagicMock(return_value=('BUY_CE','rule')))
        self.assertEqual(result[0],'HOLD')
        evaluator.analyze_setup.assert_not_called()

    def test_launcher_defaults_to_paper_and_restores_engine_after_error(self):
        import dhan_gemini_options_bot as gemini
        original,cutoff=bot.SelfLearningBrain.select_action,bot.LAST_ENTRY_TIME
        env={'DHAN_CLIENT_ID':'test','DHAN_ACCESS_TOKEN':'test','GEMINI_API_KEY':'unit-test-only'}
        with patch.object(bot,'load_dotenv_file',return_value=env),patch.object(bot,'setup_logger'),\
             patch.object(gemini,'GeminiMarketEvaluator') as factory,\
             patch.object(bot,'run_intraday_market_session',side_effect=RuntimeError('stop')) as run:
            with self.assertRaises(RuntimeError):gemini.main([])
        self.assertFalse(run.call_args.args[0])
        self.assertIs(bot.SelfLearningBrain.select_action,original)
        self.assertEqual(bot.LAST_ENTRY_TIME,cutoff)
        factory.return_value.close.assert_called_once()

    def test_missing_key_exits_before_any_broker_session(self):
        import dhan_gemini_options_bot as gemini
        with patch.dict('os.environ',{},clear=True),patch.object(bot,'load_dotenv_file',return_value={}),\
             patch.object(bot,'run_intraday_market_session') as run,patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:gemini.main([])
        self.assertEqual(error.exception.code,2)
        run.assert_not_called()

class AdaptiveSetupFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,30,18,tzinfo=bot.IST)
        self.candidate={'symbol':'NEW_STOCK','scanner':'CONFIRMED_BREAKOUT_CE','opt_type':'CE',
                        'signal_quality':70,'expected_net_rr':1.5,
                        'breakout_confirmation':{'entry_path':'DIRECT_BREAKOUT'}}

    def trades(self,rewards):
        result=[]
        for i,reward in enumerate(rewards):
            stamp=self.now-timedelta(days=3-i//3,minutes=30-i%3)
            result.append({'symbol':'STOCK_'+str(i),'scanner':'CONFIRMED_BREAKOUT_CE','opt_type':'CE',
                'entry_id':'trade_'+str(i),'timestamp':(stamp-timedelta(minutes=5)).isoformat(),
                'exit_timestamp':stamp.isoformat(),'net_pnl':reward*1000,'gross_pnl':reward*1000+100,
                'learning_risk_rs':1000,'entry_price':100,'qty':100,
                'data_source':'PAPER_BID_ASK','exit_diagnostics':{'peak_executable_vwap':116},
                'entry_diagnostics':{'breakout_confirmation':{'entry_path':'DIRECT_BREAKOUT'}}})
        return result

    def feedback(self,trades,at=None,candidate=None):
        from stock_option_policy import adaptive_setup_feedback
        return adaptive_setup_feedback(candidate or self.candidate,trades,(at or self.now).timestamp())

    def test_shared_evidence_transfers_to_new_symbol_and_poor_setups_pause_then_recover(self):
        trades=self.trades([-.8]*12)
        feedback=self.feedback(trades)
        self.assertTrue(feedback['paused'])
        self.assertLess(feedback['quality_adjustment'],0)
        self.assertEqual(rank_candidates([self.candidate],trades,self.now.timestamp()),[])
        self.assertFalse(self.feedback(trades,self.now+timedelta(days=2))['paused'])
        winners=self.trades([1]*12)
        ranked=rank_candidates([self.candidate],winners,self.now.timestamp())
        self.assertGreater(ranked[0]['learning_feedback']['quality_adjustment'],0)
        self.assertEqual(ranked[0]['expected_net_rr'],1.5)

    def test_small_samples_single_day_future_duplicates_and_bad_origin_cannot_train(self):
        self.assertEqual(self.feedback(self.trades([1]*7))['quality_adjustment'],0)
        rows=self.trades([1]*12)
        for row in rows:row['exit_timestamp']=self.now.isoformat()
        self.assertEqual(self.feedback(rows)['quality_adjustment'],0)
        self.assertEqual(self.feedback(self.trades([1]*12),self.now-timedelta(days=5))['closed_trades'],0)
        rows=self.trades([1]*12)
        self.assertEqual(self.feedback(rows+rows)['closed_trades'],12)
        for row in rows:row['data_source']='DHAN_REAL_HISTORICAL'
        self.assertEqual(self.feedback(rows)['closed_trades'],0)
        rows=self.trades([1]*12)
        for row in rows:row['learning_risk_rs']=0
        self.assertEqual(self.feedback(rows)['closed_trades'],0)

    def test_validation_disagreement_does_not_promote_and_giveback_tightens_protection(self):
        feedback=self.feedback(self.trades([1]*8+[-.5]*4))
        self.assertEqual(feedback['quality_adjustment'],0)
        self.assertEqual(feedback['exit_policy'],'PROTECT_EARNED_PROFIT')
        self.assertFalse(feedback['paused'])

    def test_stock_protection_uses_frozen_entry_feedback_without_changing_risk(self):
        import argparse
        import dhan_dynamic_fno_stock_bot as stock
        cfg=stock.DynamicStrategyConfig({},argparse.Namespace())
        when=self.now.replace(hour=12)
        base={'entry_price':100,'qty':100,'lots':1,'entry_epoch':when.timestamp()-30,
              'peak_bid':100,'current_sl':90,'initial_sl':90,'opt_1r':10,'trail_jump':5,
              'rupee_target_price':130,'adaptive_feedback':{'exit_policy':'PROTECT_EARNED_PROFIT'}}
        quote={'received_epoch':when.timestamp(),'depth':{'buy':[{'price':106.5,'quantity':100}]}}
        protected=copy.deepcopy(base);standard=copy.deepcopy(base);standard['adaptive_feedback']={}
        self.assertIsNone(stock.paper_exit(protected,quote,when,cfg))
        self.assertIsNone(stock.paper_exit(standard,quote,when,cfg))
        self.assertGreater(protected['current_sl'],100)
        self.assertEqual(standard['current_sl'],90)
        self.assertEqual(protected['initial_sl'],90)

    def test_channel_feedback_is_source_specific_and_entry_pause_is_enforced(self):
        rows=self.trades([-.8]*12)
        for row in rows:
            row.update(index='NIFTY',option_type='CE',market_structure={'telegram_call':{'source':'GrowX'}})
        call={'option_type':'CE','source':'GrowX','key':'fresh-call'}
        view={'index':'NIFTY'}
        result=bot.index_entry_signal(MagicMock(),view,0,self.now,None,call,rows)
        self.assertEqual(result,('HOLD',None))
        other={**call,'source':'Tradewithsac'}
        self.assertEqual(bot.index_entry_signal(MagicMock(),{'index':'NIFTY'},0,self.now,None,other,rows)[0],'BUY_CE')


if __name__=='__main__':unittest.main()

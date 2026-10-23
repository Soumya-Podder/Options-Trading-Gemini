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

    def test_proven_wins_enable_existing_sizing_and_a_loss_reduces_it(self):
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'));brain.rules['pattern']=rule()
            for day in ('2026-10-20','2026-10-21','2026-10-22'):
                brain.unmute_new_day(day)
                brain.learn_from_trade(day,'pattern',[1]*10,1,1)
            self.assertEqual(brain.rules['pattern']['status'],'ACTIVE')
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),2)
            brain.learn_from_trade('2026-10-23','pattern',[1]*10,1,-1)
            self.assertEqual(brain.compute_lots('NIFTY','pattern',150,40000,0),1)

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


if __name__=='__main__':unittest.main()

import unittest
from unittest.mock import MagicMock,patch
import dhan_bot_engine as b
class ScreenshotRules(unittest.TestCase):
 def test_daily_target_includes_prior_losses_and_partial_profit_once(self):
  p={'entry_price':100,'qty':65,'lots':1}
  self.assertTrue(b.daily_target_reached(0,p,120))
  self.assertFalse(b.daily_target_reached(-500,p,120))
  p={'entry_price':100,'qty':65,'lots':2,'exit_fills':[{'price':110,'qty':65}]}
  self.assertTrue(b.daily_target_reached(0,p,111))
  self.assertFalse(b.daily_target_reached(0,p,110))

 def test_stock_receipt_metadata_fails_closed(self):
  import dhan_dynamic_fno_stock_bot as stock
  import math
  self.assertTrue(math.isinf(stock.quote_received_age({},100)))
  self.assertTrue(math.isinf(stock.quote_received_age({'received_epoch':'bad'},100)))
  self.assertEqual(stock.quote_received_age({'received_epoch':90},100),10)

 def test_wall_direction(self):
  a={'recommended_dir':'BUY_CE','oi_analysis':{'ready':True,'room_to_resistance':24}}
  self.assertFalse(b.SelfLearningBrain.has_wall_room(a))
  a['oi_analysis']['room_to_resistance']=25
  self.assertTrue(b.SelfLearningBrain.has_wall_room(a))
 def test_halving_preserves_win_and_consistency(self):
  r={'wins_count':1,'losses_count':3,'total_win_r':2,'total_loss_r':3}
  b.SelfLearningBrain.halve_rule_history(r)
  self.assertEqual((r['wins_count'],r['losses_count'],r['trades_count']),(1,1,2))
 def test_cooldown_and_retirement(self):
  brain=b.SelfLearningBrain.__new__(b.SelfLearningBrain);brain.rules_file='unused';brain.recent_losing_states=[]
  brain.rules={'a':{'status':'PRUNED','pruned_on_day':'2026-10-01','prune_count':1},'b':{'status':'PRUNED','pruned_on_day':'2026-10-01','prune_count':3}}
  with patch.object(b,'atomic_write_json'):
   brain.unmute_new_day('2026-10-05');self.assertEqual(brain.rules['a']['status'],'PRUNED')
   brain.unmute_new_day('2026-10-06');self.assertEqual(brain.rules['a']['status'],'CANDIDATE');self.assertEqual(brain.rules['b']['status'],'PRUNED')
if __name__=='__main__':unittest.main()

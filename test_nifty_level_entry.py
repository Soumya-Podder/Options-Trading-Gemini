import copy
import logging
import unittest
from unittest.mock import patch

import dhan_bot_engine as bot
from nifty_market_structure import apply_nifty_level_entry
import test_nifty_market_structure as fixtures
from nifty_oi import NiftyOITracker
from test_nifty_oi import chain


class LevelEntryTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.MarketStructureTests();f.setUp()
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
            self.assertEqual(brain.select_action(self.analysis,1,bot.dtime(12,15))[0],'BUY_CE')
            self.assertEqual(self.analysis['entry_decision']['action'],'BUY_CE')

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
        self.analysis['oi_analysis']['room_to_resistance']=24
        with patch.object(bot.SelfLearningBrain,'_load_json',side_effect=lambda p,d:d),patch.object(bot,'atomic_write_json'):
            brain=bot.SelfLearningBrain(logging.getLogger('test'))
            self.assertEqual(brain.select_action(self.analysis,0),('HOLD',None))
        self.assertEqual(self.analysis['entry_decision']['reason'],'Insufficient room to nearest building OI wall')


if __name__=='__main__':unittest.main()

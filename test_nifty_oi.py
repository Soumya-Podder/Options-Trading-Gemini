import logging
import unittest
from unittest.mock import patch,MagicMock
from datetime import datetime
from nifty_oi import NiftyOITracker,IST
import dhan_bot_engine as bot


def chain(oi=10000,price=100):
    return {str(k):{side:{'oi':oi,'last_price':price,'previous_oi':8000,
                         'security_id':f'{k}_{side}'} for side in ('ce','pe')} for k in range(21800,22401,50)}


class OITests(unittest.TestCase):
    def setUp(self):
        self.tracker=NiftyOITracker()
        self.t=datetime(2026,10,23,10,tzinfo=IST).timestamp()

    def test_startup_requires_actual_five_minutes(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(11000),22025,self.t+299,'2026-10-27')
        self.assertIsNone(ref);self.assertFalse(info['ready'])

    def test_previous_day_and_intraday_changes_are_separate(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(11000),22025,self.t+300,'2026-10-27')
        row=next(r for r in info['nearby_strikes'] if r['strike']==22000 and r['side']=='pe')
        self.assertEqual(ref['ce_oi'],30000)
        self.assertEqual(row['change_from_previous_day'],3000)
        self.assertEqual(row['change_over_5m'],1000)

    def test_atm_shift_compares_same_new_basket(self):
        first=chain()
        for k,sides in first.items():
            for node in sides.values():node['oi']=float(k)
        self.tracker.assess(first,22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22075,self.t+300,'2026-10-27')
        self.assertTrue(info['ready'])
        self.assertEqual(ref['ce_oi'],22100+22150+22200)
        self.assertEqual(ref['atm_ce_strike'],22100)

    def test_expiry_change_resets_reference(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22025,self.t+300,'2026-11-03')
        self.assertIsNone(ref)

    def test_stale_reference_is_rejected(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        ref,info=self.tracker.assess(chain(),22025,self.t+391,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_security_id_change_is_rejected(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        changed=chain();changed['22050']['ce']['security_id']='OTHER_CONTRACT'
        ref,info=self.tracker.assess(changed,22025,self.t+300,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_missing_current_node_prevents_signal(self):
        self.tracker.assess(chain(),22025,self.t,'2026-10-27')
        changed=chain();del changed['22050']['ce']
        ref,info=self.tracker.assess(changed,22025,self.t+300,'2026-10-27')
        self.assertFalse(info['ready'])

    def test_missing_previous_oi_does_not_invent_daily_change(self):
        changed=chain();del changed['22000']['pe']['previous_oi']
        ref,info=self.tracker.assess(changed,22025,self.t,'2026-10-27')
        row=next(r for r in info['nearby_strikes'] if r['strike']==22000 and r['side']=='pe')
        self.assertIsNone(row['change_from_previous_day'])

    def test_unchanged_oi_is_not_short_covering(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        with patch('dhan_bot_engine.time.time',return_value=self.t):
            analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':chain()},'2026-10-27')
        with patch('dhan_bot_engine.time.time',return_value=self.t+300):
            result=analyzer.evaluate_chain('NIFTY',{'last_price':22030,'oc':chain(price=105)},'2026-10-27')
        self.assertEqual(result['ce_quadrant'],'NO_OI_CHANGE')
        self.assertEqual(result['pe_quadrant'],'NO_OI_CHANGE')
        self.assertEqual(result['recommended_dir'],'HOLD')

    def test_real_oi_changes_produce_direction_without_fake_clock(self):
        analyzer=bot.MarketMicrostructureAnalyzer()
        first=chain();second=chain()
        for sides in first.values():
            sides['ce']['oi']=10000000;sides['pe']['oi']=1000000
        for sides in second.values():
            sides['ce'].update(oi=11000000,last_price=90)
            sides['pe'].update(oi=2000000,last_price=110)
        analyzer.evaluate_chain('NIFTY',{'last_price':22025,'oc':first},'2026-10-27',snapshot_time=self.t)
        result=analyzer.evaluate_chain('NIFTY',{'last_price':22010,'oc':second},'2026-10-27',snapshot_time=self.t+300)
        self.assertTrue(result['oi_analysis']['ready'])
        self.assertEqual(result['ce_oi_change_5m'],3000000)
        self.assertEqual(result['pe_oi_change_5m'],3000000)
        self.assertEqual(result['recommended_dir'],'BUY_PE')

    def test_nifty_only_session_never_polls_sensex(self):
        clock=MagicMock()
        clock.now.return_value=datetime(2026,10,23,10)
        client=MagicMock();client.fetch_nearest_expiry.return_value='2026-10-27'
        client.fetch_option_chain.side_effect=[None,KeyboardInterrupt]
        with patch.object(bot,'datetime',clock),patch.object(bot,'load_dotenv_file',return_value={}),\
             patch.object(bot,'DhanV2RestClient',return_value=client),patch.object(bot,'SelfLearningBrain') as brain,\
             patch.object(bot,'TelegramNotifier'):
            brain._load_json.return_value={'trades':[]}
            with self.assertRaises(KeyboardInterrupt):
                bot.run_intraday_market_session(False,logging.getLogger('test'),['NIFTY'])
        client.fetch_nearest_expiry.assert_called_once_with(13,'IDX_I')
        self.assertTrue(all(call.args[0]==13 for call in client.fetch_option_chain.call_args_list))


if __name__=='__main__':unittest.main()

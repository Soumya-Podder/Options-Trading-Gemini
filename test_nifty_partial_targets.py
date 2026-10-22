import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import dhan_bot_engine as bot


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
        brain.learn_from_trade.assert_called_once()


if __name__=="__main__":
    unittest.main()

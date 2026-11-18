import ast
import unittest
from pathlib import Path
from stock_option_signals import evaluate_expert_scalp_exit

class ScalpExitTests(unittest.TestCase):
    def decision(self, net, peak, pressure=1.35):
        return evaluate_expert_scalp_exit(20,24,25,500,210,
            book_pressure_ratio=pressure,net_pnl=net,peak_net_pnl=peak)
    def test_strong_flow_holds(self):
        self.assertFalse(self.decision(1900,1900)[0])
    def test_stalled_flow_harvests(self):
        self.assertTrue(self.decision(1800,1800,.76)[0])
    def test_peak_giveback_protects(self):
        self.assertTrue(self.decision(1700,2500)[0])
    def test_net_loss_does_not_become_profit_exit(self):
        self.assertFalse(self.decision(-50,1700)[0])
    def test_each_engine_contains_active_call(self):
        for filename in ('dhan_bot_engine.py','dhan_dynamic_fno_stock_bot.py','dhan_mcx_mini_options_bot.py'):
            tree=ast.parse(Path(filename).read_text(encoding='utf-8'))
            self.assertTrue(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='evaluate_expert_scalp_exit' for n in ast.walk(tree)),filename)

if __name__=='__main__':unittest.main()

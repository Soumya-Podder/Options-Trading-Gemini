"""Offline tests for premium limits, cost-aware ranking and durable entry caps."""

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from stock_option_policy import DailyTradeGate, premium_plan, rank_candidates, signal_quality


def _reserve_worker(directory, start, results):
    start.wait(10)
    results.put(DailyTradeGate(directory).reserve("2026-10-22", {"symbol": "TEST"}))


class PremiumPlanTests(unittest.TestCase):
    def test_target_and_stop_respect_requested_percentages_after_rounding(self):
        plan = premium_plan(101.03, 1000, lambda *_: 40.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["hard_target_price"], 104.05)
        self.assertAlmostEqual(plan["initial_sl"], 100.55)
        self.assertLessEqual((plan["hard_target_price"] / 101.03 - 1) * 100, 3.0)
        self.assertLessEqual((1 - plan["initial_sl"] / 101.03) * 100, 0.5)
        self.assertEqual(plan["hard_target_price"], plan["min_rr_target_price"])
        self.assertEqual(plan["trail_jump"], 0)

    def test_charges_and_exit_slippage_are_included_in_net_ratio(self):
        calls = []

        def charges(buy, sell, qty):
            calls.append((buy, sell, qty))
            return 60.0

        plan = premium_plan(100, 1000, charges)
        self.assertIsNotNone(plan)
        self.assertEqual(calls, [(100.0, 103.0, 1000), (100.0, 99.5, 1000)])
        self.assertAlmostEqual(plan["expected_net_reward"], 2837.0)
        self.assertAlmostEqual(plan["expected_net_risk"], 659.5)
        self.assertAlmostEqual(plan["expected_net_rr"], 2837.0 / 659.5)
        self.assertAlmostEqual(plan["estimated_costs"], 163.0)
        self.assertGreaterEqual(plan["expected_net_rr"], 3.0)

    def test_costs_can_reject_a_gross_six_to_one_trade(self):
        self.assertIsNotNone(premium_plan(100, 50, lambda *_: 0, slippage_pct=0))
        self.assertIsNone(premium_plan(100, 50, lambda *_: 40, slippage_pct=0))

    def test_boundary_ratio_and_one_tick_risk(self):
        plan = premium_plan(10, 1000, lambda *_: 0, min_net_rr=6, slippage_pct=0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["opt_1r"], 0.05)
        self.assertAlmostEqual(plan["expected_net_rr"], 6.0)
        self.assertIsNone(premium_plan(9.99, 1000, lambda *_: 0, slippage_pct=0))
        self.assertIsNone(premium_plan(10, 1000, lambda *_: 0, min_net_rr=6.01, slippage_pct=0))

    def test_rejects_invalid_nonfinite_or_nonpositive_inputs(self):
        for value in (0, -1, float("nan"), float("inf"), "bad"):
            with self.subTest(entry=value):
                self.assertIsNone(premium_plan(value, 1000, lambda *_: 0))
        for qty in (0, -1, 1.5, True, float("inf")):
            with self.subTest(qty=qty):
                self.assertIsNone(premium_plan(100, qty, lambda *_: 0))
        for options in ({"stop_pct": 0}, {"stop_pct": 100}, {"target_pct": -1},
                        {"min_net_rr": 0}, {"slippage_pct": -0.1}):
            with self.subTest(options=options):
                self.assertIsNone(premium_plan(100, 1000, lambda *_: 0, **options))
        for invalid_fee in (-1, float("nan"), float("inf")):
            with self.subTest(invalid_fee=invalid_fee):
                self.assertIsNone(premium_plan(100, 1000, lambda *_: invalid_fee))


class RankingTests(unittest.TestCase):
    def test_quality_precedes_net_ratio_after_qualification(self):
        low = {"symbol": "FAST", "day_change_pct": 10, "signal_quality": 99, "expected_net_rr": 3}
        high = {"symbol": "CALM", "day_change_pct": 2.2, "signal_quality": 1, "expected_net_rr": 4}
        self.assertEqual(rank_candidates([low, high]), [low, high])

    def test_quality_then_symbol_then_original_order_break_ties(self):
        candidates = [
            {"symbol": "B", "expected_net_rr": 4, "signal_quality": 2},
            {"symbol": "A", "expected_net_rr": 4, "signal_quality": 2, "id": 1},
            {"symbol": "A", "expected_net_rr": 4, "signal_quality": 2, "id": 2},
            {"symbol": "Z", "expected_net_rr": 4, "signal_quality": 3},
            {"symbol": "EMPTY", "expected_net_rr": 4},
        ]
        self.assertEqual(rank_candidates(candidates), [candidates[i] for i in (3, 1, 2, 0, 4)])
        self.assertEqual(signal_quality({}), 0)
        self.assertEqual(signal_quality({"signal_quality": float("nan")}), 0)


class DailyTradeGateTests(unittest.TestCase):
    def test_reservation_survives_restart_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = DailyTradeGate(directory)
            self.assertIsNone(gate.read("2026-10-22"))
            self.assertTrue(gate.reserve("2026-10-22", {"symbol": "TEST"}))
            restarted = DailyTradeGate(directory)
            self.assertTrue(restarted.has_entry("2026-10-22"))
            self.assertFalse(restarted.reserve("2026-10-22", {"symbol": "SECOND"}))
            self.assertEqual(restarted.read("2026-10-22")["details"]["symbol"], "TEST")
            restarted.mark_closed("2026-10-22", {"net_pnl": -50})
            self.assertEqual(restarted.read("2026-10-22")["state"], "closed")
            self.assertFalse(restarted.reserve("2026-10-22", {}))
            self.assertTrue(restarted.reserve("2026-10-23", {}))

    def test_multiple_processes_can_reserve_exactly_once(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            start, results = context.Event(), context.Queue()
            workers = [context.Process(target=_reserve_worker, args=(directory, start, results)) for _ in range(4)]
            try:
                for worker in workers:
                    worker.start()
                start.set()
                outcomes = [results.get(timeout=15) for _ in workers]
                for worker in workers:
                    worker.join(timeout=10)
                    self.assertEqual(worker.exitcode, 0)
                self.assertEqual(outcomes.count(True), 1)
                self.assertEqual(outcomes.count(False), 3)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)
                results.close()

    def test_partial_or_corrupt_reservation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "2026-10-22.json").write_text("{", encoding="utf-8")
            gate = DailyTradeGate(directory)
            self.assertTrue(gate.has_entry("2026-10-22"))
            self.assertFalse(gate.reserve("2026-10-22", {}))
            self.assertEqual(gate.read("2026-10-22")["state"], "unreadable")

    def test_invalid_dates_and_invalid_details_do_not_consume_day(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = DailyTradeGate(directory)
            for day in ("../2026-10-22", "20261022", "2026-02-30"):
                with self.subTest(day=day), self.assertRaises(ValueError):
                    gate.reserve(day, {})
            with self.assertRaises(ValueError):
                gate.reserve("2026-10-22", {"invalid": float("nan")})
            self.assertFalse(gate.has_entry("2026-10-22"))
            with self.assertRaises(FileNotFoundError):
                gate.mark_closed("2026-10-22", {})


if __name__ == "__main__":
    unittest.main()

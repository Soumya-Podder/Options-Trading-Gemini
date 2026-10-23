import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import run_bot_session as session


class SessionLauncherTests(unittest.TestCase):
    def test_launches_both_engines_and_keeps_index_running_after_stock_finishes(self):
        for mode in ("PAPER", "LIVE"):
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
                self.assertEqual([c[1] for c in commands],
                                 ["dhan_bot_engine.py", "dhan_dynamic_fno_stock_bot.py", "bot_dashboard.py"])
                for command in commands[:2]:
                    self.assertEqual("--live" in command, mode == "LIVE")
                self.assertEqual(index.poll.call_count, 3)
                index.terminate.assert_not_called()
                stock.terminate.assert_not_called()
                dashboard.terminate.assert_called_once()

    def test_stock_start_failure_stops_already_started_index(self):
        index = MagicMock()
        index.poll.return_value = None
        with patch.object(session.subprocess, "Popen", side_effect=[index, OSError("start failed")]), \
             patch("builtins.print"):
            with self.assertRaises(OSError):
                session.main()
        index.terminate.assert_called_once()
        index.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()

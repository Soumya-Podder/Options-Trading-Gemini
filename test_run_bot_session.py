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
                self.assertTrue(all(c.kwargs['cwd']==session.PROJECT_DIR for c in launch.call_args_list))
                self.assertEqual([c[1] for c in commands],
                                 ["dhan_bot_engine.py", "dhan_dynamic_fno_stock_bot.py", "bot_dashboard.py"])
                self.assertEqual(commands[0][2:4],["--indices","NIFTY"])
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

    def test_index_finishing_first_does_not_stop_stock_engine(self):
        index,stock,dashboard=MagicMock(),MagicMock(),MagicMock()
        index.poll.return_value=0
        stock.poll.side_effect=[None,0,0]
        dashboard.poll.return_value=None
        with patch.object(session.subprocess,'Popen',side_effect=[index,stock,dashboard]),\
             patch.object(session.time,'sleep'),patch.object(session,'datetime') as clock,patch('builtins.print'):
            clock.now.return_value=datetime(2026,10,23,11)
            clock.strptime.side_effect=datetime.strptime
            session.main()
        self.assertEqual(stock.poll.call_count,3)
        stock.terminate.assert_not_called()
        index.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
DhanHQ v4.5 Automated Session Manager & Clean Shutdown Controller
=================================================================
Launches the real-data self-learning options bot (dhan_bot_engine.py)
and live terminal dashboard (bot_dashboard.py), monitors their runtime,
and triggers a clean shutdown at 03:46 PM IST.
"""

import os
import sys
import time
import subprocess
import signal
from datetime import datetime

# Load environment variables from local .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
PAPER_FLAG = os.environ.get("PAPER_MODE", "true").strip().lower()
MODE = os.environ.get("DHAN_BOT_MODE", "PAPER" if PAPER_FLAG == "true" else "LIVE").upper()
PYTHON_EXEC = sys.executable


def is_market_hours() -> bool:
    now = datetime.now()
    # Check if weekday (Monday = 0, Friday = 4)
    if now.weekday() >= 5:
        return False
    
    current_time = now.time()
    start_time = datetime.strptime("09:08", "%H:%M").time()
    end_time = datetime.strptime("15:46", "%H:%M").time()
    return start_time <= current_time <= end_time


def main():
    print(f"[{datetime.now()}] Session Manager started. Mode: {MODE}")
    
    bot_process = None
    dashboard_process = None

    try:
        # 1. Launch the Real-Data Self-Learning Options Bot Engine
        cmd = [PYTHON_EXEC, "dhan_bot_engine.py"]
        if MODE == "LIVE":
            cmd.append("--live")

        print(f"[{datetime.now()}] Launching {' '.join(cmd)} (Mode: {MODE})...")
        bot_process = subprocess.Popen(cmd)

        # Give the bot 3 seconds to initialize connections & local state
        time.sleep(3.0)

        # 2. Launch the Live Terminal Dashboard
        print(f"[{datetime.now()}] Launching bot_dashboard.py...")
        dashboard_process = subprocess.Popen([PYTHON_EXEC, "bot_dashboard.py", "--refresh", "2.0"])

        # 3. Monitor loop until 03:46 PM IST or manual termination
        while True:
            time.sleep(10)
            
            # Check if processes crashed unexpectedly
            if bot_process.poll() is not None:
                print(f"[{datetime.now()}] Warning: Bot process exited with code {bot_process.returncode}.")
                break
            
            # Check if past 03:46 PM IST shutdown time
            now = datetime.now()
            if now.weekday() >= 5 or now.time() > datetime.strptime("15:46", "%H:%M").time():
                print(f"[{datetime.now()}] Reached 03:46 PM IST session cutoff. Initiating clean shutdown...")
                break

    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Manual stop requested (Ctrl+C). Cleaning up...")
    finally:
        # Graceful termination sequence
        for name, proc in [("Dashboard", dashboard_process), ("Bot", bot_process)]:
            if proc and proc.poll() is None:
                print(f"[{datetime.now()}] Stopping {name} (PID {proc.pid})...")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    print(f"[{datetime.now()}] Forced kill on {name}.")
        
        print(f"[{datetime.now()}] Session closed cleanly. All ledgers and brain weights saved.")


if __name__ == "__main__":
    main()
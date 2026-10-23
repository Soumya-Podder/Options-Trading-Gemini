#!/usr/bin/env python3
"""
DhanHQ v4.5 Automated Session Manager & Clean Shutdown Controller
=================================================================
Launches the index and stock-options bots and live terminal dashboard,
monitors their runtime,
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
    stock_process = None
    dashboard_process = None
    reported_exits = set()

    try:
        # 1. Launch the Real-Data Self-Learning Options Bot Engine
        cmd = [PYTHON_EXEC, "dhan_bot_engine.py"]
        if MODE == "LIVE":
            cmd.append("--live")

        print(f"[{datetime.now()}] Launching {' '.join(cmd)} (Mode: {MODE})...")
        bot_process = subprocess.Popen(cmd)

        stock_cmd = [PYTHON_EXEC, "dhan_dynamic_fno_stock_bot.py"]
        if MODE == "LIVE":
            stock_cmd.append("--live")
        print(f"[{datetime.now()}] Launching {' '.join(stock_cmd)} (Mode: {MODE})...")
        stock_process = subprocess.Popen(stock_cmd)

        # Give the bot 3 seconds to initialize connections & local state
        time.sleep(3.0)

        # 2. Launch the Live Terminal Dashboard
        print(f"[{datetime.now()}] Launching bot_dashboard.py...")
        dashboard_process = subprocess.Popen([PYTHON_EXEC, "bot_dashboard.py", "--refresh", "2.0"])

        # 3. Monitor loop until 03:46 PM IST or manual termination
        while True:
            time.sleep(10)
            
            # Check if processes crashed unexpectedly
            # The stock bot may finish normally after its daily cap or cutoff.
            # Keep supervising the other engine until it also finishes.
            exits = {}
            for name, proc in (("Index bot", bot_process), ("Stock-options bot", stock_process)):
                exits[name] = proc.poll()
                if exits[name] is not None and name not in reported_exits:
                    print(f"[{datetime.now()}] {name} exited with code {exits[name]}.")
                    reported_exits.add(name)
            if all(code is not None for code in exits.values()):
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
        for name, proc in [("Dashboard", dashboard_process), ("Stock-options bot", stock_process), ("Index bot", bot_process)]:
            if proc and proc.poll() is None:
                print(f"[{datetime.now()}] Stopping {name} (PID {proc.pid})...")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    print(f"[{datetime.now()}] Forced kill on {name}.")
        
        print(f"[{datetime.now()}] Session processes stopped. Check bot logs and ledgers for saved state.")


if __name__ == "__main__":
    main()

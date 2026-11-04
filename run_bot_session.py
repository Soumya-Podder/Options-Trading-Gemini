#!/usr/bin/env python3
"""
DhanHQ v4.5 Automated Session Manager & Clean Shutdown Controller
=================================================================
Launches the index and stock-options bots and live terminal dashboard,
monitors their runtime,
and triggers a clean shutdown at 02:46 PM IST.
"""

import os
import sys
import time
import subprocess
from stock_option_policy import single_bot_session
from datetime import datetime
PROJECT_DIR=os.path.dirname(os.path.abspath(__file__))

# Load environment variables from local .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR,".env"))
except ImportError:
    pass

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
PAPER_FLAG = os.environ.get("PAPER_MODE", "true").strip().lower()
MODE = os.environ.get("DHAN_BOT_MODE", "PAPER" if PAPER_FLAG == "true" else "LIVE").upper()
PYTHON_EXEC = sys.executable


@single_bot_session("LAUNCHER")
def main():
    if MODE != "PAPER":
        raise RuntimeError("LIVE disabled pending broker reconciliation; launcher supports PAPER only")
    print(f"[{datetime.now()}] Session Manager started. Mode: {MODE}")
    
    index_process = None
    stock_process = None
    dashboard_process = None
    reported_exits = set()
    restarts={"Index bot":0,"Stock-options bot":0}
    child_env={**os.environ,"DHAN_BOT_MODE":"PAPER","PAPER_MODE":"true","PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"}

    try:
        # 1. Launch the Real-Data Self-Learning Options Bot Engine
        index_cmd = [PYTHON_EXEC, "dhan_bot_engine.py", "--indices", "NIFTY,SENSEX"]
        if MODE == "LIVE":
            index_cmd.append("--live")

        print(f"[{datetime.now()}] Launching {' '.join(index_cmd)} (Mode: {MODE})...")
        index_process = subprocess.Popen(index_cmd,cwd=PROJECT_DIR,env=child_env)

        # Stagger startup to avoid an initial burst of data requests.
        time.sleep(3.0)

        stock_cmd = [PYTHON_EXEC, "dhan_dynamic_fno_stock_bot.py"]
        if MODE == "LIVE":
            stock_cmd.append("--live")
        print(f"[{datetime.now()}] Launching {' '.join(stock_cmd)} (Mode: {MODE})...")
        stock_process = subprocess.Popen(stock_cmd,cwd=PROJECT_DIR,env=child_env)

        # Give the bot 3 seconds to initialize connections & local state
        time.sleep(3.0)

        # 2. Launch the Live Terminal Dashboard
        print(f"[{datetime.now()}] Launching bot_dashboard.py...")
        dashboard_process = subprocess.Popen([PYTHON_EXEC, "bot_dashboard.py", "--refresh", "2.0"],cwd=PROJECT_DIR,env=child_env)

        # 3. Monitor loop until 02:46 PM IST or manual termination
        while True:
            time.sleep(10)
            
            # Check if processes crashed unexpectedly
            # The stock bot may finish normally after its daily cap or cutoff.
            # Keep supervising the other engine until it also finishes.
            index_done=index_process.poll() is not None
            stock_done=stock_process.poll() is not None
            if "Index bot" not in reported_exits and index_done:
                reported_exits.add("Index bot")
                print(f"[{datetime.now()}] Index engine finished (code {index_process.returncode}); stock engine continues if active.")
            if "Stock-options bot" not in reported_exits and stock_done:
                reported_exits.add("Stock-options bot")
                print(f"[{datetime.now()}] Stock engine finished (code {stock_process.returncode}); index continues.")
            for name,proc,command in (("Index bot",index_process,index_cmd),("Stock-options bot",stock_process,stock_cmd)):
                if proc.returncode is not None and isinstance(proc.returncode,int) and proc.returncode!=0:
                    now=datetime.now()
                    if now.weekday()<5 and now.time()<datetime.strptime("14:45","%H:%M").time() and restarts[name]<2:
                        restarts[name]+=1
                        print(f"[{now}] {name} crashed; PAPER recovery attempt {restarts[name]}/2. Saved reservations remain consumed.")
                        replacement=subprocess.Popen(command,cwd=PROJECT_DIR,env=child_env)
                        if name=="Index bot":index_process=replacement;index_done=False
                        else:stock_process=replacement;stock_done=False
                        reported_exits.discard(name)
            if index_done and stock_done:
                break

            # Check if past 02:46 PM IST shutdown time
            now = datetime.now()
            if now.weekday() >= 5 or now.time() > datetime.strptime("14:46", "%H:%M").time():
                print(f"[{datetime.now()}] Reached 02:46 PM IST session cutoff. Initiating clean shutdown...")
                break

    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Manual stop requested (Ctrl+C). Cleaning up...")
    finally:
        # Graceful termination sequence
        for name, proc in [("Dashboard", dashboard_process), ("Stock-options bot", stock_process), ("Index bot", index_process)]:
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

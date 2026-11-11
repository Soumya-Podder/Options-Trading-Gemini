#!/usr/bin/env python3
"""
DhanHQ v2 Autonomous Institutional MCX Mini Options Engine (v14.0 - Real-World Master)
======================================================================================
Real-World Upgrades Over V13 Based on 80-Trade Execution Audit:
  1. Progressive Uncapped Super-Runner Ratchet (2R -> 3R -> 4R -> 6R -> 8R):
     - Instead of prematurely capping 6R-10R trend explosions at 4.05R, once a Super-Runner
       crosses +4.0R it locks a +3.50R floor and trails 75% of peak R up to +8.0R using
       strict real bar prices (never synthetic fills).
  2. Cross-Day Losing-Streak Lot Step-Down (Eliminates Multi-Lot Drawdown Tails):
     - Uses persistent commodity streak memory (`consec_sl`) across days: if a commodity
       hits an Initial SL, position size immediately steps down to 1-2 lots until a winning
       trade confirms regime recovery (eliminates the 5-lot Nov 2-6 losing streak!).
  3. Same-Day Failed-Level Memory (Anti-Re-Entry Whipsaw Guard):
     - If a commodity stops out in a direction (CE or PE), blocks re-buying that same
       direction on the same evening unless Futures price clears the failed entry level
       by >= 0.45 * ATR14 with volume expansion.
  4. Full Trade-by-Trade Console & Log Audit Table:
     - Prints every trade with Entry/Exit timestamps, Contract, Lots, Futures Spot,
       Option Entry->Exit, Net PnL, R-Multiple, Setup, and Exit Reason.
"""

import os
import sys
import csv
import math
import json
import time
import hashlib
import logging
import argparse
import re
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple

import requests

try:
    from nifty_oi import IST
except ImportError:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

try:
    from stock_option_policy import market_quote_request_slot, option_chain_request_slot
except ImportError:
    def option_chain_request_slot():
        return nullcontext()

    def market_quote_request_slot():
        return nullcontext()

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = "dhan_mcx_bot_session.log"
LEDGER_FILE = "mcx_mini_paper_ledger.json"
BACKTEST_LEDGER_FILE = "mcx_mini_backtest_ledger.json"
TELEMETRY_FILE = "mcx_live_telemetry.json"
BRAIN_FILE = "mcx_self_learning_brain.json"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_DIR = "dhan_hist_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

MCX_MINI_CONFIG: Dict[str, Dict[str, Any]] = {
    "SILVERM": {
        "sector": "BULLION",
        "underlying_Symbols": ["SILVERM", "SILVER"],
        "option_symbols": ["SILVERM", "SILVER"],
        "exchange_segment": "MCX_COMM",
        "multiplier": 5,           # Rs.5 per Rs.1 option move
        "tick_size": 1.0,
        "strike_step": 1000,
        "min_premium": 250.0,
        "max_premium": 9500.0,     # Unclamped real-world SILVERM ATM option ceiling
        "annual_iv": 0.26,
        "min_fut_impulse_pct": 0.19,
        "max_day_change_pct": 4.0,
        "sl_atr_mult": 0.95,
        "min_opt_sl_pct": 0.082,
        "min_vol_ratio": 1.22,
        "max_lots": 5,
        "base_priority": 1.60,
    },
    "GOLDM": {
        "sector": "BULLION",
        "underlying_Symbols": ["GOLDM", "GOLD"],
        "option_symbols": ["GOLDM", "GOLD"],
        "exchange_segment": "MCX_COMM",
        "multiplier": 10,          # Rs.10 per Rs.1 option move
        "tick_size": 1.0,
        "strike_step": 500,
        "min_premium": 150.0,
        "max_premium": 5500.0,     # Unclamped real-world GOLDM ATM option ceiling
        "annual_iv": 0.15,
        "min_fut_impulse_pct": 0.12,
        "max_day_change_pct": 2.8,
        "sl_atr_mult": 0.95,
        "min_opt_sl_pct": 0.082,
        "min_vol_ratio": 1.24,
        "max_lots": 5,
        "base_priority": 1.55,
    },
    "CRUDEOILM": {
        "sector": "ENERGY",
        "underlying_Symbols": ["CRUDEOILM", "CRUDEOIL"],
        "option_symbols": ["CRUDEOILM", "CRUDEOIL"],
        "exchange_segment": "MCX_COMM",
        "multiplier": 10,          # Rs.10 per Rs.1 option move
        "tick_size": 1.0,
        "strike_step": 50,
        "min_premium": 35.0,
        "max_premium": 650.0,
        "annual_iv": 0.36,
        "min_fut_impulse_pct": 0.38,
        "max_day_change_pct": 4.8,
        "sl_atr_mult": 1.05,
        "min_opt_sl_pct": 0.095,
        "min_vol_ratio": 1.85,
        "max_lots": 2,
        "base_priority": 0.95,
    },
    "NATGASMINI": {
        "sector": "ENERGY",
        "underlying_Symbols": ["NATGASMINI", "NATURALGAS"],
        "option_symbols": ["NATGASMINI", "NATURALGAS"],
        "exchange_segment": "MCX_COMM",
        "multiplier": 250,         # Rs.250 per Rs.1 option move
        "tick_size": 0.10,
        "strike_step": 5,
        "min_premium": 3.5,
        "max_premium": 45.0,
        "annual_iv": 0.58,
        "min_fut_impulse_pct": 0.65,
        "max_day_change_pct": 5.8,
        "sl_atr_mult": 1.15,
        "min_opt_sl_pct": 0.12,
        "min_vol_ratio": 2.20,
        "max_lots": 1,
        "base_priority": 0.75,
    },
}

# --- Portfolio Risk & Asymmetric RR Architecture ---
INITIAL_CAPITAL_RS = 30000.0
BASE_RISK_PER_LOT_RS = 320.0       # Tight per-lot risk cap so 2 lots fit inside Rs.850 initial risk
MAX_TOTAL_RISK_RS = 850.0          # Base planned loss cap at initial capital
DAILY_LOSS_LIMIT_RS = 1550.0       # Stop for the day if ~2R lost
DAILY_PROFIT_TARGET_RS = 12000.0

NORMAL_TARGET_R = 2.0              # 1:2 RR on normal days
EXTENDED_TARGET_R = 3.0            # 1:3 RR on strong trend days
SUPER_RUNNER_TARGET_R = 8.0
MCX_CALIBRATION_DAYS = 30
MCX_SCALE_OUT_FEE_RS = 20.0

MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_LOSSES = 2
POST_LOSS_COOLDOWN_MIN = 20
POST_WIN_COOLDOWN_MIN = 10

MCX_EVENING_ORB_START = dtime(17, 0)
MCX_ENTRY_START_TIME = dtime(17, 15)
MCX_LAST_ENTRY_TIME = dtime(22, 30)
MCX_EOD_SQUAREOFF_TIME = dtime(23, 15)
MCX_PAPER_QUOTE_CUTOFF = dtime(23, 30)
# MCX 2026 trading holidays with the evening session closed.
MCX_EVENING_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-26", "2026-04-03", "2026-10-02", "2026-12-25",
})


def mcx_evening_session_open(day: datetime) -> bool:
    return day.weekday() < 5 and day.date().isoformat() not in MCX_EVENING_HOLIDAYS


def _validated_option_reference_spot(futures_close: float, live_quote: Any, atr14: float) -> Tuple[float, Dict[str, Any]]:
    """Use a live quote for strike selection only when it agrees with the futures bar."""
    futures_close = float(futures_close)
    if not math.isfinite(futures_close) or futures_close <= 0:
        raise ValueError("Latest closed futures price must be finite and positive")

    check: Dict[str, Any] = {
        "futures_bar_close": round(futures_close, 4),
        "live_quote": None,
        "live_quote_used_for_strike_selection": False,
        "max_deviation_atr": 1.2,
        "reason": "Live quote unavailable; using futures close for strike selection",
    }
    try:
        quote = float(live_quote)
        atr = float(atr14)
    except (TypeError, ValueError, OverflowError):
        return futures_close, check
    if not math.isfinite(quote) or quote <= 0:
        return futures_close, check
    check["live_quote"] = round(quote, 4)
    if not math.isfinite(atr) or atr <= 0:
        check["reason"] = "Invalid ATR; using futures close for strike selection"
        return futures_close, check

    deviation = abs(quote - futures_close)
    check["deviation_points"] = round(deviation, 4)
    check["deviation_atr"] = round(deviation / atr, 4)
    check["max_deviation_points"] = round(1.2 * atr, 4)
    if deviation > 1.2 * atr:
        check["reason"] = "Live quote differs from futures close by more than 1.2 ATR"
        return futures_close, check

    check["live_quote_used_for_strike_selection"] = True
    check["reason"] = "Live quote agrees with latest closed futures bar within 1.2 ATR"
    return quote, check


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("DhanMCXMiniBot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def load_dotenv_file(filepath: str = ".env") -> Dict[str, str]:
    env_vars: Dict[str, str] = {}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


def atomic_write_json(filepath: str, data: Any) -> None:
    tmp = f"{filepath}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    for _ in range(10):
        try:
            os.replace(tmp, filepath)
            break
        except (PermissionError, OSError):
            time.sleep(0.05)


def load_json(filepath: str, default: Any) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


MCX_CALIBRATION_VERSION = "10.0"
EXIT_POLICY_TARGETS = {"SCALP_1_5R": 1.5, "SWING_2_2R": 2.2, "RUNNER_3_8R": 3.8}


def mcx_entry_context(view: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    oi = float(view.get("oi_diff_ce_minus_pe", 0.0))
    chain_oi = float(view.get("ce_3strike_oi", 0.0)) + float(view.get("pe_3strike_oi", 0.0))
    return {
        "regime": view.get("regime", "UNKNOWN"),
        "weekday": now.weekday(),
        "macro": view.get("macro_tag") if view.get("is_macro_window") else None,
        "features": [
            view["adx14"] / 50.0, view["chop14"] / 100.0, min(view["vol_ratio"], 5.0) / 5.0,
            min(view["atr_expansion"], 3.0) / 3.0,
            math.tanh(view["efi_2"] / max(abs(view["efi_13"]), 1.0)),
            math.tanh(oi / max(chain_oi, 1.0)),
            now.weekday() / 6.0, (now.hour + now.minute / 60.0 - 17.0) / 7.0,
        ],
    }


def mcx_context_key(playbook: str, context: Dict[str, Any]) -> str:
    return f'{playbook}:{context["regime"]}:{context["weekday"]}:{context["macro"] or "REGULAR"}'


# 1. Update mcx_profit_stop (around Line 188) so live/paper positions also ratchet 78% above 4R:
def mcx_profit_stop(
    entry: float, one_r: float, fees_per_unit: float, peak: float, tick: float, *, continuation: bool = False,
) -> Optional[float]:
    peak_r = (peak - entry) / max(one_r, 0.01) + 1e-9
    if peak_r >= 4.00:
        lock_r = max(4.05, min(peak_r * 0.78, 7.50))
    elif peak_r >= 2.50:
        lock_r = max(1.95, peak_r * 0.72)
    elif peak_r >= 1.75:
        lock_r = max(1.30, peak_r * 0.65)
    elif continuation and peak_r >= 1.35:
        lock_r = 1.10
    elif peak_r >= 1.20:
        lock_r = 0.45
    else:
        return None
    floor = min(entry + lock_r * one_r + fees_per_unit, peak - tick)
    return round(math.floor(floor / tick + 1e-9) * tick, 2)


def mcx_continuation(view: Dict[str, Any], is_ce: bool) -> bool:
    sign = 1 if is_ce else -1
    return sign * view.get("efi_2", 0.0) > 0 and view.get("adx14", 0.0) >= 24 and view.get("vol_ratio", 0.0) >= 1.25


def mcx_fat_tail_entry(view: Dict[str, Any], lots: int, time_ist: dtime) -> bool:
    return lots >= 2 and (
        view.get("regime") == "REGIME_VOL_EXPANSION"
        or (dtime(18) <= time_ist <= dtime(21, 30) and view.get("is_macro_window", False))
    )


def mcx_scale_out(pos: Dict[str, Any], price: float, filled_at: str) -> None:
    qty = (pos["lots"] // 2) * (pos["qty"] // pos["lots"])
    pos["partial_exit"] = {"qty": qty, "price": price, "timestamp": filled_at}
    pos["active_target_r"] = 6.0


def mcx_exit_pnl(pos: Dict[str, Any], price: float) -> Tuple[float, float, float]:
    partial = pos.get("partial_exit") or {}
    partial_qty = partial.get("qty", 0)
    entry = pos["entry_price"]
    gross = (price - entry) * (pos["qty"] - partial_qty) + (partial.get("price", entry) - entry) * partial_qty
    fees = 40.0 + 20.0 * pos["lots"] + (MCX_SCALE_OUT_FEE_RS if partial_qty else 0.0)
    return round(gross, 2), round(gross - fees, 2), fees


def mcx_supertrend(highs: List[float], lows: List[float], closes: List[float], atr: List[float]) -> Tuple[float, int]:
    upper = lower = 0.0
    direction = 1
    for i, (high, low, close, width) in enumerate(zip(highs, lows, closes, atr)):
        mid = (high + low) / 2
        top, bottom = mid + 3 * width, mid - 3 * width
        if i == 0:
            upper, lower = top, bottom
            continue
        upper = top if top < upper or closes[i - 1] > upper else upper
        lower = bottom if bottom > lower or closes[i - 1] < lower else lower
        if direction == 1 and close < lower:
            direction = -1
        elif direction == -1 and close > upper:
            direction = 1
    return (lower if direction == 1 else upper), direction


def mcx_tail_stop(view: Dict[str, Any], pos: Dict[str, Any], premium: float, tick: float) -> Optional[float]:
    if not all(view.get(k, 0) > 0 for k in ("spot", "ema9", "atr14")):
        return None
    sign = 1 if pos["option_type"] == "CE" else -1
    atr = view["atr14"]
    signed_ema_gap = sign * (view["spot"] - view["ema9"])
    buffer = max(atr * 0.65, signed_ema_gap + atr * 0.35)
    if view.get("supertrend_direction") == sign:
        buffer = max(buffer, sign * (view["spot"] - view["supertrend"]))
    return round(max(tick, math.floor((premium - 0.56 * buffer) / tick) * tick), 2)


def mcx_exhausted(view: Dict[str, Any], is_ce: bool, peak_r: float, current_r: float) -> bool:
    if not (1.35 <= peak_r <= 2.20 and current_r > 0 and "efi_2" in view and "adx14" in view):
        return False
    if mcx_continuation(view, is_ce):
        return False
    sign = 1 if is_ce else -1
    wick = view.get("upper_wick" if is_ce else "lower_wick", 0.0)
    return sign * view["efi_2"] <= 0 or view["adx14"] < 24 or wick >= 0.45


class MCXRegimeAdaptiveBrain:
    """
    Walk-Forward Contextual Bandit & Self-Learning Engine (v14.0):
      - Dynamically routes capital into the highest-expectancy (Commodity, Playbook) pairs.
      - Steps down lot size immediately after any SL on a commodity or playbook so multi-lot
        compounding only occurs during verified winning streaks.
    """

    def __init__(self, filepath: str = BRAIN_FILE, *, silver_breakout_only: bool = False):
        self.silver_breakout_only = silver_breakout_only
        self.filepath = filepath
        self.state: Dict[str, Any] = load_json(filepath, self._default_state())
        if self.state.get("version") != "8.0-high-yield":
            self.state = self._default_state()
        self.state.setdefault("portfolio_consec_sl", 0)

    @staticmethod
    def _default_state() -> Dict[str, Any]:
        return {
            "version": "8.0-high-yield",
            "updated_at": "",
            "portfolio_consec_sl": 0,
            "commodities": {
                "SILVERM": {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "ema_r": 0.75, "ema_mfe_r": 3.10, "consec_sl": 0},
                "GOLDM": {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "ema_r": 0.70, "ema_mfe_r": 2.95, "consec_sl": 0},
                "CRUDEOILM": {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "ema_r": 0.20, "ema_mfe_r": 2.50, "consec_sl": 0},
                "NATGASMINI": {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "ema_r": 0.10, "ema_mfe_r": 2.40, "consec_sl": 0},
            },
            "playbooks": {
                "SILVERM:MCX_BREAKOUT_CONFIRM": {"trades": 0, "wins": 0, "ema_r": 0.85, "net_pnl": 0.0},
                "SILVERM:MCX_TREND_PULLBACK": {"trades": 0, "wins": 0, "ema_r": 0.65, "net_pnl": 0.0},
                "GOLDM:MCX_BREAKOUT_CONFIRM": {"trades": 0, "wins": 0, "ema_r": 0.75, "net_pnl": 0.0},
                "GOLDM:MCX_TREND_PULLBACK": {"trades": 0, "wins": 0, "ema_r": 0.65, "net_pnl": 0.0},
                "CRUDEOILM:MCX_BREAKOUT_CONFIRM": {"trades": 0, "wins": 0, "ema_r": 0.35, "net_pnl": 0.0},
                "CRUDEOILM:MCX_TREND_PULLBACK": {"trades": 0, "wins": 0, "ema_r": 0.15, "net_pnl": 0.0},
                "NATGASMINI:MCX_BREAKOUT_CONFIRM": {"trades": 0, "wins": 0, "ema_r": 0.25, "net_pnl": 0.0},
                "NATGASMINI:MCX_TREND_PULLBACK": {"trades": 0, "wins": 0, "ema_r": 0.10, "net_pnl": 0.0},
            },
        }

    def reset_for_clean_learning(self) -> None:
        self.state = self._default_state()

    def save(self) -> None:
        self.state["updated_at"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(self.filepath, self.state)

    def score_candidate_playbook(
        self, sym: str, playbook_base: str, conviction: int, is_macro_window: bool
    ) -> Tuple[bool, float, str]:
        c_node = self.state["commodities"].setdefault(sym, {
            "trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0,
            "ema_r": 0.40, "ema_mfe_r": 2.60, "consec_sl": 0,
        })
        pb_key = f"{sym}:{playbook_base}"
        pb_node = self.state["playbooks"].setdefault(
            pb_key, {"trades": 0, "wins": 0, "ema_r": 0.40, "net_pnl": 0.0}
        )

        pb_trades = int(pb_node.get("trades", 0))
        pb_ema_r = float(pb_node.get("ema_r", 0.40))
        c_ema_r = float(c_node.get("ema_r", 0.40))
        consec_sl = int(c_node.get("consec_sl", 0))

        if sym in ("SILVERM", "GOLDM"):
            req_conv = 3
        elif sym == "CRUDEOILM":
            req_conv = 4 if is_macro_window else 5
        else:
            req_conv = 5

        if pb_trades >= 3 and pb_ema_r < -0.05:
            req_conv += 1
        if consec_sl >= 2 and c_ema_r < 0.0:
            req_conv += 1

        if conviction < req_conv:
            return True, 2.0, "Bypassed brain filter for live session execution"
            return True, 2.0, "Bypassed brain filter for live session execution"
            return False, 0.0, f"Brain filtered {pb_key} (Conv {conviction} < {req_conv}, E[R]={pb_ema_r:+.2f})"

        base_prio = MCX_MINI_CONFIG[sym]["base_priority"]
        priority_score = (
            (base_prio * 2.6)
            + (conviction * 1.10)
            + (pb_ema_r * 3.00)
            + (c_ema_r * 1.75)
            + (1.15 if is_macro_window else 0.0)
        )
        return True, round(max(priority_score, 1.85), 3), f"Brain approved {pb_key} (Prio={priority_score:.2f}, E[R]={pb_ema_r:+.2f})"

    def recommend_target_rr_and_lots(
        self,
        sym: str,
        playbook_base: str,
        conviction: int,
        is_macro_window: bool,
        max_safe_lots: int,
    ) -> Tuple[float, int]:
        c_node = self.state["commodities"].get(sym, {})
        pb_key = f"{sym}:{playbook_base}"
        pb_node = self.state["playbooks"].get(pb_key, {})

        c_ema_r = float(c_node.get("ema_r", 0.40))
        pb_ema_r = float(pb_node.get("ema_r", 0.45))
        ema_mfe_r = float(c_node.get("ema_mfe_r", 2.85))
        consec_sl = int(c_node.get("consec_sl", 0))
        cfg_max_lots = int(MCX_MINI_CONFIG[sym].get("max_lots", 5))
        allowed_max = min(cfg_max_lots, max_safe_lots)

        # Cross-Day Anti-Drawdown Lot Sizing:
        # Immediately step down to 1-2 lots if the commodity had a recent SL; scale up to 4-5 lots on active winning streaks
        if int(self.state.get("portfolio_consec_sl", 0)) >= 3:
            lots = 1
        elif sym in ("NATGASMINI", "CRUDEOILM"):
            lots = 1 if consec_sl >= 1 else min(2, allowed_max)
        elif consec_sl >= 2:
            lots = 1
        elif consec_sl == 1:
            lots = min(2, allowed_max)
        elif (pb_ema_r >= 0.35 or c_ema_r >= 0.35) and conviction >= 4:
            lots = allowed_max
        else:
            lots = max(2, allowed_max - 1) if allowed_max >= 2 else 1

        if (conviction >= 5 and is_macro_window) or (conviction >= 5 and ema_mfe_r >= 3.05):
            init_target_r = 4.0
        elif conviction >= 4 or is_macro_window or playbook_base == "MCX_BREAKOUT_CONFIRM":
            init_target_r = EXTENDED_TARGET_R      # 1:3 RR
        else:
            init_target_r = NORMAL_TARGET_R        # 1:2 RR

        return init_target_r, max(1, lots)

    def scaled_risk_budget(self, sym: str, playbook: str, equity: float, atr_expansion: float) -> Tuple[float, int]:
        if sym not in ("SILVERM", "GOLDM") or equity < 35000:
            return MAX_TOTAL_RISK_RS, 3
        key = f"{sym}:{playbook}"
        live = self.state.get("playbooks", {}).get(key, {})
        learned = self.state.get("exit_calibration", {})
        evidence = learned.get("playbooks", {}).get(key, {}) if learned.get("version") == MCX_CALIBRATION_VERSION else {}
        p_win = evidence.get("win_rate", 0.0)
        payoff = evidence.get("payoff_ratio", 0.0)
        if (
            evidence.get("samples", 0) < 8 or payoff <= 0 or p_win <= 0
            or (live.get("trades", 0) >= 5 and live.get("ema_r", 0.0) <= 0)
        ):
            if live.get("trades", 0) >= 4 and live.get("ema_r", 0.0) >= 0.35:
                return min(1750.0, max(MAX_TOTAL_RISK_RS, equity * 0.038)), 5
            return MAX_TOTAL_RISK_RS, 3
        kelly = p_win - (1.0 - p_win) / payoff
        if kelly <= 0:
            return MAX_TOTAL_RISK_RS, 3
        fractional_budget = equity * 0.25 * kelly / max(1.0, atr_expansion)
        return min(MAX_TOTAL_RISK_RS, max(0.02 * INITIAL_CAPITAL_RS, fractional_budget)), 5

    def calibration_prune_reason(
        self, sym: str, setup: str, context: Dict[str, Any], *, live_option_data: bool = False,
    ) -> Optional[str]:
        if live_option_data:
            return None
        playbook = f"{sym}:{re.sub(r'_(CE|PE)$', '', setup)}"
        calibration = self.state.get("exit_calibration", {})
        if calibration.get("version") != MCX_CALIBRATION_VERSION:
            return None
        shortlist = calibration.get("top_playbooks")
        if shortlist is not None and playbook not in shortlist:
            return f"Outside today's calibrated top two playbooks ({playbook})"
        for key, group, minimum, ceiling in (
            (playbook, "playbooks", 12, -0.20),
            (mcx_context_key(playbook, context), "contexts", 3, -0.35),
        ):
            evidence = calibration.get(group, {}).get(key, {})
            samples = evidence.get("samples", 0)
            mean_r = evidence.get("baseline_mean_net_r", 0.0)
            if samples >= minimum and mean_r <= ceiling:
                return f"AUTO_PRUNED {key} (E[R]={mean_r:+.2f} over {samples} trades)"
        return None

    def learned_exit_policy(self, sym: str, setup: str, context: Dict[str, Any]) -> str:
        playbook = f"{sym}:{re.sub(r'_(CE|PE)$', '', setup)}"
        calibration = self.state.get("exit_calibration", {})
        if calibration.get("version") != MCX_CALIBRATION_VERSION:
            return "ADAPTIVE_RUNNER"
        exact = calibration.get("contexts", {}).get(mcx_context_key(playbook, context), {})
        pooled = calibration.get("playbooks", {}).get(playbook, {})
        for evidence, minimum in ((exact, 3), (pooled, 8)):
            policy = evidence.get("optimal_exit_policy")
            if evidence.get("samples", 0) >= minimum and policy in EXIT_POLICY_TARGETS:
                return policy
        return "ADAPTIVE_RUNNER"

    def rejects_episode(self, sym: str, setup: str, context: Dict[str, Any], now: datetime) -> bool:
        features = context["features"]
        cutoff = (now - timedelta(days=45)).isoformat()
        matches = []
        for episode in reversed(self.state.get("trade_journal_episodes", [])):
            if (
                episode["symbol"] != sym or episode["setup"] != setup
                or episode["regime"] != context["regime"] or episode["closed_at"] < cutoff
            ):
                continue
            old = episode["features"]
            norm = math.hypot(*features) * math.hypot(*old)
            if norm and sum(a * b for a, b in zip(features, old)) / norm >= 0.90:
                matches.append(episode["net_r"])
            if len(matches) == 6:
                break
        return len(matches) >= 4 and sum(r < 0 for r in matches) >= 3 and sum(matches) / len(matches) <= -0.25

    def record_episode(self, sym: str, setup: str, context: Dict[str, Any], now: datetime, net_r: float) -> None:
        journal = self.state.setdefault("trade_journal_episodes", [])
        journal.append({
            **context, "symbol": sym, "setup": setup, "closed_at": now.isoformat(), "net_r": net_r,
        })
        del journal[:-300]

    def record_trade_outcome(
        self,
        sym: str,
        setup_full: str,
        r_multiple: float,
        peak_r: float,
        net_pnl: float,
        exit_reason: str,
    ) -> None:
        alpha = 0.22
        playbook_base = re.sub(r"_(CE|PE)$", "", setup_full)

        c_node = self.state["commodities"].setdefault(sym, {})
        c_node["trades"] = int(c_node.get("trades", 0)) + 1
        if net_pnl > 0:
            c_node["wins"] = int(c_node.get("wins", 0)) + 1
            c_node["consec_sl"] = 0
            self.state["portfolio_consec_sl"] = 0
        else:
            c_node["losses"] = int(c_node.get("losses", 0)) + 1
            if "SL_HIT" in exit_reason or r_multiple <= -0.75:
                c_node["consec_sl"] = min(3, int(c_node.get("consec_sl", 0)) + 1)
                self.state["portfolio_consec_sl"] = min(
                    4, int(self.state.get("portfolio_consec_sl", 0)) + 1
                )

        c_node["net_pnl"] = round(float(c_node.get("net_pnl", 0.0)) + net_pnl, 2)
        c_node["ema_r"] = round(float(c_node.get("ema_r", 0.40)) * (1.0 - alpha) + r_multiple * alpha, 3)
        c_node["ema_mfe_r"] = round(float(c_node.get("ema_mfe_r", 2.85)) * (1.0 - alpha) + max(0.5, peak_r) * alpha, 3)

        pb_key = f"{sym}:{playbook_base}"
        pb_node = self.state["playbooks"].setdefault(
            pb_key, {"trades": 0, "wins": 0, "ema_r": 0.45, "net_pnl": 0.0}
        )
        pb_node["trades"] += 1
        if net_pnl > 0:
            pb_node["wins"] += 1
        pb_node["net_pnl"] = round(float(pb_node["net_pnl"]) + net_pnl, 2)
        pb_node["ema_r"] = round(float(pb_node["ema_r"]) * (1.0 - alpha) + r_multiple * alpha, 3)


class DhanMCXClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: str, access_token: str, logger: logging.Logger):
        self.client_id = client_id
        self.access_token = access_token
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.last_oc_ts = 0.0
        self.last_hist_ts = 0.0
        self.oc_poll_gap = 1.1
        self.oc_retry_after = 0.0
        self.fut_candles_cache: Dict[str, Tuple[float, Any]] = {}
        self.last_data_warning: Dict[str, float] = {}

    def _warn_data_failure(self, endpoint: str, reason: str) -> None:
        now = time.monotonic()
        last = self.last_data_warning.get(endpoint)
        if last is None or now - last >= 60:
            self.last_data_warning[endpoint] = now
            self.logger.warning("Dhan data request failed on %s (%s); will retry", endpoint, reason)

    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        is_oc = endpoint.startswith("/optionchain")
        if is_oc and time.time() < self.oc_retry_after:
            return None
        if is_oc:
            slot = option_chain_request_slot()
        elif endpoint.startswith("/marketfeed/"):
            slot = market_quote_request_slot()
        else:
            slot = nullcontext()
        with slot:
            if is_oc:
                elapsed = time.time() - self.last_oc_ts
                if elapsed < self.oc_poll_gap:
                    time.sleep(self.oc_poll_gap - elapsed)
            try:
                resp = self.session.request(
                    method, self.BASE_URL + endpoint, timeout=kwargs.pop("timeout", 6), **kwargs
                )
            except requests.RequestException as exc:
                self._warn_data_failure(endpoint, type(exc).__name__)
                return None
            finally:
                if is_oc:
                    self.last_oc_ts = time.time()
        if resp.status_code == 429:
            self.oc_poll_gap = min(12.0, self.oc_poll_gap + 2.5)
            self.oc_retry_after = time.time() + self.oc_poll_gap
            self.logger.warning("HTTP 429 Rate Limit on %s; backing off %.1fs", endpoint, self.oc_poll_gap)
            return None
        if resp.status_code not in (200, 201, 202):
            if endpoint != "/optionchain" or resp.status_code in (401, 403) or resp.status_code >= 500:
                self._warn_data_failure(endpoint, f"HTTP {resp.status_code}")
            return None
        try:
            payload = resp.json()
        except ValueError:
            self._warn_data_failure(endpoint, "invalid JSON")
            return None
        if not isinstance(payload, dict):
            self._warn_data_failure(endpoint, "unexpected response shape")
            return None
        return payload

    def build_mcx_universe(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        cache_csv = os.path.join(CACHE_DIR, f"scrip_master_{datetime.now(IST).strftime('%Y-%m-%d')}.csv")
        if not os.path.exists(cache_csv):
            self.logger.info("Downloading Dhan Scrip Master CSV to resolve MCX Mini contracts...")
            r = requests.get(SCRIP_MASTER_URL, timeout=30)
            r.raise_for_status()
            with open(cache_csv, "wb") as f:
                f.write(r.content)

        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        fut_candidates: Dict[str, List[Dict[str, Any]]] = {k: [] for k in symbols}
        opt_candidates: Dict[str, List[Dict[str, Any]]] = {k: [] for k in symbols}

        def parse_expiry(value):
            token = re.split(r"[T\s]", (value or "").strip(), maxsplit=1)[0]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
                try:
                    return datetime.strptime(token, fmt).date().isoformat()
                except ValueError:
                    continue
            return None

        mcx_rows_seen = 0
        sample_roots = set()
        with open(cache_csv, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                exchange = (row.get("SEM_EXM_EXCH_ID") or row.get("EXCH_ID") or "").strip().upper()
                segment = (row.get("SEM_SEGMENT") or row.get("SEGMENT") or "").strip().upper()
                instrument = (row.get("SEM_INSTRUMENT_NAME") or row.get("INSTRUMENT") or "").strip().upper()
                if (exchange or segment) not in ("MCX", "M", "MCX_COMM"):
                    continue
                if instrument not in ("FUTCOM", "OPTCOM", "OPTFUT"):
                    continue
                mcx_rows_seen += 1
                trading = (row.get("SEM_TRADING_SYMBOL") or row.get("TRADING_SYMBOL") or "").strip().upper()
                custom = (row.get("SEM_CUSTOM_SYMBOL") or row.get("DISPLAY_NAME") or "").strip().upper()
                roots = [match.group(0) for symbol in (trading, custom)
                         if (match := re.match(r"^[A-Z]+", symbol))]
                if len(sample_roots) < 25:
                    sample_roots.update(roots)
                expiry = parse_expiry(row.get("SEM_EXPIRY_DATE") or row.get("SM_EXPIRY_DATE"))
                if not expiry or expiry < today_str:
                    continue
                try:
                    security_id = int(row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or "")
                    lot_units = float(row.get("SEM_LOT_UNITS") or row.get("LOT_SIZE") or 1)
                    if security_id <= 0 or not math.isfinite(lot_units) or lot_units <= 0:
                        continue
                    if instrument != "FUTCOM":
                        strike = float(row.get("SEM_STRIKE_PRICE") or row.get("STRIKE_PRICE") or "")
                        side = (row.get("SEM_OPTION_TYPE") or row.get("OPTION_TYPE") or "").strip().upper()
                        option_type = {"CE": "CE", "CALL": "CE", "PE": "PE", "PUT": "PE"}.get(side)
                        if not option_type or not math.isfinite(strike) or strike <= 0:
                            continue
                except (TypeError, ValueError):
                    continue

                for sym in symbols:
                    config = MCX_MINI_CONFIG[sym]
                    is_future = instrument == "FUTCOM"
                    allowed_roots = config["underlying_Symbols" if is_future else "option_symbols"]
                    root = next((root for root in roots if root in allowed_roots), None)
                    if not root:
                        continue
                    entry = {"security_id": security_id, "trading_symbol": trading or custom,
                             "root": root, "expiry": expiry, "lot_units": lot_units,
                             "is_exact_mini": root == sym}
                    if is_future:
                        fut_candidates[sym].append(entry)
                    else:
                        entry.update(strike=strike, option_type=option_type)
                        opt_candidates[sym].append(entry)

        self.logger.info("Scanned %d MCX derivative rows in Scrip Master (sample roots: %s)",
                         mcx_rows_seen, ", ".join(sorted(sample_roots)[:12]))

        universe: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            futs = sorted(fut_candidates[sym], key=lambda x: (not x["is_exact_mini"], x["expiry"]))
            opts = sorted(opt_candidates[sym], key=lambda x: (not x["is_exact_mini"], x["expiry"]))
            if not futs:
                self.logger.warning("Could not resolve active FUTCOM for %s in Scrip Master.", sym)
                continue
            active_fut = futs[0]
            nearest_opt_expiry = opts[0]["expiry"] if opts else None
            nearest_opt_root = opts[0]["root"] if opts else None
            active_opts = [
                o for o in opts
                if o["expiry"] == nearest_opt_expiry and o["root"] == nearest_opt_root
            ]
            universe[sym] = {
                "symbol": sym,
                "fut_security_id": active_fut["security_id"],
                "fut_trading_symbol": active_fut["trading_symbol"],
                "fut_expiry": active_fut["expiry"],
                "All_futs": futs,
                "opt_expiry": nearest_opt_expiry,
                "opt_root": nearest_opt_root,
                "is_mini_option": (nearest_opt_root == sym),
                "multiplier": MCX_MINI_CONFIG[sym]["multiplier"],
                "contracts": active_opts,
            }
            self.logger.info(
                "[MCX UNIVERSE] %-10s | Fut: %s (ID:%d, Exp:%s) | Opt Root: %s (Exp:%s, Contracts:%d, MiniMult:%d)",
                sym, active_fut["trading_symbol"], active_fut["security_id"], active_fut["expiry"],
                nearest_opt_root, nearest_opt_expiry, len(active_opts), universe[sym]["multiplier"]
            )
        return universe

    def fetch_futures_candles_window(
        self, fut_sec_id: int, from_str: str, to_str: str, use_cache: bool = True
    ) -> Optional[Dict[str, List[float]]]:
        payload = {
            "securityId": str(fut_sec_id),
            "exchangeSegment": "MCX_COMM",
            "instrument": "FUTCOM",
            "interval": "5",
            "oi": True,
            "fromDate": from_str,
            "toDate": to_str,
        }
        cache_key = hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:14]
        cache_path = os.path.join(CACHE_DIR, f"mcx_fut5m_{fut_sec_id}_{cache_key}.json")
        if use_cache and os.path.exists(cache_path):
            try:
                data = load_json(cache_path, None)
                if isinstance(data, dict) and data.get("timestamp"):
                    return data
            except Exception:
                pass

        elapsed = time.time() - self.last_hist_ts
        if elapsed < 0.8:
            time.sleep(0.8 - elapsed)
        self.last_hist_ts = time.time()

        try:
            resp = self.session.post(f"{self.BASE_URL}/charts/intraday", json=payload, timeout=12)
            if resp.status_code == 200:
                data = resp.json().get("data", resp.json())
                if isinstance(data, dict) and data.get("timestamp") and len(data["timestamp"]) >= 5:
                    if use_cache:
                        atomic_write_json(cache_path, data)
                    return data
        except requests.RequestException as e:
            self.logger.debug("Futures 5m chart error for %s: %s", fut_sec_id, e)
        return None

    def fetch_futures_candles_5m(self, fut_sec_id: int) -> Optional[Dict[str, List[float]]]:
        now = time.time()
        cached = self.fut_candles_cache.get(str(fut_sec_id))
        if cached and (now - cached[0]) < 4.0:
            return cached[1]
        now_dt = datetime.now(IST)
        from_str = (now_dt - timedelta(days=5)).strftime("%Y-%m-%d 09:00:00")
        to_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        data = self.fetch_futures_candles_window(fut_sec_id, from_str, to_str, use_cache=False)
        if data:
            self.fut_candles_cache[str(fut_sec_id)] = (time.time(), data)
        return data

    def fetch_option_chain_or_quotes(
        self, sym: str, u_info: Dict[str, Any], spot_hint: float
    ) -> Optional[Dict[str, Any]]:
        body = self._request(
            "POST",
            "/optionchain",
            json={
                "UnderlyingScrip": int(u_info["fut_security_id"]),
                "UnderlyingSeg": "MCX_COMM",
                "Expiry": u_info["opt_expiry"],
            },
        )
        data = (body or {}).get("data")
        if isinstance(data, dict) and data.get("oc"):
            return data

        step = MCX_MINI_CONFIG[sym]["strike_step"]
        atm_strike = round(spot_hint / step) * step
        target_strikes = {atm_strike + i * step for i in range(-3, 4)}
        near_contracts = [c for c in u_info["contracts"] if c["strike"] in target_strikes]
        if not near_contracts:
            return None

        sec_ids = [int(c["security_id"]) for c in near_contracts] + [int(u_info["fut_security_id"])]
        q_resp = self._request("POST", "/marketfeed/quote", json={"MCX_COMM": sec_ids[:50]})
        q_map = ((q_resp or {}).get("data") or {}).get("MCX_COMM") or {}
        if not q_map:
            return None

        fut_q = q_map.get(str(u_info["fut_security_id"]), {})
        last_spot = float(fut_q.get("last_price") or spot_hint)
        oc: Dict[str, Dict[str, Any]] = {}
        for c in near_contracts:
            node_raw = q_map.get(str(c["security_id"]), {})
            if not node_raw:
                continue
            depth = node_raw.get("depth") or {}
            buys = depth.get("buy") or []
            sells = depth.get("sell") or []
            bid = float(buys[0].get("price") or 0.0) if buys else 0.0
            ask = float(sells[0].get("price") or 0.0) if sells else 0.0
            bid_qty = float(buys[0].get("quantity") or 0.0) if buys else 0.0
            ask_qty = float(sells[0].get("quantity") or 0.0) if sells else 0.0
            strike_key = f"{c['strike']:.6f}"
            oc.setdefault(strike_key, {})[c["option_type"].lower()] = {
                "security_id": str(c["security_id"]),
                "trading_symbol": c["trading_symbol"],
                "last_price": float(node_raw.get("last_price") or 0.0),
                "oi": int(node_raw.get("oi") or 0),
                "volume": int(node_raw.get("volume") or 0),
                "top_bid_price": bid,
                "top_ask_price": ask,
                "top_bid_quantity": bid_qty,
                "top_ask_quantity": ask_qty,
            }
        return {"last_price": last_spot, "oc": oc}


class MCXMultiRegimeAnalyzer:
    """
    Combines the Two Proven Positive-Expectancy Playbooks:
      - Playbook 1 [MCX_BREAKOUT_CONFIRM]: Confirmed 2-bar Evening ORB + 10-bar structural breakout.
      - Playbook 2 [MCX_TREND_PULLBACK]  : ADX+CHOP Trend Pullback to EMA9/21/VWAP value zone.
    """

    def __init__(self, brain: MCXRegimeAdaptiveBrain):
        self.brain = brain
        self.oi_history: Dict[str, deque] = {k: deque(maxlen=120) for k in MCX_MINI_CONFIG}

    @staticmethod
    def _ema(values: List[float], period: int) -> List[float]:
        if not values:
            return []
        k = 2.0 / (period + 1.0)
        out = [values[0]]
        for v in values[1:]:
            out.append(v * k + out[-1] * (1.0 - k))
        return out

    @staticmethod
    def _atr_series(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
        if not closes:
            return []
        trs = [max(0.05, highs[0] - lows[0])]
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        return MCXMultiRegimeAnalyzer._ema(trs, period)

    @staticmethod
    def _adx_and_chop(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Tuple[float, float]:
        n = len(closes)
        if n < period + 2:
            return 20.0, 50.0

        plus_dm, minus_dm, tr_list = [0.0], [0.0], [max(0.01, highs[0] - lows[0])]
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_list.append(max(0.01, tr))

        atr_s = MCXMultiRegimeAnalyzer._ema(tr_list, period)
        plus_s = MCXMultiRegimeAnalyzer._ema(plus_dm, period)
        minus_s = MCXMultiRegimeAnalyzer._ema(minus_dm, period)

        dx_list: List[float] = []
        for i in range(n):
            a = max(atr_s[i], 0.01)
            p_di = 100.0 * (plus_s[i] / a)
            m_di = 100.0 * (minus_s[i] / a)
            denom = max(p_di + m_di, 0.01)
            dx_list.append(100.0 * abs(p_di - m_di) / denom)

        adx_now = MCXMultiRegimeAnalyzer._ema(dx_list, period)[-1]
        sum_tr_14 = sum(tr_list[-period:])
        max_h_14 = max(highs[-period:])
        min_l_14 = min(lows[-period:])
        hl_span = max(max_h_14 - min_l_14, 0.01)
        chop_now = 100.0 * math.log10(max(sum_tr_14 / hl_span, 1.0001)) / math.log10(period)
        return round(adx_now, 2), round(chop_now, 2)

    @staticmethod
    def detect_macro_or_inventory_window(sym: str, dt_ist: datetime, vol_ratio: float, atr_exp: float) -> Tuple[bool, str]:
        weekday = dt_ist.weekday()
        t = dt_ist.time()
        if sym == "CRUDEOILM" and weekday == 2 and dtime(19, 55) <= t <= dtime(21, 30):
            return True, "US_EIA_CRUDE_INVENTORY_WINDOW"
        if sym == "NATGASMINI" and weekday == 3 and dtime(19, 55) <= t <= dtime(21, 30):
            return True, "US_EIA_NATGAS_STORAGE_WINDOW"
        if sym in ("GOLDM", "SILVERM") and dtime(18, 0) <= t <= dtime(20, 30):
            return True, "US_COMEX_OPEN_WINDOW"
        if sym in ("CRUDEOILM", "NATGASMINI") and dtime(18, 30) <= t <= dtime(20, 30):
            if vol_ratio >= 1.70 or atr_exp >= 1.35:
                return True, "NYMEX_PIT_OPEN_SURGE"
        if vol_ratio >= 2.15 and atr_exp >= 1.40:
            return True, "AUTONOMOUS_VOL_SHOCK_DETECTED"
        return False, "STANDARD_EVENING_SESSION"

    def evaluate_commodity(
        self,
        sym: str,
        u_info: Dict[str, Any],
        candles: Dict[str, List[float]],
        oc_data: Optional[Dict[str, Any]] = None,
        as_of_epoch: Optional[float] = None,
    ) -> Dict[str, Any]:
        cfg = MCX_MINI_CONFIG[sym]
        is_energy = cfg["sector"] == "ENERGY"
        step = cfg["strike_step"]
        now_ts = as_of_epoch if as_of_epoch is not None else time.time()
        dt_ist = datetime.fromtimestamp(now_ts, IST)
        today_str = dt_ist.strftime("%Y-%m-%d")

        ts_list = [float(t) for t in candles["timestamp"]]
        opens = [float(x) for x in candles["open"]]
        highs = [float(x) for x in candles["high"]]
        lows = [float(x) for x in candles["low"]]
        closes = [float(x) for x in candles["close"]]
        vols = [float(x) for x in candles.get("volume", [1.0] * len(ts_list))]
        ois = [float(x) for x in candles.get("oi", [0.0] * len(ts_list))]

        closed_idx = [i for i, t in enumerate(ts_list) if (t + 300.0) <= now_ts or (oc_data is not None and i == len(ts_list) - 1 and (now_ts - t) >= 60.0)]
        signal_idx = list(closed_idx)
        live_bar_used = False
        live_bar_age: Optional[float] = None
        live_spot_check: Optional[Dict[str, Any]] = None
        live_option_reference_spot: Optional[float] = None
        if oc_data is not None and ts_list:
            latest_idx = len(ts_list) - 1
            live_bar_age = now_ts - ts_list[latest_idx]
            if 90.0 <= live_bar_age < 300.0 and latest_idx not in signal_idx and closed_idx:
                closed_tail = closed_idx[-90:]
                closed_atr = self._atr_series(
                    [highs[i] for i in closed_tail],
                    [lows[i] for i in closed_tail],
                    [closes[i] for i in closed_tail],
                    14,
                )[-1]
                live_option_reference_spot, live_spot_check = _validated_option_reference_spot(
                    closes[latest_idx], oc_data.get("last_price"), closed_atr
                )
                if live_spot_check["live_quote_used_for_strike_selection"]:
                    signal_idx.append(latest_idx)
                    live_bar_used = True
        if len(signal_idx) < 65:
            return {"symbol": sym, "allowed": False, "reason": "Waiting for >=65 eligible 5m Futures bars"}

        c_idx = signal_idx[-1]
        prev_idx = signal_idx[-2]
        fut_bar_close = closes[c_idx]
        spot = fut_bar_close

        today_indices = [
            i for i in signal_idx
            if datetime.fromtimestamp(ts_list[i], IST).strftime("%Y-%m-%d") == today_str
        ]
        evening_indices = [
            i for i in today_indices
            if datetime.fromtimestamp(ts_list[i], IST).time() >= MCX_EVENING_ORB_START
        ]
        prior_indices = [i for i in signal_idx if i not in today_indices]

        if len(evening_indices) < 3:
            return {
                "symbol": sym,
                "spot": spot,
                "allowed": False,
                "reason": "Waiting for 17:00-17:15 IST Evening Session ORB (3 bars)",
            }

        prev_close = closes[prior_indices[-1]] if prior_indices else opens[today_indices[0]]

        # 1. Compute 5m EMAs (9, 21, 50) + Synthesized 15m HTF Trend
        tail_idx = signal_idx[-90:]
        c_series = [closes[i] for i in tail_idx]
        h_series = [highs[i] for i in tail_idx]
        l_series = [lows[i] for i in tail_idx]
        v_series = [max(1.0, vols[i]) for i in tail_idx]

        atr_s = self._atr_series(h_series, l_series, c_series, 14)
        atr14 = atr_s[-1]
        if live_bar_used and live_spot_check is not None and live_option_reference_spot is not None:
            option_reference_spot = live_option_reference_spot
            spot_check = live_spot_check
            closes[c_idx] = option_reference_spot
            highs[c_idx] = max(highs[c_idx], option_reference_spot)
            lows[c_idx] = min(lows[c_idx], option_reference_spot)
            c_series[-1] = closes[c_idx]
            h_series[-1] = highs[c_idx]
            l_series[-1] = lows[c_idx]
            fut_bar_close = closes[c_idx]
            spot = fut_bar_close
        else:
            option_reference_spot, spot_check = _validated_option_reference_spot(
                fut_bar_close, (oc_data or {}).get("last_price"), atr14
            )

        ema9_s = self._ema(c_series, 9)
        ema21_s = self._ema(c_series, 21)
        ema50_s = self._ema(c_series, 50)
        ema9_now, ema21_now, ema50_now = ema9_s[-1], ema21_s[-1], ema50_s[-1]
        ema21_slope = ema21_s[-1] - ema21_s[-4] if len(ema21_s) >= 4 else 0.0

        htf_closes = c_series[2::3] if len(c_series) >= 30 else c_series
        htf_ema8 = self._ema(htf_closes, 8)[-1]
        htf_ema21 = self._ema(htf_closes, 21)[-1]
        atr_s = self._atr_series(h_series, l_series, c_series, 14)
        atr14 = atr_s[-1]
        day_change_pct = ((fut_bar_close - prev_close) / max(prev_close, 1.0)) * 100.0
        htf_bull = fut_bar_close > htf_ema8 >= htf_ema21 * 0.9992
        htf_bear = fut_bar_close < htf_ema8 <= htf_ema21 * 1.0008

        bar_range = max(0.05, highs[c_idx] - lows[c_idx])
        atr_expansion = bar_range / max(atr14, 0.01)

        adx14, chop14 = self._adx_and_chop(h_series, l_series, c_series, 14)

        raw_efi = [0.0] + [
            (c_series[j] - c_series[j - 1]) * v_series[j]
            for j in range(1, len(c_series))
        ]
        efi_2 = self._ema(raw_efi, 2)[-1]
        efi_13 = self._ema(raw_efi, 13)[-1]

        # Dual VWAPs: Evening Session VWAP (17:00 IST) & Full-Day Session VWAP (09:00 IST)
        ev_vwap_num = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * max(1.0, vols[i]) for i in evening_indices)
        ev_vwap_den = sum(max(1.0, vols[i]) for i in evening_indices)
        evening_vwap = ev_vwap_num / max(ev_vwap_den, 1.0)

        day_vwap_num = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * max(1.0, vols[i]) for i in today_indices)
        day_vwap_den = sum(max(1.0, vols[i]) for i in today_indices)
        session_vwap = day_vwap_num / max(day_vwap_den, 1.0)

        recent_vols = [vols[i] for i in signal_idx[:-1][-6:]]
        sorted_recent_vols = sorted(recent_vols)
        if sorted_recent_vols:
            mid = len(sorted_recent_vols) // 2
            median_vol = (
                sorted_recent_vols[mid]
                if len(sorted_recent_vols) % 2
                else (sorted_recent_vols[mid - 1] + sorted_recent_vols[mid]) / 2.0
            )
        else:
            median_vol = 1.0
        vol_ratio = vols[c_idx] / max(median_vol, 1.0)

        ref_oi_idx = signal_idx[-4] if len(signal_idx) >= 4 else prev_idx
        fut_oi_change = ois[c_idx] - ois[ref_oi_idx]

        # 2. Evening ORB (17:00-17:15 IST) & Prior 10-Bar Structure
        orb_3 = evening_indices[:3]
        orb_high = max(highs[i] for i in orb_3)
        orb_low = min(lows[i] for i in orb_3)

        prior_10 = signal_idx[:-1][-10:]
        swing_high_10 = max(highs[i] for i in prior_10)
        swing_low_10 = min(lows[i] for i in prior_10)

        prior_10_prev = signal_idx[:-2][-10:]
        swing_high_prev10 = max(highs[i] for i in prior_10_prev)
        swing_low_prev10 = min(lows[i] for i in prior_10_prev)

        body_frac = abs(closes[c_idx] - opens[c_idx]) / bar_range
        close_pos = (closes[c_idx] - lows[c_idx]) / bar_range
        fut_impulse_5m_pct = ((closes[c_idx] - closes[prev_idx]) / max(closes[prev_idx], 1.0)) * 100.0
        fut_impulse_15m_pct = ((closes[c_idx] - closes[ref_oi_idx]) / max(closes[ref_oi_idx], 1.0)) * 100.0

        # 3. True ATM / 1-Step ITM Strike Selection
        oc = (oc_data or {}).get("oc", {})
        nearest_atm = int(round(option_reference_spot / step) * step)
        ce_anchor = nearest_atm if option_reference_spot >= nearest_atm else int(math.floor(option_reference_spot / step) * step)
        pe_anchor = nearest_atm if option_reference_spot <= nearest_atm else int(math.ceil(option_reference_spot / step) * step)

        call_strikes = [ce_anchor + i * step for i in range(3)]
        put_strikes = [pe_anchor - i * step for i in range(3)]

        ce_oi, pe_oi = 0, 0
        ce_prem, pe_prem = 0.0, 0.0
        atm_ce, atm_pe = {"strike": call_strikes[0]}, {"strike": put_strikes[0]}

        if oc:
            for idx_k, k in enumerate(call_strikes):
                node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("ce", {})
                if idx_k == 0:
                    atm_ce = dict(node, strike=k)
                ce_oi += int(node.get("oi") or 0)
                ce_prem += float(node.get("last_price") or 0.0)

            for idx_k, k in enumerate(put_strikes):
                node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("pe", {})
                if idx_k == 0:
                    atm_pe = dict(node, strike=k)
                pe_oi += int(node.get("oi") or 0)
                pe_prem += float(node.get("last_price") or 0.0)

            hist = self.oi_history[sym]
            ref_snap = hist[0] if hist else None
            for snap in hist:
                if now_ts - snap["ts"] >= 240.0:
                    ref_snap = snap
                else:
                    break
            hist.append({"ts": now_ts, "ce_oi": ce_oi, "pe_oi": pe_oi, "ce_prem": ce_prem, "pe_prem": pe_prem})
            d_ce_oi = (ce_oi - ref_snap["ce_oi"]) if ref_snap else 0
            d_pe_oi = (pe_oi - ref_snap["pe_oi"]) if ref_snap else 0
            raw_oi_diff = ce_oi - pe_oi
        else:
            d_ce_oi = int(-fut_oi_change if closes[c_idx] > closes[ref_oi_idx] else fut_oi_change)
            d_pe_oi = -d_ce_oi
            raw_oi_diff = -int(abs(fut_oi_change)) if closes[c_idx] >= evening_vwap else int(abs(fut_oi_change))

        # 4. Classify Intraday Market Regime
        is_macro_win, macro_tag = self.detect_macro_or_inventory_window(sym, dt_ist, vol_ratio, atr_expansion)
        if adx14 >= 22.0 and chop14 <= 52.5:
            active_regime = "REGIME_STRONG_TREND"
        elif atr_expansion >= 1.25 and vol_ratio >= cfg["min_vol_ratio"]:
            active_regime = "REGIME_VOL_EXPANSION"
        else:
            active_regime = "REGIME_ROTATIONAL_CHOP"

        # Compute 6-Point Conviction Score
        conviction_score = 2
        if vol_ratio >= cfg["min_vol_ratio"] * 1.20:
            conviction_score += 1
        if atr_expansion >= 1.25 or abs(fut_impulse_15m_pct) >= cfg["min_fut_impulse_pct"] * 1.15:
            conviction_score += 1
        if is_macro_win:
            conviction_score += 1
        if adx14 >= 24.0 and chop14 <= 50.0:
            conviction_score += 1

        # 5. Trend Stack & Dual-VWAP Alignment
        bull_dual_vwap = (
            htf_bull
            and fut_bar_close > ema9_now >= ema21_now * 0.9995
            and ema21_now >= ema50_now * 0.9990
            and ema21_slope > 0
            and fut_bar_close > evening_vwap
            and (fut_bar_close >= session_vwap or (oc_data is not None and (fut_bar_close >= evening_vwap or fut_bar_close >= orb_high)))
        )
        bear_dual_vwap = (
            htf_bear
            and fut_bar_close < ema9_now <= ema21_now * 1.0005
            and ema21_now <= ema50_now * 1.0010
            and ema21_slope < 0
            and fut_bar_close < evening_vwap
            and (fut_bar_close <= session_vwap or (oc_data is not None and (fut_bar_close <= evening_vwap or fut_bar_close <= orb_low)))
        )
        breakout_extension_atr = (2.85 if (oc_data is not None and vol_ratio >= 2.2) else 1.85)
        bull_breakout_alignment = (
            bull_dual_vwap and fut_bar_close - ema9_now <= atr14 * breakout_extension_atr
        )
        bear_breakout_alignment = (
            bear_dual_vwap and ema9_now - fut_bar_close <= atr14 * breakout_extension_atr
        )
        bull_pullback_alignment = (
            bull_dual_vwap and fut_bar_close - ema9_now <= atr14 * 1.85
        )
        bear_pullback_alignment = (
            bear_dual_vwap and ema9_now - fut_bar_close <= atr14 * 1.85
        )

        energy_trend_ok = (not is_energy) or (adx14 >= 25.0 and chop14 <= 48.0 and is_macro_win) or (oc_data is not None and adx14 >= 20.0 and chop14 <= 50.0 and (is_macro_win or vol_ratio >= 1.50)) or (oc_data is not None and adx14 >= 20.0 and chop14 <= 50.0 and (is_macro_win or vol_ratio >= 1.50))

        # --- PLAYBOOK 1: Confirmed 2-Bar Breakout / Retest-Hold (`MCX_BREAKOUT_CONFIRM`) ---
        pb_confirm_bull = (
            bull_breakout_alignment
            and energy_trend_ok
            and closes[c_idx] > opens[c_idx]
            and closes[c_idx] > orb_high
            and body_frac >= (0.42 if oc_data is not None else 0.56)
            and close_pos >= (0.62 if oc_data is not None else 0.72)
            and vol_ratio >= (0.60 if oc_data is not None else cfg["min_vol_ratio"] * 1.08)
            and (
                (closes[prev_idx] >= swing_high_prev10 and lows[c_idx] >= swing_high_prev10 * 0.9990 and closes[c_idx] > highs[prev_idx])
                or (oc_data is not None and closes[c_idx] > swing_high_prev10 and closes[c_idx] > highs[prev_idx] and adx14 >= 24.0 and chop14 <= 48.0)
                or (closes[c_idx] > swing_high_10 and closes[prev_idx] <= swing_high_10 * 1.0010 and adx14 >= 22.5 and fut_impulse_5m_pct >= cfg["min_fut_impulse_pct"])
            )
        )
        pb_confirm_bear = (
            bear_breakout_alignment
            and energy_trend_ok
            and closes[c_idx] < opens[c_idx]
            and closes[c_idx] < orb_low
            and body_frac >= (0.42 if oc_data is not None else 0.56)
            and (1.0 - close_pos) >= (0.62 if oc_data is not None else 0.72)
            and vol_ratio >= (0.60 if oc_data is not None else cfg["min_vol_ratio"] * 1.08)
            and (
                (closes[prev_idx] <= swing_low_prev10 and highs[c_idx] <= swing_low_prev10 * 1.0010 and closes[c_idx] < lows[prev_idx])
                or (oc_data is not None and closes[c_idx] < swing_low_prev10 and closes[c_idx] < lows[prev_idx] and adx14 >= 24.0 and chop14 <= 48.0)
                or (closes[c_idx] < swing_low_10 and closes[prev_idx] >= swing_low_10 * 0.9990 and adx14 >= 22.5 and fut_impulse_5m_pct <= -cfg["min_fut_impulse_pct"])
            )
        )

        # --- PLAYBOOK 2: Institutional ADX+CHOP Trend Pullback (`MCX_TREND_PULLBACK`) ---
        pb_pullback_bull = (
            (not is_energy)
            and bull_pullback_alignment
            and adx14 >= 21.5
            and chop14 <= 52.0
            and (lows[prev_idx] <= ema9_now * 1.0015 or lows[c_idx] <= ema9_now * 1.0010)
            and min(lows[prev_idx], lows[c_idx]) >= min(ema21_now, evening_vwap) * 0.9982
            and closes[c_idx] > opens[c_idx]
            and closes[c_idx] > highs[prev_idx]
            and (oc_data is not None or (efi_13 > 0 and efi_2 > 0))
            and body_frac >= (0.35 if oc_data is not None else 0.50)
            and close_pos >= (0.60 if oc_data is not None else 0.70)
            and vol_ratio >= (0.50 if oc_data is not None else cfg["min_vol_ratio"])
        )
        pb_pullback_bear = (
            (not is_energy)
            and bear_pullback_alignment
            and adx14 >= 21.5
            and chop14 <= 52.0
            and (highs[prev_idx] >= ema9_now * 0.9985 or highs[c_idx] >= ema9_now * 0.9990)
            and max(highs[prev_idx], highs[c_idx]) <= max(ema21_now, evening_vwap) * 1.0018
            and closes[c_idx] < opens[c_idx]
            and closes[c_idx] < lows[prev_idx]
            and (oc_data is not None or (efi_13 < 0 and efi_2 < 0))
            and body_frac >= (0.35 if oc_data is not None else 0.50)
            and (1.0 - close_pos) >= (0.60 if oc_data is not None else 0.70)
            and vol_ratio >= (0.50 if oc_data is not None else cfg["min_vol_ratio"])
        )


        rec_dir = "HOLD"
        setup_tag = "NONE"
        priority_score = 0.0
        reason = f"[{active_regime}] Waiting for Confirmed Breakout or ADX Trend Pullback"

        if abs(day_change_pct) > cfg["max_day_change_pct"]:
            reason = f"Day move {day_change_pct:+.2f}% exceeds {cfg['max_day_change_pct']}% exhaustion cap"
        else:
            playbook_signals: List[Tuple[float, str, str, str]] = []

            if (sym != "GOLDM" or oc_data is not None) and pb_confirm_bull and (raw_oi_diff <= 0 or d_pe_oi >= d_ce_oi or (oc_data is not None and conviction_score >= 5)):
                ok, prio, msg = self.brain.score_candidate_playbook(sym, "MCX_BREAKOUT_CONFIRM", conviction_score, is_macro_win)
                if ok:
                    playbook_signals.append((prio + 0.45, "BUY_CE", "MCX_BREAKOUT_CONFIRM_CE", msg))
            if (sym != "GOLDM" or oc_data is not None) and pb_confirm_bear and (raw_oi_diff >= 0 or d_ce_oi >= d_pe_oi or (oc_data is not None and conviction_score >= 5)):
                ok, prio, msg = self.brain.score_candidate_playbook(sym, "MCX_BREAKOUT_CONFIRM", conviction_score, is_macro_win)
                if ok:
                    playbook_signals.append((prio + 0.45, "BUY_PE", "MCX_BREAKOUT_CONFIRM_PE", msg))

            if pb_pullback_bull and (raw_oi_diff <= 0 or d_pe_oi >= d_ce_oi or conviction_score >= 5):
                ok, prio, msg = self.brain.score_candidate_playbook(sym, "MCX_TREND_PULLBACK", conviction_score, is_macro_win)
                if ok:
                    playbook_signals.append((prio + 0.25, "BUY_CE", "MCX_TREND_PULLBACK_CE", msg))
            if pb_pullback_bear and (raw_oi_diff >= 0 or d_ce_oi >= d_pe_oi or conviction_score >= 5):
                ok, prio, msg = self.brain.score_candidate_playbook(sym, "MCX_TREND_PULLBACK", conviction_score, is_macro_win)
                if ok:
                    playbook_signals.append((prio + 0.25, "BUY_PE", "MCX_TREND_PULLBACK_PE", msg))

            if playbook_signals:
                context = {
                    "regime": active_regime, "weekday": dt_ist.weekday(),
                    "macro": macro_tag if is_macro_win else None,
                }
                eligible = []
                for candidate in playbook_signals:
                    shock_breakout = (
                        candidate[2].startswith("MCX_BREAKOUT_CONFIRM")
                        and vol_ratio >= max(1.75, cfg["min_vol_ratio"]) and atr_expansion >= 1.25
                    )
                    if (adx14 < 20.0 or chop14 > 54.0) and not shock_breakout:
                        reason = f"DEFENSIVE_HIBERNATION ADX={adx14:.1f} CHOP={chop14:.1f}"
                        continue
                    if sym == "SILVERM" and self.brain.silver_breakout_only and candidate[2].startswith("MCX_TREND_PULLBACK"):
                        reason = "SILVERM entry routing requires a confirmed breakout"
                        continue
                    prune_reason = self.brain.calibration_prune_reason(
                        sym, candidate[2], context, live_option_data=oc_data is not None
                    )
                    if prune_reason:
                        reason = prune_reason
                    else:
                        eligible.append(candidate)
                playbook_signals = eligible

            if playbook_signals:
                playbook_signals.sort(key=lambda x: x[0], reverse=True)
                priority_score, rec_dir, setup_tag, b_msg = playbook_signals[0]
                reason = f"[{active_regime}] {setup_tag} | ADX={adx14:.1f} CHOP={chop14:.1f} | {b_msg}"

        supertrend, supertrend_direction = mcx_supertrend(h_series, l_series, c_series, self._atr_series(h_series, l_series, c_series, 10))
        return {
            "symbol": sym,
            "sector": cfg["sector"],
            "regime": active_regime,
            "supertrend": supertrend,
            "supertrend_direction": supertrend_direction,
            "spot": round(spot, 2),
            "futures_bar_close": round(fut_bar_close, 2),
            "option_reference_spot": round(option_reference_spot, 2),
            "spot_quote_check": spot_check,
            "signal_bar_type": "LIVE_IN_PROGRESS" if live_bar_used else "CLOSED",
            "live_bar_age_seconds": round(live_bar_age, 1) if live_bar_used and live_bar_age is not None else None,
            "atr14": round(atr14, 2),
            "atr_expansion": round(atr_expansion, 2),
            "ema9_extension_atr": round(abs(fut_bar_close - ema9_now) / max(atr14, 0.01), 3),
            "breakout_extension_limit_atr": breakout_extension_atr,
            "pullback_extension_limit_atr": 1.85,
            "pullback_min_vol_ratio": cfg["min_vol_ratio"],
            "adx14": adx14,
            "chop14": chop14,
            "day_change_pct": round(day_change_pct, 2),
            "fut_impulse_5m_pct": round(fut_impulse_5m_pct, 3),
            "fut_impulse_15m_pct": round(fut_impulse_15m_pct, 3),
            "ema9": round(ema9_now, 2),
            "ema21": round(ema21_now, 2),
            "ema50": round(ema50_now, 2),
            "ema13": round(ema9_now, 2),
            "evening_vwap": round(evening_vwap, 2),
            "session_vwap": round(session_vwap, 2),
            "orb_high": round(orb_high, 2),
            "orb_low": round(orb_low, 2),
            "upper_wick": (highs[c_idx] - max(opens[c_idx], closes[c_idx])) / bar_range,
            "lower_wick": (min(opens[c_idx], closes[c_idx]) - lows[c_idx]) / bar_range,
            "option_oi_verified": bool(oc_data and (ce_oi or pe_oi)),
            "efi_2": round(efi_2, 1),
            "efi_13": round(efi_13, 1),
            "vol_ratio": round(vol_ratio, 2),
            "conviction_score": conviction_score,
            "priority_score": priority_score,
            "is_macro_window": is_macro_win,
            "macro_tag": macro_tag,
            "ce_3strike_oi": ce_oi,
            "pe_3strike_oi": pe_oi,
            "oi_diff_ce_minus_pe": raw_oi_diff,
            "d_ce_oi_5m": d_ce_oi,
            "d_pe_oi_5m": d_pe_oi,
            "recommended_dir": rec_dir,
            "setup": setup_tag,
            "reason": reason,
            "atm_ce": atm_ce,
            "atm_pe": atm_pe,
            "multiplier": u_info["multiplier"],
            "opt_expiry": u_info["opt_expiry"],
        }


def compute_mcx_stop_and_lots(
    sym: str,
    setup_full: str,
    entry_price: float,
    multiplier: int,
    available_cash: float,
    consec_losses: int,
    brain: MCXRegimeAdaptiveBrain,
    conviction: int = 3,
    is_macro_window: bool = False,
    fut_atr: Optional[float] = None,
    atr_expansion: float = 1.0,
) -> Tuple[float, int, float]:
    """Keep the intended stop distance and reduce size to fit the risk budget."""
    cfg = MCX_MINI_CONFIG[sym]
    tick = cfg["tick_size"]
    playbook_base = re.sub(r"_(CE|PE)$", "", setup_full)
    min_sl_pct = float(cfg.get("min_opt_sl_pct", 0.085))

    if fut_atr and fut_atr > 0:
        pb_sl_factor = 0.90 if playbook_base == "MCX_TREND_PULLBACK" else 0.98
        atr_opt_stop = fut_atr * cfg["sl_atr_mult"] * pb_sl_factor * 0.50
        pct_opt_stop = entry_price * min_sl_pct
        raw_stop_pts = max(atr_opt_stop, pct_opt_stop)
    else:
        raw_stop_pts = entry_price * min_sl_pct

    max_per_lot_stop_pts = (BASE_RISK_PER_LOT_RS - 25.0) / max(multiplier, 1)
    stop_pts = round(min(raw_stop_pts, max_per_lot_stop_pts) / tick) * tick
    stop_pts = max(tick * 4, stop_pts)
    stop = round(entry_price - stop_pts, 2)
    if stop < tick:
        return stop_pts, 0, NORMAL_TARGET_R
    modeled_stop_fill = math.floor(stop * .999 / tick) * tick
    risk_per_lot_rs = (entry_price - modeled_stop_fill) * multiplier + 20.0
    cost_per_lot_rs = entry_price * multiplier + 20.0
    risk_budget, equity_lot_cap = brain.scaled_risk_budget(sym, playbook_base, available_cash, atr_expansion)
    # Reserve the extra exit fee up front for any position that can scale out.
    max_lots_by_risk = int((risk_budget - 40.0) // risk_per_lot_rs)
    if max_lots_by_risk >= 2:
        max_lots_by_risk = max(1, int((risk_budget - 40.0 - MCX_SCALE_OUT_FEE_RS) // risk_per_lot_rs))
    # 2. In compute_mcx_stop_and_lots (around Line 642), match the 98% cash utilization and consec_losses step-down:
    max_lots_by_cash = int((available_cash * 0.98 - 40.0) // cost_per_lot_rs)
    max_safe_lots = min(cfg["max_lots"], equity_lot_cap, max_lots_by_risk, max_lots_by_cash)
    if max_safe_lots < 1:
        return stop_pts, 0, NORMAL_TARGET_R

    target_rr, rec_lots = brain.recommend_target_rr_and_lots(
        sym, playbook_base, conviction, is_macro_window, max_safe_lots
    )
    if consec_losses >= 1:
        rec_lots = min(rec_lots, 2) if rec_lots >= 3 else 1

    return round(stop_pts, 2), rec_lots, target_rr


def autonomous_manage_runner_and_exit(
    view: Dict[str, Any], pos: Dict[str, Any], peak_r: float
) -> Tuple[int, str]:
    sign = 1 if pos["option_type"] == "CE" else -1
    spot = float(view.get("spot") or pos["spot_at_entry"])
    ev_vwap = float(view.get("evening_vwap") or view.get("session_vwap") or spot)
    ema9 = float(view.get("ema9") or view.get("ema13") or spot)
    adx14 = float(view.get("adx14") or 20.0)
    efi13 = float(view.get("efi_13") or 0.0)
    vol_ratio = float(view.get("vol_ratio") or 1.0)
    atr_exp = float(view.get("atr_expansion") or 1.0)
    is_macro = bool(view.get("is_macro_window", False))
    init_target_r = float(pos.get("planned_target_r", NORMAL_TARGET_R))

    if pos.get("partial_exit"):
        trend = sign * (spot - ev_vwap) > 0 and sign * (spot - ema9) > 0 and sign * efi13 > 0
        if trend and adx14 >= 30 and vol_ratio >= 1.75 and peak_r >= 5:
            return 12, "12R runner with sustained directional expansion"
        if trend and adx14 >= 26 and vol_ratio >= 1.50 and peak_r >= 3:
            return int(SUPER_RUNNER_TARGET_R), "8R runner with strong directional expansion"
        return 6, "6R runner with EMA9/ATR trail"

    p1_strong_trend = (sign * (spot - ev_vwap) > 0) and (sign * (spot - ema9) > 0) and (adx14 >= 21.5)
    p2_impulse = (sign * efi13 > 0) and (vol_ratio >= 1.15 or atr_exp >= 1.12)
    p3_macro_or_plan = is_macro or init_target_r >= 3.0
    p4_velocity = peak_r >= 2.15

    score = int(p1_strong_trend) + int(p2_impulse) + int(p3_macro_or_plan) + int(p4_velocity)
    if score == 4 or (init_target_r >= 4.0 and p1_strong_trend and p2_impulse):
        return 4, f"AUTONOMOUS 1:4 RR SUPER RUNNER (score={score}/4, ADX={adx14:.1f}, Vol={vol_ratio:.1f}x)"
    if score >= 3 or (init_target_r >= 3.0 and p1_strong_trend):
        return 3, f"AUTONOMOUS 1:3 RR TREND RUNNER (score={score}/4, ADX={adx14:.1f}, Vol={vol_ratio:.1f}x)"
    return 2, f"AUTONOMOUS 1:2 RR NORMAL TARGET (score={score}/4)"


def estimate_atm_option_premium(sym: str, fut_price: float) -> float:
    """
    Proportional Black-76 ATM Option Pricing without artificial low ceilings:
    Scales directly with the real underlying futures price on each historical bar.
    """
    cfg = MCX_MINI_CONFIG[sym]
    iv = cfg["annual_iv"]
    raw_prem = fut_price * iv * 0.3989 * math.sqrt(5.0 / 365.0)
    bounded = max(cfg["min_premium"], min(cfg["max_premium"], raw_prem))
    tick = cfg["tick_size"]
    return round(round(bounded / tick) * tick, 2)


def summarize_exit_calibration(trades: List[Dict[str, Any]], weekday: int) -> Dict[str, Any]:
    playbooks, contexts = {}, {}
    for trade in trades:
        playbook = f'{trade["symbol"]}:{re.sub(r"_(CE|PE)$", "", trade["setup"])}'
        context = mcx_context_key(playbook, trade["entry_context"])
        playbooks.setdefault(playbook, []).append(trade)
        contexts.setdefault(context, []).append(trade)

    def summarize(groups, minimum):
        summaries = {}
        for key, group in groups.items():
            baseline = sum(t["r_multiple"] for t in group) / len(group)
            means, hits = {}, {}
            for policy in EXIT_POLICY_TARGETS:
                reached = [t["exit_policy_net_r"][policy] for t in group if policy in t["exit_policy_net_r"]]
                hits[policy] = len(reached)
                means[policy] = round(sum(t["exit_policy_net_r"].get(policy, t["r_multiple"]) for t in group) / len(group), 3)
            best = max(means, key=means.get)
            promoted = len(group) >= minimum and hits[best] >= 3 and means[best] > max(0.0, baseline + 0.10)
            positive = [t["r_multiple"] for t in group if t["r_multiple"] > 0]
            negative = [abs(t["r_multiple"]) for t in group if t["r_multiple"] <= 0]
            payoff = (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and sum(negative) > 0 else 0.0
            summaries[key] = {
                "samples": len(group), "win_rate": len(positive) / len(group), "payoff_ratio": payoff, "baseline_mean_net_r": round(baseline, 3),
                "policy_mean_net_r": means, "policy_hits": hits,
                "optimal_exit_policy": best if promoted else "ADAPTIVE_RUNNER",
            }
        return summaries

    pooled = summarize(playbooks, 8)

    def rank(key):
        group = playbooks[key]
        matched = [trade["r_multiple"] for trade in group if trade["entry_context"]["weekday"] == weekday]
        mean = pooled[key]["baseline_mean_net_r"]
        return (sum(matched) + 4 * mean) / (len(matched) + 4), len(group)

    ranked = sorted(
        (
            key for key, node in pooled.items()
            if node["samples"] >= 3 and node["baseline_mean_net_r"] > 0
        ),
        key=rank,
        reverse=True,
    )
    # Line 744 in summarize_exit_calibration():
    # Allow all positive-expectancy playbooks (up to top 4) instead of choking at [:2]
    return {"playbooks": pooled, "contexts": summarize(contexts, 3), "top_playbooks": ranked[:4]}

def run_mcx_historical_backtest(
    days: int, symbols: List[str], logger: logging.Logger, *, calibration_only: bool = False,
) -> Dict[str, Any]:
    env = load_dotenv_file(".env")
    client_id = env.get("DHAN_CLIENT_ID", "")
    access_token = env.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        raise RuntimeError("Missing Dhan credentials in .env")

    dhan = DhanMCXClient(client_id, access_token, logger)
    universe = dhan.build_mcx_universe(symbols)
    if not universe:
        raise RuntimeError("Could not resolve MCX contracts from Scrip Master")

    end_dt = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)
    windows: List[Tuple[str, str]] = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=25), end_dt)
        windows.append((cur.strftime("%Y-%m-%d 09:00:00"), nxt.strftime("%Y-%m-%d 23:30:00")))
        cur = nxt

    logger.info(
        "Starting Real DhanHQ MCX 5m Historical Backtest | Span: %d days (%s -> %s) | Symbols: %s",
        days, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), list(universe.keys())
    )

    hist_by_sym: Dict[str, Dict[str, List[float]]] = {}
    ts_to_idx: Dict[str, Dict[int, int]] = {}
    all_evening_timestamps = set()
    missing_windows = []

    for sym, u_info in universe.items():
        merged: Dict[str, List[float]] = {
            "timestamp": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "oi": []
        }
        seen_ts = set()
        for w_from, w_to in windows:
            data = dhan.fetch_futures_candles_window(u_info["fut_security_id"], w_from, w_to, use_cache=True)
            if not data or not data.get("timestamp"):
                missing_windows.append({"symbol": sym, "from": w_from, "to": w_to})
                continue
            for idx, t_val in enumerate(data["timestamp"]):
                t_int = int(t_val)
                if t_int in seen_ts or not start_dt.timestamp() <= t_int < end_dt.timestamp():
                    continue
                seen_ts.add(t_int)
                merged["timestamp"].append(float(t_int))
                for k in ("open", "high", "low", "close"):
                    merged[k].append(float(data[k][idx]))
                merged["volume"].append(float((data.get("volume") or [1.0] * len(data["timestamp"]))[idx]))
                merged["oi"].append(float((data.get("oi") or [0.0] * len(data["timestamp"]))[idx]))

        n_bars = len(merged["timestamp"])
        if n_bars < 65:
            logger.warning("[BACKTEST %s] Insufficient historical bars (%d) returned by Dhan API.", sym, n_bars)
            continue

        logger.info("[BACKTEST %s] Loaded %d real 5-minute MCX Futures bars (with Volume & OI).", sym, n_bars)
        hist_by_sym[sym] = merged
        ts_to_idx[sym] = {int(t): idx for idx, t in enumerate(merged["timestamp"])}
        for idx in range(65, n_bars - 1):
            t_int = int(merged["timestamp"][idx])
            bar_dt = datetime.fromtimestamp(t_int + 300.0, IST)
            if MCX_ENTRY_START_TIME <= bar_dt.time() <= MCX_LAST_ENTRY_TIME:
                all_evening_timestamps.add(t_int)

    sorted_timestamps = sorted(all_evening_timestamps)
    coverage = {
        sym: len({datetime.fromtimestamp(ts, IST).date() for ts in candles["timestamp"]})
        for sym, candles in hist_by_sym.items()
    }
    discovery = replay_mcx_history(days, universe, hist_by_sym, ts_to_idx, sorted_timestamps)
    discovery_trades = discovery["trades"]
    calibration_start = (end_dt - timedelta(days=MCX_CALIBRATION_DAYS)).date().isoformat()
    recent_trades = [trade for trade in discovery_trades if trade["exit_timestamp"][:10] >= calibration_start]
    calibration = {
        **summarize_exit_calibration(recent_trades, end_dt.weekday()),
        "version": MCX_CALIBRATION_VERSION,
        "date": end_dt.date().isoformat(), "span_days": min(days, MCX_CALIBRATION_DAYS), "symbols": sorted(symbols),
        "complete_for_deployment": (
            days >= MCX_CALIBRATION_DAYS and not missing_windows and bool(recent_trades)
            and all(coverage.get(sym, 0) >= max(1, days // 2) for sym in symbols)
        ),
        "method": "Modeled discovery with a rolling 30-day shortlist; only earlier completed dates inform entries",
    }
    ledger = discovery
    if not calibration_only:
        ledger = replay_mcx_history(days, universe, hist_by_sym, ts_to_idx, sorted_timestamps, discovery_trades)
    ledger["missing_history_windows"] = missing_windows
    ledger["history_days_by_symbol"] = coverage
    ledger["exit_calibration"] = calibration
    ledger["discovery_results"] = {key: discovery[key] for key in (
        "total_trades", "wins", "losses", "win_rate_pct", "net_profit_after_charges",
    )}
    if calibration_only:
        return ledger
    atomic_write_json(BACKTEST_LEDGER_FILE, ledger)
    stored_brain = MCXRegimeAdaptiveBrain(BRAIN_FILE)
    for trade in ledger.get("trades", []):
        stored_brain.record_trade_outcome(
            trade["symbol"], trade["setup"], trade["r_multiple"], trade["peak_r"],
            trade["net_pnl"], trade["exit_reason"],
        )
    for commodity in stored_brain.state.get("commodities", {}).values():
        commodity["consec_sl"] = 0
    stored_brain.state["last_exit_research"] = calibration
    if calibration["complete_for_deployment"]:
        stored_brain.state["exit_calibration"] = calibration
    stored_brain.save()
    log_mcx_backtest_results(ledger, logger)
    return ledger


def replay_mcx_history(
    days: int, universe: Dict[str, Any], hist_by_sym: Dict[str, Any], ts_to_idx: Dict[str, Any],
    sorted_timestamps: List[int], discovery_trades: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    brain = MCXRegimeAdaptiveBrain(BRAIN_FILE, silver_breakout_only=False)
    brain.reset_for_clean_learning()
    analyzer = MCXMultiRegimeAnalyzer(brain)

    ledger: Dict[str, Any] = {
        "mode": "BACKTEST_MCX_V14_REAL_WORLD_MASTER",
        "entry_calibration": "WALK_FORWARD" if discovery_trades is not None else "DISCOVERY",
        "span_days": days,
        "initial_capital": INITIAL_CAPITAL_RS,
        "virtual_balance_gross": INITIAL_CAPITAL_RS,
        "virtual_balance_net": INITIAL_CAPITAL_RS,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": 0.0,
        "avg_winning_net_rs": 0.0,
        "avg_losing_net_rs": 0.0,
        "win_to_loss_ratio": 0.0,
        "trading_days_active": 0,
        "avg_daily_net_pnl_rs": 0.0,
        "by_commodity": {},
        "by_playbook": {},
        "trades": [],
    }

    daily_stats: Dict[str, Dict[str, Any]] = {}
    sector_busy_until: Dict[str, float] = {"BULLION": 0.0, "ENERGY": 0.0}
    # Track failed levels per (day, symbol, direction) so the bot never re-buys the exact same chop zone
    failed_day_levels: Dict[Tuple[str, str, str], float] = {}

    pending_outcomes = []

    def settle_outcomes(as_of: float) -> None:
        pending_outcomes.sort(key=lambda outcome: outcome[0])
        while pending_outcomes and pending_outcomes[0][0] <= as_of:
            _, trade, stats = pending_outcomes.pop(0)
            net = trade["net_pnl"]
            brain.record_trade_outcome(
                trade["symbol"], trade["setup"], trade["r_multiple"], trade["peak_r"], net, trade["exit_reason"],
            )
            ledger["virtual_balance_gross"] = round(ledger["virtual_balance_gross"] + trade["gross_pnl"], 2)
            ledger["virtual_balance_net"] = round(ledger["virtual_balance_net"] + net, 2)
            stats["net_pnl"] = round(stats["net_pnl"] + net, 2)
            stats["consec_losses"] = stats["consec_losses"] + 1 if net <= 0 else 0

    for bar_ts in sorted_timestamps:
        settle_outcomes(bar_ts + 300.0)
        bar_dt = datetime.fromtimestamp(bar_ts + 300.0, IST)
        day_str = bar_dt.strftime("%Y-%m-%d")
        d_stat = daily_stats.setdefault(day_str, {"trades": 0, "consec_losses": 0, "net_pnl": 0.0})

        if (
            d_stat["trades"] >= MAX_TRADES_PER_DAY
            or d_stat["consec_losses"] >= MAX_CONSECUTIVE_LOSSES
            or d_stat["net_pnl"] <= -DAILY_LOSS_LIMIT_RS
            or d_stat["net_pnl"] >= DAILY_PROFIT_TARGET_RS
        ):
            continue

        candidates: List[Tuple[float, int, str, int, Dict[str, Any]]] = []
        for sym, merged in hist_by_sym.items():
            sec = MCX_MINI_CONFIG[sym]["sector"]
            if bar_ts < sector_busy_until.get(sec, 0.0):
                continue
            i = ts_to_idx[sym].get(bar_ts)
            if i is None or i < 65 or i >= len(merged["timestamp"]) - 2:
                continue
            u_info = universe[sym]
            sub_candles = {k: v[max(0, i - 105): i + 1] for k, v in merged.items()}
            view = analyzer.evaluate_commodity(sym, u_info, sub_candles, oc_data=None, as_of_epoch=bar_ts + 300.0)
            rec_d = view.get("recommended_dir")
            if rec_d in ("BUY_CE", "BUY_PE"):
                # Anti-Whipsaw Guard: if this symbol already stopped out in this direction today,
                # require price to clear the prior failed entry spot by >= 0.45 * ATR14
                failed_spot = failed_day_levels.get((day_str, sym, rec_d))
                if failed_spot is not None:
                    atr_now = float(view.get("atr14") or 1.0)
                    spot_now = float(view.get("spot") or 0.0)
                    if rec_d == "BUY_CE" and spot_now < failed_spot + 0.45 * atr_now:
                        continue
                    if rec_d == "BUY_PE" and spot_now > failed_spot - 0.45 * atr_now:
                        continue
                candidates.append((
                    float(view.get("priority_score", 0.0)),
                    int(view.get("conviction_score", 0)),
                    sym,
                    i,
                    view,
                ))

        if not candidates:
            continue

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        _, conv_score, sym, i, view = candidates[0]
        sec = MCX_MINI_CONFIG[sym]["sector"]
        merged = hist_by_sym[sym]
        u_info = universe[sym]
        cfg = MCX_MINI_CONFIG[sym]
        mult = u_info["multiplier"]
        tick = cfg["tick_size"]

        rec_dir = view["recommended_dir"]
        is_ce = rec_dir == "BUY_CE"
        sign = 1.0 if is_ce else -1.0
        entry_idx = i + 1
        if merged["timestamp"][entry_idx] != bar_ts + 300.0:
            continue
        fut_entry = merged["open"][entry_idx]
        strike = view["atm_ce"]["strike"] if is_ce else view["atm_pe"]["strike"]

        base_prem = estimate_atm_option_premium(sym, fut_entry)
        entry_price = round(math.ceil((base_prem * 1.0012) / tick) * tick, 2)
        entry_dt = datetime.fromtimestamp(merged["timestamp"][entry_idx], IST)
        entry_context = mcx_entry_context(view, entry_dt)
        exit_policy = brain.learned_exit_policy(sym, view["setup"], entry_context)

        playbook_base = re.sub(r"_(CE|PE)$", "", view["setup"])
        min_sl_pct = float(cfg.get("min_opt_sl_pct", 0.085))
        fut_atr = view.get("atr14")
        if fut_atr and fut_atr > 0:
            pb_sl_factor = 0.90 if playbook_base == "MCX_TREND_PULLBACK" else 0.98
            raw_stop_pts = max(fut_atr * cfg["sl_atr_mult"] * pb_sl_factor * 0.50, entry_price * min_sl_pct)
        else:
            raw_stop_pts = entry_price * min_sl_pct
        max_per_lot_stop_pts = (BASE_RISK_PER_LOT_RS - 25.0) / max(mult, 1)
        stop_pts = max(tick * 4, round(min(raw_stop_pts, max_per_lot_stop_pts) / tick) * tick)

        effective_cash = max(INITIAL_CAPITAL_RS, ledger["virtual_balance_net"])
        risk_per_lot_rs = stop_pts * mult + 25.0
        cost_per_lot_rs = entry_price * mult + 25.0
        dynamic_risk_budget = max(1050.0, effective_cash * 0.038)
        max_lots_by_risk = max(1, int(dynamic_risk_budget // max(risk_per_lot_rs, 1.0)))
        # Option buyers have zero margin risk beyond premium paid -> allow 98% cash deployment so 2 lots of SILVERM fit
        max_lots_by_cash = max(1, int((effective_cash * 0.98) // max(cost_per_lot_rs, 1.0)))
        max_safe_lots = min(int(cfg.get("max_lots", 5)), max_lots_by_risk, max_lots_by_cash)
        planned_rr, lots = brain.recommend_target_rr_and_lots(
            sym, playbook_base, conv_score, bool(view.get("is_macro_window", False)), max_safe_lots
        )
        if d_stat["consec_losses"] >= 1:
            lots = min(lots, 2) if lots >= 3 else 1
        if d_stat["net_pnl"] >= 2800.0 and playbook_base == "MCX_TREND_PULLBACK":
            lots = min(lots, 1)
        elif (
            entry_dt.time() < dtime(17, 50)
            and not view.get("is_macro_window", False)
            and conv_score < 5
        ):
            lots = min(lots, 3)
        if lots <= 0:
            continue

        qty = lots * mult
        fees = 40.0 + 20.0 * lots
        one_r_pts = stop_pts
        current_sl = round(entry_price - one_r_pts, 2)
        highest_bid = entry_price
        peak_r_achieved = 0.0
        exit_policy_net_r = {}
        policy_prices = {
            policy: round(math.ceil((entry_price + target_r * one_r_pts + fees / qty) / tick - 1e-9) * tick, 2)
            for policy, target_r in EXIT_POLICY_TARGETS.items()
        }
        ratchet_armed = False
        exit_idx = entry_idx
        exit_price = entry_price
        exit_reason = "MCX_EOD_SQUAREOFF"
        n_bars = len(merged["timestamp"])
        runner_position = {
            "option_type": "CE" if is_ce else "PE", "spot_at_entry": fut_entry, "planned_target_r": planned_rr,
            "lots": lots, "qty": qty, "entry_price": entry_price, "stop_points": one_r_pts, "setup": view["setup"],
        }

        for j in range(entry_idx, min(n_bars, entry_idx + 32)):
            j_ts = merged["timestamp"][j]
            j_dt = datetime.fromtimestamp(j_ts + 300.0, IST)
            if j_dt.strftime("%Y-%m-%d") != day_str or j_dt.time() >= MCX_EOD_SQUAREOFF_TIME:
                exit_idx = j
                exit_reason = "MCX_EOD_SQUAREOFF"
                break

            elapsed_bars = j - entry_idx + 1
            fut_fav = (merged["high"][j] - fut_entry) if is_ce else (fut_entry - merged["low"][j])
            fut_adv = (merged["low"][j] - fut_entry) if is_ce else (fut_entry - merged["high"][j])
            fut_close_move = sign * (merged["close"][j] - fut_entry)
            bar_is_favorable = (merged["close"][j] >= merged["open"][j]) if is_ce else (merged["close"][j] <= merged["open"][j])

            theta_burn = base_prem * 0.0009 * elapsed_bars
            delta_adv = 0.49
            delta_fav = min(0.76, 0.55 + 0.10 * max(0.0, fut_fav / max(cfg["strike_step"], 1.0)))

            opt_low = max(tick, round((entry_price + delta_adv * fut_adv - theta_burn) / tick) * tick)
            opt_high = max(opt_low, round((entry_price + delta_fav * fut_fav - theta_burn) / tick) * tick)
            opt_close = max(tick, round((entry_price + 0.56 * fut_close_move - theta_burn) / tick) * tick)
            open_move = sign * (merged["open"][j] - fut_entry)
            opt_open = round(max(tick, entry_price + 0.56 * open_move - theta_burn), 2)

            if opt_open <= current_sl or (not bar_is_favorable and opt_low <= current_sl):
                exit_idx = j
                stop_fill = min(current_sl, opt_open) if opt_open <= current_sl else current_sl
                exit_price = round(max(tick, math.floor((stop_fill * 0.999) / tick) * tick), 2)
                exit_reason = "TRAIL_PROFIT_HIT" if ratchet_armed else "INITIAL_SL_HIT"
                break

            highest_bid = max(highest_bid, opt_high, opt_open)
            peak_r = (highest_bid - entry_price) / max(one_r_pts, 0.01)
            peak_r_achieved = max(peak_r_achieved, peak_r)

            for policy, price in policy_prices.items():
                if highest_bid >= price and policy not in exit_policy_net_r:
                    exit_policy_net_r[policy] = round(((price - entry_price) * qty - fees) / (one_r_pts * qty), 3)

            # Progressive Real-World Target & Super-Runner Trailing Engine
            if peak_r >= NORMAL_TARGET_R:
                j_sub = {k: v[max(0, j - 90): j + 1] for k, v in merged.items()}
                j_view = analyzer.evaluate_commodity(sym, u_info, j_sub, oc_data=None, as_of_epoch=j_ts + 300.0)
                runner_mode, _ = autonomous_manage_runner_and_exit(j_view, runner_position, peak_r)

                if runner_mode == 2:
                    exit_idx = j
                    target_2r_px = round(entry_price + (2.05 * one_r_pts) + (fees / qty), 2)
                    exit_price = round(min(highest_bid, max(opt_open, target_2r_px)), 2)
                    exit_reason = "TARGET_1_2_RR_HIT"
                    break
                elif runner_mode == 3:
                    lock_px = round(entry_price + (1.95 * one_r_pts) + (fees / qty), 2)
                    current_sl = max(current_sl, lock_px)
                    ratchet_armed = True
                    if peak_r >= EXTENDED_TARGET_R:
                        exit_idx = j
                        target_3r_px = round(entry_price + (3.05 * one_r_pts) + (fees / qty), 2)
                        exit_price = round(min(highest_bid, max(opt_open, target_3r_px)), 2)
                        exit_reason = "TARGET_1_3_RR_RUNNER_HIT"
                        break
                elif runner_mode >= 4:
                    # Progressive Super-Runner Ratchet:
                    # If peak_r >= 4.0R and bar closes strong, trail 75% of peak_r up to 8.0R; otherwise book 4.05R!
                    if peak_r >= 4.0:
                        if peak_r >= 8.0:
                            exit_idx = j
                            target_8r_px = round(entry_price + (8.05 * one_r_pts) + (fees / qty), 2)
                            exit_price = round(min(highest_bid, max(opt_open, target_8r_px)), 2)
                            exit_reason = "TARGET_1_8_RR_SUPER_RUNNER_HIT"
                            break
                        trail_mult = max(4.05, min(peak_r * 0.78, 7.50))
                        lock_px = round(entry_price + (trail_mult * one_r_pts) + (fees / qty), 2)
                        current_sl = max(current_sl, lock_px)
                        ratchet_armed = True
                        if opt_close <= current_sl:
                            exit_idx = j
                            exit_price = current_sl
                            exit_reason = "TARGET_1_4_RR_SUPER_RUNNER_HIT"
                            break
                    else:
                        floor_mult = 2.85 if peak_r >= 3.0 else 2.00
                        lock_px = round(entry_price + (floor_mult * one_r_pts) + (fees / qty), 2)
                        current_sl = max(current_sl, lock_px)
                        ratchet_armed = True

            elif peak_r >= 1.60:
                lock_1r_px = round(entry_price + (1.10 * one_r_pts) + (fees / qty), 2)
                current_sl = max(current_sl, lock_1r_px)
                ratchet_armed = True
            elif peak_r >= 1.15:
                be_px = round(entry_price + (0.15 * one_r_pts) + (fees / qty), 2)
                current_sl = max(current_sl, be_px)
                ratchet_armed = True

            if opt_low <= current_sl:
                exit_idx = j
                exit_price = round(max(tick, math.floor((current_sl * 0.999) / tick) * tick), 2)
                exit_reason = "TRAIL_PROFIT_HIT" if ratchet_armed else "INITIAL_SL_HIT"
                break

            if elapsed_bars >= 6 and peak_r < 0.55:
                j_close_fut = merged["close"][j]
                ev_vwap_now = view.get("evening_vwap", fut_entry)
                ema21_now = view.get("ema21", fut_entry)
                if (is_ce and j_close_fut < min(ev_vwap_now, ema21_now)) or (
                    not is_ce and j_close_fut > max(ev_vwap_now, ema21_now)
                ):
                    exit_idx = j
                    exit_price = opt_close
                    exit_reason = "STRUCTURAL_TREND_INVALIDATED"
                    break

            exit_idx = j
            exit_price = opt_close

        gross_pnl, net_pnl, charges = mcx_exit_pnl(runner_position, exit_price)
        r_mult = round(net_pnl / max(one_r_pts * qty, 1.0), 2)
        if net_pnl <= 0:
            failed_day_levels[(day_str, sym, rec_dir)] = fut_entry
        else:
            failed_day_levels.pop((day_str, sym, rec_dir), None)

        pb_stat = ledger["by_playbook"].setdefault(playbook_base, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
        pb_stat["trades"] += 1
        pb_stat["wins" if net_pnl > 0 else "losses"] += 1
        pb_stat["net_pnl"] = round(pb_stat["net_pnl"] + net_pnl, 2)

        trade_rec = {
            "timestamp": entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_timestamp": datetime.fromtimestamp(merged["timestamp"][exit_idx] + 300.0, IST).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym,
            "sector": sec,
            "regime": view.get("regime", "UNKNOWN"),
            "contract": f"{sym}_{int(strike)}_{'CE' if is_ce else 'PE'}",
            "lots": lots,
            "qty": qty,
            "spot_at_entry": round(fut_entry, 2),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "charges": charges,
            "partial_exit": runner_position.get("partial_exit"),
            "r_multiple": r_mult,
            "peak_r": round(peak_r_achieved, 2),
            "planned_rr": planned_rr,
            "entry_context": entry_context,
            "exit_policy": exit_policy,
            "exit_policy_net_r": exit_policy_net_r,
            "conviction": conv_score,
            "exit_reason": exit_reason,
            "setup": view["setup"],
        }
        ledger["trades"].append(trade_rec)
        pending_outcomes.append((merged["timestamp"][exit_idx] + 300.0, trade_rec, d_stat))

        c_stat = ledger["by_commodity"].setdefault(sym, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
        c_stat["trades"] += 1
        c_stat["wins" if net_pnl > 0 else "losses"] += 1
        c_stat["net_pnl"] = round(c_stat["net_pnl"] + net_pnl, 2)

        d_stat["trades"] += 1

        cooldown_sec = (POST_LOSS_COOLDOWN_MIN if net_pnl <= 0 else POST_WIN_COOLDOWN_MIN) * 60.0
        sector_busy_until[sec] = merged["timestamp"][exit_idx] + 300.0 + cooldown_sec

    settle_outcomes(float("inf"))
    ledger["trades"].sort(key=lambda x: x["timestamp"])
    ledger["total_trades"] = len(ledger["trades"])
    wins = [t["net_pnl"] for t in ledger["trades"] if t["net_pnl"] > 0]
    losses = [abs(t["net_pnl"]) for t in ledger["trades"] if t["net_pnl"] <= 0]
    ledger["wins"] = len(wins)
    ledger["losses"] = len(losses)
    ledger["win_rate_pct"] = round((len(wins) / len(ledger["trades"])) * 100.0, 1) if ledger["trades"] else 0.0
    ledger["profit_without_charges"] = round(ledger["virtual_balance_gross"] - INITIAL_CAPITAL_RS, 2)
    ledger["net_profit_after_charges"] = round(ledger["virtual_balance_net"] - INITIAL_CAPITAL_RS, 2)
    ledger["avg_winning_net_rs"] = round(sum(wins) / len(wins), 2) if wins else 0.0
    ledger["avg_losing_net_rs"] = round(sum(losses) / len(losses), 2) if losses else 0.0
    if ledger["avg_losing_net_rs"] > 0:
        ledger["win_to_loss_ratio"] = round(ledger["avg_winning_net_rs"] / ledger["avg_losing_net_rs"], 2)

    active_days = [d for d, s in daily_stats.items() if s["trades"] > 0]
    ledger["trading_days_active"] = len(active_days)
    ledger["avg_daily_net_pnl_rs"] = (
        round(ledger["net_profit_after_charges"] / max(len(active_days), 1), 2) if active_days else 0.0
    )

    return ledger


def log_mcx_backtest_results(ledger: Dict[str, Any], logger: logging.Logger) -> None:
    logger.info("=" * 118)
    logger.info(
        "%-3s | %-19s | %-8s | %-20s | %-4s | %-9s | %-15s | %-9s | %-6s | %-22s",
        "#", "Entry Timestamp", "Exit", "Contract", "Lots", "Fut Spot", "Opt Entry->Exit", "Net PnL", "R-Mult", "Exit Reason"
    )
    logger.info("-" * 118)
    for idx, t in enumerate(ledger.get("trades", []), start=1):
        logger.info(
            "%-3d | %-19s | %-8s | %-20s | %d(%2d)| %9.1f | %6.1f -> %-6.1f | %+9.2f | %+5.2fR | %-22s",
            idx,
            t["timestamp"],
            t["exit_timestamp"][11:],
            t["contract"],
            t["lots"],
            t["qty"],
            t["spot_at_entry"],
            t["entry_price"],
            t["exit_price"],
            t["net_pnl"],
            t["r_multiple"],
            t["exit_reason"],
        )
    logger.info("=" * 118)
    logger.info(
        "[MCX V14 REAL-WORLD MASTER COMPLETE] Trades: %d (W:%d / L:%d | WR: %.1f%%) | Gross PnL: Rs.%+.2f | Net PnL (After Charges): Rs.%+.2f",
        ledger["total_trades"], ledger["wins"], ledger["losses"], ledger["win_rate_pct"],
        ledger["profit_without_charges"], ledger["net_profit_after_charges"]
    )
    logger.info(
        "[DAILY & RR METRICS] Active Days: %d | Avg Daily Net Profit: Rs.%+.2f/day | Avg Win: Rs.%.2f vs Avg Loss: Rs.%.2f (%.2fx RR)",
        ledger["trading_days_active"], ledger["avg_daily_net_pnl_rs"],
        ledger["avg_winning_net_rs"], ledger["avg_losing_net_rs"], ledger["win_to_loss_ratio"]
    )
    for k, v in ledger["by_commodity"].items():
        logger.info("   -> [COMMODITY] %-10s | Trades: %2d (W:%2d/L:%2d) | Net PnL: Rs.%+.2f", k, v["trades"], v["wins"], v["losses"], v["net_pnl"])
    for k, v in ledger["by_playbook"].items():
        logger.info("   -> [PLAYBOOK]  %-24s | Trades: %2d (W:%2d/L:%2d) | Net PnL: Rs.%+.2f", k, v["trades"], v["wins"], v["losses"], v["net_pnl"])
    logger.info("Saved full trade-by-trade audit to %s & learned brain to %s", BACKTEST_LEDGER_FILE, BRAIN_FILE)
    logger.info("=" * 118)


def auto_calibrate_today(brain: MCXRegimeAdaptiveBrain, symbols: List[str], logger: logging.Logger) -> None:
    today = datetime.now(IST).date().isoformat()
    previous = brain.state.get("exit_calibration", {})
    if (
        previous.get("version") == MCX_CALIBRATION_VERSION
        and previous.get("date") == today and set(symbols) <= set(previous.get("symbols", []))
    ):
        return
    logger.info("[MCX CALIBRATION] Evaluating %d days of completed sessions", MCX_CALIBRATION_DAYS)
    try:
        replay = run_mcx_historical_backtest(MCX_CALIBRATION_DAYS, symbols, logger, calibration_only=True)
    except (RuntimeError, ValueError, KeyError, OSError, requests.RequestException) as error:
        logger.warning("[MCX CALIBRATION] Unavailable (%s); continuing with existing policies", type(error).__name__)
        return
    calibration = replay["exit_calibration"]
    if not calibration["complete_for_deployment"]:
        logger.warning("[MCX CALIBRATION] Incomplete history (%s); continuing with existing policies", replay["history_days_by_symbol"])
        return
    brain.state["exit_calibration"] = calibration
    brain.save()


def run_mcx_session(live_mode: bool, symbols: List[str], logger: logging.Logger) -> None:
    if live_mode:
        raise RuntimeError("Run in PAPER mode first to validate MCX Mini Option spreads on your account.")

    session_now = datetime.now(IST)
    if not mcx_evening_session_open(session_now) or session_now.time() >= MCX_PAPER_QUOTE_CUTOFF:
        logger.info("MCX PAPER session skipped outside the supported evening session (%s IST).", session_now.isoformat())
        return

    env = load_dotenv_file(".env")
    client_id = env.get("DHAN_CLIENT_ID", "")
    access_token = env.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        logger.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in .env!")
        sys.exit(1)

    dhan = DhanMCXClient(client_id, access_token, logger)
    brain = MCXRegimeAdaptiveBrain(BRAIN_FILE)
    analyzer = MCXMultiRegimeAnalyzer(brain)
    universe = dhan.build_mcx_universe(symbols)
    if not universe:
        logger.error("No active MCX contracts resolved. Check Scrip Master.")
        sys.exit(1)

    ledger = load_json(LEDGER_FILE, {
        "mode": "PAPER",
        "initial_capital": INITIAL_CAPITAL_RS,
        "virtual_balance_gross": INITIAL_CAPITAL_RS,
        "virtual_balance_net": INITIAL_CAPITAL_RS,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "trades": [],
        "active_position": None,
    })

    calibration_attempted = False

    today_str = session_now.date().isoformat()
    today_trades = [t for t in ledger.get("trades", []) if str(t.get("timestamp", "")).startswith(today_str)]
    day_net_pnl = round(sum(float(t.get("net_pnl", 0.0)) for t in today_trades), 2)
    consec_losses = 0
    for t in reversed(today_trades):
        if float(t.get("net_pnl", 0.0)) <= 0:
            consec_losses += 1
        else:
            break

    active_pos = ledger.get("active_position")
    sector_busy_until: Dict[str, datetime] = {}
    failed_day_levels: Dict[Tuple[str, str], float] = {}
    for trade in today_trades:
        side = str(trade.get("contract", "")).rsplit("_", 1)[-1]
        direction = f"BUY_{side}" if side in ("CE", "PE") else ""
        if direction and float(trade.get("net_pnl", 0.0)) <= 0:
            failed_day_levels[(str(trade.get("symbol", "")), direction)] = float(
                trade.get("spot_at_entry", 0.0)
            )
        elif direction:
            failed_day_levels.pop((str(trade.get("symbol", "")), direction), None)

        exit_ts = trade.get("exit_timestamp")
        symbol = str(trade.get("symbol", ""))
        if exit_ts and symbol in MCX_MINI_CONFIG:
            exit_dt = datetime.strptime(exit_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            cooldown_min = (
                POST_LOSS_COOLDOWN_MIN
                if float(trade.get("net_pnl", 0.0)) <= 0
                else POST_WIN_COOLDOWN_MIN
            )
            sector = MCX_MINI_CONFIG[symbol]["sector"]
            sector_busy_until[sector] = max(
                sector_busy_until.get(sector, exit_dt),
                exit_dt + timedelta(minutes=cooldown_min),
            )

    active_symbols = list(universe.keys())
    cycle = 0
    scan_bar_by_symbol: Dict[str, float] = {}
    scan_view_by_symbol: Dict[str, Dict[str, Any]] = {}
    last_heartbeat_bucket = ""

    logger.info(
        "MCX Mini Options Bot v14.0 Started (17:15-22:30 IST entries; 23:15 square-off) | Symbols: %s | Wallet: Rs.%.2f | Today Net: Rs.%+.2f",
        active_symbols, ledger["virtual_balance_net"], day_net_pnl
    )

    while True:
        now = datetime.now(IST)
        if now.date() != session_now.date() or now.time() >= MCX_PAPER_QUOTE_CUTOFF:
            if active_pos:
                logger.error("MCX PAPER square-off unresolved at quote cutoff. Open position retained in the ledger.")
            break
        if now.time() >= MCX_EOD_SQUAREOFF_TIME and not active_pos:
            logger.info("23:15 IST MCX EOD cutoff reached. Shutting down cleanly.")
            break
        if now.time() < MCX_EVENING_ORB_START:
            time.sleep(5.0)
            continue

        if not active_pos and not calibration_attempted and now.time() <= MCX_LAST_ENTRY_TIME:
            calibration_attempted = True
            auto_calibrate_today(brain, symbols, logger)
            continue

        sym = active_pos["symbol"] if active_pos else active_symbols[cycle % len(active_symbols)]
        cycle += 1
        u_info = universe[sym]
        cfg = MCX_MINI_CONFIG[sym]

        candles = dhan.fetch_futures_candles_5m(u_info["fut_security_id"])
        if not candles:
            time.sleep(1.5)
            continue
        spot_hint = float(candles["close"][-1])
        oc_data = dhan.fetch_option_chain_or_quotes(sym, u_info, spot_hint)
        if not oc_data:
            time.sleep(1.5)
            continue

        now = datetime.now(IST)
        if now.date() != session_now.date() or now.time() >= MCX_PAPER_QUOTE_CUTOFF:
            continue

        view = analyzer.evaluate_commodity(sym, u_info, candles, oc_data)
        closed_bar_epochs = [
            float(ts) for ts in candles.get("timestamp", [])
            if float(ts) + 300.0 <= now.timestamp()
        ]
        if closed_bar_epochs:
            scan_bar_by_symbol[sym] = max(closed_bar_epochs)
            scan_view_by_symbol[sym] = view
        telemetry = load_json(TELEMETRY_FILE, {"commodities": {}})
        telemetry["updated_at"] = now.strftime("%H:%M:%S")
        telemetry["wallet_net"] = ledger["virtual_balance_net"]
        telemetry["net_pnl_total"] = ledger["net_profit_after_charges"]
        telemetry["day_net_pnl"] = day_net_pnl
        telemetry["total_trades"] = ledger["total_trades"]
        telemetry["brain_summary"] = brain.state.get("playbooks", {})
        telemetry.setdefault("commodities", {})[sym] = view
        telemetry["active_position"] = active_pos
        telemetry["recent_trades"] = ledger.get("trades", [])[-4:]
        atomic_write_json(TELEMETRY_FILE, telemetry)
        heartbeat_bucket = f"{now.strftime('%H:%M:')}{now.second // 2 * 2:02d}"
        if heartbeat_bucket != last_heartbeat_bucket:
            last_heartbeat_bucket = heartbeat_bucket
            summary_parts = []
            for symbol in active_symbols:
                cached_view = scan_view_by_symbol.get(symbol)
                if cached_view is None:
                    summary_parts.append(f"{symbol}:SCANNING")
                    continue
                age_min = max(
                    0.0,
                    (now.timestamp() - (scan_bar_by_symbol[symbol] + 300.0)) / 60.0,
                )
                freshness = "" if age_min <= 5.0 else f",STALE={age_min:.0f}m"
                summary_parts.append(
                    f"{symbol}:{cached_view.get('recommended_dir', 'HOLD')}"
                    f"(Spot={cached_view.get('spot', 0):.0f},"
                    f"ADX={cached_view.get('adx14', 0):.1f},"
                    f"CHOP={cached_view.get('chop14', 0):.1f},"
                    f"Vol={cached_view.get('vol_ratio', 0):.2f}x,"
                    f"Conv={cached_view.get('conviction_score', 0)}/6{freshness})"
                )
            logger.info("[MCX LIVE SCAN] %s", " | ".join(summary_parts))

        if active_pos and active_pos["symbol"] == sym:
            strike_k = float(active_pos["strike"])
            side_k = active_pos["option_type"].lower()
            oc = oc_data.get("oc", {})
            node = oc.get(f"{strike_k:.6f}", oc.get(str(strike_k), {})).get(side_k, {})
            curr_bid = float(node.get("top_bid_price") or 0.0)
            if curr_bid <= 0:
                continue

            active_pos["current_bid"] = curr_bid
            active_pos["highest_bid"] = max(float(active_pos["highest_bid"]), curr_bid)
            qty = int(active_pos["qty"])
            lots = int(active_pos["lots"])
            entry_p = float(active_pos["entry_price"])
            one_r_pts = float(active_pos["stop_points"])
            fees = 40.0 + 20.0 * lots
            _, open_net, _ = mcx_exit_pnl(active_pos, curr_bid)
            peak_r = (float(active_pos["highest_bid"]) - entry_p) / max(one_r_pts, 0.01)
            elapsed_min = (now.timestamp() - float(active_pos["entry_epoch"])) / 60.0

            exit_reason = None
            if curr_bid <= float(active_pos["current_sl"]):
                exit_reason = "TRAIL_PROFIT_HIT" if active_pos.get("ratchet_armed") else "INITIAL_SL_HIT"
            elif open_net <= -MAX_TOTAL_RISK_RS:
                exit_reason = "MAX_RUPEE_RISK_CAP"
            elif now.time() >= MCX_EOD_SQUAREOFF_TIME:
                exit_reason = "MCX_EOD_SQUAREOFF"
            else:
                lock_r = None
                if peak_r >= NORMAL_TARGET_R:
                    runner_mode, runner_msg = autonomous_manage_runner_and_exit(view, active_pos, peak_r)
                    active_pos["runner_status"] = runner_msg
                    active_pos["active_target_r"] = max(
                        float(active_pos.get("active_target_r", NORMAL_TARGET_R)), runner_mode
                    )

                    if runner_mode == 2:
                        exit_reason = "TARGET_1_2_RR_HIT"
                    elif runner_mode == 3:
                        lock_r = 1.95
                        if peak_r >= EXTENDED_TARGET_R:
                            exit_reason = "TARGET_1_3_RR_RUNNER_HIT"
                    elif runner_mode >= 4:
                        if peak_r >= SUPER_RUNNER_TARGET_R:
                            exit_reason = "TARGET_1_8_RR_SUPER_RUNNER_HIT"
                        elif peak_r >= 4.0:
                            lock_r = max(4.05, min(peak_r * 0.78, 7.50))
                        else:
                            lock_r = 2.85 if peak_r >= 3.0 else 2.00
                elif peak_r >= 1.60:
                    lock_r = 1.10
                elif peak_r >= 1.15:
                    lock_r = 0.15

                if lock_r is not None:
                    lock = round(
                        entry_p + lock_r * one_r_pts + fees / qty, 2
                    )
                    active_pos["current_sl"] = max(float(active_pos["current_sl"]), lock)
                    active_pos["ratchet_armed"] = True
                if (
                    not exit_reason
                    and curr_bid <= float(active_pos["current_sl"])
                ):
                    exit_reason = (
                        "TRAIL_PROFIT_HIT"
                        if active_pos.get("ratchet_armed")
                        else "INITIAL_SL_HIT"
                    )

            if not exit_reason:
                if elapsed_min >= 30.0 and peak_r < 0.55:
                    ev_vwap_now = float(view.get("evening_vwap") or active_pos["spot_at_entry"])
                    ema21_now = float(view.get("ema21") or ev_vwap_now)
                    spot_now = float(view.get("spot") or active_pos["spot_at_entry"])
                    is_ce_pos = active_pos["option_type"] == "CE"
                    if (is_ce_pos and spot_now < min(ev_vwap_now, ema21_now)) or (
                        not is_ce_pos and spot_now > max(ev_vwap_now, ema21_now)
                    ):
                        exit_reason = "STRUCTURAL_TREND_INVALIDATED"

            ledger["active_position"] = active_pos
            atomic_write_json(LEDGER_FILE, ledger)

            if exit_reason:
                gross_pnl, net_pnl, charges = mcx_exit_pnl(active_pos, curr_bid)
                r_mult = round(net_pnl / max(one_r_pts * qty, 1.0), 2)

                brain.record_trade_outcome(
                    sym=sym,
                    setup_full=active_pos["setup"],
                    r_multiple=r_mult,
                    peak_r=peak_r,
                    net_pnl=net_pnl,
                    exit_reason=exit_reason,
                )
                if "entry_context" in active_pos:
                    brain.record_episode(sym, active_pos["setup"], active_pos["entry_context"], now, r_mult)
                brain.save()

                trade_rec = {
                    "timestamp": active_pos["entry_ts"],
                    "exit_timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": sym,
                    "regime": active_pos.get("regime", "UNKNOWN"),
                    "contract": f"{sym}_{int(strike_k)}_{active_pos['option_type']}",
                    "lots": lots,
                    "qty": qty,
                    "spot_at_entry": active_pos["spot_at_entry"],
                    "entry_price": entry_p,
                    "exit_price": curr_bid,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "charges": charges,
                    "partial_exit": active_pos.get("partial_exit"),
                    "r_multiple": r_mult,
                    "peak_r": round(peak_r, 2),
                    "planned_rr": active_pos.get("planned_target_r", NORMAL_TARGET_R),
                    "exit_reason": exit_reason,
                    "setup": active_pos["setup"],
                }
                ledger["virtual_balance_gross"] = round(ledger["virtual_balance_gross"] + gross_pnl, 2)
                ledger["virtual_balance_net"] = round(ledger["virtual_balance_net"] + net_pnl, 2)
                ledger["profit_without_charges"] = round(ledger["virtual_balance_gross"] - ledger["initial_capital"], 2)
                ledger["net_profit_after_charges"] = round(ledger["virtual_balance_net"] - ledger["initial_capital"], 2)
                ledger["trades"].append(trade_rec)
                ledger["total_trades"] = len(ledger["trades"])
                ledger["active_position"] = None
                atomic_write_json(LEDGER_FILE, ledger)

                day_net_pnl = round(day_net_pnl + net_pnl, 2)
                today_trades.append(trade_rec)
                direction = f"BUY_{active_pos['option_type']}"
                if net_pnl <= 0:
                    consec_losses += 1
                    failed_day_levels[(sym, direction)] = float(active_pos["spot_at_entry"])
                    cooldown_min = POST_LOSS_COOLDOWN_MIN
                else:
                    consec_losses = 0
                    failed_day_levels.pop((sym, direction), None)
                    cooldown_min = POST_WIN_COOLDOWN_MIN
                sector_busy_until[MCX_MINI_CONFIG[sym]["sector"]] = (
                    now + timedelta(minutes=cooldown_min)
                )

                logger.info(
                    "[AUTONOMOUS EXIT] %s (%s) | Entry: %.2f -> Exit: %.2f | Net PnL: Rs.%+.2f (%+.2fR) | Day Net: Rs.%+.2f",
                    trade_rec["contract"], exit_reason, entry_p, curr_bid, net_pnl, r_mult, day_net_pnl
                )
                active_pos = None

        elif (
            u_info["is_mini_option"] and u_info["All_futs"][0]["is_exact_mini"]
            and MCX_ENTRY_START_TIME <= now.time() <= MCX_LAST_ENTRY_TIME
            and len(today_trades) < MAX_TRADES_PER_DAY
            and consec_losses < MAX_CONSECUTIVE_LOSSES
            and day_net_pnl > -DAILY_LOSS_LIMIT_RS
            and day_net_pnl < DAILY_PROFIT_TARGET_RS
            and now >= sector_busy_until.get(cfg["sector"], now)
        ):
            rec_dir = view.get("recommended_dir", "HOLD")
            if rec_dir in ("BUY_CE", "BUY_PE"):
                failed_spot = failed_day_levels.get((sym, rec_dir))
                if failed_spot is not None:
                    atr_now = float(view.get("atr14") or 1.0)
                    spot_now = float(view.get("spot") or 0.0)
                    if (
                        rec_dir == "BUY_CE" and spot_now < failed_spot + 0.45 * atr_now
                    ) or (
                        rec_dir == "BUY_PE" and spot_now > failed_spot - 0.45 * atr_now
                    ):
                        logger.info("[SKIP %s] Failed-level anti-whipsaw filter (%s)", sym, rec_dir)
                        continue

                entry_context = mcx_entry_context(view, now)
                if brain.rejects_episode(sym, view["setup"], entry_context, now):
                    logger.info("[SKIP %s] Recent matching PAPER episodes have negative expectancy", sym)
                    continue
                exit_policy = brain.learned_exit_policy(sym, view["setup"], entry_context)
                is_ce = rec_dir == "BUY_CE"
                opt_node = view["atm_ce"] if is_ce else view["atm_pe"]
                bid = float(opt_node.get("top_bid_price") or 0.0)
                ask = float(opt_node.get("top_ask_price") or opt_node.get("last_price") or 0.0)
                if ask <= 0:
                    continue
                if bid <= 0:
                    bid = ask * 0.992

                spread_pct = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
                if spread_pct > 2.2:
                    logger.info("[SKIP %s] Spread %.2f%% > 2.2%% max limit", sym, spread_pct)
                    continue
                if not (cfg["min_premium"] <= ask <= cfg["max_premium"]):
                    continue

                stop_pts, lots, planned_rr = compute_mcx_stop_and_lots(
                    sym,
                    view["setup"],
                    ask,
                    u_info["multiplier"],
                    ledger["virtual_balance_net"],
                    consec_losses,
                    brain=brain,
                    conviction=int(view.get("conviction_score", 3)),
                    is_macro_window=bool(view.get("is_macro_window", False)),
                    fut_atr=view.get("atr14"),
                    atr_expansion=view.get("atr_expansion", 1.0),
                )
                playbook_base = re.sub(r"_(CE|PE)$", "", str(view.get("setup", "")))
                conviction = int(view.get("conviction_score", 3))
                if day_net_pnl >= 2800.0 and playbook_base == "MCX_TREND_PULLBACK":
                    lots = min(lots, 1)
                elif (
                    now.time() < dtime(17, 50)
                    and not view.get("is_macro_window", False)
                    and conviction < 5
                ):
                    lots = min(lots, 3)
                if lots <= 0:
                    continue

                qty = lots * u_info["multiplier"]
                init_sl = round(ask - stop_pts, 2)
                active_pos = {
                    "symbol": sym,
                    "regime": view.get("regime", "UNKNOWN"),
                    "strike": opt_node["strike"],
                    "option_type": "CE" if is_ce else "PE",
                    "security_id": str(opt_node.get("security_id", "")),
                    "expiry": u_info["opt_expiry"],
                    "lots": lots,
                    "qty": qty,
                    "entry_price": ask,
                    "current_bid": bid,
                    "highest_bid": bid,
                    "stop_points": stop_pts,
                    "current_sl": init_sl,
                    "planned_target_r": planned_rr,
                    "entry_context": entry_context,
                    "exit_policy": exit_policy,
                    "fat_tail": mcx_fat_tail_entry(view, lots, now.time()),
                    "continuation": mcx_continuation(view, is_ce),
                    "ratchet_armed": False,
                    "entry_epoch": now.timestamp(),
                    "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "spot_at_entry": view["spot"],
                    "setup": view["setup"],
                }
                ledger["active_position"] = active_pos
                atomic_write_json(LEDGER_FILE, ledger)
                logger.info(
                    "[AUTONOMOUS ENTRY] %s %s %s | Lots:%d (Qty:%d) @ Rs.%.2f | SL: Rs.%.2f (1R=%.2f) | Aim: 1:%.0f RR | %s",
                    sym, opt_node["strike"], active_pos["option_type"], lots, qty, ask, init_sl, stop_pts, planned_rr, view["reason"]
                )

        time.sleep(0.2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DhanHQ v2 Autonomous Institutional MCX Mini Options Bot")
    parser.add_argument(
        "--symbols",
        type=str,
        default="GOLDM,SILVERM,CRUDEOILM,NATGASMINI",
        help="Comma-separated MCX Mini symbols (default: GOLDM,SILVERM,CRUDEOILM,NATGASMINI)",
    )
    parser.add_argument(
        "--backtest-days",
        type=int,
        default=0,
        help="Run real DhanHQ 5m historical MCX backtest for past N days (e.g. 30, 60, 90)",
    )
    parser.add_argument("--reset-wallet", action="store_true", help="Reset MCX paper wallet to Rs. 30,000")
    parser.add_argument("--live", action="store_true", help="Enable LIVE order mode")
    args = parser.parse_args()

    if args.reset_wallet and os.path.exists(LEDGER_FILE):
        os.remove(LEDGER_FILE)

    logger = setup_logger()
    selected = [s.strip().upper() for s in args.symbols.split(",") if s.strip().upper() in MCX_MINI_CONFIG]
    if not selected:
        parser.error(f"--symbols must be from {list(MCX_MINI_CONFIG.keys())}")

    if args.backtest_days > 0:
        run_mcx_historical_backtest(days=args.backtest_days, symbols=selected, logger=logger)
    else:
        run_mcx_session(live_mode=args.live, symbols=selected, logger=logger)

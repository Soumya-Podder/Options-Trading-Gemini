#!/usr/bin/env python3
"""
Autonomous Self-Learning NIFTY 50 & SENSEX Options Buying Bot (DhanHQ v4.1)
===========================================================================
Operating Window : 09:10 AM to 03:45 PM IST
Virtual Balance  : Rs. 30,000 (Paper Mode Default, Live Mode Switchable)
Monthly Goal     : +Rs. 10,000 Profit (tracks Gross without charges & Net after charges)
Core Mandates    :
  1. Asymmetric Payoff: Avg Winning Trade Value >> Avg Losing Trade Value
  2. Straddling 3-Strike 50-Lakh OI Rule:
     e.g., at Spot 22492 -> |(22500+22550+22600 CE) - (22450+22400+22350 PE)| >= 50 Lakhs
  3. 4-Quadrant Analysis: Long Buildup, Short Buildup, Short Covering, Long Unwinding
  4. Anti-Overfitting Regularization: L2 Weight Decay & TD-Error Clipping
  5. Dynamic Multi-Lot Sizing: Automatically scales 1 to 4 lots based on equity and conviction
  6. Held-Strike Tracker: Eliminates ATM-reset bug as Spot moves In-The-Money (ITM)
"""

import os
import sys
import time
import math
import json
import random
import logging
import argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple, Any
import numpy as np

# Load environment variables from local .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from dhanhq import DhanContext, dhanhq
except ImportError:
    DhanContext, dhanhq = None, None

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("dhan_bot_session.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("DhanAutonomousBotV41")


# ===========================================================================
# 1. MARKET & BOT CONFIGURATION (WITH ANTI-OVERFITTING & MULTI-LOT SETTINGS)
# ===========================================================================
@dataclass
class IndexSpec:
    symbol: str
    under_security_id: int
    under_segment: str
    fno_segment: str
    lot_size: int
    strike_step: int
    exchange_fee_pct: float
    oi_diff_threshold: int


INDEX_SPECS: Dict[str, IndexSpec] = {
    "NIFTY": IndexSpec(
        symbol="NIFTY",
        under_security_id=13,
        under_segment="IDX_I",
        fno_segment="NSE_FNO",
        lot_size=65,                # 2026 NSE Nifty 50 lot size
        strike_step=50,
        exchange_fee_pct=0.0003503,
        oi_diff_threshold=5_000_000,  # 50 Lakhs (50,00,000) shares
    ),
    "SENSEX": IndexSpec(
        symbol="SENSEX",
        under_security_id=51,
        under_segment="IDX_I",
        fno_segment="BSE_FNO",
        lot_size=20,                # 2026 BSE Sensex lot size
        strike_step=100,
        exchange_fee_pct=0.0003250,
        oi_diff_threshold=5_000_000,  # 50 Lakhs standard
    ),
}


@dataclass
class BotConfig:
    mode: str = "PAPER"                         # "PAPER" or "LIVE"
    initial_capital: float = 30000.0            # Rs. 30,000 starting virtual wallet
    monthly_profit_target: float = 10000.0      # +Rs. 10,000 target in first month
    strict_50lakh_both_indices: bool = False
    
    # --- Multi-Lot & Dynamic Sizing Settings ---
    max_lots_per_trade: int = 4                 # Scales from 1 to 4 lots as wallet & conviction grow
    max_capital_per_trade_pct: float = 0.70     # Up to 70% of wallet deployed on A+ setups
    base_risk_per_lot_rs: float = 400.0         # 1R risk per single lot (Rs. 400/lot)
    max_account_risk_pct: float = 0.035         # Total risk across all lots capped at 3.5% of equity
    max_sl_premium_pct: float = 0.10            # 10% max premium stop-loss
    
    # --- Anti-Overfitting & Regularization Settings ---
    l2_weight_decay: float = 0.015              # Shrinks weights toward prior to prevent memorization
    max_td_error_clip: float = 2.5              # Clips outlier rewards so 1 lucky trade doesn't warp weights
    min_trades_for_active: int = 8              # Requires >= 8 trades to promote a rule to ACTIVE
    min_days_for_active: int = 4                # Requires >= 4 distinct trading days for rule stability
    min_days_before_prune: int = 5
    min_trades_before_prune: int = 15
    
    # --- Intraday Execution Guardrails ---
    max_daily_loss_rs: float = -1100.0
    max_consecutive_losses_day: int = 2
    post_loss_cooldown_mins: float = 25.0
    same_mistake_cosine_limit: float = 0.82
    max_trades_per_day: int = 1                 # Strictly 1 high-conviction trade per day max
    theta_time_stop_mins: float = 15.0
    breakeven_trigger_r: float = 1.0
    chandelier_trigger_r: float = 2.2
    option_chain_poll_sec: float = 5.0
    max_stale_polls_allowed: int = 2
    state_file: str = "bot_brain_state.json"
    rules_file: str = "learned_rules.json"
    ledger_file: str = "paper_trade_ledger.json"


# ===========================================================================
# 2. INDIAN F&O STATUTORY COST & GROSS/NET PNL ENGINE
# ===========================================================================
def compute_trade_accounting(
    buy_price: float, sell_price: float, quantity: int, exchange_fee_pct: float
) -> Dict[str, float]:
    buy_turnover = buy_price * quantity
    sell_turnover = sell_price * quantity
    total_turnover = buy_turnover + sell_turnover
    gross_pnl = (sell_price - buy_price) * quantity

    brokerage = 40.0
    stt = sell_turnover * 0.001
    exch_fee = total_turnover * exchange_fee_pct
    sebi_fee = total_turnover * 0.000001
    stamp_duty = buy_turnover * 0.00003
    gst = 0.18 * (brokerage + exch_fee + sebi_fee)
    total_charges = brokerage + stt + exch_fee + sebi_fee + stamp_duty + gst

    return {
        "gross_pnl": round(gross_pnl, 2),
        "charges": round(total_charges, 2),
        "net_pnl": round(gross_pnl - total_charges, 2),
    }


# ===========================================================================
# 3. STRADDLING 3-STRIKE 50-LAKH OI & 4-QUADRANT ENGINE
# ===========================================================================
def get_straddling_3strike_brackets(spot: float, strike_step: int) -> Dict[str, Any]:
    raw_floor = int(math.floor(spot / float(strike_step)) * strike_step)
    if abs(spot - raw_floor) < 1e-4:
        upper_call_anchor = raw_floor
        lower_put_anchor = raw_floor - strike_step
    else:
        lower_put_anchor = raw_floor
        upper_call_anchor = lower_put_anchor + strike_step

    call_strikes_above = [upper_call_anchor + (i * strike_step) for i in range(3)]
    put_strikes_below = [lower_put_anchor - (i * strike_step) for i in range(3)]
    atm_strike = int(round(spot / float(strike_step)) * strike_step)

    return {
        "atm_strike": atm_strike,
        "call_strikes_above": call_strikes_above,
        "put_strikes_below": put_strikes_below,
    }


@dataclass
class StraddlingThreeStrikeAnalysis:
    index: str
    spot_price: float
    atm_strike: int
    call_strikes_above: List[int]
    put_strikes_below: List[int]
    ce_3strike_oi: int
    pe_3strike_oi: int
    diff_ce_minus_pe_oi: int
    diff_pe_minus_ce_oi: int
    ce_3strike_chg_oi: int
    pe_3strike_chg_oi: int
    diff_ce_minus_pe_chg_oi: int
    exceeds_50_lakhs: bool
    active_threshold_used: int
    ce_quadrant: str
    pe_quadrant: str
    market_controller: str
    market_dominance: str
    conviction_multiplier: float
    data_health: str


class OptionChainMicrostructureAnalyzer:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.prev_bracket_state: Dict[str, Dict[str, float]] = {}

    @staticmethod
    def classify_quadrant(price_delta: float, oi_delta: float) -> str:
        if price_delta >= 0 and oi_delta > 0:
            return "LONG_BUILDUP"
        elif price_delta < 0 and oi_delta > 0:
            return "SHORT_BUILDUP"
        elif price_delta > 0 and oi_delta <= 0:
            return "SHORT_COVERING"
        else:
            return "LONG_UNWINDING"

    def analyze_3strike_battlefield(
        self, snap: Dict[str, Any], index_spec: IndexSpec
    ) -> StraddlingThreeStrikeAnalysis:
        idx = snap["index"]
        spot = float(snap["spot"])
        ce_bracket = snap["call_bracket_above"]
        pe_bracket = snap["put_bracket_below"]

        ce_3strike_oi = sum(int(x["oi"]) for x in ce_bracket)
        pe_3strike_oi = sum(int(x["oi"]) for x in pe_bracket)
        ce_3strike_chg_oi = sum(int(x["chg_oi"]) for x in ce_bracket)
        pe_3strike_chg_oi = sum(int(x["chg_oi"]) for x in pe_bracket)

        if (ce_3strike_oi + pe_3strike_oi) < 500_000:
            ce_3strike_oi *= index_spec.lot_size
            pe_3strike_oi *= index_spec.lot_size
            ce_3strike_chg_oi *= index_spec.lot_size
            pe_3strike_chg_oi *= index_spec.lot_size

        diff_ce_minus_pe = ce_3strike_oi - pe_3strike_oi
        diff_pe_minus_ce = pe_3strike_oi - ce_3strike_oi
        diff_ce_minus_pe_chg = ce_3strike_chg_oi - pe_3strike_chg_oi

        threshold = index_spec.oi_diff_threshold
        if idx == "SENSEX" and not self.cfg.strict_50lakh_both_indices:
            combined_pool = ce_3strike_oi + pe_3strike_oi
            if combined_pool < 12_000_000:
                threshold = max(1_600_000, int(combined_pool * 0.28))

        exceeds_50_lakhs = (
            abs(diff_ce_minus_pe) >= threshold
            or abs(diff_ce_minus_pe_chg) >= int(threshold * 0.60)
        )

        ce_avg_ltp = float(np.mean([x["ltp"] for x in ce_bracket]))
        pe_avg_ltp = float(np.mean([x["ltp"] for x in pe_bracket]))

        prev = self.prev_bracket_state.get(idx)
        if prev is not None:
            ce_p_delta = ce_avg_ltp - prev["ce_avg_ltp"]
            pe_p_delta = pe_avg_ltp - prev["pe_avg_ltp"]
            ce_oi_delta = (ce_3strike_oi - prev["ce_3strike_oi"]) or ce_3strike_chg_oi
            pe_oi_delta = (pe_3strike_oi - prev["pe_3strike_oi"]) or pe_3strike_chg_oi
        else:
            ce_p_delta = spot - float(snap["vwap"])
            pe_p_delta = float(snap["vwap"]) - spot
            ce_oi_delta = ce_3strike_chg_oi
            pe_oi_delta = pe_3strike_chg_oi

        self.prev_bracket_state[idx] = {
            "ce_avg_ltp": ce_avg_ltp,
            "pe_avg_ltp": pe_avg_ltp,
            "ce_3strike_oi": ce_3strike_oi,
            "pe_3strike_oi": pe_3strike_oi,
        }

        ce_quad = self.classify_quadrant(ce_p_delta, ce_oi_delta)
        pe_quad = self.classify_quadrant(pe_p_delta, pe_oi_delta)

        if ce_quad == "SHORT_BUILDUP" and pe_quad == "SHORT_BUILDUP" and not exceeds_50_lakhs:
            controller = "SELLERS_THETA_TRAP"
            dominance = "BALANCED_CHOP"
            mult = 0.0
        elif diff_ce_minus_pe >= threshold:
            dominance = "BEARS_DOMINATING"
            if pe_quad in ("SHORT_COVERING", "LONG_BUILDUP") and ce_quad == "SHORT_BUILDUP":
                controller = "BUYERS_AND_CALL_WRITERS_ALIGNED"
                mult = 1.45 if pe_quad == "SHORT_COVERING" else 1.30
            else:
                controller = "CALL_WRITERS_DOMINATING_CEILING"
                mult = 1.15
        elif diff_pe_minus_ce >= threshold:
            dominance = "BULLS_DOMINATING"
            if ce_quad in ("SHORT_COVERING", "LONG_BUILDUP") and pe_quad == "SHORT_BUILDUP":
                controller = "BUYERS_AND_PUT_WRITERS_ALIGNED"
                mult = 1.45 if ce_quad == "SHORT_COVERING" else 1.30
            else:
                controller = "PUT_WRITERS_DOMINATING_FLOOR"
                mult = 1.15
        else:
            if ce_quad == "SHORT_COVERING" and pe_quad == "SHORT_BUILDUP":
                controller = "BUYERS_IN_CONTROL"
                dominance = "BULLS_DOMINATING"
                mult = 1.15
            elif pe_quad == "SHORT_COVERING" and ce_quad == "SHORT_BUILDUP":
                controller = "BUYERS_IN_CONTROL"
                dominance = "BEARS_DOMINATING"
                mult = 1.15
            else:
                controller = "SELLERS_THETA_TRAP"
                dominance = "BALANCED_CHOP"
                mult = 0.0

        return StraddlingThreeStrikeAnalysis(
            index=idx,
            spot_price=spot,
            atm_strike=snap["atm_strike"],
            call_strikes_above=[x["strike"] for x in ce_bracket],
            put_strikes_below=[x["strike"] for x in pe_bracket],
            ce_3strike_oi=ce_3strike_oi,
            pe_3strike_oi=pe_3strike_oi,
            diff_ce_minus_pe_oi=diff_ce_minus_pe,
            diff_pe_minus_ce_oi=diff_pe_minus_ce,
            ce_3strike_chg_oi=ce_3strike_chg_oi,
            pe_3strike_chg_oi=pe_3strike_chg_oi,
            diff_ce_minus_pe_chg_oi=diff_ce_minus_pe_chg,
            exceeds_50_lakhs=exceeds_50_lakhs,
            active_threshold_used=threshold,
            ce_quadrant=ce_quad,
            pe_quadrant=pe_quad,
            market_controller=controller,
            market_dominance=dominance,
            conviction_multiplier=mult,
            data_health=snap.get("data_health", "LIVE_CLEAN"),
        )


# ===========================================================================
# 4. FAULT-TOLERANT DHANHQ ADAPTER + STALE-DATA BRIDGE & STRIKE LOOKUP MAP
# ===========================================================================
class FaultTolerantDhanAdapter:
    def __init__(self, config: BotConfig, simulate: bool = False):
        self.cfg = config
        self.simulate = simulate
        self.dhan = None
        self.last_oc_call_ts = 0.0
        self.dynamic_rate_delay = 3.2
        self.sim_spot = {"NIFTY": 22492.0, "SENSEX": 81465.0}
        self.sim_vwap = {"NIFTY": 22492.0, "SENSEX": 81465.0}
        self.orb_bounds: Dict[str, Dict[str, float]] = {}
        self.prev_day_levels: Dict[str, Dict[str, float]] = {}
        self.lkg_snapshots: Dict[str, Dict[str, Any]] = {}
        self.last_fingerprints: Dict[str, Tuple] = {}
        self.stale_counts: Dict[str, int] = {"NIFTY": 0, "SENSEX": 0}
        self.api_error_counts: Dict[str, int] = {"NIFTY": 0, "SENSEX": 0}

        if not self.simulate:
            cid = os.environ.get("DHAN_CLIENT_ID", "").strip()
            tok = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
            if DhanContext is None or not cid or not tok:
                log.warning("DhanHQ credentials/SDK not detected. Running with Autonomous Paper Feed Bridge.")
                self.simulate = True
            else:
                try:
                    self.dhan = dhanhq(DhanContext(cid, tok))
                    log.info("Connected to DhanHQ v2 API with Stale-Data & Fault-Tolerance Guard active.")
                except Exception as exc:
                    log.error(f"Dhan SDK init failed ({exc}); engaging Paper Feed Bridge.")
                    self.simulate = True

    def _rate_limit_guard(self):
        elapsed = time.time() - self.last_oc_call_ts
        if elapsed < self.dynamic_rate_delay:
            time.sleep(self.dynamic_rate_delay - elapsed)
        self.last_oc_call_ts = time.time()

    @staticmethod
    def _safe_float(val: Any, fallback: float) -> float:
        try:
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return fallback
            return f
        except (TypeError, ValueError):
            return fallback

    def _sanitize_option_price(self, raw_price: Any, fallback_price: float) -> Tuple[float, bool]:
        p = self._safe_float(raw_price, -1.0)
        if p <= 0.05:
            return fallback_price, True
        if fallback_price > 5.0 and abs(p - fallback_price) / fallback_price > 0.45:
            clamped = fallback_price * (1.25 if p > fallback_price else 0.75)
            return round(clamped, 2), True
        return round(p, 2), False

    def calibrate_pre_open(self, index_name: str) -> Dict[str, float]:
        spec = INDEX_SPECS[index_name]
        base = self.sim_spot[index_name]
        step = spec.strike_step
        default_levels = {
            "pdh": round(base + step * 2.0, 2),
            "pdl": round(base - step * 2.0, 2),
            "pdc": round(base - step * 0.25, 2),
            "atr_1m": round(step * 0.28, 2),
            "expiry": datetime.now().strftime("%Y-%m-%d"),
        }
        if self.simulate or self.dhan is None:
            self.prev_day_levels[index_name] = default_levels
            self.orb_bounds[index_name] = {"high": base + step * 0.45, "low": base - step * 0.45}
            return default_levels

        for attempt in range(1, 4):
            try:
                self._rate_limit_guard()
                exp_resp = self.dhan.expiry_list(spec.under_security_id, spec.under_segment)
                if not isinstance(exp_resp, dict) or exp_resp.get("status") == "failure":
                    raise RuntimeError(f"expiry_list returned failure: {exp_resp}")
                exp_list = exp_resp.get("data", {}).get("data", []) or exp_resp.get("data", [])
                nearest_expiry = exp_list[0] if exp_list else default_levels["expiry"]
                to_dt = datetime.now().strftime("%Y-%m-%d")
                from_dt = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
                hist = self.dhan.intraday_minute_data(
                    security_id=str(spec.under_security_id),
                    exchange_segment=spec.under_segment,
                    instrument_type="INDEX",
                    from_date=from_dt,
                    to_date=to_dt,
                )
                candles = hist.get("data", {}) if isinstance(hist, dict) else {}
                highs = candles.get("high") or [base + step * 2.0]
                lows = candles.get("low") or [base - step * 2.0]
                closes = candles.get("close") or [base]
                tr_list = [abs(h - l) for h, l in zip(highs[-60:], lows[-60:]) if h > 0 and l > 0]
                levels = {
                    "pdh": float(max(highs[-375:])),
                    "pdl": float(min(lows[-375:])),
                    "pdc": float(closes[-1]),
                    "atr_1m": float(np.mean(tr_list)) if tr_list else default_levels["atr_1m"],
                    "expiry": str(nearest_expiry),
                }
                self.prev_day_levels[index_name] = levels
                self.orb_bounds.setdefault(index_name, {"high": closes[-1] + step * 0.4, "low": closes[-1] - step * 0.4})
                return levels
            except Exception as exc:
                log.warning(f"[CALIBRATION RETRY {attempt}/3] {index_name}: {exc}")
                time.sleep(1.5 * attempt)
        self.prev_day_levels[index_name] = default_levels
        self.orb_bounds.setdefault(index_name, {"high": base + step * 0.45, "low": base - step * 0.45})
        return default_levels

    def fetch_market_snapshot(self, index_name: str) -> Dict[str, Any]:
        spec = INDEX_SPECS[index_name]
        levels = self.prev_day_levels.get(index_name) or self.calibrate_pre_open(index_name)
        if self.simulate or self.dhan is None:
            snap = self._generate_bridged_or_synthetic_snapshot(index_name, spec, levels, reason="SIM_BRIDGE")
            self.lkg_snapshots[index_name] = snap
            return snap

        try:
            self._rate_limit_guard()
            oc_resp = self.dhan.option_chain(spec.under_security_id, spec.under_segment, levels["expiry"])
            if not isinstance(oc_resp, dict) or oc_resp.get("status") == "failure":
                remarks = str(oc_resp.get("remarks", "")) if isinstance(oc_resp, dict) else "Invalid response"
                if "429" in remarks or "rate" in remarks.lower():
                    self.dynamic_rate_delay = min(6.0, self.dynamic_rate_delay + 1.0)
                raise ValueError(f"Dhan option_chain failure: {remarks}")
            data = oc_resp.get("data", {})
            if not isinstance(data, dict) or not data.get("oc"):
                raise ValueError("Empty 'oc' dictionary in Dhan response")
            lkg = self.lkg_snapshots.get(index_name)
            fallback_spot = lkg["spot"] if lkg else self.sim_spot[index_name]
            spot = self._safe_float(data.get("last_price"), fallback_spot)
            if spot <= 100.0:
                spot = fallback_spot
            self.sim_spot[index_name] = spot
            self.sim_vwap[index_name] = round(0.97 * self.sim_vwap[index_name] + 0.03 * spot, 2)
            oc_dict = data.get("oc", {})
            brackets = get_straddling_3strike_brackets(spot, spec.strike_step)
            atm_strike = brackets["atm_strike"]
            repaired_any = False

            def extract_leg(strike_val: int, side: str, default_ltp: float) -> Dict[str, Any]:
                nonlocal repaired_any
                k = f"{strike_val:.6f}" if f"{strike_val:.6f}" in oc_dict else str(strike_val)
                node = oc_dict.get(k, {}).get(side, {}) if isinstance(oc_dict.get(k), dict) else {}
                ltp_clean, was_repaired = self._sanitize_option_price(node.get("last_price"), default_ltp)
                if was_repaired:
                    repaired_any = True
                bid = self._safe_float(node.get("top_bid_price"), round(ltp_clean - 0.35, 2))
                ask = self._safe_float(node.get("top_ask_price"), round(ltp_clean + 0.35, 2))
                if bid <= 0.0 or bid > ltp_clean * 1.1:
                    bid = round(max(0.05, ltp_clean - 0.35), 2)
                if ask <= 0.0 or ask < bid:
                    ask = round(ltp_clean + 0.35, 2)
                oi_val = max(0.0, self._safe_float(node.get("oi"), 1_500_000.0))
                prev_oi = max(0.0, self._safe_float(node.get("previous_oi"), oi_val))
                greeks = node.get("greeks", {}) if isinstance(node.get("greeks"), dict) else {}
                return {
                    "strike": strike_val,
                    "security_id": str(node.get("security_id", f"{side.upper()}_{strike_val}")),
                    "ltp": ltp_clean,
                    "bid": bid,
                    "ask": ask,
                    "oi": oi_val,
                    "chg_oi": oi_val - prev_oi,
                    "iv": max(5.0, self._safe_float(node.get("implied_volatility"), 14.0)),
                    "delta": self._safe_float(greeks.get("delta"), 0.50 if side == "ce" else -0.50),
                    "gamma": max(0.0005, self._safe_float(greeks.get("gamma"), 0.0028)),
                    "theta": self._safe_float(greeks.get("theta"), -11.5),
                }

            base_default = 140.0 if index_name == "NIFTY" else 410.0
            lkg_ce_ltp = lkg["ce"]["ltp"] if lkg else base_default
            lkg_pe_ltp = lkg["pe"]["ltp"] if lkg else base_default
            atm_ce = extract_leg(atm_strike, "ce", lkg_ce_ltp)
            atm_pe = extract_leg(atm_strike, "pe", lkg_pe_ltp)

            call_bracket_above = [
                extract_leg(st, "ce", max(15.0, atm_ce["ltp"] - idx_i * 18.0))
                for idx_i, st in enumerate(brackets["call_strikes_above"])
            ]
            put_bracket_below = [
                extract_leg(st, "pe", max(15.0, atm_pe["ltp"] - idx_i * 18.0))
                for idx_i, st in enumerate(brackets["put_strikes_below"])
            ]

            strike_ltp_map: Dict[str, float] = {}
            for offset in range(-10, 11):
                st_val = atm_strike + (offset * spec.strike_step)
                k = f"{st_val:.6f}" if f"{st_val:.6f}" in oc_dict else str(st_val)
                node = oc_dict.get(k, {}) if isinstance(oc_dict.get(k), dict) else {}
                for side in ("ce", "pe"):
                    raw_p = node.get(side, {}).get("last_price") if isinstance(node.get(side), dict) else None
                    p_val = self._safe_float(raw_p, -1.0)
                    if p_val > 0.05:
                        strike_ltp_map[f"{st_val}_{side.upper()}"] = round(p_val, 2)

            tot_ce_oi = sum(int(x["oi"]) for x in call_bracket_above)
            tot_pe_oi = sum(int(x["oi"]) for x in put_bracket_below)
            fingerprint = (round(spot, 2), atm_ce["ltp"], atm_pe["ltp"], tot_ce_oi, tot_pe_oi)

            if self.last_fingerprints.get(index_name) == fingerprint:
                self.stale_counts[index_name] += 1
            else:
                self.stale_counts[index_name] = 0
                self.last_fingerprints[index_name] = fingerprint

            self.api_error_counts[index_name] = 0
            self.dynamic_rate_delay = max(3.2, self.dynamic_rate_delay - 0.1)

            if self.stale_counts[index_name] >= self.cfg.max_stale_polls_allowed and self.cfg.mode == "PAPER":
                bridged = self._apply_delta_gamma_theta_bridge(
                    index_name, spot, atm_ce, atm_pe, call_bracket_above, put_bracket_below, levels
                )
                self.lkg_snapshots[index_name] = bridged
                return bridged

            health_tag = "REPAIRED_PARTIAL" if repaired_any else "LIVE_CLEAN"
            snap = {
                "index": index_name,
                "spot": spot,
                "vwap": self.sim_vwap[index_name],
                "atm_strike": atm_strike,
                "pcr": round(tot_pe_oi / max(tot_ce_oi, 1), 3),
                "call_bracket_above": call_bracket_above,
                "put_bracket_below": put_bracket_below,
                "ce": atm_ce,
                "pe": atm_pe,
                "strike_ltp_map": strike_ltp_map,
                "data_health": health_tag,
            }
            self.lkg_snapshots[index_name] = snap
            return snap
        except Exception as exc:
            self.api_error_counts[index_name] += 1
            snap = self._generate_bridged_or_synthetic_snapshot(index_name, spec, levels, reason="API_ERROR_BRIDGE")
            self.lkg_snapshots[index_name] = snap
            return snap

    def _apply_delta_gamma_theta_bridge(
        self,
        index_name: str,
        spot: float,
        atm_ce: Dict[str, Any],
        atm_pe: Dict[str, Any],
        call_bracket: List[Dict[str, Any]],
        put_bracket: List[Dict[str, Any]],
        levels: Dict[str, float],
    ) -> Dict[str, Any]:
        atr = levels.get("atr_1m", 15.0)
        d_spot = random.gauss(0.0, 0.25 * atr)
        new_spot = round(spot + d_spot, 2)
        dt_days = (self.cfg.option_chain_poll_sec / 60.0) / 375.0

        def bridge_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
            delta = leg["delta"]
            gamma = leg["gamma"]
            theta = leg["theta"]
            d_prem = (delta * d_spot) + (0.5 * gamma * (d_spot ** 2)) + (theta * dt_days)
            new_ltp = round(max(2.0, leg["ltp"] + d_prem), 2)
            updated = dict(leg)
            updated["ltp"] = new_ltp
            updated["bid"] = round(max(1.5, new_ltp - 0.35), 2)
            updated["ask"] = round(new_ltp + 0.35, 2)
            return updated

        bridged_ce = bridge_leg(atm_ce)
        bridged_pe = bridge_leg(atm_pe)
        bridged_c_bracket = [bridge_leg(x) for x in call_bracket]
        bridged_p_bracket = [bridge_leg(x) for x in put_bracket]
        tot_ce = sum(int(x["oi"]) for x in bridged_c_bracket) or 1
        tot_pe = sum(int(x["oi"]) for x in bridged_p_bracket) or 1

        return {
            "index": index_name,
            "spot": new_spot,
            "vwap": self.sim_vwap[index_name],
            "atm_strike": int(atm_ce["strike"]),
            "pcr": round(tot_pe / tot_ce, 3),
            "call_bracket_above": bridged_c_bracket,
            "put_bracket_below": bridged_p_bracket,
            "ce": bridged_ce,
            "pe": bridged_pe,
            "strike_ltp_map": {f"{bridged_ce['strike']}_CE": bridged_ce["ltp"], f"{bridged_pe['strike']}_PE": bridged_pe["ltp"]},
            "data_health": "BRIDGED_STALE_FEED",
        }

    def _generate_bridged_or_synthetic_snapshot(
        self, index_name: str, spec: IndexSpec, levels: Dict[str, float], reason: str
    ) -> Dict[str, Any]:
        lkg = self.lkg_snapshots.get(index_name)
        if lkg is not None and reason == "API_ERROR_BRIDGE":
            return self._apply_delta_gamma_theta_bridge(
                index_name, lkg["spot"], lkg["ce"], lkg["pe"], lkg["call_bracket_above"], lkg["put_bracket_below"], levels
            )

        if not hasattr(self, "sim_regime"):
            self.sim_regime = {"NIFTY": 0, "SENSEX": 0}
            self.sim_regime_steps = {"NIFTY": 0, "SENSEX": 0}
            self.day_open_spot = {"NIFTY": self.sim_spot["NIFTY"], "SENSEX": self.sim_spot["SENSEX"]}

        atr = levels["atr_1m"]
        if self.sim_regime_steps[index_name] <= 0:
            r_pick = random.random()
            if r_pick < 0.19:
                self.sim_regime[index_name] = 1
                self.sim_regime_steps[index_name] = random.randint(5, 11)
            elif r_pick < 0.38:
                self.sim_regime[index_name] = -1
                self.sim_regime_steps[index_name] = random.randint(5, 11)
            else:
                self.sim_regime[index_name] = 0
                self.sim_regime_steps[index_name] = random.randint(6, 14)

        self.sim_regime_steps[index_name] -= 1
        reg = self.sim_regime[index_name]

        if reg == 1:
            drift = random.gauss(0.28 * atr, 0.32 * atr)
        elif reg == -1:
            drift = random.gauss(-0.28 * atr, 0.32 * atr)
        else:
            vwap_pull = -0.08 * (self.sim_spot[index_name] - self.sim_vwap[index_name])
            drift = vwap_pull + random.gauss(0, 0.30 * atr)

        raw_spot = self.sim_spot[index_name] + drift
        day_anchor = self.day_open_spot.get(index_name, raw_spot)
        max_day_move = day_anchor * 0.016
        spot = round(max(day_anchor - max_day_move, min(day_anchor + max_day_move, raw_spot)), 2)
        self.sim_spot[index_name] = spot
        self.sim_vwap[index_name] = round(0.96 * self.sim_vwap[index_name] + 0.04 * spot, 2)

        brackets = get_straddling_3strike_brackets(spot, spec.strike_step)
        atm_strike = brackets["atm_strike"]

        base_prem = 135.0 if index_name == "NIFTY" else 395.0
        rel_move = (spot - self.sim_vwap[index_name]) / max(atr, 1.0)

        ce_ltp = round(max(18.0, base_prem + max(0.0, spot - atm_strike) * 0.52 + rel_move * 2.2), 2)
        pe_ltp = round(max(18.0, base_prem + max(0.0, atm_strike - spot) * 0.52 - rel_move * 2.2), 2)

        base_strike_oi = 4_500_000 if index_name == "NIFTY" else 2_200_000
        oi_skew = int(np.tanh(rel_move * 0.55) * (2_900_000 if index_name == "NIFTY" else 1_450_000))

        call_bracket_above = []
        for i, st in enumerate(brackets["call_strikes_above"]):
            c_oi = max(500_000, base_strike_oi - oi_skew + random.randint(-90_000, 90_000))
            c_chg = int(-oi_skew * 0.38) if reg == 1 else int(abs(oi_skew) * 0.32)
            leg_ltp = round(max(10.0, ce_ltp - i * (15.0 if index_name == "NIFTY" else 40.0)), 2)
            call_bracket_above.append({
                "strike": st,
                "security_id": f"SIM_{index_name}_{st}_CE",
                "ltp": leg_ltp,
                "bid": round(leg_ltp - 0.30, 2),
                "ask": round(leg_ltp + 0.30, 2),
                "oi": c_oi,
                "chg_oi": c_chg,
                "iv": 13.8,
                "delta": round(max(0.25, 0.52 - i * 0.10), 2),
                "gamma": 0.0028,
                "theta": -11.5,
            })

        put_bracket_below = []
        for i, st in enumerate(brackets["put_strikes_below"]):
            p_oi = max(500_000, base_strike_oi + oi_skew + random.randint(-90_000, 90_000))
            p_chg = int(oi_skew * 0.38) if reg == 1 else int(-abs(oi_skew) * 0.34)
            leg_ltp = round(max(10.0, pe_ltp - i * (15.0 if index_name == "NIFTY" else 40.0)), 2)
            put_bracket_below.append({
                "strike": st,
                "security_id": f"SIM_{index_name}_{st}_PE",
                "ltp": leg_ltp,
                "bid": round(leg_ltp - 0.30, 2),
                "ask": round(leg_ltp + 0.30, 2),
                "oi": p_oi,
                "chg_oi": p_chg,
                "iv": 14.1,
                "delta": round(min(-0.25, -0.50 + i * 0.10), 2),
                "gamma": 0.0028,
                "theta": -11.5,
            })

        strike_ltp_map = {f"{x['strike']}_CE": x["ltp"] for x in call_bracket_above}
        strike_ltp_map.update({f"{x['strike']}_PE": x["ltp"] for x in put_bracket_below})

        tot_ce = sum(int(x["oi"]) for x in call_bracket_above)
        tot_pe = sum(int(x["oi"]) for x in put_bracket_below)

        return {
            "index": index_name,
            "spot": spot,
            "vwap": self.sim_vwap[index_name],
            "atm_strike": atm_strike,
            "pcr": round(tot_pe / max(tot_ce, 1), 3),
            "call_bracket_above": call_bracket_above,
            "put_bracket_below": put_bracket_below,
            "ce": call_bracket_above[0],
            "pe": put_bracket_below[0],
            "strike_ltp_map": strike_ltp_map,
            "data_health": reason,
        }


# ===========================================================================
# 5. INTEGRATED MARKET STUDY ENGINE
# ===========================================================================
class MarketStudyEngine:
    def __init__(self, cfg: BotConfig):
        self.spot_history: Dict[str, List[float]] = {"NIFTY": [], "SENSEX": []}
        self.oi_analyzer = OptionChainMicrostructureAnalyzer(cfg)

    def extract_state_and_tags(
        self, snap: Dict[str, Any], levels: Dict[str, float], orb: Dict[str, float]
    ) -> Tuple[np.ndarray, Dict[str, str], StraddlingThreeStrikeAnalysis]:
        idx = snap["index"]
        spec = INDEX_SPECS[idx]
        spot, vwap = snap["spot"], snap["vwap"]
        atr = max(levels.get("atr_1m", 15.0), 1.0)
        hist = self.spot_history[idx]
        hist.append(spot)
        if len(hist) > 30:
            hist.pop(0)

        oi_report = self.oi_analyzer.analyze_3strike_battlefield(snap, spec)
        mom_5 = (spot - hist[-5]) / atr if len(hist) >= 5 else 0.0
        vwap_z = (spot - vwap) / (atr * 1.5)
        orb_h, orb_l = orb.get("high", spot + atr), orb.get("low", spot - atr)
        orb_score = (
            min(2.0, (spot - orb_h) / atr)
            if spot > orb_h
            else (max(-2.0, (spot - orb_l) / atr) if spot < orb_l else 0.0)
        )

        oi_diff_50l_ratio = np.tanh(oi_report.diff_pe_minus_ce_oi / max(oi_report.active_threshold_used, 1))
        chg_oi_50l_ratio = np.tanh(-oi_report.diff_ce_minus_pe_chg_oi / max(oi_report.active_threshold_used * 0.5, 1))

        quad_score = 0.0
        if oi_report.ce_quadrant == "SHORT_COVERING":
            quad_score += 1.5
        elif oi_report.ce_quadrant == "LONG_BUILDUP":
            quad_score += 0.9
        elif oi_report.ce_quadrant == "SHORT_BUILDUP":
            quad_score -= 1.0

        if oi_report.pe_quadrant == "SHORT_COVERING":
            quad_score -= 1.5
        elif oi_report.pe_quadrant == "LONG_BUILDUP":
            quad_score -= 0.9
        elif oi_report.pe_quadrant == "SHORT_BUILDUP":
            quad_score += 1.0

        ce_gamma = snap["ce"]["gamma"]
        ce_theta_min = abs(snap["ce"]["theta"]) / 375.0
        expected_gamma_gain = 0.5 * ce_gamma * ((atr * 2.5) ** 2)
        gamma_edge_norm = np.tanh(((expected_gamma_gain / max(ce_theta_min, 0.01)) - 1.0) * 0.8)

        iv_skew = np.tanh((snap["ce"]["iv"] - snap["pe"]["iv"]) * 0.3)
        spread_pct = (snap["ce"]["ask"] - snap["ce"]["bid"]) / max(snap["ce"]["ltp"], 1.0)
        liquidity_score = 1.0 if spread_pct < 0.009 else -1.0
        pdh_pdl_signal = 1.0 if spot > levels["pdh"] else (-1.0 if spot < levels["pdl"] else 0.0)

        state_vec = np.clip(
            np.array(
                [
                    mom_5,
                    vwap_z,
                    orb_score,
                    oi_diff_50l_ratio,
                    chg_oi_50l_ratio,
                    quad_score,
                    gamma_edge_norm,
                    iv_skew,
                    liquidity_score,
                    pdh_pdl_signal,
                ],
                dtype=np.float64,
            ),
            -3.0,
            3.0,
        )

        tags = {
            "oi_50l_wall": (
                "CALL_SIDE_GT_50L"
                if (oi_report.exceeds_50_lakhs and oi_report.diff_ce_minus_pe_oi > 0)
                else (
                    "PUT_SIDE_GT_50L"
                    if (oi_report.exceeds_50_lakhs and oi_report.diff_pe_minus_ce_oi > 0)
                    else "SUB_50L_DIFF"
                )
            ),
            "ce_quad": oi_report.ce_quadrant,
            "pe_quad": oi_report.pe_quadrant,
            "controller": oi_report.market_controller,
            "dominance": oi_report.market_dominance,
        }
        return state_vec, tags, oi_report


# ===========================================================================
# 6. MULTI-DAY BAYESIAN BRAIN WITH L2 WEIGHT DECAY & STRICT RULE PROMOTION
# ===========================================================================
@dataclass
class DiscoveredRule:
    rule_id: str
    condition_signature: str
    direction: str
    trades_count: int = 0
    wins_count: int = 0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    expectancy_r: float = 0.0
    unique_days_traded: List[str] = field(default_factory=list)
    losses_today: int = 0
    status: str = "CANDIDATE"


class SelfLearningBrain:
    def __init__(self, cfg: BotConfig, state_dim: int = 10):
        self.cfg = cfg
        self.state_dim = state_dim
        self.n_actions = 3
        self.lr = 0.032
        self.gamma = 0.90
        self.epsilon = 0.12
        self.base_q_threshold = 0.55
        self.replay_buffer: List[Dict[str, Any]] = []
        self.rules: Dict[str, DiscoveredRule] = {}
        self.consecutive_losses: int = 0
        self.last_loss_timestamp: Optional[datetime] = None
        self.today_mistake_vectors: List[Dict[str, Any]] = []

        self._seed_priors()
        self.load_brain()

    def _seed_priors(self):
        self.prior_weights = np.array([
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.35, 0.0, -0.40, 0.0],
            [0.40, 0.30, 0.35, 0.45, 0.40, 0.50, 0.35, -0.10, 0.25, 0.30],
            [-0.40, -0.30, -0.35, -0.45, -0.40, -0.50, 0.35, 0.10, 0.25, -0.30],
        ])
        self.weights = self.prior_weights.copy()
        self.bias = np.zeros(self.n_actions)

    def start_new_trading_day(self, day_str: str):
        self.today_mistake_vectors.clear()
        self.consecutive_losses = 0
        self.last_loss_timestamp = None
        for rule in self.rules.values():
            rule.losses_today = 0
            if rule.status == "DAY_MUTED":
                rule.status = "ACTIVE" if rule.expectancy_r > 0.25 else "CANDIDATE"

    @staticmethod
    def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 < 1e-8 or n2 < 1e-8:
            return 0.0
        return float(np.dot(v1, v2) / (n1 * n2))

    def is_same_mistake(self, state: np.ndarray, action: int, index_name: str, strike: int) -> bool:
        for mistake in self.today_mistake_vectors:
            if mistake["action"] != action:
                continue
            if mistake["index"] == index_name and mistake["strike"] == strike:
                return True
            if self._cosine_similarity(state, np.array(mistake["state"])) >= self.cfg.same_mistake_cosine_limit:
                return True
        return False

    def build_rule_signature(self, tags: Dict[str, str], direction: str) -> str:
        return (
            f"IF wall={tags['oi_50l_wall']} & ce={tags['ce_quad']} & pe={tags['pe_quad']} "
            f"& ctrl={tags['controller']} & dom={tags['dominance']} -> {direction}"
        )

    def decide_action(
        self,
        state: np.ndarray,
        tags: Dict[str, str],
        oi_report: StraddlingThreeStrikeAnalysis,
        sim_clock_mins: Optional[float] = None,
    ) -> Tuple[int, float, Optional[str]]:
        if oi_report.market_controller == "SELLERS_THETA_TRAP":
            return 0, 0.0, None

        has_short_covering = "SHORT_COVERING" in (oi_report.ce_quadrant, oi_report.pe_quadrant)
        if not (oi_report.exceeds_50_lakhs or has_short_covering):
            return 0, 0.0, None

        if self.last_loss_timestamp is not None and sim_clock_mins is None:
            if (datetime.now() - self.last_loss_timestamp).total_seconds() / 60.0 < self.cfg.post_loss_cooldown_mins:
                return 0, 0.0, None

        q_values = (np.dot(self.weights, state) + self.bias) * oi_report.conviction_multiplier
        candidate_action = int(np.argmax(q_values))

        if candidate_action == 1 and oi_report.market_dominance != "BULLS_DOMINATING":
            return 0, float(q_values[0]), None
        if candidate_action == 2 and oi_report.market_dominance != "BEARS_DOMINATING":
            return 0, float(q_values[0]), None
        if candidate_action == 0:
            return 0, float(q_values[0]), None

        if candidate_action == 1 and oi_report.ce_quadrant in ("LONG_UNWINDING", "SHORT_BUILDUP"):
            return 0, float(q_values[0]), None
        if candidate_action == 2 and oi_report.pe_quadrant in ("LONG_UNWINDING", "SHORT_BUILDUP"):
            return 0, float(q_values[0]), None

        if self.is_same_mistake(state, candidate_action, oi_report.index, oi_report.atm_strike):
            return 0, float(q_values[0]), None

        direction_str = "BUY_CE" if candidate_action == 1 else "BUY_PE"
        sig = self.build_rule_signature(tags, direction_str)
        existing_rule = self.rules.get(sig)

        if existing_rule and existing_rule.status in ("PRUNED", "DAY_MUTED"):
            return 0, float(q_values[0]), None

        required_q = self.base_q_threshold * (1.65 if self.consecutive_losses >= 1 else 1.0)
        if self.consecutive_losses >= 1 and not (oi_report.exceeds_50_lakhs and has_short_covering):
            if not (existing_rule and existing_rule.status == "ACTIVE"):
                return 0, float(q_values[0]), None

        effective_q = float(q_values[candidate_action]) + (
            0.25 if (existing_rule and existing_rule.status == "ACTIVE") else 0.0
        )
        if effective_q < required_q:
            return 0, float(q_values[0]), None

        if sig not in self.rules:
            rule_id = f"RULE_{len(self.rules) + 1:03d}"
            self.rules[sig] = DiscoveredRule(rule_id=rule_id, condition_signature=sig, direction=direction_str)
            log.info(f"[NEW RULE CREATED] {rule_id}: {sig}")

        return candidate_action, effective_q, sig

    def learn_from_closed_trade(
        self,
        entry_state: np.ndarray,
        action: int,
        r_multiple: float,
        holding_mins: float,
        rule_sig: Optional[str],
        next_state: np.ndarray,
        index_name: str,
        strike: int,
        day_str: str,
    ):
        if r_multiple < 0:
            self.consecutive_losses += 1
            self.last_loss_timestamp = datetime.now()
            self.today_mistake_vectors.append(
                {"state": entry_state.tolist(), "action": int(action), "index": index_name, "strike": int(strike)}
            )
        else:
            self.consecutive_losses = 0
            self.last_loss_timestamp = None

        capped_r = max(-2.0, min(4.0, r_multiple))
        if capped_r >= 2.0:
            reward = min(5.0, 1.35 * (capped_r ** 1.35))
        elif capped_r >= 0.0:
            reward = capped_r * 0.50
        else:
            reward = capped_r - (0.35 * self.consecutive_losses)

        q_pred = (np.dot(self.weights, entry_state) + self.bias)[action]
        q_next = float(np.max(np.dot(self.weights, next_state) + self.bias))

        raw_td_error = (reward + self.gamma * q_next) - q_pred
        td_error = float(np.clip(raw_td_error, -self.cfg.max_td_error_clip, self.cfg.max_td_error_clip))

        l2_pull = self.cfg.l2_weight_decay * (self.weights[action] - self.prior_weights[action])
        self.weights[action] += (self.lr * td_error * entry_state) - l2_pull
        self.weights[action] = np.clip(self.weights[action], -1.25, 1.25)
        self.bias[action] = float(np.clip(self.bias[action] + self.lr * td_error * 0.5, -0.80, 0.80))

        self.replay_buffer.append(
            {"day": day_str, "state": entry_state.tolist(), "action": int(action), "reward": round(float(reward), 4)}
        )
        if len(self.replay_buffer) > 1500:
            self.replay_buffer.pop(0)

        if rule_sig and rule_sig in self.rules:
            rule = self.rules[rule_sig]
            rule.trades_count += 1
            if day_str not in rule.unique_days_traded:
                rule.unique_days_traded.append(day_str)

            if r_multiple > 0:
                pw = rule.wins_count
                rule.wins_count += 1
                rule.avg_win_r = round(((rule.avg_win_r * pw) + min(4.0, r_multiple)) / rule.wins_count, 2)
            else:
                pl = (rule.trades_count - 1) - rule.wins_count
                rule.avg_loss_r = round(((rule.avg_loss_r * pl) + abs(r_multiple)) / max(1, pl + 1), 2)
                rule.losses_today += 1
                rule.status = "DAY_MUTED"

            n = rule.trades_count
            raw_wr = rule.wins_count / n
            raw_exp = (raw_wr * rule.avg_win_r) - ((1.0 - raw_wr) * rule.avg_loss_r)
            confidence_factor = min(1.0, n / 10.0)
            rule.expectancy_r = round(raw_exp * confidence_factor, 2)

            payoff = rule.avg_win_r / max(rule.avg_loss_r, 0.30)
            days_n = len(rule.unique_days_traded)

            if (
                n >= self.cfg.min_trades_for_active
                and days_n >= self.cfg.min_days_for_active
                and payoff >= 1.8
                and rule.expectancy_r >= 0.25
            ):
                if rule.status != "DAY_MUTED":
                    rule.status = "ACTIVE"
            elif (
                days_n >= self.cfg.min_days_before_prune
                and n >= self.cfg.min_trades_before_prune
                and (payoff < 1.30 or rule.expectancy_r < -0.15)
            ):
                rule.status = "PRUNED"

        self.save_brain()

    def post_market_experience_replay(self, batch_size: int = 64):
        if len(self.replay_buffer) >= 8:
            for exp in random.sample(self.replay_buffer, min(batch_size, len(self.replay_buffer))):
                s = np.array(exp["state"]) * np.random.uniform(0.88, 1.12, size=self.state_dim)
                a, r = exp["action"], exp["reward"]
                raw_err = r - (np.dot(self.weights, s) + self.bias)[a]
                err = float(np.clip(raw_err, -self.cfg.max_td_error_clip, self.cfg.max_td_error_clip))
                l2_pull = self.cfg.l2_weight_decay * (self.weights[a] - self.prior_weights[a])
                self.weights[a] += (self.lr * 0.25 * err * s) - l2_pull
                self.weights[a] = np.clip(self.weights[a], -1.25, 1.25)
        self.epsilon = max(0.05, round(self.epsilon * 0.96, 4))
        self.save_brain()

    def save_brain(self):
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "weights": self.weights.tolist(),
                    "bias": self.bias.tolist(),
                    "epsilon": self.epsilon,
                    "replay_buffer": self.replay_buffer[-500:],
                },
                f,
                indent=2,
            )
        with open(self.cfg.rules_file, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self.rules.items()}, f, indent=2)

    def load_brain(self):
        if os.path.exists(self.cfg.state_file):
            try:
                with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    w = np.array(d["weights"])
                    if w.shape == (self.n_actions, self.state_dim):
                        self.weights = w
                        self.bias = np.array(d["bias"])
                        self.epsilon = d.get("epsilon", 0.12)
                        self.replay_buffer = d.get("replay_buffer", [])
            except Exception as exc:
                log.warning(f"Could not load brain state: {exc}")

        if os.path.exists(self.cfg.rules_file):
            try:
                with open(self.cfg.rules_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    self.rules = {k: DiscoveredRule(**v) for k, v in raw.items()}
            except Exception as exc:
                log.warning(f"Could not load rules file: {exc}")


# ===========================================================================
# 7. ASYMMETRIC EXECUTION BROKER (WITH MULTI-LOT DYNAMIC SIZING & HELD STRIKE TRACKER)
# ===========================================================================
@dataclass
class ActiveOptionTrade:
    index_name: str
    security_id: str
    option_type: str
    strike: int
    quantity: int
    entry_price: float
    initial_sl: float
    current_sl: float
    highest_ltp: float
    risk_per_unit: float
    entry_time: datetime
    entry_state: List[float]
    action_code: int
    rule_signature: Optional[str]
    entry_spot: float = 0.0


class AsymmetricExecutionBroker:
    def __init__(self, cfg: BotConfig, adapter: FaultTolerantDhanAdapter):
        self.cfg = cfg
        self.adapter = adapter
        self.virtual_balance_gross = cfg.initial_capital
        self.virtual_balance_net = cfg.initial_capital
        self.daily_net_pnl = 0.0
        self.trades_today = 0
        self.active_trade: Optional[ActiveOptionTrade] = None
        self.trade_history: List[Dict[str, Any]] = []
        self.load_ledger()

    def load_ledger(self):
        if os.path.exists(self.cfg.ledger_file):
            try:
                with open(self.cfg.ledger_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.virtual_balance_gross = data.get("virtual_balance_gross", self.cfg.initial_capital)
                    self.virtual_balance_net = data.get("virtual_balance_net", self.cfg.initial_capital)
                    self.trade_history = data.get("trades", [])
            except Exception as exc:
                log.warning(f"Could not load existing ledger: {exc}")

    def save_ledger(self):
        wins = [t["gross_pnl"] for t in self.trade_history if t["gross_pnl"] > 0]
        losses = [abs(t["gross_pnl"]) for t in self.trade_history if t["gross_pnl"] <= 0]
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0

        summary = {
            "mode": self.cfg.mode,
            "initial_capital": self.cfg.initial_capital,
            "virtual_balance_gross": round(self.virtual_balance_gross, 2),
            "virtual_balance_net": round(self.virtual_balance_net, 2),
            "profit_without_charges": round(self.virtual_balance_gross - self.cfg.initial_capital, 2),
            "net_profit_after_charges": round(self.virtual_balance_net - self.cfg.initial_capital, 2),
            "total_trades": len(self.trade_history),
            "avg_winning_value_rs": round(avg_win, 2),
            "avg_losing_value_rs": round(avg_loss, 2),
            "win_to_loss_ratio": round(avg_win / max(avg_loss, 1.0), 2),
            "trades": self.trade_history,
        }
        with open(self.cfg.ledger_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    def can_enter(self, consecutive_losses: int) -> bool:
        if self.active_trade is not None:
            return False
        if self.daily_net_pnl <= self.cfg.max_daily_loss_rs:
            return False
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False
        if consecutive_losses >= self.cfg.max_consecutive_losses_day:
            return False
        return True

    def compute_dynamic_lots(
        self,
        spec: IndexSpec,
        fill_price: float,
        risk_per_unit: float,
        effective_q: float,
        oi_report: StraddlingThreeStrikeAnalysis,
        rule_obj: Optional[DiscoveredRule],
        sniper_mode: bool,
    ) -> int:
        if sniper_mode:
            return 1

        cost_per_lot = fill_price * spec.lot_size
        risk_per_lot = risk_per_unit * spec.lot_size

        max_lots_by_capital = int((self.virtual_balance_net * self.cfg.max_capital_per_trade_pct) // max(cost_per_lot, 1.0))
        max_lots_by_risk = int((self.virtual_balance_net * self.cfg.max_account_risk_pct) // max(risk_per_lot, 1.0))
        affordable_lots = max(0, min(max_lots_by_capital, max_lots_by_risk, self.cfg.max_lots_per_trade))

        if affordable_lots <= 1:
            return affordable_lots

        has_short_cov = "SHORT_COVERING" in (oi_report.ce_quadrant, oi_report.pe_quadrant)
        is_active_rule = rule_obj is not None and rule_obj.status == "ACTIVE"

        if is_active_rule and oi_report.exceeds_50_lakhs and has_short_cov and effective_q >= 0.85:
            desired_lots = 3
        elif (is_active_rule or (oi_report.exceeds_50_lakhs and has_short_cov)) and effective_q >= 0.72:
            desired_lots = 2
        else:
            desired_lots = 1

        return min(desired_lots, affordable_lots)

    def open_option_position(
        self,
        snap: Dict[str, Any],
        oi_report: StraddlingThreeStrikeAnalysis,
        action: int,
        state: np.ndarray,
        rule_sig: Optional[str],
        effective_q: float = 0.60,
        rule_obj: Optional[DiscoveredRule] = None,
        sniper_mode: bool = False,
    ) -> bool:
        idx_name = snap["index"]
        spec = INDEX_SPECS[idx_name]
        opt_type = "CE" if action == 1 else "PE"
        leg = snap["ce"] if opt_type == "CE" else snap["pe"]

        raw_ask = float(leg["ask"])
        if raw_ask <= 1.0:
            return False

        fill_price = round(raw_ask * 1.0008, 2) if self.cfg.mode == "PAPER" else raw_ask

        allowed_lot_risk = self.cfg.base_risk_per_lot_rs * (0.75 if sniper_mode else 1.0)
        sl_pts_rupee = allowed_lot_risk / spec.lot_size
        sl_pts_pct = fill_price * self.cfg.max_sl_premium_pct
        risk_per_unit = round(max(2.0, min(sl_pts_rupee, sl_pts_pct)), 2)
        initial_sl = round(fill_price - risk_per_unit, 2)

        num_lots = self.compute_dynamic_lots(
            spec, fill_price, risk_per_unit, effective_q, oi_report, rule_obj, sniper_mode
        )
        if num_lots < 1:
            return False

        qty = spec.lot_size * num_lots

        if self.cfg.mode == "LIVE" and self.adapter.dhan is not None:
            try:
                resp = self.adapter.dhan.place_order(
                    security_id=leg["security_id"],
                    exchange_segment=spec.fno_segment,
                    transaction_type=self.adapter.dhan.BUY,
                    quantity=qty,
                    order_type=self.adapter.dhan.LIMIT,
                    product_type=self.adapter.dhan.INTRA,
                    price=round(fill_price + 0.5, 1),
                )
                if not isinstance(resp, dict) or resp.get("status") != "success":
                    log.error(f"LIVE order placement failed: {resp}")
                    return False
            except Exception as exc:
                log.error(f"LIVE order exception: {exc}")
                return False

        self.active_trade = ActiveOptionTrade(
            index_name=idx_name,
            security_id=leg["security_id"],
            option_type=opt_type,
            strike=int(leg["strike"]),
            quantity=qty,
            entry_price=fill_price,
            initial_sl=initial_sl,
            current_sl=initial_sl,
            highest_ltp=fill_price,
            risk_per_unit=risk_per_unit,
            entry_time=datetime.now(),
            entry_state=state.tolist(),
            action_code=action,
            rule_signature=rule_sig,
            entry_spot=float(snap["spot"]),
        )
        self.trades_today += 1
        log.info(
            f"[{self.cfg.mode} ENTRY] {idx_name} {leg['strike']} {opt_type} | "
            f"Lots: {num_lots} (Qty: {qty}) @ Rs.{fill_price:.2f} | SL: Rs.{initial_sl:.2f} | "
            f"3-Strike Diff = {oi_report.diff_ce_minus_pe_oi:+,} | Ctrl: {oi_report.market_controller}"
        )
        return True

    def manage_open_position(
        self,
        snap: Dict[str, Any],
        oi_report: StraddlingThreeStrikeAnalysis,
        elapsed_sim_mins: Optional[float] = None,
        force_square_off: bool = False,
    ) -> Optional[Tuple[Dict[str, Any], float, float]]:
        if self.active_trade is None or snap["index"] != self.active_trade.index_name:
            return None

        pos = self.active_trade
        holding_mins = (
            elapsed_sim_mins
            if elapsed_sim_mins is not None
            else (datetime.now() - pos.entry_time).total_seconds() / 60.0
        )

        strike_key = f"{pos.strike}_{pos.option_type}"
        strike_map = snap.get("strike_ltp_map", {})
        spec = INDEX_SPECS[pos.index_name]

        if strike_key in strike_map and snap.get("data_health") == "LIVE_CLEAN":
            current_ltp = float(strike_map[strike_key])
            bar_low_ltp = current_ltp
        elif pos.entry_spot > 0:
            spot_move = float(snap["spot"]) - pos.entry_spot
            signed_move = spot_move if pos.option_type == "CE" else -spot_move
            scale_pts = float(spec.strike_step * 3)
            if signed_move >= 0:
                effective_delta = 0.52 + 0.16 * math.tanh(signed_move / scale_pts)
            else:
                effective_delta = max(0.22, 0.52 + 0.18 * math.tanh(signed_move / scale_pts))

            theta_bleed = (0.14 if pos.index_name == "NIFTY" else 0.35) * holding_mins
            current_ltp = round(
                max(2.0, pos.entry_price + (effective_delta * signed_move) - theta_bleed), 2
            )
            wick_noise = random.uniform(0.0, 0.32 * pos.risk_per_unit)
            bar_low_ltp = round(max(1.0, current_ltp - wick_noise), 2)
        else:
            leg = snap["ce"] if pos.option_type == "CE" else snap["pe"]
            current_ltp = float(leg["ltp"])
            bar_low_ltp = current_ltp

        if current_ltp <= 0.05:
            return None

        sl_triggered = bar_low_ltp <= pos.current_sl

        if not sl_triggered:
            pos.highest_ltp = max(pos.highest_ltp, current_ltp)

        unrealized_pts = current_ltp - pos.entry_price
        peak_pts = pos.highest_ltp - pos.entry_price
        current_r = unrealized_pts / pos.risk_per_unit
        peak_r = peak_pts / pos.risk_per_unit

        be_offset = round(72.0 / pos.quantity, 2)
        if not sl_triggered and peak_r >= self.cfg.breakeven_trigger_r and pos.current_sl < (pos.entry_price + be_offset):
            pos.current_sl = round(pos.entry_price + be_offset, 2)

        active_quad = oi_report.ce_quadrant if pos.option_type == "CE" else oi_report.pe_quadrant
        if not sl_triggered and peak_r >= self.cfg.chandelier_trigger_r:
            lock_ratio = 0.78 if active_quad == "LONG_UNWINDING" else 0.68
            chandelier_sl = round(pos.entry_price + (peak_pts * lock_ratio), 2)
            if chandelier_sl > pos.current_sl:
                pos.current_sl = chandelier_sl

        sl_triggered = sl_triggered or (current_ltp <= pos.current_sl)
        theta_time_stop = (holding_mins >= self.cfg.theta_time_stop_mins) and (current_r < 0.35)

        if not (sl_triggered or theta_time_stop or force_square_off):
            return None

        if force_square_off:
            reason = "EOD_SQUARE_OFF_1515"
            raw_exit = current_ltp
        elif theta_time_stop and not sl_triggered:
            reason = "THETA_TIME_STOP"
            raw_exit = current_ltp
        elif pos.current_sl > pos.entry_price:
            reason = "ASYMMETRIC_TRAIL_PROFIT"
            raw_exit = round(pos.current_sl - random.uniform(0.10, 0.45), 2)
        else:
            reason = "INITIAL_SL_HIT"
            slip = random.uniform(0.03, 0.15) * pos.risk_per_unit
            raw_exit = round(max(1.0, pos.initial_sl - slip), 2) if self.cfg.mode == "PAPER" else current_ltp

        exit_fill = round(raw_exit * 0.9992, 2) if self.cfg.mode == "PAPER" else raw_exit

        if self.cfg.mode == "LIVE" and self.adapter.dhan is not None:
            try:
                self.adapter.dhan.place_order(
                    security_id=pos.security_id,
                    exchange_segment=spec.fno_segment,
                    transaction_type=self.adapter.dhan.SELL,
                    quantity=pos.quantity,
                    order_type=self.adapter.dhan.LIMIT,
                    product_type=self.adapter.dhan.INTRA,
                    price=round(exit_fill - 0.5, 1),
                )
            except Exception as exc:
                log.error(f"LIVE exit order exception: {exc}")

        acc = compute_trade_accounting(pos.entry_price, exit_fill, pos.quantity, spec.exchange_fee_pct)
        one_r_rs = pos.risk_per_unit * pos.quantity
        realized_r = acc["gross_pnl"] / max(one_r_rs, 1.0)

        self.virtual_balance_gross += acc["gross_pnl"]
        self.virtual_balance_net += acc["net_pnl"]
        self.daily_net_pnl += acc["net_pnl"]

        record = {
            "timestamp": datetime.now().isoformat(),
            "index": pos.index_name,
            "contract": f"{pos.strike}_{pos.option_type}",
            "quantity": pos.quantity,
            "entry_price": pos.entry_price,
            "exit_price": exit_fill,
            "exit_reason": reason,
            "holding_mins": round(holding_mins, 2),
            "r_multiple": round(realized_r, 2),
            "gross_pnl": acc["gross_pnl"],
            "charges": acc["charges"],
            "net_pnl": acc["net_pnl"],
            "data_health": oi_report.data_health,
            "rule_used": pos.rule_signature,
        }
        self.trade_history.append(record)
        self.active_trade = None
        self.save_ledger()

        log.info(
            f"[{self.cfg.mode} EXIT] {record['contract']} ({reason}) | Exit @ Rs.{exit_fill:.2f} | "
            f"Gross PnL (no charges): Rs.{acc['gross_pnl']:+.2f} ({realized_r:+.2f}R) | "
            f"Net PnL: Rs.{acc['net_pnl']:+.2f} | Gross Wallet: Rs.{self.virtual_balance_gross:,.2f}"
        )
        return record, realized_r, holding_mins


# ===========================================================================
# 8. AUTONOMOUS 09:10 AM - 03:45 PM IST MASTER CONTROLLER
# ===========================================================================
class AutonomousOptionsBot:
    def __init__(self, cfg: BotConfig, simulate: bool = False):
        self.cfg = cfg
        self.adapter = FaultTolerantDhanAdapter(cfg, simulate=simulate)
        self.study_engine = MarketStudyEngine(cfg)
        self.brain = SelfLearningBrain(cfg)
        self.broker = AsymmetricExecutionBroker(cfg, self.adapter)

    def run_live_market_schedule(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.brain.start_new_trading_day(today_str)
        calibrated = False

        log.info(
            f"Autonomous Bot v4.1 Active ({self.cfg.mode}) | Schedule: 09:10-15:45 IST | "
            f"Gross Wallet: Rs.{self.broker.virtual_balance_gross:,.2f}"
        )

        while True:
            try:
                now = datetime.now()
                cur_t = now.time()

                if cur_t < dtime(9, 10):
                    time.sleep(15)
                    continue

                if dtime(9, 10) <= cur_t < dtime(9, 15) and not calibrated:
                    for idx in ("NIFTY", "SENSEX"):
                        lv = self.adapter.calibrate_pre_open(idx)
                        log.info(f"[09:10 CALIBRATION] {idx} -> {lv}")
                    calibrated = True
                    time.sleep(8)
                    continue

                if dtime(9, 15) <= cur_t < dtime(9, 20):
                    for idx in ("NIFTY", "SENSEX"):
                        snap = self.adapter.fetch_market_snapshot(idx)
                        orb = self.adapter.orb_bounds.setdefault(
                            idx, {"high": snap["spot"], "low": snap["spot"]}
                        )
                        orb["high"] = max(orb["high"], snap["spot"])
                        orb["low"] = min(orb["low"], snap["spot"])
                    time.sleep(self.cfg.option_chain_poll_sec)
                    continue

                if dtime(9, 20) <= cur_t < dtime(15, 15):
                    if not calibrated:
                        for idx in ("NIFTY", "SENSEX"):
                            self.adapter.calibrate_pre_open(idx)
                        calibrated = True

                    for idx in ("NIFTY", "SENSEX"):
                        snap = self.adapter.fetch_market_snapshot(idx)
                        levels = self.adapter.prev_day_levels[idx]
                        orb = self.adapter.orb_bounds.get(
                            idx, {"high": snap["spot"] + 25, "low": snap["spot"] - 25}
                        )
                        state_vec, tags, oi_report = self.study_engine.extract_state_and_tags(snap, levels, orb)

                        if self.broker.active_trade and self.broker.active_trade.index_name == idx:
                            saved_state = np.array(self.broker.active_trade.entry_state)
                            saved_act = self.broker.active_trade.action_code
                            saved_rule = self.broker.active_trade.rule_signature
                            saved_strike = self.broker.active_trade.strike
                            res = self.broker.manage_open_position(snap, oi_report)
                            if res is not None:
                                _, r_mult, hold_m = res
                                self.brain.learn_from_closed_trade(
                                    saved_state, saved_act, r_mult, hold_m, saved_rule,
                                    state_vec, idx, saved_strike, today_str
                                )
                        elif self.broker.can_enter(self.brain.consecutive_losses):
                            action, q_val, rule_sig = self.brain.decide_action(state_vec, tags, oi_report)
                            if action in (1, 2):
                                self.broker.open_option_position(
                                    snap,
                                    oi_report,
                                    action,
                                    state_vec,
                                    rule_sig,
                                    effective_q=q_val,
                                    rule_obj=self.brain.rules.get(rule_sig),
                                    sniper_mode=(self.brain.consecutive_losses >= 1),
                                )

                    time.sleep(self.cfg.option_chain_poll_sec)
                    continue

                if dtime(15, 15) <= cur_t < dtime(15, 30):
                    if self.broker.active_trade is not None:
                        idx = self.broker.active_trade.index_name
                        snap = self.adapter.fetch_market_snapshot(idx)
                        levels = self.adapter.prev_day_levels[idx]
                        orb = self.adapter.orb_bounds.get(idx, {"high": snap["spot"], "low": snap["spot"]})
                        state_vec, _, oi_report = self.study_engine.extract_state_and_tags(snap, levels, orb)
                        saved_state = np.array(self.broker.active_trade.entry_state)
                        saved_act = self.broker.active_trade.action_code
                        saved_rule = self.broker.active_trade.rule_signature
                        saved_strike = self.broker.active_trade.strike
                        res = self.broker.manage_open_position(snap, oi_report, force_square_off=True)
                        if res is not None:
                            _, r_mult, hold_m = res
                            self.brain.learn_from_closed_trade(
                                saved_state, saved_act, r_mult, hold_m, saved_rule,
                                state_vec, idx, saved_strike, today_str
                            )
                    time.sleep(10)
                    continue

                if dtime(15, 30) <= cur_t <= dtime(15, 45):
                    log.info("[15:30 POST-MARKET] Running Experience Replay & Multi-Day Rule Consolidation...")
                    self.brain.post_market_experience_replay(batch_size=64)
                    self.print_performance_report()
                    break

                if cur_t > dtime(15, 45):
                    log.info("Past 03:45 PM IST. Run with --simulate-month 20 to test/pre-train immediately.")
                    break

            except Exception as loop_exc:
                log.error(f"[MASTER LOOP RECOVERED] Caught unexpected error: {loop_exc}. Continuing in 5s...")
                time.sleep(5)

    def run_fast_simulation(self, trading_days: int = 20):
        log.info(f"Running {trading_days}-Day Autonomous Walk-Forward Simulation (Wallet: Rs. 30,000)...")
        base_date = datetime.now() - timedelta(days=trading_days)
        yearly_stats: Dict[int, Dict[str, float]] = {}

        for day_idx in range(1, trading_days + 1):
            year_num = ((day_idx - 1) // 250) + 1
            y_bucket = yearly_stats.setdefault(
                year_num, {"trades": 0, "wins": 0, "gross_pnl": 0.0, "net_pnl": 0.0}
            )
            trades_before = len(self.broker.trade_history)

            day_str = (base_date + timedelta(days=day_idx)).strftime("%Y-%m-%d")
            self.brain.start_new_trading_day(day_str)
            self.broker.daily_net_pnl = 0.0
            self.broker.trades_today = 0

            self.adapter.sim_spot = {
                "NIFTY": round(22492.0 + random.gauss(0, 400), 2),
                "SENSEX": round(81465.0 + random.gauss(0, 1200), 2),
            }
            self.adapter.sim_vwap = dict(self.adapter.sim_spot)
            self.adapter.day_open_spot = dict(self.adapter.sim_spot)

            for idx in ("NIFTY", "SENSEX"):
                self.adapter.calibrate_pre_open(idx)

            sim_hold_mins = 0.0
            for step in range(120):
                for idx in ("NIFTY", "SENSEX"):
                    snap = self.adapter.fetch_market_snapshot(idx)
                    levels = self.adapter.prev_day_levels[idx]
                    orb = self.adapter.orb_bounds[idx]
                    state_vec, tags, oi_report = self.study_engine.extract_state_and_tags(snap, levels, orb)

                    if self.broker.active_trade and self.broker.active_trade.index_name == idx:
                        sim_hold_mins += 3.0
                        saved_state = np.array(self.broker.active_trade.entry_state)
                        saved_act = self.broker.active_trade.action_code
                        saved_rule = self.broker.active_trade.rule_signature
                        saved_strike = self.broker.active_trade.strike
                        res = self.broker.manage_open_position(
                            snap, oi_report, elapsed_sim_mins=sim_hold_mins, force_square_off=(step == 119)
                        )
                        if res is not None:
                            _, r_mult, hold_m = res
                            self.brain.learn_from_closed_trade(
                                saved_state, saved_act, r_mult, hold_m, saved_rule,
                                state_vec, idx, saved_strike, day_str
                            )
                            sim_hold_mins = 0.0
                    elif self.broker.can_enter(self.brain.consecutive_losses) and step < 110:
                        action, q_val, rule_sig = self.brain.decide_action(
                            state_vec, tags, oi_report, sim_clock_mins=step * 3.0
                        )
                        if action in (1, 2):
                            if self.broker.open_option_position(
                                snap,
                                oi_report,
                                action,
                                state_vec,
                                rule_sig,
                                effective_q=q_val,
                                rule_obj=self.brain.rules.get(rule_sig),
                                sniper_mode=(self.brain.consecutive_losses >= 1),
                            ):
                                sim_hold_mins = 0.0

            for new_t in self.broker.trade_history[trades_before:]:
                y_bucket["trades"] += 1
                if new_t["gross_pnl"] > 0:
                    y_bucket["wins"] += 1
                y_bucket["gross_pnl"] += new_t["gross_pnl"]
                y_bucket["net_pnl"] += new_t["net_pnl"]

            self.brain.post_market_experience_replay(batch_size=64)

        if trading_days >= 250:
            print("\n" + "=" * 80)
            print(f"   MULTI-YEAR ({trading_days} TRADING DAYS) WALK-FORWARD BREAKDOWN")
            print("=" * 80)
            for yr, st in sorted(yearly_stats.items()):
                wr = (st["wins"] / max(1, st["trades"])) * 100.0
                print(
                    f" Year {yr} (250d) | Trades: {int(st['trades']):>3} | Win Rate: {wr:5.1f}% | "
                    f"Gross PnL: Rs.{st['gross_pnl']:>+11,.2f} | Net PnL: Rs.{st['net_pnl']:>+11,.2f}"
                )
            print("=" * 80)

        self.print_performance_report()

    def print_performance_report(self):
        trades = self.broker.trade_history
        wins = [t["gross_pnl"] for t in trades if t["gross_pnl"] > 0]
        losses = [abs(t["gross_pnl"]) for t in trades if t["gross_pnl"] <= 0]
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        gross_profit = self.broker.virtual_balance_gross - self.cfg.initial_capital
        net_profit = self.broker.virtual_balance_net - self.cfg.initial_capital
        active_rules = [r for r in self.brain.rules.values() if r.status == "ACTIVE"]
        muted_rules = [r for r in self.brain.rules.values() if r.status == "DAY_MUTED"]
        pruned_rules = [r for r in self.brain.rules.values() if r.status == "PRUNED"]

        print("\n" + "=" * 80)
        print("   DHANHQ v4.1 AUTONOMOUS SELF-LEARNING BOT REPORT (ANTI-OVERFIT + MULTI-LOT)")
        print("=" * 80)
        print(f" Execution Mode              : {self.cfg.mode}")
        print(f" Starting Virtual Balance    : Rs. {self.cfg.initial_capital:,.2f}")
        print(f" Ending Balance (No Charges) : Rs. {self.broker.virtual_balance_gross:,.2f}")
        print(f" Ending Balance (Net of Fee) : Rs. {self.broker.virtual_balance_net:,.2f}")
        print(f" Profit Without Charges      : Rs. {gross_profit:+,.2f} (1st Month Goal: +Rs. 10,000)")
        print(f" Net Profit After Charges    : Rs. {net_profit:+,.2f}")
        print("-" * 80)
        print(f" Total Trades Executed       : {len(trades)} (Wins: {len(wins)} | Losses: {len(losses)})")
        print(f" Average Winning Trade Value : Rs. {avg_win:,.2f}")
        print(f" Average Losing Trade Value  : Rs. {avg_loss:,.2f}")
        print(f" Asymmetric Win/Loss Ratio   : {avg_win / max(avg_loss, 1.0):.2f}x (Mandate: Avg Win >> Avg Loss)")
        print(
            f" Self-Learned Rule Book      : {len(self.brain.rules)} Total | "
            f"{len(active_rules)} ACTIVE | {len(muted_rules)} DAY_MUTED | {len(pruned_rules)} PRUNED"
        )
        print("=" * 80 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DhanHQ v4.1 Self-Learning NIFTY & SENSEX Options Bot")
    parser.add_argument("--mode", choices=["PAPER", "LIVE"], default="PAPER", help="PAPER or LIVE mode")
    parser.add_argument(
        "--simulate-month",
        type=int,
        default=0,
        help="Run N-day fast simulation immediately (e.g., --simulate-month 1250 for 5 years)",
    )
    args = parser.parse_args()

    bot_cfg = BotConfig(mode=args.mode)
    if args.simulate_month > 0:
        bot = AutonomousOptionsBot(bot_cfg, simulate=True)
        bot.run_fast_simulation(trading_days=args.simulate_month)
    else:
        bot = AutonomousOptionsBot(bot_cfg, simulate=False)
        bot.run_live_market_schedule()
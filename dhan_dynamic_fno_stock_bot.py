#!/usr/bin/env python3
"""Selective stock options: capped shortlist entries, rupee targets and premium exits.
API reference: https://docs.dhanhq.co/
Targets and stops are intentions, not guaranteed fills or profits.
"""

import os
import sys
import csv
import math
import time
import json
import hashlib
import logging
import argparse
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Dict, Any, List, Optional
import requests
from collections import Counter
from bisect import bisect_left
from pathlib import Path
from stock_option_signals import evaluate_stock_setup
from stock_option_policy import premium_plan, rank_candidates, DailyTradeGate, stock_reentry_reason

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_START_TIME = dtime(9, 15)
SESSION_END_TIME = dtime(14, 45)

try:
    from telegram_notifier import TelegramNotifier
except ImportError:
    TelegramNotifier = None

if __name__=="__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = "dhan_stock_fno.log"
LEDGER_FILE = "stock_fno_paper_ledger.json"
CACHE_DIR = "dhan_hist_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


def setup_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("DhanStockOptionsV42")


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


def round_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def compute_fno_option_charges(buy_price: float, sell_price: float, qty: int) -> float:
    """Statutory Indian NSE_FNO Option Buying charges (Brokerage Rs.40 + STT + Exch + SEBI + Stamp + GST)."""
    buy_turn = buy_price * qty
    sell_turn = sell_price * qty
    tot_turn = buy_turn + sell_turn
    brokerage = 40.0
    stt = sell_turn * 0.0015  # NSE rate effective 2026-04-01
    exch_fee = tot_turn * 0.0003503
    sebi = tot_turn * 0.000001
    stamp = buy_turn * 0.00003
    gst = 0.18 * (brokerage + exch_fee + sebi)
    return round(brokerage + stt + exch_fee + sebi + stamp + gst, 2)


def load_or_init_ledger(live_mode: bool, initial_capital: float) -> Dict[str, Any]:
    default_ledger = {
        "mode": "LIVE" if live_mode else "PAPER",
        "initial_capital": initial_capital,
        "virtual_balance_gross": initial_capital,
        "virtual_balance_net": initial_capital,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "avg_winning_value_rs": 0.0,
        "avg_losing_value_rs": 0.0,
        "win_to_loss_ratio": 0.0,
        "trades": [],
    }
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "trades" in data:
                data["mode"] = "LIVE" if live_mode else "PAPER"
                return data
        except Exception:
            pass
    atomic_write_json(LEDGER_FILE, default_ledger)
    return default_ledger


def normalize_expiry(value):
    raw = str(value).strip().split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


class DynamicStrategyConfig:
    def __init__(self, env: Dict[str, str], args: argparse.Namespace):
        self.initial_capital = float(env.get("STOCK_OPT_INITIAL_CAPITAL", 50000.0))
        self.min_day_change_pct = float(getattr(args, "min_pct", None) or env.get("SCANX_MIN_DAY_PCT", 2.20))
        self.ema_period = int(env.get("SCANX_EMA_PERIOD", 10))
        self.max_concurrent = int(env.get("STOCK_OPT_MAX_CONCURRENT", 2))
        self.max_daily_trades = int(env.get("STOCK_OPT_MAX_DAILY_TRADES", 2))
        self.max_consec_losses = int(env.get("MAX_CONSECUTIVE_LOSSES_PER_DAY", 2))

        self.target_pct = float(env.get("STOCK_OPT_TARGET_PCT", 12.0))
        self.stop_pct = float(env.get("STOCK_OPT_STOP_PCT", 5.0))
        self.min_rr_target = float(getattr(args, "min_rr", None) or env.get("STOCK_OPT_MIN_NET_RR", 1.3))
        self.slippage_pct = float(env.get("STOCK_OPT_SLIPPAGE_PCT", 0.1))
        self.max_spread_pct = float(env.get("STOCK_OPT_MAX_SPREAD_PCT", 3.5))
        self.max_hold_bars_5m = int(env.get("STOCK_OPT_MAX_HOLD_BARS_5M", 12))
        self.rupee_target = float(env.get("STOCK_OPT_RUPEE_TARGET", 900))
        self.rupee_max_risk = float(env.get("STOCK_OPT_RUPEE_MAX_RISK", 1800))
        self.max_lots = int(env.get("STOCK_OPT_MAX_LOTS", 2))
        self.min_days_to_expiry = int(env.get("STOCK_OPT_MIN_DAYS_TO_EXPIRY", 4))
        self.min_delta = float(env.get("STOCK_OPT_MIN_DELTA", 0.38))
        self.max_delta = float(env.get("STOCK_OPT_MAX_DELTA", 0.78))
        self.max_theta_pct_per_day = float(env.get("STOCK_OPT_MAX_THETA_PCT_PER_DAY", 6))
        self.no_progress_bars = int(env.get("STOCK_OPT_NO_PROGRESS_BARS", 5))
        self.max_premium = float(env.get("STOCK_OPT_MAX_PREMIUM", 1000))
        self.confirm_window_sec = int(env.get("STOCK_OPT_CONFIRM_WINDOW_SEC", 600))
        self.poll_interval_sec = float(env.get("STOCK_OPT_POLL_INTERVAL_SEC", 4))
        self.gate_dir = "stock_option_daily_entries"
        self.enable_opening_trend=env.get("STOCK_OPT_OPENING_TREND","false").strip().lower()=="true"
        self.extend_rupee_target=env.get("STOCK_OPT_EXTEND_RUPEE_TARGET","false").strip().lower()=="true"
        self.reentry_after_loss=env.get("STOCK_OPT_REENTRY_AFTER_LOSS","false").strip().lower()=="true"
        numbers = (self.target_pct, self.stop_pct, self.min_rr_target, self.slippage_pct,
                   self.max_spread_pct, self.rupee_target, self.rupee_max_risk,
                   self.min_delta, self.max_delta, self.max_theta_pct_per_day, self.max_premium,
                   self.poll_interval_sec)
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError("Stock option settings must be finite")
        if not (3 <= self.target_pct <= 20 and 0.5 <= self.stop_pct <= 10 and self.min_rr_target >= 1.2
                and 0 <= self.slippage_pct < 100 and 0 < self.max_spread_pct <= 5
                and 1 <= self.max_concurrent <= self.max_daily_trades <= 2
                and self.rupee_target > 0 and self.rupee_max_risk > 0 and self.max_lots >= 1
                and 0 < self.min_delta < self.max_delta <= 1 and self.min_days_to_expiry >= 4
                and self.max_theta_pct_per_day > 0 and self.max_premium >= 3
                and 300 <= self.confirm_window_sec <= 900
                and 3 <= self.poll_interval_sec <= 5
                and 1 <= self.no_progress_bars <= self.max_hold_bars_5m):
            raise ValueError("Invalid stock-option target, risk, Greek or entry limits")

    def plan(self, entry: float, qty: int, max_target_price=None):
        return premium_plan(entry, qty, compute_fno_option_charges, self.target_pct,
                            self.stop_pct, self.min_rr_target, self.slippage_pct,
                            self.rupee_target, self.max_lots, self.rupee_max_risk,self.extend_rupee_target,max_target_price)


class DynamicStockOptionsUniverse:
    """
    Dynamically parses Dhan's live `api-scrip-master.csv` to discover all NSE F&O stocks,
    their exact option strike steps, lot sizes, and nearest-expiry OPTSTK CE/PE security IDs.
    """

    @staticmethod
    def _is_real_symbol(sym: str, custom_name: str = "") -> bool:
        if not sym:
            return False
        u_sym = sym.upper()
        u_name = (custom_name or "").upper()
        if "TEST" in u_sym or "TEST" in u_name or "DUMMY" in u_sym or u_sym[0].isdigit():
            return False
        return True

    @classmethod
    def load_universe(cls, logger: logging.Logger, min_days_to_expiry=12) -> Dict[str, Dict[str, Any]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        cache_file = os.path.join(CACHE_DIR, f"dynamic_optstk_universe_{today_str}_dte{min_days_to_expiry}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    logger.info(f"Loaded {len(data)} dynamic NSE F&O stocks + OPTSTK chains from today's cache.")
                    return data
            except Exception:
                pass

        logger.info("Downloading Dhan Scrip Master CSV to build dynamic Stock Options (OPTSTK) universe...")
        resp = requests.get(SCRIP_MASTER_URL, timeout=45)
        resp.raise_for_status()
        reader = csv.DictReader(resp.text.splitlines())

        eq_map: Dict[str, Dict[str, Any]] = {}
        optstk_raw: Dict[str, List[Dict[str, Any]]] = {}

        for row in reader:
            exch = row.get("SEM_EXM_EXCH_ID", "").strip()
            inst = row.get("SEM_INSTRUMENT_NAME", "").strip()
            sym = row.get("SEM_TRADING_SYMBOL", "").strip()
            sec_id = row.get("SEM_SMST_SECURITY_ID", "").strip()
            custom_sym = row.get("SEM_CUSTOM_SYMBOL", "").strip()
            lot_str = row.get("SEM_LOT_UNITS", "1").strip()
            series = row.get("SEM_SERIES", "").strip()
            strike_str = row.get("SEM_STRIKE_PRICE", "0").strip()
            opt_type = row.get("SEM_OPTION_TYPE", "").strip().upper()
            exp_str = row.get("SEM_EXPIRY_DATE", "").strip()

            if exch == "NSE" and inst == "EQUITY" and series == "EQ":
                base_sym = sym.replace("-EQ", "").strip().upper()
                if not cls._is_real_symbol(base_sym, custom_sym):
                    continue
                eq_map[base_sym] = {
                    "symbol": base_sym,
                    "name": custom_sym or base_sym,
                    "eq_security_id": str(sec_id),
                }
            elif exch == "NSE" and inst == "OPTSTK" and opt_type in ("CE", "PE") and "-" in sym:
                base_sym = sym.split("-")[0].strip().upper()
                if not cls._is_real_symbol(base_sym, custom_sym):
                    continue
                exp_date = normalize_expiry(exp_str)
                if not exp_date or exp_date < today_str:
                    continue
                try:
                    strike_val = round(float(strike_str), 2)
                    lot_val = max(1, int(float(lot_str)))
                except ValueError:
                    continue
                if strike_val <= 0:
                    continue
                optstk_raw.setdefault(base_sym, []).append({
                    "opt_security_id": str(sec_id),
                    "trading_symbol": sym,
                    "strike": strike_val,
                    "opt_type": opt_type,
                    "lot_size": lot_val,
                    "expiry": exp_date,
                })

        universe: Dict[str, Dict[str, Any]] = {}
        for base_sym, contracts in sorted(optstk_raw.items()):
            if base_sym not in eq_map or not contracts:
                continue
            valid_exps = sorted({c["expiry"] for c in contracts
                if (datetime.strptime(c["expiry"], "%Y-%m-%d").date() - datetime.now(IST).date()).days >= min_days_to_expiry})
            if not valid_exps:
                continue
            chosen_exp = valid_exps[0]

            near_contracts = [c for c in contracts if c["expiry"] == chosen_exp]
            if not near_contracts:
                continue

            lot_size = near_contracts[0]["lot_size"]
            options_by_strike: Dict[str, Dict[str, Any]] = {}
            all_strikes_sorted = sorted({c["strike"] for c in near_contracts})
            strike_diffs = [
                round(all_strikes_sorted[k] - all_strikes_sorted[k - 1], 2)
                for k in range(1, len(all_strikes_sorted))
                if (all_strikes_sorted[k] - all_strikes_sorted[k - 1]) > 0
            ]
            strike_step = min(strike_diffs) if strike_diffs else 10.0

            for c in near_contracts:
                k = f"{c['strike']:.2f}"
                options_by_strike.setdefault(k, {"strike": c["strike"]})
                options_by_strike[k][c["opt_type"]] = {
                    "security_id": c["opt_security_id"],
                    "trading_symbol": c["trading_symbol"],
                }

            eq_info = eq_map[base_sym]
            universe[eq_info["eq_security_id"]] = {
                "symbol": base_sym,
                "name": eq_info["name"],
                "eq_security_id": eq_info["eq_security_id"],
                "fno_lot_size": lot_size,
                "strike_step": strike_step,
                "nearest_expiry": chosen_exp,
                "strikes_map": options_by_strike,
            }

        atomic_write_json(cache_file, universe)
        logger.info(f"Dynamically built Stock Options (OPTSTK) universe for {len(universe)} NSE F&O stocks.")
        return universe

    @staticmethod
    def resolve_atm_option(stock_info, spot_price, opt_type, chain, cfg):
        """Select a real broker delta in band, never an estimated moneyness delta."""
        candidates = []
        ivs = []
        for strike, node in chain.items():
            leg = node.get(opt_type.lower(), {})
            try:
                greeks = leg["greeks"]
                delta, theta, gamma = (float(greeks[k]) for k in ("delta", "theta", "gamma"))
                iv = float(leg["implied_volatility"])
                if not all(math.isfinite(v) for v in (delta, theta, gamma, iv)) or iv <= 0 or gamma <= 0:
                    continue
                ivs.append(iv)
                if not cfg.min_delta <= abs(delta) <= cfg.max_delta or (delta > 0) != (opt_type == "CE"):
                    continue
                strike_value = float(strike)
                contract = stock_info["strikes_map"].get(f"{strike_value:.2f}", {}).get(opt_type)
                security_id = leg.get("security_id", leg.get("securityId"))
                if not contract or security_id is None or str(contract["security_id"]) != str(security_id):
                    continue
                candidates.append({"symbol": stock_info["symbol"], "strike": strike_value,
                    "strike_step": stock_info["strike_step"], "delta": delta, "theta": theta,
                    "gamma": gamma, "iv": iv, "opt_type": opt_type,
                    "opt_security_id": str(security_id), "trading_symbol": contract["trading_symbol"],
                    "lot_size": int(stock_info["fno_lot_size"]), "expiry": stock_info["nearest_expiry"],
                    "greeks_timestamp": leg.get("received_epoch", time.time()),
                    "greeks_received_epoch": leg.get("received_epoch", time.time())})
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates or not ivs:
            return None
        for candidate in candidates:
            candidate["iv_range_low"], candidate["iv_range_high"] = min(ivs), max(ivs)
        return min(candidates, key=lambda c: (abs(abs(c["delta"])-(cfg.min_delta+cfg.max_delta)/2), c["iv"]))


CACHE_VERSION = 2

def boundary(value, default):
    if value is None:
        return default
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def quote_received_age(quote, now_epoch):
    """Age of local response receipt; last-trade time is not snapshot time."""
    try:
        received=float(quote["received_epoch"])
        age=float(now_epoch)-received
    except (KeyError,TypeError,ValueError,OverflowError):
        return float("inf")
    return age if math.isfinite(age) else float("inf")


class DhanStockOptionsClient:
    def _history_issue(self, payload, start, end, kind, **details):
        event = {"security_id": str(payload["securityId"]), "instrument": payload["instrument"],
                 "side": payload.get("drvOptionType"), "from": start.isoformat(),
                 "to_exclusive": end.isoformat(), "reason": kind,
                 "severity": "coverage_warning" if kind in ("internal_bar_gap", "partial_session_tail") else "error",
                 **details}
        self.history_events.append(event)
        self.logger.warning("Historical data quality: %s", json.dumps(event))

    def _history_window(self, endpoint, payload, start, end, fields, cache_dir, use_cache):
        now = datetime.now(IST)
        request = {**payload, "fromDate": start.strftime("%Y-%m-%d %H:%M:%S") if endpoint == "intraday" else start.strftime("%Y-%m-%d"),
                   "toDate": end.strftime("%Y-%m-%d %H:%M:%S") if endpoint == "intraday" else end.strftime("%Y-%m-%d")}
        key = hashlib.sha256(json.dumps({"endpoint": endpoint, "payload": request}, sort_keys=True).encode()).hexdigest()
        path = Path(cache_dir) / f"history_v{CACHE_VERSION}_{key}.json"
        final = end <= now.replace(hour=0, minute=0, second=0, microsecond=0)

        def validate(data):
            if not isinstance(data, dict) or not data.get("timestamp"):
                raise ValueError("empty")
            if any(not isinstance(data.get(k), list) or len(data[k]) != len(data["timestamp"]) for k in fields):
                raise ValueError("unequal_arrays")
            result = {k: [] for k in fields}
            previous = None
            for i, stamp in enumerate(data["timestamp"]):
                stamp = float(stamp)
                if not math.isfinite(stamp) or stamp != int(stamp) or (previous is not None and stamp <= previous):
                    raise ValueError("bad_timestamps")
                previous = stamp
                # Normalize Dhan responses to a shared [start, end) interval.
                if not start.timestamp() <= stamp < end.timestamp():
                    continue
                row = {k: float(data[k][i]) for k in fields}
                if not all(math.isfinite(v) for v in row.values()):
                    raise ValueError("nonfinite")
                if not 0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                    raise ValueError("bad_ohlc")
                if "volume" in row and row["volume"] < 0:
                    raise ValueError("bad_volume")
                if "strike" in row and row["strike"] <= 0:
                    raise ValueError("bad_strike")
                for k in fields:
                    result[k].append(int(stamp) if k == "timestamp" else row[k])
            if not result["timestamp"]:
                raise ValueError("empty_range")
            return result

        def record(data, source):
            self.history_coverage.append({"security_id":str(payload["securityId"]), "endpoint":endpoint,
                "side":payload.get("drvOptionType"), "from":start.isoformat(),"to_exclusive":end.isoformat(),
                "first_bar":datetime.fromtimestamp(data["timestamp"][0],IST).isoformat(),
                "last_bar":datetime.fromtimestamp(data["timestamp"][-1],IST).isoformat(),
                "rows":len(data["timestamp"]), "source":source})
            step = int(payload["interval"])*60
            for previous, current in zip(data["timestamp"],data["timestamp"][1:]):
                if (datetime.fromtimestamp(previous,IST).date() == datetime.fromtimestamp(current,IST).date()
                        and SESSION_START_TIME <= datetime.fromtimestamp(previous,IST).time().replace(tzinfo=None)
                        < datetime.fromtimestamp(current,IST).time().replace(tzinfo=None) < SESSION_END_TIME
                        and current-previous != step):
                    self._history_issue(payload,start,end,"internal_bar_gap",previous_bar=previous,next_bar=current)
                    break
            if (endpoint == "intraday" and payload["exchangeSegment"] == "NSE_EQ" and final
                    and end.hour == end.minute == end.second == 0):
                last = datetime.fromtimestamp(data["timestamp"][-1],IST)
                expected_last = last.replace(hour=SESSION_END_TIME.hour,minute=SESSION_END_TIME.minute,second=0,microsecond=0)-timedelta(seconds=step)
                if last < expected_last:
                    self._history_issue(payload,start,end,"partial_session_tail",last_bar=last.isoformat())
            return data

        if use_cache and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(cached["received_epoch"])
                if (cached.get("version") == CACHE_VERSION and cached.get("request_key") == key
                        and 0 <= age and (final or age <= 15)):
                    issue_count, coverage_count = len(self.history_events), len(self.history_coverage)
                    data = record(validate(cached["data"]),"cache")
                    if len(self.history_events) == issue_count:
                        return data
                    # An old partial cache is not authoritative. Try the network;
                    # only the replacement request determines final coverage.
                    del self.history_events[issue_count:]
                    del self.history_coverage[coverage_count:]
            except (KeyError, ValueError, TypeError, OSError):
                pass
        # Stop repeated network waits after three exhausted transient failures.
        if self.history_failure_streak >= 3:
            opened=getattr(self,'history_circuit_opened_at',None)
            if opened is None:
                opened=time.monotonic();self.history_circuit_opened_at=opened
            if not final and time.monotonic()-opened>=60:
                # A live scanner must not remain disabled for the rest of the
                # session after a temporary outage. Allow one bounded probe.
                self.history_failure_streak=2
            else:
                self._history_issue(payload, start, end, "circuit_open")
                return None
        for attempt in range(2):
            service = getattr(self, "before_market_request", None)
            if service:
                service()
            time.sleep(max(0, 0.65 - (time.monotonic() - self.history_request_at)))
            self.history_request_at = time.monotonic()
            retry = False
            try:
                response = self.session.post(f"{self.BASE_URL}/charts/{endpoint}", json=request, timeout=(5, 20))
                if response.status_code != 200:
                    retry = response.status_code == 429 or response.status_code >= 500
                    detail = {"http_status": response.status_code}
                    try:
                        error = response.json()
                        detail.update({k: error[k] for k in ("errorCode", "errorType") if k in error})
                    except ValueError:
                        pass
                    kind = "http_error"
                else:
                    raw = response.json()
                    data = raw.get("data", raw)
                    if endpoint == "rollingoption":
                        data = data.get("ce" if payload["drvOptionType"] == "CALL" else "pe")
                    validated = validate(data)
                    self.history_failure_streak = 0
                    self.history_circuit_opened_at=None
                    issue_count = len(self.history_events)
                    record(validated,"network")
                    if use_cache and len(self.history_events) == issue_count:
                        self._history_save(str(path), {"version": CACHE_VERSION, "request_key": key,
                            "received_epoch": time.time(), "data": validated})
                    return validated
            except requests.RequestException as error:
                kind, detail, retry = "request_error", {"error_type": type(error).__name__}, True
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                known = {"empty","empty_range","unequal_arrays","bad_timestamps","nonfinite","bad_ohlc","bad_volume","bad_strike"}
                validation = error.args[0] if error.args and isinstance(error.args[0],str) and error.args[0] in known else "invalid_json_or_response"
                kind, detail = "invalid_or_empty_data", {"error_type": type(error).__name__, "validation_reason":validation}
            if retry and attempt == 0:
                time.sleep(2)
                continue
            self.history_failure_streak = self.history_failure_streak + 1 if retry else 0
            if self.history_failure_streak>=3:self.history_circuit_opened_at=time.monotonic()
            self._history_issue(payload, start, end, kind, **detail)
            return None

    def fetch_intraday_candles(self, security_id, exchange_segment="NSE_EQ", instrument="EQUITY",
                               interval="5", from_date=None, to_date=None, use_cache=False):
        now = datetime.now(IST)
        start = boundary(from_date, (now-timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0))
        end = boundary(to_date, now)
        if end <= start:
            raise ValueError("Historical end must follow start")
        fields = ("timestamp", "open", "high", "low", "close", "volume")
        return self._history_window("intraday", {"securityId": str(security_id), "exchangeSegment": exchange_segment,
            "instrument": instrument, "interval": str(interval)}, start, end, fields, self.history_cache_dir, use_cache)

    def fetch_rolling_stock_option(self, eq_security_id, opt_type, from_date, to_date, interval="5"):
        start, end = boundary(from_date, None), boundary(to_date, None)
        if start >= end or start.time().replace(tzinfo=None).isoformat() != "00:00:00" or end.time().replace(tzinfo=None).isoformat() != "00:00:00":
            raise ValueError("Rolling history requires increasing, midnight date boundaries")
        payload = {"securityId": int(eq_security_id), "exchangeSegment": "NSE_FNO", "instrument": "OPTSTK",
            "interval": str(interval), "expiryFlag": "MONTH", "expiryCode": 1, "strike": "ATM",
            "drvOptionType": "CALL" if opt_type == "CE" else "PUT",
            "requiredData": ["open", "high", "low", "close", "strike"]}
        fields = ("timestamp", "open", "high", "low", "close", "strike")
        merged = {k: [] for k in fields}
        while start < end:
            finish = min(start+timedelta(days=5), end)
            data = self._history_window("rollingoption", payload, start, finish, fields, self.history_cache_dir, True)
            if data:
                for k in fields:
                    merged[k].extend(data[k])
            start = finish
        return merged if merged["timestamp"] else None


    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: str, access_token: str, logger: logging.Logger):
        self.client_id = client_id
        self.access_token = access_token
        self.logger = logger
        self.last_chart_ts = 0.0
        self.last_quote_ts = 0.0
        self.last_chain_ts = 0.0
        self.before_market_request = None
        self.history_events = []
        self.history_coverage = []
        self.history_failure_streak = 0
        self.history_request_at = 0.0
        self.history_cache_dir = CACHE_DIR
        self._history_save = atomic_write_json
        self.session = requests.Session()
        self.session.headers.update({
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def fetch_batch_ohlc(self, eq_security_ids: List[str], fno_security_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Dict[str, List[int]] = {}
        if eq_security_ids:
            payload["NSE_EQ"] = [int(x) for x in eq_security_ids[:500]]
        if fno_security_ids:
            payload["NSE_FNO"] = [int(x) for x in fno_security_ids[:500]]
        if not payload:
            return {}
        self._pace_quote()
        try:
            resp = self.session.post(f"{self.BASE_URL}/marketfeed/ohlc", json=payload, timeout=12)
            if resp.status_code == 200:
                return resp.json().get("data", {})
            elif resp.status_code == 429:
                time.sleep(5.0)
            else:
                self.logger.error(f"OHLC Batch HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            self.logger.warning(f"OHLC Batch request error: {e}")
        return {}

    def _pace_quote(self):
        if self.before_market_request:
            self.before_market_request()
        time.sleep(max(0, 1.05 - (time.monotonic() - self.last_quote_ts)))
        self.last_quote_ts = time.monotonic()

    def fetch_option_chain(self, underlying_id, expiry):
        expiry = normalize_expiry(expiry)
        if not expiry:
            self.logger.warning("Invalid option expiry; skipping chain request")
            return {}
        if self.before_market_request:
            self.before_market_request()
        time.sleep(max(0, 3.05 - (time.monotonic() - self.last_chain_ts)))
        self.last_chain_ts = time.monotonic()
        try:
            response = self.session.post(f"{self.BASE_URL}/optionchain", json={
    "UnderlyingScrip": int(underlying_id), "UnderlyingSeg": "NSE_EQ", "Expiry": expiry}, timeout=12)
            if response.status_code == 200:
                chain = response.json().get("data", {}).get("oc", {})
                received = time.time()
                for node in chain.values():
                    for side in ("ce", "pe"):
                        if isinstance(node.get(side), dict):
                            node[side]["received_epoch"] = received
                return chain
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass
        self.logger.warning("Option chain unavailable; skipping Greek qualification")
        return {}

    def fetch_option_quotes(self, security_ids):
        if not security_ids:
            return {}
        self._pace_quote()
        try:
            response = self.session.post(f"{self.BASE_URL}/marketfeed/quote",
                json={"NSE_FNO": [int(s) for s in security_ids]}, timeout=12)
            if response.status_code == 200:
                quotes = response.json().get("data", {}).get("NSE_FNO", {})
                received = time.time()
                for quote in quotes.values():
                    quote["received_epoch"] = received
                return quotes
            self.logger.warning("Option depth unavailable: HTTP %s", response.status_code)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            self.logger.warning("Option depth unavailable; skipping entry/valuation")
        return {}

    def place_option_super_order(
        self,
        trading_symbol: str,
        opt_security_id: str,
        qty: int,
        entry_price: float,
        target_price: float,
        sl_price: float,
        trail_jump: float,
    ) -> Optional[str]:
        """Places an atomic BUY Super Order for the Stock Option (`NSE_FNO` -> `OPTSTK`)."""
        payload = {
            "dhanClientId": self.client_id,
            "correlationId": f"OPTSTK_{int(time.time())}",
            "transactionType": "BUY",
            "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY",
            "orderType": "LIMIT",
            "securityId": str(opt_security_id),
            "quantity": int(qty),
            "price": float(entry_price),
            "targetPrice": float(target_price),
            "stopLossPrice": float(sl_price),
            "trailingJump": float(trail_jump),
        }
        try:
            resp = self.session.post(f"{self.BASE_URL}/super/orders", json=payload, timeout=10)
            if resp.status_code in (200, 201, 202):
                oid = resp.json().get("orderId")
                self.logger.info(
                    f"[LIVE OPTSTK SUPER ORDER] BUY {qty}x {trading_symbol} (SecID:{opt_security_id}) -> OrderID:{oid}"
                )
                return str(oid) if oid else None
            else:
                self.logger.error(f"OPTSTK Super Order HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            self.logger.error(f"OPTSTK Super Order error: {e}")
        return None


def shortlist_movers(quotes, cfg):
    """Preserve the existing ScanX momentum shortlist: top eight each side."""
    bulls, bears = [], []
    for sec_id, quote in quotes.items():
        ltp = float(quote.get("last_price", 0) or 0)
        prev = float(quote.get("ohlc", {}).get("close", 0) or 0)
        if ltp <= 25 or prev <= 0:
            continue
        change = 100 * (ltp / prev - 1)
        if change >= cfg.min_day_change_pct:
            bulls.append((change, str(sec_id), ltp))
        elif change <= -cfg.min_day_change_pct:
            bears.append((change, str(sec_id), ltp))
    return sorted(bulls, reverse=True)[:8] + sorted(bears)[:8]


def verify_live_scanx_and_resolve_stock_option(client, stock_info, spot_ltp, day_change_pct, cfg, reasons=None):
    def reject(reason):
        if reasons is not None:
            reasons.append(reason)
        return None
    candles = client.fetch_intraday_candles(stock_info["eq_security_id"], interval="5")
    if not candles:
        return reject("Underlying candle history unavailable")
    sig = evaluate_stock_setup(candles, time.time(), cfg.min_day_change_pct, cfg.ema_period,
                               enable_opening_trend=cfg.enable_opening_trend,reasons=reasons,
                               confirm_window_sec=cfg.confirm_window_sec)
    if not sig:
        if reasons:return None
        return reject("No fresh confirmed momentum setup (EMA/VWAP/volume/body/proximity)")
    current = client.fetch_batch_ohlc([stock_info["eq_security_id"]]).get("NSE_EQ", {}).get(str(stock_info["eq_security_id"]), {})
    spot_ltp = float(current.get("last_price", 0) or 0)
    if spot_ltp <= 0:
        return reject("Current underlying quote unavailable")
    # Require the current underlying quote to hold confirmation, without chasing.
    direction = 1 if sig["opt_type"] == "CE" else -1
    advance = direction * (spot_ltp - sig["conf_close"])
    if advance < -0.15 * sig["atr_5m"] or advance > sig["atr_5m"]:
        return reject(f"Underlying follow-through {advance:.2f} outside {-0.15*sig['atr_5m']:.2f} to {sig['atr_5m']:.2f}")
    chain = client.fetch_option_chain(stock_info["eq_security_id"], stock_info["nearest_expiry"])
    if not chain:
        return reject(f"Option chain unavailable for expiry {stock_info['nearest_expiry']}")
    contract = DynamicStockOptionsUniverse.resolve_atm_option(stock_info, spot_ltp, sig["opt_type"], chain, cfg)
    if not contract:
        return reject("No master-matched option with valid broker Greeks in the delta band")
    return {**sig, **contract, "spot_ltp": spot_ltp, "eq_security_id": stock_info["eq_security_id"]}


def qualify_quote(sig, quote, cfg, wallet, now_epoch=None, reasons=None):
    """Qualify executable depth; optionally append the first rejection reason."""
    def reject(reason):
        if reasons is not None:
            reasons.append(reason)
        return None
    try:
        now_epoch = time.time() if now_epoch is None else now_epoch
        window = getattr(cfg, "confirm_window_sec", 600)
        age_after_close = now_epoch - (float(sig["signal_timestamp"]) + 300)
        if not 0 <= age_after_close < window:
            return reject(f"Confirmation candle is stale or not yet tradable (age {age_after_close:.0f}s, window {window}s)")
        change = float(sig.get("day_change_pct", 0))
        if not math.isfinite(change):
            return reject("Non-finite underlying day change")
        strong_mover = abs(change) >= 6
        if not 0 <= quote_received_age(quote, now_epoch) <= 30:
            return reject("Option snapshot older than 30s or future-dated")
        bid_node, ask_node = quote["depth"]["buy"][0], quote["depth"]["sell"][0]
        bid, ask = float(bid_node["price"]), float(ask_node["price"])
        qty = int(sig["lot_size"])
        if not all(math.isfinite(v) for v in (bid, ask, wallet)) or not 0 < bid <= ask:
            return reject("Unusable bid/ask or wallet")
        if not 3 <= ask <= cfg.max_premium:
            return reject(f"Premium {ask:.2f} outside 3-{cfg.max_premium:.0f}")
        if min(int(bid_node["quantity"]), int(ask_node["quantity"])) < qty:
            return reject("Displayed depth below one lot")
        spread = (ask - bid) / ask * 100
        spread_limit = cfg.max_spread_pct + (1.0 if strong_mover else 0.0)
        if spread > spread_limit:
            return reject(f"Spread {spread:.2f}% above {spread_limit:.2f}%")
        if cfg.extend_rupee_target:
            # Feed the same option-move estimate into sizing. A small first lot
            # must not veto a larger affordable lot count that fits both the
            # target-move estimate and the unchanged cash-risk ceiling.
            delta,theta,gamma=(float(sig[k]) for k in ('delta','theta','gamma'))
            move=1.8*float(sig['atr_5m'])
            decay=max(0,-theta)*cfg.max_hold_bars_5m*5/(24*60)
            premium_move=abs(delta)*move+0.5*gamma*move*move-decay
            # Strong movers use the explicit 55% scenario screen below; the
            # cash target, full planned RR and cash-risk cap remain mandatory.
            plan=cfg.plan(ask,qty,max_target_price=None if strong_mover else ask+premium_move)
        else:
            plan = cfg.plan(ask, qty)
        if not plan:
            return reject(f"No lot count reaches Rs.{cfg.rupee_target:.0f} within risk/RR limits")
        qty = plan["qty"]
        if min(int(bid_node["quantity"]), int(ask_node["quantity"])) < qty:
            return reject(f"Displayed depth below {plan['lots']} lots")
        if bid <= plan["initial_sl"]:
            return reject("Bid already at or below the planned stop")
        if ask * qty + plan["estimated_costs"] > wallet:
            return reject("Insufficient wallet for the planned size")
        delta, theta, gamma, iv = (float(sig[k]) for k in ("delta", "theta", "gamma", "iv"))
        if not all(math.isfinite(v) for v in (delta, theta, gamma, iv)):
            return reject("Non-finite broker Greeks")
        if not cfg.min_delta <= abs(delta) <= cfg.max_delta:
            return reject(f"Delta {abs(delta):.2f} outside {cfg.min_delta:.2f}-{cfg.max_delta:.2f}")
        if gamma <= 0 or iv <= 0:
            return reject("Non-positive gamma or IV")
        if (delta > 0) != (sig["opt_type"] == "CE"):
            return reject("Delta sign disagrees with option type")
        if not 0 <= now_epoch-sig.get("greeks_received_epoch", sig["greeks_timestamp"]) <= 120:
            return reject("Greek snapshot received over 120s ago or future-dated")
        dte = (datetime.strptime(sig["expiry"], "%Y-%m-%d").date() - datetime.fromtimestamp(now_epoch, IST).date()).days
        if dte < cfg.min_days_to_expiry:
            return reject(f"Only {dte} days to expiry, need {cfg.min_days_to_expiry}")
        theta_pct = max(0, -theta)/ask*100
        if theta_pct > cfg.max_theta_pct_per_day:
            return reject(f"Theta {theta_pct:.2f}%/day above {cfg.max_theta_pct_per_day:.2f}%")
        decay = max(0, -theta) * cfg.max_hold_bars_5m * 5 / (24*60)
        if decay*qty >= cfg.rupee_target:
            return reject(f"Decay over hold Rs.{decay*qty:.0f} exceeds the Rs.{cfg.rupee_target:.0f} target")
        move = 1.8 * float(sig["atr_5m"])
        expected_gain = abs(delta)*move + 0.5*gamma*move*move - decay
        net_gain = expected_gain*qty - plan["target_estimated_costs"]
        minimum_fraction=0.55 if strong_mover else (1.0 if cfg.extend_rupee_target else 0.75)
        required_gain=max(cfg.rupee_target,plan.get('net_profit_target',cfg.rupee_target))*minimum_fraction
        if net_gain < required_gain:
            return reject(f"A 1.8 ATR move nets only Rs.{net_gain:.0f} of the required Rs.{required_gain:.0f} ({minimum_fraction:.0%} minimum)")
        # Cross-sectional IV range in the same expiry/side, not historical IV rank.
        low, high = float(sig["iv_range_low"]), float(sig["iv_range_high"])
        if not all(math.isfinite(v) for v in (low, high)) or high <= low:
            return reject("Unusable chain IV range")
        iv_limit = 0.9 if strong_mover else 0.8
        if (iv-low)/(high-low) >= iv_limit:
            return reject(f"IV in the top {100*(1-(iv-low)/(high-low)):.0f}% of the chain")
        try:
            last_trade_age = now_epoch - datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST).timestamp()
        except (KeyError, TypeError, ValueError):
            last_trade_age = None
        return {**sig, **plan, "opt_ltp": ask, "bid": bid, "spread_pct": spread,
                "quoted_spread_points": ask-bid,
                "spread_fraction_of_initial_risk": (ask-bid)/(ask-plan["initial_sl"]),
                "bid_room_to_initial_stop": bid-plan["initial_sl"],
                "dte": dte, "theta_pct_per_day": theta_pct, "expected_move_gain": net_gain,
                "strong_mover": strong_mover, "spread_limit_pct": spread_limit,
                "confirmation_age_seconds": age_after_close, "confirm_window_sec": window,
                "expected_gain_fraction": minimum_fraction, "iv_range_limit": iv_limit,
                "quote_age_seconds": quote_received_age(quote, now_epoch),
                "last_trade_age_seconds": last_trade_age,
                "greeks_receipt_age_seconds": now_epoch-sig.get("greeks_received_epoch",sig["greeks_timestamp"])}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        return reject(f"Malformed quote or Greek data ({type(error).__name__})")


def update_ledger_metrics(ledger):
    trades = ledger["trades"]
    ledger["total_trades"] = len(trades)
    for prefix, key in (("", "gross_pnl"), ("net_", "net_pnl")):
        wins = [float(t[key]) for t in trades if float(t[key]) > 0]
        losses = [-float(t[key]) for t in trades if float(t[key]) < 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        ledger[prefix + "avg_winning_value_rs"] = round(avg_win, 2)
        ledger[prefix + "avg_losing_value_rs"] = round(avg_loss, 2)
        ledger[prefix + "win_to_loss_ratio"] = round(avg_win / avg_loss, 2) if avg_loss else None
    for balance, pnl, key in (("virtual_balance_gross", "profit_without_charges", "gross_pnl"),
                              ("virtual_balance_net", "net_profit_after_charges", "net_pnl")):
        ledger[pnl] = round(sum(float(t[key]) for t in trades), 2)
        ledger[balance] = round(ledger["initial_capital"] + ledger[pnl], 2)


def close_paper_position(ledger, pos, price, reason, now):
    qty = pos["qty"]
    gross = round((price - pos["entry_price"]) * qty, 2)
    fees = compute_fno_option_charges(pos["entry_price"], price, qty)
    rec = {k: pos[k] for k in ("symbol", "contract", "opt_type", "scanner", "qty", "entry_price")}
    rec.update(timestamp=pos["entry_ts"], exit_timestamp=now.strftime("%Y-%m-%d %H:%M:%S"),
               lots=pos.get("lots", 1), exit_price=price, gross_pnl=gross, charges=fees, net_pnl=round(gross-fees, 2),
               r_multiple=round(gross / (pos["opt_1r"] * qty), 2), exit_reason=reason,
               data_source="PAPER_BID_ASK", entry_id=pos["entry_id"])
    rec['learning_risk_rs']=round(float(pos.get('expected_net_risk',pos['opt_1r']*qty+fees)),2)
    rec['learning_r_multiple']=round(rec['net_pnl']/rec['learning_risk_rs'],6) if rec['learning_risk_rs']>0 else None
    # Preserve decision-time evidence when the daily gate closes its position.
    # These fields are observations, not relaxed entry criteria or new signals.
    rec['entry_diagnostics']={k:pos[k] for k in (
        'signal_timestamp','conf_close','conf_high','conf_low','atr_5m','day_change_pct',
        'ema10_15m','ema10_15m_slope','higher_timeframe_aligned',
        'vwap','bid','opt_ltp','spread_pct','quoted_spread_points',
        'spread_fraction_of_initial_risk','bid_room_to_initial_stop',
        'initial_sl','rupee_target_price','expected_net_rr','expected_net_risk',
        'delta','theta','gamma','iv','iv_range_low','iv_range_high','dte',
        'quote_age_seconds','greeks_receipt_age_seconds','confirmation_age_seconds') if k in pos}
    # Idempotent if interrupted between ledger save and closing the daily record.
    if not any(t.get("entry_id") == pos["entry_id"] for t in ledger["trades"]):
        ledger["trades"].append(rec)
        update_ledger_metrics(ledger)
        atomic_write_json(LEDGER_FILE, ledger)
    return rec


def paper_exit(pos, quote, now, cfg):
    try:
        bid = float(quote["depth"]["buy"][0]["price"])
        size = int(quote["depth"]["buy"][0]["quantity"])
        if not math.isfinite(bid) or bid <= 0 or size < pos["qty"] or not 0 <= quote_received_age(quote, now.timestamp()) <= 30:
            return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    elapsed = now.timestamp() - pos["entry_epoch"]
    pos["peak_bid"] = max(pos.get("peak_bid", pos["entry_price"]), bid)
    jump = pos["trail_jump"]
    if jump > 0 and pos["peak_bid"] - pos["entry_price"] >= 2 * jump:
        pos["current_sl"] = max(pos["current_sl"], round_tick(pos["peak_bid"]-jump))
    price = math.floor(max(0.05, bid * (1 - cfg.slippage_pct/100))*20 + 1e-9)/20
    net = (price-pos["entry_price"])*pos["qty"] - compute_fno_option_charges(pos["entry_price"], price, pos["qty"])
    reason = None
    if bid <= pos["current_sl"]:
        reason = "TRAIL_STOP" if pos["current_sl"] > pos["initial_sl"] else "PREMIUM_STOP"
    elif bid >= pos["rupee_target_price"]:
        reason = "RUPEE_TARGET"
    elif bid >= pos["hard_target_price"]:
        reason = "PREMIUM_TARGET"
    elif now.time().replace(tzinfo=None) >= SESSION_END_TIME:
        reason = "EOD_SQUAREOFF"
    elif elapsed >= cfg.no_progress_bars * 300 and pos["peak_bid"] < pos["entry_price"] + jump:
        reason = "NO_PROGRESS"
    elif elapsed >= cfg.max_hold_bars_5m * 300:
        reason = "TIME_EXIT"
    return (price, reason) if reason else None


def run_dynamic_stock_options_session(live_mode, scan_once, cfg, logger):
    env = load_dotenv_file(".env")
    if not env.get("DHAN_CLIENT_ID") or not env.get("DHAN_ACCESS_TOKEN"):
        raise ValueError("Missing Dhan credentials in .env")
    client = DhanStockOptionsClient(env["DHAN_CLIENT_ID"], env["DHAN_ACCESS_TOKEN"], logger)
    universe = DynamicStockOptionsUniverse.load_universe(logger, cfg.min_days_to_expiry)
    gate = DailyTradeGate(cfg.gate_dir, cfg.max_daily_trades)
    ledger = None if scan_once else load_or_init_ledger(live_mode, cfg.initial_capital)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    positions = []
    for slot in range(1, cfg.max_daily_trades + 1):
        saved = None if scan_once else gate.read(today, slot)
        if not saved or saved.get("state") == "closed":
            continue
        details = saved.get("details", {})
        position = details.get("position")
        if saved.get("state") != "reserved" or not position or details.get("mode") != "PAPER" or live_mode:
            logger.warning("Unresolved reservation; reconcile broker/ledger before new entries.")
            return
        position["slot"] = slot
        if any(t.get("entry_id") == position["entry_id"] for t in ledger["trades"]):
            gate.mark_closed(today, {"recovered_from_ledger": True}, slot)
        else:
            positions.append(position)
    telegram = TelegramNotifier(env, logger, "LIVE" if live_mode else "PAPER") if TelegramNotifier and not scan_once else None
    logger.info("Stock options: max %d/day | net target Rs.%.0f | estimated risk cap Rs.%.0f",
                cfg.max_daily_trades, cfg.rupee_target, cfg.rupee_max_risk)
    logger.info("Entry profile: gap continuation %s | cash-target extension %s | maximum %d lots",
                cfg.enable_opening_trend,cfg.extend_rupee_target,cfg.max_lots)
    logger.info("Market quote refresh: %.1fs; position checks also run between screening requests",
                cfg.poll_interval_sec)
    market_quotes = {}
    next_refresh = 0.0
    servicing = False

    def service_market_data():
        # Synchronous cooperation keeps ledger/order ownership on this thread.
        # A slow HTTP request can exceed the cadence; never overlap requests to
        # try to catch up. The guard prevents quote pacing from recursing here.
        nonlocal market_quotes, next_refresh, servicing
        if servicing or time.monotonic() < next_refresh:
            return
        servicing = True
        next_refresh = time.monotonic() + cfg.poll_interval_sec
        try:
            now = datetime.now(IST)
            if now.strftime("%Y-%m-%d") != today or now.weekday() >= 5 or (now.time().replace(tzinfo=None) >= SESSION_END_TIME and not positions):
                return
            if positions:
                quotes = client.fetch_option_quotes([p["opt_security_id"] for p in positions])
                # Receipt timestamps are stamped after HTTP completes. Using
                # the pre-request time would give fresh depth a negative age.
                now = datetime.now(IST)
                for pos in positions[:]:
                    decision = paper_exit(pos, quotes.get(str(pos["opt_security_id"]), {}), now, cfg)
                    if decision:
                        rec = close_paper_position(ledger, pos, *decision, now)
                        gate.mark_closed(today, rec, pos["slot"])
                        positions.remove(pos)
                        logger.info("[PAPER OPTSTK EXIT] %s | %s | net Rs.%+.2f", pos["contract"], decision[1], rec["net_pnl"])
                        if telegram:
                            telegram.exited(rec)
                    else:
                        state = (pos["current_sl"], pos["peak_bid"])
                        if state != tuple(pos.get("persisted_state") or ()):
                            pos["persisted_state"] = state
                            gate.update_position(today, pos["slot"], pos)
            if SESSION_START_TIME <= now.time().replace(tzinfo=None) < SESSION_END_TIME:
                # Clear a failed refresh instead of reusing an older scan quote.
                market_quotes = client.fetch_batch_ohlc(list(universe)).get("NSE_EQ", {})
        finally:
            servicing = False

    client.before_market_request = service_market_data
    try:
        while True:
            now = datetime.now(IST)
            if now.strftime("%Y-%m-%d") != today or now.weekday() >= 5 or (now.time().replace(tzinfo=None) >= SESSION_END_TIME and not positions):
                return
            service_market_data()
            completed = [] if scan_once else [t for t in ledger["trades"] if str(t.get("timestamp", "")).startswith(today)]
            consumed = 0 if scan_once else max(gate.count_entries(today), len(completed)+len(positions))
            if consumed >= cfg.max_daily_trades or len(positions) >= cfg.max_concurrent:
                if not positions:
                    return
                time.sleep(max(0.05, next_refresh-time.monotonic()))
                continue
            if not SESSION_START_TIME <= now.time().replace(tzinfo=None) < SESSION_END_TIME:
                if scan_once or (now.hour >= 14 and not positions):
                    return
                time.sleep(cfg.poll_interval_sec)
                continue
            wallet = cfg.initial_capital if scan_once else ledger["virtual_balance_net"]
            wallet -= sum(p["entry_price"]*p["qty"] + p["estimated_costs"] for p in positions)
            shortlist = shortlist_movers(market_quotes, cfg)
            signals = []
            candidates = []
            used_symbols = {p["symbol"] for p in positions}
            if not cfg.reentry_after_loss:used_symbols|={t['symbol'] for t in completed}
            for change, sec_id, ltp in shortlist:
                service_market_data()
                if datetime.now(IST).strftime("%Y-%m-%d") != today or datetime.now(IST).time().replace(tzinfo=None) >= SESSION_END_TIME:
                    break
                if sec_id not in universe or universe[sec_id]["symbol"] in used_symbols:
                    continue
                if cfg.reentry_after_loss:
                    reason=stock_reentry_reason(universe[sec_id]['symbol'],completed,time.time())
                    if reason:
                        logger.info('[OPTSTK REENTRY WAIT] %s | %s',universe[sec_id]['symbol'],reason)
                        continue
                signal_reasons = []
                sig = verify_live_scanx_and_resolve_stock_option(client, universe[sec_id], ltp, change, cfg, reasons=signal_reasons)
                if sig:
                    if cfg.reentry_after_loss:
                        reason=stock_reentry_reason(sig['symbol'],completed,time.time(),sig['signal_timestamp'])
                        if reason:
                            logger.info('[OPTSTK REENTRY WAIT] %s | %s',sig['symbol'],reason)
                            continue
                    signals.append(sig)
                    depth = client.fetch_option_quotes([sig["opt_security_id"]])
                    reasons = []
                    qualified = qualify_quote(sig, depth.get(str(sig["opt_security_id"]), {}), cfg, wallet, reasons=reasons)
                    if qualified:
                        candidates.append(qualified)
                    else:
                        logger.info("[OPTSTK REJECT] %s %s | %s", sig["symbol"], sig["opt_type"], reasons[0] if reasons else "Unspecified rejection")
                else:
                    logger.info("[OPTSTK REJECT] %s | %s", universe[sec_id]["symbol"], signal_reasons[0] if signal_reasons else "Setup unavailable")
            ranked = rank_candidates(candidates,ledger.get('trades',[]) if ledger else [],time.time())
            logger.info("Shortlist %d | confirmed %d | qualified %d", len(shortlist), len(signals), len(ranked))
            if scan_once:
                return
            capacity = min(cfg.max_concurrent-len(positions), cfg.max_daily_trades-consumed)
            entered = 0
            for best in ranked:
                service_market_data()
                # Screening can now close a position. Recalculate cash and
                # reserved slots before qualification and never reuse its funds
                # or its symbol based on a stale scan-start snapshot.
                completed = [t for t in ledger["trades"] if str(t.get("timestamp", "")).startswith(today)]
                consumed = max(gate.count_entries(today), len(completed)+len(positions))
                if consumed >= cfg.max_daily_trades or len(positions) >= cfg.max_concurrent:
                    break
                if any(p["symbol"] == best["symbol"] for p in positions):
                    continue
                if cfg.reentry_after_loss:
                    if stock_reentry_reason(best["symbol"], completed, time.time(), best.get("signal_timestamp")):
                        continue
                elif any(t["symbol"] == best["symbol"] for t in completed):
                    continue
                wallet = ledger["virtual_balance_net"] - sum(p["entry_price"]*p["qty"] + p["estimated_costs"] for p in positions)
                if entered >= capacity:
                    break
                current = client.fetch_batch_ohlc([best["eq_security_id"]]).get("NSE_EQ", {}).get(str(best["eq_security_id"]), {})
                spot = float(current.get("last_price", 0) or 0)
                advance = (1 if best["opt_type"] == "CE" else -1)*(spot-best["conf_close"])
                if spot <= 0 or not -0.15*best["atr_5m"] <= advance <= best["atr_5m"]:
                    logger.info("[OPTSTK RECHECK REJECT] %s | Spot %.2f, follow-through %.2f outside %.2f to %.2f",
                                best["symbol"],spot,advance,-0.15*best["atr_5m"],best["atr_5m"])
                    continue
                # Ranking may have taken minutes. Rebuild the candidate from current
                # closed candles and a newly received chain before any reservation.
                signal_reasons = []
                best = verify_live_scanx_and_resolve_stock_option(client, universe[best["eq_security_id"]], spot, 0, cfg, reasons=signal_reasons)
                if not best:
                    logger.info("[OPTSTK RECHECK REJECT] %s", signal_reasons[0] if signal_reasons else "Setup no longer current")
                    continue
                fresh = client.fetch_option_quotes([best["opt_security_id"]])
                reasons = []
                symbol = best["symbol"]
                best = qualify_quote(best, fresh.get(str(best["opt_security_id"]), {}), cfg, wallet, reasons=reasons)
                if not best:
                    logger.info("[OPTSTK RECHECK REJECT] %s | %s", symbol, reasons[0] if reasons else "Unspecified rejection")
                now = datetime.now(IST)
                if not best or now.time().replace(tzinfo=None) >= SESSION_END_TIME:
                    continue
                refreshed=rank_candidates([best],ledger.get('trades',[]),now.timestamp())
                if not refreshed:
                    logger.info("[OPTSTK LEARNING PAUSE] %s | Recent closed net losses for this setup",symbol)
                    continue
                best=refreshed[0]
                slot = next((i for i in range(1, cfg.max_daily_trades+1) if gate.read(today, i) is None), None)
                if slot is None:
                    break
                pos = {**best, "index": best["symbol"], "option_type": best["opt_type"], "slot": slot,
                    "contract": f"{best['symbol']} {best['strike']:g}_{best['opt_type']}",
                    "entry_price": best["opt_ltp"], "current_ltp": best["opt_ltp"], "peak_bid": best["opt_ltp"],
                    "current_sl": best["initial_sl"], "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_epoch": now.timestamp(), "entry_id": f"STOCK_{today}_{slot}"}
                if not gate.reserve(today, {"mode": "LIVE" if live_mode else "PAPER", "position": pos}, slot):
                    return
                entered += 1
                wallet -= pos["entry_price"]*pos["qty"] + pos["estimated_costs"]
                if live_mode:
                    oid = client.place_option_super_order(best["trading_symbol"], best["opt_security_id"],
                        best["qty"], best["opt_ltp"], best["rupee_target_price"], best["initial_sl"], best["trail_jump"])
                    logger.info("LIVE submission %s; slot consumed. Broker bracket owns exits; verify fills in Dhan.", oid or "unconfirmed")
                    # Ambiguous submissions must never trigger another order.
                    if not oid:
                        return
                else:
                    positions.append(pos)
                    logger.info("[PAPER OPTSTK ENTRY] %s | %d lots @ %.2f", pos["contract"], pos["lots"], pos["entry_price"])
                    if telegram:
                        telegram.position(pos, entry=True)
            if live_mode and gate.count_entries(today):
                return
            time.sleep(max(0.05, next_refresh-time.monotonic()))
    finally:
        client.before_market_request = None
        if telegram:
            telegram.close()


def simulate_fixed_premium_trade(entry_ts, bars, qty, cfg):
    """Real OHLC only. Reject missing bars/rolling-strike changes; stop first on ties."""
    if not SESSION_START_TIME <= datetime.fromtimestamp(entry_ts, IST).time().replace(tzinfo=None) < SESSION_END_TIME:
        return None
    first = bars.get(entry_ts)
    if not first or first.get("strike", 0) <= 0 or not 3 <= first.get("open", 0) <= cfg.max_premium:
        return None
    entry = round_tick(first["open"] * (1 + cfg.slippage_pct / 100))
    plan = cfg.plan(entry, qty)
    if not plan:
        return None
    stop, peak = plan["initial_sl"], entry
    target = min(plan["hard_target_price"], plan["rupee_target_price"])
    for offset in range(cfg.max_hold_bars_5m):
        ts = entry_ts + offset * 300
        bar = bars.get(ts)
        if not bar or bar.get("strike") != first["strike"]:
            return None
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if not all(math.isfinite(x) and x > 0 for x in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
            return None
        price, reason = None, None
        # Existing stop first; a newly raised trail only applies to following bars.
        # OHLC does not establish whether the high preceded the low.
        if o <= stop:
            price, reason = o, "TRAIL_STOP" if stop > plan["initial_sl"] else "PREMIUM_STOP"
        elif o >= target:
            price, reason = target, "RUPEE_TARGET"
        elif l <= stop:
            price, reason = min(o, stop), "TRAIL_STOP" if stop > plan["initial_sl"] else "PREMIUM_STOP"
        elif h >= target:
            price, reason = target, "RUPEE_TARGET"
        elif datetime.fromtimestamp(ts+300, IST).time().replace(tzinfo=None) >= SESSION_END_TIME:
            price, reason = c, "EOD_SQUAREOFF"
        else:
            peak = max(peak, h)
            if offset+1 >= cfg.no_progress_bars and peak < entry+plan["trail_jump"]:
                price, reason = c, "NO_PROGRESS"
            elif offset == cfg.max_hold_bars_5m-1:
                price, reason = c, "TIME_EXIT"
            if peak-entry >= 2*plan["trail_jump"]:
                stop = max(stop, round_tick(peak-plan["trail_jump"]))
        if reason:
            # Floor fills to avoid rounding away the modeled adverse slippage.
            price = math.floor(max(0.05,price*(1-cfg.slippage_pct/100))*20 + 1e-9)/20
            return {**plan, "entry_price": entry, "exit_price": price, "exit_reason": reason,
                "exit_timestamp": datetime.fromtimestamp(ts+300,IST).strftime("%Y-%m-%d %H:%M:%S"),
                "strike": first["strike"]}
    return None


def run_stock_options_historical_backtest(days, max_stocks, cfg, logger, current_implementation=False):
    env = load_dotenv_file(".env")
    client = DhanStockOptionsClient(env.get("DHAN_CLIENT_ID", ""), env.get("DHAN_ACCESS_TOKEN", ""), logger)
    universe = DynamicStockOptionsUniverse.load_universe(logger, cfg.min_days_to_expiry)
    # Deterministic coverage; do not use today's momentum to select past winners.
    stocks = sorted(universe.values(), key=lambda s: s["symbol"])
    if max_stocks > 0:
        stocks = stocks[:max_stocks]
    end = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    frames, snapshots, signals = {}, {}, {}
    for stock in stocks:
        sec = stock["eq_security_id"]
        candles = client.fetch_intraday_candles(sec, interval="5", from_date=(start-timedelta(days=5) if current_implementation else start).strftime("%Y-%m-%d"),
                                                to_date=end.strftime("%Y-%m-%d"), use_cache=True)
        # Evaluate available setups; retain coverage issues in the final report.
        if not candles or not candles.get("timestamp"):
            continue
        frames[sec] = stock
        last_day, prev_close, last_close = None, None, None
        for i, ts in enumerate(candles["timestamp"]):
            ts = int(ts)
            if ts < start.timestamp():
                if current_implementation:
                    last_day=datetime.fromtimestamp(ts+300,IST).date()
                    last_close=float(candles['close'][i])
                continue
            if ts+300 > end.timestamp():continue
            dt = datetime.fromtimestamp(ts+300, IST)
            day = dt.date()
            if day != last_day:
                prev_close, last_day = last_close, day
            last_close = float(candles["close"][i])
            if prev_close is None or not SESSION_START_TIME <= dt.time().replace(tzinfo=None) < SESSION_END_TIME:
                continue
            snapshots.setdefault(ts+300, {})[sec] = {"last_price": last_close, "ohlc": {"close": prev_close}}
            # Live scans shortlist by day move before evaluating candle history.
            if abs((last_close/prev_close-1)*100)<cfg.min_day_change_pct:continue
            # The current scanner requests five calendar days from midnight.
            # Do not give its replay an expanding multi-month EMA warmup.
            lower=bisect_left(candles['timestamp'],(dt-timedelta(days=5)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()) if current_implementation else 0
            prefix = {k: v[lower:i+1] for k, v in candles.items() if isinstance(v, list)}
            sig = evaluate_stock_setup(prefix, ts+300, cfg.min_day_change_pct, cfg.ema_period,
                                        enable_opening_trend=cfg.enable_opening_trend,confirm_window_sec=cfg.confirm_window_sec)
            if sig:
                signals[(ts+300, sec)] = sig
    ledger = {"mode": "BACKTEST", "initial_capital": cfg.initial_capital, "trades": [],
              "entry_configuration":{"opening_trend":cfg.enable_opening_trend,"extend_rupee_target":cfg.extend_rupee_target,
                  "rupee_target":cfg.rupee_target,"rupee_max_risk":cfg.rupee_max_risk,"max_lots":cfg.max_lots,
                  "min_net_rr":cfg.min_rr_target,"max_spread_pct":cfg.max_spread_pct,"confirm_window_sec":cfg.confirm_window_sec,
                  "signal_lookback_calendar_days":5 if current_implementation else None},
              "limitations": ["Current universe and lot sizes; historical membership not reconstructed",
                              "Five-minute OHLC lacks bid/ask and intrabar path; modeled slippage, not verified fills",
                              "Missing bars or rolling ATM strike changes are unvalidated, never synthetic",
                              "Exit-policy replay only: historical broker Greeks, IV range, DTE and delta-selected contracts unavailable",
                              "Trailing stops advance on following bars; same-bar trail path is unvalidated"]}
    used_days, option_maps, skipped = {}, {}, 0
    data_skips, strategy_rejections = Counter(), Counter()
    active_until = []
    used_symbols = {}
    for ts in sorted(snapshots):
        day = datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")
        active_until = [end_ts for end_ts in active_until if end_ts > ts]
        if used_days.get(day, 0) >= cfg.max_daily_trades or len(active_until) >= cfg.max_concurrent:
            continue
        candidates = []
        for _, sec, _ in shortlist_movers(snapshots[ts], cfg):
            sig = signals.get((ts, sec))
            if not sig:
                continue
            stock = frames[sec]
            if stock["symbol"] in used_symbols.get(day, set()):
                if not cfg.reentry_after_loss:continue
                history=[t for t in ledger['trades'] if t.get('symbol')==stock['symbol']]
                if not history or stock_reentry_reason(stock['symbol'],history,ts,sig['signal_timestamp']):continue
            # Fetch the execution date's five-day block, rather than every
            # option date in the study for each shortlisted symbol. Missing
            # unrelated dates must not exhaust retries before this entry.
            window_start = start + timedelta(days=((datetime.fromtimestamp(ts, IST)-start).days//5)*5)
            window_end = min(window_start+timedelta(days=5), end)
            key = (sec, sig["opt_type"], window_start.date().isoformat())
            if key not in option_maps:
                raw = client.fetch_rolling_stock_option(sec, sig["opt_type"], window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d"))
                option_maps[key] = {int(t): {k: float(raw[k][i] or 0) for k in ("open", "high", "low", "close", "strike")}
                                    for i, t in enumerate(raw["timestamp"])} if raw and all(k in raw for k in ("timestamp", "open", "high", "low", "close", "strike")) else {}
            bars = option_maps[key]
            first = bars.get(ts)
            if not first or first["strike"] <= 0:
                skipped += 1
                data_skips["option_history_unavailable" if not bars else "entry_bar_missing_or_invalid_strike"] += 1
                continue
            if not 3 <= first["open"] <= cfg.max_premium:
                strategy_rejections["premium_outside_limits"] += 1
                continue
            qty = stock["fno_lot_size"]
            entry = round_tick(first["open"] * (1 + cfg.slippage_pct / 100))
            plan = cfg.plan(entry, qty)
            wallet = cfg.initial_capital + sum(t["net_pnl"] for t in ledger["trades"]
                if datetime.strptime(t["exit_timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp() <= ts)
            wallet -= sum(t["entry_price"]*t["qty"] + t["estimated_costs"] for t in ledger["trades"]
                if datetime.strptime(t["exit_timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp() > ts)
            if not plan:
                strategy_rejections["no_risk_target_plan"] += 1
            elif entry*plan["qty"] + plan["estimated_costs"] > wallet:
                strategy_rejections["insufficient_wallet"] += 1
            if plan and entry*plan["qty"] + plan["estimated_costs"] <= wallet:
                candidates.append({**sig, **plan, "symbol": stock["symbol"], "lot_size": qty, "map_key": key})
        ranked = rank_candidates(candidates,ledger['trades'],ts)
        if not ranked:
            continue
        for best in ranked[:min(cfg.max_concurrent-len(active_until), cfg.max_daily_trades-used_days.get(day,0))]:
            entry = round_tick(option_maps[best["map_key"]][ts]["open"]*(1+cfg.slippage_pct/100))
            if entry*best["qty"] + best["estimated_costs"] > wallet:
                continue
            wallet -= entry*best["qty"] + best["estimated_costs"]
            # Consume date BEFORE looking at future outcome, including missing data.
            used_symbols.setdefault(day, set()).add(best["symbol"])
            active_until.append(ts + cfg.max_hold_bars_5m*300)
            used_days[day] = used_days.get(day, 0) + 1
            sim = simulate_fixed_premium_trade(ts, option_maps[best["map_key"]], best["lot_size"], cfg)
            if sim is None:
                data_skips["simulation_missing_invalid_bars_or_strike_change"] += 1
                skipped += 1
                continue
            active_until.pop()
            active_until.append(datetime.strptime(sim["exit_timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp())
            qty = sim["qty"]
            gross = round((sim["exit_price"]-sim["entry_price"])*qty, 2)
            fees = compute_fno_option_charges(sim["entry_price"], sim["exit_price"], qty)
            ledger["trades"].append({**sim, "symbol": best["symbol"], "opt_type": best["opt_type"],
                "contract": f"{best['symbol']} {sim['strike']:g}_{best['opt_type']}", "qty": qty, "lots": sim["lots"],
                "timestamp": datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S"),
                "gross_pnl": gross, "charges": fees, "net_pnl": round(gross-fees, 2),
                "scanner":best['scanner'],"learning_risk_rs":best.get('expected_net_risk',sim['opt_1r']*qty+fees),
                "data_source": "ROLLING_ATM_5M_UNVERIFIED_CONTRACT_IDENTITY"})
    update_ledger_metrics(ledger)
    ledger["unvalidated_candidates_or_outcomes"] = skipped
    ledger["stock_coverage"] = len(frames)
    events = client.history_events
    complete = not events and not skipped and len(frames) == len(stocks)
    identity_unverified = bool(ledger["trades"])
    ledger["limitations"].append("Five-minute rolling bars may disagree with one-minute prices for the labeled strike; scored contract prices require cross-interval verification")
    ledger.update(status="COMPLETED_EXIT_REPLAY" if complete and not identity_unverified else "INCOMPLETE_DATA",
                  data_coverage_complete=complete, full_strategy_validated=False,
                  price_identity_validated=False,
                  data_rejection_counts=dict(data_skips), strategy_rejection_counts=dict(strategy_rejections),
                  historical_data_issues=events, historical_data_coverage=client.history_coverage, stock_universe_requested=len(stocks),
                  period={"from_inclusive":start.isoformat(),"to_exclusive":end.isoformat()})
    atomic_write_json("stock_fno_backtest_ledger.json", ledger)
    if not complete or identity_unverified:
        logger.error("BACKTEST INCOMPLETE: %d history issues, %d unvalidated signals/outcomes; fixed-contract price identity verified: %s. Recorded P&L is provisional.",len(events),skipped,not identity_unverified)
    logger.info("Selective backtest: %d trades, %d unvalidated candidates/outcomes | net Rs.%+.2f | avg net win/loss ratio %s",
                ledger["total_trades"], skipped, ledger["net_profit_after_charges"], ledger["net_win_to_loss_ratio"])
    logger.info("Saved separate stock_fno_backtest_ledger.json. Replay does not validate historical Greeks, strike selection or intrabar execution.")
    return ledger


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Selective stock options: best confirmed shortlist setup, maximum two entries per day")
    parser.add_argument("--live", action="store_true", help="Submit capped rupee-target brackets with broker trailing stops; verify fills in Dhan")
    parser.add_argument("--scan-once", action="store_true", help="Rank current qualifying setups without trading")
    parser.add_argument("--backtest-days", type=int, default=0)
    parser.add_argument("--max-stocks", type=int, default=50, help="Historical coverage, alphabetically selected; 0 for all")
    parser.add_argument("--min-pct", type=float, default=None)
    parser.add_argument("--min-rr", type=float, default=None, help="Minimum estimated net reward/risk, at least 1.2")
    parser.add_argument("--reset-ledger", action="store_true", help="Reset stock paper ledger; does not reset daily entry limit")
    args = parser.parse_args()
    strat_cfg = DynamicStrategyConfig(load_dotenv_file(".env"), args)
    log = setup_logger()
    if args.reset_ledger and os.path.exists(LEDGER_FILE):
        os.remove(LEDGER_FILE)
    if args.backtest_days > 0:
        result = run_stock_options_historical_backtest(args.backtest_days, args.max_stocks, strat_cfg, log, current_implementation=True)
        if result["status"] == "INCOMPLETE_DATA":
            raise SystemExit(2)
    else:
        run_dynamic_stock_options_session(args.live, args.scan_once, strat_cfg, log)

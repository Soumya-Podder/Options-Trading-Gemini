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
import struct
import threading
from urllib.parse import urlencode
import argparse
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Dict, Any, List, Optional
import requests
from collections import Counter
from bisect import bisect_left
from pathlib import Path
from stock_option_signals import evaluate_stock_setup, scanx_stock_membership, recent_entry_momentum, order_flow_direction
from stock_option_policy import premium_plan, rank_candidates, DailyTradeGate, stock_reentry_reason, option_chain_request_slot, market_quote_request_slot, single_bot_session, session_open_wait_seconds

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_START_TIME = dtime(9, 15)  # Market data and opening-range reference
ENTRY_START_TIME = dtime(9, 20)
SCAN_END_TIME = dtime(13, 30)
SESSION_END_TIME = dtime(15, 15)

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
    else:
        raise OSError(f'Could not persist JSON after 10 replacement attempts: {filepath}')


def round_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def compute_fno_option_charges(buy_price: float, sell_price: float, qty: int, exit_orders=1) -> float:
    """Statutory Indian NSE_FNO Option Buying charges (Brokerage Rs.40 + STT + Exch + SEBI + Stamp + GST)."""
    buy_turn = buy_price * qty
    sell_turn = sell_price * qty
    tot_turn = buy_turn + sell_turn
    brokerage = 20.0 * (1 + exit_orders)
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
                if data.get("mode", "PAPER") != default_ledger["mode"]:
                    raise ValueError("Ledger mode mismatch; reconcile before starting")
                return data
        except (OSError,ValueError,TypeError) as error:
            raise ValueError("Existing ledger needs reconciliation; refusing to overwrite") from error
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
        self.max_daily_trades = int(env.get("STOCK_OPT_MAX_DAILY_TRADES", 4))
        self.max_consec_losses = int(env.get("MAX_CONSECUTIVE_LOSSES_PER_DAY", 2))
        self.daily_loss_limit = float(env.get("STOCK_OPT_DAILY_LOSS_LIMIT",3000))

        self.target_pct = float(env.get("STOCK_OPT_TARGET_PCT", 12.0))
        self.stop_pct = float(env.get("STOCK_OPT_STOP_PCT", 5.0))
        self.min_rr_target = float(getattr(args, "min_rr", None) or env.get("STOCK_OPT_MIN_NET_RR", 1.3))
        self.slippage_pct = float(env.get("STOCK_OPT_SLIPPAGE_PCT", 0.1))
        self.max_spread_pct = float(env.get("STOCK_OPT_MAX_SPREAD_PCT", 3.5))
        self.max_hold_bars_5m = int(env.get("STOCK_OPT_MAX_HOLD_BARS_5M", 12))
        self.rupee_target = float(env.get("STOCK_OPT_RUPEE_TARGET", 900))
        self.runner_target = float(env.get('STOCK_OPT_RUNNER_TARGET',5000))
        self.rupee_max_risk = float(env.get("STOCK_OPT_RUPEE_MAX_RISK", 1500))
        self.max_lots = int(env.get("STOCK_OPT_MAX_LOTS", 2))
        # User policy: use the current listed expiry, including expiry day.
        # A stale minimum-DTE override must never silently select next month.
        self.min_days_to_expiry = 0
        self.min_delta = float(env.get("STOCK_OPT_MIN_DELTA", 0.38))
        self.max_delta = float(env.get("STOCK_OPT_MAX_DELTA", 0.78))
        self.max_theta_pct_per_day = float(env.get("STOCK_OPT_MAX_THETA_PCT_PER_DAY", 6))
        self.no_progress_bars = int(env.get("STOCK_OPT_NO_PROGRESS_BARS", 3))
        self.max_premium = float(env.get("STOCK_OPT_MAX_PREMIUM", 1000))
        self.confirm_window_sec = int(env.get("STOCK_OPT_CONFIRM_WINDOW_SEC", 300))
        self.poll_interval_sec = float(env.get("STOCK_OPT_POLL_INTERVAL_SEC", 4))
        self.gate_dir = "stock_option_daily_entries"
        self.enable_opening_trend=env.get("STOCK_OPT_OPENING_TREND","false").strip().lower()=="true"
        self.extend_rupee_target=env.get("STOCK_OPT_EXTEND_RUPEE_TARGET","false").strip().lower()=="true"
        self.reentry_after_loss=env.get("STOCK_OPT_REENTRY_AFTER_LOSS","false").strip().lower()=="true"
        self.require_confirmed_breakout=env.get('STOCK_OPT_REQUIRE_CONFIRMED_BREAKOUT','false').strip().lower()=='true'
        self.staged_profit=env.get('STOCK_OPT_STAGED_PROFIT','false').strip().lower()=='true'
        self.runner_max_hold_minutes=float(env.get('STOCK_OPT_RUNNER_MAX_HOLD_MINUTES',90))
        numbers = (self.target_pct, self.stop_pct, self.min_rr_target, self.slippage_pct,
                   self.max_spread_pct, self.rupee_target, self.rupee_max_risk,
                   self.min_delta, self.max_delta, self.max_theta_pct_per_day, self.max_premium,
                   self.poll_interval_sec,self.runner_target,self.daily_loss_limit,self.runner_max_hold_minutes)
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError("Stock option settings must be finite")
        if not (3 <= self.target_pct <= 20 and 0.5 <= self.stop_pct <= 10 and self.min_rr_target >= 1.2
                and 0 <= self.slippage_pct < 100 and 0 < self.max_spread_pct <= 5
                and 1 <= self.max_concurrent <= min(2,self.max_daily_trades) and self.max_daily_trades <= 4
                and 1 <= self.max_consec_losses <= 4 and self.daily_loss_limit > 0
                and 0 < self.rupee_target <= self.runner_target and self.rupee_max_risk > 0 and 1<=self.max_lots<=3
                and 0 < self.min_delta < self.max_delta <= 1 and self.min_days_to_expiry == 0
                and self.max_theta_pct_per_day > 0 and self.max_premium >= 3
                and 300 <= self.confirm_window_sec <= 900
                and 3 <= self.poll_interval_sec <= 5
                and 1 <= self.no_progress_bars <= self.max_hold_bars_5m):
            raise ValueError("Invalid stock-option target, risk, Greek or entry limits")
        if not 5<=self.runner_max_hold_minutes<=120:
            raise ValueError('Runner holding limit must be between 5 and 120 minutes')

    def plan(self, entry: float, qty: int, max_target_price=None, min_lots=1, min_net_rr=None):
        # Cash loss is an exit trigger for the whole position, not an entry veto.
        plan = premium_plan(entry, qty, compute_fno_option_charges, self.target_pct,
                            self.stop_pct, max(self.min_rr_target, min_net_rr or 0), self.slippage_pct,
                            self.rupee_target, self.max_lots, float("inf"),self.extend_rupee_target,max_target_price,
                            min_lots=min_lots,cash_loss_limit=self.rupee_max_risk)
        if plan is None:
            return None
        units = plan['qty']
        # Find the first bid tick whose modeled liquidation loss is within the
        # cash threshold. Replay uses this stop and still fills gaps at the open.
        lo, hi = 1, math.ceil(entry * 20)
        while lo < hi:
            mid = (lo + hi) // 2
            fill = math.floor(max(0.05, mid/20 * (1-self.slippage_pct/100))*20+1e-9)/20
            loss = (entry-fill)*units + compute_fno_option_charges(entry,fill,units)
            if loss <= self.rupee_max_risk: hi = mid
            else: lo = mid + 1
        plan['cash_loss_stop_price'] = lo/20
        plan['expected_net_risk'] = min(plan['expected_net_risk'],self.rupee_max_risk)
        plan['expected_net_rr'] = plan['expected_net_reward']/plan['expected_net_risk']
        plan.update(total_risk_limit=self.rupee_max_risk,
                    expected_net_risk_per_lot=plan['expected_net_risk']/plan['lots'])
        return plan


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
    def load_universe(cls, logger: logging.Logger, min_days_to_expiry=0) -> Dict[str, Dict[str, Any]]:
        if min_days_to_expiry != 0:
            raise ValueError('Current expiry required; minimum DTE cannot select a later expiry')
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
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
                if c['expiry'] >= today_str})
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
                    "entry_contract_oi":leg.get('oi'),
                    "greeks_timestamp": leg.get("received_epoch", time.time()),
                    "greeks_received_epoch": leg.get("received_epoch", time.time())})
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates or not ivs:
            return None
        for candidate in candidates:
            candidate["iv_range_low"], candidate["iv_range_high"] = min(ivs), max(ivs)
        candidates.sort(key=lambda c: (abs(abs(c["delta"])-(cfg.min_delta+cfg.max_delta)/2), c["iv"]))
        return {**candidates[0], "alternate_contracts": candidates[1:3]}


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


class DhanCandleOrderFlow:
    """Estimate aggressor volume from observed trades and the preceding book."""

    def __init__(self, client_id, token, security_ids, logger):
        self.client_id = client_id
        self.token = token
        self.security_ids = set(map(str, security_ids))
        self.logger = logger
        self.previous = {}
        self.candles = {}
        self.reported = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None

    def start(self):
        if self.security_ids and self.worker is None:
            self.worker = threading.Thread(target=self.run, name="stock-order-flow", daemon=True)
            self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=2)

    def ingest(self, packet, received):
        if not isinstance(packet, bytes) or len(packet) < 162 or packet[0] != 8 or packet[3] != 1:
            return
        security_id = str(struct.unpack_from("<I", packet, 4)[0])
        if security_id not in self.security_ids:
            return
        price, quantity, traded_at, _, cumulative = struct.unpack_from("<fHIfI", packet, 8)
        bid, ask = struct.unpack_from("<ff", packet, 74)
        if traded_at > received + 1 or not math.isfinite(price) or price <= 0:
            return
        with self.lock:
            prior = self.previous.get(security_id)
            if prior and (traded_at < prior[0] or cumulative < prior[1]):
                return
            self.previous[security_id] = (traded_at, cumulative, bid, ask, received)
            if prior is None or cumulative == prior[1]:
                return
            delta = cumulative - prior[1]
            old_bid, old_ask = prior[2:4]
            side = "unclassified"
            if (delta == quantity and 0 < old_bid < old_ask < math.inf
                    and 0 <= received - prior[4] <= 5 and -1 <= received - traded_at <= 5):
                if price >= old_ask - 0.00005:
                    side = "estimated_buy"
                elif price <= old_bid + 0.00005:
                    side = "estimated_sell"
            candle_start = traded_at // 300 * 300
            candles = self.candles.setdefault(security_id, {})
            candle = candles.setdefault(candle_start, Counter())
            candle[side] += delta
            for stamp in list(candles):
                if stamp < candle_start - 3600:
                    del candles[stamp]

    def snapshot(self, security_id, candle_start, candle_volume):
        with self.lock:
            observed = self.candles.get(str(security_id), {}).get(candle_start, {}).copy()
        buy = observed.get("estimated_buy", 0)
        sell = observed.get("estimated_sell", 0)
        classified = buy + sell
        consistent = bool(candle_volume and sum(observed.values()) <= candle_volume)
        return {
            "candle_start_epoch": candle_start,
            "estimated_buy": buy,
            "estimated_sell": sell,
            "estimated_delta": buy - sell,
            "total_candle_volume": candle_volume,
            "observed_unclassified": observed.get("unclassified", 0),
            "unclassified_or_unobserved": candle_volume - classified if consistent else None,
            "classified_fraction": round(classified / candle_volume, 4) if consistent else None,
            "status": "ESTIMATED" if consistent and classified else
                      "VOLUME_MISMATCH" if observed and not consistent else "UNAVAILABLE",
        }

    def run(self):
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.logger.warning("[STOCK ORDER FLOW] WebSocket dependency unavailable")
            return
        url = "wss://api-feed.dhan.co?" + urlencode({
            "version": 2, "token": self.token, "clientId": self.client_id, "authType": 2,
        })
        instruments = [{"ExchangeSegment": "NSE_EQ", "SecurityId": sid}
                       for sid in sorted(self.security_ids)]
        while not self.stop.is_set() and datetime.now(IST).time() < SCAN_END_TIME:
            try:
                with connect(url, open_timeout=5, close_timeout=1) as socket:
                    with self.lock:
                        self.previous.clear()
                    for start in range(0, len(instruments), 100):
                        batch = instruments[start:start + 100]
                        socket.send(json.dumps({"RequestCode": 21, "InstrumentCount": len(batch),
                                                "InstrumentList": batch}))
                    self.logger.info("[STOCK ORDER FLOW] Subscribed to %d cash stocks", len(instruments))
                    while not self.stop.is_set() and datetime.now(IST).time() < SCAN_END_TIME:
                        try:
                            packet = socket.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if isinstance(packet, bytes) and packet and packet[0] == 50:
                            code = struct.unpack_from("<H", packet, 8)[0] if len(packet) >= 10 else None
                            self.logger.warning("[STOCK ORDER FLOW] Dhan disconnected, code=%s", code)
                            break
                        self.ingest(packet, time.time())
            except Exception as error:
                # Exception details can include the credential-bearing WebSocket URL.
                self.logger.warning("[STOCK ORDER FLOW] Feed unavailable (%s); retrying",
                                    type(error).__name__)
            self.stop.wait(5)


class DhanStockOptionsClient:
    def start_order_flow(self, universe):
        self.order_flow = DhanCandleOrderFlow(self.client_id, self.access_token, universe, self.logger)
        self.order_flow.start()

    def stop_order_flow(self):
        flow = getattr(self, "order_flow", None)
        if flow is not None:
            flow.close()

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
                    if not self.history_events[issue_count:]:
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
                response = self.session.post(f"{self.BASE_URL}/charts/{endpoint}", json=request, timeout=(3,5))
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
                    if use_cache and not self.history_events[issue_count:]:
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
            # Full quotes include cumulative session volume; OHLC alone does not.
            with market_quote_request_slot():
                resp = self.session.post(f"{self.BASE_URL}/marketfeed/quote", json=payload, timeout=(3,5))
            if resp.status_code == 200:
                data=resp.json().get("data", {})
                received=time.time()
                for segment in data.values():
                    if isinstance(segment,dict):
                        for quote in segment.values():
                            if isinstance(quote,dict):quote['received_epoch']=received
                return data
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

    def scanner_previous_session(self, security_id):
        """Cache the completed daily volume reference in memory for this session."""
        now=datetime.now(IST);key=(str(security_id),now.date().isoformat())
        cache=getattr(self,'_scanner_daily_cache',{})
        self._scanner_daily_cache=cache
        if key in cache:return cache[key]
        self._pace_quote()
        try:
            response=self.session.post(f'{self.BASE_URL}/charts/historical',json={
                'securityId':str(security_id),'exchangeSegment':'NSE_EQ','instrument':'EQUITY',
                'expiryCode':0,'oi':False,'fromDate':(now-timedelta(days=10)).date().isoformat(),
                'toDate':now.date().isoformat()},timeout=15)
            if response.status_code!=200:return None
            raw=response.json();raw=raw.get('data',raw)
            rows=[(datetime.fromtimestamp(float(t),IST).date(),float(raw['volume'][i]),i)
                  for i,t in enumerate(raw['timestamp']) if datetime.fromtimestamp(float(t),IST).date()<now.date()]
            date,volume,index=max(rows)
            if not math.isfinite(volume) or volume<=0:return None
            cache[key]={'date':date.isoformat(),'volume':volume,'source':'DHAN_DAILY_OHLC',
                        'high':float(raw['high'][index]),'low':float(raw['low'][index])}
            return cache[key]
        except (requests.RequestException,KeyError,TypeError,ValueError,IndexError):return None

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
            with option_chain_request_slot():
                response = self.session.post(f"{self.BASE_URL}/optionchain", json={
                    "UnderlyingScrip": int(underlying_id), "UnderlyingSeg": "NSE_EQ", "Expiry": expiry}, timeout=(3,5))
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
            with market_quote_request_slot():
                response = self.session.post(f"{self.BASE_URL}/marketfeed/quote",
                    json={"NSE_FNO": [int(s) for s in security_ids]}, timeout=(3,5))
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
    """Day-change prefilter over every quoted stock; no top-N or price cutoff."""
    bulls, bears = [], []
    for sec_id, quote in quotes.items():
        ltp = float(quote.get("last_price", 0) or 0)
        prev = float(quote.get("ohlc", {}).get("close", 0) or 0)
        if not all(math.isfinite(v) and v>0 for v in (ltp,prev)):
            continue
        change = 100 * (ltp / prev - 1)
        if change >= cfg.min_day_change_pct:
            bulls.append((change, str(sec_id), ltp))
        elif change <= -cfg.min_day_change_pct:
            bears.append((change, str(sec_id), ltp))
    return sorted(bulls, reverse=True) + sorted(bears)


def fresh_stock_underlying_quote(client,security_id,market_quotes=None):
    quote=(market_quotes or {}).get(str(security_id),{})
    if isinstance(quote,dict) and 0<=quote_received_age(quote,time.time())<6:
        return dict(quote)
    return client.fetch_batch_ohlc([security_id]).get('NSE_EQ',{}).get(str(security_id),{})


def recheck_recent_stock_candidate(client,candidate,cfg,market_quotes,reasons):
    """Reuse a <10s verification, while checking a fresh underlying and depth."""
    def reject(reason):
        reasons.append(reason);return None
    now=time.time();verified=candidate.get('verified_epoch',0)
    if not 0<=now-verified<10 or int(now//300)!=int(verified//300):
        return reject('Candidate needs full closed-candle verification')
    context=getattr(client,'_stock_verification_context',{})
    context=context.get(str(candidate['eq_security_id'])) if isinstance(context,dict) else None
    if not context:return reject('Candidate verification context unavailable')
    quote=fresh_stock_underlying_quote(client,candidate['eq_security_id'],market_quotes)
    if not 0<=quote_received_age(quote,time.time())<6:return reject('Reused candidate underlying quote stale')
    quote['previous_session_reference']=context['daily_reference']
    membership=scanx_stock_membership(context['candles'],quote,time.time(),cfg.min_day_change_pct,cfg.ema_period)
    if not membership['allowed'] or membership['opt_type']!=candidate['opt_type']:
        return reject('Final scanner membership: '+membership['reason'])
    if not 0<=time.time()-(candidate['signal_timestamp']+300)<cfg.confirm_window_sec:
        return reject('Underlying confirmation expired during candidate selection')
    sign=1 if candidate['opt_type']=='CE' else -1
    spot=float(quote['last_price']);advance=sign*(spot-candidate['conf_close'])
    band=candidate['entry_price_validation'];level=band.get('trigger_level')
    if not band['minimum_advance']<=advance<=band['maximum_advance'] or (level is not None and sign*(spot-level)<=0):
        return reject('Reused candidate no longer holds structural entry band')
    return {**candidate,'spot_ltp':spot,'scanner_membership':membership,
            'entry_price_validation':{**band,'final_spot':spot,'reused_recent_verification':True,
                'final_signal_age_seconds':time.time()-(candidate['signal_timestamp']+300)}}


def verify_live_scanx_and_resolve_stock_option(client, stock_info, spot_ltp, day_change_pct, cfg, reasons=None, market_quotes=None, *, membership_only=False):
    def reject(reason):
        if reasons is not None:
            reasons.append(reason)
        return None
    candles = client.fetch_intraday_candles(stock_info["eq_security_id"], interval="5",
        from_date=(datetime.now(IST)-timedelta(days=10)).strftime("%Y-%m-%d"))
    if not candles:
        return reject("Underlying candle history unavailable")
    daily_reference=client.scanner_previous_session(stock_info['eq_security_id'])
    if not daily_reference:return reject('Scanner previous-session daily volume unavailable')
    current = fresh_stock_underlying_quote(client,stock_info["eq_security_id"],market_quotes)
    current['previous_session_reference']=daily_reference
    membership=scanx_stock_membership(candles,current,time.time(),cfg.min_day_change_pct,cfg.ema_period)
    if not membership['allowed']:
        return reject('Scanner membership: '+membership['reason'])
    client.logger.info('[STOCK SCANNER %s] %s | %s','MATCH',
                       stock_info['symbol'],json.dumps(membership))
    order_flow = getattr(client, "order_flow", None)
    flow = None
    if isinstance(order_flow, DhanCandleOrderFlow):
        latest_closed = int(time.time()) // 300 * 300 - 300
        timestamps = candles["timestamp"]
        index = bisect_left(timestamps, latest_closed)
        if index < len(timestamps) and timestamps[index] == latest_closed:
            security_id = str(stock_info["eq_security_id"])
            flow = order_flow.snapshot(security_id, latest_closed, int(candles["volume"][index]))
            if order_flow.reported.get(security_id) != flow:
                client.logger.info("[STOCK ORDER FLOW CANDLE] %s | %s", stock_info["symbol"], json.dumps(flow))
                order_flow.reported[security_id] = flow
    if membership_only:
        return membership
    sig = evaluate_stock_setup(candles, time.time(), cfg.min_day_change_pct, cfg.ema_period,
                               enable_opening_trend=cfg.enable_opening_trend,reasons=reasons,
                               confirm_window_sec=cfg.confirm_window_sec,
                               previous_close=membership['previous_close'],
                               require_breakout=cfg.require_confirmed_breakout,previous_session=daily_reference,
                               **({"order_flow": flow} if flow is not None else {}))
    if not sig:
        if reasons:return None
        return reject("No fresh confirmed momentum setup (EMA/VWAP/volume/body/proximity)")
    if sig['opt_type']!=membership['opt_type']:
        return reject('Trade confirmation conflicts with scanner direction')
    sig['scanner_membership']=membership
    if flow and flow['candle_start_epoch'] == sig['signal_timestamp']:
        sig['estimated_order_flow'] = flow
        if order_flow_direction(flow) == (1 if sig["opt_type"] == "CE" else -1):
            sig["order_flow_min_net_rr"] = 2.0
    spot_ltp = float(current.get("last_price", 0) or 0)
    if spot_ltp <= 0:
        return reject("Current underlying quote unavailable")
    # Require the current underlying quote to hold confirmation, without chasing.
    direction = 1 if sig["opt_type"] == "CE" else -1
    confirmation=sig.get('breakout_confirmation') or {}
    pullback_entry=confirmation.get('entry_path')=='PULLBACK_RETEST'
    pullback_tolerance=0.25 if pullback_entry else 0.10
    level=confirmation.get('trigger_level')
    minimum_advance=-pullback_tolerance*sig['atr_5m']
    # A shallow retracement into a strong confirmation candle can still hold
    # the break. Bound it by half the candle, 0.25 ATR and the defended level;
    # do not extend the chase ceiling or admit an unconfirmed pullback.
    extreme=sig.get('conf_low' if direction==1 else 'conf_high')
    if level is not None and extreme is not None and confirmation.get('closed_candle'):
        candle_room=direction*(sig['conf_close']-float(extreme))
        minimum_advance=-min(0.25*sig['atr_5m'],max(0.0,0.5*candle_room))
        minimum_advance=max(minimum_advance,
            direction*(float(level)-sig['conf_close'])+float(confirmation.get('buffer',0)))
    maximum_advance=0.70*sig['atr_5m']

    def entry_price_issue(price, observed_at):
        age=observed_at-(sig.get('signal_timestamp',observed_at-300)+300)
        if not 0<=age<cfg.confirm_window_sec:
            return 'Underlying confirmation expired or not yet closed during option selection'
        if level is not None and direction*(price-level)<=0:
            return 'Live price has lost the confirmed breakout level'
        advance=direction*(price-sig['conf_close'])
        if not minimum_advance-1e-9<=advance<=maximum_advance+1e-9:
            return (f'Underlying follow-through {advance:.2f} outside '
                    f'{minimum_advance:.2f} to {maximum_advance:.2f}')
        return None

    issue=entry_price_issue(spot_ltp,time.time())
    if issue:return reject(issue)
    # Older valid five-minute signals must not survive a newer failed impulse.
    checked_at=time.time()
    if checked_at-(sig.get('signal_timestamp',checked_at)+300)>=120:
        minutes=client.fetch_intraday_candles(stock_info['eq_security_id'],interval='1',
            from_date=datetime.fromtimestamp(checked_at-600,IST).isoformat(),
            to_date=datetime.fromtimestamp(checked_at,IST).isoformat(),use_cache=False)
        momentum=recent_entry_momentum(minutes,sig,spot_ltp,time.time())
        if not momentum['allowed']:return reject(momentum['reason'])
        sig['fresh_entry_momentum']=momentum
    chain = client.fetch_option_chain(stock_info["eq_security_id"], stock_info["nearest_expiry"])
    if not chain:
        return reject(f"Option chain unavailable for expiry {stock_info['nearest_expiry']}")
    contract = DynamicStockOptionsUniverse.resolve_atm_option(stock_info, spot_ltp, sig["opt_type"], chain, cfg)
    if not contract:
        return reject("No master-matched option with valid broker Greeks in the delta band")
    final_quote=fresh_stock_underlying_quote(client,stock_info['eq_security_id'],market_quotes)
    try:
        if not 0<=quote_received_age(final_quote,time.time())<=8:return reject('Final underlying quote stale or future-dated')
        final_spot=float(final_quote.get('last_price',0))
        if not math.isfinite(final_spot) or final_spot<=0:return reject('Final underlying quote unavailable')
        issue=entry_price_issue(final_spot,time.time())
        if issue:return reject('Final entry check: '+issue)
        final_quote['previous_session_reference']=daily_reference
        final_membership=scanx_stock_membership(candles,final_quote,time.time(),cfg.min_day_change_pct,cfg.ema_period)
        if not final_membership['allowed'] or final_membership['opt_type']!=sig['opt_type']:
            return reject('Final scanner membership: '+final_membership.get('reason','Direction changed'))
        sig['scanner_membership']=final_membership
        sig['entry_price_validation']={'initial_spot':spot_ltp,'final_spot':final_spot,
            'minimum_advance':minimum_advance,'maximum_advance':maximum_advance,
            'trigger_level':level,'final_signal_age_seconds':time.time()-(sig.get('signal_timestamp',time.time()-300)+300)}
        if sig.get('fresh_entry_momentum'):
            final_momentum=recent_entry_momentum(minutes,sig,final_spot,time.time())
            if not final_momentum['allowed']:return reject(final_momentum['reason'])
            sig['fresh_entry_momentum']=final_momentum
    except (TypeError,ValueError,OverflowError):return reject('Final underlying quote malformed')
    if sig.get('breakout_confirmation'):
        client.logger.info('[STOCK BREAKOUT CONFIRMED] %s | %s',stock_info['symbol'],json.dumps(sig['breakout_confirmation']))
    context=getattr(client,'_stock_verification_context',{})
    if not isinstance(context,dict):context={}
    context[str(stock_info['eq_security_id'])]={'candles':candles,'daily_reference':daily_reference}
    client._stock_verification_context=context
    return {**sig, **contract, "spot_ltp": final_spot, "eq_security_id": stock_info["eq_security_id"],
            'verified_epoch':time.time()}


def stock_entry_structure_check(sig, plan, ask, bid):
    """Reject a late entry whose premium stop sits inside structural risk.

    Delta translation is a conservative local scenario, not a stop-fill forecast.
    It can veto an entry; it never widens the premium/cash stop or increases size.
    """
    confirmation=sig.get('breakout_confirmation') or {}
    if not confirmation:return {'allowed':True,'reason':'No structural breakout profile'}
    try:
        sign=1 if sig['opt_type']=='CE' else -1
        spot=float(sig['spot_ltp']);atr=float(sig['atr_5m'])
        trigger=float(confirmation['trigger_level']);buffer=float(confirmation['buffer'])
        path=confirmation.get('entry_path','DIRECT_BREAKOUT')
        prepared=confirmation.get('pre_breakout') or confirmation.get('pullback') or {}
        invalidation=float(prepared.get('invalidation_level',trigger-sign*buffer))
        delta=abs(float(sig['delta']))
        stop=max(float(plan['initial_sl']),float(plan['cash_loss_stop_price']))
        values=(spot,atr,trigger,buffer,invalidation,delta,ask,bid,stop)
        if not all(math.isfinite(v) for v in values) or min(spot,atr,trigger,invalidation,delta)<=0 or buffer<0:
            raise ValueError('Invalid structural inputs')
        distance=sign*(spot-trigger);structural_move=sign*(spot-invalidation)
        maximum_distance_atr=0.80 if path=='CONSOLIDATION_BREAKOUT' else 1.10
        allowed_distance=maximum_distance_atr*atr
        projected_premium_risk=ask-bid+delta*structural_move
        available=ask-stop
        result={'allowed':False,'entry_path':path,'spot':spot,'trigger_level':trigger,
                'invalidation_level':invalidation,'distance_atr':distance/atr,
                'maximum_distance_atr':maximum_distance_atr,'structural_move_points':structural_move,
                'delta_scenario_risk_points':projected_premium_risk,'premium_stop_room_points':available,
                'basis':'ENTRY_PROXIMITY_AND_LOCAL_DELTA_SCENARIO'}
        if distance<=buffer or structural_move<=0:result['reason']='Entry has not held the structural trigger'
        elif distance>allowed_distance+1e-9:result['reason']=f'Entry is late beyond {maximum_distance_atr:.2f} ATR from its prepared trigger; wait for a new setup'
        elif projected_premium_risk>available+1e-9:result['reason']='Premium stop lies inside estimated structural risk; wait for a better entry'
        else:result.update(allowed=True,reason='Entry proximity and structural-risk scenario fit existing stop')
        return result
    except (KeyError,TypeError,ValueError,OverflowError):
        return {'allowed':False,'reason':'Structural entry/risk context unavailable or malformed'}


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
        flow_rr = sig.get("order_flow_min_net_rr")
        plan_options = {"min_net_rr": flow_rr} if flow_rr is not None else {}
        if cfg.extend_rupee_target:
            # Feed the same option-move estimate into sizing. A small first lot
            # must not veto a larger affordable lot count that fits both the
            # target-move estimate and the unchanged cash-risk ceiling.
            delta,theta,gamma=(float(sig[k]) for k in ('delta','theta','gamma'))
            move=1.5*float(sig['atr_5m'])
            decay=max(0,-theta)*cfg.max_hold_bars_5m*5/375
            premium_move=abs(delta)*move+0.5*gamma*move*move-decay
            # Strong movers use the explicit 55% scenario screen below; the
            # cash target, full planned RR and cash-risk cap remain mandatory.
            plan=cfg.plan(ask,qty,max_target_price=None if strong_mover else ask+premium_move, **plan_options)
        else:
            plan = cfg.plan(ask, qty, **plan_options)
        if not plan:
            return reject(f"No lot count reaches Rs.{cfg.rupee_target:.0f} within risk/RR limits")
        qty = plan["qty"]
        if min(int(bid_node["quantity"]), int(ask_node["quantity"])) < qty:
            return reject(f"Displayed depth below {plan['lots']} lots")
        if bid <= plan["initial_sl"]:
            return reject("Bid already at or below the planned stop")
        effective_stop=max(plan['initial_sl'],plan['cash_loss_stop_price'])
        if ask-bid > 0.25*(ask-effective_stop)+1e-9:
            return reject('Spread consumes over 25% of effective stop distance')
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
        if dte < 0:
            return reject('Contract already expired')
        theta_pct = max(0, -theta)/ask*100
        if theta_pct > cfg.max_theta_pct_per_day:
            return reject(f"Theta {theta_pct:.2f}%/day above {cfg.max_theta_pct_per_day:.2f}%")
        decay = max(0, -theta) * cfg.max_hold_bars_5m * 5 / 375
        if decay*qty >= cfg.rupee_target:
            return reject(f"Decay over hold Rs.{decay*qty:.0f} exceeds the Rs.{cfg.rupee_target:.0f} target")
        move = 1.5 * float(sig["atr_5m"])
        expected_gain = abs(delta)*move + 0.5*gamma*move*move - decay
        if not math.isfinite(expected_gain):return reject('Non-finite expected option move')
        net_gain = expected_gain*qty - plan["target_estimated_costs"]
        minimum_fraction=0.55 if strong_mover else (1.0 if cfg.extend_rupee_target else 0.75)
        required_gain=max(cfg.rupee_target,plan.get('net_profit_target',cfg.rupee_target))*minimum_fraction
        if net_gain < required_gain:
            return reject(f"A 1.5 ATR move nets only Rs.{net_gain:.0f} of the required Rs.{required_gain:.0f} ({minimum_fraction:.0%} minimum)")
        # Cross-sectional IV range in the same expiry/side, not historical IV rank.
        low, high = float(sig["iv_range_low"]), float(sig["iv_range_high"])
        if not all(math.isfinite(v) for v in (low, high)) or high <= low:
            return reject("Unusable chain IV range")
        iv_limit = 0.9 if strong_mover else 0.8
        if (iv-low)/(high-low) >= iv_limit:
            return reject(f"IV in the top {100*(1-(iv-low)/(high-low)):.0f}% of the chain")
        sizing={'basis':'Smallest qualifying size','lots':plan['lots']}
        # Scale only a strong, higher-timeframe-aligned setup that already
        # passed every one-position entry check. Fresh depth/cash still bind.
        if float(sig.get('signal_quality',0))>=80 and sig.get('higher_timeframe_aligned'):
            lot_size=int(sig['lot_size'])
            for lots in range(plan['lots']+1,min(3,cfg.max_lots)+1):
                larger=cfg.plan(ask,lot_size,min_lots=lots,
                    max_target_price=ask+premium_move if cfg.extend_rupee_target and not strong_mover else None,
                    **plan_options)
                if not larger or larger['lots']!=lots:continue
                if not stock_entry_structure_check(sig,larger,ask,bid)['allowed']:continue
                if ask-bid>0.25*(ask-max(larger['initial_sl'],larger['cash_loss_stop_price']))+1e-9:continue
                units=larger['qty']
                if min(int(bid_node['quantity']),int(ask_node['quantity']))<units:continue
                if ask*units+larger['estimated_costs']>wallet or decay*units>=cfg.rupee_target:continue
                projected=expected_gain*units-larger['target_estimated_costs']
                if projected<max(1.2*larger['net_profit_target'],1.5*larger['expected_net_risk']):continue
                plan=larger;qty=units;net_gain=projected
                sizing={'basis':'Strong aligned setup with buffered reward, funded size and depth',
                        'lots':lots,'projected_net_gain':round(projected,2),
                        'planned_total_risk':round(larger['expected_net_risk'],2)}
                if projected>=cfg.runner_target:break
        entry_structure=stock_entry_structure_check(sig,plan,ask,bid)
        if not entry_structure['allowed']:return reject(entry_structure['reason'])
        try:
            last_trade_age = now_epoch - datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST).timestamp()
        except (KeyError, TypeError, ValueError):
            last_trade_age = None
        return {**sig, **plan, "opt_ltp": ask, "bid": bid, "spread_pct": spread,"size_selection":sizing,
                'entry_structure_check':entry_structure,
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


def qualify_stock_contracts(client, sig, cfg, wallet, reject_label):
    primary = {**sig}
    alternatives = primary.pop("alternate_contracts", [])
    contracts = [primary, *({**primary, **contract} for contract in alternatives)]
    depth = client.fetch_option_quotes([contract["opt_security_id"] for contract in contracts])
    for contract in contracts:
        reasons = []
        quote = depth.get(str(contract["opt_security_id"]), {})
        qualified = qualify_quote(contract, quote, cfg, wallet, reasons=reasons)
        if qualified:
            qualified["alternate_contracts"] = [
                alternate for alternate in alternatives if alternate["opt_security_id"] != contract["opt_security_id"]
            ]
            return qualified, quote
        client.logger.info("[%s] %s %s strike %s | %s", reject_label, contract["symbol"], contract["opt_type"],
                           contract.get("strike", "?"), reasons[0] if reasons else "Unspecified rejection")
    return None, {}


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


def stock_position_value(pos, price=None):
    """One entry, all booked exits, and optional liquidation of remaining units."""
    fills=list(pos.get('exit_fills') or [])
    if any(int(f['qty'])<=0 or not math.isfinite(float(f['price'])) or float(f['price'])<=0 for f in fills):
        raise ValueError('Invalid recorded partial fill')
    if price is not None and pos.get('original_qty') is not None:
        if sum(int(f['qty']) for f in fills)+pos['qty']!=pos['original_qty']:
            raise ValueError('Remaining and booked quantities do not reconcile')
    if price is not None:fills.append({'qty':pos['qty'],'price':price})
    qty=sum(int(f['qty']) for f in fills)
    if qty<=0:return {'qty':0,'gross':0.0,'fees':0.0,'net':0.0,'price':0.0}
    weighted=sum(float(f['price'])*int(f['qty']) for f in fills)/qty
    gross=round((weighted-pos['entry_price'])*qty,2)
    fees=compute_fno_option_charges(pos['entry_price'],weighted,qty,exit_orders=len(fills))
    return {'qty':qty,'gross':gross,'fees':fees,'net':round(gross-fees,2),'price':weighted}


def book_stock_partial(pos, quote, now, cfg, gate, day):
    """Book whole lots once; persist the PAPER fill before changing memory."""
    if not cfg.staged_profit or pos.get('partial_booked') or not (pos.get('runner_assessment') or {}).get('hold'):
        return None
    if not 0<=now.timestamp()-(pos.get('runner_assessment') or {}).get('assessed_epoch',0)<=8:return None
    lot_size=int(pos.get('lot_size',0));lots=int(pos.get('lots',1))
    if lot_size<=0 or lots<2 or pos['qty']!=lots*lot_size:return None
    try:
        bid=float(quote['depth']['buy'][0]['price']);size=int(quote['depth']['buy'][0]['quantity'])
        if not math.isfinite(bid) or bid<=pos['current_sl'] or size<pos['qty'] or not 0<=quote_received_age(quote,now.timestamp())<=8:return None
        fill=math.floor(bid*(1-cfg.slippage_pct/100)*20+1e-9)/20
        # Budget the additional exit order before protecting the base profit.
        total_net=stock_position_value(pos,fill)['net']
        if total_net<pos.get('net_profit_target',cfg.rupee_target)+23.60 or total_net>=cfg.runner_target:return None
        sold=(lots//2)*lot_size
        updated=dict(pos)
        updated['original_qty']=pos.get('original_qty',pos['qty'])
        updated['original_lots']=pos.get('original_lots',lots)
        updated['qty']=pos['qty']-sold;updated['lots']=updated['qty']//lot_size
        updated['exit_fills']=list(pos.get('exit_fills') or [])+[{'qty':sold,'price':fill,
            'timestamp':now.strftime('%Y-%m-%d %H:%M:%S'),'reason':'PARTIAL_BASE_PROFIT'}]
        updated['partial_booked']=True
        booked=stock_position_value(updated)
        updated.update(partial_gross=booked['gross'],partial_net=booked['net'])
        updated['runner_net_target']=min(cfg.runner_target,
            booked['net']+cfg.runner_target*updated['qty']/updated['original_qty'])
        # No second entry fee; reserve the additional exit-order cost.
        updated['estimated_costs']=float(pos.get('estimated_costs',0))+23.60
        gate.update_position(day,pos['slot'],updated)
        pos.clear();pos.update(updated)
        return {'qty':sold,'price':fill,'booked_net':booked['net']}
    except (KeyError,TypeError,ValueError,OverflowError):return None


def close_paper_position(ledger, pos, price, reason, now):
    totals=stock_position_value(pos,price)
    qty=totals['qty'];gross=totals['gross'];fees=totals['fees']
    rec = {k: pos[k] for k in ("symbol", "contract", "opt_type", "scanner", "qty", "entry_price")}
    rec.update(timestamp=pos["entry_ts"], exit_timestamp=now.strftime("%Y-%m-%d %H:%M:%S"),
               lots=pos.get('original_lots',pos.get("lots", 1)),qty=qty,exit_price=totals['price'], gross_pnl=gross, charges=fees, net_pnl=totals['net'],
               r_multiple=round(gross / (pos["opt_1r"] * qty), 2), exit_reason=reason,
               data_source="PAPER_BID_ASK", entry_id=pos["entry_id"])
    if pos.get('exit_fills'):
        rec['exit_fills']=list(pos['exit_fills'])+[{'qty':pos['qty'],'price':price,
            'timestamp':now.strftime('%Y-%m-%d %H:%M:%S'),'reason':reason}]
    if pos.get('exit_diagnostics'):rec['exit_diagnostics']=pos['exit_diagnostics']
    rec['learning_risk_rs']=round(float(pos.get('expected_net_risk',pos['opt_1r']*qty+fees)),2)
    rec['learning_r_multiple']=round(rec['net_pnl']/rec['learning_risk_rs'],6) if rec['learning_risk_rs']>0 else None
    # Preserve decision-time evidence when the daily gate closes its position.
    # These fields are observations, not relaxed entry criteria or new signals.
    rec['entry_diagnostics']={k:pos[k] for k in (
        'signal_timestamp','conf_close','conf_high','conf_low','atr_5m','day_change_pct','scanner_membership','entry_price_validation','entry_structure_check',
        'cash_price_pressure','estimated_order_flow','order_flow_min_net_rr','volume_confirmation','volume_context','breakout_continuation',
        'ema10_15m','ema10_15m_slope','higher_timeframe_aligned',
        'vwap','bid','opt_ltp','spread_pct','quoted_spread_points',
        'spread_fraction_of_initial_risk','bid_room_to_initial_stop',
        'initial_sl','rupee_target_price','expected_net_rr','expected_net_risk','runner_assessment','entry_contract_oi','size_selection',
        'delta','theta','gamma','iv','iv_range_low','iv_range_high','dte',
        'quote_age_seconds','greeks_receipt_age_seconds','confirmation_age_seconds','fresh_entry_momentum','breakout_confirmation',
        'signal_quality','quality_volume_ratio','selection_analysis','selection_comparison','adaptive_feedback') if k in pos}
    # Idempotent if interrupted between ledger save and closing the daily record.
    if not any(t.get("entry_id") == pos["entry_id"] for t in ledger["trades"]):
        ledger["trades"].append(rec)
        update_ledger_metrics(ledger)
        atomic_write_json(LEDGER_FILE, ledger)
    return rec


def assess_stock_runner(pos, quote, underlying, chain, now_epoch, cfg):
    """Conservative live scenario; never change entry size to reach the runner cap."""
    target=cfg.runner_target
    result={'hold':False,'target_net':target,'reason':'Runner evidence unavailable','assessed_epoch':now_epoch}
    try:
        target=min(cfg.runner_target,float(pos.get('runner_net_target',cfg.runner_target)))
        if not math.isfinite(target) or target<=0:return result
        result['target_net']=target
        bid=float(quote['depth']['buy'][0]['price']);qty=int(pos['qty'])
        ask=float(quote['depth']['sell'][0]['price'])
        if not 0<=quote_received_age(quote,now_epoch)<=8 or not 0<=quote_received_age(underlying,now_epoch)<=8:return result
        if int(quote['depth']['buy'][0]['quantity'])<qty:return result
        if not math.isfinite(ask) or qty<=0 or ask<=bid or (ask-bid)/ask*100>cfg.max_spread_pct:return result
        spot=float(underlying['last_price']);sign=1 if pos['opt_type']=='CE' else -1
        advance=sign*(spot-float(pos['conf_close']));atr=float(pos['atr_5m'])
        observations=pos.get('underlying_observations',[])
        prior=next((v for v in reversed(observations) if 270<=now_epoch-float(v[0])<=450),None)
        if prior is not None:
            impulse=sign*(spot-float(prior[1]))
        elif 0<=now_epoch-pos['entry_epoch']<270:
            impulse=advance
            # Fresh post-entry volume remains necessary for staged runners.
            prior=next((v for v in observations
                        if pos['entry_epoch']<=float(v[0])<now_epoch),None)
        else:
            return result
        if impulse<0.12*atr:return result
        if cfg.staged_profit:
            if prior is None or len(prior)<3:return result
            increment=float(underlying['volume'])-float(prior[2])
            baseline=float((pos.get('volume_context') or {}).get('post_opening_median') or
                           (pos.get('volume_context') or {}).get('regular_average') or 0)
            needed_volume=.75*baseline*(now_epoch-float(prior[0]))/300
            if not all(math.isfinite(v) for v in (increment,baseline,needed_volume)) or baseline<=0 or increment<=0 or increment<needed_volume:
                return result
            result['recent_volume']=round(increment,2)
        hold_limit=(max(cfg.max_hold_bars_5m*5,cfg.runner_max_hold_minutes)
                    if cfg.staged_profit and pos.get('partial_booked') else cfg.max_hold_bars_5m*5)
        remaining=hold_limit-(now_epoch-pos['entry_epoch'])/60
        cutoff=(datetime.fromtimestamp(now_epoch,IST).replace(hour=14,minute=45,second=0,microsecond=0).timestamp()-now_epoch)/60
        if remaining<5 or cutoff<5 or atr<=0 or advance<0.20*atr:return result
        node=next(v for k,v in chain.items() if float(k)==float(pos['strike']))[pos['opt_type'].lower()]
        delta=abs(float(node['greeks']['delta']));theta=abs(float(node['greeks']['theta']))
        old_oi=float(pos['entry_contract_oi']);current_oi=float(node['oi'])
        if not all(math.isfinite(v) for v in (bid,spot,advance,atr,delta,theta,old_oi,current_oi)):return result
        if not cfg.min_delta<=delta<=cfg.max_delta or bid<=pos['entry_price']:return result
        fill=math.floor(bid*(1-cfg.slippage_pct/100)*20+1e-9)/20
        net=stock_position_value(pos,fill)['net']
        needed=max(0,(target-net)/qty)
        horizon=min(remaining,cutoff,15)
        gain=max(0,delta*min(3*atr,2*impulse)-theta*horizon/375-bid*cfg.slippage_pct/100)
        hold_runner=(gain>=1.2*needed) or (advance>=0.30*atr and impulse>=0.12*atr and delta>=cfg.min_delta)
        result.update(hold=hold_runner,current_net=round(net,2),
                      oi_regime='OI_UNCHANGED' if current_oi==old_oi else 'LONG_BUILDUP' if current_oi>old_oi else 'SHORT_COVERING',
                      underlying_impulse_5m=round(impulse,4),
                      required_premium_gain=round(needed,2),scenario_gain=round(gain,2),
                      reason='Fresh underlying continuation or option-move scenario supports runner' if hold_runner else 'Remaining move does not support runner')
        return result
    except (KeyError,TypeError,ValueError,OverflowError,StopIteration,ZeroDivisionError):return result


def update_position_quote(position, quote, now_epoch):
    """Keep observed LTP and executable depth distinct from the entry ask."""
    if not isinstance(quote,dict) or not 0<=quote_received_age(quote,now_epoch)<=30:return False
    def positive(value):
        try:
            number=float(value)
            return number if math.isfinite(number) and number>0 else None
        except (ValueError,TypeError,OverflowError):return None
    depth=quote.get('depth') or {}
    if not isinstance(depth,dict):depth={}
    def top(side):
        rows=depth.get(side) or []
        return positive(rows[0].get('price')) if isinstance(rows,list) and rows and isinstance(rows[0],dict) else None
    ltp=positive(quote.get('last_price'));bid=top('buy');ask=top('sell')
    if ltp is None and bid is None and ask is None:return False
    position.update(current_ltp=ltp,current_bid=bid,current_ask=ask,
                    quote_received_epoch=float(quote['received_epoch']),
                    quote_price_basis='LTP_WITH_BID_VALUATION')
    return True


def stock_breakout_failure(pos, candles, now_epoch):
    """Two post-entry closed one-minute candles back through the broken level."""
    result={'failed':False,'assessed_epoch':now_epoch,'reason':'No confirmed breakout failure'}
    try:
        setup=pos.get('breakout_confirmation') or {}
        level=float(setup['trigger_level']);atr=float(pos['atr_5m'])
        sign=1 if pos['opt_type']=='CE' else -1
        if not all(math.isfinite(v) and v>0 for v in (level,atr)):return result
        stamps=candles['timestamp'];rows=[]
        for i,raw in enumerate(stamps):
            ts=float(raw)
            if ts<pos['entry_epoch'] or ts+60>now_epoch:continue
            o,h,l,c=(float(candles[k][i]) for k in ('open','high','low','close'))
            if not all(math.isfinite(v) and v>0 for v in (ts,o,h,l,c)) or not l<=min(o,c)<=max(o,c)<=h:return result
            rows.append((ts,o,h,l,c))
        rows.sort()
        if len(rows)<2:return result
        a,b=rows[-2:]
        if b[0]!=a[0]+60 or not 0<=now_epoch-(b[0]+60)<=90:return result
        buffer=max(0.05,0.10*atr)
        failed=(sign*(a[4]-level)<-buffer and sign*(b[4]-level)<-buffer
                and sign*(b[4]-b[1])<0)
        result.update(failed=failed,level=level,buffer=buffer,closes=[a[4],b[4]],
                      last_closed_epoch=b[0]+60,reason='Two closed minutes lost breakout level' if failed else result['reason'])
    except (KeyError,TypeError,ValueError,IndexError,OverflowError):pass
    return result


def stock_net_protection_bid(pos, upper_bid, target_net, cfg):
    """Minimum tick bid covering modeled fees and adverse slippage, if reachable."""
    def net(bid):
        fill=math.floor(bid*(1-cfg.slippage_pct/100)*20+1e-9)/20
        return stock_position_value(pos,fill)['net']
    lo=math.ceil(pos['entry_price']*20);hi=math.floor(upper_bid*20)
    if lo>hi or net(hi/20)<target_net:return None
    while lo<hi:
        mid=(lo+hi)//2
        if net(mid/20)>=target_net:hi=mid
        else:lo=mid+1
    return lo/20


def stock_profit_protection_net(peak_net):
    """Requested net-profit tiers; actual execution can gap below a floor."""
    peak=float(peak_net)
    if not math.isfinite(peak):return None
    if peak>=1800:return 0.75*peak
    if peak>=1200:return 0.60*peak
    if peak>=750:return 100.0
    return None


def paper_exit(pos, quote, now, cfg, urgent_only=False):
    stamp=now.timestamp()
    monitor=pos.setdefault('exit_monitor',{'checks':0,'unusable_checks':0,'recent_quotes':[]})
    monitor['checks']+=1
    sample={'checked_epoch':stamp}
    try:
        age=quote_received_age(quote,stamp)
        levels=[(float(v['price']),int(v['quantity'])) for v in quote['depth']['buy']]
        levels=[(p,q) for p,q in levels if math.isfinite(p) and p>0 and q>0]
        levels.sort(reverse=True)
        if not levels or not 0<=age<=30:raise ValueError('Stale or unavailable exit depth')
        bid=levels[0][0];available=sum(q for _,q in levels)
        sample.update(bid=bid,available_qty=available,quote_age_seconds=age,
                      received_epoch=quote.get('received_epoch'),required_qty=pos['qty'])
        if available<pos['qty']:raise ValueError('Insufficient aggregate exit depth')
        remaining=pos['qty'];value=0.0
        for level_price,quantity in levels:
            take=min(remaining,quantity);value+=take*level_price;remaining-=take
            if not remaining:break
        executable=value/pos['qty']
        best_bid=bid
        bid=executable
        pos['peak_executable_vwap']=max(pos.get('peak_executable_vwap',executable),executable)
        sample['executable_vwap']=executable
        last=monitor.get('last_usable_epoch')
        if last is not None:sample['seconds_since_previous_usable']=stamp-last
        monitor['last_usable_epoch']=stamp
    except (KeyError,IndexError,TypeError,ValueError,OverflowError) as error:
        monitor['unusable_checks']+=1;sample['unusable_reason']=str(error)
        monitor['recent_quotes']=(monitor['recent_quotes']+[sample])[-24:]
        return None
    monitor['recent_quotes']=(monitor['recent_quotes']+[sample])[-24:]
    elapsed = now.timestamp() - pos["entry_epoch"]
    pos["peak_bid"] = max(pos.get("peak_bid", bid), bid)
    assessment=pos.get('runner_assessment') or {}
    runner_hold=bool(assessment.get('hold') and 0<=now.timestamp()-assessment.get('assessed_epoch',now.timestamp())<=8)
    exit_flow = pos.get("exit_order_flow") or {}
    latest_closed = int(stamp) // 300 * 300 - 300
    opposing_flow = order_flow_direction(exit_flow, latest_closed) == (-1 if pos["opt_type"] == "CE" else 1) if exit_flow else False
    managing_runner=(runner_hold or bool(pos.get('partial_booked'))) and not opposing_flow
    jump = pos["trail_jump"]
    price = math.floor(max(0.05, executable * (1 - cfg.slippage_pct/100))*20 + 1e-9)/20
    net = stock_position_value(pos,price)['net']
    pos['peak_net_pnl']=max(float(pos.get('peak_net_pnl',net)),net)
    base_target_reached=(not managing_runner and net>=pos.get('net_profit_target',cfg.rupee_target))
    staged_wait=cfg.staged_profit and pos.get('lots',1)>=2 and not pos.get('partial_booked')
    def protect_net_floor(target):
        if net<target:return
        floor=stock_net_protection_bid(pos,bid,target,cfg)
        if floor is not None and floor<bid-1e-9:pos['current_sl']=max(pos['current_sl'],floor)
    # Tiered floors use peak modeled net after charges and slippage.
    if not urgent_only:
        locked_net=stock_profit_protection_net(pos['peak_net_pnl'])
        if locked_net is not None:
            protect_net_floor(locked_net)
        if managing_runner and net>=pos.get('net_profit_target',cfg.rupee_target):
            pos['base_target_locked']=True
    reason = None
    if net <= -cfg.rupee_max_risk:
        reason = "CASH_LOSS_LIMIT"
    elif bid <= pos["current_sl"]:
        reason = "TRAIL_STOP" if pos["current_sl"] > pos["initial_sl"] else "PREMIUM_STOP"
    elif urgent_only:
        reason='EOD_SQUAREOFF' if now.time().replace(tzinfo=None)>=SESSION_END_TIME else None
    elif opposing_flow and net > 0 and (base_target_reached or pos.get('base_target_locked')):
        reason='ORDER_FLOW_PROFIT_EXIT'
    elif base_target_reached:
        reason='RUPEE_TARGET'
    elif (pos.get('breakout_failure') or {}).get('failed') and 0<=stamp-pos['breakout_failure'].get('assessed_epoch',0)<=90:
        reason='UNDERLYING_BREAKOUT_FAILED'
    elif net>=min(cfg.runner_target,pos.get('runner_net_target',cfg.runner_target)):
        reason='RUNNER_NET_TARGET'
    elif (not managing_runner and bid>=pos['rupee_target_price']
          and pos['peak_bid']>=pos['rupee_target_price']+pos['opt_1r']
          and bid<=pos['peak_bid']-0.6*pos['opt_1r']):
        reason='RUPEE_TARGET_TRAILED'
    elif now.time().replace(tzinfo=None) >= SESSION_END_TIME:
        reason = "EOD_SQUAREOFF"
    elif elapsed >= cfg.no_progress_bars * 300 and pos["peak_bid"] < pos.get('initial_bid',pos['entry_price']) + jump:
        reason = "NO_PROGRESS"
    elif elapsed >= (max(cfg.max_hold_bars_5m*5,cfg.runner_max_hold_minutes)*60
                     if cfg.staged_profit and pos.get('partial_booked') else cfg.max_hold_bars_5m*300):
        reason = "TIME_EXIT"
    if reason:
        pos['exit_diagnostics']={'trigger_reason':reason,'trigger_epoch':stamp,'best_bid':best_bid,
            'executable_vwap':executable,'modeled_fill':price,'net_at_exit':net,
            'cash_loss_limit':cfg.rupee_max_risk,'cash_limit_overrun':round(max(0,-net-cfg.rupee_max_risk),2),
            'current_stop':pos['current_sl'],'peak_bid':pos['peak_bid'],
            'peak_executable_vwap':pos.get('peak_executable_vwap'),
            'breakout_failure':pos.get('breakout_failure'),'estimated_order_flow':exit_flow or None,'monitor':monitor}
    return (price, reason) if reason else None


def executed_stock_outcomes(ledger, gate):
    """Executed outcomes remain risk/learning evidence after reporting exclusions."""
    records={str(t.get("entry_id",i)):dict(t) for i,t in enumerate(ledger.get("trades",[]))}
    path=getattr(gate,"path",None)
    if isinstance(path,(str,os.PathLike)):
        from pathlib import Path
        for filename in Path(path).glob("*.json"):
            try:
                saved=json.loads(filename.read_text(encoding="utf-8"))
                trade=saved.get("close_details",{})
                if saved.get("state")=="closed" and trade.get("entry_id") and "net_pnl" in trade:
                    records[str(trade["entry_id"])]=trade
            except (OSError,ValueError,TypeError):
                raise ValueError("Unreadable execution history; reconcile before new entries")
    return sorted(records.values(),key=lambda t:t.get("exit_timestamp",t.get("timestamp","")))


def stock_daily_risk_reason(outcomes, positions, cfg, today, additional_risk=0):
    trades=sorted((t for t in outcomes if str(t.get("timestamp","")).startswith(today)),
                  key=lambda t:t.get("exit_timestamp",t.get("timestamp","")))
    net=sum(float(t["net_pnl"]) for t in trades)
    losses=0
    for trade in reversed(trades):
        if float(trade["net_pnl"])>0:break
        losses+=1
    if losses>=cfg.max_consec_losses:return "Consecutive net-loss limit reached"
    reserved=sum(float(p.get("expected_net_risk",p.get("opt_1r",0)*p.get("qty",0))) for p in positions)
    if net-reserved-float(additional_risk)<-cfg.daily_loss_limit or net<=-cfg.daily_loss_limit:
        return "Daily loss budget including open-position risk exhausted"
    return None


@single_bot_session("STOCK")
def run_dynamic_stock_options_session(live_mode, scan_once, cfg, logger):
    if live_mode:
        raise RuntimeError("LIVE disabled: broker fill and exit reconciliation is not implemented; use PAPER.")
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
    logger.info("Stock options: max %d/day | net target Rs.%.0f | cash loss exit Rs.%.0f per position",
                cfg.max_daily_trades, cfg.rupee_target, cfg.rupee_max_risk)
    logger.info("Entry profile: gap continuation %s | cash-target extension %s | maximum %d lots",
                cfg.enable_opening_trend,cfg.extend_rupee_target,cfg.max_lots)
    logger.info("Market quote refresh: %.1fs; position checks also run between screening requests",
                cfg.poll_interval_sec)
    logger.info('Stock PAPER runner upper cap Rs.%.0f | staged profit %s | extended runner cap %.0f minutes',
                cfg.runner_target,cfg.staged_profit,cfg.runner_max_hold_minutes)
    market_quotes = {}
    runner_chains = {}
    next_refresh = 0.0
    servicing = False
    opening_response_received = False

    def service_market_data():
        # Synchronous cooperation keeps ledger/order ownership on this thread.
        # A slow HTTP request can exceed the cadence; never overlap requests to
        # try to catch up. The guard prevents quote pacing from recursing here.
        nonlocal market_quotes, next_refresh, servicing, opening_response_received
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
                for pos in positions:
                    update_position_quote(pos,quotes.get(str(pos['opt_security_id']),{}),now.timestamp())
                    if telegram:telegram.position(pos, position_key=pos["slot"])
                for pos in positions[:]:
                    urgent_quote=quotes.get(str(pos["opt_security_id"]),{})
                    try:urgent_bid=float(urgent_quote['depth']['buy'][0]['price'])
                    except (KeyError,TypeError,ValueError,IndexError):continue
                    urgent=paper_exit(pos,urgent_quote,now,cfg,urgent_only=True)
                    if urgent and urgent[1] in ("CASH_LOSS_LIMIT","TRAIL_STOP","PREMIUM_STOP","EOD_SQUAREOFF","NO_PROGRESS","TIME_EXIT","RUNNER_NET_TARGET","RUPEE_TARGET_TRAILED","UNDERLYING_BREAKOUT_FAILED"):
                        rec=close_paper_position(ledger,pos,*urgent,now)
                        gate.mark_closed(today,rec,pos["slot"])
                        positions.remove(pos)
                        logger.info("[PAPER OPTSTK EXIT] %s | %s | net Rs.%+.2f",pos["contract"],urgent[1],rec["net_pnl"])
                        if telegram:telegram.exited({**pos, **rec}, position_key=pos["slot"], ledger_trades=ledger["trades"])
                for pos in positions[:]:
                    if (pos.get('breakout_confirmation') and pos.get('eq_security_id')
                            and time.time()-pos.get('breakout_check_epoch',0)>=30):
                        checked=time.time();pos['breakout_check_epoch']=checked
                        minutes=client.fetch_intraday_candles(pos['eq_security_id'],interval='1',
                            from_date=datetime.fromtimestamp(max(pos['entry_epoch'],checked-300),IST).isoformat(),
                            to_date=datetime.fromtimestamp(checked,IST).isoformat(),use_cache=False)
                        pos['breakout_failure']=stock_breakout_failure(pos,minutes or {},time.time())
                    quote=quotes.get(str(pos['opt_security_id']),{})
                    try:
                        bid=float(quote['depth']['buy'][0]['price'])
                        net=stock_position_value(pos,bid)['net']
                        flow = getattr(client, "order_flow", None)
                        flow_now = time.time()
                        closed_start = int(flow_now) // 300 * 300 - 300
                        if (isinstance(flow, DhanCandleOrderFlow) and pos.get("eq_security_id")
                                and (net >= pos.get("net_profit_target", cfg.rupee_target) or pos.get("base_target_locked"))
                                and closed_start + 300 > pos["entry_epoch"]
                                and closed_start != pos.get("exit_flow_checked_candle")):
                            pos["exit_flow_checked_candle"] = closed_start
                            bar = client.fetch_intraday_candles(
                                pos["eq_security_id"], interval="5",
                                from_date=datetime.fromtimestamp(closed_start, IST),
                                to_date=datetime.fromtimestamp(closed_start + 300, IST),
                            )
                            if bar and closed_start in bar["timestamp"]:
                                index = bar["timestamp"].index(closed_start)
                                pos["exit_order_flow"] = flow.snapshot(pos["eq_security_id"], closed_start, int(bar["volume"][index]))
                        if net>=pos.get('net_profit_target',cfg.rupee_target):
                            # Refresh held-contract Greeks/OI only when a runner is relevant.
                            context=runner_chains.get(str(pos['opt_security_id'])) or {}
                            if time.time()-context.get('received_epoch',0)>30:
                                fresh_chain=client.fetch_option_chain(pos['eq_security_id'],pos['expiry'])
                                context={'received_epoch':time.time(),'chain':fresh_chain or {}}
                                runner_chains[str(pos['opt_security_id'])]=context
                            decision_now=datetime.now(IST)
                            old=(pos.get('runner_assessment') or {}).get('hold')
                            pos['runner_assessment']=assess_stock_runner(pos,quote,
                                market_quotes.get(str(pos.get('eq_security_id','')),{}),context.get('chain',{}),decision_now.timestamp(),cfg)
                            if old!=pos['runner_assessment']['hold']:
                                logger.info('[STOCK RUNNER] %s | %s',pos['symbol'],json.dumps(pos['runner_assessment']))
                            now=decision_now
                    except (KeyError,TypeError,ValueError,IndexError):
                        pos['runner_assessment']={'hold':False,'reason':'Runner quote malformed'}
                    now=datetime.now(IST)
                    latest_closed = int(now.timestamp()) // 300 * 300 - 300
                    adverse = order_flow_direction(pos.get("exit_order_flow"), latest_closed)
                    partial = None
                    if not adverse or adverse == (1 if pos["opt_type"] == "CE" else -1):
                        partial=book_stock_partial(pos,quote,now,cfg,gate,today)
                    if partial:
                        logger.info('[PAPER OPTSTK PARTIAL] %s | %d units @ %.2f | booked net Rs.%+.2f',
                            pos['contract'],partial['qty'],partial['price'],partial['booked_net'])
                        if telegram:telegram.partial(pos, partial, position_key=pos["slot"])
                    decision = paper_exit(pos, quotes.get(str(pos["opt_security_id"]), {}), now, cfg)
                    if decision:
                        rec = close_paper_position(ledger, pos, *decision, now)
                        gate.mark_closed(today, rec, pos["slot"])
                        positions.remove(pos)
                        logger.info("[PAPER OPTSTK EXIT] %s | %s | net Rs.%+.2f", pos["contract"], decision[1], rec["net_pnl"])
                        if telegram:
                            telegram.exited({**pos, **rec}, position_key=pos["slot"], ledger_trades=ledger["trades"])
                    else:
                        state = (pos["current_sl"], pos["peak_bid"])
                        quote_due=(pos.get('quote_received_epoch',0)-pos.get('persisted_quote_epoch',0)>=30)
                        if state != tuple(pos.get("persisted_state") or ()) or quote_due:
                            pos["persisted_state"] = state
                            pos['persisted_quote_epoch']=pos.get('quote_received_epoch',0)
                            gate.update_position(today, pos["slot"], pos)
                        if telegram:telegram.position(pos, position_key=pos["slot"])
            if SESSION_START_TIME <= now.time().replace(tzinfo=None) < SESSION_END_TIME:
                # Clear a failed refresh instead of reusing an older scan quote.
                if not opening_response_received:logger.info('[OPENING DATA REQUEST] STOCKS | %s',datetime.now(IST).isoformat(timespec='milliseconds'))
                security_ids = (list(universe) if now.time().replace(tzinfo=None) < SCAN_END_TIME
                                else list({str(p["eq_security_id"]) for p in positions if p.get("eq_security_id")}))
                market_quotes = client.fetch_batch_ohlc(security_ids).get("NSE_EQ", {}) if security_ids else {}
                if market_quotes and not opening_response_received:
                    opening_response_received=True
                    logger.info('[OPENING DATA RESPONSE] STOCKS | %s',datetime.now(IST).isoformat(timespec='milliseconds'))
                for pos in positions:
                    observed=market_quotes.get(str(pos.get('eq_security_id','')),{})
                    if 0<=quote_received_age(observed,time.time())<=8:
                        stamp=float(observed['received_epoch']);spot=float(observed['last_price'])
                        history=[v for v in pos.get('underlying_observations',[]) if stamp-float(v[0])<=600]
                        if not history or stamp>history[-1][0]:history.append([stamp,spot,observed.get('volume')])
                        pos['underlying_observations']=history
        finally:
            servicing = False

    client.before_market_request = service_market_data
    last_scan_only_reason = None
    try:
        if not scan_once:
            client.start_order_flow(universe)
        while True:
            now = datetime.now(IST)
            now_time = now.time().replace(tzinfo=None)
            if now.strftime("%Y-%m-%d") != today or now.weekday() >= 5:
                return
            if not positions and now_time < SESSION_START_TIME:
                if scan_once:return
                time.sleep(session_open_wait_seconds(now,SESSION_START_TIME))
                continue
            if now_time >= SCAN_END_TIME and not positions:
                return
            service_market_data()
            now_time = datetime.now(IST).time().replace(tzinfo=None)
            if now_time >= SCAN_END_TIME:
                if not positions:
                    return
                time.sleep(max(0.05, next_refresh-time.monotonic()))
                continue
            if now_time < ENTRY_START_TIME:
                if scan_once:return
                time.sleep(cfg.poll_interval_sec)
                continue
            outcomes=[] if scan_once else executed_stock_outcomes(ledger,gate)
            completed = [t for t in outcomes if str(t.get("timestamp", "")).startswith(today)]
            risk_reason=None if scan_once else stock_daily_risk_reason(outcomes,positions,cfg,today)
            consumed = 0 if scan_once else max(gate.count_entries(today), len(completed)+len(positions))
            scan_only_reason = risk_reason
            if consumed >= cfg.max_daily_trades:
                scan_only_reason = "Daily stock entry cap reached"
            elif len(positions) >= cfg.max_concurrent:
                scan_only_reason = scan_only_reason or "All stock position slots occupied"
            if scan_only_reason:
                if scan_only_reason != last_scan_only_reason:
                    logger.info("[STOCK SCAN ONLY] %s | Scanning through 13:30 IST", scan_only_reason)
                last_scan_only_reason = scan_only_reason
                for change, sec_id, ltp in shortlist_movers(market_quotes, cfg):
                    service_market_data()
                    scan_now = datetime.now(IST)
                    if scan_now.strftime("%Y-%m-%d") != today or scan_now.time().replace(tzinfo=None) >= SCAN_END_TIME:
                        break
                    stock = universe.get(sec_id)
                    if not stock:
                        continue
                    reasons = []
                    membership = verify_live_scanx_and_resolve_stock_option(
                        client, stock, ltp, change, cfg, reasons=reasons,
                        market_quotes=market_quotes, membership_only=True,
                    )
                    if not membership:
                        logger.info("[OPTSTK REJECT] %s | %s", stock["symbol"],
                                    reasons[0] if reasons else "Scanner membership unavailable")
                time.sleep(max(0.05, next_refresh-time.monotonic()))
                continue
            last_scan_only_reason = None
            wallet = cfg.initial_capital if scan_once else ledger["virtual_balance_net"]
            wallet -= sum(p["entry_price"]*p["qty"] + p["estimated_costs"] for p in positions)
            shortlist = shortlist_movers(market_quotes, cfg)
            signals = []
            candidates = []
            used_symbols = {p["symbol"] for p in positions}
            if not cfg.reentry_after_loss:used_symbols|={t['symbol'] for t in completed}
            def screened_candidates():
                pool=[]
                pool_started=None
                def ranked_pool():
                    ranked=rank_candidates(pool,executed_stock_outcomes(ledger,gate),time.time())
                    comparison=[{'symbol':c['symbol'],**c['selection_analysis']} for c in ranked]
                    if comparison:logger.info('[STOCK SELECTION] %s',json.dumps(comparison))
                    for candidate in ranked:
                        candidate['selection_comparison']=comparison
                        yield candidate
                    pool.clear()
                for change, sec_id, ltp in shortlist:
                    service_market_data()
                    # Compare a small live group, not the entire slow universe.
                    # Check the soft deadline between requests; final quotes are
                    # rebuilt before entry even when a request runs long.
                    if pool and time.monotonic()-pool_started>=8:
                        yield from ranked_pool()
                        pool_started=None
                    if datetime.now(IST).strftime("%Y-%m-%d") != today or datetime.now(IST).time().replace(tzinfo=None) >= SCAN_END_TIME:
                        break
                    if sec_id not in universe or universe[sec_id]["symbol"] in used_symbols:
                        continue
                    if cfg.reentry_after_loss:
                        reason=stock_reentry_reason(universe[sec_id]['symbol'],completed,time.time())
                        if reason:
                            logger.info('[OPTSTK REENTRY WAIT] %s | %s',universe[sec_id]['symbol'],reason)
                            continue
                    signal_reasons = []
                    sig = verify_live_scanx_and_resolve_stock_option(client, universe[sec_id], ltp, change, cfg, reasons=signal_reasons,market_quotes=market_quotes)
                    if sig:
                        if cfg.reentry_after_loss:
                            reason=stock_reentry_reason(sig['symbol'],completed,time.time(),sig['signal_timestamp'])
                            if reason:
                                logger.info('[OPTSTK REENTRY WAIT] %s | %s',sig['symbol'],reason)
                                continue
                        signals.append(sig)
                        qualified, _ = qualify_stock_contracts(client, sig, cfg, wallet, "OPTSTK REJECT")
                        if qualified:
                            candidates.append(qualified)
                            if not scan_once:
                                if not pool:pool_started=time.monotonic()
                                pool.append(qualified)
                                if len(pool)>=3:
                                    yield from ranked_pool()
                                    pool_started=None
                    else:
                        logger.info("[OPTSTK REJECT] %s | %s", universe[sec_id]["symbol"], signal_reasons[0] if signal_reasons else "Setup unavailable")
                if pool:yield from ranked_pool()
            if scan_once:
                list(screened_candidates())
                ranked = rank_candidates(candidates, [], time.time())
                logger.info("Shortlist %d | confirmed %d | qualified %d", len(shortlist), len(signals), len(ranked))
                return
            capacity = min(cfg.max_concurrent-len(positions), cfg.max_daily_trades-consumed)
            entered = 0
            # Bounded comparison chooses among qualified setups; every choice
            # gets a fresh final recheck before a durable reservation.
            for best in screened_candidates():
                selection_comparison=best.get('selection_comparison',[])
                service_market_data()
                # Screening can now close a position. Recalculate cash and
                # reserved slots before qualification and never reuse its funds
                # or its symbol based on a stale scan-start snapshot.
                outcomes=executed_stock_outcomes(ledger,gate)
                completed = [t for t in outcomes if str(t.get("timestamp", "")).startswith(today)]
                if stock_daily_risk_reason(outcomes,positions,cfg,today,best.get("expected_net_risk",0)):
                    continue
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
                # Ranking may have taken minutes. Rebuild the candidate from current
                # closed candles and a newly received chain before any reservation.
                # The verifier owns the structural price band; a second band here
                # would reject valid pullbacks before they can be re-evaluated.
                signal_reasons = []
                if 0<=time.time()-best.get('verified_epoch',0)<10 and int(time.time()//300)==int(best['verified_epoch']//300):
                    best=recheck_recent_stock_candidate(client,best,cfg,market_quotes,signal_reasons)
                else:
                    best = verify_live_scanx_and_resolve_stock_option(client, universe[best["eq_security_id"]], best.get('spot_ltp',0), 0, cfg, reasons=signal_reasons,market_quotes=market_quotes)
                if not best:
                    logger.info("[OPTSTK RECHECK REJECT] %s", signal_reasons[0] if signal_reasons else "Setup no longer current")
                    continue
                symbol = best["symbol"]
                best, entry_quote = qualify_stock_contracts(client, best, cfg, wallet, "OPTSTK RECHECK REJECT")
                if best:
                    best.pop("alternate_contracts", None)
                now = datetime.now(IST)
                if not best or now.time().replace(tzinfo=None) >= SCAN_END_TIME:
                    continue
                refreshed=rank_candidates([best],executed_stock_outcomes(ledger,gate),now.timestamp())
                if not refreshed:
                    logger.info("[OPTSTK LEARNING PAUSE] %s | Recent closed net losses for this setup",symbol)
                    continue
                best=refreshed[0]
                best['selection_comparison']=selection_comparison
                # Recheck the final sized plan after screening may have serviced
                # exits or changed available cash/risk.
                outcomes=executed_stock_outcomes(ledger,gate)
                if stock_daily_risk_reason(outcomes,positions,cfg,today,best.get('expected_net_risk',0)):
                    continue
                available=ledger['virtual_balance_net']-sum(p['entry_price']*p['qty']+p['estimated_costs'] for p in positions)
                if best['opt_ltp']*best['qty']+best['estimated_costs']>available:
                    continue
                slot = next((i for i in range(1, cfg.max_daily_trades+1) if gate.read(today, i) is None), None)
                if slot is None:
                    break
                pos = {**best, "index": best["symbol"], "option_type": best["opt_type"], "slot": slot,
                    "contract": f"{best['symbol']} {best['strike']:g}_{best['opt_type']}",
                    "entry_price": best["opt_ltp"], "current_ltp": None,
                    "peak_bid": best.get('bid',0.0),"initial_bid":best.get('bid',best['opt_ltp']),
                    "current_sl": best["initial_sl"], "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_epoch": now.timestamp(), "entry_id": f"STOCK_{today}_{slot}"}
                update_position_quote(pos,entry_quote,now.timestamp())
                pos['persisted_quote_epoch']=pos.get('quote_received_epoch',0)
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
                        telegram.position(pos, entry=True, position_key=pos["slot"])
            logger.info("Shortlist %d | confirmed %d | qualified %d | entered %d",
                        len(shortlist), len(signals), len(candidates), entered)
            time.sleep(max(0.05, next_refresh-time.monotonic()))
    finally:
        client.before_market_request = None
        if not scan_once:
            client.stop_order_flow()
        if telegram:
            telegram.close()


def simulate_fixed_premium_trade(entry_ts, bars, qty, cfg, opt_type="CE"):
    """Conservative fixed-strike OHLC replay; incomplete paths are unvalidated.

    This is a price-path simulation, not a reconstruction of historical depth,
    Greeks, contract selection or the live runner overlay.
    """
    first = bars.get(entry_ts)
    if not first or first.get("strike", 0) <= 0 or not 3 <= first.get("open", 0) <= cfg.max_premium:
        return None
    entry = round_tick(first["open"] * (1 + cfg.slippage_pct / 100))
    plan = cfg.plan(entry, qty)
    if not plan:
        return None
    stop, peak = max(plan["initial_sl"],plan['cash_loss_stop_price']), entry
    target = min(plan["hard_target_price"], plan["rupee_target_price"])
    entry_strike = float(first["strike"])
    for offset in range(cfg.max_hold_bars_5m):
        ts = entry_ts + offset * 300
        bar = bars.get(ts)
        if not bar or float(bar.get("strike", 0)) != entry_strike:
            return None
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if not all(math.isfinite(x) and x > 0 for x in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
            return None
        bar_time = datetime.fromtimestamp(ts, IST).time().replace(tzinfo=None)
        close_time = datetime.fromtimestamp(ts + 300, IST).time().replace(tzinfo=None)
        # The open is observed before either intrabar extreme. Gaps fill at the
        # executable open; otherwise stop-first resolves unknowable bar order.
        if bar_time >= SESSION_END_TIME:
            price, reason, exit_ts = o, "EOD_SQUAREOFF", ts
        elif o <= stop:
            price, reason, exit_ts = o, ("CASH_LOSS_LIMIT" if stop == plan['cash_loss_stop_price'] and stop > plan['initial_sl'] else "TRAIL_STOP" if stop > plan["initial_sl"] else "PREMIUM_STOP"), ts
        elif o >= target:
            price, reason, exit_ts = o, "RUPEE_TARGET", ts
        elif l <= stop:
            price, reason, exit_ts = stop, ("CASH_LOSS_LIMIT" if stop == plan['cash_loss_stop_price'] and stop > plan['initial_sl'] else "TRAIL_STOP" if stop > plan["initial_sl"] else "PREMIUM_STOP"), ts + 300
        elif h >= target:
            price, reason, exit_ts = target, "RUPEE_TARGET", ts + 300
        elif close_time >= SESSION_END_TIME:
            price, reason, exit_ts = c, "EOD_SQUAREOFF", ts + 300
        else:
            peak = max(peak,h)
            price, reason, exit_ts = c, None, ts + 300
            if offset + 1 >= cfg.no_progress_bars and peak < entry + plan["trail_jump"]:
                reason = "NO_PROGRESS"
            elif offset == cfg.max_hold_bars_5m - 1:
                reason = "TIME_EXIT"
            # Ratchets from this bar only become executable on the next bar.
            jump = plan["trail_jump"]
            peak_fill=math.floor(max(0.05,peak*(1-cfg.slippage_pct/100))*20+1e-9)/20
            peak_net=stock_position_value({'entry_price':entry,'qty':plan['qty']},peak_fill)['net']
            locked_net=stock_profit_protection_net(peak_net)
            if locked_net is not None:
                floor=stock_net_protection_bid({'entry_price':entry,'qty':plan['qty']},peak,locked_net,cfg)
                if floor is not None:stop=max(stop,floor)

        if reason:
            price = math.floor(max(0.05,price*(1-cfg.slippage_pct/100))*20+1e-9)/20
            return {**plan,"entry_price":entry,"exit_price":price,"exit_reason":reason,
                    "exit_timestamp":datetime.fromtimestamp(exit_ts,IST).strftime("%Y-%m-%d %H:%M:%S"),
                    "strike":entry_strike,"simulation_scope":("BASE_EXIT_ONLY_EXCLUDES_LIVE_STAGED_RUNNER"
                        if cfg.staged_profit else "FIXED_STRIKE_OHLC_ONLY")}
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
    scanner_rejections=Counter()
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
            if prev_close is None or not ENTRY_START_TIME <= dt.time().replace(tzinfo=None) < SCAN_END_TIME:
                continue
            snapshots.setdefault(ts+300, {})[sec] = {"last_price": last_close, "ohlc": {"close": prev_close}}
            # Live scans shortlist by day move before evaluating candle history.
            if abs((last_close/prev_close-1)*100)<cfg.min_day_change_pct:continue
            # The current scanner requests five calendar days from midnight.
            # Do not give its replay an expanding multi-month EMA warmup.
            lower=bisect_left(candles['timestamp'],(dt-timedelta(days=5)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()) if current_implementation else 0
            prefix = {k: v[lower:i+1] for k, v in candles.items() if isinstance(v, list)}
            session_volume=sum(float(v) for t,v in zip(prefix['timestamp'],prefix['volume'])
                               if datetime.fromtimestamp(t,IST).date()==day)
            membership=scanx_stock_membership(prefix,{'last_price':last_close,
                'ohlc':{'close':prev_close},'volume':session_volume,'received_epoch':ts+300},
                ts+300,cfg.min_day_change_pct,cfg.ema_period)
            if not membership['allowed']:
                scanner_rejections[membership['reason']]+=1
                continue
            sig = evaluate_stock_setup(prefix, ts+300, cfg.min_day_change_pct, cfg.ema_period,
                                        enable_opening_trend=cfg.enable_opening_trend,confirm_window_sec=cfg.confirm_window_sec,
                                        require_breakout=cfg.require_confirmed_breakout)
            if sig:
                signals[(ts+300, sec)] = sig
    ledger = {"mode": "BACKTEST", "initial_capital": cfg.initial_capital, "trades": [],
              "entry_configuration":{"opening_trend":cfg.enable_opening_trend,"extend_rupee_target":cfg.extend_rupee_target,
                  "require_confirmed_breakout":cfg.require_confirmed_breakout,
                  "staged_profit":cfg.staged_profit,"runner_max_hold_minutes":cfg.runner_max_hold_minutes,
                  "rupee_target":cfg.rupee_target,"rupee_max_risk":cfg.rupee_max_risk,"max_lots":cfg.max_lots,
                  "min_net_rr":cfg.min_rr_target,"max_spread_pct":cfg.max_spread_pct,"confirm_window_sec":cfg.confirm_window_sec,
                  "signal_lookback_calendar_days":5 if current_implementation else None,
                  "daily_loss_limit":cfg.daily_loss_limit,"max_consecutive_net_losses":cfg.max_consec_losses},
              "limitations": ["Current universe and lot sizes; historical membership not reconstructed",
                              "Five-minute OHLC lacks bid/ask and intrabar path; modeled slippage, not verified fills",
                              "Missing bars or rolling ATM strike changes are unvalidated, never synthetic",
                              "Exit-policy replay only: historical broker Greeks, IV range, DTE and delta-selected contracts unavailable",
                              "Trailing stops advance on following bars; same-bar trail path is unvalidated",
                              "Live staged profit/runner decisions require archived decision-time quotes, volume and OI; not reconstructed by this base-exit replay"]}
    used_days, option_maps, skipped = {}, {}, 0
    data_skips, strategy_rejections = Counter(), Counter()
    active_until = []
    used_symbols = {}
    unresolved_days=set()
    for ts in sorted(snapshots):
        entry_time = datetime.fromtimestamp(ts, IST)
        if entry_time.time() >= SCAN_END_TIME:
            continue
        day = entry_time.strftime("%Y-%m-%d")
        active_until = [end_ts for end_ts in active_until if end_ts > ts]
        if used_days.get(day, 0) >= cfg.max_daily_trades or len(active_until) >= cfg.max_concurrent:
            continue
        if day in unresolved_days:continue
        known=[t for t in ledger['trades'] if datetime.strptime(t['exit_timestamp'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=IST).timestamp()<=ts]
        pending=[t for t in ledger['trades'] if t not in known]
        risk_reason=stock_daily_risk_reason(known,pending,cfg,day)
        if risk_reason:
            strategy_rejections[risk_reason]+=1
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
            risk_reason=stock_daily_risk_reason(known,pending,cfg,day,best.get('expected_net_risk',0))
            if risk_reason:
                strategy_rejections[risk_reason]+=1
                continue
            wallet -= entry*best["qty"] + best["estimated_costs"]
            # Consume date BEFORE looking at future outcome, including missing data.
            used_symbols.setdefault(day, set()).add(best["symbol"])
            active_until.append(ts + cfg.max_hold_bars_5m*300)
            used_days[day] = used_days.get(day, 0) + 1
            sim = simulate_fixed_premium_trade(ts, option_maps[best["map_key"]], best["lot_size"], cfg, best["opt_type"])
            if sim is None:
                unresolved_days.add(day)
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
            pending.append(ledger["trades"][-1])
    ledger['limitations'].append('Live selective up-sizing and the PAPER runner require fresh depth, Greeks and underlying quotes; this baseline historical replay does not reproduce those decisions.')
    update_ledger_metrics(ledger)
    ledger["unresolved_session_dates"]=sorted(unresolved_days)
    ledger["unvalidated_candidates_or_outcomes"] = skipped
    ledger["stock_coverage"] = len(frames)
    events = client.history_events
    scanner_reference_missing=any('unavailable' in reason.lower() or 'incomplete' in reason.lower()
                                  for reason in scanner_rejections)
    complete = not events and not skipped and not scanner_reference_missing and len(frames) == len(stocks)
    identity_unverified = bool(ledger["trades"])
    ledger["limitations"].append("Five-minute rolling bars may disagree with one-minute prices for the labeled strike; scored contract prices require cross-interval verification")
    ledger.update(status="COMPLETED_EXIT_REPLAY" if complete and not identity_unverified else "INCOMPLETE_DATA",
                  data_coverage_complete=complete, full_strategy_validated=False,
                  price_identity_validated=False,
                  data_rejection_counts=dict(data_skips), strategy_rejection_counts=dict(strategy_rejections),
                  scanner_rejection_counts=dict(scanner_rejections),
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
    parser = argparse.ArgumentParser(description="Selective stock options: confirmed setups, maximum four entries per day and two concurrent positions")
    parser.add_argument("--live", action="store_true", help="Submit capped rupee-target brackets with broker trailing stops; verify fills in Dhan")
    parser.add_argument("--scan-once", action="store_true", help="Rank current qualifying setups without trading")
    parser.add_argument("--backtest-days", type=int, default=0)
    parser.add_argument("--max-stocks", type=int, default=0, help="Historical coverage; 0 scans the full universe, positive values limit alphabetically")
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
        try:
            run_dynamic_stock_options_session(args.live, args.scan_once, strat_cfg, log)
        except Exception:
            log.exception('Stock-options session stopped unexpectedly; saved reservations must be recovered before new entries.')
            raise

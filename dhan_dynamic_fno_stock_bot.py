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
from typing import Dict, Any, List, Optional, Tuple
import requests
from collections import Counter
from stock_data_quality import HistoricalDataMixin, quote_received_age
from stock_option_signals import evaluate_stock_setup
from stock_option_policy import premium_plan, rank_candidates, DailyTradeGate

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from telegram_notifier import TelegramNotifier
except ImportError:
    TelegramNotifier = None

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


def compute_ema_series(closes: List[float], period: int) -> List[float]:
    if not closes:
        return []
    k = 2.0 / (period + 1.0)
    ema = [float(closes[0])]
    for price in closes[1:]:
        ema.append((float(price) * k) + (ema[-1] * (1.0 - k)))
    return ema


def compute_atr_series(highs: List[float], lows: List[float], closes: List[float], period: int = 10) -> List[float]:
    if not closes:
        return []
    trs: List[float] = [max(0.05, float(highs[0]) - float(lows[0]))]
    for i in range(1, len(closes)):
        h, l, pc = float(highs[i]), float(lows[i]), float(closes[i - 1])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(max(0.05, tr))
    return compute_ema_series(trs, period=period)


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
        self.min_rr_target = float(getattr(args, "min_rr", None) or env.get("STOCK_OPT_MIN_NET_RR", 1.5))
        self.slippage_pct = float(env.get("STOCK_OPT_SLIPPAGE_PCT", 0.1))
        self.max_spread_pct = float(env.get("STOCK_OPT_MAX_SPREAD_PCT", 2.5))
        self.max_hold_bars_5m = int(env.get("STOCK_OPT_MAX_HOLD_BARS_5M", 12))
        self.rupee_target = float(env.get("STOCK_OPT_RUPEE_TARGET", 1200))
        self.rupee_max_risk = float(env.get("STOCK_OPT_RUPEE_MAX_RISK", 1800))
        self.max_lots = int(env.get("STOCK_OPT_MAX_LOTS", 2))
        self.min_days_to_expiry = int(env.get("STOCK_OPT_MIN_DAYS_TO_EXPIRY", 4))
        self.min_delta = float(env.get("STOCK_OPT_MIN_DELTA", 0.38))
        self.max_delta = float(env.get("STOCK_OPT_MAX_DELTA", 0.78))
        self.max_theta_pct_per_day = float(env.get("STOCK_OPT_MAX_THETA_PCT_PER_DAY", 6))
        self.no_progress_bars = int(env.get("STOCK_OPT_NO_PROGRESS_BARS", 5))
        self.max_premium = float(env.get("STOCK_OPT_MAX_PREMIUM", 1000))
        self.gate_dir = "stock_option_daily_entries"
        numbers = (self.target_pct, self.stop_pct, self.min_rr_target, self.slippage_pct,
                   self.max_spread_pct, self.rupee_target, self.rupee_max_risk,
                   self.min_delta, self.max_delta, self.max_theta_pct_per_day, self.max_premium)
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError("Stock option settings must be finite")
        if not (3 <= self.target_pct <= 20 and 0.5 <= self.stop_pct <= 10 and self.min_rr_target >= 1.2
                and 0 <= self.slippage_pct < 100 and 0 < self.max_spread_pct <= 5
                and 1 <= self.max_concurrent <= self.max_daily_trades <= 2
                and self.rupee_target > 0 and self.rupee_max_risk > 0 and self.max_lots >= 1
                and 0 < self.min_delta < self.max_delta <= 1 and self.min_days_to_expiry >= 4
                and self.max_theta_pct_per_day > 0 and self.max_premium >= 3
                and 1 <= self.no_progress_bars <= self.max_hold_bars_5m):
            raise ValueError("Invalid stock-option target, risk, Greek or entry limits")

    def plan(self, entry: float, qty: int):
        return premium_plan(entry, qty, compute_fno_option_charges, self.target_pct,
                            self.stop_pct, self.min_rr_target, self.slippage_pct,
                            self.rupee_target, self.max_lots, self.rupee_max_risk)


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


class DhanStockOptionsClient(HistoricalDataMixin):
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: str, access_token: str, logger: logging.Logger):
        self.client_id = client_id
        self.access_token = access_token
        self.logger = logger
        self.last_chart_ts = 0.0
        self.last_quote_ts = 0.0
        self.last_chain_ts = 0.0
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
        time.sleep(max(0, 1.05 - (time.monotonic() - self.last_quote_ts)))
        self.last_quote_ts = time.monotonic()

    def fetch_option_chain(self, underlying_id, expiry):
        expiry = normalize_expiry(expiry)
        if not expiry:
            self.logger.warning("Invalid option expiry; skipping chain request")
            return {}
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
    sig = evaluate_stock_setup(candles, time.time(), cfg.min_day_change_pct, cfg.ema_period)
    if not sig:
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
        if not 0 <= now_epoch - (sig["signal_timestamp"] + 300) < 300:
            return reject("Confirmation candle is stale or not yet tradable")
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
        if spread > cfg.max_spread_pct:
            return reject(f"Spread {spread:.2f}% above {cfg.max_spread_pct:.2f}%")
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
        if net_gain < cfg.rupee_target * 0.75:
            return reject(f"A 1.8 ATR move nets only Rs.{net_gain:.0f} of the Rs.{cfg.rupee_target:.0f} target (75% minimum)")
        # Cross-sectional IV range in the same expiry/side, not historical IV rank.
        low, high = float(sig["iv_range_low"]), float(sig["iv_range_high"])
        if not all(math.isfinite(v) for v in (low, high)) or high <= low:
            return reject("Unusable chain IV range")
        if (iv-low)/(high-low) >= 0.8:
            return reject(f"IV in the top {100*(1-(iv-low)/(high-low)):.0f}% of the chain")
        try:
            last_trade_age = now_epoch - datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST).timestamp()
        except (KeyError, TypeError, ValueError):
            last_trade_age = None
        return {**sig, **plan, "opt_ltp": ask, "bid": bid, "spread_pct": spread,
                "dte": dte, "theta_pct_per_day": theta_pct, "expected_move_gain": net_gain,
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
    elif net >= pos.get("net_profit_target", cfg.rupee_target):
        reason = "RUPEE_TARGET"
    elif bid >= pos["hard_target_price"]:
        reason = "PREMIUM_TARGET"
    elif now.time().replace(tzinfo=None) >= dtime(15, 25):
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
    try:
        while True:
            now = datetime.now(IST)
            if now.strftime("%Y-%m-%d") != today or now.weekday() >= 5 or now.time().replace(tzinfo=None) >= dtime(15, 30):
                return
            if positions:
                quotes = client.fetch_option_quotes([p["opt_security_id"] for p in positions])
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
                        # JSON restores tuples as lists; normalize before comparing.
                        state = (pos["current_sl"], pos["peak_bid"])
                        if state != tuple(pos.get("persisted_state") or ()):
                            pos["persisted_state"] = state
                            gate.update_position(today, pos["slot"], pos)
            completed = [] if scan_once else [t for t in ledger["trades"] if str(t.get("timestamp", "")).startswith(today)]
            consumed = 0 if scan_once else max(gate.count_entries(today), len(completed)+len(positions))
            if consumed >= cfg.max_daily_trades or len(positions) >= cfg.max_concurrent:
                if not positions:
                    return
                time.sleep(1.1)
                continue
            if not dtime(9, 30) <= now.time().replace(tzinfo=None) <= dtime(14, 30):
                if scan_once or (now.hour >= 14 and not positions):
                    return
                time.sleep(2)
                continue
            wallet = cfg.initial_capital if scan_once else ledger["virtual_balance_net"]
            wallet -= sum(p["entry_price"]*p["qty"] + p["estimated_costs"] for p in positions)
            quotes = client.fetch_batch_ohlc(list(universe)).get("NSE_EQ", {})
            shortlist = shortlist_movers(quotes, cfg)
            signals = []
            candidates = []
            used_symbols = {p["symbol"] for p in positions} | {t["symbol"] for t in completed}
            for change, sec_id, ltp in shortlist:
                if sec_id not in universe or universe[sec_id]["symbol"] in used_symbols:
                    continue
                signal_reasons = []
                sig = verify_live_scanx_and_resolve_stock_option(client, universe[sec_id], ltp, change, cfg, reasons=signal_reasons)
                if sig:
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
            ranked = rank_candidates(candidates)
            logger.info("Shortlist %d | confirmed %d | qualified %d", len(shortlist), len(signals), len(ranked))
            if scan_once:
                return
            capacity = min(cfg.max_concurrent-len(positions), cfg.max_daily_trades-consumed)
            entered = 0
            for best in ranked:
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
                if not best or now.time().replace(tzinfo=None) > dtime(14, 30):
                    continue
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
            time.sleep(1.1 if positions else 30)
    finally:
        if telegram:
            telegram.close()


def simulate_fixed_premium_trade(entry_ts, bars, qty, cfg):
    """Real OHLC only. Reject missing bars/rolling-strike changes; stop first on ties."""
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
        if l <= stop:
            price, reason = min(o, stop), "TRAIL_STOP" if stop > plan["initial_sl"] else "PREMIUM_STOP"
        elif h >= target:
            price, reason = target, "RUPEE_TARGET"
        elif datetime.fromtimestamp(ts, IST).time().replace(tzinfo=None) >= dtime(15,25):
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


def run_stock_options_historical_backtest(days, max_stocks, cfg, logger):
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
        candles = client.fetch_intraday_candles(sec, interval="5", from_date=start.strftime("%Y-%m-%d"),
                                                to_date=end.strftime("%Y-%m-%d"), use_cache=True)
        # Evaluate available setups; retain coverage issues in the final report.
        if not candles or not candles.get("timestamp"):
            continue
        frames[sec] = stock
        last_day, prev_close, last_close = None, None, None
        for i, ts in enumerate(candles["timestamp"]):
            ts = int(ts)
            if ts < start.timestamp() or ts+300 > end.timestamp():
                continue
            dt = datetime.fromtimestamp(ts+300, IST)
            day = dt.date()
            if day != last_day:
                prev_close, last_day = last_close, day
            last_close = float(candles["close"][i])
            if prev_close is None or not dtime(9, 30) <= dt.time().replace(tzinfo=None) <= dtime(14, 30):
                continue
            snapshots.setdefault(ts+300, {})[sec] = {"last_price": last_close, "ohlc": {"close": prev_close}}
            # Slice to already-closed bars; identical setup evaluator to live scanning.
            prefix = {k: v[:i+1] for k, v in candles.items() if isinstance(v, list)}
            sig = evaluate_stock_setup(prefix, ts+300, cfg.min_day_change_pct, cfg.ema_period)
            if sig:
                signals[(ts+300, sec)] = sig
    ledger = {"mode": "BACKTEST", "initial_capital": cfg.initial_capital, "trades": [],
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
                continue
            key = (sec, sig["opt_type"])
            if key not in option_maps:
                raw = client.fetch_rolling_stock_option(sec, sig["opt_type"], start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
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
        ranked = rank_candidates(candidates)
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
                "gross_pnl": gross, "charges": fees, "net_pnl": round(gross-fees, 2), "data_source": "REAL_OPTION_OHLC_MODELED_FILLS"})
    update_ledger_metrics(ledger)
    ledger["unvalidated_candidates_or_outcomes"] = skipped
    ledger["stock_coverage"] = len(frames)
    events = client.history_events
    complete = not events and not skipped and len(frames) == len(stocks)
    ledger.update(status="COMPLETED_EXIT_REPLAY" if complete else "INCOMPLETE_DATA",
                  data_coverage_complete=complete, full_strategy_validated=False,
                  data_rejection_counts=dict(data_skips), strategy_rejection_counts=dict(strategy_rejections),
                  historical_data_issues=events, historical_data_coverage=client.history_coverage, stock_universe_requested=len(stocks),
                  period={"from_inclusive":start.isoformat(),"to_exclusive":end.isoformat()})
    atomic_write_json("stock_fno_backtest_ledger.json", ledger)
    if not complete:
        logger.error("BACKTEST INCOMPLETE: %d history issues, %d unvalidated signals/outcomes. Recorded P&L is not a validated strategy result.",len(events),skipped)
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
        result = run_stock_options_historical_backtest(args.backtest_days, args.max_stocks, strat_cfg, log)
        if result["status"] == "INCOMPLETE_DATA":
            raise SystemExit(2)
    else:
        run_dynamic_stock_options_session(args.live, args.scan_once, strat_cfg, log)

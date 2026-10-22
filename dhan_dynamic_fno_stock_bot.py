#!/usr/bin/env python3
"""Selective stock options: one confirmed shortlist entry per day, fixed premium exits.
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


class DynamicStrategyConfig:
    def __init__(self, env: Dict[str, str], args: argparse.Namespace):
        self.initial_capital = float(env.get("STOCK_OPT_INITIAL_CAPITAL", 50000.0))
        self.min_day_change_pct = float(getattr(args, "min_pct", None) or env.get("SCANX_MIN_DAY_PCT", 2.20))
        self.ema_period = int(env.get("SCANX_EMA_PERIOD", 10))
        self.max_concurrent = 1
        self.max_daily_trades = 1
        self.max_consec_losses = int(env.get("MAX_CONSECUTIVE_LOSSES_PER_DAY", 2))

        self.target_pct = float(env.get("STOCK_OPT_TARGET_PCT", 3.0))
        self.stop_pct = float(env.get("STOCK_OPT_STOP_PCT", 0.4))
        self.min_rr_target = float(getattr(args, "min_rr", None) or env.get("STOCK_OPT_MIN_NET_RR", 3.0))
        self.slippage_pct = float(env.get("STOCK_OPT_SLIPPAGE_PCT", 0.1))
        self.max_spread_pct = float(env.get("STOCK_OPT_MAX_SPREAD_PCT", 0.2))
        self.max_hold_bars_5m = 3
        self.gate_dir = "stock_option_daily_entries"
        if not all(math.isfinite(v) for v in (self.target_pct, self.stop_pct, self.min_rr_target, self.slippage_pct, self.max_spread_pct)):
            raise ValueError("Stock option settings must be finite")
        if not (2 <= self.target_pct <= 3 and 0 < self.stop_pct <= 0.5 and self.min_rr_target >= 3
                and self.slippage_pct >= 0 and 0 < self.max_spread_pct <= 0.2):
            raise ValueError("Require target 2-3%, stop <=0.5%, net RR >=3, and spread <=0.2%")

    def plan(self, entry: float, qty: int):
        return premium_plan(entry, qty, compute_fno_option_charges, self.target_pct,
                            self.stop_pct, self.min_rr_target, self.slippage_pct)


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
    def load_universe(cls, logger: logging.Logger) -> Dict[str, Dict[str, Any]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        cache_file = os.path.join(CACHE_DIR, f"dynamic_optstk_universe_{today_str}.json")
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
                exp_date = exp_str.split(" ")[0].strip()
                if exp_date < today_str:
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
            valid_exps = sorted({c["expiry"] for c in contracts})
            chosen_exp = valid_exps[0]
            if len(valid_exps) > 1:
                days_to_exp = (datetime.strptime(chosen_exp, "%Y-%m-%d").date() - datetime.now().date()).days
                if days_to_exp <= 1:
                    chosen_exp = valid_exps[1]

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
    def resolve_atm_option(
        stock_info: Dict[str, Any], spot_price: float, opt_type: str
    ) -> Optional[Dict[str, Any]]:
        """Resolves the exact ATM or slightly ITM Stock Option contract dynamically for high Delta."""
        strikes_map = stock_info.get("strikes_map", {})
        if not strikes_map:
            return None
        best_node = None
        best_dist = float("inf")
        step = max(0.5, float(stock_info.get("strike_step", max(1.0, spot_price * 0.01))))
        for node in strikes_map.values():
            if opt_type not in node:
                continue
            strike_v = float(node["strike"])
            bias = -0.20 * step if opt_type == "CE" else 0.20 * step
            dist = abs(strike_v - (spot_price + bias))
            if dist < best_dist:
                best_dist = dist
                best_node = node
        if not best_node:
            return None
        leg = best_node[opt_type]
        strike_val = float(best_node["strike"])
        moneyness = ((spot_price - strike_val) / step) if opt_type == "CE" else ((strike_val - spot_price) / step)
        est_delta = max(0.42, min(0.70, 0.54 + 0.08 * math.tanh(moneyness)))
        return {
            "symbol": stock_info["symbol"],
            "strike": strike_val,
            "strike_step": step,
            "est_delta": round(est_delta, 3),
            "opt_type": opt_type,
            "opt_security_id": leg["security_id"],
            "trading_symbol": leg["trading_symbol"],
            "lot_size": int(stock_info["fno_lot_size"]),
            "expiry": stock_info["nearest_expiry"],
        }


class DhanStockOptionsClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: str, access_token: str, logger: logging.Logger):
        self.client_id = client_id
        self.access_token = access_token
        self.logger = logger
        self.last_chart_ts = 0.0
        self.last_quote_ts = 0.0
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

    def fetch_option_quotes(self, security_ids):
        if not security_ids:
            return {}
        self._pace_quote()
        try:
            response = self.session.post(f"{self.BASE_URL}/marketfeed/quote",
                json={"NSE_FNO": [int(s) for s in security_ids]}, timeout=12)
            if response.status_code == 200:
                return response.json().get("data", {}).get("NSE_FNO", {})
            self.logger.warning("Option depth unavailable: HTTP %s", response.status_code)
        except (requests.RequestException, ValueError):
            self.logger.warning("Option depth unavailable; skipping entry/valuation")
        return {}

    def fetch_intraday_candles(
        self,
        security_id: str,
        exchange_segment: str = "NSE_EQ",
        instrument: str = "EQUITY",
        interval: str = "5",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        use_cache: bool = False,
    ) -> Optional[Dict[str, List[Any]]]:
        today_str = to_date or datetime.now().strftime("%Y-%m-%d")
        from_str = from_date or (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        cache_path = ""
        if use_cache:
            ckey = f"{exchange_segment}_{instrument}_{security_id}_{interval}m_{from_str}_{today_str}"
            chash = hashlib.md5(ckey.encode("utf-8")).hexdigest()[:10]
            cache_path = os.path.join(CACHE_DIR, f"{ckey}_{chash}.json")
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    if isinstance(cached, dict) and cached.get("close"):
                        return cached
                except Exception:
                    pass

        elapsed = time.time() - self.last_chart_ts
        if elapsed < 0.45:
            time.sleep(0.45 - elapsed)

        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval),
            "fromDate": from_str,
            "toDate": today_str,
        }
        for attempt in range(1, 3):
            try:
                resp = self.session.post(f"{self.BASE_URL}/charts/intraday", json=payload, timeout=15)
                self.last_chart_ts = time.time()
                if resp.status_code == 200:
                    data = resp.json().get("data", resp.json())
                    if isinstance(data, dict) and data.get("close"):
                        if use_cache and cache_path:
                            atomic_write_json(cache_path, data)
                        return data
                    return None
                elif resp.status_code == 429:
                    time.sleep(3.0 * attempt)
                else:
                    return None
            except requests.RequestException:
                self.last_chart_ts = time.time()
        return None

    def fetch_rolling_stock_option(
        self,
        eq_security_id: str,
        opt_type: str,
        from_date: str,
        to_date: str,
        interval: str = "5",
    ) -> Optional[Dict[str, List[Any]]]:
        """Fetches real historical ATM Stock Option (`OPTSTK`) bars via Dhan `/v2/charts/rollingoption`."""
        drv_type = "CALL" if opt_type == "CE" else "PUT"
        ckey = f"OPTSTK_ROLL_{eq_security_id}_{drv_type}_{interval}m_{from_date}_{to_date}"
        chash = hashlib.md5(ckey.encode("utf-8")).hexdigest()[:10]
        cache_path = os.path.join(CACHE_DIR, f"{ckey}_{chash}.json")

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, dict):
                    if cached.get("empty"):
                        return None
                    if cached.get("timestamp"):
                        return cached
            except Exception:
                pass

        elapsed = time.time() - self.last_chart_ts
        if elapsed < 0.65:
            time.sleep(0.65 - elapsed)

        payload = {
            "exchangeSegment": "NSE_FNO",
            "interval": str(interval),
            "securityId": int(eq_security_id),
            "instrument": "OPTSTK",
            "expiryFlag": "MONTH",
            "expiryCode": 1,
            "strike": "ATM",
            "drvOptionType": drv_type,
            "requiredData": ["open", "high", "low", "close", "oi", "iv", "strike", "spot"],
            "fromDate": from_date,
            "toDate": to_date,
        }
        try:
            resp = self.session.post(f"{self.BASE_URL}/charts/rollingoption", json=payload, timeout=18)
            self.last_chart_ts = time.time()
            if resp.status_code == 200:
                data_node = resp.json().get("data", {})
                side_key = "ce" if opt_type == "CE" else "pe"
                leg = data_node.get(side_key) if isinstance(data_node, dict) else None
                if leg and leg.get("timestamp"):
                    atomic_write_json(cache_path, leg)
                    return leg
                atomic_write_json(cache_path, {"empty": True, "timestamp": []})
        except requests.RequestException:
            self.last_chart_ts = time.time()
        return None

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


def verify_live_scanx_and_resolve_stock_option(client, stock_info, spot_ltp, day_change_pct, cfg):
    candles = client.fetch_intraday_candles(stock_info["eq_security_id"], interval="5")
    if not candles:
        return None
    sig = evaluate_stock_setup(candles, time.time(), cfg.min_day_change_pct, cfg.ema_period)
    if not sig:
        return None
    # Require the current underlying quote to hold confirmation, without chasing.
    direction = 1 if sig["opt_type"] == "CE" else -1
    advance = direction * (spot_ltp - sig["conf_close"])
    if advance < 0 or advance > 0.25 * sig["atr_5m"]:
        return None
    contract = DynamicStockOptionsUniverse.resolve_atm_option(stock_info, spot_ltp, sig["opt_type"])
    return {**sig, **contract, "spot_ltp": spot_ltp, "eq_security_id": stock_info["eq_security_id"]} if contract else None


def qualify_quote(sig, quote, cfg, wallet, now_epoch=None):
    """Use a fresh executable ask/bid and displayed size, never a candle fallback."""
    try:
        now_epoch = time.time() if now_epoch is None else now_epoch
        if not 0 <= now_epoch - (sig["signal_timestamp"] + 300) < 300:
            return None
        trade_time = datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
        if not 0 <= now_epoch - trade_time.timestamp() <= 30:
            return None
        bid_node, ask_node = quote["depth"]["buy"][0], quote["depth"]["sell"][0]
        bid, ask = float(bid_node["price"]), float(ask_node["price"])
        qty = int(sig["lot_size"])
        if not all(math.isfinite(v) for v in (bid, ask, wallet)) or not 0 < bid <= ask or ask < 15:
            return None
        if min(int(bid_node["quantity"]), int(ask_node["quantity"])) < qty:
            return None
        spread = (ask - bid) / ask * 100
        if spread > cfg.max_spread_pct:
            return None
        plan = cfg.plan(ask, qty)
        if not plan or bid <= plan["initial_sl"] or ask * qty + plan["estimated_costs"] > wallet:
            return None
        return {**sig, **plan, "opt_ltp": ask, "bid": bid, "spread_pct": spread}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


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
               lots=1, exit_price=price, gross_pnl=gross, charges=fees, net_pnl=round(gross-fees, 2),
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
        stamp = datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
        if not math.isfinite(bid) or bid <= 0 or size < pos["qty"] or not 0 <= now.timestamp()-stamp.timestamp() <= 30:
            return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    reason = None
    if bid <= pos["current_sl"]:
        reason = "PREMIUM_STOP"
    elif bid >= pos["hard_target_price"]:
        reason = "PREMIUM_TARGET"
    elif now.time().replace(tzinfo=None) >= dtime(15, 25):
        reason = "EOD_SQUAREOFF"
    elif now.timestamp() - pos["entry_epoch"] >= cfg.max_hold_bars_5m * 300:
        reason = "TIME_EXIT_15M"
    if not reason:
        return None
    # Stops may gap; record the observed executable price, never an invented stop fill.
    price = min(bid, pos["hard_target_price"]) if reason == "PREMIUM_TARGET" else bid
    price = round_tick(max(0.05, price * (1 - cfg.slippage_pct/100)))
    return price, reason


def run_dynamic_stock_options_session(live_mode, scan_once, cfg, logger):
    env = load_dotenv_file(".env")
    if not env.get("DHAN_CLIENT_ID") or not env.get("DHAN_ACCESS_TOKEN"):
        raise ValueError("Missing Dhan credentials in .env")
    client = DhanStockOptionsClient(env["DHAN_CLIENT_ID"], env["DHAN_ACCESS_TOKEN"], logger)
    universe = DynamicStockOptionsUniverse.load_universe(logger)
    gate = DailyTradeGate(cfg.gate_dir)
    # A scan is observational and must not initialize or alter a ledger.
    if scan_once:
        wallet = cfg.initial_capital
        ledger = None
    else:
        ledger = load_or_init_ledger(live_mode, cfg.initial_capital)
        wallet = ledger["virtual_balance_net"]
    today = datetime.now(IST).strftime("%Y-%m-%d")
    pos = None
    saved = gate.read(today) if not scan_once else None
    if saved:
        details = saved.get("details", {})
        if saved.get("state") == "reserved" and details.get("mode") == "PAPER" and not live_mode:
            pos = details.get("position")
            if pos and any(t.get("entry_id") == pos["entry_id"] for t in ledger["trades"]):
                gate.mark_closed(today, {"recovered_from_ledger": True})
                pos = None
        if not pos:
            logger.info("Stock-options daily entry already consumed; no further entry today.")
            return
    if not scan_once and not pos and any(str(t.get("timestamp", "")).startswith(today) for t in ledger["trades"]):
        logger.info("Stock-options ledger already contains today's trade; no further entry.")
        return
    logger.info("Selective stock options: maximum 1 entry/day | target %.2f%% | stop %.2f%% | estimated net RR >= %.1f",
                cfg.target_pct, cfg.stop_pct, cfg.min_rr_target)
    telegram = TelegramNotifier(env, logger, "LIVE" if live_mode else "PAPER") if TelegramNotifier and not scan_once else None
    try:
        while True:
            now = datetime.now(IST)
            if now.strftime("%Y-%m-%d") != today:
                logger.warning("Date changed; retaining any unresolved paper position in the daily record.")
                return
            if now.weekday() >= 5 or now.time().replace(tzinfo=None) >= dtime(15, 30):
                logger.info("Session closed. Any unresolved position remains in the daily record.")
                return
            if pos:
                quotes = client.fetch_option_quotes([pos["opt_security_id"]])
                decision = paper_exit(pos, quotes.get(str(pos["opt_security_id"]), {}), now, cfg)
                if decision:
                    price, reason = decision
                    rec = close_paper_position(ledger, pos, price, reason, now)
                    gate.mark_closed(today, rec)
                    logger.info("[PAPER OPTSTK EXIT] %s | %s | net Rs.%+.2f", pos["contract"], reason, rec["net_pnl"])
                    if telegram:
                        telegram.exited(rec)
                    return
                time.sleep(1.1)
                continue
            if not dtime(9, 30) <= now.time().replace(tzinfo=None) <= dtime(14, 30):
                if scan_once or now.hour >= 14:
                    return
                time.sleep(10)
                continue
            quotes = client.fetch_batch_ohlc(list(universe)).get("NSE_EQ", {})
            shortlist = shortlist_movers(quotes, cfg)
            signals = []
            for change, sec_id, ltp in shortlist:
                if sec_id not in universe:
                    continue
                sig = verify_live_scanx_and_resolve_stock_option(client, universe[sec_id], ltp, change, cfg)
                if sig:
                    signals.append(sig)
            depth = client.fetch_option_quotes([s["opt_security_id"] for s in signals]) if signals else {}
            candidates = [q for s in signals if (q := qualify_quote(s, depth.get(str(s["opt_security_id"]), {}), cfg, wallet))]
            ranked = rank_candidates(candidates)
            logger.info("Shortlist %d | confirmed %d | cost/liquidity qualified %d", len(shortlist), len(signals), len(ranked))
            if not ranked:
                if scan_once:
                    return
                time.sleep(30)
                continue
            best = ranked[0]
            logger.info("Best current setup: %s %s | confirmation %.1f | estimated net RR %.2f | spread %.3f%%",
                        best["symbol"], best["opt_type"], best["signal_quality"], best["expected_net_rr"], best["spread_pct"])
            if scan_once:
                return
            # Recheck execution price immediately before the single daily reservation.
            current_eq = client.fetch_batch_ohlc([best["eq_security_id"]]).get("NSE_EQ", {}).get(str(best["eq_security_id"]), {})
            current_spot = float(current_eq.get("last_price", 0) or 0)
            advance = (1 if best["opt_type"] == "CE" else -1) * (current_spot - best["conf_close"])
            if current_spot <= 0 or not 0 <= advance <= 0.25 * best["atr_5m"]:
                time.sleep(5)
                continue
            fresh = client.fetch_option_quotes([best["opt_security_id"]])
            best = qualify_quote(best, fresh.get(str(best["opt_security_id"]), {}), cfg, wallet)
            now = datetime.now(IST)
            if not best or now.time().replace(tzinfo=None) > dtime(14, 30):
                time.sleep(5)
                continue
            pos = {**best, "index": best["symbol"], "option_type": best["opt_type"], "lots": 1,
                   "contract": f"{best['symbol']} {best['strike']:g}_{best['opt_type']}",
                   "qty": best["lot_size"], "entry_price": best["opt_ltp"], "current_ltp": best["opt_ltp"],
                   "current_sl": best["initial_sl"], "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                   "entry_epoch": now.timestamp(), "entry_id": f"STOCK_{today}"}
            if not gate.reserve(today, {"mode": "LIVE" if live_mode else "PAPER", "position": pos}):
                logger.info("Another session already reserved today's stock entry.")
                return
            if live_mode:
                # Broker owns the fixed target/stop bracket. Submission is not a fill;
                # never fabricate live P&L from local quotes or submit twice on timeout.
                oid = client.place_option_super_order(best["trading_symbol"], best["opt_security_id"],
                         best["lot_size"], best["opt_ltp"], best["hard_target_price"], best["initial_sl"], 0)
                logger.info("LIVE submission result: %s. Daily slot consumed. Verify fills and bracket in Dhan; local P&L not inferred.", oid or "unconfirmed")
                return
            logger.info("[PAPER OPTSTK ENTRY] %s @ %.2f | target %.2f | stop %.2f", pos["contract"], pos["entry_price"], pos["hard_target_price"], pos["current_sl"])
            if telegram:
                telegram.position(pos, entry=True)
    finally:
        if telegram:
            telegram.close()


def simulate_fixed_premium_trade(entry_ts, bars, qty, cfg):
    """Real OHLC only. Reject missing bars/rolling-strike changes; stop first on ties."""
    first = bars.get(entry_ts)
    if not first or first.get("strike", 0) <= 0 or first.get("open", 0) < 15:
        return None
    entry = round_tick(first["open"] * (1 + cfg.slippage_pct / 100))
    plan = cfg.plan(entry, qty)
    if not plan:
        return None
    for offset in range(cfg.max_hold_bars_5m):
        ts = entry_ts + offset * 300
        bar = bars.get(ts)
        if not bar or bar.get("strike") != first["strike"]:
            return None
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            return None
        price, reason = None, None
        if l <= plan["initial_sl"]:
            price, reason = min(o, plan["initial_sl"]), "PREMIUM_STOP"
        elif h >= plan["hard_target_price"]:
            price, reason = plan["hard_target_price"], "PREMIUM_TARGET"
        elif datetime.fromtimestamp(ts, IST).time().replace(tzinfo=None) >= dtime(15, 25):
            price, reason = c, "EOD_SQUAREOFF"
        elif offset == cfg.max_hold_bars_5m - 1:
            price, reason = c, "TIME_EXIT_15M"
        if reason:
            price = round_tick(max(0.05, price * (1 - cfg.slippage_pct / 100)))
            return {**plan, "entry_price": entry, "exit_price": price, "exit_reason": reason,
                    "exit_timestamp": datetime.fromtimestamp(ts+300, IST).strftime("%Y-%m-%d %H:%M:%S"),
                    "strike": first["strike"]}
    return None


def run_stock_options_historical_backtest(days, max_stocks, cfg, logger):
    env = load_dotenv_file(".env")
    client = DhanStockOptionsClient(env.get("DHAN_CLIENT_ID", ""), env.get("DHAN_ACCESS_TOKEN", ""), logger)
    universe = DynamicStockOptionsUniverse.load_universe(logger)
    # Deterministic coverage; do not use today's momentum to select past winners.
    stocks = sorted(universe.values(), key=lambda s: s["symbol"])
    if max_stocks > 0:
        stocks = stocks[:max_stocks]
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    frames, snapshots, signals = {}, {}, {}
    for stock in stocks:
        sec = stock["eq_security_id"]
        candles = client.fetch_intraday_candles(sec, interval="5", from_date=start.strftime("%Y-%m-%d"),
                                                to_date=end.strftime("%Y-%m-%d"), use_cache=True)
        if not candles:
            continue
        frames[sec] = stock
        last_day, prev_close, last_close = None, None, None
        for i, ts in enumerate(candles["timestamp"]):
            ts = int(ts)
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
                              "Missing bars or rolling ATM strike changes are unvalidated, never synthetic"]}
    used_days, option_maps, skipped = set(), {}, 0
    for ts in sorted(snapshots):
        day = datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")
        if day in used_days:
            continue
        candidates = []
        for _, sec, _ in shortlist_movers(snapshots[ts], cfg):
            sig = signals.get((ts, sec))
            if not sig:
                continue
            stock = frames[sec]
            key = (sec, sig["opt_type"])
            if key not in option_maps:
                raw = client.fetch_rolling_stock_option(sec, sig["opt_type"], start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
                option_maps[key] = {int(t): {k: float(raw[k][i] or 0) for k in ("open", "high", "low", "close", "strike")}
                                    for i, t in enumerate(raw["timestamp"])} if raw and all(k in raw for k in ("timestamp", "open", "high", "low", "close", "strike")) else {}
            bars = option_maps[key]
            first = bars.get(ts)
            if not first or first["open"] < 15 or first["strike"] <= 0:
                skipped += 1
                continue
            qty = stock["fno_lot_size"]
            entry = round_tick(first["open"] * (1 + cfg.slippage_pct / 100))
            plan = cfg.plan(entry, qty)
            wallet = cfg.initial_capital + sum(t["net_pnl"] for t in ledger["trades"])
            if plan and entry*qty + plan["estimated_costs"] <= wallet:
                candidates.append({**sig, **plan, "symbol": stock["symbol"], "qty": qty, "map_key": key})
        ranked = rank_candidates(candidates)
        if not ranked:
            continue
        best = ranked[0]
        # Consume date BEFORE looking at future outcome, including missing data.
        used_days.add(day)
        sim = simulate_fixed_premium_trade(ts, option_maps[best["map_key"]], best["qty"], cfg)
        if sim is None:
            skipped += 1
            continue
        qty = best["qty"]
        gross = round((sim["exit_price"]-sim["entry_price"])*qty, 2)
        fees = compute_fno_option_charges(sim["entry_price"], sim["exit_price"], qty)
        ledger["trades"].append({**sim, "symbol": best["symbol"], "opt_type": best["opt_type"],
            "contract": f"{best['symbol']} {sim['strike']:g}_{best['opt_type']}", "qty": qty, "lots": 1,
            "timestamp": datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S"),
            "gross_pnl": gross, "charges": fees, "net_pnl": round(gross-fees, 2), "data_source": "REAL_OPTION_OHLC_MODELED_FILLS"})
    update_ledger_metrics(ledger)
    ledger["unvalidated_candidates_or_outcomes"] = skipped
    ledger["stock_coverage"] = len(frames)
    atomic_write_json("stock_fno_backtest_ledger.json", ledger)
    logger.info("Selective backtest: %d trades, %d unvalidated candidates/outcomes | net Rs.%+.2f | avg net win/loss ratio %s",
                ledger["total_trades"], skipped, ledger["net_profit_after_charges"], ledger["net_win_to_loss_ratio"])
    logger.info("Saved separate stock_fno_backtest_ledger.json. Five-minute data cannot validate a 0.5%% stop's execution quality.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Selective stock options: best confirmed shortlist setup, maximum one entry per day")
    parser.add_argument("--live", action="store_true", help="Submit one fixed target/stop bracket to Dhan; verify actual fills in Dhan")
    parser.add_argument("--scan-once", action="store_true", help="Rank current qualifying setups without trading")
    parser.add_argument("--backtest-days", type=int, default=0)
    parser.add_argument("--max-stocks", type=int, default=50, help="Historical coverage, alphabetically selected; 0 for all")
    parser.add_argument("--min-pct", type=float, default=None)
    parser.add_argument("--min-rr", type=float, default=None, help="Minimum estimated net reward/risk, at least 3")
    parser.add_argument("--reset-ledger", action="store_true", help="Reset stock paper ledger; does not reset daily entry limit")
    args = parser.parse_args()
    strat_cfg = DynamicStrategyConfig(load_dotenv_file(".env"), args)
    log = setup_logger()
    if args.reset_ledger and os.path.exists(LEDGER_FILE):
        os.remove(LEDGER_FILE)
    if args.backtest_days > 0:
        run_stock_options_historical_backtest(args.backtest_days, args.max_stocks, strat_cfg, log)
    else:
        run_dynamic_stock_options_session(args.live, args.scan_once, strat_cfg, log)

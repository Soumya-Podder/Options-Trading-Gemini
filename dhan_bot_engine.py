#!/usr/bin/env python3
"""
DhanHQ v4.9.1 Final Production Real-Data Backtest & Live Paper/Order Engine
===========================================================================
Upgrades in v4.9.1 (Based on Oct 19 Live Session Audit):
1. Restored `MarketMicrostructureAnalyzer.evaluate_chain` method for live Option Chain parsing.
2. Extended Intraday Window to 03:30 PM IST:
   - `LAST_ENTRY_TIME = dtime(15, 25)` and `EOD_SQUAREOFF_TIME = dtime(15, 30)`.
3. Zero HTTP 429 Rate-Limit Flooding:
   - Stamps `self.last_oc_ts = time.time()` AFTER each HTTP response (minimum 3.6s gap).
   - Automatically backs off 15s and widens polling interval if Dhan returns HTTP 429 (805).
   - Immediately stops Dhan API polling once daily trade limit (4), consecutive loss breaker (2),
     or `LAST_ENTRY_TIME` is reached with no open trade.
4. 6-Minute Post-Win Cooldown (`POST_WIN_COOLDOWN_MINUTES = 6`):
   - Prevents 9-second same-candle whipsaw re-entries right after a trailing profit exit.
5. Strict Low-Win-Rate Rule Pruning (`wr < 0.35` after >= 5 trades) & 60% WR Multi-Lot Scaling Guard.
6. Safe Historical Cache:
   - Never caches transient network timeouts as permanent empty holiday files.
"""

import os
import sys
import json
import time
import math
import hashlib
import logging
import argparse
from collections import deque
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple, Set, Deque

import requests

from telegram_notifier import TelegramNotifier
from nifty_market_structure import assess_nifty_structure
from index_option_replay import replay_option_trade, ReplayDataError
from nifty_oi import NiftyOITracker

LOG_FILE = "dhan_bot_session.log"
CACHE_DIR = "dhan_hist_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

INDICES_CONFIG = {
    "NIFTY": {
        "security_id": 13,
        "exchange_segment": "IDX_I",
        "fno_segment": "NSE_FNO",
        "instrument": "OPTIDX",
        "lot_size": 65,
        "strike_step": 50,
        "sl_points": 6.15,           # 1R = Rs. 399.75 per lot
        "min_option_premium": 45.0,  # Blocks low-delta / high-theta decay traps < Rs. 45
        "min_spot_impulse": 8.5,
        "max_spot_impulse": 28.5,    # Blocks exhaustion spikes > 28.5 pts
        "wall_threshold": 5_000_000  # 50 Lakhs OI difference
    },
    "SENSEX": {
        "security_id": 51,
        "exchange_segment": "IDX_I",
        "fno_segment": "BSE_FNO",
        "instrument": "OPTIDX",
        "lot_size": 20,
        "strike_step": 100,
        "sl_points": 20.00,          # 1R = Rs. 400.00 per lot
        "min_option_premium": 125.0, # Blocks low-delta / high-theta decay traps < Rs. 125
        "min_spot_impulse": 25.0,
        "max_spot_impulse": 90.0,    # Blocks exhaustion spikes > 90 pts
        "wall_threshold": 5_000_000  # 50 Lakhs OI difference
    },
}

MAX_TRADES_PER_DAY = 6
MAX_CONSECUTIVE_LOSSES_PER_DAY = 2
POST_SL_COOLDOWN_MINUTES = 15
POST_WIN_COOLDOWN_MINUTES = 6
LAST_ENTRY_TIME = dtime(15, 25)      # Takes new trades up until 03:25 PM IST
EOD_SQUAREOFF_TIME = dtime(15, 30)   # Squares off any open trade at 03:30 PM IST


def setup_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger("DhanUltimateEngine")


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


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if (na > 1e-9 and nb > 1e-9) else 0.0


class DhanV2RestClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id: str, access_token: str, logger: logging.Logger):
        self.client_id = client_id
        self.access_token = access_token
        self.logger = logger
        self.last_oc_ts = 0.0
        self.last_hist_ts = 0.0
        self.oc_poll_gap = 3.6
        self.session = requests.Session()
        self.structure_snapshot = None
        self.structure_received = 0.0
        self.session.headers.update({
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _wait_oc_rate_limit(self) -> None:
        elapsed = time.time() - self.last_oc_ts
        if elapsed < self.oc_poll_gap:
            time.sleep(self.oc_poll_gap - elapsed)

    def _wait_hist_rate_limit(self) -> None:
        elapsed = time.time() - self.last_hist_ts
        if elapsed < 0.85:
            time.sleep(0.85 - elapsed)
        self.last_hist_ts = time.time()

    def fetch_nearest_expiry(self, security_id: int, segment: str = "IDX_I") -> Optional[str]:
        self._wait_oc_rate_limit()
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/optionchain/expirylist",
                json={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment},
                timeout=15,
            )
            self.last_oc_ts = time.time()
            if resp.status_code == 200:
                expiries = sorted(resp.json().get("data", []))
                today_str = datetime.now().strftime("%Y-%m-%d")
                future_exp = [e for e in expiries if e >= today_str]
                return future_exp[0] if future_exp else (expiries[0] if expiries else None)
            elif resp.status_code == 429:
                self.oc_poll_gap = min(12.0, self.oc_poll_gap + 3.0)
                time.sleep(10.0)
                self.last_oc_ts = time.time()
            else:
                self.logger.error(f"Expiry list HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            self.last_oc_ts = time.time()
            self.logger.warning(f"Expiry fetch failed for {security_id}: {e}")
        return None

    def fetch_option_chain(self, security_id: int, segment: str, expiry: str) -> Optional[Dict[str, Any]]:
        self._wait_oc_rate_limit()
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/optionchain",
                json={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment, "Expiry": expiry},
                timeout=15,
            )
            self.last_oc_ts = time.time()
            if resp.status_code == 200:
                self.oc_poll_gap = max(3.6, self.oc_poll_gap - 0.2)
                return resp.json().get("data")
            elif resp.status_code == 429:
                self.oc_poll_gap = min(12.0, self.oc_poll_gap + 3.0)
                self.logger.warning(
                    f"Dhan 429 Rate Limit hit. Cooling down 15s (poll gap adjusted to {self.oc_poll_gap:.1f}s)..."
                )
                time.sleep(15.0)
                self.last_oc_ts = time.time()
            else:
                self.logger.error(f"Option chain HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as e:
            self.last_oc_ts = time.time()
            self.logger.warning(f"Option chain poll failed for {security_id}: {e}")
        return None

    def place_market_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        tag: str = "DHAN_V49"
    ) -> Optional[str]:
        if not security_id:
            self.logger.error("Cannot place live order: missing option security_id!")
            return None
        payload = {
            "dhanClientId": self.client_id,
            "correlationId": f"{tag}_{int(time.time())}",
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(quantity),
            "disclosedQuantity": 0,
            "price": 0.0,
            "triggerPrice": 0.0,
            "afterMarketOrder": False,
        }
        try:
            resp = self.session.post(f"{self.BASE_URL}/orders", json=payload, timeout=10)
            if resp.status_code in (200, 201, 202):
                order_id = resp.json().get("orderId")
                self.logger.info(
                    f"[LIVE ORDER SENT] {transaction_type} Qty:{quantity} SecID:{security_id} -> OrderID:{order_id}"
                )
                return str(order_id)
            else:
                self.logger.error(f"Live Order HTTP {resp.status_code}: {resp.text[:250]}")
        except requests.RequestException as e:
            self.logger.error(f"Live Order exception: {e}")
        return None

    def fetch_nifty_structure_candles(self, from_date, to_date, historical=False):
        """Actual index OHLC for level mapping, independently of rolling premiums."""
        payload={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
                 "interval":"5","fromDate":from_date,"toDate":to_date}
        key=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        path=os.path.join(CACHE_DIR,f"nifty_structure_{key}.json")
        if historical and os.path.exists(path):
            try:
                with open(path,encoding="utf-8") as f:
                    return json.load(f)
            except (OSError,ValueError):
                pass
        if not historical and time.time()-self.structure_received<30:
            return self.structure_snapshot
        self._wait_hist_rate_limit()
        data=None
        try:
            response=self.session.post(f"{self.BASE_URL}/charts/intraday",json=payload,timeout=(5,15))
            if response.status_code==200:
                candidate=response.json()
                if isinstance(candidate,dict) and candidate.get("timestamp"):
                    data=candidate
                    if historical:
                        atomic_write_json(path,data)
            else:
                self.logger.warning("Nifty structure history unavailable (HTTP %s)",response.status_code)
        except (requests.RequestException,ValueError):
            self.logger.warning("Nifty structure history request failed")
        if not historical:
            self.structure_snapshot,self.structure_received=data,time.time()
        return data

    def fetch_rolling_option_window(
        self,
        idx_name: str,
        strike_label: str,
        opt_type: str,
        from_date: str,
        to_date: str,
        interval: str = "5"
    ) -> Optional[Dict[str, List[Any]]]:
        cache_key = f"{idx_name}_{strike_label}_{opt_type}_{interval}m_{from_date}_{to_date}"
        cache_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()[:12]
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}_{cache_hash}.json")

        def bounded(data):
            if not isinstance(data,dict) or not data.get("timestamp"):
                return None
            start=datetime.strptime(from_date,"%Y-%m-%d").timestamp()
            end=datetime.strptime(to_date,"%Y-%m-%d").timestamp()
            stamps=data["timestamp"]
            keep=[i for i,t in enumerate(stamps) if start<=float(t)<end]
            if not keep:
                return None
            if any(isinstance(v,list) and v and len(v)!=len(stamps) for v in data.values()):
                return None
            return {k:[v[i] for i in keep] if isinstance(v,list) and v else v for k,v in data.items()}

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, dict):
                    if cached.get("empty"):
                        return None
                    if cached.get("timestamp"):
                        return bounded(cached)
            except Exception:
                pass

        cfg = INDICES_CONFIG[idx_name]
        req_fields = (
            ["open", "high", "low", "close", "oi", "iv", "strike", "spot"]
            if strike_label == "ATM"
            else ["close", "oi", "iv", "strike"]
        )
        payload = {
            "exchangeSegment": cfg["fno_segment"],
            "interval": str(interval),
            "securityId": int(cfg["security_id"]),
            "instrument": cfg["instrument"],
            "expiryFlag": "WEEK",
            "expiryCode": 1,
            "strike": strike_label,
            "drvOptionType": opt_type,
            "requiredData": req_fields,
            "fromDate": from_date,
            "toDate": to_date,
        }

        for attempt in range(1, 3):
            self._wait_hist_rate_limit()
            try:
                resp = self.session.post(
                    f"{self.BASE_URL}/charts/rollingoption",
                    json=payload,
                    timeout=(10, 25),
                )
                if resp.status_code == 200:
                    body = resp.json()
                    data_node = body.get("data", {})
                    side_key = "ce" if opt_type == "CALL" else "pe"
                    leg_data = bounded(data_node.get(side_key)) if isinstance(data_node, dict) else None
                    if leg_data and leg_data.get("timestamp"):
                        atomic_write_json(cache_path, leg_data)
                        return leg_data
                    else:
                        atomic_write_json(cache_path, {"empty": True, "timestamp": []})
                        return None
                elif resp.status_code in (429, 500, 502, 503, 504):
                    self.logger.warning(
                        f"HTTP {resp.status_code} on {idx_name} {strike_label} {opt_type} "
                        f"({from_date}->{to_date}), retry {attempt}/2..."
                    )
                    time.sleep(1.5 * attempt)
                else:
                    return None
            except requests.exceptions.Timeout:
                self.logger.warning(
                    f"Timeout on {idx_name} {strike_label} {opt_type} ({from_date}->{to_date}), retry {attempt}/2..."
                )
                time.sleep(1.5 * attempt)
            except requests.RequestException as e:
                self.logger.error(f"RollingOption error: {e}")
                return None

        return None


class MarketMicrostructureAnalyzer:
    def __init__(self):
        self.nifty_oi_tracker=NiftyOITracker()
        self.prev_bar: Dict[str, Dict[str, Any]] = {}
        self.live_history: Dict[str, Deque[Dict[str, Any]]] = {
            "NIFTY": deque(maxlen=150),
            "SENSEX": deque(maxlen=150),
        }

    @staticmethod
    def classify_quadrant(d_price: float, d_oi: float) -> str:
        if abs(d_price) < 0.05 and abs(d_oi) < 500:
            return "NEUTRAL"
        if d_price >= 0 and d_oi > 0:
            return "LONG_BUILDUP"
        if d_price < 0 and d_oi > 0:
            return "SHORT_BUILDUP"
        if d_price >= 0 and d_oi <= 0:
            return "SHORT_COVERING"
        return "LONG_UNWINDING"

    def evaluate_snapshot(
        self,
        idx_name: str,
        spot: float,
        call_strikes: List[int],
        put_strikes: List[int],
        ce_oi: int,
        pe_oi: int,
        ce_prem: float,
        pe_prem: float,
        ce_iv: float,
        pe_iv: float,
        atm_ce_ltp: float,
        atm_pe_ltp: float,
        atm_ce_sec_id: str = "",
        atm_pe_sec_id: str = "",
        live_ts: Optional[float] = None,
        reference_snapshot=None,
        require_reference=False,
    ) -> Dict[str, Any]:
        cfg = INDICES_CONFIG[idx_name]
        step = cfg["strike_step"]
        min_impulse = cfg["min_spot_impulse"]
        max_impulse = cfg["max_spot_impulse"]

        current_snap = {
            "ts": live_ts or time.time(),
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_prem": ce_prem,
            "pe_prem": pe_prem,
            "spot": spot,
            "atm_ce_strike": call_strikes[0],
        }

        if require_reference:
            prev_snap=reference_snapshot
        elif live_ts is not None:
            hist = self.live_history[idx_name]
            hist.append(current_snap)
            ref_snap = hist[0]
            for s in hist:
                if live_ts - s["ts"] >= 270.0:
                    ref_snap = s
                else:
                    break
            prev_snap = ref_snap if len(hist) > 1 else None
        else:
            prev_snap = self.prev_bar.get(idx_name)
            self.prev_bar[idx_name] = current_snap

        if prev_snap is None:
            d_ce_oi, d_pe_oi = 0, 0
            d_ce_prem, d_pe_prem = 0.0, 0.0
            d_spot = 0.0
        else:
            d_spot = spot - prev_snap["spot"]
            strike_shift = call_strikes[0] - prev_snap["atm_ce_strike"]
            if idx_name=="NIFTY" and strike_shift:
                # Different strike baskets cannot establish premium/OI changes.
                # Wait for a comparable reference instead of inventing a delta.
                d_ce_prem=d_pe_prem=0.0
                d_ce_oi=d_pe_oi=0
            else:
                d_ce_prem = (ce_prem - prev_snap["ce_prem"]) + (strike_shift * 1.35)
                d_pe_prem = (pe_prem - prev_snap["pe_prem"]) - (strike_shift * 1.35)
                d_ce_oi = ce_oi - prev_snap["ce_oi"]
                d_pe_oi = pe_oi - prev_snap["pe_oi"]

        ce_q = self.classify_quadrant(d_ce_prem, d_ce_oi)
        pe_q = self.classify_quadrant(d_pe_prem, d_pe_oi)
        if require_reference:
            if d_ce_oi==0:ce_q="NO_OI_CHANGE"
            if d_pe_oi==0:pe_q="NO_OI_CHANGE"

        raw_diff = ce_oi - pe_oi
        diff_lakhs = round(raw_diff / 100_000.0, 2)
        exceeds_50l = abs(raw_diff) >= cfg["wall_threshold"]

        if raw_diff <= -cfg["wall_threshold"]:
            wall_tag = "PUT_SIDE_GT_50L"
        elif raw_diff >= cfg["wall_threshold"]:
            wall_tag = "CALL_SIDE_GT_50L"
        else:
            wall_tag = "SUB_50L_DIFF"

        if (
            wall_tag == "PUT_SIDE_GT_50L"
            and min_impulse <= d_spot <= max_impulse
            and ce_q in ("SHORT_COVERING", "LONG_BUILDUP")
            and pe_q in ("SHORT_BUILDUP", "LONG_UNWINDING")
        ):
            ctrl = (
                "BUYERS_AND_PUT_WRITERS_ALIGNED"
                if ce_q == "SHORT_COVERING"
                else "PUT_WRITERS_DOMINATING_FLOOR"
            )
            dom = "BULLS_DOMINATING"
            rec_dir = "BUY_CE"
        elif (
            wall_tag == "CALL_SIDE_GT_50L"
            and -max_impulse <= d_spot <= -min_impulse
            and pe_q in ("SHORT_COVERING", "LONG_BUILDUP")
            and ce_q in ("SHORT_BUILDUP", "LONG_UNWINDING")
        ):
            ctrl = (
                "BUYERS_AND_CALL_WRITERS_ALIGNED"
                if pe_q == "SHORT_COVERING"
                else "CALL_WRITERS_DOMINATING_CEILING"
            )
            dom = "BEARS_DOMINATING"
            rec_dir = "BUY_PE"
        else:
            ctrl = "CONTESTED_CHOP"
            dom = "BALANCED_CHOP"
            rec_dir = "HOLD"

        pcr = pe_oi / max(ce_oi, 1)
        strike_ref = call_strikes[0] if rec_dir == "BUY_CE" else put_strikes[0]

        state_vec = [
            round(max(-3.0, min(3.0, -diff_lakhs / 50.0)), 4),
            round(max(-3.0, min(3.0, pcr - 1.0)), 4),
            round(max(-2.0, min(2.0, (d_pe_oi - d_ce_oi) / 500_000.0)), 4),
            1.0 if ce_q == "SHORT_COVERING" else (0.6 if ce_q == "LONG_BUILDUP" else -0.8),
            1.0 if pe_q == "SHORT_COVERING" else (0.6 if pe_q == "LONG_BUILDUP" else -0.8),
            1.5 if exceeds_50l else -1.5,
            round(max(-2.0, min(2.0, d_spot / (step * 0.5))), 4),
            round(max(-2.0, min(2.0, (ce_iv - pe_iv) / 15.0)), 4),
            1.0 if dom == "BULLS_DOMINATING" else (-1.0 if dom == "BEARS_DOMINATING" else 0.0),
            round((strike_ref % 100) / 100.0, 4),
        ]

        return {
            "index": idx_name,
            "spot": spot,
            "d_spot_5m": round(d_spot, 2),
            "call_strikes_above": call_strikes,
            "put_strikes_below": put_strikes,
            "ce_3strike_lakhs": round(ce_oi / 100_000.0, 2),
            "pe_3strike_lakhs": round(pe_oi / 100_000.0, 2),
            "diff_ce_minus_pe_lakhs": diff_lakhs,
            "raw_diff_oi": raw_diff,
            "ce_oi_change_5m":d_ce_oi,
            "pe_oi_change_5m":d_pe_oi,
            "ce_premium_change_5m":round(d_ce_prem,2),
            "pe_premium_change_5m":round(d_pe_prem,2),
            "exceeds_50_lakhs": exceeds_50l,
            "wall_tag": wall_tag,
            "ce_quadrant": ce_q,
            "pe_quadrant": pe_q,
            "market_controller": ctrl,
            "market_dominance": dom,
            "recommended_dir": rec_dir,
            "state_vec": state_vec,
            "atm_ce_strike": call_strikes[0],
            "atm_pe_strike": put_strikes[0],
            "atm_ce_ltp": round(atm_ce_ltp, 2),
            "atm_pe_ltp": round(atm_pe_ltp, 2),
            "atm_ce_sec_id": atm_ce_sec_id,
            "atm_pe_sec_id": atm_pe_sec_id,
            "data_health": "OI_REFERENCE_UNAVAILABLE" if require_reference and prev_snap is None else "ATM_ROLLOVER_WAIT" if idx_name=="NIFTY" and prev_snap is not None and call_strikes[0]!=prev_snap["atm_ce_strike"] else "LIVE_CLEAN",
        }

    def evaluate_chain(self, idx_name: str, oc_data: Dict[str, Any], expiry=None, snapshot_time=None) -> Dict[str, Any]:
        cfg = INDICES_CONFIG[idx_name]
        step = cfg["strike_step"]
        spot = float(oc_data.get("last_price", 0.0))
        oc = oc_data.get("oc", {})

        put_anchor = int(math.floor(spot / step) * step)
        call_anchor = put_anchor + step
        call_strikes = [call_anchor + i * step for i in range(3)]
        put_strikes = [put_anchor - i * step for i in range(3)]

        ce_oi, pe_oi = 0, 0
        ce_prem, pe_prem = 0.0, 0.0
        ce_iv, pe_iv = 0.0, 0.0
        atm_ce_ltp, atm_pe_ltp = 0.0, 0.0
        atm_ce_sec_id, atm_pe_sec_id = "", ""

        for idx_k, k in enumerate(call_strikes):
            node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("ce", {})
            ltp = float(node.get("last_price", 0.0))
            if idx_k == 0:
                atm_ce_ltp = ltp
                atm_ce_sec_id = str(node.get("security_id", node.get("securityId", "")))
            ce_oi += int(node.get("oi", 0))
            ce_prem += ltp
            ce_iv += float(node.get("implied_volatility", 0.0))

        for idx_k, k in enumerate(put_strikes):
            node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("pe", {})
            ltp = float(node.get("last_price", 0.0))
            if idx_k == 0:
                atm_pe_ltp = ltp
                atm_pe_sec_id = str(node.get("security_id", node.get("securityId", "")))
            pe_oi += int(node.get("oi", 0))
            pe_prem += ltp
            pe_iv += float(node.get("implied_volatility", 0.0))

        received=time.time() if snapshot_time is None else float(snapshot_time)
        reference,oi_info=self.nifty_oi_tracker.assess(oc,spot,received,expiry) if idx_name=="NIFTY" else (None,None)
        result=self.evaluate_snapshot(
            idx_name, spot, call_strikes, put_strikes,
            ce_oi, pe_oi, ce_prem, pe_prem, ce_iv, pe_iv,
            atm_ce_ltp, atm_pe_ltp, atm_ce_sec_id, atm_pe_sec_id,
            live_ts=received,reference_snapshot=reference,require_reference=idx_name=="NIFTY"
        )
        if oi_info is not None:result["oi_analysis"]=oi_info
        return result


class SelfLearningBrain:
    def __init__(self, logger: logging.Logger, brain_file: str = "bot_brain_state.json", rules_file: str = "learned_rules.json"):
        self.logger = logger
        self.brain_file = brain_file
        self.rules_file = rules_file
        self.brain = self._load_json(brain_file, {
            "weights": [[-0.25, -0.1, -0.1, -0.2, -0.2, -0.45, -0.1, 0.0, -0.3, 0.0],
                        [0.35, 0.2, 0.25, 0.35, -0.25, 0.4, 0.25, -0.1, 0.35, 0.05],
                        [-0.35, -0.2, -0.25, -0.25, 0.35, 0.4, -0.25, 0.1, -0.35, 0.05]],
            "bias": [0.10, -0.15, -0.15],
            "epsilon": 0.05,
            "replay_buffer": []
        })
        self.rules = self._load_json(rules_file, {})
        self.recent_losing_states: List[Tuple[str, List[float]]] = []

    @staticmethod
    def _load_json(path: str, default: Any) -> Any:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def unmute_new_day(self, day_str: str) -> None:
        self.recent_losing_states.clear()
        for r in self.rules.values():
            t_cnt = int(r.get("trades_count", 0))
            w_cnt = int(r.get("wins_count", 0))
            l_cnt = int(r.get("losses_count", 0))
            wr = (w_cnt / t_cnt) if t_cnt > 0 else 0.0
            days_n = len(r.get("unique_days_traded", []))
            exp_r = float(r.get("expectancy_r", 0.0))

            if r.get("status") == "DAY_MUTED" and r.get("muted_on_day") != day_str:
                if (t_cnt >= 5 and wr < 0.35) or (t_cnt >= 4 and exp_r < -0.15):
                    r["status"] = "PRUNED"
                elif days_n >= 3 and exp_r >= 0.25 and wr >= 0.40:
                    r["status"] = "ACTIVE"
                else:
                    r["status"] = "CANDIDATE"
            elif r.get("status") != "PRUNED":
                if (t_cnt >= 5 and wr < 0.35) or (t_cnt >= 2 and w_cnt == 0):
                    r["status"] = "PRUNED"
                elif days_n >= 2 and w_cnt >= 2 and l_cnt == 0 and exp_r >= 1.0:
                    r["status"] = "ACTIVE"
            r["intraday_losses"] = 0
        atomic_write_json(self.rules_file, self.rules)

    def select_action(
        self, analysis: Dict[str, Any], consec_losses: int, bar_time: Optional[dtime] = None
    ) -> Tuple[str, Optional[str]]:
        if not analysis["exceeds_50_lakhs"] or analysis["recommended_dir"] == "HOLD":
            return "HOLD", None
        if analysis["index"] == "NIFTY" and not analysis.get("market_structure",{}).get("allowed",False):
            return "HOLD", None

        cfg = INDICES_CONFIG[analysis["index"]]
        opt_ltp = analysis["atm_ce_ltp"] if analysis["recommended_dir"] == "BUY_CE" else analysis["atm_pe_ltp"]
        if opt_ltp < cfg["min_option_premium"]:
            return "HOLD", None

        sig = (
            f"IF wall={analysis['wall_tag']} & ce={analysis['ce_quadrant']} "
            f"& pe={analysis['pe_quadrant']} & ctrl={analysis['market_controller']} "
            f"& dom={analysis['market_dominance']} -> {analysis['recommended_dir']}"
        )

        if sig not in self.rules:
            r_id = f"RULE_{len(self.rules) + 1:03d}"
            self.rules[sig] = {
                "rule_id": r_id,
                "condition_signature": sig,
                "status": "CANDIDATE",
                "trades_count": 0,
                "wins_count": 0,
                "losses_count": 0,
                "unique_days_traded": [],
                "total_win_r": 0.0,
                "total_loss_r": 0.0,
                "avg_win_r": 0.0,
                "avg_loss_r": 0.0,
                "expectancy_r": 0.0,
                "intraday_losses": 0,
                "last_lot_loss": False,
                "muted_on_day": ""
            }
            self.logger.info(f"[NEW RULE DISCOVERED] {r_id}: {sig}")
            atomic_write_json(self.rules_file, self.rules)

        rule = self.rules[sig]
        if rule["status"] in ("PRUNED", "DAY_MUTED"):
            return "HOLD", sig

        # Mid-day Lunch Lull Filter (12:00 PM - 13:30 PM IST)
        if bar_time and dtime(12, 0) <= bar_time <= dtime(13, 30):
            min_lunch_imp = cfg["min_spot_impulse"] * 1.75
            if rule["status"] != "ACTIVE" and (
                abs(analysis["d_spot_5m"]) < min_lunch_imp or abs(analysis["diff_ce_minus_pe_lakhs"]) < 100.0
            ):
                return "HOLD", sig

        # Sniper Recovery Gate after a loss
        if consec_losses >= 1:
            min_rec_imp = cfg["min_spot_impulse"] * 1.6
            if rule["status"] != "ACTIVE" and abs(analysis["d_spot_5m"]) < min_rec_imp:
                return "HOLD", sig

        # Cosine Similarity Veto against recent losing state vectors
        for lost_dir, lost_vec in self.recent_losing_states[-8:]:
            if lost_dir == analysis["recommended_dir"] and cosine_similarity(analysis["state_vec"], lost_vec) >= 0.92:
                return "HOLD", sig

        return analysis["recommended_dir"], sig

    def compute_lots(
        self,
        idx_name: str,
        sig: str,
        entry_price: float,
        net_wallet: float,
        consec_losses: int,
    ) -> int:
        if consec_losses >= 1:
            return 1
        cfg = INDICES_CONFIG[idx_name]
        cost_per_lot = entry_price * cfg["lot_size"]
        max_affordable = max(1, min(3, int((net_wallet * 0.65) // max(cost_per_lot, 1.0))))

        rule = self.rules.get(sig, {})
        if rule.get("last_lot_loss", False):
            return 1

        t_cnt = int(rule.get("trades_count", 0))
        w_cnt = int(rule.get("wins_count", 0))
        wr = (w_cnt / t_cnt) if t_cnt > 0 else 0.0
        exp_r = float(rule.get("expectancy_r", 0.0))

        # Scale to 2-3 lots ONLY on proven A+ rules (Win Rate >= 60% & Expectancy >= +0.80R)
        if rule.get("status") == "ACTIVE" and t_cnt >= 3 and wr >= 0.60:
            if exp_r >= 1.50 and wr >= 0.70 and net_wallet >= 42000.0:
                return min(3, max_affordable)
            if exp_r >= 0.80 and net_wallet >= 33500.0:
                return min(2, max_affordable)
        return 1

    def learn_from_trade(
        self, day_str: str, sig: str, state_vec: List[float], action_idx: int, r_mult: float, lots: int = 1
    ) -> None:
        if r_mult <= -0.70:
            self.recent_losing_states.append(("BUY_CE" if action_idx == 1 else "BUY_PE", state_vec))

        if sig in self.rules:
            r = self.rules[sig]
            r["trades_count"] += 1
            if day_str not in r["unique_days_traded"]:
                r["unique_days_traded"].append(day_str)
            if r_mult > 0:
                r["wins_count"] += 1
                r["total_win_r"] += r_mult
                r["avg_win_r"] = round(r["total_win_r"] / r["wins_count"], 2)
                r["intraday_losses"] = 0
                r["last_lot_loss"] = False
            else:
                r["losses_count"] += 1
                weighted_loss_r = abs(r_mult) * (1.0 + 0.35 * (lots - 1))
                r["total_loss_r"] += weighted_loss_r
                r["avg_loss_r"] = round(r["total_loss_r"] / r["losses_count"], 2)
                r["intraday_losses"] = r.get("intraday_losses", 0) + 1
                r["last_lot_loss"] = True

            wr = r["wins_count"] / r["trades_count"]
            r["expectancy_r"] = round((wr * r["avg_win_r"]) - ((1.0 - wr) * r["avg_loss_r"]), 2)
            days_n = len(r["unique_days_traded"])

            if r["intraday_losses"] >= 2:
                r["status"] = "DAY_MUTED"
                r["muted_on_day"] = day_str
            elif (
                (r["trades_count"] >= 2 and r["wins_count"] == 0)
                or (r["trades_count"] >= 4 and r["expectancy_r"] < -0.15)
                or (r["trades_count"] >= 5 and wr < 0.35)
            ):
                r["status"] = "PRUNED"
            elif (days_n >= 3 and r["expectancy_r"] >= 0.25 and wr >= 0.40) or (
                days_n >= 2 and r["wins_count"] >= 2 and r["losses_count"] == 0 and r["expectancy_r"] >= 1.0
            ):
                r["status"] = "ACTIVE"

            atomic_write_json(self.rules_file, self.rules)

        reward = round(max(-1.5, min(6.0, r_mult)), 4)
        self.brain["replay_buffer"].append({
            "day": day_str,
            "state": [round(x, 4) for x in state_vec],
            "action": action_idx,
            "reward": reward
        })
        if len(self.brain["replay_buffer"]) > 500:
            self.brain["replay_buffer"] = self.brain["replay_buffer"][-500:]

        lr = 0.015
        w = self.brain["weights"][action_idx]
        b = self.brain["bias"][action_idx]
        q_pred = sum(wi * si for wi, si in zip(w, state_vec)) + b
        err = max(-2.5, min(2.5, reward - q_pred))
        for i in range(10):
            self.brain["weights"][action_idx][i] = round((w[i] * 0.995) + lr * err * state_vec[i], 6)
        self.brain["bias"][action_idx] = round((b * 0.995) + lr * err, 6)
        atomic_write_json(self.brain_file, self.brain)


def update_ratchet_sl(entry_price: float, current_sl: float, highest_ltp: float, sl_points: float) -> Tuple[float, bool]:
    peak_r = (highest_ltp - entry_price) / sl_points
    armed = False
    new_sl = current_sl
    if peak_r >= 3.0:
        new_sl = max(current_sl, round(highest_ltp - (0.80 * sl_points), 2))
        armed = True
    elif peak_r >= 2.0:
        new_sl = max(current_sl, round(entry_price + (1.05 * sl_points), 2))
        armed = True
    elif peak_r >= 1.15:
        new_sl = max(current_sl, round(entry_price + (0.20 * sl_points), 2))
        armed = True
    elif peak_r >= 0.65:
        new_sl = max(current_sl, round(entry_price - (0.45 * sl_points), 2))
    return new_sl, armed


def nifty_target_quantities(index, lots, lot_size):
    """Split only multi-lot Nifty trades, rounding T1 down to whole lots."""
    if index != "NIFTY" or lots < 2:
        return None
    first = (lots // 2) * lot_size
    return first, lots * lot_size - first


def book_target_one(ledger, position, price, timestamp):
    """Realize T1 once; final trade accounting reconciles this booked amount."""
    if position.get("target_one_done"):
        return
    qty = position["target_one_qty"]
    gross = round((price-position["entry_price"])*qty, 2)
    fees = round(20 + (20 + position["lots"]*24.5)*qty/position["original_qty"], 2)
    position.update(target_one_done=True, qty=position["qty"]-qty,
                    partial_gross=gross, partial_net=round(gross-fees,2))
    position["exit_fills"] = [{"qty":qty,"price":price,"timestamp":timestamp,"reason":"TARGET_1_10_POINTS"}]
    ledger["virtual_balance_gross"] = round(ledger["virtual_balance_gross"]+gross,2)
    ledger["virtual_balance_net"] = round(ledger["virtual_balance_net"]+gross-fees,2)
    ledger["profit_without_charges"] = round(ledger["virtual_balance_gross"]-ledger["initial_capital"],2)
    ledger["net_profit_after_charges"] = round(ledger["virtual_balance_net"]-ledger["initial_capital"],2)
    ledger["active_position"] = {k:v for k,v in position.items() if k != "entry_dt"}
    atomic_write_json("paper_trade_ledger.json",ledger)


def evaluate_real_option_trade_bars(
    entry_idx, is_ce, timestamps, opens, highs, lows, closes, strikes,
    sl_points, interval_min=5, index="", lots=1, lot_size=1, exit_fills=None,
    entry_at_open=False, expected_strike=None, slippage_pct=0.0,
    require_session_end=False,
):
    return replay_option_trade(entry_idx,is_ce,timestamps,opens,highs,lows,closes,
        strikes,sl_points,interval_min,index,lots,lot_size,exit_fills,
        nifty_target_quantities(index,lots,lot_size),update_ratchet_sl,
        EOD_SQUAREOFF_TIME,entry_at_open,expected_strike,slippage_pct,require_session_end)


def record_closed_trade(
    ledger: Dict[str, Any],
    brain: SelfLearningBrain,
    day_str: str,
    entry_ts: str,
    exit_ts: str,
    idx_name: str,
    strike_val: int,
    opt_type: str,
    lots: int,
    qty: int,
    spot_at_entry: float,
    oi_diff_lakhs: float,
    entry_price: float,
    exit_price: float,
    r_mult: float,
    exit_reason: str,
    sig: str,
    state_vec: List[float],
    data_health: str,
    exit_fills=None, booked_gross: float = 0, booked_net: float = 0, market_structure=None,
    ledger_path="paper_trade_ledger.json",
) -> float:
    gross_pnl = round((exit_price - entry_price) * qty, 2)
    charges = round(40.0 + (lots * 24.5) + 20.0*max(0,len(exit_fills or [])-1), 2)
    net_pnl = round(gross_pnl - charges, 2)

    ledger["virtual_balance_gross"] = round(ledger["virtual_balance_gross"] + gross_pnl - booked_gross, 2)
    ledger["virtual_balance_net"] = round(ledger["virtual_balance_net"] + net_pnl - booked_net, 2)
    ledger["profit_without_charges"] = round(ledger["virtual_balance_gross"] - ledger["initial_capital"], 2)
    ledger["net_profit_after_charges"] = round(ledger["virtual_balance_net"] - ledger["initial_capital"], 2)

    trade_record = {
        "timestamp": entry_ts,
        "exit_timestamp": exit_ts,
        "index": idx_name,
        "contract": f"{strike_val}_{opt_type}",
        "lots": lots,
        "qty": qty,
        "spot_at_entry": round(spot_at_entry, 2),
        "oi_diff_lakhs": oi_diff_lakhs,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "r_multiple": r_mult,
        "exit_reason": exit_reason,
        "rule_used": sig,
        "data_health": data_health
    }
    if exit_fills:
        trade_record["exit_fills"] = exit_fills
        trade_record["exit_price"] = round(exit_price,4)
        ledger.pop("active_position",None)
    if market_structure is not None:
        trade_record["market_structure"]=market_structure
    ledger["trades"].append(trade_record)
    ledger["total_trades"] = len(ledger["trades"])

    wins = [float(t["gross_pnl"]) for t in ledger["trades"] if float(t["gross_pnl"]) > 0]
    losses = [abs(float(t["gross_pnl"])) for t in ledger["trades"] if float(t["gross_pnl"]) <= 0]
    ledger["avg_winning_value_rs"] = round(sum(wins) / len(wins), 2) if wins else 0.0
    ledger["avg_losing_value_rs"] = round(sum(losses) / len(losses), 2) if losses else 0.0
    if ledger["avg_losing_value_rs"] > 0:
        ledger["win_to_loss_ratio"] = round(ledger["avg_winning_value_rs"] / ledger["avg_losing_value_rs"], 2)

    brain.learn_from_trade(day_str, sig, state_vec, 1 if opt_type == "CE" else 2, r_mult, lots=lots)
    atomic_write_json(ledger_path, ledger)
    return gross_pnl


def run_real_historical_backtest(
    days: int,
    indices_to_test: List[str],
    interval: str,
    logger: logging.Logger
) -> None:
    if "NIFTY" in indices_to_test and str(interval)!="5":
        raise ValueError("Nifty market-structure replay requires five-minute option candles")
    env = load_dotenv_file(".env")
    client_id = env.get("DHAN_CLIENT_ID", "")
    access_token = env.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        logger.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in .env!")
        sys.exit(1)

    dhan = DhanV2RestClient(client_id, access_token, logger)
    analyzer = MarketMicrostructureAnalyzer()
    brain = SelfLearningBrain(logger)

    ledger = {
        "mode": "PAPER",
        "initial_capital": 30000.0,
        "virtual_balance_gross": 30000.0,
        "virtual_balance_net": 30000.0,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "avg_winning_value_rs": 0.0,
        "avg_losing_value_rs": 0.0,
        "win_to_loss_ratio": 0.0,
        "trades": []
    }

    end_dt = datetime.now() - timedelta(days=4)
    start_dt = end_dt - timedelta(days=days)

    windows: List[Tuple[str, str]] = []
    cursor = start_dt
    while cursor < end_dt:
        nxt = min(cursor + timedelta(days=2), end_dt)
        if not (cursor.weekday() == 5 and (nxt - cursor).days <= 2):
            windows.append((cursor.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cursor = nxt

    logger.info(
        f"Starting 100% REAL DhanHQ Historical Backtest | Span: {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')} "
        f"({len(windows)} x 2-day micro-chunks, cached in {CACHE_DIR}/) | Interval: {interval}m | Indices: {indices_to_test}"
    )

    call_strikes_rel = ["ATM", "ATM+1", "ATM+2"]
    put_strikes_rel = ["ATM", "ATM-1", "ATM-2"]
    interval_min = int(interval)
    cooldown_bars = max(1, POST_SL_COOLDOWN_MINUTES // interval_min)
    win_cooldown_bars = max(1, POST_WIN_COOLDOWN_MINUTES // interval_min)

    seen_bars: Set[Tuple[str, int]] = set()
    daily_stats: Dict[Tuple[str, str], Dict[str, int]] = {}
    last_view_by_index: Dict[str, Dict[str, Any]] = {}
    structure_counts={"checked":0,"unavailable":0,"allowed":0,"rejected":0}
    missing_option_windows=[]
    unvalidated_trades=[]

    for w_from, w_to in windows:
        for idx_name in indices_to_test:
            cfg = INDICES_CONFIG[idx_name]
            logger.info(f"Fetching/Loading REAL Rolling Options for {idx_name} ({w_from} -> {w_to})...")
            structure_candles = dhan.fetch_nifty_structure_candles(
                (datetime.strptime(w_from,"%Y-%m-%d")-timedelta(days=10)).strftime("%Y-%m-%d 00:00:00"),
                w_to+" 00:00:00",historical=True) if idx_name=="NIFTY" else None

            ce_legs: Dict[str, Dict[str, List[Any]]] = {}
            pe_legs: Dict[str, Dict[str, List[Any]]] = {}

            for s_lbl in call_strikes_rel:
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "CALL", w_from, w_to, interval)
                if res and res.get("timestamp"):
                    ce_legs[s_lbl] = res

            for s_lbl in put_strikes_rel:
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "PUT", w_from, w_to, interval)
                if res and res.get("timestamp"):
                    pe_legs[s_lbl] = res

            if "ATM" not in ce_legs or "ATM" not in pe_legs:
                missing_option_windows.append({"index":idx_name,"from":w_from,"to_exclusive":w_to})
                logger.warning(f"No trading session bars in ({w_from} -> {w_to}) for {idx_name}. Continuing.")
                continue

            atm_ce = ce_legs["ATM"]
            atm_pe = pe_legs["ATM"]
            # Cross-strike OI must describe the same time, never equal array indices
            # from series with different missing timestamps.
            canonical=atm_ce["timestamp"]
            if any(data["timestamp"]!=canonical for data in list(ce_legs.values())+list(pe_legs.values())):
                missing_option_windows.append({"index":idx_name,"from":w_from,"to_exclusive":w_to,
                                               "reason":"Unaligned option timestamps"})
                logger.warning("Unaligned option histories for %s (%s -> %s); window excluded",idx_name,w_from,w_to)
                continue
            avail_ce = [k for k in call_strikes_rel if k in ce_legs]
            avail_pe = [k for k in put_strikes_rel if k in pe_legs]

            ts_list = atm_ce["timestamp"]
            n_bars = min(
                len(ts_list),
                *(len(ce_legs[k]["timestamp"]) for k in avail_ce),
                *(len(pe_legs[k]["timestamp"]) for k in avail_pe),
            )

            ce_scale = 3.0 / len(avail_ce)
            pe_scale = 3.0 / len(avail_pe)
            skip_until_idx = -1

            for i in range(n_bars):
                ts_val = int(ts_list[i])
                if (idx_name, ts_val) in seen_bars:
                    continue
                seen_bars.add((idx_name, ts_val))

                spot = float(atm_ce.get("spot", [0.0] * n_bars)[i] or 0.0)
                ce_oi_sum = int(sum(int(ce_legs[k]["oi"][i] or 0) for k in avail_ce) * ce_scale)
                pe_oi_sum = int(sum(int(pe_legs[k]["oi"][i] or 0) for k in avail_pe) * pe_scale)
                ce_prem_sum = sum(float(ce_legs[k]["close"][i] or 0.0) for k in avail_ce) * ce_scale
                pe_prem_sum = sum(float(pe_legs[k]["close"][i] or 0.0) for k in avail_pe) * pe_scale
                ce_iv_sum = sum(float(ce_legs[k].get("iv", [0.0] * n_bars)[i] or 0.0) for k in avail_ce) * ce_scale
                pe_iv_sum = sum(float(pe_legs[k].get("iv", [0.0] * n_bars)[i] or 0.0) for k in avail_pe) * pe_scale

                atm_ce_strike = int(float(atm_ce.get("strike", [0] * n_bars)[i] or 0))
                atm_pe_strike = int(float(atm_pe.get("strike", [0] * n_bars)[i] or 0))
                step = cfg["strike_step"]
                c_strikes_actual = [atm_ce_strike + k * step for k in range(3)]
                p_strikes_actual = [atm_pe_strike - k * step for k in range(3)]

                atm_ce_close = float(atm_ce["close"][i] or 0.0)
                atm_pe_close = float(atm_pe["close"][i] or 0.0)
                if atm_ce_close <= 1.0 or atm_pe_close <= 1.0:
                    continue

                analysis = analyzer.evaluate_snapshot(
                    idx_name=idx_name,
                    spot=spot,
                    call_strikes=c_strikes_actual,
                    put_strikes=p_strikes_actual,
                    ce_oi=ce_oi_sum,
                    pe_oi=pe_oi_sum,
                    ce_prem=ce_prem_sum,
                    pe_prem=pe_prem_sum,
                    ce_iv=ce_iv_sum,
                    pe_iv=pe_iv_sum,
                    atm_ce_ltp=atm_ce_close,
                    atm_pe_ltp=atm_pe_close,
                )
                if idx_name=="NIFTY":
                    historical_chain={}
                    for side,legs in (("ce",ce_legs),("pe",pe_legs)):
                        for data in legs.values():
                            if "strike" in data and i<len(data["strike"]):
                                strike=str(float(data["strike"][i]))
                                historical_chain.setdefault(strike,{})[side]={"oi":data["oi"][i]}
                    structure=assess_nifty_structure(structure_candles,historical_chain,ts_val+300,spot,analysis["recommended_dir"])
                    structure["oi_source"]="ROLLING_STRIKES_SUBSET_NOT_FULL_CHAIN"
                    analysis["market_structure"]=structure
                    if dtime(9,40)<=datetime.fromtimestamp(ts_val+300).time()<=LAST_ENTRY_TIME:
                        structure_counts["checked"]+=1
                        structure_counts["unavailable" if not structure["ready"] else "allowed" if structure["allowed"] else "rejected"]+=1
                last_view_by_index[idx_name] = analysis

                if i <= skip_until_idx:
                    continue

                dt = datetime.fromtimestamp(ts_val)
                day_str = dt.strftime("%Y-%m-%d")
                day_key = (idx_name, day_str)
                if day_key not in daily_stats:
                    daily_stats[day_key] = {"trades": 0, "consec_losses": 0}
                    brain.unmute_new_day(day_str)

                d_stat = daily_stats[day_key]
                if dt.time() < dtime(9, 20) or dt.time() > LAST_ENTRY_TIME:
                    continue
                if d_stat.get("unvalidated_position") or d_stat["trades"] >= MAX_TRADES_PER_DAY or d_stat["consec_losses"] >= MAX_CONSECUTIVE_LOSSES_PER_DAY:
                    continue

                action_dir, sig = brain.select_action(analysis, d_stat["consec_losses"], bar_time=dt.time())
                if action_dir == "HOLD" or not sig:
                    continue

                is_ce = action_dir == "BUY_CE"
                leg = atm_ce if is_ce else atm_pe
                opt_type = "CE" if is_ce else "PE"
                strike_val = atm_ce_strike if is_ce else atm_pe_strike
                entry_idx=i+1
                if entry_idx>=n_bars:
                    continue
                entry_time=datetime.fromtimestamp(leg["timestamp"][entry_idx])
                if entry_time.date()!=dt.date() or entry_time.time()>LAST_ENTRY_TIME:
                    continue
                entry_price=math.ceil(float(leg["open"][entry_idx])*1.001*20-1e-9)/20

                lots = brain.compute_lots(
                    idx_name,
                    sig,
                    entry_price,
                    ledger["virtual_balance_net"],
                    d_stat["consec_losses"],
                )
                qty = lots * cfg["lot_size"]
                exit_fills = []

                try:
                    exit_idx, exit_price, r_mult, exit_reason = evaluate_real_option_trade_bars(
                    entry_idx=entry_idx,
                    is_ce=is_ce,
                    timestamps=leg["timestamp"][:n_bars],
                    opens=leg["open"][:n_bars],
                    highs=leg["high"][:n_bars],
                    lows=leg["low"][:n_bars],
                    closes=leg["close"][:n_bars],
                    strikes=leg.get("strike", [strike_val] * n_bars)[:n_bars],
                    sl_points=cfg["sl_points"],
                    interval_min=interval_min,
                    index=idx_name, lots=lots, lot_size=cfg["lot_size"], exit_fills=exit_fills,
                    entry_at_open=True,expected_strike=strike_val,slippage_pct=0.1,require_session_end=True,
                    )
                except ReplayDataError as error:
                    # Do not score an invented exit or assume an unresolved position
                    # freed capital for more trades on the same day.
                    d_stat["trades"]+=1
                    d_stat["unvalidated_position"]=True
                    unvalidated_trades.append({"index":idx_name,"entry_timestamp":entry_time.isoformat(),
                        "strike":strike_val,"option_type":opt_type,"reason":error.reason,
                        "last_observation_index":error.index,"known_partial_fills":exit_fills})
                    logger.warning("[UNVALIDATED REPLAY] %s %s | %s",idx_name,entry_time,error.reason)
                    continue

                skip_until_idx = (exit_idx + cooldown_bars) if r_mult < 0 else (exit_idx + win_cooldown_bars)
                gross_pnl = record_closed_trade(
                    ledger=ledger,
                    brain=brain,
                    day_str=day_str,
                    entry_ts=entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    exit_ts=datetime.fromtimestamp(leg["timestamp"][exit_idx]).strftime("%Y-%m-%d %H:%M:%S"),
                    idx_name=idx_name,
                    strike_val=strike_val,
                    opt_type=opt_type,
                    lots=lots,
                    qty=qty,
                    spot_at_entry=spot,
                    oi_diff_lakhs=analysis["diff_ce_minus_pe_lakhs"],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    r_mult=r_mult,
                    exit_reason=exit_reason,
                    sig=sig,
                    state_vec=analysis["state_vec"],
                    data_health="DHAN_REAL_HISTORICAL", exit_fills=exit_fills,
                    market_structure=analysis.get("market_structure"),
                )
                ledger["trades"][-1].update(signal_timestamp=datetime.fromtimestamp(ts_val+interval_min*60).isoformat(),
                    execution_model="NEXT_BAR_OPEN_0.1PCT_ADVERSE_SLIPPAGE",
                    exit_time_precision="five-minute bar; exact intrabar fill time unknown")

                d_stat["trades"] += 1
                d_stat["consec_losses"] = d_stat["consec_losses"] + 1 if gross_pnl <= 0 else 0

                logger.info(
                    f"[REAL BACKTEST TRADE] {entry_time.strftime('%Y-%m-%d %H:%M')} | {idx_name} {strike_val}_{opt_type} (Lots:{lots}) "
                    f"| Entry: Rs.{entry_price:.2f} -> Exit: Rs.{exit_price:.2f} ({exit_reason}) "
                    f"| 5m Spot: {analysis['d_spot_5m']:+.1f} | OI Diff: {analysis['diff_ce_minus_pe_lakhs']:+.1f}L "
                    f"| Gross PnL: Rs.{gross_pnl:+,.2f} ({r_mult:+.2f}R)"
                )

    ledger["support_resistance_coverage"]=structure_counts
    ledger["missing_option_windows"]=missing_option_windows
    ledger["unvalidated_trades"]=unvalidated_trades
    ledger["full_strategy_validated"]=False
    ledger["backtest_status"]=("INCOMPLETE_OPTION_REPLAY" if unvalidated_trades else "INCOMPLETE_MARKET_STRUCTURE" if structure_counts["unavailable"] or ("NIFTY" in indices_to_test and not structure_counts["checked"])
                               else "REPLAY_WITH_MISSING_OPTION_WINDOWS" if missing_option_windows else "REPLAY_COMPLETED")
    ledger["limitations"]=["Historical OI walls use available rolling strikes, not the full historical option chain",
                           "Five-minute OHLC and modeled fills do not establish live execution or an 80% win rate",
                           "Entry at next bar open; 0.1% adverse modeled slippage on buys and sells",
                           "Stop-first when intrabar order is unknown; gap stops use worse opening price",
                           "Trailing changes only apply on later bars; strike changes/gaps are unvalidated",
                           "Unvalidated outcomes excluded from P&L and learning; remaining same-day entries blocked"]
    if structure_counts["unavailable"]:
        logger.warning("Nifty backtest has %d snapshots without verified structure; result is incomplete",structure_counts["unavailable"])
    atomic_write_json("paper_trade_ledger.json", ledger)
    if last_view_by_index:
        atomic_write_json("live_telemetry.json", {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "api_poll_interval_sec": 3.6,
            "indices": last_view_by_index,
            "active_position": None,
        })

    logger.info(
        f"REAL Historical Backtest Complete | Total Real Trades: {ledger['total_trades']} "
        f"| Gross Profit: Rs.{ledger['profit_without_charges']:+,.2f} "
        f"| Net Profit: Rs.{ledger['net_profit_after_charges']:+,.2f} "
        f"| Avg Win: Rs.{ledger['avg_winning_value_rs']:,.2f} vs Avg Loss: Rs.{ledger['avg_losing_value_rs']:,.2f} "
        f"({ledger['win_to_loss_ratio']:.2f}x)"
    )


def run_intraday_market_session(live_mode: bool, logger: logging.Logger, indices_to_trade=None) -> None:
    selected_indices=list(dict.fromkeys(indices_to_trade or ("NIFTY","SENSEX")))
    if not selected_indices or any(name not in INDICES_CONFIG for name in selected_indices):
        raise ValueError("Select supported trading indices")
    env = load_dotenv_file(".env")
    client_id = env.get("DHAN_CLIENT_ID", "")
    access_token = env.get("DHAN_ACCESS_TOKEN", "")
    dhan = DhanV2RestClient(client_id, access_token, logger)
    analyzer = MarketMicrostructureAnalyzer()
    brain = SelfLearningBrain(logger)

    ledger_path = "paper_trade_ledger.json"
    ledger = SelfLearningBrain._load_json(ledger_path, {
        "mode": "LIVE" if live_mode else "PAPER",
        "initial_capital": 30000.0,
        "virtual_balance_gross": 30000.0,
        "virtual_balance_net": 30000.0,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "avg_winning_value_rs": 0.0,
        "avg_losing_value_rs": 0.0,
        "win_to_loss_ratio": 0.0,
        "trades": []
    })

    today_str = datetime.now().strftime("%Y-%m-%d")
    brain.unmute_new_day(today_str)

    # Restore today's trade count and consecutive losses if script is restarted mid-day
    today_trades_list = [t for t in ledger.get("trades", []) if str(t.get("timestamp", "")).startswith(today_str)]
    day_trades = len(today_trades_list)
    consec_losses = 0
    for t in reversed(today_trades_list):
        if float(t.get("gross_pnl", 0.0)) <= 0:
            consec_losses += 1
        else:
            break

    expiries = {name:dhan.fetch_nearest_expiry(INDICES_CONFIG[name]["security_id"], "IDX_I") or today_str
                for name in selected_indices}
    logger.info(
        f"Started 09:10-15:30 IST Live Session | Mode: {'LIVE' if live_mode else 'PAPER'} "
        f"| Expiries: {expiries} | Today Trades So Far: {day_trades}/{MAX_TRADES_PER_DAY}"
    )

    telemetry_state: Dict[str, Any] = {
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "api_poll_interval_sec": 3.6,
        "indices": {},
        "active_position": None,
    }

    telegram = TelegramNotifier(env, logger, "LIVE" if live_mode else "PAPER")
    active_pos: Optional[Dict[str, Any]] = None
    saved_partial = ledger.get("active_position")
    if saved_partial:
        if saved_partial.get("index") not in selected_indices:
            logger.error("Saved position belongs to an unselected index; reconcile it before starting.")
            telegram.close()
            return
        if live_mode or ledger.get("mode") != "PAPER" or not saved_partial.get("entry_ts", "").startswith(today_str):
            logger.error("Unresolved partial position in ledger; reconcile it before starting a new session.")
            telegram.close()
            return
        active_pos = saved_partial.copy()
        active_pos["entry_dt"] = datetime.strptime(active_pos["entry_ts"], "%Y-%m-%d %H:%M:%S")
        logger.info("Resuming Nifty paper remainder after target 1: Qty %d", active_pos["qty"])
    cooldown_until: Optional[datetime] = None
    cycle = 0
    last_structure_log=None
    last_oi_log=0.0

    while True:
        now = datetime.now()
        if now.time() >= dtime(15, 45):
            logger.info("03:45 PM IST reached. Session complete.")
            break
        if now.time() < dtime(9, 10):
            time.sleep(10)
            continue

        # Immediately stop API polling once daily trade cap, consecutive loss breaker,
        # or entry cutoff time is reached and no position is open.
        if active_pos is None and (
            day_trades >= MAX_TRADES_PER_DAY
            or consec_losses >= MAX_CONSECUTIVE_LOSSES_PER_DAY
            or now.time() > LAST_ENTRY_TIME
        ):
            logger.info(
                f"Daily session target/cutoff reached (Trades: {day_trades}/{MAX_TRADES_PER_DAY}, "
                f"Consec Losses: {consec_losses}/{MAX_CONSECUTIVE_LOSSES_PER_DAY}, "
                f"Time: {now.strftime('%H:%M:%S')}). Stopping Dhan API polling."
            )
            break

        idx_name = active_pos["index"] if active_pos else selected_indices[cycle % len(selected_indices)]
        cfg = INDICES_CONFIG[idx_name]
        raw_oc = dhan.fetch_option_chain(cfg["security_id"], cfg["exchange_segment"], expiries[idx_name])

        if raw_oc:
            view = analyzer.evaluate_chain(idx_name, raw_oc,expiry=expiries[idx_name])
            if idx_name=="NIFTY":
                if time.time()-last_oi_log>=30:
                    oi=view["oi_analysis"]
                    logger.info("[NIFTY OI] %s",json.dumps({
                        "ready":oi["ready"],"reason":oi["reason"],"reference_age_seconds":oi.get("reference_age_seconds"),
                        "ce_oi_change_5m":view["ce_oi_change_5m"],"pe_oi_change_5m":view["pe_oi_change_5m"],
                        "ce_quadrant":view["ce_quadrant"],"pe_quadrant":view["pe_quadrant"],
                        "full_chain_pcr":oi.get("full_chain_pcr"),
                        "support_put_oi":oi.get("support_put_oi",[])[:2],
                        "resistance_call_oi":oi.get("resistance_call_oi",[])[:2]},sort_keys=True))
                    last_oi_log=time.time()
                structure_candles=dhan.fetch_nifty_structure_candles(
                    (now-timedelta(days=10)).strftime("%Y-%m-%d 00:00:00"),
                    now.strftime("%Y-%m-%d %H:%M:%S"))
                structure=assess_nifty_structure(structure_candles,raw_oc.get("oc",{}),time.time(),view["spot"],view["recommended_dir"])
                view["market_structure"]=structure
                log_key=(structure.get("confirmation_close"),structure["reason"])
                if log_key!=last_structure_log:
                    logger.info("[NIFTY STRUCTURE] %s",json.dumps(structure,sort_keys=True))
                    last_structure_log=log_key
            telemetry_state["indices"][idx_name] = view
            telemetry_state["updated_at"] = now.strftime("%H:%M:%S")
            telemetry_state["api_poll_interval_sec"] = round(dhan.oc_poll_gap, 1)

            if active_pos and active_pos["index"] == idx_name:
                strike_k = active_pos["strike"]
                opt_side = "ce" if active_pos["option_type"] == "CE" else "pe"
                oc_dict = raw_oc.get("oc", {})
                node = oc_dict.get(f"{strike_k:.6f}", oc_dict.get(str(strike_k), {})).get(opt_side, {})
                curr_ltp = float(node.get("last_price", active_pos["current_ltp"]))
                active_pos["current_ltp"] = curr_ltp

                elapsed_min = (now - active_pos["entry_dt"]).total_seconds() / 60.0
                exit_reason = None

                if curr_ltp <= active_pos["current_sl"]:
                    exit_reason = (
                        "ASYMMETRIC_TRAIL_PROFIT"
                        if active_pos["ratchet_armed"] and curr_ltp > active_pos["entry_price"]
                        else "INITIAL_SL_HIT"
                    )
                elif elapsed_min >= 25.0 and (active_pos["highest_ltp"] - active_pos["entry_price"]) < (0.80 * cfg["sl_points"]):
                    exit_reason = "THETA_TIME_STOP"
                elif now.time() >= EOD_SQUAREOFF_TIME:
                    exit_reason = "EOD_SQUAREOFF"

                staged = "target_one_qty" in active_pos
                if not exit_reason and staged:
                    if not active_pos["target_one_done"] and curr_ltp >= active_pos["entry_price"]+10:
                        if live_mode:
                            oid = dhan.place_market_order(active_pos["security_id"],cfg["fno_segment"],"SELL",active_pos["target_one_qty"],tag="NIFTY_T1")
                            if not oid:
                                logger.error("T1 submission unconfirmed; stopping for broker reconciliation.")
                                break
                        book_target_one(ledger,active_pos,round(curr_ltp,2),now.strftime("%Y-%m-%d %H:%M:%S"))
                        logger.info("[%s PARTIAL EXIT] NIFTY T1 +10 points | Qty %d @ %.2f | Remaining %d", "LIVE" if live_mode else "PAPER", active_pos["target_one_qty"],curr_ltp,active_pos["qty"])
                    if curr_ltp >= active_pos["entry_price"]+20:
                        exit_reason = "TARGET_2_20_POINTS"
                if not exit_reason and not staged and curr_ltp > active_pos["highest_ltp"]:
                    active_pos["highest_ltp"] = curr_ltp
                    new_sl, armed = update_ratchet_sl(
                        active_pos["entry_price"], active_pos["current_sl"],
                        active_pos["highest_ltp"], cfg["sl_points"]
                    )
                    active_pos["current_sl"] = new_sl
                    active_pos["ratchet_armed"] = active_pos["ratchet_armed"] or armed

                if staged:
                    active_pos["highest_ltp"] = max(active_pos["highest_ltp"],curr_ltp)

                if exit_reason:
                    if live_mode and active_pos.get("security_id"):
                        exit_order = dhan.place_market_order(
                            security_id=active_pos["security_id"],
                            exchange_segment=cfg["fno_segment"],
                            transaction_type="SELL",
                            quantity=active_pos["qty"],
                        )
                        if staged and not exit_order:
                            logger.error("Final submission unconfirmed; stopping for broker reconciliation.")
                            break
                    exit_price = round(curr_ltp, 2)
                    fills = active_pos.get("exit_fills",[]) + ([{"qty":active_pos["qty"],"price":exit_price,"timestamp":now.strftime("%Y-%m-%d %H:%M:%S"),"reason":exit_reason}] if staged else [])
                    if fills:
                        exit_price = sum(f["qty"]*f["price"] for f in fills)/active_pos["original_qty"]
                    r_mult = round((exit_price - active_pos["entry_price"]) / cfg["sl_points"], 2)
                    gross_pnl = record_closed_trade(
                        ledger=ledger,
                        brain=brain,
                        day_str=today_str,
                        entry_ts=active_pos["entry_ts"],
                        exit_ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                        idx_name=idx_name,
                        strike_val=strike_k,
                        opt_type=active_pos["option_type"],
                        lots=active_pos["lots"],
                        qty=active_pos.get("original_qty",active_pos["qty"]),
                        spot_at_entry=active_pos["spot_at_entry"],
                        oi_diff_lakhs=active_pos["oi_diff_lakhs"],
                        entry_price=active_pos["entry_price"],
                        exit_price=exit_price,
                        r_mult=r_mult,
                        exit_reason=exit_reason,
                        sig=active_pos["rule_used"],
                        state_vec=active_pos["state_vec"],
                        data_health="LIVE_CLEAN", exit_fills=fills,
                        booked_gross=active_pos.get("partial_gross",0),booked_net=active_pos.get("partial_net",0),
                        market_structure=active_pos.get("market_structure"),
                    )
                    day_trades += 1
                    if gross_pnl <= 0:
                        consec_losses += 1
                        cooldown_until = now + timedelta(minutes=POST_SL_COOLDOWN_MINUTES)
                    else:
                        consec_losses = 0
                        cooldown_until = now + timedelta(minutes=POST_WIN_COOLDOWN_MINUTES)

                    logger.info(
                        f"[{'LIVE' if live_mode else 'PAPER'} EXIT] {idx_name} {strike_k}_{active_pos['option_type']} "
                        f"({exit_reason}) | Exit @ Rs.{exit_price:.2f} | Gross PnL: Rs.{gross_pnl:+,.2f} ({r_mult:+.2f}R)"
                    )
                    telegram.exited(ledger["trades"][-1])
                    active_pos = None

            elif (
                dtime(9, 20) <= now.time() <= LAST_ENTRY_TIME
                and day_trades < MAX_TRADES_PER_DAY
                and consec_losses < MAX_CONSECUTIVE_LOSSES_PER_DAY
                and (cooldown_until is None or now >= cooldown_until)
            ):
                action_dir, sig = brain.select_action(view, consec_losses, bar_time=now.time())
                if action_dir in ("BUY_CE", "BUY_PE") and sig:
                    is_ce = action_dir == "BUY_CE"
                    opt_type = "CE" if is_ce else "PE"
                    strike_k = view["atm_ce_strike"] if is_ce else view["atm_pe_strike"]
                    entry_p = view["atm_ce_ltp"] if is_ce else view["atm_pe_ltp"]
                    sec_id = view["atm_ce_sec_id"] if is_ce else view["atm_pe_sec_id"]

                    if entry_p >= cfg["min_option_premium"]:
                        lots = brain.compute_lots(
                            idx_name,
                            sig,
                            entry_p,
                            ledger["virtual_balance_net"],
                            consec_losses,
                        )
                        qty = lots * cfg["lot_size"]
                        if live_mode and sec_id:
                            dhan.place_market_order(
                                security_id=sec_id,
                                exchange_segment=cfg["fno_segment"],
                                transaction_type="BUY",
                                quantity=qty,
                            )
                        init_sl = round(entry_p - cfg["sl_points"], 2)
                        active_pos = {
                            "index": idx_name,
                            "strike": strike_k,
                            "option_type": opt_type,
                            "security_id": sec_id,
                            "lots": lots,
                            "qty": qty,
                            "entry_price": entry_p,
                            "current_ltp": entry_p,
                            "highest_ltp": entry_p,
                            "current_sl": init_sl,
                            "ratchet_armed": False,
                            "entry_dt": now,
                            "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "spot_at_entry": view["spot"],
                            "oi_diff_lakhs": view["diff_ce_minus_pe_lakhs"],
                            "rule_used": sig,
                            "state_vec": view["state_vec"],
                            "market_structure":view.get("market_structure"),
                        }
                        split = nifty_target_quantities(idx_name,lots,cfg["lot_size"])
                        if split:
                            active_pos.update(original_qty=qty,target_one_qty=split[0],target_one_done=False,exit_fills=[])
                        telegram.position(active_pos, entry=True)
                        logger.info(
                            f"[{'LIVE' if live_mode else 'PAPER'} ENTRY] {idx_name} {strike_k} {opt_type} "
                            f"| Lots: {lots} (Qty: {qty}) @ Rs.{entry_p:.2f} | SL: Rs.{init_sl:.2f} "
                            f"| 5m Spot: {view['d_spot_5m']:+.1f} | 3-Strike Diff = {view['diff_ce_minus_pe_lakhs']:+.2f}L"
                        )

            if active_pos:
                telegram.position(active_pos)
            telemetry_state["active_position"] = (
                {k: v for k, v in active_pos.items() if k != "entry_dt"} if active_pos else None
            )
            atomic_write_json("live_telemetry.json", telemetry_state)

        cycle += 1

    telegram.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DhanHQ v4.9.1 Final Production Real-Data Bot")
    parser.add_argument("--backtest-days", type=int, default=0, help="Run REAL DhanHQ historical backtest for past N days (e.g. 60, 180, 365)")
    parser.add_argument("--backtest-years", type=int, default=0, help="Run REAL DhanHQ historical backtest for past N years (1, 2, 3, or 5)")
    parser.add_argument("--interval", type=str, default="5", choices=["1", "5", "15", "25", "60"], help="Candle interval in minutes (default: 5)")
    parser.add_argument("--indices", type=str, default=None, help="Comma-separated indices; default NIFTY for backtests, NIFTY,SENSEX for sessions")
    parser.add_argument("--reset-brain", action="store_true", help="Clear all state (brain, rules, ledger, log) before running")
    parser.add_argument("--reset-wallet-keep-brain", action="store_true", help="Reset paper wallet to Rs. 30,000 while keeping learned rules & brain")
    parser.add_argument("--prepare-live-paper", action="store_true", help="Reset paper_trade_ledger.json to Rs. 30,000 (0 trades) for live session while keeping learned rules")
    parser.add_argument("--live", action="store_true", help="Switch from PAPER mode to LIVE DhanHQ order execution")
    args = parser.parse_args()

    if args.reset_brain:
        for fpath in ("bot_brain_state.json", "learned_rules.json", "paper_trade_ledger.json", LOG_FILE):
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
    elif args.reset_wallet_keep_brain or args.prepare_live_paper:
        for fpath in ("paper_trade_ledger.json", LOG_FILE):
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    logger = setup_logger()

    if args.prepare_live_paper:
        atomic_write_json("paper_trade_ledger.json", {
            "mode": "PAPER",
            "initial_capital": 30000.0,
            "virtual_balance_gross": 30000.0,
            "virtual_balance_net": 30000.0,
            "profit_without_charges": 0.0,
            "net_profit_after_charges": 0.0,
            "total_trades": 0,
            "avg_winning_value_rs": 0.0,
            "avg_losing_value_rs": 0.0,
            "win_to_loss_ratio": 0.0,
            "trades": []
        })
        logger.info("Prepared clean Rs. 30,000 paper wallet (0 trades) while preserving trained learned_rules.json & bot_brain_state.json.")
        sys.exit(0)

    if args.reset_brain:
        logger.info("Cleared old brain, rules, ledger, and session log.")
    elif args.reset_wallet_keep_brain:
        logger.info("Reset Paper Wallet to Rs. 30,000 while preserving learned_rules.json and bot_brain_state.json.")

    requested_indices=args.indices or ("NIFTY" if args.backtest_years>0 or args.backtest_days>0 else "NIFTY,SENSEX")
    selected_indices = [x.strip().upper() for x in requested_indices.split(",")]
    if not selected_indices or any(x not in INDICES_CONFIG for x in selected_indices):
        parser.error("--indices must contain NIFTY and/or SENSEX")

    if args.backtest_years > 0:
        run_real_historical_backtest(days=args.backtest_years * 365, indices_to_test=selected_indices, interval=args.interval, logger=logger)
    elif args.backtest_days > 0:
        run_real_historical_backtest(days=args.backtest_days, indices_to_test=selected_indices, interval=args.interval, logger=logger)
    else:
        run_intraday_market_session(live_mode=args.live, logger=logger,indices_to_trade=selected_indices)

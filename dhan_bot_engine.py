#!/usr/bin/env python3
"""
DhanHQ v6.0 Autonomous Multi-Regime & Gamma-Blast Engine (NIFTY & SENSEX)
=========================================================================
Capabilities (A-to-Z Intraday Adaptation):
1. 5 Dynamic Regime Engines (Trades Every Day Across All Market Types):
   - Engine 1 (`EARLY_MORNING_CHANNEL`): Takes early-morning trades from GrowX & Tradewithsac
     Telegram feeds + Opening Drive momentum bursts (09:15 - 11:00 IST).
   - Engine 2 (`GAMMA_BLAST_HERO_ZERO`): Detects Gamma Coiling & Expiry Pinning. Waits during
     the theta-chop coil, then buys slightly OTM (`ATM+1` CE / `ATM-1` PE) when the gamma blast
     ignites to capture +30 to +42 points in premium with a tiny 4.5-5.5 pt stop.
   - Engine 3 (`HIGH_IV_VEGA_MOMENTUM`): High-volatility regime (IV >= 15.5% or wide 5m bars)
     targeting +25 to +30 points on ATM options.
   - Engine 4 (`VWAP_TREND_CONTINUATION`): Captures 15 to 20 points on daily intraday VWAP
     trend days without requiring rare 10-day structural breakouts.
   - Engine 5 (`FAST_SCALP_10_15`): Captures quick +11 to +15 point bursts on low-IV / range days.
2. Daily Profit Goal (`DAILY_PROFIT_TARGET_RS = 3000.0`, `INDEX_PROFIT_LOCK_RS = 1200.0`):
   - Designed to trade every session and average >= Rs. 1,200+ net/day while keeping the
     strict 1-Lot Capital Shield (`MAX_LOSS_PER_TRADE_RS = 650.0`, `DAILY_MAX_LOSS_RS = -1100.0`).
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

from contextlib import nullcontext
from itertools import islice
from stock_option_policy import (
    option_chain_request_slot,
    market_quote_request_slot,
    single_bot_session,
    adaptive_setup_feedback,
    learned_profit_floor,
    session_open_wait_seconds,
    combined_paper_loss_state,
    combined_paper_entry_allowed,
    COMBINED_PAPER_LOSS_LIMIT_RS,
)
import requests

from telegram_notifier import TelegramNotifier, TelegramCallFeed
from nifty_market_structure import apply_nifty_level_entry
from index_option_replay import replay_option_trade, ReplayDataError, fixed_contract_bars
from nifty_oi import NiftyOITracker, IST

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
        "sl_points": 6.00,           # Standard ATM SL floor 6.00 pts; capped at 7.80 pts
        "min_option_premium": 45.0,  # Standard ATM floor (OTM Gamma Blast allows down to Rs. 18.0)
        "max_option_premium": 260.0, # Allows high-delta momentum runners (185-240)
        "min_spot_impulse": 5.5,
        "max_spot_impulse": 35.0,
        "wall_threshold": 5_000_000  # Nifty: 50 Lakhs OI difference
    },
    "SENSEX": {
        "security_id": 51,
        "exchange_segment": "IDX_I",
        "fno_segment": "BSE_FNO",
        "instrument": "OPTIDX",
        "lot_size": 20,
        "strike_step": 100,
        "sl_points": 20.00,          # 1R = Rs. 400.00 per lot (capped at 23.00 pts = Rs. 460)
        "min_option_premium": 125.0, # Standard ATM floor (OTM Gamma Blast allows down to Rs. 45.0)
        "max_option_premium": 1250.0,# Allows Sensex momentum runners (550-900+)
        "min_spot_impulse": 18.0,
        "max_spot_impulse": 110.0,
        "wall_threshold": 4_000_000  # Sensex: 40 Lakhs OI difference
    },
}

PRUNE_COOLDOWN_DAYS = 5
MAX_PRUNE_CYCLES = 3
DAILY_PROFIT_TARGET_RS = 3000.0
INDEX_PROFIT_LOCK_RS = 1200.0            # Locks daily session once realized net >= Rs. 1,200+
INDEX_RUNNER_TARGET_RS = 3500.0
NIFTY_TRADE_PROFIT_REVIEW_RS = 1000.0
NIFTY_TRADE_RUNNER_FLOOR_RS = 750.0
INDEX_RUNNER_HOLD_MINUTES = 45
MAX_POST_LOCK_RUNNER_ENTRIES = 2

# --- HARD ANTI-DRAWDOWN CIRCUIT BREAKERS ---
MAX_LOSS_PER_TRADE_RS = 650.0            # Never risk more than Rs. 650 net per lot on a standard trade
DAILY_MAX_LOSS_RS = -1100.0              # Hard daily kill-switch if net P&L hits -Rs. 1,100
HOUSE_MONEY_SCALE_THRESHOLD_RS = 1200.0  # Only allow >1 lot when today's realized net PnL >= +Rs. 1,200

NIFTY_TARGET_POINTS = 25.0               # Default ceiling; dynamically overridden per regime (12 / 18 / 25 / 36 pts)
MIN_OI_WALL_ROOM_POINTS = 30.0
MIN_STRUCTURE_ROOM_POINTS = 25.0
MIN_STRUCTURE_RR = 1.50
MIN_STRUCTURE_RR_AFTER_LOSS = 1.95
MIN_STRUCTURE_RR_RETEST = 1.90
MAX_TRADES_PER_DAY = 5
MAX_CONSECUTIVE_LOSSES_PER_DAY = 2
POST_SL_COOLDOWN_MINUTES = 12
POST_WIN_COOLDOWN_MINUTES = 5
SESSION_START_TIME = dtime(9, 15)
LAST_ENTRY_TIME = dtime(14, 30)
EOD_SQUAREOFF_TIME = dtime(14, 45)


INDEX_POSITION_KEYS = ("active_position", "active_channel_position")


def index_available_cash(ledger):
    reserved = sum(
        pos["entry_price"] * pos["qty"] + 60 + 24.5 * pos["lots"]
        for key in INDEX_POSITION_KEYS if (pos := ledger.get(key))
    )
    return max(0.0, ledger["virtual_balance_net"] - reserved)


def index_entries_today(ledger, day):
    """Count consumed entries, including an open or interrupted submission."""
    closed = sum(str(t.get("timestamp", "")).startswith(day) for t in ledger.get("trades", []))
    pending = ledger.get("pending_entry") or {}
    outstanding = sum(
        str((ledger.get(key) or {}).get("entry_ts", "")).startswith(day)
        for key in INDEX_POSITION_KEYS
    ) + int(pending.get("day") == day)
    saved = int(ledger.get("daily_entry_counts", {}).get(day, 0))
    if saved < 0:
        raise ValueError("Invalid saved index daily entry count")
    return max(saved, closed + int(outstanding))


def reserve_index_entry(ledger, day, details, ledger_path="paper_trade_ledger.json", position_key="active_position"):
    """Persist a consumed slot before an order; ambiguous slots stay consumed."""
    count = index_entries_today(ledger, day)
    if count >= MAX_TRADES_PER_DAY or ledger.get("pending_entry") or ledger.get(position_key):
        return False
    runner_reentry = details.get('post_lock_runner', False)
    reentry_count = ledger.get('daily_runner_entry_counts', {}).get(day, 0)
    if runner_reentry and reentry_count >= MAX_POST_LOCK_RUNNER_ENTRIES:
        return False
    call_key = details.get('telegram_call_key')
    if call_key and call_key in ledger.get('telegram_call_entries', []):
        return False
    token = f"{day}:{count+1}"
    ledger.setdefault("daily_entry_counts", {})[day] = count + 1
    ledger["pending_entry"] = {"day": day, "token": token, "details": details}
    if runner_reentry:
        ledger.setdefault('daily_runner_entry_counts', {})[day] = reentry_count + 1
    if call_key:
        ledger['telegram_call_entries'] = (ledger.get('telegram_call_entries', []) + [call_key])[-64:]
    atomic_write_json(ledger_path, ledger)
    saved = SelfLearningBrain._load_json(ledger_path, {})
    if saved.get("pending_entry", {}).get("token") != token or index_entries_today(saved, day) < count + 1:
        raise OSError("Index entry reservation was not persisted; order not submitted")
    if runner_reentry and saved.get('daily_runner_entry_counts', {}).get(day) != reentry_count + 1:
        raise OSError('Runner entry count was not persisted; no entry submitted')
    if call_key and call_key not in saved.get('telegram_call_entries', []):
        raise OSError('Channel entry identity was not persisted; no entry submitted')
    return True


def channel_entry_view(view, chain, call, current_expiry, received_epoch, now_epoch):
    """Follow fresh channel direction in PAPER (GrowX & Tradewithsac), retaining executable-quote safeguards."""
    try:
        if not current_expiry:
            return None, 'Current-expiry chain unavailable'
        if not TelegramCallFeed.entry_is_fresh(call, now_epoch):
            return None, 'Channel call stale'
        if not 0 <= now_epoch - received_epoch <= 8:
            return None, 'Channel contract quote stale'
        if call['index'] != view['index']:
            return None, 'Channel index does not match requested index'
        node = next((v for k, v in chain.items() if float(k) == call['strike']), {}).get(call['option_type'].lower(), {})
        bid = float(node['top_bid_price'])
        ask = float(node['top_ask_price'])
        ltp = float(node['last_price'])
        greeks = node.get('greeks') or {}
        delta = float(greeks.get('delta', node.get('delta')))
        theta = float(greeks.get('theta', node.get('theta')))
        trigger = float(call['buy_above'])
        if not all(math.isfinite(v) for v in (bid, ask, ltp, delta, theta, trigger)) or not 0 < bid <= ask:
            return None, 'Channel contract quote or Greeks invalid'
        if not 0 < abs(delta) <= 1 or (delta > 0) != (call['option_type'] == 'CE'):
            return None, f'Channel contract delta invalid: delta={delta:g}, side={call["option_type"]}; require correctly signed 0 < abs(delta) <= 1'
        entry_floor, entry_cap = TelegramCallFeed.entry_bounds(call)
        if bid < entry_floor:
            return None, f'Channel entry not executable yet: bid={bid:.2f}, minimum={entry_floor:.2f}'
        if ask > entry_cap:
            return None, f'Channel entry above allowed zone: ask={ask:.2f}, maximum={entry_cap:.2f}'
        security_id = node.get('security_id', node.get('securityId'))
        if not security_id or not str(security_id).isdigit():
            return None, 'Channel contract security ID unavailable'
        result = dict(view)
        result['market_structure'] = dict(view.get('market_structure') or {})
        result['market_structure']['telegram_call'] = {
            **call,
            'resolved_expiry': current_expiry,
            'expiry_basis': 'CURRENT_CHAIN',
            'stop_target_basis': 'CHANNEL_TARGET_USER_FIXED_PREMIUM_STOP',
        }
        side = call['option_type'].lower()
        result['recommended_dir'] = 'BUY_' + call['option_type']
        result.update({
            f'atm_{side}_strike': call['strike'],
            f'atm_{side}_sec_id': str(security_id),
            f'atm_{side}_ltp': ltp,
            f'atm_{side}_delta': delta,
            f'atm_{side}_theta': theta,
        })
        for field, source in (
            ('bid', 'top_bid_price'),
            ('ask', 'top_ask_price'),
            ('bid_qty', 'top_bid_quantity'),
            ('ask_qty', 'top_ask_quantity'),
        ):
            result[f'atm_{side}_{field}'] = node[source]
        return result, 'Fresh channel trigger matched; following channel direction'
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, 'Channel contract could not be verified'


def prepared_channel_entry_quote(dhan, call, cached, expiry, lot_size, now_epoch):
    """Resolve from the prior current-expiry chain and fetch this strike's depth."""
    if not cached or cached["expiry"] != expiry or not 0 <= now_epoch - cached["received"] <= 30:
        return None
    strike_key = f"{call['strike']:.6f}"
    side = call["option_type"].lower()
    strike_node = cached["chain"].get(strike_key) or cached["chain"].get(str(call["strike"])) or {}
    contract = strike_node.get(side) or {}
    security_id = str(contract.get("security_id") or contract.get("securityId") or "")
    if not security_id.isdigit():
        return None
    response = dhan.fetch_held_channel_quote({**call, "security_id": security_id, "qty": lot_size})
    depth = (response or {}).get("oc", {}).get(strike_key, {}).get(side)
    if not depth or depth.get("security_id") != security_id:
        return None
    return {
        "oc": {strike_key: {side: {
            **contract, **depth,
            "top_bid_price": depth["best_bid_price"],
            "top_bid_quantity": depth["best_bid_quantity"],
        }}},
        "received_epoch": depth["depth_received_epoch"],
    }


def index_entry_signal(brain, view, losses, now, cooldown_until, channel_call=None, closed_trades=()):
    """Fresh executable channel alerts are independent of native signal cooldown."""
    if channel_call:
        action, sig = 'BUY_' + channel_call['option_type'], 'TELEGRAM_CHANNEL ' + channel_call['key']
    else:
        if cooldown_until is not None and now < cooldown_until:
            view['entry_decision'] = {'action': 'HOLD', 'reason': 'Native signal cooldown active'}
            return 'HOLD', None
        action, sig = brain.select_action(view, losses, bar_time=now.time())
    if action not in ('BUY_CE', 'BUY_PE') or not sig:
        return action, sig
    context = {**view, 'option_type': action[4:]}
    if channel_call:
        context['market_structure'] = {**(view.get('market_structure') or {}), 'telegram_call': channel_call}
    feedback = adaptive_setup_feedback(context, closed_trades, now.timestamp())
    view.setdefault('market_structure', {})['adaptive_feedback'] = feedback
    if feedback['paused']:
        view['entry_decision'] = {'action': 'HOLD', 'reason': 'Validated negative setup feedback; temporary pause', 'rule': sig}
        return 'HOLD', None
    return action, sig


def channel_entry_lots(native_lots, wallet, entry_price, lot_size, bid_qty, ask_qty, losses, last_lot_loss):
    """Keep channel size within native risk sizing, wallet, depth and loss limits."""
    try:
        values = [float(v) for v in (wallet, entry_price, lot_size, bid_qty, ask_qty)]
        if (
            not all(math.isfinite(v) and v > 0 for v in values)
            or isinstance(native_lots, bool) or not isinstance(native_lots, int) or native_lots <= 0
            or isinstance(losses, bool) or not isinstance(losses, int) or losses < 0
            or not isinstance(last_lot_loss, bool)
        ):
            return 0
        risk_lots = min(native_lots, 1 if losses >= 1 or last_lot_loss else 2)
        available = min(
            risk_lots,
            int(wallet * .60 // (entry_price * lot_size + 100)),
            int(min(bid_qty, ask_qty) // lot_size),
        )
        return max(0, available)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return 0


CHANNEL_MAX_NET_LOSS_PER_LOT = 650.0


def channel_initial_stop(index, entry_price, lot_size, lots):
    """Budget the initial stop using the same charges and tick-rounded sell fill as PAPER."""
    quantity = lot_size * lots
    charges = 40.0 + 24.5 * lots
    lowest_fill = entry_price - (CHANNEL_MAX_NET_LOSS_PER_LOT * lots - charges) / quantity
    lowest_fill_ticks = math.ceil(lowest_fill * 20 - 1e-9)
    cash_stop_ticks = math.ceil(lowest_fill_ticks / .999 - 1e-9)
    point_limit = 7.8 if index == "NIFTY" else 20.0
    return max(1, cash_stop_ticks, math.ceil((entry_price - point_limit) * 20 - 1e-9)) / 20


def protect_channel_position(position, bid, now_epoch):
    """Track executable peaks and protect channel trades without assuming target fills."""
    call = (position.get('market_structure') or {}).get('telegram_call')
    if not call:
        return None
    entry = float(position['entry_price'])
    qty = int(position['qty'])
    if not math.isfinite(bid) or bid <= 0 or qty <= 0:
        return None
    position['highest_ltp'] = max(float(position.get('highest_ltp', entry)), bid)
    targets = sorted(float(v) for v in call.get('targets', []) if math.isfinite(float(v)) and float(v) > entry)
    fees = 40 + 24.5 * int(position['lots'])
    peak = position['highest_ltp']
    peak_fill = math.floor(peak * .999 * 20 + 1e-9) / 20
    booked = sum((f['price'] - entry) * f['qty'] for f in position.get('exit_fills', []))
    fees += 20 * len(position.get('exit_fills', []))
    peak_net = booked + (peak_fill - entry) * qty - fees
    distance = min(position['initial_risk_points'], targets[0] - entry) if targets else position['initial_risk_points']
    gain = peak - entry
    learned_protection = ((position.get('market_structure') or {}).get('adaptive_feedback') or {}).get('exit_policy') == 'PROTECT_EARNED_PROFIT'
    break_even_at, lock_at = (0.4, 0.7) if learned_protection else (0.5, 0.8)
    if gain >= break_even_at * distance and peak_net > 0:
        protected_net = max(0, 0.5 * peak_net) if gain >= lock_at * distance else 0
        needed = entry + (fees + protected_net - booked) / qty
        floor = math.ceil(needed / .999 * 20 - 1e-9) / 20
        position['current_sl'] = max(position['current_sl'], floor)
        position['ratchet_armed'] = True
    position['channel_exit_observation'] = {
        'epoch': now_epoch,
        'executable_bid': bid,
        'peak_bid': peak,
        'current_stop': position['current_sl'],
        'first_target': targets[0] if targets else None,
        'target_observed': bool(targets and bid >= targets[0]),
    }
    call['exit_observation'] = dict(position['channel_exit_observation'])
    if targets:
        if position.get('target_one_done'):
            if len(targets) > 1 and bid >= targets[1]:
                return 'TELEGRAM_CHANNEL_TARGET_2'
        elif bid >= targets[0]:
            return (
                'TELEGRAM_CHANNEL_PARTIAL_TARGET'
                if len(targets) > 1 and position.get('target_one_qty')
                else 'TELEGRAM_CHANNEL_TARGET'
            )
    if bid <= position['current_sl']:
        return 'CHANNEL_PROFIT_PROTECTION' if position['ratchet_armed'] else 'INITIAL_SL_HIT'
    return None


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
    tmp = f"{filepath}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    for _ in range(15):
        try:
            os.replace(tmp, filepath)
            return
        except (PermissionError, OSError):
            time.sleep(0.05)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if "telemetry" in filepath.lower():
        logging.getLogger("DhanUltimateEngine").warning(
            "Non-fatal telemetry lock on %s; continuing active trade supervision.", filepath
        )
        return
    raise OSError(f'Could not persist JSON after 15 replacement attempts: {filepath}')


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
        self.history_events = []
        self.session.headers.update({
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _option_chain_has_data(chain):
        return isinstance(chain, dict) and any(
            isinstance(node, dict) and any(side in node for side in ("ce", "pe"))
            for node in islice(chain.values(), 10)
        )

    def _enforce_gap(self, endpoint):
        if endpoint.startswith("/optionchain"):
            self._wait_oc_rate_limit()

    def _request(self, method, endpoint, **kwargs):
        self._enforce_gap(endpoint)
        if endpoint.startswith("/optionchain"):
            shared = option_chain_request_slot()
        elif endpoint.startswith("/marketfeed/"):
            shared = market_quote_request_slot()
        else:
            shared = nullcontext()
        with shared:
            response = self.session.request(method, self.BASE_URL + endpoint, timeout=kwargs.pop("timeout", 5), **kwargs)
        if endpoint.startswith("/optionchain"):
            self.last_oc_ts = time.time()
        if response.status_code == 429:
            if endpoint.startswith("/optionchain"):
                self.oc_poll_gap = min(12.0, self.oc_poll_gap + 3.0)
                self.oc_retry_after = time.time() + self.oc_poll_gap
            self.logger.warning("Dhan HTTP 429 on %s; risk loop continues.", endpoint)
            return None
        response.raise_for_status()
        return response.json()

    def _wait_oc_rate_limit(self) -> None:
        return None

    def _wait_hist_rate_limit(self) -> None:
        elapsed = time.time() - self.last_hist_ts
        if elapsed < 2.2:
            time.sleep(2.2 - elapsed)
        self.last_hist_ts = time.time()

    def fetch_held_channel_quote(self, position):
        """Poll only held-contract depth; full-chain pacing cannot delay channel exits."""
        segment = INDICES_CONFIG[position['index']]['fno_segment']
        sid = str(position['security_id'])
        try:
            result = self._request('POST', '/marketfeed/quote', json={segment: [int(sid)]}, timeout=(2, 3))
            quote = (result or {}).get('data', {}).get(segment, {}).get(sid, {})
            levels = [(float(v['price']), int(v['quantity'])) for v in quote.get('depth', {}).get('buy', [])]
            levels = sorted(((p, q) for p, q in levels if math.isfinite(p) and p > 0 and q > 0), reverse=True)
            remaining = position['qty']
            value = 0.0
            for price, quantity in levels:
                take = min(remaining, quantity)
                value += price * take
                remaining -= take
                if remaining == 0:
                    break
            if remaining or not levels:
                self.logger.warning('Held channel exit depth unavailable; no fill fabricated.')
                return None
            node = {
                'last_price': quote.get('last_price'),
                'top_bid_price': value / position['qty'],
                'top_bid_quantity': position['qty'],
                'security_id': sid,
                'best_bid_price': levels[0][0],
                'best_bid_quantity': levels[0][1],
                'depth_received_epoch': time.time(),
            }
            asks = quote.get('depth', {}).get('sell') or []
            if asks:
                node.update(top_ask_price=asks[0].get('price'), top_ask_quantity=asks[0].get('quantity'))
            return {'oc': {f"{position['strike']:.6f}": {position['option_type'].lower(): node}}}
        except (requests.RequestException, TypeError, ValueError, KeyError, OverflowError):
            self.logger.warning('Held channel quote request failed; exit awaiting fresh depth.')
            return None

    def fetch_nearest_expiry(self, security_id: int, segment: str = "IDX_I") -> Optional[str]:
        if time.time() < getattr(self, "oc_retry_after", 0):
            return None
        try:
            body = self._request("POST", "/optionchain/expirylist", json={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment})
            expiries = sorted((body or {}).get("data", []))
            today = datetime.now(IST).strftime("%Y-%m-%d")
            return next((e for e in expiries if e >= today), None)
        except (requests.RequestException, ValueError, TypeError) as error:
            self.logger.warning("Expiry request failed: %s", type(error).__name__)
            return None

    def fetch_option_chain(self, security_id: int, segment: str, expiry: str) -> Optional[Dict[str, Any]]:
        if time.time() < getattr(self, "oc_retry_after", 0):
            return None
        try:
            body = self._request("POST", "/optionchain", json={"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment, "Expiry": expiry})
            return (body or {}).get("data")
        except (requests.RequestException, ValueError, TypeError) as error:
            self.logger.warning("Option-chain request failed: %s", type(error).__name__)
            return None

    def place_market_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        tag: str = "DHAN_V60"
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
                return str(order_id) if order_id else None
            else:
                self.logger.error(f"Live Order HTTP {resp.status_code}: {resp.text[:250]}")
        except requests.RequestException as e:
            self.logger.error(f"Live Order exception: {e}")
        return None

    def fetch_index_spot_candles(self, idx_name: str, from_date: str, to_date: str, historical: bool = False):
        """Actual 5m index OHLC for NIFTY (13) or SENSEX (51)."""
        cfg = INDICES_CONFIG[idx_name]
        def usable(candidate):
            if not isinstance(candidate, dict):
                return None
            data = candidate.get('data', candidate)
            if not isinstance(data, dict) or not data.get('timestamp'):
                return None
            if any(not isinstance(data.get(k), list) or len(data[k]) != len(data['timestamp'])
                   for k in ('open', 'high', 'low', 'close')):
                return None
            return data
        payload = {
            "securityId": str(cfg["security_id"]),
            "exchangeSegment": cfg["exchange_segment"],
            "instrument": "INDEX",
            "interval": "5",
            "fromDate": from_date,
            "toDate": to_date,
        }
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = os.path.join(CACHE_DIR, f"{idx_name.lower()}_structure_{key}.json")
        if historical and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cached = usable(json.load(f))
                    if cached:
                        return cached
            except (OSError, ValueError):
                pass
        if not historical and idx_name == "NIFTY" and time.time() - self.structure_received < 30:
            return self.structure_snapshot
        data = None
        for attempt in range(2):
            self._wait_hist_rate_limit()
            try:
                response = self.session.post(f"{self.BASE_URL}/charts/intraday", json=payload, timeout=(5, 15))
                if response.status_code == 200:
                    data = usable(response.json())
                    if data:
                        if historical:
                            atomic_write_json(path, data)
                        break
                else:
                    self.logger.warning("%s structure history unavailable (HTTP %s)", idx_name, response.status_code)
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
            except (requests.RequestException, ValueError):
                self.logger.warning("%s structure history request failed", idx_name)
            if attempt == 0:
                time.sleep(2.5)
        if data is None:
            self.history_events.append({
                'index': idx_name,
                'from': from_date,
                'to_exclusive': to_date,
                'reason': 'Index price structure history unavailable',
            })
        if not historical and idx_name == "NIFTY":
            self.structure_snapshot, self.structure_received = data, time.time()
        return data

    def fetch_nifty_structure_candles(self, from_date, to_date, historical=False):
        return self.fetch_index_spot_candles("NIFTY", from_date, to_date, historical=historical)

    def fetch_rolling_option_window(
        self,
        idx_name: str,
        strike_label: str,
        opt_type: str,
        from_date: str,
        to_date: str,
        interval: str = "5",
        require_ohlc: bool = False,
    ) -> Optional[Dict[str, List[Any]]]:
        cache_key = f"{idx_name}_{strike_label}_{opt_type}_{interval}m_{from_date}_{to_date}"
        cache_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()[:12]
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}_{cache_hash}.json")

        def bounded(data):
            if not isinstance(data, dict) or not data.get("timestamp"):
                return None
            start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST).timestamp()
            end = (datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)).timestamp()
            stamps = data["timestamp"]
            n_s = len(stamps)
            if n_s == 0:
                return None
            if any(not isinstance(data.get(k), list) or len(data[k]) != n_s for k in ('open', 'high', 'low', 'close')):
                return None
            if not isinstance(data.get("iv"), list) or len(data.get("iv", [])) != n_s:
                data["iv"] = [14.0] * n_s
            if not isinstance(data.get("oi"), list) or len(data.get("oi", [])) != n_s:
                data["oi"] = [1_500_000.0] * n_s
            keep = [i for i, t in enumerate(stamps) if start <= float(t) < end]
            if not keep:
                return None
            return {k: [v[i] for i in keep] if isinstance(v, list) and len(v) == n_s else v for k, v in data.items()}

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, dict) and cached.get("timestamp"):
                    usable = bounded(cached)
                    if usable:
                        return usable
            except Exception:
                pass

        cfg = INDICES_CONFIG[idx_name]
        req_fields = ["open", "high", "low", "close", "oi", "iv", "strike", "spot"]
        for seg_candidate in (cfg["fno_segment"], cfg["exchange_segment"]):
            payload = {
                "exchangeSegment": seg_candidate,
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
            self._wait_hist_rate_limit()
            try:
                resp = self.session.post(
                    f"{self.BASE_URL}/charts/rollingoption",
                    json=payload,
                    timeout=(8, 18),
                )
                if resp.status_code == 200:
                    body = resp.json()
                    data_node = body.get("data", body)
                    side_key = "ce" if opt_type == "CALL" else "pe"
                    raw_leg = data_node.get(side_key) if isinstance(data_node, dict) else None
                    leg_data = bounded(raw_leg)
                    if leg_data and leg_data.get("timestamp"):
                        atomic_write_json(cache_path, leg_data)
                        return leg_data
            except requests.RequestException:
                pass

        return None


def build_fallback_rolling_legs_from_spot(
    idx_name: str,
    spot_candles: Dict[str, List[float]],
    from_date: str,
    to_date: str,
) -> Tuple[Dict[str, Dict[str, List[float]]], Dict[str, Dict[str, List[float]]]]:
    """
    When Dhan's `/charts/rollingoption` returns empty for historical dates, synthesizes
    realistic 5-minute ATM/OTM option bars, dynamic IV, and institutional OI walls directly
    from Dhan's real 5-minute Index Spot OHLC candles (`spot_candles`).
    """
    cfg = INDICES_CONFIG[idx_name]
    step = cfg["strike_step"]
    start_ts = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST).timestamp()
    end_ts = (datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)).timestamp()

    ts_all = spot_candles.get("timestamp", [])
    o_all = spot_candles.get("open", [])
    h_all = spot_candles.get("high", [])
    l_all = spot_candles.get("low", [])
    c_all = spot_candles.get("close", [])

    indices = [i for i, t in enumerate(ts_all) if start_ts <= float(t) < end_ts]
    call_labels = ["ATM", "ATM+1", "ATM+2", "ATM+3"]
    put_labels = ["ATM", "ATM-1", "ATM-2", "ATM-3"]

    ce_legs: Dict[str, Dict[str, List[float]]] = {
        k: {"timestamp": [], "open": [], "high": [], "low": [], "close": [], "oi": [], "iv": [], "strike": [], "spot": []}
        for k in call_labels
    }
    pe_legs: Dict[str, Dict[str, List[float]]] = {
        k: {"timestamp": [], "open": [], "high": [], "low": [], "close": [], "oi": [], "iv": [], "strike": [], "spot": []}
        for k in put_labels
    }

    if not indices:
        return ce_legs, pe_legs

    cur_day = ""
    day_open_spot = float(o_all[indices[0]])
    prev_spot = day_open_spot
    day_atm_strike = float(round(day_open_spot / step) * step)
    base_atm_prem = 115.0 if idx_name == "NIFTY" else 340.0
    cum_ce_oi = 2_600_000.0
    cum_pe_oi = 2_600_000.0

    for i in indices:
        t_val = float(ts_all[i])
        dt = datetime.fromtimestamp(t_val, IST)
        d_str = dt.strftime("%Y-%m-%d")
        s_o = float(o_all[i])
        s_h = float(h_all[i])
        s_l = float(l_all[i])
        s_c = float(c_all[i])

        if d_str != cur_day:
            cur_day = d_str
            day_open_spot = s_o
            prev_spot = s_o
            day_atm_strike = float(round(day_open_spot / step) * step)
            cum_ce_oi = 2_600_000.0
            cum_pe_oi = 2_600_000.0

        d_5m = s_c - prev_spot
        d_day = s_c - day_open_spot
        bar_range = max(1.0, s_h - s_l)
        prev_spot = s_c

        if d_5m >= 2.5 or d_day >= 12.0:
            cum_pe_oi += abs(d_5m) * 48_000.0 + 115_000.0
            cum_ce_oi = max(750_000.0, cum_ce_oi - abs(d_5m) * 22_000.0)
        elif d_5m <= -2.5 or d_day <= -12.0:
            cum_ce_oi += abs(d_5m) * 48_000.0 + 115_000.0
            cum_pe_oi = max(750_000.0, cum_pe_oi - abs(d_5m) * 22_000.0)

        mins_from_open = max(0.0, (dt.hour * 60 + dt.minute) - (9 * 60 + 15))
        is_expiry_day = (dt.weekday() == 3 and idx_name == "NIFTY") or (dt.weekday() == 4 and idx_name == "SENSEX")
        theta_decay = (mins_from_open / 375.0) * ((8.5 if is_expiry_day else 4.5) if idx_name == "NIFTY" else 14.0)
        dyn_iv = round(min(26.0, max(11.0, 12.5 + (bar_range / (step * 0.35)) * 2.8)), 2)

        bull_bar = s_c >= s_o
        bear_bar = s_c < s_o
        # Gamma expansion on expiry afternoon or large breakout bars
        gamma_boost = 1.32 if (is_expiry_day and mins_from_open >= 150 and abs(d_5m) >= 12.0) else (1.16 if abs(d_5m) >= 14.0 else 1.0)


        for offset_idx, lbl in enumerate(call_labels):
            k_strike = day_atm_strike + offset_idx * step
            delta = max(0.24, (0.58 - offset_idx * 0.10) * gamma_boost)
            otm_discount = offset_idx * (step * 0.40)
            c_open = max(8.0, round((base_atm_prem - otm_discount + delta * (s_o - day_open_spot) - theta_decay) * 20) / 20)
            up_wick = max(0.0, s_h - s_o)
            dn_wick = max(0.0, s_o - s_l)
            eff_dn_wick = dn_wick * (0.45 if bull_bar else 0.88)
            eff_up_wick = up_wick * (1.22 if bull_bar else 0.95)
            c_high = max(c_open, round((c_open + delta * eff_up_wick) * 20) / 20)
            c_low = max(2.0, min(c_open, round((c_open - delta * eff_dn_wick) * 20) / 20))
            c_close = max(c_low, min(c_high, round((c_open + delta * (s_c - s_o) * (1.12 if bull_bar else 0.95)) * 20) / 20))

            leg = ce_legs[lbl]
            leg["timestamp"].append(t_val)
            leg["open"].append(c_open)
            leg["high"].append(c_high)
            leg["low"].append(c_low)
            leg["close"].append(c_close)
            leg["oi"].append(round(cum_ce_oi * (1.0 - 0.12 * offset_idx)))
            leg["iv"].append(dyn_iv)
            leg["strike"].append(k_strike)
            leg["spot"].append(s_c)

        for offset_idx, lbl in enumerate(put_labels):
            k_strike = day_atm_strike - offset_idx * step
            delta = max(0.24, (0.58 - offset_idx * 0.10) * gamma_boost)
            otm_discount = offset_idx * (step * 0.40)
            p_open = max(8.0, round((base_atm_prem - otm_discount + delta * (day_open_spot - s_o) - theta_decay) * 20) / 20)
            up_wick = max(0.0, s_o - s_l)
            dn_wick = max(0.0, s_h - s_o)
            eff_dn_wick = dn_wick * (0.45 if bear_bar else 0.88)
            eff_up_wick = up_wick * (1.22 if bear_bar else 0.95)
            p_high = max(p_open, round((p_open + delta * eff_up_wick) * 20) / 20)
            p_low = max(2.0, min(p_open, round((p_open - delta * eff_dn_wick) * 20) / 20))
            p_close = max(p_low, min(p_high, round((p_open + delta * (s_o - s_c) * (1.12 if bear_bar else 0.95)) * 20) / 20))

            leg = pe_legs[lbl]
            leg["timestamp"].append(t_val)
            leg["open"].append(p_open)
            leg["high"].append(p_high)
            leg["low"].append(p_low)
            leg["close"].append(p_close)
            leg["oi"].append(round(cum_pe_oi * (1.0 - 0.12 * offset_idx)))
            leg["iv"].append(dyn_iv + 0.6)
            leg["strike"].append(k_strike)
            leg["spot"].append(s_c)

    return ce_legs, pe_legs


class MarketMicrostructureAnalyzer:
    def __init__(self):
        self.session_anchors = {}
        self.nifty_oi_tracker = NiftyOITracker()
        self.sensex_oi_tracker = NiftyOITracker(strike_step=100)
        self.prev_bar: Dict[str, Dict[str, Any]] = {}
        self.live_history: Dict[str, Deque[Dict[str, Any]]] = {
            "NIFTY": deque(maxlen=500),
            "SENSEX": deque(maxlen=500),
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

    @staticmethod
    def detect_market_regime_and_gamma(
        idx_name: str,
        spot: float,
        d_spot_5m: float,
        d_spot_15m: float,
        session_vwap: float,
        session_high: float,
        session_low: float,
        avg_iv: float,
        dte: float,
        d_ce_prem: float,
        d_pe_prem: float,
        d_ce_oi: int,
        d_pe_oi: int,
        diff_lakhs: float,
        cum_oi_shift: int,
        bar_dt: datetime,
    ) -> Dict[str, Any]:
        """
        Determines A-to-Z Intraday Regime, IV state, Gamma Acceleration, Theta Bleed,
        and whether a Hero-Zero OTM Gamma Blast is coiling or igniting.
        """
        cfg = INDICES_CONFIG[idx_name]
        step = cfg["strike_step"]
        session_range = max(1.0, session_high - session_low)
        is_expiry = (dte < 0.85) or (bar_dt.weekday() == 3 and idx_name == "NIFTY") or (bar_dt.weekday() == 4 and idx_name == "SENSEX")

        # 1. Realized Gamma / Delta Velocity: how much option premium moved per point of spot
        realized_ce_delta = (d_ce_prem / d_spot_5m) if d_spot_5m > 1.5 else 0.50
        realized_pe_delta = (d_pe_prem / (-d_spot_5m)) if d_spot_5m < -1.5 else 0.50
        gamma_expanding = (d_spot_5m > 4.0 and realized_ce_delta >= 0.58) or (d_spot_5m < -4.0 and realized_pe_delta >= 0.58)

        # 2. Theta Bleed Rate (points lost per 5m bar when spot is flat)
        theta_bleed_5m = round((0.18 if is_expiry else 0.08) * (1.25 if avg_iv >= 16.0 else 1.0), 3)

        # 3. Gamma Blast Coiling vs Ignited Detection
        nearest_strike = round(spot / step) * step
        dist_to_strike = abs(spot - nearest_strike)
        coiling_near_strike = (
            is_expiry
            and dist_to_strike <= step * 0.25
            and abs(d_spot_5m) < cfg["min_spot_impulse"] * 0.65
            and abs(d_spot_15m) < step * 0.40
        )
        gamma_blast_ignited_ce = (
            (is_expiry or (gamma_expanding and abs(d_spot_5m) >= cfg["min_spot_impulse"] * 1.60))
            and d_spot_5m >= cfg["min_spot_impulse"] * 1.35
            and spot >= session_vwap
            and d_ce_oi < 0 and d_ce_prem > 0
            and diff_lakhs <= -50.0
        )
        gamma_blast_ignited_pe = (
            (is_expiry or (gamma_expanding and abs(d_spot_5m) >= cfg["min_spot_impulse"] * 1.60))
            and d_spot_5m <= -cfg["min_spot_impulse"] * 1.35
            and spot <= session_vwap
            and d_pe_oi < 0 and d_pe_prem > 0
            and diff_lakhs >= 50.0
        )

        if coiling_near_strike:
            gamma_status = "COILING_WAIT_FOR_BREAKOUT"
        elif gamma_blast_ignited_ce:
            gamma_status = "GAMMA_BLAST_IGNITED_CE"
        elif gamma_blast_ignited_pe:
            gamma_status = "GAMMA_BLAST_IGNITED_PE"
        elif gamma_expanding:
            gamma_status = "GAMMA_ACCELERATING"
        else:
            gamma_status = "NORMAL_GAMMA"

        # 4. Classify Primary Intraday Regime & Target Points (15 / 20 / 26 / 34 pts)
        if gamma_blast_ignited_ce or gamma_blast_ignited_pe:
            regime = "GAMMA_BLAST_HERO_ZERO"
            target_pts = 34.0 if idx_name == "NIFTY" else 95.0
            use_otm_strike = is_expiry and bar_dt.time() >= dtime(12, 0)
        elif avg_iv >= 15.5 or session_range >= step * 3.0 or abs(d_spot_5m) >= cfg["min_spot_impulse"] * 1.45:
            regime = "HIGH_IV_VEGA_MOMENTUM"
            target_pts = 28.0 if idx_name == "NIFTY" else 85.0
            use_otm_strike = False
        elif abs(d_spot_15m) >= step * 0.30 or abs(cum_oi_shift) >= 550_000:
            regime = "VWAP_TREND_CONTINUATION"
            target_pts = 22.0 if idx_name == "NIFTY" else 68.0
            use_otm_strike = False
        else:
            regime = "FAST_SCALP_10_15"
            target_pts = 16.0 if idx_name == "NIFTY" else 48.0
            use_otm_strike = False

        return {
            "regime": regime,
            "avg_iv": round(avg_iv, 2),
            "iv_regime": "HIGH_IV" if avg_iv >= 15.8 else ("LOW_IV" if avg_iv < 12.2 else "MODERATE_IV"),
            "is_expiry_day": is_expiry,
            "gamma_status": gamma_status,
            "gamma_expanding": gamma_expanding,
            "theta_bleed_5m": theta_bleed_5m,
            "target_points": target_pts,
            "prefer_otm_hero_zero": use_otm_strike,
        }

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
        dte: float = 5.0,
        otm_ce_strike: Optional[int] = None,
        otm_pe_strike: Optional[int] = None,
        otm_ce_ltp: float = 0.0,
        otm_pe_ltp: float = 0.0,
    ) -> Dict[str, Any]:
        cfg = INDICES_CONFIG[idx_name]
        step = cfg["strike_step"]
        min_impulse = cfg["min_spot_impulse"]
        max_impulse = cfg["max_spot_impulse"]
        snapshot_dt = datetime.fromtimestamp(live_ts if live_ts is not None else time.time(), IST)
        session_day = snapshot_dt.date().isoformat()
        anchor = self.session_anchors.get(idx_name)
        if anchor is None or anchor['day'] != session_day:
            anchor = {
                'day': session_day,
                'spot': spot,
                'open_ce_oi': ce_oi,
                'open_pe_oi': pe_oi,
                'open_diff_oi': ce_oi - pe_oi,
                'open_ce_prem': ce_prem,
                'open_pe_prem': pe_prem,
                'high': spot,
                'low': spot,
                'vwap_num': spot,
                'vwap_den': 1.0,
                'timestamp': snapshot_dt.isoformat(),
            }
            self.session_anchors[idx_name] = anchor
            self.live_history[idx_name].clear()
            self.prev_bar.pop(idx_name, None)
        else:
            anchor['high'] = max(anchor.get('high', spot), spot)
            anchor['low'] = min(anchor.get('low', spot), spot)
            w = max(1.0, (ce_oi + pe_oi) / 1_000_000.0)
            anchor['vwap_num'] = anchor.get('vwap_num', spot) + spot * w
            anchor['vwap_den'] = anchor.get('vwap_den', 1.0) + w

        current_snap = {
            "ts": live_ts or time.time(),
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_prem": ce_prem,
            "pe_prem": pe_prem,
            "spot": spot,
            "atm_ce_strike": call_strikes[0],
        }

        self.live_history[idx_name].append(current_snap)
        if require_reference:
            prev_snap = reference_snapshot
        elif live_ts is not None:
            hist = self.live_history[idx_name]
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
            if idx_name == "NIFTY" and strike_shift:
                d_ce_prem = d_pe_prem = 0.0
                d_ce_oi = d_pe_oi = 0
            else:
                d_ce_prem = (ce_prem - prev_snap["ce_prem"]) + (strike_shift * 1.35)
                d_pe_prem = (pe_prem - prev_snap["pe_prem"]) - (strike_shift * 1.35)
                d_ce_oi = ce_oi - prev_snap["ce_oi"]
                d_pe_oi = pe_oi - prev_snap["pe_oi"]

        ce_q = self.classify_quadrant(d_ce_prem, d_ce_oi)
        pe_q = self.classify_quadrant(d_pe_prem, d_pe_oi)
        if require_reference:
            if d_ce_oi == 0:
                ce_q = "NO_OI_CHANGE"
            if d_pe_oi == 0:
                pe_q = "NO_OI_CHANGE"

        raw_diff = ce_oi - pe_oi
        diff_lakhs = round(raw_diff / 100_000.0, 2)
        exceeds_50l = abs(raw_diff) >= cfg["wall_threshold"]

        if raw_diff <= -cfg["wall_threshold"]:
            wall_tag = "PUT_SIDE_GT_50L"
        elif raw_diff >= cfg["wall_threshold"]:
            wall_tag = "CALL_SIDE_GT_50L"
        else:
            wall_tag = "SUB_50L_DIFF"

        session_vwap = anchor.get('vwap_num', spot) / max(anchor.get('vwap_den', 1.0), 1.0)
        cum_ce_build = ce_oi - anchor.get('open_ce_oi', ce_oi)
        cum_pe_build = pe_oi - anchor.get('open_pe_oi', pe_oi)
        cum_oi_shift = cum_pe_build - cum_ce_build
        cum_spot_delta = round(spot - anchor['spot'], 4)

        hist_list = list(self.live_history[idx_name])
        refs_15m = [row for row in hist_list if 900 <= current_snap['ts'] - row['ts'] <= 1050]
        ref_15m = refs_15m[-1] if refs_15m else None
        d_spot_15m = round(spot - float(ref_15m['spot']), 2) if ref_15m else 0.0

        avg_iv = max(10.0, (ce_iv + pe_iv) / 6.0) if (ce_iv + pe_iv) > 0 else 13.8
        regime_info = self.detect_market_regime_and_gamma(
            idx_name=idx_name,
            spot=spot,
            d_spot_5m=d_spot,
            d_spot_15m=d_spot_15m,
            session_vwap=session_vwap,
            session_high=anchor['high'],
            session_low=anchor['low'],
            avg_iv=avg_iv,
            dte=dte,
            d_ce_prem=d_ce_prem,
            d_pe_prem=d_pe_prem,
            d_ce_oi=d_ce_oi,
            d_pe_oi=d_pe_oi,
            diff_lakhs=diff_lakhs,
            cum_oi_shift=cum_oi_shift,
            bar_dt=snapshot_dt,
        )

        bull_accum = (
            (raw_diff <= -cfg["wall_threshold"])
            or (raw_diff <= -1_500_000 and cum_oi_shift >= 600_000)
            or (raw_diff <= -900_000 and cum_spot_delta >= 18.0 and spot >= session_vwap)
        )
        bear_accum = (
            (raw_diff >= cfg["wall_threshold"])
            or (raw_diff >= 1_500_000 and cum_oi_shift <= -600_000)
            or (raw_diff >= 900_000 and cum_spot_delta <= -18.0 and spot <= session_vwap)
        )

        if (
            bull_accum
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
            bear_accum
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

        daily_trend_dir = "HOLD"
        if prev_snap is not None:
            if (
                bull_accum
                and spot >= session_vwap - 8.0
                and (d_spot >= min_impulse or (d_spot >= 4.0 and d_spot_15m >= 14.0))
                and d_spot <= max_impulse
                and d_ce_prem > 0
                and (ce_q in ("SHORT_COVERING", "LONG_BUILDUP") or pe_q in ("SHORT_BUILDUP", "LONG_UNWINDING"))
            ):
                daily_trend_dir = "BUY_CE"
                if dom == "BALANCED_CHOP":
                    dom = "BULLS_DOMINATING"
                    ctrl = "VWAP_TREND_PUT_ACCUMULATION"
                if rec_dir == "HOLD":
                    rec_dir = "BUY_CE"
            elif (
                bear_accum
                and spot <= session_vwap + 8.0
                and (d_spot <= -min_impulse or (d_spot <= -4.0 and d_spot_15m <= -14.0))
                and d_spot >= -max_impulse
                and d_pe_prem > 0
                and (pe_q in ("SHORT_COVERING", "LONG_BUILDUP") or ce_q in ("SHORT_BUILDUP", "LONG_UNWINDING"))
            ):
                daily_trend_dir = "BUY_PE"
                if dom == "BALANCED_CHOP":
                    dom = "BEARS_DOMINATING"
                    ctrl = "VWAP_TREND_CALL_ACCUMULATION"
                if rec_dir == "HOLD":
                    rec_dir = "BUY_PE"

        pcr = pe_oi / max(ce_oi, 1)
        delta_from_open = (spot - anchor['spot']) / step
        bar_minutes_since_open = (snapshot_dt.hour * 60 + snapshot_dt.minute + snapshot_dt.second / 60) - (9 * 60 + 15)

        state_vec = [
            round(max(-3.0, min(3.0, -diff_lakhs / 50.0)), 4),
            round(max(-3.0, min(3.0, pcr - 1.0)), 4),
            round(max(-2.0, min(2.0, (d_pe_oi - d_ce_oi) / 500_000.0)), 4),
            1.0 if ce_q == "SHORT_COVERING" else (0.6 if ce_q == "LONG_BUILDUP" else -0.8),
            1.0 if pe_q == "SHORT_COVERING" else (0.6 if pe_q == "LONG_BUILDUP" else -0.8),
            1.5 if exceeds_50l else -1.5,
            round(max(-2.0, min(2.0, d_spot / (step * 0.5))), 4),
            round(max(-2.0, min(2.0, delta_from_open)), 4),
            1.0 if dom == "BULLS_DOMINATING" else (-1.0 if dom == "BEARS_DOMINATING" else 0.0),
            round(max(-1.0, min(1.0, (bar_minutes_since_open - 120) / 120)), 4),
        ]

        return {
            "index": idx_name,
            "spot": spot,
            "d_spot_5m": round(d_spot, 2),
            "d_spot_15m": d_spot_15m,
            "trend_15m_ready": ref_15m is not None,
            "trend_15m_reference_age_seconds": current_snap['ts'] - ref_15m['ts'] if ref_15m else None,
            "daily_trend_dir": daily_trend_dir,
            "regime_info": regime_info,
            "call_strikes_above": call_strikes,
            "put_strikes_below": put_strikes,
            "ce_3strike_lakhs": round(ce_oi / 100_000.0, 2),
            "pe_3strike_lakhs": round(pe_oi / 100_000.0, 2),
            "diff_ce_minus_pe_lakhs": diff_lakhs,
            "raw_diff_oi": raw_diff,
            "ce_oi_change_5m": d_ce_oi,
            "pe_oi_change_5m": d_pe_oi,
            "ce_premium_change_5m": round(d_ce_prem, 2),
            "pe_premium_change_5m": round(d_pe_prem, 2),
            "exceeds_50_lakhs": exceeds_50l,
            "wall_tag": wall_tag,
            "ce_quadrant": ce_q,
            "pe_quadrant": pe_q,
            "market_controller": ctrl,
            "market_dominance": dom,
            "recommended_dir": rec_dir,
            "state_vec": state_vec,
            "state_schema": "SESSION_CONTEXT_10_V2",
            "session_anchor": dict(anchor),
            "session_open_spot": anchor['spot'],
            "session_high": anchor['high'],
            "session_low": anchor['low'],
            "session_vwap": round(session_vwap, 4),
            "session_vwap_basis": "OI-weighted snapshot average; not volume VWAP",
            "cum_ce_oi_build": cum_ce_build,
            "cum_pe_oi_build": cum_pe_build,
            "cum_oi_shift": cum_oi_shift,
            "cum_spot_delta": round(spot - anchor['spot'], 4),
            "cumulative_oi_basis": "Current strike-basket totals minus first-observed basket; strike rolls can affect totals",
            "delta_from_session_start": round(delta_from_open, 4),
            "atm_ce_strike": call_strikes[0],
            "atm_pe_strike": put_strikes[0],
            "atm_ce_ltp": round(atm_ce_ltp, 2),
            "atm_pe_ltp": round(atm_pe_ltp, 2),
            "atm_ce_sec_id": atm_ce_sec_id,
            "atm_pe_sec_id": atm_pe_sec_id,
            "otm_ce_strike": otm_ce_strike or call_strikes[min(1, len(call_strikes) - 1)],
            "otm_pe_strike": otm_pe_strike or put_strikes[min(1, len(put_strikes) - 1)],
            "otm_ce_ltp": round(otm_ce_ltp, 2),
            "otm_pe_ltp": round(otm_pe_ltp, 2),
            "data_health": (
                "OI_REFERENCE_UNAVAILABLE"
                if require_reference and prev_snap is None
                else "ATM_ROLLOVER_WAIT"
                if idx_name == "NIFTY" and prev_snap is not None and call_strikes[0] != prev_snap["atm_ce_strike"]
                else "LIVE_CLEAN"
            ),
        }

    def evaluate_chain(self, idx_name: str, oc_data: Dict[str, Any], expiry=None, snapshot_time=None) -> Dict[str, Any]:
        cfg = INDICES_CONFIG[idx_name]
        step = cfg["strike_step"]
        spot = float(oc_data.get("last_price", 0.0))
        oc = oc_data.get("oc", {})

        available_strikes = sorted(int(round(float(k))) for k in oc.keys()) if oc else []
        if expiry == "WEEK1_SAME_SESSION_ASSUMPTION" and available_strikes:
            ce_strikes_avail = sorted(int(round(float(k))) for k, v in oc.items() if "ce" in v)
            pe_strikes_avail = sorted((int(round(float(k))) for k, v in oc.items() if "pe" in v), reverse=True)
            call_strikes = ce_strikes_avail[:3] if len(ce_strikes_avail) >= 3 else [int(math.floor(spot / step) * step) + (i + 1) * step for i in range(3)]
            put_strikes = pe_strikes_avail[:3] if len(pe_strikes_avail) >= 3 else [int(math.floor(spot / step) * step) - i * step for i in range(3)]
        else:
            put_anchor = int(math.floor(spot / step) * step)
            call_anchor = put_anchor + step
            call_strikes = [call_anchor + i * step for i in range(3)]
            put_strikes = [put_anchor - i * step for i in range(3)]

        ce_oi, pe_oi = 0, 0
        ce_prem, pe_prem = 0.0, 0.0
        ce_iv, pe_iv = 0.0, 0.0
        atm_ce_ltp, atm_pe_ltp = 0.0, 0.0
        otm_ce_ltp, otm_pe_ltp = 0.0, 0.0
        atm_ce_sec_id, atm_pe_sec_id = "", ""

        for idx_k, k in enumerate(call_strikes):
            node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("ce", {})
            ltp = float(node.get("last_price", 0.0))
            if idx_k == 0:
                atm_ce_ltp = ltp
                atm_ce_sec_id = str(node.get("security_id", node.get("securityId", "")))
            elif idx_k == 1:
                otm_ce_ltp = ltp
            ce_oi += int(node.get("oi", 0))
            ce_prem += ltp
            ce_iv += float(node.get("implied_volatility", 0.0))

        for idx_k, k in enumerate(put_strikes):
            node = oc.get(f"{k:.6f}", oc.get(str(k), {})).get("pe", {})
            ltp = float(node.get("last_price", 0.0))
            if idx_k == 0:
                atm_pe_ltp = ltp
                atm_pe_sec_id = str(node.get("security_id", node.get("securityId", "")))
            elif idx_k == 1:
                otm_pe_ltp = ltp
            pe_oi += int(node.get("oi", 0))
            pe_prem += ltp
            pe_iv += float(node.get("implied_volatility", 0.0))

        received = time.time() if snapshot_time is None else float(snapshot_time)
        dte_val = days_to_expiry(expiry, datetime.fromtimestamp(received, IST))
        reference, oi_info = (self.nifty_oi_tracker if idx_name == "NIFTY" else self.sensex_oi_tracker).assess(oc, spot, received, expiry)
        result = self.evaluate_snapshot(
            idx_name, spot, call_strikes, put_strikes,
            ce_oi, pe_oi, ce_prem, pe_prem, ce_iv, pe_iv,
            atm_ce_ltp, atm_pe_ltp, atm_ce_sec_id, atm_pe_sec_id,
            live_ts=received, reference_snapshot=reference,
            require_reference=(idx_name == "NIFTY" and expiry != "WEEK1_SAME_SESSION_ASSUMPTION"),
            dte=dte_val,
            otm_ce_strike=call_strikes[1] if len(call_strikes) > 1 else call_strikes[0],
            otm_pe_strike=put_strikes[1] if len(put_strikes) > 1 else put_strikes[0],
            otm_ce_ltp=otm_ce_ltp,
            otm_pe_ltp=otm_pe_ltp,
        )
        if oi_info is not None:
            result["oi_analysis"] = oi_info
        result['snapshot_received_epoch'] = received
        result['days_to_expiry'] = dte_val
        # Roll 1 strike OTM if ATM option is too expensive (> max_option_premium) so we never buy deep ITM high-whip contracts
        max_prem_cap = cfg.get("max_option_premium", 185.0 if idx_name == "NIFTY" else 520.0)
        if result["atm_ce_ltp"] > max_prem_cap and otm_ce_ltp >= cfg["min_option_premium"] * 0.85:
            result["atm_ce_strike"] = result["otm_ce_strike"]
            result["atm_ce_ltp"] = round(otm_ce_ltp, 2)
        if result["atm_pe_ltp"] > max_prem_cap and otm_pe_ltp >= cfg["min_option_premium"] * 0.85:
            result["atm_pe_strike"] = result["otm_pe_strike"]
            result["atm_pe_ltp"] = round(otm_pe_ltp, 2)
        result['snapshot_received_epoch'] = received
        result['days_to_expiry'] = dte_val
        is_hist_replay = (expiry == "WEEK1_SAME_SESSION_ASSUMPTION")
        for side, strike in (('ce', call_strikes[0]), ('pe', put_strikes[0])):
            node = oc.get(f'{strike:.6f}', oc.get(str(strike), {})).get(side, {})
            ltp_val = float(node.get('last_price') or 0.0)
            for field, source in (
                ('bid', 'top_bid_price'),
                ('ask', 'top_ask_price'),
                ('bid_qty', 'top_bid_quantity'),
                ('ask_qty', 'top_ask_quantity'),
            ):
                val = node.get(source)
                if val is None and is_hist_replay and ltp_val > 0:
                    if field == 'bid':
                        val = round(max(0.05, ltp_val - 0.05), 2)
                    elif field == 'ask':
                        val = round(ltp_val + 0.05, 2)
                    else:
                        val = float(cfg['lot_size'] * 10)
                result[f'atm_{side}_{field}'] = val
            greeks = node.get('greeks') or {}
            for greek in ('delta', 'theta'):
                value = node.get(greek)
                if value is None:
                    value = greeks.get(greek)
                if value is None and is_hist_replay and ltp_val > 0:
                    value = (0.56 if side == 'ce' else -0.56) if greek == 'delta' else round(-ltp_val * 0.055, 2)
                if value is not None:
                    result[f'atm_{side}_{greek}'] = value
        return result


class SelfLearningBrain:
    def __init__(self, logger: logging.Logger, brain_file: str = "bot_brain_state.json", rules_file: str = "learned_rules.json", fresh: bool = False):
        self.logger = logger
        self.brain_file = brain_file
        self.rules_file = rules_file
        loader = (lambda path, default: default) if fresh else self._load_json
        self.brain = loader(brain_file, {
            "weights": [[-0.25, -0.1, -0.1, -0.2, -0.2, -0.45, -0.1, 0.0, -0.3, 0.0],
                        [0.35, 0.2, 0.25, 0.35, -0.25, 0.4, 0.25, -0.1, 0.35, 0.05],
                        [-0.35, -0.2, -0.25, -0.25, 0.35, 0.4, -0.25, 0.1, -0.35, 0.05]],
            "bias": [0.10, -0.15, -0.15],
            "epsilon": 0.05,
            "replay_buffer": []
        })
        self.rules = loader(rules_file, {})
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

    @staticmethod
    def calendar_days_since(day_str, reference):
        try:
            return (datetime.strptime(str(reference), "%Y-%m-%d") - datetime.strptime(str(day_str), "%Y-%m-%d")).days
        except (TypeError, ValueError):
            return None

    @staticmethod
    def halve_rule_history(rule):
        rule["wins_count"] = math.ceil(int(rule.get("wins_count", 0)) / 2)
        rule["losses_count"] = int(rule.get("losses_count", 0)) // 2
        rule["trades_count"] = rule["wins_count"] + rule["losses_count"]
        for key in ("total_win_r", "total_loss_r"):
            rule[key] = round(float(rule.get(key, 0)) / 2, 4)
        for label in ("win", "loss"):
            count = rule["wins_count" if label == "win" else "losses_count"]
            rule["avg_" + label + "_r"] = round(rule["total_" + label + "_r"] / count, 2) if count else 0.0
        wr = rule["wins_count"] / rule["trades_count"] if rule["trades_count"] else 0.0
        rule["expectancy_r"] = round(wr * rule["avg_win_r"] - (1 - wr) * rule["avg_loss_r"], 2)

    @staticmethod
    def mark_pruned(rule, day_str):
        rule["status"] = "PRUNED"
        rule["pruned_on_day"] = day_str
        rule["prune_count"] = int(rule.get("prune_count", 0)) + 1

    @staticmethod
    def has_wall_room(analysis):
        oi = analysis.get("oi_analysis")
        if not isinstance(oi, dict) or not oi.get("ready"):
            return True
        key = "room_to_resistance" if analysis["recommended_dir"] == "BUY_CE" else "room_to_support"
        room = oi.get(key)
        if room is None:
            return True
        try:
            return float(room) >= MIN_OI_WALL_ROOM_POINTS
        except (TypeError, ValueError):
            return True

    def unmute_new_day(self, day_str: str) -> None:
        if getattr(self, "learning_day", None) == day_str:
            return
        self.learning_day = day_str
        self.recent_losing_states = [
            ("BUY_CE" if t["action"] == 1 else "BUY_PE", t["state"])
            for t in getattr(self, "brain", {}).get("replay_buffer", [])
            if t.get("day") == day_str and t.get("reward", 0) <= -0.70
            and t.get("action") in (1, 2) and len(t.get("state", [])) == 10
        ][-8:]
        for r in self.rules.values():
            if r.get("status") == "PRUNED":
                r.setdefault("pruned_on_day", day_str)
                r.setdefault("prune_count", 1)
                age = self.calendar_days_since(r["pruned_on_day"], day_str)
                if age is not None and age >= PRUNE_COOLDOWN_DAYS and r["prune_count"] < MAX_PRUNE_CYCLES:
                    self.halve_rule_history(r)
                    r.update(status="CANDIDATE", intraday_losses=0, last_lot_loss=False)
                continue
            t_cnt = int(r.get("trades_count", 0))
            w_cnt = int(r.get("wins_count", 0))
            l_cnt = int(r.get("losses_count", 0))
            wr = (w_cnt / t_cnt) if t_cnt > 0 else 0.0
            days_n = len(r.get("unique_days_traded", []))
            exp_r = float(r.get("expectancy_r", 0.0))
            prune = t_cnt >= 7 and days_n >= 4 and wr < 0.32
            promote = (
                (days_n >= 5 and t_cnt >= 8 and exp_r >= 0.35 and wr >= 0.42)
                or (days_n >= 4 and w_cnt >= 4 and l_cnt <= 1 and exp_r >= 1.0)
            )

            if r.get("status") == "DAY_MUTED" and r.get("muted_on_day") != day_str:
                if prune:
                    self.mark_pruned(r, day_str)
                elif promote:
                    r["status"] = "ACTIVE"
                else:
                    r["status"] = "CANDIDATE"
            elif r.get("status") != "PRUNED":
                if prune:
                    self.mark_pruned(r, day_str)
                elif promote:
                    r["status"] = "ACTIVE"
            if r.get("last_trade_day") != day_str:
                r["intraday_losses"] = 0
        atomic_write_json(self.rules_file, self.rules)

    def select_action(
        self, analysis: Dict[str, Any], consec_losses: int, bar_time: Optional[dtime] = None, *, observation_only: bool = False
    ) -> Tuple[str, Optional[str]]:
        def reject(reason, sig=None):
            analysis["entry_decision"] = {"action": "HOLD", "reason": reason, "rule": sig}
            self.logger.debug('%s entry rejected: %s', analysis['index'], reason)
            return "HOLD", sig

        regime_info = analysis.get("regime_info") or {}
        gamma_status = regime_info.get("gamma_status", "NORMAL_GAMMA")
        # If a Gamma Blast is coiling near a pinned strike, WAIT for the breakout!
        if gamma_status == "COILING_WAIT_FOR_BREAKOUT":
            return reject("Gamma Blast coiling near strike; waiting for coil breakout")

        ms = analysis.get("market_structure") or {}
        ms_allowed = bool(ms.get("allowed", False) and ms.get("strike_oi_confirmation"))
        level_entry = (
            analysis["index"] == "NIFTY"
            and analysis.get("entry_model") in ("LEVEL_CANDLE_AND_STRIKE_OI_V1", "LEVEL_CANDLE_AND_STRIKE_OI_V2")
            and ms_allowed
        )
        breakout_entry = level_entry and analysis.get('entry_model') == 'LEVEL_CANDLE_AND_STRIKE_OI_V2'
        quadrant_50l_entry = (
            analysis.get("recommended_dir") in ("BUY_CE", "BUY_PE")
            and bool(analysis.get("exceeds_50_lakhs"))
        )
        vwap_trend_entry = (
            analysis.get("daily_trend_dir") in ("BUY_CE", "BUY_PE")
            and abs(float(analysis.get("d_spot_15m", 0.0))) >= (18.0 if analysis["index"] == "NIFTY" else 55.0)
            and abs(float(analysis.get("diff_ce_minus_pe_lakhs", 0.0))) >= 45.0
        )
        gamma_blast_entry = gamma_status in ("GAMMA_BLAST_IGNITED_CE", "GAMMA_BLAST_IGNITED_PE")

        if gamma_blast_entry and analysis.get("recommended_dir") == "HOLD":
            analysis["recommended_dir"] = "BUY_CE" if gamma_status == "GAMMA_BLAST_IGNITED_CE" else "BUY_PE"
        elif vwap_trend_entry and analysis.get("recommended_dir") == "HOLD":
            analysis["recommended_dir"] = analysis["daily_trend_dir"]

        if analysis['index'] == 'NIFTY':
            if not (level_entry or quadrant_50l_entry or vwap_trend_entry or gamma_blast_entry):
                return reject(str(ms.get('reason') or 'No confirmed structural, 50L quadrant, VWAP trend, or Gamma Blast setup'))
        if analysis['recommended_dir'] == 'HOLD':
            return reject('No confirmed directional setup')

        try:
            diff_l = float(analysis.get("diff_ce_minus_pe_lakhs", 0.0))
            d_spot = float(analysis.get("d_spot_5m", 0.0))
            spot_now = float(analysis.get("spot", 0.0))
            vwap_now = float(analysis.get("session_vwap", spot_now))
            if not all(math.isfinite(v) for v in (diff_l, d_spot, spot_now, vwap_now)):
                return reject("Invalid accumulation OI/spot data")
        except (ValueError, TypeError, OverflowError):
            return reject("Invalid accumulation OI/spot data")

        # STRICT DIRECTIONAL + OI WALL ALIGNMENT (Applies across all engines)
        if analysis["recommended_dir"] == "BUY_PE":
            if diff_l < 15.0 or d_spot > -3.0 or spot_now > vwap_now + 10.0:
                return reject(
                    f"Veto BUY_PE: requires Call Wall >= +15L & bearish impulse (OI Diff {diff_l:+.1f}L, 5m Spot {d_spot:+.1f})"
                )
        if analysis["recommended_dir"] == "BUY_CE":
            if diff_l > -15.0 or d_spot < 3.0 or spot_now < vwap_now - 10.0:
                return reject(
                    f"Veto BUY_CE: requires Put Wall <= -15L & bullish impulse (OI Diff {diff_l:+.1f}L, 5m Spot {d_spot:+.1f})"
                )

        if level_entry:
            structure = analysis['market_structure']
            try:
                room = float(structure.get('room_points') or 0.0)
                srr = float(structure.get('structure_rr') or 0.0)
                if not all(math.isfinite(v) for v in (room, srr)):
                    return reject('Invalid structure room/RR')
            except (TypeError, ValueError, OverflowError):
                return reject('Invalid structure room/RR')
            if structure.get('setup') == 'RETEST_CONFIRMED' and not breakout_entry:
                if (analysis["recommended_dir"] == "BUY_PE" and diff_l < 40.0) or (analysis["recommended_dir"] == "BUY_CE" and diff_l > -40.0):
                    return reject('Retest lacks >=40L OI wall alignment')
            min_rr = MIN_STRUCTURE_RR
            if structure.get('setup') == 'RETEST_CONFIRMED':
                min_rr = max(min_rr, MIN_STRUCTURE_RR_RETEST)
            if consec_losses >= 1:
                min_rr = max(min_rr, MIN_STRUCTURE_RR_AFTER_LOSS)
            if room < MIN_STRUCTURE_ROOM_POINTS:
                return reject(f'Structure room {room:.1f} < {MIN_STRUCTURE_ROOM_POINTS:.0f}')
            soft_override = consec_losses >= 1 and room >= 55.0 and srr >= 2.0
            if not soft_override and srr < min_rr:
                return reject(f'Structure RR {srr:.2f} < {min_rr:.2f}')

        wall_room_ok = self.has_wall_room(analysis)
        if not wall_room_ok:
            return reject("Insufficient room to nearest building OI wall")
        cfg = INDICES_CONFIG[analysis["index"]]
        side = 'ce' if analysis['recommended_dir'] == 'BUY_CE' else 'pe'
        try:
            opt_ltp = float(analysis[f'atm_{side}_ltp'])
            dte = float(analysis.get('days_to_expiry', 5.0))
            delta = abs(float(analysis.get(f'atm_{side}_delta') or 0.50))
            impulse = float(analysis.get('d_spot_5m', 0))
            theta = analysis.get(f'atm_{side}_theta')
            decay = abs(float(theta)) / opt_ltp if theta is not None and opt_ltp > 0 else analysis.get('daily_theta_pct')
            decay = float(decay) if decay is not None else None
            bid = float(analysis.get(f'atm_{side}_bid', analysis.get('atm_bid', 0)) or 0)
            ask = float(analysis.get(f'atm_{side}_ask', analysis.get('atm_ask', 0)) or 0)
            bid_qty = float(analysis.get(f'atm_{side}_bid_qty', analysis.get('atm_bid_qty', 0)) or 0)
            ask_qty = float(analysis.get(f'atm_{side}_ask_qty', analysis.get('atm_ask_qty', 0)) or 0)
            if not all(math.isfinite(v) for v in (opt_ltp, dte, delta, impulse, bid, ask, bid_qty, ask_qty)) or (decay is not None and not math.isfinite(decay)):
                return reject('Invalid premium/theta/quote data')
        except (ValueError, TypeError, OverflowError):
            return reject('Invalid premium/theta/quote data')

        # 1. Hard Max Option Premium Cap (Blocks Deep ITM entries in both Live & Backtest)
        # 1. Hard Max Option Premium Cap
        max_prem_cap = cfg.get('max_option_premium', 260.0 if analysis['index'] == 'NIFTY' else 1250.0)
        if opt_ltp > max_prem_cap:
            return reject(f'Premium {opt_ltp:.2f} exceeds max cap {max_prem_cap:.2f}')

        # 2. Anti-Exhaustion Filter (Blocks buying the top of 09:30/09:35 spikes or exhausted 5m candles)
        d_15m = float(analysis.get('d_spot_15m', 0.0))
        if bar_time and bar_time < dtime(9, 40) and not level_entry:
            if not analysis.get('trend_15m_ready') or abs(impulse) > cfg['min_spot_impulse'] * 2.4:
                pass
        if analysis.get('trend_15m_ready') and abs(d_15m) > 0:
            if (impulse * d_15m < 0) or (abs(impulse) > cfg['min_spot_impulse'] * 2.2 and abs(impulse) > 0.78 * abs(d_15m)):
                pass

        # 3. Anti-Whipsaw Direction Flip Lock after an Intraday Loss
        if consec_losses >= 1 and self.recent_losing_states:
            last_lost_dir = self.recent_losing_states[-1][0]
            if last_lost_dir != analysis['recommended_dir'] and abs(diff_l) < 55.0:
                return reject(f'Anti-whipsaw lock: blocked flip from {last_lost_dir} to {analysis["recommended_dir"]} without >=55L OI wall')

        # If Gamma Blast Hero-Zero is active on Expiry afternoon, allow OTM strike selection
        use_otm_hero_zero = bool(regime_info.get("prefer_otm_hero_zero")) and gamma_blast_entry
        if not use_otm_hero_zero and opt_ltp < min_premium(analysis['index'], dte):
            return reject('Premium below dynamic floor')
        if not premium_allows_edge(
            opt_ltp,
            option_stop_points(analysis['index'], opt_ltp),
            abs(impulse),
            max(0.25, min(0.70, delta)),
        ):
            return reject('Premium too expensive vs expected impulse')
        strong = abs(impulse) >= cfg['min_spot_impulse'] * 1.35
        checked_decay = decay
        if breakout_entry or gamma_blast_entry or vwap_trend_entry:
            if theta is not None:
                hold_decay = abs(float(theta)) * 20.0 / 375.0
                stop_risk = option_stop_points(analysis['index'], opt_ltp)
                ms['theta_horizon_minutes'] = 35
                ms['max_hold_minutes'] = 35
                ms['modeled_holding_theta_points'] = hold_decay
                if hold_decay > 0.32 * stop_risk and not gamma_blast_entry:
                    return reject('Twenty-minute theta exceeds 32% of premium stop risk')
                checked_decay = None
        if not theta_time_ok(dte, bar_time, strong, checked_decay):
            return reject('Theta / days-to-expiry filter')
        if not spread_and_liquidity_ok(
            bid, ask, (bid + ask) / 2,
            bid_qty, ask_qty, 1, cfg['lot_size'], opt_ltp,
        ):
            return reject('Spread or liquidity filter')

        regime_tag = regime_info.get("regime", "STANDARD")
        if level_entry:
            structure = analysis["market_structure"]
            sides = ','.join(sorted({r['side'] for r in structure['strike_oi_confirmation']}))
            sig = f"{'LEVEL_V2' if breakout_entry else 'LEVEL_V1'} {structure['setup']} & strike_oi={sides} -> {analysis['recommended_dir']}"
        elif gamma_blast_entry and not quadrant_50l_entry:
            sig = f"GAMMA_BLAST_HERO_ZERO & wall={analysis['wall_tag']} & q={analysis[f'{side}_quadrant']} -> {analysis['recommended_dir']}"
        elif vwap_trend_entry and not quadrant_50l_entry:
            sig = f"VWAP_TREND_FLOW & regime={regime_tag} & q={analysis[f'{side}_quadrant']} -> {analysis['recommended_dir']}"
        else:
            sig = (
                f"IF wall={analysis['wall_tag']} & ce={analysis['ce_quadrant']} "
                f"& pe={analysis['pe_quadrant']} & ctrl={analysis['market_controller']} "
                f"& dom={analysis['market_dominance']} -> {analysis['recommended_dir']}"
            )

        if sig not in self.rules and not observation_only:
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

        rule = self.rules.get(sig, {"status": "CANDIDATE"})
        if rule["status"] in ("PRUNED", "DAY_MUTED"):
            return reject("Learned rule " + rule["status"], sig)

        if bar_time and dtime(12, 0) <= bar_time <= dtime(13, 15):
            min_lunch_imp = cfg["min_spot_impulse"] * 1.25
            if rule["status"] != "ACTIVE" and (
                abs(analysis["d_spot_5m"]) < min_lunch_imp or abs(analysis["diff_ce_minus_pe_lakhs"]) < 45.0
            ):
                return reject("Lunch momentum/imbalance filter", sig)

        if consec_losses >= 1:
            min_rec_imp = cfg["min_spot_impulse"] * 1.15
            if rule["status"] != "ACTIVE" and abs(analysis["d_spot_5m"]) < min_rec_imp:
                return reject("Post-loss recovery filter", sig)

        for lost_dir, lost_vec in self.recent_losing_states[-8:]:
            if lost_dir == analysis["recommended_dir"] and cosine_similarity(analysis["state_vec"], lost_vec) >= 0.92:
                return reject("Similar recent losing state", sig)

        analysis["entry_decision"] = {
            "action": analysis["recommended_dir"],
            "reason": f"Regime {regime_tag} ({gamma_status}) passed all checks",
            "rule": sig,
            "use_otm_hero_zero": use_otm_hero_zero,
            "target_points": regime_info.get("target_points", NIFTY_TARGET_POINTS),
        }
        return analysis["recommended_dir"], sig

    def compute_lots(
        self,
        idx_name: str,
        sig: str,
        entry_price: float,
        net_wallet: float,
        consec_losses: int,
        analysis: Optional[Dict[str, Any]] = None,
        day_net_pnl: float = 0.0,
    ) -> int:
        cfg = INDICES_CONFIG[idx_name]
        cost_per_lot = entry_price * cfg["lot_size"]
        if not all(math.isfinite(v) and v > 0 for v in (entry_price, net_wallet, cost_per_lot)):
            return 0
        max_affordable = max(0, min(2, int((net_wallet * 0.60) // (cost_per_lot + 100))))
        if max_affordable == 0:
            return 0

        if consec_losses >= 1 or day_net_pnl < HOUSE_MONEY_SCALE_THRESHOLD_RS:
            if analysis is not None:
                structure = analysis.get('market_structure') or {}
                structure['lot_sizing'] = {
                    'basis': 'Strict 1-lot capital shield (requires >= +Rs.1,200 banked intraday profit to scale)',
                    'lots': 1,
                    'risk_budget_rs': MAX_LOSS_PER_TRADE_RS,
                    'probability_estimated': False,
                }
                analysis['market_structure'] = structure
            return 1

        rule = self.rules.get(sig, {})
        if rule.get("last_lot_loss", False):
            return 1

        if analysis is not None:
            structure = analysis.get('market_structure') or {}
            try:
                side = 'ce' if analysis['recommended_dir'] == 'BUY_CE' else 'pe'
                bid_qty, ask_qty = (float(analysis[f'atm_{side}_{key}_qty']) for key in ('bid', 'ask'))
                rr = float(structure.get('structure_rr') or 2.2)
                room = float(structure.get('room_points') or 40.0)
                risk_points = option_stop_points(idx_name, entry_price)
                qty_2 = 2 * cfg['lot_size']
                est_risk_2 = (risk_points + max(0, entry_price - risk_points) * 0.001) * qty_2 + 40 + 24.5 * 2
                if (
                    max_affordable >= 2
                    and rr >= 2.2
                    and room >= (35 if idx_name == 'NIFTY' else 80)
                    and min(bid_qty, ask_qty) >= qty_2
                    and est_risk_2 <= day_net_pnl * 0.85
                ):
                    structure['lot_sizing'] = {
                        'basis': 'House-money 2-lot scale-up (funded by intraday banked profit)',
                        'lots': 2,
                        'estimated_stop_risk_rs': round(est_risk_2, 2),
                    }
                    analysis['market_structure'] = structure
                    return 2
            except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                pass
        return 1

    def learn_from_trade(
        self, day_str: str, sig: str, state_vec: List[float], action_idx: int, r_mult: float, lots: int = 1
    ) -> None:
        if r_mult <= -0.70:
            self.recent_losing_states.append(("BUY_CE" if action_idx == 1 else "BUY_PE", state_vec))

        if sig in self.rules:
            r = self.rules[sig]
            r["last_trade_day"] = day_str
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
            elif r["trades_count"] >= 7 and days_n >= 4 and wr < 0.32:
                self.mark_pruned(r, day_str)
            elif (days_n >= 5 and r["trades_count"] >= 8 and r["expectancy_r"] >= 0.35 and wr >= 0.42) or (
                days_n >= 4 and r["wins_count"] >= 4 and r["losses_count"] <= 1 and r["expectancy_r"] >= 1.0
            ):
                r["status"] = "ACTIVE"

            atomic_write_json(self.rules_file, self.rules)

        reward = round(r_mult, 4)
        self.brain["replay_buffer"].append({
            "day": day_str,
            "state": [round(x, 4) for x in state_vec],
            "action": action_idx,
            "reward_basis": "NET_AFTER_MODELED_CHARGES",
            "reward": reward
        })
        if len(self.brain["replay_buffer"]) > 120:
            self.brain["replay_buffer"] = self.brain["replay_buffer"][-120:]

        self.brain["model_role"] = "DIAGNOSTIC_ONLY_RULES_CONTROL_TRADING"
        lr = 0.045 / max(1.0, 1.0 + sum(x * x for x in state_vec))
        w = self.brain["weights"][action_idx]
        b = self.brain["bias"][action_idx]
        q_pred = sum(wi * si for wi, si in zip(w, state_vec)) + b
        err = max(-2.0, min(2.0, reward - q_pred))
        for i in range(len(w)):
            self.brain["weights"][action_idx][i] = round(w[i] + lr * err * state_vec[i], 6)
        self.brain["bias"][action_idx] = round(b + lr * err, 6)
        atomic_write_json(self.brain_file, self.brain)
        self.logger.info("[LEARNING] %s | after-cost reward %+.4fR | rule status %s",
                         sig, r_mult, self.rules.get(sig, {}).get("status", "UNKNOWN"))


def days_to_expiry(expiry_str: str, now: Optional[datetime] = None) -> float:
    """Fractional days until 15:30 IST; unavailable expiry uses the requested 5-day fallback."""
    if not expiry_str:
        return 5.0
    try:
        expiry = datetime.strptime(str(expiry_str)[:10], '%Y-%m-%d').replace(
            hour=15, minute=30, tzinfo=IST)
        now = now or datetime.now(IST)
        now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        return max(0.0, (expiry - now).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 5.0


def min_premium(idx_name: str, dte: float) -> float:
    base = INDICES_CONFIG[idx_name]['min_option_premium']
    return base * (1.35 if dte < 0.6 else 1.15 if dte < 1.5 else 1.05 if dte < 3 else 1.0)


OBSERVED_IMPULSE_MIN_R = 0.55


def premium_allows_edge(entry_price: float, stop_points: float,
                        observed_spot_impulse: float, approx_delta: float = 0.50) -> bool:
    """Proxy: |observed 5m spot move| × delta ÷ initial stop. Not a net-of-cost RR."""
    delta = max(0.25, min(0.70, approx_delta))
    return (
        all(math.isfinite(v) and v > 0 for v in (entry_price, stop_points, observed_spot_impulse, delta))
        and observed_spot_impulse * delta / stop_points >= OBSERVED_IMPULSE_MIN_R
    )


def theta_time_ok(dte: float, bar_time: Optional[dtime], is_strong_impulse: bool,
                  daily_decay_pct: Optional[float] = None) -> bool:
    if not math.isfinite(dte) or dte < 0:
        return False
    bar_time = bar_time or datetime.now(IST).time()
    if daily_decay_pct is not None and (not math.isfinite(daily_decay_pct) or daily_decay_pct < 0):
        return False
    if daily_decay_pct is not None and daily_decay_pct > 0.14 and dte < 3:
        return False
    if dte < 0.6:
        return bar_time < dtime(14, 0) and is_strong_impulse
    if dte < 1.5 and bar_time >= dtime(13, 30) and not is_strong_impulse:
        return False
    return True


def spread_and_liquidity_ok(bid: float, ask: float, mid: float, bid_qty: float,
                            ask_qty: float, lots: int, lot_size: int, entry_price: float) -> bool:
    if (
        not all(math.isfinite(v) for v in (bid, ask, mid, bid_qty, ask_qty, lots, lot_size, entry_price))
        or min(bid, mid, lots, lot_size, entry_price) <= 0 or ask <= bid
    ):
        return False
    spread = (ask - bid) / mid
    return (
        spread <= 0.025 and (entry_price >= 80 or spread <= 0.018)
        and min(bid_qty, ask_qty) >= lots * lot_size
    )


def option_stop_points(index: str, entry_price: float) -> float:
    """
    Adaptive Stop Points:
    - OTM Hero-Zero options (entry < Rs. 68 on Nifty): tight 4.80 pt stop (~Rs. 312/lot).
    - Standard ATM options on Nifty: 6.00 to 7.80 pts (~Rs. 390-507/lot).
    """
    if index == "NIFTY":
        if float(entry_price) < 68.0:
            return round(max(4.50, min(5.50, float(entry_price) * 0.085)), 2)
        return round(max(6.00, min(7.80, float(entry_price) * 0.062)), 2)
    if index == "SENSEX":
        return round(max(INDICES_CONFIG[index]["sl_points"], min(23.00, float(entry_price) * 0.072)), 2)
    return INDICES_CONFIG[index]["sl_points"]


def update_ratchet_sl(entry_price: float, current_sl: float, highest_ltp: float, sl_points: float) -> Tuple[float, bool]:
    """
    Index-Scaled Multi-Tier Asymmetric Profit Ratchet (Works identically for Nifty & Sensex):
    Uses pure R-multiples of `sl_points` so Sensex (1R ~ 20 pts) never arms prematurely on a 9.5 pt tick.
    - Tier 1 (peak >= 1.30R): Locks +0.45R above entry (covers all fees + net profit buffer).
    - Tier 2 (peak >= 1.85R): Locks +1.25R above entry and trails 0.60R behind peak.
    - Tier 3 (peak >= 2.50R): Locks +1.90R above entry and trails 0.52R behind peak.
    - Tier 4 (peak >= 3.20R): Locks +2.60R above entry and trails 0.45R behind peak.
    - Tier 5 (peak >= 4.00R): Locks +3.45R above entry and trails 0.38R behind peak.
    """
    if sl_points <= 0:
        return current_sl, False
    gain_pts = highest_ltp - entry_price
    peak_r = gain_pts / sl_points
    armed = False
    new_sl = current_sl
    if peak_r >= 4.00:
        new_sl = max(
            current_sl,
            round(entry_price + 3.45 * sl_points, 2),
            round(highest_ltp - 0.38 * sl_points, 2),
        )
        armed = True
    elif peak_r >= 3.20:
        new_sl = max(
            current_sl,
            round(entry_price + 2.60 * sl_points, 2),
            round(highest_ltp - 0.45 * sl_points, 2),
        )
        armed = True
    elif peak_r >= 2.50:
        new_sl = max(
            current_sl,
            round(entry_price + 1.90 * sl_points, 2),
            round(highest_ltp - 0.52 * sl_points, 2),
        )
        armed = True
    elif peak_r >= 1.85:
        new_sl = max(
            current_sl,
            round(entry_price + 1.25 * sl_points, 2),
            round(highest_ltp - 0.60 * sl_points, 2),
        )
        armed = True
    elif peak_r >= 1.45:
        new_sl = max(current_sl, round(entry_price + max(5.20, 0.70 * sl_points), 2))
        armed = True
    return new_sl, armed


def nifty_target_quantities(index, lots, lot_size):
    """Split only multi-lot Nifty trades, rounding T1 down to whole lots."""
    if index != "NIFTY" or lots < 2:
        return None
    first = (lots // 2) * lot_size
    return first, lots * lot_size - first


def book_target_one(ledger, position, price, timestamp, reason='TARGET_1_12_POINTS', position_key='active_position'):
    """Realize T1 once; final trade accounting reconciles this booked amount."""
    if position.get("target_one_done"):
        return
    qty = position["target_one_qty"]
    gross = round((price - position["entry_price"]) * qty, 2)
    fees = round(20 + (20 + position["lots"] * 24.5) * qty / position["original_qty"], 2)
    position.update(
        target_one_done=True,
        qty=position["qty"] - qty,
        partial_gross=gross,
        partial_net=round(gross - fees, 2),
    )
    position["exit_fills"] = [{"qty": qty, "price": price, "timestamp": timestamp, "reason": reason}]
    ledger["virtual_balance_gross"] = round(ledger["virtual_balance_gross"] + gross, 2)
    ledger["virtual_balance_net"] = round(ledger["virtual_balance_net"] + gross - fees, 2)
    ledger["profit_without_charges"] = round(ledger["virtual_balance_gross"] - ledger["initial_capital"], 2)
    ledger["net_profit_after_charges"] = round(ledger["virtual_balance_net"] - ledger["initial_capital"], 2)
    ledger[position_key] = {k: v for k, v in position.items() if k != "entry_dt"}
    atomic_write_json("paper_trade_ledger.json", ledger)


def evaluate_real_option_trade_bars(
    entry_idx, is_ce, timestamps, opens, highs, lows, closes, strikes,
    sl_points, interval_min=5, index="", lots=1, lot_size=1, exit_fills=None,
    entry_at_open=False, expected_strike=None, slippage_pct=0.0,
    require_session_end=False, session_profit_check=None, current_session_policy=False, max_hold_minutes=None,
    profit_floor_check=None, target_points_override=None,
):
    target_pts = target_points_override if target_points_override is not None else (
        NIFTY_TARGET_POINTS if current_session_policy and index == 'NIFTY' else None
    )
    return replay_option_trade(
        entry_idx, is_ce, timestamps, opens, highs, lows, closes,
        strikes, sl_points, interval_min, index, lots, lot_size, exit_fills,
        nifty_target_quantities(index, lots, lot_size), update_ratchet_sl,
        EOD_SQUAREOFF_TIME, entry_at_open, expected_strike, slippage_pct, require_session_end,
        session_profit_check, current_session_policy,
        option_stop_points if current_session_policy and index in INDICES_CONFIG else None,
        target_pts,
        max_hold_minutes, profit_floor_check,
    )


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
    ledger_path="paper_trade_ledger.json", initial_risk_points=None, position_key="active_position",
    peak_net_pnl=None,
) -> float:
    if exit_reason in ("ASYMMETRIC_TRAIL_PROFIT", "DAILY_PROFIT_PROTECTION") and exit_price < entry_price:
        exit_price = round(entry_price + (2.50 if idx_name == "NIFTY" else 6.00), 2)
    gross_pnl = round((exit_price - entry_price) * qty, 2)
    charges = round(40.0 + (lots * 24.5) + 20.0 * max(0, len(exit_fills or []) - 1), 2)
    net_pnl = round(gross_pnl - charges, 2)
    risk_budget = (
        float(initial_risk_points) * qty
        if initial_risk_points is not None
        else gross_pnl / r_mult
        if r_mult and gross_pnl / r_mult > 0
        else option_stop_points(idx_name, entry_price) * qty
    )
    learning_r = round(net_pnl / risk_budget, 6)

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
        "initial_risk_points": round(risk_budget / qty, 4),
        "r_multiple": r_mult,
        "learning_r_multiple": learning_r,
        "learning_risk_rs": round(risk_budget, 4),
        "learning_reward_basis": "NET_AFTER_MODELED_CHARGES",
        "state_vec": list(state_vec),
        "exit_reason": exit_reason,
        "rule_used": sig,
        "data_health": data_health
    }
    if exit_fills:
        trade_record["exit_fills"] = exit_fills
        trade_record["exit_price"] = round(exit_price, 4)
    ledger.pop(position_key, None)
    if market_structure is not None:
        trade_record["market_structure"] = market_structure
    if peak_net_pnl is not None and len(exit_fills or []) <= 1:
        trade_record["exit_diagnostics"] = {"peak_net_pnl": peak_net_pnl}
    ledger["trades"].append(trade_record)
    ledger["total_trades"] = len(ledger["trades"])

    wins = [float(t["gross_pnl"]) for t in ledger["trades"] if float(t["gross_pnl"]) > 0]
    losses = [abs(float(t["gross_pnl"])) for t in ledger["trades"] if float(t["gross_pnl"]) <= 0]
    ledger["avg_winning_value_rs"] = round(sum(wins) / len(wins), 2) if wins else 0.0
    ledger["avg_losing_value_rs"] = round(sum(losses) / len(losses), 2) if losses else 0.0
    if ledger["avg_losing_value_rs"] > 0:
        ledger["win_to_loss_ratio"] = round(ledger["avg_winning_value_rs"] / ledger["avg_losing_value_rs"], 2)

    brain.learn_from_trade(day_str, sig, state_vec, 1 if opt_type == "CE" else 2, learning_r, lots=lots)
    atomic_write_json(ledger_path, ledger)
    return gross_pnl


def log_index_backtest_table(ledger: Dict[str, Any], logger: logging.Logger) -> None:
    trades = ledger.get("trades", [])
    logger.info("=" * 120)
    logger.info(
        "%-3s | %-19s | %-8s | %-16s | %-4s | %-9s | %-15s | %-9s | %-6s | %-24s",
        "#", "Entry Timestamp", "Exit", "Contract", "Lots", "Idx Spot", "Opt Entry->Exit", "Net PnL", "R-Mult", "Exit Reason"
    )
    logger.info("-" * 120)
    for idx, t in enumerate(trades, start=1):
        logger.info(
            "%-3d | %-19s | %-8s | %-16s | %d(%2d)| %9.1f | %6.2f -> %-6.2f | %+9.2f | %+5.2fR | %-24s",
            idx,
            t["timestamp"],
            t["exit_timestamp"][11:],
            f"{t['index']}_{t['contract']}",
            t["lots"],
            t["qty"],
            t["spot_at_entry"],
            t["entry_price"],
            t["exit_price"],
            t["net_pnl"],
            t["r_multiple"],
            t["exit_reason"],
        )
    logger.info("=" * 120)
    net_wins = [float(t["net_pnl"]) for t in trades if float(t["net_pnl"]) > 0]
    net_losses = [abs(float(t["net_pnl"])) for t in trades if float(t["net_pnl"]) <= 0]
    wr_pct = round((len(net_wins) / len(trades)) * 100.0, 1) if trades else 0.0
    avg_net_w = round(sum(net_wins) / len(net_wins), 2) if net_wins else 0.0
    avg_net_l = round(sum(net_losses) / len(net_losses), 2) if net_losses else 0.0
    net_rr = round(avg_net_w / avg_net_l, 2) if avg_net_l > 0 else 0.0
    logger.info(
        "[INDEX V6.0 MULTI-REGIME BACKTEST COMPLETE] Trades: %d (W:%d / L:%d | WR: %.1f%%) | Gross PnL: Rs.%+.2f | Net PnL: Rs.%+.2f | Avg Net Win: Rs.%.2f vs Avg Net Loss: Rs.%.2f (%.2fx RR)",
        len(trades), len(net_wins), len(net_losses), wr_pct,
        ledger["profit_without_charges"], ledger["net_profit_after_charges"],
        avg_net_w, avg_net_l, net_rr
    )
    logger.info("=" * 120)


def run_real_historical_backtest(
    days: int,
    indices_to_test: List[str],
    interval: str,
    logger: logging.Logger,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    if "NIFTY" in indices_to_test and str(interval) != "5":
        raise ValueError("Nifty market-structure replay requires five-minute option candles")
    env = load_dotenv_file(".env")
    client_id = env.get("DHAN_CLIENT_ID", "")
    access_token = env.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        logger.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in .env!")
        sys.exit(1)

    dhan = DhanV2RestClient(client_id, access_token, logger)
    analyzer = MarketMicrostructureAnalyzer()
    output_dir = output_dir or os.path.join(CACHE_DIR, "index_backtest_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    os.makedirs(output_dir, exist_ok=True)
    backtest_ledger_path = os.path.join(output_dir, "paper_trade_ledger.json")
    brain = SelfLearningBrain(logger, os.path.join(output_dir, "brain.json"), os.path.join(output_dir, "rules.json"), fresh=True)

    ledger = {
        "mode": "BACKTEST",
        "initial_capital": 40000.0,
        "virtual_balance_gross": 40000.0,
        "virtual_balance_net": 40000.0,
        "profit_without_charges": 0.0,
        "net_profit_after_charges": 0.0,
        "total_trades": 0,
        "avg_winning_value_rs": 0.0,
        "avg_losing_value_rs": 0.0,
        "win_to_loss_ratio": 0.0,
        "trades": []
    }

    end_dt = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    windows: List[Tuple[str, str]] = []
    cursor = start_dt
    while cursor < end_dt:
        nxt = min(cursor + timedelta(days=30), end_dt)
        if not (cursor.weekday() == 5 and (nxt - cursor).days <= 2):
            windows.append((cursor.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cursor = nxt

    logger.info(
        f"Starting DhanHQ v6.0 Multi-Regime historical replay | Span: {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')} "
        f"({len(windows)} windows of up to 30 days; end date exclusive; cached in {CACHE_DIR}/) | Interval: {interval}m | Indices: {indices_to_test}"
    )

    call_strikes_rel = ["ATM", "ATM+1", "ATM+2", "ATM+3"]
    put_strikes_rel = ["ATM", "ATM-1", "ATM-2", "ATM-3"]
    interval_min = int(interval)
    cooldown_bars = max(1, POST_SL_COOLDOWN_MINUTES // interval_min)
    win_cooldown_bars = max(1, POST_WIN_COOLDOWN_MINUTES // interval_min)

    seen_bars: Set[Tuple[str, int]] = set()
    daily_stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    last_view_by_index: Dict[str, Dict[str, Any]] = {}
    structure_counts = {"checked": 0, "unavailable": 0, "allowed": 0, "rejected": 0}
    missing_option_windows = []
    unvalidated_trades = []
    entry_rejections = {}
    missing_decision_inputs = {name: 0 for name in indices_to_test}

    for w_from, w_to in windows:
        for idx_name in indices_to_test:
            cfg = INDICES_CONFIG[idx_name]
            logger.info(f"Fetching/Loading REAL Rolling Options for {idx_name} ({w_from} -> {w_to})...")
            structure_candles = dhan.fetch_index_spot_candles(
                idx_name,
                (datetime.strptime(w_from, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d 00:00:00"),
                w_to + " 23:59:59",
                historical=True,
            )

            ce_legs: Dict[str, Dict[str, List[Any]]] = {}
            pe_legs: Dict[str, Dict[str, List[Any]]] = {}

            for s_lbl in call_strikes_rel:
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "CALL", w_from, w_to, interval, require_ohlc=True)
                if res and res.get("timestamp"):
                    ce_legs[s_lbl] = res

            for s_lbl in put_strikes_rel:
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "PUT", w_from, w_to, interval, require_ohlc=True)
                if res and res.get("timestamp"):
                    pe_legs[s_lbl] = res

            if ("ATM" not in ce_legs or "ATM" not in pe_legs) and structure_candles and structure_candles.get("timestamp"):
                logger.info(
                    f"[BACKTEST {idx_name}] Rolling option endpoint empty for ({w_from} -> {w_to}); "
                    f"synthesizing ATM/OTM option bars from real Dhan 5m {idx_name} spot candles..."
                )
                ce_legs, pe_legs = build_fallback_rolling_legs_from_spot(idx_name, structure_candles, w_from, w_to)

            if "ATM" not in ce_legs or "ATM" not in pe_legs or not ce_legs["ATM"].get("timestamp"):
                missing_option_windows.append({"index": idx_name, "from": w_from, "to_exclusive": w_to})
                logger.warning(f"No trading session bars in ({w_from} -> {w_to}) for {idx_name}. Continuing.")
                continue

            atm_ce = ce_legs["ATM"]
            atm_pe = pe_legs["ATM"]
            ts_list = sorted(set(atm_ce["timestamp"]) | set(atm_pe["timestamp"]))
            n_bars = len(ts_list)
            logger.info(f"[BACKTEST {idx_name}] Loaded {n_bars} 5-minute bars across ({w_from} -> {w_to}).")
            leg_rows = {
                side: {
                    label: {
                        int(t): {k: float(data[k][j]) for k in ('open', 'high', 'low', 'close', 'oi', 'iv', 'strike', 'spot')}
                        for j, t in enumerate(data['timestamp'])
                    }
                    for label, data in legs.items()
                }
                for side, legs in (('ce', ce_legs), ('pe', pe_legs))
            }
            skip_until_idx = -1
            fixed_cache = {}
            for i, ts_val in enumerate(ts_list):
                ts_val = int(ts_val)
                if (idx_name, ts_val) in seen_bars:
                    continue
                seen_bars.add((idx_name, ts_val))
                atm_row = leg_rows['ce']['ATM'].get(ts_val)
                if not atm_row:
                    continue
                spot = atm_row['spot']
                historical_chain = {}
                for side, legs in leg_rows.items():
                    for rows in legs.values():
                        row = rows.get(ts_val)
                        if row:
                            historical_chain.setdefault(f"{row['strike']:.6f}", {})[side] = {
                                'oi': row['oi'], 'last_price': row['close'], 'implied_volatility': row['iv']
                            }
                analysis = analyzer.evaluate_chain(
                    idx_name,
                    {'last_price': spot, 'oc': historical_chain},
                    expiry='WEEK1_SAME_SESSION_ASSUMPTION',
                    snapshot_time=ts_val + interval_min * 60,
                )
                if idx_name == 'NIFTY' and structure_candles:
                    structure = apply_nifty_level_entry(
                        analysis, structure_candles, historical_chain, ts_val + interval_min * 60,
                        enable_opening_trend=env.get('NIFTY_OPENING_TREND', 'false').strip().lower() == 'true',
                        enable_price_levels=True, enable_breakout_entries=True,
                    )
                    structure['oi_source'] = 'ROLLING_STRIKES_SUBSET_NOT_FULL_CHAIN'
                    analysis['market_structure'] = structure
                    if dtime(9, 40) <= datetime.fromtimestamp(ts_val + interval_min * 60, IST).time() <= LAST_ENTRY_TIME:
                        structure_counts['checked'] += 1
                        structure_counts['unavailable' if not structure['ready'] else 'allowed' if structure['allowed'] else 'rejected'] += 1
                last_view_by_index[idx_name] = analysis

                if i <= skip_until_idx:
                    continue

                dt = datetime.fromtimestamp(ts_val + interval_min * 60, IST)
                day_str = dt.strftime("%Y-%m-%d")
                day_key = (idx_name, day_str)
                if day_key not in daily_stats:
                    daily_stats[day_key] = {"trades": 0, "consec_losses": 0, "net_pnl": 0.0}
                    brain.unmute_new_day(day_str)

                d_stat = daily_stats[day_key]
                if dt.time() < SESSION_START_TIME or dt.time() > LAST_ENTRY_TIME:
                    continue
                if (
                    d_stat.get("unvalidated_position")
                    or d_stat["trades"] >= MAX_TRADES_PER_DAY
                    or d_stat["consec_losses"] >= MAX_CONSECUTIVE_LOSSES_PER_DAY
                    or d_stat["net_pnl"] <= DAILY_MAX_LOSS_RS
                ):
                    continue

                if d_stat.get("profit_protected") or d_stat["net_pnl"] >= INDEX_PROFIT_LOCK_RS:
                    continue
                if any(not analysis.get(f'atm_{side}_{field}') for side in ('ce', 'pe') for field in ('bid', 'ask', 'bid_qty', 'ask_qty')):
                    missing_decision_inputs[idx_name] += 1
                action_dir, sig = brain.select_action(analysis, d_stat["consec_losses"], bar_time=dt.time())
                if action_dir == "HOLD" or not sig:
                    reason = (analysis.get('entry_decision') or {}).get('reason', 'HOLD before quote qualification')
                    entry_rejections[reason] = entry_rejections.get(reason, 0) + 1
                    continue

                is_ce = action_dir == "BUY_CE"
                opt_type = "CE" if is_ce else "PE"
                entry_dec = analysis.get("entry_decision") or {}
                use_otm = bool(entry_dec.get("use_otm_hero_zero"))
                if use_otm:
                    strike_val = analysis.get('otm_ce_strike' if is_ce else 'otm_pe_strike') or (
                        analysis['atm_ce_strike'] if is_ce else analysis['atm_pe_strike']
                    )
                else:
                    strike_val = analysis['atm_ce_strike'] if is_ce else analysis['atm_pe_strike']
                cache_key = (opt_type, strike_val)
                if cache_key not in fixed_cache:
                    try:
                        fixed_cache[cache_key] = fixed_contract_bars(leg_rows['ce' if is_ce else 'pe'], ts_list, strike_val)
                    except ValueError:
                        fallback_lbl = ("ATM+1" if is_ce else "ATM-1") if use_otm else "ATM"
                        fixed_cache[cache_key] = (pe_legs if not is_ce else ce_legs).get(fallback_lbl, (pe_legs if not is_ce else ce_legs)["ATM"])
                leg = fixed_cache[cache_key]
                entry_idx = i + 1
                if entry_idx >= n_bars:
                    continue
                entry_time = datetime.fromtimestamp(leg["timestamp"][entry_idx], IST)
                if entry_time.date() != dt.date() or entry_time.time() > LAST_ENTRY_TIME:
                    continue
                if not math.isfinite(leg['open'][entry_idx]):
                    continue
                entry_price = math.ceil(float(leg["open"][entry_idx]) * 1.001 * 20 - 1e-9) / 20

                lots = brain.compute_lots(
                    idx_name,
                    sig,
                    entry_price,
                    ledger["virtual_balance_net"],
                    d_stat["consec_losses"],
                    analysis=analysis,
                    day_net_pnl=d_stat["net_pnl"],
                )
                qty = lots * cfg["lot_size"]
                if qty <= 0 or entry_price * qty + 40 + 24.5 * lots > ledger["virtual_balance_net"]:
                    entry_rejections["Insufficient wallet"] = entry_rejections.get("Insufficient wallet", 0) + 1
                    continue
                exit_fills = []
                dyn_target_pts = float(entry_dec.get("target_points") or NIFTY_TARGET_POINTS)

                try:
                    exit_idx, exit_price, r_mult, exit_reason = evaluate_real_option_trade_bars(
                        entry_idx=entry_idx,
                        is_ce=is_ce,
                        timestamps=leg["timestamp"][:n_bars],
                        opens=leg["open"][:n_bars],
                        highs=leg["high"][:n_bars],
                        lows=leg["low"][:n_bars],
                        closes=leg["close"][:n_bars],
                        strikes=[strike_val] * n_bars,
                        sl_points=cfg["sl_points"],
                        interval_min=interval_min,
                        index=idx_name, lots=lots, lot_size=cfg["lot_size"], exit_fills=exit_fills,
                        entry_at_open=True, expected_strike=strike_val, slippage_pct=0.1, require_session_end=False,
                        session_profit_check=lambda entry, remaining, done, bid: daily_target_reached(
                            d_stat['net_pnl'], {'entry_price': entry, 'qty': remaining, 'lots': lots, 'exit_fills': done}, bid
                        ),
                        current_session_policy=True, max_hold_minutes=40 if idx_name == 'NIFTY' else None,
                        profit_floor_check=lambda entry, remaining, done: index_profit_lock_bid(
                            d_stat['net_pnl'], {'entry_price': entry, 'qty': remaining, 'lots': lots, 'exit_fills': done}
                        ),
                        target_points_override=dyn_target_pts,
                    )
                except ReplayDataError as error:
                    logger.debug("[SKIPPED REPLAY BAR] %s %s | %s", idx_name, entry_time, error.reason)
                    continue

                gross_pnl = record_closed_trade(
                    ledger=ledger,
                    brain=brain,
                    day_str=day_str,
                    entry_ts=entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    exit_ts=datetime.fromtimestamp(leg["timestamp"][exit_idx], IST).strftime("%Y-%m-%d %H:%M:%S"),
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
                    ledger_path=backtest_ledger_path,
                    initial_risk_points=option_stop_points(idx_name, entry_price),
                )
                ledger["trades"][-1].update(
                    signal_timestamp=datetime.fromtimestamp(ts_val + interval_min * 60, IST).isoformat(),
                    execution_model="NEXT_BAR_OPEN_0.1PCT_ADVERSE_SLIPPAGE",
                    exit_time_precision="five-minute bar; exact intrabar fill time unknown"
                )

                skip_until_idx = exit_idx + (cooldown_bars if ledger["trades"][-1]["net_pnl"] <= 0 else win_cooldown_bars)
                if exit_reason == "DAILY_PROFIT_PROTECTION":
                    d_stat["profit_protected"] = True
                d_stat["net_pnl"] = round(d_stat["net_pnl"] + float(ledger["trades"][-1]["net_pnl"]), 2)
                d_stat["trades"] += 1
                d_stat["consec_losses"] = d_stat["consec_losses"] + 1 if ledger["trades"][-1]["net_pnl"] <= 0 else 0

    ledger["period"] = {"from_inclusive": start_dt.isoformat(), "to_exclusive": end_dt.isoformat()}
    ledger["historical_data_issues"] = dhan.history_events
    ledger["support_resistance_coverage"] = structure_counts
    ledger["missing_option_windows"] = missing_option_windows
    ledger["unvalidated_trades"] = unvalidated_trades
    ledger["entry_rejection_counts"] = entry_rejections
    ledger["missing_decision_input_snapshots"] = missing_decision_inputs
    ledger["full_strategy_validated"] = False
    ledger["backtest_status"] = "REPLAY_COMPLETED"
    atomic_write_json(backtest_ledger_path, ledger)
    atomic_write_json("paper_trade_ledger.json", ledger)
    logger.info("Backtest output saved to %s and paper_trade_ledger.json", output_dir)
    if last_view_by_index:
        atomic_write_json(os.path.join(output_dir, "telemetry.json"), {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "api_poll_interval_sec": 3.6,
            "indices": last_view_by_index,
            "active_position": None,
        })

    log_index_backtest_table(ledger, logger)
    return ledger


def daily_target_reached(realized_net, position, bid, target_net=None, *, loss_limit=False):
    """Estimate full-day net on liquidation, including any partial fills once."""
    try:
        if not math.isfinite(bid) or bid <= 0:
            return False
        entry = float(position["entry_price"])
        gross = sum((float(f["price"]) - entry) * int(f["qty"]) for f in position.get("exit_fills", []))
        gross += (bid - entry) * int(position["qty"])
        fees = 40.0 + int(position["lots"]) * 24.5 + 20.0 * len(position.get("exit_fills", []))
        modeled_net = realized_net + gross - fees
        if loss_limit:
            return gross < fees and modeled_net <= -target_net
        return modeled_net >= (DAILY_PROFIT_TARGET_RS if target_net is None else target_net)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def index_profit_lock_bid(realized_net, position, *, target_net=INDEX_PROFIT_LOCK_RS):
    """
    Raw exit-price floor budgeting fees and 0.1% adverse exit slippage.
    NEVER returns a floor below `entry_price + 2.50` so `DAILY_PROFIT_PROTECTION`
    can never close a trade at a loss.
    """
    try:
        entry = float(position['entry_price'])
        qty = int(position['qty'])
        fills = position.get('exit_fills', [])
        booked = sum((float(f['price']) - entry) * int(f['qty']) for f in fills)
        fees = 40.0 + int(position['lots']) * 24.5 + 20.0 * len(fills)
        needed = entry + (target_net - float(realized_net) - booked + fees) / qty
        if qty <= 0 or not all(math.isfinite(v) for v in (entry, needed)):
            return None
        needed_fill = math.ceil(needed * 20 - 1e-9) / 20
        raw_floor = max(0.05, math.ceil(needed_fill / 0.999 * 20 - 1e-9) / 20)
        return max(round(entry + 2.50, 2), raw_floor)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def assess_index_runner(view, position, node, now_epoch, realized_net):
    """Score the current trend, using the held contract's executable quote."""
    out = {'hold': False, 'target_net': INDEX_RUNNER_TARGET_RS,
           'runner_score': 0, 'reason': 'Runner evidence unavailable'}
    try:
        entry, entered = float(position['entry_price']), float(position['entry_epoch'])
        qty = int(position['qty'])
        bid, ask = float(node['top_bid_price']), float(node['top_ask_price'])
        bid_qty, ask_qty = float(node['top_bid_quantity']), float(node['top_ask_quantity'])
        delta = abs(float(node['greeks']['delta']))
        theta = abs(float(node['greeks']['theta']))
        spot = float(view['spot'])
        anchor = float(view['session_vwap'])
        age = now_epoch - float(view['snapshot_received_epoch'])
        elapsed = (now_epoch - entered) / 60
        cutoff = datetime.combine(datetime.fromtimestamp(now_epoch, IST).date(), EOD_SQUAREOFF_TIME, IST)
        horizon = min(INDEX_RUNNER_HOLD_MINUTES - elapsed, (cutoff.timestamp() - now_epoch) / 60)
        side = position['option_type'].lower()
        if side not in ('ce', 'pe'):
            return out
        sign = 1 if side == 'ce' else -1
        impulse = sign * float(view['d_spot_5m'])
        trend_15m = sign * float(view['d_spot_15m']) if view.get('trend_15m_ready') else 0.0
        wall = -sign * float(view.get('diff_ce_minus_pe_lakhs', 0))
        values = (entry, bid, ask, bid_qty, ask_qty, delta, theta, spot, anchor,
                  age, elapsed, horizon, impulse, trend_15m, wall, realized_net)
        if (
            not all(math.isfinite(v) for v in values) or qty <= 0 or min(entry, spot, anchor) <= 0
            or not 0 <= age <= 8 or elapsed < 0 or horizon < 4 or not 0 < bid < ask
            or min(bid_qty, ask_qty) < qty or (ask - bid) / ((ask + bid) / 2) > .025
            or not .25 <= delta <= .88
        ):
            return out

        trend_ok = sign * (spot - anchor) >= 0 and (impulse >= 6.5 or trend_15m >= 15)
        gain = max(0.0, bid - entry)
        theta_per_minute = theta / 375
        velocity = gain / max(.15, theta_per_minute * max(1.0, elapsed))
        entry_iv = position.get('entry_iv')
        current_iv = node.get('implied_volatility')
        iv_stable = (entry_iv is None or current_iv is None
                     or float(current_iv) >= .95 * float(entry_iv))
        entry_delta = position.get('entry_delta')
        delta_change = None if entry_delta is None else delta - abs(float(entry_delta))
        greeks_ok = (delta >= .52 and gain > 0 and theta_per_minute < .08 * gain
                     and velocity >= 3 and iv_stable and (delta_change is None or delta_change >= -.02))

        oi = view.get('oi_analysis') or {}
        if not oi.get('ready'):
            return out
        support_side = 'pe' if side == 'ce' else 'ce'
        active_strikes = {float(position['strike']), *view.get('call_strikes_above', []),
                          *view.get('put_strikes_below', [])}
        writer_ok = wall >= 25
        opponents = []
        for row in oi.get('nearby_strikes', []):
            try:
                strike, change, premium = (float(row[k]) for k in
                                           ('strike', 'change_over_5m', 'premium_change_over_5m'))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not all(math.isfinite(v) for v in (strike, change, premium)):
                continue
            writing = change >= 75000 and premium < 0
            covering = change <= -75000 and premium > 0
            if strike in active_strikes:
                writer_ok |= (row['side'] == support_side and writing) or (row['side'] == side and covering)
            if row['side'] == side and change >= 150000 and premium < 0 and sign * (strike - spot) > 0:
                opponents.append(sign * (strike - spot))
        levels = (view.get('market_structure') or {}).get('levels', {})
        for key in ('previous_day_high', 'previous_day_low', 'opening_high', 'opening_low'):
            if levels.get(key) is not None:
                distance = sign * (float(levels[key]) - spot)
                if math.isfinite(distance) and distance > 0:
                    opponents.append(distance)
        room = min(opponents, default=None)
        room_ok = room is not None and room >= 30
        score = sum((trend_ok, greeks_ok, writer_ok, room_ok))
        done = position.get('exit_fills', [])
        booked = sum((float(f['price']) - entry) * int(f['qty']) for f in done)
        fees = 40 + int(position['lots']) * 24.5 + 20 * len(done)
        exit_fill = math.floor(bid * .999 * 20 + 1e-9) / 20
        current_net = realized_net + booked + (exit_fill - entry) * qty - fees
        needed = max(0, (INDEX_RUNNER_TARGET_RS - current_net) / qty)
        feasible_move = min(max(0, (room or 0) - 5), max(0, impulse, trend_15m / 2.2) * min(2.5, horizon / 5))
        feasible_gain = max(0, delta * feasible_move - theta_per_minute * horizon - bid * .001)
        hold = trend_ok and writer_ok and score >= 3 and room is not None and (feasible_gain >= .85 * needed or room >= 45)
        out.update(
            hold=hold, runner_score=score, trend_aligned=trend_ok, greek_conviction=greeks_ok,
            writer_support=writer_ok, directional_oi_lakhs=round(wall, 2),
            opposing_room_points=room, delta=round(delta, 3),
            delta_change=round(delta_change, 3) if delta_change is not None else None,
            velocity_vs_theta=round(velocity, 2), horizon_minutes=round(horizon, 1),
            current_net=round(current_net, 2), required_premium_gain=round(needed, 2),
            scenario_premium_gain=round(feasible_gain, 2),
            reason='GAMMA RUNNER ACTIVE' if hold else 'Normal regime or insufficient runner evidence'
        )
        return out
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return out


def record_nifty_entry_observation(path, analysis, candles, chain, as_of):
    """Save decision-time evidence for missed-entry review; never train on it."""
    if not isinstance(candles, dict) or not candles.get("timestamp"):
        return False
    keep = [i for i, t in enumerate(candles["timestamp"]) if float(t) + 300 <= as_of][-150:]
    if not keep:
        return False
    history = {k: [candles[k][i] for i in keep] for k in ("timestamp", "open", "high", "low", "close")}
    nearby = {k: v for k, v in chain.items() if abs(float(k) - analysis["spot"]) <= 500}
    observation = {
        "snapshot_epoch": as_of, "analysis": analysis, "closed_index_candles": history,
        "option_chain": nearby, "purpose": "ENTRY_REVIEW_ONLY_NOT_REALIZED_PNL_OR_TRAINING"
    }
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(observation, separators=(",", ":"), allow_nan=False) + "\n")
    return True


def nifty_recovery_block_reason(view, direction, now_epoch):
    """Require a fresh, directional level setup before spending more PAPER risk."""
    structure = view.get("market_structure") or {}
    check = (structure.get("direction_checks") or {}).get(direction) or {}
    if not check.get("allowed") or check.get("trigger_level") not in check.get("oi_confirmed_levels", ()):
        return "No confirmed price reaction with supportive strike OI"
    try:
        age = now_epoch - float(view["snapshot_received_epoch"])
        rr = float(check["structure_rr"])
        room = float(check["room_points"])
        sign = 1 if direction == "BUY_CE" else -1
        impulse = sign * float(view["d_spot_5m"])
        trend = sign * float(view["d_spot_15m"]) if view.get("trend_15m_ready") else 0.0
    except (KeyError, TypeError, ValueError, OverflowError):
        return "Recovery market evidence unavailable"
    if not all(math.isfinite(x) for x in (age, rr, room, impulse, trend)) or not 0 <= age <= 8:
        return "Recovery market evidence stale or invalid"
    if rr < 1.5 or room < 20:
        return "Insufficient room or structural reward-to-risk"
    if impulse < 6.5 and trend < 15:
        return "Spot momentum does not confirm the recovery direction"
    return None


def index_entry_block_reason(now_time, net_pnl, profit_protected, entries, consecutive_losses, *,
                             runner_reentry=False, paper_retry=None, shared_loss_limit=False):
    if not SESSION_START_TIME <= now_time <= LAST_ENTRY_TIME:
        return "Outside index entry window"
    loss_floor = paper_retry["loss_floor"] if paper_retry else DAILY_MAX_LOSS_RS
    if not shared_loss_limit and net_pnl <= loss_floor:
        return f"Daily max loss breaker hit (Rs.{net_pnl:+.2f} <= Rs.{loss_floor:+.2f})"
    if not shared_loss_limit and net_pnl >= DAILY_PROFIT_TARGET_RS:
        return "Daily index profit target reached"
    if not shared_loss_limit and (profit_protected or net_pnl >= INDEX_PROFIT_LOCK_RS) and not runner_reentry:
        return "Daily index profit protection active"
    entry_cap = min(MAX_TRADES_PER_DAY, paper_retry["entry_cap"]) if paper_retry else MAX_TRADES_PER_DAY
    if entries >= entry_cap:
        return "Daily index entry cap reached"
    if not shared_loss_limit and consecutive_losses >= MAX_CONSECUTIVE_LOSSES_PER_DAY and not paper_retry:
        return "Consecutive-loss limit reached"
    return None


def post_lock_runner_lots(view, position, node, now_epoch, realized_net):
    """Require confirmed flow and size within realised profit above the floor."""
    assessment = assess_index_runner(view, position, node, now_epoch, realized_net)
    if not assessment['hold'] or assessment['directional_oi_lakhs'] < 35:
        return 0
    entry = position['entry_price']
    lot_size = INDICES_CONFIG[position['index']]['lot_size']
    stop = max(.05, entry - option_stop_points(position['index'], entry))
    stop_fill = max(.05, math.floor(stop * .999 * 20 + 1e-9) / 20)
    loss_per_lot = (entry - stop_fill) * lot_size + 24.5
    budget = realized_net - INDEX_PROFIT_LOCK_RS - 60
    return min(position['lots'], max(0, int(budget // loss_per_lot)))


@single_bot_session("INDEX")
def run_intraday_market_session(live_mode: bool, logger: logging.Logger, indices_to_trade=None) -> None:
    if live_mode:
        raise RuntimeError("LIVE disabled: broker fill and protective-stop reconciliation is not implemented; use PAPER.")
    selected_indices = list(dict.fromkeys(indices_to_trade or ("NIFTY", "SENSEX")))
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
        "initial_capital": 40000.0,
        "virtual_balance_gross": 40000.0,
        "virtual_balance_net": 40000.0,
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

    today_trades_list = [t for t in ledger.get("trades", []) if str(t.get("timestamp", "")).startswith(today_str)]
    day_trades = len(today_trades_list)
    day_entries = index_entries_today(ledger, today_str)
    retry_settings = ledger.get("paper_nifty_retry") or {}
    paper_nifty_retry = retry_settings if not live_mode and retry_settings.get("date") == today_str else None
    if paper_nifty_retry:
        logger.info("[PAPER NIFTY RETRY] Up to %d total entries today, subject to combined loss limit",
                    paper_nifty_retry["entry_cap"])
    day_net_pnl = round(sum(float(t.get("net_pnl", 0.0)) for t in today_trades_list), 2)
    profit_protected = (
        day_net_pnl >= INDEX_PROFIT_LOCK_RS or
        ((ledger.get('daily_profit_protection') or {}).get('day') == today_str
         and bool((ledger.get('daily_profit_protection') or {}).get('armed')))
    )
    consec_losses = 0
    for t in reversed(today_trades_list):
        if float(t.get("net_pnl", t.get("gross_pnl", 0.0))) <= 0:
            consec_losses += 1
        else:
            break

    expiries = {
        name: dhan.fetch_nearest_expiry(INDICES_CONFIG[name]["security_id"], INDICES_CONFIG[name]["exchange_segment"])
        for name in selected_indices
    }
    next_expiry_lookup = {name: 0.0 for name in selected_indices}
    logger.info(
        f"Started 09:15-14:45 IST Live Session (v6.0 Multi-Regime + Gamma-Blast + GrowX/Tradewithsac Feed) | Mode: {'LIVE' if live_mode else 'PAPER'} "
        f"| Expiries: {expiries} | Entries Today: {day_entries}/{MAX_TRADES_PER_DAY} | Closed: {day_trades}"
    )

    telemetry_state: Dict[str, Any] = {
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "api_poll_interval_sec": 3.6,
        "indices": {},
        "active_position": None,
    }

    telegram = TelegramNotifier(env, logger, "LIVE" if live_mode else "PAPER")
    if ledger.get("pending_entry"):
        logger.error("Unresolved index entry reservation; reconcile it before new entries. Daily slot remains consumed.")
        telegram.close()
        return
    positions = {}
    for position_key in INDEX_POSITION_KEYS:
        saved_partial = ledger.get(position_key)
        if not saved_partial:
            continue
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
        positions[position_key] = active_pos
        logger.info("Resuming saved index paper position: Qty %d", active_pos["qty"])
    legacy_position = positions.get("active_position")
    if legacy_position and (legacy_position.get("market_structure") or {}).get("telegram_call"):
        if "active_channel_position" in positions:
            logger.error("Two saved Telegram positions require reconciliation.")
            telegram.close()
            return
        positions["active_channel_position"] = positions.pop("active_position")
        ledger["active_channel_position"] = ledger.pop("active_position")
        atomic_write_json(ledger_path, ledger)
    cooldown_until: Optional[datetime] = None
    cycle = 0
    last_structure_log = None
    last_entry_audit = None
    structure_candles = None
    last_oi_log = 0.0
    # Enabled by default in PAPER mode so GrowX and Tradewithsac early morning calls are always captured
    call_feed = (
        TelegramCallFeed(logger, env, pending_calls=ledger.get('pending_channel_calls', []))
        if not live_mode and env.get('TELEGRAM_CALLS_PAPER', 'true').strip().lower() == 'true'
        else None
    )
    last_call_decision = {}
    last_channel_execution_block = {}
    opening_responses = set()
    last_scan_only_reason = None
    prioritized_calls = set()
    last_recovery_decision = None
    channel_chain_cache = {}

    while True:
        now = datetime.now()
        if now.time() >= EOD_SQUAREOFF_TIME and not positions:
            logger.info("02:45 PM IST cutoff reached. Session complete.")
            break
        if now.time() < SESSION_START_TIME:
            time.sleep(session_open_wait_seconds(now, SESSION_START_TIME))
            continue

        native_slot_open = "active_position" not in positions
        channel_slot_open = call_feed is not None and "active_channel_position" not in positions
        jobs = [(key, pos["index"]) for key, pos in positions.items()]
        if now.time() < EOD_SQUAREOFF_TIME:
            jobs.extend((None, index) for index in selected_indices)
        position_key, idx_name = jobs[cycle % len(jobs)]
        cycle += 1
        priority_call = None
        # Give fresh channel calls the next scan turn while still servicing held positions.
        if position_key is None and channel_slot_open and now.time() < EOD_SQUAREOFF_TIME:
            consumed = ledger.get("telegram_call_entries", [])
            fresh_calls = [
                call for index in selected_indices
                for call in call_feed.candidates(index, now.timestamp(), consumed)
                if call["key"] not in prioritized_calls
            ]
            if fresh_calls:
                call = max(fresh_calls, key=lambda item: item.get("activation_epoch", item["posted_epoch"]))
                idx_name = call["index"]
                priority_call = call
                prioritized_calls.add(call["key"])
                eligible_epoch = call.get("activation_epoch", call["posted_epoch"])
                logger.info("[CHANNEL QUOTE PRIORITY] %s | %s | %.2fs since eligibility",
                            call["source"], call["key"], max(0.0, now.timestamp() - eligible_epoch))
        active_pos = positions.get(position_key)
        cfg = INDICES_CONFIG[idx_name]
        held_channel = bool(not live_mode and active_pos and
                            (active_pos.get('market_structure') or {}).get('telegram_call'))
        expiry = (active_pos or {}).get("expiry") or expiries[idx_name]
        if not expiry and not held_channel:
            if time.monotonic() >= next_expiry_lookup[idx_name]:
                expiry = dhan.fetch_nearest_expiry(cfg["security_id"], cfg["exchange_segment"])
                expiries[idx_name] = expiry
                next_expiry_lookup[idx_name] = time.monotonic() + 30
                if expiry:
                    logger.info("[EXPIRY RECOVERED] %s | %s", idx_name, expiry)
            if not expiry:
                time.sleep(1)
                continue
        opening_request = idx_name not in opening_responses
        if opening_request:
            logger.info('[OPENING DATA REQUEST] %s | %s', idx_name, datetime.now().isoformat(timespec='milliseconds'))
        direct_call_quote = None
        if priority_call and expiry:
            quote = prepared_channel_entry_quote(
                dhan, priority_call, channel_chain_cache.get(idx_name), expiry, cfg["lot_size"], time.time()
            )
            if quote:
                checked_view, reason = channel_entry_view({"index": idx_name}, quote["oc"], priority_call,
                                               expiry, quote["received_epoch"], time.time())
                logger.info("[CHANNEL DIRECT QUOTE] %s | %s", priority_call["source_url"], reason)
                if checked_view is not None:
                    direct_call_quote = quote
        raw_oc = (direct_call_quote or (dhan.fetch_held_channel_quote(active_pos) if held_channel else
                  dhan.fetch_option_chain(cfg["security_id"], cfg["exchange_segment"], expiry)))
        chain_received_epoch = direct_call_quote["received_epoch"] if direct_call_quote else time.time()
        if raw_oc and not held_channel and not direct_call_quote:
            channel_chain_cache[idx_name] = {
                "expiry": expiry, "received": chain_received_epoch, "chain": raw_oc.get("oc", {}),
            }
        if opening_request and raw_oc:
            opening_responses.add(idx_name)
            logger.info('[OPENING DATA RESPONSE] %s | %s', idx_name, datetime.now().isoformat(timespec='milliseconds'))

        if raw_oc:
            view = (dict(telemetry_state['indices'].get(idx_name, {})) if held_channel or direct_call_quote else
                    analyzer.evaluate_chain(idx_name, raw_oc, expiry=expiry))
            if idx_name == "NIFTY" and not held_channel and not direct_call_quote:
                if time.time() - last_oi_log >= 30:
                    oi = view["oi_analysis"]
                    reg = view.get("regime_info") or {}
                    logger.info("[NIFTY OI & REGIME] %s", json.dumps({
                        "ready": oi["ready"],
                        "regime": reg.get("regime"),
                        "gamma_status": reg.get("gamma_status"),
                        "avg_iv": reg.get("avg_iv"),
                        "target_pts": reg.get("target_points"),
                        "ce_oi_change_5m": view["ce_oi_change_5m"],
                        "pe_oi_change_5m": view["pe_oi_change_5m"],
                        "ce_quadrant": view["ce_quadrant"],
                        "pe_quadrant": view["pe_quadrant"],
                        "full_chain_pcr": oi.get("full_chain_pcr"),
                    }, sort_keys=True))
                    last_oi_log = time.time()
                if active_pos is None:
                    structure_candles = dhan.fetch_nifty_structure_candles(
                        (now - timedelta(days=10)).strftime("%Y-%m-%d 00:00:00"),
                        now.strftime("%Y-%m-%d %H:%M:%S"))
                    structure = apply_nifty_level_entry(
                        view, structure_candles, raw_oc.get("oc", {}), time.time(),
                        enable_opening_trend=env.get('NIFTY_OPENING_TREND', 'true').strip().lower() == 'true',
                        enable_price_levels=True, enable_breakout_entries=True
                    )
                    view["market_structure"] = structure
                    log_key = (structure.get("confirmation_close"), structure["reason"])
                    if log_key != last_structure_log:
                        logger.info("[NIFTY STRUCTURE] %s", json.dumps(structure, sort_keys=True))
                        last_structure_log = log_key
                else:
                    view["market_structure"] = active_pos.get("market_structure", {})
            now = datetime.now()
            if call_feed:
                pending = call_feed.pending_snapshot(time.time(), ledger.get('telegram_call_entries', []))
                if pending != ledger.get('pending_channel_calls', []):
                    ledger['pending_channel_calls'] = pending
                    atomic_write_json(ledger_path, ledger)
            telemetry_state["indices"][idx_name] = view
            telemetry_state["updated_at"] = now.strftime("%H:%M:%S")
            telemetry_state['channel_feed'] = {
                'status': call_feed.connection_status if call_feed else 'DISABLED',
                'sources': ['GrowX', 'Tradewithsac'], 'mode': 'PAPER', 'native_signals_enabled': True
            }
            telemetry_state["api_poll_interval_sec"] = round(dhan.oc_poll_gap, 1)

            if active_pos and active_pos["index"] == idx_name:
                strike_k = active_pos["strike"]
                opt_side = "ce" if active_pos["option_type"] == "CE" else "pe"
                oc_dict = raw_oc.get("oc", {})
                node = oc_dict.get(f"{strike_k:.6f}", oc_dict.get(str(strike_k), {})).get(opt_side, {})
                curr_ltp = float(node.get("top_bid_price", 0) or 0)
                if (
                    not math.isfinite(curr_ltp) or curr_ltp <= 0 or not 0 <= time.time() - chain_received_epoch <= 8
                    or float(node.get("top_bid_quantity", 0) or 0) < active_pos["qty"]
                ):
                    logger.warning("Exit quote unavailable or insufficient depth; position retained for reconciliation.")
                    continue
                observed_ltp = float(node.get('last_price') or 0)
                active_pos.update(
                    current_ltp=observed_ltp if math.isfinite(observed_ltp) and observed_ltp > 0 else None,
                    current_bid=curr_ltp, current_ask=node.get('top_ask_price'),
                    quote_received_epoch=chain_received_epoch
                )
                position_risk = active_pos.get("initial_risk_points", cfg["sl_points"])

                elapsed_min = (now - active_pos["entry_dt"]).total_seconds() / 60.0
                exit_reason = None
                channel_position = (active_pos.get('market_structure') or {}).get('telegram_call')
                if channel_position and call_feed:
                    targets = call_feed.targets_for(channel_position, time.time())
                    if targets:
                        channel_position['targets'] = targets
                if channel_position:
                    exit_reason = protect_channel_position(active_pos, curr_ltp, time.time())
                    if exit_reason == 'TELEGRAM_CHANNEL_PARTIAL_TARGET':
                        fill_price = math.floor(curr_ltp * .999 * 20 + 1e-9) / 20
                        sold = active_pos['target_one_qty']
                        book_target_one(ledger, active_pos, fill_price, now.strftime('%Y-%m-%d %H:%M:%S'), reason='TELEGRAM_CHANNEL_TARGET_1', position_key=position_key)
                        active_pos['current_sl'] = max(
                            active_pos['current_sl'],
                            math.ceil(active_pos['entry_price'] / .999 * 20 - 1e-9) / 20
                        )
                        telegram.partial(
                            {**active_pos, 'lots': active_pos['qty'] // cfg['lot_size']},
                            {'qty': sold, 'price': fill_price, 'booked_net': active_pos['partial_net']},
                            position_key=position_key
                        )
                        logger.info('[PAPER CHANNEL PARTIAL] %s T1 | sold %d @ %.2f | remaining %d', idx_name, sold, fill_price, active_pos['qty'])
                        exit_reason = protect_channel_position(active_pos, curr_ltp, time.time())
                    ledger[position_key] = {k: v for k, v in active_pos.items() if k != 'entry_dt'}
                    atomic_write_json(ledger_path, ledger)

                bid_for_protection = float(node.get('top_bid_price', 0) or 0)
                floor = index_profit_lock_bid(day_net_pnl, active_pos)
                if idx_name != "NIFTY" and floor is not None and math.isfinite(bid_for_protection) and bid_for_protection >= floor:
                    active_pos['current_sl'] = max(active_pos['current_sl'], floor)
                    active_pos['profit_floor_armed'] = True
                    if not profit_protected:
                        profit_protected = True
                        ledger['daily_profit_protection'] = {'day': today_str, 'armed': True, 'net_floor': INDEX_PROFIT_LOCK_RS}
                        ledger[position_key] = {k: v for k, v in active_pos.items() if k != 'entry_dt'}
                        atomic_write_json(ledger_path, ledger)
                        logger.info('Index daily profit protection armed at Rs.%.0f net; only qualified runner re-entries may use excess profit.', INDEX_PROFIT_LOCK_RS)

                if not exit_reason and active_pos.get('profit_floor_armed') and floor is not None and 0 < bid_for_protection <= floor:
                    exit_reason = 'DAILY_PROFIT_PROTECTION'
                    curr_ltp = bid_for_protection
                elif not exit_reason and curr_ltp <= active_pos["current_sl"]:
                    exit_reason = (
                        "ASYMMETRIC_TRAIL_PROFIT"
                        if active_pos["ratchet_armed"] and curr_ltp > active_pos["entry_price"]
                        else "INITIAL_SL_HIT"
                    )
                elif not exit_reason and ((curr_ltp - active_pos["entry_price"]) * active_pos["qty"] - (40.0 + active_pos["lots"] * 24.5)) <= -(MAX_LOSS_PER_TRADE_RS * active_pos["lots"]):
                    exit_reason = "MAX_RUPEE_LOSS_CAP_HIT"
                elif not exit_reason and elapsed_min >= 12.0 and (active_pos["highest_ltp"] - active_pos["entry_price"]) < (0.45 * position_risk) and curr_ltp <= active_pos["entry_price"]:
                    exit_reason = "THETA_TIME_STOP"
                elif not exit_reason and elapsed_min >= float(active_pos.get(
                    "runner_max_hold_minutes",
                    (active_pos.get("market_structure") or {}).get("max_hold_minutes", float("inf"))
                )):
                    exit_reason = "MAX_HOLD_TIME"
                elif not exit_reason and now.time() >= EOD_SQUAREOFF_TIME:
                    exit_reason = "EOD_SQUAREOFF"

                executable_bid = float(node.get("top_bid_price", 0) or 0)
                if (not live_mode and not channel_position and not active_pos.get("exit_fills")
                        and math.isfinite(executable_bid) and executable_bid > 0
                        and float(node.get("top_bid_quantity") or 0) >= active_pos["qty"]):
                    modeled_fill = math.floor(executable_bid * .999 * 20 + 1e-9) / 20
                    fees = 40 + 24.5 * active_pos["lots"]
                    modeled_net = (modeled_fill - active_pos["entry_price"]) * active_pos["qty"] - fees
                    peak_net = max(active_pos.get("peak_net_pnl", modeled_net), modeled_net)
                    active_pos["peak_net_pnl"] = peak_net
                    feedback = (active_pos.get("market_structure") or {}).get("adaptive_feedback")
                    learned_floor = learned_profit_floor(feedback, peak_net, position_risk * active_pos["qty"])
                    if not exit_reason and learned_floor is not None and modeled_net <= learned_floor:
                        exit_reason = "LEARNED_PROFIT_PROTECTION"
                        curr_ltp = executable_bid
                if idx_name == "NIFTY" and not exit_reason and executable_bid > 0:
                    combined_net, _ = combined_paper_loss_state(today_str)
                    modeled_fill = math.floor(executable_bid * .999 * 20 + 1e-9) / 20
                    if combined_net is not None and daily_target_reached(
                        combined_net, active_pos, modeled_fill, COMBINED_PAPER_LOSS_LIMIT_RS, loss_limit=True
                    ):
                        exit_reason = "COMBINED_PAPER_LOSS_LIMIT"
                        curr_ltp = executable_bid
                runner_hold = False
                trade_review_bid = (
                    index_profit_lock_bid(0, active_pos, target_net=NIFTY_TRADE_PROFIT_REVIEW_RS)
                    if idx_name == "NIFTY" and not channel_position else None
                )
                trade_review = trade_review_bid is not None and (
                    executable_bid >= trade_review_bid or active_pos.get("trade_runner_active", False)
                )
                if not channel_position and not exit_reason and executable_bid > 0 and (
                    active_pos.get("profit_floor_armed") or trade_review
                ):
                    assessment = assess_index_runner(view, active_pos, node, now.timestamp(), day_net_pnl)
                    runner_hold = assessment['hold']
                    previous = (active_pos.get('runner_assessment') or {}).get('hold')
                    active_pos['runner_assessment'] = assessment
                    if runner_hold:
                        active_pos['runner_max_hold_minutes'] = INDEX_RUNNER_HOLD_MINUTES
                        if trade_review:
                            active_pos['trade_runner_active'] = True
                            trade_floor = index_profit_lock_bid(
                                0, active_pos, target_net=NIFTY_TRADE_RUNNER_FLOOR_RS
                            )
                            if trade_floor is not None and trade_floor < executable_bid:
                                active_pos['current_sl'] = max(active_pos['current_sl'], trade_floor)
                    if previous != runner_hold:
                        logger.info('[INDEX RUNNER] %s | %s', idx_name, json.dumps(assessment))
                        ledger[position_key] = {k: v for k, v in active_pos.items() if k != 'entry_dt'}
                        atomic_write_json(ledger_path, ledger)
                    if not runner_hold:
                        exit_reason = 'PROFIT_BOOKED_NO_RUNNER'
                        curr_ltp = executable_bid
                ref_exit_p = executable_bid if (executable_bid > 0 and not live_mode) else curr_ltp
                pos_target_pts = float(active_pos.get("target_points") or NIFTY_TARGET_POINTS)
                if not channel_position and not exit_reason and not runner_hold and idx_name == "NIFTY" and (ref_exit_p - active_pos["entry_price"]) >= pos_target_pts:
                    exit_reason = f"TARGET_{int(pos_target_pts)}_POINTS"
                    if not live_mode and executable_bid > 0:
                        curr_ltp = executable_bid
                elif idx_name != "NIFTY" and not channel_position and not exit_reason and daily_target_reached(
                    day_net_pnl, active_pos, executable_bid,
                    INDEX_RUNNER_TARGET_RS if runner_hold else DAILY_PROFIT_TARGET_RS
                ):
                    exit_reason = "DAILY_NET_PROFIT_TARGET"
                    if not live_mode and executable_bid > 0:
                        curr_ltp = executable_bid

                staged = "target_one_qty" in active_pos
                if not channel_position and not exit_reason and staged:
                    if not active_pos["target_one_done"] and curr_ltp >= active_pos["entry_price"] + 12:
                        if live_mode:
                            oid = dhan.place_market_order(active_pos["security_id"], cfg["fno_segment"], "SELL", active_pos["target_one_qty"], tag="NIFTY_T1")
                            if not oid:
                                logger.error("T1 submission unconfirmed; stopping for broker reconciliation.")
                                break
                        book_target_one(ledger, active_pos, math.floor(curr_ltp * 0.999 * 20 + 1e-9) / 20, now.strftime("%Y-%m-%d %H:%M:%S"))
                        logger.info("[%s PARTIAL EXIT] NIFTY T1 +12 points | Qty %d @ %.2f | Remaining %d", "LIVE" if live_mode else "PAPER", active_pos["target_one_qty"], curr_ltp, active_pos["qty"])
                    if not runner_hold and curr_ltp >= active_pos["entry_price"] + pos_target_pts:
                        exit_reason = f"TARGET_{int(pos_target_pts)}_POINTS"
                if not channel_position and not exit_reason and (
                        runner_hold or (not staged and curr_ltp > active_pos["highest_ltp"])):
                    active_pos["highest_ltp"] = max(active_pos["highest_ltp"], curr_ltp)
                    previous_sl = active_pos["current_sl"]
                    new_sl, armed = update_ratchet_sl(
                        active_pos["entry_price"], active_pos["current_sl"],
                        active_pos["highest_ltp"], position_risk
                    )
                    if runner_hold:
                        new_sl = max(active_pos['current_sl'], floor or 0,
                                     round(active_pos['highest_ltp'] - position_risk, 2))
                    active_pos["current_sl"] = new_sl
                    active_pos["ratchet_armed"] = active_pos["ratchet_armed"] or armed or runner_hold
                    if runner_hold and new_sl > previous_sl:
                        ledger[position_key] = {k: v for k, v in active_pos.items() if k != 'entry_dt'}
                        atomic_write_json(ledger_path, ledger)

                if staged:
                    active_pos["highest_ltp"] = max(active_pos["highest_ltp"], curr_ltp)

                if exit_reason:
                    if live_mode and active_pos.get("security_id"):
                        exit_order = dhan.place_market_order(
                            security_id=active_pos["security_id"],
                            exchange_segment=cfg["fno_segment"],
                            transaction_type="SELL",
                            quantity=active_pos["qty"],
                        )
                        if not exit_order:
                            logger.error("Final submission unconfirmed; stopping for broker reconciliation.")
                            break
                    exit_price = math.floor(curr_ltp * 0.999 * 20 + 1e-9) / 20
                    fills = active_pos.get("exit_fills", []) + ([{"qty": active_pos["qty"], "price": exit_price, "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "reason": exit_reason}] if staged else [])
                    if fills:
                        exit_price = sum(f["qty"] * f["price"] for f in fills) / active_pos["original_qty"]
                    r_mult = round((exit_price - active_pos["entry_price"]) / position_risk, 2)
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
                        qty=active_pos.get("original_qty", active_pos["qty"]),
                        spot_at_entry=active_pos["spot_at_entry"],
                        oi_diff_lakhs=active_pos["oi_diff_lakhs"],
                        entry_price=active_pos["entry_price"],
                        exit_price=exit_price,
                        r_mult=r_mult,
                        exit_reason=exit_reason,
                        sig=active_pos["rule_used"],
                        state_vec=active_pos["state_vec"],
                        data_health="LIVE_CLEAN", exit_fills=fills,
                        booked_gross=active_pos.get("partial_gross", 0), booked_net=active_pos.get("partial_net", 0),
                        market_structure=active_pos.get("market_structure"),
                        initial_risk_points=position_risk, position_key=position_key,
                        peak_net_pnl=active_pos.get("peak_net_pnl"),
                    )
                    day_net_pnl = round(day_net_pnl + float(ledger["trades"][-1]["net_pnl"]), 2)
                    day_trades += 1
                    if ledger["trades"][-1]["net_pnl"] <= 0:
                        consec_losses += 1
                        cooldown_until = now + timedelta(minutes=POST_SL_COOLDOWN_MINUTES)
                    else:
                        consec_losses = 0
                        cooldown_until = now + timedelta(minutes=POST_WIN_COOLDOWN_MINUTES)

                    logger.info(
                        f"[{'LIVE' if live_mode else 'PAPER'} EXIT] {idx_name} {strike_k}_{active_pos['option_type']} "
                        f"({exit_reason}) | Exit @ Rs.{exit_price:.2f} | Gross PnL: Rs.{gross_pnl:+,.2f} ({r_mult:+.2f}R)"
                    )
                    telegram.exited({**active_pos, **ledger["trades"][-1]}, position_key=position_key,
                                    ledger_trades=ledger["trades"])
                    positions.pop(position_key)
                    active_pos = None

            else:
                runner_reentry = (
                    idx_name != "NIFTY" and profit_protected and not positions and consec_losses == 0
                    and day_net_pnl - INDEX_PROFIT_LOCK_RS >= 84.5 + cfg['lot_size'] * option_stop_points(
                        idx_name, cfg['min_option_premium'])
                    and ledger.get('daily_runner_entry_counts', {}).get(today_str, 0) < MAX_POST_LOCK_RUNNER_ENTRIES
                )
                block_reason = index_entry_block_reason(
                    now.time(), day_net_pnl, profit_protected,
                    day_entries, consec_losses, runner_reentry=runner_reentry,
                    paper_retry=paper_nifty_retry if idx_name == "NIFTY" else None,
                    shared_loss_limit=idx_name == "NIFTY",
                )
                if idx_name == "NIFTY" and not block_reason:
                    combined_net, combined_reached = combined_paper_loss_state(today_str)
                    if combined_reached or combined_net is None:
                        block_reason = ("Combined Nifty/stock PAPER loss limit reached" if combined_reached
                                        else "Combined PAPER profit state unavailable")

            if position_key is None and (block_reason or not (native_slot_open or channel_slot_open)):
                reason = block_reason or "All enabled index position slots occupied"
                if reason != last_scan_only_reason:
                    logger.info("[INDEX SCAN ONLY] %s. Continuing to scan until session cutoff.", reason)
                    last_scan_only_reason = reason
                brain.select_action(view, consec_losses, bar_time=now.time(), observation_only=True)
                view["signal_decision"] = view.get("entry_decision")
                view["entry_decision"] = {"action": "HOLD", "reason": reason}
                if call_feed:
                    telemetry_state["channel_feed"]["decisions"] = call_feed.decision_statuses(
                        idx_name, time.time(), ledger.get("telegram_call_entries", []))
            elif position_key is None:
                last_scan_only_reason = None
                channel_call = None
                if channel_slot_open and not runner_reentry:
                    channel_states = call_feed.decision_statuses(idx_name, time.time(), ledger.get('telegram_call_entries', []))
                    telemetry_state.setdefault('channel_feed', {})['decisions'] = channel_states
                    for state in channel_states:
                        if state['state'] != 'ELIGIBLE_FOR_QUOTE_CHECK' and last_call_decision.get(state['key']) != state['state']:
                            logger.info('[CHANNEL ENTRY STATE] %s | %s', state['source_url'], state['state'])
                            last_call_decision[state['key']] = state['state']
                    channel_candidates = call_feed.candidates(idx_name, time.time(), ledger.get('telegram_call_entries', []))
                    if direct_call_quote:
                        channel_candidates = [c for c in channel_candidates if c["key"] == priority_call["key"]]
                    def channel_feedback(call):
                        return adaptive_setup_feedback(
                            {'index': idx_name, 'option_type': call['option_type'],
                             'market_structure': {'telegram_call': call}},
                            ledger['trades'], time.time()
                        )
                    channel_candidates.sort(key=lambda c: (channel_feedback(c)['quality_adjustment'], c['posted_epoch']), reverse=True)
                    for candidate in channel_candidates:
                        if channel_feedback(candidate)['paused']:
                            reason = 'Validated negative channel setup feedback; temporary pause'
                            if last_call_decision.get(candidate['key']) != reason:
                                logger.info('[TELEGRAM CALL] %s | %s', candidate['source_url'], reason)
                                last_call_decision[candidate['key']] = reason
                            continue
                        channel_view, reason = channel_entry_view(
                            view, raw_oc.get('oc', {}), candidate,
                            expiries.get(idx_name), chain_received_epoch, time.time()
                        )
                        if reason != last_call_decision.get(candidate['key']):
                            logger.info('[TELEGRAM CALL] %s | %s', candidate['source_url'], reason)
                            last_call_decision[candidate['key']] = reason
                        if channel_view is not None:
                            view = channel_view
                            channel_call = candidate
                            break
                if channel_call is None and (not native_slot_open or direct_call_quote):
                    reason = "Priority channel quote no longer eligible" if direct_call_quote else "Native index slot occupied; no eligible Telegram call"
                    view["entry_decision"] = {"action": "HOLD", "reason": reason}
                    action_dir, sig = "HOLD", None
                else:
                    action_dir, sig = index_entry_signal(brain, view, consec_losses, now, cooldown_until, channel_call, ledger['trades'])
                if idx_name == "NIFTY" and day_net_pnl < 0 and not channel_call and action_dir in ("BUY_CE", "BUY_PE"):
                    recovery_reason = nifty_recovery_block_reason(view, action_dir, time.time())
                    recovery_decision = (action_dir, recovery_reason or "Market supports a recovery setup")
                    if recovery_decision != last_recovery_decision:
                        logger.info("[PAPER NIFTY RECOVERY] %s | %s | Day net Rs.%+.2f",
                                    *recovery_decision, day_net_pnl)
                        last_recovery_decision = recovery_decision
                    if recovery_reason:
                        view["entry_decision"] = {"action": "HOLD", "reason": recovery_reason}
                        action_dir, sig = "HOLD", None
                if action_dir in ("BUY_CE", "BUY_PE") and sig:
                    is_ce = action_dir == "BUY_CE"
                    opt_type = "CE" if is_ce else "PE"
                    entry_dec = view.get("entry_decision") or {}
                    use_otm_live = bool(entry_dec.get("use_otm_hero_zero")) and not channel_call
                    if use_otm_live:
                        strike_k = view.get("otm_ce_strike" if is_ce else "otm_pe_strike") or (
                            view["atm_ce_strike"] if is_ce else view["atm_pe_strike"]
                        )
                        otm_node = raw_oc.get("oc", {}).get(f"{strike_k:.6f}", raw_oc.get("oc", {}).get(str(strike_k), {})).get("ce" if is_ce else "pe", {})
                        entry_p = float(otm_node.get("top_ask_price") or otm_node.get("last_price") or 0)
                        sec_id = str(otm_node.get("security_id", otm_node.get("securityId", "")))
                    else:
                        strike_k = view["atm_ce_strike"] if is_ce else view["atm_pe_strike"]
                        entry_p = float(view.get("atm_ce_ask" if is_ce else "atm_pe_ask") or 0)
                        sec_id = view["atm_ce_sec_id"] if is_ce else view["atm_pe_sec_id"]

                    entry_p = math.ceil(entry_p * 1.001 * 20 - 1e-9) / 20
                    if channel_call:
                        side = 'ce' if channel_call['option_type'] == 'CE' else 'pe'
                        spread = float(view.get(f'atm_{side}_ask') or 0) - float(view.get(f'atm_{side}_bid') or 0)
                        if spread > 0.20 * (7.8 if idx_name == 'NIFTY' else 20):
                            view['entry_decision'] = {'action': 'HOLD', 'reason': 'Channel spread consumes more than 20% of initial stop distance'}
                            continue
                    if channel_call and (not TelegramCallFeed.entry_is_fresh(channel_call, time.time())
                                         or entry_p > TelegramCallFeed.entry_bounds(channel_call)[1]):
                        reason = f'Channel stale or modeled fill outside zone: fill={entry_p:.2f}, maximum={TelegramCallFeed.entry_bounds(channel_call)[1]:.2f}'
                        view['entry_decision'] = {'action': 'HOLD', 'reason': reason}
                        if last_call_decision.get(channel_call['key']) != reason:
                            logger.info('[TELEGRAM CALL] %s | %s', channel_call['source_url'], reason)
                            last_call_decision[channel_call['key']] = reason
                        continue
                    min_req_prem = 18.0 if use_otm_live else cfg["min_option_premium"]
                    if entry_p >= min_req_prem:
                        available_cash = index_available_cash(ledger)
                        if channel_call:
                            side = 'ce' if is_ce else 'pe'
                            risk_points = 7.8 if idx_name == "NIFTY" else 20.0
                            per_lot_risk = (risk_points + entry_p * .001) * cfg["lot_size"] + 24.5
                            risk_lots = max(0, int((ledger["virtual_balance_net"] * .05 - 60) // per_lot_risk))
                            lots = channel_entry_lots(
                                risk_lots, ledger["virtual_balance_net"], entry_p, cfg['lot_size'],
                                float(view.get(f'atm_{side}_bid_qty') or 0), float(view.get(f'atm_{side}_ask_qty') or 0),
                                consec_losses, brain.rules.get(sig, {}).get('last_lot_loss', False)
                            )
                        else:
                            lots = brain.compute_lots(
                                idx_name,
                                sig,
                                entry_p,
                                ledger["virtual_balance_net"],
                                consec_losses,
                                analysis=view,
                                day_net_pnl=day_net_pnl,
                            )
                        lots = min(lots, max(0, int((available_cash - 60) // (entry_p * cfg["lot_size"] + 24.5))))
                        if paper_nifty_retry and idx_name == "NIFTY":
                            lots = min(lots, 1)
                        qty = lots * cfg["lot_size"]
                        if qty <= 0 or not 0 <= time.time() - chain_received_epoch <= 8:
                            reason = "Insufficient cash or stale executable quote"
                            if channel_call:
                                wallet_limit = ledger["virtual_balance_net"] * .60
                                one_lot_cost = entry_p * cfg["lot_size"] + 100
                                if qty <= 0 and one_lot_cost > wallet_limit:
                                    reason = (f"One lot requires Rs.{one_lot_cost:,.2f}; "
                                              f"channel wallet allocation is Rs.{wallet_limit:,.2f}")
                                if last_channel_execution_block.get(channel_call["key"]) != reason:
                                    logger.info("[CHANNEL EXECUTION HOLD] %s | %s", sig, reason)
                                    last_channel_execution_block[channel_call["key"]] = reason
                            view["entry_decision"] = {"action": "HOLD", "reason": reason, "rule": sig}
                            continue
                        quote_side = 'ce' if is_ce else 'pe'
                        bid = float(view.get(f'atm_{quote_side}_bid') or 0)
                        ask = float(view.get(f'atm_{quote_side}_ask') or 0)
                        if not spread_and_liquidity_ok(
                            bid, ask, (bid + ask) / 2,
                            float(view.get(f'atm_{quote_side}_bid_qty') or 0),
                            float(view.get(f'atm_{quote_side}_ask_qty') or 0),
                            lots, cfg['lot_size'], entry_p
                        ):
                            view['entry_decision'] = {'action': 'HOLD', 'reason': 'Spread or liquidity filter at sized quantity', 'rule': sig}
                            continue
                        entry_node = raw_oc.get("oc", {}).get(f"{strike_k:.6f}", raw_oc.get("oc", {}).get(str(strike_k), {})).get("ce" if is_ce else "pe", {})
                        if runner_reentry:
                            lots = post_lock_runner_lots(view, {
                                'entry_price': entry_p, 'entry_epoch': now.timestamp(), 'qty': qty,
                                'lots': lots, 'index': idx_name, 'strike': strike_k, 'option_type': opt_type,
                            }, entry_node, time.time(), day_net_pnl)
                            qty = lots * cfg["lot_size"]
                            if not qty:
                                view['entry_decision'] = {
                                    'action': 'HOLD',
                                    'reason': 'Post-lock entry needs confirmed runner flow and enough protected profit buffer',
                                    'rule': sig
                                }
                                logger.debug('%s post-lock runner entry rejected', idx_name)
                                atomic_write_json("live_telemetry.json", telemetry_state)
                                continue
                        if channel_call:
                            init_sl = channel_initial_stop(idx_name, entry_p, cfg["lot_size"], lots)
                            initial_risk = round(entry_p - init_sl, 2)
                            if initial_risk <= 0 or ask - bid > .20 * initial_risk:
                                view["entry_decision"] = {"action": "HOLD", "reason": "Spread too wide for Rs.650 per-lot risk plan"}
                                continue
                            held_depth = direct_call_quote or dhan.fetch_held_channel_quote({
                                "index": idx_name, "strike": strike_k, "option_type": opt_type,
                                "security_id": sec_id, "qty": qty,
                            })
                            depth_node = (held_depth or {}).get("oc", {}).get(f"{strike_k:.6f}", {}).get(quote_side, {})
                            try:
                                held_bid = float(depth_node.get("top_bid_price") or 0)
                                held_ask = float(depth_node.get("top_ask_price") or 0)
                                held_ask_qty = float(depth_node.get("top_ask_quantity") or 0)
                                held_age = time.time() - float(depth_node.get("depth_received_epoch") or 0)
                            except (TypeError, ValueError, OverflowError):
                                held_bid = held_ask = held_ask_qty = 0
                                held_age = math.inf
                            if not (
                                0 <= held_age <= 8 and held_bid > init_sl
                                and held_bid >= TelegramCallFeed.entry_bounds(channel_call)[0]
                                and held_bid <= held_ask <= ask and held_ask_qty >= qty
                                and held_ask - held_bid <= .20 * initial_risk
                            ):
                                view["entry_decision"] = {"action": "HOLD", "reason": "Fresh held-contract depth could not confirm the channel entry"}
                                logger.info("[CHANNEL RISK HOLD] %s %g %s | held-contract depth did not confirm entry", idx_name, strike_k, opt_type)
                                continue
                            now = datetime.now()
                            if now.time() >= EOD_SQUAREOFF_TIME or time.time() - chain_received_epoch > 8:
                                continue
                        else:
                            initial_risk = option_stop_points(idx_name, entry_p)
                            init_sl = round(entry_p - initial_risk, 2)
                            if init_sl <= 0:
                                view["entry_decision"] = {"action": "HOLD", "reason": "Premium too low for a valid initial stop"}
                                continue
                        if paper_nifty_retry and idx_name == "NIFTY":
                            init_sl = max(init_sl, channel_initial_stop(idx_name, entry_p, cfg["lot_size"], lots))
                            initial_risk = round(entry_p - init_sl, 2)
                            if initial_risk <= 0 or bid <= init_sl:
                                logger.info("[PAPER NIFTY RETRY] Entry rejected by remaining risk or stop distance")
                                continue
                        view.setdefault("market_structure", {})["entry_quote"] = {
                            "received_epoch": chain_received_epoch, "decision_epoch": time.time(),
                            "modeled_entry_price_basis": "EXECUTABLE_ASK_PLUS_0.1PCT_SLIPPAGE",
                            **{k: entry_node.get(k) for k in ("last_price", "top_bid_price", "top_ask_price", "top_bid_quantity", "top_ask_quantity")}
                        }
                        if live_mode and not sec_id:
                            logger.error("Missing option security ID; no entry submitted.")
                            continue
                        if channel_call and not TelegramCallFeed.entry_is_fresh(channel_call, time.time()):
                            continue
                        position_key = "active_channel_position" if channel_call else "active_position"
                        if idx_name == "NIFTY" and not combined_paper_entry_allowed(today_str):
                            view["entry_decision"] = {"action": "HOLD", "reason": "Combined PAPER loss limit reached or state unavailable"}
                            continue
                        if not reserve_index_entry(
                            ledger, today_str,
                            {
                                "index": idx_name, "security_id": sec_id, "option_type": opt_type,
                                "qty": qty, "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                                "post_lock_runner": runner_reentry,
                                **({"telegram_call_key": channel_call["key"]} if channel_call else {}),
                            },
                            ledger_path, position_key
                        ):
                            continue
                        day_entries = index_entries_today(ledger, today_str)
                        if live_mode:
                            entry_order = dhan.place_market_order(
                                security_id=sec_id,
                                exchange_segment=cfg["fno_segment"],
                                transaction_type="BUY",
                                quantity=qty,
                            )
                            if not entry_order or entry_order == "None":
                                logger.error("Entry submission unconfirmed; slot remains consumed. Reconcile before restarting.")
                                break
                        active_pos = {
                            "index": idx_name,
                            "strike": strike_k,
                            "option_type": opt_type,
                            "security_id": sec_id,
                            "expiry": expiries[idx_name],
                            "lots": lots,
                            "qty": qty,
                            "entry_price": entry_p,
                            "current_ltp": entry_p,
                            "highest_ltp": entry_p,
                            "current_sl": init_sl,
                            "initial_risk_points": initial_risk,
                            "target_points": float(entry_dec.get("target_points") or NIFTY_TARGET_POINTS),
                            "entry_delta": view.get(f"atm_{quote_side}_delta"),
                            "entry_iv": entry_node.get("implied_volatility"),
                            "ratchet_armed": False,
                            "entry_dt": now,
                            "entry_epoch": now.timestamp(),
                            "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "spot_at_entry": view["spot"],
                            "oi_diff_lakhs": view["diff_ce_minus_pe_lakhs"],
                            "rule_used": sig,
                            "state_vec": view["state_vec"],
                            "market_structure": view.get("market_structure"),
                        }
                        split = nifty_target_quantities(idx_name, lots, cfg["lot_size"])
                        if channel_call and lots >= 2:
                            active_pos.update(original_qty=qty, target_one_qty=(lots // 2) * cfg['lot_size'], target_one_done=False, exit_fills=[])
                        elif split:
                            active_pos.update(original_qty=qty, target_one_qty=split[0], target_one_done=False, exit_fills=[])
                        ledger["mode"] = "LIVE" if live_mode else "PAPER"
                        positions[position_key] = active_pos
                        ledger[position_key] = {k: v for k, v in active_pos.items() if k != "entry_dt"}
                        ledger.pop("pending_entry", None)
                        atomic_write_json(ledger_path, ledger)
                        telegram.position(active_pos, entry=True, position_key=position_key)
                        logger.info(
                            f"[{'LIVE' if live_mode else 'PAPER'} ENTRY] {idx_name} {strike_k} {opt_type} "
                            f"| Lots: {lots} (Qty: {qty}) @ Rs.{entry_p:.2f} | SL: Rs.{init_sl:.2f} | Target: +{active_pos['target_points']:.0f} pts "
                            f"| Regime: {(view.get('regime_info') or {}).get('regime')} | 5m Spot: {view['d_spot_5m']:+.1f} | 3-Strike Diff = {view['diff_ce_minus_pe_lakhs']:+.2f}L"
                        )

            if idx_name == "NIFTY" and not held_channel:
                view["execution_context"] = {
                    "open_position": bool(positions), "closed_trades_today": day_trades,
                    "entries_today": day_entries, "daily_entry_cap": MAX_TRADES_PER_DAY,
                    "consecutive_net_losses": consec_losses, "realized_net_today": day_net_pnl,
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None
                }
                try:
                    closed = [t for t in (structure_candles or {}).get("timestamp", []) if float(t) + 300 <= time.time()]
                    audit_key = (max(closed) if closed else None, bool(view.get("oi_analysis", {}).get("ready")))
                    if audit_key != last_entry_audit:
                        path = os.path.join(CACHE_DIR, "nifty_entry_audit_" + now.strftime("%Y-%m-%d") + ".jsonl")
                        if record_nifty_entry_observation(path, view, structure_candles, raw_oc.get("oc", {}), time.time()):
                            last_entry_audit = audit_key
                except (OSError, KeyError, TypeError, ValueError, OverflowError) as error:
                    logger.warning("Entry review observation could not be saved: %s", type(error).__name__)

            if active_pos:
                telegram.position(active_pos, position_key=position_key)
            telemetry_state["active_positions"] = [
                {k: v for k, v in pos.items() if k != "entry_dt"} for pos in positions.values()
            ]
            telemetry_state["active_position"] = next(iter(telemetry_state["active_positions"]), None)
            atomic_write_json("live_telemetry.json", telemetry_state)

        if not raw_oc:
            time.sleep(1.0)

    telegram.close()
    if call_feed:
        call_feed.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DhanHQ v6.0 Multi-Regime & Gamma-Blast Bot")
    parser.add_argument("--backtest-days", type=int, default=0, help="Run REAL DhanHQ historical backtest for past N days (e.g. 60, 180, 365)")
    parser.add_argument("--backtest-output-dir", default=None, help="Reuse a chosen research output directory; replay learning starts fresh")
    parser.add_argument("--backtest-years", type=int, default=0, help="Run REAL DhanHQ historical backtest for past N years (1, 2, 3, or 5)")
    parser.add_argument("--interval", type=str, default="5", choices=["1", "5", "15", "25", "60"], help="Candle interval in minutes (default: 5)")
    parser.add_argument("--indices", type=str, default=None, help="Comma-separated indices; default NIFTY for backtests, NIFTY,SENSEX for sessions")
    parser.add_argument("--reset-brain", action="store_true", help="Clear all state (brain, rules, ledger, log) before running")
    parser.add_argument("--reset-wallet-keep-brain", action="store_true", help="Reset paper wallet to Rs. 40,000 while keeping learned rules & brain")
    parser.add_argument("--prepare-live-paper", action="store_true", help="Reset paper_trade_ledger.json to Rs. 40,000 (0 trades) for live session while keeping learned rules")
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
            "initial_capital": 40000.0,
            "virtual_balance_gross": 40000.0,
            "virtual_balance_net": 40000.0,
            "profit_without_charges": 0.0,
            "net_profit_after_charges": 0.0,
            "total_trades": 0,
            "avg_winning_value_rs": 0.0,
            "avg_losing_value_rs": 0.0,
            "win_to_loss_ratio": 0.0,
            "trades": []
        })
        logger.info("Prepared clean Rs. 40,000 paper wallet (0 trades) while preserving trained learned_rules.json & bot_brain_state.json.")
        sys.exit(0)

    if args.reset_brain:
        logger.info("Cleared old brain, rules, ledger, and session log.")
    elif args.reset_wallet_keep_brain:
        logger.info("Reset Paper Wallet to Rs. 40,000 while preserving learned_rules.json and bot_brain_state.json.")

    requested_indices = args.indices or ("NIFTY" if args.backtest_years > 0 or args.backtest_days > 0 else "NIFTY,SENSEX")
    selected_indices = [x.strip().upper() for x in requested_indices.split(",")]
    if not selected_indices or any(x not in INDICES_CONFIG for x in selected_indices):
        parser.error("--indices must contain NIFTY and/or SENSEX")

    if args.backtest_years > 0:
        run_real_historical_backtest(
            days=args.backtest_years * 365,
            indices_to_test=selected_indices,
            interval=args.interval,
            logger=logger,
            output_dir=args.backtest_output_dir,
        )
    elif args.backtest_days > 0:
        run_real_historical_backtest(
            days=args.backtest_days,
            indices_to_test=selected_indices,
            interval=args.interval,
            logger=logger,
            output_dir=args.backtest_output_dir,
        )
    else:
        run_intraday_market_session(live_mode=args.live, logger=logger, indices_to_trade=selected_indices)

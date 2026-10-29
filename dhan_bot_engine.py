#!/usr/bin/env python3
"""
DhanHQ v4.9.1 Final Production Real-Data Backtest & Live Paper/Order Engine
===========================================================================
Upgrades in v4.9.2 (requested entry and exit experiment):
0. Structure quality floors, impulse 0.95R, later trail and 15-minute stagnation exit.
Upgrades in v4.9.1 (Based on Oct 19 Live Session Audit):
1. Restored `MarketMicrostructureAnalyzer.evaluate_chain` method for live Option Chain parsing.
2. User trading window 09:15-14:45 IST:
   - `LAST_ENTRY_TIME = dtime(14, 44)` and `EOD_SQUAREOFF_TIME = dtime(14, 45)`.
3. Zero HTTP 429 Rate-Limit Flooding:
   - Stamps `self.last_oc_ts = time.time()` AFTER each HTTP response (minimum 3.6s gap).
   - Automatically backs off 15s and widens polling interval if Dhan returns HTTP 429 (805).
   - Immediately stops Dhan API polling once daily entry limit (8), consecutive loss breaker (2),
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

from contextlib import nullcontext
from itertools import islice
from stock_option_policy import option_chain_request_slot, single_bot_session
import requests

from telegram_notifier import TelegramNotifier, TelegramCallFeed
from nifty_market_structure import apply_nifty_level_entry
from index_option_replay import replay_option_trade, ReplayDataError, fixed_contract_bars
from nifty_oi import NiftyOITracker, IST

if __name__=="__main__":
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
        "sl_points": 6.15,           # Floor; Nifty entry stop scales to 8.5%, capped at 12.50.
        "min_option_premium": 45.0,  # Blocks low-delta / high-theta decay traps < Rs. 45
        "min_spot_impulse": 8.5,
        "max_spot_impulse": 28.5,    # Blocks exhaustion spikes > 28.5 pts
        "wall_threshold": 5_000_000  # Nifty: 50 Lakhs OI difference
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
        "wall_threshold": 4_000_000  # Sensex: 40 Lakhs OI difference
    },
}

PRUNE_COOLDOWN_DAYS = 5
MAX_PRUNE_CYCLES = 3
DAILY_PROFIT_TARGET_RS = 2500.0
INDEX_PROFIT_LOCK_RS = 1000.0
INDEX_RUNNER_TARGET_RS = 3000.0
NIFTY_TARGET_POINTS = 20.0
MIN_OI_WALL_ROOM_POINTS = 35.0
MIN_STRUCTURE_ROOM_POINTS = 25.0
MIN_STRUCTURE_RR = 1.35
MIN_STRUCTURE_RR_AFTER_LOSS = 2.10
MIN_STRUCTURE_RR_RETEST = 2.20
MAX_TRADES_PER_DAY = 8
MAX_CONSECUTIVE_LOSSES_PER_DAY = 2
POST_SL_COOLDOWN_MINUTES = 15
POST_WIN_COOLDOWN_MINUTES = 6
SESSION_START_TIME = dtime(9, 15)
LAST_ENTRY_TIME = dtime(14, 44)     # No new position at the 14:45 hard exit.
EOD_SQUAREOFF_TIME = dtime(14, 45)


def index_entries_today(ledger, day):
    """Count consumed entries, including an open or interrupted submission."""
    closed=sum(str(t.get("timestamp", "")).startswith(day) for t in ledger.get("trades", []))
    active=ledger.get("active_position") or {}
    pending=ledger.get("pending_entry") or {}
    outstanding=(str(active.get("entry_ts", "")).startswith(day) or pending.get("day")==day)
    saved=int(ledger.get("daily_entry_counts", {}).get(day, 0))
    if saved<0:
        raise ValueError("Invalid saved index daily entry count")
    return max(saved, closed+int(outstanding))


def reserve_index_entry(ledger, day, details, ledger_path="paper_trade_ledger.json"):
    """Persist a consumed slot before an order; ambiguous slots stay consumed."""
    count=index_entries_today(ledger, day)
    if count>=MAX_TRADES_PER_DAY or ledger.get("pending_entry") or ledger.get("active_position"):
        return False
    call_key=details.get('telegram_call_key')
    if call_key and call_key in ledger.get('telegram_call_entries',[]):
        return False
    token=f"{day}:{count+1}"
    ledger.setdefault("daily_entry_counts", {})[day]=count+1
    ledger["pending_entry"]={"day":day,"token":token,"details":details}
    if call_key:
        ledger['telegram_call_entries']=(ledger.get('telegram_call_entries',[])+[call_key])[-64:]
    atomic_write_json(ledger_path, ledger)
    # The JSON writer can exhaust Windows replace retries. Never submit an
    # order unless the reservation can actually be read from disk.
    saved=SelfLearningBrain._load_json(ledger_path, {})
    if saved.get("pending_entry", {}).get("token")!=token or index_entries_today(saved, day)<count+1:
        raise OSError("Index entry reservation was not persisted; order not submitted")
    if call_key and call_key not in saved.get('telegram_call_entries',[]):
        raise OSError('Channel entry identity was not persisted; no entry submitted')
    return True


def channel_entry_view(view, chain, call, current_expiry, received_epoch, now_epoch):
    """Follow fresh channel direction in PAPER, retaining executable-quote safeguards."""
    try:
        if not current_expiry:return None,'Current-expiry chain unavailable'
        if not 0<=now_epoch-call['posted_epoch']<=TelegramCallFeed.MAX_AGE:return None,'Channel call stale'
        if not 0<=now_epoch-received_epoch<=8:return None,'Channel contract quote stale'
        if call['index']!=view['index']:
            return None,'Channel index does not match requested index'
        node=next((v for k,v in chain.items() if float(k)==call['strike']),{}).get(call['option_type'].lower(),{})
        bid=float(node['top_bid_price']);ask=float(node['top_ask_price'])
        ltp=float(node['last_price']);greeks=node.get('greeks') or {}
        delta=float(greeks.get('delta',node.get('delta')))
        theta=float(greeks.get('theta',node.get('theta')))
        trigger=float(call['buy_above'])
        if not all(math.isfinite(v) for v in (bid,ask,ltp,delta,theta,trigger)) or not 0<bid<=ask:
            return None,'Channel contract quote or Greeks invalid'
        if not .25<=abs(delta)<=.70 or (delta>0)!=(call['option_type']=='CE'):
            return None,'Channel contract delta invalid'
        if bid<trigger:return None,'Channel buy-above trigger not executable yet'
        entry_cap=float(call.get('entry_max',trigger*1.02))
        if ask>entry_cap:return None,'Channel entry above allowed trigger/range'
        security_id=node.get('security_id',node.get('securityId'))
        if not security_id or not str(security_id).isdigit():return None,'Channel contract security ID unavailable'
        result=dict(view)
        result['market_structure']=dict(view.get('market_structure') or {})
        result['market_structure']['telegram_call']={**call,'resolved_expiry':current_expiry,
            'expiry_basis':'CURRENT_CHAIN',
            'stop_target_basis':'CHANNEL_TARGET_USER_FIXED_PREMIUM_STOP'}
        side=call['option_type'].lower()
        result['recommended_dir']='BUY_'+call['option_type']
        result.update({f'atm_{side}_strike':call['strike'],f'atm_{side}_sec_id':str(security_id),
                       f'atm_{side}_ltp':ltp,f'atm_{side}_delta':delta,f'atm_{side}_theta':theta})
        for field,source in (('bid','top_bid_price'),('ask','top_ask_price'),
                             ('bid_qty','top_bid_quantity'),('ask_qty','top_ask_quantity')):
            result[f'atm_{side}_{field}']=node[source]
        return result,'Fresh channel trigger matched; following channel direction'
    except (KeyError,TypeError,ValueError,OverflowError):
        return None,'Channel contract could not be verified'


def channel_entry_lots(native_lots, wallet, entry_price, lot_size, bid_qty, ask_qty, losses, last_lot_loss):
    """Channel sizing: up to three funded lots, independent of native rule sizing."""
    try:
        values=[float(v) for v in (wallet,entry_price,lot_size,bid_qty,ask_qty)]
        if not all(math.isfinite(v) and v>0 for v in values) or native_lots<=0:return 0
        available=min(3,int(wallet*.60//(entry_price*lot_size+100)),int(min(bid_qty,ask_qty)//lot_size))
        return max(0,available)
    except (TypeError,ValueError,OverflowError,ZeroDivisionError):return 0


def protect_channel_position(position, bid, now_epoch):
    """Track executable peaks and protect channel trades without assuming target fills."""
    call=(position.get('market_structure') or {}).get('telegram_call')
    if not call:return None
    entry=float(position['entry_price']);qty=int(position['qty'])
    if not math.isfinite(bid) or bid<=0 or qty<=0:return None
    position['highest_ltp']=max(float(position.get('highest_ltp',entry)),bid)
    targets=sorted(float(v) for v in call.get('targets',[]) if math.isfinite(float(v)) and float(v)>entry)
    fees=40+24.5*int(position['lots'])
    peak=position['highest_ltp']
    peak_fill=math.floor(peak*.999*20+1e-9)/20
    peak_net=(peak_fill-entry)*qty-fees
    distance=min(position['initial_risk_points'],targets[0]-entry) if targets else position['initial_risk_points']
    gain=peak-entry
    if gain>=0.5*distance and peak_net>0:
        protected_net=max(0,0.5*peak_net) if gain>=0.8*distance else 0
        needed=entry+(fees+protected_net)/qty
        floor=math.ceil(needed/.999*20-1e-9)/20
        position['current_sl']=max(position['current_sl'],floor)
        position['ratchet_armed']=True
    position['channel_exit_observation']={
        'epoch':now_epoch,'executable_bid':bid,'peak_bid':peak,
        'current_stop':position['current_sl'],'first_target':targets[0] if targets else None,
        'target_observed':bool(targets and bid>=targets[0])}
    call['exit_observation']=dict(position['channel_exit_observation'])
    if targets and bid>=targets[0]:return 'TELEGRAM_CHANNEL_TARGET'
    if bid<=position['current_sl']:
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
        self.history_events = []
        self.session.headers.update({
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _option_chain_has_data(chain):
        return isinstance(chain,dict) and any(
            isinstance(node,dict) and any(side in node for side in ("ce","pe"))
            for node in islice(chain.values(),10))

    def _enforce_gap(self, endpoint):
        if endpoint.startswith("/optionchain"):
            self._wait_oc_rate_limit()

    def _request(self, method, endpoint, **kwargs):
        self._enforce_gap(endpoint)
        shared = option_chain_request_slot() if endpoint.startswith("/optionchain") else nullcontext()
        with shared:
            response = self.session.request(method,self.BASE_URL+endpoint,timeout=kwargs.pop("timeout",5),**kwargs)
        if endpoint.startswith("/optionchain"):
            self.last_oc_ts=time.time()
        if response.status_code == 429:
            self.oc_poll_gap=min(12.0,self.oc_poll_gap+3.0)
            self.oc_retry_after=time.time()+self.oc_poll_gap
            self.logger.warning("Dhan rate limit: chain requests deferred; risk loop continues.")
            return None
        response.raise_for_status()
        return response.json()

    def _wait_oc_rate_limit(self) -> None:
        # The cross-process slot owns pacing; never sleep a separate 15-second
        # cooldown here while an option position needs supervision.
        return None

    def _wait_hist_rate_limit(self) -> None:
        elapsed = time.time() - self.last_hist_ts
        if elapsed < 3.2:
            time.sleep(3.2 - elapsed)
        self.last_hist_ts = time.time()

    def fetch_held_channel_quote(self,position):
        """Poll only held-contract depth; full-chain pacing cannot delay channel exits."""
        elapsed=time.monotonic()-getattr(self,'last_held_quote_request',0)
        if elapsed<4:time.sleep(4-elapsed)
        self.last_held_quote_request=time.monotonic()
        segment=INDICES_CONFIG[position['index']]['fno_segment']
        sid=str(position['security_id'])
        try:
            result=self._request('POST','/marketfeed/quote',json={segment:[int(sid)]},timeout=(2,3))
            quote=(result or {}).get('data',{}).get(segment,{}).get(sid,{})
            levels=[(float(v['price']),int(v['quantity'])) for v in quote.get('depth',{}).get('buy',[])]
            levels=sorted(((p,q) for p,q in levels if math.isfinite(p) and p>0 and q>0),reverse=True)
            remaining=position['qty'];value=0.0
            for price,quantity in levels:
                take=min(remaining,quantity);value+=price*take;remaining-=take
                if remaining==0:break
            if remaining or not levels:
                self.logger.warning('Held channel exit depth unavailable; no fill fabricated.')
                return None
            node={'last_price':quote.get('last_price'),'top_bid_price':value/position['qty'],
                  'top_bid_quantity':position['qty'],'security_id':sid,
                  'best_bid_price':levels[0][0],'depth_received_epoch':time.time()}
            asks=quote.get('depth',{}).get('sell') or []
            if asks:
                node.update(top_ask_price=asks[0].get('price'),top_ask_quantity=asks[0].get('quantity'))
            return {'oc':{f"{position['strike']:.6f}":{position['option_type'].lower():node}}}
        except (requests.RequestException,TypeError,ValueError,KeyError,OverflowError):
            self.logger.warning('Held channel quote request failed; exit awaiting fresh depth.')
            return None

    def fetch_nearest_expiry(self, security_id: int, segment: str = "IDX_I") -> Optional[str]:
        if time.time()<getattr(self,"oc_retry_after",0):return None
        try:
            body=self._request("POST","/optionchain/expirylist",json={"UnderlyingScrip":int(security_id),"UnderlyingSeg":segment})
            expiries=sorted((body or {}).get("data",[]))
            today=datetime.now(IST).strftime("%Y-%m-%d")
            return next((e for e in expiries if e>=today),None)
        except (requests.RequestException,ValueError,TypeError) as error:
            self.logger.warning("Expiry request failed: %s",type(error).__name__)
            return None

    def fetch_option_chain(self, security_id: int, segment: str, expiry: str) -> Optional[Dict[str, Any]]:
        if time.time()<getattr(self,"oc_retry_after",0):return None
        try:
            body=self._request("POST","/optionchain",json={"UnderlyingScrip":int(security_id),"UnderlyingSeg":segment,"Expiry":expiry})
            return (body or {}).get("data")
        except (requests.RequestException,ValueError,TypeError) as error:
            self.logger.warning("Option-chain request failed: %s",type(error).__name__)
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
                return str(order_id) if order_id else None
            else:
                self.logger.error(f"Live Order HTTP {resp.status_code}: {resp.text[:250]}")
        except requests.RequestException as e:
            self.logger.error(f"Live Order exception: {e}")
        return None

    def fetch_nifty_structure_candles(self, from_date, to_date, historical=False):
        """Actual index OHLC for level mapping, independently of rolling premiums."""
        def usable(candidate):
            if not isinstance(candidate,dict):return None
            data=candidate.get('data',candidate)
            if not isinstance(data,dict) or not data.get('timestamp'):return None
            if any(not isinstance(data.get(k),list) or len(data[k])!=len(data['timestamp'])
                   for k in ('open','high','low','close')):return None
            return data
        payload={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
                 "interval":"5","fromDate":from_date,"toDate":to_date}
        key=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        path=os.path.join(CACHE_DIR,f"nifty_structure_{key}.json")
        if historical and os.path.exists(path):
            try:
                with open(path,encoding="utf-8") as f:
                    cached=usable(json.load(f))
                    if cached:return cached
            except (OSError,ValueError):
                pass
        if not historical and time.time()-self.structure_received<30:
            return self.structure_snapshot
        data=None
        for attempt in range(2):
            self._wait_hist_rate_limit()
            try:
                response=self.session.post(f"{self.BASE_URL}/charts/intraday",json=payload,timeout=(5,15))
                if response.status_code==200:
                    data=usable(response.json())
                    if data:
                        if historical:atomic_write_json(path,data)
                        break
                else:
                    self.logger.warning("Nifty structure history unavailable (HTTP %s)",response.status_code)
                    if response.status_code not in (429,500,502,503,504):break
            except (requests.RequestException,ValueError):
                self.logger.warning("Nifty structure history request failed")
            if attempt==0:time.sleep(3.2)
        if data is None:
            self.history_events.append({'index':'NIFTY','from':from_date,'to_exclusive':to_date,
                                        'reason':'Index price structure history unavailable'})
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
        interval: str = "5", require_ohlc: bool = False,
    ) -> Optional[Dict[str, List[Any]]]:
        cache_key = f"{idx_name}_{strike_label}_{opt_type}_{interval}m_{from_date}_{to_date}"
        cache_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()[:12]
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}_{cache_hash}.json")

        def bounded(data):
            if not isinstance(data,dict) or not data.get("timestamp"):
                return None
            start=datetime.strptime(from_date,"%Y-%m-%d").replace(tzinfo=IST).timestamp()
            end=datetime.strptime(to_date,"%Y-%m-%d").replace(tzinfo=IST).timestamp()
            stamps=data["timestamp"]
            if require_ohlc and any(not isinstance(data.get(k),list) or len(data[k])!=len(stamps)
                                    for k in ('open','high','low','close','oi','iv','strike','spot')):
                return None
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
                    if cached.get("timestamp"):
                        usable=bounded(cached)
                        if usable:return usable
            except Exception:
                pass

        cfg = INDICES_CONFIG[idx_name]
        req_fields = ["open", "high", "low", "close", "oi", "iv", "strike", "spot"]
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
                        self.history_events.append({'index':idx_name,'side':opt_type,'relative_strike':strike_label,
                            'from':from_date,'to_exclusive':to_date,'reason':'Empty or unusable rolling history'})
                        self.logger.warning('No usable rolling history: %s %s %s (%s -> %s)',idx_name,strike_label,opt_type,from_date,to_date)
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
        self.session_anchors={}
        self.nifty_oi_tracker=NiftyOITracker()
        self.sensex_oi_tracker=NiftyOITracker(strike_step=100)
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
        snapshot_dt=datetime.fromtimestamp(live_ts if live_ts is not None else time.time(),IST)
        session_day=snapshot_dt.date().isoformat()
        anchor=self.session_anchors.get(idx_name)
        if anchor is None or anchor['day']!=session_day:
            anchor={
                'day':session_day,
                'spot':spot,
                'open_ce_oi':ce_oi,
                'open_pe_oi':pe_oi,
                'open_diff_oi':ce_oi-pe_oi,
                'open_ce_prem':ce_prem,
                'open_pe_prem':pe_prem,
                'high':spot,
                'low':spot,
                'vwap_num':spot,
                'vwap_den':1.0,
                'timestamp':snapshot_dt.isoformat()
            }
            self.session_anchors[idx_name]=anchor
            self.live_history[idx_name].clear()
            self.prev_bar.pop(idx_name,None)
        else:
            anchor['high']=max(anchor.get('high',spot),spot)
            anchor['low']=min(anchor.get('low',spot),spot)
            w=max(1.0, (ce_oi+pe_oi)/1_000_000.0)
            anchor['vwap_num']=anchor.get('vwap_num',spot)+spot*w
            anchor['vwap_den']=anchor.get('vwap_den',1.0)+w

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
            prev_snap=reference_snapshot
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

        session_vwap = anchor.get('vwap_num', spot) / max(anchor.get('vwap_den', 1.0), 1.0)
        cum_ce_build = ce_oi - anchor.get('open_ce_oi', ce_oi)
        cum_pe_build = pe_oi - anchor.get('open_pe_oi', pe_oi)
        cum_oi_shift = cum_pe_build - cum_ce_build  # >0 means net bullish Put writing since 09:15 AM
        cum_spot_delta = round(spot - anchor['spot'], 4)

        # Compute 15-minute (3-bar) rolling spot and premium trend
        hist_list = list(self.live_history[idx_name])
        refs_15m=[row for row in hist_list if 900<=current_snap['ts']-row['ts']<=1050]
        ref_15m=refs_15m[-1] if refs_15m else None
        d_spot_15m=round(spot-float(ref_15m['spot']),2) if ref_15m else 0.0

        bull_accum = (
            (raw_diff <= -cfg["wall_threshold"])
            or (raw_diff <= -1_800_000 and cum_oi_shift >= 800_000)
            or (raw_diff <= -1_200_000 and cum_spot_delta >= 20.0 and spot >= session_vwap)
        )
        bear_accum = (
            (raw_diff >= cfg["wall_threshold"])
            or (raw_diff >= 1_800_000 and cum_oi_shift <= -800_000)
            or (raw_diff >= 1_200_000 and cum_spot_delta <= -20.0 and spot <= session_vwap)
        )

        # Primary strict 50L quadrant controller (preserves exact unit test behavior)
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

        # Daily Autonomous Trend-Accumulation Detector (catches daily 20-pt Nifty moves even when
        # 5m bar is steady +6..+28 pts and structure is in open air between static levels)
        daily_trend_dir = "HOLD"
        if idx_name == "NIFTY" and prev_snap is not None:
            if (
                bull_accum
                and spot >= session_vwap - 10.0
                and (d_spot >= 6.0 or (d_spot >= 3.5 and d_spot_15m >= 12.0))
                and d_spot <= max_impulse
                and d_ce_prem > 0
                and (ce_q in ("SHORT_COVERING", "LONG_BUILDUP") or pe_q in ("SHORT_BUILDUP", "LONG_UNWINDING"))
            ):
                daily_trend_dir = "BUY_CE"
                if dom == "BALANCED_CHOP":
                    dom = "BULLS_DOMINATING"
                    ctrl = "MORNING_DELTA_PUT_ACCUMULATION"
            elif (
                bear_accum
                and spot <= session_vwap + 10.0
                and (d_spot <= -6.0 or (d_spot <= -3.5 and d_spot_15m <= -12.0))
                and d_spot >= -max_impulse
                and d_pe_prem > 0
                and (pe_q in ("SHORT_COVERING", "LONG_BUILDUP") or ce_q in ("SHORT_BUILDUP", "LONG_UNWINDING"))
            ):
                daily_trend_dir = "BUY_PE"
                if dom == "BALANCED_CHOP":
                    dom = "BEARS_DOMINATING"
                    ctrl = "MORNING_DELTA_CALL_ACCUMULATION"

        pcr = pe_oi / max(ce_oi, 1)
        delta_from_open=(spot-anchor['spot'])/step
        bar_minutes_since_open=(snapshot_dt.hour*60+snapshot_dt.minute+snapshot_dt.second/60)-(9*60+15)

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
            round(max(-1.0,min(1.0,(bar_minutes_since_open-120)/120)),4),
        ]

        return {
            "index": idx_name,
            "spot": spot,
            "d_spot_5m": round(d_spot, 2),
            "d_spot_15m":d_spot_15m,
            "trend_15m_ready":ref_15m is not None,
            "trend_15m_reference_age_seconds":current_snap['ts']-ref_15m['ts'] if ref_15m else None,
            "daily_trend_dir":daily_trend_dir,
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
            "state_schema":"SESSION_CONTEXT_10_V2",
            "session_anchor":dict(anchor),
            "session_open_spot":anchor['spot'],
            "session_high":anchor['high'],
            "session_low":anchor['low'],
            "session_vwap":round(session_vwap,4),
            "session_vwap_basis":"OI-weighted snapshot average; not volume VWAP",
            "cum_ce_oi_build":cum_ce_build,
            "cum_pe_oi_build":cum_pe_build,
            "cum_oi_shift":cum_oi_shift,
            "cum_spot_delta":round(spot-anchor['spot'],4),
            "cumulative_oi_basis":"Current strike-basket totals minus first-observed basket; strike rolls can affect totals",
            "delta_from_session_start":round(delta_from_open,4),
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
        reference,oi_info=(self.nifty_oi_tracker if idx_name=="NIFTY" else self.sensex_oi_tracker).assess(oc,spot,received,expiry)
        result=self.evaluate_snapshot(
            idx_name, spot, call_strikes, put_strikes,
            ce_oi, pe_oi, ce_prem, pe_prem, ce_iv, pe_iv,
            atm_ce_ltp, atm_pe_ltp, atm_ce_sec_id, atm_pe_sec_id,
            live_ts=received,reference_snapshot=reference,require_reference=idx_name=="NIFTY"
        )
        if oi_info is not None:result["oi_analysis"]=oi_info
        result['snapshot_received_epoch']=received
        result['days_to_expiry']=days_to_expiry(expiry,datetime.fromtimestamp(received,IST))
        for side,strike in (('ce',call_strikes[0]),('pe',put_strikes[0])):
            node=oc.get(f'{strike:.6f}',oc.get(str(strike),{})).get(side,{})
            for field,source in (('bid','top_bid_price'),('ask','top_ask_price'),
                                 ('bid_qty','top_bid_quantity'),('ask_qty','top_ask_quantity')):
                result[f'atm_{side}_{field}']=node.get(source)
            greeks=node.get('greeks') or {}
            for greek in ('delta','theta'):
                value=node.get(greek)
                if value is None:value=greeks.get(greek)
                if value is not None:result[f'atm_{side}_{greek}']=value
        return result


class SelfLearningBrain:
    def __init__(self, logger: logging.Logger, brain_file: str = "bot_brain_state.json", rules_file: str = "learned_rules.json", fresh: bool = False):
        self.logger = logger
        self.brain_file = brain_file
        self.rules_file = rules_file
        loader=(lambda path,default:default) if fresh else self._load_json
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
            return (datetime.strptime(str(reference), "%Y-%m-%d")-datetime.strptime(str(day_str), "%Y-%m-%d")).days
        except (TypeError, ValueError):
            return None

    @staticmethod
    def halve_rule_history(rule):
        rule["wins_count"]=math.ceil(int(rule.get("wins_count",0))/2)
        rule["losses_count"]=int(rule.get("losses_count",0))//2
        rule["trades_count"]=rule["wins_count"]+rule["losses_count"]
        for key in ("total_win_r","total_loss_r"):
            rule[key]=round(float(rule.get(key,0))/2,4)
        for label in ("win","loss"):
            count=rule["wins_count" if label=="win" else "losses_count"]
            rule["avg_"+label+"_r"]=round(rule["total_"+label+"_r"]/count,2) if count else 0.0
        wr=rule["wins_count"]/rule["trades_count"] if rule["trades_count"] else 0.0
        rule["expectancy_r"]=round(wr*rule["avg_win_r"]-(1-wr)*rule["avg_loss_r"],2)

    @staticmethod
    def mark_pruned(rule, day_str):
        rule["status"]="PRUNED"
        rule["pruned_on_day"]=day_str
        rule["prune_count"]=int(rule.get("prune_count",0))+1

    @staticmethod
    def has_wall_room(analysis):
        oi=analysis.get("oi_analysis")
        if not isinstance(oi,dict) or not oi.get("ready"):return True
        key="room_to_resistance" if analysis["recommended_dir"]=="BUY_CE" else "room_to_support"
        room=oi.get(key)
        if room is None:return True
        try:return float(room)>=MIN_OI_WALL_ROOM_POINTS
        except (TypeError,ValueError):return True

    def unmute_new_day(self, day_str: str) -> None:
        if getattr(self,"learning_day",None)==day_str:
            return
        self.learning_day=day_str
        self.recent_losing_states=[("BUY_CE" if t["action"]==1 else "BUY_PE",t["state"])
            for t in getattr(self,"brain",{}).get("replay_buffer",[])
            if t.get("day")==day_str and t.get("reward",0)<=-0.70
            and t.get("action") in (1,2) and len(t.get("state",[]))==10][-8:]
        for r in self.rules.values():
            if r.get("status")=="PRUNED":
                r.setdefault("pruned_on_day",day_str)
                r.setdefault("prune_count",1)
                age=self.calendar_days_since(r["pruned_on_day"],day_str)
                if age is not None and age>=PRUNE_COOLDOWN_DAYS and r["prune_count"]<MAX_PRUNE_CYCLES:
                    self.halve_rule_history(r)
                    r.update(status="CANDIDATE",intraday_losses=0,last_lot_loss=False)
                continue
            t_cnt = int(r.get("trades_count", 0))
            w_cnt = int(r.get("wins_count", 0))
            l_cnt = int(r.get("losses_count", 0))
            wr = (w_cnt / t_cnt) if t_cnt > 0 else 0.0
            days_n = len(r.get("unique_days_traded", []))
            exp_r = float(r.get("expectancy_r", 0.0))
            prune=t_cnt>=7 and days_n>=4 and wr<0.32
            promote=((days_n>=5 and t_cnt>=8 and exp_r>=0.35 and wr>=0.42)
                     or (days_n>=4 and w_cnt>=4 and l_cnt<=1 and exp_r>=1.0))

            if r.get("status") == "DAY_MUTED" and r.get("muted_on_day") != day_str:
                if prune:
                    self.mark_pruned(r,day_str)
                elif promote:
                    r["status"] = "ACTIVE"
                else:
                    r["status"] = "CANDIDATE"
            elif r.get("status") != "PRUNED":
                if prune:
                    self.mark_pruned(r,day_str)
                elif promote:
                    r["status"] = "ACTIVE"
            if r.get("last_trade_day")!=day_str:
                r["intraday_losses"] = 0
        atomic_write_json(self.rules_file, self.rules)

    def select_action(
        self, analysis: Dict[str, Any], consec_losses: int, bar_time: Optional[dtime] = None
    ) -> Tuple[str, Optional[str]]:
        def reject(reason,sig=None):
            analysis["entry_decision"]={"action":"HOLD","reason":reason,"rule":sig}
            self.logger.debug('%s entry rejected: %s',analysis['index'],reason)
            return "HOLD",sig
        ms = analysis.get("market_structure") or {}
        ms_allowed = bool(ms.get("allowed", False) and ms.get("strike_oi_confirmation"))
        level_entry = (
            analysis["index"] == "NIFTY"
            and analysis.get("entry_model") in ("LEVEL_CANDLE_AND_STRIKE_OI_V1","LEVEL_CANDLE_AND_STRIKE_OI_V2")
            and ms_allowed
        )
        breakout_entry=level_entry and analysis.get('entry_model')=='LEVEL_CANDLE_AND_STRIKE_OI_V2'
        if analysis['index']=='NIFTY':
            if not level_entry:
                return reject(str(ms.get('reason') or 'No confirmed structural level setup'))
            if not breakout_entry and not analysis.get('exceeds_50_lakhs'):
                return reject('Structural entry requires the 50-lakh OI wall')
            if not (analysis.get('oi_analysis') or {}).get('ready'):
                return reject('Structural entry requires verified three-strike OI reference')
        if analysis['recommended_dir']=='HOLD' or (not breakout_entry and not analysis.get('exceeds_50_lakhs')):
            return reject('No confirmed directional setup')

        # CRITICAL CONFLUENCE VETO: Never buy PE into a Bullish Put Wall or buy CE into a Bearish Call Wall!
        try:
            diff_l = float(analysis.get("diff_ce_minus_pe_lakhs", 0.0))
            d_spot = float(analysis.get("d_spot_5m", 0.0))
            if not all(math.isfinite(v) for v in (diff_l, d_spot)):
                return reject("Invalid accumulation OI/spot data")
        except (ValueError, TypeError, OverflowError):
            return reject("Invalid accumulation OI/spot data")
        if analysis["recommended_dir"] == "BUY_PE" and ((not breakout_entry and diff_l < -20.0) or d_spot > 4.0):
            return reject(
                f"Veto BUY_PE against Bullish Put Accumulation (OI Diff {diff_l:+.1f}L, 5m Spot {d_spot:+.1f})"
            )
        if analysis["recommended_dir"] == "BUY_CE" and ((not breakout_entry and diff_l > 20.0) or d_spot < -4.0):
            return reject(
                f"Veto BUY_CE against Bearish Call Accumulation (OI Diff {diff_l:+.1f}L, 5m Spot {d_spot:+.1f})"
            )
        if analysis['index']=='NIFTY' and not breakout_entry and abs(diff_l)*100000<INDICES_CONFIG['NIFTY']['wall_threshold']:
            return reject('Structural entry requires the 50-lakh OI wall')

        if level_entry:
            structure=analysis['market_structure']
            try:
                room=float(structure.get('room_points') or 0.0)
                srr=float(structure.get('structure_rr') or 0.0)
                if not all(math.isfinite(v) for v in (room,srr)):
                    return reject('Invalid structure room/RR')
            except (TypeError,ValueError,OverflowError):
                return reject('Invalid structure room/RR')
            # For RETEST_CONFIRMED, require strong OI wall alignment (>= 40L in trade direction)
            if structure.get('setup')=='RETEST_CONFIRMED' and not breakout_entry:
                if (analysis["recommended_dir"]=="BUY_PE" and diff_l < 40.0) or (analysis["recommended_dir"]=="BUY_CE" and diff_l > -40.0):
                    return reject('Retest lacks >=40L OI wall alignment')
            min_rr=MIN_STRUCTURE_RR
            if structure.get('setup')=='RETEST_CONFIRMED':min_rr=max(min_rr,MIN_STRUCTURE_RR_RETEST)
            if consec_losses>=1:min_rr=max(min_rr,MIN_STRUCTURE_RR_AFTER_LOSS)
            if room<MIN_STRUCTURE_ROOM_POINTS:
                return reject(f'Structure room {room:.1f} < {MIN_STRUCTURE_ROOM_POINTS:.0f}')
            soft_override=consec_losses>=1 and room>=55.0 and srr>=2.0
            if not soft_override and srr<min_rr:
                return reject(f'Structure RR {srr:.2f} < {min_rr:.2f}')

        wall_room_ok=self.has_wall_room(analysis)
        if not wall_room_ok:
            return reject("Insufficient room to nearest building OI wall")
        cfg = INDICES_CONFIG[analysis["index"]]
        side='ce' if analysis['recommended_dir']=='BUY_CE' else 'pe'
        try:
            opt_ltp=float(analysis[f'atm_{side}_ltp'])
            dte=float(analysis.get('days_to_expiry',5.0))
            delta=abs(float(analysis.get(f'atm_{side}_delta') or 0.50))
            impulse=float(analysis.get('d_spot_5m',0))
            theta=analysis.get(f'atm_{side}_theta')
            decay=abs(float(theta))/opt_ltp if theta is not None and opt_ltp>0 else analysis.get('daily_theta_pct')
            decay=float(decay) if decay is not None else None
            bid=float(analysis.get(f'atm_{side}_bid',analysis.get('atm_bid',0)) or 0)
            ask=float(analysis.get(f'atm_{side}_ask',analysis.get('atm_ask',0)) or 0)
            bid_qty=float(analysis.get(f'atm_{side}_bid_qty',analysis.get('atm_bid_qty',0)) or 0)
            ask_qty=float(analysis.get(f'atm_{side}_ask_qty',analysis.get('atm_ask_qty',0)) or 0)
            if not all(math.isfinite(v) for v in (opt_ltp,dte,delta,impulse,bid,ask,bid_qty,ask_qty)) or (decay is not None and not math.isfinite(decay)):
                return reject('Invalid premium/theta/quote data')
        except (ValueError,TypeError,OverflowError):
            return reject('Invalid premium/theta/quote data')
        if opt_ltp < min_premium(analysis['index'],dte):
            return reject('Premium below dynamic floor')
        if not premium_allows_edge(opt_ltp,option_stop_points(analysis['index'],opt_ltp),
                                   abs(impulse),max(0.25,min(0.70,delta))):
            return reject('Premium too expensive vs expected impulse')
        strong=abs(impulse)>=cfg['min_spot_impulse']*1.4
        checked_decay=decay
        if breakout_entry:
            # Index buys are intraday: bound modeled twenty-minute theta loss
            # against premium stop risk instead of vetoing a whole day's decay.
            if theta is None:
                return reject('Breakout entry requires observed option theta')
            hold_decay=abs(float(theta))*20.0/375.0
            stop_risk=option_stop_points(analysis['index'],opt_ltp)
            ms['theta_horizon_minutes']=20
            ms['max_hold_minutes']=20
            ms['modeled_holding_theta_points']=hold_decay
            if hold_decay>0.30*stop_risk:
                return reject('Twenty-minute theta exceeds 30% of premium stop risk')
            checked_decay=None
        if not theta_time_ok(dte,bar_time,strong,checked_decay):
            return reject('Theta / days-to-expiry filter')
        if not spread_and_liquidity_ok(bid,ask,(bid+ask)/2,
                bid_qty,ask_qty,1,cfg['lot_size'],opt_ltp):
            return reject('Spread or liquidity filter')

        sig = (
            f"IF wall={analysis['wall_tag']} & ce={analysis['ce_quadrant']} "
            f"& pe={analysis['pe_quadrant']} & ctrl={analysis['market_controller']} "
            f"& dom={analysis['market_dominance']} -> {analysis['recommended_dir']}"
        )
        if level_entry:
            structure=analysis["market_structure"]
            sides=','.join(sorted({r['side'] for r in structure['strike_oi_confirmation']}))
            sig=f"{'LEVEL_V2' if breakout_entry else 'LEVEL_V1'} {structure['setup']} & strike_oi={sides} -> {analysis['recommended_dir']}"

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
            return reject("Learned rule "+rule["status"],sig)

        # Mid-day Lunch Lull Filter (12:00 PM - 13:30 PM IST)
        if not level_entry and bar_time and dtime(12, 0) <= bar_time <= dtime(13, 30):
            min_lunch_imp = cfg["min_spot_impulse"] * 1.75
            if rule["status"] != "ACTIVE" and (
                abs(analysis["d_spot_5m"]) < min_lunch_imp or abs(analysis["diff_ce_minus_pe_lakhs"]) < 100.0
            ):
                return reject("Lunch momentum/imbalance filter",sig)

        # Sniper Recovery Gate after a loss
        if not level_entry and consec_losses >= 1:
            min_rec_imp = cfg["min_spot_impulse"] * 1.6
            if rule["status"] != "ACTIVE" and abs(analysis["d_spot_5m"]) < min_rec_imp:
                return reject("Post-loss recovery filter",sig)

        # Cosine Similarity Veto against recent losing state vectors
        for lost_dir, lost_vec in self.recent_losing_states[-8:]:
            if lost_dir == analysis["recommended_dir"] and cosine_similarity(analysis["state_vec"], lost_vec) >= 0.92:
                return reject("Similar recent losing state",sig)

        analysis["entry_decision"]={"action":analysis["recommended_dir"],"reason":"All entry selection checks passed","rule":sig}
        return analysis["recommended_dir"], sig

    def compute_lots(
        self,
        idx_name: str,
        sig: str,
        entry_price: float,
        net_wallet: float,
        consec_losses: int,
        analysis: Optional[Dict[str, Any]] = None,
    ) -> int:
        cfg = INDICES_CONFIG[idx_name]
        cost_per_lot = entry_price * cfg["lot_size"]
        if not all(math.isfinite(v) and v > 0 for v in (entry_price,net_wallet,cost_per_lot)):
            return 0
        max_affordable = max(0,min(3,int((net_wallet * 0.60) // (cost_per_lot + 100))))
        if max_affordable == 0:
            return 0
        if consec_losses >= 1:
            return 1

        rule = self.rules.get(sig, {})
        if rule.get("last_lot_loss", False):
            return 1

        if analysis is not None:
            structure=analysis.get('market_structure') or {}
            sizing={'basis':'One lot: current setup has not met scale-up checks',
                    'lots':1,'risk_budget_rs':round(net_wallet*.05,2),
                    'probability_estimated':False}
            structure['lot_sizing']=sizing
            analysis['market_structure']=structure
            try:
                side='ce' if analysis['recommended_dir']=='BUY_CE' else 'pe'
                bid,ask=(float(analysis[f'atm_{side}_{key}']) for key in ('bid','ask'))
                bid_qty,ask_qty=(float(analysis[f'atm_{side}_{key}_qty']) for key in ('bid','ask'))
                delta=abs(float(analysis[f'atm_{side}_delta']))
                room,rr,body,close,age,reference_age=(float(structure[key]) for key in (
                    'room_points','structure_rr','confirmation_body_fraction',
                    'confirmation_close_strength','confirmation_age_seconds','level_reference_age_seconds'))
                risk_points=option_stop_points(idx_name,entry_price)
                values=(bid,ask,bid_qty,ask_qty,delta,room,rr,body,close,age,reference_age,risk_points)
                strong=(all(math.isfinite(v) for v in values)
                    and analysis.get('index')==idx_name and analysis['recommended_dir'] in ('BUY_CE','BUY_PE')
                    and structure.get('ready') and structure.get('allowed')
                    and structure.get('strike_oi_confirmation')
                    and rule.get('status') not in ('PRUNED','DAY_MUTED')
                    and 0<=age<=120 and 270<=reference_age<=450
                    and rr>=2 and room>=(25 if idx_name=='NIFTY' else 60)
                    and body>=.60 and close>=.75 and .35<=delta<=.75
                    and 0<bid<ask and (ask-bid)/ask<=.015
                    and ask-bid<=.20*risk_points)
                if strong:
                    desired=3 if rr>=3 and body>=.70 and close>=.85 and room>=(40 if idx_name=='NIFTY' else 100) else 2
                    for lots in range(min(desired,max_affordable),1,-1):
                        qty=lots*cfg['lot_size']
                        estimated_risk=(risk_points+max(0,entry_price-risk_points)*.001)*qty+40+24.5*lots
                        if min(bid_qty,ask_qty)<qty or estimated_risk>net_wallet*.05:continue
                        sizing.update(basis='Fresh confirmed price/strike-OI setup with room, depth and bounded risk',
                                      lots=lots,estimated_stop_risk_rs=round(estimated_risk,2),
                                      structure_rr=rr)
                        return lots
                # With current quote context, do not bypass failed scale-up
                # checks through the historical-performance branch below.
                return 1
            except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):
                return 1

        t_cnt = int(rule.get("trades_count", 0))
        w_cnt = int(rule.get("wins_count", 0))
        wr = (w_cnt / t_cnt) if t_cnt > 0 else 0.0
        exp_r = float(rule.get("expectancy_r", 0.0))

        days_n = len(rule.get("unique_days_traded", []))
        # Independent trading days and a larger sample are required before sizing up.
        if (rule.get("status") == "ACTIVE" and t_cnt >= 10 and days_n >= 6
                and wr >= 0.48 and exp_r >= 0.85
                and rule.get('avg_win_r',0)>=2.2*max(0.1,rule.get('avg_loss_r',0))):
            if exp_r >= 1.20 and wr >= 0.55 and net_wallet >= 40000.0:
                return min(2, max_affordable)
        return 1

    def learn_from_trade(
        self, day_str: str, sig: str, state_vec: List[float], action_idx: int, r_mult: float, lots: int = 1
    ) -> None:
        if r_mult <= -0.70:
            self.recent_losing_states.append(("BUY_CE" if action_idx == 1 else "BUY_PE", state_vec))

        if sig in self.rules:
            r = self.rules[sig]
            r["last_trade_day"]=day_str
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
                r["trades_count"] >= 7 and days_n >= 4 and wr < 0.32
            ):
                self.mark_pruned(r,day_str)
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
            "reward_basis":"NET_AFTER_MODELED_CHARGES",
            "reward": reward
        })
        if len(self.brain["replay_buffer"]) > 120:
            self.brain["replay_buffer"] = self.brain["replay_buffer"][-120:]

        # Normalized bounded diagnostic fit; empirical rules control trading.
        self.brain["model_role"]="DIAGNOSTIC_ONLY_RULES_CONTROL_TRADING"
        lr = 0.045 / max(1.0,1.0 + sum(x*x for x in state_vec))
        w = self.brain["weights"][action_idx]
        b = self.brain["bias"][action_idx]
        q_pred = sum(wi * si for wi, si in zip(w, state_vec)) + b
        err = max(-2.0,min(2.0,reward - q_pred))
        for i in range(len(w)):
            self.brain["weights"][action_idx][i] = round(w[i] + lr * err * state_vec[i], 6)
        self.brain["bias"][action_idx] = round(b + lr * err, 6)
        atomic_write_json(self.brain_file, self.brain)
        self.logger.info("[LEARNING] %s | after-cost reward %+.4fR | rule status %s",
                         sig,r_mult,self.rules.get(sig,{}).get("status","UNKNOWN"))


def days_to_expiry(expiry_str: str, now: Optional[datetime] = None) -> float:
    """Fractional days until 15:30 IST; unavailable expiry uses the requested 5-day fallback."""
    if not expiry_str:return 5.0
    try:
        expiry=datetime.strptime(str(expiry_str)[:10],'%Y-%m-%d').replace(
            hour=15,minute=30,tzinfo=IST)
        now=now or datetime.now(IST)
        now=now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        return max(0.0,(expiry-now).total_seconds()/86400)
    except (ValueError,TypeError):return 5.0


def min_premium(idx_name: str, dte: float) -> float:
    base=INDICES_CONFIG[idx_name]['min_option_premium']
    return base*(1.45 if dte<0.6 else 1.20 if dte<1.5 else 1.08 if dte<3 else 1.0)


# Floor impulse × max allowed delta ÷ floor stop cannot reach 1.30:
#   Nifty  8.5 × 0.70 / 6.15 = 0.97
#   Sensex 25  × 0.70 / 20   = 0.88
# Gate uses the observed 5-minute spot move, not the configured minimum.
# Require the observed impulse to cover at least 0.95 times initial stop risk.
OBSERVED_IMPULSE_MIN_R = 0.95


def premium_allows_edge(entry_price: float, stop_points: float,
                        observed_spot_impulse: float, approx_delta: float = 0.50) -> bool:
    """Proxy: |observed 5m spot move| × delta ÷ initial stop. Not a net-of-cost RR."""
    delta = max(0.25, min(0.70, approx_delta))
    return (all(math.isfinite(v) and v > 0 for v in
                (entry_price, stop_points, observed_spot_impulse, delta))
            and observed_spot_impulse * delta / stop_points >= OBSERVED_IMPULSE_MIN_R)


def theta_time_ok(dte: float, bar_time: Optional[dtime], is_strong_impulse: bool,
                  daily_decay_pct: Optional[float] = None) -> bool:
    if not math.isfinite(dte) or dte<0:return False
    bar_time=bar_time or datetime.now(IST).time()
    if daily_decay_pct is not None and (not math.isfinite(daily_decay_pct) or daily_decay_pct<0):
        return False
    # Apply explicit theta even to a strong, early expiry-day setup.
    if daily_decay_pct is not None and daily_decay_pct>0.12 and dte<3:return False
    if dte<0.6:return bar_time<dtime(10,30) and is_strong_impulse
    if dte<1.5 and bar_time>=dtime(13,0) and not is_strong_impulse:return False
    return True


def spread_and_liquidity_ok(bid: float, ask: float, mid: float, bid_qty: float,
                            ask_qty: float, lots: int, lot_size: int, entry_price: float) -> bool:
    if (not all(math.isfinite(v) for v in (bid,ask,mid,bid_qty,ask_qty,lots,lot_size,entry_price))
            or min(bid,mid,lots,lot_size,entry_price)<=0 or ask<=bid):return False
    spread=(ask-bid)/mid
    return (spread<=0.025 and (entry_price>=80 or spread<=0.015)
            and min(bid_qty,ask_qty)>=lots*lot_size)


def option_stop_points(index: str, entry_price: float) -> float:
    """Freeze entry-premium risk for Nifty and Sensex within their respective bounds."""
    if index == "NIFTY":
        return round(max(6.15, min(12.50, float(entry_price) * 0.085)), 2)
    if index == "SENSEX":
        return round(max(18.0,min(32.0,float(entry_price)*0.09)),2)
    return INDICES_CONFIG[index]["sl_points"]


def update_ratchet_sl(entry_price: float, current_sl: float, highest_ltp: float, sl_points: float) -> Tuple[float, bool]:
    """Fast breakeven + 20-point profit lock ratchet."""
    if sl_points<=0:return current_sl,False
    gain_pts = highest_ltp - entry_price
    peak_r = gain_pts / sl_points
    armed = False
    new_sl = current_sl
    if gain_pts >= 18.0 or peak_r >= 2.10:
        new_sl = max(current_sl, round(entry_price + 1.35 * sl_points, 2),
                     round(highest_ltp - 0.40 * sl_points, 2))
        armed = True
    elif gain_pts >= 12.5 or peak_r >= 1.55:
        new_sl = max(current_sl, round(entry_price + 0.80 * sl_points, 2),
                     round(highest_ltp - 0.55 * sl_points, 2))
        armed = True
    elif gain_pts >= 8.5 or peak_r >= 1.15:
        new_sl = max(current_sl, round(entry_price + 0.35 * sl_points, 2))
        armed = True
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
    position["exit_fills"] = [{"qty":qty,"price":price,"timestamp":timestamp,"reason":"TARGET_1_12_POINTS"}]
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
    require_session_end=False, session_profit_check=None, current_session_policy=False,max_hold_minutes=None,
    profit_floor_check=None,
):
    return replay_option_trade(entry_idx,is_ce,timestamps,opens,highs,lows,closes,
        strikes,sl_points,interval_min,index,lots,lot_size,exit_fills,
        nifty_target_quantities(index,lots,lot_size),update_ratchet_sl,
        EOD_SQUAREOFF_TIME,entry_at_open,expected_strike,slippage_pct,require_session_end,
        session_profit_check,current_session_policy,
        option_stop_points if current_session_policy and index in INDICES_CONFIG else None,
        NIFTY_TARGET_POINTS if current_session_policy and index=='NIFTY' else None,max_hold_minutes,profit_floor_check)


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
    ledger_path="paper_trade_ledger.json", initial_risk_points=None,
) -> float:
    gross_pnl = round((exit_price - entry_price) * qty, 2)
    charges = round(40.0 + (lots * 24.5) + 20.0*max(0,len(exit_fills or [])-1), 2)
    net_pnl = round(gross_pnl - charges, 2)
    # Keep execution's gross R for audit, but train on the after-cost outcome.
    risk_budget=(float(initial_risk_points)*qty if initial_risk_points is not None else
                 gross_pnl/r_mult if r_mult and gross_pnl/r_mult>0 else option_stop_points(idx_name,entry_price)*qty)
    learning_r=round(net_pnl/risk_budget,6)

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
        "initial_risk_points": round(risk_budget/qty, 4),
        "r_multiple": r_mult,
        "learning_r_multiple":learning_r,
        "learning_reward_basis":"NET_AFTER_MODELED_CHARGES",
        "state_vec":list(state_vec),
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

    brain.learn_from_trade(day_str, sig, state_vec, 1 if opt_type == "CE" else 2, learning_r, lots=lots)
    atomic_write_json(ledger_path, ledger)
    return gross_pnl


def run_real_historical_backtest(
    days: int,
    indices_to_test: List[str],
    interval: str,
    logger: logging.Logger, output_dir: Optional[str] = None,
) -> Dict[str, Any]:
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
    output_dir=output_dir or os.path.join(CACHE_DIR,"index_backtest_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    os.makedirs(output_dir,exist_ok=True)
    backtest_ledger_path=os.path.join(output_dir,"paper_trade_ledger.json")
    brain = SelfLearningBrain(logger,os.path.join(output_dir,"brain.json"),os.path.join(output_dir,"rules.json"),fresh=True)

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

    end_dt = datetime.now(IST).replace(hour=0,minute=0,second=0,microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    windows: List[Tuple[str, str]] = []
    cursor = start_dt
    while cursor < end_dt:
        nxt = min(cursor + timedelta(days=30), end_dt)
        if not (cursor.weekday() == 5 and (nxt - cursor).days <= 2):
            windows.append((cursor.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cursor = nxt

    logger.info(
        f"Starting DhanHQ historical rolling-option replay | Span: {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')} "
        f"({len(windows)} windows of up to 30 days; end date exclusive; cached in {CACHE_DIR}/) | Interval: {interval}m | Indices: {indices_to_test}"
    )

    call_strikes_rel = ["ATM", "ATM+1", "ATM+2", "ATM+3"]
    put_strikes_rel = ["ATM", "ATM-1", "ATM-2", "ATM-3"]
    interval_min = int(interval)
    cooldown_bars = max(1, POST_SL_COOLDOWN_MINUTES // interval_min)
    win_cooldown_bars = max(1, POST_WIN_COOLDOWN_MINUTES // interval_min)

    seen_bars: Set[Tuple[str, int]] = set()
    daily_stats: Dict[Tuple[str, str], Dict[str, int]] = {}
    last_view_by_index: Dict[str, Dict[str, Any]] = {}
    structure_counts={"checked":0,"unavailable":0,"allowed":0,"rejected":0}
    missing_option_windows=[]
    unvalidated_trades=[]
    entry_rejections={}
    missing_decision_inputs={name:0 for name in indices_to_test}

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
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "CALL", w_from, w_to, interval, require_ohlc=True)
                if res and res.get("timestamp"):
                    ce_legs[s_lbl] = res

            for s_lbl in put_strikes_rel:
                res = dhan.fetch_rolling_option_window(idx_name, s_lbl, "PUT", w_from, w_to, interval, require_ohlc=True)
                if res and res.get("timestamp"):
                    pe_legs[s_lbl] = res

            if "ATM" not in ce_legs or "ATM" not in pe_legs:
                missing_option_windows.append({"index":idx_name,"from":w_from,"to_exclusive":w_to})
                logger.warning(f"No trading session bars in ({w_from} -> {w_to}) for {idx_name}. Continuing.")
                continue

            atm_ce = ce_legs["ATM"]
            atm_pe = pe_legs["ATM"]
            # Align by timestamp, not array position, and retain actual strike identity.
            ts_list=sorted(set(atm_ce["timestamp"]) | set(atm_pe["timestamp"]))
            n_bars=len(ts_list)
            leg_rows={side:{label:{int(t):{k:float(data[k][j]) for k in
                ('open','high','low','close','oi','iv','strike','spot')} for j,t in enumerate(data['timestamp'])}
                for label,data in legs.items()} for side,legs in (('ce',ce_legs),('pe',pe_legs))}
            skip_until_idx=-1
            fixed_cache={}
            for i,ts_val in enumerate(ts_list):
                ts_val=int(ts_val)
                if (idx_name,ts_val) in seen_bars:continue
                seen_bars.add((idx_name,ts_val))
                atm_row=leg_rows['ce']['ATM'].get(ts_val)
                if not atm_row:continue
                spot=atm_row['spot']
                historical_chain={}
                for side,legs in leg_rows.items():
                    for rows in legs.values():
                        row=rows.get(ts_val)
                        if row:
                            historical_chain.setdefault(f"{row['strike']:.6f}",{})[side]={
                                'oi':row['oi'],'last_price':row['close'],'implied_volatility':row['iv']}
                analysis=analyzer.evaluate_chain(idx_name,{'last_price':spot,'oc':historical_chain},
                    expiry='WEEK1_SAME_SESSION_ASSUMPTION',snapshot_time=ts_val+interval_min*60)
                if idx_name=='NIFTY':
                    structure=apply_nifty_level_entry(analysis,structure_candles,historical_chain,ts_val+interval_min*60,
                        enable_opening_trend=env.get('NIFTY_OPENING_TREND','false').strip().lower()=='true',
                        enable_price_levels=True, enable_breakout_entries=True)
                    structure['oi_source']='ROLLING_STRIKES_SUBSET_NOT_FULL_CHAIN'
                    analysis['market_structure']=structure
                    if dtime(9,40)<=datetime.fromtimestamp(ts_val+interval_min*60,IST).time()<=LAST_ENTRY_TIME:
                        structure_counts['checked']+=1
                        structure_counts['unavailable' if not structure['ready'] else 'allowed' if structure['allowed'] else 'rejected']+=1
                last_view_by_index[idx_name]=analysis

                if i <= skip_until_idx:
                    continue

                dt = datetime.fromtimestamp(ts_val+interval_min*60,IST)
                day_str = dt.strftime("%Y-%m-%d")
                day_key = (idx_name, day_str)
                if day_key not in daily_stats:
                    daily_stats[day_key] = {"trades": 0, "consec_losses": 0, "net_pnl": 0.0}
                    brain.unmute_new_day(day_str)

                d_stat = daily_stats[day_key]
                if dt.time() < SESSION_START_TIME or dt.time() > LAST_ENTRY_TIME:
                    continue
                if d_stat.get("unvalidated_position") or d_stat["trades"] >= MAX_TRADES_PER_DAY or d_stat["consec_losses"] >= MAX_CONSECUTIVE_LOSSES_PER_DAY:
                    continue

                if d_stat.get("profit_protected") or d_stat["net_pnl"] >= INDEX_PROFIT_LOCK_RS:
                    continue
                if any(not analysis.get(f'atm_{side}_{field}') for side in ('ce','pe') for field in ('bid','ask','bid_qty','ask_qty')):
                    missing_decision_inputs[idx_name]+=1
                action_dir, sig = brain.select_action(analysis, d_stat["consec_losses"], bar_time=dt.time())
                if action_dir == "HOLD" or not sig:
                    reason=(analysis.get('entry_decision') or {}).get('reason','HOLD before quote qualification')
                    entry_rejections[reason]=entry_rejections.get(reason,0)+1
                    continue

                is_ce = action_dir == "BUY_CE"
                opt_type = "CE" if is_ce else "PE"
                strike_val=analysis['atm_ce_strike'] if is_ce else analysis['atm_pe_strike']
                cache_key=(opt_type,strike_val)
                if cache_key not in fixed_cache:
                    try:
                        fixed_cache[cache_key]=fixed_contract_bars(leg_rows['ce' if is_ce else 'pe'],ts_list,strike_val)
                    except ValueError as error:
                        unvalidated_trades.append({'index':idx_name,'entry_timestamp':dt.isoformat(),
                            'strike':strike_val,'reason':str(error)})
                        continue
                leg=fixed_cache[cache_key]
                entry_idx=i+1
                if entry_idx>=n_bars:
                    continue
                entry_time=datetime.fromtimestamp(leg["timestamp"][entry_idx],IST)
                if entry_time.date()!=dt.date() or entry_time.time()>LAST_ENTRY_TIME:
                    continue
                if not math.isfinite(leg['open'][entry_idx]):
                    unvalidated_trades.append({'index':idx_name,'entry_timestamp':entry_time.isoformat(),
                        'strike':strike_val,'reason':'Selected contract absent at executable entry'})
                    d_stat['trades']+=1;d_stat['unvalidated_position']=True
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
                if qty<=0 or entry_price*qty+40+24.5*lots>ledger["virtual_balance_net"]:
                    entry_rejections["Insufficient wallet"]=entry_rejections.get("Insufficient wallet",0)+1
                    continue
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
                    session_profit_check=lambda entry,remaining,done,bid:daily_target_reached(d_stat['net_pnl'],
                        {'entry_price':entry,'qty':remaining,'lots':lots,'exit_fills':done},bid),
                    current_session_policy=True,max_hold_minutes=20 if idx_name=='NIFTY' else None,
                    profit_floor_check=lambda entry,remaining,done:index_profit_lock_bid(d_stat['net_pnl'],
                        {'entry_price':entry,'qty':remaining,'lots':lots,'exit_fills':done}),
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
                    ledger_path=backtest_ledger_path,
                    initial_risk_points=option_stop_points(idx_name,entry_price),
                )
                ledger["trades"][-1].update(signal_timestamp=datetime.fromtimestamp(ts_val+interval_min*60).isoformat(),
                    execution_model="NEXT_BAR_OPEN_0.1PCT_ADVERSE_SLIPPAGE",
                    exit_time_precision="five-minute bar; exact intrabar fill time unknown")

                skip_until_idx=exit_idx+(cooldown_bars if ledger["trades"][-1]["net_pnl"]<=0 else win_cooldown_bars)
                if exit_reason=="DAILY_PROFIT_PROTECTION":d_stat["profit_protected"]=True
                d_stat["net_pnl"] = round(d_stat["net_pnl"] + float(ledger["trades"][-1]["net_pnl"]), 2)
                d_stat["trades"] += 1
                d_stat["consec_losses"] = d_stat["consec_losses"] + 1 if ledger["trades"][-1]["net_pnl"] <= 0 else 0

                logger.info(
                    f"[REAL BACKTEST TRADE] {entry_time.strftime('%Y-%m-%d %H:%M')} | {idx_name} {strike_val}_{opt_type} (Lots:{lots}) "
                    f"| Entry: Rs.{entry_price:.2f} -> Exit: Rs.{exit_price:.2f} ({exit_reason}) "
                    f"| 5m Spot: {analysis['d_spot_5m']:+.1f} | OI Diff: {analysis['diff_ce_minus_pe_lakhs']:+.1f}L "
                    f"| Gross PnL: Rs.{gross_pnl:+,.2f} ({r_mult:+.2f}R)"
                )

    ledger["period"]={"from_inclusive":start_dt.isoformat(),"to_exclusive":end_dt.isoformat()}
    ledger["historical_data_issues"]=dhan.history_events
    ledger["support_resistance_coverage"]=structure_counts
    ledger["missing_option_windows"]=missing_option_windows
    ledger["unvalidated_trades"]=unvalidated_trades
    ledger["entry_rejection_counts"]=entry_rejections
    ledger["missing_decision_input_snapshots"]=missing_decision_inputs
    ledger["full_strategy_validated"]=False
    ledger["backtest_status"]=("INCOMPLETE_OPTION_REPLAY" if unvalidated_trades else "INCOMPLETE_MARKET_STRUCTURE" if structure_counts["unavailable"] or ("NIFTY" in indices_to_test and not structure_counts["checked"])
                               else "REPLAY_WITH_MISSING_OPTION_WINDOWS" if missing_option_windows or dhan.history_events else "REPLAY_COMPLETED")
    if not unvalidated_trades and any(missing_decision_inputs.values()):ledger["backtest_status"]="INCOMPLETE_DECISION_INPUTS"
    ledger["limitations"]=["Historical bid/ask depth and broker Greeks are absent; strict entry checks cannot be reproduced and are not bypassed",
                           "Combined index processing is sequential by index, not a shared chronological portfolio replay","Historical OI walls use available rolling strikes, not the full historical option chain",
                           "Dynamic Rs.3000 runner assessments require decision-time held-strike OI and executable quotes; this baseline exit replay does not reproduce that overlay",
                           "Five-minute OHLC and modeled fills do not establish live execution or an 80% win rate",
                           "Entry at next bar open; 0.1% adverse modeled slippage on buys and sells",
                           "Stop-first when intrabar order is unknown; gap stops use worse opening price",
                           "Trailing changes only apply on later bars; strike changes/gaps are unvalidated",
                           "Unvalidated outcomes excluded from P&L and learning; remaining same-day entries blocked"]
    if structure_counts["unavailable"]:
        logger.warning("Nifty backtest has %d snapshots without verified structure; result is incomplete",structure_counts["unavailable"])
    atomic_write_json(backtest_ledger_path, ledger)
    logger.info("Backtest output and learning isolated in %s",output_dir)
    if last_view_by_index:
        atomic_write_json(os.path.join(output_dir,"telemetry.json"), {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "api_poll_interval_sec": 3.6,
            "indices": last_view_by_index,
            "active_position": None,
        })

    logger.info(
        f"Historical replay finished [{ledger['backtest_status']}] | Closed trades: {ledger['total_trades']} "
        f"| Gross Profit: Rs.{ledger['profit_without_charges']:+,.2f} "
        f"| Net Profit: Rs.{ledger['net_profit_after_charges']:+,.2f} "
        f"| Avg Gross Win: Rs.{ledger['avg_winning_value_rs']:,.2f} vs Avg Gross Loss: Rs.{ledger['avg_losing_value_rs']:,.2f} "
        f"({ledger['win_to_loss_ratio']:.2f}x)"
    )

    return ledger


def daily_target_reached(realized_net, position, bid, target_net=None):
    """Estimate full-day net on liquidation, including any partial fills once."""
    try:
        if not math.isfinite(bid) or bid<=0:return False
        entry=float(position["entry_price"])
        gross=sum((float(f["price"])-entry)*int(f["qty"]) for f in position.get("exit_fills",[]))
        gross+=(bid-entry)*int(position["qty"])
        fees=40.0+int(position["lots"])*24.5+20.0*len(position.get("exit_fills",[]))
        return realized_net+gross-fees>=(DAILY_PROFIT_TARGET_RS if target_net is None else target_net)
    except (KeyError,TypeError,ValueError,OverflowError):return False


def index_profit_lock_bid(realized_net, position):
    """Raw exit-price floor budgeting fees and 0.1% adverse exit slippage."""
    try:
        entry=float(position['entry_price']);qty=int(position['qty'])
        fills=position.get('exit_fills',[])
        booked=sum((float(f['price'])-entry)*int(f['qty']) for f in fills)
        fees=40.0+int(position['lots'])*24.5+20.0*len(fills)
        needed=entry+(INDEX_PROFIT_LOCK_RS-float(realized_net)-booked+fees)/qty
        if qty<=0 or not all(math.isfinite(v) for v in (entry,needed)):return None
        needed_fill=math.ceil(needed*20-1e-9)/20
        return max(0.05,math.ceil(needed_fill/0.999*20-1e-9)/20)
    except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):return None


def assess_index_runner(view, position, node, now_epoch, realized_net):
    """Bounded scenario check, not a probability or forecast of guaranteed profit."""
    out={'hold':False,'target_net':INDEX_RUNNER_TARGET_RS,'reason':'Runner evidence unavailable'}
    try:
        entry=float(position['entry_price']);qty=int(position['qty']);bid=float(node['top_bid_price'])
        ask=float(node['top_ask_price']);delta=abs(float(node['greeks']['delta']))
        theta=abs(float(node['greeks']['theta']));spot=float(view['spot'])
        age=now_epoch-float(view['snapshot_received_epoch'])
        horizon=min(20.0,(float(position['entry_epoch'])+1200-now_epoch)/60,
                    (datetime.fromtimestamp(now_epoch,IST).replace(hour=14,minute=45,second=0,microsecond=0).timestamp()-now_epoch)/60)
        if not all(math.isfinite(v) for v in (entry,bid,ask,delta,theta,spot,age,horizon,float(realized_net))) or qty<=0:return out
        if not 0<=age<=8 or horizon<5 or min(bid,spot)<=0 or ask<=bid:return out
        if min(float(node['top_bid_quantity']),float(node['top_ask_quantity']))<qty:return out
        if (ask-bid)/((ask+bid)/2)>0.025 or not 0.25<=delta<=0.8:return out
        side=position['option_type'];sign=1 if side=='CE' else -1
        if side not in ('CE','PE'):return out
        impulse=sign*float(view['d_spot_5m'])
        if not math.isfinite(impulse) or impulse<INDICES_CONFIG[position['index']]['min_spot_impulse']:
            out['reason']='Directional impulse no longer supports a runner';return out
        oi=view.get('oi_analysis') or {}
        if not oi.get('ready'):return out
        rows=oi.get('nearby_strikes',[])
        supported=False;opponents=[]
        for row in rows:
            strike=float(row['strike']);change=float(row['change_over_5m']);premium=float(row['premium_change_over_5m'])
            if not all(math.isfinite(v) for v in (strike,change,premium)):continue
            if strike==float(position['strike']) and abs(change)>=100000:
                supported|=((row['side']==('pe' if sign>0 else 'ce') and change>0 and premium<0)
                            or (row['side']==side.lower() and change<0 and premium>0))
            if row['side']==('ce' if sign>0 else 'pe') and change>0 and premium<0 and sign*(strike-spot)>0:
                opponents.append(sign*(strike-spot))
        if not supported:
            out['reason']='Held strike lacks current supportive OI/premium change';return out
        levels=(view.get('market_structure') or {}).get('levels',{})
        opponents += [sign*(float(levels[k])-spot) for k in ('previous_day_high','previous_day_low','opening_high','opening_low')
                      if k in levels and math.isfinite(float(levels[k])) and sign*(float(levels[k])-spot)>0]
        if not opponents:return out
        room=min(opponents)
        done=position.get('exit_fills',[])
        booked=sum((float(f['price'])-entry)*int(f['qty']) for f in done)
        fees=40+int(position['lots'])*24.5+20*len(done)
        current_net=realized_net+booked+(bid-entry)*qty-fees
        needed=max(0,(INDEX_RUNNER_TARGET_RS-current_net)/qty)
        theta_budget=theta*horizon/375.0
        feasible_move=min(max(0,room-5),impulse*min(2,horizon/5))
        feasible_gain=max(0,delta*feasible_move-theta_budget-bid*0.001)
        out.update(current_net=round(current_net,2),required_premium_gain=round(needed,2),
                   scenario_premium_gain=round(feasible_gain,2),opposing_room_points=room,
                   horizon_minutes=horizon)
        out['hold']=feasible_gain>=1.2*needed
        out['reason']='Momentum, held-strike OI and opposing room support runner scenario' if out['hold'] else 'Remaining move does not support Rs.3000 with scenario buffer'
        return out
    except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):return out


def record_nifty_entry_observation(path, analysis, candles, chain, as_of):
    """Save decision-time evidence for missed-entry review; never train on it.

    Includes the last 150 closed index bars and nearby option contracts so later
    review need not infer a past entry from the eventual day high/low.
    """
    if not isinstance(candles,dict) or not candles.get("timestamp"):
        return False
    keep=[i for i,t in enumerate(candles["timestamp"]) if float(t)+300<=as_of][-150:]
    if not keep:return False
    history={k:[candles[k][i] for i in keep] for k in ("timestamp","open","high","low","close")}
    nearby={k:v for k,v in chain.items() if abs(float(k)-analysis["spot"])<=500}
    observation={"snapshot_epoch":as_of,"analysis":analysis,"closed_index_candles":history,
                 "option_chain":nearby,"purpose":"ENTRY_REVIEW_ONLY_NOT_REALIZED_PNL_OR_TRAINING"}
    with open(path,"a",encoding="utf-8") as stream:
        stream.write(json.dumps(observation,separators=(",",":"),allow_nan=False)+"\n")
    return True


@single_bot_session("INDEX")
def run_intraday_market_session(live_mode: bool, logger: logging.Logger, indices_to_trade=None) -> None:
    if live_mode:
        raise RuntimeError("LIVE disabled: broker fill and protective-stop reconciliation is not implemented; use PAPER.")
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

    # Restore today's trade count and consecutive losses if script is restarted mid-day
    today_trades_list = [t for t in ledger.get("trades", []) if str(t.get("timestamp", "")).startswith(today_str)]
    day_trades = len(today_trades_list)
    day_entries = index_entries_today(ledger, today_str)
    day_net_pnl = round(sum(float(t.get("net_pnl",0.0)) for t in today_trades_list), 2)
    profit_protected=(day_net_pnl>=INDEX_PROFIT_LOCK_RS or
        ((ledger.get('daily_profit_protection') or {}).get('day')==today_str
         and bool((ledger.get('daily_profit_protection') or {}).get('armed'))))
    consec_losses = 0
    for t in reversed(today_trades_list):
        if float(t.get("net_pnl", t.get("gross_pnl",0.0))) <= 0:
            consec_losses += 1
        else:
            break

    expiries = {name:dhan.fetch_nearest_expiry(INDICES_CONFIG[name]["security_id"], "IDX_I") or today_str
                for name in selected_indices}
    logger.info(
        f"Started 09:15-14:45 IST Live Session | Mode: {'LIVE' if live_mode else 'PAPER'} "
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
        logger.info("Resuming saved index paper position: Qty %d", active_pos["qty"])
    cooldown_until: Optional[datetime] = None
    cycle = 0
    last_structure_log=None
    last_entry_audit=None
    structure_candles=None
    last_oi_log=0.0
    call_feed=(TelegramCallFeed(logger,env) if not live_mode
               and env.get('TELEGRAM_CALLS_PAPER','false').strip().lower()=='true' else None)
    last_call_decision={}

    while True:
        now = datetime.now()
        if now.time() >= EOD_SQUAREOFF_TIME and active_pos is None:
            logger.info("02:45 PM IST cutoff reached. Session complete.")
            break
        if now.time() < SESSION_START_TIME:
            time.sleep(10)
            continue

        # Immediately stop API polling once daily trade cap, consecutive loss breaker,
        # or entry cutoff time is reached and no position is open.
        if active_pos is None and (
            profit_protected or day_net_pnl >= INDEX_PROFIT_LOCK_RS
            or day_entries >= MAX_TRADES_PER_DAY
            or consec_losses >= MAX_CONSECUTIVE_LOSSES_PER_DAY
            or now.time() > LAST_ENTRY_TIME
        ):
            logger.info(
                f"Daily session target/cutoff reached (Entries: {day_entries}/{MAX_TRADES_PER_DAY}, "
                f"Consec Losses: {consec_losses}/{MAX_CONSECUTIVE_LOSSES_PER_DAY}, "
                f"Net today: Rs.{day_net_pnl:+.2f}/{DAILY_PROFIT_TARGET_RS:.0f}, "
                f"Time: {now.strftime('%H:%M:%S')}). Stopping Dhan API polling."
            )
            break

        idx_name = active_pos["index"] if active_pos else selected_indices[cycle % len(selected_indices)]
        cfg = INDICES_CONFIG[idx_name]
        held_channel=bool(not live_mode and active_pos and
                          (active_pos.get('market_structure') or {}).get('telegram_call'))
        raw_oc = (dhan.fetch_held_channel_quote(active_pos) if held_channel else
                  dhan.fetch_option_chain(cfg["security_id"], cfg["exchange_segment"], expiries[idx_name]))
        chain_received_epoch = time.time()

        if raw_oc:
            view = (dict(telemetry_state['indices'].get(idx_name,{})) if held_channel else
                    analyzer.evaluate_chain(idx_name, raw_oc,expiry=expiries[idx_name]))
            if idx_name=="NIFTY" and not held_channel:
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
                if active_pos is None:
                    structure_candles=dhan.fetch_nifty_structure_candles(
                        (now-timedelta(days=10)).strftime("%Y-%m-%d 00:00:00"),
                        now.strftime("%Y-%m-%d %H:%M:%S"))
                    structure=apply_nifty_level_entry(view,structure_candles,raw_oc.get("oc",{}),time.time(),
                        enable_opening_trend=env.get('NIFTY_OPENING_TREND','false').strip().lower()=='true',
                        enable_price_levels=True, enable_breakout_entries=True)
                    view["market_structure"]=structure
                    log_key=(structure.get("confirmation_close"),structure["reason"])
                    if log_key!=last_structure_log:
                        logger.info("[NIFTY STRUCTURE] %s",json.dumps(structure,sort_keys=True))
                        last_structure_log=log_key
                else:
                    view["market_structure"]=active_pos.get("market_structure",{})
            # HTTP waits may cross the cutoff. Use receipt-time wall clock for
            # entries and exits instead of the time captured before fetching.
            now = datetime.now()
            telemetry_state["indices"][idx_name] = view
            telemetry_state["updated_at"] = now.strftime("%H:%M:%S")
            telemetry_state['channel_feed']={'status':call_feed.connection_status if call_feed else 'DISABLED',
                'sources':['GrowX','Tradewithsac'],'mode':'PAPER','native_signals_enabled':True}
            telemetry_state["api_poll_interval_sec"] = round(dhan.oc_poll_gap, 1)

            if active_pos and active_pos["index"] == idx_name:
                strike_k = active_pos["strike"]
                opt_side = "ce" if active_pos["option_type"] == "CE" else "pe"
                oc_dict = raw_oc.get("oc", {})
                node = oc_dict.get(f"{strike_k:.6f}", oc_dict.get(str(strike_k), {})).get(opt_side, {})
                curr_ltp = float(node.get("top_bid_price",0) or 0)
                if (not math.isfinite(curr_ltp) or curr_ltp <= 0 or not 0<=time.time()-chain_received_epoch<=8
                        or float(node.get("top_bid_quantity",0) or 0) < active_pos["qty"]):
                    logger.warning("Exit quote unavailable or insufficient depth; position retained for reconciliation.")
                    continue
                observed_ltp=float(node.get('last_price') or 0)
                active_pos.update(current_ltp=observed_ltp if math.isfinite(observed_ltp) and observed_ltp>0 else None,
                                  current_bid=curr_ltp,current_ask=node.get('top_ask_price'),
                                  quote_received_epoch=chain_received_epoch)
                # Restored older positions retain their original fixed stop distance.
                position_risk = active_pos.get("initial_risk_points", cfg["sl_points"])

                elapsed_min = (now - active_pos["entry_dt"]).total_seconds() / 60.0
                exit_reason = None
                channel_position=(active_pos.get('market_structure') or {}).get('telegram_call')
                if channel_position and call_feed:
                    targets=call_feed.targets_for(channel_position,time.time())
                    if targets:channel_position['targets']=targets
                if channel_position:
                    exit_reason=protect_channel_position(active_pos,curr_ltp,time.time())
                    # Persist peaks and protection even when no native ratchet runs.
                    ledger['active_position']={k:v for k,v in active_pos.items() if k!='entry_dt'}
                    atomic_write_json(ledger_path,ledger)

                bid_for_protection=float(node.get('top_bid_price',0) or 0)
                floor=index_profit_lock_bid(day_net_pnl,active_pos)
                if floor is not None and math.isfinite(bid_for_protection) and bid_for_protection>=floor:
                    active_pos['current_sl']=max(active_pos['current_sl'],floor)
                    active_pos['profit_floor_armed']=True
                    if not profit_protected:
                        profit_protected=True
                        ledger['daily_profit_protection']={'day':today_str,'armed':True,'net_floor':INDEX_PROFIT_LOCK_RS}
                        ledger['active_position']={k:v for k,v in active_pos.items() if k!='entry_dt'}
                        atomic_write_json(ledger_path,ledger)
                        logger.info('Index daily profit protection armed at Rs.%.0f net; no further entries today.',INDEX_PROFIT_LOCK_RS)

                if not exit_reason and active_pos.get('profit_floor_armed') and floor is not None and 0<bid_for_protection<=floor:
                    exit_reason='DAILY_PROFIT_PROTECTION'
                    curr_ltp=bid_for_protection
                elif not exit_reason and curr_ltp <= active_pos["current_sl"]:
                    exit_reason = (
                        "ASYMMETRIC_TRAIL_PROFIT"
                        if active_pos["ratchet_armed"] and curr_ltp > active_pos["entry_price"]
                        else "INITIAL_SL_HIT"
                    )
                elif not exit_reason and elapsed_min >= 18.0 and (active_pos["highest_ltp"] - active_pos["entry_price"]) < (0.55 * position_risk):
                    exit_reason = "THETA_TIME_STOP"
                elif not exit_reason and elapsed_min >= float((active_pos.get("market_structure") or {}).get("max_hold_minutes",float("inf"))):
                    exit_reason = "MAX_HOLD_TIME"
                elif not exit_reason and now.time() >= EOD_SQUAREOFF_TIME:
                    exit_reason = "EOD_SQUAREOFF"

                executable_bid=float(node.get("top_bid_price",0) or 0)
                if channel_position and not exit_reason:
                    targets=[v for v in channel_position.get('targets',[]) if v>active_pos['entry_price']]
                    if targets and executable_bid>=min(targets):
                        exit_reason='TELEGRAM_CHANNEL_TARGET'
                        curr_ltp=executable_bid
                runner_hold=False
                if not channel_position and not exit_reason and profit_protected and executable_bid>0:
                    assessment=assess_index_runner(view,active_pos,node,now.timestamp(),day_net_pnl)
                    runner_hold=assessment['hold']
                    previous=(active_pos.get('runner_assessment') or {}).get('hold')
                    active_pos['runner_assessment']=assessment
                    if previous!=runner_hold:
                        logger.info('[INDEX RUNNER] %s | %s',idx_name,json.dumps(assessment))
                        ledger['active_position']={k:v for k,v in active_pos.items() if k!='entry_dt'}
                        atomic_write_json(ledger_path,ledger)
                    if not runner_hold:
                        exit_reason='PROFIT_BOOKED_NO_RUNNER'
                        curr_ltp=executable_bid
                ref_exit_p = executable_bid if (executable_bid > 0 and not live_mode) else curr_ltp
                if not channel_position and not exit_reason and not runner_hold and idx_name == "NIFTY" and (ref_exit_p - active_pos["entry_price"]) >= NIFTY_TARGET_POINTS:
                    exit_reason = "TARGET_2_20_POINTS"
                    if not live_mode and executable_bid > 0:
                        curr_ltp = executable_bid
                elif not channel_position and not exit_reason and daily_target_reached(day_net_pnl,active_pos,executable_bid,
                        INDEX_RUNNER_TARGET_RS if runner_hold else DAILY_PROFIT_TARGET_RS):
                    exit_reason="TARGET_2_20_POINTS" if curr_ltp-active_pos['entry_price']>=18.0 else "DAILY_NET_PROFIT_TARGET"
                    if not live_mode and executable_bid > 0:curr_ltp=executable_bid

                staged = "target_one_qty" in active_pos
                if not exit_reason and staged:
                    if not active_pos["target_one_done"] and curr_ltp >= active_pos["entry_price"]+12:
                        if live_mode:
                            oid = dhan.place_market_order(active_pos["security_id"],cfg["fno_segment"],"SELL",active_pos["target_one_qty"],tag="NIFTY_T1")
                            if not oid:
                                logger.error("T1 submission unconfirmed; stopping for broker reconciliation.")
                                break
                        book_target_one(ledger,active_pos,math.floor(curr_ltp*0.999*20+1e-9)/20,now.strftime("%Y-%m-%d %H:%M:%S"))
                        logger.info("[%s PARTIAL EXIT] NIFTY T1 +12 points | Qty %d @ %.2f | Remaining %d", "LIVE" if live_mode else "PAPER", active_pos["target_one_qty"],curr_ltp,active_pos["qty"])
                    if not runner_hold and curr_ltp >= active_pos["entry_price"]+22:
                        exit_reason = "TARGET_2_20_POINTS"
                if not channel_position and not exit_reason and not staged and curr_ltp > active_pos["highest_ltp"]:
                    active_pos["highest_ltp"] = curr_ltp
                    new_sl, armed = update_ratchet_sl(
                        active_pos["entry_price"], active_pos["current_sl"],
                        active_pos["highest_ltp"], position_risk
                    )
                    if runner_hold:
                        new_sl=max(active_pos['current_sl'],floor or 0,
                                   round(active_pos['highest_ltp']-position_risk,2))
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
                        if not exit_order:
                            logger.error("Final submission unconfirmed; stopping for broker reconciliation.")
                            break
                    exit_price = math.floor(curr_ltp*0.999*20+1e-9)/20
                    fills = active_pos.get("exit_fills",[]) + ([{"qty":active_pos["qty"],"price":exit_price,"timestamp":now.strftime("%Y-%m-%d %H:%M:%S"),"reason":exit_reason}] if staged else [])
                    if fills:
                        exit_price = sum(f["qty"]*f["price"] for f in fills)/active_pos["original_qty"]
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
                        initial_risk_points=position_risk,
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
                    telegram.exited(ledger["trades"][-1])
                    active_pos = None

            elif (
                SESSION_START_TIME <= now.time() <= LAST_ENTRY_TIME
                and day_net_pnl < DAILY_PROFIT_TARGET_RS
                and day_net_pnl < INDEX_PROFIT_LOCK_RS and not profit_protected
                and day_entries < MAX_TRADES_PER_DAY
                and consec_losses < MAX_CONSECUTIVE_LOSSES_PER_DAY
                and (cooldown_until is None or now >= cooldown_until)
            ):
                channel_call=None
                if call_feed:
                    for candidate in call_feed.candidates(idx_name,time.time(),ledger.get('telegram_call_entries',[])):
                        channel_view,reason=channel_entry_view(view,raw_oc.get('oc',{}),candidate,
                            expiries.get(idx_name),chain_received_epoch,time.time())
                        if reason!=last_call_decision.get(candidate['key']):
                            logger.info('[TELEGRAM CALL] %s | %s',candidate['source_url'],reason)
                            last_call_decision[candidate['key']]=reason
                        if channel_view is not None:
                            view=channel_view
                            channel_call=candidate
                            break
                if channel_call:
                    action_dir,sig='BUY_'+channel_call['option_type'],'TELEGRAM_CHANNEL '+channel_call['key']
                else:
                    action_dir, sig = brain.select_action(view, consec_losses, bar_time=now.time())
                if action_dir in ("BUY_CE", "BUY_PE") and sig:
                    is_ce = action_dir == "BUY_CE"
                    opt_type = "CE" if is_ce else "PE"
                    strike_k = view["atm_ce_strike"] if is_ce else view["atm_pe_strike"]
                    entry_p = float(view.get("atm_ce_ask" if is_ce else "atm_pe_ask") or 0)
                    sec_id = view["atm_ce_sec_id"] if is_ce else view["atm_pe_sec_id"]

                    entry_p=math.ceil(entry_p*1.001*20-1e-9)/20
                    if channel_call and (not 0<=time.time()-channel_call['posted_epoch']<=TelegramCallFeed.MAX_AGE
                                         or entry_p>float(channel_call.get('entry_max',channel_call['buy_above']*1.02))):
                        view['entry_decision']={'action':'HOLD','reason':'Channel call stale or modeled fill beyond trigger tolerance'}
                        continue
                    if entry_p >= cfg["min_option_premium"]:
                        lots = brain.compute_lots(
                            idx_name,
                            sig,
                            entry_p,
                            ledger["virtual_balance_net"],
                            consec_losses,
                            analysis=view,
                        )
                        if channel_call:
                            side='ce' if is_ce else 'pe'
                            lots=channel_entry_lots(max(1,lots),ledger['virtual_balance_net'],entry_p,cfg['lot_size'],
                                float(view.get(f'atm_{side}_bid_qty') or 0),float(view.get(f'atm_{side}_ask_qty') or 0),
                                consec_losses,brain.rules.get(sig,{}).get('last_lot_loss',False))
                        qty = lots * cfg["lot_size"]
                        if qty <= 0 or entry_p * qty + 40 + 24.5 * lots > ledger["virtual_balance_net"] or not 0 <= time.time()-chain_received_epoch <= 8:
                            view["entry_decision"]={"action":"HOLD","reason":"Insufficient cash or stale executable quote","rule":sig}
                            continue
                        quote_side='ce' if is_ce else 'pe'
                        bid=float(view.get(f'atm_{quote_side}_bid') or 0)
                        ask=float(view.get(f'atm_{quote_side}_ask') or 0)
                        if not spread_and_liquidity_ok(bid,ask,(bid+ask)/2,
                                float(view.get(f'atm_{quote_side}_bid_qty') or 0),
                                float(view.get(f'atm_{quote_side}_ask_qty') or 0),lots,cfg['lot_size'],entry_p):
                            view['entry_decision']={'action':'HOLD','reason':'Spread or liquidity filter at sized quantity','rule':sig}
                            continue
                        entry_node=raw_oc.get("oc",{}).get(f"{strike_k:.6f}",raw_oc.get("oc",{}).get(str(strike_k),{})).get("ce" if is_ce else "pe",{})
                        view.setdefault("market_structure",{})["entry_quote"]={
                            "received_epoch":chain_received_epoch,"decision_epoch":time.time(),
                            "modeled_entry_price_basis":"EXECUTABLE_ASK_PLUS_0.1PCT_SLIPPAGE",
                            **{k:entry_node.get(k) for k in ("last_price","top_bid_price","top_ask_price","top_bid_quantity","top_ask_quantity")}}
                        if live_mode and not sec_id:
                            logger.error("Missing option security ID; no entry submitted.")
                            continue
                        if channel_call and not 0<=time.time()-channel_call['posted_epoch']<=TelegramCallFeed.MAX_AGE:
                            continue
                        if not reserve_index_entry(ledger,today_str,{"index":idx_name,"security_id":sec_id,"option_type":opt_type,"qty":qty,"entry_ts":now.strftime("%Y-%m-%d %H:%M:%S"),
                            **({'telegram_call_key':channel_call['key']} if channel_call else {})},ledger_path):
                            continue
                        day_entries=index_entries_today(ledger,today_str)
                        if live_mode:
                            entry_order=dhan.place_market_order(
                                security_id=sec_id,
                                exchange_segment=cfg["fno_segment"],
                                transaction_type="BUY",
                                quantity=qty,
                            )
                            if not entry_order or entry_order=="None":
                                logger.error("Entry submission unconfirmed; slot remains consumed. Reconcile before restarting.")
                                break
                        initial_risk = (10.0 if idx_name=='NIFTY' else 20.0) if channel_call else option_stop_points(idx_name, entry_p)
                        init_sl = round(entry_p - initial_risk, 2)
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
                            "ratchet_armed": False,
                            "entry_dt": now,
                            "entry_epoch": now.timestamp(),
                            "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "spot_at_entry": view["spot"],
                            "oi_diff_lakhs": view["diff_ce_minus_pe_lakhs"],
                            "rule_used": sig,
                            "state_vec": view["state_vec"],
                            "market_structure":view.get("market_structure"),
                        }
                        split = nifty_target_quantities(idx_name,lots,cfg["lot_size"])
                        if split and not channel_call:
                            active_pos.update(original_qty=qty,target_one_qty=split[0],target_one_done=False,exit_fills=[])
                        ledger["mode"]="LIVE" if live_mode else "PAPER"
                        ledger["active_position"]={k:v for k,v in active_pos.items() if k!="entry_dt"}
                        ledger.pop("pending_entry",None)
                        atomic_write_json(ledger_path,ledger)
                        telegram.position(active_pos, entry=True)
                        logger.info(
                            f"[{'LIVE' if live_mode else 'PAPER'} ENTRY] {idx_name} {strike_k} {opt_type} "
                            f"| Lots: {lots} (Qty: {qty}) @ Rs.{entry_p:.2f} | SL: Rs.{init_sl:.2f} "
                            f"| 5m Spot: {view['d_spot_5m']:+.1f} | 3-Strike Diff = {view['diff_ce_minus_pe_lakhs']:+.2f}L"
                        )

            if idx_name=="NIFTY" and not held_channel:
                view["execution_context"]={"open_position":bool(active_pos),"closed_trades_today":day_trades,"entries_today":day_entries,"daily_entry_cap":MAX_TRADES_PER_DAY,
                    "consecutive_net_losses":consec_losses,"realized_net_today":day_net_pnl,
                    "cooldown_until":cooldown_until.isoformat() if cooldown_until else None}
                # Observe one decision per closed candle/readiness state, without
                # extra API requests or treating a skipped trade as realized P&L.
                try:
                    closed=[t for t in (structure_candles or {}).get("timestamp",[]) if float(t)+300<=time.time()]
                    audit_key=(max(closed) if closed else None,bool(view.get("oi_analysis",{}).get("ready")))
                    if audit_key!=last_entry_audit:
                        path=os.path.join(CACHE_DIR,"nifty_entry_audit_"+now.strftime("%Y-%m-%d")+".jsonl")
                        if record_nifty_entry_observation(path,view,structure_candles,raw_oc.get("oc",{}),time.time()):
                            last_entry_audit=audit_key
                except (OSError,KeyError,TypeError,ValueError,OverflowError) as error:
                    logger.warning("Entry review observation could not be saved: %s",type(error).__name__)

            if active_pos:
                telegram.position(active_pos)
            telemetry_state["active_position"] = (
                {k: v for k, v in active_pos.items() if k != "entry_dt"} if active_pos else None
            )
            atomic_write_json("live_telemetry.json", telemetry_state)

        if not raw_oc:
            time.sleep(1.0)
        cycle += 1

    telegram.close()
    if call_feed:call_feed.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DhanHQ v4.9.1 Final Production Real-Data Bot")
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

    requested_indices=args.indices or ("NIFTY" if args.backtest_years>0 or args.backtest_days>0 else "NIFTY,SENSEX")
    selected_indices = [x.strip().upper() for x in requested_indices.split(",")]
    if not selected_indices or any(x not in INDICES_CONFIG for x in selected_indices):
        parser.error("--indices must contain NIFTY and/or SENSEX")

    if args.backtest_years > 0:
        result=run_real_historical_backtest(days=args.backtest_years * 365, indices_to_test=selected_indices, interval=args.interval, logger=logger,output_dir=args.backtest_output_dir)
        if result["backtest_status"]!="REPLAY_COMPLETED":raise SystemExit(2)
    elif args.backtest_days > 0:
        result=run_real_historical_backtest(days=args.backtest_days, indices_to_test=selected_indices, interval=args.interval, logger=logger,output_dir=args.backtest_output_dir)
        if result["backtest_status"]!="REPLAY_COMPLETED":raise SystemExit(2)
    else:
        run_intraday_market_session(live_mode=args.live, logger=logger,indices_to_trade=selected_indices)

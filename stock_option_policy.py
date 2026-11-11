"""Conservative stock-option planning and a persistent slot-based daily entry gate.

Rewards, losses, fees and slippage are estimates, never guaranteed fills or returns.
The daily gate consumes a slot before submission so an ambiguous order result
cannot release that slot after a restart.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import contextmanager
from time import time as request_clock, sleep as request_sleep
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


def session_open_wait_seconds(now, opening_time):
    """Shorten the last pre-open sleep to the remaining wall-clock interval."""
    opening=now.replace(hour=opening_time.hour,minute=opening_time.minute,
                        second=opening_time.second,microsecond=0)
    return min(1.0,max(0.0,(opening-now).total_seconds()))

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional


TICK = Decimal("0.05")


def single_bot_session(role):
    """Windows process-lifetime mutex; duplicates cannot own the same ledger."""
    from functools import wraps
    def decorate(function):
        @wraps(function)
        def guarded(*args,**kwargs):
            if os.name != "nt":
                return function(*args,**kwargs)
            import ctypes
            import hashlib
            identity=hashlib.sha256(str(Path(__file__).resolve().parent).lower().encode()).hexdigest()[:20]
            kernel=ctypes.WinDLL("kernel32",use_last_error=True)
            kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
            kernel.CreateMutexW.restype=ctypes.c_void_p
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            kernel.ReleaseMutex.argtypes=[ctypes.c_void_p]
            handle=kernel.CreateMutexW(None,True,"Local\\DhanBot_"+identity+"_"+role)
            error=ctypes.get_last_error()
            if not handle:raise OSError(error,"Cannot acquire bot session mutex")
            if error==183:
                kernel.CloseHandle(handle)
                raise RuntimeError("An existing "+role+" session owns this project; duplicate startup refused")
            try:return function(*args,**kwargs)
            finally:
                kernel.ReleaseMutex(handle)
                kernel.CloseHandle(handle)
        return guarded
    return decorate


@contextmanager
def option_chain_request_slot(minimum_gap=3.05, lock_path=None, *, pace_from_completion=True):
    """Serialize both bots' option-chain requests, including their HTTP wait."""
    path=Path(lock_path) if lock_path else Path(__file__).resolve().parent/'dhan_hist_cache'/'oc_global_lock.txt'
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(str(path),os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(descriptor,'r+b',buffering=0) as stream:
        if os.name=='nt':
            import msvcrt
            while True:
                try:
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                    break
                except OSError as error:
                    if error.errno not in (11,13,36):raise
                    request_sleep(0.05)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:
            stream.seek(0)
            try:last=float(stream.read(64).decode('ascii').strip() or '0')
            except (ValueError,UnicodeError):last=request_clock()
            now=request_clock()
            if not math.isfinite(last):last=now
            wait=max(0.0,minimum_gap-(now-min(last,now)))
            if wait:request_sleep(wait)
            def stamp():
                stream.seek(0)
                stream.write(f'{request_clock():.6f}'.ljust(64).encode('ascii'))
                stream.truncate(64)
            stamp()
            try:yield
            finally:
                if pace_from_completion:
                    stamp()
        finally:
            if os.name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def market_quote_request_slot():
    path = Path(__file__).resolve().parent / "dhan_hist_cache" / "quote_global_lock.txt"
    return option_chain_request_slot(1.05, path, pace_from_completion=False)


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("A finite number is required")
    return result


def _percentage_plan(
    entry: float,
    qty: int,
    charge_fn: Callable[[float, float, int], float],
    target_pct: float = 3.0,
    stop_pct: float = 0.5,
    min_net_rr: float = 3.0,
    slippage_pct: float = 0.1,
    cash_loss_limit=None,
) -> Optional[Dict[str, float]]:
    """Return a tick-rounded plan only if estimated net reward/risk qualifies.

    Percentages refer to option premium. Target rounds down and stop rounds up,
    keeping their distances within the requested limits. Charges are round-trip
    estimates from ``charge_fn(entry, exit, quantity)``. Each scenario also
    budgets adverse exit slippage as a percentage of its intended exit price.
    """
    try:
        price = _decimal(entry)
        quantity = _decimal(qty)
        target_percent = _decimal(target_pct)
        stop_percent = _decimal(stop_pct)
        required_rr = _decimal(min_net_rr)
        slippage_percent = _decimal(slippage_pct)
        if (
            isinstance(qty, bool)
            or price <= 0
            or quantity <= 0
            or quantity != quantity.to_integral_value()
            or target_percent <= 0
            or not Decimal(0) < stop_percent < Decimal(100)
            or required_rr <= 0
            or not Decimal(0) <= slippage_percent < Decimal(100)
        ):
            return None

        target = (
            price * (1 + target_percent / 100) / TICK
        ).to_integral_value(rounding=ROUND_FLOOR) * TICK
        stop = (
            price * (1 - stop_percent / 100) / TICK
        ).to_integral_value(rounding=ROUND_CEILING) * TICK
        reward_distance, risk_distance = target - price, price - stop
        if stop <= 0 or reward_distance < TICK or risk_distance < TICK:
            return None

        units = int(quantity)
        target_fees = _decimal(charge_fn(float(price), float(target), units))
        stop_fees = _decimal(charge_fn(float(price), float(stop), units))
        if target_fees < 0 or stop_fees < 0:
            return None
        target_costs = target_fees + target * slippage_percent / 100 * quantity
        stop_costs = stop_fees + stop * slippage_percent / 100 * quantity
        net_reward = reward_distance * quantity - target_costs
        net_risk = risk_distance * quantity + stop_costs
        if cash_loss_limit is not None:
            net_risk = min(net_risk, _decimal(cash_loss_limit))
        if net_reward <= 0 or net_risk <= 0 or net_reward / net_risk < required_rr:
            return None

        values = {
            "opt_1r": risk_distance,
            "initial_sl": stop,
            "min_rr_target_price": target,
            "hard_target_price": target,
            "trail_jump": Decimal(0),
            "expected_net_reward": net_reward,
            "expected_net_risk": net_risk,
            "expected_net_rr": net_reward / net_risk,
            "estimated_costs": max(target_costs, stop_costs),
            "target_estimated_costs": target_costs,
            "stop_estimated_costs": stop_costs,
        }
        result = {key: float(value) for key, value in values.items()}
        return result if all(math.isfinite(value) for value in result.values()) else None
    except (InvalidOperation, ValueError, TypeError, OverflowError, ArithmeticError):
        return None


def premium_plan(entry, qty, charge_fn, target_pct=3.0, stop_pct=0.5,
                 min_net_rr=3.0, slippage_pct=0.1, rupee_target=0,
                 max_lots=1, rupee_max_risk=float("inf"), extend_rupee_target=False,
                 max_target_price=None, risk_per_lot=False, min_lots=1,
                 cash_loss_limit=None):
    """Choose the smallest lot count reaching the net rupee target within limits.

    qty is the exchange lot size. Rupee target prices include estimated fees
    and adverse exit slippage; these estimates cannot guarantee execution.
    """
    if (not isinstance(max_lots, int) or isinstance(max_lots, bool) or max_lots < 1
            or not math.isfinite(rupee_target) or rupee_target < 0
            or math.isnan(rupee_max_risk) or rupee_max_risk <= 0):
        return None
    if cash_loss_limit is not None:
        try:
            if isinstance(cash_loss_limit,bool) or not math.isfinite(float(cash_loss_limit)) or float(cash_loss_limit)<=0:return None
        except (TypeError,ValueError,OverflowError):return None
    if not isinstance(extend_rupee_target,bool) or not isinstance(risk_per_lot,bool):return None
    if isinstance(min_lots,bool) or not isinstance(min_lots,int) or not 1<=min_lots<=max_lots:return None
    if max_target_price is not None:
        try:
            if not math.isfinite(float(max_target_price)) or float(max_target_price)<=float(entry):return None
            max_target_price=float(max_target_price)
        except (ValueError,TypeError,OverflowError):return None
    if extend_rupee_target:
        try:
            if isinstance(target_pct,bool) or not math.isfinite(float(target_pct)) or not 0<float(target_pct)<=100:return None
            target_pct=float(target_pct)
        except (ValueError,TypeError,OverflowError):return None
    for lots in range(min_lots, max_lots + 1):
        risk_budget = rupee_max_risk * lots if risk_per_lot else rupee_max_risk
        if isinstance(qty, bool):
            return None
        units = qty * lots
        # In explicit rupee-target mode, a percentage booking preference must
        # not force extra lots or veto an achievable cash target. Still bound
        # target search to at most a doubling of premium; executable Greeks and
        # underlying move feasibility are checked during qualification.
        ceiling_pct=min(25.0,max(target_pct,18.0)) if extend_rupee_target and rupee_target else target_pct
        plan = _percentage_plan(entry, units, charge_fn, ceiling_pct, stop_pct,
                                min_net_rr, slippage_pct, cash_loss_limit)
        if not plan or plan["expected_net_reward"] < rupee_target or plan["expected_net_risk"] > risk_budget:
            continue
        target_price = plan["hard_target_price"]
        if extend_rupee_target and max_target_price is not None:
            target_price=min(target_price,math.floor(max_target_price*20+1e-9)/20)
        if rupee_target:
            stop_fill = math.floor(plan["initial_sl"]*(1-slippage_pct/100)*20 + 1e-9)/20
            risk = (entry-stop_fill)*units + charge_fn(entry,stop_fill,units)
            plan['planned_premium_stop_risk'] = risk
            if cash_loss_limit is not None:risk=min(risk,float(cash_loss_limit))
            if risk <= 0 or risk > risk_budget:
                continue
            plan["expected_net_risk"] = risk
            required_reward = max(rupee_target, min_net_rr * risk)
            # Integer tick bisection avoids a slow linear search for expensive options.
            lo = math.floor(entry / float(TICK)) + 1
            hi = round(target_price / float(TICK))
            if hi<lo:
                continue
            while lo < hi:
                mid = (lo + hi) // 2
                price = mid * float(TICK)
                fill = math.floor(price * (1 - slippage_pct / 100)*20 + 1e-9)/20
                net = (fill - entry) * units - charge_fn(entry, fill, units)
                if net >= required_reward:
                    hi = mid
                else:
                    lo = mid + 1
            target_price = round(lo * float(TICK), 2)
            fill = math.floor(target_price * (1 - slippage_pct / 100)*20 + 1e-9)/20
            reward = (fill-entry)*units - charge_fn(entry, fill, units)
            if reward < required_reward:
                continue
            plan.update(expected_net_reward=reward, expected_net_rr=reward/plan["expected_net_risk"])
            plan["net_profit_target"] = required_reward
            plan["trail_jump"] = max(float(TICK), round((target_price-entry)*0.25 / float(TICK))*float(TICK))
            if extend_rupee_target:
                plan.update(hard_target_price=target_price,min_rr_target_price=target_price,
                            target_pct_reference=target_pct,
                            target_estimated_costs=charge_fn(entry,fill,units)+(target_price-fill)*units)
                plan['estimated_costs']=max(plan['target_estimated_costs'],plan['stop_estimated_costs'])
        plan.update(lots=lots, qty=units, rupee_target_price=target_price)
        if risk_per_lot:
            plan.update(risk_limit_per_lot=rupee_max_risk,
                        total_risk_limit=risk_budget,
                        expected_net_risk_per_lot=plan['expected_net_risk']/lots)
        return plan
    return None


def _finite_score(value: Any) -> float:
    try:
        score = float(value)
        return score if math.isfinite(score) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def signal_quality(candidate: Dict[str, Any]) -> float:
    """Read an optional confirmation-quality score, defaulting safely to zero."""
    return _finite_score(candidate.get("signal_quality", 0.0))


def stock_reentry_reason(symbol, trades, as_of, signal_timestamp=None):
    """A loss need not blacklist a symbol all day; require a fresh setup after 15m.

    Inspect closing time before outcome, so replay never uses a future P&L.
    Daily slot caps and open-position checks are enforced independently.
    """
    ist=timezone(timedelta(hours=5,minutes=30));day=datetime.fromtimestamp(as_of,ist).date()
    closed=[]
    for trade in trades:
        if trade.get('symbol')!=symbol:continue
        try:
            entered=datetime.fromisoformat(trade['timestamp'])
            if entered.tzinfo is None:entered=entered.replace(tzinfo=ist)
            if entered.date()!=day or entered.timestamp()>as_of:continue
            exited=datetime.fromisoformat(trade['exit_timestamp'])
            if exited.tzinfo is None:exited=exited.replace(tzinfo=ist)
            if exited.timestamp()>as_of:return 'Earlier symbol position has not closed'
            net=float(trade['net_pnl'])
            if not math.isfinite(net):return 'Earlier symbol outcome is invalid'
            if net>=0:return 'Profit or breakeven already booked for this symbol today'
            closed.append(exited.timestamp())
        except (KeyError,ValueError,TypeError,OverflowError):return 'Earlier symbol trade needs reconciliation'
    if not closed:return None
    last_exit=max(closed)
    if as_of-last_exit<900:return '15-minute cooldown after the previous net loss'
    if signal_timestamp is not None and signal_timestamp<=last_exit:
        return 'Re-entry needs a new confirmation candle after the loss'
    return None


def selection_analysis(candidate):
    """Auditable bounded priority heuristic, never a win probability."""
    quality=signal_quality(candidate)
    learned=_finite_score(candidate.get('learning_feedback',{}).get('quality_adjustment',0))
    execution=0.0
    fraction=candidate.get('spread_fraction_of_initial_risk')
    age=candidate.get('confirmation_age_seconds')
    try:
        if fraction is not None and math.isfinite(float(fraction)) and 0<=float(fraction)<=.4:
            execution+=5*(1-float(fraction)/.4)
        if age is not None and math.isfinite(float(age)) and 0<=float(age)<=600:
            execution+=3*(1-float(age)/600)
    except (TypeError,ValueError,OverflowError):pass
    return {'score':round(quality+learned+execution,4),'breakout_quality':quality,
            'learning_adjustment':learned,'execution_bonus':round(execution,4),
            'planned_net_rr':_finite_score(candidate.get('expected_net_rr',0)),
            'basis':'CONFIRMED_SETUP_VOLUME_CLOSE_EXECUTION_AND_CLOSED_NET_FEEDBACK'}


def adaptive_setup_feedback(candidate, closed_trades, as_of):
    """Walk-forward PAPER feedback across symbols, with chronological validation.

    No code/threshold rewriting: only bounded ranking, temporary setup pauses
    and earlier fee-aware profit protection. Insufficient evidence is neutral.
    """
    def key(record):
        diagnostics=record.get('entry_diagnostics') or record
        structure=record.get('market_structure') or {}
        call=structure.get('telegram_call') or {}
        if record.get('index') in ('NIFTY','SENSEX'):
            if not call.get('source') and not structure.get('setup'):return None
            family=('CHANNEL:'+str(call['source']) if call.get('source') else
                    'NATIVE:'+str(structure.get('setup','UNKNOWN')))
            side=record.get('option_type')
            if not side:
                suffix=str(record.get('contract','')).rsplit('_',1)[-1]
                side=suffix if suffix in ('CE','PE') else None
            if side not in ('CE','PE'):return None
            return (record['index'],family,side)
        family=(diagnostics.get('breakout_confirmation') or {}).get('entry_path')
        if not family:family=record.get('scanner')
        return ('STOCK',family,record.get('opt_type')) if family and record.get('opt_type') in ('CE','PE') else None
    result={'basis':'CLOSED_PAPER_SETUP_WALK_FORWARD','setup_key':key(candidate),
            'closed_trades':0,'independent_days':0,'quality_adjustment':0.0,'paused':False,
            'exit_policy':'STANDARD','reason':'Insufficient independent closed outcomes'}
    wanted=result['setup_key']
    if wanted is None:return result
    cutoff=datetime.fromtimestamp(as_of,timezone.utc);samples=[];seen=set()
    for trade in closed_trades:
        try:
            if key(trade)!=wanted:continue
            origin=str(trade.get('data_source',trade.get('data_health',''))).upper()
            if any(s in origin for s in ('BACKTEST','HISTOR','ROLLING')) or trade.get('mode','PAPER')!='PAPER':continue
            exited=datetime.fromisoformat(trade['exit_timestamp'])
            if exited.tzinfo is None:exited=exited.replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
            if not timedelta(0)<=cutoff-exited<=timedelta(days=60):continue
            identity=trade.get('entry_id') or (trade.get('timestamp'),trade.get('symbol',trade.get('index')),trade.get('contract'))
            if identity in seen:continue
            risk=float(trade.get('learning_risk_rs',0) or 0)
            if risk<=0:risk=float(trade.get('initial_risk_points',0))*float(trade.get('qty',0))
            net=float(trade['net_pnl'])
            if risk<=0 or not all(math.isfinite(v) for v in (net,risk)):continue
            reward=max(-3.0,min(3.0,net/risk))
            diag=trade.get('exit_diagnostics') or {}
            peak=diag.get('peak_executable_vwap',(((trade.get('market_structure') or {}).get('telegram_call') or {}).get('exit_observation') or {}).get('peak_bid'));giveback=None
            if peak is not None and len(trade.get('exit_fills') or [])<=1:
                peak_net=(float(peak)-float(trade['entry_price']))*float(trade['qty'])-(float(trade['gross_pnl'])-net)
                if math.isfinite(peak_net) and peak_net>=0.5*risk:
                    giveback=net<0.5*peak_net
            seen.add(identity);samples.append((exited,reward,giveback))
        except (KeyError,TypeError,ValueError,OverflowError):continue
    samples=sorted(samples,key=lambda x:x[0])[-40:]
    days=len({s[0].astimezone(timezone(timedelta(hours=5,minutes=30))).date() for s in samples})
    result.update(closed_trades=len(samples),independent_days=days)
    if len(samples)<8 or days<3:return result
    split=max(5,len(samples)*2//3);train=samples[:split];validation=samples[split:]
    if len(validation)<3:return result
    train_r=sum(s[1] for s in train)/len(train);valid_r=sum(s[1] for s in validation)/len(validation)
    same_direction=train_r*valid_r>0
    adjustment=max(-5.0,min(5.0,5*valid_r*len(samples)/(len(samples)+10))) if same_direction else 0.0
    paused=(len(samples)>=12 and train_r<-.25 and valid_r<-.15
            and all(s[1]<0 for s in samples[-3:]) and cutoff-samples[-1][0]<timedelta(days=1))
    givebacks=[s[2] for s in validation if s[2] is not None]
    protect=(len(givebacks)>=3 and sum(givebacks)/len(givebacks)>=.5 and valid_r<train_r)
    result.update(training_trades=len(train),validation_trades=len(validation),
                  training_net_r=round(train_r,4),validation_net_r=round(valid_r,4),
                  quality_adjustment=round(adjustment,4),paused=paused,
                  exit_policy='PROTECT_EARNED_PROFIT' if protect else 'STANDARD',
                  reason='Chronological validation supports adaptation' if same_direction or protect else 'Validation disagrees; retain standard policy')
    return result


def rank_candidates(candidates: Iterable[Dict[str, Any]], closed_trades=(), as_of=None) -> list[Dict[str, Any]]:
    """Rank quality with bounded feedback from this symbol/setup's closed net outcomes.

    Feedback never increases size or relaxes entry checks. Use only outcomes known
    at decision time, including during replay. Unknown/legacy risk is not invented.
    """
    candidates=[dict(c) for c in candidates]
    closed_trades=list(closed_trades)
    if as_of is not None:
        cutoff=datetime.fromtimestamp(as_of,timezone.utc)
        for candidate in candidates:
            outcomes=[]
            for trade in closed_trades:
                if any(trade.get(k)!=candidate.get(k) for k in ('symbol','scanner','opt_type')):
                    continue
                try:
                    exited=datetime.fromisoformat(trade['exit_timestamp'])
                    if exited.tzinfo is None:exited=exited.replace(tzinfo=timezone(timedelta(hours=5,minutes=30)))
                    if exited>cutoff:continue
                    net=float(trade['net_pnl']);risk=float(trade.get('learning_risk_rs',0))
                    if risk<=0 or not all(math.isfinite(v) for v in (net,risk)):continue
                    outcomes.append((exited,net/risk,net))
                except (KeyError,TypeError,ValueError,OverflowError):continue
            outcomes=sorted(outcomes,key=lambda r:r[0])[-20:]
            count=len(outcomes)
            expectancy=sum(r[1] for r in outcomes)/count if count else 0.
            adjustment=max(-10.,min(10.,10*expectancy*count/(count+5))) if count>=3 else 0.
            paused=(count>=5 and expectancy<0 and all(r[2]<0 for r in outcomes[-3:])
                    and cutoff-outcomes[-1][0]<timedelta(days=5))
            candidate['learning_feedback']={'closed_trades':count,'net_expectancy_r':round(expectancy,4),
                'quality_adjustment':round(adjustment,4),'paused':paused,'basis':'CLOSED_NET_PNL_SAME_SYMBOL_SCANNER_SIDE'}
            pooled=adaptive_setup_feedback(candidate,closed_trades,as_of)
            candidate['adaptive_feedback']=pooled
            candidate['learning_feedback']['quality_adjustment']=max(-10.0,min(10.0,adjustment+pooled['quality_adjustment']))
            candidate['learning_feedback']['paused']=paused or pooled['paused']
        candidates=[c for c in candidates if not c['learning_feedback']['paused']]
        for candidate in candidates:candidate['selection_analysis']=selection_analysis(candidate)
    return sorted(
        candidates,
        key=lambda candidate: (
            -selection_analysis(candidate)['score'],
            -_finite_score(candidate.get("expected_net_rr", 0.0)),
            str(candidate.get("symbol", "")).upper(),
        ),
    )


class DailyTradeGate:
    """Persist capped consumed entries per calendar day across processes/restarts.

    Call ``reserve`` immediately before entry. A reservation is never released,
    even for failed or ambiguous submissions. ``mark_closed`` records an outcome
    without making that day available again. The caller supplies the trading
    date in its exchange timezone as an ISO date, e.g. ``2026-10-22``.
    """

    def __init__(self, path: os.PathLike[str] | str, max_entries: int = 1):
        if not 1 <= max_entries <= 4:
            raise ValueError("Daily entries must be between 1 and 4")
        self.path = Path(path)
        self.max_entries = max_entries

    def _day_path(self, day: str, slot: int = 1) -> Path:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("day must be an ISO date: YYYY-MM-DD")
        if not 1 <= slot <= self.max_entries:
            raise ValueError("Invalid daily slot")
        legacy = self.path / (day + ".json")
        if slot == 1 and legacy.exists():
            return legacy
        return self.path / f"{day}.slot{slot}.json"

    def has_entry(self, day: str) -> bool:
        # Even a truncated file consumes the date: an order may have been sent.
        return self.count_entries(day) >= self.max_entries

    def count_entries(self, day: str) -> int:
        return sum(self._day_path(day, slot).exists() for slot in range(1, self.max_entries + 1))

    def reserve(self, day: str, details: Dict[str, Any], slot: int = 1) -> bool:
        destination = self._day_path(day, slot)
        record = {
            "day": day,
            "state": "reserved",
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "details": details,
        }
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        # Do not unlink on a write failure: preserving a consumed date is safer.
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def read(self, day: str, slot: int = 1) -> Optional[Dict[str, Any]]:
        """Read a reservation; corrupt/incomplete files remain consumed."""
        destination = self._day_path(day, slot)
        try:
            with destination.open("r", encoding="utf-8") as stream:
                record = json.load(stream)
            if not isinstance(record, dict) or record.get("day") != day:
                raise ValueError("Invalid reservation record")
            return record
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            return {"day": day, "state": "unreadable", "details": {}}

    def update_position(self, day, slot, position):
        record = self.read(day, slot)
        if record is None or record.get("state") != "reserved":
            raise ValueError("No open reservation")
        record["details"]["position"] = position
        self._write_record(day, slot, record)

    def _write_record(self, day, slot, record):
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=day + ".", suffix=".tmp", dir=self.path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._day_path(day, slot))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def mark_closed(self, day: str, details: Dict[str, Any], slot: int = 1) -> None:
        destination = self._day_path(day, slot)
        record = self.read(day, slot)
        if record is None:
            raise FileNotFoundError("Cannot close a day without an entry reservation")
        record.update(
            state="closed",
            closed_at=datetime.now(timezone.utc).isoformat(),
            close_details=details,
        )
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=day + ".", suffix=".tmp", dir=self.path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

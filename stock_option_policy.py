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
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional


TICK = Decimal("0.05")


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
                 max_lots=1, rupee_max_risk=float("inf")):
    """Choose the smallest lot count reaching the net rupee target within limits.

    qty is the exchange lot size. Rupee target prices include estimated fees
    and adverse exit slippage; these estimates cannot guarantee execution.
    """
    if (not isinstance(max_lots, int) or isinstance(max_lots, bool) or max_lots < 1
            or not math.isfinite(rupee_target) or rupee_target < 0
            or math.isnan(rupee_max_risk) or rupee_max_risk <= 0):
        return None
    for lots in range(1, max_lots + 1):
        if isinstance(qty, bool):
            return None
        units = qty * lots
        plan = _percentage_plan(entry, units, charge_fn, target_pct, stop_pct,
                                min_net_rr, slippage_pct)
        if not plan or plan["expected_net_reward"] < rupee_target or plan["expected_net_risk"] > rupee_max_risk:
            continue
        target_price = plan["hard_target_price"]
        if rupee_target:
            stop_fill = math.floor(plan["initial_sl"]*(1-slippage_pct/100)*20 + 1e-9)/20
            risk = (entry-stop_fill)*units + charge_fn(entry,stop_fill,units)
            if risk <= 0 or risk > rupee_max_risk:
                continue
            plan["expected_net_risk"] = risk
            required_reward = max(rupee_target, min_net_rr * risk)
            # Integer tick bisection avoids a slow linear search for expensive options.
            lo = math.floor(entry / float(TICK)) + 1
            hi = round(target_price / float(TICK))
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
            if reward < rupee_target or reward / plan["expected_net_risk"] < min_net_rr:
                continue
            plan.update(expected_net_reward=reward, expected_net_rr=reward/plan["expected_net_risk"])
            plan["net_profit_target"] = required_reward
            plan["trail_jump"] = max(float(TICK), round((target_price-entry)*0.25 / float(TICK))*float(TICK))
        plan.update(lots=lots, qty=units, rupee_target_price=target_price)
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


def rank_candidates(candidates: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Rank confirmed setup quality first, then net payoff and symbol, stably."""
    return sorted(
        candidates,
        key=lambda candidate: (
            -signal_quality(candidate),
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
        if not 1 <= max_entries <= 2:
            raise ValueError("Daily entries must be 1 or 2")
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

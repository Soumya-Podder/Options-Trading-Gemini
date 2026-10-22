"""Conservative stock-option planning and a persistent one-entry-per-day gate.

Rewards, losses, fees and slippage are estimates, never guaranteed fills or returns.
The day gate consumes a day before submission so an ambiguous order result cannot
lead to a second entry after a restart.
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


def premium_plan(
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
    """Persist one consumed entry per calendar day across processes/restarts.

    Call ``reserve`` immediately before entry. A reservation is never released,
    even for failed or ambiguous submissions. ``mark_closed`` records an outcome
    without making that day available again. The caller supplies the trading
    date in its exchange timezone as an ISO date, e.g. ``2026-10-22``.
    """

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path)

    def _day_path(self, day: str) -> Path:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("day must be an ISO date: YYYY-MM-DD")
        return self.path / (day + ".json")

    def has_entry(self, day: str) -> bool:
        # Even a truncated file consumes the date: an order may have been sent.
        return self._day_path(day).exists()

    def reserve(self, day: str, details: Dict[str, Any]) -> bool:
        destination = self._day_path(day)
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

    def read(self, day: str) -> Optional[Dict[str, Any]]:
        """Read a reservation; corrupt/incomplete files remain consumed."""
        destination = self._day_path(day)
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

    def mark_closed(self, day: str, details: Dict[str, Any]) -> None:
        destination = self._day_path(day)
        record = self.read(day)
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

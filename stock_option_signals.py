"""Closed-candle stock option entry confirmation, shared by live and replay.

Input is Dhan's dict of parallel timestamp/open/high/low/close/volume arrays.
Timestamps are Unix seconds for the *start* of five-minute candles. All session
boundaries use IST explicitly, independently of the machine's time zone. The
returned signal timestamp is the confirmation candle's start; it becomes usable
only five minutes later. A signal is evidence of alignment, not a price guarantee.
"""

from datetime import datetime, time, timezone, timedelta
from math import isfinite
from typing import Dict, List, Optional


IST = timezone(timedelta(hours=5, minutes=30))
BAR_SECONDS = 300
SESSION_OPEN = time(9, 15)
SESSION_LAST_BAR = time(15, 25)


def _ema(values: List[float], period: int) -> List[float]:
    result = [values[0]]
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _atr(bars: List[dict], period: int) -> List[float]:
    ranges = []
    previous = bars[0]["close"]
    for bar in bars:
        ranges.append(max(bar["high"] - bar["low"],
                          abs(bar["high"] - previous),
                          abs(bar["low"] - previous)))
        previous = bar["close"]
    return _ema(ranges, period)


def _closed_bars(candles: dict, as_of_epoch: float) -> Optional[List[dict]]:
    fields = ("timestamp", "open", "high", "low", "close", "volume")
    try:
        if any(len(candles[field]) != len(candles["timestamp"]) for field in fields):
            return None
        bars_by_timestamp = {}
        for index, raw_timestamp in enumerate(candles["timestamp"]):
            timestamp = float(raw_timestamp)
            if not isfinite(timestamp) or timestamp != int(timestamp):
                return None
            timestamp = int(timestamp)
            # Never let the current candle's price or volume affect a decision.
            if timestamp + BAR_SECONDS > as_of_epoch:
                continue
            dt = datetime.fromtimestamp(timestamp, IST)
            if not SESSION_OPEN <= dt.time() <= SESSION_LAST_BAR:
                continue
            if dt.minute % 5 or dt.second:
                return None
            bar = {field: float(candles[field][index]) for field in fields[1:]}
            if any(not isfinite(value) for value in bar.values()):
                return None
            if (min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0
                    or bar["volume"] < 0
                    or bar["high"] < max(bar["open"], bar["close"], bar["low"])
                    or bar["low"] > min(bar["open"], bar["close"], bar["high"])):
                return None
            bar.update(timestamp=timestamp, date=dt.date())
            if timestamp in bars_by_timestamp and bars_by_timestamp[timestamp] != bar:
                return None
            bars_by_timestamp[timestamp] = bar
        return sorted(bars_by_timestamp.values(), key=lambda bar: bar["timestamp"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _completed_15m_bars(bars: List[dict]) -> List[dict]:
    groups: Dict[int, List[dict]] = {}
    for bar in bars:
        dt = datetime.fromtimestamp(bar["timestamp"], IST)
        session_open = int(dt.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
        bucket_start = session_open + ((bar["timestamp"] - session_open) // 900) * 900
        groups.setdefault(bucket_start, []).append(bar)
    result = []
    for timestamp, group in sorted(groups.items()):
        if [bar["timestamp"] for bar in group] != [timestamp, timestamp + 300, timestamp + 600]:
            continue
        result.append({"timestamp": timestamp, "open": group[0]["open"],
                       "high": max(bar["high"] for bar in group),
                       "low": min(bar["low"] for bar in group),
                       "close": group[-1]["close"]})
    return result


def evaluate_stock_setup(
    candles: dict,
    as_of_epoch: float,
    min_day_change_pct: float = 2.2,
    ema_period: int = 10,
) -> Optional[dict]:
    """Return a selective CE/PE setup, or None when any confirmation is absent.

    The latest three closed five-minute bars must be a breakout beyond the true
    09:15-09:30 opening range, a retest holding that boundary (wick tolerance
    0.15 ATR), and a strong continuation beyond the retest's extreme. Trend uses
    only complete 15-minute groups; VWAP uses today's closed candles. The volume
    baseline is the ten preceding closed bars, including the prior session when
    needed. EMA and ATR use exponential smoothing, matching the stock engine.
    """
    if (not isinstance(ema_period, int) or isinstance(ema_period, bool) or ema_period < 2):
        return None
    try:
        as_of_epoch = float(as_of_epoch)
        min_day_change_pct = float(min_day_change_pct)
        if not isfinite(as_of_epoch) or not isfinite(min_day_change_pct) or min_day_change_pct <= 0:
            return None
        today = datetime.fromtimestamp(as_of_epoch, IST).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    bars = _closed_bars(candles, as_of_epoch)
    if not bars or len(bars) < 11 or bars[-1]["date"] != today:
        return None
    latest = bars[-1]
    if as_of_epoch - (latest["timestamp"] + BAR_SECONDS) > BAR_SECONDS:
        return None
    today_bars = [bar for bar in bars if bar["date"] == today]
    previous_bars = [bar for bar in bars if bar["date"] < today]
    if len(today_bars) < 6 or not previous_bars:
        return None
    session_open = int(datetime.combine(today, SESSION_OPEN, IST).timestamp())
    opening = {bar["timestamp"]: bar for bar in today_bars}
    opening_range = [opening.get(session_open + offset) for offset in (0, 300, 600)]
    if any(bar is None for bar in opening_range):
        return None
    orb_high = max(bar["high"] for bar in opening_range)
    orb_low = min(bar["low"] for bar in opening_range)
    breakout, retest, confirmation = today_bars[-3:]
    if (breakout["timestamp"] < session_open + 900
            or retest["timestamp"] != breakout["timestamp"] + BAR_SECONDS
            or confirmation["timestamp"] != retest["timestamp"] + BAR_SECONDS):
        return None

    day_change_pct = (latest["close"] / previous_bars[-1]["close"] - 1.0) * 100.0
    if abs(day_change_pct) < min_day_change_pct:
        return None
    bars_15m = _completed_15m_bars(bars)
    if len(bars_15m) < ema_period + 2:
        return None
    # Missing recent 5m data must not silently make a stale 15m trend look valid.
    expected_15m_end = session_open + ((latest["timestamp"] + 300 - session_open) // 900) * 900
    if bars_15m[-1]["timestamp"] + 900 != expected_15m_end:
        return None
    ema_values = _ema([bar["close"] for bar in bars_15m], ema_period)
    ema_value, previous_ema = ema_values[-1], ema_values[-2]
    atr_15m = _atr(bars_15m, ema_period)[-1]
    atr_5m = _atr(bars, 10)[-1]
    if atr_5m <= 0 or atr_15m <= 0 or abs(latest["close"] - ema_value) > 1.25 * atr_15m:
        return None
    total_volume = sum(bar["volume"] for bar in today_bars)
    if total_volume <= 0:
        return None
    vwap = sum(((bar["high"] + bar["low"] + bar["close"]) / 3.0) * bar["volume"]
               for bar in today_bars) / total_volume
    average_volume = sum(bar["volume"] for bar in bars[-11:-1]) / 10.0
    if average_volume <= 0 or latest["volume"] <= 0 or latest["volume"] < 1.5 * average_volume:
        return None
    candle_range = latest["high"] - latest["low"]
    if candle_range <= 0 or abs(latest["close"] - latest["open"]) < 0.6 * candle_range:
        return None

    tolerance = 0.15 * atr_5m
    bullish = (
        day_change_pct >= min_day_change_pct
        and ema_value > previous_ema
        and bars_15m[-1]["close"] > ema_value
        and latest["close"] > max(ema_value, vwap)
        and breakout["close"] > orb_high
        and orb_high - tolerance <= retest["low"] <= orb_high + tolerance
        and retest["close"] >= orb_high
        and latest["close"] > max(retest["high"], latest["open"])
        and latest["high"] - latest["close"] <= 0.25 * candle_range
    )
    bearish = (
        day_change_pct <= -min_day_change_pct
        and ema_value < previous_ema
        and bars_15m[-1]["close"] < ema_value
        and latest["close"] < min(ema_value, vwap)
        and breakout["close"] < orb_low
        and orb_low - tolerance <= retest["high"] <= orb_low + tolerance
        and retest["close"] <= orb_low
        and latest["close"] < min(retest["low"], latest["open"])
        and latest["close"] - latest["low"] <= 0.25 * candle_range
    )
    if not bullish and not bearish:
        return None
    relative_volume = latest["volume"] / average_volume
    body_ratio = abs(latest["close"] - latest["open"]) / candle_range
    close_strength = ((latest["close"] - latest["low"]) if bullish
                      else (latest["high"] - latest["close"])) / candle_range
    # This is a ranking heuristic among already-qualified setups, not a win
    # probability. Capping the volume term prevents one spike dominating rank.
    signal_quality = (40.0 * min(relative_volume / 3.0, 1.0)
                      + 35.0 * body_ratio + 25.0 * close_strength)
    return {
        "opt_type": "CE" if bullish else "PE",
        "scanner": "BULLISH_ORB_RETEST_CONFIRMATION" if bullish else "BEARISH_ORB_RETEST_CONFIRMATION",
        "spot_ltp": latest["close"],
        "day_change_pct": day_change_pct,
        "ema10_15m": ema_value,
        "conf_high": latest["high"],
        "conf_low": latest["low"],
        "atr_5m": atr_5m,
        "signal_timestamp": latest["timestamp"],
        "conf_close": latest["close"],
        "vwap": vwap,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "relative_volume": relative_volume,
        "body_ratio": body_ratio,
        "signal_quality": round(signal_quality, 4),
    }

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
from market_regime import opening_trend


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
        # Aggregate observed bars only after the entire bucket has closed.
        # Missing observations are not synthesized or treated as zero-volume bars.
        if not group or timestamp + 900 > bars[-1]["timestamp"] + BAR_SECONDS:
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
    enable_opening_trend: bool = False,
    reasons=None,
) -> Optional[dict]:
    """Return a selective CE/PE setup, or None when any confirmation is absent.

    Scanner momentum, rising volume and price versus previous completed 15m
    EMA require one directional confirmation close. ORB/retest adds rank only.
    """
    def reject(reason):
        if reasons is not None:reasons.append(reason)
        return None
    if (not isinstance(ema_period, int) or isinstance(ema_period, bool) or ema_period < 2):
        return reject('Invalid EMA period')
    try:
        as_of_epoch = float(as_of_epoch)
        min_day_change_pct = float(min_day_change_pct)
        if not isfinite(as_of_epoch) or not isfinite(min_day_change_pct) or min_day_change_pct <= 0:
            return reject('Invalid decision time or day-change threshold')
        today = datetime.fromtimestamp(as_of_epoch, IST).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return reject('Invalid decision time or day-change threshold')
    bars = _closed_bars(candles, as_of_epoch)
    if not bars or len(bars) < 11 or bars[-1]["date"] != today:
        return reject('Insufficient current-session or malformed closed candles')
    latest = bars[-1]
    if as_of_epoch - (latest["timestamp"] + BAR_SECONDS) > BAR_SECONDS:
        return reject('Stale closed confirmation candle')
    today_bars = [bar for bar in bars if bar["date"] == today]
    previous_bars = [bar for bar in bars if bar["date"] < today]
    if len(today_bars) < 2 or not previous_bars:
        return reject('Need prior close and two closed session candles')
    session_open = int(datetime.combine(today, SESSION_OPEN, IST).timestamp())
    opening = {bar["timestamp"]: bar for bar in today_bars}
    opening_range = [opening.get(session_open + offset) for offset in (0, 300, 600)]
    opening_range = [bar for bar in opening_range if bar is not None]
    orb_high = max((bar["high"] for bar in opening_range), default=latest["high"])
    orb_low = min((bar["low"] for bar in opening_range), default=latest["low"])
    previous = today_bars[-2]
    if latest["timestamp"] != previous["timestamp"] + BAR_SECONDS:
        return reject('Gap in the latest confirmation candles')

    day_change_pct = (latest["close"] / previous_bars[-1]["close"] - 1.0) * 100.0
    if abs(day_change_pct) < min_day_change_pct:
        return reject('Closed day move below scanner threshold')
    bars_15m = _completed_15m_bars(bars)
    if len(bars_15m) < ema_period + 2:
        return reject('Insufficient completed 15-minute EMA history')
    # Freshness and consecutive confirmation bars are checked above. Older gaps
    # may reduce trend observations without invalidating today's fresh setup.
    ema_values = _ema([bar["close"] for bar in bars_15m], ema_period)
    ema_value, previous_ema = ema_values[-1], ema_values[-2]
    atr_15m = _atr(bars_15m, ema_period)[-1]
    atr_5m = _atr(bars, 10)[-1]
    regime=opening_trend([{**b,'ts':b['timestamp']} for b in bars]) if enable_opening_trend else None
    if atr_5m <= 0 or atr_15m <= 0:
        return reject('Unavailable ATR')
    if abs(latest["close"] - ema_value) > 1.25 * atr_15m and not regime:
        return reject('EMA distance exceeds 1.25 ATR without a confirmed opening-trend break')
    total_volume = sum(bar["volume"] for bar in today_bars)
    if total_volume <= 0:
        return reject('No current-session volume for VWAP')
    vwap = sum(((bar["high"] + bar["low"] + bar["close"]) / 3.0) * bar["volume"]
               for bar in today_bars) / total_volume
    average_volume = sum(bar["volume"] for bar in bars[-11:-1]) / 10.0
    if average_volume <= 0 or latest["volume"] <= 0 or latest["volume"] < 1.2 * average_volume:
        return reject('Confirmation volume below 1.2 times recent average')
    candle_range = latest["high"] - latest["low"]
    if candle_range <= 0 or abs(latest["close"] - latest["open"]) < 0.45 * candle_range:
        return reject('Weak confirmation candle body')

    bullish = (day_change_pct >= min_day_change_pct
               and latest["volume"] > previous["volume"]
               and latest["close"] > previous_ema
               and latest["close"] > vwap
               and latest["close"] > max(previous["close"], latest["open"]))
    bearish = (day_change_pct <= -min_day_change_pct
               and latest["volume"] > previous["volume"]
               and latest["close"] < previous_ema
               and latest["close"] < vwap
               and latest["close"] < min(previous["close"], latest["open"]))
    tolerance = 0.15 * atr_5m
    orb_bonus = ((bullish and latest["close"] > orb_high
                  and abs(previous["low"]-orb_high) <= tolerance and previous["close"] >= orb_high)
                 or (bearish and latest["close"] < orb_low
                     and abs(previous["high"]-orb_low) <= tolerance and previous["close"] <= orb_low))
    if not bullish and not bearish:
        return reject('Candle, VWAP, EMA and rising volume are not directionally aligned')
    relative_volume = latest["volume"] / average_volume
    body_ratio = abs(latest["close"] - latest["open"]) / candle_range
    close_strength = ((latest["close"] - latest["low"]) if bullish
                      else (latest["high"] - latest["close"])) / candle_range
    # This is a ranking heuristic among already-qualified setups, not a win
    # probability. Capping the volume term prevents one spike dominating rank.
    signal_quality = (40.0 * min(relative_volume / 3.0, 1.0)
                      + 30.0 * body_ratio + 20.0 * close_strength + 10.0 * orb_bonus)
    return {
        "opt_type": "CE" if bullish else "PE",
        "scanner": (regime['setup']+('_CE' if bullish else '_PE')) if regime else ("BULLISH_MOMENTUM_CONFIRMATION" if bullish else "BEARISH_MOMENTUM_CONFIRMATION"),
        "market_regime":regime,
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

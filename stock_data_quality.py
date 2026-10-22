"""Dhan stock-data retrieval with versioned caches and explicit coverage errors."""

import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_VERSION = 2


def boundary(value, default):
    if value is None:
        return default
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def quote_received_age(quote, now):
    """Response age and last-trade age are separate; legacy quotes fail closed."""
    received = quote.get("received_epoch")
    if received is None:
        # Compatibility for older recordings, not a substitute for receipt metadata.
        received = datetime.strptime(quote["last_trade_time"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST).timestamp()
    age = now - float(received)
    return age if math.isfinite(age) else float("inf")


class HistoricalDataMixin:
    def _history_issue(self, payload, start, end, kind, **details):
        event = {"security_id": str(payload["securityId"]), "instrument": payload["instrument"],
                 "side": payload.get("drvOptionType"), "from": start.isoformat(),
                 "to_exclusive": end.isoformat(), "reason": kind, **details}
        self.history_events.append(event)
        self.logger.warning("Historical data unavailable: %s", json.dumps(event))

    def _history_window(self, endpoint, payload, start, end, fields, cache_dir, use_cache):
        now = datetime.now(IST)
        request = {**payload, "fromDate": start.strftime("%Y-%m-%d %H:%M:%S") if endpoint == "intraday" else start.strftime("%Y-%m-%d"),
                   "toDate": end.strftime("%Y-%m-%d %H:%M:%S") if endpoint == "intraday" else end.strftime("%Y-%m-%d")}
        key = hashlib.sha256(json.dumps({"endpoint": endpoint, "payload": request}, sort_keys=True).encode()).hexdigest()
        path = Path(cache_dir) / f"history_v{CACHE_VERSION}_{key}.json"
        final = end <= now.replace(hour=0, minute=0, second=0, microsecond=0)

        def validate(data):
            if not isinstance(data, dict) or not data.get("timestamp"):
                raise ValueError("empty")
            if any(not isinstance(data.get(k), list) or len(data[k]) != len(data["timestamp"]) for k in fields):
                raise ValueError("unequal_arrays")
            result = {k: [] for k in fields}
            previous = None
            for i, stamp in enumerate(data["timestamp"]):
                stamp = float(stamp)
                if not math.isfinite(stamp) or stamp != int(stamp) or (previous is not None and stamp <= previous):
                    raise ValueError("bad_timestamps")
                previous = stamp
                # Normalize Dhan responses to a shared [start, end) interval.
                if not start.timestamp() <= stamp < end.timestamp():
                    continue
                row = {k: float(data[k][i]) for k in fields}
                if not all(math.isfinite(v) for v in row.values()):
                    raise ValueError("nonfinite")
                if not 0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                    raise ValueError("bad_ohlc")
                if "volume" in row and row["volume"] < 0:
                    raise ValueError("bad_volume")
                if "strike" in row and row["strike"] <= 0:
                    raise ValueError("bad_strike")
                for k in fields:
                    result[k].append(int(stamp) if k == "timestamp" else row[k])
            if not result["timestamp"]:
                raise ValueError("empty_range")
            return result

        def record(data, source):
            self.history_coverage.append({"security_id":str(payload["securityId"]), "endpoint":endpoint,
                "side":payload.get("drvOptionType"), "from":start.isoformat(),"to_exclusive":end.isoformat(),
                "first_bar":datetime.fromtimestamp(data["timestamp"][0],IST).isoformat(),
                "last_bar":datetime.fromtimestamp(data["timestamp"][-1],IST).isoformat(),
                "rows":len(data["timestamp"]), "source":source})
            step = int(payload["interval"])*60
            for previous, current in zip(data["timestamp"],data["timestamp"][1:]):
                if (datetime.fromtimestamp(previous,IST).date() == datetime.fromtimestamp(current,IST).date()
                        and current-previous != step):
                    self._history_issue(payload,start,end,"internal_bar_gap",previous_bar=previous,next_bar=current)
                    break
            if (endpoint == "intraday" and payload["exchangeSegment"] == "NSE_EQ" and final
                    and end.hour == end.minute == end.second == 0):
                last = datetime.fromtimestamp(data["timestamp"][-1],IST)
                expected_last = last.replace(hour=15,minute=30,second=0,microsecond=0)-timedelta(seconds=step)
                if last < expected_last:
                    self._history_issue(payload,start,end,"partial_session_tail",last_bar=last.isoformat())
            return data

        if use_cache and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(cached["received_epoch"])
                if (cached.get("version") == CACHE_VERSION and cached.get("request_key") == key
                        and 0 <= age and (final or age <= 15)):
                    issue_count, coverage_count = len(self.history_events), len(self.history_coverage)
                    data = record(validate(cached["data"]),"cache")
                    if len(self.history_events) == issue_count:
                        return data
                    # An old partial cache is not authoritative. Try the network;
                    # only the replacement request determines final coverage.
                    del self.history_events[issue_count:]
                    del self.history_coverage[coverage_count:]
            except (KeyError, ValueError, TypeError, OSError):
                pass
        # Stop repeated network waits after three exhausted transient failures.
        if self.history_failure_streak >= 3:
            self._history_issue(payload, start, end, "circuit_open")
            return None
        for attempt in range(2):
            time.sleep(max(0, 0.65 - (time.monotonic() - self.history_request_at)))
            self.history_request_at = time.monotonic()
            retry = False
            try:
                response = self.session.post(f"{self.BASE_URL}/charts/{endpoint}", json=request, timeout=(5, 20))
                if response.status_code != 200:
                    retry = response.status_code == 429 or response.status_code >= 500
                    detail = {"http_status": response.status_code}
                    try:
                        error = response.json()
                        detail.update({k: error[k] for k in ("errorCode", "errorType") if k in error})
                    except ValueError:
                        pass
                    kind = "http_error"
                else:
                    raw = response.json()
                    data = raw.get("data", raw)
                    if endpoint == "rollingoption":
                        data = data.get("ce" if payload["drvOptionType"] == "CALL" else "pe")
                    validated = validate(data)
                    self.history_failure_streak = 0
                    issue_count = len(self.history_events)
                    record(validated,"network")
                    if use_cache and len(self.history_events) == issue_count:
                        self._history_save(str(path), {"version": CACHE_VERSION, "request_key": key,
                            "received_epoch": time.time(), "data": validated})
                    return validated
            except requests.RequestException as error:
                kind, detail, retry = "request_error", {"error_type": type(error).__name__}, True
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                known = {"empty","empty_range","unequal_arrays","bad_timestamps","nonfinite","bad_ohlc","bad_volume","bad_strike"}
                validation = error.args[0] if error.args and isinstance(error.args[0],str) and error.args[0] in known else "invalid_json_or_response"
                kind, detail = "invalid_or_empty_data", {"error_type": type(error).__name__, "validation_reason":validation}
            if retry and attempt == 0:
                time.sleep(2)
                continue
            self.history_failure_streak = self.history_failure_streak + 1 if retry else 0
            self._history_issue(payload, start, end, kind, **detail)
            return None

    def fetch_intraday_candles(self, security_id, exchange_segment="NSE_EQ", instrument="EQUITY",
                               interval="5", from_date=None, to_date=None, use_cache=False):
        now = datetime.now(IST)
        start = boundary(from_date, (now-timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0))
        end = boundary(to_date, now)
        if end <= start:
            raise ValueError("Historical end must follow start")
        fields = ("timestamp", "open", "high", "low", "close", "volume")
        return self._history_window("intraday", {"securityId": str(security_id), "exchangeSegment": exchange_segment,
            "instrument": instrument, "interval": str(interval)}, start, end, fields, self.history_cache_dir, use_cache)

    def fetch_rolling_stock_option(self, eq_security_id, opt_type, from_date, to_date, interval="5"):
        start, end = boundary(from_date, None), boundary(to_date, None)
        if start >= end or start.time().replace(tzinfo=None).isoformat() != "00:00:00" or end.time().replace(tzinfo=None).isoformat() != "00:00:00":
            raise ValueError("Rolling history requires increasing, midnight date boundaries")
        payload = {"securityId": int(eq_security_id), "exchangeSegment": "NSE_FNO", "instrument": "OPTSTK",
            "interval": str(interval), "expiryFlag": "MONTH", "expiryCode": 1, "strike": "ATM",
            "drvOptionType": "CALL" if opt_type == "CE" else "PUT",
            "requiredData": ["open", "high", "low", "close", "strike"]}
        fields = ("timestamp", "open", "high", "low", "close", "strike")
        merged = {k: [] for k in fields}
        while start < end:
            finish = min(start+timedelta(days=5), end)
            data = self._history_window("rollingoption", payload, start, finish, fields, self.history_cache_dir, True)
            if data:
                for k in fields:
                    merged[k].extend(data[k])
            start = finish
        return merged if merged["timestamp"] else None

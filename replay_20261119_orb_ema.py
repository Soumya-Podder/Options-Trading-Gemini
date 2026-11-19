"""Offline 19 November ORB/EMA candidate replay using Dhan equity history."""
import json
import logging
from datetime import datetime
from pathlib import Path

from dhan_dynamic_fno_stock_bot import (
    IST, DhanStockOptionsClient, DynamicStockOptionsUniverse, load_dotenv_file,
)
from stock_option_signals import _closed_bars, detect_orb_ema_trend_continuation, analyze_mtf_liquidity_and_reversal_zones

DAY = "2026-11-19"
SYMBOLS = ("PAYTM", "IREDA", "VOLTAS", "CROMPTON", "HYUNDAI", "DABUR")


def replay(data, cutoff):
    bars = _closed_bars(data, cutoff) or []
    session = [i for i, bar in enumerate(bars) if str(bar["date"]) == DAY]
    opening = [bars[i] for i in session[:3]]
    if [datetime.fromtimestamp(b["timestamp"], IST).strftime("%H:%M") for b in opening] != ["09:15", "09:20", "09:25"]:
        return {"status": "INCOMPLETE_OPENING_RANGE"}
    high = max(b["high"] for b in opening)
    low = min(b["low"] for b in opening)
    decisions = []
    for i in session[3:]:
        b = bars[i]
        close = b["close"]
        setup = detect_orb_ema_trend_continuation(bars[:i+1], high, low)
        side = setup.get("side")
        sign = 1 if side == "CE" else -1
        reason = setup["reason"]
        row = {"time_ist": datetime.fromtimestamp(b["timestamp"]+300, IST).strftime("%H:%M"),
               "close": close, **setup}
        row["reason"] = reason
        if reason is None:
            zone = analyze_mtf_liquidity_and_reversal_zones(bars[:i+1], side, close)
            row.update(liquidity_clear=zone["liquidity_clear"], blocking_zone=zone.get("blocking_zone"))
            future = [bars[j] for j in session if j > i]
            if future:
                upper = max(close, max(p["high"] for p in future))
                lower = min(close, min(p["low"] for p in future))
                row["mfe_pct"] = round((upper-close if sign == 1 else close-lower)/close*100, 3)
                row["mae_pct"] = round((close-lower if sign == 1 else upper-close)/close*100, 3)
        decisions.append(row)
    return {"orb_low": low, "orb_high": high, "decisions": decisions,
            "first_trigger": next((r for r in decisions if r["reason"] is None), None),
            "first_zone_clear": next((r for r in decisions if r.get("liquidity_clear")), None)}


def main():
    logging.basicConfig(level=logging.CRITICAL)
    logger = logging.getLogger("orb_replay")
    env = load_dotenv_file(".env")
    client = DhanStockOptionsClient(env["DHAN_CLIENT_ID"], env["DHAN_ACCESS_TOKEN"], logger)
    universe = DynamicStockOptionsUniverse.load_universe(logger)
    ids = {row["symbol"]: sid for sid, row in universe.items()}
    end = min(datetime.now(IST), datetime.fromisoformat(DAY+"T15:30:00").replace(tzinfo=IST))
    reports = {}
    for symbol in SYMBOLS:
        if symbol not in ids:
            reports[symbol] = {"status": "MISSING_SECURITY_ID"}
            continue
        data = client.fetch_intraday_candles(ids[symbol], from_date="2026-11-12", to_date=end.isoformat())
        reports[symbol] = replay(data, end.timestamp()) if data else {"status": "HISTORY_UNAVAILABLE"}
    Path("replay_20261119_orb_ema.json").write_text(
        json.dumps({"as_of_ist": end.isoformat(), "symbols": reports}, indent=2), encoding="utf-8")
    for symbol, result in reports.items():
        print(symbol, json.dumps({k:v for k,v in result.items() if k!="decisions"}))


if __name__ == "__main__":
    main()

# Swiggy missed morning opportunity — 23 October 2026

Dhan intraday candles were downloaded for the underlying and the exact active 27 October 190 PE and 195 PE contracts. These are historical OHLC observations, not executed fills or a complete historical option qualification replay.

Swiggy underlying: 09:15 open 204.95; 09:30 candle close 190.95; 10:00 candle close 181.80. The five-minute bearish move was substantial.

## Actual option observations

| Time (bar start) | 190 PE open | 190 PE close | 195 PE open | 195 PE close |
|---|---:|---:|---:|---:|
| 09:30 | 2.90 | 4.45 | 4.70 | 7.15 |
| 09:35 | 4.50 | 5.10 | 7.15 | 8.00 |
| 09:45 | 5.40 | 7.70 | 8.35 | 11.30 |
| 09:50 | 7.60 | 8.65 | 11.40 | 12.30 |
| 10:00 | 9.60 | 10.40 | 13.35 | 13.75 |

Dhan master lot size for these contracts: 1,825. Illustrative 190 PE entry at the 09:45 open of 5.40 and exit at the next bar's open of 7.60 gives gross Rs.4015.00, modeled charges Rs.78.14 and modeled net Rs.3936.86. Both opens are actual recorded option prices; bid/ask execution at those prices is not established. This is not a trade taken by the bot and is not included in P&L or learning. It was not a signal authorized by the unchanged entry rules.

## Why the bot missed it

1. The stock-options engine was not running in the morning. Its paper process was started at 13:21:44 IST.
2. A chronological replay of the existing underlying setup function found its first/only PE signal in the downloaded closed bars at 12:00 IST. Every eligible morning decision from 09:30 through 10:30 was first rejected by the EMA-distance condition: distance above 1.25 times the 15-minute ATR. At 09:35 the ratio was 3.39; at 09:50 it was 3.92. The filter was intended to avoid chasing, but rejected this sustained bearish gap/trend along with overextended moves.
3. At the illustrative entry premium of 5.40, the current rupee/risk/percentage target planner returns no acceptable plan under the restored Rs.2,000 target, Rs.1,000 maximum estimated risk and 3-lot ceiling. The fixed percentage price limits and lot sizing are an additional restriction. Passing the proximity condition alone therefore would not establish a tradable candidate.

The forensic result supports the user's observation of a profitable put-premium move. It does not establish that Swiggy was the best of all F&O stocks, that historical Greeks/depth passed, or that the current stop/trailing policy would have delivered the illustrative holding result. Later 13:00+ scans cannot disprove the morning opportunity.

No strategy thresholds were relaxed, no trading history was fabricated, and no live order was placed during this investigation. Raw downloaded bars and chronological rejection details are retained under `dhan_hist_cache/swiggy_review_20261023/`.

## Requested Rs.900 recovery profile replay

Fixed actual 190/195 PE, October 27 expiry, 1825 units/lot, 09:35 next-bar entry, 5% stop and adverse modeled slippage. Five-minute OHLC stop-first treatment applies; this is an exit replay, not historical depth/Greek qualification.

```json
[
  {
    "variant": "before",
    "strike": 190,
    "entry": 4.5,
    "stop": 4.3,
    "target": 5.7,
    "exit": 4.25,
    "reason": "PREMIUM_STOP",
    "gross": -456.25,
    "net": -521.95
  },
  {
    "variant": "before",
    "strike": 195,
    "entry": 7.15,
    "stop": 6.8,
    "target": 8.35,
    "exit": 6.75,
    "reason": "PREMIUM_STOP",
    "gross": -730.0,
    "net": -806.59
  },
  {
    "variant": "after",
    "strike": 190,
    "entry": 4.5,
    "stop": 4.3,
    "target": 5.1,
    "exit": 4.25,
    "reason": "PREMIUM_STOP",
    "gross": -456.25,
    "net": -521.95
  },
  {
    "variant": "after",
    "strike": 195,
    "entry": 7.15,
    "stop": 6.8,
    "target": 7.85,
    "exit": 6.75,
    "reason": "PREMIUM_STOP",
    "gross": -730.0,
    "net": -806.59
  }
]
```

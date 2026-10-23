# Nifty OI checks

The Nifty paper/live analyzer now compares the same six actual contracts against a saved snapshot received 300–390 seconds earlier. It selects the current three calls above spot and three puts below spot in both snapshots. An ATM shift therefore does not masquerade as new OI. Security IDs must agree when supplied, and expiry/day changes restart the reference window.

`previous_oi` in Dhan's option-chain response is previous-day OI. The bot reports current minus previous-day OI separately from its five-minute snapshot change. A fresh API response does not establish the exchange update time of its OI fields; Dhan documents that OI updates more slowly than prices.

Until a valid reference exists, the Nifty recommendation stays HOLD. Missing selected contracts, mismatched security IDs and references older than the accepted window also prevent an OI-based signal. Zero OI change is labelled NO_OI_CHANGE and cannot be interpreted as short covering or unwinding merely because premiums moved.

`live_telemetry.json` contains `indices.NIFTY.oi_analysis`, including nearby strike OI, day changes, five-minute changes, full provided-chain PCR, and the five largest put-OI levels below spot/call-OI levels above spot within 500 points. These levels are observations, not guaranteed support or resistance. The existing market-structure entry gate remains in force. This patch does not establish a profitable strategy.

The session log prints a compact `[NIFTY OI]` entry every 30 seconds. Full per-strike diagnostics remain in telemetry. The bot does not send additional broker orders to collect this information.

`python -B dhan_bot_engine.py --indices NIFTY` now selects Nifty for an actual session as well as a backtest. Omitting `--indices` preserves the existing two-index session default. `--live` is required for broker-order mode; without it the session is paper mode. Existing trade caps, stops, targets and learning files are preserved.

Official reference: https://dhanhq.co/docs/v2/option-chain/ (OI, previous_oi, expiry list, and three-second option-chain request limit).

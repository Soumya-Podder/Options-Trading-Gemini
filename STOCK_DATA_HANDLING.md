# Stock-options data quality

The stock strategy settings are separate from data coverage. Provider outages can
still occur. Failed or partial historical requests must not become a clean result.

## Current stock-only settings

`python run_bot_session.py` now launches both the index engine and the stock-options
engine, plus the index dashboard. Both engines follow the launcher's PAPER/LIVE
mode; current local `PAPER_MODE=true` keeps them in paper mode. The stock bot may
finish after its own daily limit or cutoff while the index engine continues.
Standalone stock scanning is `python dhan_dynamic_fno_stock_bot.py --scan-once`.
Initial signal, chain-selection, quote qualification and recheck failures are
logged under `OPTSTK REJECT` or `OPTSTK RECHECK REJECT`.

The 2026-10-23 clear-mover profile uses a Rs.900 minimum estimated net
stock target, minimum planned net RR 1.3, a 3.5% base spread cap and a 600-second
confirmation window after a five-minute candle closes. Set these with
`STOCK_OPT_RUPEE_TARGET`, `STOCK_OPT_MIN_NET_RR`, `STOCK_OPT_MAX_SPREAD_PCT` and
`STOCK_OPT_CONFIRM_WINDOW_SEC`. The window must be 300–900 seconds, is exclusive
at its upper boundary, and is shared by signal detection and quote qualification.
Future/forming candles and gaps between required confirmation bars remain invalid.

The current local risk ceiling remains Rs.1,000, with at most three lots and two
entries/day. Defaults without local overrides are a Rs.1,800 estimated risk cap,
two lots, 12% premium target reference and 5% stop; the local percentage reference
is 10%, with the existing cash-target extension enabled. Full planned RR and risk,
wallet, depth for the selected quantity and bid-above-stop are still required.
These are modeled constraints, not guaranteed fills, profit, or maximum loss.

A strong mover means an absolute underlying day change of at least 6%, based on
the latest confirmed signal. It can use one extra spread percentage point (4.5%
with the current profile), a 55% minimum of the required net target in the 1.8-ATR
Greek scenario, and a chain IV-range cutoff of 90% instead of 80%. This is a
cross-sectional min/max IV position, not historical IV rank or a percentile of
contracts. At the cutoff the option is rejected. Normal movers retain the 75%
scenario floor, or the stricter 100% scenario screen when cash-target extension
is enabled. Strong-mover extension plans use the full cash target and risk/RR
constraints before applying the explicit 55% scenario screen; otherwise the old
forecast-price cap would silently defeat the requested relaxation.

The rationale is to reduce stacked timing/spread/forecast rejections on large
moves such as the observed Swiggy decline. A later rally in a put premium does
not establish that an earlier entry would survive its stop. This profile requires
replay and paper observation; it does not ensure capture of that move.

Minimum DTE remains four calendar days; delta band is 0.38–0.78; premium floor is
Rs.3; maximum hold is 12 five-minute bars, with no-progress checking after five
bars; daily theta limit is 6%. Quotes must have been received within 30 seconds
and Greek snapshots within 120 seconds. The underlying follow-through band stays
-0.15 to +1.0 five-minute ATR. Valid broker Greeks remain mandatory.

Nifty confirmation now also defaults to 600 seconds (validated 300–900). The
range of already-closed current-session candles must reach 80 points before body
and directional close-strength minima soften from 0.50/0.70 to 0.40/0.60. This
range test alone does not establish a trend: marked-level reaction, matching
strike OI/premium confirmation, chase/room and structural RR checks still apply.
Future bars cannot activate the exception.

Expiry inputs are normalized to ISO dates. The documented option-chain key is
`security_id`; `securityId` is also accepted, but must match the scrip master.
Zero/invalid Greeks are still rejected, not estimated.

Observed historical gaps and missing tails are coverage warnings: they do not
discard an entire stock's available history. Signals can use sparse older 15-minute
buckets only after those buckets close, while fresh consecutive confirmation
bars are still required. No missing observations are fabricated or assumed to
mean zero trading. Warnings remain in the final report and keep it incomplete.
Rolling ATM strike changes remain unvalidated: a constant-delta adjustment cannot
reconstruct a fixed contract's historical OHLC, so those trades are not scored.

- New caches use a versioned request fingerprint and receipt time. Older cache
  formats are ignored, without deleting the existing files.
- A date-only end boundary means midnight IST, exclusive. Backtests exclude the
  unfinished current session. Live candle retrieval still ends at the current time.
- Mutable cache entries expire after 15 seconds. Completed historical intervals
  may be reused only after validation. Detected partial NSE sessions and internal
  candle gaps are reported and are not written as authoritative caches.
- Option history is requested in five-day chunks, with OHLC and strike fields.
  A failed chunk is recorded; successful chunks can be reused on the next run.
- Transient requests receive one retry. After three exhausted transient failures,
  the client opens a circuit instead of repeating long waits across
  every stock. Current-session requests can make a bounded recovery probe after
  60 seconds; completed historical requests remain blocked for that run. No synthetic bars or
  successful empty-data cache entries are created.
- Reports distinguish historical errors, missing entry bars, simulation gaps,
  premium exclusions, unavailable risk/target plans and insufficient funds.
  Requested ranges, actual first/last candles and row counts are saved.
- `INCOMPLETE_DATA` and CLI exit code 2 indicate an incomplete replay. Recorded P&L
  on any available subset is not a validated strategy result. Even a data-complete
  replay is labeled `COMPLETED_EXIT_REPLAY`, with `full_strategy_validated=false`:
  historical bid/ask, broker Greeks and delta-selected contracts are unavailable.
- Production quotes carry local response receipt timestamps. A last trade can be
  older than a newly received depth snapshot. Snapshot age, last-trade age and
  Greek receipt age are kept separate. Legacy recordings without receipt metadata
  use the restrictive last-trade-age fallback. A local receipt time is not a claim
  about the provider's underlying calculation time.
- Candidates are qualified as they are fetched and rebuilt from current candles,
  underlying quotes and option chains before entry. Stops, sizing, entry cutoffs
  and price/liquidity checks remain in force.

Regression checks:

```powershell
python -m unittest test_stock_data_quality test_stock_option_policy test_stock_option_signals test_stock_option_integration test_nifty_partial_targets
```

Official references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/expired-options-data/
- https://dhanhq.co/docs/v2/market-quote/
- https://dhanhq.co/docs/v2/option-chain/

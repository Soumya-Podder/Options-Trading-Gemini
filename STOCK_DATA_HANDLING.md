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

The revised 2026-10-23 local profile uses a Rs.1,250 minimum estimated net
stock target, minimum planned net RR 1.4, a 2.5% base spread cap and a 600-second
confirmation window after a five-minute candle closes. Set these with
`STOCK_OPT_RUPEE_TARGET`, `STOCK_OPT_MIN_NET_RR`, `STOCK_OPT_MAX_SPREAD_PCT` and
`STOCK_OPT_CONFIRM_WINDOW_SEC`. The window must be 300–900 seconds, is exclusive
at its upper boundary, and is shared by signal detection and quote qualification.
Future/forming candles and gaps between required confirmation bars remain invalid.

The current local estimated risk ceiling is Rs.1,600, with at most two lots and two
entries/day. The local percentage target reference is 14%, the premium stop is 7%,
and adverse modeled slippage is 0.05%, with the existing cash-target extension enabled.
Defaults without local overrides remain separate from the selected local profile. Full planned RR and risk,
wallet, depth for the selected quantity and bid-above-stop are still required.
These are modeled constraints, not guaranteed fills, profit, or maximum loss.

A strong mover means an absolute underlying day change of at least 6%, based on
the latest confirmed signal. It can use one extra spread percentage point (3.5%
with the current local profile), a 55% minimum of the required net target in the 1.5-ATR
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

Minimum DTE remains four calendar days; local delta band is 0.40–0.75; premium floor is
Rs.3; local maximum hold is 16 five-minute bars, with no-progress checking after eight
bars; daily theta limit is 6%. Quotes must have been received within 30 seconds
and Greek snapshots within 120 seconds. Both underlying follow-through checks use
-0.10 to +0.85 five-minute ATR. Valid broker Greeks remain mandatory.

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
python -m unittest test_stock_data_quality test_stock_option_policy test_stock_option_integration test_index_option_replay
```

Official references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/expired-options-data/
- https://dhanhq.co/docs/v2/market-quote/
- https://dhanhq.co/docs/v2/option-chain/

## Current-implementation replay audit

Live stock quotes refresh on a four-second schedule, configurable through
`STOCK_OPT_POLL_INTERVAL_SEC` within 3-5 seconds. The previous 30-second idle
sleep is removed. During a long shortlist scan, the same thread refreshes the
universe quote batch and checks open paper positions between candidates and
before chart, chain and quote requests. Entry cash and daily capacity are
rechecked after these position checks. This avoids concurrent ledger writes or
orders. Full chain/Greek qualification remains selective; all stock chains are
not fetched every four seconds. Five-minute confirmation still uses closed bars.

Nifty already polls its option chain with a 3.6-second minimum gap. Stock quote
requests retain a 1.05-second minimum gap and stock chains a 3.05-second gap.
These are nominal schedules: synchronous HTTP latency, endpoint pacing and
429/error backoff can extend refresh times. Market polling stops under existing
session cutoffs and entry limits when no position requires management.

The stock backtest CLI now requests five extra calendar days for signal warmup,
then uses the same rolling five-calendar-day history window as the live scanner.
An expanding multi-month EMA history is not substituted. All stocks in the current
universe can be requested with `--max-stocks 0`; current membership and lot sizes
still do not reconstruct historical membership. Below-threshold scanner moves
are discarded before the expensive candle evaluator, as in live scanning.

The isolated 23 October study checks every expected five-minute bar against
observed Nifty session dates and retains missing-bar details. Historical depth,
Greeks, chain IV range and delta-selected strikes remain unavailable, so an
exit-policy result stays explicitly short of full strategy validation. All
production paper ledgers and learning state are hash-checked for isolation.


## Trading window and historical coverage

The requested trading window is 09:15-14:45 IST. Nifty stops new entries before
14:45 and requests square-off at 14:45. The stock engine monitors from 09:15,
accepts entries only before 14:45 after closed-candle confirmation, and exits
paper positions at 14:45 using fresh executable quotes. The supervisor allows
one minute for shutdown after the cutoff. Live stock super-order submissions
still hand exit ownership to the broker; their existing early-return behavior
does not provide an independently verified timed broker liquidation.

Replay execution and coverage checks require only bars through 14:45: the last
five-minute candle starts at 14:40, and the last one-minute candle at 14:44.
Missing 15:15-15:25 bars are not active-session exit gaps. Prior-session close
and indicator warmup may still require earlier historical context; a missing
actual exchange close remains a separate input limitation. Monitoring from
09:15 does not manufacture an entry before the required candles have closed.

Stock option replay retrieves only the five-day block containing a candidate's
execution date, instead of downloading every option date in the study for each
new symbol and side. Raw historical gaps remain unfilled and unvalidated.


## Entry/exit audit correction

The 23 October review found 11 stock rolling entry-price disagreements and 14
first-candle OHLC disagreements against exact-strike minute series across 73
previously scored entries. Older five-minute stock P&L and policy comparisons
are provisional. The existing report contains a 70-opportunity minute-path
diagnostic with explicit selection and sample limits, not full strategy
validation. No experimental trading policy was promoted. Stock close records
now retain spread/stop geometry, Greeks, quote/signal ages and 15m EMA context.
Nifty entries retain LTP/bid/ask evidence. All 207 regression checks pass.

This revised local profile was applied after the stored 30/60-day results. Those results do not establish the profitability of these changes. Existing positions and ledger balances are not reset when configuration changes.

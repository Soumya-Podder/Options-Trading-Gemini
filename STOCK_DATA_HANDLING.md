# Stock-options data quality

The stock strategy settings are separate from data coverage. Provider outages can
still occur. Failed or partial historical requests must not become a clean result.

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
  the client opens a circuit for that run instead of repeating long waits across
  every stock. A new run creates a new client and can retry. No synthetic bars or
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

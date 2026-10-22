# Selective stock-options strategy

API reference for this project: https://docs.dhanhq.co/ (consult first for Dhan API work).

The stock bot retains its existing momentum shortlist: the top eight bullish and
top eight bearish stocks beyond the configured daily percentage threshold.
It evaluates the whole shortlist, then ranks eligible setups by confirmation
quality, estimated net reward/risk, and symbol for deterministic ties. This picks
the best eligible setup at the scan time; it cannot know the day's future best move.

Only completed candles qualify. Entry needs an opening-range breakout, a retest
that holds, and a strong directional confirmation candle, supported by 15-minute
EMA slope, VWAP alignment and relative volume of at least 1.5. Fresh option market
depth must show sufficient quantity on both sides and a spread no wider than 0.2%.

The default full-position target is 3% of entry premium. The default premium stop
is 0.4% (configurable up to 0.5%). Tick rounding can tighten these distances.
Entry additionally requires estimated net reward/risk >=3 after modeled charges
and 0.1% adverse exit slippage. Both eligibility and affordability are checked
again before entry. Zero trades is an acceptable result. Existing one-lot sizing
is retained. Tight stops are susceptible to ordinary price noise; actual losses,
fills and average win/loss ratios are not guaranteed.

Maximum stock entries: one per IST day across stock symbols, process restarts and
concurrent launch attempts. `stock_option_daily_entries` records the reservation
before submission. A failed or ambiguous live submission consumes the slot.
Resetting the paper ledger does not release that slot. Incomplete records fail
closed. Paper positions can resume from today's reservation after a restart.

Paper positions exit on target, stop, 15 minutes elapsed, or end-of-day. Paper
exits use current bid, available size and adverse slippage; stale prices are not
filled. An unresolved position at shutdown stays in the daily record for inspection.

Live mode submits a fixed Dhan Super Order bracket with trailing disabled and
then returns. Dhan manages that bracket. Submission is not treated as a fill;
local simulated P&L is not recorded for live orders. Actual fill reconciliation,
live timed exits and live Telegram exit alerts are not implemented by this version.
Check actual live entry/exit status in Dhan. This change was tested offline only.

Backtests write `stock_fno_backtest_ledger.json`, keeping paper session history
separate. They use the same confirmation rules and daily cap, next-bar entry,
stop-first handling when both levels occur in one bar, gap-aware stops, and real
option candles only. Missing bars and changing rolling ATM strikes are unvalidated;
the chosen day's slot is consumed before inspecting its future outcome. Results
still have current-universe/lot-size bias, missing historical spreads, and five-minute
resolution. They cannot establish execution quality for this small stop/target.

Stock cost model uses option-sale STT 0.15%, effective 1 April 2026:
https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax
Other charges remain estimates and should be reconciled against contract notes.

Commands from this project directory:

```powershell
python dhan_dynamic_fno_stock_bot.py --scan-once
python dhan_dynamic_fno_stock_bot.py
python -B -m unittest test_stock_option_signals test_stock_option_policy test_stock_option_integration -v
```

The first command ranks current setups without trading. The second starts the
stock paper session. `run_bot_session.py` continues to launch the index bot and
dashboard; its index configuration was not changed by this task.

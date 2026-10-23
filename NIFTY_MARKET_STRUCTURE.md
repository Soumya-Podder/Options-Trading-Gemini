# Nifty entry structure

Nifty entries now require both the existing OI/momentum/learned-rule filters and
an explicit price-structure check. Sensex and stock-options entry strategies are
unaffected. Existing daily limits, premium stops and multi-lot +10/+20 targets
are not changed by this gate.

The gate uses actual five-minute Nifty INDEX OHLC from Dhan charts/intraday and
strike-level OI from the current option chain. It maps:

- Previous full regular-session high/low.
- Today's high/low across closed five-minute candles.
- The 09:15–09:30 opening range.
- Swing highs/lows confirmed by two closed candles on each side.
- The largest nearby call and put OI concentrations, within 500 spot points.
  OI change is explicitly relative to previous close, not an intraday change.

Entry needs a level event followed by a separate directional hold/retest candle,
with current spot still holding confirmation. Default level buffer is three
underlying points. The next mapped opposing level must be at least 20 underlying
points away, and the room divided by distance to the structural invalidation
anchor must be at least 1.5. This structural ratio is a screening heuristic, not
the option trade's actual net reward/risk. Existing option stops still manage risk.

Inside-range movement without a confirmed level event is rejected. Initial
breaks alone cannot enter. A rejection must be followed by a directional close
through the rejection candle's extreme. These are defined candle patterns,
not proof that a level will hold. The confirmation
candle's new high/low is not used as an already-existing opposing barrier.

There must be a complete prior regular session, complete opening range, continuous
current-session candles and a fresh closed confirmation. No missing candles,
OI or levels are synthesized. Live index history refreshes every 30 seconds.
Unavailable or stale structure blocks a new Nifty entry, while open-position exit
management continues. Levels and reasons appear in the dashboard, telemetry and
NIFTY STRUCTURE log records. Entry positions retain their structure assessment.

Backtests use the same price gate without future candles. Historical OI can only
use the rolling strikes available to the old replay; this is explicitly marked
as a subset of the full chain. Missing structure is counted and the output is
marked INCOMPLETE_MARKET_STRUCTURE. Historical replay is not a verified live-fill
result. Existing learned rules may include the evaluation period, so a separate
chronological training/holdout study is needed before claiming performance gains.

No 80% win rate, consistent profit or average-win/loss improvement has been
established. This change must be evaluated using costs, slippage, drawdown and
unseen-session results, alongside win rate and average net wins/losses.

## Entry and replay correction

The entry gate now waits for a separate completed five-minute hold/retest candle
after a level event. The follow-through candle needs at least 50% body/range and
70% closing strength in the trade direction. Current spot must hold confirmation
and cannot run beyond a 0.25 recent true-range entry zone (minimum three points).
Reference day-range levels exclude the break and confirmation candles. Earliest
evaluation is 09:40 after five closed session candles. Stronger selectivity is not
evidence of better performance.
Nifty also waits when the current and reference ATM strike baskets differ;
cross-strike premium/OI changes are not treated as an entry signal.

Historical entries use the following bar's open, with 0.1% adverse modeled entry
and exit slippage. A gap beyond the existing stop uses the worse opening price.
If stop and target both fall inside a bar, the existing stop is assumed first;
an opening gap through a target is handled before later intrabar movement.
Trailing levels calculated from a candle's high only apply to following bars.
Rolling-strike changes, missing bars and truncated sessions produce explicit
unvalidated outcomes. Those outcomes never train rules or contribute invented
P&L. Further entries for an unresolved historical position's day are blocked.
The replay cannot establish a profitable full strategy where history is missing.
Only five-minute option replay is supported for the Nifty structure strategy.

Official API references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/option-chain/
- https://dhanhq.co/docs/v2/annexure/

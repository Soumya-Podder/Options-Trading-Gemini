# Nifty level entry rules

Implemented October 23, 2026 following the user's choice of both candle reaction and OI change at the same strike.

The `LEVEL_CANDLE_AND_STRIKE_OI_V1` entry model replaces the basket imbalance and spot impulse prerequisites for Nifty. SENSEX retains its existing entry model. Capital, sizing limits, daily trade and loss limits, cooldowns, and the Rs.1,200 net daily closing objective remain in place.

1. Mark maximum absolute put/call OI walls and maximum positive put/call buildup since the previous close. Preserve prior-day, opening-range, day-range and confirmed swing price levels for structure and room checks.
2. For an entry, reconstruct the OI walls from the matched five-minute reference snapshot. A wall created by the confirmation interval cannot retroactively become its pre-existing trigger. Both absolute-OI and positive daily-buildup walls can be triggers.
3. Require the existing completed five-minute breakout hold, retest or rejection confirmation at a marked OI strike, with a directional candle, adequate body/closing strength, no chasing and adequate structural reward/risk. Previous session and current session coverage checks remain mandatory.
4. At that exact trigger strike, require supportive five-minute OI and premium changes. BUY_CE accepts put OI increasing while put premium falls, or call OI decreasing while call premium rises. BUY_PE accepts call OI increasing while call premium falls, or put OI decreasing while put premium rises. These are operational writer/unwinding proxies, not proof of participants' intentions. No change, missing matched contracts or missing reference cannot confirm a trade.
5. Daily positive buildup can continue to mark a nearby building wall even if its most recent five-minute OI change is flat or negative. Daily buildup alone does not authorize an entry.
6. Reject ambiguous directions. Retain the nearby building-wall room gate, option premium minimum, rule pruning, recent-loss similarity veto and execution controls. Old lunch/recovery impulse prerequisites do not apply to the level entry model. New rules use a separate LEVEL_V1 signature rather than inheriting the previous basket rules' statistics.

The paper/live session and production historical replay call the same entry helper. Historical replay derives matched strike OI changes from actual option bars. The research runner supports `entry: level_oi` with the same helper; earlier research modes retain their original definitions so historical comparisons are auditable.

## Validation and limits

Unit checks cover bullish/bearish symmetry, exact-strike matching, daily versus five-minute changes, missing/invalid references, premium disagreement, weak candles, separate absolute-OI and buildup walls, and bypass of the old basket/impulse prerequisites.

These checks establish code behavior, not profitability. Earlier 30/60-day results predate this entry model. Rolling historical option data covers a subset of strikes and lacks previous-day OI; it cannot validate full-chain daily-buildup wall selection. A new replay must disclose those limitations. Response receipt times also do not establish exchange OI update times.

Editing the source does not replace the code already loaded by a running Python process. Activation requires a deliberate session restart after checking any open position.

## Missed-entry review (October 23)

The nearest mapped put/call strike expansion is an explicit research option (`include_nearest=True`), disabled by default. The 30-day comparison produced more trades but worse net P&L, so it is not enabled in execution. Direction-specific rejection reasons are retained in structure telemetry. The intraday engine can save a decision observation per closed candle/readiness state under `dhan_hist_cache/nifty_entry_audit_<date>.jsonl`, including closed index bars, nearby option contracts and the OI analysis. These observations are evidence for later review; they are not booked trades or training rewards. Existing running processes require a reload to use the new observation writer.

## Selected continuation profile

`NIFTY_OPENING_TREND=true` enables a closed-candle opening-gap continuation path (minimum 0.5% overnight gap). The opening range is known before confirmation; later continuation requires a new local consolidation break. This path can begin after four closed session bars, with complete prior-session history. It retains candle strength, same-contract OI/premium confirmation, proximity, mapped room and structural RR. A non-strike opening price maps to the nearest observed option strike within 25 points, recorded as `oi_confirmation_strike`. Wider opening-drive and arbitrary price-level expansions are not enabled. See `backtest_results/entry_continuation_20261023/REPORT.md` for the positive, negative and limited evidence.

## 23 October confirmation update

Closed-candle confirmation is usable for 600 seconds by default (configurable function parameter, valid 300–900 seconds; upper bound excluded). When the range of already-closed session candles reaches 80 points, minimum body/close-strength ratios are 0.40/0.60; otherwise they remain 0.50/0.70. The same thresholds apply to Nifty opening-gap confirmation. Matching strike OI/premium confirmation and all existing chase, room, structural RR and data checks remain mandatory. Future candles cannot enable the exception.

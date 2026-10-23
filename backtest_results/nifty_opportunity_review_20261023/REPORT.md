# Nifty missed-entry investigation — October 23, 2026

The current level entry engine is very sparse. A specific restriction was tested: four maximum-OI/buildup walls versus also admitting the nearest mapped put/call OI levels. The latter is an explicit research option and remains disabled in execution.

| Calendar window | Trading sessions | Entry variant | Trades | Gross P&L | Net P&L | Zero-trade sessions |
|---|---:|---|---:|---:|---:|---:|
| 30 days | 21 | before | 3 | 409.50 | 216.00 | 18 |
| 30 days | 21 | nearest_levels | 6 | -965.25 | -1352.25 | 15 |
| 60 days | 41 | before | 4 | 3.25 | -254.75 | 37 |
| 60 days | 41 | nearest_levels | 9 | -2184.00 | -2764.50 | 32 |

The 30-day window is September 15–October 14, 2026. The 60-day window is August 16–October 14, 2026. All amounts are rupees. The nearest-level expansion increases participation but worsens net P&L in both windows, and is therefore not enabled. Neither comparison establishes the monthly Rs.15,000 gross objective.

## Controls and limits

Both variants use one lot, a 6.15-point research stop, the same six-trade daily cap, chronological after-cost learning, and a Rs.1,200 realized-net daily cutoff. One-minute OHLC uses modeled adverse slippage and stop-first handling; there are zero unvalidated attempted outcomes in these runs. The research comparison does not reproduce the intratrade daily-profit exit. Historical OI is limited to available rolling strikes, lacks previous-day OI, and assumes a same-session expiry. Results are an isolated entry comparison, not a full-chain validation of live execution. These dates have been examined previously and are not an untouched holdout.

## Missed-entry evidence

The new diagnostics retain separate BUY_CE/BUY_PE rejection reasons. Counts in the JSON summaries are observations, including repeated observations on the same five-minute candle; they are not independent setups or missed winning trades. A live entry-review writer has also been added for closed index candles, nearby option contracts and OI evidence. It does not alter realized P&L or train the model on hypothetical outcomes. It becomes active on the next process start; the existing paper process was left running to avoid another OI warm-up gap.

This study does not reconstruct October 23 morning option P&L. The available morning logs lack the complete exact-strike historical evidence required to claim a realized Rs.1,300 option opportunity.

## Verification

173 tests pass, including the guard that keeps the nearest-level experiment disabled by default and the observation test that excludes future candles. Paper learning files are not used as research output. The full isolated ledgers, daily results, rejection counts, declaration and review script remain in `dhan_hist_cache/nifty_opportunity_review_20261023/`.

# Continuation-entry changes and evidence — 23 October 2026

The selected PAPER profile adds a confirmed opening-gap continuation path and cost-aware cash-target sizing. It does not promise capture of every profitable move or meet the monthly profit objective. No retrospective opportunities were added to actual trading P&L or learning.

## Nifty entry comparison

| Profile | Calendar days | Sessions | Trades | Gross Rs. | Net Rs. |
|---|---:|---:|---:|---:|---:|
| baseline | 30 | 21 | 3 | 409.50 | 216.00 |
| baseline | 60 | 41 | 4 | 3.25 | -254.75 |
| gap_only | 30 | 21 | 3 | 409.50 | 216.00 |
| gap_only | 60 | 41 | 5 | 897.00 | 574.50 |
| opening_drive_disabled | 30 | 21 | 11 | -1933.75 | -2643.25 |
| opening_drive_disabled | 60 | 41 | 15 | -1807.00 | -2774.50 |
| expanded_price_levels_disabled | 30 | 21 | 18 | 295.75 | -865.25 |
| expanded_price_levels_disabled | 60 | 41 | 24 | -250.25 | -1798.25 |

The 30-day window covers September 15–October 14; the 60-day window covers August 16–October 14. The gap-only path preserves closed-candle strength, same-contract OI/premium confirmation, proximity, structural room/RR, learning and execution controls. An opening-range price can be mapped to the nearest observed option strike within 25 Nifty points, with that identical contract used across the OI snapshots. Broad opening-drive and expanded-price-level variants remain disabled because results deteriorated. The earlier nearest-wall expansion also remains disabled.

These are exploratory tests on previously examined dates, using one-lot one-minute modeled execution, a 6.15-point research stop and realized-net daily cutoff. Historical OI is a rolling-strike subset without previous-day OI; the intratrade daily-profit exit is not reproduced. Zero unvalidated attempted outcomes in these particular runs does not establish full historical-chain or live validation.

## Stock options

The current profile recognizes only a fresh opening-range break or a new consolidation break in the direction of a gap of at least 0.5%. It can override the EMA-distance veto for that recognized pattern, while preserving volume expansion, body strength, VWAP/EMA direction, fresh quotes, broker Greeks, liquidity, wallet and cash-risk limits. A trend alone is not an entry.

The rupee-target planner searches a bounded premium range and the smallest size that can reach the cash target within the option-move estimate, fees, slippage, minimum RR and the Rs.1,000 estimated risk cap. The booking trigger is aligned with the cash target so an old percentage target cannot force an earlier exit. At entry 5.40 for a 1,825-unit lot, the new plan uses one lot with approximately Rs.616.90 modeled risk and Rs.2,024.28 modeled reward; target trigger 6.60. This is a plan, not a prediction.

A net loss no longer automatically blacklists a stock all day in the enabled profile. Re-entry requires a new closed confirmation after a 15-minute cooldown, no unresolved/open earlier position, all current qualification checks and an available daily slot. Winning/breakeven symbols remain excluded. Maximum two daily entries remains enforced. Replay checks closing time before inspecting the outcome.

## Stock evidence and limits

A fixed ten-stock underlying-only comparison examined 2,400 morning decision observations: 1 baseline signal observation versus 5 with the new gap path. Added observations appeared in BAJFINANCE and ANGELONE. These are not executed option trades or profitable-trade counts. Historical requests had partial-session-tail warnings; historical Greeks/depth and complete option qualification were unavailable.

Actual Swiggy underlying replay moves first PE eligibility from 12:00 to 09:35. However, the 09:35 next-bar entry in the actual October 27 190 PE would stop at a modeled Rs.521.95 net loss, before the later rise. The 195 PE version would stop at Rs.806.59 net loss. This is why the illustrative morning premium increase must not be described as captured profit. One-minute option and underlying data were also retained for sequence review.

## Operations and verification

187 tests pass. Coverage includes bullish/bearish symmetry, future-candle exclusion, the stock EMA exception retaining volume checks, cash-target sizing/risk bounds, re-entry cooldown and future-outcome exclusion, live history circuit recovery, independent session lifetimes and launching from another working directory.

The live-history circuit now permits a bounded probe after 60 seconds for current-session requests instead of remaining blocked all day. Completed-history requests retain their fail-closed circuit behavior. Both direct bot commands and the launcher use the project directory, preventing misplaced configuration/data when launched from elsewhere.

Nifty was reloaded in PAPER mode with no open position; learning and ledger history were retained. Stock source/configuration is ready for the next session; its existing 14:30 entry cutoff was not extended. A 09:10 weekday PAPER auto-start was offered separately and has not been scheduled without the user's answer. Credentials remain private in the project .env; .env.example contains only public profile settings and blank credential fields.

## Source provenance

The source used for this report is preserved in Git commit `95d6213b3647a5517cb62e80c979115b94bdb488`. Redundant copies from the report's `sources/` folder were removed after verifying they were byte-identical to the project modules. The original copies remain recoverable from that commit.


## Requested filter recovery comparison â€” 23 October 2026

Same cached dates, fixed one lot, stop 6.15, next-bar one-minute option execution with adverse slippage. Previous versus updated confirmation window and strong-day candle thresholds. Dates end 14 October; repeatedly examined data, not untouched validation. Partial-chain OI and same-expiry limitations remain. Daily cutoff uses realized net, not intratrade target exits.

```json
[
  {
    "calendar_days": 30,
    "variant": "before_filter_recovery",
    "sessions": 21,
    "trades": 3,
    "gross": 409.5,
    "net": 216.0,
    "wins": 1,
    "unvalidated": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 2,
      "ASYMMETRIC_TRAIL_PROFIT": 1
    }
  },
  {
    "calendar_days": 30,
    "variant": "after_filter_recovery",
    "sessions": 21,
    "trades": 3,
    "gross": 409.5,
    "net": 216.0,
    "wins": 1,
    "unvalidated": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 2,
      "ASYMMETRIC_TRAIL_PROFIT": 1
    }
  },
  {
    "calendar_days": 60,
    "variant": "before_filter_recovery",
    "sessions": 41,
    "trades": 5,
    "gross": 897.0,
    "net": 574.5,
    "wins": 2,
    "unvalidated": 0,
    "exit_reasons": {
      "ASYMMETRIC_TRAIL_PROFIT": 2,
      "INITIAL_SL_HIT": 3
    }
  },
  {
    "calendar_days": 60,
    "variant": "after_filter_recovery",
    "sessions": 41,
    "trades": 7,
    "gross": 780.0,
    "net": 328.5,
    "wins": 3,
    "unvalidated": 0,
    "exit_reasons": {
      "ASYMMETRIC_TRAIL_PROFIT": 3,
      "INITIAL_SL_HIT": 4
    }
  }
]
```

## Stock filter recovery data limitation

The requested 30/60-day comparison selected the first 50 current-universe symbols alphabetically. Initial requests failed with ConnectionError, leaving zero covered stocks. Those zero totals are unavailable results, not zero-profit backtests. A network-enabled retry was stopped after prolonged option-history requests without a completed comparison. No profit result from that retry is scored. The original stock ledger evidence is preserved, with the failed comparison explicitly marked profit_result_usable=false.

Historical bid/ask depth, broker Greeks and chain IV ranges are also unavailable. Even an OHLC exit replay cannot validate profitability of the requested spread/IV/Greek-screen relaxations or delayed-data recovery. The fixed-contract Swiggy before/after exit replay is recorded in swiggy_20261023_REVIEW.md. Both early entries still stop out: 190 PE net -Rs.521.95; 195 PE net -Rs.806.59. One-minute 190 PE data shows 09:35 low Rs.4.00, before the 09:36 high Rs.5.60.

## Requested profile and assessment

Stock settings: minimum estimated net target Rs.900, minimum planned net RR 1.3, base spread cap 3.5%, confirmation 600s; existing local risk Rs.1000 and maximum three lots retained. Absolute confirmed day change >=6% permits 4.5% spread, 55% of required net target in the 1.8-ATR screening scenario, and a 0.90 cross-sectional IV-range cutoff. Normal cash-target-extension screening remains 100%. Nifty uses a 600s confirmation window and 0.40/0.60 body/close strength only after the closed-session range reaches 80 points; otherwise 0.50/0.70. Same-strike OI/premium confirmation and other risk/data gates remain.

Nifty 30 days: unchanged at three trades, gross Rs.409.50, net Rs.216.00. Nifty 60 days: five to seven trades; gross Rs.897.00 to Rs.780.00; net Rs.574.50 to Rs.328.50. Added CE trades were August 17 (+Rs.10.25 net) and September 9 (-Rs.256.25 net). This sample does not demonstrate an improvement. Requested changes are applied to the PAPER configuration for subsequent launches; they were not used to alter actual trading outcomes or production learning.

Validation: 192 regression tests pass, including timing boundaries, future-candle exclusion, strong-mover liquidity/IV/expected-gain exceptions, and retained risk limits. No new source module was created. Necessary replay ledgers/rule state were isolated under the existing research cache.

Current source SHA256:
- `dhan_dynamic_fno_stock_bot.py`: `3c0220a3d032ada568021e9c2bb3815b02318144ffeacaa407ef48e178cab218`
- `nifty_market_structure.py`: `120edf807a346b35e1ef6aa7587c22c4c2be762aa5af04c5be14818ab0a6a5b2`
- `market_regime.py`: `c9ab99466360be7da54d413e38c25d5cb27ce6260e59b0fe7d905550306301dc`
- `stock_option_signals.py`: `98eacbbbe4c9d27abd49db666d56f611fccf5c34139cca02c192d73958dc4a27`

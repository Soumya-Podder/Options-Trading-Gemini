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

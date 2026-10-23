> Latest cap-8/cap-2 rerun is at the end of this report. Stock results remain provisional after the minute-price audit.

> Current audit correction: older stock five-minute rolling-entry P&L is provisional. Exact-strike minute checks found 11 entry-price disagreements and 14 first-bar OHLC disagreements across 73 entries. Read the final entry/exit audit before interpreting older totals or policy comparisons.

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


## Requested filter recovery comparison — 23 October 2026

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

## Current implementation thorough replay

Fresh chronological learning from July 17; actual current sizing and session logic, without the fixed-lot research cap. One-minute option execution, stop-first ambiguity, modeled spreads/slippage; incomplete historical chain/expiry identity and absence of bid/ask prevent full validation. The 0.3% scenario is a declared execution stress check, not a selected variant.

```json
[
  {
    "calendar_days": 30,
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 20,
    "trades": 2,
    "gross": 858.0,
    "net": 729.0,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 474.0,
    "profit_factor": 2.538,
    "closed_trade_drawdown": 474.0,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -409.5,
        "net": -474.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_164110_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 60,
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 41,
    "trades": 5,
    "gross": 74.75,
    "net": -247.75,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 362.69,
    "profit_factor": 0.829,
    "closed_trade_drawdown": 1450.75,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -406.25,
        "net": -470.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -377.0,
        "net": -506.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 858.0,
        "net": 729.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_164110_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 30,
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 20,
    "trades": 2,
    "gross": 841.75,
    "net": 712.75,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 490.25,
    "profit_factor": 2.454,
    "closed_trade_drawdown": 490.25,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -425.75,
        "net": -490.25,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_164110_slippage_0.3\\result.json"
  },
  {
    "calendar_days": 60,
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 41,
    "trades": 5,
    "gross": 19.5,
    "net": -303.0,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 376.5,
    "profit_factor": 0.799,
    "closed_trade_drawdown": 1506.0,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -419.25,
        "net": -483.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -403.0,
        "net": -532.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 841.75,
        "net": 712.75,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_164110_slippage_0.3\\result.json"
  }
]
```

Current-replay reconciliation audit: [{"slippage_pct": 0.1, "trade_count": 13, "errors": []}, {"slippage_pct": 0.3, "trade_count": 13, "errors": []}]. Checks cover gross/net ledger sums, entry cutoffs, same-day execution, cooldowns, daily entry cap and net-learning basis.



## Current stock implementation thorough replay — final

All 210 current-universe stocks were requested separately for each period. Signals use the current rolling five-calendar-day warmup, ranking feedback, target/RR/risk/lot limits, daily slots and re-entry cooldown. Each period received its own bounded network budget. Historical broker Greeks, option depth, chain IV range and actual delta-selected contracts remain absent; these are incomplete exit-policy replays, not full-strategy profitability results.

Raw provider diagnostic: equity security 13 on 21 October returned HTTP 200 with 360 one-minute rows ending 15:14 IST and 72 five-minute rows ending 15:10 IST. The last 15 minutes are missing before replay filtering. Missing windows, request errors and circuit-open failures are retained rather than fabricated. All 210 available equity histories have missing session bars in each evaluated period. Unvalidated candidates/outcomes cannot be counted as either wins or losses.

```json
[
  {
    "calendar_days": 30,
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 14,
    "gross": -2871.25,
    "net": -3889.45,
    "win_rate_pct": 28.57,
    "avg_net_win": 667.89,
    "avg_net_loss": 656.1,
    "profit_factor": 0.407,
    "closed_trade_drawdown": 4337.64,
    "unvalidated": 251,
    "history_issues": 870,
    "stocks_with_missing_session_bars": 210,
    "status": "INCOMPLETE_DATA",
    "full_strategy_validated": false,
    "exit_reasons": {
      "PREMIUM_STOP": 10,
      "TRAIL_STOP": 2,
      "RUPEE_TARGET": 2
    },
    "profitability_validated": false
  },
  {
    "calendar_days": 60,
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 18,
    "gross": -1496.25,
    "net": -2752.29,
    "win_rate_pct": 33.33,
    "avg_net_win": 745.89,
    "avg_net_loss": 602.3,
    "profit_factor": 0.619,
    "closed_trade_drawdown": 5004.23,
    "unvalidated": 537,
    "history_issues": 2579,
    "stocks_with_missing_session_bars": 210,
    "status": "INCOMPLETE_DATA",
    "profitability_validated": false,
    "full_strategy_validated": false,
    "exit_reasons": {
      "RUPEE_TARGET": 4,
      "PREMIUM_STOP": 12,
      "TRAIL_STOP": 2
    }
  }
]
```

Independent stock audit: [{"calendar_days": 30, "scored_trades": 14, "errors": []}, {"calendar_days": 60, "scored_trades": 18, "errors": []}]. Checks cover P&L reconciliation, date/time order, daily entry cap, planned risk, lot limit and minimum planned RR.

Production paper ledgers and learning-state hashes unchanged: **True**. Original stock backtest results are retained in the same ledger; current comparisons are stored under `current_implementation_thorough_20261023`.

## Final interpretation and verification

The 30-calendar-day period is 23 September–22 October 2026; the 60-calendar-day period is 24 August–22 October. Nifty has 20 and 41 index-observed sessions respectively. Nifty current-policy replay with 0.1% slippage per fill: 30 days gross Rs.858.00 / net Rs.729.00 (two trades), 60 days gross Rs.74.75 / net -Rs.247.75 (five trades). At 0.3%: gross Rs.841.75 / net Rs.712.75 and gross Rs.19.50 / net -Rs.303.00. No parameter search or choice of a better-performing variant was made. All current Nifty sizing decisions selected one lot naturally; fixed sizing was not imposed.

Stock observed-subset losses and sparse Nifty activity do not support Rs.15,000 monthly gross profit, 80% winning trades, or seven Rs.1,000-profit days in ten. Incomplete stock inputs and partial-chain Nifty data also prevent validation of the complete live strategy. Do not combine the subset P&L into a forecast of a combined portfolio.

196 regression tests pass; git diff --check passes. Trading parameters were frozen. Changes address replay parity: current daily liquidation, dynamic sizing, time/cooldown handling, historical candle-close timestamps, and live-equivalent stock indicator warmup. Current Nifty trade/day records are also saved in the existing 30days_details.json and 60days_details.json files under nifty_30_60_updated_20261023.

Working-tree source hashes (base Git revision 568eadf):
- `dhan_bot_engine.py`: `e1a7ebd2031faaf1e6bac352302bba7d773ec77cebc40fe1e514c2cdc3d9dff3`
- `index_option_replay.py`: `f369f2b594ae74e8c5e9dbca97ac9a40106c3ed4901c9df6101950999a52ac5b`
- `nifty_research.py`: `65262d1e9dce59328cc853c3b9c3bd49d5ab2f5967da6b72560f7d451d8b0a19`
- `dhan_dynamic_fno_stock_bot.py`: `dbf00419abfeca9970c318a5f587c094f33e266ac6f6428a495c3e95ae328443`
- `nifty_market_structure.py`: `120edf807a346b35e1ef6aa7587c22c4c2be762aa5af04c5be14818ab0a6a5b2`
- `stock_option_signals.py`: `98eacbbbe4c9d27abd49db666d56f611fccf5c34139cca02c192d73958dc4a27`
- `stock_option_policy.py`: `1132d598199bc46a3dac0f9c23d57233850811d0aa4acfe0a0ba068b380f8df0`

## Screenshot comparison — 23 October 2026

The screenshots show NIFTY 50 cash index and SWIGGY OCT FUT; the stock bot uses SWIGGY cash equity for signals. The multi-day evaluation ends October 22 and is not a test of the October 23 chart session. Today's saved Nifty candles reproduce the opening low 22699.70 and show a 09:30 candle close at 22681.95 (known at 09:35). The 09:45 candle retests the opening low (high 22703.15) and closes down at 22661.70 (known at 09:50), with bearish body fraction 0.613 and bearish closing strength 0.907. This is a real price-action candidate, not proof of contemporaneous strike-OI qualification or an executable option fill.

Nifty opening gap is +0.2404% (22742.20 versus previous close 22687.65); the >=0.5% gap-continuation route is inactive, leaving the restricted OI-wall route. Morning logs from the earlier deployed version report no directional OI/momentum signal at 09:40/09:50/10:00. They should not be attributed to the later revised implementation. Full decision-time OI evidence is required before claiming every final gate passed.

Current stock signal replay identifies SWIGGY PE at 09:35; most subsequent early candles remain blocked by the 1.25 ATR EMA-distance rule when no fresh gap/consolidation break exists. The recorded stock session starts at 13:21:44, so it was not active for that morning signal. The cash/futures distinction does not erase the bearish opportunity, but exact futures levels should not be silently substituted for cash-equity levels. The early 190 PE stop-out proves only that specific timing/contract/stop scenario; it does not prove the later bearish opportunity was untradeable.

## Current implementation thorough replay

Fresh chronological learning from July 17; actual current sizing and session logic, without the fixed-lot research cap. One-minute option execution, stop-first ambiguity, modeled spreads/slippage; incomplete historical chain/expiry identity and absence of bid/ask prevent full validation. The 0.3% scenario is a declared execution stress check, not a selected variant.

```json
[
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 20,
    "trades": 2,
    "gross": 858.0,
    "net": 729.0,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 474.0,
    "profit_factor": 2.538,
    "closed_trade_drawdown": 474.0,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -409.5,
        "net": -474.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_182902_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 41,
    "trades": 5,
    "gross": 74.75,
    "net": -247.75,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 362.69,
    "profit_factor": 0.829,
    "closed_trade_drawdown": 1450.75,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -406.25,
        "net": -470.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -377.0,
        "net": -506.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 858.0,
        "net": 729.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_182902_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 20,
    "trades": 2,
    "gross": 841.75,
    "net": 712.75,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 490.25,
    "profit_factor": 2.454,
    "closed_trade_drawdown": 490.25,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -425.75,
        "net": -490.25,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_182902_slippage_0.3\\result.json"
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 41,
    "trades": 5,
    "gross": 19.5,
    "net": -303.0,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 376.5,
    "profit_factor": 0.799,
    "closed_trade_drawdown": 1506.0,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -419.25,
        "net": -483.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -403.0,
        "net": -532.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 841.75,
        "net": 712.75,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_182902_slippage_0.3\\result.json"
  }
]
```

## Historical retry and requested session window, 23 October 2026

The active trading window is 09:15-14:45 IST. Prior reports used a later session
end. Active-session coverage now requires 66 five-minute bars (09:15 through
14:40), or 330 one-minute bars (09:15 through 14:44), with exits by 14:45.
Prior-day exchange close/high/low and indicator warmup remain historical context;
these are distinct from permitting afternoon trades.

A read-only retry of the equity tail returned HTTP 200 but zero bars for
22 October 15:10-15:30. A one-minute request on 21 October returned its last bar
at 15:14; the full 22 October five-minute request returned 72 bars ending 15:10.
These later missing bars are outside the user's active session.

Five attempted 30-day rolling-option retries timed out. Smaller five-day
requests succeeded, and 59 missing/warning windows were saved before switching
to candidate-date retrieval. The replay now fetches only the five-day option
block needed by each candidate, instead of the whole study period for every
symbol and side. Returned gaps are retained rather than filled. Counts of prior
circuit-open skips are not provider-confirmed permanent gaps.

The reruns use a different session end and improved retrieval, so changes versus
the earlier stock results cannot be attributed solely to one factor. Historical
bid/ask size, broker Greeks, full same-expiry IV ranges, full historical Nifty
chains and historical contract identity remain insufficient for full live-bot
reproduction. Production learning state is not trained by these isolated runs.

All 205 regression tests passed, including 14:45 cutoff, missing-tail coverage,
post-cutoff OHLC independence, polling cadence, entry gates and paper exit tests.

Live cutoff checks now refresh wall-clock time after HTTP waits. Stock paper exit checks also use the post-response time, so newly received depth is not rejected for a negative receipt age. The existing regression test now simulates that request delay. All 205 tests passed again.

## Current stock implementation thorough replay

All current-universe stocks requested. Shared signal evaluator with live five-calendar-day lookback, current target/RR/risk/lot limits, ranking feedback, two-entry cap and re-entry cooldown. Historical broker Greeks, bid/ask, IV cross-section and delta selection are absent, so these are partial exit-policy results, never a full-strategy profitability claim. Production learning and paper ledgers unchanged: True.

```json
[
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 36,
    "gross": -5401.25,
    "net": -8148.29,
    "win_rate_pct": 30.56,
    "avg_net_win": 828.33,
    "avg_net_loss": 690.4,
    "profit_factor": 0.528,
    "closed_trade_drawdown": 9115.63,
    "unvalidated": 3,
    "history_issues": 5,
    "stocks_with_missing_session_bars": 0,
    "status": "INCOMPLETE_DATA",
    "profitability_validated": false,
    "full_strategy_validated": false,
    "exit_reasons": {
      "RUPEE_TARGET": 7,
      "PREMIUM_STOP": 23,
      "TRAIL_STOP": 4,
      "EOD_SQUAREOFF": 1,
      "NO_PROGRESS": 1
    }
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 73,
    "gross": -2575.0,
    "net": -8158.24,
    "win_rate_pct": 36.99,
    "avg_net_win": 842.01,
    "avg_net_loss": 671.58,
    "profit_factor": 0.736,
    "closed_trade_drawdown": 10203.9,
    "unvalidated": 8,
    "history_issues": 14,
    "stocks_with_missing_session_bars": 0,
    "status": "INCOMPLETE_DATA",
    "profitability_validated": false,
    "full_strategy_validated": false,
    "exit_reasons": {
      "RUPEE_TARGET": 19,
      "TRAIL_STOP": 8,
      "PREMIUM_STOP": 41,
      "NO_PROGRESS": 3,
      "EOD_SQUAREOFF": 2
    }
  }
]
```

## Consolidated retry results: 09:15-14:45 IST

| Bot | Calendar days | Scored trades | Gross Rs. | Charges Rs. | Net Rs. |
|---|---:|---:|---:|---:|---:|
| Nifty, 0.1% modeled slippage | 30 | 2 | 858.00 | 129.00 | 729.00 |
| Nifty, 0.1% modeled slippage | 60 | 5 | 74.75 | 322.50 | -247.75 |
| Stock options, exit-policy replay | 30 | 36 | -5401.25 | 2747.04 | -8148.29 |
| Stock options, exit-policy replay | 60 | 73 | -2575.00 | 5583.24 | -8158.24 |

The 30/60-day periods end before 23 October (20/41 observed sessions). All 210 stocks were available in both periods; no missing bars were found in their requested active-session coverage. Remaining stock history issues are listed below, separately from the unvalidated candidate/outcome counts. The retry recovered data and reduced request skips, but the observed exit-policy results remain negative after charges. Full historical decision-time inputs remain incomplete.

```json
[
  {
    "calendar_days": 30,
    "gross": -5401.25,
    "charges": 2747.04,
    "net": -8148.29,
    "trades": 36,
    "issues_by_reason": {
      "internal_bar_gap": 5
    },
    "option_window_sources": {
      "cache": 18,
      "network": 46
    },
    "data_rejections": {
      "simulation_missing_invalid_bars_or_strike_change": 2,
      "entry_bar_missing_or_invalid_strike": 1
    },
    "max_entries_per_day": 2,
    "exit_time_and_pnl_audit": "passed"
  },
  {
    "calendar_days": 60,
    "gross": -2575.0,
    "charges": 5583.24,
    "net": -8158.24,
    "trades": 73,
    "issues_by_reason": {
      "internal_bar_gap": 13,
      "invalid_or_empty_data": 1
    },
    "option_window_sources": {
      "cache": 68,
      "network": 90
    },
    "data_rejections": {
      "option_history_unavailable": 1,
      "simulation_missing_invalid_bars_or_strike_change": 5,
      "entry_bar_missing_or_invalid_strike": 2
    },
    "max_entries_per_day": 2,
    "exit_time_and_pnl_audit": "passed"
  }
]
```

Trade P&L sums, two-entry daily caps, three-lot ceilings and all exits by 14:45 were checked. Production paper ledgers and learning state unchanged: True.

## Direction audit: signal routing versus subsequent movement

This is a diagnostic of saved trades, not a reversed-side strategy backtest.
All 36 stock entries in the 30-day study and all 73 in the 60-day study match
the current signal recomputed from the identical closed five-calendar-day
equity history available at entry. Bullish signals select CE and bearish signals
select PE. Contract routing reads the corresponding ce/pe leg; live Greek checks
require positive call delta and negative put delta. Both bought option types use
(exit premium minus entry premium) for gross P&L. No blanket sign reversal was
found. Production entry rules were not changed by this audit.

For the 60-day stock sample, the underlying moved favorably from entry open to
the next five-minute close in 36 trades, adversely in 36 and was flat in one.
At 15 minutes, 36 of 71 observed prices were favorable, 33 adverse and two flat.
Among 46 net-losing option trades, 14 had favorable underlying movement by their
exit candle close, 31 adverse and one flat. The cash candle close is not the
unknown exact option-stop timestamp: a stop-first intrabar path can still lose
even when the underlying subsequently closes favorably. These figures show weak
follow-through; they do not establish an opposite-option profit.

All five Nifty selected sides match their last closed confirmation candle.
However, the level engine permits countertrend price reactions: on 9 September
10:25 it selected CE while the index was 96.4 points below its previous-session
close; on 21 September 10:05 it selected PE while the index was 36.1 points above
its previous-session close. These produced net losses of Rs.256.25 and Rs.249.75.
The 7 October PE also lost after a bearish confirmation on a down day, so a
previous-close trend filter alone does not explain every loss.

Per-entry diagnostics are saved under direction_audit_20261023 in the existing
stock ledger and Nifty detail files. Production ledger/learning hashes were
checked again and remained unchanged.

## Predeclared entry/exit path audit

Previously examined history; chronological comparison is exploratory. Original entry opportunities are paired and replacements are not simulated. No production promotion.

```json
{
  "plan": {
    "purpose": "Entry and exit path diagnosis; no production parameter promotion",
    "window_ist": "09:15-14:45",
    "development": "2026-08-24 through 2026-09-22",
    "later_chronological_check": "2026-09-23 through 2026-10-22",
    "validation_status": "Previously inspected history; exploratory, not untouched holdout",
    "policies": {
      "baseline": "Current execution and risk rules",
      "trend_alignment": "Enter only if last completed 15-minute EMA10 slope and confirmation close versus EMA agree with selected side",
      "wait_one_close": "Wait for next five-minute close; require directional candle and hold beyond original confirmation close, no more than 0.5 original 5m ATR extension; enter next observed option open",
      "net_breakeven_1R": "After observed premium high reaches both 1 initial-risk unit and net breakeven price, protect net breakeven from following bar, retaining existing trailing rule"
    },
    "sizing": "Candidate lots cannot exceed original trade lots, risk cap unchanged; actual new entry open repriced",
    "comparison": "Paired original entry opportunities; filtered or delayed entries do not rerun scanner/ranking, learning, replacement entries or portfolio wallet. Not a deployable strategy backtest.",
    "promotion": "No production promotion from this retrospective sample; require a future paper sample with net-positive expectancy, bounded drawdown and sufficient trades",
    "limitations": [
      "Five-minute stock intrabar order unknown; stop-first ties",
      "Post-exit rolling strike changes truncate exact-contract paths",
      "Missing historical bid/ask, broker Greeks and full Nifty chain",
      "One-minute Nifty exit-minute OHLC is still not exact fill sequencing"
    ]
  },
  "stock": {
    "baseline": {
      "all": {
        "opportunities": 73,
        "scored": 73,
        "skipped": 0,
        "unvalidated": 0,
        "net": -8158.24,
        "win_rate_pct": 36.99,
        "stop_exit_count": 49,
        "target_exit_count": 19,
        "profit_factor": 0.736,
        "closed_trade_drawdown": 10203.9,
        "exit_reasons": {
          "RUPEE_TARGET": 19,
          "TRAIL_STOP": 8,
          "PREMIUM_STOP": 41,
          "NO_PROGRESS": 3,
          "EOD_SQUAREOFF": 2
        }
      },
      "earlier": {
        "opportunities": 37,
        "scored": 37,
        "skipped": 0,
        "unvalidated": 0,
        "net": -9.95,
        "win_rate_pct": 43.24,
        "stop_exit_count": 22,
        "target_exit_count": 12,
        "profit_factor": 0.999,
        "closed_trade_drawdown": 4884.15,
        "exit_reasons": {
          "RUPEE_TARGET": 12,
          "TRAIL_STOP": 4,
          "PREMIUM_STOP": 18,
          "NO_PROGRESS": 2,
          "EOD_SQUAREOFF": 1
        }
      },
      "later": {
        "opportunities": 36,
        "scored": 36,
        "skipped": 0,
        "unvalidated": 0,
        "net": -8148.29,
        "win_rate_pct": 30.56,
        "stop_exit_count": 27,
        "target_exit_count": 7,
        "profit_factor": 0.528,
        "closed_trade_drawdown": 9115.63,
        "exit_reasons": {
          "RUPEE_TARGET": 7,
          "PREMIUM_STOP": 23,
          "TRAIL_STOP": 4,
          "EOD_SQUAREOFF": 1,
          "NO_PROGRESS": 1
        }
      }
    },
    "trend_alignment": {
      "all": {
        "opportunities": 73,
        "scored": 72,
        "skipped": 1,
        "unvalidated": 0,
        "net": -7569.95,
        "win_rate_pct": 37.5,
        "stop_exit_count": 48,
        "target_exit_count": 19,
        "profit_factor": 0.75,
        "closed_trade_drawdown": 9615.61,
        "exit_reasons": {
          "RUPEE_TARGET": 19,
          "TRAIL_STOP": 8,
          "PREMIUM_STOP": 40,
          "NO_PROGRESS": 3,
          "EOD_SQUAREOFF": 2
        }
      },
      "earlier": {
        "opportunities": 37,
        "scored": 36,
        "skipped": 1,
        "unvalidated": 0,
        "net": 578.34,
        "win_rate_pct": 44.44,
        "stop_exit_count": 21,
        "target_exit_count": 12,
        "profit_factor": 1.044,
        "closed_trade_drawdown": 4295.86,
        "exit_reasons": {
          "RUPEE_TARGET": 12,
          "TRAIL_STOP": 4,
          "PREMIUM_STOP": 17,
          "NO_PROGRESS": 2,
          "EOD_SQUAREOFF": 1
        }
      },
      "later": {
        "opportunities": 36,
        "scored": 36,
        "skipped": 0,
        "unvalidated": 0,
        "net": -8148.29,
        "win_rate_pct": 30.56,
        "stop_exit_count": 27,
        "target_exit_count": 7,
        "profit_factor": 0.528,
        "closed_trade_drawdown": 9115.63,
        "exit_reasons": {
          "RUPEE_TARGET": 7,
          "PREMIUM_STOP": 23,
          "TRAIL_STOP": 4,
          "EOD_SQUAREOFF": 1,
          "NO_PROGRESS": 1
        }
      }
    },
    "wait_one_close": {
      "all": {
        "opportunities": 73,
        "scored": 16,
        "skipped": 53,
        "unvalidated": 4,
        "net": -1093.29,
        "win_rate_pct": 43.75,
        "stop_exit_count": 12,
        "target_exit_count": 3,
        "profit_factor": 0.785,
        "closed_trade_drawdown": 2543.07,
        "exit_reasons": {
          "PREMIUM_STOP": 8,
          "TRAIL_STOP": 4,
          "NO_PROGRESS": 1,
          "RUPEE_TARGET": 3
        }
      },
      "earlier": {
        "opportunities": 37,
        "scored": 9,
        "skipped": 27,
        "unvalidated": 1,
        "net": -1367.44,
        "win_rate_pct": 44.44,
        "stop_exit_count": 7,
        "target_exit_count": 1,
        "profit_factor": 0.509,
        "closed_trade_drawdown": 1814.54,
        "exit_reasons": {
          "PREMIUM_STOP": 4,
          "TRAIL_STOP": 3,
          "NO_PROGRESS": 1,
          "RUPEE_TARGET": 1
        }
      },
      "later": {
        "opportunities": 36,
        "scored": 7,
        "skipped": 26,
        "unvalidated": 3,
        "net": 274.15,
        "win_rate_pct": 42.86,
        "stop_exit_count": 5,
        "target_exit_count": 2,
        "profit_factor": 1.119,
        "closed_trade_drawdown": 1175.63,
        "exit_reasons": {
          "PREMIUM_STOP": 4,
          "TRAIL_STOP": 1,
          "RUPEE_TARGET": 2
        }
      }
    },
    "net_breakeven_1R": {
      "all": {
        "opportunities": 73,
        "scored": 73,
        "skipped": 0,
        "unvalidated": 0,
        "net": -8158.24,
        "win_rate_pct": 36.99,
        "stop_exit_count": 49,
        "target_exit_count": 19,
        "profit_factor": 0.736,
        "closed_trade_drawdown": 10203.9,
        "exit_reasons": {
          "RUPEE_TARGET": 19,
          "TRAIL_STOP": 8,
          "PREMIUM_STOP": 41,
          "NO_PROGRESS": 3,
          "EOD_SQUAREOFF": 2
        }
      },
      "earlier": {
        "opportunities": 37,
        "scored": 37,
        "skipped": 0,
        "unvalidated": 0,
        "net": -9.95,
        "win_rate_pct": 43.24,
        "stop_exit_count": 22,
        "target_exit_count": 12,
        "profit_factor": 0.999,
        "closed_trade_drawdown": 4884.15,
        "exit_reasons": {
          "RUPEE_TARGET": 12,
          "TRAIL_STOP": 4,
          "PREMIUM_STOP": 18,
          "NO_PROGRESS": 2,
          "EOD_SQUAREOFF": 1
        }
      },
      "later": {
        "opportunities": 36,
        "scored": 36,
        "skipped": 0,
        "unvalidated": 0,
        "net": -8148.29,
        "win_rate_pct": 30.56,
        "stop_exit_count": 27,
        "target_exit_count": 7,
        "profit_factor": 0.528,
        "closed_trade_drawdown": 9115.63,
        "exit_reasons": {
          "RUPEE_TARGET": 7,
          "PREMIUM_STOP": 23,
          "TRAIL_STOP": 4,
          "EOD_SQUAREOFF": 1,
          "NO_PROGRESS": 1
        }
      }
    }
  },
  "nifty": {
    "baseline": {
      "all": {
        "opportunities": 5,
        "scored": 5,
        "skipped": 0,
        "unvalidated": 0,
        "net": -247.75,
        "win_rate_pct": 20.0,
        "stop_exit_count": 4,
        "target_exit_count": 1,
        "profit_factor": 0.829,
        "closed_trade_drawdown": 1450.75,
        "exit_reasons": {
          "INITIAL_SL_HIT": 4,
          "DAILY_NET_PROFIT_TARGET": 1
        }
      },
      "earlier": {
        "opportunities": 3,
        "scored": 3,
        "skipped": 0,
        "unvalidated": 0,
        "net": -976.75,
        "win_rate_pct": 0.0,
        "stop_exit_count": 3,
        "target_exit_count": 0,
        "profit_factor": 0.0,
        "closed_trade_drawdown": 976.75,
        "exit_reasons": {
          "INITIAL_SL_HIT": 3
        }
      },
      "later": {
        "opportunities": 2,
        "scored": 2,
        "skipped": 0,
        "unvalidated": 0,
        "net": 729.0,
        "win_rate_pct": 50.0,
        "stop_exit_count": 1,
        "target_exit_count": 1,
        "profit_factor": 2.538,
        "closed_trade_drawdown": 474.0,
        "exit_reasons": {
          "INITIAL_SL_HIT": 1,
          "DAILY_NET_PROFIT_TARGET": 1
        }
      }
    },
    "trend_alignment": {
      "all": {
        "opportunities": 5,
        "scored": 1,
        "skipped": 4,
        "unvalidated": 0,
        "net": 1203.0,
        "win_rate_pct": 100.0,
        "stop_exit_count": 0,
        "target_exit_count": 1,
        "profit_factor": null,
        "closed_trade_drawdown": 0,
        "exit_reasons": {
          "DAILY_NET_PROFIT_TARGET": 1
        }
      },
      "earlier": {
        "opportunities": 3,
        "scored": 0,
        "skipped": 3,
        "unvalidated": 0,
        "net": 0,
        "win_rate_pct": null,
        "stop_exit_count": 0,
        "target_exit_count": 0,
        "profit_factor": null,
        "closed_trade_drawdown": 0,
        "exit_reasons": {}
      },
      "later": {
        "opportunities": 2,
        "scored": 1,
        "skipped": 1,
        "unvalidated": 0,
        "net": 1203.0,
        "win_rate_pct": 100.0,
        "stop_exit_count": 0,
        "target_exit_count": 1,
        "profit_factor": null,
        "closed_trade_drawdown": 0,
        "exit_reasons": {
          "DAILY_NET_PROFIT_TARGET": 1
        }
      }
    },
    "wait_one_close": {
      "all": {
        "opportunities": 5,
        "scored": 1,
        "skipped": 4,
        "unvalidated": 0,
        "net": -256.25,
        "win_rate_pct": 0.0,
        "stop_exit_count": 1,
        "target_exit_count": 0,
        "profit_factor": 0.0,
        "closed_trade_drawdown": 256.25,
        "exit_reasons": {
          "INITIAL_SL_HIT": 1
        }
      },
      "earlier": {
        "opportunities": 3,
        "scored": 1,
        "skipped": 2,
        "unvalidated": 0,
        "net": -256.25,
        "win_rate_pct": 0.0,
        "stop_exit_count": 1,
        "target_exit_count": 0,
        "profit_factor": 0.0,
        "closed_trade_drawdown": 256.25,
        "exit_reasons": {
          "INITIAL_SL_HIT": 1
        }
      },
      "later": {
        "opportunities": 2,
        "scored": 0,
        "skipped": 2,
        "unvalidated": 0,
        "net": 0,
        "win_rate_pct": null,
        "stop_exit_count": 0,
        "target_exit_count": 0,
        "profit_factor": null,
        "closed_trade_drawdown": 0,
        "exit_reasons": {}
      }
    },
    "net_breakeven_1R": {
      "all": {
        "opportunities": 5,
        "scored": 5,
        "skipped": 0,
        "unvalidated": 0,
        "net": 135.75,
        "win_rate_pct": 40.0,
        "stop_exit_count": 4,
        "target_exit_count": 1,
        "profit_factor": 1.127,
        "closed_trade_drawdown": 1067.25,
        "exit_reasons": {
          "INITIAL_SL_HIT": 2,
          "ASYMMETRIC_TRAIL_PROFIT": 2,
          "DAILY_NET_PROFIT_TARGET": 1
        }
      },
      "earlier": {
        "opportunities": 3,
        "scored": 3,
        "skipped": 0,
        "unvalidated": 0,
        "net": -593.25,
        "win_rate_pct": 33.33,
        "stop_exit_count": 3,
        "target_exit_count": 0,
        "profit_factor": 0.001,
        "closed_trade_drawdown": 593.75,
        "exit_reasons": {
          "INITIAL_SL_HIT": 1,
          "ASYMMETRIC_TRAIL_PROFIT": 2
        }
      },
      "later": {
        "opportunities": 2,
        "scored": 2,
        "skipped": 0,
        "unvalidated": 0,
        "net": 729.0,
        "win_rate_pct": 50.0,
        "stop_exit_count": 1,
        "target_exit_count": 1,
        "profit_factor": 2.538,
        "closed_trade_drawdown": 474.0,
        "exit_reasons": {
          "INITIAL_SL_HIT": 1,
          "DAILY_NET_PROFIT_TARGET": 1
        }
      }
    }
  },
  "production_state_unchanged": true
}
```

## Thorough entry/exit audit: verified minute-path findings

The active session is 09:15-14:45 IST. The earlier chronological block is
24 August-22 September; the later block is 23 September-22 October. These dates
were already examined, so all comparisons are exploratory. None of the tested
trading policies was promoted to production.

### Material correction to stock data

All 73 original stock entries were checked using real one-minute rolling series
stitched to the original labeled strike. Eleven entry prices disagree with the
five-minute-based modeled entries. Fourteen five-minute first-candle OHLC checks
disagree with exact-strike minute aggregation; 56 match and three aggregate
checks remain unavailable. This establishes an interval/contract inconsistency,
not its provider-side cause. The original rolling strike is not independently
proven known at the entry open.

Examples of modeled fills: INDHOTEL 790 CE on 22 October 10:15, 10.90 versus
6.95 from the minute series; BSE 3300 PE on 16 October 09:30, 88.40 versus 64.45;
POLICYBZR 1140 PE on 25 September 10:35, 49.75 versus 57.65. Such differences can
create false stop paths and incorrect lot/risk qualification. Dhan documents
this as minute-level data rolling relative to spot:
https://dhanhq.co/docs/v2/expired-options-data/

At repriced entry premiums, 70 original opportunities fit the original lot and
risk constraints; three do not. Conditional minute-path net is -7722.26, with
28 winners and 42 losers. One stop/target ordering remains ambiguous within a
minute. These are paired conditional paths, not a new full portfolio run:
replacement entries, changed ranking/learning, historical delta selection and
wallet interactions were not reconstructed. Older five-minute stock totals and
comparisons are explicitly provisional.

### Stock minute-path sensitivities

| Policy | Scored | Earlier net Rs. | Later net Rs. | Total net Rs. | Unvalidated | Filtered/no plan |
|---|---:|---:|---:|---:|---:|---:|
| Current execution rules | 70 | -73.23 | -7649.03 | -7722.26 | 0 | 3 |
| Completed 15m EMA alignment | 69 | 515.06 | -7649.03 | -7133.97 | 0 | 4 |
| Wait next 5m close, bounded chase | 18 | -2786.24 | 783.81 | -2002.43 | 0 | 55 |
| Protect net breakeven after 1R | 70 | -73.23 | -7649.03 | -7722.26 | 0 | 3 |
| 1x option ATR stop sensitivity | 19 | -1683.01 | 1332.06 | -350.95 | 40 | 14 |
| Trail activation at 75% of target | 69 | 3262.86 | -7667.45 | -4404.59 | 1 | 3 |

The added ATR sensitivity requires five complete pre-entry, exact-contract
five-minute candles. Its stop is at least one ATR, at least the current stop,
at most 10% of premium, with no increase to lot or cash-risk ceilings. Forty
opportunities lack the required evidence. On the 19 jointly scored opportunities,
current-policy net is -1284.16 versus -350.95 for the ATR rule. Earlier results
are identical (-1683.01); improvement comes from seven later cases. This is
insufficient evidence to activate it.

The added trailing sensitivity delays activation from two to three quarter-target
jumps (50% to 75% of target distance), retaining initial stop, target, risk and
lot ceilings. Target exits rise from 13 to 19, but initial-stop exits rise from
34 to 37 and one extended path is unavailable. The later block stays negative.
A change in target-hit count alone is not a promotion criterion.

### Entry and stop geometry

The minute baseline has 34 initial-stop exits (ten in the entry minute),
17 trailing-stop exits, 13 target exits, three no-progress exits and three
session-end exits. Mean net win is 667.68 and mean net loss 628.98. Realized
reward/loss is about 1.06, requiring a 48.51% win rate; observed win rate is 40%.
Planned RR and a 900 net target do not ensure that trailing exits achieve that
average reward. Expected net return is -110.32 per scored conditional trade.

Thirty-three entries have complete pre-entry exact-strike premium history for
five-minute ATR. Among 13 initial-stop trades in that measured subset, eight
stops are below one ATR; median stop/ATR is about 0.79. This supports measuring
volatility/structure stop room, not widening every stop. Where an appropriate
stop cannot fit one affordable lot inside the cash-risk budget, waiting for a
better entry is preferable to increasing exposure. Gaps can exceed estimated
cash-risk limits; one minute-path net loss is 1056.23 despite the 1000 plan cap.

Configured spread can consume nearly all stop room. An illustrative ask of 100,
stop of 95 and bid of 95.50 passes a 4.5% strong-mover spread ceiling while leaving
only 0.50 of bid movement before stop. This consumes 90% of the initial premium
risk. It is a configuration-derived example, not reconstructed historical depth.
New paper records retain this geometry to measure it prospectively.

### Nifty findings

All four losing entries oppose the measured completed-15m EMA slope and price
context; the winner aligns. Alignment retains only one trade, so its apparent
100% win rate is not statistically persuasive. Five trades cannot establish a
robust direction filter. The fixed 6.15-point stop ranges from about 0.59 to
2.80 times pre-entry minute premium ATR, with the winner at the low end. A blanket
volatility cutoff could remove the winner.

| Nifty diagnostic | Scored | Net Rs. | Interpretation |
|---|---:|---:|---|
| Current policy | 5 | -247.75 | Four stop exits, one target |
| 15m EMA alignment | 1 | 1203.00 | Four removed; inadequate sample |
| Wait one more close | 1 | -256.25 | Also excludes winner |
| Net breakeven protection after 1R | 5 | 135.75 | Earlier -593.25; later unchanged at 729.00 |

Two losers first reach about +1.05R before giving back the gain. Protection after
1R reduces these losses, but an observed opening gap still produces -123 net in
one protected trade. Breakeven is a planned threshold, not a guaranteed fill.
Nifty currently models entry at LTP; current bid/ask and verified fills remain
necessary for execution accuracy. New entries retain chain receipt time,
decision time, LTP, bid/ask and displayed quantities for review.

### Implemented accuracy and evidence changes

- Stock replay handles known opening stop/target events before unknown intrabar
  high/low ordering. Stop-first remains the rule for unresolved intrabar ties.
- Scored five-minute stock replays remain INCOMPLETE_DATA pending contract-price
  verification, with price_identity_validated explicitly false.
- Stock closes preserve entry-time spread/stop geometry, Greeks, signal/quote
  ages, EMA slope/alignment and planned stop/target evidence.
- No experimental entry filter, stop widening, trailing threshold or target was
  activated. Session, targets, lot ceilings and cash-risk settings are retained.

All 207 regression tests passed. Production paper ledger and learning-state
hashes remain unchanged. No statistically supported policy improvement was
found that worked in both chronological blocks with adequate coverage.

## Current implementation thorough replay

Fresh chronological learning from July 17; actual current sizing and session logic, without the fixed-lot research cap. One-minute option execution, stop-first ambiguity, modeled spreads/slippage; incomplete historical chain/expiry identity and absence of bid/ask prevent full validation. The 0.3% scenario is a declared execution stress check, not a selected variant.

```json
[
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 20,
    "trades": 2,
    "gross": 858.0,
    "net": 729.0,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 474.0,
    "profit_factor": 2.538,
    "closed_trade_drawdown": 474.0,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -409.5,
        "net": -474.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_193933_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.1,
    "sessions": 41,
    "trades": 5,
    "gross": 74.75,
    "net": -247.75,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 362.69,
    "profit_factor": 0.829,
    "closed_trade_drawdown": 1450.75,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -406.25,
        "net": -470.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -377.0,
        "net": -506.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 858.0,
        "net": 729.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_193933_slippage_0.1\\result.json"
  },
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-09-23",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 20,
    "trades": 2,
    "gross": 841.75,
    "net": 712.75,
    "win_rate_pct": 50.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 490.25,
    "profit_factor": 2.454,
    "closed_trade_drawdown": 490.25,
    "zero_trade_days": 18,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 1,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 2
    },
    "ten_session_blocks": [
      {
        "start": "2026-09-23",
        "end": "2026-10-07",
        "sessions": 10,
        "gross": -425.75,
        "net": -490.25,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-08",
        "end": "2026-10-22",
        "sessions": 10,
        "gross": 1267.5,
        "net": 1203.0,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_193933_slippage_0.3\\result.json"
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "from_inclusive": "2026-08-24",
    "to_exclusive": "2026-10-23",
    "slippage_pct": 0.3,
    "sessions": 41,
    "trades": 5,
    "gross": 19.5,
    "net": -303.0,
    "win_rate_pct": 20.0,
    "avg_net_win": 1203.0,
    "avg_net_loss": 376.5,
    "profit_factor": 0.799,
    "closed_trade_drawdown": 1506.0,
    "zero_trade_days": 36,
    "days_net_ge_1200": 1,
    "unvalidated": 0,
    "coverage_issues": 0,
    "exit_reasons": {
      "INITIAL_SL_HIT": 4,
      "DAILY_NET_PROFIT_TARGET": 1
    },
    "lot_counts": {
      "1": 5
    },
    "ten_session_blocks": [
      {
        "start": "2026-08-24",
        "end": "2026-09-04",
        "sessions": 10,
        "gross": -419.25,
        "net": -483.75,
        "trades": 1,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-07",
        "end": "2026-09-21",
        "sessions": 10,
        "gross": -403.0,
        "net": -532.0,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-09-22",
        "end": "2026-10-06",
        "sessions": 10,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-07",
        "end": "2026-10-21",
        "sessions": 10,
        "gross": 841.75,
        "net": 712.75,
        "trades": 2,
        "unvalidated_days": 0,
        "target_met": false
      },
      {
        "start": "2026-10-22",
        "end": "2026-10-22",
        "sessions": 1,
        "gross": 0.0,
        "net": 0.0,
        "trades": 0,
        "unvalidated_days": 0,
        "target_met": false
      }
    ],
    "status": "PARTIAL_CHAIN_RESEARCH_NOT_FULL_STRATEGY_VALIDATION",
    "result_path": "E:\\Options-TradingBot-Gemini\\dhan_hist_cache\\nifty_regime_review_20261023\\current_20261023_193933_slippage_0.3\\result.json"
  }
]
```

## Current stock implementation thorough replay

All current-universe stocks requested. Shared signal evaluator with live five-calendar-day lookback, current target/RR/risk/lot limits, ranking feedback, two-entry cap and re-entry cooldown. Historical broker Greeks, bid/ask, IV cross-section and delta selection are absent, so these are partial exit-policy results, never a full-strategy profitability claim. Production learning and paper ledgers unchanged: True.

```json
[
  {
    "calendar_days": 30,
    "trading_window_ist": "09:15-14:45",
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 36,
    "gross": -5401.25,
    "net": -8148.29,
    "win_rate_pct": 30.56,
    "avg_net_win": 828.33,
    "avg_net_loss": 690.4,
    "profit_factor": 0.528,
    "closed_trade_drawdown": 9115.63,
    "unvalidated": 3,
    "history_issues": 5,
    "stocks_with_missing_session_bars": 0,
    "status": "INCOMPLETE_DATA",
    "profitability_validated": false,
    "full_strategy_validated": false,
    "exit_reasons": {
      "RUPEE_TARGET": 7,
      "PREMIUM_STOP": 23,
      "TRAIL_STOP": 4,
      "EOD_SQUAREOFF": 1,
      "NO_PROGRESS": 1
    }
  },
  {
    "calendar_days": 60,
    "trading_window_ist": "09:15-14:45",
    "requested_stocks": 210,
    "available_stocks": 210,
    "trades": 73,
    "gross": -2575.0,
    "net": -8158.24,
    "win_rate_pct": 36.99,
    "avg_net_win": 842.01,
    "avg_net_loss": 671.58,
    "profit_factor": 0.736,
    "closed_trade_drawdown": 10203.9,
    "unvalidated": 8,
    "history_issues": 14,
    "stocks_with_missing_session_bars": 0,
    "status": "INCOMPLETE_DATA",
    "profitability_validated": false,
    "full_strategy_validated": false,
    "exit_reasons": {
      "RUPEE_TARGET": 19,
      "TRAIL_STOP": 8,
      "PREMIUM_STOP": 41,
      "NO_PROGRESS": 3,
      "EOD_SQUAREOFF": 2
    }
  }
]
```

## Latest current implementation rerun: Nifty cap 8, stocks cap 2

Completed 23 October 2026. Evaluation windows end 22 October (end-exclusive 23 October): 30 calendar days / 20 Nifty sessions and 60 calendar days / 41 Nifty sessions. This rerun does not include 23 October. Session 09:15-14:45 IST; no performance scoring after cutoff. No parameter search or production learning replacement. Nifty starts fresh learning on 17 July and continues chronologically into both evaluation windows; these are overlapping evaluation windows, not independent holdouts. Each stock window starts its own replay wallet. Each engine is modeled separately; do not add results as a shared-capital portfolio.

| Replay | Days | Trades | Gross Rs | Modeled charges Rs | Net Rs | Net win rate | Closed-trade drawdown Rs |
|---|---:|---:|---:|---:|---:|---:|---:|
| Nifty partial-chain, 0.1% slippage | 30 | 2 | 858.00 | 129.00 | 729.00 | 50.0% | 474.00 |
| Nifty partial-chain, 0.1% slippage | 60 | 5 | 74.75 | 322.50 | -247.75 | 20.0% | 1450.75 |
| Stock five-minute, PROVISIONAL | 30 | 36 | -5401.25 | 2747.04 | -8148.29 | 30.56% | 9115.63 |
| Stock five-minute, PROVISIONAL | 60 | 73 | -2575.00 | 5583.24 | -8158.24 | 36.99% | 10203.90 |

Nifty 0.3% adverse-slippage stress net: Rs 712.75 (30 days), Rs -303.00 (60 days). Base-case zero-trade sessions: 18/20 and 36/41. Neither evaluation period reached the daily entry cap; raising it to eight did not create additional signals. There are zero unresolved attempted Nifty outcomes in these periods, but partial-chain OI/expiry assumptions and absent historical quotes still prevent full-bot validation. Modeled slippage does not reconstruct historical bid/ask spreads.

All 210 current stock-universe underlyings have requested-session coverage. This is not historical membership coverage. Option history still has unresolved cases: 30-day replay 3 candidates/outcomes and 5 history events; 60-day replay 8 candidates/outcomes and 14 history events. Missing outcomes are retained as unresolved, not assigned fabricated P&L.

| Same-opportunity stock minute audit | Opportunities checked | Scored | No plan at corrected premium | Conditional net Rs | Entry-price mismatches |
|---|---:|---:|---:|---:|---:|
| 30 days | 36 | 34 | 2 | -7649.03 | 9 |
| 60 days | 73 | 70 | 3 | -7722.26 | 11 |

The minute audit fixes the original selected strike as a diagnostic, reprices the entry from one-minute data, and reruns the existing plan/exit policy under the original lot ceiling. It does not rerun ranking, replacement opportunities, wallet or learning after repricing and therefore is not a corrected portfolio result. Across 73 entries, 11 entry prices and 14 first-bar OHLC values disagree between intervals; 3 first-bar aggregation comparisons remain unverified. One minute still has ambiguous stop/target ordering; stop-first is used. Historical broker Greeks, same-expiry IV range, bid/ask depth, full decision-time chain and historically correct delta-selected contracts remain unavailable. The stock five-minute P&L must not be presented as validated profitability.

Artifact checks passed: requested caps, entry/exit cutoff timestamps, per-trade gross-minus-fees equals net, summed totals and unchanged production paper ledgers/brain/rules. Each replay uses the existing fee model; costs are not contract-note reconciliation. No experimental exit variant was selected for deployment. Neither run establishes the Rs 15,000 monthly net target.

Source fingerprints, run configuration, daily rows, per-trade records and limitation flags are saved in the existing Nifty 30/60 detail files under `cap8_current_implementation_20261023`, and in `stock_fno_backtest_ledger.json` under `current_implementation_thorough_20261023`. Nifty raw run folders are named `current_20261023_193933_slippage_0.1` and `current_20261023_193933_slippage_0.3`.

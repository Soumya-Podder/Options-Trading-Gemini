# Gemini retrospective filter experiment (October 25, 2026)

Used the actual configured Gemini 2.5 Flash API to review decision-time inputs
for 15 unique deterministic-qualified candidates across Nifty/Sensex and 30/60
calendar days. Candidate collection forced HOLD with fresh learning, producing
a superset before subsequent loss/learning/cooldown gates. Three batches of five
were reviewed at least 30 seconds apart. All 15 responses were valid and frozen
before replay; all reached replay candidates had a stored review. No exit prices,
subsequent bars, realized P&L or production learning were included in prompts.

Approval required the same direction and strike, a self-rated score >=8/10 and
expected premium move >=20 Nifty / >=45 Sensex points, matching the launcher.
Expiry was explicitly unverified, not falsely asserted to be five days away;
historical quotes, quantities, Greeks and full chain were labelled missing.
The batch study is not the exact live single-candidate prompt/latency process.

| Window | Index | Bot-only validated net | Bot-only closed trades | Gemini-filtered net | Gemini-filtered closed trades |
| --- | --- | ---: | ---: | ---: | ---: |
| 30 days Sep 25–Oct 24 | NIFTY | +2,406.00 | 2 | 0.00 | 0 |
| 30 days Sep 25–Oct 24 | SENSEX | 0.00, unresolved path | 0 | 0.00 | 0 |
| 60 days Aug 26–Oct 24 | NIFTY | +3,138.25 | 4 | -581.25 | 1 |
| 60 days Aug 26–Oct 24 | SENSEX | -537.00, incomplete | 4 | 0.00 | 0 |

Amounts are rupees after modeled charges. Both baseline and filtered runs use
entries before 14:30 IST and 14:45 exit, Rs.30,000 fresh capital/learning per
index and window. This differs from prior 14:44-entry baseline figures. Only
actually executed replay outcomes train; rejecting earlier trades can change
later eligible entries, so this is not merely subtraction of old losing trades.

Gemini gave only one approval: Nifty 24250 PE at 2026-08-26 12:05, score 8/10.
Entry 92.00, modeled stop exit 84.05, gross -516.75, charges 64.50, net -581.25.
The stop is inside the same five-minute candle, not a known exact intrabar
instant. Nifty's other ratings were 6–7; Sensex ratings were 3 or 7. An 8/10
model rating did not establish an 80% success probability. The Gemini samples
have no winning trades, so average net win is unavailable. The monthly target
is not met and this experiment does not demonstrate profitable Gemini gating.

The bot-only Sensex results exclude one unresolved path in 30 days and two in
60 days. Nifty has 39/45 unavailable structure comparisons with the earlier
cutoff. No Gemini-approved outcome is unresolved, but full historical strategy
inputs remain incomplete. Current-model retrospective review cannot reconstruct
Gemini decisions as they would have occurred historically or rule out model
hindsight. Batch review and historical fills omit live Gemini latency/quote
freshness. These are conditional research results, not a validated live forecast.

The existing combined ledger retains `gemini_retrospective_protocol`,
`gemini_retrospective_candidates`, `gemini_retrospective_reviews`,
`gemini_retrospective_review_batches` and `gemini_retrospective_evaluations`.
All contexts, complete reviews, ratings, reasons, request hashes and outcomes
are archived. Production state/credentials were verified unchanged. No live
orders, session restart, new source file or new report directory was used.

# Latest structural-only correction (October 25, 2026)

Removed accumulation execution and the 15m premium-edge boost. Nifty now
requires confirmed LEVEL_V1 structure, same-strike confirmation, verified OI
reference and actual aligned imbalance of at least 50 lakh. Sensex behavior
is preserved. Fresh isolated chronological learning at Rs.30,000 per run:

| Window | Trades | Wins/losses | Gross | Charges | Net | Average net win | Average net loss | Net win/loss value | Closed-trade drawdown |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 days Sep 25–Oct 24 | 3 | 3/0 | 3,266.25 | 193.50 | 3,072.75 | 1,024.25 | N/A | N/A | 0.00 |
| 60 days Aug 26–Oct 24 | 5 | 4/1 | 4,127.50 | 322.50 | 3,805.00 | 1,068.94 | 470.75 | 2.271 | 470.75 |

Amounts are rupees. This improves on the loose accumulation nets of -8,111.50
and -11,608.00, but does not reproduce the earlier observer's +3,499.50 30-day
result. Complete September in the 60-day run nets +1,869.75. Rs.15,000 monthly
net is not achieved. The 60-day net win/loss value ratio 2.271 remains below 2.3.
Three and five trades are far too few to validate a stable 80–100% win rate.
Zero closed-trade drawdown in the 30-day subset is not a claim of zero market
risk or intratrade drawdown.

No trade path is unresolved in these two evaluations, but the replay remains
INCOMPLETE_MARKET_STRUCTURE (42/48 unavailable comparisons), with incomplete
full historical chain/expiry/Greeks/depth and intrabar-order information.
These are small in-sample results. Production learning/trade files and engine
source were verified unchanged during replay. No new file/folder was created.

All trades, rejection counts and configuration are retained in existing ledger
fields `strict_structure_only_evaluations` and
`strict_structure_only_configuration`. 122 focused tests pass; earlier full-
repository stock/research/session-maintenance failures remain outside this
change. Earlier experiment results below are historical comparisons only.

# Latest daily trend accumulation replay (October 25, 2026)

Applied the requested daily trend detector, ready-structure pass-through and
15m/5m effective premium impulse. Actual timestamp references replace the
attachment's three-poll approximation; missing wall room rejects instead of
inventing 50 points. Fresh isolated learning and Rs.30,000 per replay:

| Window | Closed trades | Days with closed trades / observed sessions | Wins/losses | Gross | Net | Average net win | Average net loss | Closed-trade drawdown |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 30 days Sep 25–Oct 24 | 27 | 17/19 | 6/21 | -6,370.00 | -8,111.50 | 921.33 | 649.50 | 9,043.00 |
| 60 days Aug 26–Oct 24 | 41 | 30/40 | 11/30 | -8,963.50 | -11,608.00 | 793.20 | 677.77 | 12,539.50 |

Amounts are rupees. Previous observer nets were +3,499.50 / +1,354.25. Frequency
increased but net performance deteriorated sharply. Of the 30-day entries, 25
were accumulation trades (5 wins, 20 losses, -7,368.25 net); two were level trades
(1 win, 1 loss, -743.25). Of the 60-day entries, 37 were accumulation trades
(9 wins, 28 losses, -11,597 net); four were level trades (2 wins, 2 losses, -11).
The complete September month in the 60-day evaluation nets -2,073.00. Neither
Rs.1,000 average net win nor Rs.15,000 monthly net is demonstrated.

Observed sessions are dates actually present in cached ATM option history,
not a claim of complete provider/exchange session coverage. Structural input
comparisons remain unavailable for 42/48 checked snapshots. No attempted trade
has an unresolved outcome in these two replays, but historical full chain,
verified expiry/Greeks/depth and exact intrabar order remain limitations. These
are in-sample tests. Existing OI-weighted-price and changing-basket cumulative
OI proxies must not be described as actual volume VWAP or verified identical-
contract accumulation from 09:15.

121 focused tests passed; full-suite failures previously reported remain
unresolved. All trades, accepted model counts, rejections, attribution, observed
session counts and configuration are in existing ledger fields
`daily_trend_accumulation_evaluations` and
`daily_trend_accumulation_configuration`. Prior baselines are retained. Both
production learning/trade files and engine source were verified unchanged
during replay. The requested code changes remain applied; no live session was
started or restarted.

# Latest observer / twenty-point replay (October 25, 2026)

Executed the supplied observer patch without creating a new script, then added
live/replay hard-target parity and bounded fallback to warming/unavailable
structure. Fresh isolated chronological learning, Rs.30,000 initial capital:

| Window | Nifty trades | Wins/losses | Gross | Net | Average net win | Average net loss | Net win/loss value | Closed-trade drawdown |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 days Sep 25–Oct 24 | 5 | 4/1 | 3,822.00 | 3,499.50 | 1,068.94 | 776.25 | 1.377 | 776.25 |
| 60 days Aug 26–Oct 24 | 11 | 5/6 | 2,063.75 | 1,354.25 | 1,095.75 | 687.42 | 1.594 | 2,254.75 |

Amounts are rupees. Prior Nifty six-patch nets were +2,111.75 / +4,544.25.
The observer improves the recent 30-day sample but worsens the 60-day result
and drawdown. Complete September in the 60-day run nets +704.00. Average net
wins exceed 1,000 in both samples, but Rs.15,000 monthly net and the earlier
2.3x win/loss value target remain unmet. No new Sensex or stock backtest was
requested or run in this observer evaluation.

Nifty still has 42/48 unavailable structure comparisons. Full historical
chain/expiry/Greek/depth limitations remain, along with first-observed session
anchors, an OI-weighted price proxy rather than volume VWAP, and rolling-basket
cumulative OI rather than matched morning-contract accumulation. These are
small in-sample replays, not validated future-profit forecasts.

All trades, rejections and configuration are in the existing combined ledger,
under `observer_20pt_evaluations` and `observer_20pt_configuration`. Production
learning and paper ledgers were verified unchanged during replay. Both
requested Nifty windows were rerun; earlier comparisons remain retained.

# Latest six-patch results (October 25, 2026)

Applied all six requested patches. Structure/exit/daily-target patches were
first tested on 30 days (Nifty net 2,111.75; Sensex unresolved), followed by
learning, session context and Sensex parity changes. Final fresh-learning
results at Rs.30,000 per index/period:

| Window | Index | Closed trades | Wins/losses | Gross | Net | Average net win | Average net loss | Win/loss value ratio |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 30 days Sep 25–Oct 24 | NIFTY | 5 | 3/2 | 2,434.25 | 2,111.75 | 1,191.08 | 730.75 | 1.630 |
| 30 days Sep 25–Oct 24 | SENSEX | 0 validated | N/A | N/A | Unresolved | N/A | N/A | N/A |
| 60 days Aug 26–Oct 24 | NIFTY | 8 | 5/3 | 5,060.25 | 4,544.25 | 1,295.30 | 644.08 | 2.011 |
| 60 days Aug 26–Oct 24 | SENSEX | 4 | 2/2 | -642.00 | -900.00 | 72.50 | 522.50 | 0.139 |

All amounts are rupees. Combined validated 60-day net: 3,644.25. Combined
complete September net within that run: 3,287.50. Maximum closed-trade drawdown
is 776.25 for Nifty and 900 for Sensex. The preceding revision had combined
60-day net 3,939.75: these patches did not improve combined results. Nifty's
30-day net also declined from 2,296.50 to 2,111.75. Neither 2.3x net win/loss
value ratio nor Rs.15,000 monthly net was demonstrated. These are small,
in-sample comparisons, not an independent holdout or profitability guarantee.

Nifty retains 42/48 unavailable structure comparisons; Sensex has unresolved
contract paths. Full historical chain, verified expiry, Greek and depth inputs
remain incomplete. No unresolved outcomes are counted as realized profit.
Production learning and paper ledgers were verified unchanged. Existing result
ledger fields `patches_1_2_5_intermediate_evaluations`,
`latest_six_patch_evaluations` and `latest_six_patch_configuration` retain trades,
rejections, intermediate comparisons and the exact replay configuration.

# Latest entry/exit experiment: October 25, 2026

Applied v4.9.2 quality gates and replay/live exit alignment. Final attachment
specification used RR 1.60 for breakout/rejection, 3.20 for retests, 3.50 after
a loss, structure room 40, OI room 35, observed impulse 0.95R, ratchet arming at
1.80R/2.50R, and stagnation exit at 15 minutes below 0.45R peak advance.

| Window | Index | Closed trades | Wins/losses | Gross | Net | Average net win | Closed-trade drawdown |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |
| 30 days, Sep 25–Oct 24 | NIFTY | 4 | 3/1 | 2,554.50 | 2,296.50 | 1,024.25 | 776.25 |
| 30 days, Sep 25–Oct 24 | SENSEX | 0 validated | N/A | N/A | Unresolved | N/A | N/A |
| 60 days, Aug 26–Oct 24 | NIFTY | 7 | 5/2 | 4,858.75 | 4,407.25 | 1,130.85 | 776.25 |
| 60 days, Aug 26–Oct 24 | SENSEX | 1 | 0/1 | -403.00 | -467.50 | N/A | 467.50 |

All amounts are rupees. Combined validated 60-day net is 3,939.75. Complete
September within that window nets 2,780.75 combined. The Rs.15,000 monthly
objective is not met. Compared with the preceding observed-impulse 0.80R
baseline, Nifty 30-day net changed from -110.50 to +2,296.50 and 60-day net from
+1,777.00 to +4,407.25; Sensex 60-day net worsened from -184.00 to -467.50.

These are fresh-learning runs at Rs.30,000 per index/period using the same data
as prior evaluation; they are not an untouched holdout. Nifty still has 42/48
unavailable structure comparisons, and Sensex still has unresolved contract
paths. Historical full-chain/expiry/Greek/depth limitations remain. Small
samples and in-sample parameter selection preclude a live-profit conclusion.

All trades, rejection counts, configuration and engine hash are in the existing
`dhan_hist_cache/index_backtest_20261025_184711_943953/paper_trade_ledger.json`,
under `entry_exit_experiment_evaluations`. Baselines were retained. Production
learning and paper ledgers were verified unchanged during the replay.

# Nifty research: Rs.10,000 gross per 10 trading days

Status on October 23, 2026: target not achieved. No research candidate has been promoted to the running trading configuration.

The isolated runner `nifty_research.py` downloads actual Dhan rolling option OHLC/OI/IV/strike/spot data for ATM through ATM +/-3 on both sides. It follows the selected strike across relative series within the same session and weekly expiry selection. Missing observations remain missing; no prices are estimated from spot or a different option strike. Conflicting duplicate observations fail validation. The final execution studies use actual one-minute option OHLC, next-bar entry, modeled adverse 0.1% slippage, worse gap-stop prices, and stop-first treatment of unresolved intrabar order.

Data covers 62 verified index sessions from July 17 through October 14, 2026. All corresponding five-minute signal sessions were complete. Some one-minute option histories begin after 09:15; missing observations before the 09:25 entry window are skipped, while missing eligible signal observations invalidate the session. Neither missing bars nor unvalidated outcomes are treated as wins. Zero-trade sessions count toward each 10-session block. Two remaining sessions are reported separately rather than represented as a full block.

Each research wave declares candidates and selects using only its first 20 sessions. The selection ranks complete replay first, then the number of Rs.10,000 blocks, worst training block, and total gross. Settings are written to disk before later sessions are evaluated. Later dates have now been examined in multiple research waves and must not be described as a new untouched holdout for subsequent optimization. A future successful candidate would need additional independent validation.

Twenty-four configurations were tested across the initial waves: previous entries and rollover guards with 6.15/10/15-point stops; EMA/body confirmation; fixed one-lot 20-point exits; opening-range breakouts/retests; failed-break/rejection entries; optional OI alignment; and one-minute signal updates over a five-minute reference. A twenty-fifth control then used the corrected same-contract OI calculation. Static controls disable permanent rule pruning and keep one lot. Adaptive controls retain chronological online learning. The existing maximum lot-sizing logic was not multiplied to achieve the profit target. A research-only initial gross stop-risk ceiling of Rs.1,000 was applied; market gaps can exceed that estimate.

## Latest earlier-entry study

Training selected the one-minute signal update with the existing 6.15-point stop and adaptive rule selection. This research does not include the subsequently added full-chain, same-contract live OI tracker. Historical signal OI still uses the available rolling-strike subset and the analyzer's rollover guard. The historical and live selection baskets therefore need further reconciliation before deployment.

| Segment | Dates | Sessions | Gross P&L | Net with existing modeled charges |
|---|---|---:|---:|---:|
| Training 1 | Jul 17â€“Jul 30 | 10 | +692.25 | -1,178.25 |
| Training 2 | Jul 31â€“Aug 13 | 10 | +2,837.25 | +1,353.75 |
| Validation | Aug 14â€“Aug 27 | 10 | +1,072.50 | -153.00 |
| Later test 1 | Aug 28â€“Sep 10 | 10 | -1,784.25 | -2,880.75 |
| Later test 2 | Sep 11â€“Sep 25 | 10 | +4,683.25 | +3,586.75 |
| Later test 3 | Sep 28â€“Oct 12 | 10 | +4,764.50 | +3,345.50 |
| Partial remainder | Oct 13â€“Oct 14 | 2 | +523.25 | +329.75 |

This candidate has zero unvalidated attempted outcomes in this run, but fails the requested gross-profit target in every complete block. Its positive aggregate does not satisfy the user's per-10-trading-day requirement. Parameter sweeps on the same dates cannot establish future consistency.

## Same-contract OI control

The corrected one-minute OI comparison was also replayed with the existing 6.15-point stop and adaptive rule pruning, while bypassing the separate price-structure gate to isolate OI entry behavior. Training gross P&L was -2,437.50 and -2,193.75 in the first two 10-session blocks. All eight learned signatures were subsequently PRUNED and later blocks had zero trades. This exposes the existing permanent-pruning behavior; it does not validate the complete live strategy. No version met the requested profit target. An initial malformed research-chain key format produced an invalid zero-trade run; that run is explicitly marked invalid and superseded by the corrected run.

**The preceding OI-control numbers are superseded.** A later audit found that the research recorder entered rolling ATM even when the OI analyzer selected a different call-above/put-below strike. Earlier OI-control reports are explicitly flagged `replay_contract_selection_superseded`. The runner now trades `analysis.atm_ce_strike` / `analysis.atm_pe_strike`; a regression test checks this distinction. Earlier non-OI controls already selected their rolling ATM contract and are not affected by this particular mismatch.

## Corrected selected-contract and learning/exit comparisons

The new comparisons use the analyzer-selected contract, actual one-minute execution, one lot (65 units), a 6.15-point initial stop, at most four trades per day, the existing two-consecutive-loss breaker and cooldowns. No live settings or learning files were changed. All attempted trade outcomes were resolved from downloaded data in these runs. Historical OI remains limited to the available relative-strike subset.

| Research control | Closed trades | Gross win rate | Gross P&L, 62 sessions | Net with modeled charges |
|---|---:|---:|---:|---:|
| Permanent rule pruning | 38 | 26.32% | -5,648.50 | -8,099.50 |
| Retry pruned rules next day | 137 | 35.04% | -4,793.75 | -13,630.25 |
| Static thresholds with trailing exit | 145 | 34.48% | -2,665.00 | -12,017.50 |
| Support/resistance gate plus next-day retry | 17 | 41.18% | +1,150.50 | +54.00 |
| Static thresholds, single-lot 10-point target | 146 | 34.25% | -7,007.00 | -16,424.00 |
| Static thresholds, single-lot 20-point target | 138 | 25.36% | +812.50 | -8,088.50 |

Static controls reset rule status to CANDIDATE and clear the recent-loss similarity veto; they are not an isolated test of permanent pruning alone. The permanent-pruning and next-day-retry pair retain that veto and differ in next-day rule recovery. Four trades/day and one lot apply to all these new controls, including the baseline. Single-lot target experiments do not change or validate the live multi-lot 10/20-point partial-exit rule.

The support/resistance control averaged Rs.655.57 gross per win and Rs.343.85 per loss (1.91x), but had 48 zero-trade days. Its complete 10-session gross blocks were -1,966.25, +412.75, +412.75, 0.00, +1,816.75 and -591.50. The remaining two sessions contributed +1,066.00. No complete block reached Rs.10,000. Seventeen trades are insufficient evidence of dependable profitability, and this configuration was not selected on training or deployed.

In the static trailing control, 93 of 145 trades exited at the initial stop; 25 of those stopouts occurred within one minute of entry. Permanent pruning eventually left all eight signatures pruned and 46 of 62 days without trades. Removing permanent pruning alone therefore does not solve entry quality, while small fixed targets do not make this entry logic consistently profitable.

The minimum training sample was declared as ten closed trades. Selection used the first twenty sessions only; later controls are descriptive comparisons and do not reselect the training winner. The static trailing control won that training ranking but failed the profit target in training and later periods. All dates have now been examined repeatedly and require independent future validation for any subsequent optimization.

Reports: `dhan_hist_cache/nifty_research_20261023/run_20261023_094008/report.json` (learning/structure controls) and `run_20261023_094228/report.json` (exit controls). Full daily outcomes, ledgers and rule histories are in those run folders.

## Reproduction and files

Run from the project folder:

```text
python -B nifty_research.py download
python -B nifty_research.py download-1m
python -B nifty_research.py evaluate
python -B nifty_research.py evaluate-1m
python -B nifty_research.py evaluate-entries
python -B nifty_research.py evaluate-reversals
python -B nifty_research.py evaluate-early
python -B nifty_research.py evaluate-oi
python -B nifty_research.py evaluate-learning
python -B nifty_research.py evaluate-exits
```

The runner never places orders, and uses independent brain/rule files inside its run folder. Credentials are read from the project `.env` and are not copied into reports. Downloads, manifests, run plans, source hashes, daily outcomes, ledgers and summaries are under `dhan_hist_cache/nifty_research_20261023/`. Each evaluation creates a fresh run directory. Previous analyzer source is recovered read-only from Git commit `2ba4ea8`.

An isolation error was found during review: the shared trade recorder wrote research results into the root paper ledger despite independent brain/rule files. Its output path is now explicit and points to the research run directory. The original tracked ledger was restored, with its prior net wallet balance Rs.30,053.25 and nine historical trades, and the Nifty paper session was restarted. There were no current-day trades or open positions when restored. An integration test now executes an actual research trade and verifies that the root paper ledger remains byte-for-byte unchanged. Production brain/rule files were not replaced by research state.

Limitations include one-minute intrabar uncertainty, simplified modeled charges/slippage, incomplete historical full-chain OI, raw OI units, expiry-series assumptions within each session, and no exchange timestamp on live option-chain OI. These backtests do not establish live fills or consistent future returns.

Official API reference: https://dhanhq.co/docs/v2/expired-options-data/.

## Current implementation audit â€” 23 October 2026

The current-policy replay uses the shared daily net-liquidation helper inside an
open trade, current dynamic lot selection, the existing daily trade/loss limits,
net-outcome learning and timestamp-based cooldowns. It does not apply the older
fixed-lot research cap. The 25-minute time stop uses the peak known before the
first eligible bar opens. Threshold crossings use adverse tick-rounded fills;
an ambiguous stop/target candle takes the existing stop first. Partial targets
and daily liquidation count partial P&L and charges once.

Signals use one-minute observations with already-closed five-minute structure,
not a second duplicate analyzer update. A fresh chronological state starts on
17 July; 30/60-calendar-day evaluations end on 22 October. The declared 0.1% and
0.3% slippage runs are sensitivity cases, not a parameter optimization. Market
inputs are still an ATM +/-3 rolling subset without complete historical chain,
previous-day OI, bid/ask depth or verified expiry identity. Accordingly the output
is classified as partial-chain research even when held-strike execution bars
have no missing observations. See the existing entry-continuation report for
current results and underlying source hashes.


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

Legacy comparison code is loaded from its Git commit in memory. Research plans retain source hashes; new evaluations do not write duplicate engine modules or a source-copy folder.
# October 25 replay repair and current objectives

The native replay now retries legacy empty caches, requests complete OHLC for
ATM through the third required wing, aligns observations by timestamp, and
follows the selected exact strike across relative series. The OI parser retains
put-only strikes when a call is absent. Requests are paced at least 3.2 seconds
apart within this historical client. No synthetic contract prices were used.

Fresh research learning was used independently for each index and period, with
Rs.30,000 starting capital per index. Production learning and paper ledgers were
verified unchanged. Existing output was reused at
`dhan_hist_cache/index_backtest_20261025_184711_943953/paper_trade_ledger.json`;
the `independent_index_evaluations` field contains all four evaluations and trades.

| Calendar-day window ending October 25 exclusive | Index | Closed trades | Net P&L | Average net win | Average net loss | Closed-trade drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 60 days, August 26 onward | NIFTY | 26 | -328.25 | 983.95 | -635.48 | 4,519.00 |
| 60 days, August 26 onward | SENSEX | 2 | -184.00 | 283.50 | -467.50 | 467.50 |
| 30 days, September 25 onward | NIFTY | 14 | 2,441.25 | 1,122.68 | -773.93 | 2,228.50 |
| 30 days, September 25 onward | SENSEX | 0 | Unresolved | N/A | N/A | N/A |

All amounts are rupees after modeled charges. The recent Nifty sample clears
the Rs.1,000 average-net-win objective, but the combined Rs.15,000 monthly net
objective is not met. A fresh 30-day learning run differs from the last half of
a 60-day learning run; these are not interchangeable samples.

These are incomplete research replays: the historical chain is a rolling subset,
previous-day OI is absent, Nifty has unavailable structure comparisons (48 in
60 days; 42 in 30 days), and Sensex has unresolved contract paths. Missing
Sensex outcomes are excluded rather than recorded as zero-profit trades.
The observed P&L cannot establish full live-strategy profitability. The full
calendar month of September in the 60-day run produced -1,783 combined net.

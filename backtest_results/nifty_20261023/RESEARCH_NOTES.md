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
| Training 1 | Jul 17–Jul 30 | 10 | +692.25 | -1,178.25 |
| Training 2 | Jul 31–Aug 13 | 10 | +2,837.25 | +1,353.75 |
| Validation | Aug 14–Aug 27 | 10 | +1,072.50 | -153.00 |
| Later test 1 | Aug 28–Sep 10 | 10 | -1,784.25 | -2,880.75 |
| Later test 2 | Sep 11–Sep 25 | 10 | +4,683.25 | +3,586.75 |
| Later test 3 | Sep 28–Oct 12 | 10 | +4,764.50 | +3,345.50 |
| Partial remainder | Oct 13–Oct 14 | 2 | +523.25 | +329.75 |

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

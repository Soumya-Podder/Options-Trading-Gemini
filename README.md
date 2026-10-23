# Nifty and stock-options bots

Run commands from this project folder. Keep credentials in its single `.env` file. Runtime data, logs, replay results and temporary files belong inside the project.

Daily entry limits are eight for Nifty and two for stock options. These are maximums, not a trading quota. Nifty saves each consumed entry slot before submission and restores the count after a restart; partial exits do not consume another entry. An unresolved submission blocks further entries until reconciled. The existing profit target, consecutive-loss breaker, qualification checks and 09:15–14:45 IST session can stop trading before the entry limit is reached.

## Runtime files

| Files | Purpose |
| --- | --- |
| `dhan_bot_engine.py`, `nifty_market_structure.py`, `nifty_oi.py`, `index_option_replay.py` | Index session, level/candle/strike-OI entry and historical execution replay |
| `dhan_dynamic_fno_stock_bot.py`, `stock_option_signals.py`, `stock_option_policy.py` | Stock-option scan, qualification, execution planning, outcome feedback and daily limits |
| `run_bot_session.py`, `bot_dashboard.py`, `telegram_notifier.py` | Session management, display and configured notifications |
| `.env`, `.gitignore` | Local credentials/settings and exclusions from Git |

Paper Nifty: `python -B dhan_bot_engine.py --indices NIFTY`

Paper stock options: `python -B dhan_dynamic_fno_stock_bot.py`

Session manager: `python -B run_bot_session.py` — reads PAPER/LIVE settings from the environment. Check the configured mode before launching. Broker mode requires the existing explicit live configuration.

## Trading memory and learning

Do not delete these to tidy the project:

- `paper_trade_ledger.json`: index trade outcomes and wallet history.
- `learned_rules.json`: rule wins/losses, expectancy, activity/pruning status and lot-loss memory.
- `bot_brain_state.json`: bounded replay history and updated model weights.
- `stock_fno_paper_ledger.json`: created when the stock session initializes; its closed trades provide stock ranking feedback. Its absence before initialization does not mean the source module is missing.
- The stock daily-gate JSON files under the configured cache retain consumed entries and recoverable paper positions.
- `dhan_hist_cache/`: downloaded historical bars, session records and isolated research runs.
- `backtest_results/` and `stock_fno_backtest_ledger.json`: past evaluation evidence, separate from paper learning.

Nifty closed trades now retain both gross R and `learning_r_multiple`, calculated from net P&L after modeled charges and the original risk budget. The net reward updates rule statistics, pruning and existing lot-sizing controls. Loss streaks and post-loss cooldowns also use net outcomes, including after a restart. Substantial losing-state memory is reconstructed from the current day's saved replay history on restart. Repeated initialization during the same day cannot erase it. Each update emits a `[LEARNING]` log entry.

Rule statistics and the recent-loss veto affect actual decisions. The stored linear-model weights are updated, but are not an independent entry predictor or a guarantee that a trade has an advantage. Older saved outcomes have their original reward basis; they have not been relabeled or rewritten as new net-reward training.

Stock-option feedback is derived from closed trades for the same symbol, scanner and option side. It reads the latest 20 usable outcomes known at decision time. At least three outcomes are needed to adjust quality by at most ten points. At least five outcomes, negative average net R and three consecutive recent net losses pause that setup for five calendar days from the latest close. The setup can be reconsidered afterward. Position size and quote/risk filters are not relaxed by this feedback. New stock closes store `learning_risk_rs` and net R; older records without a verifiable risk budget are excluded rather than assigned invented risk.

Paper fills and historical fills are modeled, and charges are estimates. Stock broker brackets currently require fill reconciliation in Dhan; an order submission alone is not treated as a closed, profitable trade. There is no automatic training on unverified live stock exits.

## Replay isolation and verification

The index backtest creates a fresh `dhan_hist_cache/index_backtest_<timestamp>/` folder containing its ledger, brain, rules and telemetry. It does not overwrite the running paper bot's trading memory. `nifty_research.py` also uses isolated runs; it remains necessary for the requested 30/60-day evaluations. Historical report results predate the net-reward and level-first changes unless explicitly labeled otherwise.

Retain the `test_*.py` regression checks. In PowerShell, keep test temporary files in the project:

```powershell
$env:TEMP = Join-Path $PWD 'dhan_hist_cache\test_tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
python -B -m unittest discover -q
```

See `NIFTY_LEVEL_ENTRY.md` for current entry rules, `BACKTEST_OBJECTIVES.md` for the current monthly objective, and `NIFTY_RESEARCH.md` for the historical evaluations. The cleanup manifest records removed disposable paths without copying credential contents.

## Missed opportunities

Closed-trade learning does not automatically learn from trades never entered. Entry review records and direction-specific rejection counts are kept separate from realized P&L and training. The nearby-strike expansion remains disabled after an adverse 30-day comparison. See `dhan_hist_cache/nifty_opportunity_review_20261023/` for the declared plan and isolated 30/60-day comparisons; do not use that limited historical-chain experiment as a full-chain live profit forecast.

## Active stock-options limits

The current requested profile uses `STOCK_OPT_RUPEE_TARGET=900`, `STOCK_OPT_MIN_NET_RR=1.3`, `STOCK_OPT_MAX_SPREAD_PCT=3.5`, and `STOCK_OPT_CONFIRM_WINDOW_SEC=600`. The existing `STOCK_OPT_RUPEE_MAX_RISK=1000` and `STOCK_OPT_MAX_LOTS=3` remain unchanged. Strong movers (absolute confirmed day change at least 6%) use the documented spread, IV-range and expected-gain exceptions in `STOCK_DATA_HANDLING.md`. The daily entry cap remains two. These are estimated net target/risk constraints, not guaranteed fills or results. The session launcher explicitly selects Nifty and starts stock options separately; either engine finishing normally no longer shuts down the other.

Nifty selection now annotates `entry_decision` with the action and a specific final-selection reason. Entry review records are written after selection and include the current position, daily counts, loss streak, realized net and cooldown context. These diagnostics do not bypass risk or learning gates.

## Current PAPER continuation profile

The non-secret `.env.example` records the selected settings. Nifty and stock options can recognize a fresh, confirmed opening-gap break or a later fresh consolidation break. Stock options can plan a cash target beyond the percentage reference, with fees, slippage and the same cash-risk cap; sizing also uses the broker-Greek move estimate. After a net stock loss, the enabled re-entry path requires a new confirmation after 15 minutes, available daily capacity and full requalification. The total daily stock cap remains two. The live history circuit can probe for recovery after 60 seconds. Direct commands and the launcher anchor their working directory to this project.

These changes are for PAPER observation; a setup can still stop out. `backtest_results/entry_continuation_20261023/REPORT.md` includes the replay results and a Swiggy entry that loses despite the subsequent premium rise. The broader Nifty variants that performed worse remain disabled. Morning auto-start is not yet scheduled.

## Project cleanup

All root Python modules remain required for execution, session management, notifications, replay, or regression testing. Redundant archived source copies and a duplicate research-notes document were removed; Git commit `95d6213` retains the original snapshot. Trading ledgers, learning state, cached market bars, entry audits, research results, and session logs are retained for recovery and analysis. See `cleanup_manifest.json` for the removal record.

Maintenance rule: update existing modules and documentation first. Create a new file only when a necessary responsibility cannot reasonably fit the existing structure. Avoid alternate implementation copies and one-off report/source snapshots; retain only necessary runtime state, reproducible evaluation evidence, and regression checks.

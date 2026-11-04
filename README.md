# Nifty and stock-options bots

The combined Nifty/Sensex daily net-profit target is Rs.2,500. The index engine
checks realized daily net plus the estimated net liquidation value of an open
position, including partial fills and modeled charges. At the threshold it exits
and stops new index entries for that day. Individual trade stops, profit targets,
maximum holds and the 14:45 session cutoff remain independent exit conditions.

Index daily profit protection arms at Rs.1,000 net, including realized P&L and
partial fills plus the estimated liquidation value of the remaining position.
Once armed, no further index entries are allowed that day. A flat session with
at least Rs.1,000 realized net also stops. An open winner can continue toward
Rs.2,500, but its stop cannot fall below the fee- and 0.1%-exit-slippage-budgeted
daily floor. The lock is persisted across restarts and resets by trading date;
even a gap below its intended floor leaves the session locked against re-entry.
Stops are intentions: gaps, stale quotes and actual execution can give back more.
Replay arms protection for following candles only and preserves worse gap fills.

After the Rs.1,000 index profit lock arms, the live engine reassesses whether
the remaining position can support a Rs.3,000 daily net scenario. It requires
fresh quotes and full remaining-quantity depth, acceptable spread, supportive
matched held-strike OI/premium changes, directional five-minute impulse, mapped
opposing room, and at least five minutes left inside the twenty-minute hold and
session cutoff. A delta-scaled move bounded by both room and observed impulse,
less remaining theta and modeled exit slippage, must cover 120% of the additional
premium gain needed. This heuristic is not a probability or guaranteed forecast.
Passing allows a runner past the usual profit target and Rs.2,500 daily target,
up to Rs.3,000 net; it never lowers the existing stop or removes the Rs.1,000
floor. Runner trailing uses one initial premium-risk distance behind the peak,
subject to the already raised stop and floor. Failing the reassessment books
the available profit. Quote, time, stop and cutoff exits retain priority. Partial
booking is retained and the scenario is recalculated for the remaining quantity.
Runner hold/exit reasons and required/scenario gains are logged and persisted.
Historical OHLC-only exit replays cannot reproduce this dynamic overlay; their
limitations explicitly disclose that missing decision-time evidence.

Run commands from this project folder. Keep credentials in its single `.env` file. Runtime data, logs, replay results and temporary files belong inside the project.

Daily entry limits are eight for the index session and four for stock options, with at most two simultaneous stock positions. These are maximums, not a trading quota. Nifty saves each consumed entry slot before submission and restores the count after a restart; partial exits do not consume another entry. An unresolved submission blocks further entries until reconciled. The existing profit target, consecutive-loss breaker, qualification checks and 09:15–14:45 IST session can stop trading before the entry limit is reached.

Nifty uses LEVEL_CANDLE_AND_STRIKE_OI_V2 for live-session and historical entry checks.
A strong closed first break can qualify without an additional five-minute hold.
Pullbacks are bounded by 0.35 recent ATR and must retain the trigger; chasing is
limited to 0.25 ATR beyond the confirmation close. Three-bar consolidations can
supply pre-existing levels, with confirmation from the nearest identical strike
within 25 points. Matched OI/premium change must be meaningful (at least 100,000
OI units and 1% of the reference OI); the unrelated 50-lakh basket gate is a
diagnostic rather than an additional veto for this profile. Structure room/RR,
opposing-wall room, quote quality, learning and loss limits still apply. Structure
risk is measured to the local trigger/invalidation, separately from the option
premium stop. V2 budgets twenty-minute theta decay using a 375-minute trading
day, capped at 30% of premium stop risk, and enforces a twenty-minute maximum
hold in both execution and replay. Existing expiry/time restrictions still apply.
The one-minute opening-reclaim detector remains research-only; V2 retains the
closed five-minute structure warmup. Saved-snapshot replay found 09:55 CE and
10:40 PE candidates on October 26 with a fresh isolated research brain; that is
not a full chronological fill/exit backtest or a profitability claim.

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

See `NIFTY_LEVEL_ENTRY.md` for current entry rules, `BACKTEST_OBJECTIVES.md` for the current monthly objective, and `NIFTY_RESEARCH.md` for the historical evaluations. Cleanup history is consolidated in the existing combined research ledger, without copying credential contents.

## Missed opportunities

Closed-trade learning does not automatically learn from trades never entered. Entry review records and direction-specific rejection counts are kept separate from realized P&L and training. The nearby-strike expansion remains disabled after an adverse 30-day comparison. See `dhan_hist_cache/nifty_opportunity_review_20261023/` for the declared plan and isolated 30/60-day comparisons; do not use that limited historical-chain experiment as a full-chain live profit forecast.

## Active stock-options limits

The current local profile uses `STOCK_OPT_RUPEE_TARGET=1250`, `STOCK_OPT_MIN_NET_RR=1.4`, `STOCK_OPT_MAX_SPREAD_PCT=2.5`, and `STOCK_OPT_CONFIRM_WINDOW_SEC=600`. It uses a 7% premium stop, 14% target reference, 0.05% modeled slippage, `STOCK_OPT_RUPEE_MAX_RISK=1600`, and `STOCK_OPT_MAX_LOTS=2`. Maximum hold is 80 minutes; no-progress checking starts after 40 minutes. The initial-capital setting is Rs.50,000; existing ledgers are preserved. Strong movers (absolute confirmed day change at least 6%) use the documented spread, IV-range and expected-gain exceptions in `STOCK_DATA_HANDLING.md`. The daily entry cap remains two. These are estimated net target/risk constraints, not guaranteed fills or results. The session launcher explicitly selects Nifty and starts stock options separately; either engine finishing normally no longer shuts down the other.

Nifty selection now annotates `entry_decision` with the action and a specific final-selection reason. Entry review records are written after selection and include the current position, daily counts, loss streak, realized net and cooldown context. These diagnostics do not bypass risk or learning gates.

## Current PAPER continuation profile

The non-secret `.env.example` records the selected settings. Nifty and stock options can recognize a fresh, confirmed opening-gap break or a later fresh consolidation break. Stock options can plan a cash target beyond the percentage reference, with fees, slippage and the same cash-risk cap; sizing also uses the broker-Greek move estimate. After a net stock loss, the enabled re-entry path requires a new confirmation after 15 minutes, available daily capacity and full requalification. The total daily stock cap remains two. The live history circuit can probe for recovery after 60 seconds. Direct commands and the launcher anchor their working directory to this project.

These changes are for PAPER observation; a setup can still stop out. `backtest_results/entry_continuation_20261023/REPORT.md` includes the replay results and a Swiggy entry that loses despite the subsequent premium rise. The broader Nifty variants that performed worse remain disabled. Morning auto-start is not yet scheduled.

## Project cleanup

Core Python modules support the engines, shared analysis and execution. The dashboard and launchers are required only for those features; nifty_research.py and test_*.py support research and regression validation rather than live entry execution. Redundant archived source copies and a duplicate research-notes document were removed; Git commit `95d6213` retains the original snapshot. Trading ledgers, learning state, cached market bars, entry audits, research results, and session logs are retained for recovery and analysis. Cleanup history and unused empty wallet templates were consolidated into `dhan_hist_cache/index_backtest_20261025_184711_943953/paper_trade_ledger.json`, under `consolidated_unused_root_records`; their redundant root copies were removed.

Maintenance rule: update existing modules and documentation first. Create a new file only when a necessary responsibility cannot reasonably fit the existing structure. Avoid alternate implementation copies and one-off report/source snapshots; retain only necessary runtime state, reproducible evaluation evidence, and regression checks.

Nifty now freezes its initial stop distance at 9.5% of entry premium, clamped to 7.50-12.50 points. Sensex uses 9.5% clamped to 20-38 points. Single-lot ratchet protection starts at 1.80R (+0.25R stop); from 2.20R it locks at least +1.60R and trails 0.65R behind the peak. Multi-lot +10/+20-point exits remain in place. Replay and net learning use the entry risk. Existing historical reports predate this profile and do not validate it.

## Optional Gemini candidate review

The explicitly requested `dhan_gemini_options_bot.py` is a small PAPER-default
launcher around the existing index engine, not a second broker/execution engine.
Set the real `GEMINI_API_KEY` locally in the existing `.env`. Keep one populated
key entry: a later empty duplicate would mask it in the environment loader. `GEMINI_MODEL` defaults to the requested `gemini-2.5-flash` and can
be overridden with `--model`. Google restricts 2.5 access for new users; see
https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash . No key is logged,
sent in a URL or included in market prompts. This uses the documented Gemini
REST API through existing requests, so no Google SDK dependency is required.

Run with the installed Python, from this project:

```cmd
"C:\Users\soumy\AppData\Local\Python\pythoncore-3.14-64\python.exe" -B dhan_gemini_options_bot.py --indices NIFTY
```

Use `--indices NIFTY,SENSEX` to review both indices. PAPER is the default; the
existing LIVE path is exposed only with an explicit `--live`. The existing
launcher/scheduled sessions are not switched to Gemini automatically. Do not
run this and another index session against the same shared ledger concurrently.

Only candidates that pass the engine's deterministic entry checks are sent to
Gemini. JSON approvals require the same direction and strike, an integer rating
of at least 8/10, and an expected move of at least 20 Nifty or 45 Sensex premium
points. These are subjective model review objectives, not calibrated success
probabilities or changes to the existing risk, size and exit policies. In
particular this does not add a new hard 45-point Sensex exit or replace stops
with the prototype's fixed values. Existing daily-profit, trail and time exits
can close earlier. Gemini can veto an entry, but cannot create one or change
its contract, size or stop.

Reviews are at most once per 30 seconds across selected indices. Dhan polling
and position monitoring retain their existing pacing. Only flat entry candidates
can wait for Gemini; open-position exits never call it. Errors, incomplete JSON,
wrong contracts, insufficient rating, stale replies, old market snapshots or a
14:30 IST entry cutoff crossed during review all yield HOLD. No approval is
reused for a later snapshot. HTTP request timeouts and an eight-second response/
market-age check limit stale decisions. Mandatory 14:45 exits remain in force.
Review evidence is attached to existing telemetry and the entry's market-
structure record. The provider receives whitelisted market context only, not
Dhan credentials, account balances, brain state or the complete environment.

An authenticated synthetic connectivity test on October 25 returned HTTP 200,
completed normally (STOP) in 1.31 seconds, and the reviewer held the entry. No
orders or real-market trading session were started. Gemini 2.5 Flash reviews
use thinkingBudget=0 to avoid the optional thinking delay within the short
entry freshness window. The real key was preserved locally and never printed.
Existing historical backtests do not validate Gemini review: future evaluation
requires archived decision-time inputs and responses. The launcher deliberately
does not offer a historical Gemini replay that would use current model knowledge
to label past trades. Tests are in the existing test_learning_feedback.py.


### Execution audit corrections (26 October 2026)

The launcher and both engines currently support PAPER execution only. LIVE startup
fails before submitting orders: broker order acceptance is not a confirmed fill,
and durable fill/position/protective-stop reconciliation has not been implemented.
Do not enable LIVE by bypassing this guard.

Index PAPER buys use an executable ask with 0.1% adverse slippage; sells use the
executable bid with 0.1% adverse slippage and the existing estimated fee model.
Missing depth, stale entry quotes and insufficient cash reject entry. Nifty and
Sensex maintain separate OI histories on their own strike grids. Cross-process
option-chain pacing applies to both engines. Windows session mutexes prevent
multiple launchers or multiple copies of either engine from sharing a ledger.
The launcher makes at most two PAPER recovery attempts after a nonzero exit;
reservations remain consumed and ambiguous state requires reconciliation.

Stock risk checks use executed outcomes from the durable daily gate, including
trades excluded from the displayed P&L. The consecutive net-loss limit now applies.
`STOCK_OPT_DAILY_LOSS_LIMIT` defaults to Rs.3,000 and includes reserved open-position
risk before new entries; it is an estimated budget, not a guaranteed loss ceiling
through gaps. Per-lot limits and daily entry limits still apply. Reporting exclusions
remain unchanged. The legacy fitted index weights are diagnostic only; empirical
rule outcomes control pauses and sizing. Diagnostic updates are normalized and
bounded; they are not a calibrated probability model.

Stock OHLC replay rejects missing bars and changed rolling strikes, uses gap-open
fills and stop-first intrabar ordering, and closes at 14:45. Incomplete historical
paths are not synthetic profits. Historical Greeks, depth, full chain selection,
latest sizing and dynamic runner overlays remain outside this price-path replay.
A passing test suite establishes code behavior, not profitable strategy performance.
Market requests are synchronous: polling is targeted at 3�5 seconds but network
latency and shared pacing can exceed this. PAPER positions remain persisted if
quotes become unavailable; never interpret process shutdown as proof of exit.


Backtest verification follow-up: index reports now count missing historical quote
snapshots and entry rejection reasons, mark these runs
`INCOMPLETE_DECISION_INPUTS`, and return exit code 2 from the CLI for an incomplete
run. Index replay starts with Rs.40,000 and fresh learning state on every run,
including when `--backtest-output-dir` reuses an existing research directory.
Stock replay now applies the current daily risk breakers without using future
closed outcomes and blocks a session after an unresolved trade path.

Public Telegram calls (PAPER only)

TELEGRAM_CALLS_PAPER=true enables the public @tradewith_sac feed inside the index
worker. No personal Telegram login, OTP, subscription API or extra worker is used.
The background reader polls every ten seconds; network failure clears candidates.
Only explicit NIFTY/SENSEX BUY ABOVE strike/CE/PE alerts are parsed. Marketing,
activation-only posts, instructions embedded in posts and stock-symbol alerts do
not place trades. Calls expire once older than five minutes, future/previous-day alerts
are ignored, reposts use the earliest alert timestamp, and entry keys are consumed
atomically with the existing durable index reservation to prevent re-entry.
The executable bid must reach the trigger and the slippage-adjusted ask must be
within 2% above it. Quotes must be at most eight seconds old. Per user instruction,
channel calls use the current-expiry chain already loaded by the index worker;
expiry text in posts does not select upcoming contracts or add a signal veto.
The actual expiry is stored as CURRENT_CHAIN. An unavailable current contract
cannot be executed, and there is no switch to next expiry to force a fill.
Native direction, closed-candle/OI, theta, spread/depth, learning pauses, daily
caps, cooldowns, wallet and profit protections remain mandatory. This may reject
opening alerts before native confirmation is ready. Stops and targets are the
bot's own; public posts do not provide the premium group's complete risk plan.
After native qualification, channel candidates may use up to three funded lots,
within 60% wallet allocation and displayed bid/ask quantities. Loss-feedback
sizing remains reduced. Source and expiry basis are stored in trade structure;
notifications identify the channel. Normal native trades can still occur.
This feed is not reconstructed in historical strategy backtests; retrospective
channel profits must not be presented as actual fills or validated returns.

The selected local stock PAPER profile now enables
STOCK_OPT_REQUIRE_CONFIRMED_BREAKOUT=true. Filtered stocks are candidates; only a
strong closed breakout beyond prior-day and current-session levels, supported by
volume, EMA and VWAP, can proceed to option qualification. This profile replaces
the cumulative-full-day-volume admission condition with candle-volume evidence
so an early genuine breakout need not wait until noon. It uses closed five-minute
confirmation, not the screenshot's unverified intraminute listing time. Full
rules and limits are documented in STOCK_DATA_HANDLING.md. No retrospective
profits are credited and existing daily breakers remain consumed.

Current expiry is mandatory for index and stock option entries. The stock bot's
former four-day minimum no longer selects next-month contracts; expiry-day stock
contracts can qualify. An unsuitable current-expiry contract is skipped rather
than replaced with an upcoming expiry. Existing risk and executable-price checks
remain in force, and current-expiry selection does not authorize a larger loss.

STOCK_OPT_STAGED_PROFIT=true enables whole-lot partial booking for qualified
multi-lot PAPER positions, followed by a protected runner while fresh underlying
price/volume and contract OI support it. The combined upper profit cap remains
Rs.5,000 and cash-loss exit Rs.1,500. Only booked, confirmed runners can extend
holding up to 90 minutes, with all positions subject to the 14:45 cutoff. One
partial is durably recorded in the existing position; final fills are aggregated
into one trade and no profit is credited twice. Historical base-exit OHLC replays
do not validate this live runner. See STOCK_DATA_HANDLING.md for full details.

The stock worker compares up to three qualified setups within a soft eight-second
window before selecting an entry. It no longer ranks each candidate in isolation.
Confirmed breakout quality, volume, execution room, planned RR and bounded closed
net-learning feedback drive priority; the largest daily mover is not automatically
selected. Every chosen entry gets a fresh final recheck. Comparison reasons are
stored in the existing trade diagnostics, with no extra selection report files.


Channel PAPER execution update (28 October 2026): fresh @tradewith_sac index calls use channel direction without native directional confirmation. Nifty premium SL is 10 points; Sensex premium SL is 20 points. Stops are triggers, not guaranteed fills. Five-minute freshness, current expiry, executable trigger/depth/cash checks and session/daily limits remain. Contract-labelled numeric TARGET/TGT posts can update the open channel trade; the first target above entry closes it. Native ordinary trailing and fixed targets do not apply; session profit protection and time exits remain. Unlabelled/private targets cannot be inferred. Worker restart required; PAPER only.

Channel lot sizing: use up to three lots within 60% of available virtual balance and displayed bid/ask quantity. Native learned-rule loss downsizing does not reduce channel sizing; daily/session risk controls still apply. If only one funded liquid lot fits, size remains one.


4 November 2026 opening readiness: Windows Task Scheduler task Options TradingBot Gemini PAPER Startup launches the existing run_bot_session.py hidden at 09:10 India Standard Time, Monday-Friday, with explicit PAPER flags and UTF-8 output. It skips startup when a launcher/bot already exists. Windows wake is requested where supported; the signed-in Windows account, computer and network must be available. The Codex weekday heartbeat provides a health check/fallback. Both engines shorten their final pre-open wait to the remaining time before 09:15. OPENING DATA REQUEST and OPENING DATA RESPONSE logs distinguish request dispatch from successful broker response. Network latency and shared option-chain pacing still apply; Nifty/Sensex requests remain sequenced.

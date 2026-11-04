# Stock-options data quality

## Selective continuation confirmation

Every stock trade still requires scanner membership, at least 1.2x recent
average five-minute volume, candle/VWAP/EMA alignment and full option qualification.
A candle no longer has to exceed a preceding volume spike if it closes beyond
the previous candle's high/low with at least 60% body, 75% directional close and
aligned EMA slope. A tighter three-candle base (width <=1.5 five-minute ATR) can
provide a later-session trigger instead of requiring another opening-range break.
The existing <=2 ATR confirmation range, bounded trigger distance and <=3 fifteen-
minute ATR EMA extension still govern the breakout exception. The normal latest-
price follow-through check is retained. Entry diagnostics label cash price pressure
and the volume path; these are inferred price/volume alignment, not measured
aggressor flow or a complete stock-futures OI dominance model. No higher target-hit
rate is established until subsequent PAPER outcomes validate these conditions.

## Selective two/three-lot sizing

The maximum stock position is three lots. After normal quote qualification, an
entry with signal-quality score at least 80 and aligned completed higher-timeframe
trend can be considered for two or three lots. The score is not a win probability.
Every candidate size must fit free cash after open positions, displayed bid and
ask depth, the per-lot risk ceiling, planned RR and decay budget. The modeled
option-move net gain must cover at least 120% of its planned target and 1.5 times
planned risk. Scaling stops when the scenario reaches the runner objective, or
at the maximum affordable/eligible size; otherwise the original size is retained.
Sizing is rechecked before reservation. No fractional lots or leverage are used.
The entry audit stores the selected size and its scenario rationale. Historical
baseline replays without live depth/Greeks do not validate this sizing overlay.

## PAPER winner extension

STOCK_OPT_RUNNER_TARGET=5000 is a per-position upper net objective, separate from
the base entry-planning target. It does not force extra lots. At the normal
booking threshold, a fresh option quote and underlying quote, actual held-contract
delta/theta and meaningful OI change since entry must support a bounded continuation
scenario with 20% extra room above the additional gain needed. Missing/stale inputs
retain normal smaller-profit booking. Held-contract chain refreshes are cached
in memory for up to 30 seconds. Approved runners protect the base net gain using
the fee model and adverse exit slippage, and use a trail no tighter than one
original premium-risk distance unless an existing higher stop already applies.
Stops never move down. The Rs.5,000 objective, stop, no-progress, maximum hold and
14:45 cutoff can all close the trade. Gaps can defeat the intended profit floor.
Historical OHLC-only replays do not reproduce this live-data overlay. LIVE broker
brackets retain their base targets; the extension applies to PAPER positions only.

## ScanX-style scanner membership (current)

The stock scanner applies the shared NSE F&O filters separately from execution:
quote day change >= +2% with price above the previous completed 15-minute EMA(10)
for calls, or <= -2% with price below that EMA for puts; current cumulative
session volume must exceed the previous complete trading session's volume.
Volume/Previous Volume without an intraday qualifier is interpreted as daily
session volume, consistent with the magnitudes in the supplied screenshots.
Exact ScanX parity is not claimed: its refresh, adjustments and EMA seed may differ.
The full Dhan quote endpoint supplies session volume and the previous-close
reference. There is no top-eight cutoff or artificial Rs.25 underlying price floor.
Only complete three-candle 15-minute groups enter the EMA, and missing previous
session bars or stale quotes fail membership. The daily-history endpoint supplies the previous full-session volume, cached
in memory. A complete 75-bar intraday sum is only a fallback reference for
offline checks; missing tails are never treated as zero volume.
STOCK SCANNER MATCH logs expose membership values independently of later entry
rejections. Extra candle, option, wallet and risk requirements can exclude a trade
without implying that the stock failed scanner membership. Saved entry diagnostics
include the membership evidence. Historical exit replays without full quote/session
volume evidence do not validate this live membership gate.

The stock strategy settings are separate from data coverage. Provider outages can
still occur. Failed or partial historical requests must not become a clean result.

## Current stock-only settings

Stock virtual starting capital is Rs.50,000. STOCK_OPT_RUPEE_MAX_RISK is an
net loss exit threshold **per whole position**, currently Rs.1,500, including
modeled charges and adverse slippage. It does not multiply by lot count and does
not veto an entry because the percentage stop projects a larger loss. Fresh bid
liquidation at or below this threshold triggers CASH_LOSS_LIMIT. OHLC replay
uses a fee/slippage-aware cash stop and fills gaps at the available open.
Actual gap/slippage losses can exceed the threshold. The planner selects the smallest qualifying lot count
within the configured maximum, then quote qualification checks the entire
premium debit and estimated costs against free cash after open positions.
No leverage or fractional exchange lots are assumed; the daily entry cap is four, with at most two concurrent positions. Existing paper balances and completed trades are not reset.

The stock setup includes a bounded opening-range breakout continuation exception
to the 1.25 ATR EMA-distance gate. After the full 09:15-09:30 opening range, a
fresh range break or first crossing of the scanner day-change threshold can
qualify with aligned EMA slope, at least 1.5x relative volume, 60% candle body,
75% closing strength, a candle range at most 2 five-minute ATR, a close no more
than 2.25 five-minute ATR beyond the opening level, and EMA distance no more
than 3 fifteen-minute ATR. Subsequent extended trend candles receive no blanket
exemption. Both call and put signals use symmetric checks; live quote, Greeks,
wallet, stop/target planning and four-entry daily limits still apply.
On October 26, replaying BSE underlying candles identifies a 09:45 CE setup;
this is an underlying signal check, not proof of an executable option fill or P&L.

`python run_bot_session.py` now launches both the index engine and the stock-options
engine, plus the index dashboard. Both engines follow the launcher's PAPER/LIVE
mode; current local `PAPER_MODE=true` keeps them in paper mode. The stock bot may
finish after its own daily limit or cutoff while the index engine continues.
Standalone stock scanning is `python dhan_dynamic_fno_stock_bot.py --scan-once`.
Initial signal, chain-selection, quote qualification and recheck failures are
logged under `OPTSTK REJECT` or `OPTSTK RECHECK REJECT`.

Trading sessions screen the existing mover shortlist incrementally. Each fully
qualified candidate passes its learning veto and immediately enters the final
fresh underlying/chain/depth/risk recheck, before unrelated symbols are screened.
A failed recheck resumes the remaining shortlist. This replaces full-shortlist
ranking before entry with shortlist-order execution among eligible candidates;
it does not promise the globally highest score across unexamined symbols.
Scan-only mode still evaluates the entire shortlist. Quote freshness, the
-0.10 to +0.85 ATR follow-through band, wallet checks, open-position servicing
and daily reservations remain in force. This reduces scanner-induced delay;
network latency and invalid entry inputs can still prevent a fill.

The revised 2026-10-23 local profile uses a Rs.1,250 minimum estimated net
stock target, minimum planned net RR 1.4, a 2.5% base spread cap and a 600-second
confirmation window after a five-minute candle closes. Set these with
`STOCK_OPT_RUPEE_TARGET`, `STOCK_OPT_MIN_NET_RR`, `STOCK_OPT_MAX_SPREAD_PCT` and
`STOCK_OPT_CONFIRM_WINDOW_SEC`. The window must be 300â€“900 seconds, is exclusive
at its upper boundary, and is shared by signal detection and quote qualification.
Future/forming candles and gaps between required confirmation bars remain invalid.

The current local estimated risk ceiling is Rs.1,600, with at most three lots and four
entries/day. The local percentage target reference is 14%, the premium stop is 7%,
and adverse modeled slippage is 0.05%, with the existing cash-target extension enabled.
Defaults without local overrides remain separate from the selected local profile. Full planned RR and risk,
wallet, depth for the selected quantity and bid-above-stop are still required.
These are modeled constraints, not guaranteed fills, profit, or maximum loss.

A strong mover means an absolute underlying day change of at least 6%, based on
the latest confirmed signal. It can use one extra spread percentage point (3.5%
with the current local profile), a 55% minimum of the required net target in the 1.5-ATR
Greek scenario, and a chain IV-range cutoff of 90% instead of 80%. This is a
cross-sectional min/max IV position, not historical IV rank or a percentile of
contracts. At the cutoff the option is rejected. Normal movers retain the 75%
scenario floor, or the stricter 100% scenario screen when cash-target extension
is enabled. Strong-mover extension plans use the full cash target and risk/RR
constraints before applying the explicit 55% scenario screen; otherwise the old
forecast-price cap would silently defeat the requested relaxation.

The rationale is to reduce stacked timing/spread/forecast rejections on large
moves such as the observed Swiggy decline. A later rally in a put premium does
not establish that an earlier entry would survive its stop. This profile requires
replay and paper observation; it does not ensure capture of that move.

Minimum DTE remains four calendar days; local delta band is 0.40â€“0.75; premium floor is
Rs.3; local maximum hold is 16 five-minute bars, with no-progress checking after eight
bars; daily theta limit is 6%. Quotes must have been received within 30 seconds
and Greek snapshots within 120 seconds. Both underlying follow-through checks use
-0.10 to +0.85 five-minute ATR. Valid broker Greeks remain mandatory.

Nifty confirmation now also defaults to 600 seconds (validated 300â€“900). The
range of already-closed current-session candles must reach 80 points before body
and directional close-strength minima soften from 0.50/0.70 to 0.40/0.60. This
range test alone does not establish a trend: marked-level reaction, matching
strike OI/premium confirmation, chase/room and structural RR checks still apply.
Future bars cannot activate the exception.

Expiry inputs are normalized to ISO dates. The documented option-chain key is
`security_id`; `securityId` is also accepted, but must match the scrip master.
Zero/invalid Greeks are still rejected, not estimated.

Observed historical gaps and missing tails are coverage warnings: they do not
discard an entire stock's available history. Signals can use sparse older 15-minute
buckets only after those buckets close, while fresh consecutive confirmation
bars are still required. No missing observations are fabricated or assumed to
mean zero trading. Warnings remain in the final report and keep it incomplete.
Rolling ATM strike changes remain unvalidated: a constant-delta adjustment cannot
reconstruct a fixed contract's historical OHLC, so those trades are not scored.

- New caches use a versioned request fingerprint and receipt time. Older cache
  formats are ignored, without deleting the existing files.
- A date-only end boundary means midnight IST, exclusive. Backtests exclude the
  unfinished current session. Live candle retrieval still ends at the current time.
- Mutable cache entries expire after 15 seconds. Completed historical intervals
  may be reused only after validation. Detected partial NSE sessions and internal
  candle gaps are reported and are not written as authoritative caches.
- Option history is requested in five-day chunks, with OHLC and strike fields.
  A failed chunk is recorded; successful chunks can be reused on the next run.
- Transient requests receive one retry. After three exhausted transient failures,
  the client opens a circuit instead of repeating long waits across
  every stock. Current-session requests can make a bounded recovery probe after
  60 seconds; completed historical requests remain blocked for that run. No synthetic bars or
  successful empty-data cache entries are created.
- Reports distinguish historical errors, missing entry bars, simulation gaps,
  premium exclusions, unavailable risk/target plans and insufficient funds.
  Requested ranges, actual first/last candles and row counts are saved.
- `INCOMPLETE_DATA` and CLI exit code 2 indicate an incomplete replay. Recorded P&L
  on any available subset is not a validated strategy result. Even a data-complete
  replay is labeled `COMPLETED_EXIT_REPLAY`, with `full_strategy_validated=false`:
  historical bid/ask, broker Greeks and delta-selected contracts are unavailable.
- Production quotes carry local response receipt timestamps. A last trade can be
  older than a newly received depth snapshot. Snapshot age, last-trade age and
  Greek receipt age are kept separate. Legacy recordings without receipt metadata
  use the restrictive last-trade-age fallback. A local receipt time is not a claim
  about the provider's underlying calculation time.
- Candidates are qualified as they are fetched and rebuilt from current candles,
  underlying quotes and option chains before entry. Stops, sizing, entry cutoffs
  and price/liquidity checks remain in force.

Regression checks:

```powershell
python -m unittest test_stock_data_quality test_stock_option_policy test_stock_option_integration test_index_option_replay
```

Official references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/expired-options-data/
- https://dhanhq.co/docs/v2/market-quote/
- https://dhanhq.co/docs/v2/option-chain/

## Current-implementation replay audit

Live stock quotes refresh on a four-second schedule, configurable through
`STOCK_OPT_POLL_INTERVAL_SEC` within 3-5 seconds. The previous 30-second idle
sleep is removed. During a long shortlist scan, the same thread refreshes the
universe quote batch and checks open paper positions between candidates and
before chart, chain and quote requests. Entry cash and daily capacity are
rechecked after these position checks. This avoids concurrent ledger writes or
orders. Full chain/Greek qualification remains selective; all stock chains are
not fetched every four seconds. Five-minute confirmation still uses closed bars.

Nifty already polls its option chain with a 3.6-second minimum gap. Stock quote
requests retain a 1.05-second minimum gap and stock chains a 3.05-second gap.
These are nominal schedules: synchronous HTTP latency, endpoint pacing and
429/error backoff can extend refresh times. Market polling stops under existing
session cutoffs and entry limits when no position requires management.

The stock backtest CLI now requests five extra calendar days for signal warmup,
then uses the same rolling five-calendar-day history window as the live scanner.
An expanding multi-month EMA history is not substituted. All stocks in the current
universe can be requested with `--max-stocks 0`; current membership and lot sizes
still do not reconstruct historical membership. Below-threshold scanner moves
are discarded before the expensive candle evaluator, as in live scanning.

The isolated 23 October study checks every expected five-minute bar against
observed Nifty session dates and retains missing-bar details. Historical depth,
Greeks, chain IV range and delta-selected strikes remain unavailable, so an
exit-policy result stays explicitly short of full strategy validation. All
production paper ledgers and learning state are hash-checked for isolation.


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

This revised local profile was applied after the stored 30/60-day results. Those results do not establish the profitability of these changes. Existing positions and ledger balances are not reset when configuration changes.


Audit correction, 26 October 2026: fixed-strike replay no longer estimates a held
contract by applying delta to a different rolling strike, skips missing candles,
or clamps gap losses to a planned stop. Missing/changed-contract paths are
unvalidated. Open gaps use the observed open; ambiguous same-bar stop/target paths
use stop first. A 14:40 bar closes at the 14:45 session cutoff. The replay uses the
same no-progress threshold as PAPER; new intrabar highs ratchet the following bar.
These corrections supersede earlier reconstruction and wick-tolerance descriptions.
Theta forecasts consistently use a 375-minute trading-session convention. Runner
holds additionally need a recent 270–450-second underlying reference and positive
current directional progress; past progress since entry alone cannot qualify.
The Rs.3,000 default daily loss budget reserves estimated risk for concurrent open
positions. Durable executed outcomes drive risk and learning even when a reporting
adjustment excludes a trade. LIVE execution is blocked pending broker reconciliation.


Backtest verification follow-up: historical replay now applies the consecutive
net-loss breaker and daily loss budget using only exits known at each decision
time, with open-position risk reserved. A missing/changed-contract exit blocks
further entries for that session instead of assuming capital was released. The
replay still uses an intraday last-close proxy for the previous session close;
that proxy does not establish parity with the official close in a live quote.


27 October 2026 — opening-spike volume correction:
The ordinary closed-candle requirement remains 1.2 times the preceding ten-bar
mean. A separate strong-continuation path may pass lower relative volume only
when the complete first 15-minute opening range contains a volume spike at least
2 times the median volume of the preceding three consecutive post-opening
candles, and that spike has raised the ten-bar mean above 1.25 times the median.
The confirmation must have rising volume versus the previous candle, at least
75% of that post-opening median, a body of at least 60% of its range, a close in
the directional outer 25%, a fresh break of the previous candle's high/low and
aligned EMA slope. VWAP, EMA-distance, scanner membership, option qualification,
cash and risk checks still apply. Both CE and PE use the same rule. Forming and
future candles are excluded. Raw relative volume remains in the quality score;
the exception does not boost a candidate's score or size. Entry diagnostics store
volume_context and OPENING_SPIKE_ADJUSTED_STRONG_CONTINUATION when used.
The recorded MANKIND 09:50 candle passes this volume exception as of 09:55; this
is a setup-path regression check, not evidence of an executable option fill.


27 October 2026 — quote display correction: entry_price is the saved PAPER ask
and never doubles as an LTP observation. Open-position notifications now refresh
from received quotes, show actual last_price separately from bid/ask, identify
the expiry and mark quotes older than 30 seconds stale. Gross mark-to-market uses
the bid when available; execution continues to apply its existing slippage and
charges. Missing LTP is displayed as unavailable rather than filled from entry.
Quote timestamps are persisted periodically alongside existing stop/peak state.

Entry execution review (27 October 2026): spreads may consume at most 40% of
premium distance to the effective stop (the tighter of percentage and cash-loss
stops). Scaling rechecks this bound and retains a qualifying smaller size.
Signals at least two minutes beyond confirmation require fresh consecutive
closed one-minute candles. A weakening last close combined with a retreat over
0.5 five-minute ATR from the recent extreme blocks a fading entry, symmetrically
for calls and puts. Forming candles are excluded. Missing/stale minute data
cannot validate an aged entry. This check runs again during final qualification.
Five-minute-only historical runs cannot validate this minute/depth-dependent rule.
These controls can also exclude profitable trades; they do not guarantee wins.

The cash exit threshold is applied before planned reward/risk target inversion,
so the planner does not demand a reward based on a larger, superseded percentage
stop risk. Gap/slippage risk still exists and is not a guaranteed capped loss.
After chain/contract selection the underlying quote is refreshed (receipt age
at most eight seconds), direction/entry-zone and recent-minute momentum are
rechecked. Protective exits, including CASH_LOSS_LIMIT, are processed before
optional runner analysis. Existing day-loss breakers and executed losses remain.

Screenshot-reference breakout selection (selected local PAPER profile)

STOCK_OPT_REQUIRE_CONFIRMED_BREAKOUT=true uses price/day-move/15-minute EMA
membership to build candidates without waiting for today's cumulative volume to
exceed the prior full day. This supersedes the full-day-volume requirement for
this selected profile; false retains the earlier ScanX-style comparison.
This is an explicit interpretation of the screenshot's visible checks, not a
replica of a proprietary scanner. A candidate is not automatically an entry.
A qualifying closed five-minute candle must break both the relevant previous-day
high/low and the already-known current-session extreme, including the completed
opening range. A 0.05 ATR buffer (at least one tick), 60% body, 75% directional
closing location, aligned completed EMA slope, VWAP and bounded extension apply.
Previous-day levels come from Dhan daily OHLC or a verified complete prior
session. The current-session level history must be contiguous; forming candles
cannot supply either confirmation or a pre-existing level.
Volume ordinarily requires 1.2 times the recent ten-bar average. An opening-spike
baseline may be used only for a strong fresh continuation; the first such setup
needs at least two consecutive post-opening baseline bars and 1.2 times their
median volume, with rising volume and the existing opening-spike dominance test.
The later existing robust-baseline branch is retained. Current quotes must still
hold the breakout level during initial and final option selection. Existing
fresh-minute, spread/stop-room, wallet, Greek/expiry, loss and trade caps apply.
The shared historical signal evaluator applies the same breakout requirements
and records this flag. Missing historical decision-time depth/Greeks and fresh
minute inputs still prevent a full live-strategy validation.
Ather's 27-Oct 09:40 candle is eligible after closing at 09:45 in an underlying
signal replay; this is not a verified option entry or profitable fill. Recorded
RECLTD 11:17:39 and TATACONSUM 11:39:54 entries fail the new fresh-breakout test.

Current-expiry-only policy (user correction, 27 October 2026)

Stock options now always select the earliest unexpired NSE expiry, including
expiry day. The former four-day minimum is removed; legacy positive DTE env
values cannot roll selection to a later month. Universe caching uses the dte0
namespace rather than reusing the old dte4 upcoming-expiry universe. The selected
expiry's master-matched security ID, Greeks, spread/depth and affordability must
still qualify. If the current contract is unsuitable or unavailable, withhold
entry; never substitute an upcoming expiry. Already expired contracts remain
ineligible. Existing positions and historical ledgers are not rewritten.
This supersedes prior four-day-minimum descriptions. The earlier hypothetical
Ather 18-to-34 calculation does not change the bot's profit targets or loss limits.

Staged PAPER profit management

STOCK_OPT_STAGED_PROFIT=true enables one partial exit on multi-lot positions.
The base full-position profit must cover the additional exit fee, with fresh
quote/depth and an at-most-eight-second-old positive runner assessment. Two lots
book one; three lots book one and retain two. No fractional exchange lots are
sold. Missing continuation evidence books the ordinary full base target instead.
Before staging, meaningful favorable movement arms a fee-aware breakeven stop
rather than the tight ordinary trail. After staging, booked profit and the
remaining executable bid jointly determine net loss, profit floors and targets.
The runner needs fresh five-minute underlying progress, volume participation,
held-contract OI/Greeks and an affordable remaining target. A failed assessment
closes the remainder; a stale old assessment cannot indefinitely authorize hold.
The upper combined runner cap remains Rs.5,000. After a partial, its working
combined target is booked net plus the remaining quantity's share of that cap,
bounded by the cap. This is a scenario target, not a promise of Rs.34 or 16 points.
Only a booked, positively confirmed runner can extend beyond the ordinary hold
limit, up to STOCK_OPT_RUNNER_MAX_HOLD_MINUTES=90 and always the 14:45 cutoff.
The Rs.1,500 net cash-loss exit still applies to the combined position.
Partial fills and remaining whole lots are atomically saved to the existing
reservation before the in-memory position changes. Recovery cannot repeat the
partial. One entry remains one trade/day slot. Final P&L includes every fill,
with one entry brokerage and each exit brokerage; weighted exit price is a
summary, while exit_fills retains individual prices. Partial net profit is not
added again to the closed-trade ledger. Returned principal can be reused, but
partial profits are credited to the wallet only when the trade closes, which
keeps interim buying power conservative. Notifications display booked profit
and remaining units. No live brokerage orders are authorized by this feature.
OHLC base-exit replay explicitly excludes this live volume/OI-dependent staged
runner and must not be presented as a full validation of the selected strategy.

Candidate choice after screening

The live worker now compares a bounded group of up to three already qualified
stocks instead of executing the first qualified member of day-change order.
A soft eight-second comparison window is checked between requests; provider
latency may exceed it, so the final signal, chain, quote, wallet and risk state
are rebuilt before reservation. The entire universe is not awaited. The final
incomplete group is also ranked, so a sole valid candidate is not discarded.
Priority combines confirmed signal quality, bounded same-symbol/setup closed-net
learning feedback, planned net RR, and a bounded execution bonus (at most eight
points) for smaller spread-to-stop fraction and fresher confirmation. Day-change
percentage is an admission/visitation field, not the final priority score. The
score is a heuristic, not a probability or guarantee of the day's best performer.
For opening-spike-adjusted confirmed breakouts, quality uses the explicitly
recorded robust volume baseline; raw relative volume remains unchanged in the
diagnostics. A large opening burst must not suppress a later valid signal's
quality score merely by inflating the mean. Comparison summaries and final fresh
selection analysis are logged and stored in the existing entry diagnostics.
A failed top-candidate recheck moves to the next candidate, while normal quote
refresh and existing position exits continue between screening requests.


2026-10-27 requested base-lock and entry revision

The latest local PAPER profile supersedes the earlier Rs.3,200/Rs.6,000 setup:
base target Rs.1,200, runner ceiling Rs.5,500, target reference 16%, premium stop
7.5%, minimum net RR 1.5, cash-target extension, confirmed breakouts, opening
trend and staged profit enabled, at most two lots. The previously configured
Rs.1,800 whole-position cash-loss trigger, 30-minute no-progress window,
90-minute ordinary hold and 105-minute booked-runner limit remain unchanged.
All holding remains subject to the 14:45 IST cutoff. .env is local and ignored.

Cash-target extension uses min(25, max(target_pct, 18)) percent rather than a
100% premium ceiling. An unaffordable/unreachable cash target is rejected or
requires another qualifying lot; it is never silently reduced. Target-based
trail spacing returns to 25% of entry-to-base-target distance.

Initial, final and pre-entry stock checks all cap chase at +0.35 ATR beyond
the confirmation close. Confirmed breakout candles cannot exceed 1.5 ATR;
those closing by 09:45 IST need directional close strength at least 0.80.
These rules do not add a mandatory separate retest candle or exactly reproduce
an external scanner's five indicators.

At the base bid target, paper_exit records base_target_locked and raises the
stop to max(entry + 0.5R, base target - 0.5R), without lowering an existing stop.
The base target is no longer an automatic full exit. Existing runner/locked
positions trail with at least 1R spacing; an unmanaged retracement after a
1R extension above base can exit as RUPEE_TARGET_TRAILED. Combined net runner
ceiling, protective stops, time limits, session cutoff and fresh depth remain.
A stop floor is not a guaranteed fill or guaranteed net profit during gaps.

The preceding fast-runner revision remains: before 270 seconds of position
age, impulse may use the confirmation close, with measured post-entry volume
still required for staged runners. Older trades need a 270-450 second reference.
Unchanged OI no longer vetoes assessment; fresh depth, Greeks, volume and the
remaining-move check still apply. Durable partial runners are not liquidated
solely because an assessment pauses. Their combined target can be below the
runner ceiling after partial booking.

Historical OHLC simulation still closes at its base target and excludes the
live staged runner and the new base-lock hold logic. It must not be presented
as validation of this profile or a guaranteed profit improvement.


2026-10-28 scanner membership correction (supersedes the volume bypass)

All live PAPER entry attempts now require current cumulative session volume
above the previous full-session volume, independently of confirmed-breakout
mode. This applies both during candidate screening and the final entry recheck.
Only after strict scanner membership passes are the individual stock breakout,
confirmation and option execution checks evaluated. The low-level scanner helper no longer exposes any volume-bypass switch. SONACOMS at 11:35 on October 28 had 2,827,356
current volume versus 19,084,943 previous volume and must not qualify through
this corrected gate. Its existing PAPER reservation, eventual P&L and learning
outcome remain intact. Future logs label passing scanner results MATCH.


Permanent scanner invariants (2026-10-28)

There is no require_daily_volume override in scanx_stock_membership. Cumulative
volume must exceed previous full-session volume for every membership decision.
The day-change threshold cannot be relaxed below 2%, and the scanner uses the
previous completed 15-minute EMA(10). Confirmed breakout, opening-trend,
learning, runner or Telegram context cannot override scanner membership.
Historical stock signal replay now calls the same strict scanner gate before
individual analysis and records scanner_rejection_counts. Missing complete
previous-session volume coverage fails closed instead of assuming membership.
Historical OHLC-derived scanner prices are modeled, not archived live quotes.
All saved old diagnostic replays retain their earlier limitations and must not
be relabeled as compliant with these scanner invariants.


28 October 2026 - stock entry price-action paths:
The strict scanner remains mandatory. Direct closed breakouts may enter only within 0.75 ATR of the pre-existing level, with existing volume/body/EMA checks. Otherwise wait for a separate closed retest: one to three contiguous pullback candles, each volume at most 80% of the original breakout volume, closing no farther than the level buffer inside the old range. Pullback wick must touch within -0.25 to +0.35 ATR of the level; renewed confirmation must break the pullback range with 1.2x its maximum volume. Existing confirmation-volume and candle-quality checks still apply. Retest confirmation must remain within 0.75 ATR of the original level. Live retest entries allow at most 0.25 ATR retreat from their new confirmation close, versus 0.10 ATR for direct entries; both retain the +0.35 ATR chase cap and must hold the breakout level. Bullish/bearish symmetry, closed-bar timing and missing-bar rejection are tested. Entry diagnostics record entry_path, original breakout time, pullback bars, volume ratios and structural invalidation level. The latter is diagnostic, not a new exit rule. No improvement in profit is claimed without replay validation. Existing option-risk and profit-protection exits are unchanged.


28 October 2026 - stock exit controls:
All lot counts now arm fee/slippage-aware breakeven after the larger of 1.5 trail jumps and 0.75 initial premium risk points, provided the current executable value still covers costs. Stops never loosen. Two consecutive post-entry closed one-minute underlying candles beyond the broken level by 0.10 ATR, with the latest candle opposing the trade, trigger UNDERLYING_BREAKOUT_FAILED. This check runs at most every 30 seconds after urgent option-stop/cash exits; stale or incomplete candles cannot trigger it. Available bid levels are walked for PAPER liquidation; insufficient aggregate depth withholds a fabricated fill and is recorded. Each position keeps a bounded 24-sample exit-monitor ring, with receipt age, available quantity, executable VWAP, quote gaps and unusable checks. Closed trade exit_diagnostics include trigger, actual modeled fill, stop, peak and cash-limit overrun. No fill at an unavailable stop is fabricated. Base OHLC replay shares the new breakeven threshold for subsequent bars only; it still excludes structural one-minute exits, depth execution and the live staged runner. No new ledger/report files required; no historical PnL rewritten.


29 October 2026 entry validation: confirmed closed breakouts may retrace at most 0.25 five-minute ATR, capped at half the confirmation candle close-to-adverse-extreme distance, while holding the breakout level plus its confirmation buffer. The upper chase limit remains 0.35 ATR. Unconfirmed setups retain their existing band. Scanner membership, direction, quote freshness and signal expiry are checked again after option selection. Entry validation bounds and observed prices are retained in existing trade diagnostics. These are execution-rule changes, not evidence of improved historical profit.


30 October 2026: An additional closed-candle CONSOLIDATION_BREAKOUT entry path evaluates three to six contiguous base candles after the opening range, a base no wider than 1.5 ATR, non-expanding base ranges and volume, directional pressure, and renewed volume of at least 1.2x base median and 1.05x the previous candle. The confirmation retains 60% directional body and existing closing-strength checks, stays within 0.5 ATR of its local level, remains beyond previous-day/opening levels, and has at least one ATR to an earlier session extreme when that extreme is ahead. Scanner membership, EMA/VWAP, executable option qualification and risk controls remain mandatory. Existing entry diagnostics retain the base, volume reference, trigger and invalidation evidence. This is a PAPER experiment, not a validated profitability claim.


30 October 2026 autonomous PAPER feedback: existing closed ledgers now supply setup-family evidence across stocks. At least eight unique closed outcomes across three IST dates are required; the oldest chronological portion trains and at least three later outcomes validate. Recent evidence is limited to 40 trades within 60 days. Only aligned training/validation evidence adjusts ranking, by at most five pooled quality points; total same-symbol plus pooled adjustment remains bounded to ten. A family can pause for 24 hours after at least twelve outcomes, negative training and validation, and three latest net losses. Re-evaluation after the pause allows recovery. Historical/backtest records, future closes, duplicate entries and unknown monetary risk cannot train the pooled policy. Validated repeated givebacks can select earlier fee-aware breakeven protection at entry; this snapshot is persisted in existing trade diagnostics. No learned policy increases lots, loss budgets, chase limits or targets, or bypasses scanner/quote/Greeks checks. Ordinary policies remain neutral until evidence is sufficient. No bot process rewrites Python source files. These are bounded adaptive PAPER rules; unit tests do not establish profitability.


2 November 2026 exit refinement: the first position-service pass is restricted to cash-loss, existing-stop and session-end exits. Runner and breakout assessments refresh before discretionary target/time/no-progress decisions. A full-position executable net base target books RUPEE_TARGET unless fresh runner evidence supports holding. New protection floors must remain below the executable bid rather than forcing same-tick exits; ordinary trail distance is at least half the initial premium risk. Peak/progress uses executable bid-depth VWAP and the entry bid, not the entry ask. Spread may consume at most 25% of the effective stop distance for initial and increased lot sizing. Initial stops and cash/daily loss limits are not widened. These PAPER execution changes have automated regression coverage, not a proven increase in realised profitability.


2 November 2026 structural entry-risk check: final option qualification uses the fresh underlying spot, prepared trigger, retest/base invalidation (or trigger plus/minus buffer for a direct break), current delta and executable spread. A conservative local delta scenario must fit inside the existing effective premium/cash stop. The entry must also remain within the existing 0.75 ATR setup distance (0.50 ATR for a consolidation break). An incompatible entry is rejected until a fresh better-positioned setup forms; stop distance, cash budget and lots are not increased to force it through. The same check applies before increased sizing. Diagnostics retain scenario and stop room. Delta is a local approximation, not a guarantee of future option prices. This check blocks the recorded Reliance entry, and also rejects some profitable historical entries; it is not a full strategy profitability replay or proof of improved returns.


4 November 2026 exit audit: current modeled net liquidation profit at or above 0.5 times initial effective net risk (bounded by the position cash ceiling) arms the fee/slippage-aware breakeven floor. This addresses the recorded Dixon case without widening initial risk. Immediate base-target booking still takes priority. Floors remain monotonic and subject to real liquidity/gaps; historical peaks are not guaranteed achievable exits. The OHLC replay applies the analogous ratchet on the following bar to preserve conservative intrabar ordering. Both JSON writers raise an error if replacement retries are exhausted rather than returning silently.

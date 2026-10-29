> Current entry behavior is documented in NIFTY_LEVEL_ENTRY.md. The notes below describe the earlier momentum-gated implementation and its historical evaluation.

# Nifty entry structure

Nifty entries now require both the existing OI/momentum/learned-rule filters and
an explicit price-structure check. Sensex and stock-options entry strategies are
unaffected. Existing daily limits, premium stops and multi-lot +10/+20 targets
are not changed by this gate.

The gate uses actual five-minute Nifty INDEX OHLC from Dhan charts/intraday and
strike-level OI from the current option chain. It maps:

- Previous full regular-session high/low.
- Today's high/low across closed five-minute candles.
- The 09:15–09:30 opening range.
- Swing highs/lows confirmed by two closed candles on each side.
- The largest nearby call and put OI concentrations, within 500 spot points.
  OI change is explicitly relative to previous close, not an intraday change.

Entry needs a level event followed by a separate directional hold/retest candle,
with current spot still holding confirmation. Default level buffer is three
underlying points. The next mapped opposing level must be at least 20 underlying
points away, and the room divided by distance to the structural invalidation
anchor must be at least 1.5. This structural ratio is a screening heuristic, not
the option trade's actual net reward/risk. Existing option stops still manage risk.

Inside-range movement without a confirmed level event is rejected. Initial
breaks alone cannot enter. A rejection must be followed by a directional close
through the rejection candle's extreme. These are defined candle patterns,
not proof that a level will hold. The confirmation
candle's new high/low is not used as an already-existing opposing barrier.

There must be a complete prior regular session, complete opening range, continuous
current-session candles and a fresh closed confirmation. No missing candles,
OI or levels are synthesized. Live index history refreshes every 30 seconds.
Unavailable or stale structure blocks a new Nifty entry, while open-position exit
management continues. Levels and reasons appear in the dashboard, telemetry and
NIFTY STRUCTURE log records. Entry positions retain their structure assessment.

Backtests use the same price gate without future candles. Historical OI can only
use the rolling strikes available to the old replay; this is explicitly marked
as a subset of the full chain. Missing structure is counted and the output is
marked INCOMPLETE_MARKET_STRUCTURE. Historical replay is not a verified live-fill
result. Existing learned rules may include the evaluation period, so a separate
chronological training/holdout study is needed before claiming performance gains.

No 80% win rate, consistent profit or average-win/loss improvement has been
established. This change must be evaluated using costs, slippage, drawdown and
unseen-session results, alongside win rate and average net wins/losses.

## Entry and replay correction

The entry gate now waits for a separate completed five-minute hold/retest candle
after a level event. The follow-through candle needs at least 50% body/range and
70% closing strength in the trade direction. Current spot must hold confirmation
and cannot run beyond a 0.25 recent true-range entry zone (minimum three points).
Reference day-range levels exclude the break and confirmation candles. Earliest
evaluation is 09:40 after five closed session candles. Stronger selectivity is not
evidence of better performance.
Nifty also waits when the current and reference ATM strike baskets differ;
cross-strike premium/OI changes are not treated as an entry signal.

Historical entries use the following bar's open, with 0.1% adverse modeled entry
and exit slippage. A gap beyond the existing stop uses the worse opening price.
If stop and target both fall inside a bar, the existing stop is assumed first;
an opening gap through a target is handled before later intrabar movement.
Trailing levels calculated from a candle's high only apply to following bars.
Rolling-strike changes, missing bars and truncated sessions produce explicit
unvalidated outcomes. Those outcomes never train rules or contribute invented
P&L. Further entries for an unresolved historical position's day are blocked.
The replay cannot establish a profitable full strategy where history is missing.
Only five-minute option replay is supported for the Nifty structure strategy.

Official API references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/option-chain/
- https://dhanhq.co/docs/v2/annexure/
# Premium and theta gates applied October 25, 2026

Nifty and Sensex now apply the requested buyer gates before learned-rule lookup.
Premium floors use the configured base (45/125 rupees), multiplied by 1.45 for
DTE below 0.6, 1.20 below 1.5, and 1.08 below 3. Fractional DTE is measured to
15:30 IST using the snapshot's decision time. Missing or invalid expiry retains
the attachment's 5-day fallback; this does not establish actual expiry safety.

The expected-move proxy is configured minimum spot impulse times absolute delta,
divided by the initial option stop, with a minimum of 1.30. Delta defaults to
0.50 and is clamped to 0.25–0.70. This proxy is before charges, not net RR.
**With the current configuration this gate rejects every entry:** Nifty's best
case is 8.5 × 0.70 / 6.15 = 0.967; Sensex's is 25 × 0.70 / 20 = 0.875.
Larger actual impulses do not change this formula. Thresholds were not silently
relaxed to restore trading.

Below 0.6 DTE, entry requires time before 10:30 and impulse at least 1.4 times
the configured minimum. Below 1.5 DTE, entries from 13:00 require that strong
impulse. Available absolute daily theta above 12% of premium blocks entries
below 3 DTE, including early expiry-day entries. CE and PE Greeks remain
separate so a later direction change cannot reuse the other side's theta.

Available two-sided books require spread at most 2.5%, or 1.5% for premiums
below 80 rupees, and at least one lot on both sides. The live submission path
rechecks available quantity against the actual selected lots. As requested in
the attachment, absent bid/ask fields skip the optional book check; missing
theta skips explicit theta percentage checking. Nonfinite supplied fields are
rejected. Historical rolling bars lack these books/Greeks and verified expiry,
so past backtests do not validate these gates. Optional IV-rank filtering was
not added without a reliable historical IV series. Rejections are recorded in
`entry_decision` and DEBUG logging; learned rules cannot override them.

## Learning revision applied October 25, 2026

Closed-trade rewards still use net results after modeled charges. The learning
rate is now 0.006 with 0.997 weight/bias retention per update. Rule promotion
requires either eight trades across five days, expectancy at least 0.35R and
win rate at least 42%, or four wins across four days, at most one loss and
expectancy at least 1.0R. Two same-day losses still mute a rule. Pruning checks
now use three trades with no wins, six trades with expectancy below -0.10R,
or eight trades with win rate below 35%. Daily reset uses the same thresholds.

Position sizing stays at one lot until an ACTIVE rule has at least ten trades
across six days. Two lots require at least 55% wins, 1.20R expectancy, a wallet
of at least Rs.40,000 and affordability within 60% of the wallet. The earlier
three-lot scale-up path is removed. A loss or a current loss streak restores
one-lot sizing. Existing learning files are retained; this update does not
reset their history or retrain on invented outcomes.

The premium/expiry/quote gates above remain in force. In particular the
unchanged 1.30R minimum-impulse formula still blocks all currently configured
Nifty and Sensex entries. This revision is not a profitability validation.

## Current entry/exit experiment (v4.9.2, October 25)

This revision supersedes the earlier expected-move and exit settings above.
The expected-move proxy uses the observed five-minute spot impulse with a
0.95R floor. OI wall room is 35 points. LEVEL_V1 entries require 40 points of
structure room, RR at least 1.60 for breakout/rejection, 3.20 for retests, and
3.50 after a loss. The attachment's final table used base RR 1.60; its earlier
script used 2.80. The final table was selected and documented explicitly.

Single-lot ratchets arm at 1.80R, locking 0.55R; at 2.50R they lock at least
1.80R and trail 0.55R behind the peak. Stops never move backward. Multi-lot
Nifty split targets remain unchanged. Current-policy replay and live stagnation
exits now use 15 minutes with peak advance below 0.45R. Historical/legacy replay
mode retains its older timing for prior research. Replay does not use the
current candle's later high to avoid a stagnation exit at its opening time.

Initial stops remain the current engine's 8.5% premium stop for Nifty, bounded
by 6.15 and 12.50 points, and the configured 20-point Sensex stop. No production
learning history was reset. These changes are an in-sample experiment based
on a previously inspected ledger, not an independent proof of profitability.

## Current six-patch revision (October 25, 2026)

This revision supersedes the earlier settings described above. Structure room
is 32 points; base RR 1.45, retest RR 2.60 and post-loss RR 2.80. After a loss,
room at least 55 with RR at least 2.20 bypasses the usual RR floor, after the
room check. The bypass is evaluated before rejection so it can actually apply.
OI room remains 35. Observed impulse remains 0.95R. Daily net target is 1,450.

Ratchet arms at 1.35R and locks 0.45R; from 2.20R it locks at least 1.40R and
trails 0.45R behind peak. Stagnation uses 18 minutes / peak below 0.55R in live
and current-policy replay. On five-minute bars the first observable opening
after 18 minutes is 20 minutes. Multi-lot Nifty targets are +12/+22 points.
The requested TARGET_2_20_POINTS reason is retained as a legacy label even
though the second split target is +22. The daily-target branch uses that label
when premium gain is at least 18; it still exits immediately when the daily
net target is reached. Merely changing an exit reason does not delay the exit.

Learning uses rate 0.08 for absolute net R above 1.5, otherwise 0.045, without
weight decay or clipping the prediction error. One transition per closed trade
is appended and the replay buffer is capped at 120. Pruning requires win rate
below 32%, at least seven trades and at least four distinct days in both daily
reset and closed-trade learning. Two intraday losses still mute the rule.
Existing ACTIVE sizing additionally requires 48% wins, 0.85R expectancy and
average win at least 2.2 times max(0.1, average loss); the existing inner two-lot
requirements (55%, 1.20R, wallet 40k, ten trades, six days) still apply.

State vectors remain ten-dimensional. Index 7 now stores clipped movement
from the first observed spot of the session in strike steps; index 9 stores
normalized time since 09:15 IST. This replaces IV difference and strike parity.
The first observation is not asserted to be the actual open if startup is late.
Session anchors/history reset each IST day and use decision timestamps in
replay. Existing persisted weights are retained, so the changed feature meaning
requires fresh validation. Sensex stop is 9% of premium bounded to 18�32 points;
its wall threshold is 40 lakh. Nifty stays 50 lakh and its initial stop is unchanged.

Backtests use isolated chronological learning rather than seeding historical
runs with current production learning that may include future outcomes. Live
wallets, learning files, trading windows and rate-limit implementation remain
untouched by the research run. Cached replay makes no claim about future live
HTTP-429 frequency. The 2.3x win/loss value and monthly profit objectives were
not met; see the latest research notes.

## Current observer / twenty-point revision (October 25, 2026)

The user-supplied observer patch was executed in memory against the existing
engine; no patcher file was created. This supersedes the earlier six-patch
entry/exit settings. Daily net target is 1,200; Nifty hard premium target is
20 points, including a single lot and remaining quantity after a partial.
Current-policy replay uses the same hard target, with stop-first ambiguous-bar
handling and adverse modeled slippage. A daily-profit trigger can still exit
before 20 points; a prior daily loss cannot remove the explicit 20-point target.

Structure room is 25; base RR 1.35, retest RR 2.20, post-loss RR 2.10. Wide-room
post-loss override is room >=55 and RR >=2.0. Retests additionally require at
least 40 lakh OI imbalance aligned with the trade. Both directions are vetoed
against an opposing imbalance above 20 lakh or opposite five-minute price move
beyond four points. These are the executable patch thresholds; the prose used
25 lakh elsewhere. A permitted level does not bypass this confluence veto.

When Nifty structure is warming up/unavailable, fallback requires the 50-lakh
wall, dominance, verified five-minute OI reference, matching option/writer
quadrants, directional impulse within configured bounds, alignment with the
session weighted-price proxy, and a known opposing-wall distance >=35 points.
It never bypasses a ready-but-rejected structure assessment. Missing inputs
reject fallback rather than fabricating confluence. Other learning, premium,
theta, spread, cooldown, daily-entry and loss-breaker gates still apply.

The session anchor now stores initial call/put OI and premiums, initial OI
difference, observed high/low, cumulative spot delta and cumulative OI-total
changes. It is the first observation of the session, not a reconstructed 09:15
open if startup/data begins later. The supplied session_vwap is explicitly an
OI-weighted snapshot average, not a volume VWAP. Cumulative OI is a comparison
of basket totals, not verified identical morning contracts; strike rolls can
change those totals. Both limitations are exposed in analysis telemetry.

Ratchet follows the supplied piecewise code: lock 0.35R after gain >=8.5 points
or 1.15R; lock >=0.80R and trail 0.55R after >=12.5 points or 1.55R; lock >=1.35R
and trail 0.40R after >=18 points or 2.10R. These executable values differ from
the attachment's introductory +9/+14-point summary. Stops never move backward.
Historical replay only activates a high-based ratchet on subsequent candles.

The requested pytest command could not run because pytest is not installed.
The existing unittest runner passed 118 index/structure/learning checks. Full
254-test discovery had eight failures and three errors in stock replay/data,
research-ledger and session-maintenance checks; full-suite validation is pending.

## Current daily trend accumulation revision (October 25, 2026)

This supersedes the prior observer fallback restriction. evaluate_snapshot now
returns daily_trend_dir and a timestamp-based d_spot_15m. Rolling history retains
500 snapshots, including the require_reference Nifty path. A 15-minute reference
must be 900�1050 seconds old in the same session; three fast live polls are not
15 minutes. Until a reference exists the move is zero and trend_15m_ready is
false. Snapshot history still resets at the first observation of each IST day.

The supplied three accumulation conditions and one-of-two quadrant detector
are applied: a steady six-point five-minute move, or 3.5 points with a twelve-
point fifteen-minute move, can produce a daily candidate. Option premium must
be rising and price must satisfy the supplied session-price condition. The
strict controller remains present. OI-weighted price/basket-accumulation
limitations from the observer revision remain applicable.

A ready structure may now pass through between discrete levels. Explicit
wait-for-retest, too-close, weak-follow-through and chasing reasons remain hard
vetoes, matched case-insensitively. A ready-structure accumulation entry needs
verified OI, known wall room at least 30, structure room at least 28 when supplied,
directional imbalance at least 22 lakh or at least 12 lakh plus 8 lakh cumulative
shift, the specified 5m/15m move, price no more than eight points against the
session proxy, and at least one qualifying option/writer quadrant. An unavailable
structure retains the stricter 50-lakh, two-quadrant, 35-point-room fallback.
Missing wall room rejects rather than using the attachment's invented 50-point
default. All supplied numeric trend inputs must be finite.

For accumulation entries only, premium edge uses max(abs(5m move), 0.85 times
abs(verified 15m move)). The helper default remains 0.95R; level and Sensex
entries retain their existing effective impulse. The separate generic wall
check uses the same 30-point floor for ready-structure accumulation, so it does
not silently reinstate the older 35-point floor. Opposing-wall/price vetoes,
theta/spread/learning gates, daily target, hard Nifty target, trading window,
cooldowns, entry cap and consecutive-loss breaker remain in force.

121 index/structure/learning tests passed. New tests cover actual elapsed-time
history, weak one-quadrant signals in both directions, ready-between-level
entries at boundary room, and rejection of missing room/hard retest obstacles.
The backtests increased frequency but produced substantial net losses; this
revision fails the requested profit objectives. No live session was launched
or restarted as part of this change. See the latest research section.

## Current structural-only correction (October 25, 2026)

ACCUMULATION_BREAKOUT_V1 execution and its effective 15m impulse boost are
removed. Nifty must have the LEVEL_CANDLE_AND_STRIKE_OI_V1 model, allowed
structure, nonempty same-strike confirmation, a verified three-strike OI
reference, and an actual OI imbalance of at least 50 lakh. The existing
opposing-wall/price veto enforces direction. Structure room/RR, premium, theta,
liquidity, learning and all daily/session limits remain in force. The Sensex
entry path is preserved; the attachment's unconditional not-level gate would
incorrectly block Sensex, which does not use the Nifty-only level model.

Session and 15-minute observations remain telemetry; they cannot authorize
entries between levels or boost the premium-edge estimate. Missing, waiting
or rejected structure holds Nifty. Confirmed retests still use existing
LEVEL_V1 quality/alignment checks; no additional retest ban was specified by
the correction. No production learning reset or live restart was performed.
122 index/structure/learning checks pass, including rejection of strong trend
signals without structure, sub-50-lakh actual imbalances and unverified OI.


2026-10-28: current-setup index lot sizing

Normal PAPER index entries now pass current analysis into compute_lots. This
allows two lots without six days of learning when structure is ready/allowed,
matching-strike OI confirms, confirmation age is at most 120 seconds and the
OI reference is 270-450 seconds old. Scale-up also requires structural RR >=2,
room >=25 Nifty points or 60 Sensex points, body >=60%, directional close >=75%,
real absolute delta 0.35-0.75, spread <=1.5% and <=20% of option stop distance,
and displayed bid/ask depth covering the entire position.

Three lots additionally require RR >=3, body >=70%, close strength >=85% and
room >=40 Nifty points or 100 Sensex points. Both sizes remain capped by 60%
wallet allocation, three lots overall and modeled stop loss plus estimated
costs <=5% of the net virtual wallet. Gaps can exceed that modeled risk.
A consecutive loss or the setup's last-lot loss still restricts sizing to one.
Missing or weak current evidence uses one lot rather than overriding entry
filters. The historical-only sizing path remains conservative when decision-time
quote/candle evidence is unavailable; it does not validate the new live sizing.
Telegram-derived entries cannot enlarge size beyond this current-setup decision.
Selection diagnostics are retained under market_structure.lot_sizing. These
are evidence thresholds, not calibrated win probabilities or profit guarantees.
Stock option sizing and daily index trade/profit caps are unchanged.

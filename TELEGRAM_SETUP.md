# Telegram trade alerts

The session launcher uses `dhan_bot_engine.py`. Telegram is integrated in that
engine's intraday PAPER and LIVE sessions. Historical backtests do not send trade alerts.

1. Open https://t.me/BotFather in Telegram, send `/newbot`, and follow the prompts.
2. Add your new bot to your Telegram channel as an administrator with permission
   to post messages.
3. Fill in the two empty settings in the local `.env` file:

   ```dotenv
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=@your_public_channel_username
   ```

   For a private channel, use its numeric chat ID (usually starts with `-100`).
   You can find it in the `channel_post.chat.id` field returned by Telegram's
   `getUpdates` method after posting in the channel with the bot added. Avoid
   sharing your token or putting token-bearing URLs into screenshots or logs.
   Environment variables override the `.env` values for these two settings.

4. Start the bot normally with `python run_bot_session.py`, or restart it if it
   is already running. The log will say `Telegram alerts enabled` once both
   settings are present. This indicates configuration, not verified delivery.

Alerts contain the mode (PAPER/LIVE), contract, lots, quantity, entry price,
latest price, stop loss, peak price, and unrealized gross P&L. An entry alert is
queued when the engine opens its position. A new update is sent approximately
every 5 seconds while that position is open. The exit alert includes the exit
reason, prices, gross P&L, estimated net P&L, and R multiple.

The background sender uses the engine's latest snapshot without making any
additional Dhan requests. `Price observed` shows when the latest snapshot was
received; prices can be stale during API delays. Telegram/network delays and
rate limits can delay or prevent delivery. Failed messages are logged without
the token and are not automatically retried. The queue is in memory and does
not survive a forced shutdown. No alerts are sent while there is no open trade,
apart from entry/exit events.

Alerts reflect the existing engine's position and ledger events. LIVE order
fills are not independently verified by this notification integration.

API reference: https://core.telegram.org/bots/api#sendmessage


Authorised two-channel PAPER feed (29 October 2026 onward):
Native Nifty/Sensex signals remain enabled. Channel calls are additional entry candidates under the same balance, session cutoff, position ownership and daily limits. GrowX and Tradewithsac are restricted to their verified channel IDs. Fresh calls expire after five minutes; reposts and activation/target replies do not refresh their entry age. GrowX NEAR LEVEL ranges and dash-separated targets, and Tradewithsac T1/T2/T3 activation targets are supported. Follow-up targets must match both source and contract.
One-time setup: obtain your own API ID/hash at https://my.telegram.org > API development tools. Put TELEGRAM_API_ID and TELEGRAM_API_HASH in the existing ignored .env. Run installed Python with -B telegram_notifier.py --connect-telegram and enter your phone/code/2FA only in that local terminal. The existing .env receives TELEGRAM_SESSION_STRING; no separate session file is used and no secret is printed. Telethon 1.45 is installed. Keep TELEGRAM_CALLS_PAPER=true. Account connection failure is reported and blocks channel entries; native PAPER signals continue. GrowX is unavailable through public fallback. Never publish .env or session strings.


29 October 2026 - channel execution corrections:
Channel positions now track and persist executable peak/stop on every valid depth observation. At half the smaller of first-target distance and original risk, arm a fee/slippage-aware breakeven stop; at 80% protect half the modeled peak net. Stops never loosen. Only an executable bid reaching the numeric target books a target exit; a channel target-hit claim or candle high is not a fabricated fill. Held channel positions use a four-second held-contract quote/depth poll instead of full-chain requests; available bid levels are walked for the whole quantity. Native strategy analysis remains on full chains. Telegram displays observed LTP separately from executable bid and peak. All still-fresh calls for the index are evaluated newest-first until one is executable, rather than a newer untriggered alert hiding an older eligible call. Shared capital/position/daily limits and five-minute freshness remain. Partial depth is never used as a full-chain historical observation.


30 October 2026: Fresh executable channel alerts are evaluated while flat even during the native post-exit cooldown. Native signals retain that cooldown. Channel alerts retain the original five-minute freshness limit, duplicate prevention, quote/depth checks, and all shared session, cash, daily entry and consecutive-loss gates. The exception does not allow simultaneous index positions or replay of expired alerts.


30 October 2026 adaptive PAPER channel feedback: closed index ledger outcomes are grouped by index, channel source and CE/PE, rather than unique dated alert keys. The same chronological sample, validation and temporary-pause rules as stock setup feedback apply. Fresh candidates are prioritised using bounded validated feedback; other channels remain eligible when one source/side is paused. Every original freshness, cash, depth and daily risk check remains mandatory. A validated giveback policy can arm existing channel profit protection earlier; targets and fixed initial stops are not widened. Evidence and decisions are retained in market_structure.adaptive_feedback.


2 November 2026 pending-call lifecycle: an original alert received within five minutes may remain pending for up to thirty minutes. It retains its original timestamp and key. A fresh TRADE ACTIVATED message with matching source, index, strike and CE/PE can open one five-minute entry window from activation. Target updates and activation reposts do not renew it; cancellation removes eligibility. Alerts first retrieved late cannot be revived. Pending receipt evidence is saved in the existing index ledger pending_channel_calls field and restored on restart. Every engine freshness check uses this lifecycle, while current expiry, trigger/chase cap, quote/depth, duplicate-entry and daily risk checks remain mandatory. An already-risen option is not entered merely because activation arrived.


2 November 2026 entry/exit refinement: promptly received original alerts remain executable-trigger candidates for their bounded thirty-minute pending lifetime, before any activation message. Fresh matching activation still provides one five-minute window; late first retrievals, cancelled/consumed calls, repeated activation renewal and excessive chase remain blocked. Channel spreads must consume no more than 20% of the fixed initial premium stop. With at least two lots and at least two numeric targets, T1 books whole lots once (half, rounded down), retains at least a cost/slippage floor on the remainder, and T2 exits the remainder; one-lot or single-target calls book T1 in full. Partial fills are persisted in the existing ledger and final accounting reconciles original quantity, additional exit fees and prior realised cash exactly once. Native fixed-point partial rules do not apply to channel positions.


3 November 2026 channel entry format/execution correction: both approved sources accept BUY ABOVE and BUY NEAR (colon optional); GrowX NEAR LEVEL remains supported. An explicit two-price range is never widened. A single NEAR price uses a one-percent zone on either side, capped at 2 Nifty premium points or 4 Sensex points (20% of the existing fixed stop). Quote selection and adverse modeled-fill checks use the same upper boundary. This is an execution tolerance, not permission to chase an expired move. Channel-directed contracts accept finite correctly signed delta with 0 < abs(delta) <= 1; native strategy delta selection is unchanged. Missing/nonfinite Greeks, incorrect delta sign, stale quotes, insufficient depth/cash, consumed calls and shared daily limits remain blocking. Delta and zone rejection logs include observed values; unsupported BUY messages containing an index contract produce CHANNEL FORMAT REJECT instead of silently disappearing. Exact 3 November messages, activation matching, execution room, strict ranges and negative cases are covered in existing tests.


4 November 2026 lifecycle audit (supersedes the 2 November pre-activation entry policy): Tradewithsac alerts are pending until an explicit matching TRADE ACTIVATED message from that source. BUY ABOVE/BUY NEAR alone never authorises SAC entry; this also handles its separate unqualified Wait for trigger reply. The original alert must be received within five minutes, remain within its thirty-minute pending lifetime, and activation must be received promptly. Executable eligibility lasts five minutes from the first valid activation; reposts, target-only updates and repeated activation never renew it. GrowX posted entry calls require no activation and expire five minutes from original posting. Restored legacy SAC pending records obey the same rule even if they lack the new requires_activation field. Consumed identities remain consumed; no re-entry is manufactured for today's earlier incorrect entry. Pending/consumed/cancelled/expired states appear in channel diagnostics. Events are supplemented by a five-second reconciliation of recent messages; reconnect attempts back off from five to sixty seconds, with channel entries blocked while unavailable. New observations never overwrite original receipt evidence. Receipt latency is logged. Old historical messages no longer generate unsupported-format warnings. Channel positions may use at most the native engine's risk-sized lot count; wallet and displayed-depth checks can reduce that count, and any prior loss or last-lot-loss guard caps it at one. A zero native size blocks the channel entry rather than being promoted to one lot.


5 November 2026 requested mode change: TELEGRAM_CALLS_PAPER=false intentionally disables GrowX and Tradewithsac trade candidates. Native Nifty/Sensex signals remain enabled, and trade/report notifications still use the configured notification bot. The authorised account credentials and parser remain available but are not used to open channel-driven trades while disabled. Startup health checks respect this switch and do not require CHANNEL FEED READY in disabled mode. This supersedes earlier instructions to enable both native and channel candidates. No original consumed-call identities or ledger balances were cleared.

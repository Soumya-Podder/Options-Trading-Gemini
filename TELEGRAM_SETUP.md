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

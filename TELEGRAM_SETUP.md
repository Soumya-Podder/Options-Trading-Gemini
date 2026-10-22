# Telegram trade alerts

The session launcher uses `dhan_bot_engine.py`. Telegram is integrated in that
engine's intraday PAPER and LIVE sessions only. Historical backtests and the
older `dhan_self_learning_bot.py` are unaffected.

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

"""Background Telegram alerts; never performs broker operations."""
import atexit
import os
import queue
import threading
import time
from datetime import datetime, timezone, timedelta

import requests


def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S IST')


class TelegramNotifier:
    INTERVAL = 5.0

    def __init__(self, env, logger, mode):
        self.logger, self.mode = logger, mode
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN', env.get('TELEGRAM_BOT_TOKEN', '')).strip()
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID', env.get('TELEGRAM_CHAT_ID', '')).strip()
        self.enabled = bool(self.token and self.chat_id)
        self._events = queue.Queue()
        self._lock = threading.Lock()
        self._position = None
        self._next_update = float('inf')
        self._closing = threading.Event()
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._run, name='telegram-alerts', daemon=True)
            self._thread.start()
            atexit.register(self.close)
            logger.info('Telegram alerts enabled: entry, 5-second position updates, exit.')
        else:
            logger.info('Telegram alerts disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.')

    def _details(self, p):
        pnl = (p['current_ltp'] - p['entry_price']) * p['qty']
        return (
            f"{p['index']} {p['strike']} {p['option_type']} | BUY\n"
            f"Lots: {p['lots']} | Quantity: {p['qty']}\n"
            f"Entry: Rs.{p['entry_price']:.2f} | LTP: Rs.{p['current_ltp']:.2f}\n"
            f"Stop loss: Rs.{p['current_sl']:.2f} | Peak: Rs.{p['highest_ltp']:.2f}\n"
            f"Unrealized gross P&L: Rs.{pnl:+,.2f}\n"
            f"Entry time (engine): {p['entry_ts']}\n"
            f"Price observed: {p['_observed_at']}"
        )

    def position(self, position, entry=False):
        if not self.enabled:
            return
        snapshot = dict(position, _observed_at=ist_now())
        with self._lock:
            self._position = snapshot
            if entry:
                self._next_update = time.monotonic() + self.INTERVAL
                self._events.put(f'[{self.mode}] TRADE ENTRY\n{self._details(snapshot)}\nEngine event; broker fill not independently verified.')

    def exited(self, trade):
        if not self.enabled:
            return
        with self._lock:
            self._position = None
            self._next_update = float('inf')
            self._events.put(
                f"[{self.mode}] TRADE EXIT\n{trade['index']} {trade['contract']}\n"
                f"Lots: {trade['lots']} | Quantity: {trade['qty']}\n"
                f"Entry: Rs.{trade['entry_price']:.2f} | Exit: Rs.{trade['exit_price']:.2f}\n"
                f"Gross P&L: Rs.{trade['gross_pnl']:+,.2f}\n"
                f"Net P&L (engine estimate): Rs.{trade['net_pnl']:+,.2f}\n"
                f"Result: {trade['r_multiple']:+.2f}R | Reason: {trade['exit_reason']}\n"
                f"Exit time (engine): {trade['exit_timestamp']}\n"
                'Engine event; broker fill not independently verified.'
            )

    def _send(self, message):
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat_id, 'text': message}, timeout=(3, 4),
            )
            body = response.json()
            if response.status_code == 429:
                delay = max(1, int(body.get('parameters', {}).get('retry_after', 5)))
                self.logger.warning('Telegram rate limited; pausing alerts for %s seconds.', delay)
                self._closing.wait(delay)
            if not response.ok or not body.get('ok'):
                self.logger.warning('Telegram delivery failed (HTTP %s). Check token and channel permissions.', response.status_code)
        except Exception:
            # Request exception strings can contain the token-bearing URL.
            self.logger.warning('Telegram delivery failed; check connectivity and Telegram configuration.')

    def _run(self):
        while not self._closing.is_set() or not self._events.empty():
            try:
                message = self._events.get(timeout=0.2)
            except queue.Empty:
                message = None
            if message is None and not self._closing.is_set():
                with self._lock:
                    if self._position and time.monotonic() >= self._next_update:
                        message = f'[{self.mode}] OPEN TRADE UPDATE\n{self._details(self._position)}\nUpdate: {ist_now()}'
                        self._next_update = time.monotonic() + self.INTERVAL
            if message:
                self._send(message)

    def close(self):
        self._closing.set()
        if self._thread:
            self._thread.join(timeout=8)

import logging
import threading
import unittest
from datetime import datetime

from dhan_bot_engine import channel_entry_view
from telegram_notifier import TelegramCallFeed


class ChannelReplayTests(unittest.TestCase):
    def setUp(self):
        self.posted = datetime.fromisoformat("2026-11-19T09:23:23+05:30").timestamp()
        self.url = "https://t.me/c/replay/1"
        self.feed = TelegramCallFeed.__new__(TelegramCallFeed)
        self.feed._lock = threading.Lock()
        self.feed._calls = []
        self.feed._target_updates = []
        self.feed.logger = logging.getLogger(__name__)
        self.call = self.feed.ingest("NIFTY 25150 PE BUY NEAR 150", self.posted,
                                     self.url, self.posted + 1, "Tradewithsac")

    def candidate(self, seconds):
        return self.feed.candidate("NIFTY", self.posted + seconds, ())

    def test_original_window_eligible_but_actual_late_entry_expired(self):
        self.assertIsNotNone(self.candidate(3))
        self.assertIsNone(self.candidate(735))

    def test_targetless_call_retires_on_linked_reply(self):
        self.assertEqual(self.call["targets"], [])
        self.feed.ingest("Target 1 achieved", self.posted + 30, self.url + "/reply",
                         self.posted + 31, "Tradewithsac", reply_url=self.url)
        self.assertIsNone(self.candidate(32))
        self.assertFalse(self.feed.still_eligible(dict(self.call), self.posted + 32))

    def test_unrelated_reply_preserves_candidate(self):
        self.feed.ingest("Target 1 achieved", self.posted + 30, self.url + "/reply",
                         self.posted + 31, "Tradewithsac", reply_url="other")
        self.assertIsNotNone(self.candidate(32))

    def test_observed_impulse_retires_even_after_price_returns(self):
        now = self.posted + 15
        candidate = self.candidate(15)
        node = {"top_bid_price": 164.30, "top_ask_price": 164.50, "last_price": 164.30}
        view, reason = channel_entry_view({"index": "NIFTY"}, {"25150": {"pe": node}},
                                          candidate, "2026-11-23", now, now, feed=self.feed)
        self.assertIsNone(view)
        self.assertEqual(reason, "CHANNEL_CALL_RETIRED_MISSED_IMPULSE")
        self.assertIsNone(self.candidate(20))

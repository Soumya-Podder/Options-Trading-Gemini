"""Background Telegram alerts; never performs broker operations."""
import atexit
import html
import re
import os
import math
import queue
import threading
import time
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

import requests


class _PublicChannelHTML(HTMLParser):
    """Extract public post text and timestamps; never execute channel content."""
    def __init__(self):
        super().__init__()
        self.posts=[]
        self.post=None
        self.depth=0
        self.text_depth=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='div':
            self.depth+=1
            if attrs.get('data-post'):
                self.post={'post':attrs['data-post'],'text':'','timestamp':None}
                self.posts.append(self.post)
            if self.post and 'tgme_widget_message_text' in attrs.get('class','').split():
                self.text_depth=self.depth
        if tag=='time' and self.post:
            self.post['timestamp']=attrs.get('datetime')
        if tag=='br' and self.text_depth is not None:
            self.post['text']+='\n'

    def handle_endtag(self, tag):
        if tag=='div':
            if self.text_depth==self.depth:self.text_depth=None
            self.depth-=1

    def handle_data(self, value):
        if self.post and self.text_depth is not None:self.post['text']+=value


class TelegramCallFeed:
    """Public channel candidates only. Broker decisions remain in the PAPER engine."""
    CHANNEL='tradewith_sac'
    MAX_AGE=300

    def __init__(self, logger):
        self.logger=logger
        self._lock=threading.Lock()
        self._calls=[]
        self._closing=threading.Event()
        self._thread=threading.Thread(target=self._run,name='public-telegram-calls',daemon=True)
        self._thread.start()
        atexit.register(self.close)

    @classmethod
    def parse(cls, page, now_epoch):
        parser=_PublicChannelHTML()
        parser.feed(page)
        calls={}
        ist=timezone(timedelta(hours=5,minutes=30))
        today=datetime.fromtimestamp(now_epoch,ist).date()
        for post in parser.posts:
            try:
                if not re.fullmatch(cls.CHANNEL+r'/\d+',post['post']):continue
                text=post['text'].upper()
                contract=re.search(r'\b(NIFTY|SENSEX)\s+(\d+(?:\.\d+)?)\s+(CE|PE)\b',text)
                entry=re.search(r'\bBUY\s+ABOVE\s*:\s*(?:₹|RS\.?|INR)?\s*(\d+(?:\.\d+)?)',text)
                if not contract or not entry:continue
                posted=datetime.fromisoformat(post['timestamp'].replace('Z','+00:00'))
                if posted.tzinfo is None or posted.astimezone(ist).date()!=today:continue
                price=float(entry[1]);strike=float(contract[2])
                if not all(math.isfinite(v) and v>0 for v in (price,strike)):continue
                # Channel calls use current expiry; posts cannot choose upcoming contracts.
                expiry=None
                key=f'{today}:{contract[1]}:{strike:g}:{contract[3]}:{price:g}:CURRENT'
                call={'key':key,'index':contract[1],'strike':strike,'option_type':contract[3],
                      'buy_above':price,'posted_epoch':posted.timestamp(),'expiry':expiry,
                      'source_url':'https://t.me/'+post['post']}
                if key not in calls or call['posted_epoch']<calls[key]['posted_epoch']:calls[key]=call
            except (TypeError,ValueError,AttributeError,OverflowError):continue
        return [c for c in calls.values() if 0<=now_epoch-c['posted_epoch']<=cls.MAX_AGE]

    def candidate(self, index, now_epoch, consumed):
        with self._lock:
            matches=[dict(c) for c in self._calls if c['index']==index and c['key'] not in consumed
                     and 0<=now_epoch-c['posted_epoch']<=self.MAX_AGE]
        return max(matches,key=lambda c:c['posted_epoch'],default=None)

    def _run(self):
        previous=None
        while not self._closing.is_set():
            try:
                response=requests.get('https://t.me/s/'+self.CHANNEL,timeout=(3,4))
                response.raise_for_status()
                calls=self.parse(response.text,time.time())
                with self._lock:self._calls=calls
                keys=tuple(c['key'] for c in calls)
                if keys!=previous:
                    self.logger.info('[TELEGRAM CALLS] %d fresh public candidates; PAPER checks required',len(calls))
                    previous=keys
            except Exception:
                with self._lock:self._calls=[]
                self.logger.warning('Public Telegram feed unavailable; channel entries withheld')
            self._closing.wait(10)

    def close(self):
        self._closing.set()
        self._thread.join(timeout=8)


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

    @staticmethod
    def _bold_company(message, company):
        escaped=html.escape(str(message))
        name=html.escape(str(company))
        return escaped.replace(name,f"<b>{name}</b>",1) if name else escaped

    def _details(self, p):
        def valid(value):
            try:return math.isfinite(float(value)) and float(value)>0
            except (TypeError,ValueError,OverflowError):return False
        ltp=p.get('current_ltp');bid=p.get('current_bid');ask=p.get('current_ask')
        ltp_text=f"Rs.{float(ltp):.2f}" if valid(ltp) else "unavailable"
        quotes=(f"\nBid: Rs.{float(bid):.2f}" if valid(bid) else "\nBid: unavailable")
        quotes+=(f"\nAsk: Rs.{float(ask):.2f}" if valid(ask) else "\nAsk: unavailable")
        valuation=bid if valid(bid) else ltp
        label='Gross P&L'
        valuation_basis='Bid' if valid(bid) else 'LTP'
        pnl=round((float(valuation)-p['entry_price'])*p['qty']+p.get('partial_gross',0),2) if valid(valuation) else None
        pnl_text=(f"{chr(0x1F7E2) if pnl>0 else chr(0x1F534)} Rs.{pnl:+,.2f}" if pnl is not None else 'unavailable')
        expiry=f"\nExpiry: {p['expiry']}" if p.get('expiry') else ''
        source=((p.get('market_structure') or {}).get('telegram_call') or {}).get('source_url')
        source_text=f'\nSource: Telegram @tradewith_sac\nAlert: {source}\nExit plan: Bot managed' if source else ''
        partial_text=(f"\nOriginal quantity: {p['original_qty']}\nBooked quantity: {p['original_qty']-p['qty']}"
                      f"\nBooked net: {chr(0x1F7E2) if p.get('partial_net',0)>0 else chr(0x1F534)} Rs.{p.get('partial_net',0):+,.2f}"
                      if p.get('partial_booked') else '')
        cost_note=('Charges: Excluded from gross P&L\nBooked fills: Modeled slippage applied'
                   if p.get('partial_booked') else 'Charges/slippage: Excluded')
        stamp=p.get('quote_received_epoch')
        age=f"\nQuote age: {max(0,time.time()-float(stamp)):.0f}s" if stamp is not None else ''
        stale='\nStatus: [STALE]' if stamp is not None and time.time()-float(stamp)>30 else ''
        message = (
            f"{p['index']}\nStrike: {p['strike']:g}\nOption: {p['option_type']}\nSide: BUY{expiry}{source_text}\n"
            f"Lots: {p['lots']}\nQuantity: {p['qty']}{partial_text}\n\n"
            f"Entry: Rs.{p['entry_price']:.2f}\nLTP: {ltp_text}{quotes}\n"
            f"Stop loss: Rs.{p['current_sl']:.2f}\nPeak: Rs.{p.get('highest_ltp',p.get('peak_bid',p['entry_price'])):.2f}\n\n"
            f"{label}: {pnl_text}\nValuation: {valuation_basis}\n{cost_note}\n\n"
            f"Entry time (engine): {p['entry_ts']}\n"
            f"Price observed: {p['_observed_at']}{age}{stale}"
        )
        return self._bold_company(message,p['index'])

    def partial(self, position, fill):
        if not self.enabled:return
        self.position(position)
        self._events.put(self._bold_company(
            f"[{self.mode}] PARTIAL PROFIT BOOKED\n{position['index']}\n"
            f"Quantity sold: {fill['qty']}\nExit: Rs.{fill['price']:.2f}\n"
            f"Booked net: {chr(0x1F7E2) if fill['booked_net']>0 else chr(0x1F534)} Rs.{fill['booked_net']:+,.2f}\n"
            f"Remaining lots: {position['lots']}\nRemaining quantity: {position['qty']}\n"
            f"Time: {ist_now()}",position['index']))

    def position(self, position, entry=False):
        if not self.enabled:
            return
        stamp=position.get('quote_received_epoch')
        observed=(datetime.fromtimestamp(float(stamp),timezone(timedelta(hours=5,minutes=30))).strftime('%Y-%m-%d %H:%M:%S IST')
                  if stamp is not None else ist_now())
        snapshot = dict(position, _observed_at=observed)
        with self._lock:
            if entry or self._position is None:
                self._next_update = time.monotonic() + self.INTERVAL
            self._position = snapshot
            if entry:
                self._events.put(f'[{self.mode}] TRADE ENTRY\n{self._details(snapshot)}\nEngine event; broker fill not independently verified.')

    def exited(self, trade):
        if not self.enabled:
            return
        with self._lock:
            self._position = None
            self._next_update = float('inf')
            self._events.put(self._bold_company(
                f"[{self.mode}] TRADE EXIT\n{trade.get('index', trade.get('symbol', ''))}\nContract: {trade['contract']}\n"
                f"Lots: {trade['lots']}\nQuantity: {trade['qty']}\n\n"
                f"Entry: Rs.{trade['entry_price']:.2f}\nExit: Rs.{trade['exit_price']:.2f}\n"
                f"Gross P&L: {chr(0x1F7E2) if trade['gross_pnl']>0 else chr(0x1F534)} Rs.{trade['gross_pnl']:+,.2f}\n"
                f"Net P&L: {chr(0x1F7E2) if trade['net_pnl']>0 else chr(0x1F534)} Rs.{trade['net_pnl']:+,.2f}\n"
                f"Result: {trade.get('r_multiple', trade.get('r_mult', 0.0)):+.2f}R\nReason: {trade['exit_reason']}\n"
                f"Exit time (engine): {trade['exit_timestamp']}\n"
                'Engine event; broker fill not independently verified.',
                trade.get('index',trade.get('symbol',''))
            ))

    def _send(self, message):
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat_id, 'text': message, 'parse_mode':'HTML'}, timeout=(3, 4),
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

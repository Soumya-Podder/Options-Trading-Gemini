"""Background Telegram alerts; never performs broker operations."""
import atexit
import asyncio
import json
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
    SOURCES={-1003882006811:'Tradewithsac',-1002302217039:'GrowX'}
    MAX_AGE=300

    def __init__(self, logger, env=None):
        self.env=dict(env or {})
        self.connection_status='STARTING'
        self._authorized_mode=bool(self.env.get('TELEGRAM_SESSION_STRING'))
        self.logger=logger
        self._lock=threading.Lock()
        self._calls=[]
        self._target_updates=[]
        self._closing=threading.Event()
        self._thread=threading.Thread(target=self._run,name='public-telegram-calls',daemon=True)
        self._thread.start()
        atexit.register(self.close)

    @staticmethod
    def numeric_targets(text):
        text=text.upper()
        numbered=re.findall(r'\bT[1-9]\s*:\s*(?:₹|RS\.?|INR)?\s*(\d+(?:\.\d+)?)',text)
        match=re.search(r'\b(?:TARGETS?|TGTS?)\s*(?::|--+)\s*((?:(?:₹|RS\.?|INR)?\s*\d+(?:\.\d+)?\s*[/,\-+ ]*\s*)+)',text)
        values=numbered or (re.findall(r'\d+(?:\.\d+)?',match[1]) if match else [])
        prices=sorted(set(float(v) for v in values))
        return prices if all(math.isfinite(v) and v>0 for v in prices) else []

    @classmethod
    def parse_message(cls,text,posted_epoch,source_url,now_epoch,source='Tradewithsac'):
        if source not in cls.SOURCES.values():return None,None
        try:
            text=text.upper();posted_epoch=float(posted_epoch)
            ist=timezone(timedelta(hours=5,minutes=30))
            day=datetime.fromtimestamp(posted_epoch,ist).date()
            if day!=datetime.fromtimestamp(now_epoch,ist).date() or not 0<=now_epoch-posted_epoch<=86400:return None,None
            contract=re.search(r'\b(NIFTY|SENSEX)\s+(\d+(?:\.\d+)?)\s+(CE|PE)\b',text)
            if not contract:return None,None
            common={'index':contract[1],'strike':float(contract[2]),'option_type':contract[3],
                    'posted_epoch':posted_epoch,'source':source,'source_url':source_url}
            if common['strike']<=0:return None,None
            targets=cls.numeric_targets(text)
            update={**common,'targets':targets} if targets else None
            entry=re.search(r'\bBUY\s+ABOVE\s*:\s*(?:₹|RS\.?|INR)?\s*(\d+(?:\.\d+)?)',text)
            zone=re.search(r'\bNEAR\s+LEVEL\s*--+\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?',text)
            if re.search(r'\b(?:ACTIVATED|HIT|CANCELLED|CANCELED)\b',text):return None,update
            if not entry and not (source=='GrowX' and zone):return None,update
            price=float(entry[1] if entry else zone[1])
            upper=float(zone[2] or zone[1]) if not entry else None
            if not math.isfinite(price) or price<=0 or (upper is not None and (not math.isfinite(upper) or upper<price)):return None,update
            key=f"{day}:{common['index']}:{common['strike']:g}:{common['option_type']}:{price:g}:CURRENT"
            call={**common,'key':key,'buy_above':price,'expiry':None,'targets':targets,
                  'entry_kind':'ABOVE' if entry else 'RANGE'}
            if upper is not None:call['entry_max']=upper
            return call,update
        except (TypeError,ValueError,OverflowError,OSError):return None,None

    @classmethod
    def parse(cls,page,now_epoch):
        parser=_PublicChannelHTML();parser.feed(page);calls={}
        for post in parser.posts:
            try:
                if not re.fullmatch(cls.CHANNEL+r'/\d+',post['post']):continue
                posted=datetime.fromisoformat(post['timestamp'].replace('Z','+00:00')).timestamp()
                call,_=cls.parse_message(post['text'],posted,'https://t.me/'+post['post'],now_epoch)
                if call and (call['key'] not in calls or posted<calls[call['key']]['posted_epoch']):calls[call['key']]=call
            except (TypeError,ValueError,KeyError):continue
        return [c for c in calls.values() if 0<=now_epoch-c['posted_epoch']<=cls.MAX_AGE]

    @classmethod
    def target_updates(cls,page,now_epoch):
        parser=_PublicChannelHTML();parser.feed(page);updates=[]
        for post in parser.posts:
            try:
                if not re.fullmatch(cls.CHANNEL+r'/\d+',post['post']):continue
                posted=datetime.fromisoformat(post['timestamp'].replace('Z','+00:00')).timestamp()
                _,update=cls.parse_message(post['text'],posted,'https://t.me/'+post['post'],now_epoch)
                if update:updates.append(update)
            except (TypeError,ValueError,KeyError):continue
        return updates

    def ingest(self,text,posted_epoch,source_url,now_epoch,source):
        call,update=self.parse_message(text,posted_epoch,source_url,now_epoch,source)
        with self._lock:
            if call:
                old=next((c for c in self._calls if c['key']==call['key']),None)
                if old is None:self._calls.append(call)
                elif call['posted_epoch']<old['posted_epoch']:
                    self._calls.remove(old);self._calls.append(call)
            if update:
                self._target_updates=[u for u in self._target_updates if u.get('source_url')!=source_url]+[update]
            self._calls=[c for c in self._calls if 0<=now_epoch-c['posted_epoch']<=86400][-256:]
            self._target_updates=[u for u in self._target_updates if 0<=now_epoch-u['posted_epoch']<=86400][-256:]
        if call and old is None:
            self.logger.info('[CHANNEL CALL RECEIVED] %s | %s',source,json.dumps(call,ensure_ascii=True))
        return call

    def targets_for(self,call,now_epoch):
        with self._lock:
            matches=[u for u in self._target_updates if all(u[k]==call[k] for k in ('index','strike','option_type'))
                     and u.get('source','Tradewithsac')==call.get('source','Tradewithsac')
                     and u['posted_epoch']>=call['posted_epoch'] and 0<=now_epoch-u['posted_epoch']<=86400]
        return max(matches,key=lambda u:u['posted_epoch'])['targets'] if matches else []

    def candidates(self, index, now_epoch, consumed):
        if getattr(self,'_authorized_mode',False) and self.connection_status!='CONNECTED':return []
        with self._lock:
            matches=[dict(c) for c in self._calls if c['index']==index and c['key'] not in consumed
                     and 0<=now_epoch-c['posted_epoch']<=self.MAX_AGE]
        return sorted(matches,key=lambda c:c['posted_epoch'],reverse=True)

    def candidate(self, index, now_epoch, consumed):
        return next(iter(self.candidates(index,now_epoch,consumed)),None)

    async def _account_feed(self):
        from telethon import TelegramClient,events
        from telethon.sessions import StringSession
        client=TelegramClient(StringSession(self.env['TELEGRAM_SESSION_STRING']),
                              int(self.env['TELEGRAM_API_ID']),self.env['TELEGRAM_API_HASH'])
        try:
            await client.connect()
            if not await client.is_user_authorized():raise RuntimeError('Telegram sign-in required')
            entities={d.id:d.entity async for d in client.iter_dialogs(limit=200) if d.id in self.SOURCES}
            missing=set(self.SOURCES)-set(entities)
            if missing:raise RuntimeError('One or more approved channels unavailable to this account')
            def accept(message,channel_id):
                if channel_id not in self.SOURCES or not message.date:return
                url=f'https://t.me/c/{str(abs(channel_id))[3:]}/{message.id}'
                self.ingest(message.raw_text or '',message.date.timestamp(),url,time.time(),self.SOURCES[channel_id])
            async def receive(event):accept(event.message,event.chat_id)
            client.add_event_handler(receive,events.NewMessage(chats=list(entities.values())))
            client.add_event_handler(receive,events.MessageEdited(chats=list(entities.values())))
            for channel_id,entity in entities.items():
                messages=await client.get_messages(entity,limit=100)
                for message in reversed(messages):accept(message,channel_id)
            self.connection_status='CONNECTED'
            self.logger.info('[CHANNEL FEED READY] Authorised account: GrowX and Tradewithsac; PAPER only')
            while not self._closing.is_set():
                if not client.is_connected():raise RuntimeError('Telegram account feed disconnected')
                await asyncio.sleep(1)
        finally:
            self.connection_status='DISCONNECTED'
            await client.disconnect()

    def _run(self):
        if self._authorized_mode:
            try:asyncio.run(self._account_feed())
            except Exception as error:
                self.connection_status='UNAVAILABLE'
                self.logger.error('[CHANNEL FEED UNAVAILABLE] %s; native PAPER signals remain enabled',type(error).__name__)
            return
        self.connection_status='PUBLIC_ONLY'
        self.logger.warning('[CHANNEL SETUP REQUIRED] GrowX account feed not connected. Native PAPER signals remain enabled.')
        previous=None
        while not self._closing.is_set():
            try:
                response=requests.get('https://t.me/s/'+self.CHANNEL,timeout=(3,4))
                response.raise_for_status()
                calls=self.parse(response.text,time.time())
                with self._lock:
                    self._calls=calls
                    self._target_updates=self.target_updates(response.text,time.time())
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
        source_name=((p.get('market_structure') or {}).get('telegram_call') or {}).get('source','Tradewithsac')
        source_text=f'\nSource: Telegram {source_name}\nAlert: {source}\nExit plan: Channel target; fixed premium SL' if source else ''
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


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='One-time Telegram channel account sign-in')
    parser.add_argument('--connect-telegram',action='store_true')
    args=parser.parse_args()
    if args.connect_telegram:
        from pathlib import Path
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        from dhan_dynamic_fno_stock_bot import load_dotenv_file
        path=Path(__file__).resolve().parent/'.env'
        env=load_dotenv_file(str(path))
        if not env.get('TELEGRAM_API_ID') or not env.get('TELEGRAM_API_HASH'):
            raise SystemExit('Add TELEGRAM_API_ID and TELEGRAM_API_HASH to the existing .env first.')
        with TelegramClient(StringSession(),int(env['TELEGRAM_API_ID']),env['TELEGRAM_API_HASH']) as client:
            session=client.session.save()
            raw=path.read_bytes()
            raw=re.sub(rb'(?m)^TELEGRAM_SESSION_STRING=.*(?:\r?\n|$)',b'',raw)
            with path.open('wb') as stream:stream.write(raw.rstrip(b'\r\n')+b'\r\nTELEGRAM_SESSION_STRING='+session.encode('ascii')+b'\r\n')
        print('Telegram sign-in saved to the existing local .env. No session secret was printed.')

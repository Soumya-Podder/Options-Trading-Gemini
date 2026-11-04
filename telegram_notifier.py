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
    MAX_PENDING_AGE=1800

    def __init__(self, logger, env=None, pending_calls=()):
        self.env=dict(env or {})
        self.connection_status='STARTING'
        self._authorized_mode=bool(self.env.get('TELEGRAM_SESSION_STRING'))
        self.logger=logger
        self._lock=threading.Lock()
        self._calls=[]
        self._target_updates=[]
        self._closing=threading.Event()
        self.restore_pending(pending_calls,time.time())
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
            activated=bool(re.search(r'\bTRADE\s+ACTIVATED\b',text))
            cancelled=bool(re.search(r'\bCANCELLED|\bCANCELED',text))
            update={**common,'targets':targets,'activated':activated,'cancelled':cancelled} if targets or activated or cancelled else None
            entry=re.search(r'\bBUY\s+ABOVE\s*[:\-]*\s*(?:₹|RS\.?|INR)?\s*(\d+(?:\.\d+)?)',text)
            near=re.search(r'\bBUY\s+NEAR\s*[:\-]*\s*(?:₹|RS\.?|INR)?\s*(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?',text)
            zone=re.search(r'\bNEAR\s+LEVEL\s*--+\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?',text)
            if re.search(r'\b(?:ACTIVATED|HIT|CANCELLED|CANCELED)\b',text):return None,update
            if not entry and not near and not (source=='GrowX' and zone):return None,update
            location=near or zone
            price=float(entry[1] if entry else location[1])
            upper=float(location[2]) if not entry and location[2] else None
            if not math.isfinite(price) or price<=0 or (upper is not None and (not math.isfinite(upper) or upper<price)):return None,update
            key=f"{day}:{common['index']}:{common['strike']:g}:{common['option_type']}:{price:g}:CURRENT"
            call={**common,'key':key,'buy_above':price,'expiry':None,'targets':targets,
                  'entry_kind':'ABOVE' if entry else ('RANGE' if upper is not None else 'NEAR'),
                  'requires_activation':source=='Tradewithsac'}
            if upper is not None:call['entry_max']=upper
            return call,update
        except (TypeError,ValueError,OverflowError,OSError):return None,None

    @staticmethod
    def entry_bounds(call):
        """One bounded zone shared by quote validation and modeled-fill validation."""
        price=float(call['buy_above'])
        if call.get('entry_kind')=='NEAR':
            # One percent around the posted level, capped at 20% of fixed SL.
            tolerance=min(price*0.01,2.0 if call['index']=='NIFTY' else 4.0)
            return max(0.05,price-tolerance),price+tolerance
        return price,float(call.get('entry_max',price*1.02))

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
        if 0<=now_epoch-posted_epoch<=self.MAX_AGE and call is None and update is None and re.search(r'\b(?:NIFTY|SENSEX)\s+\d+(?:\.\d+)?\s+(?:CE|PE)\b',text.upper()) and re.search(r'\bBUY\b',text.upper()):
            self.logger.warning('[CHANNEL FORMAT REJECT] %s | %s | Unrecognised entry format; no candidate created',source,source_url)
        with self._lock:
            if call:
                call['first_received_epoch']=now_epoch
                old=next((c for c in self._calls if c['key']==call['key']),None)
                if old is None:self._calls.append(call)
                elif call['posted_epoch']<old['posted_epoch']:
                    self._calls.remove(old);self._calls.append(call)
            if update:
                self._target_updates=[u for u in self._target_updates if u.get('source_url')!=source_url]+[update]
                matching=[c for c in self._calls if all(c[k]==update[k] for k in ('source','index','strike','option_type'))
                          and 0<=update['posted_epoch']-c['posted_epoch']<=self.MAX_PENDING_AGE]
                if matching:
                    pending=max(matching,key=lambda c:c['posted_epoch'])
                    if update.get('cancelled'):pending['cancelled']=True
                    if (update.get('activated') and not pending.get('activation_epoch')
                            and 0<=pending.get('first_received_epoch',float('inf'))-pending['posted_epoch']<=self.MAX_AGE
                            and 0<=now_epoch-update['posted_epoch']<=self.MAX_AGE):
                        pending.update(activation_epoch=update['posted_epoch'],activation_received_epoch=now_epoch,
                                       activation_source_url=source_url)
                        self.logger.info('[CHANNEL ACTIVATION] %s | original alert received promptly; fresh activation entry window',source_url)
            self._calls=[c for c in self._calls if 0<=now_epoch-c['posted_epoch']<=86400][-256:]
            self._target_updates=[u for u in self._target_updates if 0<=now_epoch-u['posted_epoch']<=86400][-256:]
        if call and old is None:
            self.logger.info('[CHANNEL CALL RECEIVED] %s | %s',source,json.dumps({**call,'receipt_latency_seconds':round(now_epoch-posted_epoch,3)},ensure_ascii=True))
        return call

    def targets_for(self,call,now_epoch):
        with self._lock:
            matches=[u for u in self._target_updates if u.get('targets') and all(u[k]==call[k] for k in ('index','strike','option_type'))
                     and u.get('source','Tradewithsac')==call.get('source','Tradewithsac')
                     and u['posted_epoch']>=call['posted_epoch'] and 0<=now_epoch-u['posted_epoch']<=86400]
        return max(matches,key=lambda u:u['posted_epoch'])['targets'] if matches else []

    @classmethod
    def entry_is_fresh(cls,call,now_epoch):
        if call.get('cancelled'):return False
        # SAC announces pending alerts before publishing matching activation.
        if call.get('requires_activation',call.get('source')=='Tradewithsac') and 'activation_epoch' not in call:return False
        if 'activation_epoch' not in call and 0<=call.get('first_received_epoch',float('inf'))-call['posted_epoch']<=cls.MAX_AGE:
            return 0<=now_epoch-call['posted_epoch']<=cls.MAX_AGE
        epoch=call.get('activation_epoch',call['posted_epoch'])
        if 'activation_epoch' in call:
            if not (0<=call.get('first_received_epoch',float('inf'))-call['posted_epoch']<=cls.MAX_AGE
                    and 0<=epoch-call['posted_epoch']<=cls.MAX_PENDING_AGE
                    and 0<=call.get('activation_received_epoch',float('inf'))-epoch<=cls.MAX_AGE):return False
        return 0<=now_epoch-epoch<=cls.MAX_AGE

    def restore_pending(self,calls,now_epoch):
        day=datetime.fromtimestamp(now_epoch,timezone(timedelta(hours=5,minutes=30))).date()
        for call in calls:
            try:
                if (call.get('source') not in self.SOURCES.values() or call.get('cancelled')
                        or not 0<=now_epoch-call['posted_epoch']<=self.MAX_PENDING_AGE
                        or not 0<=call['first_received_epoch']-call['posted_epoch']<=self.MAX_AGE
                        or datetime.fromtimestamp(call['posted_epoch'],timezone(timedelta(hours=5,minutes=30))).date()!=day):continue
                self._calls.append(dict(call))
            except (KeyError,TypeError,ValueError,OverflowError):continue

    def pending_snapshot(self,now_epoch,consumed=()):
        with self._lock:
            return [dict(c) for c in self._calls if c['key'] not in consumed and not c.get('cancelled')
                    and 0<=now_epoch-c['posted_epoch']<=self.MAX_PENDING_AGE
                    and 0<=c.get('first_received_epoch',float('inf'))-c['posted_epoch']<=self.MAX_AGE]

    def candidates(self, index, now_epoch, consumed):
        if getattr(self,'_authorized_mode',False) and self.connection_status!='CONNECTED':return []
        with self._lock:
            matches=[dict(c) for c in self._calls if c['index']==index and c['key'] not in consumed
                     and self.entry_is_fresh(c,now_epoch)]
        return sorted(matches,key=lambda c:c['posted_epoch'],reverse=True)

    def decision_statuses(self,index,now_epoch,consumed):
        with self._lock:
            calls=[dict(c) for c in self._calls if c['index']==index and 0<=now_epoch-c['posted_epoch']<=self.MAX_PENDING_AGE]
        results=[]
        for call in calls:
            if call['key'] in consumed:state='ALREADY_EXECUTED'
            elif call.get('cancelled'):state='CANCELLED'
            elif not 0<=call.get('first_received_epoch',float('inf'))-call['posted_epoch']<=self.MAX_AGE:state='RECEIVED_TOO_LATE'
            elif call.get('requires_activation',call.get('source')=='Tradewithsac') and 'activation_epoch' not in call:state='WAITING_FOR_ACTIVATION'
            elif not self.entry_is_fresh(call,now_epoch):state='EXPIRED'
            else:state='ELIGIBLE_FOR_QUOTE_CHECK'
            results.append({'key':call['key'],'source':call['source'],'source_url':call['source_url'],
                            'state':state,'posted_epoch':call['posted_epoch'],'activation_epoch':call.get('activation_epoch')})
        return results

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
            next_catchup=time.monotonic()+5
            while not self._closing.is_set():
                if not client.is_connected():raise RuntimeError('Telegram account feed disconnected')
                if time.monotonic()>=next_catchup:
                    # Events provide low latency; polling recovers missed/delayed updates.
                    for channel_id,entity in entities.items():
                        messages=await client.get_messages(entity,limit=20)
                        for message in reversed(messages):accept(message,channel_id)
                    next_catchup=time.monotonic()+5
                await asyncio.sleep(0.5)
        finally:
            self.connection_status='DISCONNECTED'
            await client.disconnect()

    def _run(self):
        if self._authorized_mode:
            delay=5
            while not self._closing.is_set():
                try:
                    asyncio.run(self._account_feed())
                    if self._closing.is_set():break
                except Exception as error:
                    self.connection_status='UNAVAILABLE'
                    self.logger.error('[CHANNEL FEED UNAVAILABLE] %s; retry in %ss; channel entries blocked, native PAPER signals enabled',type(error).__name__,delay)
                if self._closing.wait(delay):break
                delay=min(60,delay*2)
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

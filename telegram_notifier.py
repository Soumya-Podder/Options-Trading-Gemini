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
from pathlib import Path
from stock_option_policy import stock_reporting_exclusions

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
        self._positions = {}
        self._next_update = {}
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
    def _price(value):
        try:
            value = float(value)
            return f"{value:.2f}" if math.isfinite(value) and value > 0 else "-"
        except (TypeError, ValueError, OverflowError):
            return "-"

    def _trade_table(self, positions, session_trades=None):
        headers, columns, gross_values = [], [], []
        now = time.time()
        for number, trade in enumerate(positions, 1):
            name = html.escape(str(trade.get("index") or trade.get("symbol") or "Trade"))
            contract = trade.get("contract") or f'{trade.get("strike", "")} {trade.get("option_type", "")}'
            if contract.startswith(f"{trade.get('symbol') or trade.get('index')}_"):
                contract = contract.split("_", 1)[1]
            entry, exit_price = trade.get("entry_price"), trade.get("exit_price")
            fills = trade.get("exit_fills") or []
            partial = trade.get("_partial_fill") or (fills[-1] if fills else {})
            closed = trade.get("exit_timestamp") is not None
            bid = trade.get("current_bid")
            stamp = trade.get("quote_received_epoch")
            age = now - float(stamp) if self._price(stamp) != "-" else math.inf
            fresh_bid = self._price(bid) != "-" and 0 <= age <= 30
            gross = trade.get("gross_pnl") if closed else (
                (float(bid) - float(entry)) * trade["qty"] + trade.get("partial_gross", 0.0)
                if fresh_bid else None
            )
            gross_values.append(gross)
            call = (trade.get("market_structure") or {}).get("telegram_call") or {}
            expiry = f" · {trade['expiry']}" if trade.get("expiry") else ""
            source = f" · {call['source']}" if call.get("source") else ""
            icon = " 🟢" if gross is not None and gross > 0 else " 🔴" if gross is not None and gross < 0 else ""
            headers.append(f"{number}. <b>{name}</b> {html.escape(str(contract) + expiry + source)}{icon}")
            targets = sorted(float(t) for t in call.get("targets", [])
                             if self._price(t) != "-" and self._price(entry) != "-" and float(t) > float(entry))
            target = trade.get("rupee_target_price")
            if call and targets:
                target = (targets[1] if len(targets) > 1 else None) if trade.get("target_one_done") else targets[0]
            elif not call and trade.get("index") == "NIFTY" and trade.get("target_points"):
                target = float(entry) + (12 if trade.get("target_one_qty") and not trade.get("target_one_done") else trade["target_points"])
            elif not target:
                target = (trade.get("entry_diagnostics") or {}).get("rupee_target_price")
            cash_stop = trade.get("cash_loss_stop_price")
            stop = trade.get("current_sl") or (trade.get("entry_diagnostics") or {}).get("initial_sl")
            if self._price(cash_stop) != "-":
                stop = max(float(cash_stop), float(stop or 0))
            net = trade.get("net_pnl") if closed else trade.get("partial_net", partial.get("booked_net"))
            columns.append([
                "Closed" if closed else "Open" if fresh_bid else "Open*",
                str(trade.get("lots", "-")), str(trade.get("qty", "-")),
                self._price(entry), self._price(exit_price if closed else partial.get("price")),
                self._price(stop), self._price(target),
                self._price(trade.get("current_ltp")) if not closed else "-",
                self._price(bid) if not closed else "-",
                self._price(trade.get("current_ask")) if not closed else "-",
                self._price(trade.get("highest_ltp", trade.get("peak_bid"))) if not closed else "-",
                f"{gross:+,.2f}" if gross is not None else "-",
                f"{net:+,.2f}" if net is not None else "-",
            ])
        labels = ("Status", "Lots", "Quantity", "Entry", "Exit/part", "SL", "Target", "LTP", "Bid", "Ask", "Peak", "Gross P&L", "Booked net")
        widths = [max(9, *(len(value) for value in column)) for column in columns]
        lines = ["Trade      " + " ".join(f"{i:>{w}}" for i, w in enumerate(widths, 1))]
        lines.extend(
            f"{label:<10} " + " ".join(f"{value:>{width}}" for value, width in zip(values, widths))
            for label, values in zip(labels, zip(*columns))
        )
        available = [value for value in gross_values if value is not None]
        total = sum(available)
        icon = chr(0x1F7E2) if total > 0 else chr(0x1F534) if total < 0 else ""
        if available:
            qualifier = " (priced trades only)" if len(available) < len(positions) else ""
            overall = f"{icon} <b>Overall gross P&amp;L{qualifier}</b> Rs.{total:+,.2f}".lstrip()
        else:
            overall = "<b>Overall gross P&amp;L</b> unavailable"
        if session_trades is not None:
            gross_total = sum(float(t["gross_pnl"]) for t in session_trades)
            net_total = sum(float(t["net_pnl"]) for t in session_trades)
            lines.extend((
                "",
                f"Session closed {len(session_trades):>9}",
                f"Session gross  {gross_total:>+9,.2f}",
                f"Session net    {net_total:>+9,.2f}",
            ))
            icon = "🟢" if net_total > 0 else "🔴" if net_total < 0 else "⚪"
            overall = f"{icon} Session net is realised, after estimated charges."
        note = "\n* Bid unavailable or older than 30s." if len(available) < len(positions) else ""
        return "\n".join(headers) + "\n<pre>" + html.escape("\n".join(lines)) + "</pre>\n" + overall + note

    def partial(self, position, fill, position_key=None):
        if not self.enabled:
            return
        self.position(dict(position, _partial_fill=fill), position_key=position_key)
        with self._lock:
            table = self._trade_table(list(self._positions.values()))
            self._events.put(
                f"[{self.mode}] PARTIAL PROFIT BOOKED\n{table}\n"
                f"Quantity sold: {fill['qty']}\nTime: {ist_now()}"
            )

    def position(self, position, entry=False, position_key=None):
        if not self.enabled:
            return
        with self._lock:
            previous = self._positions.get(position_key, {})
            snapshot = dict(position)
            if "_partial_fill" in previous and "_partial_fill" not in snapshot:
                snapshot["_partial_fill"] = previous["_partial_fill"]
            if entry or position_key not in self._positions:
                self._next_update[position_key] = time.monotonic() + self.INTERVAL
            self._positions[position_key] = snapshot
            if entry:
                table = self._trade_table(list(self._positions.values()))
                self._events.put(f"[{self.mode}] TRADE ENTRY\n{table}\nEntry time: {position['entry_ts']}")

    stock_reporting_exclusions = staticmethod(stock_reporting_exclusions)

    @staticmethod
    def completed_paper_trades(day, directory=None):
        directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
        trades, missing = [], []
        for filename, segment in (
            ("paper_trade_ledger.json", "Nifty/Sensex"),
            ("stock_fno_paper_ledger.json", "Stock options"),
        ):
            try:
                ledger = json.loads((directory / filename).read_text(encoding="utf-8-sig"))
                if ledger.get("mode") != "PAPER" or not isinstance(ledger.get("trades"), list):
                    raise ValueError("PAPER ledger unavailable")
                trades.extend(t for t in ledger["trades"] if str(t.get("exit_timestamp", ""))[:10] == day)
            except (OSError, ValueError, TypeError, AttributeError):
                missing.append(segment)
        excluded = TelegramNotifier.stock_reporting_exclusions(day, directory)
        return sorted((trade for trade in trades if trade.get("entry_id") not in excluded),
                      key=lambda trade: trade["exit_timestamp"]), missing

    @staticmethod
    def completed_trade_table(trades, missing=()):
        headings = []
        rows = [f"{'#':>2} {'Entry':>7} {'Exit':>7} {'Gross':>10} {'Net':>10}"]
        gross_total = net_total = 0.0
        for number, trade in enumerate(trades, 1):
            name = str(trade.get("index") or trade.get("symbol") or "Trade")
            contract = str(trade.get("contract") or "").removeprefix(name + " ")
            gross, net = float(trade["gross_pnl"]), float(trade["net_pnl"])
            gross_total += gross
            net_total += net
            icon = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
            headings.append(
                f"{number}. <b>{html.escape(name)}</b> {html.escape(contract)} "
                f"{icon} · {trade['exit_timestamp'][11:19]}"
            )
            rows.append(f"{number:>2} {float(trade['entry_price']):>7.2f} {float(trade['exit_price']):>7.2f} {gross:>+10,.2f} {net:>+10,.2f}")
        rows.append(f"{'Total':<18} {gross_total:>+10,.2f} {net_total:>+10,.2f}")
        coverage = f"\nIncomplete ledger data for {html.escape(', '.join(missing))}." if missing else ""
        return ("\n".join(headings) + "\n<pre>" + html.escape("\n".join(rows)) + "</pre>"
                + f"\n{len(trades)} closed · INR · Net after estimated charges" + coverage)

    def exited(self, trade, position_key=None, ledger_trades=None):
        if not self.enabled:
            return
        channel_call = (trade.get('market_structure') or {}).get('telegram_call') or {}
        source = f"Source: Telegram {html.escape(channel_call['source'])}\n" if channel_call.get('source') else ''
        with self._lock:
            previous = self._positions.pop(position_key, {})
            self._next_update.pop(position_key, None)
            closed = {**previous, **trade}
            exit_day = trade["exit_timestamp"][:10]
            session_trades = None if ledger_trades is None else [
                record for record in ledger_trades
                if str(record.get("exit_timestamp", ""))[:10] == exit_day
            ]
            if self.mode == "PAPER" and ledger_trades is not None:
                combined, missing = self.completed_paper_trades(exit_day)
                known = {(t.get("timestamp"), t["exit_timestamp"], t.get("contract")) for t in combined}
                excluded = self.stock_reporting_exclusions(exit_day)
                for record in (*session_trades, closed):
                    if record.get("entry_id") in excluded:
                        continue
                    key = (record.get("timestamp"), record["exit_timestamp"], record.get("contract"))
                    if key not in known:
                        combined.append(record)
                        known.add(key)
                combined.sort(key=lambda record: record["exit_timestamp"])
                table = self.completed_trade_table(combined, missing)
                if self._positions:
                    table += "\nStill open: " + ", ".join(
                        html.escape(str(p.get("symbol") or p.get("index") or "Trade"))
                        for p in self._positions.values()
                    )
                stop = self._price(closed.get("current_sl"))
                target = self._price(closed.get("rupee_target_price"))
                if target == "-" and closed.get("index") == "NIFTY" and closed.get("target_points"):
                    target = self._price(closed["entry_price"] + closed["target_points"])
                table += f"\nLatest exit SL: {stop} · Target: {target}"
            elif self.mode == "PAPER MCX" and session_trades is not None:
                table = self.completed_trade_table(session_trades)
                table += (f"\nLatest exit SL: {self._price(closed.get('current_sl'))}"
                          f" · Target: {self._price(closed.get('rupee_target_price'))}")
            else:
                table = self._trade_table([closed, *self._positions.values()], session_trades)
            self._events.put(
                f"[{self.mode}] TRADE EXIT\n{table}\n{source}"
                f"Result: {trade.get('r_multiple', trade.get('r_mult', 0.0)):+.2f}R\n"
                f"Reason: {html.escape(trade['exit_reason'])}\nExit time: {trade['exit_timestamp']}"
            )

    def _send(self, message):
        heading = message.partition('\n')[0]
        is_trade_event = heading.endswith(('TRADE ENTRY', 'TRADE EXIT', 'PARTIAL PROFIT BOOKED'))
        event = ' | '.join(message.splitlines()[:4]) if is_trade_event else heading
        try:
            for attempt in range(2 if is_trade_event else 1):
                response = requests.post(
                    f'https://api.telegram.org/bot{self.token}/sendMessage',
                    json={'chat_id': self.chat_id, 'text': message, 'parse_mode':'HTML'}, timeout=(3, 4),
                )
                body = response.json()
                if response.ok and body.get('ok'):
                    if is_trade_event:
                        self.logger.info('Telegram acknowledged %s | message_id=%s',
                                         event, body.get('result', {}).get('message_id'))
                    return
                if response.status_code != 429:
                    break
                delay = max(1, int(body.get('parameters', {}).get('retry_after', 5)))
                self.logger.warning('Telegram rate limited on %s; pausing alerts for %s seconds.', event, delay)
                if self._closing.wait(delay):
                    break
            self.logger.warning('Telegram delivery failed (HTTP %s) | %s', response.status_code, event)
        except Exception:
            # Request exception strings can contain the token-bearing URL.
            self.logger.warning('Telegram delivery unverified | %s', event)

    def _run(self):
        while not self._closing.is_set() or not self._events.empty():
            try:
                message = self._events.get(timeout=0.2)
            except queue.Empty:
                message = None
            if message is None and not self._closing.is_set():
                with self._lock:
                    now = time.monotonic()
                    if self._next_update and now >= min(self._next_update.values()):
                        table = self._trade_table(list(self._positions.values()))
                        message = f'[{self.mode}] OPEN TRADE UPDATE\n{table}\nUpdate: {ist_now()}'
                        for key in self._next_update:
                            self._next_update[key] = now + self.INTERVAL
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

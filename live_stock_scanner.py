"""Read-only live ScanX-style scanner. No orders, ledgers or strategy controls."""
import json
import os
import logging
import threading
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

from dhan_dynamic_fno_stock_bot import (
    IST, DhanStockOptionsClient, DynamicStockOptionsUniverse, load_dotenv_file,
)
from stock_option_signals import scanx_stock_membership
from dashboard_data import candle_rows, events, levels


class LiveStockScanner:
    interval = 3.0

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.references = {}
        self.pending = {}
        self.document = self.empty('STARTING', 'Waiting for live market session')
        self.client = None
        self.universe = {}
        self.last_recorded = self.load_recorded()
        self.saved_epoch = 0

    def empty(self, status, message, **extra):
        return {'status': status, 'message': message, 'refresh_seconds': self.interval,
                'session': datetime.now(IST).date().isoformat(), 'observed_epoch': None,
                'complete': False, 'symbols': {}, **extra}

    def load_recorded(self):
        try:
            data = json.loads((self.root/'stock_scanner_last_snapshot.json').read_text(encoding='utf-8'))
            if data.get('status') == 'LIVE' and isinstance(data.get('symbols'), dict) and data.get('observed_epoch'):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return None

    def remember(self, result, force=False):
        if result.get('status') != 'LIVE' or not result.get('observed_epoch') or not result.get('complete'):
            return
        self.last_recorded = result
        # Batch persistence; force an exact final save on the close transition.
        if not force and time.monotonic()-self.saved_epoch < 15:
            return
        try:
            path = self.root/'stock_scanner_last_snapshot.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
            os.replace(temporary, path)
            self.saved_epoch = time.monotonic()
        except (OSError, ValueError):
            pass

    def closed_snapshot(self):
        if self.last_recorded:
            return {**self.last_recorded, 'status': 'LAST_RECORDED',
                    'message': 'Market closed · last recorded scanner snapshot (not live)'}
        return self.empty('MARKET_CLOSED', 'DATA UNAVAILABLE · no completed scanner snapshot was captured before market close; this is not a zero-match result')

    def snapshot(self):
        with self.lock:
            result = {**self.document, 'symbols': dict(self.document['symbols'])}
        age = time.time() - (result.get('observed_epoch') or 0)
        if result['status'] == 'LIVE' and age > 10:
            result.update(status='STALE', message='Live quote refresh is delayed', symbols={}, complete=False)
        return result

    def start(self):
        threading.Thread(target=self.run, name='live-stock-scanner', daemon=True).start()
        threading.Thread(target=self.hydrate, name='scanner-reference-loader', daemon=True).start()

    def configure(self):
        env = load_dotenv_file(str(self.root/'.env'))
        if not env.get('DHAN_CLIENT_ID') or not env.get('DHAN_ACCESS_TOKEN'):
            raise ValueError('Dhan credentials unavailable')
        # Keep API response bodies and credential-bearing exceptions out of UI/logs.
        logger = logging.getLogger('live-scanner-private')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.client = DhanStockOptionsClient(env['DHAN_CLIENT_ID'], env['DHAN_ACCESS_TOKEN'], logger)
        self.reference_client = DhanStockOptionsClient(env['DHAN_CLIENT_ID'], env['DHAN_ACCESS_TOKEN'], logger)
        self.universe = DynamicStockOptionsUniverse.load_universe(logger)
        if not self.universe or len(self.universe) > 500:
            raise ValueError('Scanner universe unavailable or exceeds batch capacity')

    def hydrate(self):
        while not self.stop.wait(.25):
            with self.lock:
                task = next(iter(self.pending.items()), None)
                if task:
                    self.pending.pop(task[0], None)
            if not task:
                continue
            sid, boundary = task
            try:
                daily = self.reference_client.scanner_previous_session(sid)
                candles = self.reference_client.fetch_intraday_candles(sid,
                    from_date=(datetime.now(IST)-timedelta(days=10)).strftime('%Y-%m-%d'))
                if daily and candles:
                    with self.lock:
                        self.references[sid] = {'boundary': boundary, 'daily': daily, 'candles': candles}
            except Exception:
                # Retry on a subsequent quote cycle; never substitute an old reference.
                pass

    def evaluate(self, quotes, now):
        boundary = int(now)//300*300
        matches = {}
        missing = 0
        fresh = 0
        for sid, stock in self.universe.items():
            quote = quotes.get(str(sid)) or {}
            try:
                if not 0 <= now-float(quote['received_epoch']) <= 8:
                    continue
                price = float(quote['last_price']); previous = float(quote['ohlc']['close'])
                if price <= 0 or previous <= 0:
                    continue
                fresh += 1
                if abs((price/previous-1)*100) < 2:
                    continue
                with self.lock:
                    reference = self.references.get(sid)
                    if not reference or reference['boundary'] != boundary:
                        self.pending[sid] = boundary
                if not reference or reference['boundary'] != boundary:
                    missing += 1
                    continue
                membership = scanx_stock_membership(reference['candles'],
                    {**quote, 'previous_session_reference': reference['daily']}, now)
                if not membership['allowed']:
                    continue
                record = {'symbol': stock['symbol'], 'observed_epoch': now,
                    'source': 'LIVE Dhan batch quote', 'candles': candle_rows(reference['candles'], now),
                    'analysis': {'scanner_membership': membership, 'spot': price,
                        'day_change_pct': membership['day_change_pct'],
                        'entry_decision': {'action': 'SCANNER MATCH', 'reason': 'Live scanner filters passed; trade qualification is separate'}}}
                record.update(events=events(record), levels=levels(record['analysis']), segment='Stocks')
                matches[stock['symbol']] = record
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        complete = fresh == len(self.universe) and missing == 0
        return self.empty('LIVE' if fresh else 'ERROR',
            'Live quotes; loading candle/EMA references' if missing else 'Live Dhan scanner',
            observed_epoch=now, symbols=matches, complete=complete,
            universe_count=len(self.universe), fresh_quote_count=fresh, pending_references=missing)

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            now = datetime.now(IST)
            clock = now.time().replace(tzinfo=None)
            try:
                if now.weekday() >= 5 or not dtime(9,15) <= clock < dtime(15,30):
                    if self.document.get('status') == 'LIVE':
                        self.remember(self.document, force=True)
                    result = self.closed_snapshot()
                else:
                    if self.client is None:
                        self.configure()
                    quotes = self.client.fetch_batch_ohlc(list(self.universe)).get('NSE_EQ', {})
                    result = self.evaluate(quotes, time.time()) if quotes else self.empty('ERROR', 'Dhan quote request failed; no cached matches displayed')
                if result.get('status') == 'LIVE':
                    self.remember(result)
                with self.lock:
                    self.document = result
            except Exception:
                with self.lock:
                    self.document = self.empty('ERROR', 'Live scanner initialization/data request failed; check Dhan connection')
            self.stop.wait(max(.1, self.interval-(time.monotonic()-started)))

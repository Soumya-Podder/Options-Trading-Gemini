"""Local, read-only PAPER dashboard. Run python -B dashboard_web.py."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from stock_option_policy import stock_reporting_exclusions

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
FILES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
         '/style.css': ('style.css', 'text/css')}


def read_json(name, errors):
    try:
        value = json.loads((ROOT / name).read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('Expected object')
        return value
    except (OSError, ValueError):
        errors.append(name + ' unavailable')
        return {}


def number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def snapshot():
    errors, accounts, trades, positions = [], [], [], []
    for segment, name in [('Indices', 'paper_trade_ledger.json'),
                          ('Stocks', 'stock_fno_paper_ledger.json'),
                          ('Commodities', 'mcx_mini_paper_ledger.json')]:
        ledger = read_json(name, errors)
        if ledger and ledger.get('mode') != 'PAPER':
            errors.append(segment + ' ledger is not PAPER')
            continue
        accounts.append({'name': segment, 'balance': number(ledger.get('virtual_balance_net')),
                         'initial': number(ledger.get('initial_capital'))})
        excluded = {}
        for trade in ledger.get('trades', []):
            if segment == 'Stocks':
                day = str(trade.get('exit_timestamp', ''))[:10]
                if day not in excluded:
                    excluded[day] = stock_reporting_exclusions(day, ROOT)
                if trade.get('entry_id') in excluded[day]:
                    continue
            net, gross = number(trade.get('net_pnl')), number(trade.get('gross_pnl'))
            if net is None or gross is None:
                continue
            call = (trade.get('market_structure') or {}).get('telegram_call') or {}
            trades.append({'segment': segment, 'symbol': trade.get('index') or trade.get('symbol') or segment,
                           'contract': str(trade.get('contract', '—')), 'source': call.get('source', 'Native'),
                           'entry': number(trade.get('entry_price')), 'exit': number(trade.get('exit_price')),
                           'lots': trade.get('lots'), 'qty': trade.get('qty'),
                           'closed': trade.get('exit_timestamp', ''), 'opened': trade.get('timestamp', ''),
                           'net': net, 'gross': gross, 'charges': round(gross-net, 2),
                           'reason': trade.get('exit_reason', '—')})
        for key, pos in ledger.items():
            if key.startswith('active_position') and isinstance(pos, dict) and pos.get('entry_price'):
                positions.append({'segment': segment, 'symbol': pos.get('index') or pos.get('symbol'),
                                  'contract': pos.get('contract') or str(pos.get('strike', ''))+' '+pos.get('option_type', ''),
                                  'entry': number(pos.get('entry_price')), 'qty': pos.get('qty'),
                                  'stop': number(pos.get('current_sl')), 'opened': pos.get('entry_ts')})
    indices = read_json('live_telemetry.json', errors)
    mcx = read_json('mcx_live_telemetry.json', errors)
    markets = []
    for name, telemetry, key in [('Indices', indices, 'indices'), ('Commodities', mcx, 'commodities')]:
        path = ROOT / ('live_telemetry.json' if name == 'Indices' else 'mcx_live_telemetry.json')
        age = max(0, datetime.now().timestamp()-path.stat().st_mtime) if path.exists() else None
        for symbol, item in (telemetry.get(key) or {}).items():
            if not isinstance(item, dict):
                continue
            decision = item.get('entry_decision') or {}
            markets.append({'symbol': symbol, 'segment': name, 'spot': number(item.get('spot')),
                            'reason': decision.get('reason') or item.get('reason') or 'Waiting for a setup',
                            'action': decision.get('action') or item.get('recommended_dir') or 'HOLD',
                            'regime': (item.get('regime_info') or {}).get('regime') or item.get('regime'),
                            'age': round(age) if age is not None else None})
    scanner_path = ROOT / 'dhan_stock_fno.log'
    try:
        with scanner_path.open('rb') as log:
            log.seek(max(0, scanner_path.stat().st_size - 8192))
            tail = log.read().decode('utf-8', errors='replace')
        summaries = re.findall(r'Shortlist (\d+) \| confirmed (\d+) \| qualified (\d+) \| entered (\d+)', tail)
        if summaries:
            shortlisted, confirmed, qualified, entered = summaries[-1]
            markets.insert(2, {'symbol': 'STOCK OPTIONS', 'segment': 'Stocks', 'spot': None,
                              'metric': shortlisted + ' candidates',
                              'reason': f'{confirmed} confirmed · {qualified} qualified · {entered} entries in last scan',
                              'action': 'SCAN SNAPSHOT', 'regime': None,
                              'age': round(max(0, datetime.now().timestamp()-scanner_path.stat().st_mtime))})
    except OSError:
        errors.append('Stock scanner status unavailable')
    return {'as_of': datetime.now(IST).isoformat(), 'accounts': accounts,
            'trades': sorted(trades, key=lambda t: t['closed'], reverse=True),
            'positions': positions, 'markets': markets, 'errors': errors}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/snapshot':
            body = json.dumps(snapshot(), allow_nan=False).encode()
            content_type = 'application/json'
        elif self.path in FILES:
            filename, content_type = FILES[self.path]
            body = (ROOT / 'dashboard' / filename).read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type+'; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    print('PAPER dashboard http://127.0.0.1:8765', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()

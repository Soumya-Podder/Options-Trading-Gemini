"""Read recorded bot evidence for the local dashboard without fetching market data."""
import json
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def tail(path, limit):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size-limit))
            return stream.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []


def candle_rows(data, as_of):
    rows = []
    try:
        for i, stamp in enumerate((data or {}).get('timestamp', [])):
            row = {k: float(data[k][i]) for k in ('open', 'high', 'low', 'close')}
            if float(stamp)+300 <= as_of and all(math.isfinite(v) and v > 0 for v in row.values()):
                row.update(timestamp=float(stamp), volume=float(data.get('volume', [0]*len(data['timestamp']))[i]))
                rows.append(row)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return rows[-100:]


def levels(analysis):
    result = []
    structure = analysis.get('market_structure') or {}
    for key in ('previous_day_high', 'previous_day_low', 'opening_high', 'opening_low', 'day_high', 'day_low'):
        value = (structure.get('levels') or {}).get(key)
        if isinstance(value, (int, float)):
            result.append({'price': value, 'lower': value, 'upper': value,
                           'role': 'SUPPLY' if 'high' in key else 'DEMAND',
                           'timeframe': 'Session', 'label': key.replace('_', ' ')})
    mtf = structure.get('mtf_sr') or {}
    for zone in mtf.get('multi_bounce_zones', []):
        timeframe = '5m'
        for name, field in (('4h', 'htf_4h_levels'), ('15m', 'mtf_15m_levels')):
            if any(abs(float(value)-zone['level']) < .01 for value in mtf.get(field, [])):
                timeframe = name
                break
        result.append({'price': zone['level'], 'lower': zone['level'], 'upper': zone['level'],
                       'role': 'SUPPLY' if zone.get('role') == 'STRONG_RESISTANCE' else 'DEMAND',
                       'timeframe': timeframe, 'kind': 'SUPPORT_RESISTANCE',
                       'label': f"{zone.get('touches', 0)}-touch {zone.get('role', 'zone').lower().replace('_', ' ')}"})
    for timeframe, zones in (analysis.get('mtf_liquidity') or {}).get('zones', {}).items():
        for zone in zones:
            result.append({**zone, 'price': (zone['lower']+zone['upper'])/2,
                           'timeframe': timeframe, 'label': zone.get('kind', 'zone').replace('_', ' ').lower()})
    for field, role in (('orb_high', 'SUPPLY'), ('orb_low', 'DEMAND')):
        if isinstance(analysis.get(field), (int, float)):
            result.append({'price': analysis[field], 'lower': analysis[field], 'upper': analysis[field],
                           'role': role, 'timeframe': 'Session', 'label': field.replace('_', ' ').upper()})
    seen = set()
    unique = []
    for level in result:
        key = (level['price'], level['role'], level['timeframe'])
        if key not in seen:
            seen.add(key)
            unique.append(level)
    return unique


def events(record):
    analysis = record.get('analysis') or {}
    confirmation = analysis.get('breakout_confirmation') or {}
    stamp = analysis.get('signal_timestamp')
    result = []
    if stamp and confirmation.get('closed_candle'):
        result.append({'timestamp': stamp, 'type': 'SWEEP' if confirmation.get('entry_path') == 'LIQUIDITY_SWEEP_RECLAIM' else 'BREAKOUT',
                       'side': analysis.get('opt_type'), 'level': confirmation.get('trigger_level'),
                       'detail': confirmation.get('entry_path', 'Closed-bar confirmation')})
    structure = analysis.get('market_structure') or {}
    if structure.get('allowed') and structure.get('setup') and record.get('candles'):
        result.append({'timestamp': record['candles'][-1]['timestamp'],
                       'type': 'BREAKOUT' if 'BREAK' in structure['setup'] else 'RETEST',
                       'side': 'CE' if analysis.get('recommended_dir') == 'BUY_CE' else 'PE',
                       'level': structure.get('trigger_level'), 'detail': structure['setup']})
    return result


def archive_nifty(root):
    paths = sorted((root/'dhan_hist_cache').glob('nifty_entry_audit_*.jsonl'))
    if not paths:
        return None
    latest, observed_events = None, []
    for line in tail(paths[-1], 8_000_000):
        try:
            row = json.loads(line)
            record = {'symbol': 'NIFTY', 'observed_epoch': row['snapshot_epoch'],
                      'analysis': row['analysis'], 'candles': candle_rows(row.get('closed_index_candles'), row['snapshot_epoch'])}
            observed_events.extend({**event, 'observed_epoch': row['snapshot_epoch'], 'analysis': row['analysis']}
                                   for event in events(record))
            latest = record
        except (KeyError, TypeError, ValueError):
            continue
    if latest:
        latest.update(source='Saved decision audit', events=observed_events)
    return latest


_scanner_cache = {}


def scanner_log(root):
    path = root/'dhan_stock_fno.log'
    if not path.exists():
        return {}
    signature = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if signature in _scanner_cache:
        return _scanner_cache[signature]
    day = datetime.fromtimestamp(path.stat().st_mtime, IST).date().isoformat()
    records = {}
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                stamp = datetime.fromisoformat(day+'T'+line[:8]).replace(tzinfo=IST).timestamp()
                if '[STOCK SCANNER MATCH]' in line:
                    symbol, text = line.split('[STOCK SCANNER MATCH] ', 1)[1].split(' | ', 1)
                    membership = json.loads(text)
                    previous = records.get(symbol, {})
                    records[symbol] = {'events': previous.get('events', []), 'first_seen': previous.get('first_seen', stamp), 'symbol': symbol, 'observed_epoch': stamp, 'candles': [],
                        'source': 'Recent scanner log', 'analysis': {'scanner_membership': membership,
                        'spot': membership.get('price'), 'day_change_pct': membership.get('day_change_pct'),
                        'entry_decision': {'action': 'MATCHED', 'reason': 'Scanner matched; entry outcome not recorded in this observation'}}}
                elif '[STOCK BREAKOUT CONFIRMED]' in line:
                    symbol, text = line.split('[STOCK BREAKOUT CONFIRMED] ', 1)[1].split(' | ', 1)
                    confirmation = json.loads(text)
                    if symbol in records:
                        signal = confirmation.get('signal_timestamp') or confirmation.get('timestamp')
                        if signal:
                            records[symbol]['events'].append({'timestamp': signal, 'type': 'BREAKOUT', 'detail': confirmation.get('entry_path', 'Confirmed breakout'), 'side': records[symbol]['analysis']['scanner_membership'].get('opt_type')})
                elif '[OPTSTK REJECT]' in line:
                    name, reason = line.split('[OPTSTK REJECT] ', 1)[1].split(' | ', 1)
                    symbol = name.split()[0]
                    if symbol in records:
                        records[symbol]['analysis']['entry_decision'] = {'action': 'HOLD', 'reason': reason}
                        if reason.startswith(('Scanner membership:', 'Final scanner membership:')):
                            records[symbol]['analysis']['scanner_membership']['allowed'] = False
            except (ValueError, KeyError, TypeError):
                continue
    _scanner_cache.clear()
    _scanner_cache[signature] = records
    return records


def desk_snapshot(root):
    engines = {'Indices': {}, 'Stocks': {}, 'Commodities': {}}
    for segment, filename in (('Indices', 'index_desk_telemetry.json'), ('Stocks', 'stock_desk_telemetry.json'),
                              ('Commodities', 'mcx_desk_telemetry.json')):
        document = read_object(root/filename)
        for symbol, record in document.get('symbols', {}).items():
            engines[segment][symbol] = {**record, 'source': 'Bot observation'}
    archived = archive_nifty(root)
    if archived and archived['observed_epoch'] > engines['Indices'].get('NIFTY', {}).get('observed_epoch', 0):
        engines['Indices']['NIFTY'] = archived
    for segment, file, key in (('Indices', 'live_telemetry.json', 'indices'), ('Commodities', 'mcx_live_telemetry.json', 'commodities')):
        path = root/file
        for symbol, analysis in read_object(path).get(key, {}).items():
            if symbol not in engines[segment] and isinstance(analysis, dict):
                engines[segment][symbol] = {'symbol': symbol, 'analysis': analysis, 'candles': [],
                    'observed_epoch': path.stat().st_mtime, 'source': 'Telemetry; candles not recorded'}
    for symbol, record in scanner_log(root).items():
        if symbol not in engines['Stocks']:
            engines['Stocks'][symbol] = record
        else:
            current = engines['Stocks'][symbol]
            current['events'] = record.get('events', []) + events(current)
            if record['observed_epoch'] > current.get('observed_epoch', 0):
                current['analysis']['scanner_membership'] = record['analysis']['scanner_membership']
    for segment, records in engines.items():
        for record in records.values():
            record['levels'] = levels(record.get('analysis') or {})
            record.setdefault('events', events(record))
            record['segment'] = segment
    return {'engines': engines, 'as_of': datetime.now(IST).isoformat(),
            'scanner_basis': 'All recorded scanner matches from the session log. Scanner membership is separate from entry confirmation; missing breakout times are never inferred.'}

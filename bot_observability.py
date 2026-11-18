"""Bounded UI observations. These records never control trading decisions."""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
_snapshots = {}
_flushed = {}


def publish_bot_observation(engine, symbol, candles, analysis, *, directory=ROOT, now=None):
    """Publish closed OHLCV and decision evidence, at most once per five seconds."""
    if engine not in ('index', 'stock', 'mcx'):
        return False
    now = time.time() if now is None else now
    try:
        directory = Path(directory)
        day = datetime.fromtimestamp(now, IST).date().isoformat()
        key = (str(directory), engine, day)
        document = _snapshots.setdefault(key, {'session': day, 'symbols': {}})
        # A long-running process retains only the current day's UI observations.
        for previous in list(_snapshots):
            if previous[:2] == key[:2] and previous != key:
                _snapshots.pop(previous, None)
                _flushed.pop(previous, None)
        rows = []
        for i, stamp in enumerate((candles or {}).get('timestamp', [])):
            stamp = float(stamp)
            if stamp + 300 > now:
                continue
            bar = {field: float(candles[field][i]) for field in ('open', 'high', 'low', 'close')}
            if not all(math.isfinite(v) and v > 0 for v in bar.values()):
                continue
            if not bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']:
                continue
            bar.update(timestamp=stamp, volume=float((candles.get('volume') or [0]*len(candles['timestamp']))[i]))
            rows.append(bar)
        if not rows:
            return False
        allowed_fields = ('spot', 'spot_ltp', 'entry_decision', 'recommended_dir', 'reason',
                          'market_structure', 'regime_info', 'regime', 'scanner_membership',
                          'breakout_confirmation', 'mtf_liquidity', 'smc_quality', 'bsm_quality',
                          'signal_timestamp', 'opt_type', 'scanner', 'day_change_pct',
                          'orb_high', 'orb_low', 'ema9', 'ema21', 'ema50', 'atr14',
                          'trade_quality', 'setup', 'vol_ratio')
        evidence = {field: analysis[field] for field in allowed_fields if field in analysis}
        previous = document['symbols'].get(symbol, {})
        history = previous.get('events', [])
        confirmation = evidence.get('breakout_confirmation') or {}
        stamp = evidence.get('signal_timestamp')
        if stamp and confirmation.get('closed_candle'):
            event = {'timestamp': stamp, 'type': 'BREAKOUT', 'side': evidence.get('opt_type'), 'detail': confirmation.get('entry_path', 'Confirmed breakout')}
            if not any(item['timestamp'] == stamp for item in history):
                history = (history + [event])[-100:]
        document['symbols'][symbol] = {'events': history, 'symbol': symbol, 'observed_epoch': now,
                                       'candles': rows[-100:], 'analysis': evidence}
        document['symbols'] = dict(sorted(document['symbols'].items(),
                                          key=lambda item: item[1]['observed_epoch'], reverse=True)[:300])
        if now - _flushed.get(key, 0) < 5:
            return True
        payload = json.dumps(document, allow_nan=False, separators=(',', ':'))
        path = directory / f'{engine}_desk_telemetry.json'
        temporary = path.with_suffix(f'.{os.getpid()}.tmp')
        temporary.write_text(payload, encoding='utf-8')
        os.replace(temporary, path)
        _flushed[key] = now
        return True
    except (OSError, KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False

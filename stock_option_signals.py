"""Closed-candle stock option entry confirmation, shared by live and replay.

Input is Dhan's dict of parallel timestamp/open/high/low/close/volume arrays.
Timestamps are Unix seconds for the *start* of five-minute candles. All session
boundaries use IST explicitly, independently of the machine's time zone. The
returned signal timestamp is the confirmation candle's start; it becomes usable
only five minutes later. A signal is evidence of alignment, not a price guarantee.
"""

from datetime import datetime, time, timezone, timedelta
from statistics import median
from math import isfinite
from typing import Dict, List, Optional
from market_regime import opening_trend


IST = timezone(timedelta(hours=5, minutes=30))
BAR_SECONDS = 300
SESSION_OPEN = time(9, 15)
SESSION_LAST_BAR = time(15, 25)


def _ema(values: List[float], period: int) -> List[float]:
    result = [values[0]]
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _atr(bars: List[dict], period: int) -> List[float]:
    ranges = []
    previous = bars[0]["close"]
    for bar in bars:
        ranges.append(max(bar["high"] - bar["low"],
                          abs(bar["high"] - previous),
                          abs(bar["low"] - previous)))
        previous = bar["close"]
    return _ema(ranges, period)


def _closed_bars(candles: dict, as_of_epoch: float) -> Optional[List[dict]]:
    fields = ("timestamp", "open", "high", "low", "close", "volume")
    try:
        if any(len(candles[field]) != len(candles["timestamp"]) for field in fields):
            return None
        bars_by_timestamp = {}
        for index, raw_timestamp in enumerate(candles["timestamp"]):
            timestamp = float(raw_timestamp)
            if not isfinite(timestamp) or timestamp != int(timestamp):
                return None
            timestamp = int(timestamp)
            # Never let the current candle's price or volume affect a decision.
            if timestamp + BAR_SECONDS > as_of_epoch:
                continue
            dt = datetime.fromtimestamp(timestamp, IST)
            if not SESSION_OPEN <= dt.time() <= SESSION_LAST_BAR:
                continue
            if dt.minute % 5 or dt.second:
                return None
            bar = {field: float(candles[field][index]) for field in fields[1:]}
            if any(not isfinite(value) for value in bar.values()):
                return None
            if (min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0
                    or bar["volume"] < 0
                    or bar["high"] < max(bar["open"], bar["close"], bar["low"])
                    or bar["low"] > min(bar["open"], bar["close"], bar["high"])):
                return None
            bar.update(timestamp=timestamp, date=dt.date())
            if timestamp in bars_by_timestamp and bars_by_timestamp[timestamp] != bar:
                return None
            bars_by_timestamp[timestamp] = bar
        return sorted(bars_by_timestamp.values(), key=lambda bar: bar["timestamp"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def recent_entry_momentum(candles, signal, spot, now_epoch):
    """Reject a substantial post-confirmation retreat, using closed minutes only."""
    def reject(reason):
        return {'allowed':False,'reason':reason}
    try:
        direction=1 if signal['opt_type']=='CE' else -1
        atr=float(signal['atr_5m'])
        confirmation=float(signal['signal_timestamp'])+300
        rows=[]
        fields=('timestamp','open','high','low','close','volume')
        if not atr>0 or not isfinite(atr) or not isfinite(float(spot)) or spot<=0:
            return reject('Invalid fresh-momentum inputs')
        if any(len(candles[k])!=len(candles['timestamp']) for k in fields):
            return reject('Incomplete one-minute momentum data')
        for i,stamp in enumerate(candles['timestamp']):
            stamp=float(stamp)
            if stamp<confirmation or stamp+60>now_epoch:continue
            row={k:float(candles[k][i]) for k in fields}
            if (not all(isfinite(v) for v in row.values()) or stamp%60
                    or not 0<row['low']<=min(row['open'],row['close'])<=max(row['open'],row['close'])<=row['high']
                    or row['volume']<0):return reject('Invalid one-minute momentum data')
            rows.append(row)
        rows.sort(key=lambda r:r['timestamp'])
        recent=rows[-3:]
        if (len(recent)<2 or now_epoch-(recent[-1]['timestamp']+60)>90
                or any(b['timestamp']-a['timestamp']!=60 for a,b in zip(recent,recent[1:]))):
            return reject('Need fresh consecutive closed one-minute candles')
        extreme=max(r['high'] for r in recent) if direction==1 else min(r['low'] for r in recent)
        retreat=direction*(extreme-spot)
        weakening=direction*(recent[-1]['close']-recent[-2]['close'])<0
        allowed=not (weakening and retreat>0.5*atr)
        return {'allowed':allowed,'reason':'Fresh momentum held' if allowed else 'Recent breakout fading after confirmation',
                'retreat_atr':retreat/atr,'last_closed_minute':recent[-1]['timestamp'],
                'weakening_close':weakening}
    except (KeyError,TypeError,ValueError,OverflowError):
        return reject('One-minute momentum data unavailable')


def _completed_15m_bars(bars: List[dict]) -> List[dict]:
    groups: Dict[int, List[dict]] = {}
    for bar in bars:
        dt = datetime.fromtimestamp(bar["timestamp"], IST)
        session_open = int(dt.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
        bucket_start = session_open + ((bar["timestamp"] - session_open) // 900) * 900
        groups.setdefault(bucket_start, []).append(bar)
    result = []
    for timestamp, group in sorted(groups.items()):
        # Aggregate observed bars only after the entire bucket has closed.
        # Missing observations are not synthesized or treated as zero-volume bars.
        if (len(group)!=3 or [b['timestamp'] for b in group]!=[timestamp,timestamp+300,timestamp+600]
                or timestamp + 900 > bars[-1]["timestamp"] + BAR_SECONDS):
            continue
        result.append({"timestamp": timestamp, "open": group[0]["open"],
                       "high": max(bar["high"] for bar in group),
                       "low": min(bar["low"] for bar in group),
                       "close": group[-1]["close"]})
    return result


def scanx_stock_membership(candles, quote, as_of_epoch, min_day_change_pct=2.0, ema_period=10):
    """ScanX-style membership, separate from trade qualification.

    Require volume above the previous full session and the matching
    day-change and completed 15-minute EMA conditions.
    """
    result={'allowed':False,'reason':'Scanner reference unavailable','profile':'FULL_SESSION_VOLUME_PREVIOUS_15M_EMA'}
    try:
        threshold=float(min_day_change_pct)
        if not isfinite(threshold) or threshold<=0 or ema_period!=10:
            result['reason']='Scanner requires a positive day-change threshold and 15-minute EMA(10)';return result
        threshold=max(2.0,threshold)
        bars=_closed_bars(candles,as_of_epoch)
        if not bars:return result
        today=datetime.fromtimestamp(as_of_epoch,IST).date()
        previous=[b for b in bars if b['date']<today]
        if not previous:return result
        prior=[b for b in previous if b['date']==previous[-1]['date']]
        first=datetime.combine(prior[0]['date'],SESSION_OPEN,IST).timestamp()
        daily=quote.get('previous_session_reference')
        if daily:
            if daily['date']!=prior[0]['date'].isoformat():return result
            prior_volume=float(daily['volume'])
        elif len(prior)!=75 or any(b['timestamp']!=first+i*300 for i,b in enumerate(prior)):
            result['reason']='Previous session volume coverage incomplete';return result
        else:prior_volume=sum(b['volume'] for b in prior)
        completed=_completed_15m_bars(bars)
        if len(completed)<ema_period+2:return result
        stamp=float(quote['received_epoch']);age=as_of_epoch-stamp
        if not 0<=age<=30:
            result['reason']='Scanner quote stale';return result
        price=float(quote['last_price']);close=float(quote['ohlc']['close'])
        volume=float(quote['volume'])
        if not all(isfinite(v) and v>0 for v in (price,close,volume,prior_volume)):return result
        ema=_ema([b['close'] for b in completed],ema_period)[-1]
        change=(price/close-1)*100
        direction='CE' if change+1e-9>=threshold and price>ema else 'PE' if change-1e-9<=-threshold and price<ema else None
        result.update(price=price,previous_close=close,day_change_pct=change,
                      session_volume=volume,previous_session_volume=prior_volume,
                      previous_ema_15m=ema,ema_reference_epoch=completed[-1]['timestamp'],
                      previous_session_date=prior[0]['date'].isoformat(),quote_age_seconds=age)
        if volume <= prior_volume:
            result['reason']='Session volume not above previous full-session volume'
        elif direction is None:result['reason']='Day change and previous 15-minute EMA do not match a scanner side'
        else:result.update(allowed=True,opt_type=direction,reason='Scanner filters passed')
        return result
    except (KeyError,TypeError,ValueError,OverflowError,IndexError):return result


def confirmed_pullback_retest(today_bars, prior_high, prior_low, orb_high, orb_low, atr, bullish):
    """Closed breakout, one to three contracting pullback bars, then renewed participation."""
    if len(today_bars)<6 or atr<=0:return None
    sign=1 if bullish else -1
    latest=today_bars[-1]
    buffer=max(0.05,0.05*atr)
    for count in (1,2,3):
        index=len(today_bars)-count-2
        if index<3:continue
        base=today_bars[:index];br=today_bars[index];pull=today_bars[index+1:-1]
        sequence=[br]+pull+[latest]
        if any(b['timestamp']!=a['timestamp']+BAR_SECONDS for a,b in zip(sequence,sequence[1:])):continue
        level=(max(prior_high,orb_high,max(b['high'] for b in base)) if bullish else
               min(prior_low,orb_low,min(b['low'] for b in base)))
        width=br['high']-br['low']
        baseline=sum(b['volume'] for b in base[-10:])/len(base[-10:])
        if (width<=0 or baseline<=0 or br['volume']<1.2*baseline
                or sign*(br['close']-level)<=buffer
                or sign*(br['close']-br['open'])<0.60*width):continue
        # Pullback must contract participation, hold the old level and actually retrace.
        if any(b['volume']<=0 or b['volume']>0.8*br['volume'] or sign*(b['close']-level)<-buffer for b in pull):continue
        extreme=min(b['low'] for b in pull) if bullish else max(b['high'] for b in pull)
        distance=sign*(extreme-level)
        if not -0.25*atr<=distance<=0.35*atr:continue
        if not any(sign*(b['close']-br['close'])<0 for b in pull):continue
        if sign*((latest['low'] if bullish else latest['high'])-level)<-0.25*atr:continue
        confirmation_level=max(b['high'] for b in pull) if bullish else min(b['low'] for b in pull)
        if (sign*(latest['close']-confirmation_level)<=buffer or sign*(latest['close']-level)<=buffer
                or latest['volume']<1.2*max(b['volume'] for b in pull)
                or sign*(latest['close']-level)>0.75*atr):continue
        return {'setup':'PULLBACK_RETEST','trigger_level':level,'breakout_timestamp':br['timestamp'],
                'pullback_bars':count,'pullback_extreme':extreme,
                'pullback_volume_ratio':max(b['volume'] for b in pull)/br['volume'],
                'reconfirmation_volume_ratio':latest['volume']/max(b['volume'] for b in pull),
                'invalidation_level':extreme-sign*buffer}
    return None


def confirmed_consolidation_breakout(today_bars, atr, bullish):
    """A closed break from a compact, quiet base; no forming-bar prediction."""
    if len(today_bars)<7 or atr<=0:return None
    sign=1 if bullish else -1
    latest=today_bars[-1];buffer=max(0.05,0.05*atr)
    width=latest['high']-latest['low']
    if (width<=0 or width>1.5*atr or sign*(latest['close']-latest['open'])<0.60*width
            or sign*(latest['close']-(latest['low'] if bullish else latest['high']))<0.75*width):return None
    for count in range(6,2,-1):
        base=today_bars[-count-1:-1];earlier=today_bars[:-count-1]
        if len(base)!=count or len(earlier)<3:continue
        if any(b['timestamp']!=a['timestamp']+BAR_SECONDS for a,b in zip(base+[latest],(base+[latest])[1:])):continue
        if any(b['volume']<=0 for b in base):continue
        high=max(b['high'] for b in base);low=min(b['low'] for b in base)
        if not 0<high-low<=1.5*atr:continue
        middle=max(1,count//2)
        head=base[:middle];tail=base[-middle:]
        head_range=median([b['high']-b['low'] for b in head])
        if head_range<=0 or median([b['high']-b['low'] for b in tail])>1.15*head_range:continue
        if median([b['volume'] for b in tail])>1.10*median([b['volume'] for b in head]):continue
        pressure_key='low' if bullish else 'high'
        if any(sign*(b[pressure_key]-a[pressure_key]) < -0.10*atr for a,b in zip(base[-3:],base[-2:])):continue
        if sign*(base[-1]['close']-base[0]['close']) < -0.10*atr:continue
        trigger=high if bullish else low
        if not buffer<sign*(latest['close']-trigger)<=0.50*atr:continue
        baseline=median([b['volume'] for b in base])
        required=max(1.2*baseline,1.05*base[-1]['volume'])
        if latest['volume']<required:continue
        # A local break below a known high/above a known low needs room.
        obstacles=[max(b['high'] for b in earlier) if bullish else min(b['low'] for b in earlier)]
        room=min((sign*(v-latest['close']) for v in obstacles if sign*(v-latest['close'])>0),default=None)
        if room is not None and room<atr:continue
        return {'setup':'CONSOLIDATION_BREAKOUT','trigger_level':trigger,
                'base_start_timestamp':base[0]['timestamp'],'base_bars':count,
                'base_high':high,'base_low':low,'base_width_atr':(high-low)/atr,
                'base_volume_median':baseline,'required_volume':required,
                'breakout_volume_ratio':latest['volume']/baseline,'room_to_prior_extreme':room,
                'invalidation_level':(low-buffer if bullish else high+buffer)}
    return None


def order_flow_direction(flow, candle_start=None):
    """Use signed delta only when most of the completed candle was classified."""
    if not flow or flow.get("status") != "ESTIMATED":
        return 0
    if candle_start is not None and flow.get("candle_start_epoch") != candle_start:
        return 0
    volume = flow["total_candle_volume"]
    if volume <= 0 or flow["classified_fraction"] < 0.60:
        return 0
    delta = flow["estimated_delta"]
    return 1 if delta >= 0.15 * volume else -1 if delta <= -0.15 * volume else 0


def evaluate_stock_setup(
    candles: dict,
    as_of_epoch: float,
    min_day_change_pct: float = 2.2,
    ema_period: int = 10,
    enable_opening_trend: bool = False,
    reasons=None,
    confirm_window_sec: int = 300,
    previous_close=None,
    require_breakout=False,
    previous_session=None,
    order_flow=None,
) -> Optional[dict]:
    """Return a selective CE/PE setup, or None when any confirmation is absent.

    Scanner momentum, rising volume and price versus previous completed 15m
    EMA require directional confirmation. Strict breakout entries accept a fresh
    direct break or an explicitly confirmed low-volume pullback and retest.
    """
    def reject(reason):
        if reasons is not None:reasons.append(reason)
        return None
    if (not isinstance(ema_period, int) or isinstance(ema_period, bool) or ema_period < 2):
        return reject('Invalid EMA period')
    try:
        as_of_epoch = float(as_of_epoch)
        min_day_change_pct = float(min_day_change_pct)
        if not isfinite(as_of_epoch) or not isfinite(min_day_change_pct) or min_day_change_pct <= 0:
            return reject('Invalid decision time or day-change threshold')
        if isinstance(confirm_window_sec, bool) or not isinstance(confirm_window_sec, int) or not 300 <= confirm_window_sec <= 900:
            return reject("Invalid confirmation window")
        today = datetime.fromtimestamp(as_of_epoch, IST).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return reject('Invalid decision time or day-change threshold')
    bars = _closed_bars(candles, as_of_epoch)
    if not bars or len(bars) < 11 or bars[-1]["date"] != today:
        return reject('Insufficient current-session or malformed closed candles')
    latest = bars[-1]
    if not 0 <= as_of_epoch - (latest["timestamp"] + BAR_SECONDS) < confirm_window_sec:
        return reject('Stale closed confirmation candle')
    today_bars = [bar for bar in bars if bar["date"] == today]
    previous_bars = [bar for bar in bars if bar["date"] < today]
    if len(today_bars) < 2 or not previous_bars:
        return reject('Need prior close and two closed session candles')
    session_open = int(datetime.combine(today, SESSION_OPEN, IST).timestamp())
    opening = {bar["timestamp"]: bar for bar in today_bars}
    opening_range = [opening.get(session_open + offset) for offset in (0, 300, 600)]
    opening_range = [bar for bar in opening_range if bar is not None]
    orb_high = max((bar["high"] for bar in opening_range), default=latest["high"])
    orb_low = min((bar["low"] for bar in opening_range), default=latest["low"])
    previous = today_bars[-2]
    if latest["timestamp"] != previous["timestamp"] + BAR_SECONDS:
        return reject('Gap in the latest confirmation candles')

    reference_close=previous_bars[-1]['close'] if previous_close is None else float(previous_close)
    if not isfinite(reference_close) or reference_close<=0:return reject('Invalid previous close')
    day_change_pct = (latest["close"] / reference_close - 1.0) * 100.0
    if abs(day_change_pct) < min_day_change_pct:
        return reject('Closed day move below scanner threshold')
    if abs(day_change_pct)>5.5+1e-9:
        return reject('Closed day move exceeds 5.5% exhaustion cap')
    bars_15m = _completed_15m_bars(bars)
    if len(bars_15m) < ema_period + 2:
        return reject('Insufficient completed 15-minute EMA history')
    # Freshness and consecutive confirmation bars are checked above. Older gaps
    # may reduce trend observations without invalidating today's fresh setup.
    ema_values = _ema([bar["close"] for bar in bars_15m], ema_period)
    ema_value, previous_ema = ema_values[-1], ema_values[-2]
    atr_15m = _atr(bars_15m, ema_period)[-1]
    atr_5m = _atr(bars, 10)[-1]
    regime=opening_trend([{**b,'ts':b['timestamp']} for b in bars]) if enable_opening_trend else None
    if atr_5m <= 0 or atr_15m <= 0:
        return reject('Unavailable ATR')
    total_volume = sum(bar["volume"] for bar in today_bars)
    if total_volume <= 0:
        return reject('No current-session volume for VWAP')
    vwap = sum(((bar["high"] + bar["low"] + bar["close"]) / 3.0) * bar["volume"]
               for bar in today_bars) / total_volume
    average_volume = sum(bar["volume"] for bar in bars[-11:-1]) / 10.0
    if average_volume <= 0 or latest["volume"] <= 0:
        return reject('Confirmation volume below 1.2 times recent average')
    candle_range = latest["high"] - latest["low"]
    if candle_range <= 0 or abs(latest["close"] - latest["open"]) < 0.45 * candle_range:
        return reject('Weak confirmation candle body')

    body_fraction=abs(latest['close']-latest['open'])/candle_range
    buy_close=(latest['close']-latest['low'])/candle_range
    sell_close=(latest['high']-latest['close'])/candle_range
    volume_rising=latest['volume']>previous['volume']
    prepared=confirmed_consolidation_breakout(today_bars,atr_5m,day_change_pct>0) if require_breakout else None
    # A previous spike is not an ever-increasing volume hurdle. A lower-volume
    # continuation needs a fresh price break, strong close and EMA slope instead.
    buy_continuation=(latest['close']>previous['high'] and body_fraction>=0.60
                      and buy_close>=0.75 and ema_value>previous_ema)
    sell_continuation=(latest['close']<previous['low'] and body_fraction>=0.60
                       and sell_close>=0.75 and ema_value<previous_ema)
    regular_volume=latest['volume']>=1.2*average_volume
    adjusted_volume=False
    recent=today_bars[-4:-1]
    baseline=median([b['volume'] for b in recent]) if len(recent)==3 else 0.0
    opening_peak=max((b['volume'] for b in opening_range),default=0.0)
    adjusted_required=0.75*baseline
    # An opening burst can dominate the ten-bar mean long after its price
    # discovery. Only a fresh strong continuation with rising participation
    # may use a robust post-opening reference; ordinary entries keep 1.2x.
    if not regular_volume:
        contiguous=(len(recent)==3 and recent[0]['timestamp']>=session_open+900
                    and all(b['timestamp']==latest['timestamp']-(3-i)*BAR_SECONDS
                            for i,b in enumerate(recent)))
        adjusted_volume=(len(opening_range)==3 and contiguous and baseline>0
                         and opening_peak>=2.0*baseline and average_volume>1.25*baseline
                         and latest['volume']>=0.75*baseline and volume_rising
                         and ((day_change_pct>=min_day_change_pct and buy_continuation)
                              or (day_change_pct<=-min_day_change_pct and sell_continuation)))
        if not adjusted_volume and require_breakout:
            # The first post-opening breakout can follow two baseline bars.
            # Require an actual volume expansion, not the later 0.75x allowance.
            post=[b for b in today_bars[:-1] if b['timestamp']>=session_open+900][-3:]
            if len(post)>=2 and all(b['timestamp']==latest['timestamp']-(len(post)-i)*BAR_SECONDS
                                    for i,b in enumerate(post)):
                early_baseline=median([b['volume'] for b in post])
                if (early_baseline>0 and opening_peak>=2*early_baseline
                        and average_volume>1.25*early_baseline
                        and latest['volume']>=1.2*early_baseline and volume_rising
                        and (buy_continuation if day_change_pct>0 else sell_continuation)):
                    adjusted_volume=True;baseline=early_baseline;adjusted_required=1.2*baseline
        if not adjusted_volume and not prepared:
            return reject('Confirmation volume below 1.2 times recent average')
    bullish = (day_change_pct >= min_day_change_pct
               and (volume_rising or buy_continuation)
               and latest["close"] > previous_ema
               and latest["close"] > vwap
               and latest["close"] > max(previous["close"], latest["open"]))
    bearish = (day_change_pct <= -min_day_change_pct
               and (volume_rising or sell_continuation)
               and latest["close"] < previous_ema
               and latest["close"] < vwap
               and latest["close"] < min(previous["close"], latest["open"]))
    tolerance = 0.15 * atr_5m
    orb_bonus = ((bullish and latest["close"] > orb_high
                  and abs(previous["low"]-orb_high) <= tolerance and previous["close"] >= orb_high)
                 or (bearish and latest["close"] < orb_low
                     and abs(previous["high"]-orb_low) <= tolerance and previous["close"] <= orb_low))
    if not bullish and not bearish:
        return reject('Candle, VWAP, EMA and rising volume are not directionally aligned')
    relative_volume = latest["volume"] / average_volume
    body_ratio = abs(latest["close"] - latest["open"]) / candle_range
    close_strength = ((latest["close"] - latest["low"]) if bullish
                      else (latest["high"] - latest["close"])) / candle_range
    flow_direction = order_flow_direction(order_flow, latest["timestamp"])
    if flow_direction and flow_direction != (1 if bullish else -1):
        return reject("Completed candle order flow opposes the setup")
    breakout_confirmation=None
    if require_breakout:
        if (bullish and ema_value<=previous_ema) or (bearish and ema_value>=previous_ema):
            return reject('Breakout disagrees with the completed EMA slope')
        if (len(opening_range)!=3 or latest['timestamp']<session_open+900
                or any(b['timestamp']!=session_open+i*300 for i,b in enumerate(today_bars))):
            return reject('Need complete opening range and session levels for breakout')
        prior=[b for b in previous_bars if b['date']==previous_bars[-1]['date']]
        prior_open=int(datetime.combine(prior[0]['date'],SESSION_OPEN,IST).timestamp())
        daily=previous_session or {}
        if daily.get('date')==prior[0]['date'].isoformat() and daily.get('high') and daily.get('low'):
            try:prior_high,prior_low=float(daily['high']),float(daily['low'])
            except (TypeError,ValueError,OverflowError):return reject('Invalid previous-day breakout levels')
        elif len(prior)==75 and all(b['timestamp']==prior_open+i*300 for i,b in enumerate(prior)):
            prior_high=max(b['high'] for b in prior);prior_low=min(b['low'] for b in prior)
        else:return reject('Previous-day high/low coverage unavailable for breakout')
        if not all(isfinite(v) and v>0 for v in (prior_high,prior_low)) or prior_low>prior_high:
            return reject('Invalid previous-day breakout levels')
        trigger=(max(prior_high,orb_high,max(b['high'] for b in today_bars[:-1])) if bullish else
                 min(prior_low,orb_low,min(b['low'] for b in today_bars[:-1])))
        sign=1 if bullish else -1
        buffer=max(0.05,0.05*atr_5m)
        retest=confirmed_pullback_retest(today_bars,prior_high,prior_low,orb_high,orb_low,atr_5m,bullish)
        # Local bases only qualify on the already-broken side of prior-day and
        # opening levels. Basic scanner membership is still enforced upstream.
        if prepared and sign*(latest['close']-(max(prior_high,orb_high) if bullish else min(prior_low,orb_low)))<=buffer:
            prepared=None
            if not regular_volume and not adjusted_volume:
                return reject('Consolidation break has not cleared previous-day and opening levels')
        direct=sign*(latest['close']-trigger)>buffer
        if not direct and not retest and not prepared:
            return reject('No fresh closed breakout beyond previous-day and session levels')
        if prepared:
            trigger=prepared['trigger_level']
        elif retest:
            trigger=retest['trigger_level']
        elif sign*(latest['close']-trigger)>0.75*atr_5m:
            return reject('Direct breakout is overextended; wait for controlled pullback and reconfirmation')
        early_confirmation=datetime.fromtimestamp(latest['timestamp']+BAR_SECONDS,IST).time()<=time(9,45)
        minimum_body=0.55 if direct and not retest and not prepared else 0.60
        minimum_close=0.70 if direct and not retest and not prepared else (0.80 if early_confirmation else 0.75)
        if body_ratio<minimum_body or close_strength<minimum_close:
            return reject('Breakout confirmation body or closing strength insufficient')
        # Give a volume-backed break limited room while keeping the level and
        # EMA distance bounds and the live follow-through check.
        range_limit = 1.8 if flow_direction else 1.5
        if abs(latest['close']-ema_value)>3*atr_15m or candle_range>range_limit*atr_5m:
            return reject('Breakout candle is overextended')
        breakout_confirmation={'trigger_level':trigger,'previous_day_high':prior_high,'previous_day_low':prior_low,
            'opening_high':orb_high,'opening_low':orb_low,'buffer':buffer,
            'body_fraction':body_ratio,'close_strength':close_strength,'closed_candle':True,
            'relative_volume':relative_volume,'profile':'SCREENSHOT_REFERENCE_CLOSED_BREAKOUT',
            'entry_path':'CONSOLIDATION_BREAKOUT' if prepared else ('PULLBACK_RETEST' if retest else 'DIRECT_BREAKOUT'),
            'pullback':retest,'pre_breakout':prepared}
    # A scanner threshold can be crossed one candle after the opening-range
    # break. Admit that first confirmation, without exempting a whole trend
    # from the EMA-distance guard. All option quote/risk checks still follow.
    sign = 1 if bullish else -1
    level = orb_high if bullish else orb_low
    local_break=False
    if len(today_bars)>=7:
        base=today_bars[-4:-1]
        local_level=max(b['high'] for b in base) if bullish else min(b['low'] for b in base)
        if (max(b['high'] for b in base)-min(b['low'] for b in base)<=1.5*atr_5m
                and sign*(latest['close']-local_level)>0):
            local_break=True;level=local_level
    previous_day_change = (previous['close']/reference_close-1)*100
    threshold_cross = sign*previous_day_change < min_day_change_pct
    fresh_break = sign*(previous['close']-level) <= 0
    continuation = (
        len(opening_range) == 3 and latest['timestamp'] >= session_open+900
        and all(b['timestamp'] == session_open+300*i for i,b in enumerate(today_bars))
        and (threshold_cross or fresh_break)
        and sign*(ema_value-previous_ema) > 0
        and 0 < sign*(latest['close']-level) <= 2.25*atr_5m
        and abs(latest['close']-ema_value) <= 3.0*atr_15m
        and candle_range <= 2.0*atr_5m
        and relative_volume >= (1.2 if not volume_rising else 1.5)
        and body_ratio >= 0.60 and close_strength >= 0.75)
    if abs(latest['close']-ema_value) > 1.25*atr_15m and not regime and not continuation and not breakout_confirmation:
        return reject('EMA distance exceeds 1.25 ATR without a confirmed opening-trend break or bounded breakout continuation')
    # This is a ranking heuristic among already-qualified setups, not a win
    # probability. Capping the volume term prevents one spike dominating rank.
    quality_volume_ratio=(prepared['breakout_volume_ratio'] if prepared else
                          latest['volume']/baseline if require_breakout and adjusted_volume and baseline>0 else relative_volume)
    signal_quality = (40.0 * min(quality_volume_ratio / 3.0, 1.0)
                      + 30.0 * body_ratio + 20.0 * close_strength + 10.0 * orb_bonus)
    return {
        'breakout_confirmation':breakout_confirmation,
        "opt_type": "CE" if bullish else "PE",
        "scanner": ('CONFIRMED_BREAKOUT_CE' if bullish else 'CONFIRMED_BREAKOUT_PE') if breakout_confirmation else (("BREAKOUT_CONTINUATION_CE" if bullish else "BREAKOUT_CONTINUATION_PE") if continuation else ((regime['setup']+('_CE' if bullish else '_PE')) if regime else ("BULLISH_MOMENTUM_CONFIRMATION" if bullish else "BEARISH_MOMENTUM_CONFIRMATION"))),
        "breakout_continuation": {"trigger_level":level,"threshold_cross":threshold_cross,
                                  "local_consolidation_break":local_break,
                                  "distance_atr":sign*(latest['close']-level)/atr_5m} if continuation else None,
        "cash_price_pressure":"BULLISH_CONFIRMED" if bullish else "BEARISH_CONFIRMED",
        "volume_confirmation":("CONSOLIDATION_VOLUME_EXPANSION" if prepared else "OPENING_SPIKE_ADJUSTED_STRONG_CONTINUATION" if adjusted_volume else
                               "RISING_CANDLE_VOLUME" if volume_rising else "ABOVE_AVERAGE_STRONG_PRICE_CONTINUATION"),
        "volume_context":{"confirmation_volume":latest['volume'],"regular_average":average_volume,
                          "regular_ratio":latest['volume']/average_volume,
                          "post_opening_median":baseline if adjusted_volume else None,
                          "required_volume":prepared['required_volume'] if prepared else adjusted_required if adjusted_volume else 1.2*average_volume,
                          "opening_peak":opening_peak,"opening_adjustment_used":adjusted_volume},
        "market_regime":regime,
        "spot_ltp": latest["close"],
        "day_change_pct": day_change_pct,
        "ema10_15m": ema_value,
        "ema10_15m_slope": ema_value-previous_ema,
        "higher_timeframe_aligned": ((ema_value>previous_ema and latest["close"]>ema_value)
                                     if bullish else (ema_value<previous_ema and latest["close"]<ema_value)),
        "conf_high": latest["high"],
        "conf_low": latest["low"],
        "atr_5m": atr_5m,
        "signal_timestamp": latest["timestamp"],
        "conf_close": latest["close"],
        "vwap": vwap,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "relative_volume": relative_volume,
        "body_ratio": body_ratio,
        "signal_quality": round(signal_quality, 4),
        'quality_volume_ratio':quality_volume_ratio,
    }

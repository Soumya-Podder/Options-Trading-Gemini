"""Opening-gap continuation evidence from already-closed session candles."""
import math


def opening_trend(bars, min_gap_pct=0.5, min_drive_pct=None, allow_drive=False):
    """Recognize a fresh opening-range break; never authorize chasing a whole move.

    Caller validates candle times/OHLC and supplies only closed candles. The first
    three session bars define a level before the confirmation bar. After that
    first break, require a new local consolidation break rather than every trend
    candle. Volume, OI, executable quotes and risk remain the caller's checks.
    """
    if len(bars)<5:return None
    day=bars[-1]['date']
    today=[b for b in bars if b['date']==day]
    prior=[b for b in bars if b['date']<day]
    if len(today)<4 or not prior:return None
    if any(b['ts']-a['ts']!=300 for a,b in zip(today,today[1:])):return None
    # A partial opening range cannot stand in for the 09:15–09:30 range.
    from datetime import datetime,timezone,timedelta
    dt=datetime.fromtimestamp(today[0]['ts'],timezone(timedelta(hours=5,minutes=30)))
    if (dt.hour,dt.minute)!=(9,15):return None
    gap=(today[0]['open']/prior[-1]['close']-1)*100
    drive=(today[2]['close']/today[0]['open']-1)*100
    min_drive_pct=min_gap_pct if min_drive_pct is None else min_drive_pct
    if not all(math.isfinite(v) for v in (gap,drive)):return None
    if abs(gap)>=min_gap_pct:
        sign=1 if gap>0 else -1;basis='GAP'
    elif allow_drive and abs(drive)>=min_drive_pct:
        sign=1 if drive>0 else -1;basis='OPENING_DRIVE'
    else:return None
    latest,previous=today[-1],today[-2]
    width=latest['high']-latest['low']
    if width<=0 or sign*(latest['close']-latest['open'])/width<0.5:return None
    strength=(latest['close']-latest['low'] if sign>0 else latest['high']-latest['close'])/width
    if strength<0.7:return None
    orb=max(b['high'] for b in today[:3]) if sign>0 else min(b['low'] for b in today[:3])
    if sign*(latest['close']-orb)<=0:return None
    first_break=sign*(previous['close']-orb)<=0
    level=orb
    if not first_break:
        if len(today)<7:return None
        base=today[-4:-1]
        level=max(b['high'] for b in base) if sign>0 else min(b['low'] for b in base)
        tr=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
            for a,b in zip(today[:-1],today[1:-1])]
        if not tr:return None
        scale=sum(tr[-10:])/len(tr[-10:])
        if scale<=0 or max(b['high'] for b in base)-min(b['low'] for b in base)>1.5*scale:return None
        if sign*(latest['close']-level)<=0:return None
    # Confirmation must remain near its break, not many candles down the trend.
    if sign*(latest['close']-level)>0.75*width:return None
    return {'direction':'BUY_CE' if sign>0 else 'BUY_PE','gap_pct':round(gap,4),'opening_drive_pct':round(drive,4),
            'trigger_level':level,'setup':('OPENING_GAP_BREAK' if basis=='GAP' else 'OPENING_DRIVE_BREAK') if first_break else 'TREND_CONSOLIDATION_BREAK',
            'confirmation_close':latest['close']}

"""Conservative OHLC replay. Never estimate another contract's prices."""
import math
from datetime import datetime


class ReplayDataError(ValueError):
    def __init__(self, reason, index):
        super().__init__(reason)
        self.reason, self.index = reason, index


def replay_option_trade(entry_idx,is_ce,timestamps,opens,highs,lows,closes,strikes,
                        sl_points,interval_min,index,lots,lot_size,exit_fills,
                        split,ratchet,eod_time,entry_at_open=False,
                        expected_strike=None,slippage_pct=0.0,require_session_end=False,
                        session_profit_check=None,current_session_policy=False):
    arrays=(opens,highs,lows,closes,strikes)
    if (not timestamps or any(len(a)!=len(timestamps) for a in arrays)
            or not 0<=entry_idx<len(timestamps) or not math.isfinite(sl_points) or sl_points<=0
            or not math.isfinite(slippage_pct) or not 0<=slippage_pct<100):
        raise ReplayDataError("Malformed replay inputs",entry_idx)

    def bar(j):
        try:
            stamp=float(timestamps[j]); strike=float(strikes[j])
            values=tuple(float(a[j]) for a in (opens,highs,lows,closes))
            o,h,l,c=values
            if (not all(math.isfinite(v) for v in (stamp,strike,*values))
                    or stamp!=int(stamp) or strike<=0 or min(values)<=0
                    or not l<=min(o,c)<=max(o,c)<=h):
                raise ValueError()
            return int(stamp),strike,values
        except (ValueError,TypeError,OverflowError):
            raise ReplayDataError("Malformed option bar",j)

    stamp,entry_strike,values=bar(entry_idx)
    if expected_strike is not None and entry_strike!=float(expected_strike):
        raise ReplayDataError("Contract changed before executable entry",entry_idx)
    entry_day=datetime.fromtimestamp(stamp).date()
    if entry_at_open and entry_idx>0:
        if stamp-float(timestamps[entry_idx-1])!=interval_min*60:
            raise ReplayDataError("Missing entry bar",entry_idx)
    raw_entry=values[0] if entry_at_open else values[3]
    entry=(math.ceil(raw_entry*(1+slippage_pct/100)*20-1e-9)/20 if slippage_pct else raw_entry)
    stop=round(entry-sl_points,2)
    if stop<=0:
        raise ReplayDataError("Nonpositive initial stop",entry_idx)
    peak=entry
    armed=False
    remaining=lots*lot_size
    fills=exit_fills if exit_fills is not None else []
    if fills:
        raise ReplayDataError("Replay fill list must start empty",entry_idx)
    first_done=False

    def sell(price):
        return (math.floor(price*(1-slippage_pct/100)*20+1e-9)/20 if slippage_pct else round(price,2))

    def finish(j,price,reason):
        fill=sell(price)
        if split:
            fills.append({"qty":remaining,"price":fill,"timestamp":str(datetime.fromtimestamp(timestamps[j])),"reason":reason})
            average=sum(f["qty"]*f["price"] for f in fills)/(lots*lot_size)
            return j,average,round((average-entry)/sl_points,2),reason
        return j,fill,round((fill-entry)/sl_points,2),reason

    def target_one(j):
        nonlocal remaining,first_done
        fills.append({"qty":split[0],"price":sell(entry+10),"timestamp":str(datetime.fromtimestamp(timestamps[j])),"reason":"TARGET_1_10_POINTS"})
        remaining-=split[0]
        first_done=True

    def daily_trigger(high):
        if session_profit_check is None or not session_profit_check(entry,remaining,fills,sell(high)):
            return None
        lo,hi=1,int(math.floor(high*20+1e-9))
        while lo<hi:
            mid=(lo+hi)//2
            if session_profit_check(entry,remaining,fills,sell(mid/20)):hi=mid
            else:lo=mid+1
        return lo/20

    start=entry_idx if entry_at_open else entry_idx+1
    if start>=len(timestamps):
        raise ReplayDataError("No post-entry observations",entry_idx)
    for j in range(start,len(timestamps)):
        # A bar beginning at/after cutoff cannot affect this session's exit,
        # including its contract, high/low or malformed/missing OHLC values.
        current_stamp=float(timestamps[j])
        dt=datetime.fromtimestamp(current_stamp)
        if dt.date()!=entry_day or dt.time()>=eod_time:
            # No overnight open or not-yet-closed next candle may set the exit.
            if j<=entry_idx:
                raise ReplayDataError("Entry outside intraday session",j)
            last_close=datetime.fromtimestamp(float(timestamps[j-1])+interval_min*60)
            if require_session_end and last_close.time()<eod_time:
                raise ReplayDataError("History ends before session close",j-1)
            return finish(j-1,float(closes[j-1]),"EOD_SQUAREOFF")
        current_stamp,strike,(o,h,l,c)=bar(j)
        if j>entry_idx and current_stamp-float(timestamps[j-1])!=interval_min*60:
            raise ReplayDataError("Missing post-entry option bars",j)
        if strike!=entry_strike:
            raise ReplayDataError("Rolling strike changed; fixed-contract OHLC unavailable",j)
        reason="ASYMMETRIC_TRAIL_PROFIT" if armed and stop>entry else "INITIAL_SL_HIT"
        # The opening price is observed before this candle's high and low.
        if o<=stop:
            return finish(j,o,reason)
        if current_session_policy and (j-entry_idx)*interval_min>=25 and peak-entry<0.8*sl_points:
            return finish(j,o,"THETA_TIME_STOP")
        if session_profit_check is not None and session_profit_check(entry,remaining,fills,sell(o)):
            return finish(j,o,"DAILY_NET_PROFIT_TARGET")
        if split:
            if not first_done and o>=entry+10:
                target_one(j)
            if o>=entry+20:
                return finish(j,entry+20,"TARGET_2_20_POINTS")
        # Unknown intrabar order uses the existing stop before later targets.
        if l<=stop:
            return finish(j,stop,reason)
        daily=daily_trigger(h)
        if split and not first_done and h>=entry+10 and (daily is None or entry+10<daily):
            target_one(j)
            daily=daily_trigger(h)
        if daily is not None and (not split or daily<=entry+20):
            return finish(j,daily,"DAILY_NET_PROFIT_TARGET")
        if split:
            if not first_done and h>=entry+10:
                target_one(j)
            if h>=entry+20:
                return finish(j,entry+20,"TARGET_2_20_POINTS")
        peak=max(peak,h)
        elapsed=(j-entry_idx+(1 if entry_at_open else 0))*interval_min
        if not current_session_policy and elapsed>=25 and peak-entry<0.8*sl_points:
            return finish(j,c,"THETA_TIME_STOP")
        # A high from this candle can only move the stop for following candles.
        if not split:
            stop,new_armed=ratchet(entry,stop,peak,sl_points)
            armed=armed or new_armed
    end=datetime.fromtimestamp(float(timestamps[-1])+interval_min*60)
    if require_session_end and end.time()<eod_time:
        raise ReplayDataError("History ends before session close",len(timestamps)-1)
    return finish(len(timestamps)-1,float(closes[-1]),
                  "EOD_SQUAREOFF" if end.date()==entry_day and end.time()>=eod_time else "END_OF_WINDOW")

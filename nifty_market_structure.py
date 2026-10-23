"""Closed-candle Nifty price structure and strike-level OI entry checks."""
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def assess_nifty_structure(candles, chain, as_of, spot, direction,
                           buffer=3.0, min_room=20.0, min_structure_rr=1.5):
    """Return auditable levels and a gate; never treat OI as a certain barrier."""
    result = {"allowed": False, "ready":False,"reason": "Price structure unavailable", "source": "INDEX_5M_OHLC"}
    try:
        now = datetime.fromtimestamp(as_of, IST)
        if not all(math.isfinite(x) for x in (as_of, spot, buffer, min_room, min_structure_rr)) or min(spot,buffer,min_room,min_structure_rr)<=0:
            return result
        fields = ("timestamp", "open", "high", "low", "close")
        if not isinstance(candles,dict) or any(len(candles[k]) != len(candles["timestamp"]) for k in fields):
            return result
        bars=[]
        seen=set()
        for i,t in enumerate(candles["timestamp"]):
            t=float(t)
            if not math.isfinite(t) or t != int(t) or t in seen:
                return result
            seen.add(t)
            if t+300 > as_of:
                continue
            dt=datetime.fromtimestamp(t,IST)
            if not (9,15) <= (dt.hour,dt.minute) <= (15,25):
                continue
            values={k:float(candles[k][i]) for k in fields[1:]}
            if not all(math.isfinite(v) and v>0 for v in values.values()) or not values["low"]<=min(values["open"],values["close"])<=max(values["open"],values["close"])<=values["high"]:
                return result
            bars.append({**values,"ts":t,"date":dt.date()})
        bars.sort(key=lambda b:b["ts"])
        today=[b for b in bars if b["date"]==now.date()]
        prior=[b for b in bars if b["date"]<now.date()]
        if len(today)<5 or not prior:
            result["reason"]="Need prior-session data and five closed session candles"
            return result
        latest,previous=today[-1],today[-2]
        if not 0<=as_of-(latest["ts"]+300)<300 or latest["ts"]-previous["ts"]!=300:
            result["reason"]="Stale or gapped confirmation candles"
            return result
        previous_day=prior[-1]["date"]
        previous_session=[b for b in prior if b["date"]==previous_day]
        if (len(previous_session)!=75 or any(b["ts"]-a["ts"]!=300 for a,b in zip(previous_session,previous_session[1:]))
                or datetime.fromtimestamp(previous_session[0]["ts"],IST).time().isoformat()!="09:15:00"
                or datetime.fromtimestamp(previous_session[-1]["ts"],IST).time().isoformat()!="15:25:00"):
            result["reason"]="Prior-session high/low coverage incomplete"
            return result
        opening=[b for b in today if datetime.fromtimestamp(b["ts"],IST).hour==9 and datetime.fromtimestamp(b["ts"],IST).minute in (15,20,25)]
        if len(opening)!=3 or any(b["ts"]-a["ts"]!=300 for a,b in zip(today,today[1:])):
            result["reason"]="Opening range or session candle coverage incomplete"
            return result
        levels={"previous_day_high":max(b["high"] for b in previous_session),
                "previous_day_low":min(b["low"] for b in previous_session),
                "day_high":max(b["high"] for b in today),"day_low":min(b["low"] for b in today),
                "opening_high":max(b["high"] for b in opening),"opening_low":min(b["low"] for b in opening),
                "range_high_before_confirmation":max(b["high"] for b in today[:-1]),
                "range_low_before_confirmation":min(b["low"] for b in today[:-1]),
                "range_high_before_breakout":max(b["high"] for b in today[:-2]),
                "range_low_before_breakout":min(b["low"] for b in today[:-2])}
        # A pivot needs two already-closed candles on each side.
        swings=[]
        for i in range(2,len(today)-2):
            window=today[i-2:i+3]
            if today[i]["high"]==max(b["high"] for b in window): swings.append(today[i]["high"])
            if today[i]["low"]==min(b["low"] for b in window): swings.append(today[i]["low"])
        puts,calls=[],[]
        for strike,node in chain.items():
            strike=float(strike)
            if not math.isfinite(strike) or abs(strike-spot)>500:
                continue
            for side,rows in (("ce",calls),("pe",puts)):
                leg=node.get(side,{})
                oi=float(leg.get("oi",0) or 0)
                old=float(leg.get("previous_oi",oi) or 0)
                if math.isfinite(oi) and math.isfinite(old) and oi>0:
                    rows.append({"strike":strike,"oi":oi,"change_from_previous_close":oi-old})
        if not puts or not calls:
            result.update(reason="Strike-level call/put OI unavailable",levels=levels)
            return result
        put_wall=max(puts,key=lambda x:x["oi"])
        call_wall=max(calls,key=lambda x:x["oi"])
        result.update(ready=True,levels=levels,put_oi_wall=put_wall,call_oi_wall=call_wall,
                      swing_levels=sorted(set(swings)),confirmation_close=latest["close"])
        if direction not in ("BUY_CE","BUY_PE"):
            result["reason"]="No directional OI/momentum signal"
            return result
        bullish=direction=="BUY_CE"
        sign=1 if bullish else -1
        if sign*(latest["close"]-latest["open"])<=0 or sign*(spot-latest["close"]) < -buffer:
            result["reason"]="Price has not held directional confirmation"
            return result
        candle_range=latest["high"]-latest["low"]
        body=abs(latest["close"]-latest["open"])
        close_strength=(latest["close"]-latest["low"] if bullish else latest["high"]-latest["close"])
        if candle_range<=0 or body/candle_range<0.5 or close_strength/candle_range<0.7:
            result["reason"]="Weak follow-through candle body or closing location"
            return result
        recent=today[-10:]
        ranges=[max(b["high"]-b["low"],abs(b["high"]-a["close"]),abs(b["low"]-a["close"])) for a,b in zip(recent,recent[1:])]
        chase_limit=max(buffer,0.25*sum(ranges)/len(ranges))
        if sign*(spot-latest["close"])>chase_limit:
            result["reason"]="Current price has advanced beyond the confirmation entry zone"
            return result
        # Never use the confirmation candle's own new extreme as a pre-existing level.
        entry_levels=sorted(set([v for k,v in levels.items() if k not in (
            "day_high","day_low","range_high_before_confirmation","range_low_before_confirmation")]
            +swings+[put_wall["strike"],call_wall["strike"]]))
        events=[]
        for level in entry_levels:
            beyond=sign*(latest["close"]-level)>buffer
            previous_beyond=sign*(previous["close"]-level)>buffer
            held=beyond and previous_beyond and sign*(today[-3]["close"]-level)<=buffer
            touched=(latest["low"]<=level+buffer and latest["high"]>=level-buffer)
            continued=sign*(latest["close"]-previous["close"])>=0
            retest=beyond and previous_beyond and touched
            previous_touch=previous["low"]<=level+buffer and previous["high"]>=level-buffer
            rejected=(beyond and previous_beyond and previous_touch
                      and sign*(latest["close"]-(previous["high"] if bullish else previous["low"]))>buffer)
            if continued and (held or retest or rejected):
                setup="RETEST_CONFIRMED" if retest else "BREAKOUT_HOLD" if held else "REJECTION_CONFIRMED"
                events.append((abs(spot-level),level,setup))
        if not events:
            result["reason"]="Wait for a separate closed-candle hold/retest after the level event"
            return result
        _,level,event=min(events)
        # Exclude the latest candle's high/low from the next pre-existing obstacle.
        opposing=[v for v in entry_levels if sign*(v-spot)>0]
        room=min((sign*(v-spot) for v in opposing),default=None)
        risk=abs(spot-(min(level,latest["low"]) if bullish else max(level,latest["high"])))
        result.update(setup=event,trigger_level=level,next_opposing_level=(spot+sign*room if room is not None else None),
                      room_points=room,structural_risk_points=risk,
                      structure_rr=(room/risk if room is not None and risk>0 else None))
        if room is None:
            result["reason"]="No mapped opposing level to assess room; insufficient structure"
        elif room<min_room or risk<=0 or room/risk<min_structure_rr:
            result["reason"]="Insufficient room before opposing support/resistance"
        else:
            result.update(allowed=True,reason="Confirmed level event with sufficient room")
        return result
    except (KeyError,TypeError,ValueError,OverflowError,OSError):
        return result

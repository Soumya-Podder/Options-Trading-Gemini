"""Closed-candle Nifty price structure and strike-level OI entry checks."""
import math
from datetime import datetime, timedelta, timezone
from market_regime import opening_trend

IST = timezone(timedelta(hours=5, minutes=30))


def assess_nifty_structure(candles, chain, as_of, spot, direction,
                           buffer=3.0, min_room=20.0, min_structure_rr=1.5, trigger_levels=None,
                           include_nearest=False, enable_opening_trend=False, enable_opening_drive=False):
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
        if len(today)<(4 if enable_opening_trend else 5) or not prior:
            count="four" if enable_opening_trend else "five"
            result["reason"]=f"Need prior-session data and {count} closed session candles"
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
        regime=opening_trend(bars,min_gap_pct=0.2 if enable_opening_drive else 0.5,
                             min_drive_pct=0.2,allow_drive=enable_opening_drive) if enable_opening_trend else None
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
                previous_oi=leg.get("previous_oi")
                old=float(previous_oi) if previous_oi is not None else oi
                if math.isfinite(oi) and math.isfinite(old) and oi>0 and old>=0:
                    rows.append({"strike":strike,"oi":oi,"change_from_previous_close":oi-old})
        if not puts or not calls:
            result.update(reason="Strike-level call/put OI unavailable",levels=levels)
            return result
        put_wall=max(puts,key=lambda x:x["oi"])
        call_wall=max(calls,key=lambda x:x["oi"])
        put_buildup=max((r for r in puts if r["change_from_previous_close"]>0),
                        key=lambda r:r["change_from_previous_close"],default=None)
        call_buildup=max((r for r in calls if r["change_from_previous_close"]>0),
                         key=lambda r:r["change_from_previous_close"],default=None)
        # Local reactions need the nearest mapped strikes too; a distant maximum
        # must not make an otherwise confirmed nearby level invisible.
        nearest_put=max((r for r in puts if r["strike"]<=spot),key=lambda r:r["strike"],default=None)
        nearest_call=min((r for r in calls if r["strike"]>=spot),key=lambda r:r["strike"],default=None)
        result.update(ready=True,levels=levels,put_oi_wall=put_wall,call_oi_wall=call_wall,
                      opening_trend=regime,
                      put_oi_buildup=put_buildup,call_oi_buildup=call_buildup,
                      nearest_put_oi_level=nearest_put,nearest_call_oi_level=nearest_call,
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
            +swings+[r["strike"] for r in (put_wall,call_wall,put_buildup,call_buildup) if r]
            +([r["strike"] for r in (nearest_put,nearest_call) if r] if include_nearest else [])))
        result["entry_levels"]=entry_levels
        events=[]
        if regime and regime['direction']==direction:
            level=regime['trigger_level']
            if trigger_levels is None or level in trigger_levels:
                events.append((abs(spot-level),level,regime['setup']))
                # A consolidation level is known before its breakout candle.
                entry_levels=sorted(set(entry_levels+[level]))
                result['entry_levels']=entry_levels
        for level in entry_levels:
            if trigger_levels is not None and level not in trigger_levels:
                continue
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


def apply_nifty_level_entry(analysis, candles, chain, as_of, include_nearest=False,
                            enable_opening_trend=False, enable_opening_drive=False, enable_price_levels=False):
    """Generate direction from a marked OI level, closed reaction and same-strike OI.

    Mark walls from the five-minute reference, before the confirmation candle.
    Daily buildup locates levels; only matched intraday OI/premium changes confirm.
    Mutates the analysis used by both execution and replay, leaving basket tags
    available as diagnostics rather than direction prerequisites.
    """
    analysis["entry_model"]="LEVEL_CANDLE_AND_STRIKE_OI_V1"
    analysis['entry_profile']={'opening_trend':enable_opening_trend,'opening_drive':enable_opening_drive,
                               'nearest_level_expansion':include_nearest,'price_level_confirmation':enable_price_levels}
    analysis["recommended_dir"]="HOLD"
    rejected={"ready":False,"allowed":False,"reason":"Need matched five-minute strike OI", "source":"INDEX_5M_OHLC"}
    analysis["market_structure"]=rejected
    oi=analysis.get("oi_analysis",{})
    if not oi.get("ready"):
        return rejected
    marked={}
    confirmations={"BUY_CE":{},"BUY_PE":{}}
    for row in oi.get("nearby_strikes",[]):
        try:
            strike=float(row["strike"]);side=row["side"]
            change=float(row["change_over_5m"])
            premium=float(row["premium_change_over_5m"])
            current=float(row["oi"]);prior=current-change
            if side not in ("ce","pe") or prior<=0 or not all(math.isfinite(v) for v in (strike,change,premium,current,prior)):
                continue
            daily=row.get("change_from_previous_day")
            leg={"oi":prior}
            if daily is not None:
                previous=current-float(daily)
                if not math.isfinite(previous) or previous<0:continue
                leg["previous_oi"]=previous
            marked.setdefault(str(strike),{})[side]=leg
            direction=None
            if change>0 and premium<0:
                direction="BUY_CE" if side=="pe" else "BUY_PE"
            elif change<0 and premium>0:
                direction="BUY_CE" if side=="ce" else "BUY_PE"
            if direction:
                confirmations[direction].setdefault(strike,[]).append(dict(row))
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    # Include local mapped levels, not just the largest walls. Same-strike
    # OI/premium confirmation remains mandatory for every trigger.
    probe=assess_nifty_structure(candles,marked,as_of,analysis["spot"],"HOLD",include_nearest=include_nearest,
                                 enable_opening_trend=enable_opening_trend,enable_opening_drive=enable_opening_drive)
    if not probe.get("ready"):
        analysis["market_structure"]=probe
        return probe
    wall_keys=("put_oi_wall","call_oi_wall","put_oi_buildup","call_oi_buildup")
    if include_nearest:wall_keys+=("nearest_put_oi_level","nearest_call_oi_level")
    walls={r["strike"] for key in wall_keys
           if (r:=probe.get(key))}
    candidates=[]
    checks={}
    for direction,rows in confirmations.items():
        rows=dict(rows)
        triggers=walls.intersection(rows)
        available_strikes=sorted({float(k) for k in marked})
        if enable_price_levels and available_strikes:
            price_levels=[v for k,v in probe['levels'].items() if k not in (
                'day_high','day_low','range_high_before_confirmation','range_low_before_confirmation')]
            price_levels+=probe.get('swing_levels',[])
            for level in price_levels:
                nearest=min(available_strikes,key=lambda k:(abs(k-level),k))
                if nearest in rows and abs(nearest-level)<=25:
                    rows[level]=rows[nearest];triggers.add(level)
        regime=probe.get('opening_trend')
        oi_anchor=None
        if regime and regime['direction']==direction and rows:
            # Opening-range prices need not be exact option strikes. Match the
            # nearest observed strike within half a Nifty strike interval; use
            # that identical contract for both OI and premium confirmation.
            level=regime['trigger_level']
            nearest=min(available_strikes,key=lambda k:(abs(k-level),k)) if available_strikes else None
            if nearest in rows and abs(nearest-level)<=25:
                rows[level]=rows[nearest];triggers.add(level);oi_anchor=nearest
        structure=assess_nifty_structure(candles,marked,as_of,analysis["spot"],direction,
                                        trigger_levels=triggers,include_nearest=include_nearest,
                                        enable_opening_trend=enable_opening_trend,enable_opening_drive=enable_opening_drive)
        structure.update(entry_model=analysis["entry_model"],level_reference_age_seconds=oi.get("reference_age_seconds"))
        checks[direction]={k:structure.get(k) for k in ("allowed","reason","trigger_level","setup","room_points","structure_rr")}
        checks[direction]["oi_confirmed_levels"]=sorted(triggers)
        if structure.get("allowed"):
            structure["strike_oi_confirmation"]=rows[structure["trigger_level"]]
            structure['oi_confirmation_strike']=structure['strike_oi_confirmation'][0]['strike']
            if oi_anchor is not None and structure['trigger_level']==regime['trigger_level']:
                structure['oi_confirmation_strike']=oi_anchor
            candidates.append((direction,structure))
        rejected=structure
    if len(candidates)==1:
        direction,structure=candidates[0]
        analysis["recommended_dir"]=direction
    else:
        structure=rejected
        structure.update(allowed=False,reason="Ambiguous level direction" if candidates else
                         "No marked level with both closed-candle reaction and supportive same-strike OI")
    analysis["market_structure"]=structure
    structure["direction_checks"]=checks
    return structure

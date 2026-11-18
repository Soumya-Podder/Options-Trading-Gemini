"""Nifty OI comparisons using actual, identical contracts across snapshots."""
import math
from collections import deque
from datetime import datetime, timedelta, timezone

IST=timezone(timedelta(hours=5,minutes=30))
HORIZON_WINDOWS_SEC = {'5m': 300, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400}


def compute_multi_horizon_oi_iv_delta_matrix(tracker, now_ts, spot, current_nodes, current_info=None, option_type=None):
    """Compare identical current ATM +/-3 contracts with genuinely aged anchors."""
    atm = math.floor(spot / tracker.strike_step + .5) * tracker.strike_step
    keys = [(atm + i * tracker.strike_step, side) for i in range(-3, 4) for side in ('ce', 'pe')]
    history = list(tracker.history)
    snapshots = {}
    for name, seconds in HORIZON_WINDOWS_SEC.items():
        eligible = [s for s in history if seconds <= now_ts - s['time'] <= seconds + 90]
        snapshots[name] = eligible[-1] if eligible else None
    snapshots['morning'] = getattr(tracker, 'morning_snapshot', None)
    horizons = {}
    for name, anchor in snapshots.items():
        row = {'available': False, 'reason': 'Required horizon anchor unavailable',
               'reference_epoch': anchor['time'] if anchor else None,
               'reference_age_seconds': round(now_ts - anchor['time'], 2) if anchor else None,
               'ce_oi_change': None, 'pe_oi_change': None, 'net_put_minus_call_writing': None,
               'dominant_writer': 'UNAVAILABLE', 'contracts': []}
        if not anchor or anchor['time'] >= now_ts:
            horizons[name] = row
            continue
        for key in keys:
            current, prior = current_nodes.get(key), anchor['nodes'].get(key)
            if not current or not prior:
                continue
            if current['security_id'] and prior['security_id'] and current['security_id'] != prior['security_id']:
                continue
            iv_valid = current['iv'] > 0 and prior['iv'] > 0
            delta_valid = 0 < abs(current['delta']) <= 1 and 0 < abs(prior['delta']) <= 1
            row['contracts'].append({
                'strike': key[0], 'side': key[1], 'security_id': current['security_id'],
                'oi_change': current['oi'] - prior['oi'],
                'premium_change': current['price'] - prior['price'],
                'volume_change': (current['volume'] - prior['volume']) if current['volume'] >= prior['volume'] else None,
                'iv_change': current['iv'] - prior['iv'] if iv_valid else None,
                'delta_change': current['delta'] - prior['delta'] if delta_valid else None,
                'absolute_delta_change': abs(current['delta']) - abs(prior['delta']) if delta_valid else None,
            })
        row['matched_contracts'] = len(row['contracts'])
        if len(row['contracts']) != len(keys):
            row['reason'] = 'Incomplete identical-contract coverage'
            horizons[name] = row
            continue
        ce = sum(r['oi_change'] for r in row['contracts'] if r['side'] == 'ce')
        pe = sum(r['oi_change'] for r in row['contracts'] if r['side'] == 'pe')
        net = pe - ce
        dominant = 'BALANCED'
        if abs(net) > .18 * max(abs(ce), abs(pe), 1):
            dominant = 'PUT_SIDE_RELATIVE_BUILDUP' if net > 0 else 'CALL_SIDE_RELATIVE_BUILDUP'
        row.update(available=True, reason='Identical-contract horizon ready',
                   ce_oi_change=ce, pe_oi_change=pe, net_put_minus_call_writing=net, dominant_writer=dominant)
        for side in ('ce', 'pe'):
            contracts = [r for r in row['contracts'] if r['side'] == side]
            row[side + '_writing_evidence_oi'] = sum(max(0, r['oi_change']) for r in contracts if r['premium_change'] < 0)
            row[side + '_covering_evidence_oi'] = sum(max(0, -r['oi_change']) for r in contracts if r['premium_change'] > 0)
            for field in ('iv_change', 'delta_change', 'absolute_delta_change'):
                values = [r[field] for r in contracts if r[field] is not None]
                row[side + '_' + field] = sum(values) / len(values) if len(values) == len(contracts) else None
            atm_row = next(r for r in contracts if r['strike'] == atm)
            row['atm_' + side] = {field: atm_row[field] for field in ('iv_change', 'delta_change', 'absolute_delta_change')}
        horizons[name] = row
    bullish = sum(r['available'] and r['dominant_writer'] == 'PUT_SIDE_RELATIVE_BUILDUP' for r in horizons.values())
    bearish = sum(r['available'] and r['dominant_writer'] == 'CALL_SIDE_RELATIVE_BUILDUP' for r in horizons.values())
    short = [horizons[k] for k in ('5m', '10m', '15m')]
    long = [horizons[k] for k in ('30m', '1h', '4h', 'morning')]
    decisions = {}
    for side, opposing in (('CE', 'CALL_SIDE_RELATIVE_BUILDUP'), ('PE', 'PUT_SIDE_RELATIVE_BUILDUP')):
        blocked = (sum(r['available'] and r['dominant_writer'] == opposing for r in short) >= 2
                   and sum(r['available'] and r['dominant_writer'] == opposing for r in long) >= 2)
        decisions[side] = {'multi_horizon_supported': not blocked,
                           'alignment_score': 30 if blocked else 65,
                           'veto_reasons': ['PERSISTENT_OPPOSING_MULTI_HORIZON_OI'] if blocked else []}
    result = {'horizons': horizons, 'atm_strike': atm, 'strike_radius': 3, 'snapshot_epoch': now_ts,
              'bullish_horizons': bullish, 'bearish_horizons': bearish, 'directions': decisions,
              'morning_basis': 'First observed snapshot in this date/expiry session',
              'interpretation': 'OI differences measure net positioning; price/OI pairs are writing or covering evidence, not participant identification'}
    if option_type in decisions:
        result.update(decisions[option_type])
    return result


def validate_option_greeks_and_iv(node, option_type, *, iv_change_5m=None,
                                  expected_spot_move=0.0, is_expiry_day=False):
    """Check the selected contract, using daily theta over a 20-minute horizon."""
    result = {'greek_supported': False, 'greek_score': 0.0, 'greek_veto_reasons': []}
    try:
        greeks = node['greeks']
        delta = float(greeks['delta'])
        gamma = float(greeks['gamma'])
        theta = float(greeks['theta'])
        iv = float(node['implied_volatility'])
        premium = float(node['top_ask_price'])
        move = abs(float(expected_spot_move))
        values = (delta, gamma, theta, iv, premium, move)
        if not all(math.isfinite(v) for v in values) or option_type not in ('CE', 'PE'):
            raise ValueError('Invalid contract data')
        if not 0 < abs(delta) <= 1 or gamma <= 0 or iv <= 0 or premium <= 0:
            raise ValueError('Invalid Greeks or premium')
        if (delta > 0) != (option_type == 'CE'):
            raise ValueError('Delta sign conflicts with contract')
        if iv_change_5m is not None and not math.isfinite(float(iv_change_5m)):
            raise ValueError('Invalid matched IV change')
    except (KeyError, TypeError, ValueError, OverflowError):
        result['greek_veto_reasons'] = ['MISSING_OR_INVALID_SELECTED_CONTRACT_GREEKS']
        return result
    reasons = []
    if abs(delta) < .38:
        reasons.append('LOW_DELTA')
    # Compare premium points with premium points, rather than daily theta / delta.
    decay = max(0.0, -theta) * 20.0 / 375.0
    directional_gain = abs(delta) * move + .5 * gamma * move * move
    drag = decay / max(directional_gain, .01)
    if drag > (.45 if is_expiry_day else .65):
        reasons.append('HIGH_HOLDING_THETA_DRAG')
    if iv_change_5m is not None and float(iv_change_5m) < -.65:
        reasons.append('MATCHED_CONTRACT_IV_CRUSH')
    score = max(0.0, 100.0 - 25.0 * len(reasons))
    result.update(greek_supported=not reasons, greek_score=score,
                  delta=delta, gamma=gamma, theta=theta, iv=iv,
                  iv_change_5m=iv_change_5m, iv_trend_available=iv_change_5m is not None,
                  holding_minutes=20, modeled_decay_points=round(decay, 4),
                  theta_to_directional_gain=round(drag, 4), greek_veto_reasons=reasons)
    return result


class NiftyOITracker:
    def __init__(self, strike_step=50):
        if strike_step <= 0:raise ValueError("Strike step must be positive")
        self.strike_step=strike_step
        self.history=deque(maxlen=400)
        self.context=None

    def assess(self,chain,spot,now,expiry=None):
        if not isinstance(chain,dict):
            return None,{'ready':False,'reason':'Malformed option chain'}
        if not all(math.isfinite(float(v)) for v in (spot,now)) or spot<=0:
            return None,{'ready':False,'reason':'Invalid spot or snapshot time'}
        context=(datetime.fromtimestamp(now,IST).date().isoformat(),expiry)
        if context!=self.context:
            self.history.clear();self.morning_snapshot=None;self.context=context
        nodes={}
        for key,sides in chain.items():
            try:
                if not isinstance(sides,dict):continue
                strike=float(key)
                if not math.isfinite(strike) or strike<=0:continue
                for side in ('ce','pe'):
                    raw=sides.get(side,{})
                    if not isinstance(raw,dict) or 'oi' not in raw or 'last_price' not in raw:continue
                    oi=float(raw['oi']);price=float(raw['last_price'])
                    if not all(math.isfinite(v) and v>=0 for v in (oi,price)):continue
                    previous=raw.get('previous_oi')
                    previous=float(previous) if previous is not None else None
                    if previous is not None and (not math.isfinite(previous) or previous<0):previous=None
                    vol=float(raw.get('volume',0) or 0)
                    iv=float(raw.get('implied_volatility',0) or 0)
                    greeks=raw.get('greeks') if isinstance(raw.get('greeks'),dict) else {}
                    delta=float(greeks.get('delta',raw.get('delta',0)) or 0)
                    gamma=float(greeks.get('gamma',raw.get('gamma',0)) or 0)
                    theta=float(greeks.get('theta',raw.get('theta',0)) or 0)
                    nodes[(strike,side)]={'oi':oi,'price':price,'previous_oi':previous,
                                          'volume':vol if math.isfinite(vol) and vol>=0 else 0.0,
                                          'iv':iv if math.isfinite(iv) and iv>=0 else 0.0,
                                          'delta':delta if math.isfinite(delta) else 0.0,
                                          'gamma':gamma if math.isfinite(gamma) else 0.0,
                                          'theta':theta if math.isfinite(theta) else 0.0,
                                          'security_id':str(raw.get('security_id',raw.get('securityId','')))}
            except (KeyError,TypeError,ValueError,OverflowError):continue
        eligible=[s for s in self.history if 270<=now-s['time']<=450]
        prior=eligible[-1] if eligible else None
        put_anchor=math.floor(spot/self.strike_step)*self.strike_step
        calls=[put_anchor+self.strike_step+i*self.strike_step for i in range(3)]
        puts=[put_anchor-i*self.strike_step for i in range(3)]
        basket=[(k,'ce') for k in calls]+[(k,'pe') for k in puts]
        reason='Five-minute OI comparison ready'
        ready=True
        if any(k not in nodes or nodes[k]['price']<=0 for k in basket):
            ready=False;reason='Missing current contract OI or premium'
        elif prior is None:
            ready=False;reason='Need a same-expiry snapshot 270–450 seconds earlier'
        elif any(k not in prior['nodes'] or prior['nodes'][k]['price']<=0 for k in basket):
            ready=False;reason='Current contracts absent in five-minute reference'
        elif any(nodes[k]['security_id'] and prior['nodes'][k]['security_id'] and nodes[k]['security_id']!=prior['nodes'][k]['security_id'] for k in basket):
            ready=False;reason='Contract security ID changed in OI comparison'
        reference=None
        if ready:
            reference={'ts':prior['time'],'spot':prior['spot'],'atm_ce_strike':calls[0]}
            for side,prefix in (('ce','ce'),('pe','pe')):
                keys=[k for k in basket if k[1]==side]
                reference[prefix+'_oi']=sum(prior['nodes'][k]['oi'] for k in keys)
                reference[prefix+'_prem']=sum(prior['nodes'][k]['price'] for k in keys)
        rows=[]
        for (strike,side),node in nodes.items():
            if abs(strike-spot)>10*self.strike_step:continue
            old=prior['nodes'].get((strike,side)) if prior else None
            same=old and (not node['security_id'] or not old['security_id'] or node['security_id']==old['security_id'])
            morn_snap=getattr(self, "morning_snapshot", None) or (self.history[0] if self.history else None)
            morn_node=morn_snap['nodes'].get((strike,side)) if morn_snap else None
            morn_oi_diff=(node['oi']-morn_node['oi']) if morn_node else (node['oi']-node['previous_oi'] if node['previous_oi'] is not None else 0.0)
            rows.append({'strike':strike,'side':side,'oi':node['oi'],
                         'volume':node.get('volume',0.0),'iv':node.get('iv',0.0),
                         'iv_change_5m':round(node.get('iv',0.0)-old.get('iv',0.0),3) if same else 0.0,
                         'iv_comparison_available':bool(same and node['iv'] > 0 and old['iv'] > 0),
                         'delta':node.get('delta',0.0),'gamma':node.get('gamma',0.0),'theta':node.get('theta',0.0),
                         'change_since_morning':morn_oi_diff,
                         'change_from_previous_day':node['oi']-node['previous_oi'] if node['previous_oi'] is not None else None,
                         'change_over_5m':node['oi']-old['oi'] if same else None,
                         'premium_change_over_5m':node['price']-old['price'] if same else None})
        supports=sorted((r for r in rows if r['side']=='pe' and r['strike']<=spot),key=lambda r:r['oi'],reverse=True)[:5]
        resistances=sorted((r for r in rows if r['side']=='ce' and r['strike']>=spot),key=lambda r:r['oi'],reverse=True)[:5]
        totals={side:sum(n['oi'] for (k,s),n in nodes.items() if s==side) for side in ('ce','pe')}
        vol_totals={side:sum(n.get('volume',0.0) for (k,s),n in nodes.items() if s==side) for side in ('ce','pe')}
        morn_ce_write=sum(max(0.0, r.get('change_since_morning') or 0.0) for r in rows if r['side']=='ce')
        morn_pe_write=sum(max(0.0, r.get('change_since_morning') or 0.0) for r in rows if r['side']=='pe')
        avg_ce_iv=sum(r.get('iv',0.0) for r in rows if r['side']=='ce' and r.get('iv',0)>0)/max(1,sum(1 for r in rows if r['side']=='ce' and r.get('iv',0)>0))
        avg_pe_iv=sum(r.get('iv',0.0) for r in rows if r['side']=='pe' and r.get('iv',0)>0)/max(1,sum(1 for r in rows if r['side']=='pe' and r.get('iv',0)>0))
        avg_iv_change_5m=sum(r.get('iv_change_5m',0.0) for r in rows)/max(1,len(rows))
        def building(row):
            return (row['change_over_5m'] or 0)>0 or (row['change_from_previous_day'] or 0)>0
        building_supports=sorted((r for r in rows if r['side']=='pe' and r['strike']<=spot and building(r)),key=lambda r:-r['strike'])
        building_resistances=sorted((r for r in rows if r['side']=='ce' and r['strike']>=spot and building(r)),key=lambda r:r['strike'])
        nearest_support=building_supports[0] if building_supports else None
        nearest_resistance=building_resistances[0] if building_resistances else None
        info={'room_to_resistance':round(nearest_resistance['strike']-spot,1) if nearest_resistance else None,'room_to_support':round(spot-nearest_support['strike'],1) if nearest_support else None,'building_support':nearest_support,'building_resistance':nearest_resistance,'level_basis':'Nearest strike with rising five-minute OI or positive buildup since previous close','ready':ready,'reason':reason,'expiry':expiry,'snapshot_received_epoch':now,
              'reference_age_seconds':round(now-prior['time'],2) if prior else None,
              'comparison':'Same six current strike/side contracts; not a changing ATM basket',
              'oi_units':'Raw Dhan OI units, without lot conversion',
              'previous_oi_meaning':'Previous day OI; separate from five-minute change',
              'full_chain_pcr':round(totals['pe']/totals['ce'],4) if totals['ce'] else None,'call_volume_total':vol_totals['ce'],'put_volume_total':vol_totals['pe'],'put_call_volume_ratio':round(vol_totals['pe']/vol_totals['ce'],4) if vol_totals['ce']>0 else 1.0,'morning_ce_writing_total':morn_ce_write,'morning_pe_writing_total':morn_pe_write,'dominant_morning_writer':'PUT_WRITERS_BULLISH' if morn_pe_write>morn_ce_write*1.12 else ('CALL_WRITERS_BEARISH' if morn_ce_write>morn_pe_write*1.12 else 'BALANCED'),'avg_ce_iv':round(avg_ce_iv,2),'avg_pe_iv':round(avg_pe_iv,2),'avg_iv_change_5m':round(avg_iv_change_5m,3),
              'support_put_oi':supports,'resistance_call_oi':resistances,
              'nearby_strikes':sorted(rows,key=lambda r:(r['strike'],r['side'])),
              'freshness_limit':'Response receipt time does not establish exchange OI update time'}
        if reference is not None:
            for side in ('ce','pe'):
                current=[nodes[k] for k in basket if k[1]==side]
                info[side+'_oi_change_5m']=sum(n['oi'] for n in current)-reference[side+'_oi']
                info[side+'_premium_change_5m']=sum(n['price'] for n in current)-reference[side+'_prem']
        if self.history and now<=self.history[-1]['time']:
            self.history.clear();self.morning_snapshot=None;reference=None;info.update(ready=False,reason='Non-increasing snapshot time')
        snap_obj = {'time':now,'spot':spot,'nodes':nodes}
        if getattr(self, "morning_snapshot", None) is None and nodes:
            self.morning_snapshot = snap_obj
        info['multi_horizon_matrix'] = compute_multi_horizon_oi_iv_delta_matrix(self, now, spot, nodes, info)
        if self.history and int(now // 60) == int(self.history[-1]['time'] // 60):
            self.history[-1] = snap_obj
        else:
            self.history.append(snap_obj)
        return reference,info


def evaluate_index_scalp_opportunity(
    spot: float,
    option_type: str,
    oi_info: dict,
    mtf_sr: dict,
    book_pressure_ratio: float = 1.0,
) -> dict:
    """
    Determines if a NIFTY / SENSEX trade should be executed as a high-precision
    quick scalp (capturing 8-14 points / Rs.1,000 - Rs.1,800 quickly) vs a full trend runner.
    Never trades blindly: requires 5m/10m OI writer shift + Delta >= 0.42 + Order Book Bid dominance.
    """
    matrix = (oi_info or {}).get("multi_horizon_matrix") or {}
    horizons = matrix.get("horizons") or {}
    h5 = horizons.get("5m") or {}
    h10 = horizons.get("10m") or {}

    greeks = (oi_info or {}).get("atm_ce_greeks" if option_type == "CE" else "atm_pe_greeks") or {}
    delta = abs(float(greeks.get("delta") or 0.50))
    iv_chg_5m = float((oi_info or {}).get("atm_iv_change_5m") or 0.0)

    # Check if 5m & 10m writers are actively supporting the quick scalp direction
    st_writer_ok = (
        (option_type == "CE" and h5.get("net_put_minus_call_writing", 0) > 0)
        or (option_type == "PE" and h5.get("net_put_minus_call_writing", 0) < 0)
    )

    # Check distance to next 15m / 4H wall
    tf_15m = (mtf_sr or {}).get("15m") or {}
    wall_dist = 999.0
    if option_type == "CE" and float(tf_15m.get("resistance") or 0) > spot:
        wall_dist = float(tf_15m["resistance"]) - spot
    elif option_type == "PE" and 0 < float(tf_15m.get("support") or 0) < spot:
        wall_dist = spot - float(tf_15m["support"])

    # If wall is between 16 and 35 Nifty points away, a full runner will likely bounce back,
    # making it an ideal high-accuracy SCALP setup if Delta & Order Book are strong.
    is_scalp_zone = 16.0 <= wall_dist <= 38.0
    qualifies_for_scalp = delta >= 0.42 and iv_chg_5m >= -0.35 and book_pressure_ratio >= 1.05 and st_writer_ok

    if is_scalp_zone and qualifies_for_scalp:
        return {
            "trade_mode": "INDEX_PRECISION_SCALP",
            "scalp_qualified": True,
            "scalp_target_pts": 10.0,
            "scalp_target_rupees": 1500.0,
            "wall_dist_pts": round(wall_dist, 1),
            "reason": f"INDEX_SCALP_MODE(wall={wall_dist:.1f}pts,delta={delta:.2f},book={book_pressure_ratio:.2f})",
        }

    return {
        "trade_mode": "TREND_RUNNER",
        "scalp_qualified": qualifies_for_scalp,
        "scalp_target_pts": 12.0,
        "scalp_target_rupees": 1650.0,
        "wall_dist_pts": round(wall_dist, 1),
        "reason": "STANDARD_RUNNER_OR_SCALP_FALLBACK",
    }


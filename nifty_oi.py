"""Nifty OI comparisons using actual, identical contracts across snapshots."""
import math
from collections import deque
from datetime import datetime, timedelta, timezone

IST=timezone(timedelta(hours=5,minutes=30))


class NiftyOITracker:
    def __init__(self):
        self.history=deque(maxlen=200)
        self.context=None

    def assess(self,chain,spot,now,expiry=None):
        if not isinstance(chain,dict):
            return None,{'ready':False,'reason':'Malformed option chain'}
        if not all(math.isfinite(float(v)) for v in (spot,now)) or spot<=0:
            return None,{'ready':False,'reason':'Invalid spot or snapshot time'}
        context=(datetime.fromtimestamp(now,IST).date().isoformat(),expiry)
        if context!=self.context:
            self.history.clear();self.context=context
        nodes={}
        for key,sides in chain.items():
            try:
                if not isinstance(sides,dict):continue
                strike=float(key)
                if not math.isfinite(strike) or strike<=0:continue
                for side in ('ce','pe'):
                    raw=sides.get(side,{})
                    if not isinstance(raw,dict):continue
                    oi=float(raw['oi']);price=float(raw['last_price'])
                    if not all(math.isfinite(v) and v>=0 for v in (oi,price)):continue
                    previous=raw.get('previous_oi')
                    previous=float(previous) if previous is not None else None
                    if previous is not None and (not math.isfinite(previous) or previous<0):previous=None
                    nodes[(strike,side)]={'oi':oi,'price':price,'previous_oi':previous,
                                          'security_id':str(raw.get('security_id',raw.get('securityId','')))}
            except (KeyError,TypeError,ValueError,OverflowError):continue
        eligible=[s for s in self.history if 300<=now-s['time']<=390]
        prior=eligible[-1] if eligible else None
        put_anchor=math.floor(spot/50)*50
        calls=[put_anchor+50+i*50 for i in range(3)]
        puts=[put_anchor-i*50 for i in range(3)]
        basket=[(k,'ce') for k in calls]+[(k,'pe') for k in puts]
        reason='Five-minute OI comparison ready'
        ready=True
        if any(k not in nodes or nodes[k]['price']<=0 for k in basket):
            ready=False;reason='Missing current contract OI or premium'
        elif prior is None:
            ready=False;reason='Need a same-expiry snapshot 300–390 seconds earlier'
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
            if abs(strike-spot)>500:continue
            old=prior['nodes'].get((strike,side)) if prior else None
            same=old and (not node['security_id'] or not old['security_id'] or node['security_id']==old['security_id'])
            rows.append({'strike':strike,'side':side,'oi':node['oi'],
                         'change_from_previous_day':node['oi']-node['previous_oi'] if node['previous_oi'] is not None else None,
                         'change_over_5m':node['oi']-old['oi'] if same else None,
                         'premium_change_over_5m':node['price']-old['price'] if same else None})
        supports=sorted((r for r in rows if r['side']=='pe' and r['strike']<=spot),key=lambda r:r['oi'],reverse=True)[:5]
        resistances=sorted((r for r in rows if r['side']=='ce' and r['strike']>=spot),key=lambda r:r['oi'],reverse=True)[:5]
        totals={side:sum(n['oi'] for (k,s),n in nodes.items() if s==side) for side in ('ce','pe')}
        info={'ready':ready,'reason':reason,'expiry':expiry,'snapshot_received_epoch':now,
              'reference_age_seconds':round(now-prior['time'],2) if prior else None,
              'comparison':'Same six current strike/side contracts; not a changing ATM basket',
              'oi_units':'Raw Dhan OI units, without lot conversion',
              'previous_oi_meaning':'Previous day OI; separate from five-minute change',
              'full_chain_pcr':round(totals['pe']/totals['ce'],4) if totals['ce'] else None,
              'support_put_oi':supports,'resistance_call_oi':resistances,
              'nearby_strikes':sorted(rows,key=lambda r:(r['strike'],r['side'])),
              'freshness_limit':'Response receipt time does not establish exchange OI update time'}
        if self.history and now<=self.history[-1]['time']:
            self.history.clear();reference=None;info.update(ready=False,reason='Non-increasing snapshot time')
        self.history.append({'time':now,'spot':spot,'nodes':nodes})
        return reference,info

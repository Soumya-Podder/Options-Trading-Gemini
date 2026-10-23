"""Isolated Nifty research; never places orders or alters production learning state.

All downloads and results live under the project cache. Candidate settings are
declared before evaluation; holdout periods are not used to select a candidate.
"""
import argparse
import hashlib
import json
import logging
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import importlib.util
import subprocess
import os

import dhan_bot_engine as bot
from nifty_market_structure import assess_nifty_structure

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'dhan_hist_cache' / 'nifty_research_20261023'
IST = timezone(timedelta(hours=5, minutes=30))
FIELDS = ('open', 'high', 'low', 'close', 'oi', 'iv', 'strike', 'spot')
LABELS = ['ATM' if n == 0 else f'ATM{n:+d}' for n in range(-3, 4)]
PERIODS = [('2026-07-17','2026-08-16'), ('2026-08-16','2026-09-15'), ('2026-09-15','2026-10-15')]
# Fix this list before opening validation/holdout results. No lot multiplication.
CANDIDATES = [
    {'name':'previous_entries_6.15', 'stop':6.15, 'entry':'previous'},
    {'name':'rollover_guard_6.15', 'stop':6.15, 'entry':'guard'},
    {'name':'rollover_guard_10', 'stop':10.0, 'entry':'guard'},
    {'name':'rollover_guard_15', 'stop':15.0, 'entry':'guard'},
    {'name':'trend_confirmation_10', 'stop':10.0, 'entry':'trend'},
    {'name':'trend_confirmation_15', 'stop':15.0, 'entry':'trend'},
]
ENTRY_CANDIDATES = [
    {'name':'previous_static_6.15','stop':6.15,'entry':'previous','static':True},
    {'name':'guard_static_6.15','stop':6.15,'entry':'guard','static':True},
    {'name':'price_trend_6.15','stop':6.15,'entry':'price_trend','static':True},
    {'name':'price_trend_10','stop':10.0,'entry':'price_trend','static':True},
    {'name':'orb_retest_6.15','stop':6.15,'entry':'orb_retest','static':True},
    {'name':'orb_retest_10','stop':10.0,'entry':'orb_retest','static':True},
    {'name':'orb_breakout_target20','stop':10.0,'entry':'orb_breakout','static':True,'target':20},
    {'name':'price_trend_target20','stop':10.0,'entry':'price_trend','static':True,'target':20},
]
REVERSAL_CANDIDATES = [
    {'name':'orb_rejection_trail6.15','stop':6.15,'entry':'orb_rejection','static':True},
    {'name':'range_rejection_trail6.15','stop':6.15,'entry':'range_rejection','static':True},
    {'name':'orb_rejection_target20','stop':6.15,'entry':'orb_rejection','static':True,'target':20},
    {'name':'range_rejection_target20','stop':6.15,'entry':'range_rejection','static':True,'target':20},
    {'name':'range_rejection_target10','stop':6.15,'entry':'range_rejection','static':True,'target':10},
    {'name':'range_rejection_oi_target20','stop':6.15,'entry':'range_rejection','static':True,'target':20,'oi_alignment':True},
]
EARLY_CANDIDATES = [
    {'name':'minute_guard_6.15','stop':6.15,'entry':'guard','signal_minutes':1},
    {'name':'minute_guard_static_6.15','stop':6.15,'entry':'guard','static':True,'signal_minutes':1},
    {'name':'minute_trend_target20','stop':6.15,'entry':'price_trend','static':True,'target':20,'signal_minutes':1},
    {'name':'minute_orb_target20','stop':6.15,'entry':'orb_breakout','static':True,'target':20,'signal_minutes':1},
]
OI_CANDIDATES=[{'name':'same_contract_oi_6.15','stop':6.15,'entry':'oi_same_contract','signal_minutes':1}]
LEARNING_CANDIDATES=[
    {'name':'oi_permanent_pruning','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4},
    {'name':'oi_next_day_retry','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'next_day_retry':True},
    {'name':'oi_no_permanent_pruning','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'static':True},
    {'name':'oi_structure_next_day_retry','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'next_day_retry':True,'structure':True},
]
EXIT_CANDIDATES=[
    {'name':'oi_trailing_exit','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'static':True},
    {'name':'oi_fixed_10_exit','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'static':True,'target':10},
    {'name':'oi_fixed_20_exit','stop':6.15,'entry':'oi_same_contract','signal_minutes':1,'fixed_lots':1,'max_trades':4,'static':True,'target':20},
]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    tmp.replace(path)


def checked_rows(data):
    if not isinstance(data,dict) or not data.get('timestamp'):
        raise ValueError('Empty or malformed market history')
    size=len(data['timestamp'])
    if any(not isinstance(data.get(k),list) or len(data[k])!=size for k in FIELDS):
        raise ValueError('Missing or unaligned history fields')
    result={}
    for i,t in enumerate(data['timestamp']):
        if not math.isfinite(float(t)) or int(t)!=t or int(t) in result:
            raise ValueError('Invalid or duplicate timestamp')
        row={k:float(data[k][i]) for k in FIELDS}
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError('Non-finite market value')
        if min(row[k] for k in ('open','high','low','close','strike','spot'))<=0:
            raise ValueError('Nonpositive price or strike')
        if not row['low']<=min(row['open'],row['close'])<=max(row['open'],row['close'])<=row['high']:
            raise ValueError('Invalid OHLC ordering')
        result[int(t)]=row
    return result


def download(interval='5'):
    WORK.mkdir(parents=True,exist_ok=True)
    env=bot.load_dotenv_file(str(ROOT/'.env'))
    client=bot.DhanV2RestClient(env['DHAN_CLIENT_ID'],env['DHAN_ACCESS_TOKEN'],logging.getLogger('research'))
    for start,end in PERIODS:
        for side in ('CALL','PUT'):
            for label in LABELS:
                directory=WORK/('data' if interval=='5' else 'data_1m')
                path=directory/f'{start}_{end}_{side}_{label}.json'
                if path.exists():
                    checked_rows(json.loads(path.read_text()))
                    continue
                payload={'exchangeSegment':'NSE_FNO','interval':interval,'securityId':13,'instrument':'OPTIDX',
                         'expiryFlag':'WEEK','expiryCode':1,'strike':label,'drvOptionType':side,
                         'requiredData':list(FIELDS),'fromDate':start,'toDate':end}
                for attempt in range(3):
                    client._wait_hist_rate_limit()
                    response=client.session.post(client.BASE_URL+'/charts/rollingoption',json=payload,timeout=(10,40))
                    if response.status_code in (429,500,502,503,504):
                        time.sleep(2*(attempt+1))
                        continue
                    if response.status_code!=200:
                        raise RuntimeError(f'History HTTP {response.status_code}; no credentials or response body logged')
                    data=response.json().get('data',{}).get('ce' if side=='CALL' else 'pe')
                    rows=checked_rows(data)
                    lo=datetime.fromisoformat(start).replace(tzinfo=IST).timestamp()
                    hi=datetime.fromisoformat(end).replace(tzinfo=IST).timestamp()
                    keep=[i for i,t in enumerate(data['timestamp']) if lo<=t<hi]
                    excluded=len(rows)-len(keep)
                    data={k:[v[i] for i in keep] if isinstance(v,list) and len(v)==len(rows) else v for k,v in data.items()}
                    data['_request']={'payload':payload,'excluded_outside_requested_range':excluded}
                    checked_rows(data)
                    write(path,data)
                    print(f'Downloaded {start} {side} {label}: {len(rows)} bars',flush=True)
                    break
                else:
                    raise RuntimeError('History retries exhausted')
    if interval=='1':
        index_path=directory/'index.json'
        if not index_path.exists():
            combined=defaultdict(list)
            for start,end in [('2026-07-17 00:00:00','2026-09-01 00:00:00'),('2026-09-01 00:00:00','2026-10-15 00:00:00')]:
                payload={'securityId':'13','exchangeSegment':'IDX_I','instrument':'INDEX','interval':'1','fromDate':start,'toDate':end}
                client._wait_hist_rate_limit()
                response=client.session.post(client.BASE_URL+'/charts/intraday',json=payload,timeout=(10,40))
                if response.status_code!=200:raise RuntimeError(f'Minute index HTTP {response.status_code}')
                data=response.json()
                if not data.get('timestamp'):raise ValueError('Empty minute index data')
                for k,v in data.items():
                    if isinstance(v,list):combined[k].extend(v)
            write(index_path,dict(combined))
        write(WORK/'manifest_1m.json',{'interval_minutes':1,'periods':PERIODS,
              'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob('*.json'))},
              'purpose':'Same declared strategy settings; finer execution, not additional parameter selection'})
        print('One-minute download manifest saved',flush=True)
        return
    path=WORK/'data'/'index.json'
    if not path.exists():
        # 100 days needs two requests to respect Dhan's 90-day intraday limit.
        combined=defaultdict(list)
        for start,end in [('2026-07-07 00:00:00','2026-09-01 00:00:00'),('2026-09-01 00:00:00','2026-10-15 00:00:00')]:
            payload={'securityId':'13','exchangeSegment':'IDX_I','instrument':'INDEX','interval':'5',
                     'fromDate':start,'toDate':end}
            client._wait_hist_rate_limit()
            r=client.session.post(client.BASE_URL+'/charts/intraday',json=payload,timeout=(10,40))
            if r.status_code!=200:raise RuntimeError(f'Index history HTTP {r.status_code}')
            d=r.json()
            if not d.get('timestamp'):raise ValueError('Empty index history')
            for k,v in d.items():
                if isinstance(v,list):combined[k].extend(v)
        write(path,dict(combined))
    manifest={'created_at':datetime.now(IST).isoformat(),'expiry':'WEEK / expiryCode 1',
              'interval_minutes':5,'periods':PERIODS,'relative_strikes':LABELS,
              'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((WORK/'data').glob('*.json'))},
              'candidates_declared_before_evaluation':CANDIDATES,
              'selection':'First 20 verified trading days only; later 10-day blocks are held out',
              'target':'Gross profit >= Rs.10,000 per 10 verified trading days; no lot scaling to meet target'}
    write(WORK/'manifest.json',manifest)
    print('Download manifest saved',flush=True)


def fixed_contract_bars(legs, timestamps, strike):
    """Follow an exact strike across relative series within one session/expiry.

    Every value is a returned Dhan bar. Missing bars remain NaN and stop replay
    if reached. Conflicting duplicate observations are never silently preferred.
    """
    out={'timestamp':list(timestamps),**{k:[] for k in ('open','high','low','close','strike')}}
    for t in timestamps:
        found=[rows[t] for rows in legs.values() if t in rows and rows[t]['strike']==strike]
        if found and any(any(abs(r[k]-found[0][k])>1e-7 for k in ('open','high','low','close')) for r in found[1:]):
            raise ValueError(f'Conflicting fixed-strike observations at {t}')
        row=found[0] if found else {}
        for k in ('open','high','low','close','strike'):
            out[k].append(row.get(k,float('nan')))
    return out


def load_market():
    legs={side:{label:{} for label in LABELS} for side in ('CALL','PUT')}
    for start,end in PERIODS:
        for side in legs:
            for label in LABELS:
                rows=checked_rows(json.loads((WORK/'data'/f'{start}_{end}_{side}_{label}.json').read_text()))
                if set(rows)&set(legs[side][label]):raise ValueError('Overlapping downloaded windows')
                legs[side][label].update(rows)
    index=json.loads((WORK/'data'/'index.json').read_text())
    n=len(index['timestamp'])
    if any(len(index[k])!=n for k in ('open','high','low','close')):raise ValueError('Unaligned index data')
    underlying={int(t):{k:float(index[k][i]) for k in ('open','high','low','close')} for i,t in enumerate(index['timestamp'])}
    sessions=defaultdict(list)
    start_ts=datetime(2026,7,17,tzinfo=IST).timestamp()
    end_ts=datetime(2026,10,15,tzinfo=IST).timestamp()
    for t in sorted(underlying):
        dt=datetime.fromtimestamp(t,IST)
        if start_ts<=t<end_ts and bot.dtime(9,15)<=dt.time()<=bot.dtime(15,25):
            sessions[dt.date().isoformat()].append(t)
    valid={};excluded=[]
    for day,stamps in sorted(sessions.items()):
        expected=[int(datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=IST).timestamp())+300*i for i in range(75)]
        signal_legs=[legs['CALL'][k] for k in ('ATM','ATM+1','ATM+2')]+[legs['PUT'][k] for k in ('ATM','ATM-1','ATM-2')]
        if stamps!=expected or any(any(t not in rows for t in stamps) for rows in signal_legs):
            excluded.append({'day':day,'reason':'Incomplete regular-session index or signal-option history'})
        else:valid[day]=stamps
    coverage={'verified_session_count':len(valid),'excluded_sessions':excluded,'verified_days':list(valid),
              'limitation':'Index-observed sessions checked; absent dates are not automatically classified as holidays'}
    write(WORK/'coverage.json',coverage)
    if len(valid)<50:raise ValueError(f'Need >=50 complete sessions; found {len(valid)}')
    return legs,underlying,valid


def trend_allowed(stamps, i, underlying, direction):
    if i<3:return False
    bars=[underlying[t] for t in stamps[:i+1]]
    ema=bars[0]['close']
    for b in bars[1:-1]:ema+=2/11*(b['close']-ema)
    last,prev=bars[-1],bars[-2]
    span=last['high']-last['low']
    if span<=0 or abs(last['close']-last['open'])/span<0.45:return False
    if direction=='BUY_CE':
        return last['close']>max(ema,last['open'],prev['close']) and (last['close']-last['low'])/span>=0.7
    if direction=='BUY_PE':
        return last['close']<min(ema,last['open'],prev['close']) and (last['high']-last['close'])/span>=0.7
    return False


def price_action_direction(stamps,i,index,mode,signal_minutes=5):
    """Closed-bar research signals; no future pivots or retrospectively picked direction."""
    opening=15//signal_minutes
    if i<max(4,opening+1) or datetime.fromtimestamp(stamps[i]+signal_minutes*60,IST).time()>bot.dtime(14,30):return 'HOLD'
    bars=[index[t] for t in stamps[:i+1]]
    last,prev=bars[-1],bars[-2]
    orb_high=max(b['high'] for b in bars[:opening]);orb_low=min(b['low'] for b in bars[:opening])
    if mode in ('orb_rejection','range_rejection'):
        upper=orb_high if mode=='orb_rejection' else max(b['high'] for b in bars[-7:-1])
        lower=orb_low if mode=='orb_rejection' else min(b['low'] for b in bars[-7:-1])
        span=last['high']-last['low']
        if span<=0:return 'HOLD'
        bullish=last['low']<lower-3 and last['close']>max(lower,last['open'],prev['close']) and (last['close']-last['low'])/span>=0.7
        bearish=last['high']>upper+3 and last['close']<min(upper,last['open'],prev['close']) and (last['high']-last['close'])/span>=0.7
        return 'BUY_CE' if bullish else 'BUY_PE' if bearish else 'HOLD'
    if mode=='orb_breakout':
        bullish=last['close']>orb_high+3 and prev['close']<=orb_high+3
        bearish=last['close']<orb_low-3 and prev['close']>=orb_low-3
    elif mode=='orb_retest':
        bullish=prev['close']>orb_high+3 and last['low']<=orb_high+3 and last['close']>max(orb_high+3,prev['close'])
        bearish=prev['close']<orb_low-3 and last['high']>=orb_low-3 and last['close']<min(orb_low-3,prev['close'])
    else:
        lookback=15//signal_minutes
        bullish=last['close']>max(b['high'] for b in bars[-lookback-1:-1])+3
        bearish=last['close']<min(b['low'] for b in bars[-lookback-1:-1])-3
    direction='BUY_CE' if bullish else 'BUY_PE' if bearish else 'HOLD'
    return direction if trend_allowed(stamps,i,index,direction) else 'HOLD'


def fixed_target_replay(entry_i, bars, stop_points, target_points):
    """Research bracket for one lot, with one-minute stop-first execution."""
    stamps=bars['timestamp']
    raw=bars['open'][entry_i]
    entry=math.ceil(raw*1.001*20-1e-9)/20
    stop=entry-stop_points;target=entry+target_points
    def finish(j,price,reason):
        fill=math.floor(price*.999*20+1e-9)/20
        return j,fill,round((fill-entry)/stop_points,2),reason
    for j in range(entry_i,len(stamps)):
        o,h,l,c=(bars[k][j] for k in ('open','high','low','close'))
        if not all(math.isfinite(x) and x>0 for x in (o,h,l,c)):
            raise bot.ReplayDataError('Missing fixed-contract bracket bar',j)
        if not l<=min(o,c)<=max(o,c)<=h:raise bot.ReplayDataError('Invalid bracket OHLC',j)
        if j>entry_i and stamps[j]-stamps[j-1]!=60:raise bot.ReplayDataError('Missing one-minute bracket bar',j)
        if o<=stop:return finish(j,o,'INITIAL_SL_HIT')
        if o>=target:return finish(j,target,f'SINGLE_LOT_TARGET_{target_points}')
        if l<=stop:return finish(j,stop,'INITIAL_SL_HIT')
        if h>=target:return finish(j,target,f'SINGLE_LOT_TARGET_{target_points}')
    if datetime.fromtimestamp(stamps[-1]+60,IST).time()!=bot.EOD_SQUAREOFF_TIME:
        raise bot.ReplayDataError('Bracket history ends before close',len(stamps)-1)
    return finish(len(stamps)-1,bars['close'][-1],'EOD_SQUAREOFF')


def retry_pruned_next_day(brain):
    """Research-only policy: day-end recovery; intraday protections still apply."""
    for rule in brain.rules.values():
        if rule.get('status') in ('PRUNED','DAY_MUTED'):
            rule['status']='CANDIDATE'


def selected_entry_strike(analysis,side):
    key='atm_ce_strike' if side=='CALL' else 'atm_pe_strike'
    strike=float(analysis[key])
    if not math.isfinite(strike) or strike<=0:raise ValueError('Invalid selected entry strike')
    return strike


def run_candidate(config, sessions, legs, underlying, out, previous_analyzer, execution_legs=None,structure_history=None):
    out.mkdir(parents=True,exist_ok=False)
    logger=logging.getLogger('nifty_research.'+config['name'])
    logger.setLevel(logging.ERROR)
    brain=bot.SelfLearningBrain(logger,str(out/'brain.json'),str(out/'rules.json'))
    ledger={'initial_capital':30000.0,'virtual_balance_gross':30000.0,'virtual_balance_net':30000.0,
            'profit_without_charges':0.0,'net_profit_after_charges':0.0,'trades':[]}
    daily=[];unvalidated=[];entry_rejections=Counter()
    current=config.get('current_implementation',False)
    slippage=config.get('slippage_pct',0.1)
    original_stop=bot.INDICES_CONFIG['NIFTY']['sl_points']
    bot.INDICES_CONFIG['NIFTY']['sl_points']=config['stop']
    signal_minutes=config.get('signal_minutes',5)
    try:
        for day,stamps in sessions.items():
            analyzer=previous_analyzer() if config['entry']=='previous' else bot.MarketMicrostructureAnalyzer()
            brain.unmute_new_day(day)
            if config.get('next_day_retry'):retry_pruned_next_day(brain)
            count=losses=0;blocked=False;eligible=0
            skip_until=-1;day_gross=day_net=0.0
            for i,t in enumerate(stamps):
                signal_sources=[legs['CALL'][k] for k in ('ATM','ATM+1','ATM+2')]+[legs['PUT'][k] for k in ('ATM','ATM-1','ATM-2')]
                if any(t not in rows for rows in signal_sources):
                    # Some histories begin a few minutes after open. No entries
                    # are allowed before 09:25; do not fabricate those snapshots.
                    if datetime.fromtimestamp(t,IST).time()>=bot.dtime(9,25) and not blocked:
                        blocked=True
                        unvalidated.append({'day':day,'time':datetime.fromtimestamp(t,IST).isoformat(),'reason':'Missing eligible-minute signal history'})
                    continue
                c=[legs['CALL'][k][t] for k in ('ATM','ATM+1','ATM+2')]
                p=[legs['PUT'][k][t] for k in ('ATM','ATM-1','ATM-2')]
                analysis={}
                if not current or config['entry'] not in ('oi_same_contract','level_oi'):
                    analysis=analyzer.evaluate_snapshot('NIFTY',c[0]['spot'],[int(r['strike']) for r in c],
                        [int(r['strike']) for r in p],int(sum(r['oi'] for r in c)),int(sum(r['oi'] for r in p)),
                        sum(r['close'] for r in c),sum(r['close'] for r in p),sum(r['iv'] for r in c),
                        sum(r['iv'] for r in p),c[0]['close'],p[0]['close'],
                        **({'live_ts':t+60} if signal_minutes==1 else {}))
                if config['entry'] in ('oi_same_contract','level_oi'):
                    oc={}
                    for side,side_key in (('CALL','ce'),('PUT','pe')):
                        for rows in legs[side].values():
                            if t not in rows:continue
                            data=rows[t]
                            oc.setdefault(f"{data['strike']:.6f}",{})[side_key]={
                                'oi':data['oi'],'last_price':data['close'],'implied_volatility':data['iv']}
                    analysis=analyzer.evaluate_chain('NIFTY',{'last_price':c[0]['spot'],'oc':oc},
                        expiry='WEEK1_SAME_SESSION_ASSUMPTION',snapshot_time=t+60)
                    if min(analysis['atm_ce_ltp'],analysis['atm_pe_ltp'])<=0:
                        raise ValueError('OI research chain missing selected premium; do not score as a zero-trade strategy')
                if config['entry'] in ('price_trend','orb_retest','orb_breakout','orb_rejection','range_rejection'):
                    # This experiment replaces the OI-direction hard gate with
                    # explicit price confirmation. OI tags remain in the audit.
                    analysis['recommended_dir']=price_action_direction(stamps,i,underlying,config['entry'],signal_minutes)
                    analysis['exceeds_50_lakhs']=analysis['recommended_dir']!='HOLD'
                    if config.get('oi_alignment'):
                        ce_oi=sum(r['oi'] for r in c);pe_oi=sum(r['oi'] for r in p)
                        if (analysis['recommended_dir']=='BUY_CE' and pe_oi<ce_oi) or (analysis['recommended_dir']=='BUY_PE' and ce_oi<pe_oi):
                            analysis['recommended_dir']='HOLD'
                analysis['market_structure']={'allowed':config['entry']!='trend' or trend_allowed(stamps,i,underlying,analysis['recommended_dir'])}
                if config.get('structure'):
                    if structure_history is None:raise ValueError('Structure candles required for full-gate study')
                    analysis['market_structure']=assess_nifty_structure(structure_history[day],oc,t+signal_minutes*60,c[0]['spot'],analysis['recommended_dir'])
                if config['entry']=='level_oi':
                    if structure_history is None:raise ValueError('Structure candles required for level-first entry')
                    if config.get('opening_trend'):
                        bot.apply_nifty_level_entry(analysis,structure_history[day],oc,t+signal_minutes*60,
                            enable_opening_trend=True,enable_price_levels=bool(config.get('price_levels')))
                    else:
                        bot.apply_nifty_level_entry(analysis,structure_history[day],oc,t+signal_minutes*60)
                    if not analysis['market_structure'].get('allowed'):
                        checks=analysis['market_structure'].get('direction_checks',{})
                        if checks:
                            for direction,check in checks.items():
                                entry_rejections[direction+': '+str(check.get('reason'))]+=1
                        else:
                            entry_rejections[analysis['market_structure'].get('reason','Unavailable')]+=1
                if config.get("daily_profit_target") is not None and day_net>=config["daily_profit_target"]:continue
                if blocked or i<=skip_until or count>=config.get('max_trades',bot.MAX_TRADES_PER_DAY) or losses>=bot.MAX_CONSECUTIVE_LOSSES_PER_DAY:continue
                signal_time=datetime.fromtimestamp(t+signal_minutes*60,IST)
                if signal_time.time()<(bot.SESSION_START_TIME if current else bot.dtime(9,25)) or signal_time.time()>bot.LAST_ENTRY_TIME:continue
                if config.get('static'):
                    brain.recent_losing_states.clear()
                    for rule in brain.rules.values():rule['status']='CANDIDATE'
                action,sig=brain.select_action(analysis,losses,bar_time=signal_time.time())
                if action=='HOLD' and analysis.get('market_structure',{}).get('allowed'):
                    entry_rejections['Learned-rule/premium/wall-room or other execution selection veto']+=1
                if action=='HOLD' or not sig or i+1>=len(stamps):continue
                side='CALL' if action=='BUY_CE' else 'PUT'
                strike=selected_entry_strike(analysis,side)
                eligible+=1
                execution_stamps=stamps if execution_legs is None else [stamps[0]+60*k for k in range(375)]
                fixed=fixed_contract_bars(legs[side] if execution_legs is None else execution_legs[side],execution_stamps,strike)
                j=i+1 if execution_legs is None else (i+1)*signal_minutes
                if not math.isfinite(fixed['open'][j]):
                    unvalidated.append({'day':day,'time':signal_time.isoformat(),'reason':'Selected contract absent at executable entry','strike':strike})
                    blocked=True;continue
                entry=math.ceil(fixed['open'][j]*(1+slippage/100)*20-1e-9)/20
                lots=config.get('fixed_lots') or (1 if config.get('static') else brain.compute_lots('NIFTY',sig,entry,ledger['virtual_balance_net'],losses))
                # Explicit research risk ceiling, never increase lots to hit P&L.
                if not current:lots=min(lots,max(1,int(1000//(config['stop']*65))))
                if entry*lots*65>ledger['virtual_balance_net'] or ledger['virtual_balance_net']<=0:continue
                fills=[]
                try:
                    if config.get('target'):
                        if execution_legs is None or lots!=1:raise ValueError('Bracket requires one-minute execution and one lot')
                        exit_i,exit_price,r,reason=fixed_target_replay(j,fixed,config['stop'],config['target'])
                    else:
                        exit_i,exit_price,r,reason=bot.evaluate_real_option_trade_bars(j,side=='CALL',execution_stamps,
                            fixed['open'],fixed['high'],fixed['low'],fixed['close'],fixed['strike'],config['stop'],
                            interval_min=5 if execution_legs is None else 1,index='NIFTY',lots=lots,lot_size=65,exit_fills=fills,
                            entry_at_open=True,expected_strike=strike,slippage_pct=slippage,require_session_end=True,
                            session_profit_check=(lambda entry,remaining,done,bid:bot.daily_target_reached(day_net,
                                {'entry_price':entry,'qty':remaining,'lots':lots,'exit_fills':done},bid)) if current else None,
                            current_session_policy=current)
                except bot.ReplayDataError as error:
                    unvalidated.append({'day':day,'time':signal_time.isoformat(),'reason':error.reason,'strike':strike})
                    blocked=True;count+=1;continue
                gross=bot.record_closed_trade(ledger,brain,day,signal_time.strftime('%Y-%m-%d %H:%M:%S'),
                    datetime.fromtimestamp(execution_stamps[exit_i],IST).strftime('%Y-%m-%d %H:%M:%S'),'NIFTY',int(strike),
                    'CE' if side=='CALL' else 'PE',lots,lots*65,c[0]['spot'],analysis['diff_ce_minus_pe_lakhs'],
                    entry,exit_price,r,reason,sig,analysis['state_vec'],'ACTUAL_STRIKE_STITCHED_RESEARCH',fills,
                    ledger_path=str(out/'paper_trade_ledger.json'),market_structure=analysis.get('market_structure'))
                day_gross+=gross;day_net+=ledger['trades'][-1]['net_pnl']
                net_loss=ledger['trades'][-1]['net_pnl']<=0
                count+=1;losses=losses+1 if net_loss else 0
                # Next signal closes at bar i+1, so avoid signals formed before exit.
                if current:
                    # Exit-minute close is the earliest known completion time.
                    ready=execution_stamps[exit_i]+60+(bot.POST_SL_COOLDOWN_MINUTES if net_loss else bot.POST_WIN_COOLDOWN_MINUTES)*60
                    skip_until=max(-1,math.ceil((ready-stamps[0])/(signal_minutes*60))-2)
                else:
                    exit_signal_i=exit_i if execution_legs is None else exit_i//signal_minutes
                    skip_until=exit_signal_i+max(1,(bot.POST_SL_COOLDOWN_MINUTES if net_loss else bot.POST_WIN_COOLDOWN_MINUTES)//signal_minutes)
            daily.append({'day':day,'gross':round(day_gross,2),'net':round(day_net,2),'trades':count,
                          'unvalidated':blocked,'eligible_entries':eligible,
                          'rule_status_counts':dict(Counter(r.get('status') for r in brain.rules.values()))})
    finally:bot.INDICES_CONFIG['NIFTY']['sl_points']=original_stop
    result={'config':config,'daily':daily,'unvalidated':unvalidated,'ledger':ledger,
            'execution_interval_minutes':5 if execution_legs is None else 1,
            'full_strategy_validated':False,
            'session_rules_match_current':current,
            'current_policy_notes':(['Dynamic learning-based lots; no additional research risk cap',
                'Shared daily net liquidation helper; conservative stop-first OHLC ambiguity',
                '25-minute time exit at first eligible bar open using prior peak',
                'Close-time cooldown; actual intrabar exit time is unknown'] if current else []),
            'entry_rejection_observations':dict(entry_rejections),
            'limitations':[f'{5 if execution_legs is None else 1}-minute execution OHLC; adverse {slippage}% slippage and stop-first ambiguity handling',
                           'Same day, exact strike, WEEK expiryCode 1; prices never synthesized',
                           'Only ATM +/-3 coverage; missing held-strike bars invalidate outcomes',
                           'Partial ATM +/-3 chain and missing previous-day OI cannot reproduce the full historical chain',
                           ('Current dynamic sizing, no additional research risk ceiling' if current else 'Research maximum estimated initial gross stop risk Rs.1,000; gaps can exceed this'),
                           'Independent fresh learning per candidate; online learning continues chronologically']}
    write(out/'result.json',result)
    return result


def blocks(daily):
    return [{'start':part[0]['day'],'end':part[-1]['day'],'sessions':len(part),
             'gross':round(sum(x['gross'] for x in part),2),'net':round(sum(x['net'] for x in part),2),
             'trades':sum(x['trades'] for x in part),'unvalidated_days':sum(x['unvalidated'] for x in part),
             'target_met':len(part)==10 and not any(x['unvalidated'] for x in part) and sum(x['gross'] for x in part)>=10000}
            for i in range(0,len(daily),10) if (part:=daily[i:i+10])]


def evaluate(finer_execution=False,entry_research=False,reversal_research=False,early_research=False,oi_research=False,learning_research=False,exit_research=False):
    legs,index,sessions=load_market()
    execution=None
    if finer_execution:
        execution={side:{label:{} for label in LABELS} for side in ('CALL','PUT')}
        for start,end in PERIODS:
            for side in execution:
                for label in LABELS:
                    data=json.loads((WORK/'data_1m'/f'{start}_{end}_{side}_{label}.json').read_text())
                    execution[side][label].update(checked_rows(data))
    if early_research or oi_research or learning_research or exit_research:
        data=json.loads((WORK/'data_1m'/'index.json').read_text())
        index={int(t):{k:float(data[k][i]) for k in ('open','high','low','close')} for i,t in enumerate(data['timestamp'])}
        minute_sessions={day:[stamps[0]+60*i for i in range(375)] for day,stamps in sessions.items()}
        for day,stamps in minute_sessions.items():
            if any(t not in index for t in stamps):
                raise ValueError(f'Incomplete minute underlying session {day}')
        sessions=minute_sessions
        legs=execution
    training=dict(list(sessions.items())[:20])
    holdout=dict(list(sessions.items())[20:])
    run=WORK/('run_'+datetime.now(IST).strftime('%Y%m%d_%H%M%S'))
    run.mkdir(parents=True)
    source=WORK/'previous_engine_2ba4ea8.py'
    if not source.exists():
        recovered=subprocess.run(['git','-c','core.fsmonitor=false','show','2ba4ea8:dhan_bot_engine.py'],cwd=ROOT,capture_output=True,check=True)
        source.write_bytes(recovered.stdout)
    candidates=EXIT_CANDIDATES if exit_research else LEARNING_CANDIDATES if learning_research else OI_CANDIDATES if oi_research else EARLY_CANDIDATES if early_research else REVERSAL_CANDIDATES if reversal_research else ENTRY_CANDIDATES if entry_research else CANDIDATES
    structure_history={}
    if any(c.get('structure') for c in candidates):
        data=json.loads((WORK/'data'/'index.json').read_text())
        for day in sessions:
            end=datetime.fromisoformat(day).replace(tzinfo=IST)+timedelta(days=1)
            start=end-timedelta(days=11)
            indices=[i for i,t in enumerate(data['timestamp']) if start.timestamp()<=t<end.timestamp()]
            structure_history[day]={k:[v[i] for i in indices] for k,v in data.items() if isinstance(v,list) and len(v)==len(data['timestamp'])}
    sources=(Path(__file__),ROOT/'dhan_bot_engine.py',ROOT/'index_option_replay.py',ROOT/'nifty_oi.py',ROOT/'nifty_market_structure.py',source)
    for p in sources:
        dest=run/'sources'/p.name
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(p.read_bytes())
    write(run/'declared_plan.json',{'candidates':candidates,'training_days':list(training),'holdout_days':list(holdout),
          'execution_interval':1 if finer_execution else 5,
          'previously_examined_later_dates':finer_execution,
          'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    spec=importlib.util.spec_from_file_location('previous_research_engine',source)
    previous=importlib.util.module_from_spec(spec);spec.loader.exec_module(previous)
    training_results=[]
    for config in candidates:
        result=run_candidate(config,training,legs,index,run/(config['name']+'_training'),previous.MarketMicrostructureAnalyzer,execution,structure_history)
        b=blocks(result['daily'])
        item={'config':config,'blocks':b,'gross':sum(x['gross'] for x in b),'unvalidated':len(result['unvalidated']),
              'training_trade_count':len(result['ledger']['trades'])}
        training_results.append(item)
        print('Training:',json.dumps(item),flush=True)
    minimum_trades=10 if learning_research or exit_research else 0
    ranked=sorted(training_results,key=lambda x:(x['unvalidated']==0,x['training_trade_count']>=minimum_trades,sum(b['target_met'] for b in x['blocks']),min(b['gross'] for b in x['blocks']),x['gross']),reverse=True)
    chosen=ranked[0]
    # Write selection to disk BEFORE evaluating held-out sessions.
    selection={'selection_rule':'Prefer complete replay, minimum training trades, count of 10k blocks, worst training block, total gross',
               'minimum_training_trades':minimum_trades,
               'training_results':training_results,'chosen':chosen,'holdout_days':list(holdout),
               'training_target_passed':all(b['target_met'] for b in chosen['blocks'])}
    write(run/'selection_before_holdout.json',selection)
    # Re-run chronologically from fresh state to carry training learning forward.
    result=run_candidate(chosen['config'],sessions,legs,index,run/'chosen_chronological',previous.MarketMicrostructureAnalyzer,execution,structure_history)
    held=blocks(result['daily'][20:])
    report={'run':str(run),'chosen':chosen['config'],'execution_interval_minutes':1 if finer_execution else 5,
            'minimum_training_trades':minimum_trades,'minimum_training_trades_met':chosen['training_trade_count']>=minimum_trades,
            'validation_note':'Settings selected only on first 20 days. Later dates previously examined with five-minute execution.' if finer_execution else 'Settings selected before inspecting this run\'s later results.',
            'training_target_passed':selection['training_target_passed'],
            'training_blocks':blocks(result['daily'][:20]),'validation_block':held[:1],
            'test_blocks':held[1:],'all_full_holdout_blocks_passed':bool(held) and all(b['target_met'] for b in held if b['sessions']==10),
            'unvalidated_attempts':result['unvalidated'],'coverage':json.loads((WORK/'coverage.json').read_text()),
            'target':'Rs.10,000 gross per 10 trading days','limitations':result['limitations']}
    if learning_research or exit_research:
        # Every control was declared before the training results. Later returns
        # are descriptive comparisons, never used to reselect this run's winner.
        controls=[]
        for config in candidates:
            full=result if config['name']==chosen['config']['name'] else run_candidate(
                config,sessions,legs,index,run/(config['name']+'_chronological'),
                previous.MarketMicrostructureAnalyzer,execution,structure_history)
            trades=full['ledger']['trades']
            stopouts=[t for t in trades if t['exit_reason']=='INITIAL_SL_HIT']
            immediate=sum((datetime.fromisoformat(t['exit_timestamp'])-datetime.fromisoformat(t['timestamp'])).total_seconds()<=60 for t in stopouts)
            gross_wins=[t['gross_pnl'] for t in trades if t['gross_pnl']>0]
            gross_losses=[-t['gross_pnl'] for t in trades if t['gross_pnl']<0]
            item={'config':config,'blocks':blocks(full['daily']),
                  'gross':full['ledger']['profit_without_charges'],'net':full['ledger']['net_profit_after_charges'],
                  'closed_trades':len(trades),'unvalidated':len(full['unvalidated']),
                  'gross_win_rate':len(gross_wins)/len(trades) if trades else None,
                  'average_gross_win':sum(gross_wins)/len(gross_wins) if gross_wins else None,
                  'average_gross_loss':sum(gross_losses)/len(gross_losses) if gross_losses else None,
                  'exit_reasons':dict(Counter(t['exit_reason'] for t in trades)),
                  'stopouts_within_one_minute':immediate,
                  'zero_trade_days':sum(d['trades']==0 for d in full['daily']),
                  'final_rule_status_counts':full['daily'][-1]['rule_status_counts']}
            controls.append(item)
            print('Completed control:',json.dumps({k:v for k,v in item.items() if k!='blocks'}),flush=True)
        report['control_comparison']=controls
    write(run/'report.json',report)
    write(WORK/'latest_report.json',report)
    print(json.dumps(report,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['download','download-1m','evaluate','evaluate-1m','evaluate-entries','evaluate-reversals','evaluate-early','evaluate-oi','evaluate-learning','evaluate-exits'])
    args=parser.parse_args()
    if args.command=='download':download()
    elif args.command=='download-1m':download('1')
    else:evaluate(args.command in ('evaluate-1m','evaluate-entries','evaluate-reversals','evaluate-early','evaluate-oi','evaluate-learning','evaluate-exits'),args.command=='evaluate-entries',args.command=='evaluate-reversals',args.command=='evaluate-early',args.command=='evaluate-oi',args.command=='evaluate-learning',args.command=='evaluate-exits')


if __name__=='__main__':main()

"""Read-only master-account reporting; never receives a trading key."""
from __future__ import annotations
import json, os, re, sqlite3, time
from decimal import Decimal
from datetime import datetime
import requests
import core as c

START_ISO='2026-10-10T00:00:00+02:00'
START=int(datetime.fromisoformat(START_ISO).timestamp()*1000)

def calculate(points,baseline,end):
    """Modified Dietz using cashflows inferred from the exchange's sampled PnL/equity series.
    Not exact TWR: flows are attributed to the end of each sampled interval.
    """
    t0,e0,p0=baseline; t1,e1,p1=end
    e0,e1,p0,p1=map(Decimal,(e0,e1,p0,p1))
    if t1<=t0 or e0<=0: raise ValueError('Insufficient positive baseline')
    weighted=Decimal(0); total_flows=Decimal(0); prev=(t0,e0,p0)
    for ts,e,p in points:
        if ts<=t0 or ts>t1: continue
        e,p=Decimal(e),Decimal(p)
        flow=e-prev[1]-(p-prev[2])
        weighted+=Decimal(t1-ts)/Decimal(t1-t0)*flow
        total_flows+=flow; prev=(ts,e,p)
    denom=e0+weighted
    if denom<=0: raise ValueError('Non-positive weighted capital')
    return (e1-e0-total_flows)/denom*100, e1-e0-total_flows, total_flows

def update_public():
    wallet=os.getenv('PUBLIC_MASTER_WALLET','').lower()
    target=c.DATA/'public_performance.json'
    result={'status':'unconfigured','returnPct':None,'since':START_ISO,'fetchedAt':c.now(),
            'wallet':wallet,'method':'Modified Dietz on sampled Hyperliquid perpetuals portfolio; includes unrealized PnL.'}
    if not re.fullmatch('0x[0-9a-f]{40}',wallet):
        target.write_text(json.dumps(result)); return
    result['explorer']='https://hypurrscan.io/address/'+wallet
    try:
        # Always mainnet; testnet subscriber tests never appear as real public performance.
        r=requests.post('https://api.hyperliquid.xyz/info',json={'type':'portfolio','user':wallet},timeout=15)
        r.raise_for_status(); portfolio=dict(r.json())
        path=c.DATA/'public_audit.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS samples(wallet TEXT,t INTEGER,equity TEXT,pnl TEXT,PRIMARY KEY(wallet,t))')
            saved={r[0]:(r[1],r[2]) for r in db.execute('SELECT t,equity,pnl FROM samples WHERE wallet=?',(wallet,))}
            points={}
            # Window PnL baselines can differ. Align only on exact overlapping timestamps,
            # never stitch unrelated day/month PnL values into a fictitious return.
            for window in ('perpDay','perpWeek','perpMonth','perpAllTime'):
                series=portfolio.get(window,{})
                equities=dict(series.get('accountValueHistory',[])); pnl=dict(series.get('pnlHistory',[]))
                candidate={int(ts):(str(e),str(pnl[ts])) for ts,e in equities.items() if ts in pnl and ts>=START}
                if not candidate: continue
                if saved:
                    overlaps=sorted(set(candidate)&set(saved))
                    if not overlaps: continue
                    offsets=[Decimal(saved[ts][1])-Decimal(candidate[ts][1]) for ts in overlaps]
                    if max(offsets)-min(offsets)>Decimal('0.0001'): continue
                    offset=offsets[-1]
                    candidate={ts:(e,str(Decimal(p)+offset)) for ts,(e,p) in candidate.items()}
                elif min(candidate)>START+900000: continue
                points=candidate; break
            if saved and not points: raise ValueError('No consistent overlap; history cannot be joined safely')
            db.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?,?)',[(wallet,ts,e,p) for ts,(e,p) in points.items()])
            series=db.execute('SELECT t,equity,pnl FROM samples WHERE wallet=? AND t>=? ORDER BY t',(wallet,START)).fetchall()
        if not series or series[0][0]>START+900000:
            result.update(status='insufficient_baseline',reason='No sample within 15 minutes of 10/10/2026 00:00 Europe/Madrid. No percentage invented.')
        elif len(series)<2:
            result.update(status='awaiting_next_sample',effectiveStart=series[0][0])
        else:
            percent,pnl,flows=calculate(series,series[0],series[-1])
            result.update(status='ok',returnPct=float(percent),netPnl=str(pnl),externalFlows=str(flows),
                          initialEquity=series[0][1],equity=series[-1][1],effectiveStart=series[0][0],
                          asOf=series[-1][0],estimated=True,includesUnrealized=True,
                          source='Hyperliquid public API /info portfolio perp*',sampleCount=len(series))
    except Exception:
        if target.exists():
            previous=json.loads(target.read_text())
            if previous.get('wallet')==wallet and previous.get('returnPct') is not None:
                previous['stale']=True; previous['lastError']='Live data unavailable'; result=previous
            else: result.update(status='unavailable')
        else: result.update(status='unavailable')
    temp=target.with_suffix('.tmp'); temp.write_text(json.dumps(result)); temp.replace(target)

if __name__=='__main__': c.init(); update_public()

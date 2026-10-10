"""Supervise isolated subscribers, reusing Antonio's existing strategy without edits."""
from __future__ import annotations
import hashlib, json, os, shutil, signal, subprocess, sys, time, traceback
from decimal import Decimal
from pathlib import Path
import core as c
import billing

def D(x): return Decimal(str(x))

def summarize_fills(p, fills, funding):
    """Reject incomplete/mixed positions. Fees already include builder fees; never double-count."""
    relevant=[f for f in fills if f.get('coin')==p['coin'] and int(f['time'])>=p['ts']]
    opens=[f for f in relevant if f.get('dir')==('Open Long' if p['up'] else 'Open Short')]
    closes=[f for f in relevant if f.get('dir')==('Close Long' if p['up'] else 'Close Short')]
    if not opens or not closes: raise ValueError('Missing opening/closing fills')
    first=min(int(f['time']) for f in opens); last=max(int(f['time']) for f in closes)
    relevant=[f for f in relevant if first<=int(f['time'])<=last]
    if len(relevant)!=len(opens)+len(closes): raise ValueError('Unexpected direction or manual interference')
    if any(f.get('feeToken','USDC').strip()!='USDC' for f in relevant): raise ValueError('Non-USDC fee')
    opened=sum((D(f['sz']) for f in opens),D(0)); closed=sum((D(f['sz']) for f in closes),D(0))
    if abs(opened-closed)>D('0.00000001') or abs(opened-D(p.get('initial_size',p['size'])))>D('0.00000001'):
        raise ValueError('Incomplete fills or position size changed externally')
    # A single strategy IOC opens each position; different opening orders require review.
    if len({str(f['oid']) for f in opens})!=1: raise ValueError('Multiple opening orders')
    entry=sum((D(f['px'])*D(f['sz']) for f in opens),D(0))/opened
    exit=sum((D(f['px'])*D(f['sz']) for f in closes),D(0))/closed
    fees=sum((D(f.get('fee',0)) for f in relevant),D(0))
    net=sum((D(f.get('closedPnl',0)) for f in closes),D(0))-fees
    fund=sum((D(f['delta']['usdc']) for f in funding if f.get('delta',{}).get('coin')==p['coin'] and first<=int(f['time'])<=last),D(0))
    return {'opened':first,'closed':last,'entry':str(entry),'exit':str(exit),'size':str(opened),
            'margin':str(entry*opened/D(p['leverage'])),'net_profit':str(net+fund),
            'details':json.dumps({'fills':relevant,'funding':str(fund),'exchangeFees':str(fees)})}

def run_subscriber(uid):
    strategy=Path(os.environ['STRATEGY_DIR']).resolve()
    sys.path.insert(0,str(strategy))
    import agente_real as original_runner
    from infraestructura import read_config, Store, Audit, API, InstanceLock, Rejected
    from cuenta import Account as OriginalAccount
    u=c.get_user(uid)
    if not u or u['deleting'] or not u['agent_key']: return
    folder=c.DATA/'subscribers'/uid; folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Never import the main account's passwords into the subscriber config on disk.
    text=Path(os.environ['STRATEGY_CONFIG']).read_text(encoding='utf-8-sig')
    sensitive={'HL_DIRECCION_CUENTA','HL_CLAVE_PRIVADA_API','HL_SUBCUENTA','EMAIL_REMITENTE','EMAIL_PASSWORD','EMAIL_DESTINATARIO'}
    lines=[]
    for line in text.splitlines():
        name=line.split('=',1)[0].strip().upper()
        lines.append(name+' =' if name in sensitive else line)
    config_path=folder/'strategy.txt'; config_path.write_text('\n'.join(lines))
    cfg=read_config(config_path); config_path.unlink()
    if cfg['API']!=c.HL+'/info': raise RuntimeError('HL_NETWORK differs from API in strategy config. Use a separate testnet config.')
    cfg.update(HL_DIRECCION_CUENTA=u['wallet'],HL_CLAVE_PRIVADA_API=c.unseal(u['agent_key']),HL_SUBCUENTA='',
               EMAIL_REMITENTE='',EMAIL_PASSWORD='',EMAIL_DESTINATARIO='',CARPETA_ESTADO=str(folder),
               CARPETA_CACHE=str(folder/'cache'),ARCHIVO_LOG=str(folder/'events.log'))
    original_runner.BASE=folder

    class SubscriberAudit(Audit):
        def _record(self,kind,body,mail,console):
            super()._record(kind,body,False,console)
            if mail:
                bucket=int(c.now()//300) if kind not in ('APERTURA_CONFIRMADA','CIERRE_CONFIRMADO') else 0
                # Closing mail is sent by the fill accountant with final numbers.
                if kind=='CIERRE_CONFIRMADO': return
                key=hashlib.sha256((uid+kind+str(body)+str(bucket)).encode()).hexdigest()
                c.queue_mail(uid,kind,self.clean(body),key)

    class GuardedAPI(API):
        def action(self,name,details,fn):
            with c.user_lock(uid):
                current=c.get_user(uid)
                if not current or current['deleting']: raise Rejected('Subscription deleted; account writes stopped')
                if name in ('ABRIR_ORDEN','CAMBIAR_APALANCAMIENTO'):
                    if not current['active'] or os.getenv('TRADING_ENABLED')!='true': raise Rejected('New entries paused')
                    if D(self.account_state()['marginSummary']['accountValue'])<100: raise Rejected('Minimum capital: 100 USDC')
                return super().action(name,details,fn)

    class ReportingAccount(OriginalAccount):
        def sync_open(self,p):
            if not p.get('entry') or not p.get('size'): return
            p.setdefault('initial_size',p['size'])
            # Persist before removing the original position, so crash recovery keeps the fill boundary.
            with c.user_lock(uid),c.db() as db:
                current=db.execute('SELECT deleting FROM users WHERE id=?',(uid,)).fetchone()
                if not current or current['deleting']: return
                db.execute('INSERT OR IGNORE INTO trades(id,user_id,coin,side,opened,entry,size,margin,details) VALUES(?,?,?,?,?,?,?,?,?)',
                    (p['entry_cloid'],uid,p['coin'],'LONG' if p['up'] else 'SHORT',p.get('opened_ms',p['ts']),
                     str(p['entry']),str(p['initial_size']),str(D(p['entry'])*D(p['initial_size'])/D(p['leverage'])),json.dumps({'position':p})))

        def activate(self,p,act):
            super().activate(p,act); self.sync_open(p)

        def open_group(self,signals):
            current=c.get_user(uid)
            if not current or current['deleting'] or not current['active'] or os.getenv('TRADING_ENABLED')!='true': return
            self.reconcile()
            if D(self.snapshot['marginSummary']['accountValue'])<100:
                self.a.event('NO_ENTRADA_FONDOS',{'motivo':'Capital mínimo de la cuenta: 100 USDC'})
                return
            # Do not open a new cycle in a coin until the prior fills have been completely accounted for.
            with c.db() as db: pending={r[0] for r in db.execute("SELECT coin FROM trades WHERE user_id=? AND status!='closed'",(uid,))}
            super().open_group([s for s in signals if s['coin'] not in pending])

        def reconcile(self):
            for p in self.positions.values(): self.sync_open(p)
            snapshot=super().reconcile()
            for p in self.positions.values(): self.sync_open(p)
            with c.db() as db:
                rows=db.execute("SELECT * FROM trades WHERE user_id=? AND status!='closed'",(uid,)).fetchall()
            for row in rows:
                if row['coin'] in self.positions: continue
                p=json.loads(row['details']).get('position')
                if not p: continue
                try:
                    # The original store accumulates all fills, including partial fills, across restarts.
                    with self.s.lock: fills=[json.loads(r[0]) for r in self.s.db.execute('SELECT body FROM fills')]
                    closes=[f for f in fills if f.get('coin')==p['coin'] and int(f['time'])>=p['ts'] and str(f.get('dir','')).startswith('Close')]
                    if not closes or c.now()*1000-max(int(f['time']) for f in closes)<60000: continue
                    # Range-page funding conservatively. A full page never counts as complete.
                    end=max(int(f['time']) for f in closes); cursor=int(p['ts']); funding={}
                    for _ in range(100):
                        page=self.api.info({'type':'userFunding','user':u['wallet'],'startTime':cursor,'endTime':end})
                        for f in page: funding[json.dumps(f,sort_keys=True)]=f
                        if len(page)<500: break
                        latest=max(int(f['time']) for f in page)
                        if latest<=cursor: raise ValueError('Funding pagination cannot advance')
                        cursor=latest
                    else: raise ValueError('Funding history incomplete')
                    result=summarize_fills(p,fills,list(funding.values()))
                    roi,fee=c.performance_fee(result['net_profit'],result['margin'],u['fee_basis'])
                    with c.user_lock(uid),c.db() as db:
                        current=db.execute('SELECT deleting FROM users WHERE id=?',(uid,)).fetchone()
                        if not current or current['deleting']: continue
                        db.execute("UPDATE trades SET opened=?,closed=?,entry=?,exit=?,size=?,margin=?,net_profit=?,return_pct=?,fee=?,fee_status=?,balance=?,balance_at=?,status='closed',details=?,fee_pending_at=COALESCE(fee_pending_at,?) WHERE id=? AND user_id=?",
                            (result['opened'],result['closed'],result['entry'],result['exit'],result['size'],result['margin'],
                             result['net_profit'],str(roi),str(fee),'pending' if fee else 'none',snapshot['marginSummary']['accountValue'],
                             int(c.now()*1000),result['details'],c.now() if fee else None,row['id'],uid))
                    c.queue_mail(uid,'Operación cerrada',f"{row['coin']} {row['side']}\nEntrada: {result['entry']}\nSalida: {result['exit']}\nBeneficio neto: {result['net_profit']} USDC ({roi:.4f}%)\nComisión CryptoLove pendiente: {fee} USDC.\nSaldo de cuenta observado tras reconciliación: {snapshot['marginSummary']['accountValue']} USDC.\nConsulta el área privada para firmar el pago.",key=uid+row['id']+'close')
                except Exception:
                    with c.db() as db: db.execute("UPDATE trades SET status='reconciling' WHERE id=? AND user_id=?",(row['id'],uid))
                    self.a.event('ERROR_RECUPERABLE','Contabilidad pendiente de fills/funding completos: '+row['coin'],True)
            with c.db() as db: db.execute('UPDATE users SET heartbeat=?,last_error=NULL WHERE id=? AND deleting=0',(c.now(),uid))
            return snapshot

    original_runner.Account=ReportingAccount
    with InstanceLock(folder/'runner.lock'):
        store=Store(folder/'engine.sqlite3')
        audit=SubscriberAudit(cfg,store,folder); api=GuardedAPI(cfg,audit)
        if not store.get('instance'): store.put('instance',uid)
        # Fix account/config identity across restarts; changing the active strategy needs manual review.
        fingerprint=original_runner.fingerprint(cfg)
        previous=store.get('config_fingerprint')
        if previous and previous!=fingerprint and store.get('positions',{}):
            raise RuntimeError('Strategy configuration changed with open positions; restore the previous config')
        store.put('config_fingerprint',fingerprint)
        signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        audit.start_monitor()
        try: original_runner.Runner(cfg,store,audit,api).run()
        finally: audit.stop_monitor()

def purge_deleted_subscriber(uid, process=None):
    """Stop the process before removing its state; retain tombstone on cleanup failure."""
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
    folder=c.DATA/'subscribers'/uid
    try:
        if folder.exists(): shutil.rmtree(folder)
    except OSError:
        return False
    with c.db() as db:
        db.execute('DELETE FROM users WHERE id=? AND deleting=1',(uid,))
    return True


def supervise():
    c.init(); processes={}; retry={}; audit_due=0
    def stop(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    with (c.DATA/'worker.lock').open('a') as lock:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            while True:
                with c.db() as db: users=[dict(r) for r in db.execute('SELECT * FROM users ORDER BY created')]
                allowed=0
                for u in users:
                    uid=u['id']; process=processes.get(uid)
                    if not u['deleting'] and billing.process_user(uid): u['deleting']=1
                    if u['deleting']:
                        cleaned=purge_deleted_subscriber(uid,process)
                        processes.pop(uid,None)
                        if not cleaned and c.now()>=retry.get(uid,0):
                            print('Profile cleanup failed; automation stopped. Will retry.',flush=True)
                            retry[uid]=c.now()+300
                        if cleaned: retry.pop(uid,None)
                        continue
                    if not u['agent_key'] or not u['verified']: continue
                    allowed+=1
                    if allowed>int(os.getenv('MAX_SUBSCRIBERS','10')):
                        with c.db() as db: db.execute('UPDATE users SET last_error=? WHERE id=?',('Capacity reached. Waiting for operator.',uid))
                        continue
                    if process and process.poll() is not None:
                        processes.pop(uid,None); retry[uid]=c.now()+60
                        with c.db() as db: db.execute('UPDATE users SET last_error=? WHERE id=?',('Agent stopped; retry pending. Review Hetzner logs.',uid))
                        c.queue_mail(uid,'Fallo grave','El agente se ha detenido. No se abrirán operaciones hasta recuperar la conexión. Revisa las posiciones en Hyperliquid.',uid+'crash'+str(int(c.now()//300)))
                    if uid not in processes and c.now()>=retry.get(uid,0):
                        processes[uid]=subprocess.Popen([sys.executable,__file__,'--subscriber',uid],cwd=Path(__file__).parent)
                c.send_next_mail()
                with c.db() as db:
                    db.execute('DELETE FROM tokens WHERE expires<?',(c.now(),))
                    db.execute('DELETE FROM challenges WHERE expires<?',(c.now()-86400,))
                if c.now()>=audit_due:
                    from public_audit import update_public
                    update_public(); audit_due=c.now()+300
                time.sleep(2)
        except KeyboardInterrupt: pass
        finally:
            for p in processes.values():
                if p.poll() is None: p.terminate()
            for p in processes.values():
                try: p.wait(timeout=15)
                except subprocess.TimeoutExpired: p.kill(); p.wait()

if __name__=='__main__':
    os.umask(0o077)
    if len(sys.argv)==3 and sys.argv[1]=='--subscriber':
        try: run_subscriber(sys.argv[2])
        except KeyboardInterrupt: pass
        except Exception as exc:
            # Do not log wallet keys, credentials or raw API request objects.
            print('Subscriber stopped:',type(exc).__name__,flush=True)
            sys.exit(1)
    else: supervise()

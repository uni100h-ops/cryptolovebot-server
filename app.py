"""CryptoLove private API. Run behind Caddy, one uvicorn worker."""
from __future__ import annotations
import contextlib, json, os, re, secrets, sqlite3, time
from decimal import Decimal
import requests
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr
import core as c
import billing

@contextlib.asynccontextmanager
async def lifespan(app):
    c.init()
    yield
app=FastAPI(title='CryptoLove',docs_url=None,redoc_url=None,lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[c.FRONT],allow_credentials=True,
                   allow_methods=['GET','POST'],allow_headers=['Content-Type','X-CryptoLove'])
ph=PasswordHasher(); dummy=ph.hash(secrets.token_urlsafe(30))
COOKIE='__Host-cryptolove'

@app.middleware('http')
async def protect(request,call_next):
    if request.method=='POST' and (request.headers.get('origin')!=c.FRONT or request.headers.get('x-cryptolove')!='1'):
        return JSONResponse({'detail':'Origin not allowed'},status_code=403)
    if int(request.headers.get('content-length','0') or '0')>20000:
        return JSONResponse({'detail':'Request too large'},status_code=413)
    res=await call_next(request)
    res.headers['Cache-Control']='no-store'; res.headers['X-Content-Type-Options']='nosniff'
    return res

def fail(message,status=400): raise HTTPException(status,detail=message)
def rate(request,bucket,max_count=12,period=900):
    ip=request.client.host if request.client else 'unknown'
    key=c.digest(bucket+':'+ip)
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM limits WHERE key=?',(key,)).fetchone()
        if not row or c.now()-row['start']>period:
            db.execute('INSERT OR REPLACE INTO limits VALUES(?,?,1)',(key,c.now()))
        elif row['count']>=max_count: fail('Too many attempts. Try later.',429)
        else: db.execute('UPDATE limits SET count=count+1 WHERE key=?',(key,))
        db.execute('DELETE FROM limits WHERE start<?',(c.now()-86400,))

def user(request:Request):
    t=request.cookies.get(COOKIE,'')
    with c.db() as db:
        u=db.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.verified=1 AND u.deleting=0',(c.digest(t),c.now())).fetchone()
    if not u: fail('Sign in to continue.',401)
    return u
def session(response,uid):
    token=secrets.token_urlsafe(40)
    with c.db() as db:
        db.execute('DELETE FROM sessions WHERE expires<?',(c.now(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(c.digest(token),uid,c.now()+86400))
    response.set_cookie(COOKIE,token,max_age=86400,httponly=True,secure=True,samesite='none',path='/')
def safe_user(u):
    return {k:u[k] for k in ('id','username','email','wallet','agent_address','active','fee_basis','last_error','heartbeat')}
def issue_token(uid,kind):
    token=secrets.token_urlsafe(40)
    with c.db() as db:
        db.execute('DELETE FROM tokens WHERE user_id=? AND kind=?',(uid,kind))
        db.execute('INSERT INTO tokens VALUES(?,?,?,?)',(c.digest(token),uid,kind,c.now()+(86400 if kind=='verify' else 1800)))
    link=c.FRONT+'/#'+kind+'='+token
    c.queue_mail(uid,'Confirm your email' if kind=='verify' else 'Reset password',
                 ('Confirma tu correo / Confirm your email:\n' if kind=='verify' else 'Restablecer contraseña / Reset password:\n')+link)

class Signup(BaseModel):
    username:str=Field(min_length=3,max_length=32,pattern=r'^[a-zA-Z0-9_\-]+$')
    email:EmailStr
    password:str=Field(min_length=12,max_length=128)
class Login(BaseModel):
    username:str=Field(default='',max_length=32)
    email:EmailStr
    password:str=Field(min_length=1,max_length=128)
class Token(BaseModel): token:str=Field(min_length=10,max_length=3000)
class Email(BaseModel): email:EmailStr
class Reset(Token): password:str=Field(min_length=12,max_length=128)
class Google(BaseModel): credential:str=Field(min_length=10,max_length=10000)
class Wallet(BaseModel): address:str=Field(pattern=r'^0x[0-9a-fA-F]{40}$')
class Signature(BaseModel):
    id:str=Field(min_length=1,max_length=80)
    signature:str=Field(pattern=r'^0x[0-9a-fA-F]{130}$')
class Approval(BaseModel):
    chain_id:str=Field(pattern=r'^0x[0-9a-fA-F]{1,10}$')
    consent:bool
class Pause(BaseModel): active:bool

@app.get('/health')
def health(): return {'ok':True}
@app.get('/public/config')
def config():
    return {'googleClientId':os.getenv('GOOGLE_CLIENT_ID',''),'network':c.NETWORK,'feeBasis':c.FEE_BASIS,
            'feeRate':0.01,'feeThresholdPct':1,'feeCollection':'wallet_signature',
            'feePaymentWindowHours':48,'feeReminderHours':8,
            'tradingEnabled':os.getenv('TRADING_ENABLED')=='true'}
@app.get('/public/performance')
def performance():
    path=c.DATA/'public_performance.json'
    if not path.exists(): return {'status':'unconfigured','returnPct':None,'since':'2026-10-10T00:00:00+02:00'}
    result=json.loads(path.read_text())
    result['stale']=bool(result.get('stale')) or c.now()-result.get('fetchedAt',0)>600 or (result.get('asOf') is not None and c.now()*1000-result['asOf']>1800000)
    return result
@app.get('/public/audit')
def public_audit():
    wallet=os.getenv('PUBLIC_MASTER_WALLET','').lower()
    path=c.DATA/'public_audit.sqlite3'
    if not path.exists() or not re.fullmatch('0x[0-9a-f]{40}',wallet): return {'wallet':wallet,'samples':[]}
    with sqlite3.connect(path) as db:
        rows=db.execute('SELECT t,equity,pnl FROM samples WHERE wallet=? ORDER BY t',(wallet,)).fetchall()
    return {'wallet':wallet,'source':'Hyperliquid /info portfolio perp*; PnL offsets aligned across overlapping windows',
            'columns':['utc_ms','equity_usdc','aligned_pnl_usdc'],'samples':rows}

@app.post('/auth/signup')
def signup(x:Signup,request:Request):
    rate(request,'signup',5)
    uid=secrets.token_hex(16)
    try:
        with c.db() as db:
            db.execute('INSERT INTO users(id,username,email,password,created,fee_basis) VALUES(?,?,?,?,?,?)',
                       (uid,x.username.lower(),str(x.email).lower(),ph.hash(x.password),c.now(),c.FEE_BASIS))
    except sqlite3.IntegrityError: fail('Email or username already registered.')
    issue_token(uid,'verify')
    return {'message':'Check your email to confirm your account.'}

@app.post('/auth/verify')
def verify(x:Token,request:Request):
    rate(request,'verify',30)
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        t=db.execute("SELECT * FROM tokens WHERE token=? AND kind='verify' AND expires>?",(c.digest(x.token),c.now())).fetchone()
        if not t: fail('Confirmation link expired or already used.')
        db.execute('UPDATE users SET verified=1 WHERE id=? AND deleting=0',(t['user_id'],))
        db.execute('DELETE FROM tokens WHERE token=?',(c.digest(x.token),))
    return {'message':'Email confirmed. You can sign in.'}

@app.post('/auth/resend')
def resend(x:Email,request:Request):
    rate(request,'resend',3,3600)
    with c.db() as db: u=db.execute('SELECT * FROM users WHERE email=? AND verified=0 AND deleting=0',(str(x.email).lower(),)).fetchone()
    if u: issue_token(u['id'],'verify')
    return {'message':'If the account needs confirmation, an email has been queued.'}

@app.post('/auth/login')
def login(x:Login,request:Request,response:Response):
    rate(request,'login',15)
    with c.db() as db: u=db.execute('SELECT * FROM users WHERE email=? AND deleting=0',(str(x.email).lower(),)).fetchone()
    try: valid=ph.verify(u['password'] if u and u['password'] else dummy,x.password)
    except VerificationError: valid=False
    if not valid or not u or (x.username and x.username.lower()!=u['username']): fail('Invalid sign-in details.',401)
    if not u['verified']: fail('Confirm your email first.',403)
    session(response,u['id']); return safe_user(u)

@app.post('/auth/google')
def google(x:Google,request:Request,response:Response):
    rate(request,'google',15)
    from google.oauth2 import id_token
    from google.auth.transport.requests import Request as GoogleRequest
    audience=os.getenv('GOOGLE_CLIENT_ID')
    if not audience: fail('Google sign-in is not configured.',503)
    try: info=id_token.verify_oauth2_token(x.credential,GoogleRequest(),audience)
    except Exception: fail('Invalid Google credential.',401)
    if not info.get('email_verified') or not info.get('email') or not info.get('sub'): fail('Google email is not verified.',403)
    with c.db() as db:
        u=db.execute('SELECT * FROM users WHERE google_sub=? AND deleting=0',(info['sub'],)).fetchone()
        if not u:
            # Never silently merge a Google identity into an existing local account.
            if db.execute('SELECT 1 FROM users WHERE email=?',(info['email'].lower(),)).fetchone():
                fail('This email already has a password account. Use email and password.')
            uid=secrets.token_hex(16)
            name=re.sub('[^a-z0-9_]','',info['email'].split('@')[0].lower())[:20]+'_'+uid[:6]
            db.execute('INSERT INTO users(id,username,email,google_sub,verified,created,fee_basis) VALUES(?,?,?,?,0,?,?)',
                       (uid,name,info['email'].lower(),info['sub'],c.now(),c.FEE_BASIS))
            u=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    if not u['verified']:
        issue_token(u['id'],'verify')
        return {'needsConfirmation':True}
    session(response,u['id']); return safe_user(u)

@app.post('/auth/forgot')
def forgot(x:Email,request:Request):
    rate(request,'forgot',3,3600)
    with c.db() as db: u=db.execute('SELECT * FROM users WHERE email=? AND deleting=0 AND verified=1',(str(x.email).lower(),)).fetchone()
    if u and u['password']: issue_token(u['id'],'reset')
    return {'message':'If a password account exists, a reset email has been queued.'}
@app.post('/auth/reset')
def reset(x:Reset,request:Request):
    rate(request,'reset',10)
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        t=db.execute("SELECT * FROM tokens WHERE token=? AND kind='reset' AND expires>?",(c.digest(x.token),c.now())).fetchone()
        if not t: fail('Invalid or expired reset link.')
        db.execute('UPDATE users SET password=? WHERE id=?',(ph.hash(x.password),t['user_id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(t['user_id'],))
        db.execute('DELETE FROM tokens WHERE user_id=?',(t['user_id'],))
    return {'message':'Password updated. Sign in again.'}
@app.get('/me')
def me(u=Depends(user)): return safe_user(u)
@app.post('/auth/logout')
def logout(request:Request,response:Response):
    with c.db() as db: db.execute('DELETE FROM sessions WHERE token=?',(c.digest(request.cookies.get(COOKIE,'')),))
    response.delete_cookie(COOKIE,secure=True,httponly=True,samesite='none',path='/')
    return {'ok':True}

def make_challenge(uid,kind,payload):
    key=secrets.token_hex(16)
    with c.db() as db:
        db.execute('DELETE FROM challenges WHERE user_id=? AND kind=? AND state=?',(uid,kind,'pending'))
        db.execute('INSERT INTO challenges(id,user_id,kind,payload,expires) VALUES(?,?,?,?,?)',
                   (key,uid,kind,json.dumps(payload),c.now()+300))
    return key
def claim(x,u,kind):
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute("SELECT * FROM challenges WHERE id=? AND user_id=? AND kind=? AND expires>? AND state='pending'",(x.id,u['id'],kind,c.now())).fetchone()
        if not r: fail('Authorization expired or already submitted. Refresh and retry.')
        payload=json.loads(r['payload'])
        try:
            message=encode_defunct(text=payload['message']) if kind=='wallet' else encode_typed_data(full_message=typed(payload['action']))
            signer=Account.recover_message(message,signature=x.signature).lower()
        except Exception: fail('Invalid signature.')
        expected=payload['address'] if kind=='wallet' else u['wallet']
        if signer!=expected.lower(): fail('Signature belongs to another wallet.',403)
        db.execute("UPDATE challenges SET state='submitted' WHERE id=?",(x.id,))
    return payload
def typed(action): return c.typed_action(action)
def post_hl(action,signature):
    # Timeout is uncertain: callers must retain the submitted state instead of retrying payment.
    r=requests.post(c.HL+'/exchange',json={'action':action,'nonce':action.get('nonce',action.get('time')),
                                         'signature':c.signature_parts(signature)},timeout=20)
    r.raise_for_status(); return r.json()

@app.post('/wallet/challenge')
def wallet_challenge(x:Wallet,u=Depends(user)):
    if u['wallet'] and u['wallet']!=x.address.lower(): fail('This profile is already linked to a different wallet.')
    message=f"CryptoLove wallet connection\nWebsite: {c.FRONT}\nAccount: {u['id']}\nWallet: {x.address.lower()}\nNonce: {secrets.token_hex(24)}\nExpires in 5 minutes. This signature does not authorize trading or transfers."
    ident=make_challenge(u['id'],'wallet',{'address':x.address.lower(),'message':message})
    return {'id':ident,'message':message}
@app.post('/wallet/link')
def wallet_link(x:Signature,u=Depends(user)):
    with c.user_lock(u['id']):
        p=claim(x,u,'wallet')
        try:
            with c.db() as db: db.execute('UPDATE users SET wallet=? WHERE id=?',(p['address'],u['id']))
        except sqlite3.IntegrityError: fail('Wallet already linked to another profile.')
    return {'ok':True}

@app.post('/wallet/prepare')
def wallet_prepare(x:Approval,u=Depends(user)):
    if not u['wallet'] or not x.consent: fail('Connect wallet and accept the trading authorization first.')
    if u['agent_address']: fail('Agent already authorized. Pause/resume or revoke it in Hyperliquid.')
    key=Account.create(); nonce=int(c.now()*1000)
    action={'type':'approveAgent','hyperliquidChain':c.CHAIN,'signatureChainId':x.chain_id,
            'agentAddress':key.address,'agentName':'CryptoLove','nonce':nonce}
    # Pending encrypted keys expire with their challenge; never expose the private key to the browser.
    ident=make_challenge(u['id'],'agent',{'action':action,'key':c.seal(key.key.hex())})
    return {'id':ident,'typedData':typed(action)}

@app.post('/wallet/authorize')
def wallet_authorize(x:Signature,u=Depends(user)):
    with c.user_lock(u['id']):
        fresh=c.get_user(u['id'])
        if not fresh or fresh['deleting'] or fresh['agent_address']: fail('Profile state changed. Refresh.')
        p=claim(x,u,'agent')
        try: result=post_hl(p['action'],x.signature)
        except requests.RequestException: fail('Approval response uncertain. Check Hyperliquid API wallets, then retry with a fresh authorization.',502)
        if result.get('status')!='ok': fail('Hyperliquid rejected the approval: '+str(result.get('response',''))[:250])
        with c.db() as db:
            db.execute('UPDATE users SET agent_address=?,agent_key=?,active=1,consent_at=?,consent_version=? WHERE id=?',
                       (p['action']['agentAddress'].lower(),p['key'],c.now(),'1.1-fees48h-'+u['fee_basis'],u['id']))
            db.execute('DELETE FROM challenges WHERE id=?',(x.id,))
    c.queue_mail(u['id'],'Trading authorized','Tu agente de trading CryptoLove ha sido autorizado. Puedes pausar nuevas entradas en el área privada y revocar CryptoLove en https://app.hyperliquid.xyz/API .')
    return {'ok':True}
@app.post('/subscription')
def subscription(x:Pause,u=Depends(user)):
    if not u['agent_address']: fail('Authorize the trading agent first.')
    with c.user_lock(u['id']),c.db() as db:
        db.execute('UPDATE users SET active=? WHERE id=? AND deleting=0',(int(x.active),u['id']))
    return {'ok':True}
@app.get('/trades')
def trades(u=Depends(user)):
    with c.db() as db:
        rows=db.execute('SELECT * FROM trades WHERE user_id=? ORDER BY opened DESC',(u['id'],)).fetchall()
        inv=db.execute("SELECT id,amount,state,created FROM invoices WHERE user_id=? AND state IN ('submitting','uncertain')",(u['id'],)).fetchall()
        fees=billing.status(db,u['id'])
    return {'trades':[{k:r[k] for k in r.keys() if k not in ('user_id','details')} for r in rows],
            'invoices':[dict(r) for r in inv], 'fees':fees}

@app.post('/fees/prepare')
def fee_prepare(x:Approval,u=Depends(user)):
    destination=os.getenv('TREASURY_ADDRESS','').lower()
    if not re.fullmatch('0x[0-9a-f]{40}',destination) or int(destination,16)==0: fail('Fee receiving wallet is not configured.',503)
    if not u['wallet'] or destination==u['wallet']: fail('Invalid payment wallet.')
    with c.user_lock(u['id']),c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=? AND deleting=0',(u['id'],)).fetchone():
            fail('Profile deleted. Sign in again.',401)
        if db.execute("SELECT 1 FROM invoices WHERE user_id=? AND state IN ('submitting','uncertain')",(u['id'],)).fetchone():
            fail('A prior payment is awaiting reconciliation. Do not pay twice.')
        rows=db.execute("SELECT id,fee FROM trades WHERE user_id=? AND fee_status='pending' AND status='closed'",(u['id'],)).fetchall()
        amount=sum((Decimal(r['fee']) for r in rows),Decimal(0))
        if amount<=0: fail('No fees due.')
        invoice=secrets.token_hex(16)
        action={'type':'usdSend','hyperliquidChain':c.CHAIN,'signatureChainId':x.chain_id,
                'destination':destination,'amount':format(amount,'f'),'time':int(c.now()*1000)}
        db.execute("UPDATE invoices SET state='expired' WHERE user_id=? AND state='pending'",(u['id'],))
        db.execute('INSERT INTO invoices(id,user_id,amount,trade_ids,action,created) VALUES(?,?,?,?,?,?)',
                   (invoice,u['id'],str(amount),json.dumps([r['id'] for r in rows]),json.dumps(action),c.now()))
    ident=make_challenge(u['id'],'fee',{'action':action,'invoice':invoice})
    return {'id':ident,'amount':str(amount),'destination':destination,'typedData':typed(action)}
@app.post('/fees/pay')
def fee_pay(x:Signature,u=Depends(user)):
    with c.user_lock(u['id']):
        fresh=c.get_user(u['id'])
        if not fresh or fresh['deleting']: fail('Profile deleted. Sign in again.',401)
        p=claim(x,u,'fee')
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE')
            inv=db.execute("SELECT * FROM invoices WHERE id=? AND user_id=? AND state='pending'",(p['invoice'],u['id'])).fetchone()
            if not inv: fail('Invoice is not payable.')
            if db.execute("SELECT 1 FROM invoices WHERE user_id=? AND state IN ('submitting','uncertain')",(u['id'],)).fetchone(): fail('Another payment is pending.')
            db.execute("UPDATE invoices SET state='submitting' WHERE id=?",(inv['id'],))
        try: result=post_hl(p['action'],x.signature)
        except Exception:
            with c.db() as db: db.execute("UPDATE invoices SET state='uncertain' WHERE id=?",(inv['id'],))
            fail('Payment status uncertain. Do not repeat it; contact the operator to reconcile the transfer.',502)
        if result.get('status')!='ok':
            with c.db() as db: db.execute("UPDATE invoices SET state='rejected' WHERE id=?",(inv['id'],))
            fail('Payment rejected by Hyperliquid. No fees marked as paid.')
        with c.db() as db:
            db.execute("UPDATE invoices SET state='paid',settled=? WHERE id=?",(c.now(),inv['id']))
            for tid in json.loads(inv['trade_ids']):
                db.execute("UPDATE trades SET fee_status='paid' WHERE id=? AND user_id=?",(tid,u['id']))
            billing.cancel_stale_reminders(db,u['id'])
    c.queue_mail(u['id'],'Fee payment confirmed','Comisión de CryptoLove pagada: '+inv['amount']+' USDC. Referencia: '+inv['id'])
    return {'ok':True}

@app.post('/auth/unsubscribe')
def unsubscribe(x:Token,request:Request,response:Response):
    rate(request,'unsubscribe',30)
    try: uid=c.unsubscribe_uid(x.token)
    except Exception: fail('Invalid or expired unsubscribe link.')
    c.erase_profile(uid)
    response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='none')
    return {'message':'Profile deleted and automation stopped. Manage any remaining positions and revoke the API agent in Hyperliquid.'}
@app.post('/account/delete')
def delete(response:Response,u=Depends(user)):
    c.erase_profile(u['id'])
    response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='none')
    return {'ok':True}

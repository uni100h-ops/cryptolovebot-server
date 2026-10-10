"""Private persistence, signatures and mail. No trading or network on import."""
from __future__ import annotations
import contextlib, fcntl, hashlib, hmac, json, os, secrets, smtplib, sqlite3, ssl, time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from email.message import EmailMessage
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'),interpolate=False)
DATA = Path(os.environ.get('DATA_DIR', './data')).resolve()
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
DB = DATA / 'cryptolove.sqlite3'
FRONT = os.getenv('FRONTEND_URL', 'https://cryptolove.example').rstrip('/')
PUBLIC = os.getenv('API_PUBLIC_URL', 'https://api.cryptolove.example').rstrip('/')
NETWORK = os.getenv('HL_NETWORK', 'testnet')
if NETWORK not in ('mainnet', 'testnet'): raise RuntimeError('HL_NETWORK must be mainnet or testnet')
HL = 'https://api.hyperliquid.xyz' if NETWORK == 'mainnet' else 'https://api.hyperliquid-testnet.xyz'
CHAIN = 'Mainnet' if NETWORK == 'mainnet' else 'Testnet'
FEE_BASIS = os.getenv('FEE_BASIS', 'profit')
if FEE_BASIS not in ('profit', 'margin'): raise RuntimeError('FEE_BASIS must be profit or margin')

def now(): return time.time()
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def secret():
    s = os.environ.get('APP_SECRET', '')
    if len(s) < 40: raise RuntimeError('Generate APP_SECRET before starting')
    return s
def cipher(): return Fernet(os.environ['ENCRYPTION_KEY'].encode())
def seal(s): return cipher().encrypt(s.encode()).decode()
def unseal(s): return cipher().decrypt(s.encode()).decode()
class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try: return super().__exit__(*args)
        finally: self.close()

def db():
    c = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA secure_delete=ON')
    return c

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
 password TEXT, google_sub TEXT UNIQUE, verified INTEGER NOT NULL DEFAULT 0,
 wallet TEXT UNIQUE, agent_address TEXT, agent_key TEXT, active INTEGER NOT NULL DEFAULT 0,
 deleting INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, fee_basis TEXT NOT NULL,
 consent_at REAL, consent_version TEXT, last_error TEXT, heartbeat REAL
);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE, expires REAL);
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE, kind TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT, payload TEXT, expires REAL, state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS trades (
 id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 coin TEXT NOT NULL, side TEXT NOT NULL, opened INTEGER NOT NULL, closed INTEGER,
 entry TEXT NOT NULL, exit TEXT, size TEXT NOT NULL, margin TEXT NOT NULL,
 net_profit TEXT, return_pct TEXT, fee TEXT NOT NULL DEFAULT '0', fee_status TEXT NOT NULL DEFAULT 'none',
 balance TEXT, balance_at INTEGER, status TEXT NOT NULL DEFAULT 'open', details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS invoices (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 amount TEXT NOT NULL, trade_ids TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 action TEXT, created REAL, settled REAL);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 subject TEXT, body TEXT, created REAL, next_attempt REAL DEFAULT 0, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, start REAL, count INTEGER);
CREATE INDEX IF NOT EXISTS trade_user ON trades(user_id,opened);
CREATE TABLE IF NOT EXISTS fee_reminders (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 anchor REAL NOT NULL, last_slot INTEGER NOT NULL DEFAULT -1
);
'''
def init():
    secret(); cipher()
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        c.executescript(SCHEMA)
        # Additive migration: old pending fees receive a full 48-hour notice period.
        c.execute('BEGIN IMMEDIATE')
        if 'fee_pending_at' not in {r['name'] for r in c.execute('PRAGMA table_info(trades)')}:
            c.execute('ALTER TABLE trades ADD COLUMN fee_pending_at REAL')
        if 'kind' not in {r['name'] for r in c.execute('PRAGMA table_info(outbox)')}:
            c.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'notification'")
        c.execute("UPDATE trades SET fee_pending_at=? WHERE fee_status='pending' AND status='closed' AND fee_pending_at IS NULL", (now(),))
    os.chmod(DB, 0o600)

@contextlib.contextmanager
def user_lock(uid):
    if not uid.isalnum(): raise ValueError('Invalid user id')
    p = DATA / 'locks'; p.mkdir(exist_ok=True, mode=0o700)
    with (p / uid).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN)

def get_user(uid):
    with db() as c: return c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def unsubscribe_token(uid): return URLSafeTimedSerializer(secret(), salt='delete-profile-v1').dumps(uid)
def unsubscribe_uid(token):
    # Links work for one year; every new notification gets a fresh link.
    return URLSafeTimedSerializer(secret(), salt='delete-profile-v1').loads(token, max_age=31536000)
def queue_mail(uid, subject, body, key=None):
    with db() as c:
        u = c.execute('SELECT email,deleting FROM users WHERE id=?', (uid,)).fetchone()
        if u and not u['deleting'] and u['email']:
            c.execute('INSERT OR IGNORE INTO outbox(id,user_id,subject,body,created) VALUES(?,?,?,?,?)',
                      (key or secrets.token_hex(16), uid, subject, body, now()))

def send_next_mail():
    with db() as c:
        r = c.execute('SELECT o.*,u.email FROM outbox o JOIN users u ON u.id=o.user_id WHERE u.deleting=0 AND o.next_attempt<=? ORDER BY created LIMIT 1', (now(),)).fetchone()
    if not r: return
    # The same lock surrounds deletion and account writes: no new mail after deletion returns.
    with user_lock(r['user_id']):
        u=get_user(r['user_id'])
        if not u or u['deleting']: return
        # Payment/deletion may have removed this message while we waited for the lock.
        with db() as conn:
            r=conn.execute('SELECT * FROM outbox WHERE id=?',(r['id'],)).fetchone()
            if not r: return
            body=r['body']
            if r['kind']=='fee_reminder':
                from billing import reminder_body
                body=reminder_body(conn,u['id'])
                if body is None:
                    conn.execute('DELETE FROM outbox WHERE id=?',(r['id'],))
                    return
        link=FRONT+'/#unsubscribe='+unsubscribe_token(u['id'])
        m=EmailMessage(); m['From']=os.environ['SMTP_FROM']; m['To']=u['email']
        m['Subject']='CryptoLove · '+r['subject']; m['Message-ID']='<'+r['id']+'@cryptolove.local>'
        m['List-Unsubscribe']='<'+link+'>'
        m.set_content(body+'\n\nUnsubscribe / Darme de baja y borrar mi perfil:\n'+link+
                      '\n\nLa baja detiene la automatización. Las posiciones abiertas permanecen en Hyperliquid.\n')
        try:
            host=os.environ['SMTP_HOST']; port=int(os.getenv('SMTP_PORT','587'))
            cls=smtplib.SMTP_SSL if port==465 else smtplib.SMTP
            with cls(host,port,timeout=15) as smtp:
                if port!=465: smtp.starttls(context=ssl.create_default_context())
                smtp.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD'])
                smtp.send_message(m)
            with db() as c: c.execute('DELETE FROM outbox WHERE id=?',(r['id'],))
        except Exception:
            with db() as c: c.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?',(now()+min(3600,60*2**min(r['attempts'],6)),r['id']))

def performance_fee(net, margin, basis='profit'):
    net,margin=Decimal(str(net)),Decimal(str(margin))
    if not net.is_finite() or not margin.is_finite() or margin<=0: raise ValueError('Invalid monetary values')
    roi=net/margin*100
    base=net if basis=='profit' else margin
    fee=(base*Decimal('.01')).quantize(Decimal('.000001'),rounding=ROUND_DOWN) if roi>1 else Decimal(0)
    return roi,fee

def typed_action(action):
    if action['type']=='approveAgent':
        primary='HyperliquidTransaction:ApproveAgent'
        fields=[('hyperliquidChain','string'),('agentAddress','address'),('agentName','string'),('nonce','uint64')]
    elif action['type']=='usdSend':
        primary='HyperliquidTransaction:UsdSend'
        fields=[('hyperliquidChain','string'),('destination','string'),('amount','string'),('time','uint64')]
    else: raise ValueError('Action is not allowed')
    return {'domain':{'name':'HyperliquidSignTransaction','version':'1','chainId':int(action['signatureChainId'],16),
                       'verifyingContract':'0x'+'0'*40},
            'types':{'EIP712Domain':[{'name':n,'type':t} for n,t in [('name','string'),('version','string'),('chainId','uint256'),('verifyingContract','address')]],
                     primary:[{'name':n,'type':t} for n,t in fields]},
            'primaryType':primary,'message':{n:action[n] for n,t in fields}}

def signature_parts(sig):
    raw=bytes.fromhex(sig.removeprefix('0x'))
    if len(raw)!=65: raise ValueError('Invalid signature')
    v=raw[64]; v=v+27 if v<27 else v
    if v not in (27,28): raise ValueError('Invalid signature v')
    return {'r':'0x'+raw[:32].hex(),'s':'0x'+raw[32:64].hex(),'v':v}

def erase_profile_locked(conn,uid):
    """Caller must hold user_lock(uid) and a database transaction."""
    if not conn.execute('SELECT 1 FROM users WHERE id=? AND deleting=0',(uid,)).fetchone(): return False
    conn.execute("UPDATE users SET active=0,deleting=1,email=?,username=?,password=NULL,google_sub=NULL,agent_key=NULL,agent_address=NULL,wallet=NULL,last_error=NULL,consent_at=NULL,consent_version=NULL,heartbeat=NULL WHERE id=?",('deleted-'+uid,'deleted-'+uid,uid))
    for table in ('sessions','tokens','challenges','invoices','trades','outbox','fee_reminders'):
        conn.execute(f'DELETE FROM {table} WHERE user_id=?',(uid,))
    return True

def erase_profile(uid):
    with user_lock(uid), db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        erase_profile_locked(conn,uid)
    # Supervisor stops the process, removes per-user state and deletes the anonymous tombstone.
    # Public blockchain transactions are not erasable.
    with db() as conn: conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')


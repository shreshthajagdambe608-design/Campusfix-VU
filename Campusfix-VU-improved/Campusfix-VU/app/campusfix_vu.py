#!/usr/bin/env python3
"""VU CampusFix — campus issue reporting with AI-assisted triage.

Anyone can view the dashboard and submit reports. Moderator actions (editing status,
verifying, deleting, map editing, audit log) need the moderator token.
UNRESTRICTED_MODE=true removes that protection and is meant only for throw-away local demos.
"""
from __future__ import annotations
import os, sys, json, sqlite3, re, hmac, hashlib, base64, secrets, time, threading, webbrowser, urllib.parse, socket, math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / 'static'
sys.path.insert(0, str(APP_DIR))

def load_env(path: Path):
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k, v = line.split('=', 1); k = k.strip(); v = v.strip().strip('"\'')
            if k and k not in os.environ: os.environ[k] = v
    except OSError: pass

load_env(PROJECT_ROOT / '.env')

def env_flag(name, default=False):
    return os.environ.get(name, 'true' if default else 'false').strip().lower() in ('1', 'true', 'yes', 'on')
def env_int(name, default):
    try: return int(os.environ.get(name, default))
    except (TypeError, ValueError): return default

DATA_DIR = Path(os.environ.get('CAMPUSFIX_DATA_DIR', PROJECT_ROOT / 'data'))
os.environ.setdefault('CAMPUSFIX_DB_PATH', str(DATA_DIR / 'campusfix_vu.db'))
os.environ.setdefault('OLLAMA_HOST', 'http://127.0.0.1:11434')
os.environ.setdefault('OLLAMA_MODEL', 'gemma4:e4b')
import campusfix_engine as core

UNRESTRICTED_MODE = env_flag('UNRESTRICTED_MODE')      # demo only: everyone is a moderator
DEMO_DATA = env_flag('CAMPUSFIX_DEMO_DATA')            # seed sample reports into an empty database
TRUST_PROXY = env_flag('TRUST_PROXY')                  # read client IP from X-Forwarded-For (behind a proxy only)
DB_PATH = Path(os.environ['CAMPUSFIX_DB_PATH'])
COOKIE_NAME = 'vu_campusfix_session'
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY = 15 * 1024 * 1024
REPORT_RATE_LIMIT = env_int('REPORT_RATE_LIMIT', 12)      # new reports per IP per 10 minutes
ANALYZE_RATE_LIMIT = env_int('ANALYZE_RATE_LIMIT', 40)    # analysis requests per IP per 10 minutes
LOGIN_MAX_FAILURES = env_int('LOGIN_MAX_FAILURES', 5)     # wrong tokens per IP per 5 minutes
CATEGORIES = core.CATEGORIES
PRIORITIES = core.PRIORITIES
STATUSES = core.STATUSES
VERIFY_STATES = ['Pending verification','Confirmed genuine','Possible prank','Duplicate report','Not an issue','Unable to verify']

PUBLIC_USER = {'email': 'public-visitor', 'name': 'Public visitor', 'is_admin': False}
ADMIN_USER = {'email': 'moderator', 'name': 'Moderator', 'is_admin': True}
DEMO_USER = {'email': 'open-access-demo', 'name': 'Open access demo', 'is_admin': True}

# ---------- moderator token ----------
def load_admin_token():
    """ADMIN_TOKEN from .env wins; otherwise a random token is created once and kept in data/.admin_token."""
    token = os.environ.get('ADMIN_TOKEN', '').strip()
    if token: return token, 'env'
    path = DATA_DIR / '.admin_token'
    try:
        if path.exists():
            saved = path.read_text(encoding='utf-8').strip()
            if saved: return saved, 'file'
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(12)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(token)
        return token, 'generated'
    except FileExistsError:
        return path.read_text(encoding='utf-8').strip(), 'file'
    except OSError:
        return secrets.token_urlsafe(12), 'memory'

ADMIN_TOKEN, ADMIN_TOKEN_SOURCE = load_admin_token()

def token_matches(supplied: str) -> bool:
    a = hashlib.sha256(supplied.encode('utf-8')).digest(); b = hashlib.sha256(ADMIN_TOKEN.encode('utf-8')).digest()
    return hmac.compare_digest(a, b)

# ---------- tiny in-memory rate limiter ----------
class RateLimiter:
    def __init__(self): self.hits = defaultdict(list); self.lock = threading.Lock()
    def count(self, key, window):
        now = time.monotonic()
        with self.lock:
            self.hits[key] = [t for t in self.hits[key] if now - t < window]
            return len(self.hits[key])
    def add(self, key):
        with self.lock: self.hits[key].append(time.monotonic())
    def allow(self, key, limit, window):
        if self.count(key, window) >= limit: return False
        self.add(key); return True

LIMITER = RateLimiter()

# Extend the single model call to return a cautious evidence-completeness assessment too.
core.SYSTEM_PROMPT += '''\n\nAlso assess report evidence quality cautiously. Do NOT claim that an issue is definitely real or definitely a prank based only on a message or photo. Add keys: integrity_score (integer 0-100 measuring how complete and internally plausible the supplied evidence is, NOT probability the report is true), integrity_assessment (one of "Plausible evidence — human check still required", "Needs human verification", "Suspicious signals — do not auto-reject"), integrity_reason (one concise evidence-based reason), suspicious_signals (array of concrete signals; [] if none). Repeated/short wording or a duplicate may be innocent; never treat these alone as proof of a prank. Keep classification and integrity separate.'''
_ORIGINAL_NORMALIZE = core.normalize_analysis

def normalize_with_integrity(data, desc, loc, has_image):
    result = _ORIGINAL_NORMALIZE(data, desc, loc, has_image)
    try: score = int(max(0, min(100, float(data.get('integrity_score', 55)))) )
    except (TypeError, ValueError): score = 55
    assessment = str(data.get('integrity_assessment') or 'Needs human verification')
    valid = {'Plausible evidence — human check still required','Needs human verification','Suspicious signals — do not auto-reject'}
    if assessment not in valid: assessment = 'Needs human verification'
    reason = str(data.get('integrity_reason') or 'Automated review cannot confirm the physical issue; a staff check is required.')[:500]
    signals = data.get('suspicious_signals', [])
    if isinstance(signals, str): signals = [signals]
    if not isinstance(signals, list): signals = []
    result.update({'integrity_score':score,'integrity_assessment':assessment,'integrity_reason':reason,'suspicious_signals':[str(x)[:180] for x in signals if str(x).strip()][:6]})
    return result
core.normalize_analysis = normalize_with_integrity

DEFAULT_PLACES = [
    {'id':'gate-security','name':'Main Gate & Security','kind':'Entry / Safety','x':105,'y':470},
    {'id':'academic-a','name':'Academic Wing A','kind':'Academic','x':265,'y':190},
    {'id':'library-study','name':'Library & Study','kind':'Learning','x':270,'y':355},
    {'id':'academic-b','name':'Academic Wing B','kind':'Academic','x':470,'y':175},
    {'id':'student-services','name':'Student Services','kind':'Services','x':480,'y':375},
    {'id':'labs-workshops','name':'Labs & Workshops','kind':'Labs','x':700,'y':195},
    {'id':'cafeteria','name':'Cafeteria / Student Hub','kind':'Student life','x':700,'y':380},
    {'id':'utilities','name':'Facilities & Utilities','kind':'Operations','x':865,'y':200},
    {'id':'sports-open','name':'Sports & Open Area','kind':'Outdoor','x':850,'y':420},
    {'id':'parking','name':'Transport & Parking','kind':'Transport','x':135,'y':545},
    {'id':'other-area','name':'Other / Unmapped Area','kind':'Other','x':475,'y':540},
]


def stamp(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def utcnow(): return datetime.now(timezone.utc).timestamp()

def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try: conn.execute('PRAGMA journal_mode=WAL')
    except sqlite3.DatabaseError: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS reports (
      id TEXT PRIMARY KEY, created_at TEXT NOT NULL, user_description TEXT NOT NULL,
      user_location TEXT NOT NULL, image_data TEXT, ai_analysis TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'Submitted', assigned_to TEXT, staff_notes TEXT)''')
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(reports)').fetchall()}
    extra = {'reporter_email':'TEXT','reporter_sub':'TEXT','building_id':'TEXT',
             'verification_state':"TEXT NOT NULL DEFAULT 'Pending verification'",
             'verified_by':'TEXT','verified_at':'TEXT','updated_at':'TEXT'}
    for name, decl in extra.items():
        if name not in columns: conn.execute(f'ALTER TABLE reports ADD COLUMN {name} {decl}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS map_places (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'Campus zone',
      x REAL NOT NULL, y REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
      actor_email TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT, detail TEXT)''')
    if conn.execute('SELECT COUNT(*) c FROM map_places').fetchone()['c'] == 0:
        now = stamp()
        for p in DEFAULT_PLACES:
            conn.execute('INSERT INTO map_places(id,name,kind,x,y,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                         (p['id'], p['name'], p['kind'], p['x'], p['y'], now, now))
    conn.commit(); return conn

def audit(actor, action, object_id='', detail=''):
    conn = connect_db()
    conn.execute('INSERT INTO audit_events(created_at,actor_email,action,object_id,detail) VALUES(?,?,?,?,?)',
                 (stamp(), actor, action, object_id, str(detail)[:1000]))
    conn.commit(); conn.close()

def get_places():
    conn = connect_db(); rows = conn.execute('SELECT * FROM map_places ORDER BY name').fetchall(); conn.close()
    return [dict(r) for r in rows]

LIST_COLUMNS = ('id,created_at,user_description,user_location,ai_analysis,status,assigned_to,staff_notes,'
                'building_id,verification_state,verified_by,verified_at,updated_at,'
                "(image_data IS NOT NULL AND image_data != '') AS has_image")

def _parse_row(row):
    r = dict(row)
    try: r['ai_analysis'] = json.loads(r.get('ai_analysis') or '{}')
    except (ValueError, TypeError): r['ai_analysis'] = {}
    return r

def get_all_rows():
    """All reports WITHOUT the (large) image payloads; the single-report view loads the image."""
    conn = connect_db(); rows = conn.execute(f'SELECT {LIST_COLUMNS} FROM reports ORDER BY created_at DESC').fetchall(); conn.close()
    return [_parse_row(r) for r in rows]

def get_one_report(rid):
    conn = connect_db(); row = conn.execute('SELECT * FROM reports WHERE id=?', (rid,)).fetchone(); conn.close()
    return _parse_row(row) if row else None

def public_report(r, admin=False, include_image=False):
    """Never expose reporter identity; staff-only fields only go to moderators."""
    if not r: return None
    out = dict(r)
    if not include_image: out.pop('image_data', None)
    out.pop('reporter_email', None); out.pop('reporter_sub', None)
    if not admin:
        out.pop('verified_by', None); out.pop('staff_notes', None)
    return out

def create_report(desc, location, image_data, analysis, building_id):
    report_id = 'VU-' + datetime.now().strftime('%y%m%d') + '-' + secrets.token_hex(2).upper()
    now = stamp(); analysis_json = json.dumps(analysis, ensure_ascii=False)
    conn = connect_db()
    conn.execute('''INSERT INTO reports(id,created_at,user_description,user_location,image_data,ai_analysis,status,assigned_to,staff_notes,reporter_email,reporter_sub,building_id,verification_state,verified_by,verified_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (report_id, now, desc.strip() or 'Image submitted without written description.', location.strip(), image_data, analysis_json, 'Submitted', analysis.get('suggested_team') or core.team_for(analysis.get('category', 'Other')), None, None, None, building_id, 'Pending verification', None, None, now))
    conn.commit(); conn.close()
    audit('public-visitor', 'REPORT_CREATED', report_id, analysis.get('category', 'Other'))
    return get_one_report(report_id)

def update_report(rid, payload, user):
    allowed = {}
    if 'status' in payload:
        if payload['status'] not in STATUSES: raise ValueError('Invalid workflow status.')
        allowed['status'] = payload['status']
    if 'assigned_to' in payload: allowed['assigned_to'] = str(payload.get('assigned_to') or '')[:160]
    if 'staff_notes' in payload: allowed['staff_notes'] = str(payload.get('staff_notes') or '')[:3000]
    if 'verification_state' in payload:
        if payload['verification_state'] not in VERIFY_STATES: raise ValueError('Invalid verification decision.')
        if payload['verification_state'] == 'Possible prank' and len(str(payload.get('staff_notes') or '').strip()) < 8:
            raise ValueError('Add at least 8 characters of staff reasoning before marking a report as a possible prank.')
        allowed['verification_state'] = payload['verification_state']
    row = get_one_report(rid)
    if not row: return None
    if not allowed: return row
    if 'verification_state' in allowed and allowed['verification_state'] != 'Pending verification':
        allowed['verified_by'] = user['email']; allowed['verified_at'] = stamp()
    elif allowed.get('verification_state') == 'Pending verification':
        allowed['verified_by'] = None; allowed['verified_at'] = None
    allowed['updated_at'] = stamp()
    conn = connect_db(); columns = ', '.join(f'{key}=?' for key in allowed)
    conn.execute(f'UPDATE reports SET {columns} WHERE id=?', (*allowed.values(), rid)); conn.commit(); conn.close()
    audit(user['email'], 'REPORT_UPDATED', rid, json.dumps(allowed, ensure_ascii=False))
    return get_one_report(rid)

# ---------- optional sample data (CAMPUSFIX_DEMO_DATA=true) ----------
DEMO_REPORTS = [
    # (description, zone id, room, status, verification, notes, hours ago)
    ('Water is pooling under the sink in the restroom and spreading into the corridor. The floor is slippery.', 'academic-a', 'Level 2 · near Room 204', 'In Progress', 'Confirmed genuine', 'Plumber on site; isolated the supply line.', 5),
    ('A loose wire is hanging from the ceiling outside the electronics lab entrance.', 'labs-workshops', 'Ground floor · lab corridor', 'Submitted', 'Pending verification', '', 3),
    ('Wi-Fi keeps dropping for every computer in the library study zone since this morning.', 'library-study', 'Level 1 · computer zone', 'Assigned', 'Pending verification', '', 26),
    ('The ramp to the Academic Wing B entrance is blocked by stored boxes, wheelchair users cannot get in.', 'academic-b', 'Main entrance ramp', 'Resolved', 'Confirmed genuine', 'Boxes moved to storage on request.', 50),
    ('Bins near the cafeteria seating area are overflowing and there is a bad smell with flies around.', 'cafeteria', 'Outdoor seating', 'Submitted', 'Pending verification', '', 8),
    ('The street light near the parking entrance is not working so the path is very dark at night.', 'parking', 'Parking entrance', 'Submitted', 'Pending verification', '', 30),
    ('asdfasdf test test just a prank report', 'other-area', 'n/a', 'Dismissed', 'Possible prank', 'Checked the location on site; nothing found and the text reads as a test message.', 40),
]

def seed_demo_data():
    conn = connect_db(); n = conn.execute('SELECT COUNT(*) c FROM reports').fetchone()['c']; conn.close()
    if n: return 0
    places = {p['id']: p['name'] for p in get_places()}
    for desc, zone, room, status, verification, notes, hours in DEMO_REPORTS:
        loc = places.get(zone, 'Other / Unmapped Area') + ' · ' + room
        analysis = core.fallback_analyze(desc, loc, False); analysis['analysis_mode'] = 'Sample data (rule-based triage)'
        analysis = merge_integrity(analysis, desc, loc, None, [])
        created = create_report(desc, loc, None, analysis, zone)
        when = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        conn = connect_db()
        conn.execute('UPDATE reports SET created_at=?, status=?, verification_state=?, staff_notes=?, verified_by=?, updated_at=? WHERE id=?',
                     (when, status, verification, notes or None, 'moderator' if verification != 'Pending verification' else None, when, created['id']))
        conn.commit(); conn.close()
    return len(DEMO_REPORTS)

def sniff_suspicious_signals(desc, loc, image_data, duplicates):
    text=(desc or '').strip(); low=text.lower(); words=re.findall(r"[a-z0-9']+", low)
    signals=[]; hard=False
    prank_patterns=[r'\bjust (a )?prank\b',r'\bthis is fake\b',r'\bnot a real issue\b',r'\bjust kidding\b',r'\bprank report\b',r'\bfake report\b',r'\btest test\b',r'\basdf+\b',r'\bqwerty\b']
    for pat in prank_patterns:
        if re.search(pat, low): signals.append('Text explicitly resembles a prank, fake report, or test submission.'); hard=True; break
    if len(words)<4 and not image_data: signals.append('Very little descriptive detail; the physical problem is not clear.')
    if len(words)>=8 and len(set(words))/max(1,len(words))<0.38:
        signals.append('Unusually repetitive wording; ask the reporter to clarify.')
    if re.search(r'https?://|www\.', low): signals.append('Contains a web link unrelated to normal incident detail; review it before opening.')
    if re.search(r'(.)\1{7,}', low): signals.append('Contains an unusually long repeated-character sequence.')
    if len(words)>3 and not any(w in low for w in ('broken','leak','smell','blocked','not working','crack','water','wire','light','dirty','damaged','noise','door','chair','floor','ceiling','wifi','power','fire','smoke','lift','toilet','tap','photo','visible','saw','noticed','reported','stopped','missing','spill','issue','problem','leaking','overflow','sparking','cracked','unsafe','stuck')) and not image_data:
        signals.append('Description does not clearly state a campus maintenance or safety concern.')
    if duplicates and max(float(d.get('similarity_score',0)) for d in duplicates)>=0.65:
        signals.append('Strongly resembles an existing unresolved report; check whether it is a duplicate rather than a prank.')
    # Image presence is evidence submitted, never proof that a claim is true.
    score=50
    if len(words)>=18: score+=18
    elif len(words)>=8: score+=10
    elif len(words)>=4: score+=3
    else: score-=12
    if image_data: score+=12
    if len((loc or '').strip())>=9: score+=10
    if any(ch.isdigit() for ch in (loc or '')): score+=3
    if any('explicitly resembles' in s for s in signals): score-=45
    score-=min(25, sum(7 for s in signals if 'repetitive' in s or 'repeated-character' in s or 'does not clearly state' in s or 'web link' in s))
    if duplicates and max(float(d.get('similarity_score',0)) for d in duplicates)>=0.65: score-=5
    score=max(0,min(100,score))
    if hard: assessment='Suspicious signals — do not auto-reject'; reason='The text includes wording associated with a fake/test submission. A moderator should ask for clarification; do not reject automatically.'
    elif score>=72 and not any('does not clearly state' in s for s in signals): assessment='Plausible evidence — human check still required'; reason='The submission has useful details and/or an image, but the app cannot confirm the physical condition remotely.'
    else: assessment='Needs human verification'; reason='The provided details are not enough to verify the physical issue. Check location and evidence on site before deciding.'
    return {'integrity_score':score,'integrity_assessment':assessment,'integrity_reason':reason,'suspicious_signals':signals[:6]}

def merge_integrity(analysis, desc, loc, image_data, duplicates):
    heur=sniff_suspicious_signals(desc,loc,image_data,duplicates)
    model_signals=analysis.get('suspicious_signals') or []
    combined=[]
    for item in list(heur['suspicious_signals'])+list(model_signals):
        item=str(item).strip()
        if item and item not in combined: combined.append(item)
    # Report quality score is an evidence completeness indicator, not a prediction of truth.
    model_score=analysis.get('integrity_score',heur['integrity_score'])
    try: model_score=max(0,min(100,int(float(model_score))))
    except (TypeError,ValueError): model_score=heur['integrity_score']
    score=round(0.75*heur['integrity_score']+0.25*model_score)
    hard_prank = any('explicitly resembles a prank' in s.lower() for s in heur['suspicious_signals'])
    other_signals = [s for s in combined if 'duplicate' not in s.lower()]
    if hard_prank:
        assessment='Suspicious signals — do not auto-reject'
    elif score>=72 and not other_signals:
        assessment='Plausible evidence — human check still required'
    else:
        assessment='Needs human verification'
    analysis['integrity_score']=score
    analysis['integrity_assessment']=assessment
    analysis['integrity_reason']=heur['integrity_reason'] if heur['suspicious_signals'] else str(analysis.get('integrity_reason') or heur['integrity_reason'])
    analysis['suspicious_signals']=combined[:6]
    analysis['verification_note']='Pending human verification. The score measures evidence completeness, not the probability that the claim is true.'
    return analysis

def map_match(report, places):
    if report.get('building_id') and any(p['id']==report['building_id'] for p in places): return report['building_id']
    text=(report.get('user_location') or '').lower()
    scored=[]
    for p in places:
        parts=re.findall(r'[a-z0-9]+',p['name'].lower())
        score=sum(1 for part in parts if len(part)>3 and part in text)
        if score: scored.append((score,p['id']))
    return sorted(scored,reverse=True)[0][1] if scored else 'other-area'

def map_payload():
    places=get_places(); reports=get_all_rows(); positions={p['id']:p for p in places}
    markers=[]
    for r in reports:
        pid=map_match(r,places); p=positions.get(pid,positions.get('other-area',{'x':500,'y':540}))
        # Stable, visually separated micro-offsets for several tickets at one building.
        hx=int(hashlib.sha1(r['id'].encode()).hexdigest()[:8],16)
        angle=(hx%360)*3.14159265/180; radius=92+(hx%3)*13
        x=max(22,min(978,float(p['x'])+math.cos(angle)*radius))
        y=max(22,min(598,float(p['y'])+math.sin(angle)*radius))
        status=r.get('status','Submitted')
        color='#20A37A' if status=='Resolved' else ('#9CA3AF' if status=='Dismissed' else '#E33F47')
        markers.append({'id':r['id'],'title':(r.get('ai_analysis') or {}).get('title','Campus issue'),'category':(r.get('ai_analysis') or {}).get('category','Other'),'priority':(r.get('ai_analysis') or {}).get('priority','Medium'),'status':status,'verification_state':r.get('verification_state','Pending verification'),'building_id':pid,'x':round(x,1),'y':round(y,1),'color':color})
    return {'places':places,'markers':markers,'schematic':True,'notice':'Editable schematic only: building positions are placeholders until a VU administrator calibrates them against an authorised campus plan.'}


# ---------- signed moderator session ----------
def session_secret():
    env = os.environ.get('SESSION_SECRET', '').strip()
    if env: return env.encode('utf-8')
    path = DATA_DIR / '.session_secret'
    try:
        if path.exists(): return path.read_bytes()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_bytes(48)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as f: f.write(raw)
        return raw
    except FileExistsError: return path.read_bytes()
    except OSError: return _EPHEMERAL_SECRET

_EPHEMERAL_SECRET = secrets.token_bytes(48)

def b64e(raw: bytes) -> str: return base64.urlsafe_b64encode(raw).decode().rstrip('=')
def b64d(txt: str) -> bytes: return base64.urlsafe_b64decode(txt + '=' * ((4 - len(txt) % 4) % 4))

def make_session():
    payload = {'role': 'admin', 'iat': int(utcnow()), 'exp': int(utcnow() + SESSION_SECONDS)}
    token = b64e(json.dumps(payload, separators=(',', ':')).encode())
    sig = b64e(hmac.new(session_secret(), token.encode(), hashlib.sha256).digest())
    return token + '.' + sig

def parse_session(token):
    if not token or '.' not in token: return None
    try:
        payload, sig = token.rsplit('.', 1)
        expected = b64e(hmac.new(session_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected): return None
        data = json.loads(b64d(payload).decode())
        if data.get('role') != 'admin' or int(data.get('exp', 0)) < utcnow(): return None
        return dict(ADMIN_USER)
    except Exception: return None

def get_request_user(handler):
    cookie = SimpleCookie()
    try: cookie.load(handler.headers.get('Cookie', ''))
    except Exception: return None
    morsel = cookie.get(COOKIE_NAME)
    return parse_session(morsel.value) if morsel else None

def page_html():
    html = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
    for key, value in (('__CATEGORIES__', CATEGORIES), ('__PRIORITIES__', PRIORITIES), ('__STATUSES__', STATUSES), ('__VERIFY_STATES__', VERIFY_STATES)):
        html = html.replace(key, json.dumps(value, ensure_ascii=False))
    return html

def validate_image_data(value):
    if value in (None, ''): return None
    if not isinstance(value, str) or len(value) > 11*1024*1024:
        raise ValueError('Image data is missing or too large; choose an image under 8 MB.')
    if not re.match(r'^data:image/(?:jpeg|png|webp|gif);base64,[A-Za-z0-9+/=\r\n]+$', value):
        raise ValueError('Image must be a valid base64-encoded JPEG, PNG, WebP or GIF image.')
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = 'VU-CampusFix/2.0'
    timeout = 60

    def log_message(self, fmt, *args): print('[VU CampusFix]', fmt % args)

    def client_ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get('X-Forwarded-For', '')
            if fwd: return fwd.split(',')[0].strip()
        return self.client_address[0]

    def _security_headers(self):
        self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin'); self.send_header('X-Frame-Options', 'DENY')

    def send_json(self, status, data, extra_headers=None):
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8'); self.send_header('Content-Length', str(len(raw)))
        self._security_headers()
        for k, v in (extra_headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)

    def send_html(self):
        raw = page_html().encode('utf-8')
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(raw)))
        self._security_headers()
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.end_headers(); self.wfile.write(raw)

    def read_payload(self):
        try: length = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError: raise ValueError('Invalid Content-Length header.')
        if length < 0 or length > MAX_BODY: raise ValueError('Request is too large. Images must be no larger than 8 MB.')
        raw = self.rfile.read(length) if length else b'{}'
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict): raise ValueError('Expected a JSON object.')
        return data

    def current_user(self):
        if UNRESTRICTED_MODE: return DEMO_USER
        return get_request_user(self) or PUBLIC_USER

    def require_admin(self, user):
        if user.get('is_admin'): return True
        self.send_json(401, {'error': 'Moderator sign-in required.'}); return False

    def session_cookie(self, token, max_age=SESSION_SECONDS):
        secure = self.headers.get('X-Forwarded-Proto', '').lower() == 'https'
        return {'Set-Cookie': f'{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict' + ('; Secure' if secure else '')}

    def _safe(self, fn):
        try: fn()
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e:
            print('[VU CampusFix] Unhandled error:', repr(e))
            try: self.send_json(500, {'error': 'Internal server error.'})
            except Exception: pass

    def do_GET(self): self._safe(self._get)
    def do_POST(self): self._safe(self._post)
    def do_PATCH(self): self._safe(self._patch)
    def do_DELETE(self): self._safe(self._delete)

    # ----- GET -----
    def _get(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'): return self.send_html()
        if path == '/vu-logo.webp':
            try: raw = (STATIC_DIR / 'vu_logo.webp').read_bytes()
            except OSError: return self.send_json(404, {'error': 'VU logo asset not found'})
            self.send_response(200); self.send_header('Content-Type', 'image/webp'); self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'public, max-age=86400'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers(); self.wfile.write(raw); return
        user = self.current_user(); admin = bool(user.get('is_admin'))
        if path == '/api/auth/status': return self.send_json(200, {'is_admin': admin, 'unrestricted': UNRESTRICTED_MODE})
        if path == '/api/health':
            st = core.gemma.status()
            return self.send_json(200, {'status': 'healthy', 'service': 'VU CampusFix', 'version': '2.0.0', 'model': core.OLLAMA_MODEL, 'model_available': st['model_available'], 'ollama_connected': st['connected'], 'model_status': st['message'], 'local_first': True, 'unrestricted_access': UNRESTRICTED_MODE})
        if path == '/api/reports': return self.send_json(200, [public_report(r, admin) for r in get_all_rows()])
        if path == '/api/places': return self.send_json(200, get_places())
        if path == '/api/map': return self.send_json(200, map_payload())
        if path.startswith('/api/reports/'):
            rid = urllib.parse.unquote(path[len('/api/reports/'):]).strip('/')
            if not rid: return self.send_json(400, {'error': 'Missing report ID.'})
            r = get_one_report(rid)
            if not r: return self.send_json(404, {'error': 'Report not found.'})
            r['potential_duplicates'] = core.find_duplicates(r.get('user_description', ''), r.get('user_location', ''), (r.get('ai_analysis') or {}).get('category', 'Other'), [x for x in get_all_rows() if x['id'] != rid])
            return self.send_json(200, public_report(r, admin, True))
        if path == '/api/audit':
            if not self.require_admin(user): return
            conn = connect_db(); rows = conn.execute('SELECT * FROM audit_events ORDER BY id DESC LIMIT 150').fetchall(); conn.close()
            return self.send_json(200, [dict(r) for r in rows])
        return self.send_json(404, {'error': 'Not found.'})

    # ----- POST -----
    def _post(self):
        path = urllib.parse.urlparse(self.path).path; ip = self.client_ip()
        try: payload = self.read_payload()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e: return self.send_json(400, {'error': str(e)})
        if path == '/api/auth/logout': return self.send_json(200, {'ok': True}, self.session_cookie('', 0))
        if path == '/api/auth/admin':
            if UNRESTRICTED_MODE: return self.send_json(200, {'ok': True, 'is_admin': True})
            if LIMITER.count('login:' + ip, 300) >= LOGIN_MAX_FAILURES:
                return self.send_json(429, {'error': 'Too many wrong tokens. Wait a few minutes and try again.'})
            if token_matches(str(payload.get('token') or '')):
                audit('moderator', 'MODERATOR_SIGN_IN', '', 'ok')
                return self.send_json(200, {'ok': True, 'is_admin': True}, self.session_cookie(make_session()))
            LIMITER.add('login:' + ip); audit('public-visitor', 'MODERATOR_SIGN_IN_FAILED', '', ip)
            return self.send_json(401, {'error': 'Incorrect moderator token.'})
        user = self.current_user(); admin = bool(user.get('is_admin'))
        if path in ('/api/reports/preflight', '/api/reports/analyze'):
            if not admin and not LIMITER.allow('analyze:' + ip, ANALYZE_RATE_LIMIT, 600):
                return self.send_json(429, {'error': 'Too many analysis requests. Please wait a few minutes.'})
            desc = str(payload.get('description') or '')[:3000]; loc = str(payload.get('location') or '')[:300]
            try: image_data = validate_image_data(payload.get('image_base64'))
            except ValueError as e: return self.send_json(400, {'error': str(e)})
            if not desc.strip() and not image_data: return self.send_json(400, {'error': 'Provide a description or an image.'})
            if path == '/api/reports/preflight':
                analysis = core.fallback_analyze(desc, loc, bool(image_data)); source = 'rules-fallback'
                analysis['analysis_mode'] = 'Fast preliminary triage — awaiting Gemma' if desc.strip() else 'Fast preliminary triage — photo not visually inspected yet'
            else:
                analysis, source = core.gemma.analyze(desc, loc, image_data)
            dups = core.find_duplicates(desc, loc, (analysis or {}).get('category', 'Other'), get_all_rows())
            analysis = merge_integrity(analysis, desc, loc, image_data, dups)
            return self.send_json(200, {'ai_analysis': analysis, 'potential_duplicates': dups, 'source': source, 'image_received': bool(image_data), 'model_used': source == 'gemma-local', 'preflight': path.endswith('preflight')})
        if path == '/api/reports':
            if not admin and not LIMITER.allow('create:' + ip, REPORT_RATE_LIMIT, 600):
                return self.send_json(429, {'error': 'You have submitted several reports recently. Please wait a few minutes before sending more.'})
            desc = str(payload.get('user_description') or payload.get('description') or '')[:3000]; loc = str(payload.get('user_location') or payload.get('location') or '')[:300]
            try: image_data = validate_image_data(payload.get('image_data') or payload.get('image_base64'))
            except ValueError as e: return self.send_json(400, {'error': str(e)})
            analysis = payload.get('ai_analysis'); building_id = str(payload.get('building_id') or '')
            if not desc.strip() and not image_data: return self.send_json(400, {'error': 'Provide a description or an image.'})
            if not loc.strip(): return self.send_json(400, {'error': 'Campus location is required.'})
            if building_id not in {p['id'] for p in get_places()}: return self.send_json(400, {'error': 'Choose a valid campus zone.'})
            if not isinstance(analysis, dict): analysis, _source = core.gemma.analyze(desc, loc, image_data)
            try: analysis = core.normalize_analysis(analysis, desc, loc, bool(image_data))
            except Exception as e: return self.send_json(400, {'error': 'Invalid analysis object: ' + str(e)})
            dups = core.find_duplicates(desc, loc, analysis.get('category', 'Other'), get_all_rows())
            analysis = merge_integrity(analysis, desc, loc, image_data, dups)
            created = create_report(desc or 'No written description; see attached image.', loc, image_data, analysis, building_id)
            return self.send_json(201, public_report(created, admin))
        if path == '/api/places':
            if not self.require_admin(user): return
            name = str(payload.get('name') or '').strip()[:80]; kind = str(payload.get('kind') or 'Campus zone').strip()[:50]
            if len(name) < 2: return self.send_json(400, {'error': 'Enter a zone name with at least 2 characters.'})
            index = len(get_places()); x = 100 + (index % 4) * 220; y = 110 + (index // 4) * 145
            pid = 'zone-' + secrets.token_hex(4); now = stamp(); conn = connect_db()
            conn.execute('INSERT INTO map_places VALUES(?,?,?,?,?,?,?)', (pid, name, kind, x, y, now, now)); conn.commit(); conn.close()
            audit(user['email'], 'MAP_PLACE_CREATED', pid, name)
            return self.send_json(201, next(p for p in get_places() if p['id'] == pid))
        if path == '/api/places/reset':
            if not self.require_admin(user): return
            conn = connect_db(); conn.execute('DELETE FROM map_places'); now = stamp()
            for p in DEFAULT_PLACES: conn.execute('INSERT INTO map_places VALUES(?,?,?,?,?,?,?)', (p['id'], p['name'], p['kind'], p['x'], p['y'], now, now))
            conn.commit(); conn.close(); audit(user['email'], 'MAP_RESET', '', 'Default illustrative positions restored')
            return self.send_json(200, get_places())
        return self.send_json(404, {'error': 'Not found.'})

    # ----- PATCH -----
    def _patch(self):
        path = urllib.parse.urlparse(self.path).path; user = self.current_user()
        if not self.require_admin(user): return
        try: payload = self.read_payload()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e: return self.send_json(400, {'error': str(e)})
        if path.startswith('/api/reports/'):
            rid = urllib.parse.unquote(path[len('/api/reports/'):]).strip('/')
            try: updated = update_report(rid, payload, user)
            except ValueError as e: return self.send_json(400, {'error': str(e)})
            if not updated: return self.send_json(404, {'error': 'Report not found.'})
            return self.send_json(200, public_report(updated, True))
        if path.startswith('/api/places/'):
            pid = urllib.parse.unquote(path[len('/api/places/'):]).strip('/'); current = next((p for p in get_places() if p['id'] == pid), None)
            if not current: return self.send_json(404, {'error': 'Campus zone not found.'})
            name = str(payload.get('name', current['name'])).strip()[:80]; kind = str(payload.get('kind', current['kind'])).strip()[:50]
            if len(name) < 2 or not kind: return self.send_json(400, {'error': 'Zone name and type are required.'})
            try: x = max(20, min(980, float(payload.get('x', current['x'])))); y = max(20, min(600, float(payload.get('y', current['y']))))
            except (TypeError, ValueError): return self.send_json(400, {'error': 'Map coordinates must be numeric.'})
            conn = connect_db(); conn.execute('UPDATE map_places SET name=?,kind=?,x=?,y=?,updated_at=? WHERE id=?', (name, kind, x, y, stamp(), pid)); conn.commit(); conn.close()
            audit(user['email'], 'MAP_PLACE_UPDATED', pid, json.dumps({'name': name, 'kind': kind, 'x': x, 'y': y}))
            return self.send_json(200, next(p for p in get_places() if p['id'] == pid))
        return self.send_json(404, {'error': 'Not found.'})

    # ----- DELETE -----
    def _delete(self):
        path = urllib.parse.urlparse(self.path).path; user = self.current_user()
        if not self.require_admin(user): return
        if path.startswith('/api/reports/'):
            rid = urllib.parse.unquote(path[len('/api/reports/'):]).strip('/')
            if not get_one_report(rid): return self.send_json(404, {'error': 'Report not found.'})
            conn = connect_db(); conn.execute('DELETE FROM reports WHERE id=?', (rid,)); conn.commit(); conn.close()
            audit(user['email'], 'REPORT_DELETED', rid, 'Moderator delete')
            return self.send_json(200, {'message': 'Deleted', 'id': rid})
        if path.startswith('/api/places/'):
            pid = urllib.parse.unquote(path[len('/api/places/'):]).strip('/')
            if any(r.get('building_id') == pid for r in get_all_rows()): return self.send_json(409, {'error': 'This zone is referenced by reports. Rename it or move it instead of deleting it.'})
            conn = connect_db(); cur = conn.execute('DELETE FROM map_places WHERE id=?', (pid,)); conn.commit(); conn.close()
            if not cur.rowcount: return self.send_json(404, {'error': 'Campus zone not found.'})
            audit(user['email'], 'MAP_PLACE_DELETED', pid, ''); return self.send_json(200, {'ok': True, 'id': pid})
        return self.send_json(404, {'error': 'Not found.'})

def main():
    connect_db().close()
    seeded = seed_demo_data() if DEMO_DATA else 0
    host = os.environ.get('HOST', '127.0.0.1').strip() or '127.0.0.1'; preferred = env_int('PORT', 8011)
    candidates = [preferred] + [p for p in range(8000, 8011) if p != preferred]
    server = None; port = None; last_error = None
    for candidate in candidates:
        try: server = ThreadingHTTPServer((host, candidate), Handler); port = candidate; break
        except OSError as e: last_error = e
    if server is None:
        print(f'\nPorts 8000–8011 are all busy. Close any older CampusFix terminal windows and try again.\nDetails: {last_error}')
        if sys.stdin and sys.stdin.isatty(): input('Press Enter to close this window...')
        return
    url = f'http://localhost:{port}/'; st = core.gemma.status()
    print('\n' + '=' * 70 + '\n  VU CAMPUSFIX  |  CAMPUS CARE, MADE VISIBLE\n' + '=' * 70)
    print(f'  Website:          {url}')
    if host == '0.0.0.0':
        print('  LAN mode:         listening on all network interfaces; use only on a trusted network.')
        try: print(f'  LAN URL:          http://{socket.gethostbyname(socket.gethostname())}:{port}/')
        except OSError: print('  LAN URL:          run ipconfig, find your IPv4 address, then add the port above.')
    if port != preferred: print(f'  Note:             port {preferred} was busy, so port {port} is used.')
    if UNRESTRICTED_MODE:
        print('  ACCESS MODE:      UNRESTRICTED DEMO - everyone can edit and delete. Do not use for real reports.')
    else:
        print('  Access mode:      public can view + report; moderators need the token')
        if ADMIN_TOKEN_SOURCE == 'env': print('  Moderator token: set by ADMIN_TOKEN in .env')
        else: print(f'  Moderator token:  {ADMIN_TOKEN}   (saved in {DATA_DIR / ".admin_token"})')
    print(f'  AI model:         {core.OLLAMA_MODEL} at {core.OLLAMA_HOST}')
    print(f'  AI status:        {st["message"]}')
    print(f'  Database:         {DB_PATH}' + (f'  ({seeded} sample reports added)' if seeded else ''))
    print('  Keep this window open while using CampusFix. Press Ctrl+C to stop.\n' + '=' * 70 + '\n')
    if not env_flag('NO_BROWSER'): threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nVU CampusFix stopped.')
    finally: server.server_close()

if __name__ == '__main__': main()

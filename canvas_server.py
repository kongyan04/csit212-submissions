#!/usr/bin/env python3
"""
Canvas Final Project Submissions Server
- Fetches submission data from Canvas API every 30 minutes
- Serves a public read-only + comment page for students
- Comments are stored locally, posted to Canvas, and emailed to the student
- Run: python3 canvas_server.py
"""
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import json
import sqlite3
import smtplib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, jsonify, request, abort

# ── Configuration ─────────────────────────────────────────────────────────────

TOKEN        = '1925~QYBVrAy7KvKTHTCM2tG8ZTDQRx4fH2tDBvRE4YkzJxtCTzTyMecvP2YUz33E8ukC'
CANVAS_BASE  = 'https://montclair.instructure.com'
REFRESH_INTERVAL = 30 * 60  # 30 minutes

# Email settings — Montclair Office 365
SMTP_FROM    = 'kongy@montclair.edu'
SMTP_HOST    = 'smtp.office365.com'
SMTP_PORT    = 587
SMTP_PASS    = os.environ.get('MONTCLAIR_EMAIL_PASSWORD', 'QuFu1234!!')

ASSIGNMENTS = [
    {'label': 'Section 212615', 'course_id': '212615', 'assignment_id': '2606377'},
    {'label': 'Section 212604', 'course_id': '212604', 'assignment_id': '2606374'},
    {'label': 'Section 216874', 'course_id': '216874', 'assignment_id': '2624407'},
]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'comments.db')

# ── Shared cache ───────────────────────────────────────────────────────────────
cache = {'sections': [], 'last_updated': None, 'loading': False, 'error': None}
cache_lock = threading.Lock()

app = Flask(__name__)

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS comments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id    TEXT NOT NULL,
        assignment_id TEXT NOT NULL,
        student_id   TEXT NOT NULL,
        author_name  TEXT NOT NULL,
        author_email TEXT,
        body         TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )''')
    con.commit()
    con.close()

def db_add_comment(course_id, assignment_id, student_id, author_name, author_email, body):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        'INSERT INTO comments (course_id, assignment_id, student_id, author_name, author_email, body, created_at) VALUES (?,?,?,?,?,?,?)',
        (course_id, assignment_id, student_id, author_name, author_email, body,
         time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    )
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid

def db_get_comments(course_id, assignment_id, student_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT * FROM comments WHERE course_id=? AND assignment_id=? AND student_id=? ORDER BY created_at ASC',
        (course_id, assignment_id, student_id)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

def db_all_comments():
    """Return dict keyed by (course_id, assignment_id, student_id) → list of comments."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute('SELECT * FROM comments ORDER BY created_at ASC').fetchall()
    con.close()
    result = {}
    for r in rows:
        key = (r['course_id'], r['assignment_id'], str(r['student_id']))
        result.setdefault(key, []).append(dict(r))
    return result

# ── Canvas helpers ─────────────────────────────────────────────────────────────

def canvas_get(path):
    url = CANVAS_BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        link = resp.headers.get('Link', '')
        next_url = None
        for part in link.split(','):
            if 'rel="next"' in part:
                next_url = part.split('<')[1].split('>')[0].replace(CANVAS_BASE, '')
                break
        return data, next_url

def canvas_get_all(path):
    results, next_path = [], path
    while next_path:
        data, next_path = canvas_get(next_path)
        results.extend(data if isinstance(data, list) else [data])
    return results

def canvas_post_comment(course_id, assignment_id, student_id, text):
    """Post a comment to a Canvas submission."""
    url = f'{CANVAS_BASE}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}'
    body = urllib.parse.urlencode({'comment[text_body]': text}).encode()
    req = urllib.request.Request(url, data=body, method='PUT', headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, None
    except urllib.error.HTTPError as e:
        return False, f'Canvas error {e.code}: {e.read().decode()}'
    except Exception as e:
        return False, str(e)

# ── Email helper ───────────────────────────────────────────────────────────────

def send_email(to_addr, student_name, commenter_name, comment_body, project_url):
    if not SMTP_PASS:
        print(f'[email] Skipped (no SMTP_PASS set). Would email: {to_addr}')
        return False, 'Email not configured'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'New comment on your Final Project – CSIT 212'
        msg['From']    = f'Prof. Kong – CSIT 212 <{SMTP_FROM}>'
        msg['To']      = to_addr

        text = (f'Hi {student_name},\n\n'
                f'{commenter_name} left a comment on your Final Project:\n\n'
                f'"{comment_body}"\n\n'
                f'View all comments: {project_url}\n\n'
                f'— CSIT 212')

        html = f'''<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
  <div style="background:#007acc;padding:20px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:1.1rem">New comment on your Final Project</h2>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;padding:24px 28px;border-radius:0 0 8px 8px">
    <p style="color:#555;margin:0 0 16px">Hi <strong>{student_name}</strong>,</p>
    <p style="color:#555;margin:0 0 16px"><strong>{commenter_name}</strong> left a comment on your Final Project:</p>
    <blockquote style="border-left:4px solid #007acc;margin:0 0 20px;padding:12px 16px;background:#f0f8ff;border-radius:0 6px 6px 0;color:#333">
      {comment_body}
    </blockquote>
    <a href="{project_url}" style="display:inline-block;background:#007acc;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold">View all comments</a>
  </div>
</div>'''

        msg.attach(MIMEText(text, 'plain'))
        msg.attach(MIMEText(html,  'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_FROM, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        print(f'[email] Sent to {to_addr}')
        return True, None
    except Exception as e:
        print(f'[email] Failed: {e}')
        return False, str(e)

# ── Data fetcher ───────────────────────────────────────────────────────────────

def fetch_section(cfg):
    """Fetch one assignment's submissions. Returns section dict."""
    course_id, assignment_id = cfg['course_id'], cfg['assignment_id']
    try:
        assignment = canvas_get_all(f'/api/v1/courses/{course_id}/assignments/{assignment_id}')[0]
        # include[]=user gives name/login/email directly — no separate users call needed
        subs = canvas_get_all(
            f'/api/v1/courses/{course_id}/assignments/{assignment_id}'
            f'/submissions?per_page=100&include[]=user'
        )
        rows = []
        for sub in subs:
            student = sub.get('user') or {}
            uid     = str(sub.get('user_id', ''))
            links   = []
            if sub.get('url'):
                links.append(sub['url'])
            for att in sub.get('attachments') or []:
                if att.get('url'):
                    links.append(att['url'])
            login = student.get('login_id', '')
            email = student.get('email', '') or (login + '@mail.montclair.edu' if login else '')
            rows.append({
                'student_id':      uid,
                'name':            student.get('name', 'Unknown'),
                'login':           login,
                'email':           email,
                'links':           links,
                'submitted_at':    sub.get('submitted_at'),
                'score':           sub.get('score'),
                'points_possible': assignment.get('points_possible'),
                'workflow_state':  sub.get('workflow_state'),
                'late':            sub.get('late', False),
                'missing':         sub.get('missing', False),
            })
        rows.sort(key=lambda r: (0 if r['submitted_at'] else 1, r['name'].lower()))
        return {
            'label':         cfg['label'],
            'course_id':     course_id,
            'assignment_id': assignment_id,
            'assign_name':   assignment.get('name', ''),
            'due_at':        assignment.get('due_at'),
            'rows':          rows,
            'submitted':     sum(1 for r in rows if r['submitted_at']),
            'total':         len(rows),
        }
    except Exception as e:
        return {
            'label': cfg['label'], 'course_id': course_id,
            'assignment_id': assignment_id, 'assign_name': '',
            'due_at': None, 'rows': [], 'submitted': 0, 'total': 0,
            'fetch_error': str(e),
        }

def fetch_all_data():
    """Fetch all sections in parallel."""
    sections_map = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_section, cfg): cfg['label'] for cfg in ASSIGNMENTS}
        for fut in as_completed(futures):
            label = futures[fut]
            sections_map[label] = fut.result()
    # Return in original order
    return [sections_map[cfg['label']] for cfg in ASSIGNMENTS]

def do_fetch():
    """Fetch once, update cache, then schedule next fetch."""
    print('[canvas] Fetching data from Canvas…')
    with cache_lock:
        cache['loading'] = True; cache['error'] = None
    try:
        sections = fetch_all_data()
        with cache_lock:
            cache['sections']     = sections
            cache['last_updated'] = time.strftime('%B %d, %Y at %I:%M %p')
            cache['loading']      = False
        print(f'[canvas] Done — {sum(s["total"] for s in sections)} submissions loaded.')
    except Exception as e:
        with cache_lock:
            cache['error'] = str(e); cache['loading'] = False
        print(f'[canvas] Error: {e}')
    # Schedule next fetch
    t = threading.Timer(REFRESH_INTERVAL, do_fetch)
    t.daemon = True
    t.start()

def trigger_fetch_if_needed():
    """Start a fetch if cache is empty and not already loading."""
    with cache_lock:
        if cache['sections'] or cache['loading']:
            return
        cache['loading'] = True
    print('[canvas] On-demand fetch triggered.')
    threading.Thread(target=do_fetch, daemon=True).start()

def refresh_loop():
    do_fetch()

# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.response_class(HTML, mimetype='text/html')

@app.route('/data')
def data():
    trigger_fetch_if_needed()  # wake up if the background thread was killed during sleep
    with cache_lock:
        sections = json.loads(json.dumps(cache['sections']))  # deep copy
    # Attach comments
    all_comments = db_all_comments()
    for sec in sections:
        for row in sec.get('rows', []):
            key = (sec['course_id'], sec['assignment_id'], str(row['student_id']))
            row['comments'] = all_comments.get(key, [])
    with cache_lock:
        return jsonify({
            'sections':     sections,
            'last_updated': cache['last_updated'],
            'loading':      cache['loading'],
            'error':        cache['error'],
        })

@app.route('/comment', methods=['POST'])
def post_comment():
    d = request.get_json(force=True)
    course_id     = str(d.get('course_id', '')).strip()
    assignment_id = str(d.get('assignment_id', '')).strip()
    student_id    = str(d.get('student_id', '')).strip()
    author_name   = str(d.get('author_name', '')).strip()[:100]
    author_email  = str(d.get('author_email', '')).strip()[:200]
    body          = str(d.get('body', '')).strip()[:2000]

    if not all([course_id, assignment_id, student_id, author_name, body]):
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400

    # Validate against known assignments
    known = {(a['course_id'], a['assignment_id']) for a in ASSIGNMENTS}
    if (course_id, assignment_id) not in known:
        return jsonify({'ok': False, 'error': 'Unknown assignment'}), 403

    # Find student info from cache
    student_name  = 'Student'
    student_email = ''
    with cache_lock:
        for sec in cache['sections']:
            if sec['course_id'] == course_id and sec['assignment_id'] == assignment_id:
                for row in sec['rows']:
                    if str(row['student_id']) == student_id:
                        student_name  = row['name']
                        student_email = row['email']
                        break

    # 1. Save locally
    db_add_comment(course_id, assignment_id, student_id, author_name, author_email, body)

    # 2. Post to Canvas
    canvas_text = f'[Peer comment from {author_name}]: {body}'
    canvas_ok, canvas_err = canvas_post_comment(course_id, assignment_id, student_id, canvas_text)
    if not canvas_ok:
        print(f'[canvas comment] {canvas_err}')

    # 3. Send email to student
    if student_email:
        base_url = request.host_url.rstrip('/')
        send_email(student_email, student_name, author_name, body, base_url)

    return jsonify({'ok': True, 'canvas_ok': canvas_ok})

# ── HTML template ──────────────────────────────────────────────────────────────

HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Final Project Submissions – CSIT 212</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f5f7fa; color: #333; font-family: "Segoe UI", Arial, sans-serif; min-height: 100vh; }

  header { background: #007acc; padding: 28px 40px 22px; text-align: center; }
  header h1 { font-size: 1.9rem; color: #fff; font-weight: 800; }
  header p  { color: #cce7ff; font-size: 0.9rem; margin-top: 5px; }

  .meta-bar { background: #e8f4fd; border-bottom: 1px solid #bee3f8; padding: 8px 32px;
    font-size: 0.8rem; color: #2b6cb0; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #48bb78; display: inline-block; }
  .dot.loading { background: #ed8936; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .countdown { margin-left: auto; }

  main { max-width: 1200px; margin: 0 auto; padding: 28px 24px 60px; }

  .section-header { display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px; margin: 32px 0 10px; }
  .section-title { font-size: 1.05rem; font-weight: 700; color: #007acc;
    border-left: 4px solid #007acc; padding-left: 12px; }
  .section-meta { font-size: 0.82rem; color: #888; }

  .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  .toolbar input, .toolbar select { padding: 7px 12px; border: 1px solid #ccc;
    border-radius: 8px; font-size: 0.875rem; background: #fff; outline: none; font-family: inherit; }
  .toolbar input { width: 220px; }
  .toolbar input:focus, .toolbar select:focus { border-color: #007acc; }
  .count-badge { margin-left: auto; font-size: 0.8rem; color: #999; }

  .table-wrap { background: #fff; border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.07); overflow: hidden; margin-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; }
  thead th { background: #007acc; color: #fff; padding: 11px 14px; text-align: left;
    font-size: 0.77rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
    cursor: pointer; user-select: none; white-space: nowrap; }
  thead th:hover { background: #005fa3; }
  thead th .si { margin-left: 4px; opacity: 0.45; }
  thead th.sorted .si { opacity: 1; }
  tbody tr { border-bottom: 1px solid #f0f0f0; transition: background 0.1s; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: #f8fbff; }
  tbody td { padding: 10px 14px; font-size: 0.875rem; vertical-align: top; }

  .student-name  { font-weight: 600; color: #222; }
  .student-login { font-size: 0.77rem; color: #aaa; }

  .link-cell a { color: #007acc; text-decoration: none; font-size: 0.82rem;
    word-break: break-all; display: block; margin-bottom: 2px; }
  .link-cell a:hover { text-decoration: underline; }
  .no-link { color: #ccc; font-style: italic; font-size: 0.82rem; }

  .date-cell { font-size: 0.8rem; color: #777; white-space: nowrap; }
  .score-cell { font-weight: 700; }

  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.72rem; font-weight: 700; }
  .pill-green { background: #c6f6d5; color: #276749; }
  .pill-blue  { background: #bee3f8; color: #2a69ac; }
  .pill-red   { background: #fed7d7; color: #9b2c2c; }
  .pill-gray  { background: #eee;    color: #777; }

  /* Comment cell */
  .comment-cell { min-width: 180px; }
  .comment-list { margin-bottom: 8px; }
  .comment-item { background: #f0f8ff; border-left: 3px solid #bee3f8; border-radius: 0 6px 6px 0;
    padding: 7px 10px; margin-bottom: 6px; font-size: 0.8rem; }
  .comment-author { font-weight: 700; color: #2b6cb0; }
  .comment-time   { font-size: 0.72rem; color: #aaa; margin-left: 6px; }
  .comment-body   { color: #444; margin-top: 3px; line-height: 1.4; }

  .add-comment-btn { background: none; border: 1px dashed #007acc; color: #007acc;
    border-radius: 6px; padding: 4px 10px; font-size: 0.78rem; cursor: pointer;
    transition: all 0.15s; white-space: nowrap; }
  .add-comment-btn:hover { background: #007acc; color: #fff; }

  /* Modal */
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    display: flex; align-items: center; justify-content: center; z-index: 1000;
    opacity: 0; pointer-events: none; transition: opacity 0.15s; }
  .modal-backdrop.open { opacity: 1; pointer-events: all; }
  .modal { background: #fff; border-radius: 14px; box-shadow: 0 8px 40px rgba(0,0,0,0.18);
    width: 100%; max-width: 460px; padding: 28px; transform: translateY(12px);
    transition: transform 0.15s; }
  .modal-backdrop.open .modal { transform: translateY(0); }
  .modal h3 { font-size: 1rem; font-weight: 700; color: #007acc; margin-bottom: 6px; }
  .modal .target-name { font-size: 0.82rem; color: #888; margin-bottom: 18px; }
  .modal label { font-size: 0.78rem; font-weight: 700; color: #555;
    text-transform: uppercase; letter-spacing: 0.07em; display: block; margin-bottom: 4px; margin-top: 14px; }
  .modal input, .modal textarea {
    width: 100%; padding: 9px 12px; border: 1px solid #ccc; border-radius: 8px;
    font-size: 0.9rem; font-family: inherit; outline: none; resize: vertical; }
  .modal input:focus, .modal textarea:focus { border-color: #007acc; box-shadow: 0 0 0 2px rgba(0,122,204,0.15); }
  .modal textarea { min-height: 100px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  .btn { padding: 9px 22px; border: none; border-radius: 8px; font-size: 0.875rem;
    font-weight: 700; cursor: pointer; transition: background 0.15s; }
  .btn-primary { background: #007acc; color: #fff; }
  .btn-primary:hover { background: #005fa3; }
  .btn-primary:disabled { background: #a0c8e8; cursor: not-allowed; }
  .btn-secondary { background: #eee; color: #555; }
  .btn-secondary:hover { background: #ddd; }

  .modal-error { color: #c53030; font-size: 0.82rem; margin-top: 10px; }
  .modal-success { color: #276749; font-size: 0.82rem; margin-top: 10px; background: #c6f6d5;
    border-radius: 6px; padding: 8px 12px; }

  .empty-state { text-align: center; padding: 48px 20px; color: #bbb; font-size: 0.9rem; }
  .loading-state { text-align: center; padding: 60px 20px; color: #999; }
  .spinner { width: 36px; height: 36px; border: 4px solid #e2e8f0; border-top-color: #007acc;
    border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 14px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>Final Project Submissions</h1>
  <p>CSIT 212 &nbsp;&middot;&nbsp; Spring 2026</p>
</header>

<div class="meta-bar">
  <span><span class="dot" id="statusDot"></span>&nbsp;<span id="statusText">Loading…</span></span>
  <span id="lastUpdated"></span>
  <span class="countdown" id="countdown"></span>
</div>

<main id="main">
  <div class="loading-state"><div class="spinner"></div>Fetching submissions…</div>
</main>

<!-- Comment Modal -->
<div class="modal-backdrop" id="modalBackdrop" onclick="closeModal(event)">
  <div class="modal">
    <h3>Leave a Comment</h3>
    <div class="target-name" id="modalTargetName"></div>
    <label>Your Name *</label>
    <input type="text" id="modalAuthorName" placeholder="Enter your name">
    <label>Your Email (optional — not shown publicly)</label>
    <input type="email" id="modalAuthorEmail" placeholder="your@email.com">
    <label>Comment *</label>
    <textarea id="modalBody" placeholder="Write your feedback or comment…"></textarea>
    <div class="modal-error"   id="modalError"  style="display:none"></div>
    <div class="modal-success" id="modalSuccess" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary"   id="modalSubmitBtn" onclick="submitComment()">Post Comment</button>
    </div>
  </div>
</div>

<script>
let allData   = null;
let sortState = {};
let modalCtx  = null;   // { courseId, assignmentId, studentId, studentName }

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadData() {
  try {
    const res  = await fetch('/data');
    const json = await res.json();

    if (json.loading && !json.sections.length) {
      // Server is still fetching from Canvas — show spinner and poll again
      document.getElementById('statusDot').className = 'dot loading';
      document.getElementById('statusText').textContent = 'Loading submissions from Canvas…';
      document.getElementById('main').innerHTML =
        '<div class="loading-state"><div class="spinner"></div>Fetching submissions from Canvas, please wait…</div>';
      setTimeout(loadData, 5000);
      return;
    }

    document.getElementById('statusDot').className = 'dot';
    document.getElementById('statusText').textContent = 'Live';
    document.getElementById('lastUpdated').textContent =
      json.last_updated ? 'Updated ' + json.last_updated : '';
    allData = json.sections;
    renderAll();
    clearTimeout(window._refreshTimer);
    window._refreshTimer = setTimeout(loadData, 30 * 60 * 1000);
    startCountdown(30 * 60);
  } catch(e) {
    document.getElementById('statusText').textContent = 'Error: ' + e.message;
    document.getElementById('statusDot').className = 'dot loading';
    setTimeout(loadData, 15000);
  }
}

function startCountdown(seconds) {
  clearInterval(window._cdTimer);
  let s = seconds;
  const el = document.getElementById('countdown');
  window._cdTimer = setInterval(() => {
    if (s <= 0) { clearInterval(window._cdTimer); return; }
    const m = Math.floor(s/60), sec = s%60;
    el.textContent = `Refreshes in ${m}:${String(sec).padStart(2,'0')}`;
    s--;
  }, 1000);
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderAll() {
  console.log('renderAll: allData length =', (allData||[]).length);
  const main = document.getElementById('main');
  main.innerHTML = '';
  (allData || []).forEach((s, i) => {
    console.log('rendering section', i, s.label);
    try { renderSection(s); } catch(e) { console.error('renderSection error:', e); main.innerHTML += '<p style="color:red">Section error: ' + e.message + '</p>'; }
  });
}

function renderSection(section) {
  const main = document.getElementById('main');
  if (!sortState[section.label]) sortState[section.label] = { col: -1, asc: true };
  const id  = 'sec-' + section.label.replace(/\s+/g,'_');
  const due = section.due_at
    ? 'Due ' + new Date(section.due_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';

  const div = document.createElement('div');
  div.id = id;

  if (section.fetch_error) {
    div.innerHTML = `<div class="section-header"><div class="section-title">${esc(section.label)}</div></div>
      <div style="color:#c53030;background:#fff5f5;border-radius:8px;padding:12px 16px;font-size:.875rem">
        Could not load: ${esc(section.fetch_error)}</div>`;
    main.appendChild(div); return;
  }

  div.innerHTML = `
    <div class="section-header">
      <div class="section-title">${esc(section.label)} &nbsp;&ndash;&nbsp; ${esc(section.assign_name)}</div>
      <div class="section-meta">${section.submitted} submitted &nbsp;/&nbsp; ${section.total} students${due ? ' &nbsp; ' + due : ''}</div>
    </div>
    <div class="toolbar">
      <input type="text" id="search-${id}" placeholder="Search student…" oninput="filter('${id}','${section.label}')">
      <select id="status-${id}" onchange="filter('${id}','${section.label}')">
        <option value="">All Statuses</option>
        <option value="submitted">Has link</option>
        <option value="unsubmitted">No submission</option>
        <option value="graded">Graded</option>
        <option value="late">Late</option>
      </select>
      <span class="count-badge" id="count-${id}"></span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th onclick="sortSec('${id}','${section.label}',0)">Student <span class="si">↕</span></th>
        <th>Submission Link</th>
        <th onclick="sortSec('${id}','${section.label}',2)">Submitted <span class="si">↕</span></th>
        <th onclick="sortSec('${id}','${section.label}',3)">Score <span class="si">↕</span></th>
        <th>Status</th>
        <th>Comments</th>
      </tr></thead>
      <tbody id="tbody-${id}"></tbody>
    </table></div>`;
  main.appendChild(div);
  filter(id, section.label);
}

function getSection(label) { return (allData || []).find(s => s.label === label); }

function filter(id, label) {
  const sec    = getSection(label); if (!sec) return;
  const search = (document.getElementById('search-' + id)?.value || '').toLowerCase();
  const status = document.getElementById('status-' + id)?.value || '';

  let rows = sec.rows.filter(r => {
    if (search && !r.name.toLowerCase().includes(search) &&
                  !r.login.toLowerCase().includes(search)) return false;
    if (status === 'submitted'   && !r.submitted_at) return false;
    if (status === 'unsubmitted' && r.workflow_state !== 'unsubmitted') return false;
    if (status === 'graded'      && r.workflow_state !== 'graded') return false;
    if (status === 'late'        && !r.late) return false;
    return true;
  });

  const st = sortState[label];
  if (st && st.col >= 0) rows = doSort(rows, st.col, st.asc);

  document.getElementById('count-' + id).textContent =
    rows.length + ' student' + (rows.length !== 1 ? 's' : '');
  renderRows('tbody-' + id, rows, sec);
}

function sortSec(id, label, col) {
  const st = sortState[label];
  if (st.col === col) st.asc = !st.asc; else { st.col = col; st.asc = true; }
  document.querySelectorAll('#' + id + ' thead th').forEach((th,i) => {
    th.classList.remove('sorted');
    const si = th.querySelector('.si'); if (si) si.textContent = '↕';
  });
  const ths = document.querySelectorAll('#' + id + ' thead th');
  if (ths[col]) { ths[col].classList.add('sorted'); const si = ths[col].querySelector('.si'); if (si) si.textContent = st.asc ? '↑' : '↓'; }
  filter(id, label);
}

function doSort(rows, col, asc) {
  return [...rows].sort((a,b) => {
    let va, vb;
    if      (col===0) { va=a.name; vb=b.name; }
    else if (col===2) { va=a.submitted_at||''; vb=b.submitted_at||''; }
    else if (col===3) { va=a.score??-1; vb=b.score??-1; }
    else return 0;
    return va<vb ? (asc?-1:1) : va>vb ? (asc?1:-1) : 0;
  });
}

function renderRows(tbodyId, rows, section) {
  const tbody = document.getElementById(tbodyId); if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">No students match.</div></td></tr>'; return;
  }
  tbody.innerHTML = rows.map(r => {
    const linkHtml = r.links.length
      ? r.links.map(l => `<a href="${ea(l)}" target="_blank" rel="noopener">${esc(l)}</a>`).join('')
      : '<span class="no-link">—</span>';

    const dateStr = r.submitted_at
      ? new Date(r.submitted_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '—';
    const scoreStr = r.score != null
      ? r.score + (r.points_possible ? ' / ' + r.points_possible : '') : '—';

    let pill = '';
    if      (r.missing)                       pill='<span class="pill pill-red">Missing</span>';
    else if (r.late)                          pill='<span class="pill pill-red">Late</span>';
    else if (r.workflow_state==='graded')     pill='<span class="pill pill-green">Graded</span>';
    else if (r.workflow_state==='submitted')  pill='<span class="pill pill-blue">Submitted</span>';
    else                                      pill='<span class="pill pill-gray">Not Submitted</span>';

    const comments   = r.comments || [];
    const commentHtml = comments.map(c => `
      <div class="comment-item">
        <span class="comment-author">${esc(c.author_name)}</span>
        <span class="comment-time">${fmtDate(c.created_at)}</span>
        <div class="comment-body">${esc(c.body)}</div>
      </div>`).join('');

    const btnLabel = comments.length
      ? `💬 ${comments.length} comment${comments.length>1?'s':''}`
      : '+ Add comment';

    return `<tr>
      <td><div class="student-name">${esc(r.name)}</div><div class="student-login">${esc(r.login)}</div></td>
      <td class="link-cell">${linkHtml}</td>
      <td class="date-cell">${dateStr}</td>
      <td class="score-cell">${scoreStr}</td>
      <td>${pill}</td>
      <td class="comment-cell">
        <div class="comment-list">${commentHtml}</div>
        <button class="add-comment-btn" onclick="openModal('${esc(section.course_id)}','${esc(section.assignment_id)}','${esc(r.student_id)}','${esc(r.name)}')">${btnLabel}</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Modal ─────────────────────────────────────────────────────────────────────

function openModal(courseId, assignmentId, studentId, studentName) {
  modalCtx = { courseId, assignmentId, studentId, studentName };
  document.getElementById('modalTargetName').textContent = "On: " + studentName + "'s project";
  document.getElementById('modalAuthorName').value  = '';
  document.getElementById('modalAuthorEmail').value = '';
  document.getElementById('modalBody').value        = '';
  document.getElementById('modalError').style.display   = 'none';
  document.getElementById('modalSuccess').style.display = 'none';
  document.getElementById('modalSubmitBtn').disabled = false;
  document.getElementById('modalBackdrop').classList.add('open');
  setTimeout(() => document.getElementById('modalAuthorName').focus(), 50);
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modalBackdrop')) return;
  document.getElementById('modalBackdrop').classList.remove('open');
}

async function submitComment() {
  if (!modalCtx) return;
  const authorName  = document.getElementById('modalAuthorName').value.trim();
  const authorEmail = document.getElementById('modalAuthorEmail').value.trim();
  const body        = document.getElementById('modalBody').value.trim();
  const errEl       = document.getElementById('modalError');
  const okEl        = document.getElementById('modalSuccess');

  errEl.style.display = 'none';
  okEl.style.display  = 'none';

  if (!authorName) { errEl.textContent = 'Please enter your name.'; errEl.style.display='block'; return; }
  if (!body)       { errEl.textContent = 'Please write a comment.';  errEl.style.display='block'; return; }

  const btn = document.getElementById('modalSubmitBtn');
  btn.disabled = true; btn.textContent = 'Posting…';

  try {
    const res = await fetch('/comment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        course_id: modalCtx.courseId, assignment_id: modalCtx.assignmentId,
        student_id: modalCtx.studentId, author_name: authorName,
        author_email: authorEmail, body,
      })
    });
    const json = await res.json();
    if (json.ok) {
      okEl.textContent = '✓ Comment posted' + (json.canvas_ok ? ' and added to Canvas.' : '.');
      okEl.style.display = 'block';
      btn.textContent = 'Posted!';
      // Refresh data to show new comment
      setTimeout(() => {
        document.getElementById('modalBackdrop').classList.remove('open');
        loadData();
      }, 1500);
    } else {
      errEl.textContent = json.error || 'Failed to post.';
      errEl.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Post Comment';
    }
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
    errEl.style.display = 'block';
    btn.disabled = false; btn.textContent = 'Post Comment';
  }
}

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('modalBackdrop').classList.remove('open');
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US',{month:'short',day:'numeric'});
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function ea(s) { return esc(s); }

loadData();
</script>
</body>
</html>'''


# Always init DB and start background thread (works with both gunicorn and direct run)
init_db()
threading.Thread(target=refresh_loop, daemon=True).start()

if __name__ == '__main__':
    print('\n  Canvas Final Project Server')
    print('  Local:  http://localhost:5050\n')
    app.run(host='0.0.0.0', port=5050, debug=False)

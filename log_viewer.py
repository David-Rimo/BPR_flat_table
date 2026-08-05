"""Flask web app to view the BPR cron sync logs.

Run:  python3 log_viewer.py
Then open http://<server-ip>:5000/
"""

import html
import os
import re
from datetime import datetime

from flask import Flask, redirect, request, session, url_for

LOG_FILE = '/home/vantage/bpr_sync.log'

USERNAME = 'bpr_admin'
PASSWORD = 'vc@bpr2026'

app = Flask(__name__)
app.secret_key = 'bpr_log_viewer_2026'

SEPARATOR = '=' * 50

# Cron/logger prefixes we accept for "last sync time"
TIMESTAMP_RE = re.compile(
    r'\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\]?'
)


# ---------------------------------------------------------------------------
# Log reading / parsing
# ---------------------------------------------------------------------------

def read_log():
    """Return (content, error_message). error_message is None when all is well."""
    if not os.path.exists(LOG_FILE):
        return '', ("Log file not found at %s — the sync has probably not run "
                    "on this machine yet." % LOG_FILE)
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()
    except Exception as exc:
        return '', 'Could not read %s: %s' % (LOG_FILE, exc)
    if not content.strip():
        return '', 'Log file %s is empty.' % LOG_FILE
    return content, None


def log_mtime():
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(LOG_FILE)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def last_timestamp(text):
    """Last timestamp appearing in the given text, or None."""
    found = TIMESTAMP_RE.findall(text)
    return found[-1].replace('T', ' ') if found else None


def isolate_last_run(content):
    """Return only the portion of the log belonging to the most recent run.

    A run starts with the "Found N companies to process" line printed once per
    run.  If that marker is missing we fall back to the text after the
    second-to-last "Incremental sync complete." line, and finally to the whole
    log.
    """
    starts = [m.start() for m in re.finditer(r'companies to process', content)]
    if starts:
        line_start = content.rfind('\n', 0, starts[-1]) + 1
        return content[line_start:]

    completes = [m.end() for m in re.finditer(r'sync complete\.', content)]
    if len(completes) >= 2:
        return content[completes[-2]:]
    return content


def _count_from(block, key):
    """Extract a count for `key` from a company block.

    Handles both the per-company Summary lines:
        new_users_inserted: 12 pairs affected
        ❌ gift_voucher: FAILED
    and the inline progress lines:
        gift_voucher: 12 user-country pairs affected
    """
    if re.search(r'❌\s*%s\s*:\s*FAILED' % re.escape(key), block):
        return 'ERROR'
    if re.search(r'%s\s*:\s*ERROR' % re.escape(key), block):
        return 'ERROR'
    matches = re.findall(
        r'%s\s*:\s*(\d+)\s+(?:user-country\s+)?pairs affected' % re.escape(key),
        block)
    if matches:
        return int(matches[-1])
    return None


def _new_users(block):
    value = _count_from(block, 'new_users_inserted')
    if value is not None:
        return value
    if re.search(r'❌\s*ERROR detecting new users', block):
        return 'ERROR'
    match = re.findall(r'inserted\s+(\d+)\s+new user-country rows', block)
    if match:
        return int(match[-1])
    if 'No new users detected' in block:
        return 0
    return None


def _current_balance(block):
    if re.search(r'❌\s*ERROR refreshing current_balance', block):
        return 'ERROR'
    match = re.findall(
        r'Refreshed current_balance_points for\s+(-?\d+)\s+rows', block)
    if match:
        return int(match[-1])
    return None


def parse_companies(content):
    """Parse the most recent run into a list of per-company dicts."""
    run = isolate_last_run(content)
    chunks = run.split(SEPARATOR)

    companies = []
    for index, chunk in enumerate(chunks):
        match = re.search(r'Processing company_id:\s*(\d+)', chunk)
        if not match:
            continue
        company_id = match.group(1)
        # The counts live in the chunk *after* the "Processing company_id"
        # header (the header sits between two separator lines).
        body = chunk[match.end():]
        if index + 1 < len(chunks):
            body += chunks[index + 1]

        companies.append({
            'company_id': company_id,
            'last_sync': last_timestamp(body) or last_timestamp(chunk),
            'new_users': _new_users(body),
            'employee_points': _count_from(body, 'employee_points_id'),
            'locked_points': _count_from(body, 'locked_points_transactions'),
            'gift_voucher': _count_from(body, 'gift_voucher'),
            'current_balance': _current_balance(body),
            'has_error': '❌' in body,
        })

    # Keep only the newest block per company, preserving first-seen order.
    deduped = {}
    for company in companies:
        deduped[company['company_id']] = company
    ordered = list(deduped.values())
    ordered.reverse()  # most recent sync at top
    return ordered


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               Helvetica, Arial, sans-serif;
  background: #f4f6f9;
  color: #212529;
}
header {
  background: #1F4E79;
  color: #ffffff;
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
header h1 { font-size: 19px; margin: 0; font-weight: 600; letter-spacing: .3px; }
header .right { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.countdown { font-size: 13px; opacity: .9; }
.btn {
  background: #ffffff; color: #1F4E79; border: none; border-radius: 4px;
  padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
  text-decoration: none; display: inline-block;
}
.btn:hover { background: #e9eef4; }
.btn-outline {
  background: transparent; color: #ffffff; border: 1px solid rgba(255,255,255,.6);
}
.btn-outline:hover { background: rgba(255,255,255,.15); }
.wrap { padding: 20px 24px 40px; max-width: 1400px; margin: 0 auto; }
.tabs { display: flex; gap: 4px; border-bottom: 2px solid #dfe4ea; margin-bottom: 20px; }
.tab {
  padding: 10px 22px; cursor: pointer; font-size: 14px; font-weight: 600;
  color: #56606b; border: none; background: transparent;
  border-bottom: 3px solid transparent; margin-bottom: -2px;
}
.tab.active { color: #1F4E79; border-bottom-color: #1F4E79; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 20px; }
.card {
  background: #ffffff; border: 1px solid #e3e8ee; border-radius: 6px;
  padding: 14px 18px; min-width: 190px; flex: 1;
}
.card .label {
  font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
  color: #6c757d; margin-bottom: 6px;
}
.card .value { font-size: 20px; font-weight: 700; color: #1F4E79; }
.card .value.error { color: #dc3545; }
.card .value.ok { color: #28a745; }
.card .value.small { font-size: 16px; }
.table-scroll { overflow-x: auto; background: #ffffff;
                border: 1px solid #e3e8ee; border-radius: 6px; }
table { border-collapse: collapse; width: 100%; min-width: 900px; }
th {
  background: #1F4E79; color: #ffffff; text-align: left; padding: 11px 14px;
  font-size: 12px; text-transform: uppercase; letter-spacing: .5px;
  font-weight: 600; white-space: nowrap;
}
td { padding: 11px 14px; font-size: 14px; border-top: 1px solid #eef1f5; white-space: nowrap; }
tbody tr:nth-child(odd) { background: #ffffff; }
tbody tr:nth-child(even) { background: #f7f9fc; }
tbody tr:hover { background: #eef4fa; }
td.num { font-variant-numeric: tabular-nums; }
.muted { color: #9aa4ae; }
.badge-ok { color: #28a745; font-weight: 600; }
.badge-err { color: #dc3545; font-weight: 600; }
.notice {
  background: #fff8e1; border: 1px solid #ffc107; color: #7a5c00;
  padding: 16px 18px; border-radius: 6px; font-size: 14px;
}
#rawbox {
  background: #ffffff; border: 1px solid #e3e8ee; border-radius: 6px;
  height: 65vh; overflow: auto; padding: 12px 14px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
}
#rawbox .l { display: block; padding: 1px 6px; border-radius: 3px; }
#rawbox .l-err { background: #fdecec; color: #a71d2a; }
#rawbox .l-ok { background: #e8f6ec; color: #1c7430; }
#rawbox .l-co { background: #e8f0f8; color: #1F4E79; font-weight: 700; }
#rawbox .l-sum { background: #fff6d9; color: #7a5c00; }
.raw-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
.btn-dark { background: #1F4E79; color: #ffffff; }
.btn-dark:hover { background: #17395a; }
.login-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: #1F4E79;
}
.login-card {
  background: #ffffff; padding: 34px 32px; border-radius: 8px; width: 340px;
  box-shadow: 0 12px 34px rgba(0,0,0,.28);
}
.login-card h2 { margin: 0 0 4px; color: #1F4E79; font-size: 20px; }
.login-card p.sub { margin: 0 0 22px; color: #6c757d; font-size: 13px; }
.login-card label {
  display: block; font-size: 12px; font-weight: 600; color: #495057;
  margin-bottom: 6px; text-transform: uppercase; letter-spacing: .5px;
}
.login-card input {
  width: 100%; padding: 10px 12px; margin-bottom: 16px; font-size: 14px;
  border: 1px solid #ced4da; border-radius: 4px;
}
.login-card input:focus { outline: none; border-color: #1F4E79; }
.login-card button {
  width: 100%; padding: 11px; background: #1F4E79; color: #ffffff; border: none;
  border-radius: 4px; font-size: 14px; font-weight: 600; cursor: pointer;
}
.login-card button:hover { background: #17395a; }
.login-err {
  background: #fdecec; color: #dc3545; border: 1px solid #f5c2c7;
  padding: 9px 12px; border-radius: 4px; font-size: 13px; margin-bottom: 16px;
}
@media (max-width: 640px) {
  .wrap { padding: 16px 12px 32px; }
  header { padding: 14px 14px; }
  header h1 { font-size: 16px; }
}
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BPR Log Viewer — Login</title>
<style>%(css)s</style>
</head>
<body>
<div class="login-wrap">
  <form class="login-card" method="post" action="/login">
    <h2>BPR Log Viewer</h2>
    <p class="sub">Cron sync monitoring</p>
    %(error)s
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>
"""

MAIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BPR Sync Logs</title>
<style>%(css)s</style>
</head>
<body>
<header>
  <h1>BPR Cron Sync — Log Viewer</h1>
  <div class="right">
    <span class="countdown">Refreshing in <b id="countdown">30</b> seconds</span>
    <button class="btn" onclick="doRefresh()">Refresh now</button>
    <a class="btn btn-outline" href="/logout">Logout</a>
  </div>
</header>

<div class="wrap">
  <div class="tabs">
    <button class="tab active" id="tab-dash" onclick="showTab('dash')">Dashboard</button>
    <button class="tab" id="tab-raw" onclick="showTab('raw')">Raw Log</button>
  </div>

  <div id="panel-dash">%(dashboard)s</div>

  <div id="panel-raw" style="display:none">
    <div class="raw-bar">
      <button class="btn btn-dark" onclick="scrollRaw('top')">Scroll to top</button>
      <button class="btn btn-dark" onclick="scrollRaw('bottom')">Scroll to bottom</button>
      <span class="muted" style="font-size:13px">%(logpath)s</span>
    </div>
    <div id="rawbox">%(raw)s</div>
  </div>
</div>

<script>
function showTab(name) {
  var dash = name === 'dash';
  document.getElementById('panel-dash').style.display = dash ? 'block' : 'none';
  document.getElementById('panel-raw').style.display = dash ? 'none' : 'block';
  document.getElementById('tab-dash').className = dash ? 'tab active' : 'tab';
  document.getElementById('tab-raw').className = dash ? 'tab' : 'tab active';
  location.hash = dash ? '' : 'raw';
  if (!dash) { scrollRaw('bottom'); }
}

function scrollRaw(where) {
  var box = document.getElementById('rawbox');
  if (!box) { return; }
  box.scrollTop = (where === 'top') ? 0 : box.scrollHeight;
}

function doRefresh() { window.location.reload(); }

var remaining = 30;
setInterval(function () {
  remaining -= 1;
  if (remaining <= 0) { doRefresh(); return; }
  document.getElementById('countdown').textContent = remaining;
}, 1000);

if (location.hash === '#raw') { showTab('raw'); } else { scrollRaw('bottom'); }
</script>
</body>
</html>
"""


def fmt_count(value):
    if value == 'ERROR':
        return '<span class="badge-err">ERROR</span>'
    if value is None:
        return '<span class="muted">&mdash;</span>'
    return '{:,}'.format(value)


def render_dashboard(content, error):
    if error:
        return '<div class="notice">%s</div>' % html.escape(error)

    companies = parse_companies(content)
    overall = last_timestamp(content) or log_mtime() or 'unknown'
    total_errors = sum(1 for c in companies if c['has_error'])

    error_class = 'error' if total_errors else 'ok'
    cards = """
    <div class="cards">
      <div class="card"><div class="label">Last sync</div>
        <div class="value small">%s</div></div>
      <div class="card"><div class="label">Companies processed</div>
        <div class="value">%d</div></div>
      <div class="card"><div class="label">Companies with errors</div>
        <div class="value %s">%d</div></div>
    </div>
    """ % (html.escape(str(overall)), len(companies), error_class, total_errors)

    if not companies:
        return cards + ('<div class="notice">No company blocks found in the log '
                        'yet. The sync may still be starting up.</div>')

    rows = []
    for c in companies:
        status = ('<span class="badge-err">&#10060; Error</span>'
                  if c['has_error'] else
                  '<span class="badge-ok">&#9989; Success</span>')
        sync_time = c['last_sync'] or overall
        rows.append(
            '<tr><td><b>%s</b></td><td>%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>' % (
                html.escape(c['company_id']),
                html.escape(str(sync_time)),
                fmt_count(c['new_users']),
                fmt_count(c['employee_points']),
                fmt_count(c['locked_points']),
                fmt_count(c['gift_voucher']),
                fmt_count(c['current_balance']),
                status,
            ))

    table = """
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>Company ID</th><th>Last Sync Time</th><th>New Users Inserted</th>
          <th>Employee Points Updated</th><th>Locked Points Updated</th>
          <th>Gift Voucher Updated</th><th>Current Balance Refreshed</th>
          <th>Status</th>
        </tr></thead>
        <tbody>%s</tbody>
      </table>
    </div>
    """ % ''.join(rows)

    return cards + table


def render_raw(content, error):
    if error:
        return html.escape(error)

    out = []
    for line in content.splitlines():
        safe = html.escape(line) or '&nbsp;'
        if '❌' in line:
            cls = 'l l-err'
        elif '✅' in line:
            cls = 'l l-ok'
        elif 'Processing company_id' in line:
            cls = 'l l-co'
        elif 'Summary' in line:
            cls = 'l l-sum'
        else:
            cls = 'l'
        out.append('<span class="%s">%s</span>' % (cls, safe))
    return ''.join(out)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def logged_in():
    return session.get('logged_in') is True


@app.route('/login', methods=['GET', 'POST'])
def login():
    error_html = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if username == USERNAME and password == PASSWORD:
            session['logged_in'] = True
            session['user'] = username
            return redirect(url_for('index'))
        error_html = '<div class="login-err">Invalid username or password.</div>'
    elif logged_in():
        return redirect(url_for('index'))
    return LOGIN_PAGE % {'css': CSS, 'error': error_html}


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def index():
    if not logged_in():
        return redirect(url_for('login'))
    content, error = read_log()
    return MAIN_PAGE % {
        'css': CSS,
        'dashboard': render_dashboard(content, error),
        'raw': render_raw(content, error),
        'logpath': html.escape(LOG_FILE),
    }


if __name__ == '__main__':
    print('=' * 50)
    print('BPR Log Viewer starting...')
    print('  Log file : %s' % LOG_FILE)
    print('  URL      : http://0.0.0.0:5000/  (open http://<server-ip>:5000/)')
    print('  Login    : %s' % USERNAME)
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=False)

#!/usr/bin/env python3
"""DeepSearch Pro — the Deep Search tool (186 engines) as a hosted SaaS.

Built from search_server.py + search_engines.py (the home-network tool),
with user accounts, tiered access, rate limiting, a JSON API, and
Payzum payment links for Pro ($9/mo) and API ($29/mo) tiers.

Payment: Payzum (payzum.com) — non-custodial crypto payments, 0.5%/sale.
Merchant: DeepSearch Pro (pzm_ta77883s4d3dl6ov5fk8sxhn).
Phase 1: manual upgrades — check Payzum dashboard for buyer email,
run upgrade_user.py. No automated webhook yet (Payzum webhook
integration is a future step; the Teleport webhook code below is
unused).

Tiers:
  free  — 50 searches/day, web + video + images + academic + news only
          (no adult category), no API access
  pro   — $9/mo, unlimited searches, all categories including adult
  api   — $29/mo, everything Pro has + JSON API access (10,000 calls/mo)

Auth: email + password (PBKDF2-HMAC-SHA256, 600k iterations, random salt).
Sessions: random tokens in SQLite, 7-day expiry, HttpOnly cookies.
API keys: random tokens per user, sent as ?key= or X-API-Key header.

Storage: SQLite (deepsearch_pro.db) — users, sessions, daily usage.
No external dependencies — stdlib only, like the original.

Run:  python3 deepsearch_pro.py          (port 8091 by default)
      PORT=8091 python3 deepsearch_pro.py

Deploy: copy this file + search_engines.py + landing.html to the server,
run behind Cloudflare Tunnel for public HTTPS access. Do NOT run on the
same port as the home search_server.py (8090).
"""
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from search_engines import (search_all, VIDEO_ENGINES, IMAGE_ENGINES,
                            WEB_ENGINES, ACADEMIC_ENGINES, NEWS_ENGINES,
                            SOCIAL_ENGINES, ADULT_ENGINES, OBSCURE_ENGINES)

PORT = int(os.environ.get("PORT", "8091"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deepsearch_pro.db"))

# Teleport webhook secret — set in the environment when the Custom plan
# is active. Leave unset on the Free plan (webhook endpoint returns 503).
TELEPORT_WEBHOOK_SECRET = os.environ.get("TELEPORT_WEBHOOK_SECRET", "")

FREE_DAILY_LIMIT = 50
API_MONTHLY_LIMIT = 10000
SESSION_DAYS = 7

CATEGORIES = {
    "all": None,
    "web": WEB_ENGINES + OBSCURE_ENGINES,
    "video": VIDEO_ENGINES,
    "images": IMAGE_ENGINES,
    "adult": ADULT_ENGINES,
    "academic": ACADEMIC_ENGINES,
    "news": NEWS_ENGINES,
    "social": SOCIAL_ENGINES,
}

# Free tier gets everything except the adult category.
FREE_CATEGORIES = {"all", "web", "video", "images", "academic", "news",
                   "social"}
# "all" on the free tier excludes adult engines.
FREE_ALL_ENGINES = (WEB_ENGINES + OBSCURE_ENGINES + VIDEO_ENGINES
                    + IMAGE_ENGINES + ACADEMIC_ENGINES + NEWS_ENGINES
                    + SOCIAL_ENGINES)


# ---------------------------------------------------------------- database

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'free',
            api_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            tier_expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            searches INTEGER NOT NULL DEFAULT 0,
            api_calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON sessions(user_id);
    """)
    db.commit()
    db.close()


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt),
        600_000).hex()


def create_user(email: str, password: str):
    """Returns (user_id, error). Error is None on success."""
    email = email.strip().lower()
    if "@" not in email or len(email) < 5:
        return None, "Enter a valid email address."
    if len(password) < 8:
        return None, "Password must be at least 8 characters."
    salt = secrets.token_hex(16)
    pw_hash = hash_password(password, salt)
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, salt, tier,"
            " created_at) VALUES (?, ?, ?, 'free', ?)",
            (email, pw_hash, salt, now))
        db.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "An account with that email already exists."
    finally:
        db.close()


def authenticate(email: str, password: str):
    """Returns the user row on success, None on failure."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?",
                     (email.strip().lower(),)).fetchone()
    db.close()
    if not row:
        return None
    if hmac.compare_digest(hash_password(password, row["salt"]),
                           row["password_hash"]):
        return row
    return None


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc)
               + timedelta(days=SESSION_DAYS)).isoformat()
    db = get_db()
    db.execute("INSERT INTO sessions (token, user_id, expires_at)"
               " VALUES (?, ?, ?)", (token, user_id, expires))
    db.commit()
    db.close()
    return token


def get_user_from_token(token: str):
    if not token:
        return None
    db = get_db()
    row = db.execute(
        "SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id"
        " WHERE s.token = ? AND s.expires_at > ?",
        (token, datetime.now(timezone.utc).isoformat())).fetchone()
    db.close()
    return row


def delete_session(token: str):
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))
    db.commit()
    db.close()


def get_or_create_api_key(user_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT api_key FROM users WHERE id = ?",
                     (user_id,)).fetchone()
    if row and row["api_key"]:
        db.close()
        return row["api_key"]
    key = "dsp_" + secrets.token_urlsafe(24)
    db.execute("UPDATE users SET api_key = ? WHERE id = ?", (key, user_id))
    db.commit()
    db.close()
    return key


def get_user_by_api_key(key: str):
    if not key or not key.startswith("dsp_"):
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE api_key = ?",
                     (key,)).fetchone()
    db.close()
    return row


# ------------------------------------------------------------ rate limits

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record_search(user_id: int):
    db = get_db()
    db.execute(
        "INSERT INTO usage (user_id, date, searches) VALUES (?, ?, 1)"
        " ON CONFLICT(user_id, date) DO UPDATE SET"
        " searches = searches + 1",
        (user_id, _today()))
    db.commit()
    db.close()


def record_api_call(user_id: int):
    db = get_db()
    db.execute(
        "INSERT INTO usage (user_id, date, api_calls) VALUES (?, ?, 1)"
        " ON CONFLICT(user_id, date) DO UPDATE SET"
        " api_calls = api_calls + 1",
        (user_id, _today()))
    db.commit()
    db.close()


def searches_today(user_id: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT searches FROM usage WHERE user_id = ? AND date = ?",
        (user_id, _today())).fetchone()
    db.close()
    return row["searches"] if row else 0


def api_calls_this_month(user_id: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(api_calls), 0) AS total FROM usage"
        " WHERE user_id = ? AND substr(date, 1, 7) = ?",
        (user_id, _this_month())).fetchone()
    db.close()
    return row["total"] if row else 0


def can_search(user) -> tuple:
    """Returns (allowed, message). Pro/API are unlimited; free is capped."""
    if user["tier"] in ("pro", "api"):
        return True, ""
    used = searches_today(user["id"])
    if used >= FREE_DAILY_LIMIT:
        return False, (f"Daily limit reached ({FREE_DAILY_LIMIT} searches)."
                       " Upgrade to Pro for unlimited searches.")
    return True, ""


def can_api_call(user) -> tuple:
    if user["tier"] != "api":
        return False, "API access requires the API tier ($29/mo)."
    used = api_calls_this_month(user["id"])
    if used >= API_MONTHLY_LIMIT:
        return False, (f"Monthly API limit reached"
                       f" ({API_MONTHLY_LIMIT:,} calls).")
    return True, ""


# ------------------------------------------------------------- tier admin

def set_tier(email: str, tier: str, months: int = 1):
    """Upgrade/downgrade a user. Used by the webhook handler and by the
    manual upgrade script (upgrade_user.py). Returns True on success.
    Upgrading to the API tier auto-generates an API key if the user
    doesn't have one yet."""
    if tier not in ("free", "pro", "api"):
        return False
    expires = None
    if tier in ("pro", "api"):
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=30 * months)).isoformat()
    db = get_db()
    cur = db.execute(
        "UPDATE users SET tier = ?, tier_expires_at = ? WHERE email = ?",
        (tier, expires, email.strip().lower()))
    changed = cur.rowcount > 0
    if changed and tier == "api":
        row = db.execute("SELECT id, api_key FROM users WHERE email = ?",
                         (email.strip().lower(),)).fetchone()
        if row and not row["api_key"]:
            key = "dsp_" + secrets.token_urlsafe(24)
            db.execute("UPDATE users SET api_key = ? WHERE id = ?",
                       (key, row["id"]))
    db.commit()
    db.close()
    return changed


def expire_tiers():
    """Downgrade users whose paid tier has expired. Run daily via cron."""
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    cur = db.execute(
        "UPDATE users SET tier = 'free', tier_expires_at = NULL"
        " WHERE tier IN ('pro', 'api') AND tier_expires_at IS NOT NULL"
        " AND tier_expires_at < ?", (now,))
    db.commit()
    n = cur.rowcount
    db.close()
    return n


# ------------------------------------------------------------------- HTML

BASE_CSS = """
body{font-family:system-ui,sans-serif;max-width:860px;margin:40px auto;
padding:0 16px;background:#111;color:#e8e8e8}
h1{font-size:1.4em;color:#d4a94e}
a{color:#7ab8ff}
input[type=text],input[type=email],input[type=password]{padding:10px;
font-size:1em;background:#222;color:#e8e8e8;border:1px solid #444;
border-radius:6px;width:100%;box-sizing:border-box;margin:6px 0}
button,.btn{padding:10px 18px;font-size:1em;background:#d4a94e;color:#111;
border:none;border-radius:6px;cursor:pointer;text-decoration:none;
display:inline-block}
.nav{text-align:right;font-size:.9em;color:#888;margin-bottom:10px}
.nav a{margin-left:12px}
.error{color:#ff7a7a;background:#2a1616;padding:10px;border-radius:6px;
margin:10px 0}
.info{color:#9ed49e;background:#16261a;padding:10px;border-radius:6px;
margin:10px 0}
.tier-badge{background:#d4a94e;color:#111;padding:2px 8px;border-radius:10px;
font-size:.8em;font-weight:bold}
table{border-collapse:collapse;margin:16px 0}
td,th{border:1px solid #333;padding:8px 12px;text-align:left}
th{background:#1a1a1a}
code{background:#222;padding:2px 6px;border-radius:4px;font-size:.9em}
"""

SEARCH_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSearch Pro</title>
<style>
""" + BASE_CSS + """
form{display:flex;gap:8px;margin:20px 0;flex-wrap:wrap}
form input[type=text]{flex:1;min-width:200px;width:auto}
select{padding:10px;font-size:1em;background:#222;color:#e8e8e8;
border:1px solid #444;border-radius:6px}
.result{margin:18px 0;padding-bottom:12px;border-bottom:1px solid #333;
overflow:hidden}
.result a{color:#7ab8ff;text-decoration:none;font-size:1.05em}
.result .url{color:#888;font-size:.85em;word-break:break-all}
.result .snip{color:#bbb;margin-top:4px}
.result img.thumb{float:left;width:160px;max-height:120px;object-fit:cover;
margin:0 12px 6px 0;border-radius:6px;border:1px solid #333}
.src{color:#d4a94e;font-size:.8em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
gap:14px;margin:14px 0}
.card{background:#1a1a1a;border:1px solid #333;border-radius:8px;overflow:hidden}
.card img{width:100%;height:110px;object-fit:cover;display:block}
.card .body{padding:8px}
.card .body a{color:#7ab8ff;text-decoration:none;font-size:.9em;
display:block;line-height:1.3}
.card .meta{color:#888;font-size:.75em;margin-top:4px}
.note{color:#777;font-size:.85em;margin-top:30px}
.usage{color:#888;font-size:.85em}
</style></head><body>
<div class="nav">{nav}</div>
<h1>DeepSearch Pro</h1>
<p class="usage">{usage_line}</p>
<form method="get" action="/search"><input type="text" name="q" value="{q}"
placeholder="What are you looking for?" autofocus>
<select name="cat">
<option value="all"{sel_all}>Everything</option>
<option value="web"{sel_web}>Web + obscure sites</option>
<option value="video"{sel_video}>Video</option>
<option value="images"{sel_images}>Images</option>
{adult_option}
<option value="academic"{sel_academic}>Academic</option>
<option value="news"{sel_news}>News</option>
<option value="social"{sel_social}>Social</option>
</select>
<button type="submit">Search</button></form>
{message}
{results}
<p class="note">186 engines searched in parallel. Your searches are not
logged or sold. Pro users get unlimited searches and all categories.</p>
</body></html>"""

AUTH_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — DeepSearch Pro</title>
<style>""" + BASE_CSS + """
.form-box{max-width:400px;margin:60px auto}
</style></head><body>
<div class="form-box">
<h1>DeepSearch Pro</h1>
<h2>{title}</h2>
{message}
<form method="post" action="{action}">
<input type="email" name="email" placeholder="Email address" required>
<input type="password" name="password" placeholder="Password (8+ characters)" required>
<button type="submit">{button}</button>
</form>
<p>{switch_link}</p>
<p><a href="/">← Back to home</a></p>
</div>
</body></html>"""

ACCOUNT_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account — DeepSearch Pro</title>
<style>""" + BASE_CSS + """</style></head><body>
<div class="nav"><a href="/search">Search</a> <a href="/logout">Log out</a></div>
<h1>Your Account</h1>
{message}
<table>
<tr><th>Email</th><td>{email}</td></tr>
<tr><th>Plan</th><td><span class="tier-badge">{tier}</span></td></tr>
<tr><th>Member since</th><td>{created}</td></tr>
<tr><th>Plan renews/expires</th><td>{expires}</td></tr>
<tr><th>Searches today</th><td>{searches_today}</td></tr>
<tr><th>API calls this month</th><td>{api_calls}</td></tr>
</table>
<h2>API Key</h2>
{api_key_section}
<h2>Upgrade</h2>
{upgrade_section}
</body></html>"""


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _nav(user) -> str:
    if user:
        return (f'{_esc(user["email"])} '
                f'<span class="tier-badge">{_esc(user["tier"])}</span> '
                f'<a href="/account">Account</a> '
                f'<a href="/logout">Log out</a>')
    return '<a href="/login">Log in</a> <a href="/signup">Sign up</a>'


def render_results(query: str, results: list, category: str) -> str:
    if not query:
        return ""
    if not results:
        return f"<p>No results for <b>{_esc(query)}</b>.</p>"
    parts = [f"<p>{len(results)} results for <b>{_esc(query)}</b></p>"]
    if category == "images":
        cards = []
        for r in results:
            thumb = r.get("thumbnail") or r["url"]
            cards.append(
                f'<div class="card"><a href="{_esc(r["url"])}"'
                f' target="_blank">'
                f'<img src="{_esc(thumb)}" alt="" loading="lazy"></a>'
                f'<div class="body"><a href="{_esc(r["url"])}"'
                f' target="_blank">'
                f'{_esc(r["title"][:80])}</a>'
                f'<div class="meta">{_esc(r.get("source", ""))}'
                + (f' · {_esc(r["snippet"])}' if r.get("snippet") else "")
                + "</div></div></div>"
            )
        parts.append('<div class="grid">' + "\n".join(cards) + "</div>")
        return "\n".join(parts)
    for r in results:
        source = r.get("source", "")
        src_tag = (f' <span class="src">[{_esc(source)}]</span>'
                   if source else "")
        thumb = r.get("thumbnail", "")
        img = (f'<a href="{_esc(r["url"])}" target="_blank">'
               f'<img class="thumb" src="{_esc(thumb)}" alt=""'
               f' loading="lazy">'
               f"</a>" if thumb else "")
        parts.append(
            f'<div class="result">{img}'
            f'<a href="{_esc(r["url"])}" target="_blank">'
            f'{_esc(r["title"])}</a>{src_tag}'
            f'<div class="url">{_esc(r["url"])}</div>'
            + (f'<div class="snip">{_esc(r["snippet"])}</div>'
               if r.get("snippet") else "")
            + "</div>"
        )
    return "\n".join(parts)


# -------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # stay quiet

    # -- helpers ------------------------------------------------------
    def _send_html(self, body: str, status: int = 200, cookie: str = ""):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status: int = 200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie: str = ""):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _session_token(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):]
        return ""

    def _current_user(self):
        return get_user_from_token(self._session_token())

    def _session_cookie(self, token: str) -> str:
        return (f"session={token}; HttpOnly; Path=/; "
                f"Max-Age={SESSION_DAYS * 86400}; SameSite=Lax")

    def _clear_cookie(self) -> str:
        return "session=; HttpOnly; Path=/; Max-Age=0"

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw).items()}

    # -- routes -------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        user = self._current_user()

        if path == "/":
            if user:
                self._redirect("/search")
            else:
                self._serve_landing()
            return
        if path == "/search":
            self._handle_search(user, params)
            return
        if path == "/signup":
            self._serve_auth("Sign up", "/signup", "Create account",
                             'Already have an account? <a href="/login">'
                             'Log in</a>')
            return
        if path == "/login":
            self._serve_auth("Log in", "/login", "Log in",
                             'No account yet? <a href="/signup">'
                             'Sign up free</a>')
            return
        if path == "/logout":
            delete_session(self._session_token())
            self._redirect("/", cookie=self._clear_cookie())
            return
        if path == "/account":
            if not user:
                self._redirect("/login")
                return
            self._serve_account(user)
            return
        if path == "/api/search":
            self._handle_api(params)
            return
        if path == "/landing.html":
            self._serve_landing()
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/signup":
            form = self._read_form()
            user_id, err = create_user(form.get("email", ""),
                                       form.get("password", ""))
            if err:
                self._serve_auth("Sign up", "/signup", "Create account",
                                 'Already have an account? <a href="/login">'
                                 'Log in</a>',
                                 message=f'<div class="error">'
                                         f'{_esc(err)}</div>')
                return
            token = create_session(user_id)
            self._redirect("/search", cookie=self._session_cookie(token))
            return
        if path == "/login":
            form = self._read_form()
            user = authenticate(form.get("email", ""),
                                form.get("password", ""))
            if not user:
                self._serve_auth("Log in", "/login", "Log in",
                                 'No account yet? <a href="/signup">'
                                 'Sign up free</a>',
                                 message='<div class="error">Invalid email'
                                         ' or password.</div>')
                return
            token = create_session(user["id"])
            self._redirect("/search", cookie=self._session_cookie(token))
            return
        if path == "/webhook/teleport":
            self._handle_teleport_webhook()
            return
        self.send_error(404, "Not found")

    # -- page handlers ------------------------------------------------
    def _serve_landing(self):
        landing_path = os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "landing.html")
        try:
            with open(landing_path, encoding="utf-8") as f:
                self._send_html(f.read())
        except FileNotFoundError:
            self._send_html(
                "<h1>DeepSearch Pro</h1><p>Landing page not found.</p>"
                '<p><a href="/signup">Sign up</a> | '
                '<a href="/login">Log in</a></p>')

    def _serve_auth(self, title, action, button, switch_link, message=""):
        body = (AUTH_PAGE.replace("{title}", title)
                .replace("{action}", action)
                .replace("{button}", button)
                .replace("{switch_link}", switch_link)
                .replace("{message}", message))
        self._send_html(body)

    def _serve_account(self, user):
        tier = user["tier"]
        expires = user["tier_expires_at"] or "—"
        if expires != "—":
            expires = expires[:10]
        searches = searches_today(user["id"])
        api_calls = api_calls_this_month(user["id"])

        if tier == "api":
            key = get_or_create_api_key(user["id"])
            api_section = (
                f'<p>Your API key: <code>{_esc(key)}</code></p>'
                f'<p>Use it as <code>?key=YOUR_KEY</code> or the'
                f' <code>X-API-Key</code> header on'
                f' <code>/api/search?q=...</code>.</p>'
                f'<p>Calls this month: {api_calls:,} /'
                f' {API_MONTHLY_LIMIT:,}.</p>')
        elif tier == "pro":
            api_section = ("<p>API access is available on the API tier"
                           " ($29/mo).</p>")
        else:
            api_section = ("<p>API access is available on the API tier"
                           " ($29/mo).</p>")

        if tier == "free":
            upgrade = (
                '<p>You are on the <b>Free</b> plan — 50 searches/day,'
                ' no adult category.</p>'
                '<p><a class="btn" href="https://merchant.payzum.com/buy/pzl_krud98vuvr3127pkvxrzus35">Upgrade to Pro'
                ' — $9/mo</a></p>'
                '<p>Or go straight to <a href="https://merchant.payzum.com/buy/pzl_j8m3sq201dvgjvb27idt7q4t">API — $29/mo</a>'
                ' for JSON API access.</p>')
        elif tier == "pro":
            upgrade = (
                '<p>You are on <b>Pro</b> — unlimited searches, all'
                ' categories.</p>'
                '<p><a class="btn" href="https://merchant.payzum.com/buy/pzl_j8m3sq201dvgjvb27idt7q4t">Upgrade to API'
                ' — $29/mo</a></p>')
        else:
            upgrade = ('<p>You are on the <b>API</b> tier — everything'
                       ' unlocked.</p>')

        body = (ACCOUNT_PAGE
                .replace("{message}", "")
                .replace("{email}", _esc(user["email"]))
                .replace("{tier}", _esc(tier))
                .replace("{created}", (user["created_at"] or "")[:10])
                .replace("{expires}", expires)
                .replace("{searches_today}",
                         f"{searches} / {FREE_DAILY_LIMIT}"
                         if tier == "free" else f"{searches} (unlimited)")
                .replace("{api_calls}",
                         f"{api_calls:,} / {API_MONTHLY_LIMIT:,}"
                         if tier == "api" else "—")
                .replace("{api_key_section}", api_section)
                .replace("{upgrade_section}", upgrade))
        self._send_html(body)

    def _handle_search(self, user, params):
        if not user:
            self._redirect("/login")
            return
        query = params.get("q", [""])[0].strip()
        category = params.get("cat", ["all"])[0]
        if category not in CATEGORIES:
            category = "all"

        # Free tier: adult category is locked.
        if user["tier"] == "free" and category == "adult":
            category = "all"

        message = ""
        results = []
        if query:
            allowed, msg = can_search(user)
            if not allowed:
                message = (f'<div class="error">{_esc(msg)}</div>')
            else:
                record_search(user["id"])
                if user["tier"] == "free" and category == "all":
                    engines = FREE_ALL_ENGINES
                else:
                    engines = CATEGORIES[category]
                per_engine = 5 if category == "all" else 8
                results = search_all(query, engines=engines,
                                     max_per_engine=per_engine)

        if user["tier"] == "free":
            used = searches_today(user["id"])
            usage_line = (f"Free plan: {used}/{FREE_DAILY_LIMIT} searches"
                          f" used today. "
                          f'<a href="https://merchant.payzum.com/buy/pzl_krud98vuvr3127pkvxrzus35">Upgrade to Pro</a> for'
                          f" unlimited.")
        else:
            usage_line = (f"{user['tier'].title()} plan: unlimited"
                          f" searches.")

        adult_option = ""
        if user["tier"] in ("pro", "api"):
            adult_option = ('<option value="adult"'
                            + (' selected' if category == "adult" else '')
                            + '>Adult</option>')

        body = SEARCH_PAGE.replace("{nav}", _nav(user))
        body = body.replace("{usage_line}", usage_line)
        body = body.replace("{q}", html.escape(query, quote=True))
        for cat in CATEGORIES:
            if cat == "adult":
                continue
            body = body.replace("{sel_" + cat + "}",
                                " selected" if cat == category else "")
        body = body.replace("{adult_option}", adult_option)
        body = body.replace("{message}", message)
        body = body.replace("{results}",
                            render_results(query, results, category))
        self._send_html(body)

    def _handle_api(self, params):
        key = (params.get("key", [""])[0]
               or self.headers.get("X-API-Key", ""))
        user = get_user_by_api_key(key)
        if not user:
            self._send_json({"error": "Invalid or missing API key."},
                            status=401)
            return
        allowed, msg = can_api_call(user)
        if not allowed:
            self._send_json({"error": msg}, status=429)
            return
        query = params.get("q", [""])[0].strip()
        if not query:
            self._send_json({"error": "Missing ?q= query parameter."},
                            status=400)
            return
        category = params.get("cat", ["all"])[0]
        engines = CATEGORIES.get(category)
        if category not in CATEGORIES:
            self._send_json({"error": f"Unknown category: {category}."
                             f" Valid: {', '.join(CATEGORIES)}"},
                            status=400)
            return
        per_engine = int(params.get("per_engine", ["5"])[0] or 5)
        per_engine = max(1, min(per_engine, 20))
        record_api_call(user["id"])
        results = search_all(query, engines=engines,
                             max_per_engine=per_engine)
        # Strip internal keys before returning.
        clean = []
        for r in results:
            clean.append({k: v for k, v in r.items()
                          if not k.startswith("_")})
        self._send_json({
            "query": query,
            "category": category,
            "results": clean,
            "count": len(clean),
            "api_calls_this_month": api_calls_this_month(user["id"]),
            "api_monthly_limit": API_MONTHLY_LIMIT,
        })

    def _handle_teleport_webhook(self):
        """Teleport payment webhook (Custom plan only).

        Teleport signs webhook payloads with HMAC-SHA256 using the webhook
        secret configured in the dashboard. Expected payload shape
        (verify against Teleport's webhook docs when the Custom plan is
        active — see TELEPORT_INTEGRATION.md):
            {
              "event": "payment.succeeded",
              "data": {
                "customer_email": "buyer@example.com",
                "amount": 9.00,
                "currency": "USDT",
                "metadata": {"plan": "pro"}
              }
            }
        On a successful payment, the user's tier is upgraded for 30 days.
        """
        if not TELEPORT_WEBHOOK_SECRET:
            self._send_json({"error": "Webhook not configured."},
                            status=503)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        signature = self.headers.get("X-Teleport-Signature", "")
        expected = hmac.new(
            TELEPORT_WEBHOOK_SECRET.encode(), raw,
            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            self._send_json({"error": "Invalid signature."}, status=401)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "Invalid JSON."}, status=400)
            return

        event = payload.get("event", "")
        data = payload.get("data", {})
        if event in ("payment.succeeded", "subscription.payment_succeeded",
                     "invoice.paid"):
            email = data.get("customer_email", "")
            plan = (data.get("metadata", {}) or {}).get("plan", "")
            # Fallback: infer plan from amount.
            if not plan:
                amount = float(data.get("amount", 0) or 0)
                plan = "api" if amount >= 29 else "pro"
            if email and plan in ("pro", "api"):
                ok = set_tier(email, plan, months=1)
                self._send_json({"ok": ok, "tier": plan,
                                 "email": email})
                return
            self._send_json({"error": "Missing email or plan."},
                            status=400)
            return
        if event in ("subscription.cancelled", "subscription.expired"):
            email = data.get("customer_email", "")
            if email:
                set_tier(email, "free")
                self._send_json({"ok": True, "tier": "free"})
                return
        # Unknown event — acknowledge so Teleport stops retrying.
        self._send_json({"ok": True, "ignored": event})


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"DeepSearch Pro running on port {PORT}", flush=True)
    print(f"Database: {DB_PATH}", flush=True)
    server.serve_forever()

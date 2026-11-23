"""accounts.py - sign-in, accounts, the laptop-only Admin tab, and per-account settings.

Replaces the old one-time PIN. Everyone (laptop and phones) signs in with a username and password.

  * Accounts and their saved settings live in instance/rasputin.db (the same SQLite file as your chats).
    This module uses Python's built-in sqlite3, so it works even if the chat models change.
  * Passwords are stored only as salted hashes. Sessions are a signed cookie that lasts 90 days and
    survives restarts (the signing key is kept in instance/secret_key.txt).
  * Levels: 0 = User (no admin), 1 = Admin, 2 = Owner. Any level above 0 may open the Admin tab, and
    only on the laptop (a request that really comes from this PC). That is all the levels do, plus one safety
    rule: nobody can create or remove an account that outranks them.
  * The first time Rasputin starts with no accounts, the laptop shows a one-time setup page that creates the
    first Owner. Phones are told to do that on the PC.

Public pieces:
    init(app, db_path)   call once from app.py
    page_context()       extra values for render_template("index.html", ...)
    is_laptop()          True only for a request that comes from this PC itself
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import g, jsonify, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

LEVELS = {0: "User", 1: "Admin", 2: "Owner"}
LEVEL_LABELS = {0: "User (no admin access)", 1: "Admin", 2: "Owner"}
PW_MIN, PW_MAX = 6, 128
USER_RX = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SETTING_KEY_RX = re.compile(r"^rasputin[A-Za-z0-9_.@-]{0,50}$")
SETTING_MAX_BYTES, SETTING_MAX_KEYS = 20000, 40
SESSION_DAYS = 90
MAX_FAILS, LOCKOUT = 5, 60                          # wrong passwords from one address before it must wait
HASH_METHOD = "pbkdf2:sha256:260000"

PUBLIC_EXACT = {"/login", "/setup", "/health", "/manifest.webmanifest", "/sw.js"}   # reachable while signed out
PUBLIC_PREFIX = ("/pwa/",)
LOCAL_ADDRS = ("127.0.0.1", "::1")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-Ip", "Forwarded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS account_setting (
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);
"""

_DB = None
_fails = {}                                         # ip -> (count, locked_until)
_fails_lock = threading.Lock()
_dummy_hash = None


# ── database helpers ─────────────────────────────────────────────────────────
@contextmanager
def _conn():
    c = sqlite3.connect(_DB, timeout=30, isolation_level=None)   # autocommit; transactions are explicit
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        yield c
    finally:
        c.close()


@contextmanager
def _tx():
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _user(uid):
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM account WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def _count():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM account").fetchone()[0]


def _hash(pw):
    return generate_password_hash(pw, method=HASH_METHOD)


# ── who is asking ────────────────────────────────────────────────────────────
def is_laptop():
    """True only for a request that comes from this PC itself (not a phone, not a proxy forwarding for one)."""
    if request.remote_addr not in LOCAL_ADDRS:
        return False
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    h = (request.host or "").lower()
    if h.startswith("["):
        host = h[1:].split("]")[0]                      # [::1]:5000
    else:
        host = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return host in LOCAL_HOSTS


def _me():
    return getattr(g, "account", None)


def _is_page_request():
    return request.method == "GET" and "text/html" in (request.headers.get("Accept") or "")


def _denied():
    if _is_page_request():
        return redirect("/setup" if _count() == 0 else "/login")
    r = jsonify({"error": "Please sign in again."})
    r.status_code = 401
    r.headers["X-Rasputin-Auth"] = "login"
    return r


def _throttle_wait(ip):
    with _fails_lock:
        until = _fails.get(ip, (0, 0))[1]
    return int(until - time.time()) + 1 if time.time() < until else 0


def _note_fail(ip):
    with _fails_lock:
        n, _until = _fails.get(ip, (0, 0))
        n += 1
        _fails[ip] = (0, time.time() + LOCKOUT) if n >= MAX_FAILS else (n, 0)


def _clear_fails(ip):
    with _fails_lock:
        _fails.pop(ip, None)


# ── validation ───────────────────────────────────────────────────────────────
def _check_username(name):
    name = (name or "").strip()
    if not USER_RX.match(name):
        return None, "Username must be 3 to 32 characters: letters, numbers, dot, dash or underscore."
    return name, None


def _check_password(pw):
    if not isinstance(pw, str) or len(pw) < PW_MIN:
        return f"Password must be at least {PW_MIN} characters."
    if len(pw) > PW_MAX:
        return f"Password must be at most {PW_MAX} characters."
    return None


def _create_account(c, username, pw, level):
    """Insert inside an open transaction. Returns the new id, or raises ValueError with a readable message."""
    try:
        cur = c.execute("INSERT INTO account (username, username_key, pw_hash, level, created_at) VALUES (?,?,?,?,?)",
                        (username, username.lower(), _hash(pw), int(level), _now()))
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken.")
    return cur.lastrowid


# ── sessions ─────────────────────────────────────────────────────────────────
class _Sessions(SecureCookieSessionInterface):
    """Same as Flask's, except the cookie is marked Secure only on https (the PC window is plain http)."""

    def get_cookie_secure(self, app):
        try:
            return bool(request.is_secure)
        except RuntimeError:
            return False


def _secret_key(instance_dir):
    path = os.path.join(instance_dir, "secret_key.txt")
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        if len(v) >= 32:
            return v
    except OSError:
        pass
    v = secrets.token_hex(32)
    os.makedirs(instance_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(v)
    return v


def _sign_in(acct):
    session.clear()
    session.permanent = True
    session["uid"] = acct["id"]
    session["sv"] = acct["session_version"]


def _settings_of(uid):
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM account_setting WHERE account_id=?", (uid,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def page_context():
    """Values index.html needs: who is signed in, whether to show the Admin and Discord tabs, and their saved settings."""
    a = _me()
    if not a:
        return {"account_boot": {}, "show_admin": False, "show_discord": False, "show_games": False}
    return {
        "account_boot": {"id": a["id"], "username": a["username"], "level": a["level"],
                         "level_name": LEVELS.get(a["level"], "User"), "settings": _settings_of(a["id"])},
        "show_admin": bool(is_laptop() and a["level"] >= 1),
        "show_discord": bool(a["level"] >= 1),         # Admins and the Owner, on the PC and on phones
        "show_games": bool(a["level"] >= 1),           # Games tab (Mindcraft launches processes, so staff only)
    }


# ── setup ────────────────────────────────────────────────────────────────────
def init(app, db_path):
    global _DB, _dummy_hash
    _DB = db_path
    instance_dir = os.path.dirname(db_path)
    os.makedirs(instance_dir, exist_ok=True)
    with _conn() as c:
        try:
            c.execute("PRAGMA journal_mode=WAL")      # readers and writers stop blocking each other (kept in the file)
        except sqlite3.Error as exc:
            print("[ACCOUNTS] could not switch the database to WAL mode:", exc)
        c.executescript(SCHEMA)
    _dummy_hash = _hash("not-a-real-password")        # lets a wrong username take as long as a wrong password

    app.secret_key = _secret_key(instance_dir)
    app.config.update(SESSION_COOKIE_NAME="rasputin_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS))
    app.session_interface = _Sessions()

    @app.before_request
    def _guard():
        path = request.path
        if path == "/shutdown" and not is_laptop():
            return ("", 403)                           # a phone can't shut the PC app down
        g.account = None
        uid = session.get("uid")
        if uid is not None:
            acct = _user(uid)
            if acct and acct["session_version"] == session.get("sv"):
                g.account = acct
            else:
                session.clear()                        # account removed or password reset: sign this device out
        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX) or g.account:
            return None
        if path.startswith("/v1/") and is_laptop() and "Origin" not in request.headers:
            return None                                # Mindcraft bot -> local model (games/llm_api.py); this PC only, never a browser
        return _denied()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if resp.mimetype == "text/html":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.route("/health")
    def _health():
        r = jsonify({"ok": True})
        r.headers["Cache-Control"] = "no-store"
        return r

    # ---- sign in / out / first-time setup ----
    def page(mode, message="", username="", status=200):
        titles = {"login": ("SIGN IN", "SIGN IN"), "setup": ("CREATE THE FIRST ACCOUNT", "CREATE ACCOUNT"),
                  "blocked": ("NOT SET UP YET", "")}
        sub, button = titles[mode]
        return render_template("auth.html", mode=mode, subtitle=sub, button=button, message=message,
                               username=username, pw_min=PW_MIN), status

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if _count() == 0:
            return redirect("/setup")
        if request.method == "GET":
            return redirect("/") if _me() else page("login")
        ip = request.remote_addr or ""
        name = (request.form.get("username") or "").strip()[:64]
        pw = (request.form.get("password") or "")[:PW_MAX + 1]
        wait = _throttle_wait(ip)
        if wait:
            return page("login", f"Too many tries. Wait {wait} seconds.", name, 429)
        with _conn() as c:
            row = c.execute("SELECT * FROM account WHERE username_key=?", (name.lower(),)).fetchone()
        ok = check_password_hash(row["pw_hash"] if row else _dummy_hash, pw)
        if not (row and ok):
            _note_fail(ip)
            return page("login", "Wrong username or password.", name, 401)
        _clear_fails(ip)
        acct = dict(row)
        with _tx() as c:
            c.execute("UPDATE account SET last_login=? WHERE id=?", (_now(), acct["id"]))
        _sign_in(acct)
        return redirect("/")

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if _count() > 0:
            return redirect("/login")
        if not is_laptop():
            return page("blocked", "Rasputin has no accounts yet. Open Rasputin on the PC to create the first one.", status=403)
        if request.method == "GET":
            return page("setup")
        name, err = _check_username(request.form.get("username"))
        pw, pw2 = request.form.get("password") or "", request.form.get("confirm") or ""
        err = err or _check_password(pw) or (None if pw == pw2 else "The two passwords do not match.")
        if err:
            return page("setup", err, (request.form.get("username") or "").strip()[:64], 400)
        try:
            with _tx() as c:
                if c.execute("SELECT COUNT(*) FROM account").fetchone()[0]:
                    return redirect("/login")
                uid = _create_account(c, name, pw, 2)
        except ValueError as exc:
            return page("setup", str(exc), name, 400)
        _sign_in(_user(uid))
        return redirect("/")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return jsonify({"ok": True})

    # ---- saved settings (per account) ----
    @app.route("/account/settings", methods=["GET"])
    def settings_get():
        return jsonify({"settings": _settings_of(_me()["id"])})

    @app.route("/account/settings", methods=["PUT", "POST"])
    def settings_put():
        body = request.get_json(silent=True)
        incoming = body.get("settings") if isinstance(body, dict) else None
        if not isinstance(incoming, dict):
            return jsonify({"error": "Send {\"settings\": {...}}."}), 400
        for k, v in incoming.items():
            if not isinstance(k, str) or not SETTING_KEY_RX.match(k):
                return jsonify({"error": f"Not a Rasputin setting: {k[:40]}"}), 400
            if v is not None and (not isinstance(v, str) or len(v.encode("utf-8")) > SETTING_MAX_BYTES):
                return jsonify({"error": "That value is too large."}), 400
        uid = _me()["id"]
        with _tx() as c:
            have = {r[0] for r in c.execute("SELECT key FROM account_setting WHERE account_id=?", (uid,))}
            adds = {k for k, v in incoming.items() if v is not None} - have
            if len(have) + len(adds) > SETTING_MAX_KEYS:
                return jsonify({"error": "Too many saved settings."}), 400
            for k, v in incoming.items():
                if v is None:
                    c.execute("DELETE FROM account_setting WHERE account_id=? AND key=?", (uid, k))
                else:
                    c.execute("INSERT INTO account_setting (account_id, key, value) VALUES (?,?,?) "
                              "ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value", (uid, k, v))
        return jsonify({"ok": True})

    _register_admin(app)
    _register_discord(app)
    print(f"[ACCOUNTS] ready ({_count()} account{'s' if _count() != 1 else ''}) - sign-in is required everywhere")


# ── Admin tab API (laptop only) ──────────────────────────────────────────────
def _admin_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        me = _me()
        if not is_laptop():
            return jsonify({"error": "The Admin tab only works on the PC."}), 403
        if not me or me["level"] < 1:
            return jsonify({"error": "This account does not have admin access."}), 403
        return fn(me, *a, **kw)
    return wrapper


def _can_manage(me, target):
    """Reset a password or delete: only accounts ranked below you, or anyone if you are an Owner."""
    return me["level"] >= 2 or target["level"] < me["level"]


def _public(row, me):
    return {"id": row["id"], "username": row["username"], "level": row["level"],
            "level_name": LEVELS.get(row["level"], "User"), "created_at": row["created_at"],
            "last_login": row["last_login"], "you": row["id"] == me["id"],
            "can_manage": _can_manage(me, row)}


def _register_admin(app):
    @app.route("/admin/api/accounts", methods=["GET"])
    @_admin_only
    def admin_list(me):
        with _conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM account ORDER BY level DESC, username_key")]
        options = [{"value": v, "label": LEVEL_LABELS[v]} for v in sorted(LEVELS) if v <= me["level"]]
        return jsonify({"accounts": [_public(r, me) for r in rows], "levels": options,
                        "me": {"username": me["username"], "level": me["level"], "level_name": LEVELS[me["level"]]}})

    @app.route("/admin/api/accounts", methods=["POST"])
    @_admin_only
    def admin_create(me):
        d = request.get_json(silent=True)
        d = d if isinstance(d, dict) else {}
        name, err = _check_username(d.get("username"))
        pw = d.get("password")
        err = err or _check_password(pw)
        try:
            level = int(d.get("level", 0))
        except (TypeError, ValueError):
            level = -1
        if not err and level not in LEVELS:
            err = "Pick an admin level."
        if not err and level > me["level"]:
            err = "You cannot create an account with a higher level than your own."
        if err:
            return jsonify({"error": err}), 400
        try:
            with _tx() as c:
                uid = _create_account(c, name, pw, level)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "account": _public(_user(uid), me)})

    def _target(me, uid):
        t = _user(uid)
        if not t:
            return None, (jsonify({"error": "That account no longer exists."}), 404)
        if t["id"] != me["id"] and not _can_manage(me, t):
            return None, (jsonify({"error": "You cannot change an account at or above your own level."}), 403)
        return t, None

    @app.route("/admin/api/accounts/<int:uid>/password", methods=["POST"])
    @_admin_only
    def admin_password(me, uid):
        t, bad = _target(me, uid)
        if bad:
            return bad
        d = request.get_json(silent=True)
        pw = d.get("password") if isinstance(d, dict) else None
        err = _check_password(pw)
        if err:
            return jsonify({"error": err}), 400
        with _tx() as c:       # a new session_version signs that account out everywhere
            c.execute("UPDATE account SET pw_hash=?, session_version=session_version+1 WHERE id=?", (_hash(pw), uid))
        if uid == me["id"]:
            session["sv"] = _user(uid)["session_version"]
        return jsonify({"ok": True})

    @app.route("/admin/api/accounts/<int:uid>", methods=["DELETE"])
    @_admin_only
    def admin_delete(me, uid):
        if uid == me["id"]:
            return jsonify({"error": "You cannot delete the account you are signed in with."}), 400
        t, bad = _target(me, uid)
        if bad:
            return bad
        with _tx() as c:
            if t["level"] >= 2 and c.execute("SELECT COUNT(*) FROM account WHERE level>=2").fetchone()[0] <= 1:
                return jsonify({"error": "That is the last Owner account."}), 400
            c.execute("DELETE FROM account_setting WHERE account_id=?", (uid,))
            c.execute("DELETE FROM account WHERE id=?", (uid,))
        return jsonify({"ok": True})

    @app.route("/admin/api/info", methods=["GET"])
    @_admin_only
    def admin_info(me):
        try:
            import lan
            return jsonify(lan.info())
        except Exception as exc:
            return jsonify({"running": False, "error": str(exc), "urls": [], "setup_urls": []})

    @app.route("/admin/api/tunnel", methods=["GET"])
    @_admin_only
    def admin_tunnel_get(me):
        try:
            import tunnel
            return jsonify(tunnel.status())
        except Exception as exc:
            return jsonify({"state": "error", "on": False, "error": str(exc), "address": ""})

    @app.route("/admin/api/tunnel", methods=["POST"])
    @_admin_only
    def admin_tunnel_set(me):
        body = request.get_json(silent=True) or {}
        try:
            import tunnel
            if "public" in body:
                if not tunnel.set_public(bool(body.get("public"))):
                    return jsonify({**tunnel.status(), "error": "Switch the tunnel on first."}), 400
            elif body.get("on"):
                tunnel.enable(app)
            else:
                tunnel.disable()
            return jsonify(tunnel.status())
        except Exception as exc:
            return jsonify({"state": "error", "on": False, "error": str(exc), "address": ""}), 500


# ── Discord tab API (Admins and the Owner, on the PC or a phone) ─────────────
def _staff_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        me = _me()
        if not me or me["level"] < 1:
            return jsonify({"error": "This account does not have admin access."}), 403
        return fn(me, *a, **kw)
    return wrapper


def _register_discord(app):
    def _status():
        try:
            import discord_bot
            st = discord_bot.status()
        except Exception as exc:
            st = {"state": "error", "on": False, "error": str(exc), "has_token": False,
                  "dms": True, "mentions": True, "images": True, "filter": False, "channels": [], "all_channels": False, "roles": True, "music": True, "invite": ""}
        st["can_edit_token"] = is_laptop()          # the token is only typed in on the PC itself
        return st

    @app.route("/discord/api/status", methods=["GET"])
    @_staff_only
    def discord_status(me):
        return jsonify(_status())

    @app.route("/discord/api/power", methods=["POST"])
    @_staff_only
    def discord_power(me):
        want = bool((request.get_json(silent=True) or {}).get("on"))
        try:
            import discord_bot
            if want:
                discord_bot.start()
            else:
                discord_bot.stop()
        except Exception as exc:
            return jsonify({**_status(), "error": str(exc)}), 500
        return jsonify(_status())

    @app.route("/discord/api/settings", methods=["POST"])
    @_staff_only
    def discord_settings(me):
        d = request.get_json(silent=True)
        d = d if isinstance(d, dict) else {}
        chans = d.get("channels")
        if not isinstance(chans, list):
            chans = []
        try:
            import discord_bot
            err = discord_bot.save_settings(d.get("dms", True), d.get("mentions", True), chans, d.get("images", True), d.get("filter", False),
                                             d.get("all_channels", False), d.get("roles", True), d.get("music", True))
        except Exception as exc:
            err = str(exc)
        if err:
            return jsonify({**_status(), "error": err}), 400
        return jsonify(_status())

    @app.route("/discord/api/token", methods=["POST"])
    @_staff_only
    def discord_token(me):
        if not is_laptop():
            return jsonify({**_status(), "error": "Set or remove the bot token on the PC itself."}), 403
        d = request.get_json(silent=True)
        d = d if isinstance(d, dict) else {}
        try:
            import discord_bot
            err = discord_bot.save_token(str(d.get("token") or ""))
        except Exception as exc:
            err = str(exc)
        if err:
            return jsonify({**_status(), "error": err}), 400
        return jsonify(_status())

    @app.route("/discord/api/log", methods=["GET"])
    @_staff_only
    def discord_log(me):
        try:
            since = int(request.args.get("since", "0"))
        except ValueError:
            since = 0
        try:
            import discord_bot
            entries, last = discord_bot.log_since(since)
        except Exception:
            entries, last = [], 0
        resp = jsonify({"entries": entries, "last": last})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/discord/api/log/clear", methods=["POST"])
    @_staff_only
    def discord_log_clear(me):
        try:
            import discord_bot
            discord_bot.log_clear()
        except Exception:
            pass
        return jsonify({"ok": True})

"""tunnel.py - optional ngrok tunnel that only answers YOUR home internet connection.

The tunnel is OFF every time Rasputin starts. It only opens when you flip the switch in the Admin tab
(Web Addresses), and it closes again when you flip it back or exit Rasputin.

How it works:
  * enable(app) opens an ngrok tunnel to the local server (a trusted https address that never changes).
  * Every request that arrives through the tunnel carries the visitor's public IP in X-Forwarded-For.
    A small gate in front of Flask compares it with this PC's current public IP. Anything else gets a plain 404.
  * The PC's public IP is looked up when the tunnel is switched on and again every few minutes, so a rolling IP
    is followed automatically. If the IP cannot be found at all, the tunnel stays closed (fails safe).
  * Optional "open to anyone" switch (Admin tab): turns that IP check off while the tunnel is on. The sign-in
    (accounts.py) still applies. It is never saved, so every launch starts with it off.
  * Phones on your home Wi-Fi share the PC's public IP, so they pass. A phone on mobile data does not.
  * Requests that come straight over the LAN (no X-Forwarded-For) are untouched.
  * For tunnel requests the real visitor IP is also put into REMOTE_ADDR, so the login lockout in accounts.py
    counts each visitor separately and is_laptop() stays False for the tunnel.

Setup (once): pip install pyngrok, then create instance/ngrok.json:
    {"authtoken": "YOUR_NGROK_AUTHTOKEN", "domain": "your-name.ngrok-free.app"}
Without that file nothing here runs and Rasputin starts exactly as before.
"""
import ipaddress
import json
import os
import threading
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(BASE, "instance", "ngrok.json")
PORT = 5000
DEFAULT_DOMAIN = "welldecked-collins-nonsecretive.ngrok-free.dev"
REFRESH_SECONDS = 120

_lock = threading.Lock()
_allowed = {"v4": set(), "v6": set()}      # exact IPv4 addresses; IPv6 /64 networks (a home gets a whole prefix)
_state = {"state": "off", "url": "", "error": "", "public": False}     # state: off | starting | on | error
# public=True lets anyone on the internet reach the tunnel (the sign-in still applies). Never saved: it is off on every launch.
_stop = threading.Event()                  # stops the IP-refresh thread of the current session
_gate_installed = False
_cleanup_added = False
_op = threading.Lock()                     # one switch-on / switch-off at a time


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def _refresh():
    """Look up this connection's public address(es). Keeps the old ones if the lookup fails."""
    new4, new6 = set(), set()
    a = _fetch("https://api.ipify.org")
    b = _fetch("https://api6.ipify.org")
    try:
        if a:
            new4.add(str(ipaddress.IPv4Address(a)))
    except ValueError:
        pass
    try:
        if b:
            new6.add(str(ipaddress.IPv6Network(b + "/64", strict=False)))
    except ValueError:
        pass
    if not (new4 or new6):
        return
    with _lock:
        changed = new4 != _allowed["v4"] or new6 != _allowed["v6"]
        _allowed["v4"], _allowed["v6"] = new4, new6
    if changed:
        print("[TUNNEL] allowed home address updated")   # not printed in full: the Terminal tab is readable by every account


def _is_allowed(ip):
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    with _lock:
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr) in _allowed["v4"]
        return any(addr in ipaddress.IPv6Network(n) for n in _allowed["v6"])


class _Gate:
    """WSGI wrapper that runs before Flask (and before the sign-in check)."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        xff = environ.get("HTTP_X_FORWARDED_FOR")
        if xff and environ.get("REMOTE_ADDR") in ("127.0.0.1", "::1"):
            client = xff.split(",")[-1].strip()        # the last entry is the one ngrok added
            with _lock:
                public = _state["public"] and _state["state"] == "on"
            if not public and not _is_allowed(client):
                body = b"Not found"
                start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
                return [body]
            environ["REMOTE_ADDR"] = client
            if environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[-1].strip() == "https":
                environ["wsgi.url_scheme"] = "https"     # so the session cookie gets the Secure flag
        return self.wsgi(environ, start_response)


def _domain_url():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = (json.load(f).get("domain") or "").strip()
    except Exception:
        d = ""
    d = (d or DEFAULT_DOMAIN).replace("https://", "").replace("http://", "").strip("/")
    return "https://" + d


def status():
    """What the Admin tab shows. The address is always the fixed one; `state` says whether it is reachable."""
    with _lock:
        st = dict(_state)
    st["on"] = st["state"] == "on"
    st["address"] = st["url"] or _domain_url()
    return st


def _set(state, url="", error=""):
    with _lock:
        _state.update(state=state, url=url, error=error)
        if state != "on":
            _state["public"] = False           # fail safe: a closed or failed tunnel never stays "public"


def _watch(stop):
    while not stop.wait(REFRESH_SECONDS):
        _refresh()


def _close():
    """Shut the tunnel and the ngrok agent completely, and forget the allowed address."""
    _stop.set()
    with _lock:
        _allowed["v4"], _allowed["v6"] = set(), set()      # fail safe: nothing passes the gate while off
    try:
        from pyngrok import ngrok
        ngrok.kill()
    except Exception:
        pass


def set_public(on):
    """Allow (or stop allowing) visitors from outside your home connection. Only works while the tunnel is on."""
    with _lock:
        if _state["state"] != "on":
            return False
        _state["public"] = bool(on)
    print("[TUNNEL] open to anyone with the link" if on else "[TUNNEL] home connection only")
    return True


def disable():
    """Switch the tunnel off. Never raises."""
    with _op:
        _close()
        _set("off")
        print("[TUNNEL] off")


def stop():
    """Used by the EXIT button / window close."""
    _close()


def _open(app):
    global _gate_installed, _cleanup_added, _stop
    with _op:
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            _set("error", error="Create instance/ngrok.json with your authtoken first.")
            print("[TUNNEL] cannot start: instance/ngrok.json is missing")
            return
        except Exception as exc:
            _set("error", error="Could not read instance/ngrok.json.")
            print("[TUNNEL] could not read instance/ngrok.json:", exc)
            return
        token = (cfg.get("authtoken") or "").strip()
        if not token:
            _set("error", error="No authtoken in instance/ngrok.json.")
            print("[TUNNEL] cannot start: no authtoken in instance/ngrok.json")
            return
        try:
            from pyngrok import ngrok
        except ImportError:
            _set("error", error="Run  pip install pyngrok  and restart.")
            print("[TUNNEL] cannot start: run  pip install pyngrok  and restart")
            return

        if not _gate_installed:
            app.wsgi_app = _Gate(app.wsgi_app)     # in place before the tunnel opens, so nothing is reachable early
            _gate_installed = True
        if not _cleanup_added:
            try:
                import termlog
                termlog.add_cleanup(stop)          # EXIT button and window close shut the tunnel down too
                _cleanup_added = True
            except Exception:
                pass

        _stop = threading.Event()
        _refresh()
        with _lock:
            have_ip = bool(_allowed["v4"] or _allowed["v6"])
        if not have_ip:
            _set("error", error="Could not find this PC's public IP, so the tunnel stayed closed.")
            print("[TUNNEL] could not find the public IP: tunnel stays closed")
            return
        threading.Thread(target=_watch, args=(_stop,), daemon=True, name="tunnel-watch").start()
        try:
            try:
                ngrok.set_auth_token(token)
            except Exception as exc:
                if "already set" not in str(exc).lower():          # an earlier run saved it: not a problem
                    print("[TUNNEL] auth token warning:", str(exc)[:160])
            host = _domain_url().replace("https://", "")
            t = None
            for kw in ({"url": "https://" + host}, {"domain": host}):   # newer agents say "url", older say "domain"
                try:
                    t = ngrok.connect(PORT, "http", **kw)
                    break
                except Exception as exc:
                    print("[TUNNEL] retrying with another setting:", str(exc)[:160])
            if t is None:
                _close()
                _set("error", error="ngrok could not start (try: pip install --upgrade pyngrok).")
                print("[TUNNEL] could not start ngrok: no setting worked")
                return
            _set("on", url=t.public_url)
            print("[TUNNEL] online:", t.public_url)
        except Exception as exc:
            _close()
            _set("error", error="ngrok could not start.")
            print("[TUNNEL] could not start ngrok:", exc)


def enable(app):
    """Switch the tunnel on (in the background). Never raises. Does nothing if it is already starting or on."""
    with _lock:
        if _state["state"] in ("starting", "on"):
            return
        _state.update(state="starting", url="", error="", public=False)
    threading.Thread(target=_open, args=(app,), daemon=True, name="tunnel-open").start()

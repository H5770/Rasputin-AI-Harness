"""lan.py - use Rasputin from a phone on your home Wi-Fi (no VPN, nothing leaves your network).

What it adds (the normal local window on 127.0.0.1:5000 is untouched):
  https://<pc-name>.local:5443   the same app over your Wi-Fi, encrypted. THIS is the address to put on the phone:
                                 it keeps working when the router hands the PC a new IP address.
  https://<pc-ip>:5443           same thing by number (changes whenever the router changes the PC's IP)
  http://<pc-name>.local:5080    a tiny page that only hands out the public certificate (rasputin-ca.crt)
                                 so the phone can trust the connection

Signing in is handled by accounts.py (username + password). The old PIN is gone.

Why HTTPS: phone browsers refuse the microphone on plain http:// pages. The PC makes its own little certificate
authority the first time, signs a certificate with it, and you install the authority on the phone once.
Needs:  pip install cryptography

Stability work in here:
  * the certificate covers the PC's name AND every address the PC has, and is re-issued on the fly if an address changes
  * TLS handshakes happen in the worker thread, so one half-open phone connection can't freeze everyone else
  * each server restarts itself if it ever crashes
  * the PC is asked not to go to sleep while Rasputin runs (Windows)

Files live in instance/lan/ (keep ca.key private).
"""
import datetime
import ipaddress
import os
import socket
import ssl
import sys
import threading
import time
import traceback

HTTPS_PORT = 5443
CA_PORT = 5080
LOCAL_URL = "https://192.168.4.129:5443"   # the address shown first under Admin > Web Addresses
KEEP_PC_AWAKE = True            # Windows: stop the PC from sleeping while Rasputin is open (the screen may still turn off)
CHECK_EVERY = 20                # seconds between "did the PC's address change?" checks
IDLE_TIMEOUT = 120              # seconds a phone connection may sit silent before it is dropped
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(BASE, "instance", "lan")

try:
    import psutil
except Exception:
    psutil = None

STATE = {"running": False, "ips": [], "hostname": ""}
_log_times = {}


def _p(name):
    return os.path.join(DIR, name)


def _log_once(key, msg, every=60):
    """Print a line at most once per `every` seconds for the same key (keeps the Terminal tab readable)."""
    now = time.time()
    if now - _log_times.get(key, 0) >= every:
        _log_times[key] = now
        print(msg)


def hostname():
    return (socket.gethostname().split(".")[0] or "pc").lower()


def lan_ip():
    """This PC's address on your Wi-Fi/router (no traffic is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def local_ips():
    """Every usable IPv4 address of this PC, the one the router uses first."""
    out = []

    def add(ip):
        if ip and not ip.startswith(("127.", "169.254.", "0.")) and ip not in out:
            out.append(ip)

    add(lan_ip())
    if psutil:
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET:
                        add(a.address)
        except Exception:
            pass
    else:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(info[4][0])
        except Exception:
            pass
    return out


def info():
    """What the Admin tab shows: the addresses to use on a phone."""
    ips = STATE["ips"] or local_ips()
    host = STATE["hostname"] or hostname()
    return {
        "running": STATE["running"], "hostname": host, "ips": ips,
        "https_port": HTTPS_PORT, "ca_port": CA_PORT,
        "local_url": LOCAL_URL,
        "urls": [f"https://{host}.local:{HTTPS_PORT}"] + [f"https://{ip}:{HTTPS_PORT}" for ip in ips],
        "setup_urls": [f"http://{host}.local:{CA_PORT}"] + [f"http://{ip}:{CA_PORT}" for ip in ips],
        # who each address is for (same order as "urls"): iPhone/Mac resolve .local names, Android browsers do not
        "labels": ["IPHONE / IPAD / MAC"] + ["ANDROID (CHANGES WITH THE ROUTER)"] * len(ips),
    }


# ── certificates ─────────────────────────────────────────────────────────────
def _ensure_certs(ips):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    os.makedirs(DIR, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def save_key(key, path):
        with open(path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))

    def save_cert(cert, path):
        with open(path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    # certificate authority (made once, valid 10 years)
    if not (os.path.isfile(_p("ca.key")) and os.path.isfile(_p("ca.crt"))):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rasputin Local CA")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        save_key(key, _p("ca.key"))
        save_cert(cert, _p("ca.crt"))
        print("[LAN] created a new local certificate authority")

    # server certificate: redo it if missing, expiring soon, or this PC's name / addresses are not all on it
    host = hostname()
    want_ips = {"127.0.0.1", *ips}
    want_names = {"localhost", host, host + ".local"}
    need = True
    if os.path.isfile(_p("server.crt")) and os.path.isfile(_p("server.key")):
        try:
            with open(_p("server.crt"), "rb") as f:
                cur = x509.load_pem_x509_certificate(f.read())
            san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            have_names = set(san.get_values_for_type(x509.DNSName))
            left = cur.not_valid_after_utc - now
            need = not (want_ips <= have_ips and want_names <= have_names) or left < datetime.timedelta(days=30)
        except Exception:
            need = True
    if need:
        with open(_p("ca.key"), "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), None)
        with open(_p("ca.crt"), "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        key = ec.generate_private_key(ec.SECP256R1())
        sans = [x509.IPAddress(ipaddress.ip_address(a)) for a in sorted(want_ips)]
        sans += [x509.DNSName(n) for n in sorted(want_names)]
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rasputin")]))
                .issuer_name(ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=800))          # iPhone rejects anything over 825 days
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        save_key(key, _p("server.key"))
        save_cert(cert, _p("server.crt"))
        print(f"[LAN] made a certificate for {host}.local and {', '.join(sorted(want_ips - {'127.0.0.1'})) or 'this PC'}")


# ── servers ──────────────────────────────────────────────────────────────────
CA_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rasputin setup</title><style>
body{background:#1a0505;color:#ff4d4d;font-family:'Courier New',monospace;padding:20px;line-height:1.5;max-width:560px;margin:0 auto}
a{color:#fff}.big{font-size:20px;display:block;margin:6px 0 14px;word-break:break-all}
.card{border:1px solid #5a1a1a;border-radius:10px;padding:12px 16px;margin:14px 0}
.dim{opacity:.7;font-size:13px}.tag{font-size:11px;letter-spacing:1px;border:1px solid #ff4d4d;border-radius:99px;padding:1px 8px}
</style></head><body>
<h2>RASPUTIN // PHONE SETUP</h2>
<p>Detected: <b>__DEVICE__</b></p>

<div class="card"><span class="tag">STEP 1</span>
<a class="big" href="/rasputin-ca.crt">Download the certificate</a>
__STEPS__
</div>

<div class="card"><span class="tag">STEP 2</span>
<p>Open this address, sign in with your Rasputin account, then add it to your Home Screen:</p>
<a class="big" href="__MAIN_URL__">__MAIN_URL__</a>
__QR__
<p class="dim">__ALT__</p></div>

<p class="dim">This page only serves the public certificate. It contains no secrets.</p></body></html>"""

STEPS_IOS = """<p>Tap <b>Allow</b>, then Settings &gt; <b>Profile Downloaded</b> &gt; Install. Then Settings &gt; General &gt; About &gt;
<b>Certificate Trust Settings</b> &gt; turn on <b>Rasputin Local CA</b>. Use <b>Safari</b>.</p>"""
STEPS_ANDROID = """<p>Open the downloaded file, or go to Settings &gt; Security (or Security &amp; privacy) &gt; More security settings &gt;
Encryption &amp; credentials &gt; <b>Install a certificate</b> &gt; <b>CA certificate</b> &gt; Install anyway, and pick
<b>rasputin-ca.crt</b>. Android needs a screen lock set for this. Use <b>Chrome</b> (Firefox ignores user certificates by default).</p>"""


def _host_only(h):
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]")[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _qr_svg(url):
    """Optional QR code (pip install segno). Returns "" if segno is missing."""
    try:
        import segno
        svg = segno.make(url, error="m").svg_inline(scale=5, dark="#ffffff", light="#1a0505", border=2)
        return '<div style="margin:8px 0;background:#1a0505;max-width:240px">' + svg + "</div>"
    except Exception:
        return ""


def _ca_app():
    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/rasputin-ca.crt":
            with open(_p("ca.crt"), "rb") as f:
                body = f.read()
            start_response("200 OK", [("Content-Type", "application/x-x509-ca-cert"),
                                      ("Content-Disposition", 'attachment; filename="rasputin-ca.crt"'),
                                      ("Content-Length", str(len(body)))])
            return [body]
        ips = STATE["ips"] or local_ips()
        name = STATE["hostname"] or hostname()
        ua = (environ.get("HTTP_USER_AGENT") or "").lower()
        came_from = _host_only(environ.get("HTTP_HOST"))
        android = "android" in ua
        apple = any(k in ua for k in ("iphone", "ipad", "ipod", "macintosh"))
        # Android browsers cannot resolve <name>.local, so they always get a number. Apple devices get the
        # name that survives router changes. Anything else gets whichever address it used to reach this page.
        by_name = f"https://{name}.local:{HTTPS_PORT}"
        reached_ip = came_from if _is_ip(came_from) else ""
        by_num = f"https://{reached_ip or (ips[0] if ips else '127.0.0.1')}:{HTTPS_PORT}"
        if android:
            main, device, steps = by_num, "Android", STEPS_ANDROID
            alt = ("This is a number address: if your router gives the PC a new one, open the setup page again. "
                   "A fixed address for the PC (DHCP reservation in the router) stops that from ever happening.")
        elif apple:
            main, device, steps = by_name, "iPhone / iPad / Mac", STEPS_IOS
            alt = f"If that name does not open, use {by_num} instead."
        else:
            main = by_name if not reached_ip else by_num
            device, steps = "Other device", STEPS_IOS + STEPS_ANDROID
            alt = f"Android phones need the number address: {by_num}"
        body = (CA_PAGE.replace("__DEVICE__", device).replace("__STEPS__", steps)
                .replace("__MAIN_URL__", main).replace("__QR__", _qr_svg(main)).replace("__ALT__", alt).encode())
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]
    return app


def _quiet_errors(server):
    """A phone that drops a connection or does not trust the certificate is normal: one short line, not a traceback."""
    def handle_error(request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):                        # ssl.SSLError, resets and timeouts are all OSError
            reason = getattr(exc, "reason", None) or type(exc).__name__
            _log_once(("conn", client_address[0], str(reason)), f"[LAN] {client_address[0]}: connection dropped ({reason})")
        else:
            traceback.print_exc()
    server.handle_error = handle_error


def _serve_forever(name, build):
    """Keep one server alive: if it cannot start (port busy) or ever crashes, wait and start it again."""
    def loop():
        while True:
            srv = None
            try:
                srv = build()
                STATE["running"] = True
                srv.serve_forever()
            except Exception as exc:
                _log_once(("srv", name), f"[LAN] {name} stopped ({exc}); restarting")
            finally:
                if srv is not None:
                    try:
                        srv.server_close()
                    except Exception:
                        pass
            time.sleep(3)
    threading.Thread(target=loop, daemon=True, name="lan-" + name).start()


def _watch(ctx):
    """Every few seconds: keep the PC awake, and if its addresses changed, re-issue the certificate."""
    last = tuple(STATE["ips"])
    while True:
        if KEEP_PC_AWAKE and os.name == "nt":
            try:    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED; it lasts as long as this thread, so it is renewed here
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            except Exception:
                pass
        time.sleep(CHECK_EVERY)
        try:
            ips = tuple(local_ips())
            if ips and ips != last:
                print("[LAN] this PC's address changed:", ", ".join(ips))
                _ensure_certs(ips)
                ctx.load_cert_chain(_p("server.crt"), _p("server.key"))     # new connections use it at once
                STATE["ips"] = list(ips)
                last = ips
        except Exception as exc:
            _log_once("watch", f"[LAN] address check failed: {exc}")


def start(app):
    """Call once after the Flask app is created and before it starts serving. Never raises."""
    try:
        from werkzeug.serving import WSGIRequestHandler, make_server
        ips = local_ips()
        _ensure_certs(ips)
        STATE.update(ips=ips, hostname=hostname())
        for old in ("lan_pin.txt", "lan_secret.txt"):               # the old PIN login is gone; its files are not needed
            try:
                os.remove(_p(old))
            except OSError:
                pass

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(_p("server.crt"), _p("server.key"))

        class Handler(WSGIRequestHandler):
            timeout = IDLE_TIMEOUT          # a silent connection is dropped instead of holding a thread forever

        def build_https():
            srv = make_server("0.0.0.0", HTTPS_PORT, app, threaded=True, request_handler=Handler)
            # Wrap the socket ourselves WITHOUT handshaking on accept: werkzeug's own ssl_context= does the
            # handshake inside accept(), so one phone that connects and says nothing would stall every other phone.
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
            srv.ssl_context = ctx           # tells werkzeug/Flask the requests are https
            _quiet_errors(srv)
            return srv

        def build_ca():
            srv = make_server("0.0.0.0", CA_PORT, _ca_app(), threaded=True)
            _quiet_errors(srv)
            return srv

        _serve_forever("https", build_https)
        _serve_forever("setup page", build_ca)
        threading.Thread(target=_watch, args=(ctx,), daemon=True, name="lan-watch").start()

        host = hostname()
        print(f"[LAN] iPhone / iPad / Mac: https://{host}.local:{HTTPS_PORT}   (survives address changes)")
        for ip in ips:
            print(f"[LAN] Android (by number): https://{ip}:{HTTPS_PORT}")
        print(f"[LAN] first-time setup on the phone: http://{host}.local:{CA_PORT}  (or http://{ips[0] if ips else '?'}:{CA_PORT})")
    except ImportError:
        print("[LAN] phone access is off: run  pip install cryptography  and restart")
    except Exception as exc:
        print("[LAN] phone access not started:", exc)

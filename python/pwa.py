"""pwa.py - lets Rasputin be installed as an app on a phone (iPhone and Android).

Adds three things to the Flask app and changes nothing else:
  /manifest.webmanifest  name, colours and icons (this is what makes "Install app" / "Add to Home Screen" work)
  /sw.js                 a tiny service worker (must live at the site root so it covers the whole app)
  /pwa/<icon>.png        the app icons, from static/pwa/

The service worker does NOT cache the app, so you always get the current version.
If the PC can't be reached, the phone shows a short "can't be reached" page that retries by itself every few
seconds and, if the app was installed from an address that no longer works, points to the PC's stable
<pc-name>.local address.
"""
import json
import os

from flask import Response, abort, jsonify, send_from_directory

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "pwa")

MANIFEST = {
    "name": "Rasputin",
    "short_name": "Rasputin",
    "description": "Rasputin, your local AI.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#1a0505",
    "theme_color": "#1a0505",
    "icons": [
        {"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SW_JS = r"""
const ALT = __ALT__;                 // the PC's stable name, e.g. https://my-pc.local:5443
const WAIT_MS = 12000;               // a page that has not started loading after this long counts as unreachable

function offlinePage() {
  const here = self.location.origin;
  const alt = ALT && ALT !== here
    ? '<p style="opacity:.85;font-size:14px;line-height:1.5">If your router gave the PC a new address, open <a style="color:#fff" href="' + ALT + '">' + ALT +
      '</a> and add Rasputin to your Home Screen again. That name does not change.</p>'
    : '';
  const html = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rasputin</title>' +
    '<body style="background:#1a0505;color:#ff4d4d;font-family:monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:24px;box-sizing:border-box">' +
    '<div style="max-width:420px"><h2 style="letter-spacing:2px">RASPUTIN CAN\'T BE REACHED</h2>' +
    '<p id="s">Trying again automatically...</p>' +
    '<p style="opacity:.75;font-size:14px;line-height:1.5">The PC may be off or asleep, Rasputin may be closed, or this phone is not on the home Wi-Fi.</p>' +
    alt + '<p><a style="color:#ff4d4d" href="/">TRY NOW</a></p></div>' +
    '<script>setInterval(function(){fetch("/health",{cache:"no-store"}).then(function(r){if(r.ok)location.replace("/")}).catch(function(){})},4000)</script>';
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  // Network only (nothing is cached). Only page loads get the "can't be reached" page.
  if (e.request.mode === 'navigate') {
    const limit = new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), WAIT_MS));
    e.respondWith(Promise.race([fetch(e.request), limit]).catch(() => offlinePage()));
  }
});
"""


def _stable_origin():
    try:
        import lan
        return f"https://{lan.hostname()}.local:{lan.HTTPS_PORT}"
    except Exception:
        return ""


def register(app):
    @app.route("/manifest.webmanifest")
    def _pwa_manifest():
        r = jsonify(MANIFEST)
        r.headers["Content-Type"] = "application/manifest+json"
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.route("/sw.js")
    def _pwa_sw():
        body = SW_JS.replace("__ALT__", json.dumps(_stable_origin()))
        return Response(body, mimetype="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.route("/pwa/<path:name>")
    def _pwa_icon(name):
        if not name.endswith(".png"):
            abort(404)
        return send_from_directory(_DIR, name, max_age=86400)

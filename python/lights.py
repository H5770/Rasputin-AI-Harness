"""lights.py - local-network control of Philips Hue and WiZ bulbs for the Home tab and the AI.

Everything here stays on your own Wi-Fi / LAN:
  * WiZ bulbs   are driven with small UDP messages straight to the bulb (port 38899).
  * Hue lights  are driven through the Hue Bridge's local REST API on your LAN.
  * Every address is checked to be a private LAN address before anything is sent, and the HTTP
    client ignores system proxies, so nothing can be routed out to the internet.

Setup that the bulbs themselves need (done once, in their own apps):
  * WiZ:  WiZ app -> Settings -> Security -> turn on "Allow local communication".
  * Hue:  the lights must be paired to a Hue Bridge (Hue bulbs talk Zigbee to the bridge, not Wi-Fi).

Public pieces:
  register_lights(app)   adds the /home/... routes used by the Home tab
  handle_text(text)      "turn off the kitchen light", "make the bedroom blue" -> spoken reply, or None
                         if the message is not a lighting command (so the normal chat model answers)

State lives in instance/lights.json: bridge address + key, WiZ bulbs, rooms, room assignments, names.
"""
from __future__ import annotations

import colorsys
import http.client
import ipaddress
import json
import math
import os
import random
import re
import socket
import ssl
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil
except Exception:          # only used to find the subnet broadcast addresses
    psutil = None

from flask import jsonify, request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(BASE_DIR, "instance", "lights.json")
WIZ_PORT = 38899
UNASSIGNED = "Unassigned"


# ── LAN-only guard and plain HTTP helper ─────────────────────────────────────
def _host(addr) -> str:
    a = str(addr or "").strip()
    return a.rsplit(":", 1)[0] if a.count(":") == 1 else a     # "ip:port" is allowed (used by the tests)


def require_lan(addr) -> str:
    """Raise unless addr is a private / link-local IP address. Names and public addresses are refused."""
    try:
        ip = ipaddress.ip_address(_host(addr))
    except ValueError:
        raise ValueError("Enter an IP address such as 192.168.1.50.")
    if not (ip.is_private or ip.is_link_local):
        raise ValueError("That address is not on your local network. This panel only talks to devices on your own Wi-Fi.")
    return str(addr).strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_ctx = ssl.create_default_context()
_ctx.check_hostname = False                 # the bridge uses a self-signed certificate
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                      urllib.request.HTTPSHandler(context=_ctx))


def _http_json(method, url, body=None, timeout=2.5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


_NET_ERRORS = (OSError, ValueError, http.client.HTTPException)


# ── Saved configuration ──────────────────────────────────────────────────────
_cfg_lock = threading.RLock()
_cfg = None


def _load() -> dict:
    global _cfg
    with _cfg_lock:
        if _cfg is None:
            try:
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    _cfg = json.load(f)
            except Exception:
                _cfg = {}
            _cfg.setdefault("hue", {})        # ip, user, scheme, name
            _cfg.setdefault("wiz", {})        # mac -> {ip, module}
            _cfg.setdefault("rooms", [])      # rooms the user made (ordered)
            _cfg.setdefault("assign", {})     # light id -> room ("" = unassigned)
            _cfg.setdefault("names", {})      # light id -> custom name
        return _cfg


def _save():
    with _cfg_lock:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cfg, f, indent=2)
        os.replace(tmp, CFG_PATH)


# ── Colour helpers ───────────────────────────────────────────────────────────
def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def from_hex(s):
    s = str(s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError("bad colour")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _kelvin_rgb(k):
    """Approximate swatch colour for a white temperature."""
    t = max(1000.0, min(40000.0, float(k))) / 100.0
    if t <= 66:
        r, g = 255, 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r, g = 329.698727446 * ((t - 60) ** -0.1332047592), 288.1221695283 * ((t - 60) ** -0.0755148492)
    if t >= 66:
        b = 255
    elif t <= 19:
        b = 0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0, min(255, int(v))) for v in (r, g, b))


def _xy_rgb(x, y):
    if y <= 0:
        return (255, 255, 255)
    Y = 1.0
    X, Z = (Y / y) * x, (Y / y) * (1 - x - y)
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    r, g, b = max(r, 0), max(g, 0), max(b, 0)
    m = max(r, g, b) or 1
    r, g, b = r / m, g / m, b / m
    gam = lambda c: 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return tuple(int(max(0, min(1, gam(c))) * 255) for c in (r, g, b))


# ── WiZ (UDP, straight to the bulb) ──────────────────────────────────────────
def _wiz_call(ip, payload, timeout=0.8, tries=2):
    """Send one JSON message to a WiZ bulb and return its JSON reply, or None if it did not answer."""
    try:
        require_lan(ip)
    except ValueError:
        return None
    data = json.dumps(payload).encode()
    port = int(str(ip).rsplit(":", 1)[1]) if str(ip).count(":") == 1 else WIZ_PORT
    for _ in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(data, (_host(ip), port))
            raw, _addr = s.recvfrom(4096)
            return json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        finally:
            s.close()
    return None


def _local_ipv4s():
    out = []
    if psutil:
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET and a.address and not a.address.startswith(("127.", "169.254.")):
                        out.append((a.address, getattr(a, "broadcast", None)))
        except Exception:
            pass
    return out


def wiz_discover(wait=2.2) -> dict:
    """Broadcast on the LAN and return {mac: ip} for every WiZ bulb that answers."""
    targets = {"255.255.255.255"} | {b for _a, b in _local_ipv4s() if b}
    msgs = [json.dumps({"method": "getPilot", "params": {}}).encode(),
            json.dumps({"method": "registration", "params": {"phoneMac": "AAAAAAAAAAAA", "register": False,
                                                              "phoneIp": "1.2.3.4", "id": "1"}}).encode()]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    found, end, last = {}, time.time() + wait, 0.0
    try:
        while time.time() < end:
            if time.time() - last > 0.7:
                for t in targets:
                    for m in msgs:
                        try:
                            s.sendto(m, (t, WIZ_PORT))
                        except OSError:
                            pass
                last = time.time()
            try:
                raw, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                mac = (json.loads(raw.decode("utf-8", "replace")).get("result") or {}).get("mac")
            except ValueError:
                continue
            if mac:
                found[str(mac).lower()] = addr[0]
    finally:
        s.close()
    return found


def _wiz_register(mac, ip):
    """Remember a bulb (and its module name, which tells us whether it does colour). Returns True if it is new."""
    cfg = _load()
    mac = str(mac).lower()
    new = mac not in cfg["wiz"]
    entry = cfg["wiz"].setdefault(mac, {})
    entry["ip"] = ip
    if not entry.get("module"):
        r = _wiz_call(ip, {"method": "getSystemConfig", "params": {}})
        entry["module"] = ((r or {}).get("result") or {}).get("moduleName", "")
    _save()
    return new


def _wiz_caps(module):
    m = (module or "").upper()
    if "SOCKET" in m or "PLUG" in m:
        return {"color": False, "ct": False, "dim": False}
    if "RGB" in m:
        return {"color": True, "ct": True, "dim": True}
    if "TW" in m:
        return {"color": False, "ct": True, "dim": True}
    if "DW" in m:
        return {"color": False, "ct": False, "dim": True}
    return {"color": True, "ct": True, "dim": True}     # unknown module: assume a full-colour bulb


def _wiz_light(mac, info, pilot, live):
    caps = _wiz_caps(info.get("module"))
    L = {"id": "wiz:" + mac, "kind": "wiz", "default_name": "WiZ bulb " + mac[-4:].upper(),
         "model": info.get("module") or "WiZ", "ip": info.get("ip", ""), "caps": caps,
         "on": False, "bri": 100, "color": None, "kelvin": None, "mode": "off", "reachable": True,
         "hue_room": None}
    if not live:
        return L
    res = (pilot or {}).get("result") if pilot else None
    if not res:
        L["reachable"] = False
        return L
    L["on"] = bool(res.get("state"))
    L["bri"] = int(res.get("dimming", 100))
    if all(k in res for k in ("r", "g", "b")) and (res.get("r") or res.get("g") or res.get("b")):
        L["mode"], L["color"] = "color", _hex((res["r"], res["g"], res["b"]))
    elif "temp" in res:
        L["mode"], L["kelvin"] = "ct", int(res["temp"])
        L["color"] = _hex(_kelvin_rgb(res["temp"]))
    else:
        L["mode"] = "scene"
    return L


def _wiz_set(L, on, bri, rgb, kelvin):
    ip = L["ip"]
    params = {}
    if on is False:
        params["state"] = False
    else:
        params["state"] = True
        if bri is not None:
            params["dimming"] = max(10, min(100, int(bri)))
        if rgb:
            params.update(r=int(rgb[0]), g=int(rgb[1]), b=int(rgb[2]))
        elif kelvin:
            params["temp"] = max(2200, min(6500, int(kelvin)))
    r = _wiz_call(ip, {"method": "setPilot", "params": params})
    if r is None:
        return False, "unreachable"
    if (r.get("result") or {}).get("success"):
        return True, ""
    return False, "refused"


# ── Hue (Bridge, local REST) ─────────────────────────────────────────────────
_hue_lock = threading.Lock()
_hue = {"t": 0.0, "lights": {}, "groups": {}, "err": None, "err_t": 0.0}


def _hue_call(method, path, body=None, timeout=2.5, authed=True, ip=None):
    h = _load()["hue"]
    ip = ip or h.get("ip")
    if not ip:
        raise RuntimeError("No Hue Bridge address is saved.")
    require_lan(ip)
    if authed and not h.get("user"):
        raise RuntimeError("The Hue Bridge is not paired yet.")
    prefix = ("/api/" + h["user"]) if authed else "/api"
    first = h.get("scheme", "http")
    last = None
    for scheme in (first, "https" if first == "http" else "http"):
        try:
            out = _http_json(method, f"{scheme}://{ip}{prefix}{path}", body, timeout)
            if authed and scheme != h.get("scheme"):
                h["scheme"] = scheme
                _save()
            return out
        except _NET_ERRORS as exc:
            last = exc
    raise RuntimeError(f"Could not reach the Hue Bridge at {ip} ({last}).")


def _hue_state(force=False):
    h = _load()["hue"]
    if not (h.get("ip") and h.get("user")):
        return None
    with _hue_lock:
        now = time.time()
        if not force:
            if _hue["err"] and now - _hue["err_t"] < 15:
                return _hue                      # bridge was unreachable a moment ago: don't hammer it
            if not _hue["err"] and now - _hue["t"] < 1.5:
                return _hue
        try:
            lights = _hue_call("GET", "/lights")
            groups = _hue_call("GET", "/groups")
            if isinstance(lights, list):
                raise RuntimeError("The Hue Bridge rejected the saved key. Use Forget, then connect again.")
            _hue.update(t=now, lights=lights, groups=groups if isinstance(groups, dict) else {}, err=None)
        except Exception as exc:
            _hue.update(err=str(exc), err_t=now, lights={}, groups={})
        return _hue


def _hue_light(lid, d, room):
    st = d.get("state") or {}
    t = (d.get("type") or "").lower()
    color = "color light" in t
    ct = "temperature" in t or "extended color" in t
    dim = "dimmable" in t or color or ct
    L = {"id": "hue:" + lid, "kind": "hue", "default_name": d.get("name") or ("Hue light " + lid),
         "model": d.get("productname") or d.get("modelid") or "Hue", "ip": "",
         "caps": {"color": color, "ct": ct, "dim": dim}, "on": bool(st.get("on")),
         "bri": int(round(st.get("bri", 254) / 254 * 100)) if dim else 100,
         "color": None, "kelvin": None, "mode": "white", "reachable": bool(st.get("reachable", True)),
         "hue_room": room}
    mode = st.get("colormode")
    try:
        if mode == "ct" and st.get("ct"):
            k = 1e6 / st["ct"]
            L["mode"], L["kelvin"], L["color"] = "ct", int(round(k)), _hex(_kelvin_rgb(k))
        elif mode == "hs" and "hue" in st:
            L["mode"], L["color"] = "color", _hex([c * 255 for c in colorsys.hsv_to_rgb(st["hue"] / 65535, st.get("sat", 254) / 254, 1)])
        elif mode == "xy" and st.get("xy"):
            L["mode"], L["color"] = "color", _hex(_xy_rgb(*st["xy"]))
    except Exception:
        pass
    return L


def _hue_set(L, on, bri, rgb, kelvin):
    body = {}
    if on is False:
        body["on"] = False
    else:
        body["on"] = True
        if bri is not None:
            body["bri"] = max(1, min(254, int(round(bri * 2.54))))
        if rgb:
            h, s, _v = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
            body["hue"], body["sat"] = int(h * 65535), int(s * 254)
        elif kelvin:
            body["ct"] = max(153, min(500, int(round(1e6 / kelvin))))
    try:
        out = _hue_call("PUT", f"/lights/{L['id'].split(':', 1)[1]}/state", body)
    except RuntimeError:
        return False, "unreachable"
    _hue["t"] = 0.0                                   # force a fresh read next time
    if isinstance(out, list):
        for item in out:
            if isinstance(item, dict) and "error" in item:
                return False, "unreachable" if "unreachable" in str(item["error"]).lower() else "refused"
    return True, ""


def hue_discover(wait=2.0) -> list:
    """SSDP on the local network (multicast, never leaves the LAN). Returns bridge IPs."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: upnp:rootdevice\r\n\r\n").encode()
    socks = []
    for addr, _b in (_local_ipv4s() or [("", None)]):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.bind((addr, 0))
            s.settimeout(0.3)
            socks.append(s)
        except OSError:
            pass
    found, end, sent = [], time.time() + wait, False
    try:
        while time.time() < end:
            for s in socks:
                if not sent:
                    try:
                        s.sendto(msg, ("239.255.255.250", 1900))
                    except OSError:
                        pass
                try:
                    raw, addr = s.recvfrom(4096)
                except (socket.timeout, OSError):
                    continue
                low = raw.decode("utf-8", "replace").lower()
                if ("hue-bridgeid" in low or "ipbridge" in low) and addr[0] not in found:
                    found.append(addr[0])
            sent = True
    finally:
        for s in socks:
            s.close()
    return found


def hue_pair(ip=None):
    """One pairing attempt. Press the bridge's link button first, then call this."""
    cfg = _load()
    h = cfg["hue"]
    ip = (ip or h.get("ip") or "").strip()
    if not ip:
        return {"status": "error", "error": "Enter the Hue Bridge IP address, or press SCAN NETWORK first."}
    try:
        require_lan(ip)
        out = _hue_call("POST", "", {"devicetype": "rasputin#home"}, authed=False, ip=ip)
    except (ValueError, RuntimeError) as exc:
        return {"status": "error", "error": str(exc)}
    item = out[0] if isinstance(out, list) and out else {}
    if "success" in item:
        h.update(ip=ip, user=item["success"]["username"])
        try:
            h["name"] = _hue_call("GET", "/config", authed=False, ip=ip).get("name", "")
        except Exception:
            pass
        _save()
        _hue.update(t=0.0, err=None)
        return {"status": "ok"}
    if (item.get("error") or {}).get("type") == 101:
        return {"status": "link_button"}
    return {"status": "error", "error": (item.get("error") or {}).get("description", "The bridge refused to pair.")}


# ── Unified view of every light ──────────────────────────────────────────────
_last_rediscover = 0.0


def _maybe_rediscover():
    """A bulb that stops answering has usually just been given a new IP by the router: look for it again."""
    global _last_rediscover
    if time.time() - _last_rediscover < 90:
        return
    _last_rediscover = time.time()

    def work():
        try:
            for mac, ip in wiz_discover(2.0).items():
                if mac in _load()["wiz"] and _load()["wiz"][mac].get("ip") != ip:
                    _load()["wiz"][mac]["ip"] = ip
                    _save()
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


def snapshot(live=True) -> dict:
    """Every light with its current state. live=False skips the WiZ round trips (names/rooms/capabilities only)."""
    cfg = _load()
    lights, errors = [], []
    hue_info = {"paired": bool(cfg["hue"].get("user")), "ip": cfg["hue"].get("ip", ""),
                "name": cfg["hue"].get("name", ""), "error": None}
    hs = _hue_state()
    if hs:
        if hs["err"]:
            hue_info["error"] = hs["err"]
            errors.append("Hue: " + hs["err"])
        room_of = {}
        for g in hs["groups"].values():
            if g.get("type") == "Room":
                for lid in g.get("lights", []):
                    room_of.setdefault(str(lid), g.get("name") or "Room")
        for lid, d in hs["lights"].items():
            lights.append(_hue_light(str(lid), d, room_of.get(str(lid))))

    macs = list(cfg["wiz"])
    pilots = {}
    if live and macs:
        with ThreadPoolExecutor(max_workers=min(8, len(macs))) as ex:
            futs = {m: ex.submit(_wiz_call, cfg["wiz"][m].get("ip", ""), {"method": "getPilot", "params": {}}) for m in macs}
            pilots = {m: f.result() for m, f in futs.items()}
    for m in macs:
        lights.append(_wiz_light(m, cfg["wiz"][m], pilots.get(m), live))
    if live and any(not L["reachable"] for L in lights if L["kind"] == "wiz"):
        _maybe_rediscover()

    for L in lights:
        L["name"] = cfg["names"].get(L["id"]) or L["default_name"]
        if L["id"] in cfg["assign"]:
            L["room"] = cfg["assign"][L["id"]] or UNASSIGNED
        else:
            L["room"] = L.get("hue_room") or UNASSIGNED
        L.pop("hue_room", None)
    lights.sort(key=lambda L: (L["name"].lower(), L["id"]))

    rooms = list(cfg["rooms"])
    for L in lights:
        if L["room"] != UNASSIGNED and L["room"] not in rooms:
            rooms.append(L["room"])
    if any(L["room"] == UNASSIGNED for L in lights):
        rooms.append(UNASSIGNED)
    return {"lights": lights, "rooms": rooms, "hue": hue_info, "wiz_count": len(macs), "errors": errors}


def apply(ids, act, snap=None) -> dict:
    """Run one action on many lights. act keys: on, toggle, bri (0-100), bri_delta, rgb, kelvin.
    Returns {light id: (ok, note)}; note is "", "unreachable", "refused", "nocolor", "noct" or "nothing"."""
    need_state = bool(act.get("toggle")) or act.get("bri_delta") is not None
    if need_state:
        snap = snapshot(live=True)
    elif snap is None:
        snap = snapshot(live=False)
    by_id = {L["id"]: L for L in snap["lights"]}

    def one(lid):
        L = by_id.get(lid)
        if not L:
            return lid, (False, "refused")
        if need_state and not L["reachable"]:
            return lid, (False, "unreachable")
        on, bri, rgb, kelvin, note = act.get("on"), act.get("bri"), act.get("rgb"), act.get("kelvin"), ""
        if act.get("toggle"):
            on = not L["on"]
        if act.get("bri_delta") is not None:
            bri = max(5, min(100, (L["bri"] if L["on"] else 0) + act["bri_delta"]))
            if on is None:
                on = True
        if rgb and not L["caps"]["color"]:
            rgb, note = None, "nocolor"
        if kelvin and not L["caps"]["ct"]:
            kelvin, note = None, note or "noct"
        if bri is not None and not L["caps"]["dim"]:
            bri = None
        if on is None and bri is None and not rgb and not kelvin:
            return lid, (False, note or "refused")
        if L["kind"] == "hue":
            ok, why = _hue_set(L, on, bri, rgb, kelvin)
        else:
            ok, why = _wiz_set(L, on, bri, rgb, kelvin)
        return lid, (ok, why or note)

    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, ids))


# ── Plain-English commands (used by the chat and the voice link) ─────────────
COLORS = {
    "red": (255, 0, 0), "orange": (255, 110, 0), "yellow": (255, 220, 0), "green": (0, 255, 0),
    "lime": (150, 255, 0), "teal": (0, 200, 160), "cyan": (0, 255, 255), "aqua": (0, 255, 255),
    "turquoise": (0, 220, 200), "blue": (0, 0, 255), "indigo": (75, 0, 200), "purple": (150, 0, 255),
    "violet": (160, 60, 255), "magenta": (255, 0, 255), "pink": (255, 60, 160), "coral": (255, 80, 60),
    "gold": (255, 170, 0), "amber": (255, 140, 0), "lavender": (190, 150, 255), "crimson": (220, 10, 40),
}
_NUMWORDS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
             "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
_LIGHTWORD = re.compile(r"\b(?:lights?|lamps?|bulbs?|lighting)\b")
_VERB = re.compile(r"\b(?:turn|switch|set|make|change|dim|brighten|toggle|shut|power|put|kill|lower|raise|"
                   r"increase|decrease|flip|darken|illuminate|light\s+up)\b")
_QUESTION = re.compile(r"^(?:why|what|what's|whats|how|when|where|who|which|is|are|was|were|did|does|do|has|have)\b")
_SPLIT = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s+(?=(?:also\s+)?(?:turn|switch|set|make|change|dim|"
                    r"brighten|toggle|shut|kill|put|lower|raise)\b)")
_STOP = set("the my our all every these those any some both of a an house whole entire on off up down bright dim "
            "dimmer brighter please and then turn switch set make change lights light lamp lamps bulb bulbs to it "
            "them this that room rooms smart new main other here there in at for with white warm cool cold soft "
            "neutral daylight half full maximum max minimum min kelvin percent".split()) | set(COLORS)


def _normalise(text):
    t = str(text or "").lower().replace("’", "'").replace("“", "").replace("”", "").replace('"', "")
    t = re.sub(r"[.!?]+$", "", t.strip()).strip()
    for pat in (r"^(?:hey|ok|okay|yo)[,\s]+", r"^rasputin[,:\s]+", r"^(?:please|kindly)[,\s]+",
                r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?",
                r"^(?:go\s+ahead\s+and|i\s+(?:want|need)\s+you\s+to|i'd\s+like\s+you\s+to)\s+"):
        t = re.sub(pat, "", t)
    t = re.sub(r",?\s*(?:please|for me)$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _phrases(name, drop_suffix=True):
    n = re.sub(r"\s+", " ", name.lower().strip())
    out = [n]
    if drop_suffix:
        s = re.sub(r"\s+(?:lights?|lamps?|bulbs?)$", "", n).strip()
        if s and s != n:
            out.append(s)
    return [p for p in out if len(p) >= 2]


def _rx(phrase, allow_plural):
    tail = r"(?:'s|s|es)?" if allow_plural else ""
    return re.compile(r"(?<!\w)" + re.escape(phrase) + tail + r"(?!\w)")


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _resolve(seg, snap):
    """Find which rooms / lights a sentence names. Returns ids, a spoken label, and the sentence with names blanked out."""
    rooms = {}
    for r in snap["rooms"]:
        if r != UNASSIGNED:
            rooms[r] = [L["id"] for L in snap["lights"] if L["room"] == r]
    room_words = {p for r in rooms for p in _phrases(r, False)}
    found = []                                                       # (start, end, kind, ids, label)
    for r, ids in rooms.items():
        if ids:
            for p in _phrases(r, False):
                for m in _rx(p, True).finditer(seg):
                    found.append((m.start(), m.end(), "room", ids, r.lower()))
    for L in snap["lights"]:
        for p in _phrases(L["name"]):
            if p in room_words and p != L["name"].lower():
                continue                                             # "kitchen" means the room, not "Kitchen light"
            plural_ok = not re.search(r"(?:light|lamp|bulb)$", p)
            for m in _rx(p, plural_ok).finditer(seg):
                found.append((m.start(), m.end(), "light", [L["id"]], L["name"].lower()))
    found.sort(key=lambda f: -(f[1] - f[0]))                         # longest first; rooms win ties (added first)
    kept = []
    for f in found:
        if not any(k[0] <= f[0] and f[1] <= k[1] for k in kept):
            kept.append(f)
    kept.sort(key=lambda f: f[0])
    room_ids = {i for f in kept if f[2] == "room" for i in f[3]}
    light_ids = {i for f in kept if f[2] == "light" for i in f[3]}
    if room_ids and light_ids and light_ids <= room_ids:
        kept = [f for f in kept if f[2] == "light"]       # a room used as an adjective: "the kitchen ceiling"
    ids = []
    for f in kept:
        for i in f[3]:
            if i not in ids:
                ids.append(i)
    room_labels = [f[4] for f in kept if f[2] == "room"]
    light_labels = [f[4] for f in kept if f[2] == "light"]
    parts = []
    if room_labels:
        parts.append("the " + _join(room_labels) + " lights")
    parts += ["the " + n for n in light_labels]
    masked = seg
    for f in kept:
        masked = masked[:f[0]] + " " * (f[1] - f[0]) + masked[f[1]:]
    return {"ids": ids, "label": _join(parts) if parts else "", "plural": bool(room_labels) or len(ids) > 1,
            "masked": masked}


def _parse_action(t):
    a = {}
    if re.search(r"\b(?:toggle|flip)\b", t):
        a["toggle"] = True
    else:
        m = re.search(r"\b(on|off)\b", t)
        if m:
            a["on"] = m.group(1) == "on"
        elif re.search(r"\b(?:kill|darken)\b|\bshut\s*down\b", t):
            a["on"] = False
        elif re.search(r"\blights?\s+up\b|\billuminate\b", t):
            a["on"] = True
    for w, n in _NUMWORDS.items():
        t = re.sub(rf"\b{w}\s*(?=%|percent)", f"{n} ", t)
    m = re.search(r"(\d{1,3})\s*(?:%|percent|per cent)", t) or \
        re.search(r"\b(?:to|at)\s+(\d{1,3})\b(?!\s*(?:k\b|kelvin|degrees|:))", t)
    if m and 0 <= int(m.group(1)) <= 100:
        a["bri"] = max(1, int(m.group(1)))
    elif re.search(r"\b(?:full|maximum|max|brightest)\b", t):
        a["bri"] = 100
    elif re.search(r"\b(?:minimum|min|lowest|dimmest)\b", t):
        a["bri"] = 5
    elif re.search(r"\bhalf\b", t):
        a["bri"] = 50
    elif re.search(r"\b(?:dim|dimmer|darker|lower|decrease|turn\s+(?:it\s+|them\s+)?down)\b", t):
        a["bri_delta"] = -30
    elif re.search(r"\b(?:brighten|brighter|increase|raise|turn\s+(?:it\s+|them\s+)?up)\b", t):
        a["bri_delta"] = 30
    k = re.search(r"\b(\d{4})\s*(?:k\b|kelvin)", t)
    if re.search(r"#[0-9a-f]{6}\b", t):
        a["rgb"] = from_hex(re.search(r"#([0-9a-f]{6})\b", t).group(1))
        a["color_word"] = "that color"
    elif k:
        a["kelvin"] = max(2200, min(6500, int(k.group(1))))
        a["color_word"] = f"{a['kelvin']} kelvin"
    else:
        cm = re.search(r"\b(" + "|".join(COLORS) + r")\b", t)
        if cm:
            a["rgb"], a["color_word"] = COLORS[cm.group(1)], cm.group(1)
        elif re.search(r"\b(?:cool|cold)\s+(?:white|light|lights|tone)\b|\bdaylight\b", t):
            a["kelvin"], a["color_word"] = (6200, "daylight") if "daylight" in t else (5500, "cool white")
        elif re.search(r"\bwarm\b|\bsoft\s+white\b|\bcandle(?:light)?\b", t):
            a["kelvin"], a["color_word"] = 2700, "warm white"
        elif re.search(r"\b(?:white|neutral)\b", t):
            a["kelvin"], a["color_word"] = 4000, "white"
    return a


def _cap(s):
    return s[:1].upper() + s[1:]


def _compose(label, plural, act, results, names):
    ok = [i for i, (o, _n) in results.items() if o]
    bad = {i: n for i, (o, n) in results.items() if not o}
    if bad and ok:                                    # only some lights obeyed: name those
        who, be = "the " + _join([names[i].lower() for i in ok]), ("are" if len(ok) > 1 else "is")
    else:
        who, be = label, ("are" if plural else "is")
    out = []
    if ok:
        details = []
        if act.get("color_word"):
            details.append(act["color_word"])
        if act.get("bri") is not None:
            details.append(f"at {act['bri']} percent")
        if act.get("toggle"):
            s = f"{who} toggled."
        elif act.get("on") is False:
            s = f"{who} {be} off."
        elif details:
            s = f"{who} {be} now {' '.join(details)}."
        elif act.get("bri_delta") is not None:
            s = f"{who} {be} {'brightened' if act['bri_delta'] > 0 else 'dimmed'}."
        else:
            s = f"{who} {be} on."
        out.append(random.choice(["", "", "Done. ", "As you wish. "]) + _cap(s))
    for why, phrase in (("unreachable", "I could not reach {n}."), ("nocolor", "{n} cannot change color."),
                        ("noct", "{n} cannot change white temperature."), ("refused", "{n} did not accept the command.")):
        ns = [names[i].lower() for i in bad if bad[i] == why]
        if ns:
            out.append(phrase.format(n=_cap("the " + _join(ns))))
    return " ".join(out)


def _status(t):
    snap = snapshot(live=True)
    if not snap["lights"]:
        return None
    if re.search(r"\b(?:which|what)\b.*\b(?:lights?|lamps?|bulbs?)\b.*\b(?:on|lit)\b", t):
        on = [L["name"] for L in snap["lights"] if L["on"]]
        return ("Lit: " + _join(on) + ".") if on else "No lights are on."
    if re.match(r"^(?:is|are)\b", t) and re.search(r"\b(?:on|off|lit)\b", t):
        res = _resolve(t, snap)
        ids = res["ids"] or ([L["id"] for L in snap["lights"]] if _LIGHTWORD.search(t) else [])
        if not ids:
            return None
        by = {L["id"]: L for L in snap["lights"]}
        on = [by[i]["name"] for i in ids if by[i]["on"]]
        off = [by[i]["name"] for i in ids if not by[i]["on"]]
        who = _join(on or off) if len(ids) <= 3 else "everything you asked about"
        if not off:
            return f"Yes. {_cap("the " + who.lower() if len(ids) <= 3 else who)} {'is' if len(on) == 1 else 'are'} on."
        if not on:
            return f"No. {_cap("the " + who.lower() if len(ids) <= 3 else who)} {'is' if len(off) == 1 else 'are'} off."
        return f"On: {_join(on)}. Off: {_join(off)}."
    return None


def _do_segment(seg, snap, prev):
    has_lw = bool(_LIGHTWORD.search(seg))
    res = _resolve(seg, snap)
    act = _parse_action(res["masked"])
    if not act:
        return None
    if not snap["lights"]:
        return ("No lights are connected yet. Open the Home tab and scan the network.", None) if (has_lw or res["ids"]) else None
    ids, label, plural = res["ids"], res["label"], res["plural"]
    if not ids:
        if has_lw:
            m = re.search(r"\b([a-z']+)\s+(?:lights?|lamps?|bulbs?)\b", res["masked"])
            if m and m.group(1) not in _STOP:
                return (f"I do not know a light or room called {m.group(1)}.", None)
            ids, label, plural = [L["id"] for L in snap["lights"]], "all lights", True
        elif prev:
            ids, label, plural = prev
        else:
            return None
    results = apply(ids, {k: v for k, v in act.items() if k != "color_word"}, snap)
    names = {L["id"]: L["name"] for L in snap["lights"]}
    return _compose(label, plural, act, results, names), (ids, label, plural)


def handle_text(text):
    """Return a spoken reply if `text` is a lighting command or question, else None (the chat model answers)."""
    try:
        t = _normalise(text)
        if not t or len(t) > 240:
            return None
        is_q = bool(_QUESTION.match(t))
        has_verb = bool(_VERB.search(t)) or bool(re.search(r"\b(?:lights?|lamps?|bulbs?)\s+(?:on|off)\b", t))
        if is_q:
            return _status(t) if _LIGHTWORD.search(t) else None
        if not has_verb:
            return None
        snap = snapshot(live=False)
        replies, prev = [], None
        for seg in _SPLIT.split(t):
            r = _do_segment(seg.strip(), snap, prev)
            if r:
                replies.append(r[0])
                prev = r[1] or prev
        return " ".join(replies) or None
    except Exception:
        traceback.print_exc()
        return None


# ── Routes for the Home tab ──────────────────────────────────────────────────
def _parse_body():
    d = request.get_json(silent=True) or {}
    act = {}
    if "on" in d:
        act["on"] = bool(d["on"])
    if "toggle" in d:
        act["toggle"] = bool(d["toggle"])
    if d.get("bri") is not None:
        act["bri"] = max(1, min(100, int(d["bri"])))
    if d.get("color"):
        act["rgb"] = from_hex(d["color"])
    if d.get("kelvin"):
        act["kelvin"] = max(2200, min(6500, int(d["kelvin"])))
    return d, act


def _summary(results, snap):
    names = {L["id"]: L["name"] for L in snap["lights"]}
    bad = [names.get(i, i) + (" (not reachable)" if n == "unreachable" else " (not supported)" if n in ("nocolor", "noct") else "")
           for i, (o, n) in results.items() if not o]
    return {"ok": not bad, "message": ("Failed: " + ", ".join(bad)) if bad else ""}


def register_lights(app):
    def fail(msg, code=400):
        return jsonify({"error": str(msg)}), code

    @app.route("/home/state")
    def home_state():
        return jsonify(snapshot(live=True))

    @app.route("/home/light", methods=["POST"])
    def home_light():
        try:
            d, act = _parse_body()
        except (ValueError, TypeError) as exc:
            return fail(exc)
        snap = snapshot(live=bool(act.get("toggle")))
        return jsonify(_summary(apply([str(d.get("id"))], act, snap), snap))

    @app.route("/home/room", methods=["POST"])
    def home_room():
        try:
            d, act = _parse_body()
        except (ValueError, TypeError) as exc:
            return fail(exc)
        snap = snapshot(live=False)
        room = d.get("room")
        ids = [L["id"] for L in snap["lights"] if room in ("*", None) or L["room"] == room]
        return jsonify(_summary(apply(ids, act, snap), snap))

    @app.route("/home/assign", methods=["POST"])
    def home_assign():
        d = request.get_json(silent=True) or {}
        room = str(d.get("room") or "").strip()[:40]
        cfg = _load()
        cfg["assign"][str(d.get("id"))] = "" if room in ("", UNASSIGNED) else room
        if room and room != UNASSIGNED and room not in cfg["rooms"]:
            cfg["rooms"].append(room)
        _save()
        return jsonify({"ok": True})

    @app.route("/home/rename-light", methods=["POST"])
    def home_rename_light():
        d = request.get_json(silent=True) or {}
        name = str(d.get("name") or "").strip()[:40]
        cfg = _load()
        if name:
            cfg["names"][str(d.get("id"))] = name
        else:
            cfg["names"].pop(str(d.get("id")), None)
        _save()
        return jsonify({"ok": True})

    @app.route("/home/rooms", methods=["POST"])
    def home_rooms():
        d = request.get_json(silent=True) or {}
        action, name, new = d.get("action"), str(d.get("name") or "").strip()[:40], str(d.get("new") or "").strip()[:40]
        cfg = _load()
        lower = [r.lower() for r in cfg["rooms"]]
        if action == "add":
            if not name or name.lower() == UNASSIGNED.lower():
                return fail("Give the room a name.")
            if name.lower() in lower:
                return fail("That room already exists.")
            cfg["rooms"].append(name)
        elif action == "rename":
            if not new or new.lower() == UNASSIGNED.lower():
                return fail("Give the room a name.")
            for L in snapshot(live=False)["lights"]:
                if L["room"] == name:
                    cfg["assign"][L["id"]] = new
            cfg["rooms"] = [new if r == name else r for r in cfg["rooms"]]
            if new not in cfg["rooms"]:
                cfg["rooms"].append(new)
        elif action == "delete":
            for L in snapshot(live=False)["lights"]:
                if L["room"] == name:
                    cfg["assign"][L["id"]] = ""
            cfg["rooms"] = [r for r in cfg["rooms"] if r != name]
        else:
            return fail("Unknown action.")
        _save()
        return jsonify({"ok": True})

    @app.route("/home/scan", methods=["POST"])
    def home_scan():
        with ThreadPoolExecutor(max_workers=2) as ex:
            fw, fh = ex.submit(wiz_discover), ex.submit(hue_discover)
            wiz, bridges = fw.result(), fh.result()
        new = sum(1 for mac, ip in wiz.items() if _wiz_register(mac, ip))
        h = _load()["hue"]
        if bridges and not h.get("ip"):
            h["ip"] = bridges[0]
            _save()
        return jsonify({"wiz_found": len(wiz), "wiz_new": new, "hue_bridges": bridges})

    @app.route("/home/hue/connect", methods=["POST"])
    def home_hue_connect():
        d = request.get_json(silent=True) or {}
        return jsonify(hue_pair(d.get("ip")))

    @app.route("/home/hue/forget", methods=["POST"])
    def home_hue_forget():
        _load()["hue"].clear()
        _save()
        _hue.update(t=0.0, lights={}, groups={}, err=None)
        return jsonify({"ok": True})

    @app.route("/home/wiz/add", methods=["POST"])
    def home_wiz_add():
        ip = str((request.get_json(silent=True) or {}).get("ip") or "").strip()
        try:
            require_lan(ip)
        except ValueError as exc:
            return fail(exc)
        r = _wiz_call(ip, {"method": "getPilot", "params": {}}, timeout=1.2, tries=3)
        mac = ((r or {}).get("result") or {}).get("mac")
        if not mac:
            return fail("No WiZ bulb answered at that address. In the WiZ app, open Settings, Security, "
                        "and turn on Allow local communication.")
        return jsonify({"ok": True, "new": _wiz_register(mac, ip)})

    @app.route("/home/wiz/forget", methods=["POST"])
    def home_wiz_forget():
        lid = str((request.get_json(silent=True) or {}).get("id") or "")
        cfg = _load()
        cfg["wiz"].pop(lid.split(":", 1)[-1], None)
        cfg["assign"].pop(lid, None)
        cfg["names"].pop(lid, None)
        _save()
        return jsonify({"ok": True})

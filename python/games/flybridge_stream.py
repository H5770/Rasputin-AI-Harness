"""Live 30 fps view of the Minecraft window for the FlyBridge tab (run by flybridge.py, Windows only).

Captures the game window with Windows Graphics Capture (works while the window is covered by other windows, not minimized),
crops to the game area and serves it as an MJPEG stream:  http://<host>:<port>/stream   (health: /health)
Needs: pip install windows-capture pillow numpy
"""
import argparse
import ctypes
import io
import json
import re
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=3100)
ap.add_argument("--fps", type=float, default=30)
ap.add_argument("--width", type=int, default=960)
ap.add_argument("--quality", type=int, default=72)
A = ap.parse_args()

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

TITLE = re.compile(r"^Minecraft\*? \d+\.\d+")      # the game ("Minecraft* 1.21.4 - Singleplayer"), not "Minecraft Launcher"
cond = threading.Condition()
state = {"jpg": b"", "seq": 0, "ts": 0.0, "cap_fps": 0.0, "out_fps": 0.0}
raw = {"buf": None, "w": 0, "h": 0, "n": 0}                 # newest captured frame, handed to the encoder thread
raw_cond = threading.Condition()
counts = {"cap": 0, "out": 0, "t": time.time()}


def find_window():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if TITLE.match(buf.value):
                found.append((hwnd, buf.value))
        return True
    user32.EnumWindows(cb, 0)
    return found[0] if found else (None, None)


def client_size(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom


def move_window(hide, mode="sliver"):
    """Park the game window out of the way, or bring it back.
    sliver   = push it to the bottom-right corner of all your screens, leaving 3 px visible and send it behind everything.
               Windows still treats it as on screen, so the game keeps its full frame rate.
    offscreen = fully off every screen (Windows/Optimus can slow a window that is not on any screen).
    """
    hwnd, _ = find_window()
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 4)                                       # SW_SHOWNOACTIVATE (undo a minimize if there was one)
    if not hide:
        user32.SetWindowPos(hwnd, 0, 100, 100, 0, 0, 0x0001 | 0x0004 | 0x0010)   # NOSIZE | NOZORDER | NOACTIVATE
        return True
    if mode == "behind":                                             # stays where it is, just under every other window
        user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)    # HWND_BOTTOM, NOSIZE | NOMOVE | NOACTIVATE
        try:
            user32.SetForegroundWindow(user32.GetShellWindow())
        except Exception:
            pass
        print("[stream] window sent behind others (iconic=%d visible=%d)" % (user32.IsIconic(hwnd), user32.IsWindowVisible(hwnd)), flush=True)
        return True
    if mode == "offscreen":
        x = y = -32000
    else:
        vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # virtual screen across all monitors
        x, y = vx + vw - 3, vy + vh - 3
    user32.SetWindowPos(hwnd, 1, x, y, 0, 0, 0x0001 | 0x0010)       # HWND_BOTTOM, NOSIZE | NOACTIVATE
    try:
        user32.SetForegroundWindow(user32.GetShellWindow())          # give the mouse back to you
    except Exception:
        pass
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            ok = time.time() - state["ts"] < 3
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "cap_fps": state["cap_fps"], "out_fps": state["out_fps"]}).encode())
            return
        if self.path.startswith(("/hide", "/show")):
            ok = move_window(self.path.startswith("/hide"), "offscreen" if "offscreen" in self.path else "behind" if "behind" in self.path else "sliver")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"1" if ok else b"0")
            return
        if not self.path.startswith("/stream"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        seq = 0
        try:
            while True:
                with cond:
                    cond.wait_for(lambda: state["seq"] != seq, timeout=5)
                    seq, jpg = state["seq"], state["jpg"]
                if jpg:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
        except (OSError, ValueError):
            return


def encoder_loop():
    """JPEG-encodes the newest frame on its own thread so the capture callback never stalls (older frames are dropped)."""
    n = 0
    while True:
        with raw_cond:
            raw_cond.wait_for(lambda: raw["n"] != n)
            n, buf, w, h = raw["n"], raw["buf"], raw["w"], raw["h"]
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        if w > A.width:
            img = img.resize((A.width, max(1, h * A.width // w)), Image.BILINEAR)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=A.quality)
        with cond:
            state["jpg"], state["ts"] = out.getvalue(), time.time()
            state["seq"] += 1
            cond.notify_all()
        counts["out"] += 1


def capture_loop():
    from windows_capture import WindowsCapture
    min_gap = 1.0 / A.fps
    while True:
        hwnd, title = find_window()
        if not hwnd:
            time.sleep(1)
            continue
        print("[stream] capturing window: %s" % title, flush=True)
        try:
            try:
                cap = WindowsCapture(cursor_capture=False, draw_border=False, window_name=title)
            except TypeError:
                cap = WindowsCapture(window_name=title)
            last = [0.0, 0.0]                                               # last frame time, last title check

            @cap.event
            def on_frame_arrived(frame, control):
                now = time.time()
                counts["cap"] += 1                                          # every frame the game window presents = its real fps
                if now - counts["t"] >= 1.0:
                    dt = now - counts["t"]
                    state["cap_fps"], state["out_fps"] = round(counts["cap"] / dt, 1), round(counts["out"] / dt, 1)
                    counts.update(cap=0, out=0, t=now)
                if now - last[1] > 1.0:
                    last[1] = now
                    if find_window()[1] != title:                           # title changed (menu -> world): re-attach
                        control.stop()
                        return
                if now - last[0] < min_gap * 0.9:
                    return
                last[0] = now
                arr = frame.frame_buffer
                h, w = arr.shape[:2]
                cw, ch = client_size(hwnd)
                if cw <= 0 or ch <= 0:
                    return                                                  # minimized
                x0 = max((w - cw) // 2, 0)
                y0 = max(h - ch - x0, 0)
                crop = np.ascontiguousarray(arr[y0:y0 + ch, x0:x0 + cw])    # copy: the capture buffer is reused after this call
                with raw_cond:
                    raw.update(buf=crop, w=crop.shape[1], h=crop.shape[0], n=raw["n"] + 1)
                    raw_cond.notify_all()

            @cap.event
            def on_closed():
                pass

            cap.start()
        except Exception as exc:
            print("[stream] capture error: %s" % exc, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", A.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=encoder_loop, daemon=True).start()
    print("[stream] serving http://0.0.0.0:%d/stream at up to %d fps" % (A.port, A.fps), flush=True)
    capture_loop()

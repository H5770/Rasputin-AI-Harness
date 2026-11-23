"""Rasputin in its own window, without pywebview/pythonnet.

Starts the Flask server, then opens a Chromium-based browser in app mode (no tabs, no address bar).
Browser choice: BROWSER_EXE if set, else a portable browser unpacked into the "browser" folder next to this file
(Brave is preferred), else Microsoft Edge.
With WIPE_PROFILE = True the browser uses a throwaway profile that is deleted when the window closes.
Closing that window shuts Rasputin down safely, like desktop.py does.
"""
import ctypes
import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser

try:
    import psutil
except Exception:
    psutil = None

import termlog
termlog.install()   # start capturing console output before anything else prints

from app import app   # loads the model, so it can take a few seconds

try:
    import pwa
    pwa.register(app)   # phone-installable app: manifest, service worker, icons
except Exception as _exc:
    print("[RASPUTIN] phone app support not loaded:", _exc)

try:
    import lan
    lan.start(app)   # same app over your home Wi-Fi (HTTPS + PIN) for the phone; the local window is unchanged
except Exception as _exc:
    print("[RASPUTIN] phone Wi-Fi access not loaded:", _exc)

# The ngrok tunnel (tunnel.py) is NOT started here. It stays off until you flip the switch in Admin > Web Addresses.

URL = "http://127.0.0.1:5000"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(BASE, "instance", "edge-profile")   # used only when WIPE_PROFILE is False: keeps voice, tone, zoom
TEMP_PROFILE = os.path.join(BASE, "instance", "browser-temp")   # throwaway profile, deleted on exit
BROWSER_DIR = os.path.join(BASE, "browser")                # unpack a portable Chrome / Chromium / Brave in here
BROWSER_NAMES = ("waterfox.exe", "brave.exe", "chrome.exe", "chromium.exe", "msedge.exe")   # earlier = preferred
FIREFOX_NAMES = ("waterfox.exe", "firefox.exe")   # Firefox-family browsers have no --app mode, so they launch differently
HIDE_BROWSER_UI = True   # Firefox-family only: hide the tabs, address bar and toolbars (keeps the window title bar buttons)
KIOSK = False   # Firefox-family only: True = fullscreen with no tabs or address bar (the closest thing to app mode)
BROWSER_SEARCH_DEPTH = 8   # how many folders deep inside ./browser to look
# Optional: exact path to the browser you want (absolute, or relative to this folder). Leave "" to auto-detect.
# Example: BROWSER_EXE = r"browser\BravePortable\App\Brave-Portable\brave.exe"
BROWSER_EXE = ""
WIPE_PROFILE = True   # True: nothing the browser stores survives the window closing (but settings reset each launch)

# Hides Waterfox's own interface so only the page is left (like Chromium's --app mode).
TITLEBAR_COLOR = "#1e1e22"   # dark grey for the top strip; not used elsewhere in the app UI (change to taste)
BORDER_COLOR = TITLEBAR_COLOR   # outer window border (Windows normally paints this in your accent color, often blue)
ICON_FILE = os.path.join(BASE, "rasputin.ico")   # taskbar / window icon (keep rasputin.ico next to this file)
WINDOW_TITLE = "Rasputin"   # text shown when you hover the taskbar button (replaces "Home — Waterfox")
TITLEBAR_HEIGHT = 22   # pixels: height of the thin top strip that holds the minimize / maximize / close buttons
USER_CHROME_CSS = """
:root { --tab-min-height: 0px !important; }
#nav-bar, #PersonalToolbar, #toolbar-menubar, #tabbrowser-tabs, #new-tab-button, #alltabs-button,
#firefox-view-button, #TabsToolbar .tabbrowser-tab, #TabsToolbar #tabbrowser-arrowscrollbox,
#TabsToolbar-customization-target > toolbarbutton { visibility: collapse !important; }
#sidebar-box, #sidebar-splitter, #sidebar-header, #sidebar-main, #sidebar-button,
#sidebar-revamp-splitter, sidebar-main, #sidebar-launcher-splitter { display: none !important; }
#TabsToolbar, #TabsToolbar-customization-target, #titlebar {
  min-height: 0 !important; height: __H__px !important; max-height: __H__px !important; }
.titlebar-buttonbox-container, .titlebar-buttonbox, .titlebar-button {
  height: __H__px !important; min-height: 0 !important; max-height: __H__px !important;
  padding-top: 0 !important; padding-bottom: 0 !important; }
.titlebar-button .toolbarbutton-icon { width: 12px !important; height: 12px !important; }
:root, #navigator-toolbox, #titlebar, #TabsToolbar, #toolbar-menubar {
  background: __C__ !important; background-color: __C__ !important;
  --toolbar-bgcolor: __C__ !important; --lwt-accent-color: __C__ !important; --titlebar-background: __C__ !important; }
.titlebar-button { color: #ececec !important; fill: #ececec !important; -moz-context-properties: fill !important; }
.titlebar-button:hover { background: rgba(255,255,255,.12) !important; }
.titlebar-close:hover { background: #d92228 !important; }
#navigator-toolbox, #titlebar, #TabsToolbar, #browser, #appcontent, #tabbrowser-tabbox, #tabbrowser-tabpanels {
  border: 0 !important; outline: 0 !important; box-shadow: none !important; }
#navigator-toolbox::after, #navigator-toolbox::before { display: none !important; }
#main-window, #main-window[customtitlebar], #browser, #appcontent, #navigator-toolbox-background, #tabbrowser-tabbox {
  appearance: none !important; -moz-appearance: none !important;
  background: __C__ !important; background-color: __C__ !important; }
#main-window { border: 0 !important; margin: 0 !important; }
""".replace("__H__", str(TITLEBAR_HEIGHT)).replace("__C__", TITLEBAR_COLOR)

def prefer_gpu(exe):
    """Windows: mark the browser as 'High performance' (your dedicated GPU) in Settings > Graphics, same as clicking it there.
    Without this, laptops with two GPUs often run the browser on the weak integrated one. Harmless if it fails."""
    if os.name != "nt" or not exe:
        return
    try:
        import winreg
        path = os.path.abspath(exe)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences") as k:
            winreg.SetValueEx(k, path, 0, winreg.REG_SZ, "GpuPreference=2;")
        print("[RASPUTIN] browser set to use the high-performance GPU")
    except Exception as exc:
        print("[RASPUTIN] could not set the browser's GPU preference:", exc)


def run_flask():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

def find_edge():
    for p in (
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if p and os.path.isfile(p):
            return p
    return None

def find_waterfox():
    """An installed (non-portable) Waterfox."""
    for p in (
        shutil.which("waterfox"),
        r"C:\Program Files\Waterfox\waterfox.exe",
        r"C:\Program Files (x86)\Waterfox\waterfox.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Waterfox", "waterfox.exe"),
    ):
        if p and os.path.isfile(p):
            return p
    return None

def configured_browser():
    """The browser named in BROWSER_EXE, if it is set and exists."""
    if not BROWSER_EXE:
        return None
    p = BROWSER_EXE if os.path.isabs(BROWSER_EXE) else os.path.join(BASE, BROWSER_EXE)
    if os.path.isfile(p):
        return p
    print("[RASPUTIN] BROWSER_EXE not found:", p)
    return None

def find_portable(names=BROWSER_NAMES):
    """First known browser .exe found inside ./browser (looks a few folders deep, for PortableApps-style layouts)."""
    if not os.path.isdir(BROWSER_DIR):
        return None
    found = {}
    for root, dirs, files in os.walk(BROWSER_DIR):
        if root[len(BROWSER_DIR):].count(os.sep) > BROWSER_SEARCH_DEPTH:
            dirs[:] = []
            continue
        for f in files:
            if f.lower() in names and f.lower() not in found:
                found[f.lower()] = os.path.join(root, f)
    for name in names:
        if name in found:
            return found[name]
    return None


def wipe_profile():
    """Delete the throwaway profile (retries, because the browser may still be releasing its files)."""
    if not WIPE_PROFILE:
        return
    for _ in range(15):
        shutil.rmtree(TEMP_PROFILE, ignore_errors=True)
        if not os.path.exists(TEMP_PROFILE):
            return
        time.sleep(0.4)
    print("[RASPUTIN] could not fully delete the browser profile:", TEMP_PROFILE)


def wait_for_server(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", 5000), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False

def window_size():
    """About 85% wide and 88% tall of the screen, like desktop.py."""
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        return int(user32.GetSystemMetrics(0) * 0.85), int(user32.GetSystemMetrics(1) * 0.88)
    except Exception:
        return 1280, 800

_proc = None
_profile = None

def profile_procs():
    """Browser processes using our profile folder. Some browsers hand the window to a helper process and
    the one we started exits at once, so 'the process we started ended' does not mean 'the window closed'."""
    if not (psutil and _profile):
        return []
    key, out = _profile.lower(), []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(key in (a or "").lower() for a in (p.info["cmdline"] or [])):
                out.append(p)
        except Exception:
            pass
    return out

def close_window():
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:
            pass
    for p in profile_procs():
        try:
            p.terminate()
        except Exception:
            pass

def apply_window_icon(hwnd, user32, icons):
    """Give one window the Rasputin icon (this is what the taskbar button shows)."""
    big, small = icons
    if big:
        user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0080, ctypes.c_void_p(1), ctypes.c_void_p(big))     # WM_SETICON, ICON_BIG
        user32.SetClassLongPtrW(ctypes.c_void_p(hwnd), -14, ctypes.c_void_p(big))                        # GCLP_HICON
    if small:
        user32.SendMessageW(ctypes.c_void_p(hwnd), 0x0080, ctypes.c_void_p(0), ctypes.c_void_p(small))   # ICON_SMALL
        user32.SetClassLongPtrW(ctypes.c_void_p(hwnd), -34, ctypes.c_void_p(small))                      # GCLP_HICONSM

def style_window_border(seconds=60):
    """Windows 11: recolor the outer window border of the browser window (default is your accent color)."""
    try:
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        c = BORDER_COLOR.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        colorref = ctypes.c_uint(r | (g << 8) | (b << 16))      # COLORREF is 0x00BBGGRR
        ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        user32.LoadImageW.restype = ctypes.c_void_p
        icons = (None, None)
        if os.path.isfile(ICON_FILE):
            def load(size):   # IMAGE_ICON = 1, LR_LOADFROMFILE = 0x10
                return user32.LoadImageW(None, ICON_FILE, 1, size, size, 0x10)
            icons = (load(user32.GetSystemMetrics(11) or 32), load(user32.GetSystemMetrics(49) or 16))
        else:
            print("[RASPUTIN] icon file not found:", ICON_FILE)
        done = set()

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or hwnd in done:
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                h = ctypes.c_void_p(hwnd)
                one = ctypes.c_int(1)
                dwm.DwmSetWindowAttribute(h, 20, ctypes.byref(one), 4)         # 20 = dark mode frame
                dwm.DwmSetWindowAttribute(h, 38, ctypes.byref(one), 4)         # 38 = backdrop type, 1 = none (no Mica tint)
                dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(colorref), 4)    # 34 = border color
                dwm.DwmSetWindowAttribute(h, 35, ctypes.byref(colorref), 4)    # 35 = caption (title bar) color
                apply_window_icon(hwnd, user32, icons)
                done.add(hwnd)
            return True

        end = time.time() + seconds
        while time.time() < end:
            pids = {p.pid for p in profile_procs()}
            if _proc is not None:
                pids.add(_proc.pid)
            user32.EnumWindows(ENUM(visit), 0)
            time.sleep(0.5)
    except Exception as exc:
        print("[RASPUTIN] could not recolor the window border:", exc)

def keep_window_title():
    """Waterfox always writes "<page title> — Waterfox" into the window title, so keep overwriting it."""
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        pids, refreshed = set(), 0.0

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "MozillaWindowClass":
                return True
            cur = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, cur, 256)
            if cur.value != WINDOW_TITLE:
                user32.SetWindowTextW(hwnd, WINDOW_TITLE)
            return True

        while True:
            if time.time() - refreshed > 3:
                pids = {p.pid for p in profile_procs()}
                if _proc is not None:
                    pids.add(_proc.pid)
                refreshed = time.time()
            user32.EnumWindows(ENUM(visit), 0)
            time.sleep(0.3)
    except Exception as exc:
        print("[RASPUTIN] could not set the window title:", exc)

def keep_alive():
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    termlog.set_window_closer(close_window)
    threading.Thread(target=run_flask, daemon=True).start()
    wait_for_server()

    chosen = configured_browser() or find_portable(("waterfox.exe",)) or find_waterfox() or find_portable()   # the browser YOU picked
    browser = chosen or find_edge()
    if browser:
        print("[RASPUTIN] browser:", browser)
        wipe_profile()                       # clears anything left behind by a crash last time
        profile = TEMP_PROFILE if WIPE_PROFILE else PROFILE
        os.makedirs(profile, exist_ok=True)
        w, h = window_size()
        if os.path.basename(browser).lower() in FIREFOX_NAMES:
            # Firefox family (Waterfox): own throwaway profile, own process, no --app mode available.
            # user.js skips the first-run / welcome / default-browser pages on a fresh profile.
            with open(os.path.join(profile, "user.js"), "w", encoding="utf-8") as f:
                f.write('user_pref("browser.shell.checkDefaultBrowser", false);\n'
                        'user_pref("browser.aboutwelcome.enabled", false);\n'
                        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
                        'user_pref("startup.homepage_welcome_url", "");\n'
                        'user_pref("startup.homepage_welcome_url.additional", "");\n'
                        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
                        'user_pref("browser.startup.page", 0);\n'
                        'user_pref("browser.tabs.warnOnClose", false);\n'
                        'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
                        'user_pref("ui.systemUsesDarkTheme", 1);\n'
                        'user_pref("browser.theme.toolbar-theme", 0);\n'
                        'user_pref("browser.theme.content-theme", 0);\n'
                        'user_pref("widget.windows.mica", false);\n'
                        'user_pref("widget.windows.mica.popups", false);\n'
                        'user_pref("widget.windows.mica.toplevel-backdrop", 0);\n'
                        'user_pref("widget.windows.accent-color-in-titlebar", false);\n'
                        'user_pref("sidebar.revamp", false);\n'
                        'user_pref("sidebar.verticalTabs", false);\n'
                        'user_pref("sidebar.visibility", "hide-sidebar");\n'
                        # no toolbar = no place for the mic permission popup, so allow the mic without asking
                        'user_pref("media.navigator.permission.disabled", true);\n'
                        'user_pref("permissions.default.microphone", 1);\n'
                        # GPU: hardware-accelerated compositing and WebRender even on drivers Firefox is cautious about
                        'user_pref("gfx.webrender.all", true);\n'
                        'user_pref("layers.acceleration.force-enabled", true);\n'
                        'user_pref("media.hardware-video-decoding.force-enabled", true);\n')
            chrome_dir = os.path.join(profile, "chrome")
            os.makedirs(chrome_dir, exist_ok=True)
            with open(os.path.join(chrome_dir, "userChrome.css"), "w", encoding="utf-8") as f:
                f.write(USER_CHROME_CSS if HIDE_BROWSER_UI else "")
            args = [browser, "-no-remote", "-profile", profile]
            if KIOSK:
                args.append("--kiosk")
            else:
                args += ["-width", str(w), "-height", str(h)]
            args.append(URL)
        else:
            args = [
                browser,
                f"--app={URL}",
                f"--user-data-dir={profile}",   # separate profile => separate process we can wait on
                f"--window-size={w},{h}",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-ui-for-media-stream",   # auto-approve the mic permission (the app window has no visible prompt)
                # GPU: a fresh profile plus Chromium's driver blocklist can silently fall back to the CPU
                "--ignore-gpu-blocklist",
                "--enable-gpu-rasterization",
                "--enable-zero-copy",
                "--enable-accelerated-video-decode",
                "--use-angle=d3d11",
            ]
            # --incognito disabled: some Brave builds quit right after launch with it. WIPE_PROFILE still
            # deletes the throwaway profile folder when the window closes, so nothing is left behind.
            # if WIPE_PROFILE:
            #     args.append("--incognito")
        prefer_gpu(browser)
        _profile = profile
        started = time.time()
        _proc = subprocess.Popen(args)
        threading.Thread(target=style_window_border, daemon=True).start()
        threading.Thread(target=keep_window_title, daemon=True).start()
        _proc.wait()   # returns when the window is closed (X button or the in-app EXIT button)
        print("[RASPUTIN] browser process exited with code", _proc.returncode, "after", round(time.time() - started, 1), "seconds")
        time.sleep(3)  # ...unless the browser handed the window to another process: wait for that one too
        while profile_procs():
            time.sleep(1)
        if time.time() - started < 10:
            if chosen:
                # Your own browser was used, so don't open a different one behind your back.
                print("[RASPUTIN] the browser window closed right after opening (is another copy of it still running?). "
                      "Shutting down instead of opening a different browser.")
            else:
                print("[RASPUTIN] the browser window closed right after opening (is another copy of it still running?). "
                      "Opening your default browser instead; close this console or press Ctrl+C to quit.")
                webbrowser.open(URL)
                keep_alive()
        wipe_profile()
    else:
        print("[RASPUTIN] No portable browser in ./browser and Microsoft Edge not found; opening your default browser instead.")
        webbrowser.open(URL)
        keep_alive()

    termlog.run_cleanup()
    os._exit(0)

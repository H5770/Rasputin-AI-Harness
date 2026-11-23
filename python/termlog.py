"""termlog.py - feeds the Terminal tab and handles safe shutdown.

install()      captures everything the program prints (stdout + stderr) into a ring buffer
register(app)  adds /terminal-log and /shutdown routes to the Flask app
add_cleanup()  lets other modules register "close this safely" functions
run_cleanup()  runs those functions once (waits briefly for a reply that is still being generated)
"""
import atexit
import logging
import os
import signal
import sys
import threading
import time
from collections import deque

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()
_buf = deque(maxlen=3000)
_seq = 0
_partial = ""
_logfile = None
_installed = False
_cleanups = []
_cleaned = False
_cleanup_lock = threading.Lock()
_active = 0
_closer = None
MAX_LINE = 2000


def _add(text):
    global _seq, _partial
    with _lock:
        parts = (_partial + text).split("\n")
        _partial = parts.pop()
        for line in parts:
            line = line.rstrip("\r")
            if "\r" in line:                      # carriage return = overwrite the line
                line = line.rsplit("\r", 1)[-1]
            line = line[:MAX_LINE]
            _seq += 1
            _buf.append((_seq, line))
            if _logfile:
                try:
                    _logfile.write(line + "\n")
                    _logfile.flush()
                except Exception:
                    pass


class _Tee:
    """Looks like sys.stdout/sys.stderr. Copies text to the buffer and to the real console (if any)."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, orig):
        self._orig = orig

    def write(self, s):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        if self._orig is not None:
            try:
                self._orig.write(s)
            except Exception:
                pass
        _add(s)
        return len(s)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._orig and self._orig.isatty())
        except Exception:
            return False

    def __getattr__(self, name):
        if self._orig is not None:
            return getattr(self._orig, name)
        raise AttributeError(name)


def install():
    global _installed, _logfile
    if _installed:
        return
    _installed = True
    try:
        os.makedirs(os.path.join(_BASE, "instance"), exist_ok=True)
        _logfile = open(os.path.join(_BASE, "instance", "terminal.log"), "w", encoding="utf-8")
    except Exception:
        _logfile = None
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    install_exit_hooks()
    _wipe_music()                                   # leftovers from a run that was killed
    print("[RASPUTIN] terminal capture online")


def add_cleanup(fn, first=False):
    """first=True runs it before the others (the Discord bot uses this so it leaves Discord straight away)."""
    if first:
        _cleanups.insert(0, fn)
    else:
        _cleanups.append(fn)


_ctrl_handler = None        # kept alive on purpose: ctypes frees the callback if nothing references it


def _wipe_music():
    """Delete everything in the music_temp folder (songs the Discord bot downloaded). Safe to call any number of times."""
    try:
        dbm = sys.modules.get("discord_bot")
        if dbm is not None:
            dbm._music_cleanup_all()            # also kills the ffmpeg that may still hold a song open
            return
    except Exception:
        pass
    d = os.path.join(_BASE, "music_temp")
    try:
        for n in os.listdir(d):
            fp = os.path.join(d, n)
            for _ in range(20):
                try:
                    if os.path.isdir(fp):
                        import shutil
                        shutil.rmtree(fp, ignore_errors=True)
                    else:
                        os.remove(fp)
                    break
                except Exception:
                    time.sleep(0.25)
    except Exception:
        pass


def _hard_exit(wait):
    try:
        run_cleanup(wait=wait)
    finally:
        _wipe_music()
        os._exit(0)


def install_exit_hooks():
    """Make sure cleanup (Discord bot offline, tunnel closed, model freed) also runs when the program is stopped
    WITHOUT the EXIT button: Ctrl+C, closing the console window, logging off / shutting Windows down, `kill`."""
    global _ctrl_handler
    atexit.register(run_cleanup, 3.0)               # normal interpreter exit, sys.exit(), unhandled error
    if os.name == "nt":
        try:
            import ctypes
            HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint)

            def _on_ctrl(kind):
                # 0 Ctrl+C, 1 Ctrl+Break, 2 console window closed, 5 log off, 6 shutdown.
                # Windows kills the process a few seconds after 2/5/6, so do not wait for a running reply.
                _hard_exit(0.0 if kind in (2, 5, 6) else 2.0)
                return 1
            _ctrl_handler = HandlerRoutine(_on_ctrl)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler, 1)
        except Exception as exc:
            print("[RASPUTIN] could not install the console-close handler:", exc)
    else:
        def _on_signal(signum, frame):
            _hard_exit(2.0)
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _on_signal)
                except Exception:
                    pass                            # not the main thread


def busy():
    return _active > 0


def set_window_closer(fn):
    global _closer
    _closer = fn


def run_cleanup(wait=10.0):
    global _cleaned
    _wipe_music()                                 # first thing, before anything slow can be cut short
    with _cleanup_lock:
        if _cleaned:
            return
        _cleaned = True
    end = time.time() + wait
    while busy() and time.time() < end:           # let a reply that is mid-generation finish
        time.sleep(0.1)
    for fn in _cleanups:
        try:
            fn()
        except Exception as exc:
            print("[RASPUTIN] cleanup warning:", exc)
    print("[RASPUTIN] shutdown complete")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        if _logfile:
            _logfile.flush()
    except Exception:
        pass


class _Quiet(logging.Filter):
    """Hide the constant polling lines so real output stays readable."""
    def filter(self, record):
        msg = record.getMessage()
        return "/terminal-log" not in msg and "/system-data" not in msg and "/image/" not in msg and "/home/state" not in msg


def register(app):
    from flask import abort, jsonify, request

    install()
    logging.getLogger("werkzeug").addFilter(_Quiet())

    @app.before_request
    def _track_in():
        global _active
        if request.path.startswith("/terminal-log"):
            return
        with _lock:
            _active += 1
        request.environ["rasputin.counted"] = True

    @app.teardown_request
    def _track_out(exc=None):
        global _active
        if request.environ.get("rasputin.counted"):
            with _lock:
                _active -= 1

    @app.route("/terminal-log")
    def _terminal_log():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        with _lock:
            lines = [{"i": i, "t": t} for i, t in _buf if i > since]
            nxt = _seq
        return jsonify({"lines": lines, "next": nxt})

    @app.route("/shutdown", methods=["POST"])
    def _shutdown():
        # Custom header + same-origin check: a random web page can't trigger this.
        if request.headers.get("X-Rasputin-Action") != "shutdown":
            abort(403)
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403)
        print("[RASPUTIN] shutdown requested from the UI")

        def _later():
            time.sleep(0.4)                       # let this response reach the browser
            if _closer:
                _closer()                         # desktop: closes the window, desktop.py finishes cleanup
                time.sleep(8)
            run_cleanup()
            _wipe_music()
            os._exit(0)

        threading.Thread(target=_later, daemon=True).start()
        return jsonify({"ok": True})

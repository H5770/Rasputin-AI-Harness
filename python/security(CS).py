"""security(CS).py - the Security tab: live camera face recognition, profiles, liveness check and an audit log.

Ported from the standalone CapstoneAI face program and rebuilt to fit the Rasputin harness:

  * Everything lives in instance/rasputin.db (the same SQLite file as chats and accounts). New tables only:
    cs_face (profiles + captured unknowns, with their face signature and a small thumbnail),
    cs_log (the recognition log) and cs_setting (this tab's switches). No second .db file.
  * Staff only (Admins and the Owner, PC or phone), same guard as the Discord and Games tabs.
  * The camera is OFF at launch. It only opens when the Security tab's CAMERA switch is turned on, and it is
    released when you switch it off or exit Rasputin.
  * One background thread owns the camera. The live feed is an MJPEG stream any number of viewers can watch.
  * Works with both mediapipe APIs: the old mp.solutions.face_mesh, and the newer FaceLandmarker (which needs
    models/face_landmarker.task - downloaded automatically the first time the camera is switched on).
  * cv2 / mediapipe / numpy are imported only when the camera or an image import needs them, so Rasputin starts
    exactly as fast as before and keeps working if they are not installed (pip install opencv-python mediapipe).

The file name has "(CS)" in it, so app.py loads it by path instead of with a normal import.

One-time import of the old program: if a folder named security_import sits in the Rasputin folder, with any of
Faces/, UnknownFaces/ and Database/faces_db.db inside it, its profiles and log history are merged into
rasputin.db the first time Rasputin starts, and the folder is renamed security_import_done.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from importlib.util import find_spec

from flask import Response, jsonify, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "models")
MODEL_FILE = os.path.join(MODEL_DIR, "face_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
             "float16/1/face_landmarker.task")
IMPORT_DIR = os.path.join(ROOT, "security_import")

MATCH_THRESHOLD = 0.42          # smaller = stricter (same value as the original program)
RECOGNISE_EVERY = 4             # run the matcher on every 4th frame
UNKNOWN_STREAK = 3              # unmatched checks in a row before an unknown face is captured
MAX_UNKNOWNS = 200              # oldest captures are dropped past this
FACE_GRACE = 1.5                # seconds without a face before the log treats the person as gone
THUMB_PX = 180
NAME_MAX = 40

# ---- lazily imported heavy libraries ----------------------------------------------------------------------------
cv2 = None
np = None
_libs_lock = threading.Lock()


def _load_libs():
    """Import cv2 and numpy on first use. Returns "" or a human-readable reason it failed."""
    global cv2, np
    with _libs_lock:
        if cv2 is not None and np is not None:
            return ""
        try:
            import cv2 as _cv2
            import numpy as _np
        except Exception as exc:
            return "OpenCV / NumPy are missing. Run: pip install opencv-python mediapipe  (" + str(exc) + ")"
        cv2, np = _cv2, _np
        return ""


def _libs_installed():
    return all(find_spec(m) is not None for m in ("cv2", "mediapipe", "numpy"))


# ---- database (rasputin.db) -------------------------------------------------------------------------------------
_DB = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS cs_face (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'known',
    signature TEXT NOT NULL,
    image BLOB,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cs_face_kind ON cs_face (kind, name);
CREATE TABLE IF NOT EXISTS cs_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(_DB, timeout=30, isolation_level=None)
    try:
        c.execute("PRAGMA busy_timeout=30000")
        yield c
    finally:
        c.close()


def _now_ms():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _now_s():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _setting(key, default):
    with _conn() as c:
        row = c.execute("SELECT value FROM cs_setting WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_setting(key, value):
    with _conn() as c:
        c.execute("INSERT INTO cs_setting (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---- face signature (the original SVD / Procrustes method, unchanged) -------------------------------------------
STABLE_LANDMARK_INDICES = [
    10, 21, 54, 67, 103, 109, 127, 139, 143, 151, 156, 162, 234, 251, 284, 297,
    332, 338, 356, 368, 372, 383, 389, 446, 454, 6, 168, 197, 195, 5, 4, 1,
]
_BASELINE = [
    [0.0, 0.6, 0.0], [-0.4, 0.4, -0.1], [-0.5, 0.3, -0.1], [-0.3, 0.4, -0.1],
    [-0.2, 0.4, -0.1], [-0.1, 0.4, -0.1], [-0.6, 0.2, -0.2], [-0.4, 0.2, -0.1],
    [-0.3, 0.1, -0.1], [0.0, 0.5, 0.0], [0.3, 0.1, -0.1], [-0.4, 0.0, -0.1],
    [-0.5, -0.2, -0.2], [0.4, 0.4, -0.1], [0.5, 0.3, -0.1], [0.3, 0.4, -0.1],
    [0.2, 0.4, -0.1], [0.1, 0.4, -0.1], [0.6, 0.2, -0.2], [0.4, 0.2, -0.1],
    [0.4, 0.0, -0.1], [0.3, 0.1, -0.1], [0.4, 0.3, -0.1], [0.5, -0.2, -0.2],
    [0.4, -0.3, -0.2], [0.0, 0.3, 0.1], [0.0, 0.4, 0.1], [0.0, 0.2, 0.2],
    [0.0, 0.1, 0.2], [0.0, 0.0, 0.2], [0.0, -0.1, 0.3], [0.0, -0.2, 0.4],
]


def extract_signature(points):
    """points: (N, 3) array of normalised landmarks -> flat list of 96 numbers, pose and scale removed."""
    baseline = np.array(_BASELINE, dtype=np.float32)
    stable = points[STABLE_LANDMARK_INDICES]
    c_cur, c_base = stable.mean(axis=0), baseline.mean(axis=0)
    cur, base = stable - c_cur, baseline - c_base
    u, _s, vt = np.linalg.svd(cur.T @ base)
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[2, :] *= -1
        r = vt.T @ u.T
    scale = float(np.mean(np.linalg.norm(cur, axis=1))) or 1.0
    factor = float(np.mean(np.linalg.norm(base, axis=1))) / scale
    return (factor * ((stable - c_cur) @ r.T)).flatten().tolist()


def _eye_ratio(p, w, h):
    top, bottom = p[159][:2] * (w, h), p[145][:2] * (w, h)
    left, right = p[33][:2] * (w, h), p[133][:2] * (w, h)
    span = float(np.linalg.norm(left - right))
    return float(np.linalg.norm(top - bottom)) / span if span else 0.0


def _face_box(p, w, h):
    xs, ys = (p[:, 0] * w).astype(int), (p[:, 1] * h).astype(int)
    x, y = max(0, int(xs.min())), max(0, int(ys.min()))
    return x, y, max(0, min(w - x, int(xs.max()) - x)), max(0, min(h - y, int(ys.max()) - y))


def _thumb(bgr):
    """Small JPEG of a face crop for the profiles list (stored in the database)."""
    if bgr is None or bgr.size == 0:
        return None
    h, w = bgr.shape[:2]
    k = THUMB_PX / float(max(h, w))
    if k < 1:
        bgr = cv2.resize(bgr, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


PHOTO_PX = 960                  # longest side of the full, time-stamped photo kept with each saved face


def _photo(bgr):
    """Full picture (not just the face crop) with the date and time burned into the corner. JPEG bytes."""
    if bgr is None or bgr.size == 0:
        return None
    img = bgr.copy()
    h, w = img.shape[:2]
    k = PHOTO_PX / float(max(h, w))
    if k < 1:
        img = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    text = "Rasputin Security  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scale = max(0.45, w / 1400.0)
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    pad = int(8 * scale) + 2
    cv2.rectangle(img, (0, h - th - base - pad * 2), (tw + pad * 2, h), (0, 0, 0), -1)
    cv2.putText(img, text, (pad, h - base - pad), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes() if ok else None


# ---- face store (cached in memory so matching never touches the disk) -------------------------------------------
class _Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []          # [(id, name, kind)]
        self.mat = None         # (n, 96) float32
        self.dirty = True

    def invalidate(self):
        with self.lock:
            self.dirty = True

    def _reload(self):
        with _conn() as c:
            data = c.execute("SELECT id, name, kind, signature FROM cs_face ORDER BY id").fetchall()
        rows, sigs = [], []
        for fid, name, kind, sig in data:
            try:
                v = np.array(json.loads(sig), dtype=np.float32)
                if v.shape == (96,):
                    rows.append((fid, name, kind))
                    sigs.append(v)
            except Exception:
                continue
        self.rows = rows
        self.mat = np.stack(sigs) if sigs else None
        self.dirty = False

    def match(self, sig):
        """-> (name, face_id, kind) of the closest saved face, or None when nothing is close enough."""
        with self.lock:
            if self.dirty:
                self._reload()
            if self.mat is None:
                return None
            d = np.linalg.norm(self.mat - np.array(sig, dtype=np.float32), axis=1)
            i = int(d.argmin())
            if float(d[i]) < MATCH_THRESHOLD:
                fid, name, kind = self.rows[i]
                return name, fid, kind
        return None


_store = _Store()


def _clean_name(raw):
    name = re.sub(r"\s+", " ", re.sub(r"[^\w .'\-]", "", str(raw or ""))).strip()[:NAME_MAX]
    if not name:
        return ""
    with _conn() as c:      # reuse the spelling of an existing profile ("bob" -> "Bob")
        row = c.execute("SELECT name FROM cs_face WHERE kind='known' AND name=? COLLATE NOCASE LIMIT 1",
                        (name,)).fetchone()
    return row[0] if row else name


def _save_face(name, kind, sig, thumb, photo=None):
    with _conn() as c:
        cur = c.execute("INSERT INTO cs_face (name, kind, signature, image, photo, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (name, kind, json.dumps([round(float(x), 6) for x in sig]), thumb, photo, _now_s()))
        fid = cur.lastrowid
        if kind == "unknown":
            name = "Unknown_Captured_%d" % (1000 + fid)
            c.execute("UPDATE cs_face SET name=? WHERE id=?", (name, fid))
            extra = c.execute("SELECT id FROM cs_face WHERE kind='unknown' ORDER BY id DESC LIMIT -1 OFFSET ?",
                              (MAX_UNKNOWNS,)).fetchall()
            for (old,) in extra:
                c.execute("DELETE FROM cs_face WHERE id=?", (old,))
    _store.invalidate()
    return fid, name


def _log_event(name):
    ts = _now_ms()
    try:
        with _conn() as c:
            c.execute("INSERT INTO cs_log (name, ts) VALUES (?, ?)", (name, ts))
    except Exception as exc:
        print("[SECURITY] log write failed:", exc, file=sys.stderr)


# ---- landmark detector (old mp.solutions, or the newer FaceLandmarker) -------------------------------------------
def _ensure_model():
    if os.path.isfile(MODEL_FILE) and os.path.getsize(MODEL_FILE) > 1_000_000:
        return MODEL_FILE
    os.makedirs(MODEL_DIR, exist_ok=True)
    print("[SECURITY] downloading the face landmark model (about 4 MB) ...")
    tmp = MODEL_FILE + ".part"
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=40) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, MODEL_FILE)
    except Exception as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("Could not download the face landmark model (%s). Download %s and save it as %s"
                           % (exc, MODEL_URL, MODEL_FILE))
    return MODEL_FILE


class _Detector:
    def __init__(self, still):
        import mediapipe as mp
        self._mp = mp
        self.lock = threading.Lock()
        self.legacy = self.task = None
        sol = getattr(mp, "solutions", None)
        if sol is not None and hasattr(sol, "face_mesh"):
            self.legacy = sol.face_mesh.FaceMesh(static_image_mode=still, max_num_faces=1, refine_landmarks=True,
                                                 min_detection_confidence=0.6, min_tracking_confidence=0.6)
        else:
            from mediapipe.tasks import python as mpt
            from mediapipe.tasks.python import vision
            opts = vision.FaceLandmarkerOptions(
                base_options=mpt.BaseOptions(model_asset_path=_ensure_model()),
                running_mode=vision.RunningMode.IMAGE, num_faces=1,
                min_face_detection_confidence=0.6, min_face_presence_confidence=0.6, min_tracking_confidence=0.6)
            self.task = vision.FaceLandmarker.create_from_options(opts)

    def detect(self, bgr):
        """-> (N x 3 float32 landmark array or None, number of faces seen)"""
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        with self.lock:
            if self.legacy is not None:
                res = self.legacy.process(rgb)
                faces = res.multi_face_landmarks or []
                lms = faces[0].landmark if faces else None
            else:
                mp = self._mp
                res = self.task.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
                faces = res.face_landmarks or []
                lms = faces[0] if faces else None
        if lms is None:
            return None, 0
        return np.array([[l.x, l.y, l.z] for l in lms], dtype=np.float32), len(faces)

    def close(self):
        try:
            (self.legacy or self.task).close()
        except Exception:
            pass


_still = None
_still_lock = threading.Lock()


def _still_detector():
    global _still
    with _still_lock:
        if _still is None:
            _still = _Detector(True)
        return _still


# ---- the camera engine -------------------------------------------------------------------------------------------
class _Engine:
    def __init__(self):
        self.thread = None
        self.stop_ev = threading.Event()
        self.state = "off"                      # off / starting / on / error
        self.error = ""
        self.notice = ""
        self.cam_index = int(_setting("camera_index", "0") or 0)
        self.want_idx = self.cam_index
        self.capture_unknown = _setting("capture_unknown", "0") == "1"
        self.logs_enabled = _setting("logs_enabled", "1") == "1"
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.jpg, self.seq, self.viewers = None, 0, 0
        self._reset_metrics()

    # -- state ----------------------------------------------------------------------------------------------------
    def _reset_metrics(self):
        self.fps, self.faces = 0, 0
        self.label, self.recognised, self.kind = "Unknown", False, "none"
        self.matched_unknown_id = None
        self.eyes_closed = self.blink = False
        self.last_nose = None
        self.moves = 0
        self.is_human = self.ran_verify = self.verifying = False
        self.verify_t0 = 0.0
        self.latest = None                      # (landmarks, face crop, time) for "save from camera"
        self.session_name = None
        self.last_face_t = 0.0
        self.unk_streak = 0

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state, "error": self.error, "notice": self.notice, "camera": self.cam_index,
                "fps": self.fps, "faces": self.faces, "label": self.label,
                "liveness": "HUMAN" if self.is_human else "NOT HUMAN" if self.ran_verify else "N/A",
                "verifying": self.verifying, "capture_unknown": self.capture_unknown, "logs": self.logs_enabled,
            }

    # -- control --------------------------------------------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_ev.clear()
        with self.lock:
            self.state, self.error, self.notice = "starting", "", ""
            self._reset_metrics()
        self.thread = threading.Thread(target=self._run, name="security-camera", daemon=True)
        self.thread.start()

    def stop(self, wait=3.0):
        self.stop_ev.set()
        with self.cond:
            self.cond.notify_all()
        t = self.thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(wait)
        with self.lock:
            if self.state != "error":
                self.state = "off"
            self._reset_metrics()

    def set_camera(self, idx):
        self.want_idx = int(idx)

    def set_capture(self, on):
        self.capture_unknown = bool(on)
        _set_setting("capture_unknown", "1" if on else "0")

    def set_logs(self, on):
        self.logs_enabled = bool(on)
        _set_setting("logs_enabled", "1" if on else "0")

    def start_verification(self):
        with self.lock:
            self.eyes_closed = self.blink = self.is_human = False
            self.moves = 0
            self.verifying = self.ran_verify = True
            self.verify_t0 = time.time()

    def fresh_photo(self, max_age=2.5):
        with self.lock:
            full = getattr(self, "full", None)
            if full and time.time() - full[1] <= max_age:
                return full[0].copy()
        return None

    def fresh_face(self, max_age=2.0):
        with self.lock:
            if self.latest and time.time() - self.latest[2] <= max_age:
                return self.latest[0].copy(), self.latest[1].copy()
        return None

    # -- video stream ---------------------------------------------------------------------------------------------
    def frames(self):
        with self.cond:
            self.viewers += 1
        last = -1
        try:
            while not self.stop_ev.is_set() and self.state in ("starting", "on"):
                with self.cond:
                    if self.seq == last:
                        self.cond.wait(1.0)
                    jpg, seq = self.jpg, self.seq
                if jpg is None or seq == last:
                    continue
                last = seq
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
        finally:
            with self.cond:
                self.viewers -= 1

    # -- the loop -------------------------------------------------------------------------------------------------
    @staticmethod
    def _open(idx):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap

    def _run(self):
        cap = det = None
        try:
            err = _load_libs()
            if err:
                raise RuntimeError(err)
            try:
                det = _Detector(False)
            except ImportError as exc:
                raise RuntimeError("mediapipe is missing. Run: pip install mediapipe  (" + str(exc) + ")")
            cur = self.want_idx
            cap = self._open(cur)
            if cap is None:
                raise RuntimeError("Could not open camera %d. Is another program using it?" % cur)
            with self.lock:
                self.cam_index, self.state = cur, "on"
            print("[SECURITY] camera %d is on" % cur)
            n, failed, prev = 0, 0, time.time()
            while not self.stop_ev.is_set():
                if self.want_idx != cur:
                    new = self._open(self.want_idx)
                    if new is not None:
                        cap.release()
                        cap, cur = new, self.want_idx
                        with self.lock:
                            self.cam_index = cur
                            self._reset_metrics()
                        _set_setting("camera_index", cur)
                    else:
                        with self.lock:
                            self.notice = "Camera %d would not open, so it is still on camera %d." % (self.want_idx, cur)
                        self.want_idx = cur
                ok, frame = cap.read()
                if not ok or frame is None:
                    failed += 1
                    if failed > 40:
                        raise RuntimeError("The camera stopped sending pictures.")
                    time.sleep(0.03)
                    continue
                failed = 0
                n += 1
                frame = cv2.flip(frame, 1)
                self._process(frame, det, n)
                now = time.time()
                with self.lock:
                    inst = 1.0 / max(now - prev, 1e-4)
                    self.fps = int(round(inst if not self.fps else self.fps * 0.8 + inst * 0.2))
                prev = now
                if self.viewers > 0:
                    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        with self.cond:
                            self.jpg, self.seq = buf.tobytes(), self.seq + 1
                            self.cond.notify_all()
        except Exception as exc:
            with self.lock:
                self.state, self.error = "error", str(exc)
            print("[SECURITY] camera stopped:", exc, file=sys.stderr)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            if det:
                det.close()
            with self.lock:
                if self.state != "error":
                    self.state = "off"
                self.fps = 0
            with self.cond:
                self.jpg = None
                self.cond.notify_all()
            print("[SECURITY] camera is off")

    def _process(self, frame, det, n):
        h, w = frame.shape[:2]
        pts, count = det.detect(frame)
        now = time.time()

        if pts is None:
            with self.lock:
                self.faces = 0
                self.recognised, self.label, self.kind, self.matched_unknown_id = False, "Unknown", "none", None
                self.eyes_closed = self.blink = False
                self.last_nose, self.moves = None, 0
                self.is_human = self.ran_verify = self.verifying = False
                self.latest, self.unk_streak = None, 0
                if self.session_name is not None and now - self.last_face_t > FACE_GRACE:
                    self.session_name = None
            return

        x, y, bw, bh = _face_box(pts, w, h)
        crop = frame[y:y + bh, x:x + bw]
        with self.lock:
            self.faces = count
            self.last_face_t = now
            self.latest = (pts, crop.copy(), now)

        if n % RECOGNISE_EVERY == 0:
            with self.lock:
                self.full = (frame.copy(), now)      # clean picture (no boxes drawn yet) for "save from camera"
            self._recognise(pts, crop, frame)

        with self.lock:
            label, recognised, kind = self.label, self.recognised, self.kind
            # liveness: a blink plus enough head movement inside the 2 second window
            if _eye_ratio(pts, w, h) < 0.18:
                self.eyes_closed = True
            elif self.eyes_closed:
                self.eyes_closed, self.blink = False, True
            nose = pts[1]
            if self.last_nose is not None and (abs(nose[0] - self.last_nose[0]) > 0.005
                                               or abs(nose[1] - self.last_nose[1]) > 0.005):
                self.moves += 1
            self.last_nose = (float(nose[0]), float(nose[1]))
            if self.verifying and now - self.verify_t0 >= 2.0:
                self.verifying = False
                self.is_human = self.blink and self.moves >= 10
            verifying, ran, human = self.verifying, self.ran_verify, self.is_human

        color = (0, 255, 0) if kind == "known" else (0, 165, 255) if kind == "unknown" else (0, 0, 255)
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), color, 3, cv2.LINE_AA)
        cv2.putText(frame, label, (x, y - 10 if y - 10 > 20 else y + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2,
                    cv2.LINE_AA)
        if verifying:
            cv2.putText(frame, "VERIFYING MOVEMENT & BLINK...", (20, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 255, 255), 2, cv2.LINE_AA)
        if ran:
            cv2.putText(frame, "Detection: " + ("HUMAN" if human else "NOT HUMAN"), (20, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0) if human else (0, 0, 255), 3, cv2.LINE_AA)

    def _recognise(self, pts, crop, frame=None):
        sig = extract_signature(pts)
        hit = _store.match(sig)
        if hit:
            name, fid, kind = hit
            with self.lock:
                self.label, self.recognised, self.unk_streak = name, True, 0
                self.kind = "unknown" if kind == "unknown" else "known"
                self.matched_unknown_id = fid if kind == "unknown" else None
        else:
            capture = False
            with self.lock:
                self.unk_streak += 1
                capture = self.capture_unknown and self.unk_streak >= UNKNOWN_STREAK and crop.size > 0
                if not capture:
                    self.label, self.recognised, self.kind, self.matched_unknown_id = "Unknown", False, "none", None
            if capture:
                fid, name = _save_face("Unknown", "unknown", sig, _thumb(crop), _photo(frame))
                with self.lock:
                    self.label, self.recognised, self.kind, self.matched_unknown_id = name, True, "unknown", fid
                    self.unk_streak = 0
        with self.lock:
            name = self.label
            changed = name != self.session_name
            if changed:
                self.session_name = name
        if changed and self.logs_enabled:
            _log_event(name)


_engine = None


# ---- legacy import (old CapstoneAI folders) ---------------------------------------------------------------------
def _import_legacy():
    if not os.path.isdir(IMPORT_DIR):
        return
    done = {"faces": 0, "unknown": 0, "logs": 0}
    with _conn() as c:
        for sub, kind in (("Faces", "known"), ("UnknownFaces", "unknown")):
            d = os.path.join(IMPORT_DIR, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if not f.lower().endswith(".json"):
                    continue
                stem = os.path.splitext(f)[0]
                try:
                    with open(os.path.join(d, f), "r") as fh:
                        sig = json.load(fh)
                    if len(sig) != 96:
                        continue
                    image = None
                    for ext in (".jpg", ".jpeg", ".png"):
                        p = os.path.join(d, stem + ext)
                        if os.path.isfile(p):
                            with open(p, "rb") as fh:
                                image = fh.read()
                            break
                    name = re.sub(r"_\d+$", "", stem) if kind == "known" else stem
                    c.execute("INSERT INTO cs_face (name, kind, signature, image, created_at) VALUES (?, ?, ?, ?, ?)",
                              (name or stem, kind, json.dumps(sig), image, _now_s()))
                    done["faces" if kind == "known" else "unknown"] += 1
                except Exception as exc:
                    print("[SECURITY] skipped", f, "-", exc)
        old = os.path.join(IMPORT_DIR, "Database", "faces_db.db")
        if os.path.isfile(old):
            try:
                src = sqlite3.connect(old)
                rows = src.execute("SELECT name, log_timestamp FROM recognition_logs ORDER BY id").fetchall()
                src.close()
                c.executemany("INSERT INTO cs_log (name, ts) VALUES (?, ?)", rows)
                done["logs"] = len(rows)
            except Exception as exc:
                print("[SECURITY] could not read the old log database:", exc)
    try:
        os.replace(IMPORT_DIR, IMPORT_DIR + "_done")
    except OSError:
        pass
    print("[SECURITY] imported the old face program: %(faces)d profiles, %(unknown)d unknown captures, "
          "%(logs)d log rows" % done)


# ---- routes -----------------------------------------------------------------------------------------------------
def _split_ts(ts):
    d, _, t = str(ts).partition(" ")
    return d, t or "N/A"


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def register(app, db_path, guard):
    """Call once from app.py. guard = accounts._staff_only (Admins and the Owner)."""
    global _DB, _engine
    _DB = db_path
    with _conn() as c:
        c.executescript(SCHEMA)
        if "photo" not in [r[1] for r in c.execute("PRAGMA table_info(cs_face)")]:
            c.execute("ALTER TABLE cs_face ADD COLUMN photo BLOB")      # full time-stamped picture (older rows have none)
    _import_legacy()
    _engine = _Engine()

    def full_status():
        s = _engine.snapshot()
        s["installed"] = _libs_installed()
        with _conn() as c:
            s["profiles"] = c.execute("SELECT COUNT(DISTINCT name) FROM cs_face WHERE kind='known'").fetchone()[0]
            s["unknowns"] = c.execute("SELECT COUNT(*) FROM cs_face WHERE kind='unknown'").fetchone()[0]
        if not s["installed"] and s["state"] != "error":
            s["error"] = "Security needs OpenCV and mediapipe. Run: pip install opencv-python mediapipe"
        return s

    def body():
        return request.get_json(silent=True) or {}

    @app.route("/security/api/status")
    @guard
    def security_status(me):
        return jsonify(full_status())

    @app.route("/security/api/power", methods=["POST"])
    @guard
    def security_power(me):
        if body().get("on"):
            if not _libs_installed():
                return jsonify(full_status())
            _engine.start()
        else:
            _engine.stop()
        return jsonify(full_status())

    @app.route("/security/api/cameras")
    @guard
    def security_cameras(me):
        err = _load_libs()
        if err:
            return jsonify({"error": err, "cameras": []})
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        running = _engine.state in ("starting", "on")
        found = []
        for i in range(4):
            if running and i == _engine.cam_index:
                found.append(i)
                continue
            cap = cv2.VideoCapture(i, backend)
            if cap.isOpened():
                found.append(i)
            cap.release()
        return jsonify({"cameras": found or [_engine.cam_index], "active": _engine.cam_index})

    @app.route("/security/api/camera", methods=["POST"])
    @guard
    def security_camera(me):
        try:
            idx = int(body().get("index", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Bad camera number."}), 400
        if idx < 0 or idx > 9:
            return jsonify({"error": "Bad camera number."}), 400
        if _engine.state in ("starting", "on"):
            _engine.set_camera(idx)
        else:
            _engine.cam_index = _engine.want_idx = idx
            _set_setting("camera_index", idx)
        return jsonify(full_status())

    @app.route("/security/api/settings", methods=["POST"])
    @guard
    def security_settings(me):
        d = body()
        if "capture_unknown" in d:
            _engine.set_capture(d["capture_unknown"])
        if "logs" in d:
            _engine.set_logs(d["logs"])
        return jsonify(full_status())

    @app.route("/security/api/verify", methods=["POST"])
    @guard
    def security_verify(me):
        if _engine.state != "on":
            return jsonify({"success": False, "message": "Switch the camera on first."})
        _engine.start_verification()
        return jsonify({"success": True})

    def _forget_matched_unknown():
        fid = _engine.matched_unknown_id
        if fid:
            with _conn() as c:
                c.execute("DELETE FROM cs_face WHERE id=? AND kind='unknown'", (fid,))
        _engine.matched_unknown_id = None

    def _enrolled(name, fid):
        _forget_matched_unknown()
        _store.invalidate()
        with _engine.lock:
            _engine.label, _engine.recognised, _engine.kind = name, True, "known"
            _engine.session_name = None

    @app.route("/security/api/enroll", methods=["POST"])
    @guard
    def security_enroll(me):
        name = _clean_name(body().get("name"))
        if not name:
            return jsonify({"success": False, "message": "Type a name first."})
        if _engine.state != "on":
            return jsonify({"success": False, "message": "Switch the camera on first."})
        snap = _engine.fresh_face()
        if snap is None:
            return jsonify({"success": False, "message": "No face visible. Look at the camera and try again."})
        pts, crop = snap
        fid, _ = _save_face(name, "known", extract_signature(pts), _thumb(crop), _photo(_engine.fresh_photo()))
        _enrolled(name, fid)
        return jsonify({"success": True, "message": "Saved a face sample for '%s'." % name})

    @app.route("/security/api/import", methods=["POST"])
    @guard
    def security_import(me):
        name = _clean_name(request.form.get("name"))
        if not name:
            return jsonify({"success": False, "message": "Type a name first."})
        f = request.files.get("image")
        if f is None or not f.filename:
            return jsonify({"success": False, "message": "No image was chosen."})
        err = _load_libs()
        if err:
            return jsonify({"success": False, "message": err})
        try:
            img = cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return jsonify({"success": False, "message": "That file is not a readable image."})
            pts, _n = _still_detector().detect(img)
            if pts is None:
                return jsonify({"success": False, "message": "No face found in that picture."})
            h, w = img.shape[:2]
            x, y, bw, bh = _face_box(pts, w, h)
            crop = img[y:y + bh, x:x + bw]
            if crop.size == 0:
                return jsonify({"success": False, "message": "Could not outline the face in that picture."})
            fid, _ = _save_face(name, "known", extract_signature(pts), _thumb(crop), _photo(img))
        except Exception as exc:
            return jsonify({"success": False, "message": "Import failed: %s" % exc})
        _enrolled(name, fid)
        return jsonify({"success": True, "message": "Imported a face sample for '%s'." % name})

    @app.route("/security/api/profiles")
    @guard
    def security_profiles(me):
        q = (request.args.get("q") or "").strip()
        sql = ("SELECT name, COUNT(*), MIN(id) FROM cs_face WHERE kind='known'"
               + (" AND name LIKE ? ESCAPE '\\'" if q else "") + " GROUP BY name ORDER BY name COLLATE NOCASE")
        with _conn() as c:
            rows = c.execute(sql, (_like(q),) if q else ()).fetchall()
        return jsonify([{"name": r[0], "samples": r[1], "thumb": r[2]} for r in rows])

    @app.route("/security/api/face/<int:fid>.jpg")
    @guard
    def security_face_image(me, fid):
        with _conn() as c:
            row = c.execute("SELECT image FROM cs_face WHERE id=?", (fid,)).fetchone()
        if not row or not row[0]:
            return Response(status=404)
        return Response(row[0], mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.route("/security/api/photo/<int:fid>.jpg")
    @guard
    def security_photo(me, fid):
        """The full time-stamped picture when one was saved, otherwise the small face crop."""
        with _conn() as c:
            row = c.execute("SELECT photo, image FROM cs_face WHERE id=?", (fid,)).fetchone()
        data = row and (row[0] or row[1])
        if not data:
            return Response(status=404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.route("/security/api/face/<int:fid>/info")
    @guard
    def security_face_info(me, fid):
        with _conn() as c:
            row = c.execute("SELECT name, kind, created_at, photo IS NOT NULL FROM cs_face WHERE id=?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "That picture is no longer saved."}), 404
        return jsonify({"name": row[0], "kind": row[1], "created": row[2], "full": bool(row[3])})

    @app.route("/security/api/profiles/delete", methods=["POST"])
    @guard
    def security_delete(me):
        name = str(body().get("name") or "").strip()
        with _conn() as c:
            n = c.execute("DELETE FROM cs_face WHERE kind='known' AND name=?", (name,)).rowcount
        _store.invalidate()
        if not n:
            return jsonify({"success": False, "message": "No such profile."})
        return jsonify({"success": True, "message": "Deleted '%s'." % name})

    @app.route("/security/api/unknowns/clear", methods=["POST"])
    @guard
    def security_unknowns_clear(me):
        with _conn() as c:
            n = c.execute("DELETE FROM cs_face WHERE kind='unknown'").rowcount
        _store.invalidate()
        return jsonify({"success": True, "message": "Removed %d unknown capture%s." % (n, "" if n == 1 else "s")})

    @app.route("/security/api/logs")
    @guard
    def security_logs(me):
        try:
            since = max(0, int(request.args.get("since", 0)))
        except ValueError:
            since = 0
        with _conn() as c:
            last = c.execute("SELECT COALESCE(MAX(id), 0) FROM cs_log").fetchone()[0]
            if since == 0:
                rows = c.execute("SELECT id, name, ts FROM (SELECT * FROM cs_log ORDER BY id DESC LIMIT 200) "
                                 "ORDER BY id").fetchall()
            else:
                rows = c.execute("SELECT id, name, ts FROM cs_log WHERE id>? ORDER BY id LIMIT 300",
                                 (since,)).fetchall()
        entries = []
        for rid, name, ts in rows:
            d, t = _split_ts(ts)
            entries.append({"id": rid, "name": name, "date": d, "time": t})
        return jsonify({"entries": entries, "last": entries[-1]["id"] if entries and since else last})

    @app.route("/security/api/logs/clear", methods=["POST"])
    @guard
    def security_logs_clear(me):
        with _conn() as c:
            c.execute("DELETE FROM cs_log")
        return jsonify({"success": True})

    @app.route("/security/api/export")
    @guard
    def security_export(me):
        fmt = (request.args.get("format") or "text").lower()
        with _conn() as c:
            rows = c.execute("SELECT ts, name FROM cs_log ORDER BY id").fetchall()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if fmt == "json":
            data = json.dumps([{"timestamp": r[0], "recognized_target": r[1]} for r in rows], indent=4)
            fname, mime = "SecurityLog_%s.json" % stamp, "application/json"
        elif fmt == "excel":
            buf = io.StringIO()
            wr = csv.writer(buf)
            wr.writerow(["Timestamp", "Recognized Target"])
            wr.writerows(rows)
            data, fname, mime = buf.getvalue(), "SecurityLog_%s.csv" % stamp, "text/csv"
        elif fmt == "pipe":
            data = "TIMESTAMP | TARGET_IDENTITY\n" + "-" * 50 + "\n" + "".join("%s | %s\n" % r for r in rows)
            fname, mime = "SecurityLog_%s_delimited.txt" % stamp, "text/plain"
        else:
            data = "=== RASPUTIN SECURITY LOG REPORT ===\n\n" + "".join("[%s] - Event Match: %s\n" % r for r in rows)
            fname, mime = "SecurityLog_%s_report.txt" % stamp, "text/plain"
        return Response(data, mimetype=mime + "; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % fname})

    @app.route("/security/stream")
    @guard
    def security_stream(me):
        if _engine.state not in ("starting", "on"):
            return Response(status=204)
        return Response(_engine.frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    print("[SECURITY] tab ready (camera is off until you switch it on)")


def shutdown():
    """Release the camera and the detectors (called when Rasputin exits)."""
    try:
        if _engine is not None:
            _engine.stop(2.0)
    except Exception:
        pass
    try:
        if _still is not None:
            _still.close()
    except Exception:
        pass

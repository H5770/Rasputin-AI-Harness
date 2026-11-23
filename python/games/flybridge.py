"""FlyBridge (https://github.com/swotstudio/FlyBridge): toggle ON -> Minecraft (Fabric + FlyBridge mod) -> fruit-fly brain plays.

FlyBridge is NOT a server + bot. The brain is a Python program that drives a real Minecraft 1.21.4 *client* (with the
FlyBridge Fabric mod) in a singleplayer world named "Fly Colony School ...". So:  GAME console = the Minecraft client's log,
BRAIN console = the Python learner's output, and the preview on the left shows the frames the brain is looking at.

Everything is CHECKED first and only downloaded when missing (nothing that already exists is re-downloaded):
  repo        games_data/flybridge/FlyBridge (or "repo_dir" / any folder named FlyBridge next to Rasputin / in your home)
  python      repo\\.toolchain\\vision-venv, "python" in config, or Rasputin's own Python - whichever already has CUDA torch+numpy+Pillow
  mod         minecraft/client/mods/flybridge-*.jar (copied from the repo) + fabric-api jar (only downloaded if absent)
  fabric      versions/fabric-loader-0.19.5-1.21.4 in your .minecraft (only fetched if absent; needs vanilla 1.21.4 from the launcher)
  checkpoint  brain/finale/checkpoint.pt copied once into minecraft/runs/vision/tab-finale
Config: games_data/flybridge/config.json (created on first use).
One manual step, once: start the game once and create a singleplayer world whose name starts with "Fly Colony School" (e.g. "Fly Colony School 1").
After that the switch auto-loads it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from .mindcraft import Log, Proc, ANSI  # noqa: F401  (same log buffer / process helper as Mindcraft)

ID, NAME = "flybridge", "FlyBridge"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = os.path.join(ROOT, "games_data", "flybridge")
CONFIG = os.path.join(HOME, "config.json")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_WIN = os.name == "nt"

DEFAULTS = {
    "repo": "https://github.com/swotstudio/FlyBridge.git",
    "repo_dir": "",                  # existing FlyBridge folder; empty = auto-detect, else clone into games_data/flybridge/FlyBridge
    "python": "",                    # python.exe with CUDA torch/numpy/pillow; empty = auto-detect
    "minecraft_dir": "",             # your .minecraft; empty = %APPDATA%\\.minecraft
    "game_cmd": [],                  # full command to launch the game yourself (e.g. Prism: ["prismlauncher","-l","FlyBridge","-w","Fly Colony School 1"])
    "memory": "4G",                  # game heap
    "mc_version": "1.21.4",
    "fabric_loader": "0.19.5",
    "fabric_api_version": "0.119.4+1.21.4",
    "username": "FlyBrain",
    "world": "",                     # exact world folder name; empty = first folder in saves/ starting with "Fly Colony School"
    "stream": True,                  # live 60 fps view of the game window in the tab (Windows; needs pip package windows-capture)
    "hide_mode": "behind",           # once the brain runs: "behind" (stays put, under all windows), "sliver" (3 px in a screen corner), "offscreen", "none"
    "stream_port": 3100,
    "stream_fps": 60,
    "perf_mods": ["sodium", "reeses-sodium-options", "sodium-extra"],         # Modrinth projects added to the game's mods folder if absent ([] = none)
    "stream_width": 960,
    "game_wait_seconds": 600,        # how long to wait for the game + world (covers creating the world by hand the first time)
}

game_log, brain_log = Log(), Log()
game, brain, stream = Proc(game_log), Proc(brain_log), Proc(game_log)
_lock = threading.RLock()
_st = {"state": "off", "stage": "", "error": ""}
_gen = 0
_run = {"dir": None}                 # current brain run folder (frames are read from <dir>/frames)


def _set(**kw):
    with _lock:
        _st.update(kw)


# ── config + discovery ──────────────────────────────────────────────────────
def load_cfg():
    os.makedirs(HOME, exist_ok=True)
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
    except (OSError, ValueError) as exc:
        raise RuntimeError("config.json is invalid: %s" % exc)
    return cfg


def _tool(name):
    p = shutil.which(name)
    if not p:
        raise RuntimeError("'%s' was not found on PATH. Install it and restart Rasputin." % name)
    return p


def _is_repo(p):
    return bool(p) and os.path.isfile(os.path.join(p, "minecraft", "vision", "resource_finale_runner.py"))


def find_repo(cfg):
    home = os.path.expanduser("~")
    cands = [cfg["repo_dir"], os.path.join(HOME, "FlyBridge"), os.path.join(ROOT, "FlyBridge"),
             os.path.join(os.path.dirname(ROOT), "FlyBridge"), os.path.join(home, "FlyBridge"),
             os.path.join(home, "Desktop", "FlyBridge"), os.path.join(home, "Documents", "FlyBridge"),
             os.path.join(home, "Downloads", "FlyBridge"), os.path.join(home, "Downloads", "FlyBridge-main"),
             os.path.join(home, "Downloads", "FlyBridge-master")]
    for p in cands:
        if _is_repo(p):
            return os.path.abspath(p)
    return ""


def ensure_repo(cfg):
    repo = find_repo(cfg)
    if repo:
        brain_log.add("[setup] Using existing FlyBridge at %s" % repo)
        return repo
    if cfg["repo_dir"]:
        raise RuntimeError('"repo_dir" in config.json does not look like a FlyBridge folder: %s' % cfg["repo_dir"])
    dest = os.path.join(HOME, "FlyBridge")
    brain_log.add("[setup] FlyBridge not found anywhere, cloning it (about 180 MB, first run only)...")
    if brain.run([_tool("git"), "clone", "--depth", "1", cfg["repo"], dest], HOME):
        raise RuntimeError("git clone failed (see BRAIN log).")
    return dest


def _py_ok(py):
    if not py or not os.path.isfile(py):
        return False
    try:
        r = subprocess.run([py, "-c", "import torch,numpy,PIL;assert torch.cuda.is_available()"], capture_output=True,
                           timeout=120, creationflags=NOWIN, cwd=HOME)
        return r.returncode == 0
    except Exception:
        return False


def ensure_python(cfg, repo):
    venv = os.path.join(repo, ".toolchain", "vision-venv", "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")
    me = sys.executable
    if me.lower().endswith("pythonw.exe") and os.path.isfile(me[:-5] + ".exe"):
        me = me[:-5] + ".exe"                       # Rasputin runs under pythonw; the brain needs a console-capable python.exe
    for py in (cfg["python"], venv, me):
        if _py_ok(py):
            brain_log.add("[setup] Using Python with CUDA torch: %s" % py)
            return py
    if not IS_WIN:
        raise RuntimeError("No Python with CUDA torch + numpy + pillow found. Set \"python\" in games_data/flybridge/config.json.")
    brain_log.add("[setup] No existing Python has CUDA torch, numpy and pillow. Running FlyBridge's setup-learner.ps1 (one time)...")
    ps = shutil.which("powershell") or "powershell"
    if brain.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(repo, "scripts", "setup-learner.ps1")], repo):
        raise RuntimeError("setup-learner.ps1 failed (see BRAIN log). Needs Python 3.14 on PATH and an NVIDIA GPU.")
    if not _py_ok(venv):
        raise RuntimeError("Learner environment was created but CUDA torch is not working (see BRAIN log).")
    return venv


def mc_dir(cfg):
    d = cfg["minecraft_dir"] or os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), ".minecraft")
    return d


def _download(url, dest, log=game_log):
    log.add("[setup] downloading %s" % url)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Rasputin-FlyBridge/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(dest + ".part", dest)


def _modrinth_jar(project, cfg, mods):
    pat = re.compile(r"^%s-(fabric-)?\d" % re.escape(project.lower()))     # "sodium-" must not match sodium-extra-*.jar
    have = [f for f in os.listdir(mods) if pat.match(f.lower()) and f.endswith(".jar")]
    if have:
        return
    api = ("https://api.modrinth.com/v2/project/%s/version?game_versions=%%5B%%22%s%%22%%5D&loaders=%%5B%%22fabric%%22%%5D"
           % (project, cfg["mc_version"]))
    with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "Rasputin-FlyBridge/1.0"}), timeout=30) as r:
        vs = json.load(r)
    v = next((x for x in vs if x.get("version_type") == "release"), vs[0] if vs else None)
    if not v:
        raise RuntimeError("no %s build for Minecraft %s on Modrinth" % (project, cfg["mc_version"]))
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    _download(f["url"], os.path.join(mods, f["filename"]))


def ensure_mod(cfg, repo):
    mods = os.path.join(repo, "minecraft", "client", "mods")
    os.makedirs(mods, exist_ok=True)
    src_dir = os.path.join(repo, "mod", "releases")
    jars = [f for f in os.listdir(mods) if re.match(r"flybridge-.*\.jar$", f)]
    if not jars:
        src = os.path.join(src_dir, "flybridge-0.9.2.jar")
        if not os.path.isfile(src):
            raise RuntimeError("mod/releases/flybridge-0.9.2.jar is missing from the FlyBridge folder.")
        shutil.copy2(src, mods)
        game_log.add("[setup] installed flybridge-0.9.2.jar")
    if not any(re.match(r"fabric-api-.*\.jar$", f) for f in os.listdir(mods)):
        ver = cfg["fabric_api_version"]
        api = "https://api.modrinth.com/v2/project/fabric-api/version?game_versions=%5B%22" + cfg["mc_version"] + "%22%5D&loaders=%5B%22fabric%22%5D"
        with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "Rasputin-FlyBridge/1.0"}), timeout=30) as r:
            vs = json.load(r)
        v = next((x for x in vs if x["version_number"].startswith(ver)), None)
        if not v:
            raise RuntimeError("Fabric API %s was not found on Modrinth. Put fabric-api-*.jar in %s" % (ver, mods))
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        _download(f["url"], os.path.join(mods, f["filename"]))
    for proj in cfg["perf_mods"]:                       # performance mods (Sodium): only downloaded when absent
        try:
            _modrinth_jar(proj, cfg, mods)
        except Exception as exc:
            game_log.add("[warn] could not add %s (%s); continuing without it." % (proj, exc))


def ensure_options(repo):
    """Never pause when the game window loses focus, so clicking elsewhere (the Rasputin page, another app) doesn't stop the brain."""
    path = os.path.join(repo, "minecraft", "client", "options.txt")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if not ln.startswith(("pauseOnLostFocus:", "fullscreen:"))]
    lines += ["pauseOnLostFocus:false", "fullscreen:false"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def stage_checkpoint(repo):
    d = os.path.join(repo, "minecraft", "runs", "vision", "tab-finale")
    os.makedirs(d, exist_ok=True)
    for n in ("checkpoint.pt", "checkpoint.pt.json", "farm-demonstrations.pt"):
        dst, src = os.path.join(d, n), os.path.join(repo, "brain", "finale", n)
        if not os.path.exists(dst):
            if not os.path.exists(src):
                raise RuntimeError("brain/finale/%s is missing from the FlyBridge folder." % n)
            shutil.copy2(src, dst)
    studio = os.path.join(repo, ".toolchain", "studio-session.json")
    os.makedirs(os.path.dirname(studio), exist_ok=True)
    if not os.path.exists(studio):
        with open(studio, "w") as f:
            f.write("{}")
    return os.path.join(d, "checkpoint.pt")


# ── launching the Minecraft client (Fabric profile, built from your existing launcher files) ──────────────
def _lib_ok(lib):
    for r in lib.get("rules", []):
        os_ = r.get("os")
        hit = True
        if os_:
            if os_.get("name") and os_["name"] != ("windows" if IS_WIN else "linux"):
                hit = False
            if os_.get("arch") and os_["arch"] in ("x86", "arm64"):
                hit = False
        if r.get("features"):
            hit = False
        if r["action"] == "allow" and not hit:
            return False
        if r["action"] == "disallow" and hit:
            return False
    if lib.get("rules") and not any(r["action"] == "allow" for r in lib["rules"]):
        return False
    return True


def _rules_ok(rules):
    if not rules:
        return True
    ok = False
    for r in rules:
        hit = not r.get("features") and (not r.get("os") or (r["os"].get("name") in (None, "windows" if IS_WIN else "linux")
                                                           and r["os"].get("arch") not in ("x86", "arm64")))
        if hit:
            ok = r["action"] == "allow"
    return ok


def _ensure_fabric_json(cfg, mc):
    ver = "fabric-loader-%s-%s" % (cfg["fabric_loader"], cfg["mc_version"])
    path = os.path.join(mc, "versions", ver, ver + ".json")
    if not os.path.isfile(path):
        van = os.path.join(mc, "versions", cfg["mc_version"], cfg["mc_version"] + ".json")
        if not os.path.isfile(van):
            raise RuntimeError("Minecraft %s is not installed. Open the official launcher and run %s once." % (cfg["mc_version"], cfg["mc_version"]))
        _download("https://meta.fabricmc.net/v2/versions/loader/%s/%s/profile/json" % (cfg["mc_version"], cfg["fabric_loader"]), path)
    return ver, path


def build_game_cmd(cfg, repo):
    mc = mc_dir(cfg)
    ver, fpath = _ensure_fabric_json(cfg, mc)
    with open(fpath, encoding="utf-8") as f:
        fab = json.load(f)
    with open(os.path.join(mc, "versions", cfg["mc_version"], cfg["mc_version"] + ".json"), encoding="utf-8") as f:
        van = json.load(f)
    libdir = os.path.join(mc, "libraries")
    cp, seen = [], set()
    for lib in fab["libraries"] + van["libraries"]:
        if not _lib_ok(lib):
            continue
        group, art, vers = (lib["name"].split(":") + [""])[:3]
        classifier = lib["name"].split(":")[3] if lib["name"].count(":") >= 3 else ""
        key = (group, art, classifier)
        if key in seen:                      # fabric's (newer) copy wins because it was listed first
            continue
        seen.add(key)
        rel = lib.get("downloads", {}).get("artifact", {}).get("path") or "%s/%s/%s/%s-%s%s.jar" % (
            group.replace(".", "/"), art, vers, art, vers, "-" + classifier if classifier else "")
        p = os.path.join(libdir, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            url = lib.get("downloads", {}).get("artifact", {}).get("url") or (lib.get("url", "https://libraries.minecraft.net/") + rel)
            _download(url, p)
        cp.append(p)
    cp.append(os.path.join(mc, "versions", cfg["mc_version"], cfg["mc_version"] + ".jar"))
    sep = os.pathsep
    gdir = os.path.join(repo, "minecraft", "client")
    natives = os.path.join(HOME, "natives")
    os.makedirs(natives, exist_ok=True)
    sub = {"natives_directory": natives, "launcher_name": "Rasputin", "launcher_version": "1", "classpath": sep.join(cp),
           "classpath_separator": sep, "library_directory": libdir, "version_name": ver, "game_directory": gdir,
           "assets_root": os.path.join(mc, "assets"), "assets_index_name": van["assetIndex"]["id"],
           "auth_player_name": cfg["username"], "auth_uuid": str(uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + cfg["username"])),
           "auth_access_token": "0", "clientid": "0", "auth_xuid": "0", "user_type": "legacy", "version_type": "release",
           "resolution_width": "960", "resolution_height": "540"}
    fill = lambda s: re.sub(r"\$\{(\w+)\}", lambda m: str(sub.get(m.group(1), "")), s)

    def flat(args):
        out = []
        for a in args:
            if isinstance(a, str):
                out.append(fill(a))
            elif _rules_ok(a.get("rules")):
                v = a["value"]
                out += [fill(x) for x in ([v] if isinstance(v, str) else v)]
        return out
    jvm = flat(van["arguments"]["jvm"] + fab.get("arguments", {}).get("jvm", []))
    gm = flat(van["arguments"]["game"] + fab.get("arguments", {}).get("game", []))
    world = cfg["world"]
    saves = os.path.join(gdir, "saves")
    if not world and os.path.isdir(saves):
        world = next((d for d in sorted(os.listdir(saves)) if d.startswith("Fly Colony School")), "")
    if world:
        gm += ["--quickPlaySingleplayer", world]
    else:
        game_log.add("[setup] No 'Fly Colony School' world yet. When the game opens, create a singleplayer world whose name "
                     "starts with 'Fly Colony School' (e.g. Fly Colony School 1, any game mode). It will be loaded automatically from then on.")
    return [_tool("java"), "-Xmx" + cfg["memory"]] + jvm + [fab["mainClass"]] + gm, gdir


# ── mod bridge + lifecycle ──────────────────────────────────────────────────
def _session(repo):
    p = os.path.join(repo, "minecraft", "client", "config", "flybridge-session.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), os.path.getmtime(p)
    except (OSError, ValueError):
        return None, 0


def _mod_status(desc):
    try:
        req = urllib.request.Request(desc["baseUrl"] + "/status", headers={"Authorization": "Bearer " + desc["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _no_throttle(pids):
    """Windows 11 puts background / windowless processes in 'efficiency mode' (slow cores, low clocks). Opt our processes out."""
    if not IS_WIN:
        return
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE

    class PPT(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]
    for pid in pids:
        h = k.OpenProcess(0x0200, False, pid)               # PROCESS_SET_INFORMATION
        if h:
            st = PPT(1, 1, 0)                               # EXECUTION_SPEED controlled by us, throttling OFF
            k.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))
            k.CloseHandle(h)


def _boost():
    try:
        import psutil
        pids = []
        for pr in (game, brain, stream):
            if pr.alive():
                pids.append(pr.p.pid)
                try:
                    pids += [c.pid for c in psutil.Process(pr.p.pid).children(recursive=True)]
                except psutil.Error:
                    pass
        _no_throttle(pids)
    except Exception:
        pass


def kill_orphans(repo):
    """Kill leftover game/brain processes of this FlyBridge folder (e.g. from before a Rasputin restart or a crash)."""
    if not repo:
        return
    try:
        import psutil
        gdir = os.path.normcase(os.path.join(repo, "minecraft", "client"))
        mine = os.getpid()
        for pr in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cl = " ".join(pr.info["cmdline"] or [])
                if pr.pid != mine and (
                        (pr.info["name"] or "").lower().startswith("java") and gdir in os.path.normcase(cl)
                        or "minecraft.vision." in cl and "-m" in (pr.info["cmdline"] or [])):
                    pr.kill()
            except psutil.Error:
                pass
    except Exception as exc:
        game_log.add("[warn] orphan cleanup failed: %s" % exc)


def clear_lock(repo):
    """The runner's lease file is left behind when its process is killed; with no runner alive it is stale."""
    if brain.alive():
        return
    try:
        os.remove(os.path.join(repo, ".toolchain", "school-controller.lock"))
        brain_log.add("[setup] removed a stale lock from an earlier run")
    except OSError:
        pass


def release_mod(repo):
    """If an earlier brain left the mod 'owned', tell it to stop so a new run can take over."""
    desc, _ = _session(repo)
    st = _mod_status(desc) if desc else None
    if st and st.get("active"):
        try:
            req = urllib.request.Request(desc["baseUrl"] + "/stop", data=b"{}", method="POST",
                                         headers={"Authorization": "Bearer " + desc["token"], "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            brain_log.add("[setup] released the game from an earlier brain run")
            time.sleep(1)
        except Exception as exc:
            brain_log.add("[warn] could not release the game: %s" % exc)


def start_stream(cfg, py):
    """Live MJPEG view of the game window (flybridge_stream.py). Optional: on any problem the tab falls back to the brain's own frames."""
    if not cfg["stream"] or not IS_WIN or stream.alive():
        return
    try:
        ok = subprocess.run([py, "-c", "import windows_capture, PIL, numpy"], capture_output=True, creationflags=NOWIN, cwd=HOME).returncode == 0
        if not ok:
            game_log.add("[setup] installing windows-capture for the live view (one time, small)...")
            if stream.run([py, "-m", "pip", "install", "windows-capture", "pillow", "numpy"], HOME):
                raise RuntimeError("pip install windows-capture failed")
        stream.start([py, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "flybridge_stream.py"),
                      "--port", str(cfg["stream_port"]), "--fps", str(cfg["stream_fps"]), "--width", str(cfg["stream_width"])], HOME)
    except Exception as exc:
        game_log.add("[warn] live view unavailable (%s); showing the brain's own frames instead." % exc)


_perf = {"game": 0.0, "stream": 0.0, "rf": None, "t": 0.0}


def _stream_ok(port):
    if not stream.alive():
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % int(port), timeout=0.3) as r:
            h = json.load(r)
        _perf["stream"] = h.get("out_fps", 0)
        _perf["cap"] = h.get("cap_fps", 0)
        return bool(h.get("ok"))
    except Exception:
        return False


def _sample_fps(desc):
    """Game FPS = how fast the mod's render-frame counter advances (the real Minecraft frame rate)."""
    st = _mod_status(desc)
    now = time.time()
    if st and "renderFrames" in st:
        rf = st["renderFrames"]
        if _perf["rf"] is not None and rf >= _perf["rf"] and now > _perf["t"]:
            _perf["game"] = round((rf - _perf["rf"]) / (now - _perf["t"]), 1)
        _perf["rf"], _perf["t"] = rf, now
    return st


def _teardown():
    stream.kill()
    brain.kill()
    if game.alive():
        game.kill()
    elif _extern["pid"]:
        try:
            import psutil
            for pr in psutil.Process(_extern["pid"]).children(recursive=True) + [psutil.Process(_extern["pid"])]:
                pr.kill()
        except Exception:
            pass
    _extern["pid"] = 0
    try:
        kill_orphans(find_repo(load_cfg()))
    except Exception:
        pass


_extern = {"pid": 0}


def _launch(gen):
    live = lambda: gen == _gen
    try:
        cfg = load_cfg()
        _set(stage="Checking FlyBridge files")
        repo = ensure_repo(cfg)
        py = ensure_python(cfg, repo)
        if not live():
            return
        ckpt = stage_checkpoint(repo)
        ensure_mod(cfg, repo)
        clear_lock(repo)
        kill_orphans(repo)                              # a game left over from a crash/restart would hold the mouse and the mod
        time.sleep(2)
        t0 = time.time()
        desc, mt = _session(repo)
        already = desc is not None and _mod_status(desc) is not None
        if already:
            game_log.add("[setup] Minecraft with FlyBridge is already running, attaching to it.")
        else:
            _set(stage="Starting Minecraft")
            ensure_options(repo)
            if cfg["game_cmd"]:
                cmd, cwd = list(cfg["game_cmd"]), repo
            else:
                cmd, cwd = build_game_cmd(cfg, repo)
            game.start(cmd, cwd)
            try:                                                     # keep Windows from treating the game as a background app
                import psutil
                psutil.Process(game.p.pid).nice(getattr(psutil, "HIGH_PRIORITY_CLASS", 0) or -5)
            except Exception:
                pass
            start_stream(cfg, py)
            deadline = time.time() + float(cfg["game_wait_seconds"])
            _set(stage="Waiting for the game and a 'Fly Colony School' world")
            while live():
                desc, mt = _session(repo)
                if desc and mt >= t0 - 1 and _mod_status(desc) is not None:
                    break
                if not cfg["game_cmd"] and not game.alive():
                    raise RuntimeError("Minecraft exited during startup (see GAME log).")
                if time.time() > deadline:
                    raise RuntimeError("The game did not report a Fly Colony School world in time (see GAME log).")
                time.sleep(1)
            if not live():
                return
            time.sleep(3)
        start_stream(cfg, py)
        _set(stage="Starting the fly brain")
        # The mod's session file appears as soon as the game starts, before any world is open, so the brain may be
        # refused ("Open the single-player school world"). Keep retrying until the world is open (or time runs out).
        deadline = time.time() + float(cfg["game_wait_seconds"])
        started, started_seq, fails = 0, 0, 0
        while live():
            if not brain.alive():
                if started and brain.p.returncode == 0:
                    break
                if started and brain.p.returncode not in (0, None) and not brain_log.has("single-player school world", started_seq):
                    fails += 1
                    if fails > 3:
                        raise RuntimeError("The brain stopped with an error (see BRAIN log). Switch off and on, or type 'restart'.")
                    brain_log.add("[setup] brain stopped, trying again (%d/3)..." % fails)
                    time.sleep(3)
                if time.time() > deadline:
                    raise RuntimeError("No 'Fly Colony School' world was opened in time (see GAME log).")
                if started:
                    _set(stage="Waiting for you to open a 'Fly Colony School' world in the game")
                    time.sleep(5)
                started_seq = brain_log.seq
                _start_brain(py, repo, ckpt)
                started = time.time()
            elif time.time() - started > 20:
                break                                   # survived startup: it is in the world and running
            time.sleep(1)
        if not live():
            return
        _set(state="running", stage="")
        if cfg["hide_mode"] != "none" and cfg["stream"]:
            time.sleep(2)
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/hide?mode=%s" % (int(cfg["stream_port"]), cfg["hide_mode"]), timeout=3).read()
                fps_before = _perf["game"]
                _boost()
                game_log.add("[stream] game window parked out of the way (%s). It keeps running; switch OFF to close it" % cfg["hide_mode"] + "")
            except Exception as exc:
                game_log.add("[warn] could not move the game window: %s" % exc)
        fps_before = _perf["game"]
        misses, tick, pending_report = 0, 0, time.time() + 8 if cfg["hide_mode"] != "none" else 0
        while live():
            time.sleep(1)
            tick += 1
            if tick % 10 == 0:
                _boost()
            if pending_report and time.time() > pending_report:
                pending_report = 0
                game_log.add("[perf] game fps before parking: %s, 8 s after: %s" % (fps_before, _perf["game"]))
            if brain.p is not None and not brain.alive() and brain.p.returncode not in (0, None) and live():
                fails += 1
                if fails > 3:
                    raise RuntimeError("The brain stopped with an error (see BRAIN log). Switch off and on, or type 'restart'.")
                st = _mod_status(desc) or {}
                brain_log.add("[setup] brain stopped (game says: %s). Restarting (%d/3)..." % (st.get("stopReason") or "no reason given", fails))
                time.sleep(3)
                _start_brain(py, repo, ckpt)
            misses = misses + 1 if _sample_fps(desc) is None else 0
            if misses >= 5 or (not cfg["game_cmd"] and not already and not game.alive()):
                raise RuntimeError("Minecraft was closed.")
    except Exception as exc:
        if live():
            _teardown()
            _set(state="error", stage="", error=str(exc))
            (brain_log if "BRAIN" in str(exc) else game_log).add("[error] %s" % exc)


def _report_run(p, run_dir):
    """When the brain exits, print WHY into the BRAIN console (result summary + last events + the game's own stop reason)."""
    try:
        p.wait()
        time.sleep(1)
        try:
            with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
                sm = json.load(f)
        except (OSError, ValueError):
            return
        brain_log.add("[result] phase=%s passed=%s error=%s" % (sm.get("phase"), sm.get("passed"), sm.get("error")))
        for i, ev in enumerate(sm.get("evaluation") or []):
            simple = {k: v for k, v in ev.items() if isinstance(v, (str, int, float, bool)) or v is None}
            brain_log.add("[result] attempt %d: %s" % (i + 1, json.dumps(simple)[:900]))
            if ev.get("stageTransitions"):
                brain_log.add("[result] stages reached: %s" % json.dumps(ev["stageTransitions"])[:600])
        try:
            with open(os.path.join(run_dir, "events.jsonl"), encoding="utf-8") as f:
                for ln in f.read().splitlines()[-6:]:
                    brain_log.add("[events] " + ln[:300])
        except OSError:
            pass
    except Exception as exc:
        brain_log.add("[warn] could not read the run result: %s" % exc)


def _start_brain(py, repo, ckpt):
    if brain.alive():
        brain.kill()
        time.sleep(0.5)
    clear_lock(repo)
    release_mod(repo)
    rid = "tab-%d" % int(time.time())
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    _run["dir"] = os.path.join(repo, "minecraft", "runs", "vision", "development-" + rid)
    brain_log.add("[setup] run id %s" % rid)
    brain.start([py, "-u", "-m", "minecraft.vision.resource_finale_runner", "--id", rid, "--checkpoint", ckpt], repo, env)
    threading.Thread(target=_report_run, args=(brain.p, _run["dir"]), daemon=True).start()


def power(on):
    global _gen
    with _lock:
        if on and _st["state"] in ("off", "error"):
            _gen += 1
            _set(state="starting", stage="Starting", error="")
            threading.Thread(target=_launch, args=(_gen,), daemon=True).start()
        elif not on and _st["state"] in ("starting", "running", "error"):
            _gen += 1
            _set(state="stopping", stage="Stopping", error="")

            def _stop():
                _teardown()
                _set(state="off", stage="")
            threading.Thread(target=_stop, daemon=True).start()


def _frames_dir():
    d = _run["dir"]
    return os.path.join(d, "frames") if d else ""


def frame():
    """Newest complete PNG the brain wrote (bytes) or None."""
    fd = _frames_dir()
    try:
        files = [os.path.join(fd, n) for n in os.listdir(fd) if n.endswith(".png")]
        files.sort(key=os.path.getmtime, reverse=True)
        for p in files[:3]:
            with open(p, "rb") as f:
                b = f.read()
            if b.endswith(b"IEND\xaeB`\x82"):
                return b
    except OSError:
        pass
    return None


def status():
    try:
        cfg = load_cfg()
    except Exception:
        cfg = DEFAULTS
    with _lock:
        s = dict(_st)
    fd = _frames_dir()
    has = False
    try:
        has = s["state"] == "running" and bool(fd) and any(n.endswith(".png") for n in os.listdir(fd))
    except OSError:
        pass
    s.update(server=game.alive(), bot=brain.alive(), mc_version=cfg["mc_version"], mc_port="local mod", viewer_ready=has,
             viewer_port=0, stream_port=cfg["stream_port"], stream_ready=s["state"] in ("starting", "running") and _stream_ok(cfg["stream_port"]),
             game_fps=_perf["game"] or _perf.get("cap", 0), stream_fps=_perf["stream"])
    return s


def log(src, since):
    return (brain_log if src == "bot" else game_log).since(since)


def clear(src):
    (brain_log if src == "bot" else game_log).clear()


def command(text):
    """'restart' runs the brain again in the same world (the runner builds a fresh finale arena)."""
    if text.strip().lower().lstrip("/") != "restart" or _st["state"] != "running":
        return False
    try:
        cfg = load_cfg()
        repo = find_repo(cfg)
        py = ensure_python(cfg, repo)
        _start_brain(py, repo, stage_checkpoint(repo))
        return True
    except Exception as exc:
        brain_log.add("[error] %s" % exc)
        return False


def shutdown():
    global _gen
    _gen += 1
    _teardown()

"""Mindcraft (https://github.com/mindcraft-bots/mindcraft): toggle ON -> vanilla Minecraft server -> Mindcraft bot.

Layout created on first use (all under <project>/games_data/mindcraft):
  config.json      settings (version, ports, profile, extra env such as API keys)
  server/          server-<version>.jar, world, eula.txt, server.properties
  mindcraft/       git clone of the Mindcraft repo (+ node_modules)
Needs on PATH: Java 21+, Node.js 18+, npm, git.
Switch ON also loads Qwen2.5-7B into Rasputin (llm_api.load_qwen); switch OFF frees it again. Chat keeps using Phi-4.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque

import psutil

ID, NAME = "mindcraft", "Mindcraft"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = os.path.join(ROOT, "games_data", "mindcraft")
SERVER_DIR = os.path.join(HOME, "server")
REPO_DIR = os.path.join(HOME, "mindcraft")
CONFIG = os.path.join(HOME, "config.json")
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

DEFAULTS = {
    "mc_version": "1.21.6",
    "mc_port": 25565,
    "memory": "2G",                  # server heap
    "viewer_port": 3000,             # Mindcraft's first-person viewer (render_bot_view); bot 2 would be 3001...
    "profile": "./andy.json",        # Mindcraft profile (model, prompts); edit/replace in the repo
    "accept_eula": False,            # must be true: https://aka.ms/MinecraftEULA
    "repo": "https://github.com/mindcraft-bots/mindcraft.git",
    "env": {},                       # extra env for the bot, e.g. {"OPENAI_API_KEY": "sk-..."}
}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ── log buffer + child process ──────────────────────────────────────────────
class Log:
    def __init__(self, size=2500):
        self.q, self.seq, self.lock = deque(maxlen=size), 0, threading.Lock()

    def add(self, text):
        with self.lock:
            for line in ANSI.sub("", str(text)).replace("\r", "").split("\n"):
                if line.strip():
                    self.seq += 1
                    self.q.append((self.seq, line[:1000]))

    def since(self, i):
        with self.lock:
            return [{"i": s, "t": t} for s, t in self.q if s > i], self.seq

    def has(self, needle, after=0):
        with self.lock:
            return any(s > after and needle in t for s, t in self.q)

    def clear(self):
        with self.lock:
            self.q.clear()


class Proc:
    def __init__(self, log):
        self.p, self.log = None, log

    def start(self, cmd, cwd, env=None):
        self.p = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, args=(self.p,), daemon=True).start()

    def _pump(self, p):
        for line in p.stdout:
            self.log.add(line)
        self.log.add("[process exited with code %s]" % p.wait())

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def send(self, text):
        try:
            self.p.stdin.write(text + "\n")
            self.p.stdin.flush()
            return True
        except Exception:
            return False

    def run(self, cmd, cwd):
        """Run a setup command to completion, streaming its output; returns the exit code."""
        self.start(cmd, cwd)
        return self.p.wait()

    def kill(self):
        if not self.alive():
            return
        try:
            root = psutil.Process(self.p.pid)
            kids = root.children(recursive=True)
            for pr in kids + [root]:
                try:
                    pr.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass


server_log, bot_log = Log(), Log()
server, bot = Proc(server_log), Proc(bot_log)
_lock = threading.RLock()
_st = {"state": "off", "stage": "", "error": ""}   # state: off | starting | running | stopping | error
_gen = 0                                           # bumped by stop(); cancels a launch that is still in progress


def _set(**kw):
    with _lock:
        _st.update(kw)


# ── config + preparation ────────────────────────────────────────────────────
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


def _java():
    java = _tool("java")
    out = subprocess.run([java, "-version"], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stderr
    m = re.search(r'version "(\d+)', out)
    if not m or int(m.group(1)) < 21:
        raise RuntimeError("Minecraft 1.21.6 needs Java 21 or newer (found: %s)." % (out.splitlines() or ["none"])[0])
    return java


def _get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def ensure_server_jar(cfg):
    ver = cfg["mc_version"]
    jar = os.path.join(SERVER_DIR, "server-%s.jar" % ver)
    if os.path.exists(jar):
        return jar
    server_log.add("[setup] Downloading Minecraft %s server..." % ver)
    entry = next((v for v in _get_json(MANIFEST)["versions"] if v["id"] == ver), None)
    if not entry:
        raise RuntimeError("Minecraft %s is not in Mojang's version list." % ver)
    url = _get_json(entry["url"])["downloads"]["server"]["url"]
    urllib.request.urlretrieve(url, jar + ".part")
    os.replace(jar + ".part", jar)
    server_log.add("[setup] Saved %s" % os.path.basename(jar))
    return jar


def prepare_server_files(cfg):
    if not cfg.get("accept_eula"):
        raise RuntimeError('Set "accept_eula": true in games_data/mindcraft/config.json to accept the Minecraft EULA '
                           "(https://aka.ms/MinecraftEULA).")
    with open(os.path.join(SERVER_DIR, "eula.txt"), "w") as f:
        f.write("eula=true\n")
    want = {"server-port": cfg["mc_port"], "online-mode": "false",      # offline: the bot has no Microsoft account
            "motd": "Mindcraft", "enable-command-block": "false"}
    path, lines, seen = os.path.join(SERVER_DIR, "server.properties"), [], set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                k = ln.split("=", 1)[0].strip()
                if k in want:
                    ln, _ = "%s=%s" % (k, want[k]), seen.add(k)
                lines.append(ln)
    lines += ["%s=%s" % (k, v) for k, v in want.items() if k not in seen]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def ensure_mindcraft(cfg):
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        bot_log.add("[setup] Cloning Mindcraft...")
        if bot.run([_tool("git"), "clone", "--depth", "1", cfg["repo"], REPO_DIR], HOME):
            raise RuntimeError("git clone failed (see BOT log).")
    if not os.path.isdir(os.path.join(REPO_DIR, "node_modules")):
        bot_log.add("[setup] npm install (first run only, a few minutes)...")
        if bot.run([_tool("npm"), "install"], REPO_DIR):
            raise RuntimeError("npm install failed (see BOT log).")


def server_cmd(cfg, jar):
    return [_java(), "-Xmx" + cfg["memory"], "-jar", jar, "nogui"]


def bot_cmd(cfg):
    return [_tool("node"), "main.js"]


def bot_env(cfg):
    env = dict(os.environ, **{k: str(v) for k, v in cfg["env"].items()})
    env["SETTINGS_JSON"] = json.dumps({            # overrides Mindcraft's settings.js
        "minecraft_version": cfg["mc_version"], "host": "127.0.0.1", "port": int(cfg["mc_port"]), "auth": "offline",
        "profiles": [cfg["profile"]], "render_bot_view": True,
        "auto_open_ui": False,                     # never pop a browser window; the UI lives in this tab
        # a small local model (Qwen2.5-7B): a smaller prompt is faster and fits the context
        "show_command_syntax": "shortened", "max_messages": 10, "num_examples": 1, "relevant_docs_count": 3,
        "allow_vision": False,
    })
    return env


# ── lifecycle ───────────────────────────────────────────────────────────────
def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def _teardown():
    bot.kill()
    if server.alive():
        server.send("stop")
        for _ in range(60):
            if not server.alive():
                break
            time.sleep(0.5)
        server.kill()
    try:
        from . import llm_api
        llm_api.unload_qwen()          # give Qwen2.5's VRAM back; the chat tab's Phi-4 is untouched
    except Exception as exc:
        server_log.add("[warn] could not unload Qwen2.5: %s" % exc)


def _start_qwen_load():
    """Load Qwen2.5 on a side thread while the Minecraft server boots. Returns (thread, result) where result['err'] is set on failure."""
    from . import llm_api
    result = {"err": None}

    def work():
        try:
            llm_api.load_qwen()
        except Exception as exc:
            result["err"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, result


def _launch(gen):
    live = lambda: gen == _gen
    try:
        cfg = load_cfg()
        os.makedirs(SERVER_DIR, exist_ok=True)
        _set(stage="Preparing Minecraft server")
        _tool("node"), _tool("npm"), _tool("git")
        cmd = server_cmd(cfg, ensure_server_jar(cfg))
        prepare_server_files(cfg)
        mark = server_log.seq
        _set(stage="Starting Minecraft %s, loading Qwen2.5" % cfg["mc_version"])
        qwen_thread, qwen = _start_qwen_load()
        server.start(cmd, SERVER_DIR)
        while live() and not server_log.has("Done (", mark):
            if not server.alive():
                raise RuntimeError("Minecraft server exited during startup (see SERVER log).")
            time.sleep(0.5)
        if not live():
            return
        _set(stage="Preparing Mindcraft")
        ensure_mindcraft(cfg)
        if not live():
            return
        if qwen_thread.is_alive():
            _set(stage="Loading Qwen2.5")
            while live() and qwen_thread.is_alive():
                qwen_thread.join(0.5)
            if not live():
                return
        if qwen["err"]:
            raise RuntimeError("Qwen2.5 failed to load: %s" % qwen["err"])
        _set(stage="Connecting bot")
        bot.start(bot_cmd(cfg), REPO_DIR, bot_env(cfg))
        time.sleep(2)
        if not bot.alive():
            raise RuntimeError("Mindcraft exited immediately (see BOT log).")
        _set(state="running", stage="")
        while live() and server.alive() and bot.alive():
            time.sleep(1)
        if live():
            raise RuntimeError("%s stopped unexpectedly." % ("Server" if not server.alive() else "Bot"))
    except Exception as exc:
        if live():
            _teardown()
            _set(state="error", stage="", error=str(exc))
            (bot_log if "BOT" in str(exc) else server_log).add("[error] %s" % exc)


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


def status():
    try:
        cfg = load_cfg()
    except Exception:
        cfg = DEFAULTS
    with _lock:
        s = dict(_st)
    s.update(server=server.alive(), bot=bot.alive(), mc_version=cfg["mc_version"], mc_port=cfg["mc_port"],
             viewer_port=cfg["viewer_port"],
             viewer_ready=s["state"] == "running" and _port_open(cfg["viewer_port"]))
    return s


def log(src, since):
    return (bot_log if src == "bot" else server_log).since(since)


def clear(src):
    (bot_log if src == "bot" else server_log).clear()


def command(text):
    return bool(text.strip()) and server.alive() and server.send(text.strip().lstrip("/"))


def shutdown():
    global _gen
    _gen += 1
    _teardown()

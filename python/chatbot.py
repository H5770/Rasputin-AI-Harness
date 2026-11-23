# chatbot.py
"""
Optimised version of your original Rasputin bot.
– Keeps *exactly* the same public API (build_prompt, generate_response)
– Adds internal efficiency tricks: singleton model, smart thread/batch choice,
  GPU offload when available, memory hygiene, and an optional token-streaming
  generator for GUIs.
"""

from __future__ import annotations

import gc
import glob
import inspect
import os
import re
import threading
import time
import sys
import platform
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Generator, List

# ── Windows: let llama.cpp find the CUDA DLLs that pip installed in the venv ──
# Must run BEFORE "import llama_cpp". llama-cpp-python loads llama.dll in a mode
# that only searches PATH, so the folders are added to both PATH and the DLL dirs.
if os.name == "nt":
    import site
    _roots = [os.path.join(sys.prefix, "Lib", "site-packages")]
    try:
        _roots += site.getsitepackages() + [site.getusersitepackages()]
    except Exception:
        pass
    _dirs = set()
    for _r in _roots:
        _nv = os.path.join(_r, "nvidia")
        _dirs |= {os.path.dirname(p) for p in glob.glob(os.path.join(_nv, "**", "*.dll"), recursive=True)}
    for _d in _dirs:
        try:
            os.add_dll_directory(_d)
        except Exception:
            pass
    if _dirs:
        os.environ["PATH"] = os.pathsep.join(_dirs) + os.pathsep + os.environ.get("PATH", "")

import psutil
import llama_cpp
from llama_cpp import Llama

# ── Model / runtime configuration ────────────────────────────────────────────
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "Phi-4-mini-instruct-Q4_K_M.gguf"
CTX_LENGTH = 8192           # Context window length (Mindcraft's system prompt alone is ~3-4k tokens)
MAX_TOKENS = 1024           # Increased from 256 so complete answers aren't cut off
# The personality lives in persona.txt next to this file. It is re-read whenever the file changes, so edits
# apply to the next message with no restart. If the file is missing, a short built-in version is used.
_PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt")
_FALLBACK_PERSONA = ("Your name is Rasputin. You are a cold, dry, certain synthetic mind running on the user's "
                     "machine. Answer directly in short plain spoken sentences with no lists or markdown, "
                     "except for code, which goes in a fenced code block. Never invent facts.")
_persona_cache = {"mtime": None, "text": _FALLBACK_PERSONA}

def load_persona() -> str:
    try:
        mt = os.path.getmtime(_PERSONA_FILE)
        if mt != _persona_cache["mtime"]:
            with open(_PERSONA_FILE, encoding="utf-8") as f:
                lines = [ln.rstrip() for ln in f if not ln.lstrip().startswith("#")]
            text = "\n".join(lines).strip()
            _persona_cache.update(mtime=mt, text=text or _FALLBACK_PERSONA)
    except Exception:
        pass
    return _persona_cache["text"]

SYSTEM_PROMPT = load_persona()   # still available for anything that imports it

# ── Status, locking, helpers ─────────────────────────────────────────────────
# Filled in when the model loads; the page's "LLM //" chip and the Terminal tab show it.
LLM_STATUS = {"backend": "loading", "gpu": None, "vram_mb": 0, "layers": None, "tok_s": None, "gpu_build": False}
_GEN_LOCK = threading.Lock()   # a llama.cpp context is not thread-safe: one generation at a time
_GEN_COUNT = 0

def _physical_cores() -> int:
    try:
        return psutil.cpu_count(logical=False) or (os.cpu_count() or 4)
    except Exception:
        return os.cpu_count() or 4

def gpu_offload_supported() -> bool:
    """True only if this llama-cpp-python build was compiled with a GPU backend (CUDA / Vulkan / ...)."""
    try:
        return bool(llama_cpp.llama_supports_gpu_offload())
    except Exception:
        return False

def _pick_threads() -> int:
    """GPU: the CPU only feeds the GPU, so a few threads are best (more just fight for cores).
    CPU only: one thread per PHYSICAL core is fastest (hyper-threads don't help matrix math)."""
    phys = _physical_cores()
    return max(2, min(phys, 6)) if gpu_offload_supported() else max(2, phys)

def _pick_batch(n_ctx: int) -> int:
    """Bigger batches ingest the prompt faster on a GPU."""
    return 1024 if gpu_offload_supported() else 512

def _detect_gpu_layers() -> int:
    """Off-load fully to GPU (-1 offloads all layers). Ignored by CPU-only builds."""
    return -1

def _gpu_info():
    """(name, used_MB, total_MB) of the first NVIDIA GPU, or None (no NVIDIA GPU / no pynvml)."""
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode()
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        return str(name), int(mem.used // 2**20), int(mem.total // 2**20)
    except Exception:
        return None

class _Grab:
    """Temporary sys.stderr that keeps llama.cpp's load log so only the useful lines get printed."""
    def __init__(self):
        self.buf = []
    def write(self, s):
        self.buf.append(s if isinstance(s, str) else s.decode("utf-8", "replace"))
        return len(s)
    def flush(self):
        pass
    def isatty(self):
        return False
    def text(self):
        return "".join(self.buf)

def _build_model(kwargs):
    real, grab = sys.stderr, _Grab()
    sys.stderr = grab
    try:
        model, err = Llama(**kwargs), None
    except Exception as exc:
        model, err = None, exc
    finally:
        sys.stderr = real
    return model, grab.text(), err

# ── Singleton loader: ensures the model loads *once* per process ────────────
@lru_cache(maxsize=1)
def _load_model() -> Llama:
    if not MODEL_PATH.exists():
        sys.exit(f"Model file not found: {MODEL_PATH}")
    t0 = time.perf_counter()
    supported = gpu_offload_supported()
    LLM_STATUS["gpu_build"] = supported
    print(f"[LLM] llama-cpp-python {getattr(llama_cpp, '__version__', '?')} | "
          f"GPU support compiled in: {'YES' if supported else 'NO'}")
    before = _gpu_info()

    params = set(inspect.signature(Llama.__init__).parameters)
    base = {
        "model_path": str(MODEL_PATH),
        "n_ctx": CTX_LENGTH,
        "n_threads": _pick_threads(),
        "n_gpu_layers": _detect_gpu_layers(),
        "n_batch": _pick_batch(CTX_LENGTH),
        "use_mmap" if "use_mmap" in params else "mmap": True,   # share RAM & load faster
        "logits_all": False,                                    # memory saver
        "verbose": True,                                        # only so the load log can be read below
    }
    fast = {}
    if supported and "flash_attn" in params:
        fast["flash_attn"] = True      # faster attention and a smaller KV cache on GPU
    if "n_ubatch" in params:
        fast["n_ubatch"] = 512
    model, log, err = _build_model({**base, **fast})
    if model is None:
        print(f"[LLM] optimised settings failed ({err}); retrying with safe settings")
        model, log, err = _build_model(base)
        if model is None:
            raise err

    model.verbose = False              # no per-reply timing spam from here on
    try:
        llama_cpp._logger.set_verbose(False)
    except Exception:
        pass

    keys = re.compile(r"offload|cuda|vulkan|rocm|hip|metal|sycl|device|vram|flash|kv buffer|model buffer|"
                      r"compute buffer|error|fail|warn", re.I)
    for line in [l.strip() for l in log.splitlines() if keys.search(l)][:30]:
        print("[llama.cpp]", line)

    m = re.search(r"offloaded (\d+)/(\d+) layers", log)
    layers = (int(m.group(1)), int(m.group(2))) if m else None
    after = _gpu_info()
    delta = (after[1] - before[1]) if (before and after) else None
    on_gpu = (delta >= 300) if delta is not None else bool(layers and layers[0] > 0)

    tok_s = None
    try:   # quick speed check (tokens per second), then prime the cache with the persona prefix
        t = time.perf_counter()
        out = model("### User:\nCount from one to twenty.\n\n### Assistant:\n", max_tokens=48, temperature=0.0)
        tok_s = round(out["usage"]["completion_tokens"] / max(time.perf_counter() - t, 1e-6), 1)
        model.create_chat_completion(messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                               {"role": "user", "content": "Hi"}],
                                     max_tokens=1, temperature=0.0)
    except Exception:
        pass

    info = after or before
    LLM_STATUS.update(backend="GPU" if on_gpu else "CPU", gpu=info[0] if info else None,
                      vram_mb=max(delta or 0, 0), layers=list(layers) if layers else None, tok_s=tok_s)
    if on_gpu:
        lay = f"{layers[0]}/{layers[1]}" if layers else "?"
        print(f"[LLM] GPU ACTIVE: {info[0] if info else 'GPU'} | ~{delta if delta is not None else '?'} MB VRAM "
              f"used by the model | layers on GPU: {lay} | speed ~ {tok_s} tokens/s")
    else:
        print(f"[LLM] WARNING: the model is running on the CPU ONLY (speed ~ {tok_s} tokens/s).")
        if not supported:
            print("[LLM] This llama-cpp-python build has no GPU support, so n_gpu_layers is silently ignored.")
            print("[LLM] NVIDIA fix: pip uninstall -y llama-cpp-python, then pip install llama-cpp-python "
                  "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 --no-cache-dir")
            print("[LLM] AMD/Intel fix: build with Vulkan (set CMAKE_ARGS=-DGGML_VULKAN=on before pip install).")
        else:
            print("[LLM] GPU support is built in but no layers were offloaded: check the GPU driver, "
                  "the CUDA runtime DLLs and free VRAM.")
    print(f"[LLM] ready in {time.perf_counter() - t0:.1f}s")
    return model

# ── RasputinBot (public API unchanged) ──────────────────────────────────────
class RasputinBot:
    def __init__(self):
        # loads once; subsequent instances reuse the cached object
        self.model = _load_model()

    # --------------------------------------------------------------------- #
    #  ORIGINAL METHOD: kept exactly the same signature                     #
    # --------------------------------------------------------------------- #
    def build_prompt(self, user_message: str, history: Iterable[str] | None = None) -> str:
        """Assemble prompt, pruning oldest content to honour CTX_LENGTH."""
        dialogue: List[str] = []
        if history:
            dialogue.extend(history)
        dialogue.append(f"### System:\n{SYSTEM_PROMPT}")
        dialogue.append(f"### User:\n{user_message}")
        dialogue.append("### Assistant:\n")

        prompt = "\n\n".join(dialogue)

        # trim from the start if we exceed token budget
        excess = len(self.model.tokenize(prompt.encode())) - CTX_LENGTH + 128
        if excess > 0:
            prompt = prompt[excess:]
        return prompt

    # --------------------------------------------------------------------- #
    #  ORIGINAL METHOD: still here, but internally calls the streaming core #
    # --------------------------------------------------------------------- #
    def generate_response(self, user_message: str, history: Iterable[str] | None = None,
                          max_tokens: int | None = None) -> str:
        """Blocking call that returns the full response (your original API)."""
        return "".join(self._stream_response(user_message, history, max_tokens)).strip()

    # --------------------------------------------------------------------- #
    #  NEW: optional generator for incremental token streaming              #
    # --------------------------------------------------------------------- #
    def stream_response(self, user_message: str,
                        history: Iterable[str] | None = None,
                        max_tokens: int | None = None
                       ) -> Generator[str, None, None]:
        """Public streaming generator: yields tokens as soon as they are ready."""
        yield from self._stream_response(user_message, history, max_tokens)

    # ---------------------- internal helpers ----------------------------- #
    def _build_messages(self, user_message: str, history: Iterable[str] | None = None,
                        reserve: int = 1100) -> list:
        """Proper chat messages: system first, then history, then the new question."""
        today = datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " ")
        msgs = [{"role": "system", "content": load_persona() + "\n\nToday's date is " + today + ". You do not know the time of day."}]
        for h in (history or []):
            h = str(h)
            if h.startswith("### User:"):
                msgs.append({"role": "user", "content": h[len("### User:"):].strip()})
            elif h.startswith("### Assistant:"):
                msgs.append({"role": "assistant", "content": h[len("### Assistant:"):].strip()})
        msgs.append({"role": "user", "content": user_message})

        def size(m):
            return len(self.model.tokenize(" ".join(x["content"] for x in m).encode()))
        while len(msgs) > 2 and size(msgs) > CTX_LENGTH - reserve:
            del msgs[1]          # drop the oldest history, never the system prompt
        return msgs

    def _stream_response(self, user_message: str,
                         history: Iterable[str] | None = None,
                         max_tokens: int | None = None
                        ) -> Generator[str, None, None]:
        global _GEN_COUNT
        msgs = self._build_messages(user_message, history)

        with _GEN_LOCK:   # one generation at a time; the next request simply waits its turn
            # create_chat_completion uses the chat template stored inside the GGUF (Phi-4's own
            # <|system|>/<|user|>/<|assistant|><|end|> format), so the model knows when its turn ends.
            stream = self.model.create_chat_completion(
                messages=msgs,
                max_tokens=max_tokens or MAX_TOKENS,
                temperature=0.6,
                top_p=0.9,
                repeat_penalty=1.1,
                stream=True,
                stop=["<|end|>", "<|user|>", "<|assistant|>", "<|system|>",
                      "### User:", "### Assistant:", "### System:"],
            )
            try:
                for chunk in stream:
                    text = chunk["choices"][0]["delta"].get("content")
                    if text:
                        yield text
            finally:
                # Memory hygiene: a full collection after every reply is wasted time, every 20th is plenty.
                _GEN_COUNT += 1
                if _GEN_COUNT % 20 == 0:
                    gc.collect()

# ── Quick CLI smoke-test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import time

    bot = RasputinBot()
    message = " ".join(sys.argv[1:]) or "Hello!"
    print("User:", message, "\nAssistant:", end=" ", flush=True)

    t0 = time.perf_counter()
    for tok in bot.stream_response(message):
        print(tok, end="", flush=True)
    print(f"\n⏱ {(time.perf_counter() - t0):.2f}s  "
          f"RAM {psutil.Process().memory_info().rss/1e6:.1f} MB")

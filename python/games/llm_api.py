"""OpenAI-compatible endpoint (/v1/...) so Mindcraft talks to Qwen2.5-7B-Instruct, loaded by Rasputin itself.

Why: Mindcraft has no model of its own, it only sends HTTP requests to one. When the Mindcraft switch is turned ON,
mindcraft.py calls load_qwen() here, which loads Qwen2.5 into this process (llama-cpp-python, GPU) the same way
imagegen.py loads its pipeline: only when it is needed, in its own object, and freed again afterwards. When the switch
is turned OFF (or Rasputin closes) unload_qwen() frees it. The chat tab keeps using Phi-4 the whole time; the two
models are separate and never swap each other out.

VRAM: Phi-4 mini (~2.5 GB) and Qwen2.5-7B Q4_K_M (~4.7 GB + context) sit on the GPU together while Mindcraft is on.

Access: only requests from this PC itself (loopback address, loopback Host header, no proxy headers, no browser
Origin). A phone, the LAN listener, the ngrok tunnel and web pages in the browser cannot use it. See accounts._guard.
"""
import gc
import inspect
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, request

import chatbot

BOT_MAX_TOKENS = 512          # a Minecraft reply is a sentence plus a command; long generations only slow the bot down
BOT_MAX_TOKENS_CAP = 768
QWEN_NAME = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
# Normally models/ next to Phi-4 (the same folder as your path below); the full path is the fallback.
QWEN_PATH = chatbot.MODEL_PATH.parent / QWEN_NAME
QWEN_FALLBACK = Path(r"C:\Users\Haden\Favorites\Rasputin - Copy - Copy - Copy\models\Qwen2.5-7B-Instruct-Q4_K_M.gguf")
QWEN_CTX = chatbot.CTX_LENGTH  # Mindcraft's system prompt alone is ~3-4k tokens
MODEL_ID = QWEN_NAME
STOPS = ["<|im_end|>", "<|im_start|>", "<|endoftext|>"]   # Qwen's ChatML turn markers

_qwen = None
_qwen_lock = threading.Lock()       # held while loading/unloading, so the two never overlap
_QWEN_GEN_LOCK = threading.Lock()   # a llama.cpp context is not thread-safe: one Minecraft reply at a time


def _qwen_path():
    for p in (QWEN_PATH, QWEN_FALLBACK):
        if p.exists():
            return p
    raise RuntimeError("Qwen model file not found: %s" % QWEN_PATH)


def load_qwen():
    """Load Qwen2.5 onto the GPU (once). Returns the model. Safe to call repeatedly and from any thread."""
    global _qwen
    with _qwen_lock:
        if _qwen is not None:
            return _qwen
        path = _qwen_path()
        t0 = time.perf_counter()
        print("[MINDCRAFT-LLM] loading %s" % path.name)
        supported = chatbot.gpu_offload_supported()
        params = set(inspect.signature(chatbot.Llama.__init__).parameters)
        base = {
            "model_path": str(path),
            "n_ctx": QWEN_CTX,
            "n_threads": chatbot._pick_threads(),
            "n_gpu_layers": chatbot._detect_gpu_layers(),
            "n_batch": chatbot._pick_batch(QWEN_CTX),
            "use_mmap" if "use_mmap" in params else "mmap": True,
            "logits_all": False,
            "verbose": True,
        }
        fast = {}
        if supported and "flash_attn" in params:
            fast["flash_attn"] = True
        if "n_ubatch" in params:
            fast["n_ubatch"] = 512
        model, _log, err = chatbot._build_model({**base, **fast})
        if model is None:
            print("[MINDCRAFT-LLM] optimised settings failed (%s); retrying with safe settings" % err)
            model, _log, err = chatbot._build_model(base)
            if model is None:
                raise RuntimeError("Could not load Qwen2.5: %s" % err)
        model.verbose = False
        _qwen = model
        print("[MINDCRAFT-LLM] Qwen2.5 ready in %.1fs (GPU: %s)" % (
            time.perf_counter() - t0, "yes" if supported else "NO - this llama-cpp-python build is CPU only"))
        return _qwen


def unload_qwen():
    """Free Qwen2.5's RAM/VRAM. Waits for a load or reply that is still running."""
    global _qwen
    with _qwen_lock:
        if _qwen is None:
            return
        with _QWEN_GEN_LOCK:
            model, _qwen = _qwen, None
            try:
                model.close()          # releases the VRAM right away (newer llama-cpp-python)
            except Exception:
                pass
            del model
            gc.collect()
        print("[MINDCRAFT-LLM] Qwen2.5 unloaded")


def _err(msg, code=400):
    return jsonify({"error": {"message": msg, "type": "invalid_request_error"}}), code


def _text(content):
    """Message content as plain text (clients may send a list of parts; this model cannot see images)."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _clean(messages):
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role") if m.get("role") in ("system", "user", "assistant") else "user"
        txt = _text(m.get("content")).strip()
        if not txt:
            continue
        if out and out[-1]["role"] == role:      # chat templates want alternating turns
            out[-1]["content"] += "\n\n" + txt
        else:
            out.append({"role": role, "content": txt})
    return out


def _fit(model, msgs, budget):
    """Drop the oldest turns (never the system prompt or the newest message) until the prompt fits the context."""
    def count(ms):
        return sum(len(model.tokenize(m["content"].encode("utf-8", "replace"), add_bos=False)) + 8 for m in ms)

    dropped = 0
    while count(msgs) > budget and len(msgs) > 2:
        del msgs[1 if msgs[0]["role"] == "system" else 0]
        dropped += 1
    used = count(msgs)
    if used > budget and msgs[0]["role"] == "system":      # still too big: shorten the system prompt itself
        toks = model.tokenize(msgs[0]["content"].encode("utf-8", "replace"), add_bos=False)
        keep = max(256, len(toks) - (used - budget) - 16)
        msgs[0]["content"] = model.detokenize(toks[:keep]).decode("utf-8", "replace")
        used = count(msgs)
    return msgs, dropped, used


def register(app, bot):
    @app.route("/v1/models")
    def v1_models():
        return jsonify({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "rasputin"}]})

    @app.route("/v1/embeddings", methods=["POST"])
    def v1_embeddings():
        # Mindcraft falls back to word-overlap matching when this fails, which is fine for a small local model.
        return _err("Embeddings are not served here.", 501)

    @app.route("/v1/chat/completions", methods=["POST"])
    def v1_chat():
        body = request.get_json(silent=True) or {}
        msgs = _clean(body.get("messages") or [])
        if not msgs:
            return _err("messages is required.")
        model = _qwen               # loaded by the Mindcraft switch (mindcraft.py), freed when it goes off
        if model is None:
            return _err("Qwen2.5 is not loaded. Turn Mindcraft on in the Games tab.", 503)
        try:
            want = int(body.get("max_tokens") or body.get("max_completion_tokens") or BOT_MAX_TOKENS)
        except (TypeError, ValueError):
            want = BOT_MAX_TOKENS
        max_tokens = max(16, min(want, BOT_MAX_TOKENS_CAP))
        msgs, dropped, used = _fit(model, msgs, model.n_ctx() - max_tokens - 64)

        stop = body.get("stop")
        stop = [stop] if isinstance(stop, str) else [s for s in (stop or []) if isinstance(s, str)]
        try:
            temp = float(body.get("temperature", 0.7))
        except (TypeError, ValueError):
            temp = 0.7

        t0 = time.perf_counter()
        with _QWEN_GEN_LOCK:          # Qwen has its own context, so the chat tab (Phi-4) is never blocked by the bot
            if _qwen is not model:    # the switch was turned off while this request waited
                return _err("Qwen2.5 was unloaded.", 503)
            out = model.create_chat_completion(messages=msgs, max_tokens=max_tokens, temperature=temp, top_p=0.9,
                                               repeat_penalty=1.1, stream=False, stop=stop + STOPS)
        dt = max(time.perf_counter() - t0, 1e-6)
        u = out.get("usage", {})
        print("[MINDCRAFT-LLM] prompt %s tok%s -> %s tok in %.1fs (%.1f tok/s, %s)" % (
            u.get("prompt_tokens", used), " (trimmed %d old turns)" % dropped if dropped else "",
            u.get("completion_tokens", "?"), dt, (u.get("completion_tokens") or 0) / dt,
            "GPU" if chatbot.gpu_offload_supported() else "CPU"))
        out["id"] = out.get("id") or "chatcmpl-" + uuid.uuid4().hex[:12]
        out["model"] = MODEL_ID
        return jsonify(out)

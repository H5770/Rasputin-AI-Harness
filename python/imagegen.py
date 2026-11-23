"""imagegen.py - local image generation (Stable Diffusion 1.5) for the chat.

How it fits the app:
  parse_image_request(text)  -> the image prompt if the message asks for a picture, else None
  start(image_id, prompt)    -> queues a background job (one at a time, so VRAM never runs out)
  register_images(app, db, GenImage) adds:
        GET /image/<id>          the finished PNG, straight from SQLite
        GET /image/<id>/status   {status, step, total, preview, error}  (the page polls this)
        GET /image/<id>/preview  a tiny live preview decoded from the latents (shown blurred)

torch / diffusers are imported lazily, so the app starts exactly as before even if they are missing.
Nothing is written to disk by this module: the PNG goes from memory into the database.
"""
from __future__ import annotations

import gc
import hashlib
import io
import random
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from flask import Response, jsonify, request
from sqlalchemy import text

# ── Settings ─────────────────────────────────────────────────────────────────
MODEL_ID = "Lykon/dreamshaper-8"   # SD 1.5 fine-tune, ~2 GB download on first use
STEPS = 25
GUIDANCE = 7.0
SIZE = 512                       # SD 1.5 is trained at 512x512; larger sizes repeat subjects
NEGATIVE = "blurry, low quality, deformed, bad anatomy, extra limbs, watermark, text, signature"
STYLE_SUFFIX = ", highly detailed, sharp focus"
IDLE_UNLOAD_SECONDS = 300        # free the pipeline's RAM after 5 idle minutes
EXPAND_PROMPTS = True            # let the chat LLM rewrite short requests into detailed prompts first
EXPAND_BELOW_WORDS = 25          # requests with this many words or more are used exactly as typed

# ── Which chat messages count as an image request ────────────────────────────
# "/image a fox in the snow"            "draw a fox in the snow"
# "generate an image of a fox"          "make me a realistic photo of a fox"
# "i want a picture of a fox"           "can you give me a painting of a fox"
# "show me a drawing of a fox"          "create some wallpaper of a fox"
_NOUN = (r"(?:images?|pictures?|pics?|photos?|photographs?|illustrations?|drawings?|paintings?|artworks?|art|renders?|"
         r"sketch(?:es)?|portraits?|wallpapers?|posters?|cartoons?|anime|concept\s+art|digital\s+art|pixel\s+art)")
_OF = r"(?:of|showing|depicting|featuring|containing|about|with|that\s+shows|where|:)"
_STYLE = r"(?:[\w-]+\s+){0,3}?"                      # up to three style words: "a realistic oil painting of"
_POLITE = r"(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
_CMD = re.compile(r"^\s*/(?:image|imagine|img)\s+(?P<p>.+?)\s*$", re.I | re.S)
_ASK = re.compile(
    r"^\s*" + _POLITE +
    r"(?:generate|create|make|produce|render|show|draw|paint|sketch|design|whip\s+up|come\s+up\s+with)\s+(?:me\s+)?(?:an?\s+|some\s+|the\s+)?"
    + _STYLE + _NOUN + r"\s*" + _OF + r"\s*(?P<p>.+?)\s*[.?!]*\s*$", re.I | re.S)
_WANT = re.compile(
    r"^\s*" + _POLITE +
    r"(?:i\s+(?:want|would\s+like|need|'d\s+like)|i'd\s+like|give\s+me|get\s+me|let\s+me\s+(?:see|have)|can\s+i\s+(?:see|get|have)|"
    r"i\s+wanna\s+see|i\s+want\s+to\s+see)\s+(?:an?\s+|some\s+)?"
    + _STYLE + _NOUN + r"\s*" + _OF + r"\s*(?P<p>.+?)\s*[.?!]*\s*$", re.I | re.S)
_DRAW = re.compile(r"^\s*" + _POLITE + r"(?:draw|paint|sketch|illustrate|doodle)\s+(?:me\s+)?(?P<p>.+?)\s*[.?!]*\s*$", re.I | re.S)
# "try again", "another one", "redo it": only meaningful straight after a picture (the caller checks that)
_REDO = re.compile(
    r"^\s*" + _POLITE + r"(?:try\s+(?:it\s+|that\s+)?again|do\s+(?:it|that)\s+again|redo(?:\s+(?:it|that|this|the\s+(?:image|picture|photo)))?|"
    r"regenerate(?:\s+(?:it|that|this|the\s+(?:image|picture|photo)))?|(?:make|draw|generate)\s+(?:it|that|another(?:\s+one)?|a\s+new\s+one)(?:\s+again)?|"
    r"another\s+one|one\s+more(?:\s+time)?|again|new\s+one)\s*(?:please\s*)?[.!?]*\s*$", re.I)


def parse_image_request(message: str):
    for rx in (_CMD, _ASK, _WANT, _DRAW):
        m = rx.match(message or "")
        if m:
            return m.group("p").strip()[:300]
    return None


def is_redo(message: str) -> bool:
    """True for "try again" style messages. Only act on it when the previous reply was a picture."""
    return bool(_REDO.match(message or ""))


# ── Background worker ────────────────────────────────────────────────────────
_app = None
_db = None
_Img = None
_pool = ThreadPoolExecutor(max_workers=1)   # one image at a time
_pipe = None
_pipe_lock = threading.Lock()               # held while generating, so the idle-unload waits
_timer = None
JOBS = {}                                   # image_id -> {"step", "total", "preview"}

# Approximate latent->RGB map for SD 1.x: turns the half-finished latents into a cheap colour preview.
_LATENT_RGB = [[0.298, 0.207, 0.208], [0.187, 0.286, 0.173], [-0.158, 0.189, 0.264], [-0.184, -0.271, -0.473]]


_bot = None
EXPAND_SYSTEM = (
    "You write prompts for the Stable Diffusion 1.5 image model. Rewrite the user's idea as ONE "
    "comma-separated prompt of at most 45 words. Keep every detail the user asked for exactly: "
    "subjects, counts, colours, objects, setting. Put the main subject first. Then add concrete "
    "visual detail: pose, expression, lighting, camera angle, art style or photo style. "
    "Never add new main subjects. Output only the prompt: no quotes, no labels, no explanation."
)


def _expand(prompt):
    """Ask the already-loaded chat model for a richer SD prompt. Any problem -> use the original text."""
    if not (EXPAND_PROMPTS and _bot) or len(prompt.split()) >= EXPAND_BELOW_WORDS:
        return prompt, False
    try:
        import chatbot
        with chatbot._GEN_LOCK:      # same lock the chat uses, so the two never run on the model at once
            out = _bot.model.create_chat_completion(
                messages=[{"role": "system", "content": EXPAND_SYSTEM}, {"role": "user", "content": prompt}],
                max_tokens=100, temperature=0.4, top_p=0.9)
        text = out["choices"][0]["message"]["content"].strip().split("\n")[0]
        text = re.sub(r"^(?:prompt|rewritten prompt)\s*:\s*", "", text, flags=re.I).strip(" \"'")
        return (text[:400], True) if len(text) > 8 else (prompt, False)
    except Exception as exc:
        print("[IMG] prompt rewrite skipped:", exc)
        return prompt, False


def _get_pipe():
    global _pipe
    if _pipe is None:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch cannot see the GPU (CPU-only build installed). "
                               "Reinstall torch with CUDA support.")
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline
        print(f"[IMG] loading {MODEL_ID} (first run downloads ~2 GB)")
        kw = dict(torch_dtype=torch.float16, safety_checker=None, requires_safety_checker=False)
        try:
            p = StableDiffusionPipeline.from_pretrained(MODEL_ID, variant="fp16", use_safetensors=True, **kw)
        except Exception:
            p = StableDiffusionPipeline.from_pretrained(MODEL_ID, **kw)
        try:   # some fine-tunes (DreamShaper) ship a DEIS config, so name the algorithm explicitly
            p.scheduler = DPMSolverMultistepScheduler.from_config(
                p.scheduler.config, algorithm_type="dpmsolver++", use_karras_sigmas=True)
        except Exception as exc:
            print("[IMG] keeping the model's own scheduler:", exc)
        p.enable_model_cpu_offload()   # only the part that is working sits in VRAM (the LLM keeps the rest)
        try:
            p.vae.enable_slicing()     # newer diffusers moved this onto the VAE
        except Exception:
            pass
        p.set_progress_bar_config(disable=True)
        _pipe = p
        print("[IMG] pipeline ready")
    return _pipe


def _preview_jpeg(latents):
    import torch
    from PIL import Image
    f = torch.tensor(_LATENT_RGB, dtype=latents.dtype, device=latents.device)
    rgb = torch.einsum("chw,cr->hwr", latents[0], f)
    arr = ((rgb / 2 + 0.5).clamp(0, 1) * 255).byte().cpu().numpy()
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _free_vram():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _unload():
    global _pipe
    with _pipe_lock:
        if _pipe is not None:
            _pipe = None
            gc.collect()
            _free_vram()
            print("[IMG] idle, pipeline unloaded")


def _run(image_id, prompt):
    global _timer
    if _timer:
        _timer.cancel()
    png, err = None, None
    try:
        with _pipe_lock:
            import torch
            pipe = _get_pipe()
            gen = torch.Generator("cpu").manual_seed(random.randrange(2 ** 32))
            job = JOBS[image_id]

            def on_step(_pipe, step, _t, kw):
                job["step"] = step + 1
                if step % 2 == 1 and step < STEPS - 1:
                    try:
                        job["preview"] = _preview_jpeg(kw["latents"])
                    except Exception:
                        pass
                return kw

            final, rewritten = _expand(prompt)
            # The prompt is deliberately NOT printed: the Terminal tab is readable by every signed-in account.
            print(f"[IMG] generating #{image_id}" + (" (prompt rewritten)" if rewritten else ""))
            img = pipe(prompt=final if rewritten else prompt + STYLE_SUFFIX, negative_prompt=NEGATIVE, num_inference_steps=STEPS,
                       guidance_scale=GUIDANCE, width=SIZE, height=SIZE, generator=gen,
                       callback_on_step_end=on_step, callback_on_step_end_tensor_inputs=["latents"]).images[0]
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png = buf.getvalue()
    except Exception as exc:
        traceback.print_exc()
        err = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        _free_vram()
        _timer = threading.Timer(IDLE_UNLOAD_SECONDS, _unload)
        _timer.daemon = True
        _timer.start()

    try:
        with _app.app_context():
            row = _db.session.get(_Img, image_id)
            if row is not None:          # the chat may have been deleted while this was generating
                row.data, row.status, row.error = png, ("done" if png else "error"), err
                _db.session.commit()
            _db.session.remove()
    finally:
        JOBS.pop(image_id, None)
    print(f"[IMG] #{image_id} {'done' if png else 'failed'}")


def start(image_id, prompt):
    JOBS[image_id] = {"step": 0, "total": STEPS, "preview": None}
    _pool.submit(_run, image_id, prompt)


def _run_bytes(prompt, extra_negative=""):
    """Same pipeline as _run, but returns (png_bytes, error) and touches no database. Used by the Discord bot."""
    global _timer
    if _timer:
        _timer.cancel()
    png, err = None, None
    try:
        with _pipe_lock:
            import torch
            pipe = _get_pipe()
            gen = torch.Generator("cpu").manual_seed(random.randrange(2 ** 32))
            final, rewritten = _expand(prompt)
            print("[IMG] generating a picture for Discord" + (" (prompt rewritten)" if rewritten else ""))
            img = pipe(prompt=final if rewritten else prompt + STYLE_SUFFIX, negative_prompt=NEGATIVE + extra_negative,
                       num_inference_steps=STEPS, guidance_scale=GUIDANCE, width=SIZE, height=SIZE, generator=gen).images[0]
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png = buf.getvalue()
    except Exception as exc:
        traceback.print_exc()
        err = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        _free_vram()
        _timer = threading.Timer(IDLE_UNLOAD_SECONDS, _unload)
        _timer.daemon = True
        _timer.start()
    print("[IMG] Discord picture " + ("done" if png else "failed"))
    return png, err


def generate_png(prompt, extra_negative=""):
    """Blocking. Waits its turn in the same one-at-a-time queue as the chat's pictures. Returns (png, error)."""
    return _pool.submit(_run_bytes, prompt, extra_negative).result()


def vacuum(db):
    """SQLite keeps deleted rows as free space until VACUUM, so this really shrinks the file."""
    try:
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("VACUUM"))
    except Exception as exc:
        print("[IMG] vacuum warning:", exc)


# ── Routes ───────────────────────────────────────────────────────────────────
def register_images(app, db, GenImage, bot=None):
    global _app, _db, _Img, _bot
    _app, _db, _Img, _bot = app, db, GenImage, bot

    with app.app_context():
        db.create_all()    # creates only the missing gen_image table; desktop_edge.py never calls this
        # A job that was running when the app last closed can never finish: mark it failed.
        stale = GenImage.query.filter_by(status="pending").all()
        for row in stale:
            row.status, row.error = "error", "Interrupted when the app closed."
        if stale:
            db.session.commit()

    def _row(image_id, with_data=False):
        q = GenImage.query
        if not with_data:
            q = q.options(db.defer(GenImage.data))   # don't pull the PNG just to read a status
        return q.filter_by(id=image_id).first_or_404()

    @app.route("/image/<int:image_id>")
    def image_png(image_id):
        row = _row(image_id, with_data=True)
        if row.status != "done" or not row.data:
            return jsonify({"error": "not ready"}), 404
        # IDs can be reused after a chat is deleted, so never let the browser trust its cache blindly:
        # it must ask every time, and the ETag (a fingerprint of the PNG) makes "unchanged" a cheap 304.
        resp = Response(row.data, mimetype="image/png")
        resp.set_etag(hashlib.sha1(row.data).hexdigest())
        resp.headers["Cache-Control"] = "private, no-cache"
        return resp.make_conditional(request)

    @app.route("/image/<int:image_id>/status")
    def image_status(image_id):
        row = _row(image_id)
        job = JOBS.get(image_id)
        if row.status == "pending" and job is None:     # no worker owns it any more
            row.status, row.error = "error", "Interrupted."
            db.session.commit()
        return jsonify({"status": row.status, "error": row.error,
                        "step": job["step"] if job else STEPS, "total": STEPS,
                        "preview": bool(job and job["preview"])})

    @app.route("/image/<int:image_id>/preview")
    def image_preview(image_id):
        job = JOBS.get(image_id)
        if not job or not job["preview"]:
            return ("", 204)
        return Response(job["preview"], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

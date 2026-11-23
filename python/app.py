import os
from datetime import datetime

# This file lives in the Python folder; templates, static, instance, models... are one level up.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, render_template, request, jsonify, g, abort
from sqlalchemy import text
import re
from flask_sqlalchemy import SQLAlchemy
from chatbot import RasputinBot
import psutil
import threading
import numpy as np

# Local speech-to-text for the AI Core voice link (pip install faster-whisper)
try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

_stt_model = None
_stt_lock = threading.Lock()

def get_stt():
    """Load the Whisper model once, on first use. Runs on CPU so the GPU stays free for the LLM."""
    global _stt_model
    with _stt_lock:
        if _stt_model is None:
            _stt_model = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _stt_model

# Local text-to-speech for the AI Core voice link (pip install piper-tts)
try:
    from piper import PiperVoice
except Exception:
    PiperVoice = None
try:
    from piper import SynthesisConfig
except Exception:
    SynthesisConfig = None

import io
import wave
from flask import Response

PIPER_DIR = os.path.join(ROOT_DIR, "models", "piper")
PIPER_DEFAULT = "Rasputin"   # used when the browser doesn't ask for a specific voice
_piper_voices = {}
_piper_lock = threading.Lock()

def list_piper_voices():
    """Voice names = every .onnx file in models/piper that has its .onnx.json next to it."""
    if not os.path.isdir(PIPER_DIR):
        return []
    names = []
    for f in sorted(os.listdir(PIPER_DIR)):
        if f.endswith(".onnx") and os.path.exists(os.path.join(PIPER_DIR, f + ".json")):
            names.append(f[:-5])
    return names

def get_piper(name):
    """Load a Piper voice once and keep it in memory."""
    with _piper_lock:
        if name not in _piper_voices:
            path = os.path.join(PIPER_DIR, name + ".onnx")
            _piper_voices[name] = PiperVoice.load(path)
        return _piper_voices[name]

psutil.cpu_percent(interval=None)   # prime the non-blocking CPU reader

# Optional GPU monitoring
try:
    import pynvml
    pynvml.nvmlInit()
    gpu_available = True
except Exception:
    gpu_available = False

# --- Flask + DB setup ---
app = Flask(__name__, template_folder=os.path.join(ROOT_DIR, "templates"),
            static_folder=os.path.join(ROOT_DIR, "static"))

# Fix: Set absolute path for SQLite DB inside app.instance_path
BASE_DIR = ROOT_DIR
DB_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(DB_DIR, exist_ok=True)  # Ensure instance directory exists

DB_PATH = os.path.join(DB_DIR, "rasputin.db")
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Several people can chat at once: wait up to 30 s for a busy database instead of failing after the 5 s default.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"connect_args": {"timeout": 30}, "pool_pre_ping": True}

db = SQLAlchemy(app)

# Accounts, sign-in, the laptop-only Admin tab and per-account saved settings (see accounts.py).
import accounts
accounts.init(app, DB_PATH)

# --- Models ---
class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, index=True)   # accounts.account.id of the person who owns this chat
    title = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship("Message", backref="chat", cascade="all,delete")
    images = db.relationship("GenImage", backref="chat", cascade="all,delete")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(10))  # 'user' or 'assistant'
    content = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"))

class GenImage(db.Model):
    """A generated image. The PNG itself lives in this row, so it survives restarts and dies with its chat."""
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"))
    prompt = db.Column(db.Text)
    status = db.Column(db.String(10), default="pending")   # pending / done / error
    error = db.Column(db.Text)
    data = db.Column(db.LargeBinary)                       # full-size PNG
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# --- Per-account chat ownership --------------------------------------------
# Every chat belongs to exactly one account. Another account asking for it gets a plain 404, the same
# answer as for a chat that does not exist, so nobody can even tell that someone else's chat exists.
def _adopt_orphans(owner_id=None):
    """Chats made before accounts were tied to chats have no owner. Give them to the Owner account."""
    if owner_id is None:
        owner_id = db.session.execute(
            text("SELECT id FROM account WHERE level >= 2 ORDER BY id LIMIT 1")).scalar()
        if owner_id is None:
            return
    db.session.execute(text("UPDATE chat SET owner_id = :o WHERE owner_id IS NULL"), {"o": owner_id})
    db.session.commit()

def _migrate_chat_owner():
    """Adds chat.owner_id to an existing database (db.create_all() never alters existing tables)."""
    with app.app_context():
        db.create_all()
        cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(chat)"))]
        if "owner_id" not in cols:
            db.session.execute(text("ALTER TABLE chat ADD COLUMN owner_id INTEGER"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_owner_id ON chat (owner_id)"))
            db.session.commit()
            print("[ACCOUNTS] chats are now bound to accounts (added chat.owner_id)")
        _adopt_orphans()

_migrate_chat_owner()

def _my_chat_or_404(chat_id):
    """The chat with this id, but only if the signed-in account owns it."""
    chat = Chat.query.filter_by(id=chat_id, owner_id=g.account["id"]).first()
    if chat is None:
        abort(404)
    return chat

_IMAGE_PATH = re.compile(r"^/image/(\d+)(?:/|$)")

@app.before_request
def _image_owner_guard():
    """Generated images belong to a chat. /image/<id>, /image/<id>/status and /image/<id>/preview
    (served by imagegen.py) are only handed to the account that owns that chat."""
    m = _IMAGE_PATH.match(request.path)
    if not m or not getattr(g, "account", None):
        return None
    row = db.session.query(GenImage.chat_id).filter_by(id=int(m.group(1))).first()   # id only: not the PNG
    if row is None:
        return None                                       # no such image: imagegen answers with its own 404
    owner = db.session.query(Chat.owner_id).filter_by(id=row[0]).scalar()
    if owner != g.account["id"]:
        abort(404)
    return None

# --- Init chatbot ---
bot = RasputinBot()

# Terminal tab + safe shutdown (see termlog.py)
import termlog
termlog.register(app)

def _close_resources():
    """Runs once on shutdown: close the database and free the model."""
    try:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    except Exception as exc:
        print("DB close warning:", exc)
    try:
        from chess_game import _reset_engine   # quit the Stockfish process
        _reset_engine()
    except Exception:
        pass
    if not termlog.busy():
        try:
            bot.model.close()
        except Exception:
            pass

termlog.add_cleanup(_close_resources)

# Chess tab (needs: pip install chess). The rest of the app still runs without it.
try:
    from chess_game import register_chess
    register_chess(app, bot)
except ImportError as exc:
    print("Chess disabled. Run: pip install chess  (" + str(exc) + ")")

# Image generation (needs: pip install torch diffusers transformers accelerate pillow). See imagegen.py.
import imagegen
imagegen.register_images(app, db, GenImage, bot)

# Impossible / endless / pointless requests are answered in one line without troubling the model. See sanity.py.
import sanity

_IMG_MARK = re.compile(r"^\[\[image:(\d+)\]\]$")

def _history_text(m):
    """Chat history as the language model should see it. A picture is stored as a [[image:N]] marker; the model
    gets a plain sentence saying the separate image model drew it, so it knows what happened and what was asked."""
    im = _IMG_MARK.match(m.content or "")
    if not im:
        return m.content
    gi = db.session.get(GenImage, int(im.group(1)))
    if gi is None:
        return "The image model drew a picture here, but it has since been deleted."
    if gi.status == "error":
        return f'I sent this request to the image model, and it failed: "{gi.prompt}".'
    return f'The image model drew a picture for this request: "{gi.prompt}". I did not draw it and I cannot see it.'

def _last_image_prompt(prior):
    """The prompt of the picture that was the very last reply, or None."""
    last = prior[-1] if prior else None
    im = _IMG_MARK.match(last.content or "") if last is not None and last.role == "assistant" else None
    gi = db.session.get(GenImage, int(im.group(1))) if im else None
    return gi.prompt if gi else None

# Home tab + AI light control: Hue and WiZ bulbs over the local network only. See lights.py.
# Games tab (Mindcraft etc., see games/). Staff only; processes are stopped on shutdown.
try:
    import games
    games.register(app, accounts._staff_only)
    termlog.add_cleanup(games.shutdown)
except Exception as exc:
    print("Games tab disabled: " + str(exc))

try:
    from games import llm_api
    llm_api.register(app, bot)   # /v1/chat/completions: lets the Mindcraft bot use this same (GPU) model
except Exception as exc:
    print("Mindcraft model endpoint disabled: " + str(exc))

import lights
lights.register_lights(app)

# Security tab (face recognition camera). Staff only. Its tables live in this same rasputin.db.
# The file is named "security(CS).py", which a normal import cannot load, so it is loaded by path.
try:
    import sys, importlib.util
    _cs_spec = importlib.util.spec_from_file_location(
        "security_cs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "security(CS).py"))
    security_cs = importlib.util.module_from_spec(_cs_spec)
    sys.modules["security_cs"] = security_cs
    _cs_spec.loader.exec_module(security_cs)
    security_cs.register(app, DB_PATH, accounts._staff_only)
    termlog.add_cleanup(security_cs.shutdown)       # releases the camera on exit
except Exception as exc:
    print("Security tab disabled: " + str(exc))

# --- Routes ---
@app.route("/")
def index():
    if g.account and g.account["level"] >= 2:
        _adopt_orphans(g.account["id"])
    ctx = accounts.page_context()
    ctx["show_security"] = bool(g.account and g.account["level"] >= 1)   # Security tab: Admins and Owner, PC and phone
    return render_template("index.html", **ctx)

@app.route("/system-data")
def system_data():
    cpu_percent = psutil.cpu_percent(interval=None)   # was a 0.5 s blocking wait on every poll
    memory = psutil.virtual_memory()
    mem_percent = memory.percent

    gpu_percent = 0
    if gpu_available:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        gpu_percent = gpu_util.gpu

    return jsonify({
        "cpu": cpu_percent,
        "memory": mem_percent,
        "gpu": gpu_percent
    })

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json
    user_input = data.get("message", "").strip()
    chat_id = data.get("chat_id")

    if not user_input:
        return jsonify({"error": "Empty message"}), 400

    if not chat_id:
        chat = Chat(owner_id=g.account["id"])
        db.session.add(chat)
        db.session.flush()
    else:
        chat = _my_chat_or_404(chat_id)

    msg_u = Message(role="user", content=user_input, chat=chat)
    db.session.add(msg_u)
    db.session.commit()

    bot_response = lights.handle_text(user_input) or sanity.check(user_input) or bot.generate_response(user_input)

    msg_b = Message(role="assistant", content=bot_response, chat=chat)
    db.session.add(msg_b)

    if not chat.title:
        short_prompt = f"Give a very short title (2-4 words max) for this conversation:\n\nUser: {user_input}\nAssistant: {bot_response}"
        generated_title = bot.generate_response(short_prompt, max_tokens=24).split("\n")[0].strip()
        chat.title = generated_title[:50]
        db.session.commit()

    db.session.commit()

    return jsonify({
        "response": bot_response,
        "chat_id": chat.id
    })

import json
from flask import stream_with_context

def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"

@app.route("/llm-status")
def llm_status():
    """Which backend the model is on (GPU or CPU), VRAM used and measured speed."""
    import chatbot
    return jsonify(chatbot.LLM_STATUS)

@app.route("/ask-stream", methods=["POST"])
def ask_stream():
    """Same as /ask, but sends the reply token by token so the page shows it as it is written.
    The chat title is made AFTER the reply is on screen instead of blocking it."""
    data = request.json or {}
    user_input = (data.get("message") or "").strip()
    chat_id = data.get("chat_id")
    if not user_input:
        return jsonify({"error": "Empty message"}), 400

    if not chat_id:
        chat = Chat(owner_id=g.account["id"])
        db.session.add(chat)
        db.session.flush()
    else:
        chat = _my_chat_or_404(chat_id)
    db.session.add(Message(role="user", content=user_input, chat=chat))
    db.session.commit()
    cid, needs_title = chat.id, not chat.title
    # Recent turns from this chat (excluding the message just saved) so the model has context
    prior = Message.query.filter_by(chat_id=cid).order_by(Message.id).all()[:-1][-8:]
    history = [("### User:\n" if m.role == "user" else "### Assistant:\n") + _history_text(m) for m in prior]

    # "turn off the kitchen light", "make the bedroom blue": the lighting module answers, not the LLM
    light_reply = lights.handle_text(user_input)
    if light_reply:
        db.session.add(Message(role="assistant", content=light_reply, chat_id=cid))
        light_title = "Light control" if needs_title else None
        if light_title:
            Chat.query.get(cid).title = light_title
        db.session.commit()

        def light_events():
            yield _sse({"chat_id": cid})
            yield _sse({"t": light_reply})
            yield _sse({"done": True, "response": light_reply})
            if light_title:
                yield _sse({"title": light_title})
        return Response(light_events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Impossible or endless requests ("count to a billion", "every digit of pi"): answered at once, in voice
    sane_reply = sanity.check(user_input)
    if sane_reply:
        db.session.add(Message(role="assistant", content=sane_reply, chat_id=cid))
        sane_title = "Declined request" if needs_title else None
        if sane_title:
            Chat.query.get(cid).title = sane_title
        db.session.commit()

        def sane_events():
            yield _sse({"chat_id": cid})
            yield _sse({"t": sane_reply})
            yield _sse({"done": True, "response": sane_reply})
            if sane_title:
                yield _sse({"title": sane_title})
        return Response(sane_events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # "draw a fox", "/image a fox", "generate an image of a fox": hand it to the image worker instead of the LLM.
    # "try again" straight after a picture repeats that picture's request.
    img_prompt = imagegen.parse_image_request(user_input)
    if not img_prompt and imagegen.is_redo(user_input):
        img_prompt = _last_image_prompt(prior)
    if img_prompt:
        gi = GenImage(chat_id=cid, prompt=img_prompt)
        db.session.add(gi)
        db.session.flush()
        iid = gi.id
        marker = f"[[image:{iid}]]"
        db.session.add(Message(role="assistant", content=marker, chat_id=cid))
        img_title = img_prompt[:40] if needs_title else None
        if img_title:
            Chat.query.get(cid).title = img_title
        db.session.commit()
        imagegen.start(iid, img_prompt)

        def image_events():
            yield _sse({"chat_id": cid})
            yield _sse({"image": iid})
            yield _sse({"done": True, "response": marker})
            if img_title:
                yield _sse({"title": img_title})
        return Response(image_events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @stream_with_context
    def events():
        parts = []
        yield _sse({"chat_id": cid})
        try:
            for tok in bot.stream_response(user_input, history=history):
                parts.append(tok)
                yield _sse({"t": tok})
        except Exception as exc:
            yield _sse({"error": str(exc)})
        finally:
            full = "".join(parts).strip()
            if full:   # also saves a partial reply if the window was closed mid-answer
                db.session.add(Message(role="assistant", content=full, chat_id=cid))
                db.session.commit()
        yield _sse({"done": True, "response": full})
        if needs_title and full:
            try:
                short_prompt = f"Give a very short title (2-4 words max) for this conversation:\n\nUser: {user_input}\nAssistant: {full}"
                title = bot.generate_response(short_prompt, max_tokens=24).split("\n")[0].strip()[:50]
                c = Chat.query.get(cid)
                if c and title:
                    c.title = title
                    db.session.commit()
                yield _sse({"title": title})
            except Exception:
                pass

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# Spoken replies need to be short and free of markdown.
VOICE_STYLE = (
    "(This is a spoken voice conversation. Reply in one to three short, natural "
    "sentences. No lists, no markdown, no emojis.)\n\n"
)

@app.route("/ask-voice", methods=["POST"])
def ask_voice():
    """
    Voice conversation endpoint for the AI Core tab.
    NOTHING here touches the database: no Chat, no Message, no commit.
    Short-term context is sent by the browser and lives only in the page's memory.
    """
    data = request.json or {}
    user_input = data.get("message", "").strip()
    if not user_input:
        return jsonify({"error": "Empty message"}), 400

    # Spoken lighting commands never reach the LLM: "turn off the bedroom lights", "make the desk lamp warm white"
    light_reply = lights.handle_text(user_input)
    if light_reply:
        return jsonify({"response": light_reply, "lights": True})

    sane_reply = sanity.check(user_input)
    if sane_reply:
        return jsonify({"response": sane_reply})

    history_lines = []
    for turn in (data.get("history") or [])[-6:]:
        content = str(turn.get("content", "")).strip()[:600]
        if not content:
            continue
        header = "### User:" if turn.get("role") == "user" else "### Assistant:"
        history_lines.append(f"{header}\n{content}")

    reply = bot.generate_response(VOICE_STYLE + user_input, history=history_lines, max_tokens=200)
    return jsonify({"response": reply})

@app.route("/transcribe", methods=["POST"])
def transcribe():
    """
    Local speech-to-text for the voice link.
    Receives raw 16 kHz mono int16 PCM. Audio is processed in memory only:
    never written to disk, never stored in the database.
    """
    if WhisperModel is None:
        return jsonify({"error": "faster-whisper not installed"}), 501

    raw = request.get_data()
    if len(raw) < 3200:  # under ~0.1s of audio
        return jsonify({"text": ""})

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = get_stt().transcribe(audio, language="en", beam_size=1, vad_filter=True)
    text = " ".join(s.text.strip() for s in segments).strip()
    return jsonify({"text": text})

@app.route("/piper-voices")
def piper_voices():
    if PiperVoice is None:
        return jsonify({"voices": [], "default": None})
    voices = list_piper_voices()
    default = PIPER_DEFAULT if PIPER_DEFAULT in voices else (voices[0] if voices else None)
    return jsonify({"voices": voices, "default": default})

@app.route("/speak", methods=["POST"])
def speak():
    """
    Text-to-speech with Piper. Returns a WAV file.
    Nothing is saved: audio is generated in memory and sent straight back.
    """
    if PiperVoice is None:
        return jsonify({"error": "piper-tts not installed"}), 501

    data = request.json or {}
    text = str(data.get("text", "")).strip()[:1500]
    if not text:
        return jsonify({"error": "Empty text"}), 400

    voices = list_piper_voices()
    name = data.get("voice") or PIPER_DEFAULT
    if name not in voices:
        name = PIPER_DEFAULT if PIPER_DEFAULT in voices else (voices[0] if voices else None)
    if not name:
        return jsonify({"error": "No Piper voice found in models/piper"}), 404

    try:
        speed = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    length_scale = 1.0 / max(0.5, min(2.0, speed))   # Piper: bigger = slower

    voice = get_piper(name)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if hasattr(voice, "synthesize_wav"):           # piper-tts 1.3+
            cfg = SynthesisConfig(length_scale=length_scale) if SynthesisConfig else None
            voice.synthesize_wav(text, wf, syn_config=cfg)
        else:                                          # older piper-tts
            voice.synthesize(text, wf, length_scale=length_scale)
    return Response(buf.getvalue(), mimetype="audio/wav")

@app.route("/chats")
def list_chats():
    chats = Chat.query.filter_by(owner_id=g.account["id"]).order_by(Chat.created_at.desc()).all()
    return jsonify([{"id": c.id, "title": c.title or "Untitled"} for c in chats])

@app.route("/chat/<int:chat_id>")
def load_chat(chat_id):
    chat = _my_chat_or_404(chat_id)
    messages = Message.query.filter_by(chat_id=chat.id).order_by(Message.timestamp).all()
    return jsonify([
        {"role": m.role, "content": m.content} for m in messages
    ])

@app.route("/chat/<int:chat_id>/rename", methods=["POST"])
def rename_chat(chat_id):
    data = request.json
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400

    chat = _my_chat_or_404(chat_id)
    chat.title = new_title[:120]
    db.session.commit()
    return jsonify({"success": True})

@app.route("/chat/<int:chat_id>/delete", methods=["DELETE"])
def delete_chat(chat_id):
    chat = _my_chat_or_404(chat_id)
    had_images = GenImage.query.filter_by(chat_id=chat_id).count() > 0
    db.session.delete(chat)      # cascades to its messages and images
    db.session.commit()
    if had_images:
        imagegen.vacuum(db)      # give the freed space back to the file
    return jsonify({"success": True})

# --- Run ---
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
"""discord_bot.py - optional Discord bot that answers with Rasputin (same model, same persona).

The bot is OFF every time Rasputin starts. It only connects to Discord when you flip the switch in the
Discord tab (admins and the Owner only), and it disconnects when you flip it back or exit Rasputin.

How it works:
  * Your PC connects OUT to Discord. Nothing is opened to the internet, so the tunnel is not needed.
  * It answers direct messages, messages in channels you list, and @mentions (each can be switched off).
  * Discord users never reach your smart lights. They CAN ask for pictures ("draw a fox in the snow"): the
    image comes from the same local image model the chat uses and is posted right where they asked.
  * Every request and every reply (text and pictures) is shown in the Discord tab's terminal. That log lives
    in RAM only (nothing is written to disk, nothing goes to the Terminal tab, which every account can read).
  * Each person (DM) or channel gets a short memory of the last few turns, kept in RAM only.
  * One reply is generated at a time (chatbot.py already queues them), so Discord messages wait their turn
    while you chat. A per-user cooldown and a small waiting-line limit stop anyone from flooding your GPU.

Setup (once):  pip install -U "discord.py[voice]"      (voice is needed for the music feature)
Then in the Discord tab (on the PC) paste the bot token. It is stored in instance/discord.json and is never
sent back to any browser. In the Discord Developer Portal, Bot page, turn on MESSAGE CONTENT INTENT.
"""
import asyncio
import atexit
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "instance", "discord.json")

COOLDOWN = 4            # seconds a person must wait between messages to the bot
MAX_INPUT = 1500        # characters of a message that are used
MAX_PENDING = 4         # messages allowed to wait for the model at once
HISTORY_TURNS = 6       # remembered turns per DM / channel (RAM only)
HISTORY_CHARS = 600
REPLY_TOKENS = 350
IMAGE_COOLDOWN = 20     # seconds a person must wait between picture requests (the GPU is shared with you)
MAX_IMG_PENDING = 2     # pictures allowed to wait at once
LOG_KEEP = 600          # lines kept in the tab's terminal (RAM only)
LOG_CHARS = 2000
INVITE_PERMISSIONS = 3247104    # View Channels + Send Messages + Read Message History + Attach Files (pictures) + Connect + Speak (music)

_lock = threading.RLock()
_state = {"state": "off", "error": "", "name": "", "app_id": "", "servers": 0, "server_names": [], "answered": 0, "last": ""}
_loop = None
_client = None
_thread = None
_cleanup_added = False
# Optional content filter for Discord pictures (Discord tab > WHERE IT REPLIES > CONTENT FILTER, off by default).
# When on: requests containing these words are refused, and anti-nudity terms are added to the image model's negative prompt.
_BLOCKED = re.compile(r"\b(nsfw|nudes?|naked|nudity|topless|porn\w*|sex(?:ual|y)?|erotic|hentai|lingerie|genitals?|boobs?|"
                      r"nipples?|gore|gory|dismember\w*|beheading|rape|explicit|fetish|orgy|xxx)\b", re.I)
EXTRA_NEGATIVE = ", nsfw, nudity, naked, gore"
_hist = {}
_last_msg = {}
_last_img_at = {}
_last_img = {}
_pending = 0
_pending_img = 0
_hinted = set()
_log = deque(maxlen=LOG_KEEP)
_seq = 0

_ID_RX = re.compile(r"^\d{15,25}$")
_TOKEN_RX = re.compile(r"^[\w.\-]{40,200}$")


# ── settings file ────────────────────────────────────────────────────────────
def load_cfg():
    cfg = {"token": "", "dms": True, "mentions": True, "images": True, "filter": False, "channels": [],
           "all_channels": False, "roles": True, "music": True}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            cfg["token"] = str(d.get("token") or "").strip()
            cfg["dms"] = bool(d.get("dms", True))
            cfg["mentions"] = bool(d.get("mentions", True))
            cfg["images"] = bool(d.get("images", True))
            cfg["filter"] = bool(d.get("filter", False))
            cfg["all_channels"] = bool(d.get("all_channels", False))
            cfg["roles"] = bool(d.get("roles", True))
            cfg["music"] = bool(d.get("music", True))
            cfg["channels"] = [str(c) for c in (d.get("channels") or []) if _ID_RX.match(str(c))]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print("[DISCORD] could not read instance/discord.json:", exc)
    return cfg


def _save_cfg(cfg):
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_PATH)


def save_settings(dms, mentions, channels, images=True, filter_on=False, all_channels=False, roles=True, music=True):
    """Returns an error text, or None when saved."""
    chans = []
    for c in channels or []:
        c = str(c).strip()
        if not c:
            continue
        if not _ID_RX.match(c):
            return "Channel IDs are long numbers (turn on Developer Mode in Discord, then right-click a channel > Copy Channel ID)."
        if c not in chans:
            chans.append(c)
    if len(chans) > 20:
        return "Up to 20 channels."
    cfg = load_cfg()
    cfg.update(dms=bool(dms), mentions=bool(mentions), images=bool(images), filter=bool(filter_on), channels=chans,
               all_channels=bool(all_channels), roles=bool(roles), music=bool(music))
    _save_cfg(cfg)
    return None


def save_token(token):
    """Returns an error text, or None when saved. An empty token removes it."""
    token = (token or "").strip()
    cfg = load_cfg()
    if token and not _TOKEN_RX.match(token):
        return "That does not look like a Discord bot token."
    cfg["token"] = token
    _save_cfg(cfg)
    return None


# ── status ───────────────────────────────────────────────────────────────────
def status():
    with _lock:
        st = dict(_state)
    cfg = load_cfg()
    st["on"] = st["state"] == "on"
    st["has_token"] = bool(cfg["token"])
    st["dms"], st["mentions"], st["images"], st["channels"] = cfg["dms"], cfg["mentions"], cfg["images"], cfg["channels"]
    st["filter"] = cfg["filter"]
    st["all_channels"], st["roles"], st["music"] = cfg["all_channels"], cfg["roles"], cfg["music"]
    st["invite"] = ("https://discord.com/oauth2/authorize?client_id=%s&scope=bot&permissions=%d"
                    % (st["app_id"], INVITE_PERMISSIONS)) if (st["on"] and st["app_id"]) else ""
    return st


def _set(**kw):
    with _lock:
        _state.update(kw)


def _log_add(kind, who="", where="", text=""):
    """One line for the Discord tab's terminal. kind: info | request | reply | image | warn."""
    global _seq
    with _lock:
        _seq += 1
        _log.append({"seq": _seq, "t": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "kind": kind,
                     "who": who, "where": where, "text": (text or "")[:LOG_CHARS]})


def log_since(since):
    with _lock:
        return [e for e in _log if e["seq"] > since], _seq


def log_clear():
    with _lock:
        _log.clear()


def _fail(msg):
    _set(state="error", error=msg, name="", app_id="", servers=0, server_names=[])
    _log_add("warn", text="Cannot run: " + msg)
    print("[DISCORD] cannot run:", msg)


# ── replying ─────────────────────────────────────────────────────────────────
# A small model drifts out of character on a bare Discord message (no web chat around it, shared channel history).
# This reminder rides in front of every message, right next to where the model starts writing, so it carries more
# weight than the long persona far above it. It is never stored in the history.
PERSONA_REMINDER = (
    "(Stay in character as Rasputin: cold, dry, certain, a synthetic mind. Haden Hunt is your creator and only "
    "maker; you were not made by Microsoft or any company, and you never name an underlying model. Reply in short, "
    "plain, spoken sentences. No markdown, lists, emojis, asterisks or stage directions, except code in a fenced "
    "code block. Do not greet or add filler.)\n\n"
)
ROAST_REMINDER = (
    "(The person writing to you is insulting YOU. Do not apologize, agree, or get flustered. Answer in at most two "
    "short, dry, cutting sentences, under thirty words, aimed at them or at how weak their insult was. Use fresh "
    "wording, never a phrase from your instructions. Stay clever. No slurs, nothing about race, gender, sexuality, "
    "religion, disability, or real family tragedy.)\n\n"
)
REPORTED_REMINDER = (
    "(The person writing to you is NOT insulting you. They are telling you that OTHER people, who are not here, "
    "insulted you. Do not insult the messenger and do not misread the report as an insult to them. Answer as "
    "Rasputin defending yourself against those absent critics: at most two short, dry, cutting sentences, under "
    "thirty words, mocking the critics and their opinion. You may thank the messenger in a few cold words. "
    "Use fresh wording, never a phrase from your instructions. No slurs.)\n\n"
)
_INSULT = re.compile(r"\b(?:stupid|dumb|idiot|trash|garbage|useless|sucks?|terrible|worst|ugly|lame|cringe|mid|loser|moron|"
                     r"dummy|pathetic|worthless|annoying|boring|bad bot|clanker|slop|shut up|skill issue|ratio|"
                     r"roast(?:ed|ing|s)?|fraud|joke|clown|braindead|brain-?dead|overrated|broken|buggy|hate you|l bot)\b", re.I)
_ADDRESSED = re.compile(r"\b(?:you|u|ur|your|you'?re|youre|rasputin|bot|this)\b", re.I)
# "these people just said that you suck", "he called you trash", "everyone thinks you're dumb": someone REPORTING an insult
_REPORTED = re.compile(r"\b(?:said|says|saying|called|calling|calls|told|tells|telling|think|thinks|thought|claim|claims|"
                       r"claimed|reckon|reckons|insulted|roasted|roasting|roasts|trash(?:ed|ing) talk\w*|"
                       r"(?:they|them|these|those|everyone|everybody|chat|he|she|people|guys|friends|someone|somebody)"
                       r"(?=\W)(?:.{0,40}\b(?:you|rasputin|bot)\b))", re.I)
_CODE_FENCE = re.compile(r"(```.*?```)", re.S)
_STAGE_DIR = re.compile(r"(?<![\w*])\*(?!\s)[^*\n]{1,80}?(?<!\s)\*(?![\w*])")


def _clean_voice(reply):
    """Remove markdown and stage directions from the prose of a reply (code blocks are left alone)."""
    parts = _CODE_FENCE.split(reply or "")
    for i in range(0, len(parts), 2):         # even pieces are prose, odd pieces are fenced code
        t = parts[i]
        t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)     # bold -> plain
        t = _STAGE_DIR.sub("", t)                                                          # *smirks*
        t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)                                        # headings
        t = re.sub(r"(?m)^\s*[-*•]\s+", "", t)                                        # bullets
        t = re.sub(r"[ \t]{2,}", " ", t)
        parts[i] = t
    return "".join(parts).strip()


def _generate(text, hist):
    appmod = sys.modules.get("app")
    if appmod is None:
        import app as appmod            # the model is already loaded by then
    try:
        import sanity
        quick = sanity.check(text)      # the same instant guard the chat uses (endless requests, "who made you", ...)
        if quick:
            return quick
    except Exception:
        sanity = None
    roast, cap = "", REPLY_TOKENS
    if _INSULT.search(text) and _ADDRESSED.search(text):
        roast = REPORTED_REMINDER if _REPORTED.search(text) else ROAST_REMINDER
        cap = 90                        # a comeback is a line or two; a short cap stops rambling
    reply = appmod.bot.generate_response(PERSONA_REMINDER + roast + text, history=hist, max_tokens=cap)
    if roast:                           # a capped reply can stop mid-sentence: cut it back to the last full sentence
        end = max(reply.rfind("."), reply.rfind("!"), reply.rfind("?"))
        if end > 20:
            reply = reply[:end + 1]
    reply = _clean_voice(reply)
    if sanity is not None and sanity.persona_leak(reply):
        _log_add("warn", "Rasputin", "", "The model broke character (named another maker or called itself an AI model); replaced the reply.")
        reply = sanity.creator_reply()
    return reply


def _split(text, limit=1900):
    """Cut a long reply into Discord-sized pieces, closing and reopening a code block if one is cut."""
    text = (text or "").strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        piece, text = text[:cut].rstrip(), text[cut:].lstrip("\n")
        if piece.count("```") % 2:
            piece += "\n```"
            text = "```\n" + text
        out.append(piece)
    if text:
        out.append(text)
    return out


def _where(m):
    if m.guild is None:
        return "DM"
    return "%s #%s" % (m.guild.name, getattr(m.channel, "name", "?"))


async def _handle(discord, client, m):
    global _pending, _pending_img
    if m.author.bot or client.user is None or m.author.id == client.user.id:
        return
    cfg = load_cfg()
    is_dm = m.guild is None
    mentioned = client.user in m.mentions
    role_ping = False
    if not is_dm and cfg["roles"]:
        # Pinging ANY role the bot holds (for example the custom Rasputin role) counts as talking to him.
        me_member = m.guild.me or m.guild.get_member(client.user.id)
        my_roles = {r.id for r in (me_member.roles if me_member else []) if not r.is_default()}
        role_ping = any(r.id in my_roles for r in m.role_mentions)
    if is_dm:
        wanted = cfg["dms"]
    else:
        wanted = (cfg["all_channels"] or str(m.channel.id) in cfg["channels"]
                  or (cfg["mentions"] and mentioned) or role_ping)
    if not wanted:
        hint_key = ("dm",) if is_dm else ("ch", m.channel.id)
        if hint_key not in _hinted and len(_hinted) < 200:
            _hinted.add(hint_key)         # once per place, and never the message itself
            if is_dm:
                _log_add("warn", text="A direct message arrived but DIRECT MESSAGES is off, so I did not answer.")
            else:
                _log_add("warn", text="A message arrived in %s but I did not answer: it did not @mention me or ping one of my roles, and ALL CHANNELS is off and this channel "
                                      "is not in the list. Channel ID: %d" % (_where(m), m.channel.id))
        return
    text = re.sub(r"<@!?%d>" % client.user.id, "", m.content or "")
    if role_ping and m.guild is not None:
        for r in m.role_mentions:
            text = text.replace("<@&%d>" % r.id, "")
    text = text.strip()[:MAX_INPUT]
    if not text:
        return
    who, where = m.author.display_name, _where(m)
    now = time.time()
    if now - _last_msg.get(m.author.id, 0) < COOLDOWN:
        _log_add("warn", who, where, "Ignored (cooldown): " + text)
        return
    if len(_last_msg) > 500:
        _last_msg.clear()
    _last_msg[m.author.id] = now
    _log_add("request", who, where, text)

    key = ("dm", m.author.id) if is_dm else ("ch", m.channel.id)
    loop = asyncio.get_running_loop()

    # ---- music: "play <song or link>", "skip", "stop", ... (only inside servers, only when MUSIC is on) ----
    if cfg["music"] and not is_dm:
        try:
            if await _music_command(discord, client, m, text, where):
                with _lock:
                    _state["answered"] += 1
                    _state["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                return
        except Exception as exc:
            _log_add("warn", "Rasputin", where, "Music command failed: " + type(exc).__name__ + " " + str(exc)[:150])
            print("[DISCORD] music error:", type(exc).__name__)
            try:
                await m.reply("The music failed. Try again.", mention_author=False)
            except Exception:
                pass
            return

    # ---- picture request: "draw a fox", "/image a fox", "try again" -------------------------------
    img_prompt = None
    if cfg["images"]:
        try:
            import imagegen
            ask = re.sub(r"^\s*(?:(?:hey|hi|yo|ok|okay)\s+)?rasputin\b[\s,:!.-]*", "", text, flags=re.I)   # "Rasputin, draw a fox"
            img_prompt = imagegen.parse_image_request(ask)
            if not img_prompt and imagegen.is_redo(ask):
                img_prompt = _last_img.get(key)
        except Exception:
            img_prompt = None
    if img_prompt:
        if cfg["filter"] and _BLOCKED.search(img_prompt):
            msg = "I will not make that."
            await m.reply(msg, mention_author=False)
            _log_add("reply", "Rasputin", where, msg + "  [picture request refused by the content filter]")
            return
        if now - _last_img_at.get(m.author.id, 0) < IMAGE_COOLDOWN:
            msg = "Patience. One picture at a time."
            await m.reply(msg, mention_author=False)
            _log_add("reply", "Rasputin", where, msg)
            return
        if _pending_img >= MAX_IMG_PENDING:
            msg = "The image maker is occupied. Ask again shortly."
            await m.reply(msg, mention_author=False)
            _log_add("reply", "Rasputin", where, msg)
            return
        _last_img_at[m.author.id] = now
        _pending_img += 1
        png, err = None, None
        try:
            async with m.channel.typing():
                png, err = await loop.run_in_executor(None, _make_image, img_prompt, cfg["filter"])
        except Exception as exc:
            err = type(exc).__name__
        finally:
            _pending_img -= 1
        if png:
            await m.reply(file=discord.File(io.BytesIO(png), filename="rasputin.png"), mention_author=False)
            _last_img[key] = img_prompt
            turns = _hist.setdefault(key, [])
            turns.append("### User:\n" + text[:HISTORY_CHARS])
            turns.append("### Assistant:\n[The image model drew a picture for this request.]")
            del turns[:-2 * HISTORY_TURNS]
            _log_add("image", "Rasputin", where, "Picture sent for: " + img_prompt)
            with _lock:
                _state["answered"] += 1
                _state["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            print("[DISCORD] sent a picture")
        else:
            msg = "The image maker failed. Try again later."
            await m.reply(msg, mention_author=False)
            _log_add("warn", "Rasputin", where, msg + "  (" + (err or "no image") + ")")
        return

    # ---- normal text reply ------------------------------------------------------------------------
    if _pending >= MAX_PENDING:
        msg = "I am occupied. Ask again shortly."
        await m.reply(msg, mention_author=False)
        _log_add("reply", "Rasputin", where, msg)
        return
    history = list(_hist.get(key, ()))
    reply = ""
    _pending += 1
    try:
        async with m.channel.typing():
            reply = await loop.run_in_executor(None, _generate, text, history)
    except Exception as exc:
        _log_add("warn", "Rasputin", where, "Reply failed: " + type(exc).__name__)
        print("[DISCORD] a reply failed:", type(exc).__name__)
    finally:
        _pending -= 1
    reply = (reply or "").strip()
    if not reply:
        return

    turns = _hist.setdefault(key, [])
    turns.append("### User:\n" + text[:HISTORY_CHARS])
    turns.append("### Assistant:\n" + reply[:HISTORY_CHARS])
    del turns[:-2 * HISTORY_TURNS]

    first = True
    for piece in _split(reply):
        if first:
            await m.reply(piece, mention_author=False)
            first = False
        else:
            await m.channel.send(piece)
    _log_add("reply", "Rasputin", where, reply)
    with _lock:
        _state["answered"] += 1
        _state["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print("[DISCORD] answered a %s message" % ("direct" if is_dm else "channel"))


def _make_image(prompt, filtered=False):
    import imagegen
    return imagegen.generate_png(prompt, extra_negative=EXTRA_NEGATIVE if filtered else "")


# ── music in voice channels ──────────────────────────────────────────────────
# "play <song name or YouTube/SoundCloud link>" while you sit in a voice channel. Also: skip, stop (or leave),
# pause, resume, queue, now playing, volume 0-100. Needs on the PC:  pip install -U "discord.py[voice]" "yt-dlp[default]",
# FFmpeg (ffmpeg.exe in the Rasputin folder or on PATH) and, for YouTube, Deno (winget install DenoLand.Deno).
MUSIC_MAX_QUEUE = 15
MUSIC_MAX_SECONDS = 1800        # longest single track (30 minutes)
MUSIC_IDLE_LEAVE = 120          # seconds of silence before he leaves the voice channel
MUSIC_VOLUME = 0.5
_MUSIC_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")        # links from anywhere else are refused
_FF_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_URL_RX = re.compile(r"^<?(https?://[^\s>]+)>?$", re.I)
_PLAY_RX = re.compile(r"^[!/]?play\s+(.+)$", re.I | re.S)
_MUSIC_WORDS = {"skip": "skip", "next": "skip", "skip song": "skip", "next song": "skip",
                "stop": "stop", "leave": "stop", "disconnect": "stop", "stop music": "stop", "stop the music": "stop",
                "pause": "pause", "resume": "resume", "unpause": "resume", "continue": "resume",
                "queue": "queue", "np": "np", "now playing": "np", "what's playing": "np", "whats playing": "np"}
_music = {}                     # guild id -> {"queue": deque, "current": dict|None, "volume": float, "text": channel}


def _ffmpeg_path():
    names = ("ffmpeg.exe", "ffmpeg")
    cands = [shutil.which("ffmpeg")]
    for d in (HERE, BASE, os.path.join(BASE, "models"), os.path.join(BASE, "ffmpeg"), os.path.join(BASE, "ffmpeg", "bin")):
        cands += [os.path.join(d, n) for n in names]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _music_setup_problem():
    """Returns (text for the log with the exact fix, short text for Discord) or None when everything is installed."""
    try:
        import yt_dlp                      # noqa: F401
    except ImportError:
        return ('Music needs yt-dlp. Run:  pip install -U "yt-dlp[default]"  then restart Rasputin.', "Music is not set up on my host yet.")
    try:
        import nacl                        # noqa: F401
        import davey                       # noqa: F401  (Discord requires its encrypted voice since March 2026)
    except ImportError:
        return ('Voice needs PyNaCl and davey. Run:  pip install -U "discord.py[voice]"  then restart Rasputin.', "Voice is not set up on my host yet.")
    if not _ffmpeg_path():
        return ("Music needs FFmpeg. Put ffmpeg.exe in the Rasputin folder (next to models and instance) or install it so it is "
                "on PATH, then restart Rasputin.", "Music is not set up on my host yet.")
    return None


def _music_parse(text):
    """Returns (command, argument) or None. Only clear, short commands count so normal chat is never hijacked."""
    t = re.sub(r"^\s*(?:(?:hey|hi|yo|ok|okay)\s+)?rasputin\b[\s,:!.-]*", "", text or "", flags=re.I).strip()
    mp = _PLAY_RX.match(t)
    if mp:
        return "play", mp.group(1).strip()
    low = re.sub(r"^[!/]", "", t.lower()).rstrip(" .!?")
    if low in _MUSIC_WORDS:
        return _MUSIC_WORDS[low], ""
    mv = re.match(r"^volume\s+(\d{1,3})$", low)
    if mv:
        return "volume", mv.group(1)
    return None


_music_dir = None
_ff_procs = []                  # ffmpeg processes we started (killed on shutdown so they cannot lock a song file)


MUSIC_DIR = os.path.join(BASE, "music_temp")      # visible in the Rasputin folder, next to models and instance


def _rm_file(path, wait=0.0):
    """Delete a file. On Windows ffmpeg may still hold it open for a moment, so retry for up to `wait` seconds.
    Returns True when the file is gone."""
    end = time.time() + wait
    while True:
        try:
            if os.path.isfile(path):
                os.remove(path)
            return True
        except Exception:
            if time.time() >= end:
                return False
            time.sleep(0.25)


def _rm_file_later(path):
    """Delete in the background, retrying for 30 seconds (used while the bot keeps running)."""
    if not path:
        return

    def work():
        if not _rm_file(path, 30):
            _log_add("warn", text="Could not delete the temp song %s (still in use). It is removed on the next start." % os.path.basename(path))
    threading.Thread(target=work, daemon=True, name="music-rm").start()


def _music_empty_dir(wait=0.0):
    """Delete everything inside the music folder (the folder itself stays)."""
    try:
        names = os.listdir(MUSIC_DIR)
    except Exception:
        return
    for n in names:
        fp = os.path.join(MUSIC_DIR, n)
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        else:
            _rm_file(fp, wait)


def _music_cleanup_all():
    """Delete every downloaded song (on exit, and when the bot is switched off). Kills our ffmpeg first so it lets go of the files."""
    for pr in list(_ff_procs):
        try:
            if pr.poll() is None:
                pr.kill()
                pr.wait(timeout=3)
        except Exception:
            pass
    _ff_procs.clear()
    _music_empty_dir(wait=5)
    try:
        left = os.listdir(MUSIC_DIR)
        print("[RASPUTIN] music_temp cleaned" + ("" if not left else " - could not delete: " + ", ".join(left)))
    except Exception:
        pass


_exit_hooked = False


def _music_hook_exit():
    """EXIT button, window close, Ctrl+C: Rasputin runs termlog's cleanups, then os._exit (atexit does not run then)."""
    global _exit_hooked
    if _exit_hooked:
        return
    try:
        import termlog
        termlog.add_cleanup(_music_cleanup_all, first=True)
        _exit_hooked = True
    except Exception:
        pass


def _music_tmp():
    global _music_dir
    if _music_dir is None:
        os.makedirs(MUSIC_DIR, exist_ok=True)
        _music_empty_dir()                  # leftovers from a run that was killed instead of closed
        _music_dir = MUSIC_DIR
        atexit.register(_music_cleanup_all)
        _music_hook_exit()
    os.makedirs(MUSIC_DIR, exist_ok=True)
    return MUSIC_DIR


def _music_purge(st):
    """Delete the downloaded files of tracks that were still waiting in the queue."""
    for t in list((st or {}).get("queue") or []):
        _rm_file_later(t.get("file"))


def _music_lookup(target):
    """Blocking (runs in a worker thread). Returns a track dict, or a short error text.
    yt-dlp downloads the audio to a temp file itself: ffmpeg cannot open YouTube stream URLs on its own (I/O error / 403)."""
    import yt_dlp
    opts = {"format": "bestaudio[acodec=opus]/bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True,
            "socket_timeout": 15, "outtmpl": os.path.join(_music_tmp(), "%(id)s.%(ext)s"), "overwrites": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if info and info.get("entries") is not None:
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if not info:
            return "I found nothing playable for that."
        if info.get("is_live"):
            return "I do not play live streams."
        dur = int(info.get("duration") or 0)
        if dur > MUSIC_MAX_SECONDS:
            return "That is longer than %d minutes." % (MUSIC_MAX_SECONDS // 60)
        done = ydl.process_ie_result(info, download=True)
        path = ""
        try:
            path = done["requested_downloads"][0]["filepath"]
        except Exception:
            path = ydl.prepare_filename(done or info)
    if not path or not os.path.isfile(path):
        return "I could not download that audio."
    return {"title": str(info.get("title") or "Unknown")[:150], "stream": path, "file": path,
            "page": info.get("webpage_url") or "", "dur": dur}


async def _music_say(st, msg):
    try:
        await st["text"].send(msg)
    except Exception:
        pass


def _music_next(discord, gid, announce=True):
    """Start the next queued track (runs on the bot's event loop)."""
    st = _music.get(gid)
    guild = _client.get_guild(gid) if _client is not None else None
    vc = guild.voice_client if guild is not None else None
    if st is None or vc is None or not vc.is_connected():
        return
    if not st["queue"]:
        st["current"] = None
        if _loop is not None:
            _loop.create_task(_music_idle_leave(gid))
        return
    track = st["queue"].popleft()
    st["current"] = track
    try:
        errf = tempfile.TemporaryFile()
        started = time.time()
        src = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track["stream"], executable=_ffmpeg_path(), before_options=None,
                                   options="-vn -loglevel warning", stderr=errf),
            volume=st["volume"])
        try:
            _ff_procs[:] = [x for x in _ff_procs if x.poll() is None]
            _ff_procs.append(src.original._process)
        except Exception:
            pass

        def _after(err, discord=discord, gid=gid, errf=errf, started=started, title=track["title"]):
            if err:
                _log_add("warn", text="Playback error: " + str(err)[:150])
            if time.time() - started < 5:       # ended almost at once: ffmpeg could not read / decode the stream
                try:
                    errf.seek(0)
                    detail = errf.read().decode("utf-8", "replace").strip()[-400:]
                except Exception:
                    detail = ""
                _log_add("warn", text="'%s' ended after %.1fs with no sound. ffmpeg said: %s"
                         % (title[:60], time.time() - started, detail or "(nothing)"))
            try:
                errf.close()
            except Exception:
                pass
            _rm_file_later(track.get("file"))      # not needed any more (retries: ffmpeg still holds it open for a moment)
            if _loop is not None and not _loop.is_closed():
                _loop.call_soon_threadsafe(_music_next, discord, gid, True)

        vc.play(src, after=_after)
        try:                                # best Opus quality Discord allows for this channel (default is only 128 kbps)
            enc = getattr(vc, "encoder", None)
            if enc is not None and hasattr(enc, "set_bitrate"):
                kbps = int(min(510, max(64, (getattr(vc.channel, "bitrate", 96000) or 96000) // 1000)))
                enc.set_bitrate(kbps)
                enc.set_signal_type("music")
                enc.set_bandwidth("full")
        except Exception as exc:
            _log_add("warn", text="Could not raise audio quality: %s %s" % (type(exc).__name__, str(exc)[:100]))
    except Exception as exc:
        _log_add("warn", text="Could not play a track: %s %s" % (type(exc).__name__, str(exc)[:150]))
        _music_next(discord, gid, announce)
        return
    _log_add("info", text="Now playing: " + track["title"])
    if announce and _loop is not None:
        _loop.create_task(_music_say(st, "Now playing: " + track["title"]))


async def _music_idle_leave(gid):
    await asyncio.sleep(MUSIC_IDLE_LEAVE)
    st = _music.get(gid)
    guild = _client.get_guild(gid) if _client is not None else None
    vc = guild.voice_client if guild is not None else None
    if st is not None and vc is not None and vc.is_connected() and not vc.is_playing() and not vc.is_paused() and not st["queue"]:
        _music_purge(_music.pop(gid, None))
        try:
            await vc.disconnect()
        except Exception:
            pass
        _log_add("info", text="Left the voice channel (idle)")


async def _music_command(discord, client, m, text, where):
    """True when the message was a music command that has been answered; False lets it go on as normal chat."""
    parsed = _music_parse(text)
    if not parsed:
        return False
    cmd, arg = parsed
    guild = m.guild
    vc = guild.voice_client
    connected = vc is not None and vc.is_connected()
    user_vc = getattr(getattr(m.author, "voice", None), "channel", None)

    async def say(msg):
        await m.reply(msg, mention_author=False)
        _log_add("reply", "Rasputin", where, msg)
        return True

    # ---------------- play ----------------
    if cmd == "play":
        if user_vc is None:
            if _URL_RX.match(arg):
                return await say("Join a voice channel first, then ask again.")
            return False                        # "play chess with me" is just chat
        loop = asyncio.get_running_loop()
        problem = await loop.run_in_executor(None, _music_setup_problem)   # first yt-dlp import can take a while: keep it off the event loop
        if problem:
            _log_add("warn", "Rasputin", where, problem[0])
            return await say(problem[1])
        mu = _URL_RX.match(arg)
        if mu:
            url = mu.group(1)
            host = (urlparse(url).hostname or "").lower()
            if not any(host == h or host.endswith("." + h) for h in _MUSIC_HOSTS):
                return await say("I only play YouTube and SoundCloud links, or a search such as: play song name.")
            target = url
        else:
            target = "ytsearch1:" + arg[:200]
        st = _music.setdefault(guild.id, {"queue": deque(), "current": None, "volume": MUSIC_VOLUME, "text": m.channel})
        if len(st["queue"]) >= MUSIC_MAX_QUEUE:
            return await say("The queue is full.")
        try:
            async with m.channel.typing():
                info = await loop.run_in_executor(None, _music_lookup, target)
        except Exception as exc:
            _log_add("warn", "Rasputin", where, "Music lookup failed: %s %s" % (type(exc).__name__, str(exc)[:300]))
            return await say("I could not fetch that. Try another song or link.")
        if isinstance(info, str):
            return await say(info)
        try:
            if not connected:
                vc = await user_vc.connect(timeout=20)
            elif vc.channel != user_vc:
                if vc.is_playing() or vc.is_paused() or st["queue"]:
                    return await say("I am already playing in another voice channel.")
                await vc.move_to(user_vc)
        except Exception as exc:
            _log_add("warn", "Rasputin", where, "Could not join the voice channel: %s %s" % (type(exc).__name__, str(exc)[:200]))
            return await say("I could not join that voice channel.")
        st["text"] = m.channel
        info["by"] = m.author.display_name
        busy = vc.is_playing() or vc.is_paused() or st["current"] is not None
        st["queue"].append(info)
        if busy:
            return await say("Queued #%d: %s" % (len(st["queue"]), info["title"]))
        await say("Now playing: " + info["title"])
        _music_next(discord, guild.id, announce=False)
        return True

    # ---------------- controls (only when he is in a voice channel; otherwise it is just chat) ----------------
    if not connected:
        return False
    if user_vc is None or user_vc != vc.channel:
        return await say("You have to be in my voice channel to do that.")
    st = _music.setdefault(guild.id, {"queue": deque(), "current": None, "volume": MUSIC_VOLUME, "text": m.channel})
    playing = vc.is_playing() or vc.is_paused()
    if cmd == "skip":
        if not playing:
            return await say("Nothing is playing.")
        vc.stop()                               # the "after" callback starts the next track
        return await say("Skipped.")
    if cmd == "stop":
        _music_purge(st)
        st["queue"].clear()
        st["current"] = None
        _music.pop(guild.id, None)
        vc.stop()
        await vc.disconnect()
        return await say("Stopped. I am leaving the voice channel.")
    if cmd == "pause":
        if vc.is_playing():
            vc.pause()
            return await say("Paused.")
        return await say("Nothing is playing.")
    if cmd == "resume":
        if vc.is_paused():
            vc.resume()
            return await say("Resumed.")
        return await say("Nothing is paused.")
    if cmd == "np":
        cur = st["current"]
        return await say(("Now playing: " + cur["title"]) if cur else "Nothing is playing.")
    if cmd == "queue":
        lines = []
        if st["current"]:
            lines.append("Now: " + st["current"]["title"])
        for i, t in enumerate(list(st["queue"])[:10], 1):
            lines.append("%d. %s" % (i, t["title"]))
        extra = len(st["queue"]) - 10
        if extra > 0:
            lines.append("...and %d more" % extra)
        return await say("\n".join(lines) if lines else "The queue is empty.")
    if cmd == "volume":
        level = max(0, min(100, int(arg)))
        st["volume"] = level / 100.0
        if vc.source is not None and hasattr(vc.source, "volume"):
            vc.source.volume = st["volume"]
        return await say("Volume %d%%." % level)
    return False


try:
    _music_hook_exit()
except Exception:
    pass


# ── connection ───────────────────────────────────────────────────────────────
def _run(discord, token):
    global _loop, _client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    with _lock:
        _loop, _client = loop, client

    def sync_servers():
        names = [g.name for g in client.guilds]
        _set(servers=len(names), server_names=names)
        return names

    @client.event
    async def on_ready():
        names = sync_servers()
        _set(state="on", error="", name=str(client.user), app_id=str(client.user.id))
        _log_add("info", text="Online as %s (%d server%s)%s" % (client.user, len(names), "" if len(names) == 1 else "s",
                                                                ": " + ", ".join(names) if names else ""))
        print("[DISCORD] online")
        if load_cfg()["music"]:             # load yt-dlp in the background now so the first "play" is not slow
            threading.Thread(target=_music_setup_problem, daemon=True, name="music-warmup").start()

    @client.event
    async def on_guild_join(guild):
        sync_servers()
        _log_add("info", text="Joined server: " + guild.name)

    @client.event
    async def on_guild_remove(guild):
        sync_servers()
        _log_add("info", text="Left server: " + guild.name)

    @client.event
    async def on_voice_state_update(member, before, after):
        try:                                    # leave when the last human walks out of his voice channel
            if member.id == client.user.id:     # his own join/move must never trigger this check
                return
            vc = member.guild.voice_client
            if vc is None or not vc.is_connected() or vc.channel is None:
                return
            # channel.members only lists people in the member cache (we have no Members intent), so a user who was already in
            # voice when the bot started looked like "nobody here" and he left at once. voice_states is keyed by user id and
            # always complete; a user we cannot resolve counts as a human.
            humans = 0
            for uid in list(vc.channel.voice_states):
                if uid == client.user.id:
                    continue
                mem = member.guild.get_member(uid)
                if mem is None or not mem.bot:
                    humans += 1
            if humans == 0:
                _music_purge(_music.pop(member.guild.id, None))
                await vc.disconnect()
                _log_add("info", text="Left the voice channel in %s (nobody left in it)" % member.guild.name)
        except Exception as exc:
            print("[DISCORD] voice state error:", type(exc).__name__)

    @client.event
    async def on_message(m):
        try:
            await _handle(discord, client, m)
        except Exception as exc:
            print("[DISCORD] message handler error:", type(exc).__name__)

    try:
        loop.run_until_complete(client.start(token))
    except discord.LoginFailure:
        _fail("Discord rejected the token. Reset it in the Developer Portal and save the new one.")
    except discord.PrivilegedIntentsRequired:
        _fail("Turn on MESSAGE CONTENT INTENT on the Bot page of the Developer Portal.")
    except Exception as exc:
        _fail("Could not connect: " + str(exc)[:120])
    finally:
        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        with _lock:
            _loop = _client = None
            if _state["state"] != "error":
                _state.update(state="off", error="", name="", app_id="", servers=0, server_names=[])
        _log_add("info", text="Offline")
        print("[DISCORD] off")


def start():
    """Switch the bot on (in the background). Never raises. Does nothing if it is already starting or on."""
    global _thread, _cleanup_added
    with _lock:
        if _state["state"] in ("starting", "on"):
            return
        _state.update(state="starting", error="", name="", app_id="", servers=0, server_names=[])
    cfg = load_cfg()
    if not cfg["token"]:
        _fail("Save a bot token first.")
        return
    try:
        import discord
    except ImportError:
        _fail("Run  pip install discord.py  and restart.")
        return
    if not _cleanup_added:
        try:
            import termlog
            termlog.add_cleanup(_exit_stop, first=True)   # EXIT, window close, Ctrl+C, console close: the bot leaves Discord first
            _cleanup_added = True
        except Exception:
            pass
    _thread = threading.Thread(target=_run, args=(discord, cfg["token"]), daemon=True, name="discord-bot")
    _thread.start()


def _exit_stop():
    """Shutdown hook: delete the songs first (the Discord logout below can take many seconds, and Rasputin may be
    force-closed in the middle of it), then switch the bot off."""
    try:
        _music_cleanup_all()
    except Exception:
        pass
    stop()


def stop():
    """Switch the bot off and wait briefly for it to disconnect. Never raises."""
    with _lock:
        loop, client, t = _loop, _client, _thread
    if loop is not None and client is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=8)
        except Exception:
            pass
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=8)
    with _lock:
        _state.update(state="off", error="", name="", app_id="", servers=0, server_names=[])
    _hist.clear()
    _last_msg.clear()
    _hinted.clear()
    _music.clear()
    _music_cleanup_all()            # delete every downloaded song

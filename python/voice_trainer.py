"""
Rasputin Voice Trainer
======================
A step-by-step helper that turns recordings of a voice into a new Piper voice
and installs it into Rasputin (models/piper). It does not touch any existing
Rasputin code: Rasputin already lists every voice found in models/piper.

Put this file in the same folder as app.py (the "Python" folder) and start it
with "Train Voice.bat" (or:  python voice_trainer.py).

The data prep (steps 1-3) runs right here with what Rasputin already uses
(numpy, faster-whisper). The actual training (step 4) needs Linux tools, so on
Windows it runs inside WSL2, which this tool sets up scripts for.
"""
import os
import re
import sys
import json
import wave
import shutil
import subprocess
import numpy as np

SR = 22050                       # Piper "medium" voices use 22050 Hz
IS_WIN = os.name == "nt"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE) if os.path.basename(HERE).lower() == "python" else HERE
PIPER_DIR = os.path.join(ROOT, "models", "piper")
TRAIN_DIR = os.path.join(ROOT, "voice_training")

CKPT_PAGE = "https://huggingface.co/datasets/rhasspy/piper-checkpoints/tree/main"
DOCS_PAGE = "https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md"

LANGS = {
    "1": ("English (US)", "en-us"),
    "2": ("English (UK)", "en-gb"),
}

# ---------------------------------------------------------------------------
# Sentences to read when you choose "record with the microphone".
# (You can replace them: put your own lines, one per line, in prompts.txt
#  inside your voice's folder.)
# ---------------------------------------------------------------------------
PROMPTS = [
    "The quick brown fox jumps over the lazy dog near the river bank.",
    "Please tell me what you found when you opened the old wooden door.",
    "I did not expect the weather to change so quickly this afternoon.",
    "Some people prefer quiet mornings, while others love the noise of the city.",
    "Can you explain why the engine stopped running without any warning?",
    "Every system is online, and all of the sensors are reporting normal levels.",
    "She carefully measured the water, then slowly poured it into the glass.",
    "We should leave before the sun goes down and the road becomes dangerous.",
    "What happens when a machine learns faster than the people who built it?",
    "He walked across the long bridge, thinking about everything that had changed.",
    "The library was silent except for the soft sound of turning pages.",
    "Look at that! The whole sky has turned a deep shade of orange.",
    "Nobody believed the story at first, but the evidence was impossible to ignore.",
    "Thank you for waiting. I will be with you in just a moment.",
    "Why would anyone choose to travel through the mountains in the middle of winter?",
    "The report was finished late, but it covered every detail we had asked for.",
    "I have seen what lies beyond the edge of the map, and it is not empty.",
    "Turn left at the next corner, then continue straight until you reach the harbor.",
    "It was a simple plan, and that is exactly why it worked so well.",
    "Fresh bread, hot coffee, and a quiet table by the window made the morning perfect.",
    "Do not touch that switch unless you are absolutely certain about what it does.",
    "Strange lights moved slowly across the field, and then they vanished completely.",
    "The teacher asked everyone to write a few sentences about their favorite season.",
    "You are stronger than you think, and you are closer than you realize.",
    "A thousand small decisions led us to this moment, and none of them were accidental.",
    "How long have you been waiting here, and who told you to come?",
    "The package arrived yesterday, but the label was damaged and hard to read.",
    "Silence settled over the room as the final message appeared on the screen.",
    "Whether you agree or not, the facts remain exactly the same.",
    "They built the tower stone by stone, year after year, long before anyone remembered why.",
    "Honestly, I would rather walk home than sit in traffic for another hour.",
    "Keep your voice low, keep your eyes open, and stay close to the wall.",
    "The garden was full of tall yellow flowers, buzzing bees, and warm green shade.",
    "Several attempts were made to repair the signal, but each one failed.",
    "Is it true that the oldest clocks in the world are still keeping perfect time?",
    "This is the part where most people give up, and this is where we continue.",
    "Wonderful news travels fast, but trouble always seems to travel faster.",
    "A calm voice can say more than a loud one ever will.",
    "Before you decide, listen carefully to everything that has been said tonight.",
    "The old machine hummed, clicked, and finally began to move.",
    "Please speak clearly, take your time, and begin again from the start.",
    "Rain tapped against the window while the lamp flickered and the night grew long.",
    "Which path do you choose, and are you willing to accept what follows?",
    "Today we learned that patience is often the quickest way forward.",
    "The result was neither a failure nor a success, but something far more interesting.",
    "Ask yourself what you truly want, then ask what you are willing to give up.",
    "Behind every great discovery is a long list of questions that nobody could answer.",
    "Wait, did you hear that? Something is moving in the next room.",
    "Everything has been prepared, and the only thing left is to begin.",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def ask(prompt, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip().strip('"')
    except EOFError:
        sys.exit(0)
    return val or (default or "")


def yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({d})").lower()
    if not val:
        return default
    return val.startswith("y")


def pause():
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


def slug(text):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-")
    return s or "my-voice"


def header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        print(f"(Open this yourself: {path})")


# ---------------------------------------------------------------------------
# Project (one folder per voice)
# ---------------------------------------------------------------------------
class Project:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(TRAIN_DIR, name)
        self.wavs = os.path.join(self.dir, "wavs")
        self.meta = os.path.join(self.dir, "metadata.csv")
        self.base = os.path.join(self.dir, "base")
        self.out = os.path.join(self.dir, "output")
        self.cfg_path = os.path.join(self.dir, "project.json")
        self.cfg = {"espeak": "en-us", "lang": "en", "batch": 16}
        for d in (self.wavs, self.base, self.out):
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path, encoding="utf-8") as f:
                self.cfg.update(json.load(f))

    def save(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2)

    def read_meta(self):
        rows = []
        if os.path.exists(self.meta):
            with open(self.meta, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "|" in line:
                        fn, tx = line.split("|", 1)
                        rows.append((fn.strip(), tx.strip()))
        return rows

    def append_meta(self, fn, text):
        text = re.sub(r"\s+", " ", text).replace("|", " ").strip()
        with open(self.meta, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"{fn}|{text}\n")

    def next_index(self):
        nums = [int(m.group(1)) for f in os.listdir(self.wavs)
                if (m := re.match(r"clip_(\d+)\.wav$", f))]
        return (max(nums) + 1) if nums else 1


def list_projects():
    if not os.path.isdir(TRAIN_DIR):
        return []
    return sorted(d for d in os.listdir(TRAIN_DIR)
                  if os.path.isdir(os.path.join(TRAIN_DIR, d)))


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def save_wav(path, int16):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(np.asarray(int16, dtype=np.int16).tobytes())


def load_audio(path):
    """Any audio/video file -> mono float32 array at 22050 Hz (-1..1)."""
    try:
        import av
        container = av.open(path)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
        chunks = []
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
        try:
            for f in resampler.resample(None):
                chunks.append(f.to_ndarray().reshape(-1))
        except Exception:
            pass
        container.close()
        if not chunks:
            raise RuntimeError("no audio found")
        return np.concatenate(chunks).astype(np.float32) / 32768.0
    except ImportError:
        pass
    # Fallback: plain 16-bit WAV only
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise RuntimeError("Without PyAV only 16-bit WAV files can be read "
                               "(pip install faster-whisper installs PyAV).")
        ch, rate = wf.getnchannels(), wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    data = data.reshape(-1, ch).mean(axis=1).astype(np.float32) / 32768.0
    if rate != SR:
        n = int(len(data) * SR / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return data.astype(np.float32)


def to_int16(x):
    return np.clip(x * 32767.0, -32768, 32767).astype(np.int16)


def trim_silence(x, thresh=0.01, pad=0.12):
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) == 0:
        return x
    a = max(0, idx[0] - int(pad * SR))
    b = min(len(x), idx[-1] + int(pad * SR))
    return x[a:b]


# ---------------------------------------------------------------------------
# Step 1a: import existing audio (auto-split + auto-transcribe)
# ---------------------------------------------------------------------------
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
             ".mp4", ".mkv", ".webm", ".mov")


def collect_files(path):
    if os.path.isdir(path):
        out = []
        for r, _, fs in os.walk(path):
            out += [os.path.join(r, f) for f in sorted(fs) if f.lower().endswith(AUDIO_EXT)]
        return out
    return [path] if os.path.isfile(path) else []


def merge_segments(segs):
    """Join tiny neighbouring segments so clips land between ~2 and ~12 seconds."""
    merged = []
    for st, en, tx in segs:
        if (merged and (en - merged[-1][0]) <= 12.0
                and (st - merged[-1][1]) < 0.4
                and (merged[-1][1] - merged[-1][0]) < 3.0):
            a, _, t = merged[-1]
            merged[-1] = (a, en, t + " " + tx)
        else:
            merged.append((st, en, tx))
    return merged


def import_audio(p):
    header("Add recordings you already have")
    print("Best results: ONE clean voice, no music, no background noise, no echo.")
    print("Drag a file or a folder of audio into this window, then press Enter.\n")
    src = ask("Path to audio file or folder")
    files = collect_files(src)
    if not files:
        print("Couldn't find any audio there.")
        return

    try:
        from faster_whisper import WhisperModel
    except Exception:
        print("This step needs faster-whisper (Rasputin already uses it).")
        print("Install it with:   pip install faster-whisper")
        return

    en = p.cfg["lang"] == "en"
    model_name = "small.en" if en else "small"
    print(f"\nLoading the listening model '{model_name}' (first time = one-off download)...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    added, seconds = 0, 0.0
    for fp in files:
        print(f"\nReading {os.path.basename(fp)} ...")
        try:
            audio = load_audio(fp)
        except Exception as e:
            print(f"  skipped ({e})")
            continue
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio / peak * 0.9                         # level the whole file once

        segs, _ = model.transcribe(fp, language=p.cfg["lang"], vad_filter=True,
                                   beam_size=5, condition_on_previous_text=False)
        raw = [(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]
        n_before = added
        for st, en, tx in merge_segments(raw):
            dur = en - st
            if dur < 1.0 or dur > 15.0:
                continue
            a = max(0, int((st - 0.08) * SR))
            b = min(len(audio), int((en + 0.08) * SR))
            clip = audio[a:b]
            name = f"clip_{p.next_index():05d}.wav"
            save_wav(os.path.join(p.wavs, name), to_int16(clip))
            p.append_meta(name, tx)
            added += 1
            seconds += len(clip) / SR
        print(f"  -> {added - n_before} clips")

    print(f"\nDone. Added {added} clips ({seconds / 60:.1f} minutes).")
    if added:
        print("TIP: the computer's spelling of what was said can have mistakes.")
        print("     Use 'Check my recordings' -> 'Fix transcript' to correct any wrong words.")


# ---------------------------------------------------------------------------
# Step 1b: record with the microphone
# ---------------------------------------------------------------------------
def record_mic(p):
    header("Record with the microphone")
    try:
        import sounddevice as sd
    except Exception:
        print("Recording needs one small extra package. Install it with:")
        print("     pip install sounddevice")
        print("then come back. (Or use option 1 and import audio you already have.)")
        return

    prompts_file = os.path.join(p.dir, "prompts.txt")
    if os.path.exists(prompts_file):
        with open(prompts_file, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    else:
        lines = PROMPTS
    done = {tx for _, tx in p.read_meta()}
    todo = [l for l in lines if l not in done]
    if not todo:
        print("You've already recorded every sentence on the list!")
        return

    print("How it works:")
    print("  - Sit in a quiet room, same distance from the mic every time.")
    print("  - Read each sentence in the voice you want Rasputin to have.")
    print("  - Enter = start, Enter again = stop.  Type  r  to redo,  q  to quit.")
    print("  - Each sentence is saved as soon as you finish it, so you can stop any time.\n")
    print(f"{len(todo)} sentences to go.")
    try:
        dev = ask("Microphone number (Enter = default)", "")
        if dev:
            sd.default.device = (int(dev), None)
        elif ask("Show the list of microphones first? (y/n)", "n").lower().startswith("y"):
            print(sd.query_devices())
            dev = ask("Microphone number (Enter = default)", "")
            if dev:
                sd.default.device = (int(dev), None)
    except Exception as e:
        print(f"Mic selection problem: {e}")

    for i, line in enumerate(todo, 1):
        while True:
            print(f"\n[{i}/{len(todo)}]  {line}\n")
            cmd = ask("Enter = record, q = quit").lower()
            if cmd == "q":
                return
            frames = []

            def cb(indata, n, t, status):
                frames.append(indata.copy())

            try:
                stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=cb)
                stream.start()
            except Exception as e:
                print(f"Couldn't open the microphone: {e}")
                return
            input("   ● RECORDING...  press Enter when finished")
            stream.stop()
            stream.close()
            if not frames:
                continue
            clip = trim_silence(np.concatenate(frames).reshape(-1))
            dur = len(clip) / SR
            peak = float(np.max(np.abs(clip))) if len(clip) else 0
            if dur < 1.0 or peak < 0.02:
                print("   Too short or too quiet - try again.")
                continue
            if peak > 0.98:
                print("   That clipped (too loud). Move back from the mic a little and try again.")
                continue
            clip = clip / peak * 0.9
            fb = ask("   Keep it? (Enter = keep, r = redo)").lower()
            if fb == "r":
                continue
            name = f"clip_{p.next_index():05d}.wav"
            save_wav(os.path.join(p.wavs, name), to_int16(clip))
            p.append_meta(name, line)
            break


# ---------------------------------------------------------------------------
# Step 2: check the dataset
# ---------------------------------------------------------------------------
def analyse(p):
    rows = p.read_meta()
    total, issues, good = 0.0, [], 0
    for fn, tx in rows:
        path = os.path.join(p.wavs, fn)
        if not os.path.exists(path):
            issues.append(f"{fn}: file is missing")
            continue
        try:
            with wave.open(path, "rb") as wf:
                rate, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
                n = wf.getnframes()
                raw = wf.readframes(n)
        except Exception as e:
            issues.append(f"{fn}: can't read ({e})")
            continue
        dur = n / float(rate or 1)
        total += dur
        bad = False
        if rate != SR or ch != 1 or sw != 2:
            issues.append(f"{fn}: wrong format ({rate} Hz, {ch} ch) - needs {SR} Hz mono 16-bit")
            bad = True
        else:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if len(x) and np.max(np.abs(x)) > 0.99:
                issues.append(f"{fn}: audio is clipped (too loud)")
                bad = True
            if len(x) and np.sqrt(np.mean(x ** 2)) < 0.01:
                issues.append(f"{fn}: very quiet")
                bad = True
        if dur < 0.8 or dur > 16:
            issues.append(f"{fn}: length {dur:.1f}s (best is 1-15 s)")
            bad = True
        if re.search(r"\d", tx):
            issues.append(f"{fn}: has digits - write numbers as words (\"twenty\" not \"20\")")
            bad = True
        if len(tx) < 3:
            issues.append(f"{fn}: text is empty/too short")
            bad = True
        if not bad:
            good += 1
    return len(rows), total, good, issues


def verdict(minutes):
    if minutes < 5:
        return "Too little. Aim for at least 15 minutes."
    if minutes < 15:
        return "Enough for a quick test. The voice will be rough; more is better."
    if minutes < 45:
        return "Good amount for fine-tuning."
    return "Great amount."


def check_dataset(p):
    header("Check my recordings")
    n, total, good, issues = analyse(p)
    if n == 0:
        print("No recordings yet. Do step 1 first.")
        return
    mins = total / 60
    print(f"Clips: {n}   Total: {mins:.1f} minutes   Clean: {good}")
    print(verdict(mins))
    if issues:
        print(f"\n{len(issues)} things to look at (first 15):")
        for s in issues[:15]:
            print("  -", s)
        print("\nFix the text in metadata.csv, or delete a bad clip's line (and its wav).")
    else:
        print("\nNo problems found.")
    print("\n  1) Fix transcript (open metadata.csv - each line is  file.wav|what was said)")
    print("  2) Open the clips folder (to listen / delete bad ones)")
    print("  3) Back")
    c = ask("Choose", "3")
    if c == "1":
        open_path(p.meta)
    elif c == "2":
        open_path(p.wavs)


# ---------------------------------------------------------------------------
# Step 3: write the training scripts
# ---------------------------------------------------------------------------
TRAIN_SH = r'''#!/usr/bin/env bash
# Auto-generated by voice_trainer.py. Safe to re-run: it picks up where it left off.
#   bash train.sh           -> train (Ctrl+C when you're happy; checkpoints are saved)
#   bash train.sh export    -> turn the newest checkpoint into the .onnx voice
set -e
cd "$(dirname "$0")"
WORK="$(pwd)"
NAME="@NAME@"
ESPEAK="@ESPEAK@"
BATCH=@BATCH@        # lower this (8 or 4) if you get an "out of memory" error
SAMPLE_RATE=@SR@

BASEDIR="$HOME/piper-train"
CODE="$BASEDIR/piper1-gpl"
RUN="$BASEDIR/runs/$NAME"
mkdir -p "$RUN" "$WORK/output"

# ---- one-time setup -------------------------------------------------------
missing=""
for c in git cmake gcc python3 ninja; do command -v "$c" >/dev/null 2>&1 || missing=1; done
dpkg -s python3-dev >/dev/null 2>&1 || missing=1
dpkg -s python3-venv >/dev/null 2>&1 || missing=1
if [ -n "$missing" ]; then
  echo ">> Installing build tools (it may ask for your Linux password)..."
  sudo apt-get update
  sudo apt-get install -y build-essential cmake ninja-build python3-dev python3-venv git espeak-ng
fi

if [ ! -d "$CODE" ]; then
  echo ">> Downloading Piper training code..."
  git clone https://github.com/OHF-Voice/piper1-gpl.git "$CODE"
fi
cd "$CODE"
if [ ! -f .venv/.ready ]; then
  echo ">> Setting up the training environment (one time, takes a while)..."
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -U pip
  pip install -e '.[train]'
  ./build_monotonic_align.sh
  python3 setup.py build_ext --inplace
  touch .venv/.ready
else
  . .venv/bin/activate
fi
cd "$WORK"

newest_ckpt() { ls -t "$RUN"/lightning_logs/*/checkpoints/*.ckpt 2>/dev/null | head -1; }

# ---- export ---------------------------------------------------------------
if [ "$1" = "export" ]; then
  CKPT="$(newest_ckpt)"
  if [ -z "$CKPT" ]; then echo "No training checkpoint found yet. Train first."; exit 1; fi
  echo ">> Exporting $CKPT"
  # Newer PyTorch defaults to the torch.export (dynamo) ONNX exporter, which cannot trace
  # Piper's VITS spline code. Force the classic exporter instead.
  python3 - "$CKPT" "$WORK/output/$NAME.onnx" <<'PYEOF'
import sys, runpy, torch
ckpt, out = sys.argv[1], sys.argv[2]
_orig = torch.onnx.export
def _classic(*a, **k):
    try:
        return _orig(*a, **{**k, "dynamo": False})
    except TypeError:          # older torch has no 'dynamo' argument
        return _orig(*a, **k)
torch.onnx.export = _classic
sys.argv = ["export_onnx", "--checkpoint", ckpt, "--output-file", out]
runpy.run_module("piper.train.export_onnx", run_name="__main__")
PYEOF
  cp "$RUN/config.json" "$WORK/output/$NAME.onnx.json"
  echo ">> Done: $WORK/output/$NAME.onnx"
  exit 0
fi

# ---- train ----------------------------------------------------------------
BASE="$(ls "$WORK"/base/*.ckpt 2>/dev/null | head -1)"
if [ -z "$BASE" ]; then
  echo "No starting voice found. Put a downloaded .ckpt file in: $WORK/base"
  exit 1
fi
LATEST="$(newest_ckpt)"
if [ -n "$LATEST" ]; then
  echo ">> Continuing your training from: $LATEST"
  RESUME=(--ckpt_path "$LATEST")
else
  echo ">> First run - starting from base voice: $BASE"
  RESUME=(--model.warmstart_ckpt "$BASE")
fi
echo ">> Press Ctrl+C when you want to stop. Run again later to continue."

python3 -m piper.train fit \
  --data.voice_name "$NAME" \
  --data.csv_path "$WORK/metadata.csv" \
  --data.audio_dir "$WORK/wavs" \
  --model.sample_rate $SAMPLE_RATE \
  --data.espeak_voice "$ESPEAK" \
  --data.cache_dir "$RUN/cache" \
  --data.config_path "$RUN/config.json" \
  --data.batch_size $BATCH \
  --trainer.default_root_dir "$RUN" \
  "${RESUME[@]}"
'''


def write_scripts(p):
    sh = (TRAIN_SH.replace("@NAME@", p.name)
          .replace("@ESPEAK@", p.cfg["espeak"])
          .replace("@BATCH@", str(p.cfg["batch"]))
          .replace("@SR@", str(SR)))
    with open(os.path.join(p.dir, "train.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(sh)


def have_base(p):
    return any(f.endswith(".ckpt") for f in os.listdir(p.base))


def setup_training(p):
    header("Get ready to train")
    n, total, good, _ = analyse(p)
    if n == 0:
        print("Add recordings first (step 1).")
        return
    print(f"You have {n} clips / {total / 60:.1f} minutes. {verdict(total / 60)}\n")

    print("Training starts from an existing voice (this is what makes ~20 minutes")
    print("of audio enough). You need to download ONE starting file:\n")
    print(f"  1. Open:  {CKPT_PAGE}")
    print(f"  2. Go to your language, e.g.  en -> en_GB (or en_US) -> pick a voice ending in 'medium'")
    print("  3. Download the  .ckpt  file in there (it's large, ~800 MB)")
    print(f"  4. Put it in this folder:\n       {p.base}\n")
    print("(It must be a 'medium' quality voice because that's what this tool trains.)")

    if ask("Open the download page and the folder now? (y/n)", "y").lower().startswith("y"):
        open_path(CKPT_PAGE)
        open_path(p.base)

    b = ask("GPU memory?  1 = 6 GB or less, 2 = 8-12 GB, 3 = 16 GB+", "2")
    p.cfg["batch"] = {"1": 8, "2": 16, "3": 32}.get(b, 16)
    p.save()
    write_scripts(p)
    print("\nTraining scripts written.")
    print("Waiting on:", "nothing - ready!" if have_base(p) else
          "the .ckpt file in the 'base' folder above.")
    print("When it's there, choose  'Train'  from the main menu.")


# ---------------------------------------------------------------------------
# Step 4: train / export
# ---------------------------------------------------------------------------
def run_in_linux(p, *args):
    """Run train.sh in WSL (Windows) or directly (Linux/mac)."""
    if IS_WIN:
        if not shutil.which("wsl"):
            print("Training needs WSL2 (Linux inside Windows). One-time setup:")
            print("  1. Open PowerShell as Administrator and run:   wsl --install")
            print("  2. Restart your PC, open 'Ubuntu' once to create a username/password.")
            print("  3. Make sure your NVIDIA driver is up to date (that's all the GPU needs).")
            print("  4. Run this step again.")
            return False
        cmd = ["wsl", "bash", "./train.sh", *args]
    else:
        cmd = ["bash", "./train.sh", *args]
    try:
        return subprocess.call(cmd, cwd=p.dir) == 0
    except KeyboardInterrupt:
        print("\nStopped. Your progress is saved.")
        return True


def train(p):
    header("Train")
    if not os.path.exists(os.path.join(p.dir, "train.sh")):
        print("Do 'Get ready to train' first.")
        return
    if not have_base(p):
        print(f"Missing the starting .ckpt file. Put it in:\n  {p.base}")
        return
    n, total, _, _ = analyse(p)
    if total < 120:
        print("You have under 2 minutes of audio. Add more first.")
        return
    write_scripts(p)       # refresh in case settings changed
    print("Training will open Linux output below. The first run also installs things")
    print("(10-30 minutes, only once).\n")
    print("WHEN TO STOP: let it run about 1-3 hours, then press Ctrl+C.")
    print("Then pick 'Finish' in the main menu to turn it into a voice and hear it.")
    print("Not good enough? Choose Train again - it continues from where it stopped.\n")
    ask("Press Enter to start")
    run_in_linux(p)


# ---------------------------------------------------------------------------
# Step 5: export + install into Rasputin
# ---------------------------------------------------------------------------
def test_voice(onnx, out_wav, text):
    try:
        from piper import PiperVoice
    except Exception:
        print("(piper-tts isn't importable here, so skipping the sound test.)")
        return False
    try:
        v = PiperVoice.load(onnx)
        with wave.open(out_wav, "wb") as wf:
            if hasattr(v, "synthesize_wav"):
                v.synthesize_wav(text, wf)
            else:
                v.synthesize(text, wf)
        return True
    except Exception as e:
        print(f"Test failed: {e}")
        return False


def finish(p):
    header("Finish: make the voice and install it into Rasputin")
    onnx = os.path.join(p.out, p.name + ".onnx")
    cfg = onnx + ".json"

    if os.path.exists(os.path.join(p.dir, "train.sh")):
        write_scripts(p)       # refresh so the latest export fix is used
        if yes("Export the newest training checkpoint now?", True):
            if not run_in_linux(p, "export"):
                print("Export didn't finish - see messages above.")
    if not (os.path.exists(onnx) and os.path.exists(cfg)):
        print("\nNo exported voice found in the output folder.")
        alt = ask("If you trained elsewhere (e.g. Colab), paste the path to your .onnx file (or Enter to cancel)")
        if not alt or not os.path.exists(alt) or not os.path.exists(alt + ".json"):
            print("Need both  voice.onnx  and  voice.onnx.json  side by side. Nothing installed.")
            return
        onnx, cfg = alt, alt + ".json"

    # Hear it first
    wav = os.path.join(p.dir, "test.wav")
    sample = "Systems are online. This is how my new voice sounds."
    if test_voice(onnx, wav, sample):
        print("Playing a test sentence...")
        open_path(wav)
        if not yes("Install this voice into Rasputin?", True):
            return

    os.makedirs(PIPER_DIR, exist_ok=True)
    name = slug(ask("Name for the voice in Rasputin", p.name))
    dst = os.path.join(PIPER_DIR, name + ".onnx")
    if os.path.exists(dst) and not yes(f"'{name}' already exists in models/piper. Replace it?", False):
        return
    shutil.copy2(onnx, dst)
    shutil.copy2(cfg, dst + ".json")
    print(f"\n✔ Installed: {dst}")
    print("Restart Rasputin, then pick the voice in the voice OPTIONS menu.")
    print(f"(To make it the automatic default, set PIPER_DEFAULT = \"{name}\" near the top of app.py.)")


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
def status_line(p):
    n, total, good, issues = analyse(p)
    mins = total / 60
    base = "yes" if have_base(p) else "no"
    scripts = os.path.exists(os.path.join(p.dir, "train.sh"))
    exported = os.path.exists(os.path.join(p.out, p.name + ".onnx"))
    installed = os.path.exists(os.path.join(PIPER_DIR, p.name + ".onnx"))
    print(f"\nVoice: {p.name}   |   {n} clips, {mins:.1f} min   |   starting voice: {base}")
    if n == 0:
        nxt = "1  (add recordings)"
    elif mins < 5:
        nxt = "1  (add more recordings - aim for 15+ minutes)"
    elif not scripts or not have_base(p):
        nxt = "3  (get ready to train)"
    elif not exported and not installed:
        nxt = "4  (train)"
    elif not installed:
        nxt = "5  (finish & install)"
    else:
        nxt = "none - it's installed! (add more audio and retrain to improve it)"
    print(f"Next step: {nxt}")


def add_audio_menu(p):
    header("Step 1 - Add recordings")
    print("  1) I have audio already (interviews, voice lines, a recording)  <- easiest")
    print("  2) Record myself with the microphone")
    print("  3) Back")
    c = ask("Choose", "1")
    if c == "1":
        import_audio(p)
    elif c == "2":
        record_mic(p)


def new_project():
    header("New voice")
    name = slug(ask("Give your voice a name (letters/numbers, e.g. rasputin-deep)"))
    p = Project(name)
    print("\nWhat language will it speak?")
    for k, (label, _) in LANGS.items():
        print(f"  {k}) {label}")
    print("  3) Other (you'll type the code)")
    c = ask("Choose", "1")
    if c in LANGS:
        p.cfg["espeak"] = LANGS[c][1]
    else:
        p.cfg["espeak"] = ask("espeak language code (e.g. de, fr, es)", "en-us")
    p.cfg["lang"] = p.cfg["espeak"].split("-")[0].lower()
    p.save()
    print(f"\nCreated: {p.dir}")
    return p


def choose_project():
    header("Rasputin Voice Trainer")
    print("Train a new Piper voice and add it to Rasputin.")
    print("Only train voices you own or have permission to use.\n")
    projs = list_projects()
    for i, n in enumerate(projs, 1):
        print(f"  {i}) {n}")
    print(f"  N) New voice")
    print(f"  Q) Quit")
    c = ask("Choose", "N" if not projs else "1").lower()
    if c == "q":
        return None
    if c.isdigit() and 1 <= int(c) <= len(projs):
        return Project(projs[int(c) - 1])
    return new_project()


def main():
    p = choose_project()
    if not p:
        return
    while True:
        header(f"Voice: {p.name}")
        status_line(p)
        print("\n  1) Add recordings")
        print("  2) Check my recordings")
        print("  3) Get ready to train")
        print("  4) Train")
        print("  5) Finish: hear it & install into Rasputin")
        print("  6) Help")
        print("  7) Switch voice / quit")
        c = ask("Choose")
        try:
            if c == "1":
                add_audio_menu(p)
            elif c == "2":
                check_dataset(p)
            elif c == "3":
                setup_training(p)
            elif c == "4":
                train(p)
            elif c == "5":
                finish(p)
            elif c == "6":
                header("Help")
                print("Good recordings = good voice:")
                print(" - One speaker, clear, same mic, no music/echo/noise.")
                print(" - 15-30+ minutes. Natural, expressive speech beats flat reading.")
                print(" - The transcript must match what's said (check step 2).")
                print(f"\nTraining uses Piper's official trainer:\n  {DOCS_PAGE}")
                print(f"\nYour files: {p.dir}")
            elif c == "7":
                p = choose_project()
                if not p:
                    return
                continue
        except KeyboardInterrupt:
            print("\n(cancelled)")
        pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")

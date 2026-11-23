"""
Rasputin Voice Data Maker
=========================
Makes lots of training audio for a Piper voice when you don't have enough real
recordings, by using an RVC voice model (.pth + .index), e.g. an Ultron model.

  1. Takes sentences (from any .txt book/file you give it, or a small built-in list)
  2. Piper reads them aloud with a normal voice you already have
  3. RVC re-voices every clip into the target voice (your .pth model)
  4. The clips are cleaned and saved into your voice project, WITH the exact text,
     ready for  voice_trainer.py  (steps 2-5). Optional mp3 copies for listening.

Put this file next to voice_trainer.py (the "Python" folder).
RVC runs in its OWN separate Python environment so it can't break Rasputin.
"""
import os
import re
import sys
import json
import wave
import random
import shutil
import subprocess
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_trainer as vt          # noqa: E402  (shares projects, audio helpers)

ask, yes, header, slug = vt.ask, vt.yes, vt.header, vt.slug
SR = vt.SR
ENV_DIR = os.path.join(vt.TRAIN_DIR, "_rvc_env")
WORDS_PER_SEC = 2.6          # rough speaking speed, used to estimate minutes

# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------
def clean_sentence(s):
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u2014", ", ").replace("\u2013", ", ").replace("--", ", ")
    s = re.sub(r"\s+", " ", s).strip().strip('"').strip()
    return s


def good_sentence(s):
    if not s or not s[0].isupper() or s[-1] not in ".?!":
        return False
    if re.search(r"[0-9_\[\]{}<>@#*/\\=+~^|]", s) or '"' in s:
        return False
    n = len(s.split())
    if n < 6 or n > 25 or len(s) > 220:
        return False
    letters = sum(c.isalpha() for c in s)
    if letters / len(s) < 0.75 or s.isupper():
        return False
    return True


def read_text_source(path):
    files = []
    if os.path.isdir(path):
        for r, _, fs in os.walk(path):
            files += [os.path.join(r, f) for f in sorted(fs) if f.lower().endswith(".txt")]
    elif os.path.isfile(path):
        files = [path]
    raw = ""
    for f in files:
        try:
            t = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        a = re.search(r"\*\*\* ?START OF.*?\*\*\*", t)      # Project Gutenberg headers
        b = re.search(r"\*\*\* ?END OF", t)
        if a:
            t = t[a.end():]
        if b:
            t = t[:b.start()] if not a else t[:max(0, b.start() - (a.end() if a else 0))]
        raw += "\n" + t
    return raw


def build_sentences(raw, minutes):
    sents, seen = [], set()
    for para in re.split(r"\n\s*\n", raw):
        para = re.sub(r"\s+", " ", para)
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"\u201c])", para):
            s = clean_sentence(s)
            if good_sentence(s) and s not in seen:
                seen.add(s)
                sents.append(s)
    random.Random(1).shuffle(sents)
    need_words = int(minutes * 60 * WORDS_PER_SEC * 1.15)       # +15% for rejects
    out, words = [], 0
    for s in sents:
        out.append(s)
        words += len(s.split())
        if words >= need_words:
            break
    return out


# ---------------------------------------------------------------------------
# Stage 2: Piper reads the sentences (normal voice)
# ---------------------------------------------------------------------------
def pick_source_voice():
    voices = []
    if os.path.isdir(vt.PIPER_DIR):
        voices = [f[:-5] for f in sorted(os.listdir(vt.PIPER_DIR))
                  if f.endswith(".onnx") and os.path.exists(os.path.join(vt.PIPER_DIR, f + ".json"))]
    if not voices:
        print("No Piper voice found in models/piper to read the sentences with.")
        return None
    print("\nWhich Piper voice should read the sentences first?")
    print("(RVC keeps this voice's rhythm and emphasis. A calm, steady, male voice is best.)")
    for i, v in enumerate(voices, 1):
        print(f"  {i}) {v}")
    default = "1"
    if "en_GB-alan-medium" in voices:
        default = str(voices.index("en_GB-alan-medium") + 1)
    c = ask("Choose", default)
    return voices[int(c) - 1] if c.isdigit() and 1 <= int(c) <= len(voices) else voices[0]


def synth_all(sents, src_dir, voice_name, length_scale):
    try:
        from piper import PiperVoice
    except Exception:
        print("piper-tts isn't installed in this Python (pip install piper-tts).")
        return False
    try:
        from piper import SynthesisConfig
    except Exception:
        SynthesisConfig = None
    os.makedirs(src_dir, exist_ok=True)
    v = PiperVoice.load(os.path.join(vt.PIPER_DIR, voice_name + ".onnx"))
    total, done = len(sents), 0
    for name, text in sents.items():
        out = os.path.join(src_dir, name)
        done += 1
        if os.path.exists(out):
            continue
        try:
            with wave.open(out, "wb") as wf:
                if hasattr(v, "synthesize_wav"):
                    cfg = SynthesisConfig(length_scale=length_scale) if SynthesisConfig else None
                    v.synthesize_wav(text, wf, syn_config=cfg)
                else:
                    v.synthesize(text, wf, length_scale=length_scale)
        except Exception as e:
            print(f"  skipped one sentence ({e})")
            if os.path.exists(out):
                os.remove(out)
        if done % 25 == 0 or done == total:
            print(f"  read aloud: {done}/{total}")
    return True


# ---------------------------------------------------------------------------
# Stage 3: RVC in its own environment
# ---------------------------------------------------------------------------
WORKER = r'''
import sys, os, json, traceback
a = json.load(open(sys.argv[1], encoding="utf-8"))
from rvc_python.infer import RVCInference

def make(version):
    r = RVCInference(device=a["device"])
    r.set_params(f0method=a["f0method"], f0up_key=a["pitch"], index_rate=a["index_rate"], protect=0.33)
    try:
        r.load_model(a["model"], version=version, index_path=a["index"])
    except TypeError:
        r.load_model(a["model"])
    return r

files = sorted(f for f in os.listdir(a["src"]) if f.lower().endswith(".wav"))
os.makedirs(a["out"], exist_ok=True)
todo = [f for f in files if not os.path.exists(os.path.join(a["out"], f))]
print(f"RVC: {len(todo)} to convert ({len(files) - len(todo)} already done)", flush=True)
if not todo:
    sys.exit(0)

rvc = make("v2")
version = "v2"
failed = 0
for i, f in enumerate(todo, 1):
    s, o = os.path.join(a["src"], f), os.path.join(a["out"], f)
    try:
        rvc.infer_file(s, o)
    except Exception:
        if i == 1 and version == "v2":          # model may be v1: retry once
            try:
                rvc = make("v1"); version = "v1"; rvc.infer_file(s, o)
            except Exception:
                traceback.print_exc(); print("RVC could not run this model."); sys.exit(2)
        else:
            failed += 1
            if os.path.exists(o):
                os.remove(o)
    if i % 10 == 0 or i == len(todo):
        print(f"  converted: {i}/{len(todo)}", flush=True)
print(f"RVC finished. failed: {failed}", flush=True)
'''


def env_python():
    return os.path.join(ENV_DIR, "Scripts" if vt.IS_WIN else "bin", "python.exe" if vt.IS_WIN else "python")


def is_py310(exe):
    try:
        r = subprocess.run([exe, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.split() == ["3", "10"]
    except Exception:
        return False


def find_py310():
    if vt.IS_WIN and shutil.which("py"):
        try:
            r = subprocess.run(["py", "-3.10", "-c", "import sys;print(sys.executable)"],
                               capture_output=True, text=True)
            exe = r.stdout.strip()
            if r.returncode == 0 and exe and is_py310(exe):
                return exe
        except Exception:
            pass
    for n in ("python3.10", "python3.10.exe"):
        w = shutil.which(n)
        if w and is_py310(w):
            return w
    return None


def ensure_env(gpu):
    py = env_python()
    if os.path.exists(os.path.join(ENV_DIR, ".ready")):
        return py
    print("\nSetting up RVC in its own environment (one time, 5-15 minutes, ~3 GB)...")
    os.makedirs(vt.TRAIN_DIR, exist_ok=True)

    # rvc-python only installs on Python 3.10 (newer versions like 3.12-3.14 fail).
    py310 = find_py310()
    if not py310:
        print("\nRVC needs Python 3.10, and it wasn't found on this PC.")
        print("Install it (it will NOT affect Rasputin or your other Python):")
        print("  1. Go to python.org -> Downloads -> Windows -> Python 3.10.11 (64-bit installer)")
        print("  2. In the installer keep 'py launcher' ticked, then Install")
        print("  3. Run this tool again.")
        alt = ask("Or paste the path to a Python 3.10 python.exe now (Enter to cancel)", "")
        if not alt or not os.path.isfile(alt) or not is_py310(alt):
            return None
        py310 = alt

    shutil.rmtree(ENV_DIR, ignore_errors=True)         # remove any half-built/wrong-version env
    if subprocess.call([py310, "-m", "venv", ENV_DIR]) != 0:
        print("Couldn't create the environment.")
        return None

    # Use our own pip cache (your normal one can be locked by permissions, which makes pip
    # appear to freeze) and skip pip's "is there a newer pip" check.
    tmp_dir = os.path.join(vt.TRAIN_DIR, "_tmp")      # your Windows Temp folder refused the compiler
    os.makedirs(tmp_dir, exist_ok=True)
    pip_env = dict(os.environ,
                   PIP_CACHE_DIR=os.path.join(vt.TRAIN_DIR, "_pip_cache"),
                   PIP_DISABLE_PIP_VERSION_CHECK="1",
                   TMP=tmp_dir, TEMP=tmp_dir, TMPDIR=tmp_dir)

    def pip(*args):
        return subprocess.call([py, "-m", "pip", *args], env=pip_env) == 0

    pip("install", "pip==24.0")      # rvc-python needs pip older than 24.1 (newer pip rejects omegaconf 2.0.6)
    if gpu:
        print("Installing GPU PyTorch...")
        if not pip("install", "torch==2.1.1", "torchaudio==2.1.1",
                   "--index-url", "https://download.pytorch.org/whl/cu118"):
            print("GPU PyTorch didn't install (needs Python 3.8-3.11). Continuing with CPU.")
    print("Installing rvc-python...")
    if not pip("install", "rvc-python"):
        print("\nrvc-python failed to install. Scroll up and send me the last ~20 lines of the error.")
        print("(To retry from scratch, delete this folder and run the tool again:)")
        print(f"  {ENV_DIR}")
        return None
    with open(os.path.join(ENV_DIR, ".ready"), "w") as f:
        f.write("ok")
    return py


def run_rvc(p, work, gpu, pitch):
    py = ensure_env(gpu)
    if not py:
        return False
    worker = os.path.join(work, "rvc_worker.py")
    with open(worker, "w", encoding="utf-8") as f:
        f.write(WORKER)
    args = {
        "model": p.cfg["rvc_model"], "index": p.cfg.get("rvc_index", ""),
        "src": os.path.join(work, "src"), "out": os.path.join(work, "converted"),
        "device": "cuda:0" if gpu else "cpu:0", "f0method": "rmvpe",
        "pitch": pitch, "index_rate": 0.5,
    }
    cfgp = os.path.join(work, "rvc_args.json")
    with open(cfgp, "w", encoding="utf-8") as f:
        json.dump(args, f)
    print("\nConverting to the target voice (you can stop with Ctrl+C and resume later)...")
    try:
        return subprocess.call([py, worker, cfgp]) == 0
    except KeyboardInterrupt:
        print("\nStopped. Run again to continue.")
        return False


# ---------------------------------------------------------------------------
# Stage 4: clean + add to the voice project (+ optional mp3)
# ---------------------------------------------------------------------------
def write_mp3(wav_path, mp3_path):
    if shutil.which("ffmpeg"):
        if subprocess.call(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
                            "-q:a", "2", mp3_path]) == 0:
            return True
    try:
        import av
        src = av.open(wav_path)
        dst = av.open(mp3_path, "w")
        ins = src.streams.audio[0]
        outs = dst.add_stream("mp3", rate=ins.rate)
        for frame in src.decode(ins):
            for pk in outs.encode(frame):
                dst.mux(pk)
        for pk in outs.encode(None):
            dst.mux(pk)
        dst.close()
        src.close()
        return True
    except Exception:
        return False


def finalize(p, work, sents, want_mp3):
    existing = {t for _, t in p.read_meta()}
    mp3_dir = os.path.join(p.dir, "mp3")
    if want_mp3:
        os.makedirs(mp3_dir, exist_ok=True)
    kept = rejected = 0
    seconds = 0.0
    for name, text in sents.items():
        conv = os.path.join(work, "converted", name)
        src = os.path.join(work, "src", name)
        if not os.path.exists(conv) or text in existing:
            continue
        try:
            x = vt.trim_silence(vt.load_audio(conv))
            s = vt.trim_silence(vt.load_audio(src))
        except Exception:
            rejected += 1
            continue
        dur, sdur = len(x) / SR, max(len(s) / SR, 0.01)
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        # reject: too short/long, silent, or RVC stretched/garbled it versus the source
        if dur < 1.0 or dur > 15.0 or peak < 0.02 or not (0.7 <= dur / sdur <= 1.4):
            rejected += 1
            continue
        x = x / peak * 0.9
        clip = f"clip_{p.next_index():05d}.wav"
        path = os.path.join(p.wavs, clip)
        vt.save_wav(path, vt.to_int16(x))
        p.append_meta(clip, text)
        kept += 1
        seconds += dur
        if want_mp3 and not write_mp3(path, os.path.join(mp3_dir, clip[:-4] + ".mp3")):
            print("  (couldn't make mp3 copies - install ffmpeg or skip them; wavs are still saved)")
            want_mp3 = False
    print(f"\nAdded {kept} clips ({seconds / 60:.1f} minutes) to your voice project. Rejected {rejected}.")
    if want_mp3 and kept:
        print(f"mp3 copies for listening: {mp3_dir}")
    return kept


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
def main():
    p = vt.choose_project()
    if not p:
        return
    work = os.path.join(p.dir, "rvc_work")
    os.makedirs(work, exist_ok=True)
    sent_file = os.path.join(work, "sentences.json")

    header("Make training audio with an RVC voice")
    print("This builds a big dataset by: Piper reads sentences -> RVC changes the voice ->")
    print("clips + exact text are saved into your voice project.\n")

    # --- model files
    model = ask("Path to your RVC .pth file", p.cfg.get("rvc_model", ""))
    if not os.path.isfile(model):
        print("File not found.")
        return
    index = ask("Path to the matching .index file (Enter to skip)", p.cfg.get("rvc_index", ""))
    if index and not os.path.isfile(index):
        print("Index not found - continuing without it.")
        index = ""
    p.cfg["rvc_model"], p.cfg["rvc_index"] = model, index
    p.save()

    # --- sentences
    sents = {}
    if os.path.exists(sent_file) and yes("Found an earlier run. Continue it?", True):
        sents = json.load(open(sent_file, encoding="utf-8"))
    else:
        print("\nWhere should the sentences come from?")
        print("  - Any .txt file or folder of .txt files (a book works great; free public-domain")
        print("    books are on gutenberg.org - download the 'Plain Text UTF-8' version).")
        print("  - Or press Enter to use the small built-in list (only ~4 minutes).")
        src = ask("Path to .txt file/folder (Enter = built-in)", "")
        minutes = float(ask("How many minutes of audio do you want", "40") or 40)
        if src:
            raw = read_text_source(src)
            lst = build_sentences(raw, minutes)
            if not lst:
                print("Couldn't find usable sentences in that.")
                return
        else:
            lst = list(vt.PROMPTS)
        sents = {f"s_{i:05d}.wav": t for i, t in enumerate(lst, 1)}
        with open(sent_file, "w", encoding="utf-8") as f:
            json.dump(sents, f, ensure_ascii=False, indent=0)
        est = sum(len(t.split()) for t in lst) / WORDS_PER_SEC / 60
        print(f"\n{len(lst)} sentences (about {est:.0f} minutes of speech).")

    # --- piper reads them
    voice = pick_source_voice()
    if not voice:
        return
    slow = ask("Speaking pace: 1.0 = normal, 1.15 = slower and more deliberate", "1.1")
    try:
        ls = float(slow)
    except ValueError:
        ls = 1.1
    print("\nReading the sentences aloud with Piper...")
    if not synth_all(sents, os.path.join(work, "src"), voice, ls):
        return

    # --- rvc
    gpu = yes("\nDo you have an NVIDIA GPU to speed RVC up?", True)
    pitch = int(ask("Pitch shift in semitones (0 = none; try -2 to +2 if it sounds off)", "0") or 0)
    ok = run_rvc(p, work, gpu, pitch)
    if not ok and not os.path.isdir(os.path.join(work, "converted")):
        print("\nRVC didn't produce any audio. Scroll up for the error message and send it to me.")
        return

    # --- finish
    want_mp3 = yes("\nAlso save mp3 copies so you can listen to them?", True)
    print("\nChecking and saving the converted clips...")
    kept = finalize(p, work, sents, want_mp3)
    if kept:
        print("\nNext: open Train Voice.bat -> choose this voice -> step 2 (check), 3 (get ready), 4 (train).")
        print("TIP: listen to a few clips in the 'wavs' folder first. If they sound bad,")
        print("     it's better to fix it now (pitch shift, different Piper voice) than to train on them.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

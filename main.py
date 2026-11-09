"""
Telugu "Did you know?" Reels bot: ONE topic per reel, colourful fact-card style.

  python main.py generate | publish | all

Env / secrets:
  IG_ACCESS_TOKEN, IG_USER_ID, IG_GRAPH_HOST   Instagram API
  PEXELS_API_KEY        free key: real animal/nature footage + photos (strongly recommended)
  ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID   optional, most natural Telugu voice (model eleven_v3)
  TTS_VOICE             te-IN-ShrutiNeural (default, female) or te-IN-MohanNeural
  ANTHROPIC_API_KEY + AI_FACTS=1   optional: let Claude write NEW subjects when the bank is used up
  COOLDOWN_DAYS         days before a subject can repeat (default 30)
  SUBJECT=<id>          force a subject (testing)
  DRY_RUN=1             build but do not post
"""
import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import wave

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from facts_data import SUBJECTS

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0"))
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
W, H, FPS, SR = 1080, 1920, 30, 44100
FACTS_PER_REEL = 4
WIN_W, WIN_H, WIN_X, WIN_Y = 940, 760, 70, 440
NAVY, WHITE, GOLD = (24, 28, 62), (255, 255, 255), (255, 214, 102)

# (dark corner, light corner, glow, solid accent)
THEMES = {
    "green": ((4, 50, 34), (40, 170, 90), (180, 255, 140), (34, 160, 80)),
    "blue": ((6, 30, 80), (30, 130, 230), (120, 210, 255), (30, 120, 220)),
    "yellow": ((70, 40, 0), (245, 180, 20), (255, 235, 120), (225, 150, 0)),
    "orange": ((70, 22, 0), (240, 110, 20), (255, 190, 110), (225, 95, 15)),
    "purple": ((30, 10, 60), (130, 70, 220), (200, 150, 255), (120, 60, 210)),
    "red": ((60, 6, 10), (220, 50, 60), (255, 150, 150), (210, 40, 55)),
    "teal": ((4, 48, 56), (20, 170, 170), (130, 240, 230), (15, 150, 150)),
    "pink": ((60, 10, 40), (235, 90, 160), (255, 170, 215), (225, 70, 145)),
    "gold": ((50, 24, 8), (200, 130, 30), (255, 220, 140), (190, 115, 20)),
    "indigo": ((10, 12, 50), (70, 80, 200), (160, 170, 255), (70, 80, 200)),
    "slate": ((16, 24, 40), (90, 110, 150), (180, 200, 235), (80, 100, 145)),
    "black": ((4, 4, 14), (60, 30, 110), (150, 100, 230), (100, 60, 190)),
}
ANCHOR_TAGS = ["#telugufacts", "#factsintelugu"]
POOL_TAGS = ["#didyouknow", "#telugureels", "#amazingfacts", "#trending", "#knowledge", "#learnontelugu"]
NUM_WORDS = {3: "మూడు", 4: "నాలుగు", 5: "ఐదు"}
OUTRO_TE = "మీకు ఏ నిజం ఎక్కువగా నచ్చింది? కామెంట్‌లో చెప్పండి, ఫాలో అవ్వండి."


# ------------------------------------------------------------------ helpers
def set_output(key, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-3000:])
        raise RuntimeError(f"Command failed: {' '.join(cmd[:3])} ...")


def ease_out(k):
    k = max(0.0, min(1.0, k))
    return 1 - (1 - k) ** 3


def ease_back(k):
    k = max(0.0, min(1.0, k))
    c1 = 1.70158
    return 1 + (c1 + 1) * (k - 1) ** 3 + c1 * (k - 1) ** 2


# -------------------------------------------------------------------- state
def load_state():
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    st.setdefault("used", [])
    return st


def save_state(meta):
    st = load_state()
    st["used"].append({"id": meta["subject_id"], "name": meta["subject_name"],
                       "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "theme": meta["theme"]})
    st["used"] = st["used"][-400:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def recent_ig_names(user_id, token):
    """Subject names already on our Instagram (first caption line), as a backup to the state file."""
    try:
        r = requests.get(f"{GRAPH}/{user_id}/media",
                         params={"fields": "caption", "limit": 25, "access_token": token}, timeout=30)
        r.raise_for_status()
        return [(m.get("caption") or "").split("\n")[0] for m in r.json().get("data", [])]
    except Exception as e:
        print(f"Recent-post check skipped: {e}")
        return []


def ai_subject(used_names):
    """Optional: Claude writes a NEW subject. AI facts can contain mistakes: review your page!"""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or os.getenv("AI_FACTS") != "1":
        return None
    prompt = (
        "Create ONE new 'Did you know?' subject for a Telugu Instagram reel (animals, space, science, "
        "human body, famous Indian places). It must NOT be one of: " + ", ".join(used_names) + ". "
        "Write 5 short, simple, well-established TRUE facts in natural spoken Telugu (max 18 words each). "
        "If you are not sure a fact is true, do not include it. Return ONLY JSON: "
        '{"name":"Telugu name","about":"Telugu genitive form","theme":"one of ' + "|".join(THEMES) + '",'
        '"tag":"#englishhashtag","pexels":["english search 1","english search 2"],"facts":["...x5"]}')
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 1200,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=90)
        r.raise_for_status()
        text = re.sub(r"^```(?:json)?|```$", "", r.json()["content"][0]["text"].strip(), flags=re.M).strip()
        s = json.loads(text)
        assert len(s["facts"]) >= 4 and s["theme"] in THEMES
        s["id"] = "ai-" + hashlib.md5(s["name"].encode()).hexdigest()[:8]
        return s
    except Exception as e:
        print(f"AI subject failed: {e}")
        return None


def choose_subject(state, ig_lines):
    forced = os.getenv("SUBJECT")
    if forced:
        for s in SUBJECTS:
            if s["id"] == forced:
                return s
    now = dt.datetime.now(dt.timezone.utc)
    cooldown = dt.timedelta(days=int(os.getenv("COOLDOWN_DAYS", "30")))
    last = {}
    for u in state["used"]:
        try:
            last[u["id"]] = max(last.get(u["id"], dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
                                dt.datetime.fromisoformat(u["ts"]))
        except Exception:
            pass
    elig = [s for s in SUBJECTS if s["id"] not in last or now - last[s["id"]] > cooldown]
    elig = [s for s in elig if not any(s["name"] in ln for ln in ig_lines)]
    if not elig:
        return ai_subject([s["name"] for s in SUBJECTS] + [u.get("name", "") for u in state["used"]])
    last_theme = state["used"][-1]["theme"] if state["used"] else None
    never = [s for s in elig if s["id"] not in last]
    pool = [s for s in (never or elig) if s["theme"] != last_theme] or (never or elig)
    return random.choice(pool) if never else min(pool, key=lambda s: last[s["id"]])


# -------------------------------------------------------------------- fonts
_fc = {}


def find_te_font():
    paths = [pathlib.Path("fonts/NotoSansTelugu-Bold.ttf"),
             pathlib.Path("/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf")]
    for p in paths:
        if p.exists():
            return str(p)
    subprocess.run(["sudo", "-n", "apt-get", "install", "-y", "-qq", "fonts-noto-core"], capture_output=True)
    if paths[1].exists():
        return str(paths[1])
    raise RuntimeError("Telugu font missing: add fonts/NotoSansTelugu-Bold.ttf")


def te_font(size):
    if ("te", size) not in _fc:
        kw = {"layout_engine": ImageFont.Layout.RAQM} if features.check("raqm") else {}
        _fc[("te", size)] = ImageFont.truetype(find_te_font(), size, **kw)
    return _fc[("te", size)]


def latin_font(size):
    if ("la", size) not in _fc:
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
            try:
                _fc[("la", size)] = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
        else:
            _fc[("la", size)] = ImageFont.load_default(size)
    return _fc[("la", size)]


LATIN_RE = re.compile(r"[A-Za-z]")
NEUTRAL = set(" .,:;-–—!?()'\"/&%+#@|•")


def text_runs(text):
    out, cur, cur_latin = [], "", None
    for ch in text:
        if ch in NEUTRAL or ch.isdigit():
            cur += ch
            continue
        is_l = bool(LATIN_RE.match(ch))
        if cur_latin is None:
            cur_latin = is_l
        if is_l != cur_latin:
            out.append((cur, cur_latin))
            cur, cur_latin = "", is_l
        cur += ch
    if cur:
        out.append((cur, bool(cur_latin)))
    return out


def run_font(is_latin, size):
    return latin_font(size) if is_latin else te_font(size)


def mixed_width(d, text, size):
    return sum(d.textlength(r, font=run_font(l, size)) for r, l in text_runs(text))


def draw_mixed(d, xy, text, size, fill, anchor="lt", sw=0, stroke_fill=(0, 0, 0, 255)):
    x, y = xy
    total = mixed_width(d, text, size)
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    asc, desc = te_font(size).getmetrics()
    base = y + (asc - desc) / 2 if anchor[1] == "m" else y + asc
    for r, l in text_runs(text):
        f = run_font(l, size)
        d.text((x, base), r, font=f, fill=fill, anchor="ls", stroke_width=sw, stroke_fill=stroke_fill)
        x += d.textlength(r, font=f)


def pill(text, size, fg, bg, pad=26):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w = int(mixed_width(tmp, text, size)) + pad * 2
    h = int(size * 1.7)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=bg)
    draw_mixed(d, (pad, h // 2), text, size, fg, anchor="lm")
    return img


# ----------------------------------------------------------------- voiceover
VOICE_FX = ("highpass=f=90,acompressor=threshold=0.12:ratio=3:attack=5:release=90,"
            "equalizer=f=3200:t=q:w=1.2:g=2.5,aecho=0.85:0.9:55:0.14")


def eleven_tts(text, path):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        params={"output_format": "mp3_44100_128"},
        json={"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_v3"),
              "voice_settings": {"stability": 0.5}}, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)


def tts(text, path):
    if os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID"):
        try:
            eleven_tts(text, path)
            if path.stat().st_size > 1000:
                return
        except Exception as e:
            print(f"ElevenLabs failed ({e}); using Edge TTS")
    try:
        import edge_tts
        rate = f"+{random.choice([2, 4, 6, 8])}%"
        asyncio.run(edge_tts.Communicate(text, os.getenv("TTS_VOICE", "te-IN-ShrutiNeural"),
                                         rate=rate).save(str(path)))
        if path.exists() and path.stat().st_size > 1000:
            return
    except Exception as e:
        print(f"edge-tts failed ({e}); using gTTS")
    from gtts import gTTS
    gTTS(text, lang="te").save(str(path))


def load_wav_trim(path):
    with wave.open(str(path)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    idx = np.where(np.abs(data) > 0.012)[0]
    if len(idx):
        data = data[max(0, idx[0] - int(0.03 * SR)): idx[-1] + int(0.05 * SR)]
    return data


def make_voice(segments):
    clips, starts, durs, t = [], [], [], 0.3
    for i, seg in enumerate(segments):
        mp3, wav = OUT / f"seg_{i}.mp3", OUT / f"seg_{i}.wav"
        tts(seg["text"], mp3)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", str(SR), "-ac", "1"]
        if os.getenv("VOICE_FX", "1") == "1":
            cmd += ["-af", VOICE_FX]
        run(cmd + ["-c:a", "pcm_s16le", str(wav)])
        data = load_wav_trim(wav)
        clips.append(data)
        starts.append(t)
        durs.append(len(data) / SR)
        t += len(data) / SR + random.uniform(0.35, 0.6)  # natural, uneven pauses
    total = t + 0.5
    voice = np.zeros(int(total * SR), np.float32)
    for s, d in zip(starts, clips):
        voice[int(s * SR): int(s * SR) + len(d)] += d
    voice = voice / max(1e-9, np.abs(voice).max()) * 0.9
    return voice, starts, durs, total


# ------------------------------------------------------------ music and sfx
def synth_music(seconds, rng):
    """Playful original instrumental: marimba-like melody, soft bass, light percussion."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    root = rng.choice([261.63, 293.66, 329.63])
    scale = [1, 9 / 8, 5 / 4, 3 / 2, 5 / 3, 2]  # major pentatonic
    bpm = rng.choice([108, 116, 124])
    beat = 60 / bpm
    out = np.zeros(n)
    for ci in range(int(seconds / (beat * 4)) + 1):
        f = root / 2 * rng.choice([1, 4 / 3, 3 / 2, 9 / 8])
        i0, ln = int(ci * beat * 4 * SR), int(beat * 4 * SR)
        seg = min(ln, n - i0)
        if seg > 0:
            tt = np.arange(seg) / SR
            out[i0:i0 + seg] += np.sin(2 * np.pi * f * tt) * np.minimum(1, tt / 0.05) * 0.22 * (1 - tt / (beat * 4) * 0.4)
    pl = np.arange(int(0.5 * SR)) / SR
    deg = 2
    for k in range(int(seconds / (beat / 2))):
        deg = max(0, min(len(scale) - 1, deg + rng.choice([-1, 0, 1, 1, -2, 2])))
        if rng.random() < 0.8:
            f = root * 2 * scale[deg]
            tone = (np.sin(2 * np.pi * f * pl) + 0.35 * np.sin(2 * np.pi * 4 * f * pl)) * np.exp(-9 * pl) * 0.2
            i0 = int(k * beat / 2 * SR)
            seg = min(len(tone), n - i0)
            if seg > 0:
                out[i0:i0 + seg] += tone[:seg]
    kick = np.arange(int(0.25 * SR)) / SR
    thump = np.sin(2 * np.pi * (55 + 80 * np.exp(-30 * kick)) * kick) * np.exp(-12 * kick) * 0.5
    for k in range(int(seconds / beat)):
        if k % 2 == 0:
            i0 = int(k * beat * SR)
            seg = min(len(thump), n - i0)
            if seg > 0:
                out[i0:i0 + seg] += thump[:seg]
    return (out / max(1e-9, np.abs(out).max()) * 0.9).astype(np.float32)


def get_music(seconds, rng):
    files = [p for p in sorted(MUSIC_DIR.glob("*")) if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".aac"}]
    if files:
        choice = rng.choice(files)
        print(f"Music: {choice.name}")
        wav = OUT / "music_user.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(choice), "-t", str(seconds),
             "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        with wave.open(str(wav)) as w:
            m = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        m = m / max(1e-9, np.abs(m).max()) * 0.9
    else:
        print("Music: synthesized playful instrumental")
        m = synth_music(seconds, rng)
    out = np.zeros(int(seconds * SR), np.float32)
    out[:min(len(out), len(m))] = m[:len(out)]
    return out


def make_sfx(starts, n, rng):
    sfx = np.zeros(n, np.float32)
    wl = int(0.4 * SR)
    noise = np.random.default_rng(rng.randint(0, 10**6)).normal(0, 1, wl)
    whoosh = np.convolve(noise, np.ones(30) / 30, mode="same") * np.sin(np.pi * np.linspace(0, 1, wl)) ** 2 * 1.6
    tt = np.arange(int(0.16 * SR)) / SR
    pop = np.sin(2 * np.pi * (900 - 4200 * tt) * tt) * np.exp(-28 * tt)
    for i, s in enumerate(starts):
        if i > 0:
            i0 = max(0, int((s - 0.38) * SR))
            seg = min(wl, n - i0)
            sfx[i0:i0 + seg] += whoosh[:seg] * 0.5
        i0 = int((s + 0.02) * SR)
        seg = min(len(pop), n - i0)
        if seg > 0:
            sfx[i0:i0 + seg] += pop[:seg] * 0.55
    return sfx


def mix_audio(voice, music, sfx, path):
    n = len(voice)
    k = int(0.3 * SR)
    c = np.cumsum(np.abs(voice), dtype=np.float64)
    env = (c[k:] - c[:-k]) / k
    env = np.concatenate([np.full(k // 2, env[0]), env, np.full(n - len(env) - k // 2, env[-1])])
    duck = np.clip(env / 0.06, 0, 1)
    gain = 0.32 - 0.19 * duck
    fade = np.ones(n)
    f = int(1.5 * SR)
    fade[-f:] = np.linspace(1, 0, f)
    mix = np.clip((voice + music[:n] * gain + sfx[:n] * 0.35) * fade, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())


# ------------------------------------------------------------------- visuals
SW, SH = 270, 480
_yy, _xx = np.mgrid[0:SH, 0:SW].astype(np.float32)
_yy /= SH
_xx /= SW
VIG = (1 - 0.45 * ((_xx - 0.5) ** 2 + (_yy - 0.5) ** 2))[..., None]


def background(t, theme):
    c1, c2, c3 = (np.array(c, np.float32) for c in THEMES[theme][:3])
    k = (0.75 * _yy + 0.25 * _xx + 0.05 * np.sin(t * 0.7))[..., None]
    arr = c1 * (1 - k) + c2 * k
    for j in range(4):
        cx = 0.5 + 0.42 * math.sin(t * (0.45 + 0.08 * j) + j * 1.9)
        cy = 0.5 + 0.42 * math.cos(t * (0.37 + 0.07 * j) + j * 2.4)
        arr += np.exp(-(((_xx - cx) * 0.5625) ** 2 + (_yy - cy) ** 2) / 0.05)[..., None] * c3 * 0.5
    arr *= VIG
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((W, H), Image.BICUBIC)


def make_dots():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(0, H, 56):
        for x in range((y // 56 % 2) * 28, W, 56):
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(255, 255, 255, 24))
    return img


def rounded_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
    return m


def window_base():
    """Static shadow + white frame; the photo is pasted on top each frame."""
    aw, ah = WIN_W + 100, WIN_H + 100
    base = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    sh = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((44, 66, aw - 44, ah - 30), 70, fill=(0, 0, 0, 120))
    base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22)))
    ImageDraw.Draw(base).rounded_rectangle((36, 36, aw - 36, ah - 36), 62, fill=(255, 255, 255, 255))
    return base


def fallback_window(theme, name):
    c1, c2, c3, solid = THEMES[theme]
    arr = np.zeros((WIN_H, WIN_W, 3), np.float32)
    k = np.linspace(0, 1, WIN_H)[:, None, None]
    arr[:] = np.array(c1, np.float32) * (1 - k) + np.array(c3, np.float32) * k * 0.8
    img = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for cx, cy, r in ((150, 160, 130), (800, 620, 170), (700, 120, 70)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(min(255, v + 40) for v in solid))
    draw_mixed(d, (WIN_W // 2, WIN_H // 2), name, 130, WHITE, anchor="mm", sw=8)
    return img


def badge(n, solid):
    s = 170
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = []
    for i in range(24):  # starburst sticker
        ang = i * math.pi / 12
        r = s / 2 - (4 if i % 2 else 18)
        pts.append((s / 2 + r * math.cos(ang), s / 2 + r * math.sin(ang)))
    d.polygon(pts, fill=GOLD + (255,), outline=(255, 255, 255, 255))
    d.text((s / 2, s / 2 - 34), "FACT", font=latin_font(30), fill=NAVY, anchor="mm")
    d.text((s / 2, s / 2 + 22), str(n), font=latin_font(74), fill=NAVY, anchor="mm")
    return img.rotate(-10, resample=Image.BICUBIC)


def ribbon(name, right_text, solid):
    img = Image.new("RGBA", (960, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 959, 149), 44, fill=WHITE + (255,), outline=solid + (255,), width=8)
    draw_mixed(d, (40, 75), name, 78, NAVY, anchor="lm")
    p = pill(right_text, 34, WHITE, solid + (255,), pad=26)
    img.alpha_composite(p, (960 - 34 - p.width, 75 - p.height // 2))
    return img


def caption_card(solid):
    img = Image.new("RGBA", (960, 300), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, 959, 299), 52, fill=WHITE + (255,), outline=solid + (255,), width=9)
    return img


def render_chunk(words, active, solid, size=70, max_w=830):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    space = tmp.textlength(" ", font=te_font(size))
    lines, cur = [[]], 0
    for i, w in enumerate(words):
        ww = mixed_width(tmp, w, size)
        if lines[-1] and cur + space + ww > max_w:
            lines.append([])
            cur = 0
        cur += (space if lines[-1] else 0) + ww
        lines[-1].append((i, w, ww))
    lh = int(size * 1.5)
    img = Image.new("RGBA", (max_w + 80, lh * len(lines) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 10
    for ln in lines:
        total = sum(x[2] for x in ln) + space * (len(ln) - 1)
        x = (img.width - total) / 2
        for i, w, ww in ln:
            if i == active:
                d.rounded_rectangle((x - 12, y + 6, x + ww + 12, y + lh - 8), 24, fill=solid + (255,))
                draw_mixed(d, (x, y), w, size, WHITE)
            else:
                draw_mixed(d, (x, y), w, size, NAVY)
            x += ww + space
        y += lh
    return img


def swipe_band():
    bw = 300
    a = np.concatenate([np.linspace(0, 210, bw // 2), np.linspace(210, 0, bw // 2)]).astype(np.uint8)
    arr = np.zeros((H, bw, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = 255, 255, 255, a[None, :]
    return Image.fromarray(arr, "RGBA")


def confetti(d, since, seed, colors):
    if since > 0.9:
        return
    k = ease_out(since / 0.9)
    r = random.Random(seed)
    for _ in range(22):
        ang = r.uniform(0, 2 * math.pi)
        dist = r.uniform(200, 560) * k
        x = W / 2 + math.cos(ang) * dist
        y = 820 + math.sin(ang) * dist * 0.85 + 90 * k * k
        sz = r.uniform(9, 20) * (1 - 0.7 * k)
        col = r.choice(colors)
        if r.random() < 0.5:
            d.ellipse((x - sz, y - sz, x + sz, y + sz), fill=col)
        else:
            d.polygon([(x, y - sz * 1.4), (x + sz, y), (x, y + sz * 1.4), (x - sz, y)], fill=col)


# ------------------------------------------------------- footage and photos
WINSZ = (WIN_W, WIN_H)


class VideoVisual:
    def __init__(self, path):
        self.p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-stream_loop", "-1", "-i", str(path), "-vf",
             f"scale={WIN_W}:{WIN_H}:force_original_aspect_ratio=increase,crop={WIN_W}:{WIN_H},fps={FPS}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=WIN_W * WIN_H * 3 * 2)

    def frame(self, t):
        buf = self.p.stdout.read(WIN_W * WIN_H * 3)
        if len(buf) < WIN_W * WIN_H * 3:
            return None
        return Image.frombuffer("RGB", WINSZ, buf, "raw", "RGB", 0, 1)

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


class StillVisual:
    def __init__(self, path):
        img = Image.open(path).convert("RGB")
        s = max(WIN_W * 1.3 / img.width, WIN_H * 1.3 / img.height, 1.0)
        self.img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS) if s > 1 else img
        self.dir = random.choice([-1, 1])

    def frame(self, t):
        z = 1.0 + 0.10 * min(1.0, t / 6)
        cw, ch = self.img.width / z, self.img.height / z
        # keep window aspect
        ar = WIN_W / WIN_H
        if cw / ch > ar:
            cw = ch * ar
        else:
            ch = cw / ar
        cx = self.img.width / 2 + self.dir * (self.img.width - cw) / 2 * (t / 8 - 0.5)
        cy = self.img.height / 2
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        box = (max(0, box[0]), max(0, box[1]), min(self.img.width, box[2]), min(self.img.height, box[3]))
        return self.img.crop(tuple(int(v) for v in box)).resize(WINSZ, Image.BILINEAR)

    def close(self):
        pass


def _download(url, path, cap=45 * 1024 * 1024):
    with requests.get(url, stream=True, timeout=90) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > cap:
                    raise RuntimeError("file too large")
                f.write(chunk)


def fetch_visuals(subject, n=4):
    out = []
    local = pathlib.Path("images") / subject["id"]
    if local.exists():
        for p in sorted(local.glob("*")):
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                out.append(("still", p))
    key = os.getenv("PEXELS_API_KEY")
    if key and len(out) < n:
        hdr = {"Authorization": key}
        for q in subject["pexels"]:
            if len(out) >= n:
                break
            try:
                r = requests.get("https://api.pexels.com/videos/search", headers=hdr,
                                 params={"query": q, "per_page": 12}, timeout=30)
                r.raise_for_status()
                vids = [v for v in r.json().get("videos", []) if v.get("duration", 0) >= 4]
                random.shuffle(vids)
                for v in vids[:3]:
                    files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and f.get("width", 0) >= 960]
                    if not files:
                        continue
                    files.sort(key=lambda f: abs(f["width"] - 1280))
                    path = OUT / f"vis_{len(out)}.mp4"
                    _download(files[0]["link"], path)
                    out.append(("video", path))
                    break
            except Exception as e:
                print(f"Pexels video '{q}' failed: {e}")
        for q in subject["pexels"]:
            if len(out) >= n:
                break
            try:
                r = requests.get("https://api.pexels.com/v1/search", headers=hdr,
                                 params={"query": q, "per_page": 10, "orientation": "landscape"}, timeout=30)
                r.raise_for_status()
                photos = r.json().get("photos", [])
                random.shuffle(photos)
                for ph in photos[:2]:
                    if len(out) >= n:
                        break
                    path = OUT / f"vis_{len(out)}.jpg"
                    _download(ph["src"]["large2x"], path)
                    out.append(("still", path))
            except Exception as e:
                print(f"Pexels photo '{q}' failed: {e}")
    print(f"Visuals found: {len(out)}")
    return out[:n]


def open_visual(item):
    kind, path = item
    return VideoVisual(path) if kind == "video" else StillVisual(path)


# ---------------------------------------------------------------- video build
def build_chunks(segments, starts, durs, solid):
    chunks = []
    for si, seg in enumerate(segments):
        words = seg["text"].split()
        weights = [len(w) + 1.5 for w in words]
        tot, acc, wstart = sum(weights), 0.0, []
        for wt in weights:
            wstart.append(starts[si] + durs[si] * acc / tot)
            acc += wt
        for i in range(0, len(words), 3):
            cw, ws = words[i:i + 3], wstart[i:i + 3]
            end = wstart[i + 3] if i + 3 < len(words) else starts[si] + durs[si] + 0.2
            chunks.append({"words": cw, "starts": ws, "end": end,
                           "imgs": [render_chunk(cw, a, solid) for a in range(len(cw))]})
    return chunks


def render_video(segments, starts, durs, total, audio_path, out_path, subject, visual_items, fact_no):
    theme = subject["theme"]
    solid = THEMES[theme][3]
    chunks = build_chunks(segments, starts, durs, solid)
    dots, base, wmask = make_dots(), window_base(), rounded_mask(WIN_W, WIN_H, 46)
    fb = fallback_window(theme, subject["name"])
    chip = pill(f"FACT FILE #{fact_no:02d}", 32, WHITE, solid + (255,), pad=24)
    n_facts = sum(1 for s in segments if s["kind"] == "fact")
    ribbons = []
    for sg in segments:
        right = "FACTS" if sg["kind"] == "intro" else "FOLLOW" if sg["kind"] == "outro" else f"{sg['n']}/{n_facts}"
        ribbons.append(ribbon(subject["name"], right, solid))
    badges = {sg["n"]: badge(sg["n"], solid) for sg in segments if sg["kind"] == "fact"}
    card, band = caption_card(solid), swipe_band()
    sparkle = [WHITE, GOLD, tuple(min(255, v + 60) for v in THEMES[theme][2])]
    bounds = [x - 0.3 for x in starts] + [total]

    log = open(OUT / "ffmpeg.log", "w")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-i", str(audio_path), "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
         "-shortest", "-movflags", "+faststart", str(out_path)], stdin=subprocess.PIPE, stderr=log)
    ci, vis, vis_seg = 0, None, -1
    for fi in range(int(total * FPS)):
        t = fi / FPS
        si = max([i for i, x in enumerate(starts) if t >= x - 0.3] or [0])
        since = t - bounds[si]
        sg = segments[si]

        frame = background(t, theme)
        frame.paste(dots, (0, 0), dots)
        d = ImageDraw.Draw(frame)

        # progress bar + chip
        seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
        for i in range(len(segments)):
            x0 = 60 + i * (seg_w + 8)
            d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(255, 255, 255, 90) if False else tuple(int(v * 0.55 + 90) for v in THEMES[theme][0]))
            fill = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
            if fill > 0:
                d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * fill), 150), 4, fill=WHITE)
        frame.paste(chip, (60, 172), chip)
        rb = ribbons[si]
        frame.paste(rb, (60, 262), rb)

        # framed photo window with pop-in
        if vis_seg != si:
            if vis:
                vis.close()
            vis, vis_seg = (open_visual(visual_items[si % len(visual_items)]) if visual_items else None), si
        win = vis.frame(max(0.0, since)) if vis else None
        if win is None:
            win = fb
        asm = base.copy()
        asm.paste(win, (50, 50), wmask)
        sc = 0.88 + 0.12 * ease_back(since / 0.45)
        if abs(sc - 1) > 0.002:
            asm = asm.resize((int(asm.width * sc), int(asm.height * sc)), Image.BILINEAR)
        frame.paste(asm, (int(WIN_X - 50 + (WIN_W + 100 - asm.width) / 2), int(WIN_Y - 50 + (WIN_H + 100 - asm.height) / 2)), asm)

        if sg["kind"] == "fact":
            bs = max(0.05, ease_back((since - 0.15) / 0.5))
            b = badges[sg["n"]]
            b = b.resize((max(1, int(b.width * bs)), max(1, int(b.height * bs))), Image.BILINEAR)
            frame.paste(b, (WIN_X + WIN_W - 130 - b.width // 2, WIN_Y + WIN_H - 120 - b.height // 2), b)

        # caption card with word-by-word highlight
        frame.paste(card, (60, 1245), card)
        while ci < len(chunks) - 1 and t >= chunks[ci]["end"]:
            ci += 1
        ch = chunks[ci]
        if ch["starts"][0] - 0.05 <= t < ch["end"]:
            ai = max(i for i, x in enumerate(ch["starts"]) if t >= x - 0.05)
            img = ch["imgs"][ai]
            frame.paste(img, ((W - img.width) // 2, 1245 + (300 - img.height) // 2), img)

        confetti(d, since, 1000 + si, sparkle)
        if si > 0 and since < 0.4:
            frame.paste(band, (int(-300 + (W + 300) * ease_out(since / 0.4)), 0), band)
        proc.stdin.write(frame.tobytes())
    if vis:
        vis.close()
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------------------------ commands
def generate():
    OUT.mkdir(exist_ok=True)
    state = load_state()
    ig_lines = []
    if os.getenv("IG_ACCESS_TOKEN"):
        ig_lines = recent_ig_names(os.getenv("IG_USER_ID") or "me", os.environ["IG_ACCESS_TOKEN"])
    subject = choose_subject(state, ig_lines)
    if not subject:
        print("No unused subject available (all are in cooldown). Add more in facts_data.py. Skipping.")
        set_output("has_post", "false")
        return
    print(f"Subject: {subject['name']} ({subject['id']})")
    idx = sorted(random.sample(range(len(subject["facts"])), min(FACTS_PER_REEL, len(subject["facts"]))))
    facts = [subject["facts"][i] for i in idx]
    now = dt.datetime.now(IST)
    rng = random.Random(int(now.timestamp()))
    fact_no = len(state["used"]) + 1

    intro = f"{subject['about']} గురించి మీకు తెలియని {NUM_WORDS.get(len(facts), 'కొన్ని')} నిజాలు. చివరి వరకు చూడండి!"
    segments = [{"kind": "intro", "text": intro}]
    segments += [{"kind": "fact", "text": f, "n": i} for i, f in enumerate(facts, 1)]
    segments.append({"kind": "outro", "text": OUTRO_TE})

    voice, starts, durs, total = make_voice(segments)
    music = get_music(total, rng)
    sfx = make_sfx(starts, len(voice), rng)
    audio = OUT / "audio.wav"
    mix_audio(voice, music, sfx, audio)
    items = fetch_visuals(subject)
    render_video(segments, starts, durs, total, audio, OUT / "reel.mp4", subject, items, fact_no)

    tags = " ".join(ANCHOR_TAGS + [subject["tag"]] + rng.sample(POOL_TAGS, 2))
    caption = "\n".join(
        [f"📌 {subject['name']} గురించి మీకు తెలియని {NUM_WORDS.get(len(facts), 'కొన్ని')} నిజాలు", ""]
        + [f"{i}. {f}" for i, f in enumerate(facts, 1)]
        + ["", "మీకు ఏ నిజం నచ్చింది? కామెంట్ చేయండి 👇 ఫాలో అవ్వండి!", "", tags])[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps(
        {"caption": caption, "video_name": name, "subject_id": subject["id"],
         "subject_name": subject["name"], "theme": subject["theme"]}, ensure_ascii=False))
    print(f"Built out/reel.mp4 ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB, {total:.0f}s)")
    set_output("has_post", "true")
    set_output("video_name", name)


def api(method, path, **params):
    r = requests.request(method, f"{GRAPH}/{path}", data=params, timeout=90)
    if not r.ok:
        raise RuntimeError(f"Graph API error {r.status_code}: {r.text}")
    return r.json()


def wait_until_public(url, tries=12):
    for _ in range(tries):
        if requests.head(url, timeout=20).status_code == 200:
            return
        time.sleep(10)
    raise RuntimeError(f"Video not reachable publicly: {url}")


def publish():
    meta_path = OUT / "meta.json"
    if not meta_path.exists():
        print("Nothing to publish.")
        return
    meta = json.loads(meta_path.read_text())
    if os.getenv("DRY_RUN") == "1":
        print("DRY_RUN=1 -> not publishing.\n\n" + meta["caption"])
        return
    user_id = os.getenv("IG_USER_ID") or "me"
    token = os.environ["IG_ACCESS_TOKEN"]
    if any(meta["subject_name"] in ln for ln in recent_ig_names(user_id, token)):
        print("This subject was already posted recently. Skipping publish.")
        save_state(meta)
        return
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://raw.githubusercontent.com/{repo}/{os.getenv('MEDIA_BRANCH', 'media')}/{meta['video_name']}"
    wait_until_public(url)
    container = api("POST", f"{user_id}/media", media_type="REELS", video_url=url,
                    caption=meta["caption"], share_to_feed="true", access_token=token)["id"]
    for _ in range(60):
        st = requests.get(f"{GRAPH}/{container}", params={"fields": "status_code,status", "access_token": token},
                          timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Timed out waiting for Instagram to process the video.")
    res = api("POST", f"{user_id}/media_publish", creation_id=container, access_token=token)
    print(f"Published Reel! Media ID: {res['id']}")
    save_state(meta)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("generate", "all"):
        generate()
    if cmd in ("publish", "all"):
        publish()

"""
Telugu 3D-toon story Reels bot: talking animals / kids tell short moral stories.

  python main.py generate | publish | all

No stock footage or image keys needed: characters and scenery are drawn by the program.
Env / secrets:
  IG_ACCESS_TOKEN, IG_USER_ID, IG_GRAPH_HOST   Instagram API
  ELEVENLABS_API_KEY + ELEVENLABS_VOICE_MALE / ELEVENLABS_VOICE_FEMALE   optional, more natural voices (eleven_v3)
  ANTHROPIC_API_KEY + AI_STORIES=1   optional: Claude writes NEW stories when the bank is used up
  COOLDOWN_DAYS (default 30), STORY=<id> to force one, DRY_RUN=1 to skip posting
"""
import asyncio
import copy
import datetime as dt
import hashlib
import hmac
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
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

from stories_data import CAST, CHARACTER_LOOKS, EMOTIONS, NARRATOR, SHOTS, STAGING, STORIES, STYLE

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").strip().rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0").strip())


def ig_user():
    return (os.getenv("IG_USER_ID") or "me").strip()


def sign(params):
    """Adds appsecret_proof when FB_APP_SECRET is set (needed if the app has 'Require App Secret' ON)."""
    secret = (os.getenv("FB_APP_SECRET") or "").strip()
    tok = params.get("access_token")
    if secret and tok:
        params["appsecret_proof"] = hmac.new(secret.encode(), tok.encode(), hashlib.sha256).hexdigest()
    return params


def ig_token():
    return (os.getenv("IG_ACCESS_TOKEN") or "").strip()
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
CLIPS_DIR = pathlib.Path("clips")
W, H, FPS, SR = 1080, 1920, 30, 44100
GY = 1190                      # ground line for characters
SLOT = {0: 290, 1: 780}        # x of left / right character
NAVY, WHITE, GOLD = (24, 28, 62), (255, 255, 255), (255, 214, 102)
HASHTAGS = ["#telugustories", "#moralstories", "#kidsstories", "#teluguanimation", "#telugureels", "#neetikathalu"]


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


def ease_bounce(k):
    k = max(0.0, min(1.0, k))
    n, d = 7.5625, 2.75
    if k < 1 / d:
        return n * k * k
    if k < 2 / d:
        k -= 1.5 / d
        return n * k * k + 0.75
    if k < 2.5 / d:
        k -= 2.25 / d
        return n * k * k + 0.9375
    k -= 2.625 / d
    return n * k * k + 0.984375


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
    st["used"].append({"id": meta["story_id"], "title": meta["title"],
                       "ts": dt.datetime.now(dt.timezone.utc).isoformat()})
    st["used"] = st["used"][-400:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def recent_ig_titles(user_id, token):
    try:
        r = requests.get(f"{GRAPH}/{user_id}/media", params=sign({"fields": "caption", "limit": 25,
                                                                                "access_token": token}), timeout=30)
        r.raise_for_status()
        return [(m.get("caption") or "").split("\n")[0] for m in r.json().get("data", [])]
    except Exception as e:
        print(f"Recent-post check skipped: {e}")
        return []


def ai_story(used_titles):
    """Optional: Claude writes a NEW short moral story. Always review AI Telugu on your page."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or os.getenv("AI_STORIES") != "1":
        return None
    prompt = (
        "Write ONE new short moral story (traditional Panchatantra/Aesop style, your own wording) for kids in simple, "
        "natural spoken Telugu. Exactly 2 characters from this list: " + ", ".join(CAST) + ". 8 to 10 lines; each line max 22 words; "
        "mix a few 'narrator' lines with character lines. Do NOT reuse these titles: " + ", ".join(used_titles) + ". "
        'Return ONLY JSON: {"title":"Telugu title","scene":"day|sunset|night","cast":["id1","id2"],"tag":"#englishtag",'
        '"lines":[["narrator","..."],["id1","..."]],"moral":"one Telugu sentence"}')
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 2000,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=90)
        r.raise_for_status()
        text = re.sub(r"^```(?:json)?|```$", "", r.json()["content"][0]["text"].strip(), flags=re.M).strip()
        s = json.loads(text)
        assert len(s["cast"]) == 2 and all(c in CAST for c in s["cast"]) and s["scene"] in ("day", "sunset", "night")
        s["lines"] = [tuple(x) for x in s["lines"]]
        assert 6 <= len(s["lines"]) <= 14 and all(w == "narrator" or w in s["cast"] for w, _ in s["lines"])
        s["id"] = "ai-" + hashlib.md5(s["title"].encode()).hexdigest()[:8]
        return s
    except Exception as e:
        print(f"AI story failed: {e}")
        return None


def choose_story(state, ig_titles):
    forced = os.getenv("STORY")
    if forced:
        for s in STORIES:
            if s["id"] == forced:
                return s
    now = dt.datetime.now(dt.timezone.utc)
    cooldown = dt.timedelta(days=int(os.getenv("COOLDOWN_DAYS", "30")))
    last = {}
    for u in state["used"]:
        try:
            ts = dt.datetime.fromisoformat(u["ts"])
            last[u["id"]] = max(last.get(u["id"], ts), ts)
        except Exception:
            pass
    elig = [s for s in STORIES if (s["id"] not in last or now - last[s["id"]] > cooldown)
            and not any(s["title"] in t for t in ig_titles)]
    if not elig:
        if os.getenv("REQUIRE_CLIPS") == "1":
            return None
        return ai_story([s["title"] for s in STORIES] + [u.get("title", "") for u in state["used"]])
    with_clips = [s for s in elig if story_clips(s["id"])]
    if os.getenv("REQUIRE_CLIPS") == "1":
        elig = with_clips
        if not elig:
            return None
    elif with_clips:
        elig = with_clips
    never = [s for s in elig if s["id"] not in last]
    return random.choice(never) if never else min(elig, key=lambda s: last[s["id"]])


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
NEUTRAL = set(" .,:;-–—!?()'\"/&%+#@|•…")


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
            "equalizer=f=3200:t=q:w=1.2:g=2.5,aecho=0.85:0.9:55:0.12")


def eleven_tts(text, path, gender):
    vid = os.getenv(f"ELEVENLABS_VOICE_{gender.upper()}") or os.getenv("ELEVENLABS_VOICE_ID")
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                      headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                               "Accept": "audio/mpeg"}, params={"output_format": "mp3_44100_128"},
                      json={"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_v3"),
                            "voice_settings": {"stability": 0.5}}, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)


def tts(text, path, spk):
    gender = spk["voice"]
    if os.getenv("ELEVENLABS_API_KEY") and (os.getenv(f"ELEVENLABS_VOICE_{gender.upper()}") or os.getenv("ELEVENLABS_VOICE_ID")):
        try:
            eleven_tts(text, path, gender)
            if path.stat().st_size > 1000:
                return
        except Exception as e:
            print(f"ElevenLabs failed ({e}); using Edge TTS")
    try:
        import edge_tts
        voice = "te-IN-MohanNeural" if gender == "male" else "te-IN-ShrutiNeural"
        rate = spk.get("rate", 0) + random.choice([0, 2, 4])
        asyncio.run(edge_tts.Communicate(text, voice, rate=f"{rate:+d}%").save(str(path)))
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


def envelope(data):
    """Mouth-opening amount per video frame (0..1) from the loudness of the speech."""
    win = SR // FPS
    n = len(data) // win + 1
    rms = np.array([np.sqrt(np.mean(data[i * win:(i + 1) * win] ** 2)) if len(data[i * win:(i + 1) * win]) else 0
                    for i in range(n)])
    ref = max(1e-6, np.percentile(rms, 90))
    env = np.clip(rms / ref, 0, 1.2)
    sm = np.convolve(env, [0.25, 0.5, 0.25], mode="same")
    return np.clip(sm, 0, 1)


def make_voice(segments):
    clips, starts, durs, envs, t = [], [], [], [], 0.4
    for i, seg in enumerate(segments):
        spk = seg["spk"]
        mp3, wav = OUT / f"seg_{i}.mp3", OUT / f"seg_{i}.wav"
        tts(seg["text"], mp3, spk)
        f = 2 ** (spk.get("semi", 0) / 12)
        chain = f"aresample={SR},asetrate={int(SR * f)},aresample={SR},atempo={1 / f:.4f}" if abs(f - 1) > 1e-3 else f"aresample={SR}"
        if os.getenv("VOICE_FX", "1") == "1":
            chain += "," + VOICE_FX
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-af", chain,
             "-ar", str(SR), "-c:a", "pcm_s16le", str(wav)])
        data = load_wav_trim(wav)
        clips.append(data)
        envs.append(envelope(data))
        starts.append(t)
        durs.append(len(data) / SR)
        t += len(data) / SR + random.uniform(0.3, 0.5)
    total = t + 0.6
    voice = np.zeros(int(total * SR), np.float32)
    for s, d in zip(starts, clips):
        voice[int(s * SR): int(s * SR) + len(d)] += d
    voice = voice / max(1e-9, np.abs(voice).max()) * 0.9
    return voice, starts, durs, envs, total


# ------------------------------------------------------------ music and sfx
def synth_music(seconds, rng):
    n = int(seconds * SR)
    root = rng.choice([261.63, 293.66, 329.63])
    scale = [1, 9 / 8, 5 / 4, 3 / 2, 5 / 3, 2]
    bpm = rng.choice([100, 108, 116])
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
        if rng.random() < 0.7:
            f = root * 2 * scale[deg]
            tone = (np.sin(2 * np.pi * f * pl) + 0.35 * np.sin(2 * np.pi * 4 * f * pl)) * np.exp(-9 * pl) * 0.2
            i0 = int(k * beat / 2 * SR)
            seg = min(len(tone), n - i0)
            if seg > 0:
                out[i0:i0 + seg] += tone[:seg]
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


def make_sfx(events, n, rng):
    """events: list of (time, kind) kind in pop|whoosh|thud"""
    sfx = np.zeros(n, np.float32)
    wl = int(0.4 * SR)
    noise = np.random.default_rng(rng.randint(0, 10**6)).normal(0, 1, wl)
    whoosh = np.convolve(noise, np.ones(30) / 30, mode="same") * np.sin(np.pi * np.linspace(0, 1, wl)) ** 2 * 1.6
    tt = np.arange(int(0.16 * SR)) / SR
    pop = np.sin(2 * np.pi * (900 - 4200 * tt) * tt) * np.exp(-28 * tt)
    for t, kind in events:
        snd, g = (whoosh, 0.5) if kind == "whoosh" else (pop, 0.5)
        i0 = max(0, int(t * SR))
        seg = min(len(snd), n - i0)
        if seg > 0:
            sfx[i0:i0 + seg] += snd[:seg] * g
    return sfx


def mix_audio(voice, music, sfx, path):
    n = len(voice)
    k = int(0.3 * SR)
    c = np.cumsum(np.abs(voice), dtype=np.float64)
    env = (c[k:] - c[:-k]) / k
    env = np.concatenate([np.full(k // 2, env[0]), env, np.full(n - len(env) - k // 2, env[-1])])
    duck = np.clip(env / 0.06, 0, 1)
    gain = 0.26 - 0.17 * duck
    fade = np.ones(n)
    f = int(1.5 * SR)
    fade[-f:] = np.linspace(1, 0, f)
    mix = np.clip((voice + music[:n] * gain + sfx[:n] * 0.35) * fade, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())


# ------------------------------------------------- 3D-toon sprite renderer
_SPR, _ROT = {}, {}
_L = np.array([-0.45, -0.62, 0.64], np.float32)
_L /= np.linalg.norm(_L)


def _q(v):
    return max(2, int(round(v / 2)) * 2)


def sphere(rx, ry, color, spec=0.45, rim=0.3):
    """Lit, glossy ellipsoid sprite (cached). This is what gives the 3D look."""
    rx, ry = _q(rx), _q(ry)
    key = (rx, ry, tuple(color), round(spec, 2), round(rim, 2))
    if key in _SPR:
        return _SPR[key]
    w, h = 2 * rx + 6, 2 * ry + 6
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx, ny = (xx - w / 2 + 0.5) / rx, (yy - h / 2 + 0.5) / ry
    d2 = nx * nx + ny * ny
    nz = np.sqrt(np.clip(1 - d2, 0, 1))
    ndl = nx * _L[0] + ny * _L[1] + nz * _L[2]
    diff = np.clip(ndl, 0, 1)
    rgb = np.array(color, np.float32)[None, None, :] * (0.40 + 0.78 * diff)[..., None]
    rz = 2 * ndl * nz - _L[2]
    rgb += (np.clip(rz, 0, 1) ** 22 * spec)[..., None] * 255
    rgb += ((1 - nz) ** 2.6 * rim)[..., None] * np.array([120, 170, 255], np.float32) * 0.6
    alpha = np.clip((1 - np.sqrt(d2)) * min(rx, ry) * 0.9, 0, 1)
    img = Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8), "RGBA")
    _SPR[key] = img
    return img


def rot_sphere(rx, ry, color, ang, spec=0.45, rim=0.3):
    key = (_q(rx), _q(ry), tuple(color), int(ang), round(spec, 2), round(rim, 2))
    if key not in _ROT:
        _ROT[key] = sphere(rx, ry, color, spec, rim).rotate(int(ang), resample=Image.BICUBIC, expand=True)
    return _ROT[key]


def put(frame, spr, cx, cy):
    frame.paste(spr, (int(cx - spr.width / 2), int(cy - spr.height / 2)), spr)


_SHADOW = None


def shadow(frame, cx, cy, rx, alpha=1.0):
    global _SHADOW
    if _SHADOW is None:
        im = Image.new("RGBA", (600, 120), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((60, 30, 540, 90), fill=(0, 0, 0, 120))
        _SHADOW = im.filter(ImageFilter.GaussianBlur(12))
    s = _SHADOW.resize((int(rx * 2.2), int(rx * 0.44)))
    if alpha < 1:
        s = s.copy()
        s.putalpha(s.getchannel("A").point(lambda v: int(v * alpha)))
    put(frame, s, cx, cy)


# --------------------------------------------------------------- characters
K = 1.35                       # the "world" is drawn at 1.35x so the virtual camera can zoom in sharply
GROUND = GY * K

EMO = {
    "neutral":  dict(angry=0.0, lift=0, lid=0.0, eye=1.0, smile=0.25, gape=0.0),
    "happy":    dict(angry=0.0, lift=3, lid=0.0, eye=1.0, smile=1.0, gape=0.0, sparkle=1),
    "excited":  dict(angry=0.0, lift=9, lid=0.0, eye=1.12, smile=1.0, gape=0.3, sparkle=1),
    "laugh":    dict(angry=0.0, lift=4, lid=0.0, eye=1.0, smile=1.0, gape=0.8, arc=1),
    "kind":     dict(angry=-0.2, lift=2, lid=0.1, eye=1.0, smile=0.85, gape=0.0),
    "proud":    dict(angry=0.25, lift=0, lid=0.18, eye=1.0, smile=0.7, gape=0.0),
    "sly":      dict(angry=0.5, lift=0, lid=0.36, eye=0.95, smile=0.8, gape=0.0),
    "sad":      dict(angry=-0.75, lift=2, lid=0.18, eye=1.06, smile=-0.9, gape=0.0, tear=1),
    "angry":    dict(angry=0.85, lift=-4, lid=0.12, eye=0.95, smile=-0.6, gape=0.18),
    "surprised": dict(angry=0.0, lift=17, lid=0.0, eye=1.22, smile=0.0, gape=0.6),
    "scared":   dict(angry=-0.55, lift=11, lid=0.0, eye=1.15, smile=-0.4, gape=0.22, sweat=1),
    "sleepy":   dict(angry=0.0, lift=0, lid=1.0, eye=1.0, smile=0.15, gape=0.0, zzz=1),
}


def eye(frame, x, y, r, look, blink, headcol):
    put(frame, sphere(r, r, (250, 250, 252), 0.35, 0.1), x, y)
    px, py = x + look * r * 0.34, y + r * 0.05
    put(frame, sphere(r * 0.56, r * 0.56, (25, 22, 38), 0.9, 0.0), px, py)
    d = ImageDraw.Draw(frame)
    hr = max(2, r * 0.15)
    d.ellipse((px - r * 0.2 - hr, py - r * 0.22 - hr, px - r * 0.2 + hr, py - r * 0.22 + hr), fill=(255, 255, 255))
    if blink > 0:
        h = max(2, r * 2.1 * min(1.0, blink))
        d.ellipse((x - r * 1.08, y - r * 1.08, x + r * 1.08, y - r * 1.08 + h), fill=headcol)
        if blink > 0.9:
            d.line((x - r * 0.9, y + r * 0.1, x + r * 0.9, y + r * 0.1), fill=(40, 30, 35), width=max(4, int(r * 0.2)))


def mouth(frame, x, y, w, op, smile=0.2):
    d = ImageDraw.Draw(frame)
    lw = max(5, int(w * 0.13))
    col = (95, 35, 45)
    if op < 0.12:
        if smile > 0.3:
            d.arc((x - w / 2, y - w * 0.35, x + w / 2, y + w * 0.30), 15, 165, fill=col, width=lw)
        elif smile < -0.3:
            d.arc((x - w / 2, y - w * 0.12, x + w / 2, y + w * 0.5), 195, 345, fill=col, width=lw)
        else:
            d.line((x - w * 0.3, y, x + w * 0.3, y), fill=col, width=lw)
        return
    h = (8 + op * 40) * (w / 50)
    d.ellipse((x - w / 2, y - h / 2, x + w / 2, y + h / 2), fill=(96, 22, 44))
    if op > 0.3:
        d.ellipse((x - w * 0.27, y + h * 0.08, x + w * 0.27, y + h / 2 - 1), fill=(236, 96, 112))


def blink_amt(t, off):
    p = (t + off) % 3.6
    return max(0.0, 1 - abs(p - 0.09) / 0.09) if p < 0.18 else 0.0


def star(d, x, y, r, fill):
    d.polygon([(x, y - r * 1.5), (x + r * 0.4, y - r * 0.4), (x + r * 1.5, y), (x + r * 0.4, y + r * 0.4),
               (x, y + r * 1.5), (x - r * 0.4, y + r * 0.4), (x - r * 1.5, y), (x - r * 0.4, y - r * 0.4)], fill=fill)


def face(frame, cx, cy, s, geo, headcol, look, t, blink_off, e, op, sleeping=False, tilt=0.0, cheeks=True):
    """Eyes, brows, mouth and acting extras for one head. (cx, cy) = head centre in world pixels."""
    (edx, edy, er), (mdx, mdy, mw) = geo
    c, sn = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))

    def R(dx, dy):
        return cx + (dx * c - dy * sn) * s, cy + (dx * sn + dy * c) * s

    r = er * s * e.get("eye", 1.0)
    bl = 1.0 if sleeping else max(blink_amt(t, blink_off), e.get("lid", 0.0))
    d = ImageDraw.Draw(frame)
    for sgn in (-1, 1):
        ex, ey = R(sgn * edx, edy)
        if e.get("arc") and not sleeping:
            d.arc((ex - r, ey - r * 0.25, ex + r, ey + r * 1.5), 200, 340, fill=(40, 30, 35), width=max(5, int(8 * s)))
        else:
            eye(frame, ex, ey, r, look + math.sin(t * 2.3 + sgn) * 0.12, bl, headcol)
        by = ey - r * 1.55 - e.get("lift", 0) * s
        w = r * 0.85
        ang = e.get("angry", 0.0)
        d.line((ex - sgn * w, by + ang * 14 * s, ex + sgn * w, by - ang * 14 * s), fill=(50, 35, 35), width=max(4, int(8 * s)))
        if cheeks:
            put(frame, sphere(22 * s, 14 * s, (255, 150, 150), 0.0, 0.0), *R(sgn * (edx + 28), edy + 40))
    if mw > 0:
        mx, my = R(mdx, mdy)
        mouth(frame, mx, my, mw * s, max(op, e.get("gape", 0.0)), e.get("smile", 0.2))
    if e.get("tear"):
        ex, ey = R(-edx, edy)
        put(frame, sphere(7 * s, 11 * s, (130, 200, 255), 0.7, 0.2), ex, ey + r * 1.15 + ((t * 70) % 55) * s * 0.7)
    if e.get("sweat"):
        sx, sy = R(edx * 1.9, edy - er * 2.3)
        put(frame, sphere(9 * s, 14 * s, (140, 205, 255), 0.7, 0.2), sx, sy + math.sin(t * 5) * 4 * s)
    if e.get("zzz"):
        for i in range(3):
            ph = (t * 0.7 + i * 0.33) % 1
            size = int((26 + i * 9) * s)
            d.text((cx + (120 + ph * 80) * s, cy - (80 + ph * 150) * s), "Z", font=latin_font(max(10, size)),
                   fill=(255, 255, 255), stroke_width=3, stroke_fill=(70, 70, 130))
    if e.get("sparkle"):
        for i, sgn in enumerate((-1, 1)):
            px, py = R(sgn * (edx + 78), edy - 60 - 18 * i)
            star(d, px, py, max(3, 12 * s * (0.55 + 0.45 * math.sin(t * 6 + i * 2))), (255, 236, 150))


# per-species face geometry relative to the head centre: ((eye dx, dy, r), (mouth dx, dy, width))
FACE = {"boy": ((50, -14, 31), (0, 60, 52)), "mouse": ((46, -12, 27), (0, 56, 44)),
        "rabbit": ((46, -10, 28), (0, 60, 44)), "lion": ((48, -22, 28), (0, 66, 40)),
        "fox": ((48, -16, 27), (0, 64, 46)), "crow": ((44, -12, 24), (0, 0, 0)),
        "tortoise": ((40, -8, 22), (0, 44, 44)), "snake": ((38, -12, 27), (0, 54, 64))}


def soft(rx, ry, color, alpha):
    key = ("soft", _q(rx), _q(ry), tuple(color), alpha)
    if key not in _SPR:
        img = sphere(rx, ry, color, 0.1, 0.0).copy()
        img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
        _SPR[key] = img
    return _SPR[key]


def draw_char(frame, ch, X, t, amp, speaking, look, enter, happy, emo, moving=0.0, lift=0.0, pose="idle", prop=None):
    """X = world x. All sizes use s = scale * K, so everything stays crisp when the camera zooms in."""
    sp, s = ch["species"], ch["scale"] * K
    e = EMO.get(emo, EMO["neutral"])
    sleeping = pose == "sleep"
    if sp == "snake":
        return draw_snake(frame, ch, X, t, amp, speaking, look, enter, happy, e, sleeping)
    drop = (1 - ease_bounce(enter)) * -950 * K
    run = 1.0 if pose == "run" else 0.0
    walk = math.sin(t * 11)
    bob = (-abs(math.sin(t * 9.5)) * 15 * speaking - happy * abs(math.sin(t * 7)) * 24) * s
    bob += -abs(walk) * 13 * s * moving - abs(math.sin(t * 16)) * 20 * s * run
    if sleeping:
        bob += math.sin(t * 2.0) * 3 * s
    hb = bob * 1.3 - math.sin(t * 9.5 + 0.8) * 4 * speaking + (18 * s if sleeping else 0)
    Y0 = GROUND + drop - lift * K
    X = X + math.sin(t * 1.3) * 4
    shadow(frame, X, GROUND + 8 * K, 150 * s, ease_out(enter) * (0.5 if lift else 1.0))
    fy = walk * 11 * s * moving + math.sin(t * 16) * 14 * s * run

    def part(x, y, rx, ry, col, off=0.0, ang=0, spec=0.45, rim=0.3):
        spr = sphere(rx * s, ry * s, col, spec, rim) if ang == 0 else rot_sphere(rx * s, ry * s, col, ang, spec, rim)
        put(frame, spr, X + x * s, Y0 + y * s + off)

    c = ch["body"]
    hx, hy, hrx, hry = 0, -410, 128, 120
    if run:  # dust puffs behind the feet
        for i in range(4):
            ph = (t * 3 + i * 0.25) % 1
            put(frame, soft(26 * s * (0.6 + ph), 18 * s * (0.6 + ph), (225, 205, 170), 0.5 * (1 - ph)),
                X - 90 * s - ph * 90 * s, Y0 - 10 * s - ph * 30 * s)
    # --- back parts
    if sp == "mouse":
        for i in range(10):
            part(140 + i * 13, -60 - i * 9 - math.sin(t * 3 + i * 0.5) * 3, 13 - i * 0.6, 13 - i * 0.6, (205, 150, 160), bob * 0.3)
    if sp == "fox":
        part(165, -135, 62, 118, c, bob * 0.5, ang=28)
        part(215, -225, 34, 34, (255, 245, 235), bob * 0.5)
    if sp == "lion":
        for i in range(9):
            part(150 + i * 12, -70 - i * 8, 12, 12, (150, 80, 30), bob * 0.3)
        part(238, -145, 30, 34, (150, 80, 30), bob * 0.3)
    if sp == "crow":
        part(-135, -190, 54, 125, (40, 42, 66), bob, ang=-22)
        part(135, -190, 54, 125, (40, 42, 66), bob, ang=22)
    arm = 18 + math.sin(t * 8) * 16 * speaking + happy * math.sin(t * 6) * 30
    if sp == "boy":
        part(-165, -230, 36, 100, ch["shirt"], bob, ang=int(arm / 4) * 4)
        part(165, -230, 36, 100, ch["shirt"], bob, ang=-int(arm / 4) * 4)
    # --- feet
    foot = {"crow": (240, 150, 40), "boy": (50, 40, 50), "tortoise": (120, 170, 80)}.get(sp, c)
    if sp == "tortoise":
        part(-150, -38, 62, 42, foot, fy)
        part(150, -38, 62, 42, foot, -fy)
    else:
        part(-70, -22, 56, 30, foot, fy)
        part(70, -22, 56, 30, foot, -fy)
    if sp == "boy":
        part(-52, -62, 30, 56, ch["skin"], 0)
        part(52, -62, 30, 56, ch["skin"], 0)
        part(0, -105, 125, 58, ch["shorts"], bob)
    # --- body
    if sp == "tortoise":
        part(0, -260, 218, 150, c, bob, spec=0.55)
        for (px, py) in ((0, -330), (-105, -270), (105, -270), (-52, -190), (52, -190)):
            part(px, py, 48, 34, ch["shell2"], bob, spec=0.35)
    else:
        part(0, -178, 138, 150, ch.get("shirt", c), bob)
        if "belly" in ch:
            part(0, -150, 90, 104, ch["belly"], bob, spec=0.2)
    # --- head
    if sp == "tortoise":
        hx, hy, hrx, hry = 0, -112, 88, 80
        head_col = ch["head"]
    else:
        head_col = ch.get("skin", c)
    if sp == "lion":
        part(0, -410, 192, 186, (160, 85, 30), hb, spec=0.2)
        part(-118, -540, 42, 42, c, hb)
        part(118, -540, 42, 42, c, hb)
    if sp == "mouse":
        for sgn in (-1, 1):
            part(sgn * 100, -505, 62, 62, c, hb)
            part(sgn * 100, -505, 38, 38, (255, 170, 190), hb, spec=0.1)
    if sp == "rabbit":
        droop = 10 if e.get("smile", 0) < -0.3 or sleeping else 0
        for sgn in (-1, 1):
            part(sgn * 56, -610 + math.sin(t * 3 + sgn) * 3 + droop, 34, 108, c, hb, ang=-sgn * (8 + droop * 1.4))
            part(sgn * 56, -605 + droop, 18, 84, (255, 175, 195), hb, ang=-sgn * (8 + droop * 1.4), spec=0.1)
    if sp == "fox":
        for sgn in (-1, 1):
            part(sgn * 92, -525, 42, 86, c, hb, ang=-sgn * 24)
            part(sgn * 92, -520, 22, 58, (70, 35, 30), hb, ang=-sgn * 24, spec=0.1)
    part(hx, hy, hrx, hry, head_col, hb)
    if sp == "boy":
        for sgn in (-1, 1):
            part(sgn * 126, -405, 26, 32, head_col, hb)
        part(0, -498, 124, 74, (40, 30, 38), hb, spec=0.55)
        part(-92, -455, 34, 44, (40, 30, 38), hb)
        part(92, -455, 34, 44, (40, 30, 38), hb)
    if sp == "lion":
        part(0, -362, 78, 56, (250, 228, 175), hb)
        part(0, -394, 26, 17, (85, 42, 32), hb, spec=0.7)
    if sp == "fox":
        part(0, -362, 74, 54, (255, 242, 228), hb)
        part(0, -393, 22, 15, (30, 25, 30), hb, spec=0.7)
        part(-106, -348, 34, 26, (255, 242, 228), hb)
        part(106, -348, 34, 26, (255, 242, 228), hb)
    if sp == "mouse":
        part(0, -388, 18, 15, (255, 130, 150), hb, spec=0.7)
    if sp == "rabbit":
        part(0, -390, 17, 13, (255, 130, 150), hb, spec=0.7)
    # --- face (acting)
    hxw, hyw = X + hx * s, Y0 + hy * s + hb
    tilt = math.sin(t * 1.7 + ch["blink"]) * 2.0 + math.sin(t * 5) * 3 * speaking + (-5 if e.get("smile", 0) < -0.3 else 0)
    op = amp if speaking > 0 else 0.0
    face(frame, hxw, hyw, s, FACE[sp], head_col, look, t, ch["blink"], e, op, sleeping, tilt, cheeks=(sp != "crow"))
    d = ImageDraw.Draw(frame)
    if sp == "crow":
        up_y = Y0 + (hy + 40) * s + hb
        o = max(op, e.get("gape", 0.0)) * 26 * s
        put(frame, sphere(46 * s, 20 * s, (225, 140, 30), 0.5), X, up_y + 8 * s + o)
        if o > 4:
            d.ellipse((X - 30 * s, up_y - 6 * s, X + 30 * s, up_y + 8 * s + o), fill=(110, 30, 40))
        put(frame, sphere(60 * s, 28 * s, (248, 175, 45), 0.6), X, up_y - 4 * s)
        if prop == "roti":
            put(frame, sphere(34 * s, 24 * s, (222, 170, 95), 0.5), X + 38 * s, up_y + 14 * s + o)
    if sp == "rabbit" and op < 0.2 and e.get("smile", 0) > 0.0:
        d.rounded_rectangle((X - 12 * s, hyw + 66 * s, X + 12 * s, hyw + 94 * s), 5, fill=(255, 255, 255))
    if sp in ("mouse", "rabbit", "fox"):
        for sgn in (-1, 1):
            for k in (-1, 0, 1):
                d.line((X + sgn * 52 * s, hyw + 30 * s + k * 8, X + sgn * 118 * s, hyw + (24 + k * 12) * s), fill=(60, 55, 60), width=3)
    if sp == "boy":  # hands in front
        for sgn in (-1, 1):
            a = math.radians(int(arm / 4) * 4) * sgn
            part(sgn * (165 + math.sin(a) * 96), -230 + (96 * math.cos(a)) * 0.9, 30, 30, ch["skin"], bob)


def draw_snake(frame, ch, X, t, amp, speaking, look, enter, happy, e, sleeping):
    s = ch["scale"] * K
    shadow(frame, X + 120 * s, GROUND + 10 * K, 250 * s, ease_out(enter))
    base = X + (1 - ease_out(enter)) * 700 * K
    N = 38
    pts, rads = [], []
    for i in range(N):
        u = i / (N - 1)
        r = (10 + 38 * min(1.0, u / 0.42)) * s
        if u > 0.82:
            r *= 1 - (u - 0.82) * 0.55
        if u < 0.6:
            v = u / 0.6
            x = base + (40 + 235 * (1 - v)) * s
            y = GROUND - r - 4 * K - (14 + 14 * math.sin(v * 9 - t * 3.2)) * s * (0.45 + 0.55 * (1 - v))
        else:
            w = (u - 0.6) / 0.4
            x = base + (40 - 76 * w + 26 * math.sin(w * 4.2 - t * 1.6) * w) * s
            y = GROUND - r - 18 * s - 405 * s * (w ** 1.05) * (0.8 if sleeping else 1.0) - happy * abs(math.sin(t * 7)) * 10
        pts.append((x, y))
        rads.append(r)
    for i, ((x, y), r) in enumerate(zip(pts, rads)):
        band = 0.78 if (i // 2) % 2 else 1.0
        put(frame, sphere(r, r, tuple(int(c * band) for c in ch["body"]), 0.5, 0.35), x, y)
    hx, hy = pts[-1]
    hy -= 18 * s
    hx += math.sin(t * 2.0) * 5 * K
    d = ImageDraw.Draw(frame)
    if (int(t * 1.4) % 3 == 0 and (t * 1.4) % 1 < 0.35) or amp > 0.3:
        tx, ty = hx - 10 * s, hy + 44 * s + amp * 18 * s
        d.line((tx, ty, tx - 30 * s, ty + 54 * s + math.sin(t * 20) * 4), fill=(235, 55, 85), width=int(8 * K))
        d.line((tx - 30 * s, ty + 54 * s, tx - 48 * s, ty + 78 * s), fill=(235, 55, 85), width=int(6 * K))
        d.line((tx - 30 * s, ty + 54 * s, tx - 12 * s, ty + 80 * s), fill=(235, 55, 85), width=int(6 * K))
    put(frame, sphere(78 * s, 62 * s, ch["head"], 0.55, 0.35), hx, hy)
    for sgn in (-1, 1):
        d.ellipse((hx + sgn * 12 * s - 8 * s - 4, hy + 16 * s, hx + sgn * 12 * s - 8 * s + 4, hy + 24 * s), fill=(60, 90, 50))
    tilt = math.sin(t * 1.7 + ch["blink"]) * 2.0
    face(frame, hx - 8 * s, hy, s, FACE["snake"], ch["head"], look, t, ch["blink"], e,
         amp if speaking > 0 else 0.0, sleeping, tilt, cheeks=False)


# --------------------------------------------------------------------- props
def draw_tree(frame, x, lift):
    """Big tree with a branch for the crow to stand on (world coords, x in screen px)."""
    wx, top = x * K, GROUND
    d = ImageDraw.Draw(frame)
    tx = wx + 230 * K
    d.polygon([(tx - 52 * K, top + 12 * K), (tx + 52 * K, top + 12 * K), (tx + 34 * K, top - 1000 * K), (tx - 34 * K, top - 1000 * K)],
              fill=(98, 66, 46))
    d.polygon([(tx - 12 * K, top + 12 * K), (tx + 14 * K, top + 12 * K), (tx + 6 * K, top - 1000 * K), (tx - 10 * K, top - 1000 * K)],
              fill=(122, 86, 60))
    by = top - lift * K + 6 * K
    if lift:
        d.rounded_rectangle((wx - 170 * K, by, tx + 20 * K, by + 40 * K), int(18 * K), fill=(104, 70, 48))
    for i, (dx, dy, r, col) in enumerate([(-60, -1020, 190, (58, 150, 70)), (110, -1100, 210, (72, 168, 80)),
                                           (300, -1010, 190, (50, 130, 62)), (40, -930, 160, (80, 176, 86)),
                                           (230, -940, 170, (60, 150, 72))]):
        put(frame, sphere(r * K, r * 0.9 * K, col, 0.25, 0.25), tx + dx * K, top + dy * K)


def draw_flag(frame, x, t):
    wx = x * K
    d = ImageDraw.Draw(frame)
    d.ellipse((wx - 150 * K, GROUND + 30 * K, wx + 150 * K, GROUND + 60 * K), fill=(245, 245, 245))
    d.rounded_rectangle((wx - 8 * K, GROUND - 520 * K, wx + 8 * K, GROUND + 40 * K), 6, fill=(240, 240, 240))
    wave = math.sin(t * 5) * 8 * K
    sq = 36 * K
    for i in range(6):
        for j in range(4):
            col = (20, 20, 25) if (i + j) % 2 == 0 else (250, 250, 250)
            x0 = wx + 8 * K + i * sq
            y0 = GROUND - 520 * K + j * sq + math.sin(t * 5 + i * 0.7) * 6 * K
            d.rectangle((x0, y0, x0 + sq, y0 + sq), fill=col)


def draw_net(frame, cx, cy, rx, ry, cut, t):
    d = ImageDraw.Draw(frame)
    rope, dark = (186, 140, 84), (90, 60, 30)
    lines = []
    for i in range(-4, 5):
        pts = [(cx + (i / 4) * rx * math.sqrt(max(0.0, 1 - (yy / ry) ** 2)), cy + yy) for yy in [(-1 + k / 14) * ry for k in range(15)]]
        lines.append((("v", i), pts))
    for j in range(-3, 4):
        yy = (j / 3) * ry * 0.96
        w = rx * math.sqrt(max(0.0, 1 - (yy / ry) ** 2))
        lines.append((("h", j), [(cx - w + k / 12 * 2 * w, cy + yy + math.sin(k) * 2) for k in range(13)]))
    for n, (key, pts) in enumerate(lines):
        if cut > ((n * 37) % 100) / 100.0 * 0.9 + 0.05:
            continue
        d.line(pts, fill=dark, width=int(11 * K))
    for n, (key, pts) in enumerate(lines):
        if cut > ((n * 37) % 100) / 100.0 * 0.9 + 0.05:
            continue
        d.line(pts, fill=rope, width=int(6 * K))


def draw_pond(frame, cx, t, lion=None, emo="surprised"):
    w, h = int(560 * K), int(250 * K)
    cy = GROUND + 20 * K
    patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(patch)
    pd.ellipse((0, 0, w - 1, h - 1), fill=(46, 104, 186, 255), outline=(190, 225, 255, 255), width=int(6 * K))
    pd.ellipse((int(w * 0.06), int(h * 0.1), int(w * 0.94), int(h * 0.9)), fill=(70, 138, 214, 255))
    if lion is not None:
        s = lion["scale"] * 0.52 * K
        refl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        parts = [(0, 0, 192, 186, (160, 85, 30)), (0, 0, 128, 120, lion["body"]), (0, 48, 78, 56, (250, 228, 175))]
        e = EMO.get(emo, EMO["neutral"])
        for dx, dy, rx, ry, col in parts:
            spr = sphere(rx * s, ry * s, col, 0.2, 0.1).transpose(Image.FLIP_TOP_BOTTOM)
            refl.paste(spr, (int(w / 2 + dx * s - spr.width / 2), int(h / 2 - dy * s - spr.height / 2)), spr)
        rd = ImageDraw.Draw(refl)
        for sgn in (-1, 1):
            ex, ey = w / 2 + sgn * 48 * s, h / 2 + 22 * s
            rd.ellipse((ex - 28 * s, ey - 28 * s, ex + 28 * s, ey + 28 * s), fill=(250, 250, 252))
            rd.ellipse((ex - 14 * s, ey - 14 * s, ex + 14 * s, ey + 14 * s), fill=(25, 22, 38))
            rd.line((ex - 34 * s * sgn, ey + 44 * s - e.get("angry", 0) * 10 * s, ex + 30 * s * sgn, ey + 44 * s + e.get("angry", 0) * 10 * s),
                    fill=(50, 35, 35), width=int(7 * s))
        rd.ellipse((w / 2 - 22 * s, h / 2 - 58 * s, w / 2 + 22 * s, h / 2 - 40 * s), fill=(85, 42, 32))
        a = refl.getchannel("A").point(lambda v: int(v * 0.75))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).ellipse((int(w * 0.04), int(h * 0.08), int(w * 0.96), int(h * 0.92)), fill=255)
        refl.putalpha(ImageChops.multiply(a, mask))
        patch.alpha_composite(refl)
    for i in range(3):
        ph = (t * 0.5 + i / 3) % 1
        rw, rh = w * (0.2 + 0.7 * ph), h * (0.2 + 0.7 * ph)
        pd.ellipse((w / 2 - rw / 2, h / 2 - rh / 2, w / 2 + rw / 2, h / 2 + rh / 2), outline=(215, 238, 255, int(200 * (1 - ph))), width=int(3 * K))
    frame.paste(patch, (int(cx * K - w / 2), int(cy - h / 2)), patch)


# --------------------------------------------------------------- atmosphere
def make_atmosphere(kind, seed):
    rng = random.Random(seed)
    P = SCENES[kind]
    tint = {"day": (255, 238, 190, 20), "sunset": (255, 150, 70, 38), "night": (40, 70, 170, 46)}[kind]
    vig = Image.new("RGBA", (W, H), tint)
    arr = np.zeros((H, W), np.float32)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dist = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H * 0.46) / (H / 2)) ** 2)
    arr = np.clip((dist - 0.55) / 0.75, 0, 1) ** 1.6 * 150
    base = np.zeros((H, W, 4), np.uint8)
    base[..., :3] = (0, 0, 8)
    base[..., 3] = arr.astype(np.uint8)
    vig.alpha_composite(Image.fromarray(base, "RGBA"))
    rays = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    sx = int(W * (0.15 + 0.7 * P["sunpos"]) * 0.8)
    col = {"day": (255, 250, 215, 38), "sunset": (255, 200, 140, 50), "night": (200, 220, 255, 26)}[kind]
    for i in range(6):
        a0 = -0.55 + i * 0.22
        rd.polygon([(sx, 330), (sx + int(a0 * 1500), H), (sx + int((a0 + 0.1) * 1500), H)], fill=col)
    rays = rays.filter(ImageFilter.GaussianBlur(26))
    bok = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for _ in range(7):
        r = rng.randint(70, 170)
        x, y = rng.randint(-50, W + 50), rng.randint(1250, 1900)
        c = rng.choice([(255, 240, 190), (190, 255, 200), (255, 210, 230)]) if kind != "night" else rng.choice([(150, 190, 255), (255, 240, 150)])
        ImageDraw.Draw(bok).ellipse((x - r, y - r, x + r, y + r), fill=c + (34,))
    bok = bok.filter(ImageFilter.GaussianBlur(18))
    leaves = Image.new("RGBA", (W, 760), (0, 0, 0, 0))
    gcols = [(30, 110, 50), (24, 90, 44), (44, 135, 60)]
    for side in (0, 1):
        for _ in range(7):
            rx, ry = rng.randint(60, 120), rng.randint(130, 230)
            x = rng.randint(-60, 230) if side == 0 else W - rng.randint(-60, 230)
            y = rng.randint(380, 800)
            spr = rot_sphere(rx, ry, rng.choice(gcols) if kind != "night" else (20, 70, 70), rng.randint(-60, 60), 0.2, 0.2)
            leaves.paste(spr, (int(x - spr.width / 2), int(y - spr.height / 2)), spr)
    leaves = leaves.filter(ImageFilter.GaussianBlur(9))
    motes = [(rng.random(), rng.random(), rng.uniform(0.4, 1.6), rng.randint(3, 9), rng.random() * 6.28) for _ in range(46)]
    return {"grade": vig, "rays": rays, "bokeh": bok, "leaves": leaves, "motes": motes, "kind": kind}


_GLOW = {}


def glow(r, color, a):
    k = (r, color, a)
    if k not in _GLOW:
        im = Image.new("RGBA", (r * 4, r * 4), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((r, r, 3 * r, 3 * r), fill=color + (a,))
        _GLOW[k] = im.filter(ImageFilter.GaussianBlur(r * 0.7))
    return _GLOW[k]


def apply_atmosphere(frame, atm, t):
    frame.paste(atm["rays"], (int(math.sin(t * 0.35) * 14), 0), atm["rays"])
    frame.paste(atm["bokeh"], (int(math.sin(t * 0.25) * 18), int(math.cos(t * 0.2) * 12)), atm["bokeh"])
    night = atm["kind"] == "night"
    for (x0, y0, sp, r, ph) in atm["motes"]:
        x = (x0 * W + t * sp * 18 + math.sin(t * 0.7 + ph) * 28) % W
        y = (y0 * H - t * sp * 12 + math.cos(t * 0.5 + ph) * 18) % H
        if night:
            a = int(90 + 140 * (0.5 + 0.5 * math.sin(t * 2.2 + ph * 3)))
            g = glow(r + 4, (255, 240, 130), min(255, a))
        else:
            g = glow(r, (255, 252, 225), 110)
        frame.paste(g, (int(x - g.width / 2), int(y - g.height / 2)), g)
    sway = int(math.sin(t * 1.3) * 10)
    frame.paste(atm["leaves"], (sway, H - 760), atm["leaves"])
    frame.paste(atm["grade"], (0, 0), atm["grade"])


def make_ambience(total, kind, rng):
    n = int(total * SR)
    out = np.zeros(n, np.float32)
    rg = np.random.default_rng(rng.randint(0, 10**6))
    # soft wind
    noise = rg.normal(0, 1, n).astype(np.float32)
    wind = np.convolve(noise, np.ones(900) / 900, mode="same") * (0.5 + 0.5 * np.sin(np.arange(n) / SR * 0.4))
    out += wind * 0.9
    tt = np.arange(int(0.16 * SR)) / SR
    if kind == "night":  # crickets
        for k in range(int(total * 5)):
            i0 = int(rg.uniform(0, max(0.1, total - 0.4)) * SR)
            f = rg.uniform(4200, 5200)
            for b in range(3):
                seg = np.sin(2 * np.pi * f * tt[:int(0.05 * SR)]) * np.exp(-30 * tt[:int(0.05 * SR)])
                j = i0 + b * int(0.07 * SR)
                m = min(len(seg), n - j)
                if m > 0:
                    out[j:j + m] += seg[:m] * 0.08
    else:  # birds
        for k in range(int(total * 0.7)):
            i0 = int(rg.uniform(0, max(0.1, total - 0.5)) * SR)
            f0 = rg.uniform(2300, 3400)
            for b in range(rg.integers(2, 5)):
                seg = np.sin(2 * np.pi * (f0 + 1400 * tt) * tt) * np.exp(-14 * tt)
                j = i0 + b * int(0.13 * SR)
                m = min(len(seg), n - j)
                if m > 0:
                    out[j:j + m] += seg[:m] * 0.07
    return out / max(1e-9, np.abs(out).max()) * 0.5


# ------------------------------------------------------------------- scenery
SCENES = {
    "day": dict(sky=((105, 190, 255), (220, 244, 255)), hill=(125, 190, 150), hill2=(96, 165, 122), glow=(255, 250, 220),
                ground=((125, 205, 95), (66, 148, 62)), trees=[(58, 150, 70), (80, 176, 82), (44, 124, 60)], sunpos=0.72),
    "sunset": dict(sky=((255, 120, 90), (255, 226, 160)), hill=(196, 128, 110), hill2=(150, 96, 96), glow=(255, 232, 170),
                   ground=((196, 176, 84), (128, 108, 50)), trees=[(122, 150, 60), (150, 170, 72), (96, 126, 52)], sunpos=0.3),
    "night": dict(sky=((8, 14, 48), (52, 62, 124)), hill=(40, 60, 112), hill2=(28, 44, 92), glow=(220, 230, 255),
                  ground=((52, 104, 92), (24, 62, 62)), trees=[(30, 82, 92), (42, 102, 102), (24, 66, 82)], sunpos=0.68),
}
PAD = 220


def make_scene(kind, seed):
    P, rng = SCENES[kind], random.Random(seed)
    BW = W + PAD
    k = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    sky = np.array(P["sky"][0], np.float32) * (1 - k) + np.array(P["sky"][1], np.float32) * k
    img = Image.fromarray(np.repeat(sky, BW, axis=1).astype(np.uint8)).convert("RGBA")
    # sun / moon glow
    glow = Image.new("RGBA", (BW, H), (0, 0, 0, 0))
    sx, sy = int(BW * P["sunpos"]), 430
    ImageDraw.Draw(glow).ellipse((sx - 230, sy - 230, sx + 230, sy + 230), fill=P["glow"] + (90,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))
    ImageDraw.Draw(img).ellipse((sx - 78, sy - 78, sx + 78, sy + 78), fill=P["glow"] + (255,))
    if kind == "night":
        d = ImageDraw.Draw(img)
        for _ in range(70):
            x, y = rng.randint(0, BW), rng.randint(0, 800)
            d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 255, 255, 220))
    cl = Image.new("RGBA", (BW, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cl)
    for _ in range(6):
        x, y = rng.randint(0, BW), rng.randint(520, 860)
        w = rng.randint(180, 340)
        cd.ellipse((x, y, x + w, y + w * 0.32), fill=(255, 255, 255, 120 if kind != "night" else 40))
    img.alpha_composite(cl.filter(ImageFilter.GaussianBlur(14)))

    def hills(col, base, amp, ph):
        layer = Image.new("RGBA", (BW, H), (0, 0, 0, 0))
        pts = [(x, base - amp * math.sin(x / 260 + ph) - amp * 0.5 * math.sin(x / 101 + ph * 2)) for x in range(0, BW + 40, 40)]
        ImageDraw.Draw(layer).polygon(pts + [(BW, H), (0, H)], fill=col + (255,))
        return layer.filter(ImageFilter.GaussianBlur(5))

    img.alpha_composite(hills(P["hill"], 930, 70, rng.random() * 6))
    img.alpha_composite(hills(P["hill2"], 1010, 50, rng.random() * 6))
    trees = Image.new("RGBA", (BW, H), (0, 0, 0, 0))
    x = 30
    while x < BW:
        th = rng.randint(190, 280)
        base_y = 1070
        ImageDraw.Draw(trees).rounded_rectangle((x - 14, base_y - th, x + 14, base_y), 8, fill=(92, 62, 44, 255))
        for _ in range(4):
            col = rng.choice(P["trees"])
            r = rng.randint(58, 92)
            put(trees, sphere(r, r * 0.9, col, 0.25, 0.25), x + rng.randint(-46, 46), base_y - th + rng.randint(-40, 30))
        x += rng.randint(120, 190)
    img.alpha_composite(trees.filter(ImageFilter.GaussianBlur(2.2)))
    # ground
    g0, g1 = np.array(P["ground"][0], np.float32), np.array(P["ground"][1], np.float32)
    gk = np.linspace(0, 1, H - 1040, dtype=np.float32)[:, None, None]
    gimg = Image.fromarray(np.repeat(g0 * (1 - gk) + g1 * gk, BW, axis=1).astype(np.uint8)).convert("RGBA")
    mask = Image.new("L", (BW, H - 1040), 0)
    ImageDraw.Draw(mask).polygon([(x, 18 + 12 * math.sin(x / 130)) for x in range(0, BW + 40, 40)] + [(BW, H), (0, H)], fill=255)
    img.paste(gimg, (0, 1040), mask)
    # soft light patches on ground
    lp = Image.new("RGBA", (BW, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lp)
    for _ in range(9):
        x, y = rng.randint(0, BW), rng.randint(1120, 1500)
        ld.ellipse((x, y, x + rng.randint(160, 320), y + rng.randint(30, 60)), fill=(255, 255, 220, 40))
    img.alpha_composite(lp.filter(ImageFilter.GaussianBlur(14)))
    back = img.convert("RGB")
    # foreground grass + flowers (drawn over the characters' feet)
    fh = 330
    fr = Image.new("RGBA", (BW, fh), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fr)
    gcols = [tuple(int(c) for c in g1 * m) for m in (0.8, 1.0, 1.25)]
    x = -10
    while x < BW + 20:
        hgt = rng.randint(50, 110)
        for b in range(5):
            bx = x + b * 9
            fd.polygon([(bx, fh), (bx + 9, fh), (bx + 4 + rng.randint(-14, 14), fh - hgt + rng.randint(-14, 10))],
                       fill=rng.choice(gcols) + (255,))
        if rng.random() < 0.12:
            put(fr, sphere(13, 13, rng.choice([(255, 90, 120), (255, 210, 70), (255, 255, 255)]), 0.4, 0.1), x + 20, fh - hgt - 10)
        x += rng.randint(26, 52)
    return back, fr


# ------------------------------------------------------------- caption bits
def render_chunk(words, active, solid, size=64, max_w=830):
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


def caption_card(solid):
    img = Image.new("RGBA", (960, 290), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, 959, 289), 52, fill=WHITE + (255,), outline=solid + (255,), width=9)
    return img


def ribbon(title, right_text, solid):
    img = Image.new("RGBA", (960, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 959, 149), 44, fill=WHITE + (255,), outline=solid + (255,), width=8)
    draw_mixed(d, (40, 75), title, 70, NAVY, anchor="lm")
    p = pill(right_text, 34, WHITE, solid + (255,), pad=26)
    img.alpha_composite(p, (960 - 34 - p.width, 75 - p.height // 2))
    return img


def confetti(d, since, seed, colors):
    if since > 0.9:
        return
    k = ease_out(since / 0.9)
    r = random.Random(seed)
    for _ in range(24):
        ang = r.uniform(0, 2 * math.pi)
        dist = r.uniform(200, 560) * k
        x, y = W / 2 + math.cos(ang) * dist, 760 + math.sin(ang) * dist * 0.85 + 90 * k * k
        sz = r.uniform(9, 20) * (1 - 0.7 * k)
        col = r.choice(colors)
        if r.random() < 0.5:
            d.ellipse((x - sz, y - sz, x + sz, y + sz), fill=col)
        else:
            d.polygon([(x, y - sz * 1.4), (x + sz, y), (x, y + sz * 1.4), (x - sz, y)], fill=col)


# ---------------------------------------------------------------- video build
SPECIES_LOOK = {
    "boy": dict(skin=(232, 176, 126), shirt=(70, 150, 230), shorts=(70, 80, 130)),
    "snake": dict(body=(70, 175, 80), head=(96, 196, 90)),
    "lion": dict(body=(228, 172, 72)),
    "mouse": dict(body=(168, 168, 182), belly=(238, 228, 228)),
    "rabbit": dict(body=(246, 240, 236), belly=(255, 252, 250)),
    "tortoise": dict(body=(72, 152, 82), shell2=(46, 112, 62), head=(160, 196, 96)),
    "crow": dict(body=(52, 54, 78), belly=(70, 74, 104)),
    "fox": dict(body=(234, 124, 42), belly=(255, 238, 220)),
}


def build_cast(story):
    chars = {}
    for i, cid in enumerate(story["cast"]):
        base = dict(CAST[cid])
        base.update(SPECIES_LOOK[base["species"]])
        if "body" not in base:
            base["body"] = base.get("skin", (200, 200, 200))
        base["blink"] = random.uniform(0, 3)
        base["slot"] = 640 if base["species"] == "snake" else SLOT[i]
        chars[cid] = base
    return chars


def stage_plan(story, segments, chars):
    """Per-segment staging (positions, poses, lifts, emotions, props) that persists from line to line."""
    st = STAGING.get(story["id"], {})
    order = story["cast"]
    cur = {"pos": {c: chars[c]["slot"] for c in order}, "pose": {c: "idle" for c in order},
           "lift": {c: 0 for c in order}, "emo": {c: "neutral" for c in order}, "props": {}}

    def merge(upd):
        for k in ("pos", "pose", "lift", "emo"):
            cur[k].update(upd.get(k, {}))
        if "props" in upd:
            cur["props"] = dict(upd["props"])

    merge(st.get("init", {}))
    plans = []
    for seg in segments:
        if seg["kind"] == "line":
            merge(st.get(seg["li"], {}))
        elif seg["kind"] in ("moral", "outro"):
            for c in order:
                cur["pose"][c] = "idle"
                cur["emo"][c] = "happy"
            cur["props"] = {k: v for k, v in cur["props"].items() if k != "net"}
        plans.append(copy.deepcopy(cur))
    return plans


def render_video(story, segments, starts, durs, envs, total, audio_path, out_path, story_no, drop_t):
    chars = build_cast(story)
    order, sid = story["cast"], story["id"]
    emos = EMOTIONS.get(sid, [])
    plans = stage_plan(story, segments, chars)
    WK, HK = int(W * K), int(H * K)
    back, front = make_scene(story["scene"], random.randint(0, 999))
    back_w = back.resize((int(back.width * K), int(back.height * K)), Image.BILINEAR)
    front_w = front.resize((int(front.width * K), int(front.height * K)), Image.BILINEAR)
    atm = make_atmosphere(story["scene"], random.randint(0, 999))
    ribbon_img = ribbon(story["title"], "కథ", (150, 90, 210))
    chip = pill(f"STORY #{story_no:02d}", 32, WHITE, (150, 90, 210, 255), pad=24)
    card = caption_card(NARRATOR["color"])
    title_big = Image.new("RGBA", (960, 230), (0, 0, 0, 0))
    td = ImageDraw.Draw(title_big)
    td.rounded_rectangle((0, 0, 959, 229), 60, fill=(255, 255, 255, 245), outline=(150, 90, 210, 255), width=10)
    draw_mixed(td, (480, 70), "ఈ రోజు కథ", 52, (150, 90, 210), anchor="mm")
    draw_mixed(td, (480, 150), story["title"], 80, NAVY, anchor="mm")
    sparkle = [WHITE, GOLD, (255, 150, 200), (150, 220, 255)]
    bounds = [x - 0.3 for x in starts] + [total]
    chunks = []
    for si, seg in enumerate(segments):
        words = seg["text"].split()
        weights = [len(w) + 1.5 for w in words]
        tot, acc, wst = sum(weights), 0.0, []
        for wt in weights:
            wst.append(starts[si] + durs[si] * acc / tot)
            acc += wt
        col = seg["spk"]["color"]
        for i in range(0, len(words), 3):
            cw_, ws = words[i:i + 3], wst[i:i + 3]
            end = wst[i + 3] if i + 3 < len(words) else starts[si] + durs[si] + 0.2
            chunks.append({"words": cw_, "starts": ws, "end": end, "col": col,
                           "imgs": [render_chunk(cw_, a, col) for a in range(len(cw_))]})
    name_chips = {id(seg): pill(seg["spk"]["name"], 36, WHITE, tuple(seg["spk"]["color"]) + (255,), pad=28) for seg in segments}
    cut_li = {"lion_mouse": 6}.get(sid)
    cut_start = next((starts[i] for i, sg in enumerate(segments) if sg.get("li") == cut_li and sg["kind"] == "line"), None)

    posn = {c: float(plans[0]["pos"][c]) for c in order}
    cam = {"cx": WK / 2, "cy": HK / 2, "z": 1.0, "ay": 0.5}
    dt = 1.0 / FPS

    log = open(OUT / "ffmpeg.log", "w")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-i", str(audio_path), "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
         "-shortest", "-movflags", "+faststart", str(out_path)], stdin=subprocess.PIPE, stderr=log)
    ci = 0
    for fi in range(int(total * FPS)):
        t = fi / FPS
        si = max([i for i, x in enumerate(starts) if t >= x - 0.3] or [0])
        seg, plan = segments[si], plans[si]
        since = max(0.0, t - bounds[si])
        speaker_id = seg["who"] if seg["who"] in chars else None
        emo_speaker = emos[seg["li"]] if (seg["kind"] == "line" and seg.get("li") is not None and seg["li"] < len(emos)) else "neutral"

        # --- character motion
        vel = {}
        for c in order:
            old = posn[c]
            posn[c] = old + (plan["pos"][c] - old) * (1 - math.exp(-dt * 2.4))
            vel[c] = abs(posn[c] - old) / dt

        # --- the world (scene + props + characters), drawn at K x resolution
        pan = 0.5 + 0.5 * math.sin(t * 0.22)
        px = int(pan * PAD * K * 0.6)
        world = back_w.crop((px, 0, px + WK, HK))
        props = plan["props"]
        if "tree" in props:
            draw_tree(world, props["tree"], plan["lift"].get("crow", 0))
        if "flag" in props:
            draw_flag(world, props["flag"], t)
        if "pond" in props:
            lion_c = chars.get("lion")
            lion_emo = emo_speaker if speaker_id == "lion" else plan["emo"].get("lion", "neutral")
            draw_pond(world, props["pond"], t, lion_c, lion_emo)
        speaking_ids = [speaker_id] if (speaker_id and seg["kind"] == "line" and t >= starts[si]) else []
        amp_arr = envs[si]
        fidx = int((t - starts[si]) * FPS)
        amp = float(amp_arr[fidx]) if 0 <= fidx < len(amp_arr) else 0.0
        happy_phase = seg["kind"] in ("moral", "outro") and t >= starts[si]
        for cid in sorted(order, key=lambda c: c == speaker_id):
            ch = chars[cid]
            idx = order.index(cid)
            other = order[1 - idx]
            look = 0.9 if posn[other] > posn[cid] else -0.9
            is_sp = cid in speaking_ids
            emo = emo_speaker if (cid == speaker_id and seg["kind"] == "line") else plan["emo"].get(cid, "neutral")
            moving = min(1.0, vel[cid] / 140.0) if plan["pose"].get(cid) != "run" else 0.0
            prop = "roti" if (cid == "crow" and story.get("drop_line") is not None and t < drop_t) else None
            draw_char(world, ch, posn[cid] * K, t + idx * 0.7, amp if is_sp else 0.0, 1.0 if is_sp else 0.0, look,
                      (t - 0.12 * idx) / 1.0, 0.7 if happy_phase else 0.0, emo, moving, plan["lift"].get(cid, 0),
                      plan["pose"].get(cid, "idle"), prop)
        if props.get("net") in chars:
            lc = chars[props["net"]]
            ls = lc["scale"] * K
            cut = 0.0
            if cut_start is not None and t >= cut_start:
                cut = min(1.0, (t - cut_start) / (durs[[i for i, sg in enumerate(segments) if sg.get("li") == cut_li][0]] + 0.4))
            draw_net(world, posn[props["net"]] * K, GROUND - 270 * ls, 235 * ls, 335 * ls, cut, t)
        if story.get("drop_line") is not None and "crow" in chars and "fox" in chars and t >= drop_t:
            cs, fs = chars["crow"]["scale"] * K, chars["fox"]["scale"] * K
            bx0, by0 = posn["crow"] * K + 38 * cs, GROUND - plan["lift"].get("crow", 0) * K - 360 * cs
            bx1, by1 = posn["fox"] * K, GROUND - 330 * fs
            k = min(1.0, (t - drop_t) / 1.0)
            put(world, sphere(34 * K, 24 * K, (222, 170, 95), 0.5), bx0 + (bx1 - bx0) * ease_out(k), by0 + (by1 - by0) * (k ** 2))
        pxf = int(pan * PAD * K * 0.95)
        fg = front_w.crop((pxf, 0, pxf + WK, front_w.height))
        world.paste(fg, (0, int((GY - 50) * K)), fg)

        # --- virtual camera: wide for narration, close-ups / medium shots for dialogue
        if speaker_id is None or seg["kind"] != "line":
            tg = {"z": 1.0 + 0.08 * min(1.0, since / 4), "cx": WK / 2, "cy": HK / 2, "ay": 0.5}
        else:
            ch = chars[speaker_id]
            cx_ = posn[speaker_id] * K - (30 * K if ch["species"] == "snake" else 0)
            head_y = GROUND - plan["lift"].get(speaker_id, 0) * K - (440 if ch["species"] == "snake" else 410) * ch["scale"] * K
            strong = emo_speaker in ("surprised", "scared", "angry", "sad", "laugh")
            if strong or len(seg["text"].split()) <= 7:
                tg = {"z": 1.7 + 0.1 * min(1.0, since / 3), "cx": cx_, "cy": head_y + 40 * ch["scale"] * K, "ay": 0.40}
            else:
                tg = {"z": 1.32 + 0.07 * min(1.0, since / 3), "cx": cx_, "cy": head_y + 130 * ch["scale"] * K, "ay": 0.44}
        f_ = 1 - math.exp(-4.0 * dt)
        for k_ in cam:
            cam[k_] += (tg[k_] - cam[k_]) * f_
        cwid, chei = WK / cam["z"], HK / cam["z"]
        cwid, chei = WK / max(1.0, cam["z"]), HK / max(1.0, cam["z"])
        left = min(max(cam["cx"] - 0.5 * cwid + math.sin(t * 0.9) * 5 * K, 0), WK - cwid)
        top = min(max(cam["cy"] - cam["ay"] * chei + math.cos(t * 0.7) * 4 * K, 0), HK - chei)
        frame = world.resize((W, H), Image.BILINEAR, box=(left, top, left + cwid, top + chei))
        apply_atmosphere(frame, atm, t)
        d = ImageDraw.Draw(frame)

        # --- UI
        seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
        for i in range(len(segments)):
            x0 = 60 + i * (seg_w + 8)
            d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(120, 110, 160))
            fill = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
            if fill > 0:
                d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * fill), 150), 4, fill=WHITE)
        frame.paste(chip, (60, 172), chip)
        frame.paste(ribbon_img, (60, 262), ribbon_img)
        if seg["kind"] == "intro":
            sc = max(0.05, ease_back(since / 0.5))
            tb = title_big.resize((int(820 * sc), int(196 * sc)), Image.BILINEAR)
            frame.paste(tb, (int(540 - tb.width / 2), int(820 - tb.height / 2)), tb)
        frame.paste(card, (60, 1300), card)
        nc = name_chips[id(seg)]
        frame.paste(nc, (90, 1300 - nc.height // 2), nc)
        while ci < len(chunks) - 1 and t >= chunks[ci]["end"]:
            ci += 1
        ch_ = chunks[ci]
        if ch_["starts"][0] - 0.05 <= t < ch_["end"]:
            ai = max(i for i, x in enumerate(ch_["starts"]) if t >= x - 0.05)
            img = ch_["imgs"][ai]
            frame.paste(img, ((W - img.width) // 2, 1300 + (290 - img.height) // 2 + 10), img)
        if seg["kind"] == "moral":
            confetti(d, since, 77, sparkle)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------- cinematic clip mode (AI clips)
def story_clips(story_id):
    d = CLIPS_DIR / story_id
    return sorted(p for p in d.glob("*") if p.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}) if d.exists() else []


def write_prompts():
    out = pathlib.Path("prompts")
    out.mkdir(exist_ok=True)
    for st in STORIES:
        shots = SHOTS.get(st["id"], [])
        lines = [f"# {st['title']}  (folder: clips/{st['id']}/)", "",
                 "Make one clip per shot, in an AI video tool (image-to-video or text-to-video), 9:16 vertical, ~5 seconds.",
                 f"Save them as clips/{st['id']}/shot1.mp4, shot2.mp4 ... in order.", "",
                 f"**Characters (paste into every prompt for consistency):** {CHARACTER_LOOKS.get(st['id'], '')}", "",
                 f"**Style (paste into every prompt):** {STYLE}", ""]
        for i, sh in enumerate(shots, 1):
            spoken = " / ".join(st["lines"][j][1] for j in sh["lines"])
            lines += [f"## Shot {i}", f"Prompt: {sh['scene']} {CHARACTER_LOOKS.get(st['id'], '')} {STYLE}",
                      f"(Voice in this shot: {spoken})", ""]
        (out / f"{st['id']}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote prompts/*.md for {len(STORIES)} stories")


class FrameReader:
    """Decodes a clip to 1080x1920 frames; holds the last frame if the voice runs longer than the clip."""

    def __init__(self, path):
        self.p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf",
             f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=W * H * 3 * 2)
        self.last = None

    def read(self):
        buf = self.p.stdout.read(W * H * 3)
        if len(buf) == W * H * 3:
            self.last = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1).copy()
        return self.last

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def shot_of_line(story, line_idx, n_clips):
    for k, sh in enumerate(SHOTS.get(story["id"], [])):
        if line_idx in sh["lines"]:
            return k % n_clips
    return min(n_clips - 1, line_idx * n_clips // max(1, len(story["lines"])))


def render_clip_video(story, segments, starts, durs, total, audio_path, out_path, clips):
    n = len(clips)
    seg_clip, li = [], 0
    for seg in segments:
        if seg["kind"] == "intro":
            seg_clip.append(0)
        elif seg["kind"] == "line":
            seg_clip.append(shot_of_line(story, li, n))
            li += 1
        else:
            seg_clip.append((n - 1) if n > 1 else 0)
    bounds = [x - 0.3 for x in starts] + [total]
    chunks = []
    for si, seg in enumerate(segments):
        words = seg["text"].split()
        weights = [len(w) + 1.5 for w in words]
        tot, acc, wst = sum(weights), 0.0, []
        for wt in weights:
            wst.append(starts[si] + durs[si] * acc / tot)
            acc += wt
        col = tuple(seg["spk"]["color"])
        for i in range(0, len(words), 3):
            cw, ws = words[i:i + 3], wst[i:i + 3]
            end = wst[i + 3] if i + 3 < len(words) else starts[si] + durs[si] + 0.2
            chunks.append({"starts": ws, "end": end, "imgs": [render_chunk(cw, a, col, size=74) for a in range(len(cw))]})
    card = Image.new("RGBA", (960, 300), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, 959, 299), 52, fill=(255, 255, 255, 235))
    chips = {id(sg): pill(sg["spk"]["name"], 34, WHITE, tuple(sg["spk"]["color"]) + (255,), pad=26) for sg in segments}
    vign = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vign)
    for y in range(0, 520, 4):
        vd.rectangle((0, y, W, y + 4), fill=(0, 0, 0, int(120 * (1 - y / 520))))
    log = open(OUT / "ffmpeg.log", "w")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio_path), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest",
         "-movflags", "+faststart", str(out_path)], stdin=subprocess.PIPE, stderr=log)
    reader, cur_clip, prev_frame, switch_t, ci = None, -1, None, 0.0, 0
    blank = Image.new("RGB", (W, H), (20, 20, 30))
    for fi in range(int(total * FPS)):
        t = fi / FPS
        si = max([i for i, x in enumerate(starts) if t >= x - 0.3] or [0])
        seg = segments[si]
        if seg_clip[si] != cur_clip:
            if reader:
                prev_frame = reader.last
                reader.close()
            reader, cur_clip, switch_t = FrameReader(clips[seg_clip[si] % n]), seg_clip[si], t
        frame = reader.read() or blank
        if prev_frame is not None and t - switch_t < 0.35:
            frame = Image.blend(prev_frame, frame, ease_out((t - switch_t) / 0.35))
        frame = frame.copy()
        frame.paste(vign, (0, 0), vign)
        d = ImageDraw.Draw(frame)
        seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
        for i in range(len(segments)):
            x0 = 60 + i * (seg_w + 8)
            d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(255, 255, 255, 60) if False else (150, 150, 160))
            f = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
            if f > 0:
                d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * f), 150), 4, fill=WHITE)
        frame.paste(card, (60, 250), card)
        nc = chips[id(seg)]
        frame.paste(nc, (90, 250 - nc.height // 2), nc)
        while ci < len(chunks) - 1 and t >= chunks[ci]["end"]:
            ci += 1
        c = chunks[ci]
        if c["starts"][0] - 0.05 <= t < c["end"]:
            ai = max(i for i, x in enumerate(c["starts"]) if t >= x - 0.05)
            im = c["imgs"][ai]
            frame.paste(im, ((W - im.width) // 2, 250 + (300 - im.height) // 2 + 8), im)
        proc.stdin.write(frame.tobytes())
    if reader:
        reader.close()
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------------------------ commands
def telegram_configured():
    return bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() and (os.getenv("TELEGRAM_CHAT_ID") or "").strip())


def telegram_send(video_path, title, caption):
    """Send the finished video + caption to your phone (post it yourself from the Instagram app)."""
    if not telegram_configured():
        return False
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN").strip(), os.getenv("TELEGRAM_CHAT_ID").strip()
    try:
        with open(video_path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendVideo",
                              data={"chat_id": chat, "supports_streaming": "true", "caption": f"📖 {title}"[:1000]},
                              files={"video": f}, timeout=300)
        ok = r.ok and r.json().get("ok")
        if not ok:
            print("Telegram video failed:", r.status_code, r.text[:300].replace(tok, "***"))
            return False
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": "Caption to copy:\n\n" + caption[:3800]}, timeout=60)
        print("Sent to Telegram.")
        return True
    except Exception as e:
        print("Telegram failed:", str(e).replace(tok, "***"))
        return False


def generate():
    OUT.mkdir(exist_ok=True)
    state = load_state()
    ig = recent_ig_titles(ig_user(), ig_token()) if ig_token() else []
    story = choose_story(state, ig)
    found = {st["id"]: len(story_clips(st["id"])) for st in STORIES}
    print("Clip files found per story:", json.dumps(found))
    if not story:
        if os.getenv("REQUIRE_CLIPS") == "1" and not any(found.values()):
            print("NO CLIPS FOUND. Add AI clips as clips/<story id>/shot1.mp4, shot2.mp4 ... (see the prompts/ folder), "
                  "or set the repository variable REQUIRE_CLIPS to 0 to allow the simple cartoon style. Skipping this run.")
        elif os.getenv("REQUIRE_CLIPS") == "1":
            print("Stories that have clips were already posted recently (cooldown). Add clips for another story. Skipping.")
        else:
            print("No unused story (all in cooldown). Add more in stories_data.py. Skipping.")
        set_output("has_post", "false")
        return
    print(f"Story: {story['title']} ({story['id']})")
    now = dt.datetime.now(IST)
    rng = random.Random(int(now.timestamp()))
    story_no = len(state["used"]) + 1

    segments = [{"kind": "intro", "who": "narrator", "spk": NARRATOR, "text": f"ఈ రోజు కథ, {story['title']}."}]
    drop_idx = None
    for i, (who, text) in enumerate(story["lines"]):
        segments.append({"kind": "line", "li": i, "who": who, "spk": NARRATOR if who == "narrator" else CAST[who], "text": text})
        if story.get("drop_line") == i:
            drop_idx = len(segments) - 1
    segments.append({"kind": "moral", "who": "narrator", "spk": NARRATOR, "text": f"నీతి: {story['moral']}"})
    segments.append({"kind": "outro", "who": "narrator", "spk": NARRATOR, "text": "ఈ కథ నచ్చితే ఫాలో అవ్వండి, మీ స్నేహితులకు షేర్ చేయండి."})

    voice, starts, durs, envs, total = make_voice(segments)
    drop_t = starts[drop_idx] + 0.6 if drop_idx is not None else 1e9
    music = get_music(total, rng)
    events = [(s - 0.38, "whoosh") for i, s in enumerate(starts) if i > 0 and segments[i]["kind"] in ("moral", "outro")]
    events += [(starts[0] + 0.05, "pop")] + ([(drop_t, "pop")] if drop_idx is not None else [])
    sfx = make_sfx(events, len(voice), rng)
    amb = make_ambience(total, story["scene"], rng)
    sfx[:len(amb)] += amb[:len(sfx)] * 0.6
    audio = OUT / "audio.wav"
    mix_audio(voice, music, sfx, audio)
    clips = story_clips(story["id"])
    if clips:
        print(f"Cinematic clip mode: {len(clips)} clips")
        render_clip_video(story, segments, starts, durs, total, audio, OUT / "reel.mp4", clips)
    else:
        render_video(story, segments, starts, durs, envs, total, audio, OUT / "reel.mp4", story_no, drop_t)

    tags = " ".join(rng.sample(HASHTAGS, 3) + [story.get("tag", "#story")] + ["#telugu"])
    caption = "\n".join([f"📖 {story['title']}", "", f"నీతి: {story['moral']}", "",
                         "మీకు ఈ కథ నచ్చిందా? కామెంట్ చేయండి 👇 ఫాలో అవ్వండి!", "", tags])[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps({"caption": caption, "video_name": name, "story_id": story["id"],
                                               "title": story["title"]}, ensure_ascii=False))
    print(f"Built out/reel.mp4 ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB, {total:.0f}s)")
    telegram_send(OUT / "reel.mp4", story["title"], caption)
    set_output("has_post", "true")
    set_output("video_name", name)


def diagnose():
    """Ask Meta why the token is refused and print facts (never the token itself)."""
    tok = ig_token()
    base = "https://graph.facebook.com"
    print("---- DIAGNOSTIC (safe to share, contains no token) ----")
    try:
        r = requests.get(f"{base}/debug_token", params=sign({"input_token": tok, "access_token": tok}), timeout=30)
        d = (r.json() or {}).get("data", {})
        keep = {k: d.get(k) for k in ("is_valid", "type", "app_id", "application", "scopes", "expires_at",
                                      "data_access_expires_at", "granular_scopes", "error")}
        print("TOKEN:", json.dumps(keep, ensure_ascii=False)[:1500])
        if not d:
            print("TOKEN raw:", r.status_code, r.text[:400])
    except Exception as e:
        print("TOKEN check failed:", e)
    for label, path in (("me", "me?fields=id,name"), ("instagram account", f"{ig_user()}?fields=id,username")):
        try:
            r = requests.get(f"{base}/{path}", params=sign({"access_token": tok}), timeout=30)
            print(f"CHECK {label}: {r.status_code} {r.text[:300]}")
        except Exception as e:
            print(f"CHECK {label} failed: {e}")
    print("---- END DIAGNOSTIC ----")


def api(method, path, **params):
    r = requests.request(method, f"{GRAPH}/{path}", data=sign(dict(params)), timeout=90)
    if not r.ok:
        hint = ""
        if "blocked" in r.text.lower() or '"code":190' in r.text.replace(" ", ""):
            hint = ("\nHINT: Instagram rejected the token/app itself (not the video). Check: 1) the secret has no trailing "
                    "newline/space, 2) the token in developers.facebook.com/tools/debug/accesstoken is Valid and has "
                    "instagram_content_publish, 3) the app's Alert Inbox for restrictions, 4) wait 24h if you posted a lot.")
        if hint:
            diagnose()
        raise RuntimeError(f"Graph API error {r.status_code}: {r.text}{hint}")
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
    user_id, token = ig_user(), ig_token()
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN secret is empty")
    if any(meta["title"] in t for t in recent_ig_titles(user_id, token)):
        print("This story was already posted recently. Skipping publish.")
        save_state(meta)
        return
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://raw.githubusercontent.com/{repo}/{os.getenv('MEDIA_BRANCH', 'media')}/{meta['video_name']}"
    wait_until_public(url)
    try:
        container = api("POST", f"{user_id}/media", media_type="REELS", video_url=url, caption=meta["caption"],
                        share_to_feed="true", access_token=token)["id"]
    except RuntimeError as e:
        if "blocked" in str(e).lower():
            print("::warning::Instagram is blocking this app/token (see DIAGNOSTIC above). Video was not posted by the bot.")
            if telegram_configured():
                print("The video was sent to Telegram instead; post it from the Instagram app.")
                save_state(meta)
                return
        raise
    for _ in range(60):
        st = requests.get(f"{GRAPH}/{container}", params=sign({"fields": "status_code,status", "access_token": token}),
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
    if cmd == "prompts":
        write_prompts()
        sys.exit(0)
    if cmd in ("generate", "all"):
        generate()
    if cmd in ("publish", "all"):
        publish()

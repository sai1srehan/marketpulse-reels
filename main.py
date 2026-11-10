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

from stories_data import CAST, NARRATOR, STORIES

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").strip().rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0").strip())


def ig_user():
    return (os.getenv("IG_USER_ID") or "me").strip()


def ig_token():
    return (os.getenv("IG_ACCESS_TOKEN") or "").strip()
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
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
        r = requests.get(f"{GRAPH}/{user_id}/media", params={"fields": "caption", "limit": 25,
                                                              "access_token": token}, timeout=30)
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
        return ai_story([s["title"] for s in STORIES] + [u.get("title", "") for u in state["used"]])
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
def eye(frame, x, y, r, look, blink, headcol):
    put(frame, sphere(r, r, (250, 250, 252), 0.35, 0.1), x, y)
    px, py = x + look * r * 0.34, y + r * 0.05
    put(frame, sphere(r * 0.56, r * 0.56, (25, 22, 38), 0.9, 0.0), px, py)
    d = ImageDraw.Draw(frame)
    hr = max(2, r * 0.15)
    d.ellipse((px - r * 0.2 - hr, py - r * 0.22 - hr, px - r * 0.2 + hr, py - r * 0.22 + hr), fill=(255, 255, 255))
    if blink > 0:
        h = max(2, r * 2.1 * blink)
        d.ellipse((x - r * 1.08, y - r * 1.08, x + r * 1.08, y - r * 1.08 + h), fill=headcol)
        if blink > 0.6:
            d.line((x - r * 0.9, y + r * 0.1, x + r * 0.9, y + r * 0.1), fill=(40, 30, 35), width=5)


def mouth(frame, x, y, w, op):
    d = ImageDraw.Draw(frame)
    if op < 0.12:
        d.arc((x - w / 2, y - w * 0.32, x + w / 2, y + w * 0.32), 15, 165, fill=(95, 35, 45), width=7)
        return
    h = 8 + op * 40
    d.ellipse((x - w / 2, y - h / 2, x + w / 2, y + h / 2), fill=(96, 22, 44))
    if op > 0.3:
        d.ellipse((x - w * 0.27, y + h * 0.08, x + w * 0.27, y + h / 2 - 1), fill=(236, 96, 112))


def blink_amt(t, off):
    p = (t + off) % 3.6
    return max(0.0, 1 - abs(p - 0.09) / 0.09) if p < 0.18 else 0.0


# per-species face geometry relative to head centre: eye(dx, dy, r), mouth(dy, w)
FACE = {"boy": ((50, -14, 31), (60, 52)), "mouse": ((46, -12, 27), (56, 44)), "rabbit": ((46, -10, 28), (60, 44)),
        "lion": ((48, -22, 28), (66, 40)), "fox": ((48, -16, 27), (64, 46)), "crow": ((44, -12, 24), (0, 0)),
        "tortoise": ((40, -8, 22), (44, 44))}


def draw_char(frame, ch, cx, t, spk_amp, speaking, look, enter, happy, prop=None):
    sp, s = ch["species"], ch["scale"]
    if sp == "snake":
        return draw_snake(frame, ch, cx, t, spk_amp, speaking, look, enter, happy)
    drop = (1 - ease_bounce(enter)) * -950
    bob = (-abs(math.sin(t * 9.5)) * 15 * speaking - happy * abs(math.sin(t * 7)) * 24) * s
    hb = bob * 1.3 - math.sin(t * 9.5 + 0.8) * 4 * speaking
    X, Y0 = cx + math.sin(t * 1.3) * 4, GY + drop
    shadow(frame, cx, GY + 8, 150 * s, ease_out(enter))

    def part(x, y, rx, ry, col, off=0.0, ang=0, spec=0.45, rim=0.3):
        spr = sphere(rx * s, ry * s, col, spec, rim) if ang == 0 else rot_sphere(rx * s, ry * s, col, ang, spec, rim)
        put(frame, spr, X + x * s, Y0 + y * s + off)

    c = ch["body"]
    hx, hy, hrx, hry = 0, -410, 128, 120
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
    if sp == "boy":
        arm = 18 + math.sin(t * 8) * 16 * speaking + happy * math.sin(t * 6) * 30
        part(-165, -230, 36, 100, ch["shirt"], bob, ang=int(arm / 4) * 4)
        part(165, -230, 36, 100, ch["shirt"], bob, ang=-int(arm / 4) * 4)
    # --- feet
    foot = {"crow": (240, 150, 40), "boy": (50, 40, 50), "tortoise": (120, 170, 80)}.get(sp, c)
    if sp == "tortoise":
        part(-150, -38, 62, 42, foot, 0)
        part(150, -38, 62, 42, foot, 0)
    else:
        part(-70, -22, 56, 30, foot, 0)
        part(70, -22, 56, 30, foot, 0)
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
        for sgn in (-1, 1):
            part(sgn * 56, -610 + math.sin(t * 3 + sgn) * 3, 34, 108, c, hb, ang=-sgn * 8)
            part(sgn * 56, -605, 18, 84, (255, 175, 195), hb, ang=-sgn * 8, spec=0.1)
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
    # --- face
    (edx, edy, er), (mdy, mw) = FACE[sp]
    ex, ey = X + hx * s, Y0 + (hy + edy) * s + hb
    bl = blink_amt(t, ch["blink"])
    for sgn in (-1, 1):
        eye(frame, ex + sgn * edx * s, ey, er * s, look, bl, head_col)
    d = ImageDraw.Draw(frame)
    for sgn in (-1, 1):  # eyebrows
        lift = spk_amp * 8 * s
        bx, by = ex + sgn * edx * s, ey - er * s * 1.55 - lift
        d.line((bx - er * s * 0.8, by + sgn * 3, bx + er * s * 0.8, by - sgn * 3), fill=(50, 35, 35), width=max(4, int(8 * s)))
    if sp == "crow":
        up_y = Y0 + (hy + 40) * s + hb
        op = spk_amp * 26 * s
        put(frame, sphere(46 * s, 20 * s, (225, 140, 30), 0.5), X, up_y + 8 * s + op)
        d.ellipse((X - 30 * s, up_y - 6 * s, X + 30 * s, up_y + 8 * s + op), fill=(110, 30, 40)) if op > 4 else None
        put(frame, sphere(60 * s, 28 * s, (248, 175, 45), 0.6), X, up_y - 4 * s)
        if prop == "roti":
            put(frame, sphere(34 * s, 24 * s, (222, 170, 95), 0.5), X + 38 * s, up_y + 14 * s + op)
    else:
        mouth(frame, X + hx * s, Y0 + (hy + mdy) * s + hb, mw * s, spk_amp if speaking > 0 else 0.0 + happy * 0.0)
        if sp == "rabbit" and spk_amp < 0.2:
            d.rounded_rectangle((X - 12 * s, Y0 + (hy + mdy + 6) * s + hb, X + 12 * s, Y0 + (hy + mdy + 34) * s + hb), 5, fill=(255, 255, 255))
        if sp in ("mouse", "rabbit", "fox"):
            for sgn in (-1, 1):
                for k in (-1, 0, 1):
                    d.line((X + sgn * 52 * s, Y0 + (hy + 30) * s + hb + k * 8, X + sgn * 118 * s, Y0 + (hy + 24 + k * 12) * s + hb), fill=(60, 55, 60), width=3)
    if sp in ("boy", "rabbit", "mouse", "fox", "lion", "tortoise"):  # cheeks
        for sgn in (-1, 1):
            put(frame, sphere(22 * s, 14 * s, (255, 150, 150), 0.0, 0.0), X + sgn * (edx + 28) * s, ey + 40 * s)
    if sp == "boy":  # hands in front
        arm = 18 + math.sin(t * 8) * 16 * speaking + happy * math.sin(t * 6) * 30
        for sgn in (-1, 1):
            a = math.radians(int(arm / 4) * 4) * sgn
            part(sgn * (165 + math.sin(a) * 96) * 1.0, -230 + (96 * math.cos(a)) * 0.9, 30, 30, ch["skin"], bob)


def draw_snake(frame, ch, cx, t, amp, speaking, look, enter, happy):
    s = ch["scale"]
    shadow(frame, cx + 120, GY + 10, 250, ease_out(enter))
    slide = (1 - ease_out(enter)) * 700
    base = cx + slide
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
            y = GY - r - 4 - (14 + 14 * math.sin(v * 9 - t * 3.2)) * s * (0.45 + 0.55 * (1 - v))
        else:
            w = (u - 0.6) / 0.4
            x = base + (40 - 76 * w + 26 * math.sin(w * 4.2 - t * 1.6) * w) * s
            y = GY - r - 18 * s - 405 * s * (w ** 1.05) - happy * abs(math.sin(t * 7)) * 10
        pts.append((x, y))
        rads.append(r)
    for i, ((x, y), r) in enumerate(zip(pts, rads)):
        band = 0.78 if (i // 2) % 2 else 1.0
        base_col = tuple(int(c * band) for c in ch["body"])
        put(frame, sphere(r, r, base_col, 0.5, 0.35), x, y)
    hx, hy = pts[-1]
    hy -= 18 * s
    sway = math.sin(t * 2.0) * 5
    hx += sway
    hrx, hry = 78 * s, 62 * s
    # tongue
    if int(t * 1.4) % 3 == 0 and (t * 1.4) % 1 < 0.35 or amp > 0.3:
        d = ImageDraw.Draw(frame)
        tx, ty = hx - 10 * s, hy + 44 * s + amp * 18 * s
        d.line((tx, ty, tx - 30 * s, ty + 54 * s + math.sin(t * 20) * 4), fill=(235, 55, 85), width=8)
        d.line((tx - 30 * s, ty + 54 * s, tx - 48 * s, ty + 78 * s), fill=(235, 55, 85), width=6)
        d.line((tx - 30 * s, ty + 54 * s, tx - 12 * s, ty + 80 * s), fill=(235, 55, 85), width=6)
    put(frame, sphere(hrx, hry, ch["head"], 0.55, 0.35), hx, hy)
    bl = blink_amt(t, ch["blink"])
    for sgn in (-1, 1):
        eye(frame, hx + sgn * 38 * s - 8 * s, hy - 12 * s, 27 * s, look, bl, ch["head"])
    d = ImageDraw.Draw(frame)
    for sgn in (-1, 1):
        d.ellipse((hx + sgn * 12 * s - 8 * s - 4, hy + 16 * s, hx + sgn * 12 * s - 8 * s + 4, hy + 24 * s), fill=(60, 90, 50))
    mouth(frame, hx - 8 * s, hy + 42 * s, 64 * s, amp if speaking > 0 else 0.0)


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


def render_video(story, segments, starts, durs, envs, total, audio_path, out_path, story_no, drop_t):
    chars = build_cast(story)
    order = story["cast"]
    solid = NARRATOR["color"]
    back, front = make_scene(story["scene"], hash(story["id"]) % 1000)
    ribbon_img = ribbon(story["title"], "కథ", (150, 90, 210))
    chip = pill(f"STORY #{story_no:02d}", 32, WHITE, (150, 90, 210, 255), pad=24)
    card = caption_card(solid)
    title_big = Image.new("RGBA", (960, 230), (0, 0, 0, 0))
    td = ImageDraw.Draw(title_big)
    td.rounded_rectangle((0, 0, 959, 229), 60, fill=(255, 255, 255, 245), outline=(150, 90, 210, 255), width=10)
    draw_mixed(td, (480, 70), "ఈ రోజు కథ", 52, (150, 90, 210), anchor="mm")
    draw_mixed(td, (480, 150), story["title"], 80, NAVY, anchor="mm")
    sparkle = [WHITE, GOLD, (255, 150, 200), (150, 220, 255)]
    chunk_cache = {}
    bounds = [x - 0.3 for x in starts] + [total]
    # chunks per segment
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
            cw, ws = words[i:i + 3], wst[i:i + 3]
            end = wst[i + 3] if i + 3 < len(words) else starts[si] + durs[si] + 0.2
            chunks.append({"words": cw, "starts": ws, "end": end, "col": col,
                           "imgs": [render_chunk(cw, a, col) for a in range(len(cw))]})
    name_chips = {id(seg): pill(seg["spk"]["name"], 36, WHITE, tuple(seg["spk"]["color"]) + (255,), pad=28) for seg in segments}

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
        seg = segments[si]
        since = t - bounds[si]
        pan = 0.5 + 0.5 * math.sin(t * 0.22)
        frame = back.crop((int(pan * PAD * 0.6), 0, int(pan * PAD * 0.6) + W, H)).copy()
        d = ImageDraw.Draw(frame)

        # header
        seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
        for i in range(len(segments)):
            x0 = 60 + i * (seg_w + 8)
            d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(255, 255, 255, 90) if False else (120, 110, 160))
            fill = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
            if fill > 0:
                d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * fill), 150), 4, fill=WHITE)
        frame.paste(chip, (60, 172), chip)
        frame.paste(ribbon_img, (60, 262), ribbon_img)

        # characters (listener first, speaker on top)
        speaker_id = seg["who"] if seg["who"] in chars else None
        draw_order = sorted(order, key=lambda c: c == speaker_id)
        amp_arr = envs[si]
        fidx = int((t - starts[si]) * FPS)
        amp = float(amp_arr[fidx]) if 0 <= fidx < len(amp_arr) else 0.0
        happy_phase = seg["kind"] in ("moral", "outro")
        for cid in draw_order:
            ch = chars[cid]
            idx = order.index(cid)
            other = chars[order[1 - idx]]
            look = 0.9 if other["slot"] > ch["slot"] else -0.9
            is_sp = cid == speaker_id and seg["kind"] == "line" and t >= starts[si]
            enter = (t - 0.12 * idx) / 1.0
            prop = "roti" if (cid == "crow" and story.get("drop_line") is not None and t < drop_t) else None
            draw_char(frame, ch, ch["slot"], t + idx * 0.7, amp if is_sp else 0.0, 1.0 if is_sp else 0.0,
                      look, enter, 0.7 if happy_phase and t >= starts[si] else 0.0, prop)
        # falling roti (crow & fox story)
        if story.get("drop_line") is not None and drop_t <= t < drop_t + 1.0 and "crow" in chars:
            k = (t - drop_t) / 1.0
            cx0 = chars["crow"]["slot"]
            fx = cx0 + (chars["fox"]["slot"] - cx0) * 0.5 * ease_out(k) if "fox" in chars else cx0
            put(frame, sphere(34, 24, (222, 170, 95), 0.5), fx + 38, GY - 400 + 560 * (k ** 2))
        elif story.get("drop_line") is not None and t >= drop_t + 1.0 and "fox" in chars:
            fx = (chars["crow"]["slot"] + chars["fox"]["slot"]) / 2
            put(frame, sphere(34, 24, (222, 170, 95), 0.5), fx + 38, GY - 40)

        # foreground grass
        px = int(pan * PAD * 0.95)
        fg = front.crop((px, 0, px + W, front.height))
        frame.paste(fg, (0, GY - 50), fg)

        # big title in the first segment
        if seg["kind"] == "intro":
            sc = max(0.05, ease_back(since / 0.5))
            tb = title_big.resize((int(820 * sc), int(196 * sc)), Image.BILINEAR)
            frame.paste(tb, (int(540 - tb.width / 2), int(500 - tb.height / 2)), tb)

        # caption + speaker chip
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
        if si > 0 and since < 0.35:
            pass
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------------------------ commands
def generate():
    OUT.mkdir(exist_ok=True)
    state = load_state()
    ig = recent_ig_titles(ig_user(), ig_token()) if ig_token() else []
    story = choose_story(state, ig)
    if not story:
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
        segments.append({"kind": "line", "who": who, "spk": NARRATOR if who == "narrator" else CAST[who], "text": text})
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
    audio = OUT / "audio.wav"
    mix_audio(voice, music, sfx, audio)
    render_video(story, segments, starts, durs, envs, total, audio, OUT / "reel.mp4", story_no, drop_t)

    tags = " ".join(rng.sample(HASHTAGS, 3) + [story.get("tag", "#story")] + ["#telugu"])
    caption = "\n".join([f"📖 {story['title']}", "", f"నీతి: {story['moral']}", "",
                         "మీకు ఈ కథ నచ్చిందా? కామెంట్ చేయండి 👇 ఫాలో అవ్వండి!", "", tags])[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps({"caption": caption, "video_name": name, "story_id": story["id"],
                                               "title": story["title"]}, ensure_ascii=False))
    print(f"Built out/reel.mp4 ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB, {total:.0f}s)")
    set_output("has_post", "true")
    set_output("video_name", name)


def api(method, path, **params):
    r = requests.request(method, f"{GRAPH}/{path}", data=params, timeout=90)
    if not r.ok:
        hint = ""
        if "blocked" in r.text.lower() or '"code":190' in r.text.replace(" ", ""):
            hint = ("\nHINT: Instagram rejected the token/app itself (not the video). Check: 1) the secret has no trailing "
                    "newline/space, 2) the token in developers.facebook.com/tools/debug/accesstoken is Valid and has "
                    "instagram_content_publish, 3) the app's Alert Inbox for restrictions, 4) wait 24h if you posted a lot.")
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
    container = api("POST", f"{user_id}/media", media_type="REELS", video_url=url, caption=meta["caption"],
                    share_to_feed="true", access_token=token)["id"]
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

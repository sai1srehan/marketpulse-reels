"""
Telugu stock-news Reel bot (trending style).

Latest Telugu stock-market headlines -> Telugu voiceover (TTS) + animated
word-by-word captions + moving background + live Nifty line + music
-> Instagram Reel (official Graph API).

    python main.py generate | publish | all

Secrets / env:
    IG_ACCESS_TOKEN, IG_USER_ID, IG_GRAPH_HOST   Instagram API
    ANTHROPIC_API_KEY   optional: rewrites headlines into short spoken Telugu
    TTS_VOICE           optional: te-IN-ShrutiNeural (female, default) or te-IN-MohanNeural
    DRY_RUN=1           build everything but do not post
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
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, features

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0"))
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
W, H, FPS, SR = 1080, 1920, 30, 44100
NEWS_PER_VIDEO = 3

NEWS_QUERIES = [
    "స్టాక్ మార్కెట్ OR సెన్సెక్స్ OR నిఫ్టీ when:1d",
    "షేర్ మార్కెట్ OR బ్యాంక్ నిఫ్టీ OR ఐపీఓ when:1d",
]
TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")

WHITE, GREY, GOLD = (255, 255, 255), (190, 198, 214), (255, 214, 102)
GREEN, RED = (34, 197, 94), (239, 68, 68)
PALETTES = [  # (dark corner, light corner, glow blobs)
    ((8, 14, 40), (70, 20, 120), (120, 70, 220)),
    ((4, 30, 40), (10, 100, 100), (30, 200, 170)),
    ((30, 8, 40), (150, 30, 80), (255, 90, 120)),
    ((8, 16, 36), (20, 70, 160), (60, 140, 255)),
    ((30, 14, 8), (160, 80, 20), (255, 170, 60)),
]

DISCLAIMER_EN = "Educational content only. Not investment advice. I am not a SEBI registered adviser."
DISCLAIMER_TE = "ఇది పెట్టుబడి సలహా కాదు. కేవలం సమాచారం కోసమే."
INTRO_TE = "ఈ రోజు స్టాక్ మార్కెట్ ముఖ్యాంశాలు ఇవే."
OUTRO_TE = "మరిన్ని అప్‌డేట్స్ కోసం ఫాలో అవ్వండి. ఇది పెట్టుబడి సలహా కాదు."

# Instagram now allows max 5 hashtags: 2 anchors + 3 rotating
ANCHOR_TAGS = ["#stockmarkettelugu", "#sharemarket"]
ROTATING_TAGS = ["#nifty50", "#sensex", "#telugunews", "#telugufinance",
                 "#investingtelugu", "#hyderabad", "#telugureels", "#telangana"]


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


def load_posted():
    try:
        return json.loads(STATE.read_text())["posted"]
    except Exception:
        return []


def save_posted(keys):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"posted": (load_posted() + keys)[-400:]}, indent=1))


# --------------------------------------------------------------------- fonts
_font_cache = {}


def find_te_font():
    paths = [
        pathlib.Path("fonts/NotoSansTelugu-Bold.ttf"),
        pathlib.Path("/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf"),
    ]
    for p in paths:
        if p.exists():
            return str(p)
    print("Telugu font missing, trying to install fonts-noto-core ...")
    subprocess.run(["sudo", "-n", "apt-get", "install", "-y", "-qq", "fonts-noto-core"],
                   capture_output=True)
    for p in paths[1:]:
        if p.exists():
            return str(p)
    raise RuntimeError("Telugu font not found. Add fonts/NotoSansTelugu-Bold.ttf to the repo.")


def te_font(size):
    key = ("te", size)
    if key not in _font_cache:
        kw = {"layout_engine": ImageFont.Layout.RAQM} if features.check("raqm") else {}
        if not kw:
            print("WARNING: raqm missing, Telugu conjuncts may render incorrectly")
        _font_cache[key] = ImageFont.truetype(find_te_font(), size, **kw)
    return _font_cache[key]


def latin_font(size):
    key = ("la", size)
    if key not in _font_cache:
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
            try:
                _font_cache[key] = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
        else:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def mixed_font(text, size):
    return latin_font(size) if text.isascii() else te_font(size)


# --------------------------------------------------------------------- news
def fetch_news():
    items, seen = [], set()
    for q in NEWS_QUERIES:
        try:
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": q, "hl": "te", "gl": "IN", "ceid": "IN:te"},
                             timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"Feed failed: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            source = (it.findtext("source") or "").strip()
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)]
            elif " - " in title and not source:
                title, source = title.rsplit(" - ", 1)
            title = re.split(r"\s*[|｜]\s*", title)[0].strip()
            if not TELUGU_RE.search(title) or len(title) < 15:
                continue
            norm = re.sub(r"\W+", " ", title.lower()).strip()
            key = hashlib.md5(norm.encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                pub = dt.datetime.now(dt.timezone.utc)
            items.append({"title": title, "source": source or "News", "key": key, "pub": pub})
    items.sort(key=lambda n: n["pub"], reverse=True)
    return items


def trim_words(text, n=20):
    w = text.split()
    return " ".join(w[:n]) + ("." if len(w) > n else "")


def rewrite_te(news):
    """Short spoken Telugu lines. Falls back to trimmed headlines."""
    fallback = [trim_words(n["title"]) for n in news]
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return fallback
    prompt = (
        "Rewrite each Telugu stock-market headline below as ONE short, natural "
        "spoken Telugu sentence (max 16 words) for a voiceover. Use only facts "
        "in the headline. No advice, no predictions, no opinions. Return ONLY a "
        "JSON array of Telugu strings in the same order.\n" + json.dumps(fallback, ensure_ascii=False)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 900,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        r.raise_for_status()
        text = re.sub(r"^```(?:json)?|```$", "", r.json()["content"][0]["text"].strip(), flags=re.M).strip()
        lines = json.loads(text)
        if isinstance(lines, list) and len(lines) == len(news) and all(TELUGU_RE.search(str(x)) for x in lines):
            return [str(x).strip() for x in lines]
    except Exception as e:
        print(f"Rewrite skipped: {e}")
    return fallback


def index_moves():
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    for name, sym in {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN"}.items():
        try:
            s = yf.Ticker(sym).history(period="5d", interval="1d")["Close"].dropna()
            if len(s) >= 2:
                out[name] = (float(s.iloc[-1]), (float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100)
        except Exception as e:
            print(f"Index {name} failed: {e}")
    return out


def nifty_series():
    try:
        import yfinance as yf
        s = yf.Ticker("^NSEI").history(period="5d", interval="30m")["Close"].dropna()
        vals = [float(x) for x in s.values][-60:]
        return vals if len(vals) >= 10 else None
    except Exception as e:
        print(f"Nifty series failed: {e}")
        return None


# ----------------------------------------------------------------- voiceover
def tts(text, path):
    voice = os.getenv("TTS_VOICE", "te-IN-ShrutiNeural")
    try:
        import edge_tts
        asyncio.run(edge_tts.Communicate(text, voice, rate="+4%").save(str(path)))
        if path.exists() and path.stat().st_size > 1000:
            return
    except Exception as e:
        print(f"edge-tts failed ({e}); falling back to gTTS")
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
    """Create each segment's speech, return (voice array, start times, durations)."""
    GAP = 0.35
    clips, starts, durs, t = [], [], [], 0.25
    for i, seg in enumerate(segments):
        mp3, wav = OUT / f"seg_{i}.mp3", OUT / f"seg_{i}.wav"
        tts(seg["text"], mp3)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", str(SR),
             "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        data = load_wav_trim(wav)
        clips.append(data)
        starts.append(t)
        durs.append(len(data) / SR)
        t += len(data) / SR + GAP
    total = t + 0.6
    voice = np.zeros(int(total * SR), np.float32)
    for s, d in zip(starts, clips):
        i0 = int(s * SR)
        voice[i0:i0 + len(d)] += d
    voice = voice / max(1e-9, np.abs(voice).max()) * 0.9
    return voice, starts, durs, total


# --------------------------------------------------------------------- music
def synth_music(seconds, rng):
    """Original Indian-flavoured track: tanpura drone + flute melody + tabla-like beat."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    sa = rng.choice([220.0, 233.08, 246.94])
    scale = [1, 16 / 15, 5 / 4, 4 / 3, 3 / 2, 8 / 5, 15 / 8, 2]  # Mayamalavagowla
    out = np.zeros(n)
    # drone (Sa + Pa) with slow shimmer
    for f, g in ((sa / 2, 0.22), (sa * 0.75, 0.16), (sa, 0.12)):
        for h in (1, 2, 3):
            out += np.sin(2 * np.pi * f * h * t + 0.3 * np.sin(0.5 * t)) * g / h
    bpm = rng.choice([92, 96, 100])
    beat = 60 / bpm
    # flute melody (random walk on the scale)
    pos, deg = 0.0, rng.randint(2, 4)
    while pos < seconds - 0.2:
        dur = beat * rng.choice([0.5, 1, 1, 1.5, 2])
        deg = max(0, min(len(scale) - 1, deg + rng.choice([-2, -1, -1, 0, 1, 1, 2])))
        f = sa * 2 * scale[deg]
        i0, ln = int(pos * SR), int(min(dur * 1.1, seconds - pos) * SR)
        if ln > 0:
            tt = np.arange(ln) / SR
            env = np.minimum(1, tt / 0.06) * np.exp(-1.6 * tt / max(dur, 0.2))
            vib = 1 + 0.006 * np.sin(2 * np.pi * 5.2 * tt)
            tone = np.sin(2 * np.pi * f * vib * tt) + 0.25 * np.sin(4 * np.pi * f * vib * tt)
            out[i0:i0 + ln] += tone * env * 0.26
        pos += dur
    # tabla-like percussion
    kick = np.arange(int(0.35 * SR)) / SR
    dha = np.sin(2 * np.pi * (60 + 90 * np.exp(-25 * kick)) * kick) * np.exp(-9 * kick)
    tin = np.sin(2 * np.pi * 420 * kick[:int(0.12 * SR)]) * np.exp(-40 * kick[:int(0.12 * SR)])
    nrng = np.random.default_rng(rng.randint(0, 10**6))
    pattern = ["dha", None, "tin", "tin", "dha", "dha", "tin", None]
    for k in range(int(seconds / (beat / 2))):
        hit = pattern[k % len(pattern)]
        if not hit:
            continue
        i0 = int(k * beat / 2 * SR)
        snd = (dha * 0.5) if hit == "dha" else (tin * 0.28 + nrng.normal(0, 0.02, len(tin)) * np.exp(-60 * kick[:len(tin)]))
        seg = min(len(snd), n - i0)
        if seg > 0:
            out[i0:i0 + seg] += snd[:seg]
    return (out / max(1e-9, np.abs(out).max()) * 0.9).astype(np.float32)


def get_music(seconds, rng):
    files = [p for p in sorted(MUSIC_DIR.glob("*"))
             if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".aac"}]
    if files:
        choice = rng.choice(files)
        print(f"Music: {choice.name}")
        wav = OUT / "music_user.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(choice),
             "-t", str(seconds), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        with wave.open(str(wav)) as w:
            m = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        m = m / max(1e-9, np.abs(m).max()) * 0.9
    else:
        print("Music: synthesized Indian-style instrumental")
        m = synth_music(seconds, rng)
    out = np.zeros(int(seconds * SR), np.float32)
    out[:min(len(out), len(m))] = m[:len(out)]
    return out


def mix_audio(voice, music, path):
    n = len(voice)
    # duck the music while the voice is speaking
    k = int(0.3 * SR)
    c = np.cumsum(np.abs(voice), dtype=np.float64)
    env = (c[k:] - c[:-k]) / k
    env = np.concatenate([np.full(k // 2, env[0]), env, np.full(n - len(env) - k // 2, env[-1])])
    duck = np.clip(env / 0.06, 0, 1)
    gain = 0.30 - 0.17 * duck
    fade = np.ones(n)
    f = int(1.5 * SR)
    fade[-f:] = np.linspace(1, 0, f)
    mix = np.clip((voice + music[:n] * gain) * fade, -1, 1)
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
VIG = (1 - 0.55 * ((_xx - 0.5) ** 2 + (_yy - 0.5) ** 2))[..., None]


def background(t, pal, flash):
    c1, c2, c3 = (np.array(c, np.float32) for c in pal)
    k = (0.7 * _yy + 0.3 * _xx + 0.06 * np.sin(t * 0.7))[..., None]
    arr = c1 * (1 - k) + c2 * k
    for j in range(4):
        cx = 0.5 + 0.42 * math.sin(t * (0.45 + 0.08 * j) + j * 1.9)
        cy = 0.5 + 0.42 * math.cos(t * (0.37 + 0.07 * j) + j * 2.4)
        d2 = ((_xx - cx) * 0.5625) ** 2 + (_yy - cy) ** 2
        arr += np.exp(-d2 / 0.045)[..., None] * c3 * 0.55
    arr *= VIG
    if flash > 0:
        arr = arr * (1 - flash) + 255 * flash
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((W, H), Image.BICUBIC)


def render_chunk(words, active, fnt, max_w=900):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    space = tmp.textlength(" ", font=fnt)
    lines, cur = [[]], 0
    for i, w in enumerate(words):
        ww = tmp.textlength(w, font=fnt)
        if lines[-1] and cur + space + ww > max_w:
            lines.append([])
            cur = 0
        cur += (space if lines[-1] else 0) + ww
        lines[-1].append((i, w, ww))
    lh = int(fnt.size * 1.5)
    img = Image.new("RGBA", (max_w + 80, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 14
    for ln in lines:
        total = sum(x[2] for x in ln) + space * (len(ln) - 1)
        x = (img.width - total) / 2
        for i, w, ww in ln:
            d.text((x, y), w, font=fnt, fill=GOLD if i == active else WHITE,
                   stroke_width=7, stroke_fill=(0, 0, 0, 255))
            x += ww + space
        y += lh
    return img


def pill(text, size, fg, bg, pad=26):
    f = mixed_font(text, size)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w = int(tmp.textlength(text, font=f)) + pad * 2
    h = int(size * 1.7)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=bg)
    d.text((pad, h // 2), text, font=f, fill=fg, anchor="lm")
    return img


def static_top(moves, now_label):
    img = Image.new("RGBA", (W, 460), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    img.alpha_composite(pill("LIVE  MARKET UPDATE", 34, WHITE, (220, 38, 38, 235)), (60, 175))
    d.text((60, 270), "మార్కెట్ న్యూస్", font=te_font(60), fill=WHITE, stroke_width=5, stroke_fill=(0, 0, 0, 255))
    y = 365
    for name, (last, pct) in moves.items():
        col = GREEN if pct >= 0 else RED
        d.text((60, y), f"{name}  {last:,.0f}", font=latin_font(38), fill=WHITE, stroke_width=4, stroke_fill=(0, 0, 0, 255))
        d.text((W - 60, y), f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%", font=latin_font(38),
               fill=col, anchor="ra", stroke_width=4, stroke_fill=(0, 0, 0, 255))
        y += 52
    return img


def draw_chart(frame, series, progress):
    x0, x1, y0, y1 = 70, W - 70, 1260, 1500
    lo, hi = min(series), max(series)
    pts = [(x0 + (x1 - x0) * i / (len(series) - 1),
            y1 - (y1 - y0) * (v - lo) / max(hi - lo, 1e-9)) for i, v in enumerate(series)]
    n = max(2, int(len(pts) * min(1, progress) + 0.999))
    pts = pts[:n]
    col = GREEN if series[-1] >= series[0] else RED
    glow = tuple(int(c * 0.35) for c in col)
    d = ImageDraw.Draw(frame)
    d.line(pts, fill=glow, width=18, joint="curve")
    d.line(pts, fill=col, width=7, joint="curve")
    px, py = pts[-1]
    d.ellipse((px - 14, py - 14, px + 14, py + 14), fill=WHITE, outline=col, width=5)


# ---------------------------------------------------------------- video build
def build_chunks(segments, starts, durs):
    fnt = te_font(86)
    chunks = []
    for si, seg in enumerate(segments):
        words = seg["text"].split()
        weights = [len(w) + 1.5 for w in words]
        tot = sum(weights)
        acc, wstart = 0.0, []
        for wt in weights:
            wstart.append(starts[si] + durs[si] * acc / tot)
            acc += wt
        for i in range(0, len(words), 4):
            cw = words[i:i + 4]
            ws = wstart[i:i + 4]
            end = wstart[i + 4] if i + 4 < len(words) else starts[si] + durs[si] + 0.15
            chunks.append({"seg": si, "words": cw, "starts": ws, "end": end,
                           "imgs": [render_chunk(cw, a, fnt) for a in range(len(cw))]})
    return chunks


def render_video(segments, starts, durs, total, audio_path, out_path, moves, series, rng):
    chunks = build_chunks(segments, starts, durs)
    top = static_top(moves, "")
    pals = [PALETTES[(i + rng.randint(0, 4)) % len(PALETTES)] for i in range(len(segments))]
    chips = []
    for seg in segments:
        if seg["kind"] == "news":
            label = f"వార్త {seg['n']}  -  {seg['source']}"[:44]
            chips.append(pill(label, 36, WHITE, (0, 0, 0, 170)))
        else:
            chips.append(None)
    disc = Image.new("RGBA", (W, 190), (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    dd.rounded_rectangle((60, 0, W - 60, 180), 40, fill=(0, 0, 0, 170))
    dd.text((W // 2, 62), DISCLAIMER_TE, font=te_font(46), fill=WHITE, anchor="mm")
    dd.text((W // 2, 128), "Educational only. Not investment advice.", font=latin_font(32), fill=GREY, anchor="mm")

    n_frames = int(total * FPS)
    log = open(OUT / "ffmpeg.log", "w")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio_path),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest",
         "-movflags", "+faststart", str(out_path)],
        stdin=subprocess.PIPE, stderr=log)
    ci = 0
    for fi in range(n_frames):
        t = fi / FPS
        si = max([i for i, s in enumerate(starts) if t >= s - 0.25] or [0])
        since = t - (starts[si] - 0.25)
        flash = max(0.0, 1 - since / 0.22) * 0.45 if si > 0 else 0.0
        frame = background(t, pals[si], flash)
        frame.paste(top, (0, 0), top)
        d = ImageDraw.Draw(frame)
        d.rectangle((0, 150, int(W * t / total), 158), fill=GOLD)
        if series:
            draw_chart(frame, series, t / total)
        if chips[si]:
            frame.paste(chips[si], (60, 560), chips[si])
        if segments[si]["kind"] == "outro" and t >= starts[si]:
            frame.paste(disc, (0, 1068), disc)
        while ci < len(chunks) - 1 and t >= chunks[ci]["end"]:
            ci += 1
        ch = chunks[ci]
        if ch["starts"][0] - 0.05 <= t < ch["end"]:
            ai = max(i for i, s in enumerate(ch["starts"]) if t >= s - 0.05)
            img = ch["imgs"][ai]
            k = min(1.0, (t - ch["starts"][ai] + 0.05) / 0.12)
            sc = 0.86 + 0.14 * (1 - (1 - k) ** 3)
            if sc < 0.999:
                img = img.resize((int(img.width * sc), int(img.height * sc)), Image.LANCZOS)
            frame.paste(img, ((W - img.width) // 2, 880 - img.height // 2), img)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------------------------ commands
def generate():
    OUT.mkdir(exist_ok=True)
    posted = set(load_posted())
    fresh = [n for n in fetch_news() if n["key"] not in posted][:NEWS_PER_VIDEO]
    if len(fresh) < 2:
        print("Fewer than 2 new Telugu headlines. Skipping this run.")
        set_output("has_post", "false")
        return
    lines = rewrite_te(fresh)
    now = dt.datetime.now(IST)
    rng = random.Random(int(now.timestamp()))

    segments = [{"kind": "intro", "text": INTRO_TE}]
    for i, (ln, n) in enumerate(zip(lines, fresh), 1):
        segments.append({"kind": "news", "text": ln, "source": n["source"], "n": i})
    segments.append({"kind": "outro", "text": OUTRO_TE})

    voice, starts, durs, total = make_voice(segments)
    music = get_music(total, rng)
    audio = OUT / "audio.wav"
    mix_audio(voice, music, audio)
    render_video(segments, starts, durs, total, audio, OUT / "reel.mp4",
                 index_moves(), nifty_series(), rng)

    tags = " ".join(ANCHOR_TAGS + rng.sample(ROTATING_TAGS, 3))
    caption = "\n".join(
        [f"📈 స్టాక్ మార్కెట్ అప్‌డేట్ | {now.strftime('%d %b %Y')}", ""]
        + [f"{i}. {ln} ({n['source']})" for i, (ln, n) in enumerate(zip(lines, fresh), 1)]
        + ["", DISCLAIMER_TE, DISCLAIMER_EN, "", tags]
    )[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps(
        {"caption": caption, "video_name": name, "keys": [n["key"] for n in fresh]},
        ensure_ascii=False))
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
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.getenv("MEDIA_BRANCH", "media")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{meta['video_name']}"
    wait_until_public(url)

    container = api("POST", f"{user_id}/media", media_type="REELS", video_url=url,
                    caption=meta["caption"], share_to_feed="true", access_token=token)["id"]
    for _ in range(60):
        st = requests.get(f"{GRAPH}/{container}",
                          params={"fields": "status_code,status", "access_token": token},
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
    save_posted(meta["keys"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("generate", "all"):
        generate()
    if cmd in ("publish", "all"):
        publish()

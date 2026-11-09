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
from PIL import Image, ImageChops, ImageDraw, ImageFont, features

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0"))
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
W, H, FPS, SR = 1080, 1920, 30, 44100
NEWS_PER_VIDEO = 3
MAX_AGE_HOURS = {"default": 22, "jobs": 48, "health": 48}
SIM_THRESHOLD = 0.34  # trigram similarity above this = "same story"
TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")

# Headlines containing these are skipped (politics, crime, tragedy): edit freely.
BLOCKLIST = [
    "హత్య", "అత్యాచార", "ఆత్మహత్య", "మృతి", "మరణ", "దుర్మరణ", "ప్రమాద", "అరెస్ట్", "కాల్పుల",
    "ఎన్నిక", "ఓటర్", "వైసీపీ", "టీడీపీ", "బీఆర్ఎస్", "కాంగ్రెస్", "బీజేపీ", "జనసేన", "ముఖ్యమంత్రి",
    "ఎమ్మెల్యే", "కులం", "మతం", "అల్లర్లు", "నేరం", "పోలీస", "జైలు", "లైంగిక", "వేధింపు", "యుద్ధ", "ఉగ్ర",
    "murder", "rape", "suicide",
]

# One topic per video, rotated automatically so the page never feels repetitive.
TOPICS = {
    "money": {
        "label": "మనీ & మార్కెట్", "emoji": "📈", "palette": 3, "disc": "invest",
        "intro": "ఈ రోజు స్టాక్ మార్కెట్ ముఖ్యాంశాలు ఇవే.",
        "queries": ["స్టాక్ మార్కెట్ OR సెన్సెక్స్ OR నిఫ్టీ when:1d", "బంగారం ధర OR వెండి ధర when:1d"],
        "pexels": ["stock market trading", "candlestick chart", "gold coins"],
        "tags": ["#stockmarkettelugu", "#sharemarket"],
        "pool": ["#nifty50", "#sensex", "#telugufinance", "#investingtelugu", "#trending", "#telugureels"],
    },
    "cinema": {
        "label": "టాలీవుడ్ అప్‌డేట్స్", "emoji": "🎬", "palette": 2, "disc": None,
        "intro": "టాలీవుడ్ నుంచి ఈ రోజు హాట్ అప్‌డేట్స్ ఇవే.",
        "queries": ["టాలీవుడ్ OR తెలుగు సినిమా OR ట్రైలర్ when:1d", "సినిమా రిలీజ్ OR బాక్సాఫీస్ OR ఓటీటీ when:1d"],
        "pexels": ["movie theatre", "film camera cinema", "red carpet lights"],
        "tags": ["#tollywood", "#telugucinema"],
        "pool": ["#telugumovies", "#movienews", "#tollywoodupdates", "#trending", "#telugureels", "#ott"],
    },
    "cricket": {
        "label": "క్రికెట్ & స్పోర్ట్స్", "emoji": "🏏", "palette": 4, "disc": None,
        "intro": "క్రికెట్ ప్రపంచం నుంచి ఈ రోజు ముఖ్యమైన వార్తలు ఇవే.",
        "queries": ["క్రికెట్ OR ఐపీఎల్ OR టీమిండియా when:1d", "క్రీడలు OR బ్యాడ్మింటన్ OR ఫుట్‌బాల్ when:1d"],
        "pexels": ["cricket stadium", "cricket bat ball", "sports crowd stadium"],
        "tags": ["#cricket", "#telugusports"],
        "pool": ["#indiancricket", "#teamindia", "#ipl", "#cricketnews", "#trending", "#telugureels"],
    },
    "tech": {
        "label": "టెక్ న్యూస్", "emoji": "📱", "palette": 1, "disc": None,
        "intro": "టెక్ ప్రపంచంలో ఈ రోజు ఏం జరిగిందో చూద్దాం.",
        "queries": ["టెక్నాలజీ OR స్మార్ట్‌ఫోన్ OR ఏఐ OR గూగుల్ when:1d", "గాడ్జెట్స్ OR యాప్ OR ఇంటర్నెట్ when:1d"],
        "pexels": ["technology circuit", "smartphone hands", "artificial intelligence"],
        "tags": ["#technews", "#telugutech"],
        "pool": ["#ai", "#smartphones", "#gadgets", "#trending", "#telugureels", "#techtelugu"],
    },
    "jobs": {
        "label": "జాబ్స్ & ఎడ్యుకేషన్", "emoji": "💼", "palette": 0, "disc": None,
        "intro": "ఉద్యోగాలు, చదువు గురించి ఈ రోజు ముఖ్యమైన అప్‌డేట్స్ ఇవే.",
        "queries": ["ఉద్యోగ నోటిఫికేషన్ OR జాబ్స్ OR పరీక్ష ఫలితాలు when:2d", "విద్య OR స్కాలర్‌షిప్ OR నియామకాలు when:2d"],
        "pexels": ["students studying", "job interview office", "graduation"],
        "tags": ["#jobsintelugu", "#telanganajobs"],
        "pool": ["#govtjobs", "#jobalert", "#education", "#trending", "#telugureels", "#careertips"],
    },
    "health": {
        "label": "ఆరోగ్యం & లైఫ్‌స్టైల్", "emoji": "🩺", "palette": 4, "disc": "health",
        "intro": "ఆరోగ్యం గురించి ఈ రోజు తెలుసుకోవాల్సిన విషయాలు ఇవే.",
        "queries": ["ఆరోగ్య చిట్కాలు OR ఆరోగ్యం when:2d", "డయాబెటిస్ OR ఆహారం OR యోగా when:2d"],
        "pexels": ["healthy food", "yoga morning", "walking park"],
        "tags": ["#healthtips", "#teluguhealth"],
        "pool": ["#wellness", "#healthyliving", "#lifestyle", "#trending", "#telugureels", "#yoga"],
    },
    "viral": {
        "label": "ట్రెండింగ్ న్యూస్", "emoji": "🔥", "palette": 2, "disc": None,
        "intro": "సోషల్ మీడియాలో ఈ రోజు ట్రెండింగ్ విషయాలు ఇవే.",
        "queries": ["వైరల్ OR ట్రెండింగ్ when:1d", "వైరల్ వీడియో OR సోషల్ మీడియా when:1d"],
        "pexels": ["social media phone", "city night lights", "crowd celebration"],
        "tags": ["#trending", "#viralreels"],
        "pool": ["#telugu", "#telugureels", "#viral", "#explore", "#teluguupdates", "#news"],
    },
}

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
HEALTH_TE = "ఇది వైద్య సలహా కాదు. ఆరోగ్య సమస్యలకు డాక్టర్‌ను సంప్రదించండి."
HEALTH_EN = "General information only. Not medical advice. Consult a doctor."
GENERIC_TE = "వార్తలు పబ్లిక్ సోర్స్‌ల నుంచి సంగ్రహించబడ్డాయి."
GENERIC_EN = "News summarised from public sources."
OUTRO_TE = "మరిన్ని అప్‌డేట్స్ కోసం ఫాలో అవ్వండి."


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


def load_state():
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    st.setdefault("posted", [])
    st.setdefault("items", [])
    st.setdefault("topics", [])
    return st


def save_state(meta):
    st = load_state()
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    st["posted"] = (st["posted"] + meta["keys"])[-600:]
    st["items"] = (st["items"] + [{"t": t, "ts": ts, "topic": meta["topic"]} for t in meta["titles"]])[-300:]
    st["topics"] = (st["topics"] + [meta["topic"]])[-30:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def norm_title(t):
    return re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", t.lower()).strip()


def trigrams(t):
    x = norm_title(t).replace(" ", "_")
    return {x[i:i + 3] for i in range(len(x) - 2)}


def jaccard(a, b):
    return len(a & b) / max(1, len(a | b))


def is_repeat(tri, others):
    return any(jaccard(tri, o) >= SIM_THRESHOLD for o in others)


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
def fetch_news(topic_key):
    topic = TOPICS[topic_key]
    max_age = dt.timedelta(hours=MAX_AGE_HOURS.get(topic_key, MAX_AGE_HOURS["default"]))
    now_utc = dt.datetime.now(dt.timezone.utc)
    items, seen = [], set()
    for q in topic["queries"]:
        try:
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": q, "hl": "te", "gl": "IN", "ceid": "IN:te"},
                             timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"Feed failed ({topic_key}): {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            source = (it.findtext("source") or "").strip()
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)]
            elif " - " in title and not source:
                title, source = title.rsplit(" - ", 1)
            title = re.split(r"\s*[|｜]\s*", title)[0].strip()
            if not TELUGU_RE.search(title) or not (15 <= len(title) <= 190):
                continue
            if any(b in title.lower() for b in BLOCKLIST):
                continue
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if now_utc - pub > max_age:
                continue
            key = hashlib.md5(norm_title(title).encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            items.append({"title": title, "source": source or "News", "key": key, "pub": pub})
    items.sort(key=lambda n: n["pub"], reverse=True)
    return items


def fresh_stories(topic_key, state, extra_lines=()):
    """Newest stories that are neither posted before nor a re-wording of a recent post."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=96)
    recent = []
    for i in state["items"]:
        try:
            if dt.datetime.fromisoformat(i["ts"]) >= cutoff:
                recent.append(trigrams(i["t"]))
        except Exception:
            recent.append(trigrams(i["t"]))
    recent += [trigrams(x) for x in extra_lines]
    posted, picked, picked_tri = set(state["posted"]), [], []
    for n in fetch_news(topic_key):
        if n["key"] in posted:
            continue
        tri = trigrams(n["title"])
        if is_repeat(tri, recent) or is_repeat(tri, picked_tri):
            continue
        picked.append(n)
        picked_tri.append(tri)
        if len(picked) >= NEWS_PER_VIDEO:
            break
    return picked


def recent_ig_lines(user_id, token):
    """Story lines from our own recent Instagram captions (protects against lost state)."""
    try:
        r = requests.get(f"{GRAPH}/{user_id}/media",
                         params={"fields": "caption", "limit": 25, "access_token": token}, timeout=30)
        r.raise_for_status()
        out = []
        for m in r.json().get("data", []):
            for ln in (m.get("caption") or "").splitlines():
                mm = re.match(r"^\d\.\s+(.+?)(?:\s+\([^()]*\))?$", ln.strip())
                if mm:
                    out.append(mm.group(1))
        return out
    except Exception as e:
        print(f"Recent-post check skipped: {e}")
        return []


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
        "Rewrite each Telugu news headline below as ONE short sentence in warm, "
        "conversational spoken Telugu, as a friendly Instagram news presenter would "
        "say it (simple everyday words, max 16 words, no formal bookish phrases). Use "
        "only facts in the headline. No advice, no predictions, no opinions. Return ONLY a "
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
VOICE_FX = ("highpass=f=90,acompressor=threshold=0.12:ratio=3:attack=5:release=90,"
            "equalizer=f=3200:t=q:w=1.2:g=2.5,aecho=0.85:0.9:55:0.14")


def eleven_tts(text, path):
    """Most natural option. Needs ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID (Telugu needs model eleven_v3)."""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        params={"output_format": "mp3_44100_128"},
        json={"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_v3"),
              "voice_settings": {"stability": 0.5}},
        timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)


def tts(text, path):
    if os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID"):
        try:
            eleven_tts(text, path)
            if path.stat().st_size > 1000:
                return
        except Exception as e:
            print(f"ElevenLabs failed ({e}); falling back to Edge TTS")
    voice = os.getenv("TTS_VOICE", "te-IN-ShrutiNeural")
    try:
        import edge_tts
        rate = f"+{random.choice([3, 5, 7, 9])}%"  # slight variation so it is less monotone
        asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(path)))
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
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", str(SR), "-ac", "1"]
        if os.getenv("VOICE_FX", "1") == "1":  # light compression/EQ/room so it sounds produced
            cmd += ["-af", VOICE_FX]
        run(cmd + ["-c:a", "pcm_s16le", str(wav)])
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
    bpm = rng.choice([104, 112, 120])
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
    pattern = ["dha", "tin", "tin", "dha", "dha", "tin", "dha", "tin"]
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
BRAND = os.getenv("BRAND_NAME", "MARKET PULSE")

# darkening layer so captions stay readable over stock footage
_dy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
_dm = (0.40 + 0.27 * np.sin(np.pi * _dy)) * np.ones((1, W), np.float32)
DARK_IMG = Image.fromarray(np.repeat((_dm * 255).astype(np.uint8)[..., None], 3, axis=2))


def background(t, pal, flash=0.0):
    c1, c2, c3 = (np.array(c, np.float32) for c in pal)
    k = (0.7 * _yy + 0.3 * _xx + 0.06 * np.sin(t * 0.7))[..., None]
    arr = c1 * (1 - k) + c2 * k
    for j in range(4):
        cx = 0.5 + 0.42 * math.sin(t * (0.45 + 0.08 * j) + j * 1.9)
        cy = 0.5 + 0.42 * math.cos(t * (0.37 + 0.07 * j) + j * 2.4)
        d2 = ((_xx - cx) * 0.5625) ** 2 + (_yy - cy) ** 2
        arr += np.exp(-d2 / 0.045)[..., None] * c3 * 0.55
    arr *= VIG
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((W, H), Image.BICUBIC)


def ease_out(k):
    k = max(0.0, min(1.0, k))
    return 1 - (1 - k) ** 3


def glass(w, h, r=34, fill=(8, 12, 28, 165), border=(255, 255, 255, 70)):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=fill, outline=border, width=2)
    return img


def arrow(d, x, y, size, up, col):
    pts = [(x, y + size), (x + size, y + size), (x + size / 2, y)] if up else \
          [(x, y), (x + size, y), (x + size / 2, y + size)]
    d.polygon(pts, fill=col)


def stroke_text(d, xy, text, font, fill, anchor=None, sw=4):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=sw, stroke_fill=(0, 0, 0, 255))


def index_card(name, last, pct, k, w=465, h=150):
    img = glass(w, h)
    d = ImageDraw.Draw(img)
    col = GREEN if pct >= 0 else RED
    d.text((26, 18), name, font=latin_font(28), fill=GREY)
    d.text((26, 62), f"{last * k:,.0f}", font=latin_font(52), fill=WHITE)
    txt = f"{abs(pct) * k:.2f}%"
    pf = latin_font(30)
    pw = int(d.textlength(txt, font=pf)) + 66
    x0 = w - pw - 22
    d.rounded_rectangle((x0, 22, w - 22, 70), 24, fill=col + (255,))
    arrow(d, x0 + 18, 36, 20, pct >= 0, WHITE)
    d.text((x0 + 46, 46), txt, font=pf, fill=WHITE, anchor="lm")
    return img


def brand_bar(date_label, label):
    img = Image.new("RGBA", (W, 110), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 6, 140, 86), fill=GOLD + (255,))
    d.text((100, 46), "MP", font=latin_font(34), fill=(20, 24, 48), anchor="mm")
    stroke_text(d, (165, 4), BRAND, latin_font(40), WHITE, sw=3)
    stroke_text(d, (165, 56), label, te_font(30), GOLD, sw=3)
    stroke_text(d, (W - 60, 70), date_label, latin_font(26), WHITE, anchor="ra", sw=3)
    return img


def live_pill(dot):
    img = Image.new("RGBA", (170, 54), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 169, 53), 27, fill=(220, 38, 38, 245))
    if dot:
        d.ellipse((20, 16, 42, 38), fill=WHITE)
    d.text((56, 28), "LIVE", font=latin_font(30), fill=WHITE, anchor="lm")
    return img


LATIN_RE = re.compile(r"[A-Za-z]")
NEUTRAL = set(" .,:;-–—!?()'\"/&%+#@|•")


def text_runs(text):
    """Split into (chunk, is_latin) runs so each script uses a font that has its glyphs."""
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


def render_chunk(words, active, fnt, max_w=900):
    size = fnt.size
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    space = tmp.textlength(" ", font=fnt)
    lines, cur = [[]], 0
    for i, w in enumerate(words):
        ww = mixed_width(tmp, w, size)
        if lines[-1] and cur + space + ww > max_w:
            lines.append([])
            cur = 0
        cur += (space if lines[-1] else 0) + ww
        lines[-1].append((i, w, ww))
    lh = int(size * 1.5)
    img = Image.new("RGBA", (max_w + 80, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 14
    for ln in lines:
        total = sum(x[2] for x in ln) + space * (len(ln) - 1)
        x = (img.width - total) / 2
        for i, w, ww in ln:
            if i == active:  # highlighted word gets a gold pill behind it
                d.rounded_rectangle((x - 12, y + 6, x + ww + 12, y + lh - 8), 26, fill=GOLD + (255,))
                draw_mixed(d, (x, y), w, size, (18, 20, 40))
            else:
                draw_mixed(d, (x, y), w, size, WHITE, sw=7)
            x += ww + space
        y += lh
    return img


def pill(text, size, fg, bg, pad=26):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w = int(mixed_width(tmp, text, size)) + pad * 2
    h = int(size * 1.7)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=bg)
    draw_mixed(d, (pad, h // 2), text, size, fg, anchor="lm")
    return img


PANEL_W, PANEL_H = 960, 290


def chart_base():
    img = glass(PANEL_W, PANEL_H, 36)
    d = ImageDraw.Draw(img)
    d.text((34, 20), "NIFTY 50  -  LAST 5 DAYS", font=latin_font(26), fill=GREY)
    for gy in (100, 150, 200):
        d.line((34, gy, PANEL_W - 34, gy), fill=(255, 255, 255, 26), width=1)
    d.text((PANEL_W // 2, PANEL_H - 24), "Educational only. Not investment advice.",
           font=latin_font(22), fill=GREY, anchor="mm")
    return img


def chart_gradient(col):
    a = np.linspace(120, 0, PANEL_H, dtype=np.float32)[:, None] * np.ones((1, PANEL_W), np.float32)
    arr = np.zeros((PANEL_H, PANEL_W, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = col[0], col[1], col[2], a.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def draw_chart(base, grad, series, progress):
    x0, x1, y0, y1 = 40, PANEL_W - 40, 70, 232
    lo, hi = min(series), max(series)
    pts = [(x0 + (x1 - x0) * i / (len(series) - 1),
            y1 - (y1 - y0) * (v - lo) / max(hi - lo, 1e-9)) for i, v in enumerate(series)]
    n = max(2, int(len(pts) * min(1, progress) + 0.999))
    pts = pts[:n]
    col = GREEN if series[-1] >= series[0] else RED
    img = base.copy()
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts + [(pts[-1][0], y1 + 14), (pts[0][0], y1 + 14)], fill=255)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(grad, (0, 0), mask)
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    d.line(pts, fill=tuple(int(c * 0.4) for c in col) + (255,), width=14, joint="curve")
    d.line(pts, fill=col + (255,), width=6, joint="curve")
    px, py = pts[-1]
    d.ellipse((px - 13, py - 13, px + 13, py + 13), fill=WHITE, outline=col, width=5)
    val = series[0] + (series[-1] - series[0]) * 0  # label shows the latest plotted value
    cur = series[min(len(series) - 1, n - 1)]
    d.text((PANEL_W - 34, 20), f"{cur:,.0f}", font=latin_font(30), fill=WHITE, anchor="ra")
    return img


def swipe_band():
    bw = 300
    a = np.concatenate([np.linspace(0, 210, bw // 2), np.linspace(210, 0, bw // 2)]).astype(np.uint8)
    arr = np.zeros((H, bw, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = 255, 214, 102
    arr[..., 3] = a[None, :]
    return Image.fromarray(arr, "RGBA")


# ------------------------------------------------------------- stock footage
def fetch_stock_clips(topic_key, n=4):
    """Optional: free vertical stock videos from Pexels (needs PEXELS_API_KEY)."""
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return []
    queries = TOPICS[topic_key]["pexels"] + ["india city night traffic", "city skyline night"]
    random.shuffle(queries)
    clips = []
    for q in queries:
        if len(clips) >= n:
            break
        try:
            r = requests.get("https://api.pexels.com/videos/search",
                             headers={"Authorization": key},
                             params={"query": q, "orientation": "portrait", "size": "medium", "per_page": 15},
                             timeout=30)
            r.raise_for_status()
            vids = [v for v in r.json().get("videos", []) if v.get("duration", 0) >= 5]
            random.shuffle(vids)
            for v in vids:
                files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4"
                         and f.get("height", 0) >= 1280 and f.get("width", 0) <= 1440]
                if not files:
                    continue
                files.sort(key=lambda f: abs(f["width"] - 1080))
                path = OUT / f"stock_{len(clips)}.mp4"
                with requests.get(files[0]["link"], stream=True, timeout=90) as resp:
                    resp.raise_for_status()
                    size = 0
                    with open(path, "wb") as fh:
                        for chunk in resp.iter_content(1 << 20):
                            size += len(chunk)
                            if size > 60 * 1024 * 1024:
                                raise RuntimeError("clip too large")
                            fh.write(chunk)
                clips.append(path)
                break
        except Exception as e:
            print(f"Pexels '{q}' failed: {e}")
    print(f"Stock clips: {len(clips)}")
    return clips


class BgReader:
    def __init__(self, path):
        self.p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-stream_loop", "-1", "-i", str(path),
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=W * H * 3 * 2)

    def read(self):
        buf = self.p.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            return None
        return Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1)

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


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


def topic_card(text, n):
    img = glass(960, 150)
    d = ImageDraw.Draw(img)
    d.text((40, 75), text, font=te_font(54), fill=WHITE, anchor="lm")
    d.rounded_rectangle((960 - 40 - 150, 45, 960 - 40, 105), 30, fill=GOLD + (255,))
    d.text((960 - 40 - 75, 75), f"TOP {n}", font=latin_font(32), fill=(18, 20, 40), anchor="mm")
    return img


def info_panel(n_news, cur):
    img = glass(PANEL_W, 230, 36)
    d = ImageDraw.Draw(img)
    d.text((PANEL_W // 2, 56), "ప్రతిరోజూ కొత్త తెలుగు అప్‌డేట్స్", font=te_font(38), fill=WHITE, anchor="mm")
    gap = 130
    x0 = PANEL_W // 2 - gap * (n_news - 1) // 2
    for i in range(n_news):
        cx, on = x0 + i * gap, (i + 1 == cur)
        d.ellipse((cx - 32, 110, cx + 32, 174), fill=GOLD + (255,) if on else (255, 255, 255, 40),
                  outline=(255, 255, 255, 100), width=2)
        d.text((cx, 142), str(i + 1), font=latin_font(34), fill=(18, 20, 40) if on else GREY, anchor="mm")
    return img


def render_video(segments, starts, durs, total, audio_path, out_path, moves, series, rng,
                 clips=None, date_label="", topic=None):
    topic = topic or TOPICS["money"]
    clips = clips or []
    n_news = sum(1 for sg in segments if sg["kind"] == "news")
    chunks = build_chunks(segments, starts, durs)
    bar = brand_bar(date_label, topic["label"])
    live = [live_pill(True), live_pill(False)]
    pals = [PALETTES[(topic["palette"] + i) % len(PALETTES)] for i in range(len(segments))]
    chips = []
    for seg in segments:
        if seg["kind"] == "news":
            chips.append(pill(f"వార్త {seg['n']}  -  {seg['source']}"[:44], 36, (18, 20, 40), GOLD + (255,)))
        else:
            chips.append(None)
    follow = pill("ఫాలో అవ్వండి", 64, (18, 20, 40), GOLD + (255,), pad=60)
    band = swipe_band()
    base = chart_base() if series else None
    grad = chart_gradient(GREEN if series[-1] >= series[0] else RED) if series else None
    card_items = list(moves.items())
    final_cards = [index_card(n, v[0], v[1], 1.0) for n, v in card_items]
    if not card_items:  # non-market topics: topic card + numbered panel instead of cards/chart
        tcard = topic_card(f"ఈ రోజు టాప్ {n_news} అప్‌డేట్స్", n_news)
        panels = [info_panel(n_news, sg.get("n", 0) if sg["kind"] == "news" else 0) for sg in segments]
    bounds = [x - 0.25 for x in starts] + [total]

    n_frames = int(total * FPS)
    log = open(OUT / "ffmpeg.log", "w")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio_path),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest",
         "-movflags", "+faststart", str(out_path)],
        stdin=subprocess.PIPE, stderr=log)
    ci, reader, reader_seg = 0, None, -1
    for fi in range(n_frames):
        t = fi / FPS
        si = max([i for i, x in enumerate(starts) if t >= x - 0.25] or [0])
        since = t - bounds[si]

        frame = None
        if clips:
            if reader_seg != si:
                if reader:
                    reader.close()
                reader, reader_seg = BgReader(clips[si % len(clips)]), si
            raw = reader.read()
            if raw is not None:
                frame = ImageChops.multiply(raw, DARK_IMG)
        if frame is None:
            frame = background(t, pals[si])

        frame.paste(bar, (0, 165), bar)
        lp = live[0 if int(t * 2) % 2 == 0 else 1]
        frame.paste(lp, (W - 230, 175), lp)
        d = ImageDraw.Draw(frame)
        seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
        for i in range(len(segments)):
            x0 = 60 + i * (seg_w + 8)
            d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(110, 114, 130))
            fill = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
            if fill > 0:
                d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * fill), 150), 4, fill=GOLD)

        if card_items:
            k = ease_out((t - 0.2) / 1.3)
            for i, (name, (last, pct)) in enumerate(card_items):
                img = final_cards[i] if k >= 1 else index_card(name, last, pct, k)
                frame.paste(img, (60 + i * 495, 300), img)
        else:
            frame.paste(tcard, (60, 300), tcard)

        if chips[si]:
            slide = ease_out(since / 0.35)
            frame.paste(chips[si], (int(60 - (1 - slide) * 700), 500), chips[si])

        if series:
            panel = draw_chart(base, grad, series, t / total)
            frame.paste(panel, ((W - PANEL_W) // 2, 1225), panel)
        elif not card_items:
            frame.paste(panels[si], ((W - PANEL_W) // 2, 1260), panels[si])

        while ci < len(chunks) - 1 and t >= chunks[ci]["end"]:
            ci += 1
        ch = chunks[ci]
        if ch["starts"][0] - 0.05 <= t < ch["end"]:
            ai = max(i for i, x in enumerate(ch["starts"]) if t >= x - 0.05)
            img = ch["imgs"][ai]
            kk = min(1.0, (t - ch["starts"][ai] + 0.05) / 0.12)
            sc = 0.86 + 0.14 * ease_out(kk)
            if sc < 0.999:
                img = img.resize((int(img.width * sc), int(img.height * sc)), Image.LANCZOS)
            frame.paste(img, ((W - img.width) // 2, 880 - img.height // 2), img)

        if segments[si]["kind"] == "outro" and t >= starts[si]:
            sc2 = 1 + 0.05 * math.sin(t * 7)
            fb = follow.resize((int(follow.width * sc2), int(follow.height * sc2)), Image.LANCZOS)
            frame.paste(fb, ((W - fb.width) // 2, 1110 - fb.height // 2), fb)

        if si > 0 and since < 0.4:
            x = int(-300 + (W + 300) * ease_out(since / 0.4))
            frame.paste(band, (x, 0), band)

        proc.stdin.write(frame.tobytes())
    if reader:
        reader.close()
    proc.stdin.close()
    if proc.wait() != 0:
        print(open(OUT / "ffmpeg.log").read()[-2000:])
        raise RuntimeError("ffmpeg video encode failed")


# ------------------------------------------------------------------ commands
def pick_topic(state, ig_lines):
    forced = os.getenv("TOPIC")
    if forced in TOPICS:
        order = [forced]
    else:  # least-recently-used topics first, random tie-break
        recent = state["topics"][-8:]
        order = sorted(TOPICS, key=lambda k: (recent.count(k), random.random()))
    for tk in order:
        fresh = fresh_stories(tk, state, ig_lines)
        print(f"Topic {tk}: {len(fresh)} fresh stories")
        if len(fresh) >= 2:
            return tk, fresh
    return None, []


def generate():
    OUT.mkdir(exist_ok=True)
    state = load_state()
    ig_lines = []
    if os.getenv("IG_ACCESS_TOKEN"):
        ig_lines = recent_ig_lines(os.getenv("IG_USER_ID") or "me", os.environ["IG_ACCESS_TOKEN"])
    topic_key, fresh = pick_topic(state, ig_lines)
    if not topic_key:
        print("No topic has 2+ fresh, non-repeated stories right now. Skipping this run.")
        set_output("has_post", "false")
        return
    topic = TOPICS[topic_key]
    print(f"Chosen topic: {topic_key}")
    lines = rewrite_te(fresh)
    now = dt.datetime.now(IST)
    rng = random.Random(int(now.timestamp()))

    outro = OUTRO_TE + (" " + DISCLAIMER_TE if topic["disc"] == "invest" else
                        " " + "ఇది వైద్య సలహా కాదు." if topic["disc"] == "health" else "")
    segments = [{"kind": "intro", "text": topic["intro"]}]
    for i, (ln, n) in enumerate(zip(lines, fresh), 1):
        segments.append({"kind": "news", "text": ln, "source": n["source"], "n": i})
    segments.append({"kind": "outro", "text": outro})

    voice, starts, durs, total = make_voice(segments)
    music = get_music(total, rng)
    audio = OUT / "audio.wav"
    mix_audio(voice, music, audio)
    money = topic_key == "money"
    render_video(segments, starts, durs, total, audio, OUT / "reel.mp4",
                 index_moves() if money else {}, nifty_series() if money else None, rng,
                 clips=fetch_stock_clips(topic_key), date_label=now.strftime("%d %b %Y"), topic=topic)

    if topic["disc"] == "invest":
        disc = [DISCLAIMER_TE, DISCLAIMER_EN]
    elif topic["disc"] == "health":
        disc = [HEALTH_TE, HEALTH_EN]
    else:
        disc = [GENERIC_TE, GENERIC_EN]
    tags = " ".join(topic["tags"] + rng.sample(topic["pool"], 3))
    caption = "\n".join(
        [f"{topic['emoji']} {topic['label']} | {now.strftime('%d %b %Y')}", ""]
        + [f"{i}. {ln} ({n['source']})" for i, (ln, n) in enumerate(zip(lines, fresh), 1)]
        + [""] + disc + ["", tags]
    )[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps(
        {"caption": caption, "video_name": name, "keys": [n["key"] for n in fresh],
         "titles": [n["title"] for n in fresh], "lines": lines, "topic": topic_key},
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

    # last safety net: never post a story set we already posted on Instagram
    ig_lines = recent_ig_lines(user_id, token)
    if ig_lines:
        tris = [trigrams(x) for x in ig_lines]
        dup = sum(1 for ln in meta["lines"] if is_repeat(trigrams(ln), tris))
        if dup >= 2:
            print("These stories look like a repeat of recent posts. Skipping publish.")
            save_state(meta)
            return

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
    save_state(meta)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("generate", "all"):
        generate()
    if cmd in ("publish", "all"):
        publish()

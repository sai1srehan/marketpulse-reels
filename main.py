"""
Stock-news Reel bot.

Latest Indian stock-market headlines -> 9:16 video with a freshly generated
background + background music -> Instagram Reel (official Graph API).

    python main.py generate   # build out/reel.mp4 + out/meta.json
    python main.py publish    # publish it as a Reel
    python main.py all        # both (local testing)

Secrets / env:
    IG_ACCESS_TOKEN               Instagram API token (Instagram Login)
    IG_USER_ID                    optional, defaults to 'me'
    ANTHROPIC_API_KEY             optional: rewrites headlines into short lines
    DRY_RUN=1                     build everything but do not post
"""
import datetime as dt
import hashlib
import json
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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GRAPH = (os.getenv("IG_GRAPH_HOST", "https://graph.instagram.com").rstrip("/")
         + "/" + os.getenv("IG_API_VERSION", "v23.0"))
OUT = pathlib.Path("out")
STATE = pathlib.Path("state/posted.json")
MUSIC_DIR = pathlib.Path("music")
W, H, FPS = 1080, 1920, 30

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=Indian+stock+market+OR+Sensex+OR+Nifty+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=NSE+BSE+shares+earnings+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
]
NEWS_PER_VIDEO = 4

PALETTES = [  # (top-left colour, bottom-right colour)
    ((10, 25, 60), (88, 28, 135)),
    ((6, 40, 52), (16, 120, 110)),
    ((35, 12, 50), (190, 50, 90)),
    ((12, 18, 40), (30, 90, 180)),
    ((20, 30, 20), (20, 110, 70)),
    ((40, 18, 12), (200, 100, 30)),
]
WHITE, GREY, GOLD = (245, 247, 252), (170, 180, 200), (255, 214, 102)

DISCLAIMER = (
    "Educational content only. Not investment advice. I am not a SEBI "
    "registered investment adviser or research analyst. Do your own research."
)
HASHTAGS = (
    "#stockmarket #nifty50 #sensex #indianstockmarket #sharemarket "
    "#marketnews #investing #finance #stocksindia #reels"
)


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
    posted = (load_posted() + keys)[-400:]
    STATE.write_text(json.dumps({"posted": posted}, indent=1))


# --------------------------------------------------------------------- news
def fetch_news():
    items, seen = [], set()
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
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
            norm = re.sub(r"\W+", " ", title.lower()).strip()
            if len(norm) < 20:
                continue
            key = hashlib.md5(norm.encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                pub = dt.datetime.now(dt.timezone.utc)
            items.append({"title": title, "source": source or "News",
                          "key": key, "pub": pub})
    items.sort(key=lambda n: n["pub"], reverse=True)
    return items


def rewrite(news):
    """Short plain-English slide lines. Falls back to the raw headlines."""
    fallback = [n["title"] for n in news]
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return fallback
    prompt = (
        "Rewrite each stock-market headline below as ONE short plain-English "
        "line (max 14 words) for a video slide. Use only facts contained in "
        "the headline. No advice, no predictions, no opinions. Return ONLY a "
        "JSON array of strings in the same order.\n" + json.dumps(fallback)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
                  "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        lines = json.loads(text)
        if isinstance(lines, list) and len(lines) == len(news):
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


# ------------------------------------------------------------------- visuals
def font(size, bold=False):
    base = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for p in (f"/usr/share/fonts/truetype/dejavu/{base}", base):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def make_background(rng):
    c1, c2 = rng.choice(PALETTES)
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    t = 0.7 * y + 0.3 * x
    arr = np.zeros((H, W, 3), np.float32)
    for i in range(3):
        arr[..., i] = c1[i] * (1 - t) + c2[i] * t
    img = Image.fromarray(arr.astype(np.uint8)).convert("RGBA")
    orbs = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(orbs)
    for _ in range(5):
        r = rng.randint(250, 520)
        cx, cy = rng.randint(0, W), rng.randint(0, H)
        base = rng.choice([c1, c2])
        d.ellipse((cx - r, cy - r, cx + r, cy + r),
                  fill=tuple(min(255, v + 70) for v in base) + (80,))
    orbs = orbs.filter(ImageFilter.GaussianBlur(130))
    return Image.alpha_composite(img, orbs).convert("RGB")


def card(img, box, alpha=150, radius=44):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(box, radius, fill=(8, 12, 24, alpha))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for w in text.split():
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def intro_slide(rng, label, moves):
    img = make_background(rng)
    d = ImageDraw.Draw(img)
    d.text((90, 300), "STOCK MARKET", font=font(104, True), fill=WHITE)
    d.text((90, 420), "UPDATE", font=font(104, True), fill=GOLD)
    d.text((90, 570), label, font=font(38), fill=GREY)
    y = 760
    for name, (last, pct) in moves.items():
        img = card(img, (60, y, W - 60, y + 190))
        d = ImageDraw.Draw(img)
        col = (34, 197, 94) if pct >= 0 else (239, 68, 68)
        d.text((100, y + 28), name, font=font(36), fill=GREY)
        d.text((100, y + 92), f"{last:,.2f}", font=font(58, True), fill=WHITE)
        d.text((W - 100, y + 70), f"{pct:+.2f}%", font=font(64, True), fill=col, anchor="ra")
        y += 225
    if not moves:
        d.text((90, 760), "Top headlines right now", font=font(54, True), fill=WHITE)
    return img


def news_slide(rng, idx, text, source):
    img = make_background(rng)
    d = ImageDraw.Draw(img)
    f = font(64, True)
    lines = wrap(d, text, f, 820)[:7]
    lh = 84
    ch = len(lines) * lh + 320
    top = (H - ch) // 2 - 120
    img = card(img, (60, top, W - 60, top + ch))
    d = ImageDraw.Draw(img)
    d.text((110, top + 45), f"{idx:02d}", font=font(96, True), fill=GOLD)
    y = top + 195
    for ln in lines:
        d.text((110, y), ln, font=f, fill=WHITE)
        y += lh
    d.text((110, top + ch - 75), f"Source: {source}"[:46], font=font(32), fill=GREY)
    return img


def outro_slide(rng):
    img = make_background(rng)
    d = ImageDraw.Draw(img)
    d.text((90, 560), "Follow for", font=font(88, True), fill=WHITE)
    d.text((90, 670), "market updates", font=font(88, True), fill=GOLD)
    d.text((90, 790), "every 2 hours", font=font(60), fill=WHITE)
    img = card(img, (60, 1000, W - 60, 1330), alpha=120)
    d = ImageDraw.Draw(img)
    y = 1040
    for ln in wrap(d, DISCLAIMER, font(34), 880):
        d.text((100, y), ln, font=font(34), fill=GREY)
        y += 50
    return img


# --------------------------------------------------------------------- audio
def synth_music(path, seconds, rng, sr=44100):
    """Original ambient track (no copyright issues)."""
    roots = [220.00, 196.00, 174.61, 246.94, 164.81]
    minor = [1, 6 / 5, 3 / 2, 9 / 5]
    prog = rng.sample(roots, 4)
    n = int(seconds * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    clen = seconds / len(prog)
    for ci, root in enumerate(prog):
        s0, s1 = int(ci * clen * sr), min(n, int((ci + 1) * clen * sr))
        tt = t[s0:s1]
        env = np.clip(np.minimum((tt - tt[0]) / 1.5, (tt[-1] - tt) / 1.5), 0, 1)
        for ratio in minor:
            for det in (-0.4, 0.4):
                out[s0:s1] += np.sin(2 * np.pi * (root * ratio + det) * tt) * env * 0.10
        out[s0:s1] += np.sin(2 * np.pi * (root / 2) * tt) * env * 0.18
    beat = 60 / rng.choice([84, 92, 100])
    pl_t = np.arange(int(0.6 * sr)) / sr
    for k in range(int(seconds / beat)):
        pos = k * beat
        ci = min(len(prog) - 1, int(pos / clen))
        f = prog[ci] * 2 * minor[k % 4]
        pluck = np.sin(2 * np.pi * f * pl_t) * np.exp(-6 * pl_t) * 0.16
        i0 = int(pos * sr)
        seg = min(len(pluck), n - i0)
        if seg > 0:
            out[i0:i0 + seg] += pluck[:seg]
    out = out / max(1e-9, np.abs(out).max()) * 0.7
    pcm = (np.stack([out, out], axis=1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def pick_music(rng, seconds):
    files = [p for p in sorted(MUSIC_DIR.glob("*"))
             if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".aac"}]
    if files:
        choice = rng.choice(files)
        print(f"Music: {choice.name}")
        return choice
    path = OUT / "music.wav"
    synth_music(path, seconds + 1, rng)
    print("Music: synthesized ambient track")
    return path


# --------------------------------------------------------------------- video
def build_video(slide_paths, durations, music, out_path):
    clips = []
    for i, (sp, dur) in enumerate(zip(slide_paths, durations)):
        frames = int(dur * FPS)
        clip = OUT / f"clip_{i}.mp4"
        vf = (
            f"scale=1620:2880,"
            f"zoompan=z='1+0.05*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={W}x{H}:fps={FPS},"
            f"fade=t=in:st=0:d=0.35,fade=t=out:st={dur - 0.35}:d=0.35,format=yuv420p"
        )
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(sp), "-vf", vf,
             "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-r", str(FPS), str(clip)])
        clips.append(clip)
    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    silent = OUT / "silent.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(silent)])
    total = sum(durations)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent),
         "-stream_loop", "-1", "-i", str(music), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-af", f"afade=t=in:d=1,afade=t=out:st={total - 1.5}:d=1.5",
         "-shortest", "-movflags", "+faststart", str(out_path)])


# ------------------------------------------------------------------ commands
def generate():
    OUT.mkdir(exist_ok=True)
    posted = set(load_posted())
    fresh = [n for n in fetch_news() if n["key"] not in posted][:NEWS_PER_VIDEO]
    if len(fresh) < 2:
        print("Fewer than 2 new headlines. Skipping this run.")
        set_output("has_post", "false")
        return
    texts = rewrite(fresh)
    now = dt.datetime.now(IST)
    label = now.strftime("%d %b %Y  |  %I:%M %p IST")
    rng = random.Random(int(now.timestamp()))  # new look every run

    slides = [intro_slide(rng, label, index_moves())]
    for i, (t, n) in enumerate(zip(texts, fresh), 1):
        slides.append(news_slide(rng, i, t, n["source"]))
    slides.append(outro_slide(rng))
    durations = [4.0] + [5.5] * len(fresh) + [4.0]

    paths = []
    for i, s in enumerate(slides):
        p = OUT / f"slide_{i}.png"
        s.save(p)
        paths.append(p)

    music = pick_music(rng, sum(durations))
    build_video(paths, durations, music, OUT / "reel.mp4")

    caption = "\n".join(
        [f"Stock market update | {label}", ""]
        + [f"{i}. {t} ({n['source']})" for i, (t, n) in enumerate(zip(texts, fresh), 1)]
        + ["", "Headlines summarised from public news sources.", "", DISCLAIMER, "", HASHTAGS]
    )[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps(
        {"caption": caption, "video_name": name, "keys": [n["key"] for n in fresh]}))
    print(f"Built out/reel.mp4 ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB)")
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
                    caption=meta["caption"], share_to_feed="true",
                    access_token=token)["id"]
    for _ in range(60):  # video processing can take a few minutes
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

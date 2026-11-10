# -*- coding: utf-8 -*-
"""
Serialized cinematic Reel engine (thriller/suspense), separate from the cartoon
flow in main.py.  Run with:  python main.py series   (or  series publish ).

- Picks the NEXT unposted part of the series (ordered release).
- ElevenLabs emotional Telugu voice (reuses main.make_voice -> main.tts).
- Cue-based cinematic music per scene emotion + a sting on the cliffhanger.
- Dark cinematic background, big word-revealed Telugu subtitles, hook + cliffhanger,
  and a "Watch full episode on YouTube" end-card on the last part of an episode.
- Writes out/reel.mp4 + out/meta.json, compatible with main.publish().

Dev: SILENT_TTS=1 renders with silent placeholder audio (offline pipeline test).
"""
import json
import os
import pathlib
import random
import re
import subprocess
import datetime as dt

import numpy as np
from PIL import Image, ImageDraw

import main as M
from series_data import SERIES

W, H, FPS, SR = M.W, M.H, M.FPS, M.SR
OUT = M.OUT

# emotion -> (top gradient rgb, accent rgb for subtitles)
THEME = {
    "tense":     ((14, 18, 34), (120, 200, 255)),
    "mystery":   ((10, 14, 28), (150, 160, 255)),
    "emotional": ((22, 18, 30), (255, 210, 180)),
    "shock":     ((34, 10, 14), (255, 120, 120)),
    "calm":      ((16, 22, 30), (200, 220, 255)),
}
GOLD = (255, 214, 102)
WHITE = (245, 246, 250)


# ---------------------------------------------------------------- selection
def _part_id(ep, part):
    return f"{SERIES['id']}-e{ep}p{part}"


def _flat_parts():
    out = []
    for ep in SERIES["episodes"]:
        parts = ep["parts"]
        for p in parts:
            out.append((ep, p, p is parts[-1]))
    return out


def choose_part(state):
    used = {u["id"] for u in state.get("used", [])}
    forced = os.getenv("PART")  # e.g. "1-e1p2"
    for ep, p, is_last in _flat_parts():
        pid = _part_id(ep["ep"], p["part"])
        if forced:
            if pid.endswith(forced) or pid == forced:
                return ep, p, is_last, pid
        elif pid not in used:
            return ep, p, is_last, pid
    return None, None, None, None


# ---------------------------------------------------------------- segments
def _say(t):
    """Clean text for speech: drop any [delivery tags] so free TTS doesn't read them."""
    return re.sub(r"\[[^\]]*\]", "", t).strip()


def build_segments(ep, part):
    """hook + scenes + cliffhanger -> list of segments for make_voice + render.
    Voice text == caption (minus any tags), so what's spoken matches what's shown."""
    segs = []
    segs.append({"kind": "hook", "who": "narrator", "emotion": "tense",
                 "caption": part["hook"], "text": _say(part["hook"]),
                 "spk": {"voice": "female", "semi": -0.5}})
    for sc in part["scenes"]:
        segs.append({"kind": "scene", "who": sc["who"], "emotion": sc.get("emotion", "tense"),
                     "caption": sc["caption"], "text": _say(sc["caption"]),
                     "spk": {"voice": sc.get("voice", "female"),
                             "semi": -1.0 if sc.get("voice") == "male" else -0.3}})
    segs.append({"kind": "cliff", "who": "narrator", "emotion": "shock",
                 "caption": part["cliffhanger"], "text": _say(part["cliffhanger"]),
                 "spk": {"voice": "female", "semi": -0.5}})
    return segs


# ---------------------------------------------------------------- music beds
def _norm(x, peak=0.9):
    m = float(np.abs(x).max()) if len(x) else 0.0
    return (x / m * peak).astype(np.float32) if m > 1e-9 else x.astype(np.float32)


def bed(emotion, seconds, rng):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    if emotion in ("tense", "shock"):
        base = 58.0 if emotion == "tense" else 46.0
        out = 0.6 * np.sin(2 * np.pi * base * t) + 0.3 * np.sin(2 * np.pi * (base * 1.5) * t)
        out *= 0.8 + 0.2 * np.sin(2 * np.pi * 0.15 * t)            # slow tremolo
        beat = 1.15
        for k in range(int(seconds / beat)):                       # faint heartbeat
            i0 = int((k * beat + rng.uniform(0, 0.05)) * SR)
            thp = np.arange(int(0.18 * SR)) / SR
            thump = np.sin(2 * np.pi * 60 * thp) * np.exp(-14 * thp) * 0.5
            seg = min(len(thump), n - i0)
            if seg > 0:
                out[i0:i0 + seg] += thump[:seg]
    elif emotion == "emotional":
        root = 196.0
        out = sum(0.25 * np.sin(2 * np.pi * root * r * t) for r in (1.0, 1.25, 1.5))
        out *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.08 * t) ** 2       # slow swell
    else:  # mystery / calm : airy shimmer
        out = np.zeros(n)
        for f in (523, 659, 784, 988):
            out += 0.12 * np.sin(2 * np.pi * (f + rng.uniform(-2, 2)) * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.05 * t))
    return _norm(out, 0.6)


def _fit(arr, need):
    if len(arr) >= need:
        return arr[:need]
    return np.concatenate([arr, np.zeros(need - len(arr), np.float32)])


def build_music(segs, starts, durs, total, rng):
    music = np.zeros(int(total * SR), np.float32)
    for i, seg in enumerate(segs):
        s = starts[i]
        e = starts[i + 1] if i + 1 < len(segs) else total
        if seg["kind"] == "cliff":
            s = s + 0.45                       # ~0.45s of silence before the reveal
        i0, i1 = int(s * SR), int(min(e, total) * SR)
        need = i1 - i0
        if need > 0:
            music[i0:i1] += _fit(bed(seg["emotion"], need / SR + 0.1, rng), need)
    # soft tail bed under the end-card
    tail0 = int(starts[-1] * SR + durs[-1] * SR)
    if tail0 < len(music):
        need = len(music) - tail0
        music[tail0:] += _fit(bed("emotional", need / SR + 0.1, rng), need) * 0.7
    return _norm(music, 0.85)


def build_sfx(segs, starts, durs, n, rng):
    sfx = np.zeros(n, np.float32)
    # whoosh into each scene change, sting on the cliffhanger
    events = []
    for i, seg in enumerate(segs):
        if i > 0:
            events.append((starts[i] - 0.32, "whoosh"))
        if seg["kind"] == "cliff":
            events.append((starts[i] + 0.5, "sting"))
    noise = np.random.default_rng(rng.randint(0, 10 ** 6)).normal(0, 1, int(0.4 * SR))
    whoosh = np.convolve(noise, np.ones(30) / 30, mode="same") * np.sin(np.pi * np.linspace(0, 1, len(noise))) ** 2 * 1.3
    st = np.arange(int(0.9 * SR)) / SR
    sting = (np.sin(2 * np.pi * (220 - 120 * st) * st) + 0.6 * np.random.default_rng(1).normal(0, 1, len(st)) * np.exp(-6 * st))
    sting = sting * np.exp(-3.5 * st) * 1.2
    for t, kind in events:
        snd = sting if kind == "sting" else whoosh
        g = 0.8 if kind == "sting" else 0.4
        i0 = max(0, int(t * SR))
        seg = min(len(snd), n - i0)
        if seg > 0:
            sfx[i0:i0 + seg] += snd[:seg] * g
    return sfx


# ---------------------------------------------------------------- rendering
def _vignette(size):
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    v = np.clip(1.0 - 0.75 * np.clip(d - 0.2, 0, 1.3) ** 2, 0, 1)
    return v[..., None]


_VIG = None
_BG = {}


def bg_for(emotion):
    if emotion in _BG:
        return _BG[emotion]
    global _VIG
    if _VIG is None:
        _VIG = _vignette((W, H))
    top, _ = THEME.get(emotion, THEME["tense"])
    g = np.zeros((H, W, 3), np.float32)
    for c in range(3):
        g[..., c] = np.linspace(top[c], max(0, top[c] * 0.25), H)[:, None]
    g *= _VIG
    img = Image.fromarray(np.clip(g, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    _BG[emotion] = img
    return img


def wrap(d, text, size, max_w):
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if M.mixed_width(d, trial, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_caption(base, text, size, color, y_center):
    img = base.copy()
    d = ImageDraw.Draw(img)
    lines = wrap(d, text, size, W - 150)
    lh = int(size * 1.5)
    total_h = lh * len(lines)
    y = y_center - total_h // 2
    band = Image.new("RGBA", (W, total_h + 80), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rounded_rectangle((0, 0, W - 1, total_h + 79), 36, fill=(0, 0, 0, 150))
    img.alpha_composite(band, (0, y - 40))
    for ln in lines:
        M.draw_mixed(d, (W // 2, y + lh // 2), ln, size, color, anchor="mm", sw=6, stroke_fill=(0, 0, 0, 255))
        y += lh
    return img


def end_card(ep, part, yt_cta):
    img = bg_for("emotional").copy()
    d = ImageDraw.Draw(img)
    M.draw_mixed(d, (W // 2, H // 2 - 220), SERIES["title_te"], 84, GOLD, anchor="mm", sw=6)
    if yt_cta:
        bar = Image.new("RGBA", (760, 150), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rounded_rectangle((0, 0, 759, 149), 28, fill=(210, 20, 20, 255))
        img.alpha_composite(bar, ((W - 760) // 2, H // 2 - 70))
        M.draw_mixed(d, (W // 2, H // 2 + 5), "▶  YouTube", 64, WHITE, anchor="mm", sw=4)
        M.draw_mixed(d, (W // 2, H // 2 + 170), "పూర్తి ఎపిసోడ్ యూట్యూబ్‌లో చూడండి", 52, WHITE, anchor="mm", sw=5)
        M.draw_mixed(d, (W // 2, H // 2 + 250), "లింక్ కింద కామెంట్‌లో 👇", 44, GOLD, anchor="mm", sw=4)
    else:
        M.draw_mixed(d, (W // 2, H // 2 + 10), f"Part {part + 1} త్వరలో…", 70, WHITE, anchor="mm", sw=5)
        M.draw_mixed(d, (W // 2, H // 2 + 130), "ఫాలో అవ్వండి • షేర్ చేయండి", 48, GOLD, anchor="mm", sw=4)
    return img


def render(segs, starts, durs, total, total_render, ep, part, yt_cta, audio_path, out_path):
    frames_dir = OUT / "frames"
    if frames_dir.exists():
        for f in frames_dir.glob("*.png"):
            f.unlink()
    frames_dir.mkdir(exist_ok=True)
    seg_end = starts[-1] + durs[-1]
    ec = end_card(ep["ep"], part["part"], yt_cta)
    label = f"{SERIES['title_en']} • EP{ep['ep']} • Part {part['part']}"
    nframes = int(total_render * FPS)
    for fi in range(nframes):
        t = fi / FPS
        if t >= seg_end:
            ec.convert("RGB").save(frames_dir / f"f{fi:05d}.png")
            continue
        i = max((j for j in range(len(segs)) if starts[j] <= t), default=0)
        seg = segs[i]
        _, accent = THEME.get(seg["emotion"], THEME["tense"])
        reveal = min(1.0, (t - starts[i]) / max(0.6, min(durs[i], 2.2)))
        words = seg["caption"].split(" ")
        shown = " ".join(words[: max(1, int(round(reveal * len(words))))])
        big = seg["kind"] in ("hook", "cliff")
        size = 72 if big else 60
        color = (255, 120, 120) if seg["kind"] == "cliff" else (GOLD if seg["kind"] == "hook" else WHITE)
        img = draw_caption(bg_for(seg["emotion"]), shown, size, color, int(H * 0.60))
        d = ImageDraw.Draw(img)
        M.draw_mixed(d, (W // 2, 150), label, 40, accent, anchor="mm", sw=4)
        img.convert("RGB").save(frames_dir / f"f{fi:05d}.png")

    vf = ("zoompan=z='min(1.001+0.00035*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
          f"s={W}x{H}:fps={FPS},vignette=PI/5,noise=alls=7:allf=t,eq=contrast=1.06:saturation=1.05")
    M.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames_dir / "f%05d.png"),
           "-i", str(audio_path), "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)])


# ---------------------------------------------------------------- caption
def build_caption(ep, part, is_last):
    L = [f"{SERIES['title_en']} — EP{ep['ep']} Part {part['part']}: {ep['ep_title_te']}", "",
         part["hook"], "",
         "ఈ కథ మీకు నచ్చిందా? Part " + str(part["part"] + 1) + " కోసం ఫాలో అవ్వండి 👇", ""]
    if part.get("yt_cta"):
        L += [f"▶ పూర్తి ఎపిసోడ్ యూట్యూబ్‌లో చూడండి: {SERIES['youtube_url']}", ""]
    L.append(" ".join(SERIES["hashtags"][:5] + ["#telugu"]))
    return "\n".join(L)[:2200]


# ---------------------------------------------------------------- dev voice
def _install_silent_tts():
    def silent(text, path, spk):
        secs = max(1.4, min(9.0, len(text) * 0.055))
        M.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               f"sine=frequency=1:duration={secs:.2f}", "-ar", str(SR), str(path)])
    M.tts = silent


# ---------------------------------------------------------------- entry
def generate():
    OUT.mkdir(exist_ok=True)
    state = M.load_state()
    ep, part, is_last, pid = choose_part(state)
    if not ep:
        print("No unposted part left in the series. Add more episodes in series_data.py.")
        M.set_output("has_post", "false")
        return
    print(f"Series part: {SERIES['title_en']} EP{ep['ep']} Part {part['part']}  ({pid})")
    if os.getenv("SILENT_TTS") == "1":
        _install_silent_tts()

    segs = build_segments(ep, part)
    rng = random.Random(int(dt.datetime.now().timestamp()))
    voice, starts, durs, envs, total = M.make_voice(segs)

    cta = 3.4 if part.get("yt_cta") else 1.6
    voice = np.concatenate([voice, np.zeros(int(cta * SR), np.float32)])
    total_render = total + cta
    music = _fit(build_music(segs, starts, durs, total_render, rng), len(voice))
    sfx = _fit(build_sfx(segs, starts, durs, len(voice), rng), len(voice))
    audio = OUT / "audio.wav"
    M.mix_audio(voice, music, sfx, audio)

    render(segs, starts, durs, total, total_render, ep, part, part.get("yt_cta", False), audio, OUT / "reel.mp4")

    now = dt.datetime.now(M.IST)
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    title = f"{SERIES['title_en']} EP{ep['ep']} Part {part['part']}"
    caption = build_caption(ep, part, is_last)
    (OUT / "meta.json").write_text(json.dumps(
        {"caption": caption, "video_name": name, "story_id": pid, "title": title}, ensure_ascii=False),
        encoding="utf-8")
    size_mb = os.path.getsize(OUT / "reel.mp4") / 1e6
    print(f"Built out/reel.mp4 ({size_mb:.1f} MB, {total_render:.0f}s)")
    try:
        M.telegram_send(OUT / "reel.mp4", title, caption)
    except Exception as e:
        print(f"Telegram skipped: {e}")
    M.set_output("has_post", "true")
    M.set_output("video_name", name)

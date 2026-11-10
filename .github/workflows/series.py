# -*- coding: utf-8 -*-
"""
Serialized cinematic Reel engine (thriller/suspense).
Run with:  python main.py series   (or  series publish ).

Fixes vs previous version:
  - series_mix_audio: music gain raised to 0.5 (was 0.09 when voice plays)
  - series_tts: emotion-aware pitch/rate makes voice less robotic
  - draw_action: displays scene action description at bottom of screen
  - build_sfx: adds gasp + horror_sting events for thriller atmosphere
  - VOICE_FX patched to thriller preset (no echo, adds bass warmth)

Dev: SILENT_TTS=1 renders with silent placeholder audio (offline pipeline test).
"""
import asyncio
import json
import os
import pathlib
import random
import re
import subprocess
import wave
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
GOLD  = (255, 214, 102)
WHITE = (245, 246, 250)
CYAN  = (90, 230, 220)

# voice FX for thriller: punchy EQ, no echo, slight warmth
_THRILLER_VOICE_FX = (
    "highpass=f=80,"
    "acompressor=threshold=0.1:ratio=3:attack=3:release=80,"
    "equalizer=f=2500:t=q:w=1.2:g=3,"
    "equalizer=f=120:t=q:w=0.8:g=2"
)

# edge-tts voice parameters per emotion (rate %, pitch hz)
_EMOTION_VOICE = {
    "tense":     ("+0%",   "-3Hz"),
    "mystery":   ("-10%",  "-6Hz"),
    "shock":     ("+12%",  "+5Hz"),
    "emotional": ("-6%",   "-2Hz"),
    "calm":      ("-4%",   "0Hz"),
}


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
    forced = os.getenv("PART")
    for ep, p, is_last in _flat_parts():
        pid = _part_id(ep["ep"], p["part"])
        if forced:
            if pid.endswith(forced) or pid == forced:
                return ep, p, is_last, pid
        elif pid not in used:
            return ep, p, is_last, pid
    return None, None, None, None


# ---------------------------------------------------------------- voice (emotion-aware)
def series_tts(text, path, spk):
    """Thriller voice with emotion-aware pitch/rate so it sounds less robotic."""
    gender = spk.get("voice", "female")
    emotion = spk.get("emotion", "tense")

    # Try ElevenLabs if key provided
    if os.getenv("ELEVENLABS_API_KEY") and (
            os.getenv(f"ELEVENLABS_VOICE_{gender.upper()}") or os.getenv("ELEVENLABS_VOICE_ID")):
        try:
            M.eleven_tts(text, path, gender)
            if path.exists() and path.stat().st_size > 1000:
                return
        except Exception as e:
            print(f"ElevenLabs failed ({e}); using edge-tts")

    # edge-tts with emotion-aware pitch + rate
    try:
        import edge_tts
        voice = "te-IN-MohanNeural" if gender == "male" else "te-IN-ShrutiNeural"
        rate_pct, pitch_hz = _EMOTION_VOICE.get(emotion, ("+0%", "0Hz"))
        # add tiny random variation so adjacent lines don't sound identical
        base_rate = int(rate_pct.replace("%", "").replace("+", ""))
        vary = random.choice([-2, 0, 0, 2, 4])
        final_rate = f"{base_rate + vary:+d}%"
        asyncio.run(
            edge_tts.Communicate(text, voice, rate=final_rate, pitch=pitch_hz).save(str(path))
        )
        if path.exists() and path.stat().st_size > 1000:
            return
    except Exception as e:
        print(f"edge-tts failed ({e}); falling back to gTTS")

    from gtts import gTTS
    gTTS(text, lang="te").save(str(path))


def _say(t):
    """Strip [delivery tags] so TTS doesn't read them aloud."""
    return re.sub(r"\[[^\]]*\]", "", t).strip()


def _strip_action(text):
    """Remove [ action ] annotations from text before passing to voice."""
    return re.sub(r"\[.*?\]", "", text).strip()


# ---------------------------------------------------------------- segments
def build_segments(ep, part):
    """hook + scenes + cliffhanger -> list of segments for make_voice + render."""
    segs = []
    segs.append({
        "kind": "hook", "who": "narrator", "emotion": "tense",
        "caption": part["hook"],
        "text": _strip_action(part["hook"]),
        "action": "",
        "spk": {"voice": "female", "emotion": "tense", "semi": -0.5},
    })
    for sc in part["scenes"]:
        emotion = sc.get("emotion", "tense")
        spk_text = _strip_action(sc.get("text", sc["caption"]))
        segs.append({
            "kind": "scene", "who": sc["who"], "emotion": emotion,
            "caption": sc["caption"],
            "text": spk_text,
            "action": sc.get("action", ""),
            "spk": {
                "voice": sc.get("voice", "female"),
                "emotion": emotion,
                "semi": -1.0 if sc.get("voice") == "male" else -0.3,
            },
        })
    segs.append({
        "kind": "cliff", "who": "narrator", "emotion": "shock",
        "caption": part["cliffhanger"],
        "text": _strip_action(part["cliffhanger"]),
        "action": "",
        "spk": {"voice": "female", "emotion": "shock", "semi": -0.5},
    })
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
        out *= 0.8 + 0.2 * np.sin(2 * np.pi * 0.15 * t)
        beat = 1.15 if emotion == "tense" else 0.9   # faster heartbeat for shock
        for k in range(int(seconds / beat)):
            i0 = int((k * beat + rng.uniform(0, 0.05)) * SR)
            thp = np.arange(int(0.18 * SR)) / SR
            thump = np.sin(2 * np.pi * 60 * thp) * np.exp(-14 * thp) * 0.6
            seg = min(len(thump), n - i0)
            if seg > 0:
                out[i0:i0 + seg] += thump[:seg]
    elif emotion == "emotional":
        root = 196.0
        out = sum(0.25 * np.sin(2 * np.pi * root * r * t) for r in (1.0, 1.25, 1.5))
        out *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.08 * t) ** 2
    else:  # mystery / calm
        out = np.zeros(n)
        for f in (523, 659, 784, 988):
            out += 0.12 * np.sin(2 * np.pi * (f + rng.uniform(-2, 2)) * t) * (
                0.5 + 0.5 * np.sin(2 * np.pi * 0.05 * t))
    return _norm(out, 0.65)


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
            s = s + 0.45
        i0, i1 = int(s * SR), int(min(e, total) * SR)
        need = i1 - i0
        if need > 0:
            music[i0:i1] += _fit(bed(seg["emotion"], need / SR + 0.1, rng), need)
    tail0 = int(starts[-1] * SR + durs[-1] * SR)
    if tail0 < len(music):
        need = len(music) - tail0
        music[tail0:] += _fit(bed("emotional", need / SR + 0.1, rng), need) * 0.7
    return _norm(music, 0.90)


# ---------------------------------------------------------------- SFX
def build_sfx(segs, starts, durs, n, rng):
    """Whoosh on scene changes, sting on cliffhanger, gasp on shock scenes."""
    sfx = np.zeros(n, np.float32)
    events = []
    for i, seg in enumerate(segs):
        if i > 0:
            events.append((starts[i] - 0.32, "whoosh"))
        if seg["kind"] == "cliff":
            events.append((starts[i] + 0.5, "horror_sting"))
        if seg.get("emotion") == "shock":
            events.append((starts[i] + 0.1, "gasp"))

    rng2 = np.random.default_rng(rng.randint(0, 10 ** 6))

    # whoosh
    noise = rng2.normal(0, 1, int(0.4 * SR))
    whoosh = (np.convolve(noise, np.ones(30) / 30, mode="same")
              * np.sin(np.pi * np.linspace(0, 1, len(noise))) ** 2 * 1.3).astype(np.float32)

    # horror sting: descending tone + noise burst
    st = np.arange(int(0.9 * SR)) / SR
    sting = (np.sin(2 * np.pi * (220 - 120 * st) * st)
             + 0.6 * rng2.normal(0, 1, len(st)) * np.exp(-6 * st))
    sting = (sting * np.exp(-3.5 * st) * 1.4).astype(np.float32)

    # gasp: sharp high-pitched noise burst
    gp = np.arange(int(0.25 * SR)) / SR
    gasp_env = np.exp(-18 * gp) * np.sin(np.pi * gp / gp[-1])
    gasp_snd = (rng2.normal(0, 1, len(gp)).astype(np.float32)
                * gasp_env.astype(np.float32)
                * 1.5)

    snd_map = {"whoosh": (whoosh, 0.45), "horror_sting": (sting, 0.85), "gasp": (gasp_snd, 0.60)}

    for t, kind in events:
        snd, g = snd_map.get(kind, (whoosh, 0.4))
        i0 = max(0, int(t * SR))
        seg = min(len(snd), n - i0)
        if seg > 0:
            sfx[i0:i0 + seg] += snd[:seg] * g
    return sfx


# ---------------------------------------------------------------- custom mix (louder music for thriller)
def series_mix_audio(voice, music, sfx, path):
    """Higher music presence than the kids-story default (0.09 -> 0.4 when voice plays)."""
    n = len(voice)
    k = int(0.3 * SR)
    c = np.cumsum(np.abs(voice), dtype=np.float64)
    env = (c[k:] - c[:-k]) / k
    env = np.concatenate([
        np.full(k // 2, env[0]),
        env,
        np.full(n - len(env) - k // 2, env[-1]),
    ])
    duck = np.clip(env / 0.06, 0, 1)
    # music gain: 0.55 when silent, 0.35 when voice is at peak (thriller needs audible bed)
    gain = (0.55 - 0.20 * duck).astype(np.float32)
    fade = np.ones(n, np.float32)
    f = int(1.5 * SR)
    fade[-f:] = np.linspace(1, 0, f)
    mix = np.clip(
        (voice + music[:n] * gain + sfx[:n] * 0.65) * fade,
        -1, 1,
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())


# ---------------------------------------------------------------- rendering helpers
def _vignette(size):
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    v = np.clip(1.0 - 0.75 * np.clip(d - 0.2, 0, 1.3) ** 2, 0, 1)
    return v[..., None]


_VIG = None
_BG  = {}


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
    bd.rounded_rectangle((0, 0, W - 1, total_h + 79), 36, fill=(0, 0, 0, 155))
    img.alpha_composite(band, (0, y - 40))
    for ln in lines:
        M.draw_mixed(d, (W // 2, y + lh // 2), ln, size, color,
                     anchor="mm", sw=6, stroke_fill=(0, 0, 0, 255))
        y += lh
    return img


def draw_action(base, text, y_bottom):
    """Render a small action caption line near the bottom of the screen."""
    if not text.strip():
        return base
    img = base.copy()
    d = ImageDraw.Draw(img)
    # strip outer brackets for display
    display = text.strip().lstrip("[").rstrip("]").strip()
    lines = wrap(d, display, 36, W - 120)
    lh = int(36 * 1.45)
    total_h = lh * len(lines) + 32
    band = Image.new("RGBA", (W - 80, total_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rounded_rectangle((0, 0, W - 81, total_h - 1), 18, fill=(0, 0, 0, 185))
    img.alpha_composite(band, (40, y_bottom - total_h))
    dy = y_bottom - total_h + 16
    for ln in lines:
        M.draw_mixed(d, (W // 2, dy + lh // 2), ln, 36, CYAN,
                     anchor="mm", sw=3, stroke_fill=(0, 0, 0, 255))
        dy += lh
    return img


def end_card(ep, part, yt_cta):
    img = bg_for("emotional").copy()
    d = ImageDraw.Draw(img)
    M.draw_mixed(d, (W // 2, H // 2 - 220), SERIES["title_te"], 84, GOLD, anchor="mm", sw=6)
    if yt_cta:
        bar = Image.new("RGBA", (760, 150), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rounded_rectangle((0, 0, 759, 149), 28, fill=(210, 20, 20, 255))
        img.alpha_composite(bar, ((W - 760) // 2, H // 2 - 70))
        M.draw_mixed(d, (W // 2, H // 2 + 5),   "YouTube-lo Full Episode",   64, WHITE, anchor="mm", sw=4)
        M.draw_mixed(d, (W // 2, H // 2 + 170),  "Link kinda comment lo undi",  52, WHITE, anchor="mm", sw=5)
        M.draw_mixed(d, (W // 2, H // 2 + 250),  "Link kindata comment lo 👇",  44, GOLD,  anchor="mm", sw=4)
    else:
        M.draw_mixed(d, (W // 2, H // 2 + 10),  f"Part {part + 1} vasthundi...", 70, WHITE, anchor="mm", sw=5)
        M.draw_mixed(d, (W // 2, H // 2 + 130),  "Follow avvandi • Share cheyandi", 48, GOLD, anchor="mm", sw=4)
    return img


def render(segs, starts, durs, total, total_render, ep, part, yt_cta, audio_path, out_path):
    frames_dir = OUT / "frames"
    if frames_dir.exists():
        for f in frames_dir.glob("*.png"):
            f.unlink()
    frames_dir.mkdir(exist_ok=True)
    seg_end = starts[-1] + durs[-1]
    ec = end_card(ep["ep"], part["part"], yt_cta)
    label = f"{SERIES['title_en']}  EP{ep['ep']}  Part {part['part']}"
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
        big   = seg["kind"] in ("hook", "cliff")
        size  = 72 if big else 60
        color = (
            (255, 120, 120) if seg["kind"] == "cliff"
            else (GOLD if seg["kind"] == "hook" else WHITE)
        )
        img = draw_caption(bg_for(seg["emotion"]), shown, size, color, int(H * 0.55))
        # action caption at bottom (appears after first word is revealed)
        if seg.get("action") and reveal > 0.15:
            img = draw_action(img, seg["action"], H - 80)
        d = ImageDraw.Draw(img)
        M.draw_mixed(d, (W // 2, 150), label, 40, accent, anchor="mm", sw=4)
        img.convert("RGB").save(frames_dir / f"f{fi:05d}.png")

    vf = (
        "zoompan=z='min(1.001+0.00035*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
        f"s={W}x{H}:fps={FPS},"
        "vignette=PI/5,"
        "noise=alls=7:allf=t,"
        "eq=contrast=1.06:saturation=1.05"
    )
    M.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS), "-i", str(frames_dir / "f%05d.png"),
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_path),
    ])


# ---------------------------------------------------------------- caption
def build_caption(ep, part, is_last):
    L = [
        f"{SERIES['title_en']} — EP{ep['ep']} Part {part['part']}: {ep['ep_title_te']}", "",
        part["hook"], "",
        "Nachindaa? Part " + str(part["part"] + 1) + " kosam follow avvandi 👇", "",
    ]
    if part.get("yt_cta"):
        L += [f"Full episode YouTube lo: {SERIES['youtube_url']}", ""]
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
    else:
        # patch in emotion-aware TTS and thriller voice FX
        M.tts = series_tts
        M.VOICE_FX = _THRILLER_VOICE_FX

    segs = build_segments(ep, part)
    rng  = random.Random(int(dt.datetime.now().timestamp()))
    voice, starts, durs, envs, total = M.make_voice(segs)

    cta   = 3.4 if part.get("yt_cta") else 1.6
    voice = np.concatenate([voice, np.zeros(int(cta * SR), np.float32)])
    total_render = total + cta

    music = _fit(build_music(segs, starts, durs, total_render, rng), len(voice))
    sfx   = _fit(build_sfx(segs, starts, durs, len(voice), rng), len(voice))
    audio = OUT / "audio.wav"
    series_mix_audio(voice, music, sfx, audio)

    render(segs, starts, durs, total, total_render,
           ep, part, part.get("yt_cta", False), audio, OUT / "reel.mp4")

    now  = dt.datetime.now(M.IST)
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    title   = f"{SERIES['title_en']} EP{ep['ep']} Part {part['part']}"
    caption = build_caption(ep, part, is_last)
    (OUT / "meta.json").write_text(
        json.dumps({"caption": caption, "video_name": name, "story_id": pid, "title": title},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    size_mb = os.path.getsize(OUT / "reel.mp4") / 1e6
    print(f"Built out/reel.mp4 ({size_mb:.1f} MB, {total_render:.0f}s)")
    try:
        M.telegram_send(OUT / "reel.mp4", title, caption)
    except Exception as e:
        print(f"Telegram skipped: {e}")
    M.set_output("has_post", "true")
    M.set_output("video_name", name)

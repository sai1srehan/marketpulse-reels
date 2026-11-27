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
import base64
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
import shutil
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
SHOW_PROGRESS = os.getenv("SHOW_PROGRESS", "0") == "1"      # the thin line segments at the top
SHOW_SUBTITLES = os.getenv("SHOW_SUBTITLES", "0") == "1"    # caption card with highlighted words
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
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("used", [])
    return st


def save_state(meta):
    st = load_state()
    st["used"].append({"id": meta["story_id"], "title": meta["title"], "moral": meta.get("moral", ""),
                       "ts": dt.datetime.now(dt.timezone.utc).isoformat()})
    st["used"] = st["used"][-400:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_ig_titles(user_id, token):
    try:
        r = requests.get(f"{GRAPH}/{user_id}/media", params=sign({"fields": "caption", "limit": 25,
                                                                                "access_token": token}), timeout=30)
        r.raise_for_status()
        return [(m.get("caption") or "").split("\n")[0] for m in r.json().get("data", [])]
    except Exception as e:
        print(f"Recent-post check skipped: {e}")
        return []


def _tri(t):
    x = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", "_", (t or "").lower())
    return {x[i:i + 3] for i in range(len(x) - 2)}


def _sim(a, b):
    ta, tb = _tri(a), _tri(b)
    return len(ta & tb) / max(1, len(ta | tb))


TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")
POSES = {"idle", "run", "sleep"}


def gemini_key():
    return (os.getenv("GEMINI_API_KEY") or "").strip()


def anthropic_key():
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def ai_enabled():
    return bool(gemini_key() or anthropic_key()) and os.getenv("AI_STORIES") != "0"


_GEMINI_MODELS = None


def gemini_models():
    """Newest 'gemini-X-flash' your key can use (auto-detected), or GEMINI_MODEL if you set it."""
    global _GEMINI_MODELS
    if _GEMINI_MODELS is not None:
        return _GEMINI_MODELS
    forced = (os.getenv("GEMINI_MODEL") or "").strip()
    if forced:
        _GEMINI_MODELS = [forced]
        return _GEMINI_MODELS
    found, lite = [], []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": gemini_key()}, params={"pageSize": 200}, timeout=30)
        r.raise_for_status()
        for md in r.json().get("models", []):
            name = md.get("name", "").replace("models/", "")
            mm = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-flash(-lite)?", name)
            if mm and "generateContent" in md.get("supportedGenerationMethods", []):
                (lite if mm.group(2) else found).append((tuple(int(x) for x in mm.group(1).split(".")), name))
    except Exception as e:
        print(f"Gemini model list failed ({e}); using built-in names")
    found.sort(reverse=True)
    lite.sort(reverse=True)
    # flash-lite models have their own (usually larger) free quota: try them before giving up on Gemini
    names = [n for _, n in found][:3] + [n for _, n in lite][:2] + \
        ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    _GEMINI_MODELS = list(dict.fromkeys(names))
    return _GEMINI_MODELS


def free_llm_text(prompt):
    """Free, no-key text model (Pollinations)."""
    r = requests.post("https://text.pollinations.ai/openai", timeout=240,
                      json={"model": os.getenv("POLLINATIONS_TEXT_MODEL", "openai"), "temperature": 1.0,
                            "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}})
    r.raise_for_status()
    print(f"Story written by Pollinations ({r.json().get('model', '?')}, free)")
    return r.json()["choices"][0]["message"]["content"]


def llm_json_text(prompt):
    """Ask the configured AI (Gemini first if GEMINI_API_KEY is set, otherwise Claude, otherwise the free
    no-key Pollinations text API) and return its raw text."""
    if not gemini_key() and not anthropic_key():
        return free_llm_text(prompt)
    if gemini_key():
        for model in list(gemini_models()):
            try:
                for wait in (0, 20, 40):   # 500/503 "high demand" is temporary: wait and retry the same model
                    time.sleep(wait)
                    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                      headers={"x-goog-api-key": gemini_key(), "Content-Type": "application/json"},
                                      json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                            "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0,
                                                                 "maxOutputTokens": 8192}}, timeout=100)
                    if r.status_code not in (500, 503):
                        break
                    print(f"Gemini {model} busy ({r.status_code}), retrying")
                if r.status_code == 404:
                    print(f"Gemini model {model} not available, trying the next one")
                    continue
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if text.strip():
                    if gemini_models()[0] != model:  # remember the model that works
                        gemini_models().remove(model)
                        gemini_models().insert(0, model)
                    print(f"Story written by Gemini ({model})")
                    return text
                print(f"Gemini {model} returned no text: {str(r.json())[:200]}")
            except requests.HTTPError as e:
                print(f"Gemini {model} failed: {e.response.status_code} {e.response.text[:200]}")
                if e.response.status_code in (401, 403):
                    break   # key problem: other models will fail the same way
                # 429 (quota) and 400 are per model: the next, older model has its own free quota
            except Exception as e:
                print(f"Gemini {model} error: {e}")
        if not anthropic_key():
            print("All Gemini models failed; using the free Pollinations text model instead.")
            return free_llm_text(prompt)
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": anthropic_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    print("Story written by Claude")
    return r.json()["content"][0]["text"]


def _build_ai_story(raw, pair, scene_hint):
    """Validate Claude's JSON and convert it into the same structures the hand-written stories use."""
    cast = raw["cast"]
    assert isinstance(cast, list) and sorted(cast) == sorted(pair), "wrong cast"
    scene = raw.get("scene") if raw.get("scene") in ("day", "sunset", "night") else scene_hint
    assert TELUGU_RE.search(raw["title"]) and TELUGU_RE.search(raw["moral"]), "title/moral not Telugu"
    items = raw["lines"]
    assert 5 <= len(items) <= 12, "line count"
    lines, emotions, staging, cut_line, drop_line = [], [], {}, None, None
    props_now = {}
    for i, it in enumerate(items):
        who, text = it["who"], str(it["text"]).strip()
        assert who == "narrator" or who in cast, f"bad speaker {who}"
        assert TELUGU_RE.search(text) and 1 <= len(text.split()) <= 26, "bad line"
        lines.append((who, text))
        emo = it.get("emo", "neutral")
        emotions.append(emo if emo in EMO else "neutral")
        upd = {}
        if isinstance(it.get("pos"), dict):
            upd["pos"] = {k: float(v) for k, v in it["pos"].items() if k in cast and -300 <= float(v) <= 1600}
        if isinstance(it.get("pose"), dict):
            upd["pose"] = {k: v for k, v in it["pose"].items() if k in cast and v in POSES}
        if isinstance(it.get("lift"), dict):
            upd["lift"] = {k: max(0, min(450, int(v))) for k, v in it["lift"].items() if k in cast}
        if isinstance(it.get("others"), dict):
            upd["emo"] = {k: v for k, v in it["others"].items() if k in cast and v in EMO}
        if isinstance(it.get("props"), dict):
            pr = {}
            for name, val in it["props"].items():
                if name in ("tree", "flag", "pond") and -100 <= float(val) <= 1200:
                    pr[name] = float(val)
                elif name == "net" and val in cast:
                    pr["net"] = val
            if props_now.get("net") and not pr.get("net") and i > 0:
                cut_line = i - 1
            props_now = pr
            upd["props"] = pr
        if it.get("drop") and "crow" in cast and "fox" in cast:
            drop_line = i
        if upd:
            staging[i] = upd
    extra = [t for t in (raw.get("hashtags") or []) if isinstance(t, str) and re.fullmatch(r"#[A-Za-z0-9_]{3,30}", t)]
    tag = raw.get("tag") if isinstance(raw.get("tag"), str) and re.fullmatch(r"#[A-Za-z0-9_]{3,30}", raw.get("tag", "")) else "#story"
    hook = str(raw.get("hook", "")).strip()
    hook = hook if (TELUGU_RE.search(hook) and len(hook.split()) <= 14) else ""
    story = {"id": "ai-" + hashlib.md5(raw["title"].encode()).hexdigest()[:8], "title": raw["title"].strip(), "scene": scene, "hook": hook,
             "cast": cast, "tag": tag, "hashtags": extra, "lines": lines, "moral": raw["moral"].strip(),
             "emotions": emotions, "staging": staging}
    if cut_line is not None:
        story["cut_line"] = cut_line
    if drop_line is not None:
        story["drop_line"] = drop_line
    return story


def ai_story(used_titles, used_morals):
    """The AI (Gemini or Claude) writes a brand-new kids' moral story: script, acting and staging."""
    if not ai_enabled():
        return None
    pair = random.sample(list(CAST), 2)
    scene_hint = random.choice(["day", "sunset", "night"])
    names = {c: f"{c} ({CAST[c]['name']})" for c in pair}
    prompt = f"""You write short moral stories for a Telugu kids' Instagram Reel. A 3D-toon engine will act them out.

Characters (use EXACTLY these two, no others): {names[pair[0]]} and {names[pair[1]]}, plus a "narrator".
Scene look: "{scene_hint}" (you may change it to day, sunset or night if the story needs).
Write a NEW, original kid-friendly story with a clear, positive moral (traditional-fable style is fine, retold in your own words).
No violence, nothing scary or sad for long, no politics or religion, no real brands or copyrighted characters.
Do NOT repeat or closely imitate any of these titles: {json.dumps(used_titles[-40:], ensure_ascii=False)}
and avoid these morals: {json.dumps(used_morals[-40:], ensure_ascii=False)}

Language: simple, warm, natural SPOKEN Telugu (everyday words). 6 to 8 lines, each at most 14 words (the whole reel must stay under 30 seconds); mix narrator lines with the two characters talking.
Also write a \"hook\": ONE short Telugu teaser question or surprising statement (max 10 words) that makes people want to watch to the end (for example about what will happen), and make the moral something people want to send to a friend.
Each line is an object:
  "who": "narrator" or one of {pair}
  "text": the Telugu line
  "emo": how the speaker looks: one of {list(EMO)}
Optional staging (include only when something CHANGES; it persists to later lines):
  "pos": {{"<character id>": x}}  x is screen position 150..900 (left character starts ~290, right ~780); use 1450 to run off-screen right
  "pose": {{"<character id>": "idle"|"run"|"sleep"}}
  "lift": {{"<character id>": pixels}}  e.g. 330 to stand on a tree branch
  "others": {{"<character id>": emotion}}  how a NON-speaking character looks
  "props": {{"tree": x, "flag": x, "pond": x, "net": "<character id>"}}  give the FULL current set whenever it changes; "pond" shows the lion's reflection if a lion is present
  "drop": true  (only for a crow and fox story: the crow drops its bread on this line)
Return ONLY JSON: {{"title": "Telugu title", "hook": "Telugu teaser (max 10 words)", "scene": "...", "cast": {json.dumps(pair)}, "moral": "one Telugu sentence",
"tag": "#englishtag", "hashtags": ["#kidsstories", "#moralstory"], "lines": [ ... ]}}"""
    for attempt in range(3):
        try:
            text = llm_json_text(prompt)
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            story = _build_ai_story(json.loads(text), pair, scene_hint)
            if any(_sim(story["title"], t) > 0.5 for t in used_titles) or any(_sim(story["moral"], m_) > 0.5 for m_ in used_morals if m_):
                raise ValueError("too similar to an earlier story")
            print(f"AI story written: {story['title']} ({pair[0]} + {pair[1]})")
            return story
        except Exception as e:
            print(f"AI story attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(8 * (attempt + 1))  # give a temporary rate limit time to clear
    return None


PIXAR_INSPIRATION = {   # random nudges only; the AI invents the characters, plot and dialogue itself
    "setting": ["a village pond", "a monsoon rooftop", "a busy Hyderabad street at night", "a mango orchard", "a school garden",
                "a grandmother's kitchen", "a paddy field at harvest", "a Sankranti kite festival", "a riverside market",
                "a coconut grove by the sea", "a hilltop temple town", "a toy shop after closing", "a snowy Himalayan village",
                "a desert caravan in Rajasthan", "a bird sanctuary lake", "a railway station in the rain"],
    "theme": ["sharing", "honesty", "courage", "patience", "teamwork", "kindness to animals", "gratitude", "saying sorry",
              "not judging by looks", "helping the elderly", "saving water", "keeping a promise", "trying again after failing",
              "listening to others", "being yourself", "protecting nature"],
}


def pixar_ai_story(used_titles, used_morals):
    """The AI invents a complete, original story: characters (with looks and voices), Telugu script and Pixar shot prompts."""
    setting, theme = random.choice(PIXAR_INSPIRATION["setting"]), random.choice(PIXAR_INSPIRATION["theme"])
    prompt = f"""You are the writer and director of a Telugu kids' Instagram Reel made as a Pixar-style 3D animated short.
Invent a COMPLETELY NEW, original story. Invent your own characters (animals, birds, children, or personified everyday objects,
1 to 3 characters). Inspiration (you may reinterpret): setting near "{setting}", theme around "{theme}".
Kid-friendly, warm, a little funny or surprising; no violence, nothing scary, no politics or religion, no brands, no
copyrighted or famous characters, no real people. Do NOT reuse or imitate these titles: {json.dumps(used_titles[-40:], ensure_ascii=False)}
and avoid these morals: {json.dumps(used_morals[-40:], ensure_ascii=False)}

Script: simple, natural SPOKEN Telugu (everyday words). 6 to 8 lines, each at most 14 words, so the reel stays under 35 seconds.
Mix a "narrator" with the characters talking. The first line must already be intriguing.

Shots: 5 or 6 shots that together cover every line in order. Each shot has an English image prompt for an AI image model:
start with the camera framing (close-up / medium / wide / low angle), then ONE clear main subject doing ONE clear action with a
clear facial expression, then the background and lighting. Image models draw only one or two subjects well, so keep each shot simple.
Never put any text, letters or signs in the picture.
LANGUAGE RULE: "title", "hook", "moral", character "name" and line "text" are Telugu. "setting", character "look" and
shot "prompt" MUST be written in ENGLISH only (the image model cannot read Telugu).

Return ONLY JSON:
{{"title": "Telugu title", "hook": "Telugu teaser question (max 10 words)", "moral": "one Telugu sentence",
"scene": "day" | "sunset" | "night",
"setting": "English description of the world, colours and lighting (one sentence)",
"characters": [{{"id": "short english id like ammu_duck", "name": "Telugu name", "voice": "male" | "female",
   "pitch": integer -5..5 (higher = smaller/younger), "look": "English visual description: species, colours, clothes, size, eyes (one sentence)"}}],
"lines": [{{"who": "narrator" or a character id, "text": "Telugu line"}}],
"shots": [{{"lines": [line indexes, 0-based], "chars": [character ids visible], "prompt": "English image prompt"}}],
"tag": "one real English hashtag about the theme, e.g. #kindness", "hashtags": ["#kidsstories", "#moralstory"]}}
Every shot's "chars" must list the character ids that appear in it (at least one).
The narrator is only a voice: never list the narrator in "characters" or "chars"."""
    tries = 3 if (gemini_key() or anthropic_key()) else 5   # the free model fails more often
    for attempt in range(tries):
        try:
            raw = json.loads(re.sub(r"^```(?:json)?|```$", "", llm_json_text(prompt).strip(), flags=re.M).strip())
            chars = raw["characters"]
            assert 1 <= len(chars) <= 3, "character count"
            speakers, looks = {}, {}
            for k, c in enumerate(chars):
                cid = re.sub(r"[^a-z0-9_]", "", str(c["id"]).lower())[:24] or f"c{k}"
                if cid == "narrator" or "narrator" in str(c.get("look", "")).lower():
                    continue   # the narrator is only a voice, never drawn
                assert TELUGU_RE.search(str(c["name"])) and len(str(c["look"])) > 15, "bad character"
                assert not TELUGU_RE.search(str(c["look"]) + str(raw.get("setting", ""))), "look/setting must be English"
                hue = (k * 0.33 + random.random() * 0.2) % 1
                rgb = tuple(int(255 * v) for v in __import__("colorsys").hsv_to_rgb(hue, 0.65, 0.85))
                speakers[cid] = {"name": str(c["name"]).strip(), "voice": "female" if c.get("voice") == "female" else "male",
                                 "semi": max(-5, min(5, int(c.get("pitch", 0)))), "rate": 0, "color": rgb}
                looks[cid] = str(c["look"]).strip()
                c["id"] = cid
            assert speakers, "no drawable characters"
            lines = []
            for it in raw["lines"]:
                who = re.sub(r"[^a-z0-9_]", "", str(it["who"]).lower())
                who = who if who in speakers else "narrator"
                text = str(it["text"]).strip()
                assert TELUGU_RE.search(text) and 1 <= len(text.split()) <= 26, "bad line"
                lines.append((who, text))
            assert 5 <= len(lines) <= 10, "line count"
            assert TELUGU_RE.search(raw["title"]) and TELUGU_RE.search(raw["moral"]), "title/moral not Telugu"
            shots = []
            for sh in raw["shots"]:
                ids = [i for i in sh.get("lines", []) if isinstance(i, int) and 0 <= i < len(lines)]
                vis = [c for c in sh.get("chars", []) if c in looks] or list(looks)   # never a shot without the story's characters
                assert len(str(sh.get("prompt", ""))) > 20, "shot prompt too short"
                assert not TELUGU_RE.search(str(sh["prompt"])), "shot prompt must be English"
                # name the exact characters and forbid extras, or the image model invents people that are not in the story
                shots.append({"lines": ids, "scene": f"{sh['prompt']} The ONLY characters in the picture: "
                              f"{' AND '.join(looks[c] for c in vis)}. No other people or animals. Setting: {raw.get('setting', '')}"})
            assert 3 <= len(shots) <= 8, "shot count"
            if any(_sim(raw["title"], t) > 0.5 for t in used_titles) or any(_sim(raw["moral"], m_) > 0.5 for m_ in used_morals if m_):
                raise ValueError("too similar to an earlier story")
            placeholder = {"#englishtag", "#tag", "#hashtag"}   # free models sometimes copy the example literally
            extra = [t for t in (raw.get("hashtags") or []) if isinstance(t, str) and re.fullmatch(r"#[A-Za-z0-9_]{3,30}", t)
                     and t.lower() not in placeholder]
            if str(raw.get("tag", "")).lower() in placeholder:
                raw["tag"] = ""
            hook = str(raw.get("hook", "")).strip()
            story = {"id": "px-" + hashlib.md5(raw["title"].encode()).hexdigest()[:8], "title": raw["title"].strip(),
                     "scene": raw.get("scene") if raw.get("scene") in ("day", "sunset", "night") else "day",
                     "hook": hook if TELUGU_RE.search(hook) and len(hook.split()) <= 14 else "",
                     "cast": list(speakers), "speakers": speakers, "looks": " ".join(looks.values()), "shots": shots,
                     "lines": lines, "moral": raw["moral"].strip(), "hashtags": extra,
                     "tag": raw["tag"] if re.fullmatch(r"#[A-Za-z0-9_]{3,30}", str(raw.get("tag", ""))) else "#story"}
            print(f"AI Pixar story: {story['title']} | " + " | ".join(f"{k}: {v}" for k, v in looks.items())[:400])
            return story
        except Exception as e:
            print(f"AI Pixar story attempt {attempt + 1} failed: {e}")
            if attempt < tries - 1:
                time.sleep(8 * (attempt + 1))
    return None


def choose_story(state, ig_titles):
    forced = os.getenv("STORY")
    if forced:
        for s in STORIES:
            if s["id"] == forced:
                return s
    if ai_enabled() and os.getenv("REQUIRE_CLIPS") != "1":
        used_titles = [u.get("title", "") for u in state["used"]] + [x["title"] for x in STORIES] + [t.replace("📖", "").strip() for t in ig_titles]
        used_morals = [u.get("moral", "") for u in state["used"]] + [x["moral"] for x in STORIES]
        story = ai_story([t for t in used_titles if t], [m_ for m_ in used_morals if m_])
        if story:
            return story
        print("AI story unavailable: falling back to the built-in stories.")
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
        return None
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
    fit = lambda a: np.pad(a[:n], (0, max(0, n - len(a))))   # rounding can leave a track a sample short
    mix = np.clip((voice + fit(music) * gain + fit(sfx) * 0.35) * fade, -1, 1)
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
    st = story.get("staging") or STAGING.get(story["id"], {})
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
    emos = story.get("emotions") or EMOTIONS.get(sid, [])
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
    draw_mixed(td, (480, 70), "చివరి వరకు చూడండి", 52, (150, 90, 210), anchor="mm")
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
    cut_li = story.get("cut_line", {"lion_mouse": 6}.get(sid))
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
        if SHOW_PROGRESS:
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
        if SHOW_SUBTITLES:
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


# ------------------------------------------- free Pixar-style clip maker (no paid API, no GPU needed)
# 1) one Pixar-style keyframe per shot from a free image model (Pollinations.ai, or Hugging Face FLUX if HF_TOKEN is set)
# 2) each keyframe becomes a moving shot with a smooth virtual-camera move (ffmpeg zoompan)
# 3) saved as clips/<story id>/shotN.mp4, which the cinematic clip mode above already knows how to use
PIXAR_STILL = ("Pixar-style 3D animated movie still, rounded 3D CGI render (not anime, not 2D), cute cartoon characters with "
               "big glossy expressive eyes, any people are wholesome kids in modest fully clothed kid-friendly outfits, "
               "bright warm cinematic lighting, well-lit faces, vibrant colours, soft depth of field, no text, no watermark")
SPECIES_EN = {
    "boy": "RAMU: a cheerful 8-year-old Indian boy with black hair, blue t-shirt and brown shorts.",
    "snake": "SNAKE: a friendly bright-green cartoon snake with big kind eyes.",
    "lion": "LION: a big golden cartoon lion with a thick brown mane.",
    "mouse": "MOUSE: a tiny grey cartoon mouse with big round ears and shiny eyes.",
    "rabbit": "RABBIT: a fluffy white cartoon rabbit with long pink-inside ears.",
    "tortoise": "TORTOISE: a kind green cartoon tortoise with a dome shell and gentle eyes.",
    "crow": "CROW: a glossy black cartoon crow with a bright orange beak.",
    "fox": "FOX: a clever orange cartoon fox with white cheeks and a bushy tail.",
}
SCENE_EN = {"day": "a sunny afternoon in a lush green Indian forest clearing",
            "sunset": "golden sunset light at the edge of an Indian village forest",
            "night": "a magical moonlit night in an Indian forest with glowing fireflies"}
CAMERA_MOVES = ["in", "pan_r", "out", "pan_l", "up"]


def pixar_shots(story):
    """English scene prompts per shot. AI Pixar stories bring their own; hand-written ones use SHOTS;
    older AI stories get shots built from cast + emotions."""
    if story.get("shots"):
        return [sh["scene"] for sh in story["shots"]], ""   # looks are already inside each shot prompt
    if SHOTS.get(story["id"]):
        return [sh["scene"] for sh in SHOTS[story["id"]]], CHARACTER_LOOKS.get(story["id"], "")
    looks = " ".join(SPECIES_EN.get(CAST[c]["species"], "") for c in story["cast"])
    names = [CAST[c]["species"] for c in story["cast"]]
    place = SCENE_EN.get(story.get("scene"), SCENE_EN["day"])
    emos = story.get("emotions") or []
    framing = ["Wide establishing shot", "Medium shot", "Close-up", "Medium two-shot", "Low-angle shot"]
    scenes = []
    for k, i in enumerate(range(0, len(story["lines"]), 2)):
        who = next((w for w, _ in story["lines"][i:i + 2] if w != "narrator"), None)
        emo = emos[i] if i < len(emos) else "happy"
        if who:
            focus = f"the {CAST[who]['species']} looks {emo} and talks to the {next((n for n in names if n != CAST[who]['species']), names[0])}"
        else:
            focus = f"the {' and the '.join(names)} together, feeling {emo}"
        scenes.append(f"{framing[k % len(framing)]} in {place}: {focus}.")
    return scenes, looks


def free_image(prompt, seed, path):
    """Pixar-style keyframe from a free model. Returns the provider name."""
    hf = (os.getenv("HF_TOKEN") or "").strip()
    if hf:
        try:
            r = requests.post(f"https://router.huggingface.co/hf-inference/models/{os.getenv('HF_IMAGE_MODEL', 'black-forest-labs/FLUX.1-schnell')}",
                              headers={"Authorization": f"Bearer {hf}"},
                              json={"inputs": prompt, "parameters": {"width": 768, "height": 1344, "seed": seed}}, timeout=180)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                path.write_bytes(r.content)
                return "huggingface"
            print(f"  Hugging Face failed ({r.status_code}): {r.text[:200]}; trying Pollinations")
        except Exception as e:
            print(f"  Hugging Face failed: {e}; trying Pollinations")
    from urllib.parse import quote
    global _POLL_LAST
    url = "https://image.pollinations.ai/prompt/" + quote(prompt[:1500], safe="")
    params = {"width": 1080, "height": 1920, "seed": seed, "model": os.getenv("POLLINATIONS_MODEL", "flux"), "nologo": "true"}
    gap = float(os.getenv("POLLINATIONS_GAP", "47"))   # free tier: 1 image per 45 s per IP
    last = ""
    for attempt in range(5):
        wait = _POLL_LAST + gap - time.time()
        if wait > 0:
            print(f"  waiting {wait:.0f}s for the free image tier")
            time.sleep(wait)
        try:
            _POLL_LAST = time.time()
            r = requests.get(url, params=params, headers={"User-Agent": "telugu-story-reel-bot"}, timeout=180)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                img = Image.open(__import__("io").BytesIO(r.content)).convert("RGB")
                img = img.crop((0, 0, img.width, int(img.height * 0.94)))   # trim the free-tier watermark strip
                img.save(path)
                return "pollinations"
            last = f"{r.status_code} {r.text[:150]}"
            print(f"  image attempt {attempt + 1} failed: {last}")
        except Exception as e:
            last = str(e)
            print(f"  image attempt {attempt + 1} failed: {last}")
    raise RuntimeError(f"free image generation failed: {last}")


_POLL_LAST = 0.0


def cover_fit(path):
    img = Image.open(path).convert("RGB")
    s = max(W / img.width, H / img.height)
    img = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    l, t = (img.width - W) // 2, (img.height - H) // 2
    img.crop((l, t, l + W, t + H)).save(path)


def still_to_clip(img_path, out_path, seconds, move):
    """Smooth eased camera move over a still (2x upscale first so zoompan does not jitter)."""
    n = int(seconds * FPS)
    e = f"((1-cos(PI*on/{n}))/2)"
    z = {"in": f"1+0.16*{e}", "out": f"1.16-0.16*{e}"}.get(move, "1.12")
    x = {"pan_r": f"(iw-iw/zoom)*{e}", "pan_l": f"(iw-iw/zoom)*(1-{e})"}.get(move, "(iw-iw/zoom)/2")
    y = f"(ih-ih/zoom)*(1-{e})" if move == "up" else "(ih-ih/zoom)/2"
    vf = f"scale={W * 2}:{H * 2},zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS},format=yuv420p"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(img_path), "-vf", vf, "-frames:v", str(n),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(out_path)])


def make_pixar_clips(story, force=False):
    """Fills clips/<story id>/ with Pixar-style shots. Keyframes are kept in keyframes/ so you can also
    feed them to a free image-to-video tool by hand and drop the result in as shotN.mp4."""
    d = (OUT / "pixar" if story["id"].startswith("px-") else CLIPS_DIR) / story["id"]   # one-off AI stories stay out of the repo
    kf = d / "keyframes"
    kf.mkdir(parents=True, exist_ok=True)
    scenes, looks = pixar_shots(story)
    seed = int(hashlib.md5(story["id"].encode()).hexdigest()[:6], 16)   # same seed per story = more consistent look
    secs = float(os.getenv("PIXAR_CLIP_SECONDS", "10"))
    made = []
    for i, scene in enumerate(scenes, 1):
        clip, still = d / f"shot{i}.mp4", kf / f"shot{i}.png"
        if clip.exists() and not force:
            made.append(clip)
            continue
        try:
            if force or not still.exists():
                # scene first: image models weight the start of the prompt most, so the action must lead
                prov = free_image(f"{scene} {looks} {PIXAR_STILL}.", seed + i, still)
                cover_fit(still)
                print(f"  shot {i}/{len(scenes)} keyframe via {prov}")
            still_to_clip(still, clip, secs, CAMERA_MOVES[(i - 1) % len(CAMERA_MOVES)])
            made.append(clip)
        except Exception as e:
            print(f"  shot {i} failed: {str(e)[:300]}")
            if len(made) < 2:
                raise
            print(f"  continuing with the {len(made)} shots already made")
            break
    print(f"Pixar clips ready in {d}/ ({len(made)} of {len(scenes)} shots)")
    return made


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
    for k, sh in enumerate(story.get("shots") or SHOTS.get(story["id"], [])):
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
        if SHOW_PROGRESS:
            seg_w = (W - 120 - 8 * (len(segments) - 1)) / len(segments)
            for i in range(len(segments)):
                x0 = 60 + i * (seg_w + 8)
                d.rounded_rectangle((x0, 142, x0 + seg_w, 150), 4, fill=(150, 150, 160))
                f = min(1.0, max(0.0, (t - bounds[i]) / (bounds[i + 1] - bounds[i])))
                if f > 0:
                    d.rounded_rectangle((x0, 142, x0 + max(8, seg_w * f), 150), 4, fill=WHITE)
        if SHOW_SUBTITLES:
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
# ------------------------------------------------ Gemini Omni mode (AI-made cinematic video)
OMNI_MODEL = (os.getenv("GEMINI_OMNI_MODEL") or "gemini-omni-1.2-flash").strip()
GEN_BASE = "https://generativelanguage.googleapis.com/v1beta"


def omni_wanted():
    return os.getenv("OMNI_VIDEOS") == "1" and bool(gemini_key())


def omni_today_count():
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return sum(1 for x in load_state().get("omni", []) if str(x).startswith(today))


def record_omni():
    st = load_state()
    st.setdefault("omni", []).append(dt.datetime.now(dt.timezone.utc).isoformat())
    st["omni"] = st["omni"][-200:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def omni_concept(used_titles, used_morals):
    """Gemini invents one original 15-second micro-story: Telugu caption + an English video prompt."""
    prompt = f"""You are the creative director of a Telugu Instagram Reels page that posts short, heart-touching animated films.
Invent ONE original 15-second Pixar-style 3D animated micro-story with a clear, positive, shareable message.
Cast: two or three cute, expressive personified characters (animals, everyday objects, plants...) in an Indian setting
(a festival, village lane, school, kitchen, garden, Hyderabad street, monsoon, harvest...). Warm cinematic look.
Rules: kid-friendly; no violence; no politics or religion; no real people; no brands or copyrighted characters.
Do NOT reuse or imitate these titles: {json.dumps(used_titles[-40:], ensure_ascii=False)}
and avoid these morals: {json.dumps(used_morals[-40:], ensure_ascii=False)}
Do not use matchsticks, firecrackers, candles or airports as the idea.
The FIRST 2 SECONDS must already show the surprising or emotional moment (no slow intro), and the last second should flow back into the first so the reel loops.
The Telugu \"caption\" must be something people want to send to a friend, and add a Telugu \"question\" that invites a real comment.

Return ONLY JSON with these keys:
"title": short Telugu title
"caption": the Telugu message shown on screen at the top (max 18 words, 1 or 2 emotional sentences, simple spoken Telugu)
"moral": one Telugu sentence
"question": one short Telugu question that invites a real comment
"video_prompt": ONE English prompt for the video model, at least 120 words. Start with: "Pixar-style 3D animated short film, cinematic lighting, shallow depth of field, vertical 9:16." Describe each character's exact look
(colours, shapes, big expressive eyes), the setting, lighting, camera moves (close-ups, slow push-ins) and a clear emotional story in timed beats:
[0-4s] ... [4-8s] ... [8-12s] ... [12-15s] ... with small physical interactions (a hug, sharing, helping). Sound design: emotional cinematic background music with a steady soft beat and a gentle melodic humming vocal line (no lyrics), plus sparkle, whoosh and small magical sound effects that match the action.
End with: "No spoken dialogue. No on-screen text, no subtitles, no logos."
"tag": one English hashtag for the theme, "hashtags": ["#kidsstories", "#moralstory"]"""
    for attempt in range(3):
        try:
            raw = json.loads(re.sub(r"^```(?:json)?|```$", "", llm_json_text(prompt).strip(), flags=re.M).strip())
            assert all(TELUGU_RE.search(str(raw.get(k, ""))) for k in ("title", "caption", "moral")), "Telugu fields missing"
            assert len(str(raw.get("video_prompt", ""))) > 200, "video prompt too short"
            if any(_sim(raw["title"], t) > 0.5 for t in used_titles) or any(_sim(raw["moral"], m_) > 0.5 for m_ in used_morals if m_):
                raise ValueError("too similar to an earlier video")
            return raw
        except Exception as e:
            print(f"Omni concept attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(8 * (attempt + 1))
    return None


def _find_video(node):
    if isinstance(node, dict):
        if node.get("type") == "video" and (node.get("data") or node.get("uri")):
            return node
        for v in node.values():
            r = _find_video(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_video(v)
            if r:
                return r
    return None


def omni_generate(video_prompt, out_path):
    """One call = one clip (up to 15 s, 9:16, with native audio). Costs about $0.07 per second at 720p."""
    hdr = {"x-goog-api-key": gemini_key(), "Content-Type": "application/json"}
    body = {"model": OMNI_MODEL, "input": video_prompt,
            "response_format": {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "duration": "15s", "delivery": "uri"}}
    r = requests.post(f"{GEN_BASE}/interactions", headers=hdr, json={**body, "store": False, "background": False, "stream": False}, timeout=900)
    if r.status_code == 400 and ("Unknown name" in r.text or "Invalid JSON" in r.text):
        r = requests.post(f"{GEN_BASE}/interactions", headers=hdr, json=body, timeout=900)  # retry without the optional flags
    if not r.ok:
        hint = ""
        if r.status_code in (401, 403):
            hint = " (check the key, and that billing is enabled: Omni has no free tier)"
        elif r.status_code == 429:
            hint = " (rate limit or daily quota reached)"
        elif r.status_code == 404:
            hint = " (model not available to this key; set GEMINI_OMNI_MODEL)"
        raise RuntimeError(f"Omni request failed {r.status_code}{hint}: {r.text[:300]}")
    data = r.json()
    iid = data.get("id")
    waited = 0
    while data.get("status") not in (None, "completed") and iid and waited < 600:
        if data.get("status") in ("failed", "cancelled", "incomplete"):
            raise RuntimeError(f"Omni generation {data.get('status')}: {str(data)[:300]}")
        time.sleep(10)
        waited += 10
        data = requests.get(f"{GEN_BASE}/interactions/{iid}", headers=hdr, timeout=60).json()
    vid = _find_video(data)
    if not vid:
        raise RuntimeError(f"Omni returned no video: {str(data)[:300]}")
    if vid.get("data"):
        out_path.write_bytes(base64.b64decode(vid["data"]))
        return
    m = re.search(r"files/([A-Za-z0-9_-]+)", vid["uri"])
    for _ in range(60):  # wait until the hosted file is ready
        if not m:
            break
        info = requests.get(f"{GEN_BASE}/files/{m.group(1)}", headers=hdr, timeout=60).json()
        if info.get("state") in (None, "ACTIVE"):
            break
        if info.get("state") == "FAILED":
            raise RuntimeError("Omni video file failed")
        time.sleep(5)
    with requests.get(vid["uri"], headers={"x-goog-api-key": gemini_key()}, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        size = 0
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                size += len(chunk)
                if size > 120 * 1024 * 1024:
                    raise RuntimeError("video too large")
                f.write(chunk)


def has_audio(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return "audio" in p.stdout


def caption_png(text, width, size, path):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines, cur = [], ""
    for w in text.split():
        test = f"{cur} {w}".strip()
        if cur and mixed_width(tmp, test, size) > width - 70:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    lh = int(size * 1.5)
    img = Image.new("RGBA", (width, lh * len(lines) + 24), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        draw_mixed(d, (width / 2, 10 + i * lh), ln, size, (255, 255, 255, 255), anchor="mt", sw=max(3, size // 10))
    img.save(path)
    return img.height


def finish_omni_video(raw, caption, out_path, voiceover=None):
    """Re-encode for Instagram (30 fps, H.264/AAC) and put the Telugu caption on top like the reference reel."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=s=x:p=0", str(raw)], capture_output=True, text=True).stdout.strip()
    vw, vh = (int(x) for x in probe.split("x")) if "x" in probe else (720, 1280)
    png = OUT / "omni_caption.png"
    caption_png(caption, vw, max(34, vw // 17), png)
    y = int(vh * 0.115)
    audio = has_audio(raw)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-loop", "1", "-i", str(png)]
    voice = None
    if voiceover is None:
        voiceover = os.getenv("OMNI_VOICEOVER") == "1"
    if voiceover:
        mp3, voice = OUT / "omni_voice.mp3", OUT / "omni_voice.wav"
        tts(caption, mp3, NARRATOR)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(voice)])
        cmd += ["-i", str(voice)]
    fc = f"[0:v]fps=30,format=yuv420p[v0];[1:v]format=rgba,fade=t=in:st=0.5:d=0.7:alpha=1[c];[v0][c]overlay=0:{y}:shortest=1[v]"
    maps = ["-map", "[v]"]
    if voice and audio:
        fc += ";[0:a]volume=0.5[a0];[2:a]adelay=900|900[a1];[a0][a1]amix=inputs=2:duration=first:normalize=0[a]"
        maps += ["-map", "[a]"]
    elif voice:
        fc += ";[2:a]adelay=900|900[a]"
        maps += ["-map", "[a]"]
    elif audio:
        maps += ["-map", "0:a"]
    cmd += ["-filter_complex", fc] + maps + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                             "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", "-t", "15.5", str(out_path)]
    run(cmd)


def generate_omni(state, ig_titles):
    """Returns True if a finished reel was written. Never raises: any problem means 'use the built-in engine'."""
    limit = int(os.getenv("OMNI_DAILY_LIMIT", "2"))
    if omni_today_count() >= limit:
        print(f"Omni daily limit ({limit}) reached; using the built-in engine to protect your budget.")
        return False
    try:
        used_titles = [u.get("title", "") for u in state["used"]] + [t.replace("📖", "").strip() for t in ig_titles]
        used_morals = [u.get("moral", "") for u in state["used"]]
        idea = omni_concept([t for t in used_titles if t], [m_ for m_ in used_morals if m_])
        if not idea:
            return False
        print(f"Omni idea: {idea['title']}")
        raw = OUT / "omni_raw.mp4"
        omni_generate(idea["video_prompt"], raw)
        record_omni()
        now = dt.datetime.now(IST)
        finish_omni_video(raw, idea["caption"], OUT / "reel.mp4")
    except Exception as e:
        print(f"Omni video failed: {str(e)[:500]}")
        return False
    return post_idea(idea, "omni-", "Gemini Omni")


def build_caption(idea, post=False):
    """Instagram caption + hashtags for a story reel, a fact reel (kind=facts) or a fact image post (post=True)."""
    # Instagram allows at most 5 hashtags: big, relevant tags beat many tiny ones. 4 proven reach tags + 1 that rotates
    # (the story's own theme tag when it is a popular one), so posts don't all look identical to the algorithm.
    limit = int(os.getenv("HASHTAG_LIMIT", "5"))
    if idea.get("kind") == "devotional":
        tags = []
        for tg in [idea.get("month_tag", ""), str(idea.get("tag", "")).lower()] + \
                  os.getenv("DEVOTION_HASHTAGS", "#telugu,#devotional,#pooja,#hindufestivals").split(","):
            tg = tg.strip()
            if re.fullmatch(r"#[a-z0-9_]{3,30}", tg) and tg not in tags:
                tags.append(tg)
        return "\n".join([f"🪔 {idea['title']}", "", idea["caption"], "", "స్వైప్ చేసి అన్నీ చూడండి 👉",
                          "సేవ్ చేసుకోండి, మీ కుటుంబ సభ్యులకు షేర్ చేయండి 🙏",
                          (idea.get("question") or "మీ ఇంట్లో ఎలా చేస్తారు? కామెంట్‌లో చెప్పండి") + " 👇",
                          "రోజూ పండుగలు, పూజలు, ఆచారాల కోసం ఫాలో అవ్వండి 🙏", "",
                          str(idea.get("search") or "తెలుగు పండుగలు | పూజ విధానం | Telugu devotional"), "",
                          " ".join(tags[:limit])])[:2200]
    facts = idea.get("kind") == "facts"
    core = [t.strip() for t in (os.getenv("FACT_HASHTAGS", "#telugu,#telugufacts,#telugureels,#facts") if facts else
                                # the two biggest Telugu reel tags + Telugu story niche + story niche (+1 reach tag below)
                                os.getenv("HASHTAGS", "#telugu,#telugureels,#telugukathalu,#moralstories")).split(",") if t.strip()]
    # 5th tag rotates through big discovery tags so every reel reaches a slightly different audience
    rotating = [t.strip() for t in os.getenv("ROTATING_HASHTAGS", "#trending,#viral,#explorepage,#reelsinstagram,#trendingreels,"
                                             "#viralreels,#kidsstories,#animation,#cartoon,#teluguviral").split(",") if t.strip()]
    tag = str(idea.get("tag", "")).lower()
    if facts:   # fact reels: the subject's own tag (#space, #gold ...) is usually a big one
        pick = tag if re.fullmatch(r"#[a-z0-9_]{3,30}", tag) else "#didyouknow"
    else:
        pick = rotating[int(hashlib.md5(idea["title"].encode()).hexdigest(), 16) % len(rotating)]
    tag_list = []
    for tg in core + [pick]:
        if tg not in tag_list:
            tag_list.append(tg)
    if facts:
        return "\n".join([f"🤯 {idea['title']}", "", idea["caption"], ""] +
                         (["స్వైప్ చేసి అన్నీ చూడండి 👉"] if post else []) +
                         [f"ఈ {'పోస్ట్' if post else 'రీల్'} మీ ఫ్రెండ్‌కి పంపండి 💌",
                          (idea.get("question") or "మీకు ఇది ముందే తెలుసా?") + " 👇",
                          "రోజూ ఇలాంటి ఆసక్తికరమైన విషయాల కోసం ఫాలో అవ్వండి ❤️", "",
                          "మీకు తెలుసా | తెలుగు ఫ్యాక్ట్స్ | Telugu facts | Did you know", "",
                          " ".join(tag_list[:limit])])[:2200]
    # Instagram now ranks reels by the WORDS in the caption (search + recommendations) more than by hashtags,
    # so the first line and a keyword line name the reel in Telugu and English.
    return "\n".join([f"📖 {idea['title']} | తెలుగు నీతి కథ", "", idea["caption"], "", f"నీతి: {idea['moral']}", "",
                      "చివరి వరకు చూడండి, ఈ కథ మీ పిల్లలకు & ఫ్రెండ్స్‌కి షేర్ చేయండి 💌",
                      (idea.get("question") or "మీకు నచ్చిందా? కామెంట్‌లో చెప్పండి") + " 👇",
                      "రోజూ కొత్త కథల కోసం ఫాలో అవ్వండి ❤️", "",
                      "తెలుగు కథలు | నీతి కథలు | చందమామ కథలు | పిల్లల కథలు | Telugu moral stories | Telugu kathalu | "
                      "Telugu cartoon story | Telugu animated story | Telugu kids stories", "",
                      " ".join(tag_list[:limit])])[:2200]


def post_idea(idea, id_prefix, engine):
    """Caption, hashtags and meta.json for a finished out/reel.mp4 made from an idea dict (Omni or animated short)."""
    now = dt.datetime.now(IST)
    caption = build_caption(idea)
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps({"caption": caption, "video_name": name,
                                               "story_id": id_prefix + hashlib.md5(idea["title"].encode()).hexdigest()[:8],
                                               "title": idea["title"], "moral": idea["moral"]}, ensure_ascii=False), encoding="utf-8")
    print(f"Built out/reel.mp4 with {engine} ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB)")
    telegram_send(OUT / "reel.mp4", idea["title"], caption)
    set_output("has_post", "true")
    set_output("video_name", name)
    return True


# ------------------------------- free animated shorts: Wan 2.2 image-to-video on free Hugging Face ZeroGPU Spaces
# One opening keyframe -> beat 1 animated from it -> beat 2 animated from the LAST frame of beat 1 -> beat 3 ...
# so the whole reel is one continuous scene with the same characters (like the reference reels).
VIDEO_SPACES = [s.strip() for s in os.getenv("VIDEO_SPACES", "zerogpu-aoti/wan2-2-fp8da-aoti-faster,"
                                             "r3gm/wan2-2-fp8da-aoti-preview").split(",") if s.strip()]
VIDEO_NEG = ("static, frozen, blurry, distorted, deformed face, extra limbs, text, subtitles, watermark, logo, flicker, "
             "realistic, photorealistic, creepy, uncanny, morphing, melting, characters merging together, "
             "new characters appearing, low quality, jpeg artifacts")
# the look of every short's start pictures: cute stylised cartoon, never realistic animals
SHORT_STYLE_LEAD = "Adorable stylized 3D cartoon, still frame from a modern Pixar or Disney animated feature film."
SHORT_STYLE = ("Cute cartoon characters with big round heads, huge sparkling glossy eyes, small rounded bodies, soft smooth "
               "simplified textures, friendly appealing faces with clear expressions, clean simple shapes, each character "
               "clearly separated and fully visible (not overlapping), any people are wholesome kids in modest fully clothed "
               "outfits, bright warm cinematic lighting, vibrant saturated colours, soft depth of field, high quality 3D "
               "render, subsurface scattering. Not realistic, not photographic, not creepy. No text, no watermark")


def short_still_prompt(scene, looks):
    return f"{SHORT_STYLE_LEAD} {scene} The ONLY characters in the picture: {looks}. No other people or animals. {SHORT_STYLE}."


class QuotaError(RuntimeError):
    pass


def animate_still(img, motion, out, seconds, seed):
    """Free image-to-video. Returns the Space used. Raises QuotaError when the free daily GPU quota is used up."""
    import shutil
    from gradio_client import Client, handle_file
    tok = (os.getenv("HF_TOKEN") or "").strip() or None
    last = ""
    for space in VIDEO_SPACES:
        secs = seconds
        for _try in range(2):
            try:
                c = Client(space, token=tok, verbose=False)
                res = c.predict(input_image=handle_file(str(img)), prompt=motion, negative_prompt=VIDEO_NEG,
                                steps=int(os.getenv("SHORT_STEPS", "4")),   # fewer steps = less of the free GPU quota per scene
                                duration_seconds=secs, seed=seed, randomize_seed=False, api_name="/generate_video")
                vid = res[0] if isinstance(res, (list, tuple)) else res
                if isinstance(vid, dict):
                    vid = vid.get("video") or vid.get("path")
                shutil.copy(vid, out)
                return space
            except Exception as e:
                msg = str(e)
                last = f"{space}: {msg[:300]}"
                print(f"  video model failed - {last}")
                if "quota" not in msg.lower():
                    break   # not a quota problem: try the next Space
                # "75s requested vs. 71s left": use the leftover quota for a shorter scene instead of wasting it
                m = re.search(r"(\d+)s requested vs\.? (\d+)s left", msg)
                if _try == 0 and m and int(m.group(2)) >= 25:
                    req, left = int(m.group(1)), int(m.group(2))
                    secs = round(max(1.5, secs * left / req * 0.85), 1)
                    print(f"  retrying with a shorter {secs}s scene to fit the {left}s of quota left")
                    continue
                raise QuotaError(msg[:300])   # the free quota is per account across all ZeroGPU Spaces
    raise RuntimeError(last)


# ------------------------------------------------ Kaggle free GPU backend (about 30 GPU hours per week, free)
# The bot pushes a private Kaggle notebook with the job embedded, Kaggle runs it on a free GPU, and the bot downloads
# the clips. Needs the secret KAGGLE_USERNAME plus KAGGLE_API_TOKEN (new) or KAGGLE_KEY (legacy).
KAGGLE_SCRIPT = r'''
import base64, io, json, os, subprocess, sys, time
t0 = time.time()
JOB = json.loads(base64.b64decode("__JOB__").decode())
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "diffusers>=0.35", "transformers>=4.51",
                "accelerate>=1.6", "ftfy", "sentencepiece", "imageio", "imageio-ffmpeg", "spandrel"], check=True)
import gc
import torch
from diffusers import WanImageToVideoPipeline
ngpu = torch.cuda.device_count()
JOB["t0"] = t0
print("GPUs:", ngpu, "x", torch.cuda.get_device_name(0), "| reels:", len(JOB["chains"]), "| scene size",
      JOB["size"][0], "x", JOB["size"][1], "| setup", round(time.time() - t0), "s", flush=True)
dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
# 0) one cute opening picture per reel on the GPU (DreamShaper XL Turbo, free and open); the bot's picture is the fallback
if JOB.get("image_model"):
    try:
        from diffusers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
        try:
            sd = StableDiffusionXLPipeline.from_pretrained(JOB["image_model"], torch_dtype=torch.float16, variant="fp16")
        except Exception:
            sd = StableDiffusionXLPipeline.from_pretrained(JOB["image_model"], torch_dtype=torch.float16)
        sd.scheduler = DPMSolverMultistepScheduler.from_config(sd.scheduler.config)
        sd.to("cuda")
        cp = None
        try:   # prompts longer than 77 tokens
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "compel"], check=True)
            from compel import Compel, ReturnedEmbeddingsType
            cp = Compel(tokenizer=[sd.tokenizer, sd.tokenizer_2], text_encoder=[sd.text_encoder, sd.text_encoder_2],
                        returned_embeddings_type=ReturnedEmbeddingsType.PENULTIMATE_HIDDEN_STATES_NON_NORMALIZED,
                        requires_pooled=[False, True], truncate_long_prompts=False)
        except Exception as e:
            print("long prompts unavailable:", e, flush=True)
        made = 0
        for k, ch in enumerate(JOB["chains"]):
            p = ch.get("image_prompt")
            if not p:
                continue
            kw = dict(prompt=p, negative_prompt=JOB["image_neg"])
            if cp is not None:
                try:
                    c, pc = cp(p)
                    nc, npc = cp(JOB["image_neg"])
                    e, _ = cp("")   # pad the shorter one with empty-prompt chunks (77 tokens each)
                    while c.shape[1] < nc.shape[1]:
                        c = torch.cat([c, e], 1)
                    while nc.shape[1] < c.shape[1]:
                        nc = torch.cat([nc, e], 1)
                    kw = dict(prompt_embeds=c, pooled_prompt_embeds=pc, negative_prompt_embeds=nc, negative_pooled_prompt_embeds=npc)
                except Exception as e:
                    print("long prompt failed, using the short form:", e, flush=True)
            with torch.no_grad():
                im = sd(**kw, width=JOB["image_size"][0], height=JOB["image_size"][1],
                        num_inference_steps=JOB["image_steps"], guidance_scale=JOB["image_cfg"],
                        generator=torch.Generator("cuda").manual_seed(ch["seed"])).images[0]
            im.save("/kaggle/working/c%d_start.png" % k)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            ch["image"] = base64.b64encode(buf.getvalue()).decode()
            made += 1
        print("opening pictures made on the GPU:", made, "at", round(time.time() - t0), "s", flush=True)
        del sd, cp
    except Exception as e:
        print("GPU opening pictures failed, using the bot's pictures:", str(e)[:300], flush=True)
    gc.collect()
    torch.cuda.empty_cache()
# 1) only the big text encoder on GPU 0: encode every prompt once, save the results, free it completely
enc = WanImageToVideoPipeline.from_pretrained(JOB["model"], transformer=None, vae=None, torch_dtype=dtype)
enc.text_encoder.to("cuda")
EMBEDS = []
with torch.no_grad():
    for ch in JOB["chains"]:
        row = []
        for p in ch["beats"]:
            pe, ne = enc.encode_prompt(prompt=p, negative_prompt=JOB["neg"], do_classifier_free_guidance=True, device="cuda")
            row.append((pe.cpu(), ne.cpu()))
        EMBEDS.append(row)
torch.save(EMBEDS, "/tmp/embeds.pt")
del enc, EMBEDS
gc.collect()
torch.cuda.empty_cache()
print("prompts encoded", round(time.time() - t0), "s", flush=True)
# 2) one worker per GPU; each animates whole reels: every scene continues from the LAST frame of the previous one,
#    so a reel is one continuous scene with the same characters
WORKER = """
import base64, io, json, os, sys, time
import numpy as np
import torch
from PIL import Image
from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
from diffusers.utils import export_to_video
JOB = json.load(open("/tmp/job.json"))
mine = json.loads(sys.argv[1])
try:   # T4 has no flash attention: memory-efficient attention never builds the full attention matrix
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
except Exception as e:
    print("sdp setting skipped:", e)
dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
EMBEDS = torch.load("/tmp/embeds.pt")
vae = AutoencoderKLWan.from_pretrained(JOB["model"], subfolder="vae", torch_dtype=torch.float32)
pipe = WanImageToVideoPipeline.from_pretrained(JOB["model"], vae=vae, text_encoder=None, tokenizer=None, torch_dtype=dtype)
try:
    pipe.transformer.enable_layerwise_casting(storage_dtype=torch.float8_e4m3fn, compute_dtype=dtype)
except Exception as e:
    print("8-bit weight storage unavailable:", e)
pipe.transformer.to("cuda")
pipe.vae.to("cuda")
if getattr(pipe, "image_encoder", None) is not None:
    pipe.image_encoder.to("cuda")
try:
    pipe.vae.enable_tiling()
except Exception as e:
    print("vae tiling unavailable:", e)
print("video model on GPU, free GB:", round(torch.cuda.mem_get_info()[0] / 1e9, 1), "| reels", mine, flush=True)
try:
    pipe.transformer.set_attention_backend("_native_efficient")
    print("attention: memory-efficient", flush=True)
except Exception as e:
    print("memory-efficient attention unavailable:", str(e)[:200], flush=True)
# AI upscaler (Real-ESRGAN x2) for sharp frames
UP = None
if JOB.get("upscale"):
    try:
        import urllib.request
        from spandrel import ModelLoader
        urllib.request.urlretrieve(JOB["upscale"], "/tmp/upscaler.pth")
        UP = ModelLoader().load_from_file("/tmp/upscaler.pth").eval()
        print("upscaler ready: x%s" % UP.scale, flush=True)
    except Exception as e:
        print("upscaler unavailable:", str(e)[:200], flush=True)
import gc, traceback
def upscale(frames):
    half = bool(getattr(UP, "supports_half", False))
    UP.to("cuda")
    if half:
        UP.half()
    out = []
    try:
        with torch.no_grad():
            for fr in frames:
                x = torch.from_numpy(np.asarray(fr.convert("RGB")).copy()).permute(2, 0, 1)[None]
                x = x.to("cuda", torch.float16 if half else torch.float32) / 255.0
                y = UP(x).clamp(0, 1)[0].permute(1, 2, 0).float().cpu().numpy()
                out.append(Image.fromarray((y * 255).round().astype("uint8")))
    finally:
        UP.to("cpu")
    return out
def decode(lat):
    # the 5B video model leaves the GPU while the VAE turns latents into frames (the decode needs several GB)
    pipe.transformer.to("cpu")
    gc.collect()
    torch.cuda.empty_cache()
    try:
        with torch.no_grad():
            lat = lat.to("cuda", pipe.vae.dtype)
            mean = torch.tensor(pipe.vae.config.latents_mean).view(1, pipe.vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
            std = 1.0 / torch.tensor(pipe.vae.config.latents_std).view(1, pipe.vae.config.z_dim, 1, 1, 1).to(lat.device, lat.dtype)
            video = pipe.vae.decode(lat / std + mean, return_dict=False)[0]
            frames = pipe.video_processor.postprocess_video(video, output_type="pil")[0]
            del video, lat
            torch.cuda.empty_cache()
        last = frames[-1].copy()   # the next scene continues from this native-size frame
        if UP is not None:
            try:
                frames = upscale(frames)
            except Exception as e:
                print("upscale skipped:", str(e)[:200], flush=True)
        return frames, last
    finally:
        pipe.transformer.to("cuda")
def generate(img, pe, ne, w, h, nf, seed):
    lat = pipe(image=img.resize((w, h), Image.LANCZOS), prompt_embeds=pe.to("cuda"), negative_prompt_embeds=ne.to("cuda"),
               height=h, width=w, num_frames=nf, guidance_scale=JOB["guidance"], num_inference_steps=JOB["steps"],
               output_type="latent", generator=torch.Generator("cpu").manual_seed(seed)).frames
    return decode(lat)
W, H = JOB["size"]
tries = [(W, H, JOB["frames"])] + ([(W, H, 49)] if JOB["frames"] > 49 else [])
last_dur = 0
for c in mine:
    ch = JOB["chains"][c]
    img = Image.open(io.BytesIO(base64.b64decode(ch["image"]))).convert("RGB")
    for i in range(len(ch["beats"])):
        if last_dur and time.time() - JOB["t0"] + last_dur > JOB["budget"]:
            print("time budget reached: reel %d stops after %d scenes" % (c + 1, i), flush=True)
            break
        b0 = time.time()
        pe, ne = EMBEDS[c][i]
        frames = None
        for (w, h, nf) in list(tries):
            failed = None
            try:
                frames, img_next = generate(img, pe, ne, w, h, nf, ch["seed"] + i)
                break
            except torch.OutOfMemoryError:
                failed = " <- ".join("%s:%d %s" % (f.filename.split("/")[-1], f.lineno, f.name)
                                     for f in reversed(traceback.extract_tb(sys.exc_info()[2])[-4:]))
            except RuntimeError as e:
                if "kernel" in str(e).lower():
                    print("memory-efficient attention not possible here (%s): using the default" % str(e)[:150], flush=True)
                    pipe.transformer.set_attention_backend("native")
                    failed = "attention kernel"
                else:
                    failed = "error: " + str(e)[:200]
            # cleanup OUTSIDE the except block, so the failed attempt's GPU memory is really released
            gc.collect()
            torch.cuda.empty_cache()
            if failed == "attention kernel":
                try:
                    frames, img_next = generate(img, pe, ne, w, h, nf, ch["seed"] + i)
                    break
                except torch.OutOfMemoryError:
                    failed = "default attention"
                gc.collect()
                torch.cuda.empty_cache()
            print("scene failed at %dx%d, %d frames (%s)" % (w, h, nf, failed), flush=True)
            tries.remove((w, h, nf))   # never try a setting that already failed again
        if frames is None:
            print("reel %d stops after %d scenes (scene failed)" % (c + 1, i), flush=True)
            break
        export_to_video(frames, "/kaggle/working/c%d_beat%d.mp4" % (c, i + 1), fps=JOB["fps"], quality=9)
        frames, img = None, img_next
        last_dur = time.time() - b0
        print("reel %d scene %d done in %d s (at %d s)" % (c + 1, i + 1, last_dur, time.time() - JOB["t0"]), flush=True)
"""
os.makedirs("/kaggle/working", exist_ok=True)
json.dump(JOB, open("/tmp/job.json", "w"))
open("/tmp/worker.py", "w").write(WORKER)
n = len(JOB["chains"])
g = max(1, min(ngpu, n))
procs = []
for k in range(g):
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(k), PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
    path = "/tmp/w%d.log" % k
    procs.append((subprocess.Popen([sys.executable, "-u", "/tmp/worker.py", json.dumps(list(range(k, n, g)))],
                                   env=env, stdout=open(path, "w"), stderr=subprocess.STDOUT), path))
    if k + 1 < g:   # let the first worker finish loading before the next one fills RAM too
        for _ in range(60):
            time.sleep(5)
            if "video model on GPU" in open(path, errors="ignore").read() or procs[-1][0].poll() is not None:
                break
pos = [0] * g
def pump():
    for k, (p, path) in enumerate(procs):
        txt = open(path, errors="ignore").read()
        new, pos[k] = txt[pos[k]:], len(txt)
        for ln in new.replace("\r", "\n").split("\n"):
            if ln.strip() and "%|" not in ln:
                print("[gpu%d] %s" % (k, ln.strip()[:300]), flush=True)
while any(p.poll() is None for p, _ in procs):
    time.sleep(30)
    pump()
pump()
print("workers finished with exit codes", [p.returncode for p, _ in procs], "at", round(time.time() - t0), "s", flush=True)
'''


def kaggle_user():
    u = (os.getenv("KAGGLE_USERNAME") or "").strip()
    if u:
        return u
    f = pathlib.Path.home() / ".kaggle" / "kaggle.json"
    return json.loads(f.read_text()).get("username", "") if f.exists() else ""


def kaggle_ready():
    """Username plus either the new API token (KAGGLE_API_TOKEN) or the legacy key (KAGGLE_KEY / kaggle.json)."""
    home = pathlib.Path.home() / ".kaggle"
    cred = any((os.getenv(k) or "").strip() for k in ("KAGGLE_API_TOKEN", "KAGGLE_KEY")) \
        or (home / "kaggle.json").exists() or (home / "access_token").exists()
    return bool(kaggle_user()) and cred


def kaggle_cli(*args, timeout=300):
    entry = "import sys; from kaggle.cli import main; sys.argv = ['kaggle'] + sys.argv[1:]; main()"
    p = subprocess.run([sys.executable, "-c", entry, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def _wh(spec, default):
    try:
        w, h = (int(x) // 32 * 32 for x in (spec or default).lower().split("x"))
        return [w, h]
    except Exception:
        return [int(x) for x in default.split("x")]


SDXL_NEG = ("realistic, photo, photorealistic, creepy, scary, ugly, deformed, disfigured, bad anatomy, extra limbs, "
            "extra heads, merged bodies, overlapping characters, blurry, lowres, jpeg artifacts, text, watermark, logo, "
            "signature, 2d, anime, sketch, dark, gloomy")


def kaggle_animate(chains, workdir):
    """chains: [{"key": png path, "image_prompt": str, "beats": [motion prompts], "seed": int}], one reel each.
    One Kaggle session animates every reel (one per T4 GPU, in parallel); inside a reel each scene continues from the
    last frame of the previous one (one continuous scene, like the Nov 20 reel). Returns {reel index: [clip paths]}."""
    import io
    slug = os.getenv("KAGGLE_KERNEL_SLUG", "telugu-reel-video")
    kid = f"{kaggle_user()}/{slug}"
    work = workdir / "kaggle"
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    for old in list(work.glob("*.*")) + list(out.glob("*")):
        old.unlink()
    size = _wh(os.getenv("KAGGLE_SIZE"), "480x832")   # Wan 480p vertical: proven on the T4; upscaled x2 afterwards
    use_sdxl = os.getenv("KAGGLE_IMAGE_MODEL", "").lower() != "none"
    jchains = []
    for ch in chains:
        buf = io.BytesIO()
        Image.open(ch["key"]).convert("RGB").resize(tuple(size), Image.LANCZOS).save(buf, "JPEG", quality=90)
        jchains.append({"image": base64.b64encode(buf.getvalue()).decode(), "beats": ch["beats"], "seed": ch["seed"],
                        "image_prompt": ch.get("image_prompt", "") if use_sdxl else ""})
    job = {"chains": jchains, "neg": VIDEO_NEG, "size": size,
           "fps": 16, "frames": int(os.getenv("KAGGLE_FRAMES", "81")),   # 81 frames at 16 fps = 5 s
           "steps": int(os.getenv("KAGGLE_STEPS", "25")), "guidance": float(os.getenv("KAGGLE_GUIDANCE", "5")),
           "model": os.getenv("KAGGLE_MODEL", "Wan-AI/Wan2.2-TI2V-5B-Diffusers"),
           # opening pictures drawn on the GPU (set KAGGLE_IMAGE_MODEL=none to use the bot's Pollinations pictures)
           "image_model": (os.getenv("KAGGLE_IMAGE_MODEL") or "Lykon/dreamshaper-xl-v2-turbo") if use_sdxl else "",
           "image_neg": SDXL_NEG, "image_size": [768, 1344],
           # Real-ESRGAN x2 upscaler for sharp 1080p frames (KAGGLE_UPSCALER=none turns it off)
           "upscale": "" if os.getenv("KAGGLE_UPSCALER", "").lower() == "none" else os.getenv("KAGGLE_UPSCALER") or
                      "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
           "image_steps": int(os.getenv("KAGGLE_IMAGE_STEPS", "8")), "image_cfg": float(os.getenv("KAGGLE_IMAGE_CFG", "2")),
           # a scene is only started if it should finish before this many minutes
           "budget": int(os.getenv("KAGGLE_BUDGET_MIN", "118")) * 60}
    (work / "gen.py").write_text(KAGGLE_SCRIPT.replace("__JOB__", base64.b64encode(json.dumps(job).encode()).decode()),
                                 encoding="utf-8")
    (work / "kernel-metadata.json").write_text(json.dumps({
        "id": kid, "title": slug, "code_file": "gen.py", "language": "python", "kernel_type": "script",
        "is_private": True, "enable_gpu": True, "enable_internet": True,
        "machine_shape": os.getenv("KAGGLE_ACCELERATOR", "NvidiaTeslaT4"),
        "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": []}), encoding="utf-8")
    code, msg = kaggle_cli("kernels", "push", "-p", str(work))
    print(f"  Kaggle push: {msg[-300:]}")
    if code != 0 or "error" in msg.lower():
        raise RuntimeError(f"Kaggle push failed: {msg[-300:]}")
    # use the exact notebook address Kaggle reports (the account it really went to), not the one built from the secret
    m = re.search(r"kaggle\.com/code/([^/\s]+)/([^/\s?#]+)", msg)
    if m:
        if f"{m.group(1)}/{m.group(2)}".lower() != kid.lower():
            print(f"  note: Kaggle created the notebook as {m.group(1)}/{m.group(2)} "
                  f"(set the KAGGLE_USERNAME secret to '{m.group(1)}')")
        kid = f"{m.group(1)}/{m.group(2)}"
    deadline = time.time() + int(os.getenv("KAGGLE_TIMEOUT_MIN", "140")) * 60
    status, t_start, errors = "", time.time(), 0
    time.sleep(60)
    while time.time() < deadline:
        code, status = kaggle_cli("kernels", "status", kid, timeout=120)
        s = status.lower()
        if code != 0 or "denied" in s or "cannot access" in s or "traceback" in s:
            errors += 1
            if errors == 1:
                print(f"  Kaggle status check failed for {kid}: {status[-500:]}")
            if errors >= 5:
                raise RuntimeError(f"Kaggle status keeps failing for {kid}: {status[-300:]}")
            time.sleep(60)
            continue
        errors = 0
        if "complete" in s or "error" in s or "cancel" in s:
            print(f"  Kaggle finished: {status[-120:]}")
            break
        print(f"  Kaggle GPU working... {int(time.time() - t_start) // 60} min ({status[-60:]})")
        time.sleep(90)
    code, msg = kaggle_cli("kernels", "output", kid, "-p", str(out), timeout=900)
    if code != 0:
        print(f"  Kaggle output download problem: {msg[-400:]}")
    log = next(iter(out.glob("*.log")), None)
    if log:   # show the notebook's own progress lines (GPU type, timings, errors)
        txt = log.read_text(encoding="utf-8", errors="ignore")
        try:
            txt = "".join(x.get("data", "") for x in json.loads(txt))   # Kaggle logs are a JSON list of {data: ...}
        except Exception:
            pass
        keep = ("GPU", "scene", "prompts", "worker", "pictures", "prompt", "attention", "upscale", "Error", "error",
                "Traceback", "memory", "budget", "reel")
        lines_ = [ln.strip() for ln in txt.replace("\r", "\n").split("\n") if ln.strip() and "%|" not in ln]
        tail = [ln for ln in lines_ if any(k in ln for k in keep)][-30:] or lines_[-12:]
        print("  Kaggle notebook log (last lines):\n    " + "\n    ".join(x[:200] for x in tail))
    result = {}
    for k in range(len(chains)):
        # scenes are chained, so only the unbroken run beat1, beat2, ... is usable
        clips = []
        while (out / f"c{k}_beat{len(clips) + 1}.mp4").exists():
            clips.append(out / f"c{k}_beat{len(clips) + 1}.mp4")
        if clips:
            result[k] = clips
            start = out / f"c{k}_start.png"
            if start.exists():
                chains[k]["start_png"] = start
    if not result:
        raise RuntimeError(f"Kaggle finished without clips (status: {status[-120:]})")
    print(f"  Kaggle made " + ", ".join(f"reel {k + 1}: {len(v)} scenes" for k, v in result.items()) +
          f" in {int(time.time() - t_start) // 60} min")
    return result


def last_frame(clip, png):
    run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", str(clip), "-update", "1", "-frames:v", "1", str(png)])


def media_seconds(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(p.stdout.strip() or 0)


def short_concept(used_titles, used_morals, trends=None):
    """AI invents one original animated micro-story (Telugu narration + English image/motion prompts)."""
    setting, theme = random.choice(PIXAR_INSPIRATION["setting"]), random.choice(PIXAR_INSPIRATION["theme"])
    trend_hint = ""
    if trends:
        trend_hint = ("\nTrending in Telangana/Andhra Pradesh right now: " + "; ".join(t for t, _, _ in trends[:15]) +
                      ".\nIf one of these is a kid-friendly subject (a festival, season, weather, sport, food, animal, "
                      "place or invention; never politics, religion, crime, accidents or real people), build the story "
                      "around it so it feels timely; otherwise ignore the list.")
    nb = max(2, min(8, int(os.getenv("SHORT_BEATS", "4"))))
    secs = float(os.getenv("SHORT_BEAT_SECONDS", "5"))
    prompt = f"""You are the creative director of a Telugu Instagram Reels page that posts short, heart-touching or funny animated films.
Invent ONE original {int(nb * secs)}-second Pixar-style 3D animated micro-story with a clear, positive, shareable message.
Cast: one or two cute, expressive personified characters (animals, birds, everyday objects, fruits, vegetables, toys...).
Give each character a simple, iconic cartoon design (one main colour plus one accessory such as a scarf, cap or bow) so it
looks the same in every picture; if two are animals, make them clearly different species and colours.
Inspiration (you may reinterpret): setting near "{setting}", theme around "{theme}". Warm cinematic look.{trend_hint}
Rules: kid-friendly; no violence; no politics or religion; no real people; no brands; no famous or copyrighted characters.
Do NOT reuse or imitate these titles: {json.dumps(used_titles[-40:], ensure_ascii=False)}
and avoid these morals: {json.dumps(used_morals[-40:], ensure_ascii=False)}

HOW IT IS MADE: one opening image, then {nb} beats of {secs:g} seconds. Each beat is animated from the LAST frame of the
previous beat, so it is ONE continuous scene: same place, same characters, roughly the same camera. The opening image
shows the characters big in the frame, side by side (never overlapping or tangled), well lit. Each beat describes
motion only: what the characters do, their facial expressions, and small effects (sparkles, wind, splash, glow);
simple clear actions (walk, turn, jump, wave, laugh, cry, hug), no transformations, no new characters appearing.
Beat 1 must already be surprising or emotional (no slow intro). The last beat is the warm or funny payoff.
A Telugu storyteller narrates the reel: one "narration" sentence per beat, spoken while that beat plays, describing
what happens so the viewer clearly follows a complete little story (beginning, problem, happy ending). Natural spoken
Telugu like a grandmother telling a story, each sentence 6 to 10 words (about 4 seconds when spoken, so it fits its
{secs:g}-second shot). Characters can have simple Telugu names.
The Telugu "caption" goes in the Instagram post text (not on the video): something people want to send to a friend.

Return ONLY JSON:
{{"title": "short Telugu title",
"narration": [exactly {nb} Telugu sentences, one per beat, in order],
"caption": "Telugu post message (max 18 words, 1 or 2 emotional sentences, simple spoken Telugu)",
"moral": "one Telugu sentence", "question": "one short Telugu question that invites a real comment",
"characters": ["English look of each character: species/object, colours, size, big expressive eyes (one sentence each)"],
"opening_image": "English image prompt: close medium shot where ALL characters are large, side by side, fully visible and fill most of the frame, their pose and expression, a bright well-lit setting",
"beats": [exactly {nb} English motion prompts, one per {secs:g}-second beat, in order],
"tag": "one real English hashtag about the theme, e.g. #friendship", "hashtags": ["#kidsstories", "#moralstory"]}}
LANGUAGE RULE: title, narration, caption, moral and question are Telugu; characters, opening_image and beats are ENGLISH only."""
    tries = 3 if (gemini_key() or anthropic_key()) else 5
    for attempt in range(tries):
        try:
            raw = json.loads(re.sub(r"^```(?:json)?|```$", "", llm_json_text(prompt).strip(), flags=re.M).strip())
            assert all(TELUGU_RE.search(str(raw.get(k, ""))) for k in ("title", "caption", "moral")), "Telugu fields missing"
            chars = [str(c).strip() for c in raw.get("characters", []) if len(str(c).strip()) > 10][:2]
            beats = [str(b).strip() for b in raw.get("beats", []) if len(str(b).strip()) > 15]
            assert chars and len(beats) >= 2, "characters/beats missing"
            narration = [str(x).strip() for x in raw.get("narration", []) if TELUGU_RE.search(str(x))]
            m = min(len(beats), len(narration), nb)   # beats and narration lines must pair up
            assert m >= 2 and all(3 <= len(x.split()) <= 22 for x in narration[:m]), "narration missing or too long"
            assert not any(LATIN_RE.search(x) for x in narration[:m] + [str(raw["moral"])]), "narration has English letters mixed in"
            raw["narration"] = narration[:m]
            beats = beats[:m]
            opening = str(raw.get("opening_image") or (raw.get("scenes") or [""])[0]).strip()
            assert len(opening) > 30, "opening image prompt too short"
            assert not TELUGU_RE.search(" ".join(chars + beats) + opening), "prompts must be English"
            raw["opening_image"] = opening
            if any(_sim(raw["title"], t) > 0.5 for t in used_titles) or any(_sim(raw["moral"], m_) > 0.5 for m_ in used_morals if m_):
                raise ValueError("too similar to an earlier video")
            raw["characters"], raw["beats"] = chars, beats
            if str(raw.get("tag", "")).lower() in {"#englishtag", "#tag", "#hashtag"}:
                raw["tag"] = ""
            return raw
        except Exception as e:
            print(f"Short concept attempt {attempt + 1} failed: {e}")
            if attempt < tries - 1:
                time.sleep(8 * (attempt + 1))
    return None


def assemble_short(idea, clips, d, out_path, seed):
    """Narration (one line per animated beat + the moral) over the beats, music ducked under the voice. No text on
    screen. Returns the number of narration lines used."""
    k = len(clips)
    lens = [media_seconds(c) for c in clips]
    narr = idea["narration"]
    lines = narr[:k] if k >= len(narr) else (narr[:k - 1] + [narr[-1]] if k > 1 else [narr[0]])  # missing scenes: jump to the ending
    segs = [{"kind": "line", "spk": NARRATOR, "text": t} for t in lines]
    segs.append({"kind": "moral", "spk": NARRATOR, "text": f"నీతి: {idea['moral']}"})
    voice, starts, durs, _envs, total = make_voice(segs)
    bounds = [0.0] + [max(0.5, starts[i] - 0.25) for i in range(1, k)] + [total]   # beat i spans its narration line
    mi = f"minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:vsbmc=1,"
    cmd, fc = ["ffmpeg", "-y", "-loglevel", "error"], ""
    for i, c in enumerate(clips):
        cmd += ["-i", str(c)]
        want = bounds[i + 1] - bounds[i]
        f = want / max(0.1, lens[i])
        if f <= 1.0:   # line is shorter than the beat: cut the beat
            speed, pad = f"trim=duration={want:.3f},setpts=PTS-STARTPTS,", ""
        else:          # line is longer: slow the beat a little (max 1.5x, more looks smeary), hold the last frame for any rest
            s = min(f, 1.5)
            speed = f"setpts={s:.3f}*PTS,{mi}"
            extra = want - lens[i] * s
            pad = f",tpad=stop_mode=clone:stop_duration={extra:.3f}" if extra > 0.05 else ""
        fc += (f"[{i}:v]{speed}scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
               f"unsharp=5:5:0.45:5:5:0.0,fps={FPS},setsar=1,format=yuv420p{pad}[v{i}];")
    fc += "".join(f"[v{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=0[v]"
    joined = d / "joined.mp4"
    run(cmd + ["-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "17",
               "-pix_fmt", "yuv420p", str(joined)])
    rng = random.Random(seed)
    n = len(voice)
    music = get_music(n / SR, rng) * 1.6
    sfx = make_sfx([(0.05, "pop")], n, rng)
    wav = d / "audio.wav"
    mix_audio(voice, music, sfx, wav)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-i", str(wav), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest", "-movflags", "+faststart", str(out_path)])
    print(f"  reel '{idea['title']}': {k} animated scenes, {len(lines)} narration lines + moral, "
          f"{media_seconds(out_path):.1f}s -> {out_path}")
    return len(lines)


def queue_reel(idea, video, n_scenes):
    """Keep a finished reel for the next 'queued' slot (the workflow stores it on the 'queue' branch)."""
    q = OUT / "queue"
    q.mkdir(parents=True, exist_ok=True)
    if video != q / "reel.mp4":
        shutil.move(str(video), q / "reel.mp4")
    (q / "meta.json").write_text(json.dumps({
        "caption": build_caption(idea), "title": idea["title"], "moral": idea["moral"],
        "story_id": "short-" + hashlib.md5(idea["title"].encode()).hexdigest()[:8],
        "made": dt.datetime.now(dt.timezone.utc).isoformat(), "scenes": n_scenes}, ensure_ascii=False), encoding="utf-8")
    if os.getenv("DRY_RUN") == "1":
        print("  second reel kept in out/queue/ for the preview only (dry run: not saved for a later slot)")
    else:
        set_output("queue_save", "true")
        print("  second reel saved: it will be posted at the next 'queued' slot")


def generate_pixar_short(state, ig_titles):
    """Animated story reels in the Nov 20 style (one continuous scene, each beat continues from the last frame).
    With Kaggle: REELS_PER_RUN reels (default 2, one per T4 GPU) in ONE session; the first becomes out/reel.mp4 and is
    posted now, the second goes to out/queue/ for the next slot. Returns True if out/reel.mp4 is ready. Never raises."""
    try:
        used_titles = [u.get("title", "") for u in state["used"]] + [t.replace("📖", "").strip() for t in ig_titles]
        used_morals = [u.get("moral", "") for u in state["used"]]
        trends = fetch_trends() if os.getenv("STORY_TRENDS", "1") == "1" else []
        use_kaggle = kaggle_ready() and os.getenv("VIDEO_BACKEND", "auto") in ("auto", "kaggle")
        want = max(1, min(2, int(os.getenv("REELS_PER_RUN", "2")))) if use_kaggle else 1
        d = OUT / "short"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.rglob("*"):
            if old.is_file():
                old.unlink()
        ideas = []
        for r in range(want):
            idea = short_concept([t for t in used_titles if t], [m_ for m_ in used_morals if m_],
                                 trends if r == 0 else [])   # the second reel stays evergreen, so the two differ
            if not idea:
                continue
            used_titles.append(idea["title"])
            used_morals.append(idea["moral"])
            idea["seed"] = random.randint(1, 10 ** 6)
            idea["looks"] = " AND ".join(idea["characters"])
            idea["motions"] = [f"{b} The characters look exactly the same: {idea['looks']}. Cute stylized Pixar-style 3D "
                               f"cartoon animation, smooth natural motion, consistent faces." for b in idea["beats"]]
            idea["still_prompt"] = short_still_prompt(idea["opening_image"], idea["looks"])
            key = d / f"key{r}.png"
            try:
                prov = free_image(idea["still_prompt"], idea["seed"], key)
                cover_fit(key)
                print(f"  reel {r + 1} opening picture via {prov} (the GPU redraws it in the cute style)")
            except Exception as e:
                if not use_kaggle or os.getenv("KAGGLE_IMAGE_MODEL", "").lower() == "none":
                    print(f"  reel {r + 1} opening picture failed: {str(e)[:200]}")
                    continue
                Image.new("RGB", (W, H), (120, 170, 220)).save(key)   # placeholder: the GPU draws the real one
                print(f"  reel {r + 1}: free picture service busy; the GPU will draw the opening picture")
            idea["key"] = key
            ideas.append(idea)
            print(f"Short idea {r + 1}: {idea['title']} | {idea['looks'][:250]}")
            print("  Story: " + " / ".join(idea["narration"]) + f" / నీతి: {idea['moral']}")
        if not ideas:
            return False
        clips_for = {}
        if use_kaggle:   # Kaggle only: the Hugging Face Spaces are not used once Kaggle is set up
            try:
                chains = [{"key": i["key"], "image_prompt": i["still_prompt"], "beats": i["motions"], "seed": i["seed"]}
                          for i in ideas]
                clips_for = kaggle_animate(chains, d)
            except Exception as e:
                print(f"  Kaggle video failed: {str(e)[:500]}")
        else:
            print("  Kaggle is not set up (secrets KAGGLE_USERNAME + KAGGLE_API_TOKEN); using the free Hugging Face Spaces")
            idea, src, clips = ideas[0], ideas[0]["key"], []
            secs = float(os.getenv("SHORT_BEAT_SECONDS", "5"))
            for i, motion in enumerate(idea["motions"]):
                clip = d / f"beat{i + 1}.mp4"
                try:
                    space = animate_still(src, motion, clip, secs, idea["seed"] + i)
                    print(f"  beat {i + 1}/{len(idea['motions'])} animated via {space}")
                except QuotaError as e:
                    print(f"  free GPU quota used up after {len(clips)} beat(s): {e}")
                    break
                except Exception as e:
                    print(f"  beat {i + 1} animation failed: {str(e)[:300]}")
                    break   # the next beat must start from this one's last frame, so stop the chain here
                clips.append(clip)
                nxt = d / f"last{i + 1}.png"
                last_frame(clip, nxt)
                src = nxt
            if clips:
                clips_for = {0: clips}
        # a reel needs at least SHORT_MIN_SCENES animated scenes; the most complete one is posted now
        min_scenes = int(os.getenv("SHORT_MIN_SCENES", "2"))
        done = sorted(((k, v) for k, v in clips_for.items() if len(v) >= min_scenes), key=lambda kv: -len(kv[1]))
        if not done:
            print("No reel got enough animated scenes (see the video lines above).")
            return False
        posted = None
        for n_, (k, clips) in enumerate(done):
            idea = ideas[k]
            sub = d / f"reel{k}"
            sub.mkdir(exist_ok=True)
            out_path = OUT / "reel.mp4" if n_ == 0 else OUT / "queue" / "reel.mp4"
            assemble_short(idea, clips, sub, out_path, idea["seed"])
            if n_ == 0:
                posted = (idea, len(clips))
            else:
                queue_reel(idea, out_path, len(clips))
    except Exception as e:
        print(f"Animated short failed: {str(e)[:500]}")
        return False
    idea, n_scenes = posted
    return post_idea(idea, "short-", f"free animated short ({n_scenes}/{len(idea['beats'])} scenes, continuous)")


def post_queued(state, ig_titles):
    """Posts the reel the previous Kaggle run kept (fetched into queued/ by the workflow). Returns True if ready."""
    q = pathlib.Path("queued")
    meta_p, video = q / "meta.json", q / "reel.mp4"
    if not (meta_p.exists() and video.exists() and video.stat().st_size > 100000):
        print("No queued reel waiting (the last Kaggle run made only one, or it was already posted).")
        return False
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(meta["made"])).total_seconds() / 3600
    if age_h > float(os.getenv("QUEUE_MAX_HOURS", "20")):
        print(f"Queued reel is {age_h:.0f} h old; dropping it.")
        if os.getenv("DRY_RUN") != "1":
            set_output("queue_used", "true")
        return False
    if any(meta["title"] in t for t in ig_titles):
        print("Queued reel's story is already on Instagram; dropping it.")
        if os.getenv("DRY_RUN") != "1":
            set_output("queue_used", "true")
        return False
    OUT.mkdir(exist_ok=True)
    shutil.copy(video, OUT / "reel.mp4")
    name = f"reel-{dt.datetime.now(IST).strftime('%Y%m%d-%H%M')}.mp4"
    meta["video_name"] = name
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"Built out/reel.mp4 with the queued animated reel '{meta['title']}' (made {age_h:.1f} h ago, "
          f"{meta.get('scenes', '?')} scenes)")
    telegram_send(OUT / "reel.mp4", meta["title"], meta["caption"])
    set_output("has_post", "true")
    set_output("video_name", name)
    if os.getenv("DRY_RUN") != "1":
        set_output("queue_used", "true")
    return True


# ------------------------------------------- trending "మీకు తెలుసా?" fact reels (no GPU: AI pictures + camera moves)
# What Telugu people search right now (free Google Trends feeds for Telangana / Andhra Pradesh) becomes the hook; the
# reel then tells timeless, well-known facts about that subject, so it rides the trend without repeating news claims.
FACT_STYLE = ("Bright, clear, highly detailed vertical image, the main subject large, centred and instantly recognisable, "
              "vivid natural colours, bright daylight or clean studio lighting, sharp focus, high quality photo or 3D "
              "render, eye-catching, not dark, no text, no letters, no watermark, no logos")


def fetch_trends():
    """[(topic, approx_traffic, [headlines])] from Google Trends, Telugu states first. Never raises."""
    import xml.etree.ElementTree as ET
    out, seen = [], set()
    for geo in [g.strip() for g in os.getenv("TREND_GEOS", "IN-TG,IN-AP,IN").split(",") if g.strip()]:
        try:
            r = requests.get(f"https://trends.google.com/trending/rss?geo={geo}", timeout=30,
                             headers={"User-Agent": "telugu-story-reel-bot"})
            for it in ET.fromstring(r.content).iter("item"):
                kids = {c.tag.split("}")[-1]: c for c in it}
                title = (it.findtext("title") or "").strip()
                if not title or title.lower() in seen:
                    continue
                seen.add(title.lower())
                news = [(n.find("{*}news_item_title").text or "").strip() for n in it.findall("{*}news_item")
                        if n.find("{*}news_item_title") is not None][:2]
                out.append((title, (kids.get("approx_traffic").text if "approx_traffic" in kids else "") or "", news))
        except Exception as e:
            print(f"  trends {geo} unavailable: {str(e)[:120]}")
    return out[:30]


def trend_concept(trends, used_titles):
    listing = "\n".join(f"- {t} ({tr} searches): {' / '.join(n)[:220]}" for t, tr, n in trends) or "- (no trends available)"
    prompt = f"""You run a viral Telugu Instagram Reels page. Make ONE 30-40 second "మీకు తెలుసా?" (did you know?) reel.
What people in Telangana and Andhra Pradesh are searching on Google right now:
{listing}

Pick the trend that gives the most surprising, share-worthy reel for Telugu viewers. SKIP anything about politics,
politicians, crime, deaths, accidents, disasters, religion or temples, caste, celebrity gossip or personal life,
TV-serial or movie spoilers, betting, lottery, adult topics or medical advice. Good: science, space, nature,
animals, food, festivals' history, sports in general, technology, money basics, history, famous places in AP/TG.
Use the trend only as the HOOK, then tell timeless, well-known, verifiable facts about its SUBJECT (e.g. trend
"gold rate" -> amazing facts about gold). NEVER state today's news, prices, scores, dates or any number you are not
sure of. If no trend is suitable, choose an evergreen surprising topic Telugu people love.
Do NOT repeat these earlier reels: {json.dumps(used_titles[-40:], ensure_ascii=False)}

Narration: natural spoken Telugu, excited storyteller voice. The hook must stop the scroll in 2 seconds (a
question or shocking statement). Then 5 facts, each one sentence of 9 to 15 words, most surprising fact last.
Write numbers as Telugu words. No English words in the narration.
Images: one English prompt per line, a striking cinematic picture of that fact; never a real person's face or
likeness, no text, no logos.

Return ONLY JSON:
{{"trend": "the trend you used (or evergreen)", "title": "short Telugu title",
"hook": "Telugu hook sentence", "hook_image": "English image prompt",
"facts": [{{"line": "Telugu fact sentence", "image": "English image prompt"}}, ... exactly 5],
"caption": "Telugu post text (max 18 words) that makes people share", "question": "short Telugu question for comments",
"tag": "one popular English hashtag about the subject, e.g. #space"}}"""
    for attempt in range(3):
        try:
            raw = json.loads(re.sub(r"^```(?:json)?|```$", "", llm_json_text(prompt).strip(), flags=re.M).strip())
            facts = [f for f in raw.get("facts", []) if isinstance(f, dict) and TELUGU_RE.search(str(f.get("line", "")))
                     and len(str(f.get("image", ""))) > 15][:6]
            assert len(facts) >= 4, "facts missing"
            lines = [str(raw.get("hook", ""))] + [str(f["line"]) for f in facts]
            assert all(TELUGU_RE.search(x) for x in lines + [str(raw.get("title", "")), str(raw.get("caption", ""))]), "Telugu missing"
            assert not any(LATIN_RE.search(x) for x in lines), "narration has English letters"
            assert all(3 <= len(x.split()) <= 24 for x in lines), "narration lines too long or short"
            assert not TELUGU_RE.search(" ".join(str(f["image"]) for f in facts) + str(raw.get("hook_image", ""))), "image prompts must be English"
            if any(_sim(raw["title"], t) > 0.5 for t in used_titles):
                raise ValueError("too similar to an earlier reel")
            raw["facts"] = facts
            return raw
        except Exception as e:
            print(f"Trend reel attempt {attempt + 1} failed: {e}")
            time.sleep(8 * (attempt + 1))
    return None


def generate_trend_reel(state, ig_titles):
    """Returns True if out/reel.mp4 is ready to post. Never raises."""
    try:
        trends = fetch_trends()
        print(f"Trending now ({len(trends)}): " + " | ".join(t for t, _, _ in trends[:12]))
        used = [u.get("title", "") for u in state["used"]] + [t.replace("📖", "").replace("🤯", "").strip() for t in ig_titles]
        idea = trend_concept(trends, [t for t in used if t])
        if not idea:
            return False
        print(f"Trend reel: {idea['title']} (trend: {idea.get('trend', '?')})")
        d = OUT / "trend"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*"):
            old.unlink()
        shots = [(idea["hook"], idea.get("hook_image") or idea["facts"][0]["image"])] + \
                [(f["line"], f["image"]) for f in idea["facts"]]
        seed = random.randint(1, 10 ** 6)
        imgs, prev = [], None
        for i, (_, prompt) in enumerate(shots):
            p = d / f"img{i}.png"
            try:
                free_image(f"{prompt}. {FACT_STYLE}.", seed + i, p)
                cover_fit(p)
                prev = p
            except Exception as e:
                print(f"  picture {i + 1} failed ({str(e)[:120]})" + ("; reusing the previous one" if prev else ""))
                if not prev:
                    return False
            imgs.append(prev)
            print(f"  picture {i + 1}/{len(shots)} ready")
        segs = [{"kind": "line", "spk": NARRATOR, "text": t} for t, _ in shots]
        segs.append({"kind": "outro", "spk": NARRATOR, "text": "ఇలాంటి ఆసక్తికరమైన విషయాల కోసం ఫాలో అవ్వండి!"})
        voice, starts, durs, _envs, total = make_voice(segs)
        bounds = [0.0] + [max(0.5, starts[i] - 0.15) for i in range(1, len(shots))] + [total]   # outro stays on the last picture
        moves = ["in", "pan_r", "out", "pan_l", "up", "in"]
        clips = []
        for i, img in enumerate(imgs):
            dur = max(1.0, bounds[i + 1] - bounds[i])
            # quick cuts hold attention: a long line gets two different camera moves on its picture
            parts = [dur] if dur < 4.5 else [dur / 2, dur - dur / 2]
            for k, p_dur in enumerate(parts):
                c = d / f"shot{i}_{k}.mp4"
                still_to_clip(img, c, p_dur, moves[(i + k * 3) % len(moves)])
                clips.append(c)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for c in clips:
            cmd += ["-i", str(c)]
        fc = "".join(f"[{i}:v]setsar=1[v{i}];" for i in range(len(clips))) + \
             "".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]"
        joined = d / "joined.mp4"
        run(cmd + ["-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                   "-pix_fmt", "yuv420p", str(joined)])
        rng = random.Random(seed)
        n = len(voice)
        music = get_music(n / SR, rng) * 1.4
        sfx = make_sfx([(0.05, "pop")] + [(b - 0.3, "whoosh") for b in bounds[1:-1]], n, rng)
        wav = d / "audio.wav"
        mix_audio(voice, music, sfx, wav)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-i", str(wav), "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest", "-movflags", "+faststart",
             str(OUT / "reel.mp4")])
        print(f"Trend reel {media_seconds(OUT / 'reel.mp4'):.1f}s, {len(shots)} pictures")
    except Exception as e:
        print(f"Trend reel failed: {str(e)[:500]}")
        return False
    idea["kind"] = "facts"
    idea["moral"] = ""
    return post_idea(idea, "facts-", "trending fact reel")


# ------------------------------------------------ image posts: swipeable "మీకు తెలుసా?" fact cards (carousel)
PW, PH = 1080, 1350   # Instagram portrait post (4:5)


def _wrap(text, font, width):
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if cur and font.getlength(t) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    return lines + ([cur] if cur else [])


def render_card(img_path, text, out, badge=None, cover=False):
    """A sharp picture with big Telugu text on a dark gradient at the bottom (plus a small '2/6' badge)."""
    im = Image.open(img_path).convert("RGB")
    s = max(PW / im.width, PH / im.height)
    im = im.resize((math.ceil(im.width * s), math.ceil(im.height * s)), Image.LANCZOS)
    l, t = (im.width - PW) // 2, max(0, (im.height - PH) // 3)
    im = im.crop((l, t, l + PW, t + PH)).convert("RGBA")
    font = te_font(78 if cover else 58)
    lines = _wrap(text, font, PW - 120)
    lh = int(font.size * 1.45)
    extra = 80 if cover else 0
    block = lh * len(lines) + extra
    top = PH - 90 - block
    grad = Image.new("L", (1, PH))
    g0 = max(0, top - 260)
    for y in range(PH):
        grad.putpixel((0, y), 0 if y < g0 else min(235, int(235 * (y - g0) / max(1, PH - g0) * 1.6)))
    shade = Image.new("RGBA", (PW, PH), (0, 0, 0, 255))
    shade.putalpha(grad.resize((PW, PH)))
    im = Image.alpha_composite(im, shade)
    dr = ImageDraw.Draw(im)
    y = top
    for ln in lines:
        w = font.getlength(ln)
        dr.text(((PW - w) / 2, y), ln, font=font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
        y += lh
    if cover:   # swipe hint
        sf = te_font(44)
        hint = "స్వైప్ చేయండి"
        hw = sf.getlength(hint) + 70
        dr.text(((PW - hw) / 2, y + 16), hint, font=sf, fill=(255, 214, 10), stroke_width=2, stroke_fill=(0, 0, 0))
        dr.text(((PW - hw) / 2 + sf.getlength(hint) + 18, y + 18), ">>", font=latin_font(42), fill=(255, 214, 10),
                stroke_width=2, stroke_fill=(0, 0, 0))
    if badge:
        bf = latin_font(40)
        bw = bf.getlength(badge) + 44
        dr.rounded_rectangle((40, 44, 40 + bw, 108), radius=32, fill=(255, 214, 10))
        dr.text((62, 52), badge, font=bf, fill=(20, 20, 20))
    im.convert("RGB").save(out, "JPEG", quality=93)


def generate_fact_post(state, ig_titles):
    """Instagram carousel post: a cover (the trending hook) + one card per fact. Returns True if out/post/ is ready."""
    try:
        if not features.check("raqm"):   # without it Telugu letters render broken (vowel signs and conjuncts split)
            print("Fact post skipped: Telugu text shaping (raqm) is not available on this machine.")
            return False
        trends = fetch_trends()
        print(f"Trending now ({len(trends)}): " + " | ".join(t for t, _, _ in trends[:12]))
        used = [u.get("title", "") for u in state["used"]] + [t.replace("📖", "").replace("🤯", "").strip() for t in ig_titles]
        idea = trend_concept(trends, [t for t in used if t])
        if not idea:
            return False
        print(f"Fact post: {idea['title']} (trend: {idea.get('trend', '?')})")
        idea["kind"] = "facts"
        return build_carousel(idea, FACT_STYLE, "fact post")
    except Exception as e:
        print(f"Fact post failed: {str(e)[:500]}")
        return False


def build_carousel(idea, style, label):
    """Cover (idea['hook']) + one card per idea['facts'] item into out/post/, plus meta.json. Returns True when ready."""
    try:
        d = OUT / "post"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*"):
            old.unlink()
        cards = [(idea["hook"], idea.get("hook_image") or idea["facts"][0]["image"])] + \
                [(f["line"], f["image"]) for f in idea["facts"]]
        seed = random.randint(1, 10 ** 6)
        stamp = dt.datetime.now(IST).strftime("%Y%m%d-%H%M")
        names, prev = [], None
        for i, (text, prompt) in enumerate(cards):
            raw = d / f"raw{i}.png"
            try:
                free_image(f"{prompt}. {style}.", seed + i, raw)
                prev = raw
            except Exception as e:
                print(f"  picture {i + 1} failed ({str(e)[:120]})" + ("; reusing the previous one" if prev else ""))
                if not prev:
                    return False
            name = f"post-{stamp}-{i + 1}.jpg"
            render_card(prev, text, d / name, badge=None if i == 0 else f"{i}/{len(cards) - 1}", cover=(i == 0))
            names.append(name)
            print(f"  card {i + 1}/{len(cards)} ready")
        for r in d.glob("raw*.png"):
            r.unlink()
        caption = build_caption(idea, post=True)
        (OUT / "meta.json").write_text(json.dumps({
            "type": "carousel", "images": names, "caption": caption, "video_name": names[0],
            "story_id": "post-" + hashlib.md5(idea["title"].encode()).hexdigest()[:8],
            "title": idea["title"], "moral": ""}, ensure_ascii=False), encoding="utf-8")
        print(f"Built out/post/ with a {len(names)}-card {label}")
        set_output("has_post", "true")
        set_output("post_type", "carousel")
        set_output("video_name", names[0])
        return True
    except Exception as e:
        print(f"{label} failed: {str(e)[:500]}")
        return False


# ---------------------------------- devotional image posts: the current Telugu month, its festivals, poojas and foods
# Telugu (amavasyant) month start dates and big festivals. Dates are approximate (+-1 day): posts never print them,
# they only decide what is "now" and "coming up". Extend the lists for later years.
TELUGU_MONTHS = [
    ("2026-10-10", "ఆశ్వయుజ", "Ashwayuja"), ("2026-11-09", "కార్తీక", "Karthika"), ("2026-12-09", "మార్గశిర", "Margasira"),
    ("2027-01-08", "పుష్య", "Pushya"), ("2027-02-06", "మాఘ", "Magha"), ("2027-03-08", "ఫాల్గుణ", "Phalguna"),
    ("2027-04-06", "చైత్ర", "Chaitra"), ("2027-05-06", "వైశాఖ", "Vaisakha"), ("2027-06-04", "జ్యేష్ఠ", "Jyeshta"),
    ("2027-07-04", "ఆషాఢ", "Ashadha"), ("2027-08-02", "శ్రావణ", "Sravana"), ("2027-08-31", "భాద్రపద", "Bhadrapada"),
    ("2027-09-30", "ఆశ్వయుజ", "Ashwayuja"), ("2027-10-29", "కార్తీక", "Karthika"), ("2027-11-28", "మార్గశిర", "Margasira"),
    ("2027-12-27", "పుష్య", "Pushya")]
FESTIVALS = [
    ("2026-11-24", "కార్తీక పౌర్ణమి", "Karthika Pournami"), ("2026-12-15", "సుబ్రహ్మణ్య షష్ఠి", "Subrahmanya Shashti"),
    ("2026-12-16", "ధనుర్మాసం ప్రారంభం", "Dhanurmasam begins"), ("2026-12-20", "వైకుంఠ ఏకాదశి", "Vaikuntha Ekadasi"),
    ("2026-12-24", "దత్త జయంతి", "Datta Jayanti"), ("2027-01-14", "భోగి", "Bhogi"), ("2027-01-15", "మకర సంక్రాంతి", "Sankranti"),
    ("2027-01-16", "కనుమ", "Kanuma"), ("2027-02-11", "వసంత పంచమి", "Vasantha Panchami"), ("2027-02-13", "రథ సప్తమి", "Ratha Saptami"),
    ("2027-03-06", "మహా శివరాత్రి", "Maha Shivaratri"), ("2027-03-22", "హోళీ", "Holi"), ("2027-04-07", "ఉగాది", "Ugadi"),
    ("2027-04-15", "శ్రీరామ నవమి", "Sri Rama Navami"), ("2027-05-09", "అక్షయ తృతీయ", "Akshaya Tritiya"),
    ("2027-07-15", "తొలి ఏకాదశి", "Tholi Ekadasi"), ("2027-07-19", "గురు పౌర్ణమి", "Guru Pournami"),
    ("2027-08-13", "వరలక్ష్మీ వ్రతం", "Varalakshmi Vratam"), ("2027-08-17", "రాఖీ పౌర్ణమి", "Raksha Bandhan"),
    ("2027-08-25", "శ్రీ కృష్ణాష్టమి", "Krishnashtami"), ("2027-09-04", "వినాయక చవితి", "Vinayaka Chavithi"),
    ("2027-09-30", "బతుకమ్మ ప్రారంభం", "Bathukamma begins"), ("2027-10-01", "దేవీ నవరాత్రులు", "Devi Navaratri"),
    ("2027-10-10", "విజయదశమి (దసరా)", "Dussehra"), ("2027-10-29", "దీపావళి", "Deepavali"),
    ("2027-11-02", "నాగుల చవితి", "Nagula Chavithi"), ("2027-11-13", "కార్తీక పౌర్ణమి", "Karthika Pournami")]
# special weekdays of some months (Monday=0)
MONTH_WEEKDAYS = {"Karthika": {0: "కార్తీక సోమవారం"}, "Margasira": {3: "మార్గశిర లక్ష్మీవారం (గురువారం)"},
                  "Sravana": {4: "శ్రావణ శుక్రవారం", 0: "శ్రావణ సోమవారం"}, "Ashadha": {6: "ఆషాఢ బోనాలు ఆదివారం"}}
DEVOTION_STYLE = ("Beautiful traditional South Indian devotional photograph, warm golden light of brass oil lamps (diyas), "
                  "marigold and jasmine flowers, rangoli, banana leaves, brass and copper pooja items, temple architecture, "
                  "rich festive colours, sharp focus, high detail, peaceful and auspicious mood, the main subject large and "
                  "clear. No deity faces in close-up, no people's faces, no text, no letters, no watermark, no logos")


def telugu_today(now=None):
    """What is special today in the Telugu calendar: month, paksha, weekday specials, festivals today / coming up."""
    now = now or dt.datetime.now(IST)
    today = now.date()
    month = None
    for start, te, en in TELUGU_MONTHS:
        s = dt.date.fromisoformat(start)
        if s <= today:
            month = (te, en, (today - s).days)
    if month and month[2] > 31:
        month = None   # past the end of the table
    info = {"date": today.isoformat(), "weekday": now.strftime("%A"), "month_te": month[0] if month else "",
            "month_en": month[1] if month else "", "paksha": "", "special_day": "", "festivals": []}
    if month:
        day = month[2]
        info["paksha"] = ("శుక్ల పక్షం (పౌర్ణమి వైపు)" if day <= 15 else "బహుళ పక్షం (అమావాస్య వైపు)")
        info["special_day"] = MONTH_WEEKDAYS.get(month[1], {}).get(today.weekday(), "")
    if dt.date(today.year, 12, 16) <= today or today <= dt.date(today.year, 1, 14):
        info["season"] = "ధనుర్మాసం (Dhanurmasam: Tiruppavai, Goda Devi, early-morning muggulu)"
    for d_, te, en in FESTIVALS:
        gap = (dt.date.fromisoformat(d_) - today).days
        if -1 <= gap <= 6:
            info["festivals"].append({"te": te, "en": en, "in_days": gap})
    return info


def devotional_concept(info, angle, used_titles):
    fest = "; ".join(f"{f['en']} / {f['te']} " + ("(today)" if f["in_days"] == 0 else "(yesterday)" if f["in_days"] < 0
                     else f"(in {f['in_days']} days)") for f in info["festivals"]) or "none this week"
    prompt = f"""You run a popular Telugu devotional Instagram page (Hindu festivals, poojas, traditions, prasadam).
Today: {info['date']} ({info['weekday']}). Telugu month: {info['month_en']} masam / {info['month_te']} మాసం
{info['paksha']}. Special day today: {info['special_day'] or 'none'}. Season: {info.get('season', 'none')}.
Festivals now / coming up: {fest}.
(If the month is empty, work out the current Telugu month yourself from the date.)

Make ONE swipeable image post (carousel) on this angle: {angle}
It must feel timely ("now", "this month", "this week") so Telugu families save and share it.
Content rules: traditional, widely-followed beliefs and customs as practised in Andhra Pradesh and Telangana, said
respectfully ("అంటారు", "పెద్దలు చెబుతారు" for beliefs). Simple, practical, positive. NO exact dates, times, muhurtham or
tithi timings, no astrology predictions, no fear or curses, no medical claims, nothing against any community or sect.
Do NOT repeat these earlier posts: {json.dumps(used_titles[-40:], ensure_ascii=False)}

Text (printed big on the cards): natural Telugu, no English letters. Cover hook: one catchy line of 5 to 12 words
(question or "తప్పక తెలుసుకోండి" style). Then exactly 5 cards, each ONE sentence of 8 to 18 words (for food or item
lists start with the item name then why/how, e.g. "ఉసిరికాయ: ...").
Images: one English prompt per card showing that exact item/ritual: lamps, pooja thali, rangoli, temples, tulasi
plant, river at dawn, festive foods on banana leaf, flowers. Do not show deities' faces up close or any person's face.

Return ONLY JSON:
{{"title": "short Telugu title", "hook": "Telugu cover line", "hook_image": "English image prompt",
"facts": [{{"line": "Telugu card sentence", "image": "English image prompt"}}, ... exactly 5],
"caption": "warm Telugu post text (max 22 words) that makes people save and share",
"question": "short Telugu question for comments", "search": "Telugu and English search words, e.g. కార్తీక మాసం | Karthika Masam",
"tag": "one popular English hashtag for this festival or month, e.g. #karthikamasam"}}"""
    for attempt in range(3):
        try:
            raw = json.loads(re.sub(r"^```(?:json)?|```$", "", llm_json_text(prompt).strip(), flags=re.M).strip())
            facts = [f for f in raw.get("facts", []) if isinstance(f, dict) and TELUGU_RE.search(str(f.get("line", "")))
                     and len(str(f.get("image", ""))) > 15][:5]
            assert len(facts) >= 4, "cards missing"
            lines = [str(raw.get("hook", ""))] + [str(f["line"]) for f in facts]
            assert all(TELUGU_RE.search(x) for x in lines + [str(raw.get("title", "")), str(raw.get("caption", ""))]), "Telugu missing"
            assert not any(LATIN_RE.search(x) for x in lines), "card text has English letters"
            assert all(3 <= len(x.split()) <= 24 for x in lines), "card text too long or short"
            assert not TELUGU_RE.search(" ".join(str(f["image"]) for f in facts) + str(raw.get("hook_image", ""))), "image prompts must be English"
            if any(_sim(raw["title"], t) > 0.6 for t in used_titles):
                raise ValueError("too similar to an earlier post")
            raw["facts"] = facts
            return raw
        except Exception as e:
            print(f"Devotional post attempt {attempt + 1} failed: {e}")
            time.sleep(8 * (attempt + 1))
    return None


def devotional_angles(info):
    m = f"{info['month_en']} masam ({info['month_te']} మాసం)" if info["month_en"] else "the current Telugu month"
    near = [f for f in info["festivals"] if 0 <= f["in_days"] <= 3]
    angles = []
    if near:
        f = f"{near[0]['en']} ({near[0]['te']})"
        angles += [f"{f}: why it is celebrated and its significance", f"{f}: simple pooja at home, step by step",
                   f"{f}: traditional naivedyam / prasadam and special foods made that day",
                   f"{f}: what to do and what not to do that day"]
    if info["special_day"]:
        angles.append(f"today is {info['special_day']}: what devotees do today and why it is special")
    angles += [f"{m}: why this month is special and what people do every day of it",
               f"{m}: foods traditionally eaten in this month and why (satvik, seasonal)",
               f"{m}: foods and habits people avoid in this month and the reason behind it",
               f"{m}: deepam / lamp lighting customs and their meaning",
               f"{m}: simple daily pooja and good habits for the whole family"]
    if info.get("season"):
        angles.append(f"{info['season']}: its customs and their meaning")
    for f in info["festivals"]:
        if f["in_days"] > 3:
            angles.append(f"{f['en']} ({f['te']}) is coming in {f['in_days']} days: how to get ready")
    return angles


def generate_devotional_post(state, ig_titles):
    """Telugu devotional carousel about the current month / festival. Returns True if out/post/ is ready."""
    try:
        if not features.check("raqm"):
            print("Devotional post skipped: Telugu text shaping (raqm) is not available on this machine.")
            return False
        info = telugu_today()
        print(f"Telugu calendar: {info['month_en']} masam, {info['weekday']}, special: {info['special_day'] or '-'}, "
              f"festivals: {[(f['en'], f['in_days']) for f in info['festivals']] or '-'}")
        used = [u.get("title", "") for u in state["used"]] + [re.sub(r"^[^\w]+", "", t).strip() for t in ig_titles]
        angles = devotional_angles(info)
        now = dt.datetime.now(IST)
        start = (now.timetuple().tm_yday * 8 + now.hour // 3) % len(angles)   # a different angle every slot
        if any(f["in_days"] == 0 for f in info["festivals"]) and now.hour < 12:
            start = 0   # festival morning: lead with the festival itself
        idea = None
        for angle in (angles[start:] + angles[:start])[:2]:
            print(f"Devotional angle: {angle}")
            idea = devotional_concept(info, angle, [t for t in used if t])
            if idea:
                break
        if not idea:
            return False
        print(f"Devotional post: {idea['title']}")
        idea["kind"] = "devotional"
        idea["month_tag"] = f"#{info['month_en'].lower()}masam" if info["month_en"] else ""
        return build_carousel(idea, DEVOTION_STYLE, "devotional post")
    except Exception as e:
        print(f"Devotional post failed: {str(e)[:500]}")
        return False


# story videos only (UTC hours). Kaggle starts 06:30 IST -> video 1 ~08:30 IST | saved video 2 at 12:30 IST |
# Kaggle starts 16:30 IST -> video 3 ~18:30 IST | saved video 4 at 21:30 IST (Telugu prime time)
SLOT_PLAN_DEFAULT = "1:animated,7:queued,11:animated,16:queued"


def slot_task():
    """What this run makes: REEL_FORMAT, or the 3-hour slot's task from SLOT_PLAN (UTC hour: task)."""
    fmt = os.getenv("REEL_FORMAT", "auto").strip().lower()
    if fmt in ("animated", "queued", "trending", "post", "factpost"):
        return fmt
    now = dt.datetime.now(dt.timezone.utc)
    hour = now.hour + now.minute / 60
    plan = {}
    for item in (os.getenv("SLOT_PLAN") or SLOT_PLAN_DEFAULT).split(","):
        h, _, task = item.partition(":")
        if h.strip().isdigit():
            plan[int(h)] = task.strip().lower()
    # the planned hour nearest to now (GitHub often starts scheduled runs late)
    gap = lambda h: min((hour - h) % 24, (h - hour) % 24)
    best = min(plan, key=gap, default=None)
    return plan[best] if best is not None and gap(best) <= 1.25 else "none"


# -------------------------------- manual mode: you make the clip with your own Gemini plan, the bot does the rest
INBOX = pathlib.Path("inbox")


def manual_wanted():
    return os.getenv("MANUAL_VIDEOS") == "1"


def save_state_dict(st):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def tg_post(method, params=None, timeout=60):
    tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=params or {}, timeout=timeout)
    return r.json()


def tg_say(text):
    if not telegram_configured():
        return
    try:
        tg_post("sendMessage", {"chat_id": os.getenv("TELEGRAM_CHAT_ID").strip(), "text": text[:3900]})
    except Exception as e:
        print("Telegram message failed:", str(e).replace(os.getenv("TELEGRAM_BOT_TOKEN", "x"), "***"))


def gh_headers():
    tok = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def gh_ready():
    return bool((os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip() and os.getenv("GITHUB_REPOSITORY"))


def idea_markdown(i, mention=""):
    return (f"{mention}\n" if mention else "") + (
        f"# Video idea #{i['id']}: {i['title']}\n\n"
        f"**Telugu caption the bot will put on the video:** {i['caption']}\n\n"
        "## What to do\n"
        "1. Open **Gemini** (or **Google Flow**) with your Google AI plan, choose **Gemini Omni Flash**, vertical **9:16**, **15 seconds**.\n"
        "2. Paste the prompt below and generate the video, then download it.\n"
        f"3. In this repo open the **inbox** folder > **Add file** > **Upload files**, upload the video named **{i['id']}.mp4**, and commit.\n"
        "The bot adds the Telugu caption and posts it to Instagram within a minute or two.\n\n"
        "## Prompt to paste\n\n```text\n" + i["video_prompt"] + "\n```\n")


def publish_idea(idea):
    """Make the idea easy to find without any extra app: a file in the repo, the run summary, and an Issue."""
    d = pathlib.Path("ideas")
    d.mkdir(exist_ok=True)
    (d / f"idea-{idea['id']}.md").write_text(idea_markdown(idea), encoding="utf-8")
    summ = os.getenv("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(idea_markdown(idea) + "\n")
    if gh_ready() and os.getenv("MANUAL_ISSUES") != "0":
        try:
            owner = os.getenv("GITHUB_REPOSITORY_OWNER", "")
            r = requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues", headers=gh_headers(),
                              json={"title": f"Video idea #{idea['id']}: {idea['title']}",
                                    "body": idea_markdown(idea, mention=f"@{owner}" if owner else "")}, timeout=30)
            if r.status_code == 201:
                idea["issue"] = r.json().get("number")
            else:
                print("Issue creation failed:", r.status_code, r.text[:200])
        except Exception as e:
            print("Issue creation failed:", e)


def finish_idea_files(idea):
    try:
        os.remove(pathlib.Path("ideas") / f"idea-{idea['id']}.md")
    except OSError:
        pass
    if idea.get("issue") and gh_ready():
        try:
            requests.patch(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{idea['issue']}",
                           headers=gh_headers(), json={"state": "closed"}, timeout=30)
        except Exception:
            pass


def idea_message(i):
    return (f"🎬 New video idea #{i['id']}: {i['title']}\n\n"
            "1) Open Gemini (or Google Flow) and choose Gemini Omni Flash, vertical 9:16, 15 seconds.\n"
            "2) Paste this prompt:\n\n" + i["video_prompt"] + "\n\n"
            f"3) Send the finished video back to this chat (as a File keeps the full quality) with the caption {i['id']}.\n"
            "The bot adds the Telugu caption and posts it.")


def top_up_ideas(state, ig_titles):
    limit = int(os.getenv("MANUAL_PENDING_MAX", "2"))
    made = 0
    while len(state.setdefault("ideas", [])) < limit and made < 3:
        pend_t = [x["title"] for x in state["ideas"]]
        pend_m = [x["moral"] for x in state["ideas"]]
        used_t = [u.get("title", "") for u in state["used"]] + [t.replace("📖", "").strip() for t in ig_titles] + pend_t
        used_m = [u.get("moral", "") for u in state["used"]] + pend_m
        idea = omni_concept([t for t in used_t if t], [m_ for m_ in used_m if m_])
        if not idea:
            break
        n = state.get("next_idea_id", 1)
        state["next_idea_id"] = n + 1
        idea = {k: idea.get(k) for k in ("title", "caption", "question", "moral", "video_prompt", "tag", "hashtags")} | {"id": n}
        state["ideas"].append(idea)
        print(f"New idea #{n}: {idea['title']}\n--- prompt for Gemini/Flow ---\n{idea['video_prompt']}\n------------------------------")
        publish_idea(idea)
        save_state_dict(state)
        tg_say(idea_message(idea))
        made += 1
    return made


def pick_idea(state, hint):
    ideas = state.get("ideas", [])
    m = re.search(r"\d+", hint or "")
    if m:
        for i in ideas:
            if i["id"] == int(m.group()):
                return i
    return ideas[0] if ideas else None


def tg_fetch_clip(state):
    """Return (path, caption_hint) for the oldest video you sent to the bot chat, or None."""
    if not telegram_configured():
        return None
    chat = int(os.getenv("TELEGRAM_CHAT_ID").strip())
    try:
        res = tg_post("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0, "allowed_updates": json.dumps(["message"])})
    except Exception as e:
        print("Telegram check failed:", str(e).replace(os.getenv("TELEGRAM_BOT_TOKEN", "x"), "***"))
        return None
    for upd in res.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1  # consume this update
        msg = upd.get("message") or {}
        if (msg.get("chat") or {}).get("id") != chat:
            continue  # only accept videos from your own chat
        media = msg.get("video") or (msg.get("document") if str((msg.get("document") or {}).get("mime_type", "")).startswith("video/") else None)
        if not media:
            continue
        info = tg_post("getFile", {"file_id": media["file_id"]}).get("result") or {}
        if not info.get("file_path"):
            tg_say("I could not download that video (Telegram allows bots up to 20 MB). Please send a smaller file.")
            continue
        out = OUT / "manual_raw.mp4"
        tok = os.getenv("TELEGRAM_BOT_TOKEN").strip()
        with requests.get(f"https://api.telegram.org/file/bot{tok}/{info['file_path']}", stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return out, msg.get("caption", "")
    return None


def inbox_fetch_clip():
    files = sorted(p for p in INBOX.glob("*") if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}) if INBOX.exists() else []
    return (files[0], files[0].stem) if files else None


def generate_manual(state, ig_titles):
    OUT.mkdir(exist_ok=True)
    dry = os.getenv("DRY_RUN") == "1"
    clip = tg_fetch_clip(state)
    source = "telegram"
    if not clip:
        clip, source = inbox_fetch_clip(), "inbox"
    if clip:
        raw, hint = clip
        idea = pick_idea(state, hint)
        if not idea:
            tg_say("I received a video but have no waiting idea for it. A new idea will be created soon.")
            print("Video received but there is no pending idea. Skipping.")
            if not dry:
                save_state_dict(state)
        else:
            try:
                now = dt.datetime.now(IST)
                finish_omni_video(raw, idea["caption"], OUT / "reel.mp4")
            except Exception as e:
                print(f"Could not process the video: {str(e)[:400]}")
                tg_say("I could not process that video file. Please send it again as a File.")
                if not dry:
                    save_state_dict(state)
                return False
            if not dry:  # a dry run only previews: it keeps your video and idea for the real run
                if source == "inbox":
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
                state["ideas"] = [x for x in state["ideas"] if x["id"] != idea["id"]]
                finish_idea_files(idea)
                save_state_dict(state)
            else:
                print("DRY RUN: preview built; your video and idea stay queued for the real run.")
            extra = [t for t in (idea.get("hashtags") or []) if isinstance(t, str) and re.fullmatch(r"#[A-Za-z0-9_]{3,30}", t)]
            tag = idea.get("tag") if re.fullmatch(r"#[A-Za-z0-9_]{3,30}", str(idea.get("tag", ""))) else "#story"
            tag_list = []
            for tg in [tag] + extra[:2] + ["#telugureels", "#teluguanimation", "#moralstories", "#telugu"]:
                if tg not in tag_list:
                    tag_list.append(tg)
            caption = "\n".join([f"📖 {idea['title']}", "", idea["caption"], "", f"నీతి: {idea['moral']}", "",
                                 "ఈ రీల్ మీ స్నేహితుడికి పంపండి 💌", (idea.get("question") or "మీకు నచ్చిందా? కామెంట్‌లో చెప్పండి 👇") + " 👇", "", " ".join(tag_list[:5])])[:2200]
            name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
            (OUT / "meta.json").write_text(json.dumps({"caption": caption, "video_name": name,
                                                       "story_id": "clip-" + hashlib.md5(idea["title"].encode()).hexdigest()[:8],
                                                       "title": idea["title"], "moral": idea["moral"]}, ensure_ascii=False), encoding="utf-8")
            print(f"Video #{idea['id']} captioned and ready ({os.path.getsize(OUT / 'reel.mp4') / 1e6:.1f} MB)")
            if not dry:
                tg_say(f"✅ Got your video for idea #{idea['id']}. Adding the Telugu caption and posting it now.")
            set_output("has_post", "true")
            set_output("video_name", name)
            return True
    else:
        print("No finished video waiting.")
    top_up_ideas(state, ig_titles)
    save_state_dict(state)
    pend = [f"#{x['id']} {x['title']}" for x in state.get("ideas", [])]
    print("Ideas waiting for your video:", pend or "none")
    return False


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
    print(f"MODE: PIXAR_STORIES={os.getenv('PIXAR_STORIES', '0')} MANUAL_VIDEOS={os.getenv('MANUAL_VIDEOS', '0')} "
          f"OMNI_VIDEOS={os.getenv('OMNI_VIDEOS', '0')} | story AI: "
          f"{'Gemini' if gemini_key() else 'Claude' if anthropic_key() else 'free Pollinations'} | "
          f"images: {'Hugging Face' if os.getenv('HF_TOKEN') else 'free Pollinations'}")
    if manual_wanted():
        if not generate_manual(state, ig):
            set_output("has_post", "false")
        return
    if omni_wanted() and generate_omni(state, ig):
        return
    if os.getenv("PIXAR_STORIES") == "1" and os.getenv("PIXAR_FORMAT", "short") == "short":
        # every 3 hours: animated story reels (Kaggle GPU) at the peak slots, devotional image posts in the others
        task = slot_task()
        print(f"THIS RUN: {task}")
        made = {"post": generate_devotional_post, "factpost": generate_fact_post, "queued": post_queued,
                "trending": generate_trend_reel, "animated": generate_pixar_short}.get(task, lambda s, i: False)(state, ig)
        if made:
            return
        # story videos only by default; FALLBACK_POST=1 fills a failed slot with a devotional image post
        if task in ("animated", "queued") and os.getenv("FALLBACK_POST", "0") == "1":
            print(f"Nothing from '{task}' this slot: posting a devotional image post instead.")
            if generate_devotional_post(state, ig):
                return
        print("Nothing made this run; the next scheduled run will try again.")
        set_output("has_post", "false")
        return
    story = None
    if os.getenv("PIXAR_STORIES") == "1":   # PIXAR_FORMAT=story: longer narrated story over Pixar stills
        used_titles = [u.get("title", "") for u in state["used"]] + [x["title"] for x in STORIES] + [t.replace("📖", "").strip() for t in ig]
        used_morals = [u.get("moral", "") for u in state["used"]] + [x["moral"] for x in STORIES]
        try:
            story = pixar_ai_story([t for t in used_titles if t], [m_ for m_ in used_morals if m_])
        except Exception as e:
            print(f"AI Pixar story failed: {e}")
        if not story:   # never fall back to the old built-in stories in Pixar mode
            print("AI Pixar story could not be written this run (see errors above). Skipping; the next run will try again.")
            set_output("has_post", "false")
            return
    story = story or choose_story(state, ig)
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

    hook_text = story.get("hook") or f"{story['title']}. చివరి వరకు చూడండి!"
    segments = [{"kind": "intro", "who": "narrator", "spk": NARRATOR, "text": hook_text}]
    drop_idx = None
    for i, (who, text) in enumerate(story["lines"]):
        segments.append({"kind": "line", "li": i, "who": who, "text": text,
                         "spk": NARRATOR if who == "narrator" else (story.get("speakers") or CAST)[who]})
        if story.get("drop_line") == i:
            drop_idx = len(segments) - 1
    segments.append({"kind": "moral", "who": "narrator", "spk": NARRATOR, "text": f"నీతి: {story['moral']}"})
    segments.append({"kind": "outro", "who": "narrator", "spk": NARRATOR, "text": "ఈ కథ మీ స్నేహితుడికి పంపండి, ఫాలో అవ్వండి."})

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
    if not clips and (os.getenv("PIXAR_CLIPS") == "1" or story.get("shots")):
        try:
            clips = make_pixar_clips(story)
        except Exception as e:
            print(f"Pixar clips failed ({str(e)[:300]}); using the built-in cartoon engine.")
            clips = []
    if not clips and story.get("speakers"):
        print("No Pixar shots could be made for this AI story (the cartoon engine cannot draw its new characters). Skipping this run.")
        set_output("has_post", "false")
        return
    if clips:
        print(f"Cinematic clip mode: {len(clips)} clips")
        render_clip_video(story, segments, starts, durs, total, audio, OUT / "reel.mp4", clips)
    else:
        render_video(story, segments, starts, durs, envs, total, audio, OUT / "reel.mp4", story_no, drop_t)

    tag_list = []
    for tg in [story.get("tag", "#story")] + (story.get("hashtags") or [])[:2] + rng.sample(HASHTAGS, 3) + ["#telugu"]:
        if tg not in tag_list:
            tag_list.append(tg)
    tags = " ".join(tag_list[:5])  # Instagram allows at most 5 hashtags
    caption = "\n".join([f"📖 {story['title']}"] + ([story["hook"]] if story.get("hook") else []) + ["", f"నీతి: {story['moral']}", "",
                         "ఈ రీల్ మీ స్నేహితుడికి పంపండి 💌", "మీకు ఏ పాత్ర బాగా నచ్చింది? కామెంట్‌లో చెప్పండి 👇", "", tags])[:2200]
    name = f"reel-{now.strftime('%Y%m%d-%H%M')}.mp4"
    (OUT / "meta.json").write_text(json.dumps({"caption": caption, "video_name": name, "story_id": story["id"],
                                               "title": story["title"], "moral": story["moral"]}, ensure_ascii=False), encoding="utf-8")
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


def wait_container(container, token, tries=60):
    for _ in range(tries):
        st = requests.get(f"{GRAPH}/{container}", params=sign({"fields": "status_code,status", "access_token": token}),
                          timeout=30).json()
        code = st.get("status_code")
        if code in (None, "FINISHED"):   # image containers may not report a status at all
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the media: {st}")
        time.sleep(5)
    raise RuntimeError("Timed out waiting for Instagram to process the media.")


def publish():
    meta_path = OUT / "meta.json"
    if not meta_path.exists():
        print("Nothing to publish.")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
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
    base = f"https://raw.githubusercontent.com/{repo}/{os.getenv('MEDIA_BRANCH', 'media')}"
    if meta.get("type") == "carousel":   # image post: one container per card, then the carousel
        children = []
        for name in meta["images"]:
            wait_until_public(f"{base}/{name}")
            cid = api("POST", f"{user_id}/media", image_url=f"{base}/{name}", is_carousel_item="true",
                      access_token=token)["id"]
            wait_container(cid, token)
            children.append(cid)
        container = api("POST", f"{user_id}/media", media_type="CAROUSEL", children=",".join(children),
                        caption=meta["caption"], access_token=token)["id"]
        wait_container(container, token)
        res = api("POST", f"{user_id}/media_publish", creation_id=container, access_token=token)
        print(f"Published image post ({len(children)} cards)! Media ID: {res['id']}")
        save_state(meta)
        return
    url = f"{base}/{meta['video_name']}"
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
    if cmd in ("trend", "post", "factpost"):   # one trending fact reel / devotional post / fact post in out/ (not posted)
        os.environ.update(PIXAR_STORIES="1", PIXAR_FORMAT="short", REEL_FORMAT="trending" if cmd == "trend" else cmd)
        generate()
        sys.exit(0)
    if cmd in ("pixar-reel", "short"):   # AI writes a new idea and it becomes a reel in out/reel.mp4 (not posted)
        os.environ["PIXAR_STORIES"] = "1"
        if cmd == "short":
            os.environ["PIXAR_FORMAT"] = "short"
        else:
            os.environ.setdefault("PIXAR_FORMAT", "story")
        generate()
        sys.exit(0)
    if cmd == "pixar":   # python main.py pixar [story_id|all] [--force]
        want = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else "all"
        for st in STORIES:
            if want in ("all", st["id"]):
                print(f"Making Pixar clips for {st['id']}")
                make_pixar_clips(st, force="--force" in sys.argv)
        sys.exit(0)
    if cmd in ("generate", "all"):
        # keep a copy of this run's output in state/last_run.log (committed by the workflow) so problems can be
        # diagnosed without access to the Actions logs. Only story text and status lines are printed, never tokens.
        class _Tee:
            def __init__(self, *s):
                self.s = s

            def write(self, x):
                for f in self.s:
                    f.write(x)

            def flush(self):
                for f in self.s:
                    f.flush()

            def __getattr__(self, name):   # encoding, isatty, fileno... come from the real console
                return getattr(self.s[0], name)
        STATE.parent.mkdir(exist_ok=True)
        _log = open(STATE.parent / "last_run.log", "w", encoding="utf-8")
        _log.write(f"run at {dt.datetime.now(dt.timezone.utc).isoformat()} event={os.getenv('GITHUB_EVENT_NAME', 'local')}\n")
        sys.stdout = _Tee(sys.__stdout__, _log)
        try:
            generate()
        except Exception as e:
            print(f"generate crashed: {e!r}"[:800])
            raise
        finally:
            sys.stdout = sys.__stdout__
            _log.close()
    if cmd in ("publish", "all"):
        publish()

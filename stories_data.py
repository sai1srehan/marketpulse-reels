# -*- coding: utf-8 -*-
"""
Cast + stories. Each story = one Reel. Speakers: a cast id (e.g. "snake") or "narrator".
Add your own stories by copying a block (keep sentences short, kid-friendly, and have a Telugu speaker check them).
Scenes: day | sunset | night.   The first cast member stands on the left, the second on the right.
"""

# species: boy snake lion mouse rabbit tortoise crow fox
# voice: male (Mohan) / female (Shruti); semi = pitch shift in semitones (cartoon voice); rate = speed %
CAST = {
    "boy":      {"species": "boy",      "name": "రాము",     "voice": "male",   "semi": 4,  "rate": 0,   "scale": 1.0,  "color": (60, 130, 220)},
    "snake":    {"species": "snake",    "name": "పాము",     "voice": "male",   "semi": -3, "rate": -8,  "scale": 1.0,  "color": (50, 150, 70)},
    "lion":     {"species": "lion",     "name": "సింహం",    "voice": "male",   "semi": -5, "rate": -4,  "scale": 1.1,  "color": (215, 140, 40)},
    "mouse":    {"species": "mouse",    "name": "ఎలుక",     "voice": "female", "semi": 5,  "rate": 4,   "scale": 0.8,  "color": (130, 130, 160)},
    "rabbit":   {"species": "rabbit",   "name": "కుందేలు",  "voice": "female", "semi": 3,  "rate": 4,   "scale": 0.95, "color": (225, 120, 150)},
    "tortoise": {"species": "tortoise", "name": "తాబేలు",   "voice": "male",   "semi": -4, "rate": -14, "scale": 0.95, "color": (60, 140, 75)},
    "crow":     {"species": "crow",     "name": "కాకి",     "voice": "female", "semi": 1,  "rate": 0,   "scale": 0.95, "color": (70, 72, 120)},
    "fox":      {"species": "fox",      "name": "నక్క",     "voice": "female", "semi": -2, "rate": 0,   "scale": 1.0,  "color": (225, 110, 30)},
}
NARRATOR = {"name": "కథకుడు", "voice": "female", "semi": 0, "rate": 0, "color": (120, 90, 200)}

STORIES = [
    {"id": "snake_friend", "title": "పాము మిత్రుడు", "scene": "day", "cast": ["boy", "snake"], "tag": "#snakes",
     "lines": [
         ("narrator", "ఒక అడవి పక్కన రాము అనే బాలుడు ఆడుకుంటున్నాడు."),
         ("boy", "అమ్మో! పాము! నన్ను కరవకు, ప్లీజ్!"),
         ("snake", "భయపడకు రామూ, నేను నిన్ను ఏమీ చేయను."),
         ("boy", "అరే! పాము మాట్లాడుతోందా?"),
         ("snake", "అవును. నీకో విషయం చెప్పనా? నాకు కనురెప్పలు ఉండవు, అందుకే కళ్లు తెరిచే నిద్రపోతాను."),
         ("boy", "వావ్! ఇంకా ఏం చెబుతావు?"),
         ("snake", "నేను నాలుకతో వాసనలను పసిగడతాను. అన్ని పాములూ విషపు పాములు కావు."),
         ("snake", "కానీ ఏ పామునైనా దూరం నుంచే చూడాలి, దగ్గరకు వెళ్లకూడదు."),
         ("boy", "సరే మిత్రమా, ఇప్పుడు అర్థమైంది. నేను అందరికీ చెబుతాను."),
     ],
     "moral": "తెలియకుండా భయపడకండి, తెలుసుకుని జాగ్రత్తగా ఉండండి."},

    {"id": "lion_mouse", "title": "సింహం మరియు ఎలుక", "scene": "sunset", "cast": ["lion", "mouse"], "tag": "#lion",
     "lines": [
         ("narrator", "అడవిలో ఒక సింహం గాఢంగా నిద్రపోతోంది. ఒక చిన్న ఎలుక పొరపాటున దాని మీద పరుగెత్తింది."),
         ("lion", "ఎవరది! నా నిద్ర చెడగొడతావా? నిన్ను తినేస్తాను!"),
         ("mouse", "మహారాజా, క్షమించండి! నన్ను వదిలేస్తే ఏదో ఒక రోజు మీకు సాయం చేస్తాను."),
         ("lion", "హా హా! నువ్వు నాకు సాయం చేస్తావా? సరే, వెళ్లు."),
         ("narrator", "కొన్ని రోజుల తర్వాత సింహం వేటగాడి వలలో చిక్కుకుంది."),
         ("lion", "అయ్యో! ఈ వల నుంచి నేను బయటపడలేకపోతున్నాను!"),
         ("mouse", "భయపడకండి మహారాజా! నేను ఇప్పుడే వల తాళ్లను కొరికేస్తాను."),
         ("lion", "నువ్వు చిన్నదానివైనా నా ప్రాణం కాపాడావు. ధన్యవాదాలు మిత్రమా."),
     ],
     "moral": "చిన్న వారిని తక్కువగా చూడకూడదు, చిన్న సాయం కూడా గొప్పదే."},

    {"id": "tortoise_rabbit", "title": "తాబేలు మరియు కుందేలు", "scene": "day", "cast": ["rabbit", "tortoise"], "tag": "#tortoise",
     "lines": [
         ("narrator", "ఒక అడవిలో కుందేలు తాబేలును చూసి నవ్వింది."),
         ("rabbit", "హా హా! తాబేలన్నా, నువ్వు ఇంత నెమ్మదిగా నడుస్తావేంటి?"),
         ("tortoise", "నెమ్మదిగా నడిచినా నేను నీతో పందెం కడతాను. ఎవరు ముందు చేరుకుంటారో చూద్దాం!"),
         ("rabbit", "సరే సరే! నేను గెలవడం ఖాయం."),
         ("narrator", "పందెం మొదలైంది. కుందేలు వేగంగా పరుగెత్తి చాలా ముందుకు వెళ్లిపోయింది."),
         ("rabbit", "తాబేలు ఇంకా చాలా వెనుక ఉంది. కాసేపు చెట్టు కింద నిద్రపోతాను."),
         ("tortoise", "నెమ్మదిగా అయినా ఆగకుండా వెళ్తే గమ్యం చేరుకుంటాను."),
         ("narrator", "కుందేలు నిద్ర లేచి చూసేసరికి, తాబేలు గెలిచేసింది!"),
         ("rabbit", "అయ్యో! నేను నిర్లక్ష్యం చేశాను."),
     ],
     "moral": "పట్టుదలతో ఆగకుండా ప్రయత్నిస్తే గెలుపు మనదే."},

    {"id": "crow_fox", "title": "కాకి మరియు నక్క", "scene": "sunset", "cast": ["crow", "fox"], "tag": "#crow",
     "lines": [
         ("narrator", "ఒక చెట్టు కొమ్మపై కాకి నోట్లో రొట్టె ముక్క పట్టుకుని కూర్చుంది."),
         ("fox", "అబ్బా! ఏం వాసన! కాకమ్మా, నువ్వు ఎంత అందంగా ఉన్నావు!"),
         ("fox", "నీ గొంతు కూడా చాలా మధురం అంటారు. ఒక్క పాట పాడవా?"),
         ("narrator", "పొగడ్తలు విని కాకి చాలా సంతోషపడింది."),
         ("crow", "కా... కా... కా..."),
         ("narrator", "కాకి నోరు తెరవగానే రొట్టె ముక్క కింద పడిపోయింది. నక్క దాన్ని ఎత్తుకుంది."),
         ("fox", "ధన్యవాదాలు కాకమ్మా! పొగడ్తలకు మురిసిపోతే ఇలాగే అవుతుంది."),
         ("crow", "అయ్యో! నేను మోసపోయాను."),
     ],
     "drop_line": 5, "moral": "పొగడ్తలకు పొంగిపోకూడదు, ఆలోచించి నడుచుకోవాలి."},

    {"id": "clever_rabbit", "title": "తెలివైన కుందేలు", "scene": "night", "cast": ["rabbit", "lion"], "tag": "#rabbit",
     "lines": [
         ("narrator", "ఒక అడవిలో ఒక పెద్ద సింహం రోజూ జంతువులను భయపెట్టేది."),
         ("narrator", "ఒకరోజు ఒక చిన్న కుందేలు సింహం దగ్గరకు వెళ్లింది."),
         ("rabbit", "మహారాజా, ఆలస్యం అయినందుకు క్షమించండి. దారిలో మరో సింహం నన్ను ఆపింది."),
         ("lion", "ఏమిటీ! మరో సింహమా? చూపించు, ఆ సింహం ఎక్కడ ఉంది?"),
         ("rabbit", "అదిగో ఆ నీటిలో ఉంది మహారాజా. చూడండి, ఎంత కోపంగా ఉందో!"),
         ("lion", "అరే! ఇది నా నీడేనా? నేనే భయపడ్డానా!"),
         ("rabbit", "అవును మహారాజా. కోపంలో ఆలోచించకపోతే ఇలాగే తికమకపడతాం."),
         ("lion", "నువ్వు నాకు మంచి పాఠం నేర్పావు చిన్న మిత్రమా. ఇకపై ఎవరినీ భయపెట్టను."),
     ],
     "moral": "బలం కన్నా బుద్ధి గొప్పది."},
]


# ---------------------------------------------------------------------------
# CINEMATIC CLIP MODE (the "ultiworld" look): you make one short AI video clip per shot,
# the bot adds Telugu voice, captions, music and posts. Prompts are generated by:  python main.py prompts
STYLE = ("Pixar-style 3D animated short film, cinematic lighting, shallow depth of field, soft warm glow, "
         "expressive cute characters with big emotional eyes, smooth camera movement, vertical 9:16, 5 seconds, "
         "no text, no subtitles, no logos")

CHARACTER_LOOKS = {
    "snake_friend": "RAMU: a cheerful 8-year-old Indian boy with black hair, blue t-shirt and brown shorts. "
                    "SNAKE: a friendly bright-green cartoon snake with big kind eyes and a gentle smile.",
    "lion_mouse": "LION: a big golden lion with a thick brown mane and proud eyes. "
                  "MOUSE: a tiny grey cartoon mouse with big round ears and shiny eyes.",
    "tortoise_rabbit": "RABBIT: a white cartoon rabbit with long pink-inside ears, cheeky smile. "
                       "TORTOISE: a kind green cartoon tortoise with a dome shell and gentle eyes.",
    "crow_fox": "CROW: a glossy black cartoon crow with bright orange beak. "
                "FOX: a clever orange cartoon fox with white cheeks and a sly smile.",
    "clever_rabbit": "RABBIT: a small white cartoon rabbit with long ears, brave and polite. "
                     "LION: a huge golden lion with a thick mane, angry but proud.",
}

# lines = indexes into story["lines"] shown during this shot
SHOTS = {
    "snake_friend": [
        {"lines": [0], "scene": "Wide shot: Ramu plays with a small toy at the edge of a green forest on a sunny afternoon."},
        {"lines": [1], "scene": "Close-up: Ramu freezes in surprise, eyes wide, as a green snake rises from the tall grass in front of him."},
        {"lines": [2, 3], "scene": "Medium shot: the friendly green snake raises its head and talks gently to Ramu, who looks amazed."},
        {"lines": [4, 5], "scene": "Close-up of the snake's big eyes and Ramu leaning in, fascinated."},
        {"lines": [6, 7], "scene": "The snake flicks its tongue and shows Ramu a safe distance on the ground with its tail; Ramu nods."},
        {"lines": [8], "scene": "Ramu waves goodbye with a smile as the snake slithers back into the grass at sunset."},
    ],
    "lion_mouse": [
        {"lines": [0], "scene": "A big golden lion sleeps under a tree in the savanna at sunset while a tiny grey mouse runs across his paw."},
        {"lines": [1, 2], "scene": "The lion wakes up angry and holds the frightened mouse by its tail; the mouse pleads with folded paws."},
        {"lines": [3], "scene": "The lion laughs and lets the mouse go; the mouse runs away happily."},
        {"lines": [4, 5], "scene": "Days later: the lion is trapped in a hunter's rope net in the forest, looking sad and helpless."},
        {"lines": [6], "scene": "The tiny mouse arrives and bravely gnaws the ropes of the net."},
        {"lines": [7], "scene": "The freed lion and the little mouse smile at each other as friends in the golden sunset."},
    ],
    "tortoise_rabbit": [
        {"lines": [0, 1], "scene": "A white rabbit laughs and points at a slow, kind green tortoise on a sunny forest path."},
        {"lines": [2, 3], "scene": "The tortoise challenges the rabbit; they stand side by side at a starting line."},
        {"lines": [4], "scene": "The rabbit sprints fast down the path with dust flying, far ahead of the tortoise."},
        {"lines": [5], "scene": "The rabbit lies down and falls asleep under a big tree while the tortoise keeps walking in the background."},
        {"lines": [6, 7], "scene": "The tortoise steadily walks across the finish line while the rabbit still sleeps behind."},
        {"lines": [8], "scene": "The rabbit wakes up shocked and sad; the tortoise is celebrated at the finish line."},
    ],
    "crow_fox": [
        {"lines": [0], "scene": "A black crow sits on a tree branch holding a piece of bread in her beak at sunset."},
        {"lines": [1, 2], "scene": "A clever orange fox below looks up and flatters the crow with a sweet smile."},
        {"lines": [3, 4], "scene": "The proud crow puffs up her chest and begins to sing."},
        {"lines": [5], "scene": "The bread falls from the crow's open beak and the fox catches it."},
        {"lines": [6, 7], "scene": "The fox walks away happily with the bread while the crow looks sad on the branch."},
    ],
    "clever_rabbit": [
        {"lines": [0, 1], "scene": "A huge lion roars in a moonlit forest while small animals hide; a tiny rabbit walks toward him."},
        {"lines": [2, 3], "scene": "The rabbit bows politely to the lion and talks; the lion looks surprised and angry."},
        {"lines": [4], "scene": "The rabbit leads the lion to a still pond at night and points at the water."},
        {"lines": [5], "scene": "The lion sees his own reflection in the water and looks confused."},
        {"lines": [6, 7], "scene": "The lion realizes it is his own reflection and smiles kindly at the rabbit under the moon."},
    ],
}


# ---------------------------------------------------------------------------
# ACTING + STAGING (used by the built-in 3D-toon renderer, no outside tools)
# EMOTIONS[story][line] = how the speaker looks while saying that line:
# neutral happy excited laugh kind proud sly sad angry surprised scared sleepy
EMOTIONS = {
    "snake_friend": ["happy", "scared", "kind", "surprised", "kind", "excited", "kind", "proud", "happy"],
    "lion_mouse": ["neutral", "angry", "scared", "laugh", "neutral", "sad", "kind", "happy"],
    "tortoise_rabbit": ["neutral", "laugh", "proud", "sly", "neutral", "sleepy", "proud", "neutral", "sad"],
    "crow_fox": ["neutral", "sly", "sly", "neutral", "proud", "neutral", "laugh", "sad"],
    "clever_rabbit": ["neutral", "neutral", "kind", "angry", "sly", "surprised", "kind", "happy"],
}

# STAGING[story]["init"] and STAGING[story][line] update (and persist): pos{cast:x}, pose{cast:idle|run|sleep},
# lift{cast:px}, emo{cast:emotion} (for non-speakers), props{name:x|cast}
STAGING = {
    "snake_friend": {
        "init": {"emo": {"boy": "happy", "snake": "neutral"}},
        1: {"pos": {"boy": 190}},
        3: {"pos": {"boy": 240}},
        8: {"pos": {"boy": 330}, "emo": {"snake": "happy"}},
    },
    "lion_mouse": {
        "init": {"pose": {"lion": "sleep"}, "emo": {"lion": "sleepy", "mouse": "neutral"}, "pos": {"mouse": 640}},
        0: {"pos": {"mouse": 520}, "pose": {"mouse": "run"}},
        1: {"pose": {"lion": "idle", "mouse": "idle"}, "emo": {"lion": "angry", "mouse": "scared"}, "pos": {"mouse": 600}},
        3: {"emo": {"mouse": "happy"}, "pos": {"mouse": 1420}, "pose": {"mouse": "run"}},
        4: {"props": {"net": "lion"}, "emo": {"lion": "sad"}},
        6: {"pos": {"mouse": 520}, "pose": {"mouse": "idle"}, "emo": {"mouse": "kind"}},
        7: {"props": {}, "emo": {"lion": "happy", "mouse": "happy"}, "pos": {"mouse": 560}},
    },
    "tortoise_rabbit": {
        "init": {"emo": {"rabbit": "happy", "tortoise": "kind"}},
        2: {"emo": {"rabbit": "sly"}},
        4: {"pos": {"rabbit": 1500, "tortoise": 880}, "pose": {"rabbit": "run"}},
        5: {"pos": {"rabbit": 330}, "pose": {"rabbit": "sleep"}, "emo": {"rabbit": "sleepy", "tortoise": "proud"},
            "props": {"tree": 300}},
        6: {"pos": {"tortoise": 760}},
        7: {"pos": {"tortoise": 860}, "props": {"tree": 300, "flag": 960}},
        8: {"pose": {"rabbit": "idle"}, "emo": {"tortoise": "happy"}},
    },
    "crow_fox": {
        "init": {"lift": {"crow": 330}, "props": {"tree": 290}, "emo": {"crow": "proud", "fox": "sly"}},
        5: {"pos": {"fox": 540}},
        6: {"pos": {"fox": 780}, "emo": {"fox": "happy"}},
        7: {"emo": {"crow": "sad"}},
    },
    "clever_rabbit": {
        "init": {"emo": {"lion": "angry", "rabbit": "neutral"}, "pos": {"rabbit": 330}},
        4: {"props": {"pond": 540}, "pos": {"lion": 780}},
        6: {"emo": {"lion": "surprised"}},
        7: {"emo": {"rabbit": "proud"}},
    },
}

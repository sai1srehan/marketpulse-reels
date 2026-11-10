# -*- coding: utf-8 -*-
"""
Serialized Telugu thriller for Instagram Reels + YouTube funnel.

Model:
  - An EPISODE is released on Instagram as several short Reels (PARTS).
  - After all parts of an episode are up, the FULL episode goes on YouTube.
  - The LAST part of each episode shows a "Watch full episode on YouTube" badge,
    and the YouTube link is added below the Reel (in the caption).

Schema (consumed by series.py):
  SERIES = {
    id, title_te, title_en, youtube_url, hashtags[],
    episodes: [ { ep, ep_title_te, parts: [ PART ] } ]
  }
  PART = {
    part, hook, scenes[], cliffhanger, yt_cta(bool)  # yt_cta True on last part of an episode
    scene = { who, voice('male'|'female'), emotion, caption, text(optional, for voice only) }
  }

Emotions drive music + subtitle colour: tense | mystery | emotional | shock | calm
`caption` is what viewers READ.  `text` (if set) is what the voice SAYS
(lets us add ElevenLabs v3 delivery tags like [whisper] without showing them).
"""

# NOTE: set this to YOUR channel/episode link before going live.
YOUTUBE_URL = "https://youtube.com/@yourchannel"

SERIES = {
    "id": "unknown",
    "title_te": "తెలియని నంబర్",
    "title_en": "UNKNOWN",
    "youtube_url": YOUTUBE_URL,
    "hashtags": ["#teluguthriller", "#teluguwebseries", "#suspense", "#telugureels",
                 "#thrillerstories", "#teluguhorror", "#webseries"],
    "episodes": [
        {
            "ep": 1,
            "ep_title_te": "ఆ వీడియో",
            "parts": [
                {
                    "part": 1,
                    "hook": "నా భర్త మంగళవారం యాక్సిడెంట్‌లో చనిపోయాడు. శుక్రవారం… ఒక తెలియని నంబర్ నుంచి ఆ యాక్సిడెంట్ వీడియో వచ్చింది.",
                    "scenes": [
                        {"who": "text", "voice": "female", "emotion": "mystery",
                         "caption": "“వాళ్ళు చెప్పినట్టు అతను చనిపోలేదు.” — ఒక స్నేహితుడు."},
                        {"who": "meera", "voice": "female", "emotion": "tense",
                         "caption": "అది క్రూరమైన జోక్ అనుకున్నా. కానీ ఆ వీడియో ప్లే చేశాక… నా వొళ్ళు గగుర్పొడిచింది.",
                         "text": "[nervous] అది క్రూరమైన జోక్ అనుకున్నా. కానీ ఆ వీడియో ప్లే చేశాక… నా వొళ్ళు గగుర్పొడిచింది."},
                        {"who": "meera", "voice": "female", "emotion": "shock",
                         "caption": "అర్జున్ కారులో ఒక్కడే బయలుదేరాడు. మరి… వెనక సీట్లో కూర్చున్నది ఎవరు?"},
                        {"who": "friend", "voice": "male", "emotion": "mystery",
                         "caption": "“కాల్ చేయకు. వాళ్ళు వింటున్నారు. రేపు రాత్రి తొమ్మిది. నువ్వు చూడని నాలుగు సెకన్లు ఇంకా ఉన్నాయి.”",
                         "text": "[whispering] కాల్ చేయకు. వాళ్ళు వింటున్నారు. రేపు రాత్రి తొమ్మిది. నువ్వు చూడని నాలుగు సెకన్లు ఇంకా ఉన్నాయి."},
                    ],
                    "cliffhanger": "ఆ వెనక సీట్లో ఉన్న వ్యక్తి చేతికి అర్జున్ వాచ్… అతనితో పాటే పాతిపెట్టిన వాచ్. అంతలో కొత్త మెసేజ్: “నీ వెనక్కి చూడు.”",
                },
                {
                    "part": 2,
                    "hook": "రాత్రి సరిగ్గా తొమ్మిదింటికి ఆ వీడియో మళ్ళీ వచ్చింది. ఈసారి… నాలుగు సెకన్లు ఎక్కువ.",
                    "scenes": [
                        {"who": "meera", "voice": "female", "emotion": "tense",
                         "caption": "ఆ అదనపు నాలుగు సెకన్లలో, వెనక సీట్లో ఉన్న వ్యక్తి ఒక పేరు నోటితో పలికాడు."},
                        {"who": "meera", "voice": "female", "emotion": "shock",
                         "caption": "ఆ పేరు… రానా. అర్జున్ బిజినెస్ పార్ట్‌నర్. అంత్యక్రియల్లో నా భుజం మీద చెయ్యేసి ఏడ్చినవాడు."},
                        {"who": "friend", "voice": "male", "emotion": "mystery",
                         "caption": "“అతను ఏడ్చింది నీ కోసం కాదు. భయపడ్డది నువ్వు ఇది కనిపెడతావని.”",
                         "text": "[tense] అతను ఏడ్చింది నీ కోసం కాదు. భయపడ్డది నువ్వు ఇది కనిపెడతావని."},
                        {"who": "meera", "voice": "female", "emotion": "tense",
                         "caption": "అంతలో డోర్ బెల్ మోగింది. కిటికీలోంచి చూశా. బయట నిల్చున్నది… రానా."},
                    ],
                    "cliffhanger": "తలుపు తీశా. “ఒంటరిగా ఉన్నావా?” అన్నాడు నవ్వుతూ. అతని చేతికి ఉన్న వాచ్… వీడియోలో చూసిన అదే వాచ్.",
                },
                {
                    "part": 3,
                    "hook": "“ఒంటరిగా ఉన్నావా?” — ఆ ప్రశ్నలో పరామర్శ లేదు. ఒక హెచ్చరిక ఉంది.",
                    "scenes": [
                        {"who": "meera", "voice": "female", "emotion": "tense",
                         "caption": "నాకు తెలీదని నటించా. కాఫీ పెట్టా. చేతులు వణుకుతున్నా, మొహం మాత్రం నవ్వింది."},
                        {"who": "meera", "voice": "female", "emotion": "mystery",
                         "caption": "అతను వెళ్ళాక, అర్జున్ కార్ తాళం కింద ఒక చిన్న నల్లని పరికరం దొరికింది. జీపీఎస్ ట్రాకర్."},
                        {"who": "friend", "voice": "male", "emotion": "shock",
                         "caption": "అదే క్షణం ఫోన్ మోగింది: “నవ్వు. అతను నిన్ను ఇప్పుడు కూడా చూస్తున్నాడు.”",
                         "text": "[whispering] నవ్వు. అతను నిన్ను ఇప్పుడు కూడా చూస్తున్నాడు."},
                    ],
                    "cliffhanger": "నేను ఒంటరిని కాదు. ఎప్పుడూ కాదు. ఆ రహస్యం ఎవరు పంపుతున్నారో… రేపు వాళ్ళని కలవబోతున్నా. ఎపిసోడ్ 2లో.",
                    "yt_cta": True,
                },
            ],
        },
    ],
}

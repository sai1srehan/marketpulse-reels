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
    part, hook, scenes[], cliffhanger, yt_cta(bool)
  }
  scene = {
    who, voice('male'|'female'), emotion, caption,
    text(optional -- what voice says if different from caption),
    action(optional -- shown on screen as stage direction)
  }

Emotions drive music + subtitle colour: tense | mystery | emotional | shock | calm
Language: daily spoken Telugu, NOT formal/literary.
"""

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
                    "hook": "మంగళవారం రాత్రి అర్జున్ పోయాడు. యాక్సిడెంట్ అన్నారు. అయిదు రోజుల తర్వాత... తెలియని నంబర్ నుంచి ఒక వీడియో వచ్చింది.",
                    "scenes": [
                        {
                            "who": "friend",
                            "voice": "male",
                            "emotion": "mystery",
                            "caption": '"అతను చావలేదు మీరా. వాళ్ళు అబద్ధం చెప్తున్నారు."',
                            "action": "[ తెలియని నంబర్ నుంచి మెసేజ్ ]",
                        },
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "tense",
                            "caption": "జోక్ అనుకున్నా. కానీ ఆ వీడియో ప్లే చేశాక... వొళ్ళు గగుర్పొడిచింది.",
                            "action": "[ ఫోన్ స్క్రీన్ మీద వీడియో ప్లే ]",
                        },
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "shock",
                            "caption": "అర్జున్ ఒంటరిగా వెళ్ళాడు. మరి... వెనక సీట్లో అది ఎవరు?",
                            "action": "[ వీడియో ఫ్రీజ్ అయింది ]",
                        },
                        {
                            "who": "friend",
                            "voice": "male",
                            "emotion": "mystery",
                            "caption": '"కాల్ చేయకు. వాళ్ళు వింటున్నారు. రేపు రాత్రి 9. నాలుగు సెకన్లు ఇంకా చూడలేదు నువ్వు."',
                            "action": "[ మెసేజ్ వచ్చింది - నంబర్ బ్లాక్ అయింది ]",
                        },
                    ],
                    "cliffhanger": "వాడి చేతికి అర్జున్ వాచ్... పాతిపెట్టిన వాచ్. అంతలో కొత్త మెసేజ్ వచ్చింది - 'నీ వెనక్కి చూడు.'",
                },
                {
                    "part": 2,
                    "hook": "రాత్రి సరిగ్గా 9కి... వీడియో మళ్ళీ వచ్చింది. ఈసారి నాలుగు సెకన్లు ఎక్కువ ఉన్నాయి.",
                    "scenes": [
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "tense",
                            "caption": "ఆ అదనపు నాలుగు సెకన్లలో - వెనక సీట్లో వాడు ఒక పేరు నోటితో చెప్పాడు.",
                            "action": "[ వీడియో స్లో మోషన్‌లో ప్లే ]",
                        },
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "shock",
                            "caption": "ఆ పేరు... రానా. అర్జున్ పార్ట్‌నర్. ఫ్యూనరల్లో నా భుజం మీద చెయ్యేసి ఏడ్చినవాడే.",
                            "action": "[ రానా ఫోటో మెదిలింది ]",
                        },
                        {
                            "who": "friend",
                            "voice": "male",
                            "emotion": "mystery",
                            "caption": '"అతను నీ కోసం ఏడవలేదు. నువ్వు తెలుసుకుంటావని భయపడ్డాడు."',
                            "action": "[ మళ్ళీ తెలియని నంబర్ ]",
                        },
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "tense",
                            "caption": "అంతలో... డోర్ బెల్. కిటికీలోంచి చూశా. బయట రానా నిల్చున్నాడు.",
                            "action": "[ డోర్ బెల్ మోగింది ]",
                        },
                    ],
                    "cliffhanger": "తలుపు తీశా. 'ఒంటరిగా ఉన్నావా?' అన్నాడు నవ్వుతూ. చేతికి... అదే వాచ్.",
                },
                {
                    "part": 3,
                    "hook": "'ఒంటరిగా ఉన్నావా?' - ఆ మాటల్లో పరామర్శ లేదు. హెచ్చరిక ఉంది.",
                    "scenes": [
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "tense",
                            "caption": "తెలీదని నటించా. కాఫీ పెట్టా. చేతులు వణుకుతున్నా - మొహం మాత్రం నవ్వింది.",
                            "action": "[ వణికే చేతులతో కాఫీ పెట్టింది ]",
                        },
                        {
                            "who": "meera",
                            "voice": "female",
                            "emotion": "mystery",
                            "caption": "రానా వెళ్ళాక... కార్ తాళాల కింద చిన్న నల్లని పరికరం దొరికింది. GPS ట్రాకర్.",
                            "action": "[ చీకట్లో వెతుకుతోంది ]",
                        },
                        {
                            "who": "friend",
                            "voice": "male",
                            "emotion": "shock",
                            "caption": '"నవ్వు. అతను నిన్ను ఇప్పుడూ చూస్తున్నాడు."',
                            "action": "[ వణికే చేతితో ఫోన్ చదివింది ]",
                        },
                    ],
                    "cliffhanger": "నేను ఒంటరిని కాదు. ఎప్పుడూ కాదు. ఈ రహస్యం ఎవరు పంపిస్తున్నారో... ఎపిసోడ్ 2లో తెలుస్తుంది.",
                    "yt_cta": True,
                },
            ],
        },
    ],
}

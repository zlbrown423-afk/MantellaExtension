"""Touch consent: an NPC who agrees to a hug, holding hands or other contact gets it without fuss.

Grabbing someone with HIGGS normally reads as an assault: PLANCK makes them protest and push away and
slowly turns them hostile, Living World has them pull away and speak up (a guard who saw it steps in),
body language steps back, and the memory and the gossip say "grabbed". All of that is right for a
stranger and wrong for someone who just said "come here, then" to a hug.

So an NPC can agree, in the conversation, to being touched in some ways (the *kinds* below):

- the ``AllowTouch`` action (``actions/touch_allow.json``), which the dialogue model calls when the NPC
  agrees; or, because the model often answers in words only, the player asking ("can I hug you?") and the
  NPC's next line agreeing ("Of course, come here."), see ``words.py``;
- the ``StopTouch`` action, or the NPC saying "stop that" / "hands off", takes it back.

The server sends the agreement to the game (``KEY_ALLOW`` / ``KEY_WITHDRAW`` on the NPC's line). There,
for as long as it lasts (the conversation and a few minutes after, or the minutes the NPC named):

- PLANCK leaves them alone (its own ignore list): no "stop that", no shove, no growing anger;
- no pull-away reaction, no guard stepping in, no body-language step back or folded arms;
- a hug opens their arms (an idle), and the player opening their arms (VR gesture ``open_arms``) to
  someone who agreed to a hug has them open theirs;
- a touch inside the agreement is reported as what it is ("Zach hugged Gerdur, as she had agreed"),
  and remembered and talked about as affection, not as being grabbed (``consent=<kind>`` on the grab record).

A hand on a sensitive place they did not agree to (``consent=breach``) ends the agreement on the spot and
is remembered as worse than an ordinary grab: they trusted the player and the player went further.

What is never read: gifts, gold, rank, how often the player asked. The NPC decides in character. Drink
never makes anyone more willing: an NPC who is drunk cannot agree to the intimate kinds (``INTIMATE``).
Children are left out entirely.

On with the Living World unless ``MANTELLA_LW_TOUCH_CONSENT=0``.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_TOUCH_CONSENT"
_OFF = {"0", "false", "no", "off"}

ALLOW = "mantella_lw_touch_allow"
WITHDRAW = "mantella_lw_touch_withdraw"

KEY_ON = "mantella_lw_touch_on"              # start reply: the game side on or off
KEY_ALLOW = "mantella_lw_touch_allow"        # NPC line: ["Name|hug,hands|minutes"] (0 minutes: the conversation)
KEY_WITHDRAW = "mantella_lw_touch_withdraw"  # NPC line: ["Name"]

# In the order a list of them is told. The game (MantellaRepository.psc, LwTouchCovers) has the same table.
KINDS = ("hug", "hands", "back", "neck", "face", "butt", "chest", "anywhere")
INTIMATE = frozenset({"butt", "chest", "anywhere"})   # never while drunk

# Areas (without "left"/"right") each kind covers. Areas are the ones the game sends on a grab
# (perception/records.py AREAS).
AREAS: dict[str, frozenset[str]] = {
    "hug": frozenset({"upper back", "lower back", "side", "hip", "shoulder", "upper arm", "stomach", "head",
                      "neck"}),   # arms round someone: hands drift to the neck or the side of the hip
    "hands": frozenset({"hand", "forearm"}),
    "back": frozenset({"upper back", "lower back", "shoulder"}),
    "neck": frozenset({"neck", "head"}),
    "face": frozenset({"face", "head"}),
    "butt": frozenset({"backside", "hip"}),
    "chest": frozenset({"chest"}),
    "anywhere": frozenset({"head", "face", "neck", "shoulder", "upper arm", "forearm", "hand", "chest",
                           "upper back", "stomach", "lower back", "side", "hips", "hip", "backside", "thigh",
                           "leg", "foot"}),
}

# How a kind reads in a sentence: "let Zach <phrase>", "Zach <past> Gerdur".
PHRASES = {
    "hug": ("hug them", "hugged"),
    "hands": ("hold their hand", "held hands with"),
    "back": ("rub their back", "rubbed the back of"),
    "neck": ("touch their neck", "touched the neck of"),
    "face": ("touch their face", "touched the face of"),
    "butt": ("touch their backside", "put a hand on the backside of"),
    "chest": ("touch their chest", "touched the chest of"),
    "anywhere": ("touch them as they like", "touched"),
}

ALIASES = {
    "hugs": "hug", "hugging": "hug", "embrace": "hug", "embracing": "hug", "cuddle": "hug", "cuddling": "hug",
    "hand": "hands", "hold_hands": "hands", "holding_hands": "hands", "hold hands": "hands",
    "holding hands": "hands", "hand holding": "hands",
    "back rub": "back", "back_rub": "back", "rub_back": "back", "rub back": "back", "massage": "back",
    "shoulders": "back",
    "nape": "neck",
    "cheek": "face", "cheeks": "face",
    "bum": "butt", "backside": "butt", "behind": "butt", "rear": "butt", "ass": "butt", "arse": "butt",
    "buttocks": "butt", "bottom": "butt",
    "breasts": "chest", "breast": "chest", "boobs": "chest", "bosom": "chest", "tits": "chest",
    "any": "anywhere", "everywhere": "anywhere", "all": "anywhere", "whole body": "anywhere",
}

DEFAULT_GRACE_MINUTES = 3     # the game keeps an agreement this long after the conversation ends
MAX_MINUTES = 120


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def normalize_kinds(value) -> list[str]:
    """The kinds named in ``value`` (a list, or a string such as "hug, hands"), known ones only, in KINDS order."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace(";", ",").split(",")
    found = set()
    for item in items:
        word = " ".join(str(item).strip().lower().replace("-", " ").split())
        word = ALIASES.get(word, ALIASES.get(word.replace(" ", "_"), word))
        if word in KINDS:
            found.add(word)
    return [k for k in KINDS if k in found]


def covers(kinds, area: str | None) -> str | None:
    """The first kind in ``kinds`` that covers a hand on ``area`` ("left shoulder"), or None.
    An area the game could not tell is covered by a hug (or anywhere), the commonest reason to be close."""
    kinds = normalize_kinds(kinds)
    if not area:
        return next((k for k in kinds if k in ("hug", "anywhere")), None)
    words = area.lower().split()
    base = " ".join(words[1:]) if words and words[0] in ("left", "right") else " ".join(words)
    return next((k for k in kinds if base in AREAS[k]), None)


def describe(kinds) -> str:
    """'hug them and hold their hand'."""
    phrases = [PHRASES[k][0] for k in normalize_kinds(kinds)]
    if not phrases:
        return ""
    return phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]

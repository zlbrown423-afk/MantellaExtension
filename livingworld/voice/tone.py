"""Tone of voice: the tone the player picked on the tone wheel, against how their voice actually sounded.

The player picks the tone they mean (friendly, polite, flirty, stern, threatening, sarcastic, pleading) on the
tone wheel in game. Each line they then speak is measured (livingworld/voice/live.py) and turned into plain
delivery cues against their own baseline: louder or quieter than usual, higher or lower pitch, lively or flat,
hesitant, rushed or slow. This module scores the cues against the picked tone, speechcraft style, and writes:

- a note for the NPCs ("Zachary is trying to sound threatening. How it came across: hesitant, higher pitch than
  usual. It sounds like a bluff."), so they react to a delivery that lands or falls flat, as well as to the words;
- a short line for the player's HUD ("[Tone] Threatening: unconvincing (hesitant, higher pitch)").

Only relative cues are used: the Steam Link mic has a noise gate and gain is not fixed, so a level only means
something against the player's own recent lines. Pure functions, no I/O; nothing here ever touches consent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

TONE_ENV = "MANTELLA_LW_TONE"                 # 0: no tone notes, no tone wheel handling
UNPICKED_ENV = "MANTELLA_LW_TONE_UNPICKED"    # 0: no note on a striking delivery when no tone is picked
HUD_ENV = "MANTELLA_LW_TONE_HUD"              # 0: no [Tone] line on the HUD
_OFF = {"0", "false", "no", "off"}

REQUEST_SET = "mantella_lw_tone_set"          # game -> server: the player picked a tone on the wheel
KEY_TONE = "mantella_lw_tone"                 # the tone's name ("Stern", "Neutral" clears)
KEY_SPEECH = "mantella_lw_tone_speech"        # the player's Speech skill, for speechcraft leniency

TONES = ("friendly", "polite", "flirty", "stern", "threatening", "sarcastic", "pleading")
NEUTRAL = "neutral"
_ALIASES = {"flirtatious": "flirty", "flirt": "flirty", "threat": "threatening", "menacing": "threatening",
            "intimidating": "threatening", "plead": "pleading", "begging": "pleading", "kind": "friendly",
            "warm": "friendly", "firm": "stern", "courteous": "polite", "sarcasm": "sarcastic", "none": NEUTRAL,
            "clear": NEUTRAL, "normal": NEUTRAL, "": NEUTRAL}

# Cue thresholds, in z (spreads of the player's own baseline). Voice Lab on the player's mic: deliberately loud
# or quiet moved loudness ~3.5 z, a deliberately high voice moved pitch ~3.6 z, natural variation stays under ~1.
NOTICE_Z = 1.5
STRONG_Z = 2.5


def enabled() -> bool:
    return os.environ.get(TONE_ENV, "").strip().lower() not in _OFF


def unpicked_enabled() -> bool:
    return os.environ.get(UNPICKED_ENV, "").strip().lower() not in _OFF


def hud_enabled() -> bool:
    return os.environ.get(HUD_ENV, "").strip().lower() not in _OFF


def normalize(name: str | None) -> str | None:
    """'Tone: Stern' / 'STERN' / 'menacing' -> 'stern' / 'threatening'; 'neutral' for a clear; None if unknown."""
    t = (name or "").strip().lower()
    if t.startswith("tone:"):
        t = t[5:].strip()
    t = _ALIASES.get(t, t)
    if t == NEUTRAL or t in TONES:
        return t
    return None


# --------------------------------------------------------------------------------------------------------------
# Cues: what the voice did, in words


@dataclass(frozen=True)
class Cue:
    key: str        # loud, shout, quiet, whisper, high, low, lively, flat, hesitant, fast, slow, steady
    words: str      # how an NPC would put it
    strength: float  # 1 = noticeable, 2 = strong


_WORDS = {
    "shout": "shouting", "loud": "louder than usual", "quiet": "quieter than usual", "whisper": "barely above a whisper",
    "high": "pitch higher than usual", "low": "pitch lower than usual", "lively": "lively, up-and-down pitch",
    "flat": "flat, monotone", "hesitant": "hesitant, with long pauses", "fast": "rushed", "slow": "slow and deliberate",
    "steady": "steady",
}
_SHORT = {"shout": "shouting", "loud": "loud", "quiet": "quiet", "whisper": "whisper", "high": "higher pitch",
          "low": "lower pitch", "lively": "lively", "flat": "flat", "hesitant": "hesitant", "fast": "rushed",
          "slow": "slow", "steady": "steady"}


def _cue(key: str, strength: float) -> Cue:
    return Cue(key, _WORDS[key], strength)


def cues_from(z: dict[str, float | None], longest_pause_s: float | None = None, speech_s: float | None = None) -> list[Cue]:
    """Delivery cues from z-scores against the player's baseline. Missing / unreliable measurements give no cue.

    Keys used: loudness_db, pitch_median_st, pitch_range_st, longest_pause_s, voiced_ratio, text_rate_syl_s."""
    out: list[Cue] = []
    loud = z.get("loudness_db")
    if loud is not None:
        if loud >= STRONG_Z + 0.5:
            out.append(_cue("shout", 2))
        elif loud >= NOTICE_Z:
            out.append(_cue("loud", 2 if loud >= STRONG_Z else 1))
        elif loud <= -(STRONG_Z + 0.5):
            out.append(_cue("whisper", 2))
        elif loud <= -NOTICE_Z:
            out.append(_cue("quiet", 2 if loud <= -STRONG_Z else 1))
    pitch = z.get("pitch_median_st")
    if pitch is not None:
        if pitch >= NOTICE_Z:
            out.append(_cue("high", 2 if pitch >= STRONG_Z else 1))
        elif pitch <= -NOTICE_Z:
            out.append(_cue("low", 2 if pitch <= -STRONG_Z else 1))
    rng = z.get("pitch_range_st")
    if rng is not None:
        if rng >= NOTICE_Z:
            out.append(_cue("lively", 2 if rng >= STRONG_Z else 1))
        elif rng <= -NOTICE_Z:
            out.append(_cue("flat", 2 if rng <= -STRONG_Z else 1))
    pause, voiced = z.get("longest_pause_s"), z.get("voiced_ratio")
    hesitant = 0.0
    if pause is not None and pause >= 2.0 and (longest_pause_s is None or longest_pause_s >= 0.45):
        hesitant = 2 if pause >= 3.5 else 1
    if voiced is not None and voiced <= -2.0:
        hesitant = max(hesitant, 2 if voiced <= -3.5 else 1)
    if hesitant:
        out.append(_cue("hesitant", hesitant))
    rate = z.get("text_rate_syl_s")
    if rate is not None:
        if rate >= NOTICE_Z:
            out.append(_cue("fast", 2 if rate >= STRONG_Z else 1))
        elif rate <= -NOTICE_Z:
            out.append(_cue("slow", 2 if rate <= -STRONG_Z else 1))
    if not hesitant and (pause is not None or voiced is not None) and (speech_s is None or speech_s >= 1.0):
        out.append(_cue("steady", 1))      # no faltering: a weak plus for firm tones
    return out


# --------------------------------------------------------------------------------------------------------------
# Judging a picked tone

# How each cue fits each tone: + sells it, - works against it. Per unit of cue strength (capped at 2).
FIT: dict[str, dict[str, float]] = {
    "friendly":    {"high": 0.7, "lively": 1.0, "shout": -1.5, "flat": -1.0, "low": -0.4, "whisper": -0.3},
    "polite":      {"steady": 0.5, "shout": -2.0, "loud": -0.8, "fast": -0.4},
    "flirty":      {"quiet": 0.8, "whisper": 0.6, "slow": 0.8, "lively": 0.5, "low": 0.4,
                    "shout": -2.0, "loud": -1.0, "fast": -0.8, "flat": -0.5},
    "stern":       {"loud": 0.8, "low": 0.8, "slow": 0.4, "steady": 0.5, "flat": 0.3,
                    "hesitant": -1.5, "high": -0.8, "quiet": -0.8, "whisper": -1.2, "lively": -0.4, "fast": -0.4},
    "threatening": {"loud": 0.7, "shout": 0.6, "low": 1.0, "slow": 0.8, "steady": 0.5,
                    "hesitant": -1.5, "high": -1.0, "fast": -0.8, "lively": -0.5, "whisper": 0.2},
    "sarcastic":   {"lively": 0.8, "slow": 0.5, "flat": 0.5, "hesitant": -0.4},
    "pleading":    {"high": 0.8, "quiet": 0.5, "lively": 0.4, "hesitant": 0.4,
                    "low": -0.8, "flat": -0.8, "steady": -0.2, "shout": -0.3},
}

# What a clash sounds like to a listener, by tone and the cue that does the most damage.
CLASH: dict[str, dict[str, str]] = {
    "friendly":    {"flat": "the friendliness sounds forced", "shout": "too loud to sound friendly",
                    "low": "it sounds cool rather than warm", "whisper": "it comes out too timid to sound warm"},
    "polite":      {"shout": "polite words, but the voice is close to shouting", "loud": "the voice is curt for polite words",
                    "fast": "it sounds hurried, not courteous"},
    "flirty":      {"shout": "far too loud to be flirtatious: it comes across as awkward",
                    "loud": "too loud for flirting: it comes across as awkward", "fast": "it is rushed and nervous, not smooth",
                    "flat": "it sounds flat, not playful"},
    "stern":       {"hesitant": "it falters: there is no authority in it", "high": "the voice rises: it sounds strained, not firm",
                    "quiet": "too quiet to carry authority", "whisper": "too quiet to carry authority",
                    "lively": "too sing-song to sound stern", "fast": "rushed: it sounds nervous, not firm"},
    "threatening": {"hesitant": "it falters: it sounds like a bluff", "high": "the voice rises: it sounds nervous, not menacing",
                    "fast": "rushed: it sounds nervous, not menacing", "lively": "too sing-song to be menacing"},
    "sarcastic":   {"hesitant": "the sarcasm falters"},
    "pleading":    {"low": "it sounds like a demand, not a plea", "flat": "it sounds flat: the plea does not sound heartfelt",
                    "steady": "it sounds composed, not desperate", "shout": "it sounds more like an outburst than a plea"},
}
LAND: dict[str, str] = {
    "friendly": "it sounds genuinely warm", "polite": "it sounds courteous", "flirty": "it sounds playful and inviting",
    "stern": "it carries authority", "threatening": "it sounds menacing", "sarcastic": "the sarcasm is plain to hear",
    "pleading": "it sounds heartfelt and desperate",
}

CONVINCING = "convincing"
FITS = "fits"
UNREAD = "unreadable"       # nothing in the voice either way: judge by the words
UNCONVINCING = "unconvincing"
CLASHES = "clashes"
NOT_MEASURED = "not measured"


@dataclass
class Judgement:
    tone: str
    verdict: str
    score: float = 0.0
    cues: list[Cue] = field(default_factory=list)
    reason: str = ""          # the listener's view: "it sounds like a bluff"
    why_unmeasured: str = ""


def speech_bonus(speech_skill: float | None) -> float:
    """Speechcraft: a practised speaker gets the benefit of the doubt on small slips (0 at 15, +0.8 at 100)."""
    if speech_skill is None:
        return 0.0
    return max(0.0, min(0.8, (float(speech_skill) - 15.0) / 85.0 * 0.8))


def judge(tone: str, cues: list[Cue] | None, speech_skill: float | None = None, why_unmeasured: str = "") -> Judgement:
    """Score the cues against the picked tone. ``cues`` None means the voice was not measured this line."""
    if cues is None:
        return Judgement(tone, NOT_MEASURED, why_unmeasured=why_unmeasured or "voice not measured")
    fit = FIT.get(tone, {})
    score, worst, worst_val = 0.0, None, 0.0
    for c in cues:
        v = fit.get(c.key, 0.0) * min(2.0, c.strength)
        score += v
        if v < worst_val:
            worst, worst_val = c, v
    raw = score
    score += speech_bonus(speech_skill)
    if raw <= -2.0 and score < -0.5:
        verdict = CLASHES
    elif score <= -0.5 or (worst_val <= -1.5 and score < 0.75):
        verdict = UNCONVINCING     # one clear give-away undermines it unless the rest really sells it
    elif score >= 1.5:
        verdict = CONVINCING
    elif score >= 0.75:
        verdict = FITS
    else:
        verdict = UNREAD
    if verdict in (CLASHES, UNCONVINCING) and worst is not None:
        reason = CLASH.get(tone, {}).get(worst.key) or f"it does not sound {tone}"
    elif verdict in (CONVINCING, FITS):
        reason = LAND.get(tone, "")
    else:
        reason = ""
    return Judgement(tone, verdict, round(score, 2), list(cues), reason)


def _cue_words(cues: list[Cue], skip_steady: bool = True) -> str:
    words = [c.words for c in cues if not (skip_steady and c.key == "steady")]
    return ", ".join(words)


def note_text(player: str, j: Judgement) -> str:
    """The note for the NPCs about one line spoken in a picked tone."""
    head = f"[Tone of voice] {player} is trying to sound {j.tone}."
    if j.verdict == NOT_MEASURED:
        return (f"{head} Their voice could not be judged this time, so go by the words: if the words do not fit "
                f"{j.tone}, it does not come across that way.")
    heard = _cue_words(j.cues) or "their ordinary voice, nothing unusual"
    body = f" How it came across: {heard}."
    if j.verdict == CONVINCING:
        tail = f" Convincing: {j.reason}. Let it land the way such a line would on you."
    elif j.verdict == FITS:
        tail = f" It fits: {j.reason}."
    elif j.verdict == UNREAD:
        tail = f" The voice gives nothing away either way: judge it by the words."
    elif j.verdict == UNCONVINCING:
        tail = f" Not convincing: {j.reason}. React to the delivery as much as the words."
    else:
        tail = (f" The voice works against it: {j.reason}. React to how it really came across, not to what "
                f"{player} meant it to be.")
    return head + body + tail


def unpicked_note(player: str, cues: list[Cue]) -> str | None:
    """With no tone picked: a note only when the delivery itself was striking (shouting, whispering, faltering)."""
    strong = [c for c in cues if c.strength >= 2 and c.key != "steady"]
    if not strong:
        return None
    return (f"[Tone of voice] {player}'s voice just now: {_cue_words(strong)}. People notice how something is said, "
            f"not only what is said.")


def hud_line(j: Judgement) -> str:
    """'[Tone] Threatening: unconvincing (hesitant, higher pitch)'"""
    name = j.tone.capitalize()
    if j.verdict == NOT_MEASURED:
        return f"[Tone] {name}: voice not measured"
    shorts = [_SHORT[c.key] for c in j.cues if c.key != "steady"]
    tail = f" ({', '.join(shorts)})" if shorts else ""
    return f"[Tone] {name}: {j.verdict}{tail}"

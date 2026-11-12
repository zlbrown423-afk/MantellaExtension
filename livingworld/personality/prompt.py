"""Prompt lines from a profile: only what fits the moment, kept apart from identity, mood and relationship lines.

Two entry points, both returning plain lines (the caller adds a header naming her):

* ``standing_lines`` - in every prompt for her (Living World recall block). Short: how she is in love in general
  (public tier only) and, if she is drawn to the player, how she acts on it. Nothing private.
* ``topic_lines`` - added to the player's line only when it turns to romance, intimacy, touch or her preferences.
  Then: what she is willing to tell the player (``discovery.gate``), behaviour guidance from personal-tier values,
  her boundaries in full, and what the player already knows of her. Private-tier values appear only when the gate
  allows private talk; otherwise the line says she keeps those to herself, without the values.

Every block ends with the same rule: these are her real, settled preferences; do not invent others; desire is never
a yes, and she decides each time (the consent rules in ``social/consent.py`` and touch consent stay the gate).
"""
from __future__ import annotations

import re

from livingworld.personality import discovery, schema
from livingworld.personality.profile import Profile

TOPIC_WORDS = {
    # only clear signs: "lovely day" or "my partner in trade" must not turn a talk romantic
    "romance": ("love you", "love me", "in love", "court you", "courting", "marry me", "marry you", "beautiful", "handsome",
                "flirt", "flirting", "attracted", "fancy you", "sweetheart", "darling", "romance", "romantic",
                "feelings for", "kiss", "are you married", "seeing anyone", "your husband", "a husband"),
    "intimacy": ("bed", "bedroom", "sleep with", "make love", "night together", "intimate", "undress", "naked",
                 "desire", "want you", "alone together", "room together"),
    "touch": ("hug", "hold hands", "hold your hand", "touch", "cuddle", "embrace", "kiss"),
    "preferences": ("your type", "what do you like", "what kind of man", "what kind of woman", "what draws you",
                    "what do you look for", "what you like", "turn you off", "can't stand", "your boundaries",
                    "comfortable with"),
}
RULE = ("These are her real, settled preferences: stay true to them and never invent other likes, dislikes or "
        "limits. Desire is never a yes; she decides each time, and her boundaries always hold.")


def topics(text: str) -> set[str]:
    low = " " + re.sub(r"[^a-z' ]+", " ", (text or "").lower()) + " "
    return {t for t, words in TOPIC_WORDS.items() if any(f" {w} " in low for w in words)}


def _list(values: list[str]) -> str:
    if not values:
        return ""
    return values[0] if len(values) == 1 else ", ".join(values[:-1]) + " and " + values[-1]


def _words(key: str, value) -> str:
    attr = schema.BY_KEY[key]
    if attr.kind == "list":
        return _list([attr.words.get(v, v) for v in value])
    if attr.kind == "choice":
        return attr.words.get(value, value)
    return f"{schema.describe_number(value)} {attr.label}"


def _manner(p: Profile) -> str | None:
    parts = []
    if "relationship_style" in p.values:
        parts.append(f"in love she looks for {_words('relationship_style', p.values['relationship_style'])}")
    rom = p.values.get("romantic_inclination")
    if rom is not None and (rom >= 62 or rom <= 38):
        parts.append("she is romantic at heart" if rom >= 62 else "she has little patience for romance")
    soc = p.values.get("social_confidence")
    if soc is not None and (soc >= 62 or soc <= 38):
        parts.append("at ease with people" if soc >= 62 else "unsure of herself with people")
    return ("; ".join(parts)[:1].upper() + "; ".join(parts)[1:] + ".") if parts else None


def _drawn(db, p: Profile, player_id: int, player_sex: str | None, player_name: str) -> str | None:
    row = db.execute("SELECT attraction FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (p.npc_id, player_id)).fetchone()
    attraction = row[0] if row else 0.0
    sexes = p.values.get("attracted_to_sex")
    if attraction < 0.3 or (sexes is not None and player_sex and player_sex not in sexes):
        return None
    taken = db.execute("SELECT 1 FROM v_romance_bond WHERE (a_id = ? OR b_id = ?) AND stage IN"
                       " ('courting','dating','partners','betrothed','married') LIMIT 1",
                       (p.npc_id, p.npc_id)).fetchone()
    if taken and p.values.get("exclusivity", 50) >= 60:
        return f"She finds {player_name} attractive, but she is spoken for and will not act on it."
    initiation = p.values.get("initiation", 50)
    confidence = p.values.get("sexual_confidence", 50)
    if initiation >= 62 and confidence >= 50:
        how = "and may well make the first move when the moment feels right"
    elif initiation <= 38:
        how = "but she hints at most and waits to be asked"
    else:
        how = "and lets it show now and then"
    return f"She is drawn to {player_name}, {how}."


def standing_lines(db, p: Profile, player_id: int | None, player_name: str, player_sex: str | None = None) -> list[str]:
    if not p.active:
        return []
    lines = [x for x in (_manner(p),) if x]
    if player_id is not None:
        drawn = _drawn(db, p, player_id, player_sex, player_name)
        if drawn:
            lines.append(drawn)
    if lines:
        lines.append(RULE)
    return lines


def topic_lines(db, p: Profile, player_id: int, player_name: str, text: str, others_listening: bool = False,
                player_sex: str | None = None) -> list[str]:
    """Lines for the player's romantic / intimate / touch / preference line. [] when the line is about other things."""
    found = topics(text)
    if not p.active or not found:
        return []
    g = discovery.gate(db, p, player_id, others_listening)
    share = set(discovery.shareable(p, g))
    lines: list[str] = []
    guide = []
    for key in ("intimacy_pace", "relationship_dynamic"):
        if key in p.values:
            guide.append(f"{schema.BY_KEY[key].label}: {_words(key, p.values[key])}")
    for key in ("attracted_to", "dislikes"):
        if p.values.get(key):
            guide.append(f"{schema.BY_KEY[key].label}: {_words(key, p.values[key])}")
    if guide:
        lines.append("How she is (let it show in what she does; " + (
            "she would tell them if asked" if "personal" in g.tiers else "she does not explain it to them yet")
            + "): " + "; ".join(guide) + ".")
    private_keys = [a.key for a in schema.ATTRIBUTES if a.tier == "private" and a.key in p.values]
    if "private" in g.tiers:
        told = [f"{schema.BY_KEY[k].label}: {_words(k, p.values[k])}" for k in private_keys if k in share]
        if told:
            lines.append(f"Private, but she trusts {player_name} enough to speak of it if it comes up: "
                         + "; ".join(told) + ".")
    elif private_keys:
        lines.append(f"She keeps her desires and her intimate life private from {player_name} for now ({g.why}); "
                     f"she does not discuss them, though they may colour her manner.")
    if found & {"intimacy", "touch", "romance"}:
        bound = []
        if "intimacy_boundary" in p.values:
            bound.append(_words("intimacy_boundary", p.values["intimacy_boundary"]))
        bound += [schema.BOUNDARIES[b] for b in p.values.get("boundaries", [])]
        if bound:
            lines.append("Her firm limits, which she states plainly if pushed and which no mood or desire changes: "
                         + "; ".join(bound) + ".")
    if "romance" in found:
        drawn = _drawn(db, p, player_id, player_sex, player_name)
        if drawn:
            lines.append(drawn)
    knows = discovery.known(db, p.npc_id, player_id)
    if knows:
        said = [_known_words(p, item, knows[item]) for item in sorted(knows)]
        lines.append(f"{player_name} already knows this about her (from her or what they saw): " + "; ".join(said) + ".")
    if lines:
        lines.append(RULE)
    return lines


def _known_words(p: Profile, item: str, state: str) -> str:
    key, _, value = item.partition(":")
    attr = schema.BY_KEY.get(key)
    if attr is None:
        return item
    if value:
        said = f"{attr.label}: {attr.words.get(value, value)}"
    elif key in p.values:
        said = f"{attr.label}: {_words(key, p.values[key]) if attr.kind != 'number' else schema.describe_number(p.values[key])}"
    else:
        said = attr.label
    return said + (" (only suspected)" if state == "suspected" else "")

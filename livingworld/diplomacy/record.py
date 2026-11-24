"""Turning the scribe's ``diplomacy`` list into records, after checking every item against the transcript.

What the model says is only a proposal. Each item must point at a real line said by the person the model names;
the line's own words then set the modality (a question is a question, "if" makes it conditional, "what if" makes
it hypothetical, hearsay about someone else is a rumour); and only a person can speak for themselves, so a
promise, a position or an invitation answer passed on by someone else is stored as a report and changes nothing
but what the listeners have heard.

Then, all deterministic:
- the statement, once (unique per line, kind, issue and person), on the conversation's own timeline;
- awareness for everyone present at that line, the player included: said / witnessed, or told for a report;
- the speaker's position, only from their own asserted words: private words move their personal layer, public
  words move the official layer of a faction head; public words never rewrite private belief;
- a gossip claim for a first-hand public statement, held by the people who heard it, so the existing gossip can
  carry it through the speaker's court;
- promises, fulfilments, retractions (``commitments.py``), invitations and answers (``meetings.py``);
- double-dealing: a listener who now knows the same person said opposite things on one issue to different
  people loses a little honesty standing for them, once per pair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from livingworld.diplomacy import DIMENSIONS, ISSUES, KINDS, MAX_ITEMS, MODALITIES, RESPONSES
from livingworld.diplomacy import commitments as C
from livingworld.diplomacy import common as K
from livingworld.diplomacy import figures as F
from livingworld.diplomacy import meetings as M
from livingworld.store.minors import is_child

_HYPOTHETICAL = re.compile(r"\b(what if|suppose|supposing|imagine|hypothetical\w*|in theory|say that|let us say|"
                           r"let's say)\b", re.I)
_CONDITIONAL = re.compile(r"\b(if|unless|provided|providing|on condition|only when|as long as|so long as)\b", re.I)
_RUMOR = re.compile(r"\b(i heard|i've heard|i have heard|they say|people say|word is|rumou?rs?|rumou?red|gossip|"
                    r"so i'm told|so i am told|apparently)\b", re.I)
_ISSUE_ALIASES = {"truce": "ceasefire", "armistice": "ceasefire", "cease_fire": "ceasefire", "cease-fire": "ceasefire",
                  "peace": "ceasefire", "white_gold_concordat": "concordat", "white-gold concordat": "concordat",
                  "treaty": "concordat", "talos": "concordat", "council": "meeting", "summit": "meeting",
                  "conference": "meeting", "peace_conference": "meeting", "parley": "meeting", "talks": "negotiation",
                  "negotiations": "negotiation", "imperials": "empire", "imperial": "empire", "legion": "empire",
                  "stormcloak": "stormcloaks", "rebels": "stormcloaks", "dominion": "thalmor",
                  "prisoner": "prisoners", "prisoner_exchange": "prisoners", "land": "territory", "holds": "territory"}
POSITIONAL = ("position", "support", "opposition")
CLAIMABLE = ("proposal", "position", "support", "opposition", "condition", "commitment", "refusal",
             "invitation_response", "disagreement", "agreement", "goodwill")
DOUBLE_DEALING_GAP = 1.0
DOUBLE_DEALING_DAYS = 30.0
DOUBLE_DEALING_HONESTY = -0.15


@dataclass
class RecordResult:
    statements: int = 0
    duplicates: int = 0
    positions: int = 0
    claims: int = 0
    changes: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip().strip("[]"))
    except (TypeError, ValueError):
        return None


def _word(value: Any) -> str:
    return re.sub(r"[^a-z_ -]+", "", str(value or "").strip().lower()).replace(" ", "_").replace("-", "_")


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def norm_issue(value: Any) -> str:
    w = _word(value)
    w = _ISSUE_ALIASES.get(w, _ISSUE_ALIASES.get(w.replace("_", " "), w))
    return w if w in ISSUES else "other"


def validate(db, item: dict, lines: list, present: dict[int, str]) -> dict | str:
    """One checked statement (a dict ready for ``insert``) or the reason it was dropped."""
    if not isinstance(item, dict):
        return f"not an object: {item!r}"
    by_seq = {l.seq: l for l in lines}
    claimed = _as_int(item.get("line"))
    said_by = F.resolve(db, str(item.get("said_by") or ""), present)
    if said_by is None:
        return f"unknown speaker: {item}"
    line = by_seq.get(claimed) if claimed is not None else None
    if line is None or line.speaker_id != said_by:
        # the model miscounted: accept a nearby line by that speaker only if it shares a real word with the summary
        words = {w for w in re.findall(r"[a-z']{4,}", str(item.get("summary") or "").lower())}
        near = [l for l in lines if l.speaker_id == said_by and claimed is not None and abs(l.seq - claimed) <= 2
                and words & set(re.findall(r"[a-z']{4,}", (l.text or "").lower()))]
        line = min(near, key=lambda l: abs(l.seq - claimed)) if near else None
    if line is None:
        return f"not said on that line by {item.get('said_by')}: {item}"
    kind = _word(item.get("kind"))
    if kind not in KINDS:
        return f"unknown kind: {item}"
    issue = norm_issue(item.get("issue"))
    summary = _text(item.get("summary"), 160)
    if not summary:
        return f"no summary: {item}"
    whose_name = _text(item.get("whose"), 60)
    whose = F.resolve(db, whose_name, present) if whose_name else said_by
    about_name = None
    if whose is None:   # someone Living World has never met: kept only as what the speaker said about them
        whose, about_name = said_by, whose_name
    to_name = _text(item.get("to"), 60)
    target = F.resolve(db, to_name, present) if to_name else None
    if target == whose:
        target = None
    text = line.text or ""
    modality = _word(item.get("modality")) or "asserted"
    modality = modality if modality in MODALITIES else "asserted"
    reported = whose != said_by or about_name is not None
    if reported:
        modality = "rumor" if (modality == "rumor" or _RUMOR.search(text)) else "reported"
        if kind != "invitation":   # carrying an invitation is still an invitation (meetings.py checks it)
            kind = "report"
    else:
        if modality in ("reported", "rumor"):
            modality = "asserted"   # your own view is never hearsay
        if _HYPOTHETICAL.search(text):
            modality = "hypothetical"
        elif text.rstrip().endswith("?") and kind not in ("invitation", "request", "proposal"):
            modality = "question"
        elif modality == "asserted" and _CONDITIONAL.search(text):
            modality = "conditional"
    stance = item.get("stance")
    try:
        stance = None if stance in (None, "", "null") else max(-1.0, min(1.0, float(stance)))
    except (TypeError, ValueError):
        stance = None
    if kind == "support" or (kind == "report" and _word(item.get("kind")) == "support"):
        stance = abs(stance) if stance is not None else 0.7
    elif kind == "opposition" or (kind == "report" and _word(item.get("kind")) in ("opposition", "refusal")):
        stance = -abs(stance) if stance is not None else -0.7
    response = _word(item.get("response")) or None
    if kind == "invitation_response":
        if response not in RESPONSES:
            return f"invitation answer without a clear response: {item}"
    else:
        response = response if response in RESPONSES else None
    try:
        due = float(item.get("due_days")) if item.get("due_days") not in (None, "", "null") else None
        due = due if due is not None and 0 < due <= 365 else None
    except (TypeError, ValueError):
        due = None
    holders = K.present_at(db, line_conv(db, line), line.seq)
    others = [h for h in holders if h != said_by]
    return {"line": line, "speaker_id": said_by, "subject_id": whose, "target_id": target, "about_name": about_name,
            "kind": kind, "issue": issue, "stance": stance, "modality": modality, "response": response,
            "summary": summary, "quote": _text(text, 240), "condition": _text(item.get("condition"), 200) or None,
            "venue": _text(item.get("venue"), 60) or None, "due_days": due, "holders": holders,
            "audience": "public" if len(others) >= 2 else "private"}


def line_conv(db, line) -> int:
    return db.execute("SELECT conv_id FROM utterance WHERE utt_id = ?", (line.utt_id,)).fetchone()[0]


def record(db, conv_id: int, items: list[dict], lines: list, present: dict[int, str], location: str | None,
           game_time: float) -> RecordResult:
    """Record the checked statements of one conversation. Safe to run twice (a job retry adds nothing)."""
    result = RecordResult()
    timeline = K.conversation_timeline(db, conv_id)
    meeting_id = M.meeting_of_conversation(db, conv_id)
    kept = 0
    for item in items:
        if kept >= MAX_ITEMS:
            break
        v = validate(db, item, lines, present)
        if isinstance(v, str):
            result.rejected.append(v)
            continue
        if is_child(db, v["speaker_id"]) or is_child(db, v["subject_id"]):
            continue
        stmt = insert(db, timeline, conv_id, v, location, game_time, meeting_id)
        if stmt is None:
            result.duplicates += 1
            continue
        kept += 1
        result.statements += 1
        _awareness(db, timeline, stmt, v["holders"])
        result.positions += _position(db, timeline, stmt)
        if _claim(db, timeline, stmt, v["holders"], location):
            result.claims += 1
        changed = None
        if stmt["kind"] == "commitment" and _first_hand(stmt) and stmt["modality"] in ("asserted", "conditional"):
            if C.make(db, timeline, stmt["stmt_id"], stmt["subject_id"], stmt["target_id"], stmt["issue"],
                      stmt["summary"], stmt["condition"], stmt["due_days"], stmt["audience"], game_time, meeting_id):
                changed = "promise"
        elif _first_hand(stmt) and stmt["kind"] in ("fulfilment", "retraction"):
            changed = C.on_statement(db, timeline, stmt)
        meeting_change = M.on_statement(db, timeline, stmt, v["holders"], v["line"].text)
        for part in (changed, meeting_change):
            if part:
                result.changes.append(part)
        for holder in v["holders"]:
            double_dealing(db, timeline, holder, stmt)
    return result


def _first_hand(stmt: dict) -> bool:
    return stmt["subject_id"] == stmt["speaker_id"] and stmt["about_name"] is None


def insert(db, timeline: int, conv_id: int, v: dict, location: str | None, game_time: float,
           meeting_id: int | None) -> dict | None:
    import json
    from livingworld.diplomacy import figures as Fg
    fig = Fg.figure_of(db, v["subject_id"])
    factions = sorted({f for f in ([fig.faction] if fig else []) + _factions_named(v["quote"]) if f})
    if K.is_player(db, v["subject_id"]):
        factions = sorted(set(factions) | {"player"})
    cur = db.execute(
        "INSERT OR IGNORE INTO dip_statement (timeline_id, conv_id, utt_id, speaker_id, subject_id, target_id,"
        " about_name, kind, issue, factions_json, stance, modality, audience, response, summary, quote, condition,"
        " venue, due_days, confidence, meeting_id, game_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (timeline, conv_id, v["line"].utt_id, v["speaker_id"], v["subject_id"], v["target_id"], v["about_name"],
         v["kind"], v["issue"], json.dumps(factions), v["stance"], v["modality"], v["audience"], v["response"],
         v["summary"], v["quote"], v["condition"], v["venue"], v["due_days"],
         0.9 if v["modality"] in ("asserted", "conditional") else 0.6, meeting_id, game_time))
    if cur.rowcount == 0:
        return None
    return dict(db.execute("SELECT * FROM dip_statement WHERE stmt_id = ?", (cur.lastrowid,)).fetchone())


_FACTION_WORDS = {"empire": r"\b(empire|imperials?|legion|tullius)\b", "stormcloaks": r"\b(stormcloaks?|ulfric|rebels?)\b",
                  "thalmor": r"\b(thalmor|dominion|elenwen|justiciars?)\b", "whiterun": r"\b(whiterun|balgruuf)\b"}


def _factions_named(text: str) -> list[str]:
    return [f for f, pat in _FACTION_WORDS.items() if re.search(pat, text or "", re.I)]


def _awareness(db, timeline: int, stmt: dict, holders: list[int]) -> None:
    reported = not _first_hand(stmt)
    for holder in holders:
        if holder == stmt["speaker_id"]:
            origin, teller, conf = "said", None, 1.0
        elif reported:
            origin, teller = "told", stmt["speaker_id"]
            conf = 0.4 if stmt["modality"] == "rumor" else 0.6
        else:
            origin, teller, conf = "witnessed", None, 1.0
        db.execute("INSERT OR IGNORE INTO dip_awareness (timeline_id, holder_id, stmt_id, origin, told_by_id, confidence,"
                   " learned_gt) VALUES (?,?,?,?,?,?,?)", (timeline, holder, stmt["stmt_id"], origin, teller, conf,
                                                           stmt["game_time"]))


def _position(db, timeline: int, stmt: dict) -> int:
    """Only the person's own asserted words on a dimension move a position (see module doc)."""
    if (not _first_hand(stmt) or stmt["kind"] not in POSITIONAL or stmt["modality"] != "asserted"
            or stmt["issue"] not in DIMENSIONS or stmt["stance"] is None or K.is_player(db, stmt["subject_id"])):
        return 0
    fig = F.figure_of(db, stmt["subject_id"])
    if stmt["audience"] == "private":
        layer, why = "personal", "said in private"
    elif fig is not None and fig.speaks_for:
        layer, why = "official", f"declared in front of others, as head of {fig.speaks_for}"
    else:
        return 0
    db.execute("INSERT INTO political_position (timeline_id, npc_id, dimension, layer, value, confidence, source,"
               " stmt_id, reason, game_time) VALUES (?,?,?,?,?,0.6,'statement',?,?,?)",
               (timeline, stmt["subject_id"], stmt["issue"], layer, stmt["stance"], stmt["stmt_id"], why,
                stmt["game_time"]))
    return 1


def _claim(db, timeline: int, stmt: dict, holders: list[int], location: str | None) -> bool:
    """First-hand public statements become a claim the people who heard it can pass on (existing gossip)."""
    from livingworld.social import claims
    if (not _first_hand(stmt) or stmt["audience"] != "public" or stmt["kind"] not in CLAIMABLE
            or stmt["modality"] not in ("asserted", "conditional")):
        return False
    event_id = K.event(db, timeline, "diplomacy.statement", {"key": f"stmt:{stmt['stmt_id']}",
                                                             "stmt_id": stmt["stmt_id"], "conv_id": stmt["conv_id"]},
                       stmt["game_time"], location)
    fig = F.figure_of(db, stmt["subject_id"])
    magnitude = 0.6 if fig is not None else 0.45
    claim_id = db.execute(
        "INSERT INTO claim (timeline_id, subject_id, act, object_id, detail, valence, magnitude, location, game_time,"
        " truth_event_id, sensitivity) VALUES (?,?, 'declared', NULL, ?, 0, ?, ?, ?, ?, 'public')",
        (timeline, stmt["subject_id"], " " + stmt["summary"], magnitude, location, stmt["game_time"], event_id)).lastrowid
    db.execute("UPDATE dip_statement SET claim_id = ? WHERE stmt_id = ?", (claim_id, stmt["stmt_id"]))
    for holder in holders:
        if K.is_player(db, holder) or is_child(db, holder):
            continue
        origin = "self" if holder == stmt["subject_id"] else "witnessed"
        claims._hold(db, timeline, holder, claim_id, stmt["subject_id"], origin, None, None, event_id,
                     stmt["utt_id"], magnitude, 1.0, "yes", stmt["game_time"])
    return True


def sync_heard(db) -> int:
    """People who picked up a diplomatic claim through gossip now hold the statement too, as a rumour, with how
    much they believed it as the confidence. Idempotent."""
    timeline = K.active_timeline(db)
    rows = db.execute(
        "SELECT k.holder_id, d.stmt_id, k.told_by_id, k.believed, k.learned_gt FROM v_claim_knowledge k"
        " JOIN dip_statement d ON d.claim_id = k.claim_id"
        " WHERE k.origin = 'told' AND k.told_by_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM dip_awareness a WHERE a.holder_id = k.holder_id AND a.stmt_id = d.stmt_id)"
    ).fetchall()
    for holder, stmt_id, teller, believed, gt in rows:
        db.execute("INSERT OR IGNORE INTO dip_awareness (timeline_id, holder_id, stmt_id, origin, told_by_id, confidence,"
                   " learned_gt) VALUES (?,?,?, 'rumor', ?, ?, ?)",
                   (timeline, holder, stmt_id, teller, max(0.0, min(1.0, believed or 0.3)), gt))
        stmt = dict(db.execute("SELECT * FROM dip_statement WHERE stmt_id = ?", (stmt_id,)).fetchone())
        double_dealing(db, timeline, holder, stmt)
    return len(rows)


def double_dealing(db, timeline: int, holder: int, stmt: dict) -> bool:
    """``holder`` now knows ``stmt``; if they also know the same person took the opposite stance on the same issue
    to someone else, within a month, they think a little less of that person's honesty (once per pair)."""
    subject = stmt["subject_id"]
    if (holder == subject or stmt["stance"] is None or stmt["about_name"] is not None or stmt["issue"] == "other"
            or stmt["modality"] in ("hypothetical", "question")):
        return False
    for other in db.execute(
            "SELECT d.* FROM v_dip_awareness a JOIN v_dip_statement d ON d.stmt_id = a.stmt_id"
            " WHERE a.holder_id = ? AND d.subject_id = ? AND d.issue = ? AND d.stance IS NOT NULL AND d.stmt_id <> ?"
            " AND d.about_name IS NULL AND d.modality NOT IN ('hypothetical','question') AND d.conv_id <> ?"
            " AND abs(d.game_time - ?) <= ?",
            (holder, subject, stmt["issue"], stmt["stmt_id"], stmt["conv_id"], stmt["game_time"],
             DOUBLE_DEALING_DAYS)).fetchall():
        if abs(other["stance"] - stmt["stance"]) <= DOUBLE_DEALING_GAP:
            continue
        a, b = sorted((other["stmt_id"], stmt["stmt_id"]))
        reason = f"diplomacy: double-dealing {a}/{b}"
        if db.execute("SELECT 1 FROM standing_change WHERE holder_id = ? AND subject_id = ? AND reason = ?",
                      (holder, subject, reason)).fetchone():
            continue
        gt = max(other["game_time"], stmt["game_time"])
        if K.standing(db, timeline, holder, subject, "honesty", DOUBLE_DEALING_HONESTY, reason, gt):
            who = K.name_of(db, subject)
            K.remember(db, timeline, holder,
                       f"{who} said one thing about {stmt['issue']} to one side and the opposite to the other: that "
                       f"{who} {other['summary']}, and also that {who} {stmt['summary']}.", [subject], gt, 0.6, -0.3,
                       "inferred")
            return True
    return False

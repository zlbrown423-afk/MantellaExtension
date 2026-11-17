"""AI cost logging: one record per LLM call, with tokens and cost (``MANTELLA_LW_COST_LOG``, on by default).

Mantella's LLM clients (``src/llm/client_base.py``) report every call here when it finishes: dialogue
replies (streaming), tool-calling and vision calls, Mantella's conversation summaries, and the Living World
background calls (memory extraction, diaries). Each record says:

* ``task``: ``dialogue``, ``tools``, ``vision``, ``summary``, ``extract``, ``diary`` or ``request``
  (a one-shot call nothing named).
* ``lane``: ``foreground`` (the player waits for it: dialogue, tools, vision) or ``background``.
* ``model``, ``prompt_tokens``, ``completion_tokens``, ``cached_tokens`` (prompt tokens served from the
  provider's cache), ``estimated`` (1 when the service sent no usage and the tokens were counted locally).
* ``cost_usd`` and ``cost_source``: ``reported`` (the service said what it charged; OpenRouter does when
  asked), ``free`` (a ``:free`` model), ``local`` (a model on this PC), ``priced`` (tokens times the price
  table below or ``MANTELLA_LW_PRICES``), or ``unknown`` (cost stored as 0; the tokens are still there).

Where records go: the Living World store sets a sink (``set_sink``) that writes them to the world
database's ``llm_request`` table (migration 024 added ``lane``, ``cached_tokens``, ``estimated`` and
``cost_source``). Calls made before a world is open wait in a small buffer. A dialogue turn collects its
records (``collect``) so the one ``llm_request`` row the turn writes carries its tokens and cost.

Cost is real money, so the summary (``python -m livingworld.perf.costs``) counts every row, whatever
timeline (save) it was made on. Nothing here changes what is sent to the model except asking
OpenAI and OpenRouter to include usage (``request_extras``).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger("Mantella")

ENV_VAR = "MANTELLA_LW_COST_LOG"
PRICES_ENV = "MANTELLA_LW_PRICES"
_OFF = {"0", "false", "no", "off"}

FOREGROUND_TASKS = frozenset({"dialogue", "tools", "vision"})
TASKS = frozenset({"dialogue", "tools", "vision", "summary", "extract", "diary", "request"})
PENDING_MAX = 500

OPENAI_URL = "https://api.openai.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# US dollars per million tokens (input, output), list prices on 2026-11-08 (see the project's cost notes).
# Used only when the service does not report the cost itself. Model ids are matched without the provider
# prefix too ("openai/gpt-4o-mini" and "gpt-4o-mini"). Add or correct prices with MANTELLA_LW_PRICES,
# a JSON file {"model id": [input, output], ...}.
PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "deepseek/deepseek-v4.1-flash": (0.15, 0.60),
    "minimax/minimax-m3.1-flash": (0.075, 0.30),
}


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() not in _OFF


def lane_of(task: str) -> str:
    return "foreground" if task in FOREGROUND_TASKS else "background"


@dataclass
class UsageRecord:
    task: str
    model: str
    lane: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    estimated: bool = False
    cost_usd: float = 0.0
    cost_source: str = "unknown"
    latency_ms: int | None = None
    status: str = "ok"
    error: str | None = None
    npc_id: int | None = None
    conv_id: int | None = None
    job_id: int | None = None
    at: float = field(default_factory=time.time)
    store: Any = field(default=None, repr=False, compare=False)   # the world to write to, when known

    def __post_init__(self) -> None:
        if not self.lane:
            self.lane = lane_of(self.task)
        if self.status not in ("ok", "error", "rate_limited", "fallback"):
            self.status = "error"

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("store", None)
        return d


# ---- pricing ----
_extra_prices: dict[str, tuple[float, float]] | None = None


def _load_extra_prices() -> dict[str, tuple[float, float]]:
    global _extra_prices
    if _extra_prices is None:
        _extra_prices = {}
        path = os.environ.get(PRICES_ENV, "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    for model, pair in json.load(f).items():
                        _extra_prices[str(model).lower()] = (float(pair[0]), float(pair[1]))
            except Exception as e:
                logger.warning(f"Living World: could not read the price file {path} ({e}); using built-in prices")
    return _extra_prices


def price_of(model: str) -> tuple[float, float] | None:
    """(input, output) dollars per million tokens, or None when the model is not in the tables."""
    m = (model or "").strip().lower()
    if not m:
        return None
    tables = (_load_extra_prices(), PRICES)
    for table in tables:
        if m in table:
            return table[m]
    bare = m.split("/", 1)[-1]
    for table in tables:
        for key, pair in table.items():
            if key.split("/", 1)[-1] == bare:
                return pair
    return None


def is_free_model(model: str) -> bool:
    return (model or "").strip().lower().endswith(":free")


def cost_for(model: str, prompt_tokens: int | None, completion_tokens: int | None,
             reported: float | None = None, local: bool = False) -> tuple[float, str]:
    """(dollars, source) for one call."""
    if reported is not None:
        try:
            return max(0.0, float(reported)), "reported"
        except (TypeError, ValueError):
            pass
    if local:
        return 0.0, "local"
    if is_free_model(model):
        return 0.0, "free"
    pair = price_of(model)
    if pair is not None and (prompt_tokens is not None or completion_tokens is not None):
        return ((prompt_tokens or 0) * pair[0] + (completion_tokens or 0) * pair[1]) / 1_000_000, "priced"
    return 0.0, "unknown"


def usage_numbers(usage: Any) -> dict[str, Any]:
    """Tokens and reported cost from an OpenAI-style ``usage`` object or dict (None for what is missing)."""
    if usage is None:
        return {}

    def get(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        value = getattr(obj, key, None)
        if value is None:
            extra = getattr(obj, "model_extra", None)
            if isinstance(extra, dict):
                value = extra.get(key)
        return value

    def as_int(v: Any) -> int | None:
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None

    cost = get(usage, "cost")
    try:
        cost = None if cost is None else float(cost)
    except (TypeError, ValueError):
        cost = None
    return {
        "prompt_tokens": as_int(get(usage, "prompt_tokens")),
        "completion_tokens": as_int(get(usage, "completion_tokens")),
        "cached_tokens": as_int(get(get(usage, "prompt_tokens_details"), "cached_tokens")),
        "reported_cost": cost,
    }


def request_extras(base_url: str, streaming: bool) -> dict[str, Any]:
    """Request arguments that make the service send usage back; empty for services not known to take them
    (a local server may refuse an unknown field)."""
    if not enabled():
        return {}
    url = (base_url or "").rstrip("/")
    extras: dict[str, Any] = {}
    if url in (OPENAI_URL, OPENROUTER_URL) and streaming:
        extras["stream_options"] = {"include_usage": True}
    if url == OPENROUTER_URL:
        extras["extra_body"] = {"usage": {"include": True}}   # OpenRouter then reports what it charged
    return extras


def merge_extras(params: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
    """``params`` plus ``extras``; what the player configured wins, and extra_body dicts are merged."""
    merged = dict(params)
    for key, value in extras.items():
        if key == "extra_body" and isinstance(merged.get(key), dict):
            merged[key] = {**value, **merged[key]}
        elif key not in merged:
            merged[key] = value
    return merged


# ---- attribution and collection ----
_attribution: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("lw_llm_attr", default=None)
_collector: contextvars.ContextVar[list[UsageRecord] | None] = contextvars.ContextVar("lw_llm_collect",
                                                                                      default=None)


@contextmanager
def attribute(**fields: Any) -> Iterator[None]:
    """Calls made inside carry these fields (task, npc_id, conv_id, job_id, store), unless they set their own.
    Inner blocks add to outer ones."""
    merged = dict(_attribution.get() or {})
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _attribution.set(merged)
    try:
        yield
    finally:
        _attribution.reset(token)


@contextmanager
def collect() -> Iterator[list[UsageRecord]]:
    """Records made inside are handed to the caller (the list) instead of the sink."""
    records: list[UsageRecord] = []
    token = _collector.set(records)
    try:
        yield records
    finally:
        _collector.reset(token)


# ---- the ledger ----
@dataclass
class Totals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    unknown_cost_calls: int = 0

    def add(self, rec: UsageRecord) -> None:
        self.calls += 1
        self.prompt_tokens += rec.prompt_tokens or 0
        self.completion_tokens += rec.completion_tokens or 0
        self.cost_usd += rec.cost_usd or 0.0
        if rec.cost_source == "unknown":
            self.unknown_cost_calls += 1


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sink: Callable[[list[UsageRecord]], None] | None = None
        self._pending: list[UsageRecord] = []
        self.session: dict[str, Totals] = {"foreground": Totals(), "background": Totals()}
        self.dropped = 0

    def set_sink(self, sink: Callable[[list[UsageRecord]], None] | None) -> None:
        """Where finished records go; records that waited for a sink are handed over now."""
        with self._lock:
            self._sink = sink
            waiting, self._pending = self._pending, []
        if sink is not None and waiting:
            self._deliver(sink, waiting)

    def _deliver(self, sink: Callable[[list[UsageRecord]], None], records: list[UsageRecord]) -> None:
        try:
            sink(records)
        except Exception:
            self.dropped += len(records)
            logger.exception("Living World: could not store AI usage records; carrying on")

    def record(self, rec: UsageRecord) -> None:
        """One finished call. Never raises."""
        if not enabled():
            return
        try:
            attr = _attribution.get() or {}
            for key in ("npc_id", "conv_id", "job_id", "store"):
                if getattr(rec, key) is None and attr.get(key) is not None:
                    setattr(rec, key, attr[key])
            with self._lock:
                self.session[rec.lane if rec.lane in self.session else "background"].add(rec)
            collector = _collector.get()
            if collector is not None:
                collector.append(rec)
                return
            self.deliver([rec])
        except Exception:
            logger.exception("Living World: could not record an AI call's usage; carrying on")

    def deliver(self, records: list[UsageRecord]) -> None:
        """Hand records to the sink (or keep them until there is one)."""
        if not records:
            return
        with self._lock:
            sink = self._sink
            if sink is None:
                self._pending.extend(records)
                over = len(self._pending) - PENDING_MAX
                if over > 0:
                    del self._pending[:over]
                    self.dropped += over
                return
        self._deliver(sink, records)

    def session_line(self) -> str:
        with self._lock:
            fg, bg = self.session["foreground"], self.session["background"]
            unknown = fg.unknown_cost_calls + bg.unknown_cost_calls
        line = (f"AI cost this session: ${fg.cost_usd + bg.cost_usd:.4f} "
                f"(foreground ${fg.cost_usd:.4f} in {fg.calls} calls, background ${bg.cost_usd:.4f} in {bg.calls})")
        if unknown:
            line += f"; {unknown} call(s) with no known price"
        return line

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            self._sink = None
            self._pending = []
            self.session = {"foreground": Totals(), "background": Totals()}
            self.dropped = 0


LEDGER = Ledger()


# ---- what the clients call ----
def task_for(default: str) -> str:
    """The task a call is for: the attributed one (e.g. ``extract`` from the job runner) or the client's own."""
    attr = _attribution.get() or {}
    task = attr.get("task") or default
    return task if task in TASKS else "request"


def record_call(*, task: str, model: str, usage: Any = None, latency_ms: int | None = None, status: str = "ok",
                error: str | None = None, local: bool = False,
                estimate: Callable[[], tuple[int | None, int | None]] | None = None) -> UsageRecord | None:
    """Build and record one call's usage. ``estimate`` counts tokens locally when ``usage`` has none.
    Never raises; returns the record (None when cost logging is off or it failed)."""
    if not enabled():
        return None
    try:
        nums = usage_numbers(usage)
        prompt, completion = nums.get("prompt_tokens"), nums.get("completion_tokens")
        estimated = False
        if prompt is None and completion is None and estimate is not None and status == "ok":
            try:
                prompt, completion = estimate()
                estimated = True
            except Exception:
                prompt = completion = None
        cost, source = cost_for(model, prompt, completion, nums.get("reported_cost"), local)
        rec = UsageRecord(task=task, model=str(model or "unknown"), prompt_tokens=prompt,
                          completion_tokens=completion, cached_tokens=nums.get("cached_tokens"),
                          estimated=estimated, cost_usd=cost, cost_source=source, latency_ms=latency_ms,
                          status=status, error=(error[:500] if error else None))
        LEDGER.record(rec)
        return rec
    except Exception:
        logger.exception("Living World: could not record an AI call's usage; carrying on")
        return None


def combine(records: list[UsageRecord]) -> dict[str, Any]:
    """Sum the records of one dialogue turn into the columns of its ``llm_request`` row."""
    def total(key: str) -> int | None:
        values = [getattr(r, key) for r in records if getattr(r, key) is not None]
        return sum(values) if values else None
    sources = {r.cost_source for r in records}
    source = "unknown" if "unknown" in sources else (sources.pop() if len(sources) == 1 else "mixed")
    return {
        "prompt_tokens": total("prompt_tokens"),
        "completion_tokens": total("completion_tokens"),
        "cached_tokens": total("cached_tokens"),
        "estimated": int(any(r.estimated for r in records)),
        "cost_usd": sum(r.cost_usd or 0.0 for r in records),
        "cost_source": source if records else "unknown",
        "model": next((r.model for r in records if r.task == "dialogue"), records[0].model if records else None),
    }


def _iso(at: float | None) -> str | None:
    if at is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def insert_usage(db: Any, timeline_id: int, task: str, model: str, npc_id: int | None, conv_id: int | None,
                 trace_id: int | None, latency_ms: int | None, status: str, error: str | None,
                 cost: dict[str, Any] | None, lane: str | None = None, at: float | None = None,
                 job_id: int | None = None) -> int:
    """One ``llm_request`` row (schema 024). ``cost`` holds the token and cost columns (``combine`` or a
    record's own); None leaves them empty, as before cost logging."""
    cost = cost or {}
    if status not in ("ok", "error", "rate_limited", "fallback"):
        status = "error"
    return db.execute(
        "INSERT INTO llm_request (timeline_id, task, model, npc_id, conv_id, job_id, trace_id, prompt_tokens,"
        " completion_tokens, cached_tokens, estimated, cost_usd, cost_source, lane, latency_ms, status, error, at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')))",
        (timeline_id, task, model, npc_id, conv_id, job_id, trace_id, cost.get("prompt_tokens"),
         cost.get("completion_tokens"), cost.get("cached_tokens"), int(cost.get("estimated") or 0),
         float(cost.get("cost_usd") or 0.0), cost.get("cost_source"), lane or lane_of(task), latency_ms, status,
         error, _iso(at))).lastrowid

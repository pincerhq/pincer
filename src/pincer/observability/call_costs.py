"""
Per-call cost record (Sprint 9, T9.1) — "what did that phone call cost us?"

Four components, three of which nobody was adding up before:

* **Twilio** — PSTN minutes, plus the ConversationRelay add-on when that engine
  is in use (CR bundles its own STT/TTS, so Deepgram/ElevenLabs are zero there).
* **STT** — Deepgram streaming seconds (`media_streams` engine only).
* **TTS** — ElevenLabs characters (Sprint 4 T4.5 already counted them per call;
  this is where that counter finally becomes money).
* **LLM** — tokens spent on the call's own turns.

LLM attribution is the subtle one. `cost_log.session_id` is a *per-user* session,
not per-call, so summing by session would bill a user's chat traffic to whichever
call happened to be running. Instead a `ContextVar` binds the current call SID
for the duration of a turn; `CostTracker.record` reports into it. Since each turn
runs in its own task, the binding propagates to everything the turn awaits and to
nothing else.

Results land in `call_costs`, one row per `call_sid`, so the dashboard and the
weekly digest can answer per-call questions the metric store deliberately cannot
(cardinality: `call_sid` is never a metric label).
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pincer.config import Settings

logger = logging.getLogger(__name__)

CALL_COSTS_SQL = """
CREATE TABLE IF NOT EXISTS call_costs (
    call_sid TEXT PRIMARY KEY,
    direction TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    language TEXT DEFAULT '',
    duration_seconds INTEGER DEFAULT 0,
    twilio_usd REAL DEFAULT 0.0,
    stt_seconds REAL DEFAULT 0.0,
    stt_usd REAL DEFAULT 0.0,
    tts_characters INTEGER DEFAULT 0,
    tts_usd REAL DEFAULT 0.0,
    llm_input_tokens INTEGER DEFAULT 0,
    llm_output_tokens INTEGER DEFAULT 0,
    llm_usd REAL DEFAULT 0.0,
    total_usd REAL DEFAULT 0.0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_costs_recorded ON call_costs(recorded_at);
"""


@dataclass
class CallCost:
    """One call's fully-priced cost breakdown."""

    call_sid: str
    direction: str = ""
    engine: str = ""
    language: str = ""
    duration_seconds: int = 0
    twilio_usd: float = 0.0
    stt_seconds: float = 0.0
    stt_usd: float = 0.0
    tts_characters: int = 0
    tts_usd: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return round(self.twilio_usd + self.stt_usd + self.tts_usd + self.llm_usd, 6)

    def components(self) -> dict[str, float]:
        return {
            "twilio": round(self.twilio_usd, 6),
            "stt": round(self.stt_usd, 6),
            "tts": round(self.tts_usd, 6),
            "llm": round(self.llm_usd, 6),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "direction": self.direction,
            "engine": self.engine,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "twilio_usd": round(self.twilio_usd, 6),
            "stt_seconds": round(self.stt_seconds, 2),
            "stt_usd": round(self.stt_usd, 6),
            "tts_characters": self.tts_characters,
            "tts_usd": round(self.tts_usd, 6),
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "llm_usd": round(self.llm_usd, 6),
            "total_usd": self.total_usd,
        }


# ── LLM attribution (ContextVar + in-process accumulators) ───────────

_current_call: ContextVar[str] = ContextVar("pincer_current_call_sid", default="")


@dataclass
class _LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stt_seconds: float = 0.0


# Bounded: a call that never ends must not leak an accumulator forever.
_MAX_TRACKED_CALLS = 200
_usage: dict[str, _LLMUsage] = {}


def begin_call(call_sid: str) -> None:
    """Start accumulating LLM/STT usage for a call."""
    if not call_sid:
        return
    _usage.setdefault(call_sid, _LLMUsage())
    while len(_usage) > _MAX_TRACKED_CALLS:
        _usage.pop(next(iter(_usage)), None)


def end_call(call_sid: str) -> _LLMUsage:
    """Take and clear a call's accumulated usage."""
    return _usage.pop(call_sid, _LLMUsage())


@contextlib.contextmanager
def call_context(call_sid: str) -> Iterator[None]:
    """Bind `call_sid` so LLM cost recorded inside is attributed to this call.

    Wrap one voice turn. Everything the turn awaits inherits the binding;
    nothing outside the task sees it.
    """
    token = _current_call.set(call_sid or "")
    try:
        yield
    finally:
        _current_call.reset(token)


def current_call_sid() -> str:
    return _current_call.get()


def attribute_llm_cost(input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    """Report LLM spend against the call bound to the current context.

    Called from `CostTracker.record`. A no-op outside a voice turn, which is
    what makes ordinary chat traffic stay out of the call's bill.
    """
    call_sid = _current_call.get()
    if not call_sid:
        return
    usage = _usage.setdefault(call_sid, _LLMUsage())
    usage.input_tokens += max(0, int(input_tokens))
    usage.output_tokens += max(0, int(output_tokens))
    usage.cost_usd += max(0.0, float(cost_usd))


def add_stt_seconds(call_sid: str, seconds: float) -> None:
    """Report streamed STT audio seconds (media_streams engine)."""
    if not call_sid or seconds <= 0:
        return
    _usage.setdefault(call_sid, _LLMUsage()).stt_seconds += float(seconds)


def reset_for_tests() -> None:
    _usage.clear()


# ── Pricing ──────────────────────────────────────────────────────────


def price_call(
    settings: Settings | Any,
    *,
    call_sid: str,
    direction: str,
    engine: str,
    language: str,
    duration_seconds: int,
    tts_characters: int = 0,
) -> CallCost:
    """Turn a finished call's usage counters into money.

    ConversationRelay bundles STT and TTS into its per-minute add-on, so on that
    engine the Deepgram/ElevenLabs line items are zero by construction rather
    than merely unmeasured — double-counting them would inflate every DACH call.
    """
    usage = end_call(call_sid)
    minutes = max(0.0, duration_seconds / 60.0)
    is_relay = str(engine or "").lower().replace("-", "_") in ("conversation_relay", "conversationrelay", "relay")

    per_min = (
        float(getattr(settings, "price_twilio_outbound_per_min", 0.0))
        if direction == "outbound"
        else float(getattr(settings, "price_twilio_inbound_per_min", 0.0))
    )
    twilio_usd = minutes * per_min
    if is_relay:
        twilio_usd += minutes * float(getattr(settings, "price_conversationrelay_per_min", 0.0))

    if is_relay:
        stt_seconds, stt_usd, tts_usd = 0.0, 0.0, 0.0
        tts_characters = 0
    else:
        # Deepgram bills the audio it received; fall back to call duration when
        # the stream never reported seconds (it is the honest upper bound).
        stt_seconds = usage.stt_seconds or float(duration_seconds)
        stt_usd = (stt_seconds / 60.0) * float(getattr(settings, "price_deepgram_per_min", 0.0))
        tts_usd = (tts_characters / 1000.0) * float(getattr(settings, "price_elevenlabs_per_1k_chars", 0.0))

    return CallCost(
        call_sid=call_sid,
        direction=direction,
        engine=engine,
        language=language,
        duration_seconds=duration_seconds,
        twilio_usd=twilio_usd,
        stt_seconds=stt_seconds,
        stt_usd=stt_usd,
        tts_characters=tts_characters,
        tts_usd=tts_usd,
        llm_input_tokens=usage.input_tokens,
        llm_output_tokens=usage.output_tokens,
        llm_usd=usage.cost_usd,
    )


# ── Persistence ──────────────────────────────────────────────────────


async def ensure_call_costs_table(db: aiosqlite.Connection) -> None:
    await db.executescript(CALL_COSTS_SQL)
    await db.commit()


@asynccontextmanager
async def _db(settings: Settings | Any) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await ensure_call_costs_table(conn)
        yield conn


async def save_call_cost(settings: Settings | Any, cost: CallCost) -> None:
    """Persist the cost row and emit the cost metric. Never raises."""
    try:
        async with _db(settings) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO call_costs (call_sid, direction, engine, language, duration_seconds, "
                "twilio_usd, stt_seconds, stt_usd, tts_characters, tts_usd, llm_input_tokens, llm_output_tokens, "
                "llm_usd, total_usd, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cost.call_sid,
                    cost.direction,
                    cost.engine,
                    cost.language,
                    cost.duration_seconds,
                    cost.twilio_usd,
                    cost.stt_seconds,
                    cost.stt_usd,
                    cost.tts_characters,
                    cost.tts_usd,
                    cost.llm_input_tokens,
                    cost.llm_output_tokens,
                    cost.llm_usd,
                    cost.total_usd,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await conn.commit()
    except Exception:
        logger.exception("Failed to persist call cost for %s", cost.call_sid)
        return

    from pincer.observability.metrics import record_call_cost

    record_call_cost(
        total_usd=cost.total_usd,
        components=cost.components(),
        direction=cost.direction,
        engine=cost.engine,
        language=cost.language,
    )
    logger.info(
        "Call cost [%s]: $%.4f (twilio $%.4f, stt $%.4f, tts $%.4f, llm $%.4f over %d+%d tokens)",
        cost.call_sid,
        cost.total_usd,
        cost.twilio_usd,
        cost.stt_usd,
        cost.tts_usd,
        cost.llm_usd,
        cost.llm_input_tokens,
        cost.llm_output_tokens,
    )


async def get_call_cost(settings: Settings | Any, call_sid: str) -> dict[str, Any] | None:
    try:
        async with _db(settings) as conn:
            cursor = await conn.execute("SELECT * FROM call_costs WHERE call_sid = ?", (call_sid,))
            row = await cursor.fetchone()
    except Exception:
        logger.debug("call_costs lookup failed", exc_info=True)
        return None
    return dict(row) if row else None


async def get_call_costs(settings: Settings | Any, call_sids: list[str]) -> dict[str, float]:
    """`{call_sid: total_usd}` for a page of calls (one query, not N)."""
    if not call_sids:
        return {}
    placeholders = ",".join("?" * len(call_sids))
    try:
        async with _db(settings) as conn:
            rows = await conn.execute_fetchall(
                f"SELECT call_sid, total_usd FROM call_costs WHERE call_sid IN ({placeholders})",  # noqa: S608
                call_sids,
            )
    except Exception:
        logger.debug("call_costs batch lookup failed", exc_info=True)
        return {}
    return {str(r["call_sid"]): float(r["total_usd"] or 0.0) for r in rows}

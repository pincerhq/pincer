"""
Per-call conversation analytics — talk time, interruptions, sentiment.

Computed from data the call already produces: no extra LLM pass (sentiment
rides along on the Sprint 3 outcome extraction) and no audio model (sentiment
is text-grounded; prosody is deliberately out of scope — it is a different
risk class).

The guiding rule is that **analytics describe the call, never the person**.
That shows up here as three concrete constraints rather than as a slogan:

- **Method is data.** Media Streams gives real byte counts and word timings,
  so its numbers are ``exact``. ConversationRelay hands audio to Twilio and
  tells us nothing about playout, so its numbers are ``estimated`` from
  character counts at a per-language speaking rate. The distinction is stored
  and surfaced, because an estimate presented as a measurement is a lie the
  UI cannot walk back.
- **Absence is not neutrality.** A voicemail, a no-answer, or a call with
  under ten seconds of speech gets NULL sentiment and a reason — never
  "neutral", which would read as "we assessed this and the caller was fine".
- **Overlap counts for both.** When both parties speak at once, the time is
  credited to each of them; nobody arbitrates who "had the floor".
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

METHOD_EXACT = "exact"
METHOD_ESTIMATED = "estimated"

# Twilio's Media Streams carry 8-bit μ-law at 8 kHz: one byte is one sample is
# 0.125 ms of playout. This is the whole basis of the `exact` agent path.
MULAW_BYTES_PER_SECOND = 8000
MS_PER_MULAW_BYTE = 1000.0 / MULAW_BYTES_PER_SECOND

# Characters of spoken text per second, per language. German averages slower
# per character than English (longer compounds, more consonant clusters).
# Tunable: these are the only knobs behind every `estimated` number.
SPEAKING_RATE_CHARS_PER_S: dict[str, float] = {"en": 15.5, "de": 14.5, "uk": 14.0}
DEFAULT_SPEAKING_RATE = 15.5

# Below this much total speech there is nothing to read a stance from.
MIN_SPEECH_MS_FOR_SENTIMENT = 10_000

SENTIMENTS: tuple[str, ...] = ("positive", "neutral", "negative", "mixed")
TRAJECTORIES: tuple[str, ...] = ("improving", "stable", "declining")

# Why sentiment is absent. Stored so the UI can say which kind of "unknown"
# this is — "call too short to assess" and "not assessed" are different facts.
REASON_TOO_SHORT = "too_short"
REASON_NOT_CONVERSED = "not_conversed"  # voicemail / no answer / never connected
REASON_EXTRACTION_FAILED = "extraction_failed"

# Outcomes where no conversation happened, so there is no stance to read.
NON_CONVERSATIONAL_OUTCOMES: frozenset[str] = frozenset({"voicemail", "no_answer"})
NON_CONVERSATIONAL_FAILURES: frozenset[str] = frozenset(
    {"voicemail", "no_answer", "busy", "call_setup", "briefing_lost", "twilio_api"}
)


def speaking_rate(language: str) -> float:
    return SPEAKING_RATE_CHARS_PER_S.get(str(language or "").strip().lower()[:2], DEFAULT_SPEAKING_RATE)


def estimate_speech_ms(text: str, language: str) -> float:
    """Spoken duration of `text` at the language's rate. 0 for empty text."""
    chars = len(str(text or "").strip())
    if not chars:
        return 0.0
    return (chars / speaking_rate(language)) * 1000.0


@dataclass
class Span:
    """A half-open speech interval in ms since call start."""

    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


def _total(spans: list[Span]) -> float:
    return sum(s.duration_ms for s in spans)


def _merge(spans: list[Span]) -> list[Span]:
    """Union of possibly-overlapping spans, sorted."""
    ordered = sorted((s for s in spans if s.duration_ms > 0), key=lambda s: s.start_ms)
    merged: list[Span] = []
    for span in ordered:
        if merged and span.start_ms <= merged[-1].end_ms:
            merged[-1] = Span(merged[-1].start_ms, max(merged[-1].end_ms, span.end_ms))
        else:
            merged.append(Span(span.start_ms, span.end_ms))
    return merged


def _overlap_ms(a: list[Span], b: list[Span]) -> float:
    """Time both sides were speaking. Computed on the merged intervals so a
    speaker's own internal overlaps cannot inflate it."""
    left, right = _merge(a), _merge(b)
    total = 0.0
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i].start_ms, right[j].start_ms)
        end = min(left[i].end_ms, right[j].end_ms)
        if end > start:
            total += end - start
        if left[i].end_ms <= right[j].end_ms:
            i += 1
        else:
            j += 1
    return total


@dataclass
class CallAnalytics:
    """The finished per-call record (§1). Speech fields are None when no
    conversation happened — never 0, which would claim we measured silence."""

    call_sid: str = ""
    agent_speech_ms: int | None = None
    caller_speech_ms: int | None = None
    silence_ms: int | None = None
    overlap_ms: int | None = None
    interruptions: int = 0
    talk_ratio: float | None = None
    method: str = METHOD_ESTIMATED
    sentiment: str | None = None
    sentiment_trajectory: str | None = None
    sentiment_rationale: str | None = None
    sentiment_reason: str = ""  # why sentiment is absent ('' when present)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_speech_ms": self.agent_speech_ms,
            "caller_speech_ms": self.caller_speech_ms,
            "silence_ms": self.silence_ms,
            "overlap_ms": self.overlap_ms,
            "interruptions": self.interruptions,
            "talk_ratio": self.talk_ratio,
            "method": self.method,
            "sentiment": self.sentiment,
            "sentiment_trajectory": self.sentiment_trajectory,
            "sentiment_rationale": self.sentiment_rationale,
            "sentiment_reason": self.sentiment_reason,
            "created_at": self.created_at,
        }


class TalkTimeAccumulator:
    """Live speaking-time bookkeeping for one call.

    Lives on ``CallState.metadata["talktime"]`` and is fed by whichever engine
    is running. Both engines feed the same interval model, so the arithmetic
    (union, overlap, silence) is identical and only the *provenance* differs.
    """

    def __init__(self, method: str = METHOD_ESTIMATED, clock: Callable[[], float] = _time.monotonic) -> None:
        self.method = METHOD_EXACT if method == METHOD_EXACT else METHOD_ESTIMATED
        self._clock = clock
        self._t0 = clock()
        self.agent_spans: list[Span] = []
        self.caller_spans: list[Span] = []
        self.interruptions = 0
        # Media Streams: the utterance currently being streamed to Twilio.
        self._open_start_ms: float | None = None
        self._open_sent_ms: float = 0.0

    # ── Clock ─────────────────────────────────────────────

    def _now_ms(self) -> float:
        return (self._clock() - self._t0) * 1000.0

    # ── Media Streams (exact) ─────────────────────────────

    def agent_audio_begin(self) -> None:
        """First audio chunk of an utterance dispatched — playout starts now."""
        self._close_open_utterance()
        self._open_start_ms = self._now_ms()
        self._open_sent_ms = 0.0

    def agent_audio_bytes(self, byte_count: int) -> None:
        """μ-law bytes handed to Twilio. One byte is 0.125 ms of playout."""
        if self._open_start_ms is None:
            self.agent_audio_begin()
        self._open_sent_ms += max(0, int(byte_count)) * MS_PER_MULAW_BYTE

    def agent_audio_cancelled(self) -> None:
        """Barge-in: Twilio drops whatever is still buffered.

        Only what had time to play counts as agent speech. Everything sent
        beyond the wall-clock elapsed since playout began was never heard, so
        counting it would inflate the agent's share exactly on the calls where
        the caller was cutting in.
        """
        if self._open_start_ms is None:
            return
        elapsed = self._now_ms() - self._open_start_ms
        played = max(0.0, min(self._open_sent_ms, elapsed))
        self.agent_spans.append(Span(self._open_start_ms, self._open_start_ms + played))
        self._open_start_ms = None
        self._open_sent_ms = 0.0

    def _close_open_utterance(self) -> None:
        if self._open_start_ms is None:
            return
        self.agent_spans.append(Span(self._open_start_ms, self._open_start_ms + self._open_sent_ms))
        self._open_start_ms = None
        self._open_sent_ms = 0.0

    def caller_span(self, start_s: float, end_s: float) -> None:
        """A caller utterance from STT word timings (seconds since stream start)."""
        start_ms, end_ms = float(start_s) * 1000.0, float(end_s) * 1000.0
        if end_ms > start_ms:
            self.caller_spans.append(Span(start_ms, end_ms))

    # ── ConversationRelay (estimated) ─────────────────────

    def agent_text(self, text: str, language: str) -> None:
        """Agent tokens handed to CR: duration estimated from character count."""
        duration = estimate_speech_ms(text, language)
        if duration <= 0:
            return
        start = self._now_ms()
        self.agent_spans.append(Span(start, start + duration))

    def caller_text(self, text: str, language: str) -> None:
        """A CR `prompt` message. It arrives once the caller has finished, so
        the estimated duration is anchored backwards from now."""
        duration = estimate_speech_ms(text, language)
        if duration <= 0:
            return
        end = self._now_ms()
        self.caller_spans.append(Span(max(0.0, end - duration), end))

    # ── Both engines ──────────────────────────────────────

    def interruption(self) -> None:
        self.interruptions += 1

    # ── Result ────────────────────────────────────────────

    def finalize(self, duration_ms: float, *, conversed: bool = True) -> CallAnalytics:
        """Close the books. `conversed=False` (voicemail, no answer) yields a
        row with null speech fields — the call has an analytics record saying
        "nothing to measure", which is different from having no record."""
        self._close_open_utterance()
        stamp = datetime.now(UTC).isoformat()
        if not conversed:
            return CallAnalytics(
                interruptions=self.interruptions,
                method=self.method,
                sentiment_reason=REASON_NOT_CONVERSED,
                created_at=stamp,
            )

        agent_ms = _total(_merge(self.agent_spans))
        caller_ms = _total(_merge(self.caller_spans))
        overlap = _overlap_ms(self.agent_spans, self.caller_spans)
        speaking = max(0.0, agent_ms + caller_ms - overlap)  # union: overlap counted once
        silence = max(0.0, float(duration_ms) - speaking)

        both = agent_ms + caller_ms
        ratio = round(agent_ms / both, 4) if both > 0 else None
        return CallAnalytics(
            agent_speech_ms=int(round(agent_ms)),
            caller_speech_ms=int(round(caller_ms)),
            silence_ms=int(round(silence)),
            overlap_ms=int(round(overlap)),
            interruptions=self.interruptions,
            talk_ratio=ratio,
            method=self.method,
            created_at=stamp,
        )


# ── Wiring helpers ───────────────────────────────────────────────────


def get_accumulator(state: Any) -> TalkTimeAccumulator | None:
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    accumulator = metadata.get("talktime")
    return accumulator if isinstance(accumulator, TalkTimeAccumulator) else None


def ensure_accumulator(state: Any, method: str) -> TalkTimeAccumulator | None:
    """Attach an accumulator to a call state (idempotent)."""
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    existing = metadata.get("talktime")
    if isinstance(existing, TalkTimeAccumulator):
        return existing
    accumulator = TalkTimeAccumulator(method=method)
    metadata["talktime"] = accumulator
    return accumulator


def was_conversational(outcome_code: str = "", failure_code: str = "") -> bool:
    """Did anyone actually talk? Voicemail and no-answer did not."""
    if str(outcome_code or "").strip().lower() in NON_CONVERSATIONAL_OUTCOMES:
        return False
    return str(failure_code or "").strip().lower() not in NON_CONVERSATIONAL_FAILURES


def apply_sentiment(analytics: CallAnalytics, outcome: Any) -> CallAnalytics:
    """Fold the Sprint 3 extraction's sentiment fields into the record.

    Every path that leaves sentiment absent records WHY, because the UI has to
    distinguish "too short to assess" from "we tried and failed" from "no call
    happened" — and none of them may render as an empty box.
    """
    if analytics.sentiment_reason == REASON_NOT_CONVERSED:
        return analytics

    speech = (analytics.agent_speech_ms or 0) + (analytics.caller_speech_ms or 0)
    if speech < MIN_SPEECH_MS_FOR_SENTIMENT:
        analytics.sentiment_reason = REASON_TOO_SHORT
        return analytics

    sentiment = str(getattr(outcome, "sentiment", "") or "").strip().lower()
    if outcome is None or sentiment not in SENTIMENTS:
        analytics.sentiment_reason = REASON_EXTRACTION_FAILED
        return analytics

    trajectory = str(getattr(outcome, "sentiment_trajectory", "") or "").strip().lower()
    analytics.sentiment = sentiment
    analytics.sentiment_trajectory = trajectory if trajectory in TRAJECTORIES else None
    analytics.sentiment_rationale = str(getattr(outcome, "sentiment_rationale", "") or "").strip() or None
    analytics.sentiment_reason = ""
    return analytics


# ── Persistence ──────────────────────────────────────────────────────


async def save_analytics(db_path: str | Path, call_sid: str, analytics: CallAnalytics) -> None:
    """Write the row. Never raises: analytics must not cost a user their report."""
    if not db_path or not call_sid:
        return
    try:
        from pincer.voice.retention import ensure_voice_tables

        async with aiosqlite.connect(str(db_path)) as db:
            await ensure_voice_tables(db)
            await db.execute(
                "INSERT OR REPLACE INTO call_analytics "
                "(call_sid, agent_speech_ms, caller_speech_ms, silence_ms, overlap_ms, interruptions, "
                "talk_ratio, method, sentiment, sentiment_trajectory, sentiment_rationale, sentiment_reason, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_sid,
                    analytics.agent_speech_ms,
                    analytics.caller_speech_ms,
                    analytics.silence_ms,
                    analytics.overlap_ms,
                    analytics.interruptions,
                    analytics.talk_ratio,
                    analytics.method,
                    analytics.sentiment,
                    analytics.sentiment_trajectory,
                    analytics.sentiment_rationale,
                    analytics.sentiment_reason,
                    analytics.created_at or datetime.now(UTC).isoformat(),
                ),
            )
            await db.commit()
    except Exception:
        logger.exception("Analytics persistence failed [%s]", call_sid)


def analytics_from_row(row: aiosqlite.Row) -> CallAnalytics:
    def _int(key: str) -> int | None:
        value = row[key]
        return int(value) if value is not None else None

    return CallAnalytics(
        call_sid=str(row["call_sid"]),
        agent_speech_ms=_int("agent_speech_ms"),
        caller_speech_ms=_int("caller_speech_ms"),
        silence_ms=_int("silence_ms"),
        overlap_ms=_int("overlap_ms"),
        interruptions=int(row["interruptions"] or 0),
        talk_ratio=float(row["talk_ratio"]) if row["talk_ratio"] is not None else None,
        method=str(row["method"] or METHOD_ESTIMATED),
        sentiment=row["sentiment"],
        sentiment_trajectory=row["sentiment_trajectory"],
        sentiment_rationale=row["sentiment_rationale"],
        sentiment_reason=str(row["sentiment_reason"] or ""),
        created_at=str(row["created_at"] or ""),
    )


async def load_analytics(db_path: str | Path, call_sid: str) -> CallAnalytics | None:
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_analytics WHERE call_sid = ?", (call_sid,))
            row = await cursor.fetchone()
    except aiosqlite.OperationalError:
        return None
    return analytics_from_row(row) if row is not None else None


async def load_many(db_path: str | Path, call_sids: list[str]) -> dict[str, CallAnalytics]:
    """One batched lookup for a page of calls, not one query per row."""
    if not call_sids:
        return {}
    placeholders = ", ".join("?" for _ in call_sids)
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                f"SELECT * FROM call_analytics WHERE call_sid IN ({placeholders})",  # noqa: S608 - placeholders only
                call_sids,
            )
    except aiosqlite.OperationalError:
        return {}
    return {str(r["call_sid"]): analytics_from_row(r) for r in rows}


async def sentiment_distribution(
    db_path: str | Path,
    *,
    days: int = 7,
    direction: str = "",
) -> dict[str, int]:
    """Counts per sentiment over the window, optionally for one direction."""
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat()
    sql = (
        "SELECT a.sentiment AS sentiment, COUNT(*) AS n FROM call_analytics a "
        "JOIN voice_calls c ON c.call_sid = a.call_sid "
        "WHERE a.sentiment IS NOT NULL AND c.started_at >= ?"
    )
    args: list[Any] = [cutoff]
    if direction:
        sql += " AND c.direction = ?"
        args.append(direction)
    sql += " GROUP BY a.sentiment"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(sql, args)
    except aiosqlite.OperationalError:
        return dict.fromkeys(SENTIMENTS, 0)
    counts = dict.fromkeys(SENTIMENTS, 0)
    for row in rows:
        key = str(row["sentiment"] or "")
        if key in counts:
            counts[key] = int(row["n"])
    return counts


async def count_negative_since(db_path: str | Path, cutoff_iso: str) -> int:
    """Negative-sentiment calls started at or after `cutoff_iso` (alert input)."""
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM call_analytics a JOIN voice_calls c ON c.call_sid = a.call_sid "
                "WHERE a.sentiment = 'negative' AND c.started_at >= ?",
                (cutoff_iso,),
            )
            row = await cursor.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row["n"]) if row else 0


# ── Report line (§5) ─────────────────────────────────────────────────

_NEGATIVE_LINE = {
    "en": "⚠️ The caller seemed dissatisfied: {rationale}",
    "de": "⚠️ Der Anrufer wirkte unzufrieden: {rationale}",
    "uk": "⚠️ Абонент виглядав незадоволеним: {rationale}",
}


def render_report_line(analytics: CallAnalytics | None, language: str = "en") -> str:
    """One line, and ONLY for negative calls.

    A note on every call would be noise the owner learns to skip, and the point
    of the line is that a dissatisfied caller gets noticed. "Seemed", never
    "was": this is a reading of one conversation, not a verdict on a person.
    """
    if analytics is None or analytics.sentiment != "negative":
        return ""
    lang = str(language or "en").strip().lower()[:2]
    template = _NEGATIVE_LINE.get(lang, _NEGATIVE_LINE["en"])
    rationale = (analytics.sentiment_rationale or "").strip()
    if not rationale:
        return template.split(":")[0].strip() + "."
    return template.format(rationale=rationale)


def record_metrics(analytics: CallAnalytics, *, engine: str = "", direction: str = "", language: str = "") -> None:
    """Emit the Sprint 9 instruments for one finished call (best effort)."""
    try:
        from pincer.observability.metrics import record_call_analytics

        record_call_analytics(
            talk_ratio=analytics.talk_ratio,
            sentiment=analytics.sentiment or "",
            interruptions=analytics.interruptions,
            method=analytics.method,
            engine=engine,
            direction=direction,
            language=language,
        )
    except Exception:  # pragma: no cover — metrics must never break a call
        logger.debug("analytics metrics failed", exc_info=True)


def to_json(analytics: CallAnalytics) -> str:
    return json.dumps(analytics.to_dict(), ensure_ascii=False)


__all__ = [
    "METHOD_ESTIMATED",
    "METHOD_EXACT",
    "MIN_SPEECH_MS_FOR_SENTIMENT",
    "REASON_EXTRACTION_FAILED",
    "REASON_NOT_CONVERSED",
    "REASON_TOO_SHORT",
    "SENTIMENTS",
    "SPEAKING_RATE_CHARS_PER_S",
    "TRAJECTORIES",
    "CallAnalytics",
    "Span",
    "TalkTimeAccumulator",
    "apply_sentiment",
    "count_negative_since",
    "ensure_accumulator",
    "estimate_speech_ms",
    "get_accumulator",
    "load_analytics",
    "load_many",
    "record_metrics",
    "render_report_line",
    "save_analytics",
    "sentiment_distribution",
    "speaking_rate",
    "was_conversational",
]

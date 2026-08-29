"""
Call briefing — the task the user gave the agent, as a first-class object.

The product promise of an outbound call is "the agent does what you told it".
That promise used to rest on a string travelling through four places
(`make_phone_call` → status_notify → the relay setup handler → the system
prompt), any of which could drop it, and on a prompt block that read as
context rather than as an instruction. When it broke, the agent still placed
the call and talked pleasantly about what an assistant can do.

This module makes the briefing something that either exists and binds, or
stops the call:

- **Validated at every entry point.** A task under
  :data:`MIN_TASK_CHARS` characters is rejected with an actionable message.
  There is no such thing as an outbound call without a task.
- **Carried as one object**, not as loose keyword arguments, so a new caller
  cannot forget half of it.
- **Persisted verbatim** (``voice_calls.briefing_json``) and written into the
  transcript as a ``[BRIEFING]`` system line, so "what was the agent told?" is
  answerable after the fact from the record rather than from logs.
- **Checked afterwards** (:func:`check_adherence`): if the agent's opening
  turns don't reference the task, that is logged as ``briefing_adherence_low``.
  It is a smoke detector, not a gate — it never blocks a call or a report.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

# A task shorter than this is not a task ("call mum", "ask", "hi"): the agent
# has nothing to open the call with and falls back to talking about itself.
MIN_TASK_CHARS = 10
MAX_TASK_CHARS = 2000
MAX_INSTRUCTIONS_CHARS = 4000
# How much of the task goes into the transcript's audit line and the API preview.
TRANSCRIPT_PREVIEW_CHARS = 200
ACTIVE_PREVIEW_CHARS = 120

# The appointment flow composes its task ("Schedule appointment with X: <topic>"),
# so the composed string always clears MIN_TASK_CHARS. The part the user
# actually supplies is the topic, so that is what gets checked there.
MIN_TOPIC_CHARS = 3

TASK_TOO_SHORT = "Purpose too short — tell the agent concretely what to do on this call."
TOPIC_TOO_SHORT = "Purpose too short — say what the appointment is about, so the agent can open the call with it."
TASK_TOO_LONG = (
    f"Purpose too long — keep the call task under {MAX_TASK_CHARS} characters "
    "(put background the agent does not need to say out loud into the instructions)."
)

SOURCE_DASHBOARD = "dashboard"
SOURCE_CHAT = "chat"
SOURCE_API = "api"
SOURCE_SCHEDULER = "scheduler"


class BriefingError(ValueError):
    """The briefing is unusable. Carries the user-facing message verbatim."""


def validate_task(task: str) -> str:
    """The one gate every entry point goes through. Returns the cleaned task.

    Raises :class:`BriefingError` whose message is meant to be shown to the
    user as-is — a tool returns it as ``Error: …``, an API route as a 422
    detail. Length is measured after stripping, so whitespace is never a task.
    """
    text = str(task or "").strip()
    if len(text) < MIN_TASK_CHARS:
        raise BriefingError(TASK_TOO_SHORT)
    if len(text) > MAX_TASK_CHARS:
        raise BriefingError(TASK_TOO_LONG)
    return text


def validate_topic(topic: str) -> str:
    """The appointment-flow equivalent of :func:`validate_task`."""
    text = str(topic or "").strip()
    if len(text) < MIN_TOPIC_CHARS:
        raise BriefingError(TOPIC_TOO_SHORT)
    return text


@dataclass
class CallBriefing:
    """Everything the agent was told to do on one call."""

    task: str  # the user's purpose text, verbatim
    target_name: str = ""
    language: str = ""
    source: str = ""  # dashboard | chat | api | scheduler
    thread_context: str = ""  # Sprint 13 THREAD CONTEXT block, when threaded
    # Extra per-call guidance ("accept any slot 9–12, never Friday"). Part of
    # the briefing rather than a loose argument so it cannot be dropped
    # independently of the task it qualifies.
    instructions: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        task: str,
        *,
        target_name: str = "",
        language: str = "",
        source: str = "",
        instructions: str = "",
        thread_context: str = "",
    ) -> CallBriefing:
        """Validated constructor — the only way a briefing should be built."""
        return cls(
            task=validate_task(task),
            target_name=str(target_name or "").strip()[:200],
            language=str(language or "").strip()[:8],
            source=str(source or "").strip()[:32],
            thread_context=str(thread_context or ""),
            instructions=str(instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS],
        )

    def preview(self, limit: int = ACTIVE_PREVIEW_CHARS) -> str:
        return self.task[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "target_name": self.target_name,
            "language": self.language,
            "source": self.source,
            "instructions": self.instructions,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> CallBriefing | None:
        """Parse a persisted briefing. None when absent or unreadable — a
        historical row without one is not an error."""
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not str(data.get("task", "")).strip():
            return None
        return cls(
            task=str(data.get("task", "")),
            target_name=str(data.get("target_name", "") or ""),
            language=str(data.get("language", "") or ""),
            source=str(data.get("source", "") or ""),
            instructions=str(data.get("instructions", "") or ""),
        )


def briefing_from_state(state: Any) -> CallBriefing | None:
    """Recover the briefing a live call is running under.

    Prefers the object stashed at registration; falls back to reconstructing
    it from the call state, so a call registered by an older path still
    reports a briefing instead of nothing.
    """
    metadata = getattr(state, "metadata", None)
    if isinstance(metadata, dict):
        stored = metadata.get("briefing")
        if isinstance(stored, CallBriefing):
            return stored
        if isinstance(stored, dict) and str(stored.get("task", "")).strip():
            return CallBriefing(
                task=str(stored.get("task", "")),
                target_name=str(stored.get("target_name", "") or ""),
                language=str(stored.get("language", "") or ""),
                source=str(stored.get("source", "") or ""),
                instructions=str(stored.get("instructions", "") or ""),
            )
    task = str(getattr(state, "purpose", "") or "").strip()
    if not task:
        return None
    return CallBriefing(
        task=task,
        target_name=str(getattr(state, "target_name", "") or ""),
        language=str(getattr(state, "language", "") or ""),
        instructions=str(getattr(state, "instructions", "") or ""),
    )


def log_briefing_bound(call_sid: str, briefing: CallBriefing) -> None:
    """One INFO line per briefed dial — the thing to grep when a user says
    "it ignored me". Absence of this line IS the diagnosis."""
    logger.info(
        "BRIEFING_BOUND call=%s chars=%d source=%s",
        call_sid or "(pending)",
        len(briefing.task),
        briefing.source or "unknown",
    )


def record_briefing(transcript: TranscriptLogger | None, briefing: CallBriefing | None) -> None:
    """Write the briefing into the call's own transcript as a system line.

    Makes briefing presence visible in every transcript the user or an
    operator opens, without needing the logs of the process that dialled.
    """
    if transcript is None or briefing is None:
        return
    from pincer.voice.transcript import Speaker

    try:
        transcript.log_utterance(
            Speaker.SYSTEM,
            f"[BRIEFING] {briefing.task[:TRANSCRIPT_PREVIEW_CHARS]}",
            state="briefing",
        )
    except Exception:  # pragma: no cover — an audit line must not break a call
        logger.debug("Briefing transcript entry failed", exc_info=True)


# ── Adherence check (§4.2) ───────────────────────────────────────────

_WORD_RE = re.compile(r"\w+", re.UNICODE)
# Words too common to be evidence that the agent talked about the task.
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "and",
        "any",
        "are",
        "ask",
        "asked",
        "back",
        "because",
        "been",
        "before",
        "call",
        "called",
        "calling",
        "can",
        "could",
        "did",
        "does",
        "for",
        "from",
        "get",
        "give",
        "going",
        "has",
        "have",
        "her",
        "here",
        "him",
        "his",
        "how",
        "into",
        "just",
        "know",
        "like",
        "make",
        "more",
        "much",
        "need",
        "not",
        "now",
        "one",
        "only",
        "our",
        "out",
        "over",
        "please",
        "said",
        "say",
        "see",
        "she",
        "should",
        "some",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "time",
        "und",
        "want",
        "was",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        # German
        "aber",
        "alle",
        "als",
        "auch",
        "auf",
        "bei",
        "bin",
        "bitte",
        "das",
        "dass",
        "dem",
        "den",
        "der",
        "die",
        "dir",
        "doch",
        "ein",
        "eine",
        "einen",
        "einer",
        "für",
        "haben",
        "hallo",
        "hat",
        "ich",
        "ihr",
        "ihre",
        "ist",
        "kann",
        "mich",
        "mir",
        "mit",
        "nicht",
        "noch",
        "nur",
        "oder",
        "schon",
        "sehr",
        "sein",
        "sich",
        "sie",
        "sind",
        "über",
        "uns",
        "vom",
        "von",
        "war",
        "werden",
        "wie",
        "wir",
        "wird",
        "würde",
        "zum",
        "zur",
    }
)
# Share of the task's content words that must appear in the opening turns…
ADHERENCE_THRESHOLD = 0.3
# …and how many distinct words must match. The share alone is not enough: on a
# four-word task a single incidental "today" scores 25%, which would let a pure
# capability monologue ("I can help you with appointments, emails, reminders…")
# pass as on-task. A task short enough to have one or two content words has
# nothing else to match on, so it keeps the single-word rule.
ADHERENCE_MIN_MATCHES = 2
ADHERENCE_TURNS = 3


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or "")) if len(w) > 3 and w.lower() not in _STOPWORDS}


def check_adherence(
    task: str,
    transcript: TranscriptLogger | None,
    *,
    task_result: str = "",
    turns: int = ADHERENCE_TURNS,
) -> tuple[bool, float]:
    """(adhered, overlap) — did the agent's opening turns mention the task?

    Deliberately cheap and deliberately non-blocking. A call that reached a
    Sprint 3 ``task_result`` demonstrably did the job whatever its phrasing,
    so that short-circuits to "adhered": the check exists to catch the agent
    opening with a capability monologue, not to police wording.
    """
    wanted = _content_words(task)
    if not wanted:
        return True, 1.0
    if str(task_result or "").strip():
        return True, 1.0
    if transcript is None:
        return True, 1.0

    from pincer.voice.transcript import Speaker

    agent_turns = [e.text for e in getattr(transcript, "entries", []) if e.speaker == Speaker.AGENT][:turns]
    if not agent_turns:
        return True, 1.0  # nothing was said at all — that is a different failure

    matched = wanted & _content_words(" ".join(agent_turns))
    overlap = len(matched) / len(wanted)
    required = 1 if len(wanted) <= 2 else ADHERENCE_MIN_MATCHES
    return overlap >= ADHERENCE_THRESHOLD and len(matched) >= required, overlap


def report_adherence(call_sid: str, task: str, transcript: TranscriptLogger | None, task_result: str = "") -> bool:
    """Run the adherence check and raise the smoke alarm if it fails."""
    try:
        adhered, overlap = check_adherence(task, transcript, task_result=task_result)
    except Exception:  # pragma: no cover — a smoke detector must not start fires
        logger.debug("Adherence check failed [%s]", call_sid, exc_info=True)
        return True
    if adhered:
        return True
    logger.warning(
        "briefing_adherence_low call=%s overlap=%.2f — the agent's opening turns did not reference its task",
        call_sid,
        overlap,
    )
    try:
        from pincer.observability.metrics import record_briefing_adherence

        record_briefing_adherence(adhered=False)
    except Exception:  # pragma: no cover
        logger.debug("adherence metric failed", exc_info=True)
    return False


__all__ = [
    "ACTIVE_PREVIEW_CHARS",
    "MAX_TASK_CHARS",
    "MIN_TASK_CHARS",
    "SOURCE_API",
    "SOURCE_CHAT",
    "SOURCE_DASHBOARD",
    "SOURCE_SCHEDULER",
    "TASK_TOO_LONG",
    "TASK_TOO_SHORT",
    "TOPIC_TOO_SHORT",
    "BriefingError",
    "CallBriefing",
    "briefing_from_state",
    "check_adherence",
    "log_briefing_bound",
    "record_briefing",
    "report_adherence",
    "validate_task",
    "validate_topic",
]

"""
Structured post-call outcome extraction (Sprint 3, T3.1).

One LLM pass over the final transcript + action list produces a structured
CallOutcome (result, key facts, commitments, follow-up suggestions). Every
field must be supported by the transcript — the prompt forbids inference, and
`filter_ungrounded()` mechanically drops facts/commitments whose content does
not appear in the transcript. Extraction failure never blocks the basic
report; callers fall back to `TranscriptLogger.generate_summary()`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

VALID_OUTCOMES = {
    "completed",
    "partial",
    "declined",
    "callback_requested",
    "voicemail",
    "no_answer",
    "failed",
}

EXTRACTION_SYSTEM_PROMPT = """\
You extract the structured outcome of a completed phone call from its transcript.

STRICT GROUNDING RULES:
1. Every field MUST be directly supported by the transcript below. Do NOT infer, \
guess, or add anything the transcript does not say.
2. Statements made by the CALLEE are the callee's statements — never turn them into \
commitments of the agent or the user unless the AGENT explicitly agreed in the transcript.
3. If the transcript does not support a field, leave it empty ([] or null). An empty \
field is correct; an invented one is a failure.
4. follow_up_suggestions may only propose actions that follow directly from an explicit \
agreement or request in the transcript.
5. Write task_result, key_facts, and commitments in the language given by "language" below.

SENTIMENT RULES (same grounding requirements as everything above):
6. "sentiment" is the CALLER's stance toward THIS CALL and the matter it is about — nothing else. \
"frustrated about the repeated delay" is a stance; "an angry person" is a character judgement and is \
FORBIDDEN. Never infer personality, mood disorders, or anything about the person beyond this conversation.
7. Every sentiment MUST be supported by something that actually happened in the transcript, and \
"sentiment_rationale" MUST reference it in one sentence. With no clear signals either way, use "neutral" \
with the rationale "no clear signals".
8. "sentiment_trajectory" compares the LAST third of the call with the FIRST third: "improving", \
"stable", or "declining".
9. Judge only the caller. The agent's own tone is not sentiment.

Respond with ONLY a JSON object, no markdown fences, matching exactly:
{
  "outcome": "completed | partial | declined | callback_requested | voicemail | no_answer | failed",
  "task_result": "what was achieved, one sentence",
  "key_facts": ["..."],
  "commitments": [{"who": "callee|agent", "what": "...", "when": "ISO datetime or null"}],
  "follow_up_suggestions": [{"tool": "...", "reason": "...", "draft_args": {}}],
  "language": "en|de|uk",
  "abusive": false,
  "sentiment": "positive | neutral | negative | mixed | null",
  "sentiment_trajectory": "improving | stable | declining | null",
  "sentiment_rationale": "one sentence citing what in the call supports this, or null"
}
"abusive" is true ONLY when the other party was insulting, threatening, or clearly harassing — never for \
mere frustration or a declined request.\
"""


@dataclass
class CallOutcome:
    """Structured result of a finished call."""

    outcome: str = "completed"
    task_result: str = ""
    key_facts: list[str] = field(default_factory=list)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    follow_up_suggestions: list[dict[str, Any]] = field(default_factory=list)
    language: str = "en"
    abusive: bool = False  # Sprint 12 §10.2: owner report suggests a blocklist entry (never auto-added)
    # Conversation analytics: the caller's stance toward THIS call, read from
    # the transcript in the same pass. None when the model gave nothing usable
    # — a missing reading is never silently rounded to "neutral".
    sentiment: str | None = None
    sentiment_trajectory: str | None = None
    sentiment_rationale: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "outcome": self.outcome,
                "task_result": self.task_result,
                "key_facts": self.key_facts,
                "commitments": self.commitments,
                "follow_up_suggestions": self.follow_up_suggestions,
                "language": self.language,
                "abusive": self.abusive,
                "sentiment": self.sentiment,
                "sentiment_trajectory": self.sentiment_trajectory,
                "sentiment_rationale": self.sentiment_rationale,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> CallOutcome | None:
        """Parse LLM output robustly (fences, surrounding prose). None on failure."""
        raw = str(text or "").strip()
        if not raw:
            return None
        # Strip markdown fences and find the outermost JSON object
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None

        outcome = str(data.get("outcome", "")).strip().lower()
        if outcome not in VALID_OUTCOMES:
            return None

        def _str_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item).strip() for item in value if str(item).strip()]

        def _dict_list(value: Any) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                return []
            return [item for item in value if isinstance(item, dict)]

        commitments = []
        for item in _dict_list(data.get("commitments")):
            who = str(item.get("who", "")).strip().lower()
            what = str(item.get("what", "")).strip()
            if who in ("callee", "agent") and what:
                commitments.append({"who": who, "what": what, "when": item.get("when") or None})

        suggestions: list[dict[str, Any]] = []
        for item in _dict_list(data.get("follow_up_suggestions")):
            tool = str(item.get("tool", "")).strip()
            if tool:
                suggestions.append(
                    {
                        "tool": tool,
                        "reason": str(item.get("reason", "")).strip(),
                        "draft_args": item.get("draft_args") if isinstance(item.get("draft_args"), dict) else {},
                    }
                )

        def _enum(key: str, allowed: tuple[str, ...]) -> str | None:
            value = str(data.get(key) or "").strip().lower()
            return value if value in allowed else None

        from pincer.voice.analytics import SENTIMENTS, TRAJECTORIES

        sentiment = _enum("sentiment", SENTIMENTS)
        rationale = str(data.get("sentiment_rationale") or "").strip()
        # A label with no rationale is exactly the over-claiming this feature is
        # supposed to avoid, so the pair travels together or not at all.
        if sentiment is None:
            rationale = ""

        return cls(
            outcome=outcome,
            task_result=str(data.get("task_result", "")).strip(),
            key_facts=_str_list(data.get("key_facts")),
            commitments=commitments,
            follow_up_suggestions=suggestions,
            language=str(data.get("language", "en")).strip().lower()[:2] or "en",
            abusive=bool(data.get("abusive", False)) if isinstance(data.get("abusive", False), bool) else False,
            sentiment=sentiment,
            sentiment_trajectory=_enum("sentiment_trajectory", TRAJECTORIES) if sentiment else None,
            sentiment_rationale=rationale or None,
        )


_WORD_RE = re.compile(r"\w+", re.UNICODE)
_NUM_RE = re.compile(r"\d[\d:.,]*")


def _significant_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 3}


def _numbers(text: str) -> set[str]:
    """Digit runs with the separators stripped, so 14:30 / 14.30 / 1430
    compare equal (and a sentence-final '14:30.' matches '14:30')."""
    return {re.sub(r"[:.,]", "", n) for n in _NUM_RE.findall(text)}


def _is_grounded(
    claim: str,
    transcript_words: set[str],
    transcript_numbers: set[str],
    threshold: float = 0.5,
) -> bool:
    # Times, dates and prices are the values a fabricated claim most needs to
    # get right, and the word check can't see them (\w+ splits "14:30" into
    # tokens too short to keep). Every number in a claim must appear in the
    # transcript — enforced only when the transcript contains digits at all,
    # because some STT paths spell numbers out in words and wholesale-dropping
    # true numeric facts would be worse than the word check alone.
    if transcript_numbers and _numbers(claim) - transcript_numbers:
        return False
    words = _significant_words(claim)
    if not words:
        return False
    overlap = len(words & transcript_words) / len(words)
    return overlap >= threshold


def filter_ungrounded(outcome: CallOutcome, transcript_text: str) -> CallOutcome:
    """Mechanical grounding check: drop key facts and commitments whose
    significant words don't substantially appear in the transcript, or whose
    numbers (times, dates, prices) the transcript never mentions.

    A belt to the extraction prompt's suspenders, not a semantic verifier:
    bag-of-words overlap is blind to negation and entity swaps ("not
    available on Tuesday" grounds "available on Tuesday"), so the prompt
    remains the primary defense against invented facts."""
    transcript_words = _significant_words(transcript_text)
    transcript_numbers = _numbers(transcript_text)

    kept_facts = [f for f in outcome.key_facts if _is_grounded(f, transcript_words, transcript_numbers)]
    dropped = len(outcome.key_facts) - len(kept_facts)
    kept_commitments = [
        c for c in outcome.commitments if _is_grounded(str(c.get("what", "")), transcript_words, transcript_numbers)
    ]
    dropped += len(outcome.commitments) - len(kept_commitments)

    if dropped:
        logger.warning("Grounding filter dropped %d ungrounded item(s) from call outcome", dropped)
    outcome.key_facts = kept_facts
    outcome.commitments = kept_commitments
    return outcome


# ~10-15 minutes of dialogue; voice_max_call_duration allows far longer.
MAX_TRANSCRIPT_CHARS = 8000


async def extract_outcome(
    llm: BaseLLMProvider,
    transcript_text: str,
    actions_text: str,
    language: str = "en",
    purpose: str = "",
    talk_time: str = "",
) -> CallOutcome | None:
    """One LLM pass over transcript + actions. None on any failure —
    the caller falls back to the basic summary report."""
    if not transcript_text.strip():
        return None
    from pincer.llm.base import LLMMessage, MessageRole

    # Long calls exceed the excerpt budget; keep the tail — the outcome,
    # agreement, and goodbyes live at the end, the greeting is expendable.
    # (filter_ungrounded still runs against the full text.)
    excerpt = transcript_text
    if len(excerpt) > MAX_TRANSCRIPT_CHARS:
        logger.warning(
            "Transcript truncated for outcome extraction: %d -> %d chars (keeping the tail)",
            len(excerpt),
            MAX_TRANSCRIPT_CHARS,
        )
        excerpt = excerpt[-MAX_TRANSCRIPT_CHARS:]

    user_content = (
        f"language: {language}\n"
        f"call purpose: {purpose or '(not stated)'}\n"
        # Speaking-time context helps the sentiment read (a caller who barely
        # got a word in, long silences) — labelled with its method so an
        # estimate is not mistaken for a measurement.
        + (f"{talk_time}\n" if talk_time else "")
        + f"\nTRANSCRIPT:\n{excerpt}\n\n"
        f"TOOL ACTIONS DURING CALL:\n{actions_text or '(none)'}"
    )
    try:
        response = await llm.complete(
            messages=[LLMMessage(role=MessageRole.USER, content=user_content)],
            system=EXTRACTION_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=1000,
        )
        outcome = CallOutcome.from_json(response.content or "")
        if outcome is None:
            logger.warning("Outcome extraction returned unparseable JSON")
            return None
        return filter_ungrounded(outcome, transcript_text)
    except Exception:
        logger.exception("Outcome extraction failed")
        return None


# ── Report rendering (T3.2) ──────────────────────────────────────────────────

_OUTCOME_EMOJI = {
    "completed": "✅",
    "partial": "🟡",
    "declined": "❌",
    "callback_requested": "📲",
    "voicemail": "📼",
    "no_answer": "📵",
    "failed": "⚠️",
}

_OUTCOME_LABELS = {
    "en": {
        "completed": "completed",
        "partial": "partially completed",
        "declined": "declined by the callee",
        "callback_requested": "callback requested",
        "voicemail": "reached voicemail",
        "no_answer": "no answer",
        "failed": "failed",
    },
    "de": {
        "completed": "erfolgreich",
        "partial": "teilweise erledigt",
        "declined": "vom Gesprächspartner abgelehnt",
        "callback_requested": "Rückruf vereinbart",
        "voicemail": "Anrufbeantworter erreicht",
        "no_answer": "keine Antwort",
        "failed": "fehlgeschlagen",
    },
}


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    return f"{minutes}:{secs:02d}"


def _who_label(who: str, language: str) -> str:
    if language == "de":
        return "Gegenseite" if who == "callee" else "Assistent"
    return "callee" if who == "callee" else "agent"


def render_report(
    outcome: CallOutcome,
    target_label: str,
    duration_seconds: int,
    call_sid: str,
    language: str = "en",
) -> str:
    """Channel-appropriate post-call report in the user's language."""
    lang = "de" if language.lower().startswith("de") else "en"
    emoji = _OUTCOME_EMOJI.get(outcome.outcome, "📞")
    label = _OUTCOME_LABELS[lang].get(outcome.outcome, outcome.outcome)

    lines: list[str] = []
    if lang == "de":
        lines.append(f"{emoji} Anruf bei {target_label} beendet ({_fmt_duration(duration_seconds)} Min) — {label}")
        if outcome.task_result:
            lines.append(f"Ergebnis: {outcome.task_result}")
        if outcome.key_facts:
            lines.append("Besprochen: " + " ".join(outcome.key_facts[:3]))
        if outcome.commitments:
            parts = [f"{_who_label(c['who'], lang)}: {c['what']}" for c in outcome.commitments[:3]]
            lines.append("Zusagen: " + "; ".join(parts))
        for suggestion in outcome.follow_up_suggestions[:2]:
            reason = suggestion.get("reason") or suggestion.get("tool", "")
            lines.append(f"➡️ Soll ich das übernehmen: {reason}? Antworte einfach mit Ja.")
        lines.append(f"📄 Vollständiges Transkript: /transcript {call_sid}")
    else:
        lines.append(f"{emoji} Call with {target_label} ended ({_fmt_duration(duration_seconds)} min) — {label}")
        if outcome.task_result:
            lines.append(f"Result: {outcome.task_result}")
        if outcome.key_facts:
            lines.append("Discussed: " + " ".join(outcome.key_facts[:3]))
        if outcome.commitments:
            parts = [f"{_who_label(c['who'], lang)}: {c['what']}" for c in outcome.commitments[:3]]
            lines.append("Commitments: " + "; ".join(parts))
        for suggestion in outcome.follow_up_suggestions[:2]:
            reason = suggestion.get("reason") or suggestion.get("tool", "")
            lines.append(f"➡️ Shall I take care of this: {reason}? Just reply yes.")
        lines.append(f"📄 Full transcript: /transcript {call_sid}")

    return "\n".join(lines)


def render_fallback_report(
    target_label: str,
    duration_seconds: int,
    call_sid: str,
    completed: bool,
    language: str = "en",
) -> str:
    """Minimal report when extraction is unavailable or failed (never blocks)."""
    lang = "de" if language.lower().startswith("de") else "en"
    if lang == "de":
        status = "abgeschlossen" if completed else "nicht abgeschlossen"
        return (
            f"📞 Anruf bei {target_label} beendet ({_fmt_duration(duration_seconds)} Min) — {status}.\n"
            f"📄 Vollständiges Transkript: /transcript {call_sid}"
        )
    status = "completed" if completed else "did not complete"
    return (
        f"📞 Call with {target_label} ended ({_fmt_duration(duration_seconds)} min) — {status}.\n"
        f"📄 Full transcript: /transcript {call_sid}"
    )

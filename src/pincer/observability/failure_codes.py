"""
Structured failure taxonomy (Sprint 9, T9.3).

Every way a call can fail gets one **stable** code. Stable is the whole point:
the code is a metric label, a `voice_calls` column, a runbook heading, and a
line in the weekly digest, so renaming one breaks four things at once. Add new
codes; never repurpose an existing one.

The taxonomy is deliberately about *what broke*, not *what the caller did*.
`no_answer` and `voicemail` are outcomes of a healthy system reaching an
unavailable human — they are excluded from the call-success SLO
(`COUNTS_AGAINST_SLO`) because we cannot fix a callee who is out.
"""

from __future__ import annotations

import re
from enum import StrEnum


class FailureCode(StrEnum):
    """Why a call did not complete. `NONE` means it did."""

    NONE = "none"

    # ── Callee-side outcomes (not our fault, still worth counting) ──
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    WRONG_NUMBER = "wrong_number"
    CALLEE_HANGUP = "callee_hangup"
    SILENT_CALLEE = "silent_callee"

    # ── Transport / telephony ──────────────────────────────────────
    WS_DROP = "ws_drop"
    TWILIO_API = "twilio_api"
    TWIML_ERROR = "twiml_error"
    CALL_SETUP = "call_setup"
    # Outbound call whose briefing could not be recovered at relay setup.
    # Refused rather than run: an unbriefed outbound call is a generic
    # assistant monologue on a call the user gave a task to.
    BRIEFING_LOST = "briefing_lost"

    # ── Media pipeline ─────────────────────────────────────────────
    STT_ERROR = "stt_error"
    TTS_ERROR = "tts_error"
    NO_AUDIO = "no_audio"

    # ── Agent brain ────────────────────────────────────────────────
    LLM_ERROR = "llm_error"
    TOOL_ERROR = "tool_error"
    LANGUAGE_DRIFT = "language_drift"
    PHASE_TIMEOUT = "phase_timeout"

    # ── Policy / guardrails ────────────────────────────────────────
    BUDGET = "budget"
    INJECTION_BLOCKED = "injection_blocked"
    ABUSE_GATE = "abuse_gate"
    DO_NOT_CALL = "do_not_call"
    QUIET_HOURS = "quiet_hours"
    # Sprint 12: inbound policy declines
    BLOCKED = "blocked"
    BUSY_CAPACITY = "busy_capacity"

    # ── Operational ────────────────────────────────────────────────
    SHUTDOWN = "shutdown"
    STUCK = "stuck"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


# Codes that mean "the callee was simply not reachable". The call-attempt SLO
# (T9.5: 99% success *excluding callee no-answer*) is measured with these
# removed from the denominator — otherwise a week of holidays reads as an
# outage and the error budget burns for something no deploy can fix.
CALLEE_UNREACHABLE: frozenset[FailureCode] = frozenset(
    {
        FailureCode.NO_ANSWER,
        FailureCode.BUSY,
        FailureCode.VOICEMAIL,
        FailureCode.WRONG_NUMBER,
    }
)

# Blocked before dialling by a deliberate policy decision. Not a failure of the
# system — the system worked — so these never burn error budget either.
POLICY_BLOCKED: frozenset[FailureCode] = frozenset(
    {
        FailureCode.DO_NOT_CALL,
        FailureCode.QUIET_HOURS,
        FailureCode.ABUSE_GATE,
        FailureCode.INJECTION_BLOCKED,
        FailureCode.BUDGET,
        FailureCode.BLOCKED,
        FailureCode.BUSY_CAPACITY,
    }
)

# Everything else that is not NONE is ours to fix.
EXCLUDED_FROM_SLO: frozenset[FailureCode] = CALLEE_UNREACHABLE | POLICY_BLOCKED

# Codes that should wake someone up rather than merely be reported.
PAGEABLE: frozenset[FailureCode] = frozenset(
    {
        FailureCode.STUCK,
        FailureCode.NO_AUDIO,
        FailureCode.WS_DROP,
        FailureCode.TWILIO_API,
    }
)


def counts_against_slo(code: FailureCode | str) -> bool:
    """True when this failure burns error budget."""
    try:
        parsed = FailureCode(str(code))
    except ValueError:
        return True  # an unknown code is assumed to be ours until proven otherwise
    return parsed is not FailureCode.NONE and parsed not in EXCLUDED_FROM_SLO


# Human-readable, one line each — used by the digest, the dashboard, and the
# runbook index so the same words appear everywhere an operator looks.
DESCRIPTIONS: dict[FailureCode, str] = {
    FailureCode.NONE: "Completed normally",
    FailureCode.NO_ANSWER: "Nobody picked up",
    FailureCode.BUSY: "Line was busy",
    FailureCode.VOICEMAIL: "Answering machine detected",
    FailureCode.WRONG_NUMBER: "Callee said we had the wrong number",
    FailureCode.CALLEE_HANGUP: "Callee hung up mid-task",
    FailureCode.SILENT_CALLEE: "Call connected but the callee never spoke",
    FailureCode.WS_DROP: "ConversationRelay / Media Streams WebSocket dropped mid-call",
    FailureCode.TWILIO_API: "Twilio REST API rejected or failed the request",
    FailureCode.TWIML_ERROR: "Twilio could not execute our TwiML (fallback handler fired)",
    FailureCode.CALL_SETUP: "Call could not be placed at all",
    FailureCode.BRIEFING_LOST: "Outbound call refused: the task it was given could not be recovered",
    FailureCode.STT_ERROR: "Speech recognition failed or returned nothing usable",
    FailureCode.TTS_ERROR: "Speech synthesis failed (voice unusable or provider error)",
    FailureCode.NO_AUDIO: "Agent turns were produced but never reached the caller",
    FailureCode.LLM_ERROR: "The model failed repeatedly during the call",
    FailureCode.TOOL_ERROR: "A tool the call depended on failed",
    FailureCode.LANGUAGE_DRIFT: "The agent answered in the wrong language and could not recover",
    FailureCode.PHASE_TIMEOUT: "A conversation phase timed out and the call was ended politely",
    FailureCode.BUDGET: "Daily spend limit reached",
    FailureCode.INJECTION_BLOCKED: "Call ended after the callee attempted instruction override / extraction",
    FailureCode.ABUSE_GATE: "Blocked by the outbound abuse gate (daily cap or target cooldown)",
    FailureCode.DO_NOT_CALL: "Target is on the do-not-call list",
    FailureCode.QUIET_HOURS: "Blocked by quiet hours",
    FailureCode.BLOCKED: "Inbound caller is on the blocklist (declined with one sentence)",
    FailureCode.BUSY_CAPACITY: "Inbound call declined: concurrent-call capacity reached",
    FailureCode.SHUTDOWN: "Call ended by a deploy/restart drain",
    FailureCode.STUCK: "Call exceeded max duration and had to be reaped",
    FailureCode.INTERNAL: "Unhandled internal error",
    FailureCode.UNKNOWN: "Failed for a reason we do not yet classify",
}


def describe(code: FailureCode | str) -> str:
    try:
        return DESCRIPTIONS[FailureCode(str(code))]
    except (ValueError, KeyError):
        return f"Unclassified failure ({code})"


# Ordered most-specific-first: the first pattern that matches a state-machine
# `reason` (or an exception string) wins. Order matters — "timeout_ringing" must
# reach NO_ANSWER before the generic "timeout" reaches PHASE_TIMEOUT.
_REASON_PATTERNS: tuple[tuple[re.Pattern[str], FailureCode], ...] = (
    (re.compile(r"timeout_ringing|no[-_ ]?answer"), FailureCode.NO_ANSWER),
    (re.compile(r"\bbusy\b"), FailureCode.BUSY),
    (re.compile(r"voicemail|answering[-_ ]?machine|\bamd\b"), FailureCode.VOICEMAIL),
    (re.compile(r"wrong[-_ ]?number"), FailureCode.WRONG_NUMBER),
    (re.compile(r"shutdown|drain|sigterm"), FailureCode.SHUTDOWN),
    (re.compile(r"stuck|max[-_ ]?duration|reaped"), FailureCode.STUCK),
    (re.compile(r"injection|jailbreak|extraction[-_ ]?attempt"), FailureCode.INJECTION_BLOCKED),
    (re.compile(r"do[-_ ]?not[-_ ]?call"), FailureCode.DO_NOT_CALL),
    (re.compile(r"busy[-_ ]?capacity"), FailureCode.BUSY_CAPACITY),
    (re.compile(r"blocklist|\bblocked\b"), FailureCode.BLOCKED),
    (re.compile(r"quiet[-_ ]?hours"), FailureCode.QUIET_HOURS),
    (re.compile(r"abuse|cooldown|daily[-_ ]?(?:call[-_ ]?)?limit"), FailureCode.ABUSE_GATE),
    (re.compile(r"budget|spend[-_ ]?limit"), FailureCode.BUDGET),
    (re.compile(r"repeated_errors|llm|model[-_ ]?error|brain"), FailureCode.LLM_ERROR),
    (re.compile(r"tool[-_ ]?(?:error|fail)"), FailureCode.TOOL_ERROR),
    (re.compile(r"language[-_ ]?drift"), FailureCode.LANGUAGE_DRIFT),
    (re.compile(r"\btts\b|64111|converting tokens to speech|synthes"), FailureCode.TTS_ERROR),
    (re.compile(r"\bstt\b|transcri|deepgram"), FailureCode.STT_ERROR),
    (re.compile(r"no[-_ ]?audio|undelivered|silent[-_ ]?agent"), FailureCode.NO_AUDIO),
    (re.compile(r"websocket|ws[-_ ]?(?:drop|close|disconnect)|1006|64107"), FailureCode.WS_DROP),
    (re.compile(r"64101|twiml"), FailureCode.TWIML_ERROR),
    (re.compile(r"twilio"), FailureCode.TWILIO_API),
    (re.compile(r"call[-_ ]?setup|could not be placed|\bfailed\b|canceled|cancelled"), FailureCode.CALL_SETUP),
    (re.compile(r"timeout_(?:greeting|outbound_greeting)"), FailureCode.SILENT_CALLEE),
    (re.compile(r"timeout"), FailureCode.PHASE_TIMEOUT),
    (re.compile(r"call_end_cleanup|hangup"), FailureCode.CALLEE_HANGUP),
)


def classify_failure(reason: str, *, completed: bool = False) -> FailureCode:
    """Map a state-machine reason / error string onto a stable failure code.

    Called at exactly one place per call (`_handle_call_end`) so the code that
    lands in the database, the metric label, and the digest are the same value.
    """
    if completed:
        return FailureCode.NONE
    text = (reason or "").strip().lower()
    if not text:
        return FailureCode.UNKNOWN
    for pattern, code in _REASON_PATTERNS:
        if pattern.search(text):
            return code
    return FailureCode.UNKNOWN

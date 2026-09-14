"""
Call outcome categories and the denominators built from them.

"Failure rate" is meaningless until you say which failures and out of what. A
callee who was on another line is not a defect; an inbound call declined by the
capacity policy is the system working. Folding either into a technical failure
rate makes the number move for reasons no deploy can change, and an alert that
fires on holidays gets muted.

This module reuses the existing `FailureCode` taxonomy
(`pincer/observability/failure_codes.py`) rather than inventing a second one,
and maps it onto the four categories the telephony dashboard reports.
"""

from __future__ import annotations

from enum import StrEnum

from pincer.observability.failure_codes import CALLEE_UNREACHABLE, POLICY_BLOCKED, FailureCode


class FailureCategory(StrEnum):
    #: The call did what it was supposed to.
    NONE = "none"
    #: Ours. Burns error budget, pages people, belongs on the technical failure rate.
    TECHNICAL = "technical"
    #: The human was not reachable. Healthy system, unavailable person.
    CALLEE_UNAVAILABLE = "callee_unavailable"
    #: A guardrail said no before or during the call. The system worked.
    POLICY_DECLINED = "policy_declined"
    #: A party hung up. Normal in conversation phases; see `unexpected_disconnect`.
    ENDED_BY_PARTY = "ended_by_party"
    #: We could not classify it — counted as technical until proven otherwise.
    UNKNOWN = "unknown"


#: Terminations that indicate the media path broke under us rather than someone
#: choosing to end the call. This is the numerator of the unexpected-disconnect
#: rate, and it is deliberately narrow: a caller saying goodbye and hanging up
#: is not a disconnect.
UNEXPECTED_DISCONNECT: frozenset[FailureCode] = frozenset(
    {
        FailureCode.WS_DROP,
        FailureCode.NO_AUDIO,
        FailureCode.STUCK,
        FailureCode.TWILIO_API,
        FailureCode.TWIML_ERROR,
        FailureCode.CALL_SETUP,
    }
)

_ENDED_BY_PARTY: frozenset[FailureCode] = frozenset(
    {
        FailureCode.CALLEE_HANGUP,
        FailureCode.SILENT_CALLEE,
    }
)


def categorise(code: FailureCode | str | None) -> FailureCategory:
    """Map a failure code onto its reporting category."""
    if code in (None, ""):
        return FailureCategory.UNKNOWN
    try:
        parsed = FailureCode(str(code))
    except ValueError:
        return FailureCategory.UNKNOWN
    if parsed is FailureCode.NONE:
        return FailureCategory.NONE
    if parsed in CALLEE_UNREACHABLE:
        return FailureCategory.CALLEE_UNAVAILABLE
    if parsed in POLICY_BLOCKED:
        return FailureCategory.POLICY_DECLINED
    if parsed in _ENDED_BY_PARTY:
        return FailureCategory.ENDED_BY_PARTY
    return FailureCategory.TECHNICAL


def is_unexpected_disconnect(code: FailureCode | str | None) -> bool:
    try:
        return FailureCode(str(code)) in UNEXPECTED_DISCONNECT
    except (ValueError, TypeError):
        return False


#: Rendered in the UI next to each rate so nobody has to guess the denominator.
DENOMINATORS: dict[str, str] = {
    "connection_rate": (
        "connected ÷ attempted, where attempted excludes calls a policy declined before dialling "
        "(do-not-call, quiet hours, blocklist, capacity) — those were never attempts."
    ),
    "technical_failure_rate": (
        "calls whose failure category is `technical` ÷ all terminated calls. Callee-unavailable, "
        "policy-declined and party-ended calls are in the denominator but never the numerator."
    ),
    "unexpected_disconnect_rate": (
        "calls terminated with ws_drop / no_audio / stuck / twilio_api / twiml_error / call_setup "
        "÷ calls that connected. A caller hanging up after goodbye is not counted."
    ),
    "answer_rate": "connected ÷ attempted, same denominator as connection rate; reported for outbound only.",
}

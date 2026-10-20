"""
Backfill failure codes for outbound calls that never reached a person.

Before calls recorded an explicit `end_code`, a busy / no-answer / failed /
canceled callback or an acted-on AMD verdict ended the call as `none` (or
`callee_hangup`, for a voicemail detected after relay setup). The provider told
us what happened, and those events are still on the telemetry timeline for as
long as it is retained, so the code can be re-derived from them.

Only the timeline is evidence. A candidate it says nothing about is left alone
and listed as undetermined, never guessed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from pincer.observability.failure_codes import EXCLUDED_FROM_SLO, NOT_CONNECTED_STATUS, FailureCode
from pincer.voice.telemetry import store
from pincer.voice.telemetry.outcomes import categorise
from pincer.voice.telemetry.schema import EventName

#: Stored codes an unanswered call could wrongly carry. Anything else was
#: classified from real evidence and is not second-guessed.
RECLASSIFIABLE = ("none", "callee_hangup", "")

_EVIDENCE = (str(EventName.PROVIDER_STATUS), str(EventName.AMD_VERDICT))


@dataclass(frozen=True, slots=True)
class Change:
    call_sid: str
    started_at: str
    old: str
    new: str
    evidence: str
    call_id: str = ""


@dataclass(slots=True)
class Plan:
    since: str
    changes: list[Change] = field(default_factory=list)
    #: Candidates whose timeline proves nothing: (call_sid, started_at, failure_code).
    undetermined: list[tuple[str, str, str]] = field(default_factory=list)
    #: Success rate and call-attempt SLI over the window, before and after the changes.
    rates: dict[str, dict[str, float | None]] = field(default_factory=dict)
    telemetry_missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "changes": [asdict(c) for c in self.changes],
            "undetermined": [{"call_sid": s, "started_at": t, "failure_code": c} for s, t, c in self.undetermined],
            "rates": self.rates,
            "telemetry_missing": self.telemetry_missing,
        }


def derive(events: list[dict[str, Any]], turn_count: int) -> tuple[FailureCode, str] | None:
    """The failure code a call's timeline proves, with the evidence; None if it proves none.

    `events` are the call's provider-status and AMD events in timeline order.
    """
    not_connected = [
        status
        for e in events
        if e["name"] == str(EventName.PROVIDER_STATUS)
        and (status := str(e["attributes"].get("status", ""))) in NOT_CONNECTED_STATUS
    ]
    if not_connected:
        return NOT_CONNECTED_STATUS[not_connected[-1]], f"provider status {not_connected[-1]}"

    for event in events:
        if event["name"] != str(EventName.AMD_VERDICT):
            continue
        answered_by = str(event["attributes"].get("answered_by", ""))
        # A turn means someone spoke to us: the verdict may have been ignored
        # as a false positive, and the timeline cannot say which.
        if (answered_by.startswith("machine") or answered_by == "fax") and turn_count == 0:
            return FailureCode.VOICEMAIL, f"AMD {answered_by}, no turns"
    return None


def _rates(codes: list[str]) -> dict[str, float | None]:
    excluded = {str(c) for c in EXCLUDED_FROM_SLO}
    completed = sum(1 for c in codes if c in ("none", ""))
    eligible = sum(1 for c in codes if c not in excluded)
    return {
        "call_success_rate": completed / len(codes) if codes else None,
        "call_attempt_success_rate": completed / eligible if eligible else None,
        "terminated": float(len(codes)),
    }


async def plan(url: str, since: str) -> Plan:
    """What a reclassification since `since` would change. Writes nothing."""
    from pincer.services.telephony_reclassify import TelephonyReclassifyService
    from pincer.services.voice import CallsService

    result = Plan(since=since)
    found = await TelephonyReclassifyService(url).evidence(since, RECLASSIFIABLE, _EVIDENCE)
    if found is None:
        result.telemetry_missing = True
        return result
    candidates, events, turns = found

    by_call: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        event["attributes"] = store.loads(event.get("attributes"), {})
        by_call.setdefault(str(event["call_id"]), []).append(event)

    for row in candidates:
        call_sid = str(row["call_sid"])
        call_id = str(row["call_id"] or "")
        old = str(row["failure_code"] or "")
        started = str(row["started_at"] or "")
        derived = derive(by_call.get(call_id, []), turns.get(call_id, 0)) if call_id else None
        if derived is None:
            result.undetermined.append((call_sid, started, old))
        elif str(derived[0]) != old:
            result.changes.append(Change(call_sid, started, old, str(derived[0]), derived[1], call_id))

    window = await CallsService(url).terminated_between(since)
    corrected = {c.call_sid: c.new for c in result.changes}
    before = [str(r["failure_code"] or "") for r in window]
    after = [corrected.get(str(r["call_sid"]), str(r["failure_code"] or "")) for r in window]
    result.rates = {"before": _rates(before), "after": _rates(after)}
    return result


async def apply(url: str, result: Plan) -> None:
    """Write `result`'s changes to both tables and audit it, in one transaction."""
    from pincer.security.audit import AuditAction, AuditEntry
    from pincer.services.telephony_reclassify import TelephonyReclassifyService

    if not result.changes:
        return
    changes = [
        {
            "call_sid": c.call_sid,
            "failure_code": c.new,
            "call_id": c.call_id,
            "telemetry": {
                "status": "failed",
                "outcome": "failed",
                "failure_code": c.new,
                "failure_category": str(categorise(c.new)),
            },
        }
        for c in result.changes
    ]
    audit = AuditEntry(
        user_id="cli",
        action=AuditAction.TELEPHONY_RECLASSIFY,
        channel="cli",
        metadata={
            "since": result.since,
            "changed": len(result.changes),
            "by_code": dict(Counter(c.new for c in result.changes)),
            "undetermined": len(result.undetermined),
        },
    )
    await TelephonyReclassifyService(url).apply(changes, audit)

"""Backfill of call failure codes from the telephony timeline.

Reads the outbound calls whose stored code may be wrong and the provider events
that say what really happened; writes the corrections to `voice_calls` and the
telemetry call row, with one audit row, in a single transaction. Deciding what
the evidence means is the caller's job (`pincer.voice.telemetry.reclassify`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.repositories.audit import AuditLogRepository
from pincer.repositories.telemetry import TelemetryCallRepository
from pincer.repositories.telemetry_reads import TelemetryReads
from pincer.repositories.voice import CallRepository
from pincer.services.audit import to_row
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pincer.security.audit import AuditEntry

#: Ids per `IN (…)`: far below every dialect's bound-parameter limit.
_CHUNK = 500


class TelephonyReclassifyService(DatabaseService):
    async def evidence(
        self, since: str, codes: Sequence[str], names: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """(candidates, their events); None while the telemetry tables do not exist."""
        async with get_engine(self._url).connect() as conn:
            reads = TelemetryReads(conn)
            if not await reads.tables_present():
                return None
            candidates = await reads.reclassify_candidates(since, codes)
            call_ids = [str(row["call_id"]) for row in candidates if row["call_id"]]
            events: list[dict[str, Any]] = []
            for start in range(0, len(call_ids), _CHUNK):
                events.extend(await reads.events_for_calls(call_ids[start : start + _CHUNK], names))
        return candidates, events

    async def apply(self, changes: Sequence[Mapping[str, Any]], audit_entry: AuditEntry) -> None:
        """Write every change and the audit row, or none of them.

        Each change carries `call_sid`, `failure_code`, and — when telemetry has
        a row for the call — `call_id` plus the `telemetry` fields to overwrite.
        """
        async with session_scope(self._url) as session:
            calls = CallRepository(session)
            telemetry = TelemetryCallRepository(session)
            for change in changes:
                await calls.set_fields(str(change["call_sid"]), {"failure_code": change["failure_code"]})
                if change.get("call_id"):
                    await telemetry.correct(str(change["call_id"]), dict(change["telemetry"]))
            await AuditLogRepository(session).add_many([to_row(audit_entry)])

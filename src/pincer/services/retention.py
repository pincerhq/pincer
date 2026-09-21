"""Deleting expired voice data — GDPR storage limitation (Art. 5(1)(e)).

The maps here are the policy: which rows expire, and by which timestamp. What
is deliberately absent matters as much as what is present, so the reasons live
next to `pincer.voice.retention`'s maps, which these mirror.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.session import session_scope
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.models.voice import CallAction, InboundMessage, OutboundCallLog, VoiceCall
from pincer.models.voice import CallTranscript as CallTranscriptModel
from pincer.repositories.base import repository_for
from pincer.repositories.voice import AnalyticsRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Voice rows that expire with the transcript window, and the column that
#: dates them. The table name is the key each count is reported under.
VOICE_TABLES: Sequence[tuple[Any, Any]] = (
    (VoiceCall, VoiceCall.started_at),
    (CallTranscriptModel, CallTranscriptModel.timestamp),
    (CallAction, CallAction.timestamp),
    (InboundMessage, InboundMessage.created_at),
    (OutboundCallLog, OutboundCallLog.placed_at),
)

#: Telephony telemetry keeps its own, longer window: it is technical data
#: (stage timings, failure codes, masked numbers) with no transcript in it.
#: Child rows go before the call row, so an interrupted purge leaves orphaned
#: events rather than a call whose detail pages are empty.
TELEMETRY_TABLES: Sequence[tuple[Any, Any]] = (
    (TelephonyEvent, TelephonyEvent.ts_utc),
    (TelephonySpan, TelephonySpan.start_utc),
    (TelephonyTurn, TelephonyTurn.created_at),
    (TelephonyCall, TelephonyCall.registered_at),
)


class RetentionService(DatabaseService):
    async def purge_voice(self, cutoff: str) -> dict[str, int]:
        """Delete expired voice rows and redact what survives them."""
        async with session_scope(self._url) as session:
            # Redact before deleting: the analytics row survives, but the one
            # field that can quote the call does not. Keeping a grounded
            # rationale ("said the third delay was unacceptable") after its
            # transcript is gone would preserve exactly the content the purge
            # exists to remove — and once the call row is deleted, nothing is
            # left to tell us the rationale belonged to an expired call.
            redacted = await AnalyticsRepository(session).redact_rationales_older_than(cutoff)
            deleted = await _delete_older(session, VOICE_TABLES, cutoff)
        if redacted:
            deleted["call_analytics.sentiment_rationale"] = redacted
        return deleted

    async def purge_telemetry(self, cutoff: str) -> dict[str, int]:
        async with session_scope(self._url) as session:
            return await _delete_older(session, TELEMETRY_TABLES, cutoff)


async def _delete_older(session: Any, tables: Sequence[tuple[Any, Any]], cutoff: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for model, timestamp in tables:
        removed = await repository_for(model)(session).delete_older_than(timestamp, cutoff)
        if removed:
            deleted[str(model.__tablename__)] = removed
    return deleted

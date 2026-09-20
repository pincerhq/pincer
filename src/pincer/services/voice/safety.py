"""The dialling gate's records: objections and the dial log."""

from __future__ import annotations

from typing import Any

from pincer.db.session import session_scope
from pincer.models.voice import OutboundCallLog
from pincer.repositories.voice import DoNotCallRepository, OutboundCallLogRepository
from pincer.services.base import DatabaseService


class SafetyGateService(DatabaseService):
    async def is_do_not_call(self, phone_number: str) -> bool:
        async with session_scope(self._url) as session:
            return await DoNotCallRepository(session).contains(phone_number)

    async def add_do_not_call(self, values: dict[str, str]) -> bool:
        """Record an objection. Returns whether it was new."""
        async with session_scope(self._url) as session:
            repo = DoNotCallRepository(session)
            already = await repo.contains(str(values["phone_number"]))
            await repo.add(values)
        return not already

    async def remove_do_not_call(self, phone_number: str) -> bool:
        async with session_scope(self._url) as session:
            return bool(await DoNotCallRepository(session).remove(phone_number))

    async def list_do_not_call(self) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await DoNotCallRepository(session).newest_first()
        return [
            {
                "phone_number": row.phone_number,
                "reason": row.reason,
                "source": row.source,
                "call_sid": row.call_sid,
                "added_at": row.added_at,
            }
            for row in rows
        ]

    async def log_outbound(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await OutboundCallLogRepository(session).add(OutboundCallLog(**values), refresh=False)

    async def calls_today(self, local_day: str) -> int:
        async with session_scope(self._url) as session:
            return await OutboundCallLogRepository(session).count_for_day(local_day)

    async def placed_since(self, phone_number: str, cutoff: str) -> list[str]:
        async with session_scope(self._url) as session:
            return list(await OutboundCallLogRepository(session).placed_since(phone_number, cutoff))

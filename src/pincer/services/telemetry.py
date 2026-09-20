"""Telemetry persistence.

Every write here runs behind the audio path: the recorder queues records and a
background task drains them (`pincer.voice.telemetry.tracer`), which is what
keeps a slow database from being heard. Nothing in this module may be awaited
from the audio loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.engine import get_engine
from pincer.repositories.telemetry import TelemetryStatements
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class TelemetryService(DatabaseService):
    #: One connection, reused. Telemetry is a single writer per process, and a
    #: connection (and so a thread) per write costs milliseconds of audio-loop
    #: delay at 25 concurrent calls — the overhead test measures it.
    pooled = True

    def __init__(self, url: str | None = None) -> None:
        super().__init__(url)
        self._sql = TelemetryStatements()

    async def _run(self, statement: Any, params: Any) -> None:
        """Execute on a connection, without an ORM session.

        A session would buy an identity map for rows nobody reads back, and
        pay for it in loop time behind the audio path.
        """
        engine = get_engine(self._url, pooled=self.pooled)
        async with engine.begin() as conn:
            await conn.execute(statement, params)

    async def write_records(self, events: Sequence[dict[str, Any]], spans: Sequence[dict[str, Any]]) -> None:
        """One batch of timeline records: events and closed spans together."""
        if not events and not spans:
            return
        engine = get_engine(self._url, pooled=self.pooled)
        async with engine.begin() as conn:
            if events:
                await conn.execute(self._sql.events(), list(events))
            if spans:
                await conn.execute(self._sql.spans(), list(spans))

    async def upsert_call(self, call_id: str, known: dict[str, Any]) -> None:
        if not known:
            return
        await self._run(self._sql.call(known), {"call_id": call_id, **known})

    async def upsert_turn(self, turn_id: str, known: dict[str, Any]) -> None:
        if not known:
            return
        await self._run(self._sql.turn(known), {"turn_id": turn_id, **known})

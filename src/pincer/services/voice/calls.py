"""Call rows, transcripts and actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.session import session_scope
from pincer.models.voice import CallAction, CallTranscript
from pincer.repositories.voice import (
    CallActionRepository,
    CallRepository,
    ThreadMemberRepository,
    TranscriptRepository,
)
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class CallsService(DatabaseService):
    async def save_call(self, values: dict[str, Any], *, thread_columns_from_members: bool = False) -> None:
        """Write the call row.

        With `thread_columns_from_members`, the thread columns are re-derived
        from `call_thread_members` afterwards — the durable membership record —
        and the member row's start date and direction are backfilled from this
        call, so a purged call still shows a date in its thread.
        """
        async with session_scope(self._url) as session:
            calls = CallRepository(session)
            await calls.save(values)
            if thread_columns_from_members:
                members = ThreadMemberRepository(session)
                call_sid = str(values["call_sid"])
                membership = await members.for_call(call_sid)
                await calls.set_fields(
                    call_sid,
                    {
                        "thread_id": membership.thread_id if membership else "",
                        "thread_attach_kind": membership.attach_kind if membership else "",
                    },
                )
                if membership is not None:
                    await members.backfill_call_facts(
                        call_sid,
                        call_started_at=str(values.get("started_at") or ""),
                        direction=str(values.get("direction") or ""),
                    )

    async def set_fields(self, call_sid: str, values: dict[str, Any]) -> int:
        async with session_scope(self._url) as session:
            return await CallRepository(session).set_fields(call_sid, values)

    async def get(self, call_sid: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await CallRepository(session).by_sid(call_sid)
        return None if row is None else _as_dict(row)

    async def add_transcript_lines(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        async with session_scope(self._url) as session:
            await TranscriptRepository(session).add_many([CallTranscript(**row) for row in rows])

    async def add_actions(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        async with session_scope(self._url) as session:
            await CallActionRepository(session).add_many([CallAction(**row) for row in rows])

    async def transcript_for(self, call_id: str) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await TranscriptRepository(session).for_call(call_id)
        return [_as_dict(row) for row in rows]

    async def actions_for(self, call_id: str) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await CallActionRepository(session).for_call(call_id)
        return [_as_dict(row) for row in rows]


def _as_dict(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in row.__class__.model_fields}

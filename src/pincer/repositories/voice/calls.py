"""The call log and what was said on it: `voice_calls`, `call_transcripts`, `call_actions`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.db.ids import is_id
from pincer.models.voice import CallAction, CallThread, CallTranscript, VoiceCall
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class CallRepository(BaseRepository[VoiceCall, str]):
    model = VoiceCall

    async def save(self, values: dict[str, Any]) -> None:
        """Insert the call row, or update the columns this caller knows about.

        Keyed on `call_sid`. Unlike the `INSERT OR REPLACE` this replaces, a
        column the caller does not pass keeps its stored value instead of
        being reset — which is what the thread columns needed patching back up
        after (see `pincer.services.voice.calls`).
        """
        stored = dict(values)
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                VoiceCall,
                stored,
                index_elements=["call_sid"],
                set_=lambda excluded: {name: excluded[name] for name in stored if name != "call_sid"},
            )
        )

    async def by_sid(self, call_sid: str) -> VoiceCall | None:
        stmt = select(VoiceCall).where(col(VoiceCall.call_sid) == call_sid)
        return (await self.session.exec(stmt)).first()

    async def reassign_thread(self, from_thread_id: str, to_thread_id: str, *, attach_kind: str) -> int:
        """Repoint the calls of one thread at another, and only those."""
        stmt = (
            update(VoiceCall)
            .where(col(VoiceCall.thread_id) == from_thread_id)
            .values(thread_id=to_thread_id, thread_attach_kind=attach_kind)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def set_fields(self, call_sid: str, values: dict[str, Any]) -> int:
        stmt = update(VoiceCall).where(col(VoiceCall.call_sid) == call_sid).values(**values)
        return int((await self.session.exec(stmt)).rowcount)

    async def newest_for_user(self, pincer_user_id: str, limit: int = 1) -> Sequence[VoiceCall]:
        return await self.list(
            col(VoiceCall.pincer_user_id) == pincer_user_id,
            order_by=[col(VoiceCall.started_at).desc()],
            limit=limit,
        )

    async def owned_by(self, call_sid: str, pincer_user_id: str) -> VoiceCall | None:
        """The call, but only if it belongs to this user."""
        stmt = select(VoiceCall).where(
            col(VoiceCall.call_sid) == call_sid, col(VoiceCall.pincer_user_id) == pincer_user_id
        )
        return (await self.session.exec(stmt)).first()

    async def terminated_between(
        self,
        start: str,
        end: str | None = None,
        *,
        language: str | None = None,
        only_failures: bool = False,
        failure_code: str | None = None,
    ) -> Sequence[VoiceCall]:
        """Calls that ended, started in `[start, end)`, newest first.

        A call still in progress has no outcome to report on, so every
        reporting read starts from "it ended".
        """
        where = [col(VoiceCall.ended_at).isnot(None), col(VoiceCall.started_at) >= start]
        if end:
            where.append(col(VoiceCall.started_at) < end)
        if language:
            where.append(col(VoiceCall.language).like(f"{language}%"))
        if only_failures:
            where.append(col(VoiceCall.failure_code).notin_(["none", ""]))
        if failure_code is not None:
            where.append(col(VoiceCall.failure_code) == failure_code)
        return await self.list(*where, order_by=[col(VoiceCall.started_at).desc()])

    async def page_with_thread(
        self,
        *,
        direction: str | None = None,
        completed: bool | None = None,
        thread_id: str | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[tuple[VoiceCall, str | None]]:
        """A page of calls with their thread's subject, newest first."""
        where = []
        if direction:
            where.append(col(VoiceCall.direction) == direction)
        if completed is not None:
            where.append(col(VoiceCall.ended_at).isnot(None) if completed else col(VoiceCall.ended_at).is_(None))
        if thread_id:
            # A thread id that could never exist has no calls, rather than
            # failing to bind: it comes straight from a query parameter.
            if not is_id(thread_id):
                return []
            where.append(col(VoiceCall.thread_id) == thread_id)
        stmt = (
            select(VoiceCall, CallThread.subject)
            .outerjoin(CallThread, col(CallThread.thread_id) == col(VoiceCall.thread_id))
            .where(*where)
            .order_by(col(VoiceCall.started_at).desc())
            .limit(limit)
            .offset(offset)
        )
        return [(call, subject) for call, subject in (await self.session.exec(stmt)).all()]

    async def with_thread(self, call_sid: str) -> tuple[VoiceCall, str | None] | None:
        stmt = (
            select(VoiceCall, CallThread.subject)
            .outerjoin(CallThread, col(CallThread.thread_id) == col(VoiceCall.thread_id))
            .where(col(VoiceCall.call_sid) == call_sid)
        )
        return (await self.session.exec(stmt)).first()

    async def started_since(self, cutoff: str, *, failure_code: str | None = None) -> Sequence[VoiceCall]:
        where = [col(VoiceCall.started_at) >= cutoff]
        if failure_code is not None:
            where.append(col(VoiceCall.failure_code) == failure_code)
        return await self.list(*where, order_by=[col(VoiceCall.started_at).desc()])

    async def count_since(self, cutoff: str, *, failure_code: str | None = None) -> int:
        where = [col(VoiceCall.started_at) >= cutoff]
        if failure_code is not None:
            where.append(col(VoiceCall.failure_code) == failure_code)
        stmt = select(func.count()).select_from(VoiceCall).where(*where)
        return int((await self.session.exec(stmt)).one())

    async def reported_since(self, cutoff: str) -> Sequence[tuple[str | None, str | None]]:
        """(ended_at, report_delivered_at) for calls whose report went out."""
        stmt = select(VoiceCall.ended_at, VoiceCall.report_delivered_at).where(
            col(VoiceCall.ended_at).isnot(None),
            col(VoiceCall.report_delivered_at).isnot(None),
            col(VoiceCall.started_at) >= cutoff,
        )
        return [(ended, delivered) for ended, delivered in (await self.session.exec(stmt)).all()]


class TranscriptRepository(BaseRepository[CallTranscript, str]):
    model = CallTranscript

    async def add_many(self, rows: Sequence[CallTranscript]) -> None:
        self.session.add_all(list(rows))
        await self.session.flush()

    async def for_call(
        self, call_id: str, *, limit: int | None = None, final_only: bool = False
    ) -> Sequence[CallTranscript]:
        where = [col(CallTranscript.call_id) == call_id]
        if final_only:
            where.append(col(CallTranscript.is_final).is_(True))
        # `id` breaks a timestamp tie, and utterances inside one second are
        # the normal case. That still works now the key is a UUIDv7: the
        # canonical form is fixed-width lowercase hex, so ordering it as text
        # is ordering the uuid by its bytes, which for a v7 is ordering by
        # time — and rows written in the same millisecond are separated by the
        # generator's counter. Migration 0017 gave the pre-existing rows ids
        # carrying their own timestamps, so history sorts the same way.
        return await self.list(*where, order_by=[col(CallTranscript.timestamp), col(CallTranscript.id)], limit=limit)


class CallActionRepository(BaseRepository[CallAction, str]):
    model = CallAction

    async def add_many(self, rows: Sequence[CallAction]) -> None:
        self.session.add_all(list(rows))
        await self.session.flush()

    async def for_call(self, call_id: str) -> Sequence[CallAction]:
        return await self.list(col(CallAction.call_id) == call_id, order_by=[col(CallAction.timestamp)])

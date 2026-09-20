"""Call threads and their members: `call_threads`, `call_thread_members`.

A member row is the durable record that a call belonged to a thread: it
outlives the call itself, so a purged call still shows in its thread as a stub
(sid, date, outcome) with no transcript.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.voice import CallThread, CallThreadMember, VoiceCall
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class ThreadRepository(BaseRepository[CallThread, str]):
    model = CallThread

    async def set_fields(self, thread_id: str, values: dict[str, Any]) -> int:
        stmt = update(CallThread).where(col(CallThread.thread_id) == thread_id).values(**values)
        return int((await self.session.exec(stmt)).rowcount)

    async def for_number(
        self, primary_number: str, status: str, updated_since: str, limit: int
    ) -> Sequence[CallThread]:
        """Threads for a number in a status, most recently active first.

        More than one is the caller's problem to refuse: an ambiguous match
        must not be guessed at.
        """
        stmt = (
            select(CallThread)
            .where(
                col(CallThread.primary_number) == primary_number,
                col(CallThread.status) == status,
                col(CallThread.updated_at) >= updated_since,
            )
            .order_by(col(CallThread.updated_at).desc())
            .limit(limit)
        )
        return (await self.session.exec(stmt)).all()

    async def stale_ids(self, status: str, older_than: str) -> Sequence[str]:
        stmt = select(CallThread.thread_id).where(
            col(CallThread.status) != status, col(CallThread.updated_at) < older_than
        )
        return (await self.session.exec(stmt)).all()

    async def search(
        self,
        *,
        statuses: Sequence[str] = (),
        query: str = "",
        limit: int,
        offset: int = 0,
    ) -> Sequence[CallThread]:
        """Threads by most recent activity, newest first."""
        stmt = select(CallThread)
        if statuses:
            stmt = stmt.where(col(CallThread.status).in_(list(statuses)))
        if query:
            like = f"%{query}%"
            stmt = stmt.where(
                col(CallThread.subject).like(like)
                | col(CallThread.contact_name).like(like)
                | col(CallThread.primary_number).like(like)
            )
        stmt = stmt.order_by(col(CallThread.updated_at).desc()).limit(limit).offset(offset)
        return (await self.session.exec(stmt)).all()


class ThreadMemberRepository(BaseRepository[CallThreadMember, str]):
    model = CallThreadMember

    async def for_call(self, call_sid: str) -> CallThreadMember | None:
        stmt = select(CallThreadMember).where(col(CallThreadMember.call_sid) == call_sid)
        return (await self.session.exec(stmt)).first()

    async def for_thread(self, thread_id: str) -> Sequence[CallThreadMember]:
        """A thread's calls, oldest first; a member with no call date sorts last."""
        stmt = (
            select(CallThreadMember)
            .where(col(CallThreadMember.thread_id) == thread_id)
            .order_by(col(CallThreadMember.call_started_at), col(CallThreadMember.attached_at))
        )
        return (await self.session.exec(stmt)).all()

    async def attach(self, values: dict[str, Any]) -> None:
        """Attach a call to a thread; re-attaching moves it."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                CallThreadMember,
                values,
                index_elements=["call_sid"],
                set_=lambda excluded: {
                    "thread_id": excluded.thread_id,
                    "attach_kind": excluded.attach_kind,
                    "attached_at": excluded.attached_at,
                },
            )
        )

    async def detach(self, call_sid: str) -> int:
        return await self.delete_where(col(CallThreadMember.call_sid) == call_sid)

    async def backfill_call_facts(self, call_sid: str, *, call_started_at: str, direction: str) -> int:
        """Copy the call's date and direction onto its member row, which
        survives the call."""
        stmt = (
            update(CallThreadMember)
            .where(col(CallThreadMember.call_sid) == call_sid)
            .values(call_started_at=call_started_at, direction=direction)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def set_outcome(self, call_sid: str, *, outcome_code: str, task_result: str) -> int:
        stmt = (
            update(CallThreadMember)
            .where(col(CallThreadMember.call_sid) == call_sid)
            .values(outcome_code=outcome_code, task_result=task_result)
        )
        return int((await self.session.exec(stmt)).rowcount)

    async def with_calls(self, thread_id: str) -> Sequence[tuple[CallThreadMember, VoiceCall | None]]:
        """A thread's members, each with its call row when the call still exists.

        A member whose call has been purged comes back with `None` rather than
        being dropped: the thread keeps a stub for it (§5).

        Ordered by when the call happened, falling back to when it was
        attached, with `call_sid` breaking ties (SQLite's `rowid` has no
        Postgres equivalent). Sorted here rather than in SQL: the fallback
        needs an empty-string check, and `call_started_at` is `TEXT` on SQLite
        but `TIMESTAMP` on Postgres, where comparing it to `''` is an error.
        """
        stmt = (
            select(CallThreadMember, VoiceCall)
            .outerjoin(VoiceCall, col(VoiceCall.call_sid) == col(CallThreadMember.call_sid))
            .where(col(CallThreadMember.thread_id) == thread_id)
        )
        rows = [(member, call) for member, call in (await self.session.exec(stmt)).all()]
        return sorted(rows, key=lambda pair: (pair[0].call_started_at or pair[0].attached_at or "", pair[0].call_sid))

    async def move_thread(self, from_thread_id: str, to_thread_id: str, *, attach_kind: str, attached_at: str) -> int:
        stmt = (
            update(CallThreadMember)
            .where(col(CallThreadMember.thread_id) == from_thread_id)
            .values(thread_id=to_thread_id, attach_kind=attach_kind, attached_at=attached_at)
        )
        return int((await self.session.exec(stmt)).rowcount)

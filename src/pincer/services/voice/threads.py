"""Call threads: the rows behind `ThreadManager`.

`ThreadManager` owns the rules — what may attach to what, when a thread
reopens, what a subject may look like. This owns the rows, and keeps each
operation that touches both a thread and its members in one transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pincer.db.session import session_scope
from pincer.models.voice import CallThread
from pincer.repositories.voice import CallRepository, ThreadMemberRepository, ThreadRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence


class ThreadsService(DatabaseService):
    # ── reads ────────────────────────────────────────────────────────

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await ThreadRepository(session).get(thread_id)
        return None if row is None else _as_dict(row)

    async def search(
        self, *, statuses: Sequence[str] = (), query: str = "", limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ThreadRepository(session).search(statuses=statuses, query=query, limit=limit, offset=offset)
        return [_as_dict(row) for row in rows]

    async def thread_for_call(self, call_sid: str) -> str:
        async with session_scope(self._url) as session:
            member = await ThreadMemberRepository(session).for_call(call_sid)
        return "" if member is None else str(member.thread_id or "")

    async def calls(self, thread_id: str) -> list[dict[str, Any]]:
        """A thread's calls, each marked `purged` when only the member row is left."""
        async with session_scope(self._url) as session:
            pairs = await ThreadMemberRepository(session).with_calls(thread_id)
        return [
            {
                "call_sid": member.call_sid,
                "thread_id": member.thread_id,
                "attach_kind": member.attach_kind or "",
                "attached_at": member.attached_at or "",
                "started_at": (call.started_at if call else member.call_started_at) or "",
                "ended_at": call.ended_at if call else None,
                "direction": ((call.direction if call else member.direction) or ""),
                "outcome_code": member.outcome_code or "",
                "task_result": member.task_result or "",
                "failure_code": (call.failure_code or "") if call else "",
                "purged": call is None,
            }
            for member, call in pairs
        ]

    async def stale_ids(self, *, not_status: str, older_than: str) -> list[str]:
        async with session_scope(self._url) as session:
            return list(await ThreadRepository(session).stale_ids(not_status, older_than))

    async def for_number(
        self, primary_number: str, *, status: str, updated_since: str, limit: int
    ) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ThreadRepository(session).for_number(primary_number, status, updated_since, limit)
        return [_as_dict(row) for row in rows]

    # ── writes ───────────────────────────────────────────────────────

    async def create(self, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await ThreadRepository(session).add(CallThread(**values), refresh=False)

    async def set_fields(self, thread_id: str, values: dict[str, Any]) -> int:
        async with session_scope(self._url) as session:
            return await ThreadRepository(session).set_fields(thread_id, values)

    async def attach(self, values: dict[str, Any], *, touched_at: str) -> None:
        """Attach a call to a thread: the member row, the call's own columns
        and the thread's activity stamp, together."""
        call_sid, thread_id = str(values["call_sid"]), str(values["thread_id"])
        async with session_scope(self._url) as session:
            await ThreadMemberRepository(session).attach(values)
            await CallRepository(session).set_fields(
                call_sid, {"thread_id": thread_id, "thread_attach_kind": values.get("attach_kind", "")}
            )
            await ThreadRepository(session).set_fields(thread_id, {"updated_at": touched_at})

    async def detach(self, call_sid: str) -> None:
        async with session_scope(self._url) as session:
            await ThreadMemberRepository(session).detach(call_sid)
            await CallRepository(session).set_fields(call_sid, {"thread_id": "", "thread_attach_kind": ""})

    async def merge_into(self, source_thread_id: str, target_thread_id: str, *, attach_kind: str, stamp: str) -> None:
        """Move every call of one thread into another, in one transaction."""
        async with session_scope(self._url) as session:
            # The calls are repointed by their OLD thread id, so a call that
            # was already in the target keeps the attach kind it was attached
            # with — only the moved ones become `attach_kind`.
            await CallRepository(session).reassign_thread(source_thread_id, target_thread_id, attach_kind=attach_kind)
            await ThreadMemberRepository(session).move_thread(
                source_thread_id, target_thread_id, attach_kind=attach_kind, attached_at=stamp
            )
            await ThreadRepository(session).set_fields(target_thread_id, {"updated_at": stamp})

    async def record_outcome(self, call_sid: str, *, outcome_code: str, task_result: str) -> None:
        async with session_scope(self._url) as session:
            await ThreadMemberRepository(session).set_outcome(
                call_sid, outcome_code=outcome_code, task_result=task_result
            )

    async def update_summary(self, thread_id: str, values: dict[str, Any]) -> None:
        async with session_scope(self._url) as session:
            await ThreadRepository(session).set_fields(thread_id, values)


def _as_dict(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in row.__class__.model_fields}

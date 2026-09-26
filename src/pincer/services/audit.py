"""Audit-log persistence and queries.

Writes arrive in batches from `AuditLogger`'s queue — auditing must never
block the path being audited — so one unit of work covers a whole batch.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends

from pincer.db.session import session_scope
from pincer.models.audit import AuditLog
from pincer.repositories.audit import AuditLogRepository
from pincer.services.base import DatabaseService

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Longest input/output summary stored, in characters.
MAX_SUMMARY_LENGTH = 2000


class AuditService(DatabaseService):
    async def add_batch(self, entries: Sequence[Any]) -> None:
        """Persist a batch of `AuditEntry` objects in one transaction.

        An entry that cannot be converted at all is dropped with a log line
        rather than failing the batch: the caller re-queues a failed batch, so
        one unconvertible entry would otherwise block every later write.
        """
        rows = []
        for entry in entries:
            try:
                rows.append(_to_row(entry))
            except Exception:
                # `getattr`: the entry that failed may be the one without an
                # `action`, and re-raising here would fail the whole batch.
                logger.exception(
                    "Dropping an audit entry that could not be stored: action=%s",
                    getattr(entry, "action", "<unknown>"),
                )
        if not rows:
            return
        async with session_scope(self._url) as session:
            await AuditLogRepository(session).add_many(rows)

    async def query(
        self,
        user_id: str | None = None,
        action: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Matching entries, newest first."""
        async with session_scope(self._url) as session:
            repo = AuditLogRepository(session)
            rows = await repo.newest_first(
                repo.filters(user_id, action, tool, since, until), limit=limit, offset=offset
            )
            return [_as_dict(row) for row in rows]

    async def count(
        self,
        user_id: str | None = None,
        action: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        async with session_scope(self._url) as session:
            repo = AuditLogRepository(session)
            return await repo.count(repo.filters(user_id, action, tool, since, until))

    async def export_json(
        self,
        output_path: str | Path,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """Write matching entries to `output_path` oldest first. Returns the count.

        Rows are streamed, so the file may be far larger than memory. Each
        record's `metadata_json` is decoded back into a `metadata` object.
        """
        from pathlib import Path as _Path

        count = 0
        with open(_Path(output_path), "w") as handle:
            handle.write("[\n")
            async with session_scope(self._url) as session:
                repo = AuditLogRepository(session)
                async for row in repo.oldest_first(repo.filters(user_id, None, None, since, until)):
                    record = _as_dict(row)
                    if record.get("metadata_json"):
                        try:
                            record["metadata"] = json.loads(record.pop("metadata_json"))
                        except json.JSONDecodeError:
                            record["metadata"] = {}
                    if count:
                        handle.write(",\n")
                    handle.write(f"  {json.dumps(record, default=str)}")
                    count += 1
            handle.write("\n]")
        return count

    async def stats(self, since: str | None = None) -> dict[str, Any]:
        async with session_scope(self._url) as session:
            repo = AuditLogRepository(session)
            return await repo.stats(repo.filters(since=since))


def _as_dict(row: AuditLog) -> dict[str, Any]:
    return {name: getattr(row, name) for name in AuditLog.model_fields}


def _to_row(entry: Any) -> AuditLog:
    """An `AuditEntry` as its stored row: enum to value, summaries capped,
    booleans as the 0/1 the column holds, metadata as JSON text."""
    action = getattr(entry.action, "value", entry.action)
    return AuditLog(
        timestamp=entry.timestamp,
        user_id=entry.user_id,
        session_id=entry.session_id,
        action=action,
        tool=entry.tool,
        input_summary=(entry.input_summary or "")[:MAX_SUMMARY_LENGTH],
        output_summary=(entry.output_summary or "")[:MAX_SUMMARY_LENGTH],
        approved=1 if entry.approved else 0,
        cost_usd=entry.cost_usd,
        duration_ms=entry.duration_ms,
        ip_address=entry.ip_address,
        channel=entry.channel,
        # `default=str` keeps an exotic value in someone's metadata from
        # failing the write — the audit row matters more than its fidelity.
        metadata_json=json.dumps(entry.metadata, default=str) if entry.metadata else None,
    )


async def get_audit_service() -> AuditService:
    """FastAPI dependency: the audit log on the configured database."""
    from pincer.config import get_settings_relaxed

    return await AuditService.for_path(get_settings_relaxed().db_path)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]

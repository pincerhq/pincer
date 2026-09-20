"""The audit log: `audit_logs`.

`timestamp` holds ISO-8601 UTC text on both dialects, so the `since`/`until`
range filters are plain string comparisons and sort chronologically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlmodel import col, select

from pincer.models.audit import AuditLog
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy import ColumnElement


class AuditLogRepository(BaseRepository[AuditLog, str]):
    model = AuditLog

    @staticmethod
    def filters(
        user_id: str | None = None,
        action: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[ColumnElement[bool]]:
        """The shared filter set, in the order the callers document it."""
        where: list[ColumnElement[bool]] = []
        if user_id:
            where.append(col(AuditLog.user_id) == user_id)
        if action:
            where.append(col(AuditLog.action) == action)
        if tool:
            where.append(col(AuditLog.tool) == tool)
        if since:
            where.append(col(AuditLog.timestamp) >= since)
        if until:
            where.append(col(AuditLog.timestamp) <= until)
        return where

    async def add_many(self, entries: Sequence[AuditLog]) -> None:
        self.session.add_all(list(entries))
        await self.session.flush()

    async def newest_first(
        self, where: Sequence[ColumnElement[bool]], *, limit: int, offset: int
    ) -> Sequence[AuditLog]:
        stmt = select(AuditLog).where(*where).order_by(col(AuditLog.timestamp).desc()).limit(limit).offset(offset)
        return (await self.session.exec(stmt)).all()

    async def oldest_first(self, where: Sequence[ColumnElement[bool]]) -> AsyncIterator[AuditLog]:
        """Streamed, so an export does not hold the whole table in memory."""
        stmt = select(AuditLog).where(*where).order_by(col(AuditLog.timestamp))
        result = await self.session.stream(stmt)
        async for (row,) in result:
            yield row

    async def count(self, where: Sequence[ColumnElement[bool]]) -> int:
        stmt = select(func.count()).select_from(AuditLog).where(*where)
        return int((await self.session.exec(stmt)).one())

    async def stats(self, where: Sequence[ColumnElement[bool]]) -> dict[str, Any]:
        """Totals, the per-action and per-tool breakdowns, and refusals."""
        total = await self.count(where)

        by_action = await self._grouped_counts(col(AuditLog.action), where)
        by_tool = await self._grouped_counts(col(AuditLog.tool), [*where, col(AuditLog.tool).isnot(None)])

        cost_stmt = select(func.sum(AuditLog.cost_usd)).where(*where)
        total_cost = (await self.session.exec(cost_stmt)).one()

        refused = await self.count([*where, col(AuditLog.approved) == 0])

        return {
            "total_entries": total,
            "by_action": by_action,
            "by_tool": by_tool,
            "total_cost_usd": round(total_cost or 0.0, 6),
            "failed_actions": refused,
        }

    async def _grouped_counts(self, column: Any, where: Sequence[ColumnElement[bool]]) -> dict[str, int]:
        stmt = select(column, func.count()).where(*where).group_by(column).order_by(func.count().desc())
        return {key: int(count) for key, count in (await self.session.exec(stmt)).all()}

"""The generic repository every domain repository builds on.

A repository wraps one session and one table. It reads and stages writes; it
never commits — the service that opened the `session_scope()` decides when the
unit of work is done.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import delete
from sqlmodel import SQLModel, select

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import ColumnElement
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlmodel.ext.asyncio.session import AsyncSession


class BaseRepository[M: SQLModel, PK]:
    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, pk: PK) -> M | None:
        found: M | None = await self.session.get(self.model, pk)
        return found

    async def add(self, obj: M) -> M:
        """Stage `obj` and flush, so server-generated values (an autoincrement
        id, a column default) are on it — this replaces `cursor.lastrowid`."""
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def list(
        self,
        *where: ColumnElement[bool],
        order_by: Sequence[Any] = (),
        limit: int | None = None,
    ) -> Sequence[M]:
        stmt = select(self.model).where(*where).order_by(*order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.exec(stmt)
        return result.all()

    async def delete_where(self, *where: ColumnElement[bool]) -> int:
        """Bulk delete; returns the number of rows removed."""
        result = await self.session.exec(delete(self.model).where(*where))
        return int(result.rowcount)

    async def delete_older_than(self, timestamp: InstrumentedAttribute[Any], cutoff: float | str) -> int:
        """Retention: delete rows whose `timestamp` column is before `cutoff`.

        `cutoff` is in the column's own storage format — epoch seconds for the
        REAL timestamp columns, an ISO string for the text ones.
        """
        return await self.delete_where(timestamp < cutoff)

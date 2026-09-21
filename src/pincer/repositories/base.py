"""The generic repository every domain repository builds on.

A repository wraps one session and one table. It reads and stages writes; it
never commits — the service that opened the `session_scope()` decides when the
unit of work is done.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import delete
from sqlmodel import SQLModel, select

from pincer.db.ids import is_id
from pincer.db.types import Uuid7

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
        """The row with this key, or None.

        A key that could never name a row — a malformed id in a URL or a tool
        argument — is None rather than an error. `Uuid7` refuses to bind one,
        which is what a write needs, but a lookup's honest answer is "no such
        row": the caller turns that into a 404, not a 500.
        """
        if self._has_minted_key() and not is_id(pk):
            return None
        found: M | None = await self.session.get(self.model, pk)
        return found

    @classmethod
    def _has_minted_key(cls) -> bool:
        """True when this table's key is a single id Pincer mints.

        `channel_identities` has a two-column natural key, and every Twilio
        SID and phone-number key is plain text; none of those is guarded.
        """
        columns = list(cls.model.__table__.primary_key.columns)
        return len(columns) == 1 and isinstance(columns[0].type, Uuid7)

    async def add(self, obj: M, *, refresh: bool = True) -> M:
        """Stage `obj` and flush, so its generated id is set — this replaces
        `cursor.lastrowid`.

        `refresh` re-reads the row so server-side column defaults are loaded
        too; it costs a SELECT. Pass `refresh=False` when the caller only
        writes and discards the object, since reading an unloaded default
        afterwards would raise rather than lazy-load (there is no greenlet
        context outside the session).
        """
        self.session.add(obj)
        await self.session.flush()
        if refresh:
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


@cache
def repository_for(model: type[SQLModel]) -> type[BaseRepository[Any, Any]]:
    """A repository for `model` with only the generic operations, for a table
    that has no domain repository of its own."""
    return type(f"{model.__name__}Repository", (BaseRepository,), {"model": model})

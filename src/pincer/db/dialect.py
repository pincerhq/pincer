"""The few SQL constructs that differ between SQLite and Postgres.

Repositories stay dialect-agnostic by building these through the helpers here
instead of writing `INSERT OR IGNORE`, `json_each` or `date(x,'unixepoch')`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ColumnElement, Table, cast, exists, func
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from sqlalchemy.sql.base import ReadOnlyColumnCollection
    from sqlalchemy.sql.elements import KeyedColumnElement
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

#: Builds the `DO UPDATE SET` mapping from `excluded` (the row that lost the
#: conflict), for assignments that combine the stored and incoming values.
SetBuilder = Callable[["ReadOnlyColumnCollection[str, KeyedColumnElement[Any]]"], Mapping[str, Any]]


def dialect_of(session: AsyncSession) -> str:
    """The session's dialect name: `"sqlite"` or `"postgresql"`."""
    return session.get_bind().dialect.name


def _table(target: Table | type[SQLModel]) -> Table:
    return target if isinstance(target, Table) else target.__table__  # type: ignore[attr-defined]


def upsert(
    dialect: str,
    target: Table | type[SQLModel],
    values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    index_elements: Sequence[str],
    set_: Mapping[str, Any] | SetBuilder | None = None,
    where: ColumnElement[bool] | None = None,
) -> sqlite.Insert | postgresql.Insert:
    """`INSERT … ON CONFLICT (index_elements) DO UPDATE / DO NOTHING`.

    `set_=None` replaces `INSERT OR IGNORE`. A mapping, or a callable given
    `excluded` for assignments like `col = col + excluded.col`, replaces
    `ON CONFLICT DO UPDATE` and `INSERT OR REPLACE`. Unlike `OR REPLACE`, the
    stored row is updated in place, not deleted and re-inserted.
    """
    table = _table(target)
    stmt = (sqlite.insert(table) if dialect == "sqlite" else postgresql.insert(table)).values(values)
    if set_ is None:
        return stmt.on_conflict_do_nothing(index_elements=list(index_elements))
    assignments = set_(stmt.excluded) if callable(set_) else set_
    return stmt.on_conflict_do_update(index_elements=list(index_elements), set_=dict(assignments), where=where)


def json_contains(dialect: str, column: Any, value: str) -> ColumnElement[bool]:
    """True when the JSON array stored (as text) in `column` contains `value`."""
    if dialect == "sqlite":
        elements = func.json_each(column).table_valued("value")
        return exists().where(elements.c.value == value)
    return cast(column, JSONB).op("?", return_type=Boolean)(value)


def day_bucket(dialect: str, epoch_seconds: Any) -> ColumnElement[str]:
    """The UTC calendar day (`YYYY-MM-DD`) of an epoch-seconds column."""
    if dialect == "sqlite":
        return func.date(epoch_seconds, "unixepoch")
    return func.to_char(func.timezone("UTC", func.to_timestamp(epoch_seconds)), "YYYY-MM-DD")

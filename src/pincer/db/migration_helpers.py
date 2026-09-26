"""Steps the data migrations share (0014, 0017, 0018).

A revision has to keep doing what it did when it shipped, so change these only
in ways every revision that calls them still wants.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


def drop_stale_table(name: str) -> None:
    """Clear the wreckage of an upgrade that died mid-rebuild.

    SQLite has no transactional DDL, so a rebuild's first statement — the
    `CREATE TABLE` of the copy — lands in autocommit and survives the rollback
    that follows any later failure. The revision then cannot be retried: the
    next `upgrade` dies on "table already exists", and since the app brings its
    own database to head at startup, it never boots again.
    """
    op.execute(f"DROP TABLE IF EXISTS {name}")


def drop_stale_batch_table(table: str) -> None:
    """`drop_stale_table` for the copy Alembic's batch mode makes of `table`."""
    drop_stale_table(f"_alembic_tmp_{table}")


def to_ms(value: Any, kind: str) -> int | None:
    """Epoch milliseconds from whatever the driver handed back.

    Branching on the Python type, not the dialect: an `IsoText` column returns
    a `str` on SQLite and a naive `datetime` on Postgres, while plain TEXT
    returns a `str` on both. `kind` is `"epoch"` for float seconds, anything
    else for ISO-8601.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        stamped = value if value.tzinfo else value.replace(tzinfo=UTC)
        return in_range(int(stamped.timestamp() * 1000))
    if kind == "epoch":
        try:
            return in_range(int(float(value) * 1000))
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return in_range(int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp() * 1000))


def in_range(ms: int) -> int | None:
    """A timestamp a UUIDv7 can carry, or None for one it cannot.

    Nonsense happens: an epoch column holding milliseconds instead of seconds
    reads as the year 58000. Treating it as "unknown time" keeps the row and
    its position; raising would abort the upgrade, and on SQLite an aborted
    upgrade is expensive — see `drop_stale_table`.
    """
    return ms if 0 <= ms <= 0xFFFF_FFFF_FFFF else None


@contextmanager
def id_map(bind: sa.Connection, name: str, mapping: Mapping[str, str]) -> Iterator[str]:
    """Materialise `old -> new` as a temporary table and yield its name.

    Rewriting from a table is one set-based `UPDATE` per column, where binding
    the pairs to the `UPDATE` itself is one statement per row — against the
    largest tables Pincer has, `telephony_events` among them. Temporary, so a
    failed upgrade leaves nothing behind for the retry to trip over.

    Dropped only on the way out, not in a `finally`: on Postgres, a caller that
    raises has already aborted the transaction, so a `DROP` here would itself
    raise `InFailedSqlTransaction` and bury the real error under it — the
    `ExitStack` in 0018 holds one of these per table, so that would be a stack
    of them. The rollback discards the temporary table regardless, and the
    `DROP TABLE IF EXISTS` above already covers a leftover on retry.
    """
    table = f"_uuid7_map_{name}"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    bind.execute(sa.text(f"CREATE TEMPORARY TABLE {table} (old TEXT PRIMARY KEY, new TEXT NOT NULL)"))
    if mapping:
        bind.execute(
            sa.text(f"INSERT INTO {table} (old, new) VALUES (:old, :new)"),  # noqa: S608 - fixed identifiers
            [{"old": old, "new": new} for old, new in mapping.items()],
        )
    yield table
    bind.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))


def rewrite_from(bind: sa.Connection, table: str, column: str, map_table: str) -> None:
    """Replace every `column` value `map_table` knows with its new one, in one statement.

    `column` has to be text-typed on both sides when this runs; every caller
    retypes it to `uuid` on Postgres afterwards.
    """
    bind.execute(
        sa.text(
            f"UPDATE {table} SET {column} = "  # noqa: S608 - fixed identifiers
            f"(SELECT m.new FROM {map_table} m WHERE m.old = {table}.{column}) "
            f"WHERE {column} IN (SELECT old FROM {map_table})"
        )
    )

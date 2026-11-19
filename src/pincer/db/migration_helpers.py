"""Steps the data migrations share (0014, 0017, 0018, 0032-0034).

A revision has to keep doing what it did when it shipped, so change these only
in ways every revision that calls them still wants.
"""

from __future__ import annotations

import logging
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

from pincer.db.ids import Uuid7Sequence
from pincer.db.metadata import VERSION_TABLE

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


logger = logging.getLogger("pincer.db.migrations")

#: How many keys of the rows `drop_rows` deletes its warning lists.
LOGGED_KEYS = 20


def hold_transaction(bind: sa.Connection) -> None:
    """Make the rest of a revision one transaction on SQLite, DDL included.

    The sqlite3 driver opens its transaction at the first INSERT, UPDATE or
    DELETE, so a revision that begins with DDL (a `DROP INDEX`) commits that
    statement on its own: a failure later rolls the rest back and leaves the
    revision un-retryable ("no such index"). A no-op UPDATE first opens the
    transaction; SQLite itself rolls DDL back. Postgres DDL is transactional
    already.
    """
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text(f"UPDATE {VERSION_TABLE} SET version_num = version_num WHERE 1 = 0"))  # noqa: S608


def drop_rows(bind: sa.Connection, table: str, where: str, *, key: str | None, reason: str) -> int:
    """Delete the rows of `table` matching `where`, with a warning naming how
    many and (when `key` is given) which: a revision never drops data silently.

    Returns the number of rows deleted. A downgrade does not bring them back.
    """
    found = bind.execute(sa.text(f"SELECT {key or '1'} FROM {table} WHERE {where}")).scalars().all()  # noqa: S608
    if not found:
        return 0
    shown = ""
    if key is not None:
        listed = ", ".join(str(value) for value in found[:LOGGED_KEYS])
        more = len(found) - LOGGED_KEYS
        shown = f" ({key}: {listed}{f', and {more} more' if more > 0 else ''})"
    logger.warning("Dropping %d row(s) of %s %s%s", len(found), table, reason, shown)
    bind.execute(sa.text(f"DELETE FROM {table} WHERE {where}"))  # noqa: S608 - fixed identifiers
    return len(found)


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


def pk_name(table: str) -> str:
    """The name a swapped primary key gets, on both dialects."""
    return f"{table}_pkey"


def referencing_foreign_keys(bind: sa.Connection, table: str) -> list[tuple[str, str | None]]:
    """(table, constraint name) of every foreign key that points at `table`."""
    inspector = sa.inspect(bind)
    return [
        (other, fk["name"])
        for other in inspector.get_table_names()
        for fk in inspector.get_foreign_keys(other)
        if fk["referred_table"] == table
    ]


def mint_ids(
    bind: sa.Connection, table: str, key: Sequence[str], time: str, kind: str, tiebreak: Sequence[str] = ()
) -> list[tuple[tuple[Any, ...], str]]:
    """(old key, new UUIDv7) per row of `table`, dated by its own `time`.

    The same rules as 0017's: a row with no readable time inherits the
    previous row's in key order, rows before the first readable time take the
    earliest one, and `Uuid7Sequence` keeps ties strictly increasing. `time`
    may be an SQL expression (`coalesce(a, b)`); `tiebreak` columns order rows
    that share a millisecond, before the key does.

    The whole table is read and sorted in memory: the ids depend on every
    row's place in time order. That suits the tables it swaps, which hold
    thousands of rows, not millions; the large ones already had ids (0017).
    """
    columns = ", ".join([*key, time, *tiebreak])
    rows = bind.execute(sa.text(f"SELECT {columns} FROM {table}")).all()  # noqa: S608 - fixed identifiers
    width = len(key)
    carried: int | None = None
    stamped: list[tuple[tuple[Any, ...], int | None, tuple[Any, ...]]] = []
    for row in sorted(rows, key=lambda row: tuple(str(v) for v in row[:width])):
        ms = to_ms(row[width], kind)
        carried = ms if ms is not None else carried
        stamped.append((tuple(row[:width]), carried, tuple(row[width + 1 :])))
    earliest = next((ms for _old, ms, _tie in stamped if ms is not None), 0)

    def order(item: tuple[tuple[Any, ...], int | None, tuple[Any, ...]]) -> tuple[Any, ...]:
        old, ms, tie = item
        return (
            earliest if ms is None else ms,
            tuple((value is None, value if value is not None else 0) for value in tie),
            tuple(str(v) for v in old),
        )

    sequence = Uuid7Sequence()
    return [(old, sequence.next(earliest if ms is None else ms)) for old, ms, _tie in sorted(stamped, key=order)]


#: Rows per `INSERT` into `fill_ids`' map, so a large table is not sent as one batch.
FILL_BATCH = 5000


def fill_ids(bind: sa.Connection, table: str, key: Sequence[str], pairs: Sequence[tuple[tuple[Any, ...], str]]) -> None:
    """Set `table.id` from `pairs`, matched on the (text) `key` columns, in one `UPDATE`.

    Through a temporary table for the same reason as `id_map`; that one maps
    a single text column, and these keys can span two.
    """
    tmp = f"_uuid7_pk_{table}"
    slots = [f"k{i}" for i in range(len(key))]
    bind.execute(sa.text(f"DROP TABLE IF EXISTS {tmp}"))
    bind.execute(
        sa.text(
            f"CREATE TEMPORARY TABLE {tmp} ("
            + ", ".join(f"{slot} TEXT NOT NULL" for slot in slots)
            + f", new TEXT NOT NULL, PRIMARY KEY ({', '.join(slots)}))"
        )
    )
    insert = sa.text(
        f"INSERT INTO {tmp} ({', '.join(slots)}, new) "  # noqa: S608 - fixed identifiers
        f"VALUES ({', '.join(':' + slot for slot in slots)}, :new)"
    )
    for start in range(0, len(pairs), FILL_BATCH):
        bind.execute(
            insert,
            [
                {**{slot: str(value) for slot, value in zip(slots, old, strict=True)}, "new": new}
                for old, new in pairs[start : start + FILL_BATCH]
            ],
        )
    # A join, not a subquery per row: Postgres hashes the map once. SQLite
    # has had UPDATE ... FROM since 3.33.
    match = " AND ".join(f"m.{slot} = {table}.{column}" for slot, column in zip(slots, key, strict=True))
    bind.execute(sa.text(f"UPDATE {table} SET id = m.new FROM {tmp} m WHERE {match}"))  # noqa: S608
    bind.execute(sa.text(f"DROP TABLE IF EXISTS {tmp}"))


def swap_primary_key(
    bind: sa.Connection,
    table: str,
    key: Sequence[str],
    *,
    time: str,
    kind: str,
    unique: str,
    tiebreak: Sequence[str] = (),
) -> None:
    """Give `table` a UUIDv7 `id` primary key and demote `key` to UNIQUE `unique`.

    `key` must be text on both dialects. Postgres will not drop a primary key
    that a foreign key still points at, so the caller drops those first; this
    refuses to guess what should become of them.
    """
    postgres = bind.dialect.name == "postgresql"
    if postgres and (pointing := referencing_foreign_keys(bind, table)):
        raise RuntimeError(f"drop the foreign keys onto {table} first: {pointing}")

    if not postgres:
        # SQLite let a TEXT primary key hold NULL; Postgres never did. Such a
        # row was never found by its key, gets no id (NULL matches nothing)
        # and could not keep its key once that turns NOT NULL below: it goes.
        null_key = " OR ".join(f"{column} IS NULL" for column in key)
        drop_rows(bind, table, null_key, key=None, reason="whose old primary key is NULL")

    op.add_column(table, sa.Column("id", sa.Text(), nullable=True))
    fill_ids(bind, table, key, mint_ids(bind, table, key, time, kind, tiebreak))

    if postgres:
        old_pk = sa.inspect(bind).get_pk_constraint(table)["name"]
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE uuid USING id::uuid")
        for column in ("id", *key):
            # The old key may be a column added just before, still nullable.
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{old_pk}"')
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {pk_name(table)} PRIMARY KEY (id)")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {unique} UNIQUE ({', '.join(key)})")
        return

    drop_stale_batch_table(table)
    with warnings.catch_warnings():
        # The reflected columns still carry `primary_key=True`; SQLAlchemy
        # warns that the new constraint overrides them, which is the point.
        warnings.filterwarnings("ignore", message=".*specifies columns .* as primary_key=True")
        with op.batch_alter_table(table, recreate="always") as batch:
            # A TEXT primary key in SQLite accepts NULL unless told otherwise,
            # and the old key's columns reflect as nullable for the same
            # reason: once they stop being the primary key, say it.
            batch.alter_column("id", existing_type=sa.Text(), nullable=False)
            for column in key:
                batch.alter_column(column, existing_type=sa.Text(), nullable=False)
            batch.create_primary_key(pk_name(table), ["id"])
            batch.create_unique_constraint(unique, list(key))


def unswap_primary_key(bind: sa.Connection, table: str, key: Sequence[str], *, unique: str) -> None:
    """Undo `swap_primary_key`: `key` is the primary key again and `id` is gone.

    Every row keeps its key: `key` was unique the whole time (rows
    `swap_primary_key` dropped stay dropped). The restored key is named
    `pk_name(table)` and stays NOT NULL on SQLite; before the swap it was
    unnamed there, and a TEXT primary key that SQLite let hold NULL. Nothing
    reads either.
    """
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {unique}")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {pk_name(table)}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {pk_name(table)} PRIMARY KEY ({', '.join(key)})")
        op.execute(f"ALTER TABLE {table} DROP COLUMN id")
        return

    drop_stale_batch_table(table)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*specifies columns .* as primary_key=True")
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(unique, type_="unique")
            batch.drop_constraint(pk_name(table), type_="primary")
            batch.drop_column("id")
            batch.create_primary_key(pk_name(table), list(key))


def require_foreign_key_aware_rename(bind: sa.Connection) -> None:
    """Refuse a rename SQLite would do without rewriting the foreign keys onto it.

    Only SQLite 3.26+ with `legacy_alter_table` off updates the `REFERENCES`
    of other tables when a table or column they point at is renamed; an older
    one, or the legacy mode, leaves them dangling. No-op on Postgres.
    """
    if bind.dialect.name != "sqlite":
        return
    version = bind.execute(sa.text("SELECT sqlite_version()")).scalar_one()
    if tuple(int(part) for part in str(version).split(".")[:3]) < (3, 26, 0):
        raise RuntimeError(f"SQLite {version} cannot rename what a foreign key references; 3.26.0 is required")
    if bind.execute(sa.text("PRAGMA legacy_alter_table")).scalar_one():
        raise RuntimeError("PRAGMA legacy_alter_table is ON; renaming would leave foreign keys dangling")

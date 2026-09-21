"""Turn the autoincrement primary keys into UUIDv7.

Fourteen tables identified their rows with `INTEGER PRIMARY KEY AUTOINCREMENT`
(`SERIAL` on Postgres). A dense counter forces a round trip to read the id
back after every insert, collides the moment rows come from more than one
database, and tells anyone who sees an id how many rows there are. Phase 10
(issue #212) replaces every id Pincer mints with a UUIDv7.

Nothing in the schema references these keys — `voice_calls.id` in particular
is vestigial, since every child table joins on `call_sid` — so each one is
rewritten in place with no cross-table repair. That is why they go first, and
alone: the six minted TEXT keys in 0018 are the half that needs a mapping.

Each row keeps its place in time. Its new id carries the timestamp from the
row's own creation column, fed through `Uuid7Sequence` in the order the rows
already had, so a tie, a clock that runs backwards, or a NULL cannot reorder
them. `call_transcripts` and `inbound_messages` need exactly that: they order
by `(timestamp, id)`, using the key to keep same-second utterances in the
order they were spoken.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-20
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

from pincer.db.ids import Uuid7Sequence

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

#: (table, creation-time column, how that column stores time).
#:
#: Frozen rather than inferred: the formats genuinely differ, and guessing
#: would silently pick a column that does not mean "when this row was made".
#: `audit_logs.timestamp` is plain TEXT on both dialects, the two cost logs
#: hold epoch floats, and the rest are `IsoText` (TEXT on SQLite, TIMESTAMP on
#: Postgres). Four are nullable, which `Uuid7Sequence` absorbs by falling back
#: to the previous row's position.
TABLES: tuple[tuple[str, str, str], ...] = (
    ("audit_logs", "timestamp", "iso"),
    ("cost_logs", "timestamp", "epoch"),
    ("image_cost_logs", "timestamp", "epoch"),
    ("schedules", "created_at", "iso"),
    ("event_triggers", "processed_at", "iso"),
    ("briefing_configs", "created_at", "iso"),
    ("appointment_outcomes", "recorded_at", "iso"),
    ("canary_runs", "ran_at", "iso"),
    ("voice_calls", "started_at", "iso"),
    ("call_transcripts", "timestamp", "iso"),
    ("call_actions", "timestamp", "iso"),
    ("phone_contacts", "created_at", "iso"),
    ("outbound_call_logs", "placed_at", "iso"),
    ("inbound_messages", "created_at", "iso"),
)


def _to_ms(value: Any, kind: str) -> int | None:
    """Epoch milliseconds from whatever the driver handed back.

    Branching on the Python type, not the dialect: an `IsoText` column returns
    a `str` on SQLite and a naive `datetime` on Postgres, while
    `audit_logs.timestamp` is plain TEXT and returns a `str` on both.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        stamped = value if value.tzinfo else value.replace(tzinfo=UTC)
        return _in_range(int(stamped.timestamp() * 1000))
    if kind == "epoch":
        try:
            return _in_range(int(float(value) * 1000))
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _in_range(int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp() * 1000))


def _in_range(ms: int) -> int | None:
    """A timestamp a UUIDv7 can carry, or None for one it cannot.

    Nonsense happens: an epoch column holding milliseconds instead of seconds
    reads as the year 58000. Treating it as "unknown time" keeps the row and
    its position; raising would abort the upgrade, and on SQLite an aborted
    upgrade is expensive — see `_drop_stale_batch_table`.
    """
    return ms if 0 <= ms <= 0xFFFF_FFFF_FFFF else None


def _new_ids(bind: sa.Connection, table: str, time_column: str, kind: str) -> list[tuple[str, int]]:
    """(new id, old id) per row, in the order the rows should be dated.

    A row whose creation time is missing or unreadable inherits the previous
    row's, walking the table in its existing key order. That keeps it where it
    was: sorting such rows to the front would date them to 1970 and move them
    ahead of every dated row — and `downgrade()` would then bake that
    reordering into the integers.
    """
    rows = bind.execute(sa.text(f"SELECT id, {time_column} FROM {table}")).all()  # noqa: S608 - fixed identifiers
    carried: int | None = None
    stamped: list[tuple[Any, int | None]] = []
    for row in sorted(rows, key=lambda row: row[0]):
        ms = _to_ms(row[1], kind)
        carried = ms if ms is not None else carried
        stamped.append((row[0], carried))
    # Rows before the first readable timestamp have nothing to inherit; they
    # take the earliest one there is rather than the epoch.
    earliest = next((ms for _old, ms in stamped if ms is not None), 0)

    sequence = Uuid7Sequence()
    ordered = sorted(
        ((old, earliest if ms is None else ms) for old, ms in stamped), key=lambda item: (item[1], item[0])
    )
    return [(sequence.next(ms), old) for old, ms in ordered]


def _present(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _drop_stale_batch_table(table: str) -> None:
    """Clear the wreckage of an upgrade that died mid-rebuild.

    SQLite has no transactional DDL, so batch mode's first statement —
    `CREATE TABLE _alembic_tmp_<t>` — lands in autocommit and survives the
    rollback that follows any later failure. The revision then cannot be
    retried: the next `upgrade` dies on "table already exists", and since the
    app brings its own database to head at startup, it never boots again.
    """
    op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{table}")


#: The one index in these tables whose definition SQLite's reflection cannot
#: carry: `0001` declared it `COLLATE NOCASE`, and a batch rebuild re-emits it
#: without the collation, silently. The index keeps its name, so nothing
#: complains and the drift test cannot see it either.
_COLLATED_INDEXES = {
    "phone_contacts": (
        "idx_phone_contacts_name",
        "CREATE INDEX idx_phone_contacts_name ON phone_contacts(name COLLATE NOCASE)",
    ),
}


def _restore_collated_index(table: str) -> None:
    entry = _COLLATED_INDEXES.get(table)
    if entry is None:
        return
    name, ddl = entry
    op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(ddl)


def _sequence_name(bind: sa.Connection, table: str) -> str | None:
    """The sequence feeding this column, whatever it happens to be called.

    Not `{table}_id_seq`: `ALTER TABLE … RENAME` (0011) does not rename a
    table's sequence, and 0005's rebuilds left `…_seq1` names behind, so
    guessing misses half of them and leaves the objects orphaned. Must be
    asked before the default is dropped, which is what owns the link.
    """
    found = bind.execute(sa.text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table}).scalar()
    return str(found) if found else None


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    existing = _present(bind)

    for table, time_column, kind in TABLES:
        if table not in existing:
            continue
        mapping = _new_ids(bind, table, time_column, kind)

        sequence = _sequence_name(bind, table) if postgres else None

        if postgres:
            # The default is `nextval(...)`, typed integer; re-casting the
            # column while it is still attached fails on it.
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE text USING id::text")
        else:
            _drop_stale_batch_table(table)
            # Reflected, not transcribed: none of these tables has a single
            # CREATE TABLE in the tree matching its current shape — half were
            # renamed by 0011, and the voice tables were rebuilt by 0005 and
            # altered by 0006-0008.
            #
            # `nullable=False` is not inherited: `INTEGER PRIMARY KEY` could
            # not hold NULL because it was the rowid, but a TEXT primary key
            # in SQLite accepts NULL — and several of them. A row with a NULL
            # key is worse than an error, because SQLAlchemy discards it on
            # read and the row simply disappears.
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column("id", existing_type=sa.Integer(), type_=sa.Text(), nullable=False)

        if mapping:
            bind.execute(
                sa.text(f"UPDATE {table} SET id = :new WHERE id = :old"),  # noqa: S608 - fixed identifiers
                [{"new": new, "old": str(old)} for new, old in mapping],
            )

        if not postgres:
            _restore_collated_index(table)

        if postgres:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE uuid USING id::uuid")
            if sequence:
                op.execute(f"DROP SEQUENCE IF EXISTS {sequence}")


def downgrade() -> None:
    """Lossy: the original integers are gone.

    Rows are renumbered from 1 in their current order, which preserves how
    they sort but not what they were called. That is only safe because nothing
    in the schema references these keys.
    """
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    existing = _present(bind)

    for table, _time_column, _kind in TABLES:
        if table not in existing:
            continue
        rows = bind.execute(sa.text(f"SELECT id FROM {table} ORDER BY id")).all()  # noqa: S608 - fixed identifiers
        # Both sides as text: the column is uuid when read and text when
        # written, and Postgres has no `text = uuid` operator to bridge that.
        renumbered = [{"new": str(index), "old": str(row[0])} for index, row in enumerate(rows, start=1)]

        if postgres:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE text USING id::text")
        else:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column("id", existing_type=sa.Text(), type_=sa.Text(), existing_nullable=True)

        if renumbered:
            bind.execute(
                sa.text(f"UPDATE {table} SET id = :new WHERE id = :old"),  # noqa: S608 - fixed identifiers
                renumbered,
            )

        if postgres:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE integer USING id::integer")
            op.execute(f"CREATE SEQUENCE IF NOT EXISTS {table}_id_seq OWNED BY {table}.id")
            op.execute(f"SELECT setval('{table}_id_seq', COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{table}_id_seq')")
        else:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column("id", existing_type=sa.Text(), type_=sa.Integer(), existing_nullable=True)

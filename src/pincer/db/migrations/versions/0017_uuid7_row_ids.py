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
        return int(stamped.timestamp() * 1000)
    if kind == "epoch":
        try:
            return int(float(value) * 1000)
        except (TypeError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp() * 1000)


def _new_ids(bind: sa.Connection, table: str, time_column: str, kind: str) -> list[tuple[str, int]]:
    """(new id, old id) per row, in the order the rows already had."""
    rows = bind.execute(sa.text(f"SELECT id, {time_column} FROM {table}")).all()  # noqa: S608 - fixed identifiers
    ordered = sorted(rows, key=lambda row: (_to_ms(row[1], kind) is not None, _to_ms(row[1], kind) or 0, row[0]))
    sequence = Uuid7Sequence()
    return [(sequence.next(_to_ms(row[1], kind)), row[0]) for row in ordered]


def _present(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    existing = _present(bind)

    for table, time_column, kind in TABLES:
        if table not in existing:
            continue
        mapping = _new_ids(bind, table, time_column, kind)

        if postgres:
            # The default is `nextval(...)`, typed integer; re-casting the
            # column while it is still attached fails on it.
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE text USING id::text")
        else:
            # Reflected, not transcribed: none of these tables has a single
            # CREATE TABLE in the tree matching its current shape — half were
            # renamed by 0011, and the voice tables were rebuilt by 0005 and
            # altered by 0006-0008.
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column("id", existing_type=sa.Integer(), type_=sa.Text(), existing_nullable=True)

        if mapping:
            bind.execute(
                sa.text(f"UPDATE {table} SET id = :new WHERE id = :old"),  # noqa: S608 - fixed identifiers
                [{"new": new, "old": str(old)} for new, old in mapping],
            )

        if postgres:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE uuid USING id::uuid")
            op.execute(f"DROP SEQUENCE IF EXISTS {table}_id_seq")


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

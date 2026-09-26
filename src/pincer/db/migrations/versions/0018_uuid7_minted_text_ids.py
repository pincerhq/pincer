"""Turn the minted TEXT keys into UUIDv7, with everything that points at them.

Six tables identify their rows with a string Pincer chose, minted four
different ways: `uuid4` for memories, entities and conversations,
`token_hex(16)` for `telephony_calls.call_id`, the span-id generator for
`telephony_turns.turn_id`, and `"thr_" + token_hex(6)` for threads. None of
them sorts by time. They become UUIDv7, like the autoincrement keys did in
0017 — but unlike those, seven columns point at them, so this revision builds
a map per table first and rewrites both sides from it.

What deliberately does NOT change: every `call_sid` (Twilio's), every
`pincer_user_id` and `session_id`, `trace_id` and `span_id` and
`parent_span_id` (W3C trace-context, 16 and 32 hex on the wire), and
`telephony_events.event_id` (a SHA-1 of the call and event name, which is what
collapses Twilio's retried status callbacks into one row). Also unchanged:
`call_transcripts.call_id` and `call_actions.call_id`, which despite the name
hold a Twilio CallSid.

Three of the referencing columns default to `''` as the app's "no thread / no
turn" sentinel. An empty string is not a uuid and will not cast, so the
sentinel becomes NULL — which is what the column meant all along.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-20
"""

from __future__ import annotations

import json
from contextlib import ExitStack

import sqlalchemy as sa
from alembic import op

from pincer.db.ids import Uuid7Sequence
from pincer.db.migration_helpers import drop_stale_batch_table, id_map, rewrite_from, to_ms

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

#: (table, key column, creation-time column, how that column stores time).
KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("memories", "id", "created_at", "epoch"),
    ("entities", "id", "last_seen", "epoch"),
    ("conversations", "id", "created_at", "epoch"),
    ("telephony_calls", "call_id", "registered_at", "iso"),
    ("telephony_turns", "turn_id", "created_at", "iso"),
    ("call_threads", "thread_id", "created_at", "iso"),
)

#: (table, column, what it points at, what to do with a row that points nowhere).
#:
#: A column that cannot be NULL has to lose the row; one that can just loses
#: the link. `telephony_*.call_id` is NOT NULL and retention deletes calls out
#: from under their telemetry, so orphans there are expected, not corruption.
REFERENCES: tuple[tuple[str, str, str, str], ...] = (
    ("telephony_events", "call_id", "telephony_calls", "delete"),
    ("telephony_spans", "call_id", "telephony_calls", "delete"),
    ("telephony_turns", "call_id", "telephony_calls", "delete"),
    ("telephony_events", "turn_id", "telephony_turns", "null"),
    ("telephony_spans", "turn_id", "telephony_turns", "null"),
    ("voice_calls", "thread_id", "call_threads", "null"),
    ("call_thread_members", "thread_id", "call_threads", "delete"),
)

#: Each converted table's key column, for the orphan sweep's subquery.
KEY_OF = {table: key for table, key, _time, _kind in KEYS}

#: Columns whose `DEFAULT ''` has to go: the sentinel is not a uuid.
SENTINEL_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("voice_calls", "thread_id"),
    ("telephony_events", "turn_id"),
    ("telephony_spans", "turn_id"),
)

_FK = "call_thread_members_thread_id_fkey"

#: `memories_fts`'s triggers, verbatim from 0001. Rewriting `memories.id` does
#: not move a row's rowid, which is all the index keys on, so leaving them live
#: would be harmless — but that is an inference; dropping them for the rewrite
#: and rebuilding the index afterwards makes it a guarantee.
_MEMORY_FTS_TRIGGERS = {
    "memories_ai": """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
    "memories_ad": """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
    END
    """,
    "memories_au": """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category)
        VALUES ('delete', old.rowid, old.content, old.category);
        INSERT INTO memories_fts(rowid, content, category)
        VALUES (new.rowid, new.content, new.category);
    END
    """,
}


def _mapping(bind: sa.Connection, table: str, key: str, time_column: str, kind: str) -> dict[str, str]:
    """old id -> new id, in the order the rows should be dated.

    A row whose creation time is missing or unreadable inherits the previous
    row's rather than sorting to the front, which would date it to 1970 and
    move it ahead of every dated row. `telephony_calls.registered_at` is
    nullable, so this is reachable.
    """
    rows = bind.execute(sa.text(f"SELECT {key}, {time_column} FROM {table}")).all()  # noqa: S608 - fixed identifiers
    carried: int | None = None
    stamped: list[tuple[str, int | None]] = []
    for row in sorted(rows, key=lambda row: str(row[0])):
        ms = to_ms(row[1], kind)
        carried = ms if ms is not None else carried
        stamped.append((str(row[0]), carried))
    earliest = next((ms for _old, ms in stamped if ms is not None), 0)

    sequence = Uuid7Sequence()
    ordered = sorted(
        ((old, earliest if ms is None else ms) for old, ms in stamped), key=lambda item: (item[1], item[0])
    )
    return {old: sequence.next(ms) for old, ms in ordered}


def _retype(tables: set[str], table: str, column: str, to: str) -> None:
    if table in tables:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {to} USING {column}::{to}")


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    tables = set(sa.inspect(bind).get_table_names())

    if postgres and "call_thread_members" in tables:
        # Postgres will not let a referenced key be updated, nor its type
        # altered without the referencing column in the same breath. SQLite
        # bakes the constraint into the CREATE TABLE and does not enforce it
        # (`PRAGMA foreign_keys` is off), so there is nothing to drop there.
        op.execute(f"ALTER TABLE call_thread_members DROP CONSTRAINT IF EXISTS {_FK}")

    maps = {
        table: _mapping(bind, table, key, time_column, kind)
        for table, key, time_column, kind in KEYS
        if table in tables
    }

    # Orphans and sentinels first, so that every value left can cast.
    #
    # The stale set is resolved in SQL rather than gathered into an `IN` list:
    # retention deletes a call and leaves its telemetry behind (see
    # `services.retention`), so on a long-lived deployment the orphans number
    # in the tens of thousands — and psycopg caps a statement at 65535
    # parameters, which would abort the upgrade with no way forward.
    for table, column, parent, policy in REFERENCES:
        if table not in tables or parent not in maps:
            continue
        missing = f"{column} IS NOT NULL AND {column} NOT IN (SELECT {KEY_OF[parent]} FROM {parent})"
        if policy == "delete":
            op.execute(f"DELETE FROM {table} WHERE {missing}")  # noqa: S608 - fixed identifiers
        else:
            op.execute(f"UPDATE {table} SET {column} = NULL WHERE {missing}")  # noqa: S608 - fixed identifiers

    fts = not postgres and "memories_fts" in tables
    if fts:
        for trigger in _MEMORY_FTS_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    # One map table per converted table, each rewriting its key and every
    # column that points at it in one statement per column.
    with ExitStack() as stack:
        map_tables = {table: stack.enter_context(id_map(bind, table, mapping)) for table, mapping in maps.items()}
        for table, key, _time_column, _kind in KEYS:
            if table in map_tables:
                rewrite_from(bind, table, key, map_tables[table])
        for table, column, parent, _policy in REFERENCES:
            if table in tables and parent in map_tables:
                rewrite_from(bind, table, column, map_tables[parent])

    _rewrite_memory_tags(bind, tables, maps.get("call_threads", {}))

    if fts:
        for ddl in _MEMORY_FTS_TRIGGERS.values():
            op.execute(ddl)
        op.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

    for table, column in SENTINEL_DEFAULTS:
        if table not in tables:
            continue
        if postgres:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column(column, existing_type=sa.Text(), server_default=None)

    if postgres:
        for table, key, _time_column, _kind in KEYS:
            _retype(tables, table, key, "uuid")
        for table, column, _parent, _policy in REFERENCES:
            _retype(tables, table, column, "uuid")
        if "call_thread_members" in tables and "call_threads" in tables:
            op.execute(
                f"ALTER TABLE call_thread_members ADD CONSTRAINT {_FK} "
                "FOREIGN KEY (thread_id) REFERENCES call_threads(thread_id)"
            )


def _rewrite_memory_tags(bind: sa.Connection, tables: set[str], threads: dict[str, str]) -> None:
    """Thread ids have a second home in `memories.tags`.

    `voice/threads.py` writes `thread:{thread_id}` into the tag array and reads
    it back to find the note it wrote last time — one note per thread. Leaving
    the tag naming an id that no longer exists would not error; it would
    quietly split a thread's memory in two.
    """
    if not threads or "memories" not in tables:
        return
    rows = bind.execute(sa.text("SELECT id, tags FROM memories WHERE tags LIKE '%\"thread:%'")).all()
    for row_id, raw in rows:
        try:
            tags = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(tags, list):
            continue
        rewritten = [
            f"thread:{threads[tag.removeprefix('thread:')]}"
            if isinstance(tag, str) and tag.startswith("thread:") and tag.removeprefix("thread:") in threads
            else tag
            for tag in tags
        ]
        if rewritten != tags:
            bind.execute(
                sa.text("UPDATE memories SET tags = :tags WHERE id = :id"),
                {"tags": json.dumps(rewritten), "id": row_id},
            )


def downgrade() -> None:
    """Not lossy: the ids stay as they are, only the storage type reverts.

    The old code read these columns as TEXT and never parsed the id format, so
    it runs unchanged against uuid-shaped strings. The memory tags are not
    reverted either — the thread ids they name do not revert.
    """
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if bind.dialect.name == "postgresql":
        if "call_thread_members" in tables:
            op.execute(f"ALTER TABLE call_thread_members DROP CONSTRAINT IF EXISTS {_FK}")
        for table, key, _time_column, _kind in KEYS:
            _retype(tables, table, key, "text")
        for table, column, _parent, _policy in REFERENCES:
            _retype(tables, table, column, "text")
        if "call_thread_members" in tables and "call_threads" in tables:
            op.execute(
                f"ALTER TABLE call_thread_members ADD CONSTRAINT {_FK} "
                "FOREIGN KEY (thread_id) REFERENCES call_threads(thread_id)"
            )

    for table, column in SENTINEL_DEFAULTS:
        if table not in tables:
            continue
        bind.execute(sa.text(f"UPDATE {table} SET {column} = '' WHERE {column} IS NULL"))  # noqa: S608
        if bind.dialect.name == "postgresql":
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT ''")
        else:
            drop_stale_batch_table(table)
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column(column, existing_type=sa.Text(), server_default=sa.text("''"))

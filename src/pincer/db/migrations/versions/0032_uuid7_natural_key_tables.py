"""Give the seven tables keyed by someone else's identifier a UUIDv7 `id`.

0017 and 0018 converted every key Pincer mints, and deliberately left alone
the ones that come from outside: Twilio CallSids, phone numbers, the W3C span
id and the SHA-1 event key. Those keys still mean what they meant, so they
stay — as UNIQUE constraints, which is all the upserts' `ON CONFLICT` needs —
and each row also gets an `id` of its own, so every table in the schema is
identified the same way.

Nothing points at these keys, so each table is rewritten in place. Each id
carries the row's own creation time, through `mint_ids` with 0017's rules for
missing and tied times. Four tables (`call_costs`, `call_analytics`,
`call_origins`, `call_thread_members`) overwrite that time on every upsert,
so their ids date the last write, not the first; nothing orders by them.

On SQLite a row whose old key is NULL — which SQLite let a TEXT primary key
hold — is dropped, with a warning: it was never found by its key and cannot
keep one that is now NOT NULL. The downgrade drops `id` and restores the old
primary key; it does not bring such rows back.

Columns are renamed so a table's own key is `id` everywhere, and an outside
identifier says it is one:

- `pincer_telephony_turns.turn_id`, `pincer_telephony_calls.call_id` and
  `pincer_call_threads.thread_id` — UUIDv7 keys since 0018 — become `id`.
- `pincer_telephony_events.event_id` becomes `reference_event_id` and
  `pincer_telephony_spans.span_id` becomes `reference_span_id`, renamed
  before their key swap so their UNIQUE constraints carry the new names.

Columns that point at these keys keep their names: `call_thread_members.
thread_id`, `voice_calls.thread_id`, and the `call_id`, `turn_id` and
`span_id` of events, spans and turns. On SQLite the rename of
`call_threads.thread_id` rewrites the members' foreign key, which needs
SQLite 3.26+ with `legacy_alter_table` off.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

from pincer.db.migration_helpers import (
    hold_transaction,
    require_foreign_key_aware_rename,
    swap_primary_key,
    unswap_primary_key,
)

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

#: (table, old name, new name) of the outside keys renamed before their swap.
REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("pincer_telephony_events", "event_id", "reference_event_id"),
    ("pincer_telephony_spans", "span_id", "reference_span_id"),
)

#: (table, old name) of the UUIDv7 keys renamed `id`.
RENAMED_KEYS: tuple[tuple[str, str], ...] = (
    ("pincer_telephony_turns", "turn_id"),
    ("pincer_telephony_calls", "call_id"),
    ("pincer_call_threads", "thread_id"),
)

#: (table, old key, creation time, how it stores time, tiebreakers).
TABLES: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("pincer_call_costs", "call_sid", "recorded_at", "iso", ()),
    ("pincer_call_analytics", "call_sid", "created_at", "iso", ()),
    ("pincer_call_origins", "call_sid", "created_at", "iso", ()),
    ("pincer_call_thread_members", "call_sid", "attached_at", "iso", ()),
    ("pincer_do_not_call_numbers", "phone_number", "added_at", "iso", ()),
    ("pincer_telephony_events", "reference_event_id", "ts_utc", "iso", ("mono_ns", "seq")),
    ("pincer_telephony_spans", "reference_span_id", "start_utc", "iso", ("start_mono_ns",)),
)


def unique_name(table: str, key: str) -> str:
    return f"uq_{table.removeprefix('pincer_')}_{key}"


def upgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    require_foreign_key_aware_rename(bind)
    for table, old, new in REFERENCES:
        _rename_column(table, old, new)
    for table, key, time, kind, tiebreak in TABLES:
        swap_primary_key(bind, table, [key], time=time, kind=kind, tiebreak=tiebreak, unique=unique_name(table, key))
    for table, old in RENAMED_KEYS:
        _rename_column(table, old, "id")


def downgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    require_foreign_key_aware_rename(bind)
    for table, old in reversed(RENAMED_KEYS):
        _rename_column(table, "id", old)
    for table, key, _time, _kind, _tiebreak in reversed(TABLES):
        unswap_primary_key(bind, table, [key], unique=unique_name(table, key))
    for table, old, new in reversed(REFERENCES):
        _rename_column(table, new, old)


def _rename_column(table: str, old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")

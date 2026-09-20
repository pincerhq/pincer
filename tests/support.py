"""Helpers shared by tests that seed the database through raw SQL.

Several suites predate the repository layer and still seed with `aiosqlite`
against the real migrated schema. That is a deliberate seam — it is how they
prove the read path copes with rows it did not write — but it bypasses the
models, and since migration 0017 the models are where a row's id comes from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.db.ids import new_id

if TYPE_CHECKING:
    import aiosqlite

#: The tables whose primary key migration 0017 turned into a UUIDv7.
UUID_KEYED = (
    "audit_logs",
    "cost_logs",
    "image_cost_logs",
    "schedules",
    "event_triggers",
    "briefing_configs",
    "appointment_outcomes",
    "canary_runs",
    "voice_calls",
    "call_transcripts",
    "call_actions",
    "phone_contacts",
    "outbound_call_logs",
    "inbound_messages",
)


async def fill_row_ids(db: aiosqlite.Connection, *tables: str) -> None:
    """Give raw-seeded rows the id the model would have minted.

    A plain `INSERT` that names no `id` leaves it NULL, because the generator
    is a Python-side column default rather than something the database knows
    how to do — there is no UUIDv7 function in SQLite or in Postgres 16. A
    NULL primary key is worse than it sounds: SQLAlchemy discards such a row
    when loading an entity, so the seed appears to vanish rather than fail.

    Call this after seeding. `tables` defaults to every converted table, and
    a table this database does not have is skipped — several of these suites
    build only the corner of the schema they need.
    """
    present = {row[0] for row in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in tables or UUID_KEYED:
        if table not in present:
            continue
        columns = {row[1] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
        if "id" not in columns:
            # A suite that hand-rolled a cut-down table to exercise one read.
            continue
        rows = await db.execute_fetchall(f"SELECT rowid FROM {table} WHERE id IS NULL")  # noqa: S608 - fixed names
        for (rowid,) in rows:
            await db.execute(f"UPDATE {table} SET id = ? WHERE rowid = ?", (new_id(), rowid))  # noqa: S608
    await db.commit()

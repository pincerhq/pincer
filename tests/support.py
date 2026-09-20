"""Helpers shared by tests that seed the database through raw SQL.

Several suites predate the repository layer and still seed with `aiosqlite`
against the real migrated schema. That is a deliberate seam — it is how they
prove the read path copes with rows it did not write — but it bypasses the
models, and since migration 0017 the models are where a row's id comes from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.db.ids import Uuid7Sequence, new_id

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


_MINTED: dict[str, str] = {}
_SEQUENCE = Uuid7Sequence()
#: A fixed point in time, so a run's ids are stable and readable in failures.
_EPOCH_MS = 1_788_256_800_000


def seeded_id(label: str) -> str:
    """A stable UUIDv7 for a test's shorthand id — `seeded_id("m1")`.

    Tests read better naming rows `m1`, `m2`, `call-a` than pasting uuids, but
    `Uuid7` refuses anything that is not one (on SQLite too, deliberately: a
    lenient branch there would let the suite pass locally and fail in CI's
    postgres job). This keeps the shorthand and mints a real id behind it.

    Same label, same id, for the life of the process — so a test can seed with
    one and assert with the other. Ids are handed out in first-use order, so
    `m1` sorts before `m2` and reads that order back from the database.
    """
    if label not in _MINTED:
        _MINTED[label] = _SEQUENCE.next(_EPOCH_MS)
    return _MINTED[label]


def label_of(value: object) -> str:
    """The shorthand behind a `seeded_id`, for readable assertions.

    `[label_of(row["id"]) for row in rows] == ["m2", "m1"]` says what the test
    means; comparing raw uuids would not.
    """
    text = str(value)
    for label, minted in _MINTED.items():
        if minted == text:
            return label
    return text

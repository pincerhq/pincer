"""
Transcript retention — GDPR storage limitation (Art. 5(1)(e)) for voice data.

Purges rows older than PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS from the voice
call tables. Runs as a scheduled cron action (``retention_purge``); every purge
that deletes data is written to the audit log.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

# Table -> timestamp column holding an ISO-8601 UTC string (lexicographic
# comparison is chronological for these).
RETENTION_TABLES: dict[str, str] = {
    "voice_calls": "started_at",
    "call_transcripts": "timestamp",
    "call_actions": "timestamp",
    # Sprint 12: a taken message is personal data of the caller
    "inbound_messages": "created_at",
    # Sprint 8 (T8.3/T8.5): the abuse gate's dial log is personal data too and
    # only needs to outlive the longest limit window (a day / the cooldown).
    # `do_not_call` is deliberately NOT here: it records an Art. 21 objection,
    # and purging it would silently re-enable calls the callee refused.
    "outbound_call_log": "placed_at",
    # `call_analytics` is deliberately NOT here either: talk ratio, silence and
    # sentiment are derived numbers about a call, not a recording of it, so
    # they outlive the transcript like the Sprint 13 thread summaries do. Its
    # `sentiment_rationale` is the exception — that sentence may quote what was
    # said, so `purge_expired_voice_data` NULLs it on the same schedule as the
    # transcript it was drawn from.
    # Sprint 13: `call_threads` and `call_thread_members` are deliberately NOT
    # here either. A thread's rolling summary and commitments are DERIVED
    # facts (the Sprint 3 T3.3 memory-note precedent), and the member rows are
    # what keeps a purged call visible in its thread as a stub (sid, date,
    # outcome code) with no transcript. Threads are closed by the §5
    # auto-close job, not by the transcript purge.
}


async def ensure_schema_for_connection(db: aiosqlite.Connection) -> None:
    """Bring the database behind an open connection to the latest revision.

    Schema DDL for the voice tables is owned entirely by the Alembic
    revisions under `pincer.db.migrations.versions` (0005-0009). This helper
    only resolves which file the caller's connection is attached to and hands
    it to the migration runner, which is synchronous and therefore has to run
    off the event loop.

    `PRAGMA database_list` reports the resolved filename of the `main`
    schema; it is empty for an in-memory database, which Alembic cannot
    migrate through a separate connection, so that is rejected explicitly
    rather than silently skipped.
    """
    from pincer.db import ensure_schema_current

    rows = await db.execute_fetchall("PRAGMA database_list")
    for _seq, name, filename in rows:
        if name == "main" and filename:
            await asyncio.to_thread(ensure_schema_current, Path(filename))
            return

    raise RuntimeError("Alembic schema management requires a file-backed SQLite database")


async def ensure_voice_tables(db: aiosqlite.Connection) -> None:
    """Compatibility entry point for voice callers — see `ensure_schema_for_connection`."""
    await ensure_schema_for_connection(db)


async def _null_expired_rationales(db: aiosqlite.Connection, cutoff: str) -> int:
    """Blank sentiment rationales for calls older than the retention cutoff.

    Returns the number of rows changed. A missing table is not an error: a
    deployment that has never run an analysed call simply has nothing to redact.
    """
    # The voice_calls rows for these calls are usually already gone by the time
    # this runs (the table loop above deletes them first), so the analytics
    # row's own timestamp is the primary test; the subquery covers the case
    # where the call row is still present but already past the cutoff.
    try:
        cursor = await db.execute(
            "UPDATE call_analytics SET sentiment_rationale = NULL "
            "WHERE sentiment_rationale IS NOT NULL AND ("
            "    created_at < ? "
            "    OR call_sid IN (SELECT call_sid FROM voice_calls WHERE started_at < ?)"
            ")",
            (cutoff, cutoff),
        )
    except aiosqlite.OperationalError:
        return 0
    return int(cursor.rowcount or 0)


async def purge_expired_voice_data(
    db_path: str | Path,
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete voice rows older than the retention window.

    Returns per-table deletion counts. ``retention_days <= 0`` means keep
    forever (no-op). Missing tables are skipped, not created.
    """
    if retention_days <= 0:
        return {}

    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat()
    deleted: dict[str, int] = {}

    async with aiosqlite.connect(str(db_path)) as db:
        for table, ts_column in RETENTION_TABLES.items():
            exists = await db.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not exists:
                continue
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE {ts_column} < ?",  # noqa: S608 - identifiers from module constant
                (cutoff,),
            )
            if cursor.rowcount > 0:
                deleted[table] = cursor.rowcount

        # The analytics row survives; the one field that can quote the call
        # does not. Keeping a grounded rationale ("said the third delay was
        # unacceptable") after its transcript is gone would preserve exactly
        # the content the purge exists to remove.
        redacted = await _null_expired_rationales(db, cutoff)
        if redacted:
            deleted["call_analytics.sentiment_rationale"] = redacted
        await db.commit()

    if deleted:
        logger.info("Retention purge (cutoff=%s): %s", cutoff, deleted)
    return deleted


#: Telephony telemetry is TECHNICAL data — stage timings, span names, failure
#: codes, masked numbers. It carries no transcript and no audio, so it keeps its
#: own window (`PINCER_TELEPHONY_TELEMETRY_RETENTION_DAYS`) rather than the
#: transcript's. Diagnosing a latency regression needs weeks of history;
#: keeping what was *said* that long would not be justifiable.
TELEMETRY_TABLES: dict[str, str] = {
    "telephony_events": "ts_utc",
    "telephony_spans": "start_utc",
    "telephony_turns": "created_at",
    "telephony_calls": "registered_at",
}


async def purge_expired_telemetry(
    db_path: str | Path,
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete telephony telemetry older than its own window.

    Child rows go before the call row, so an interrupted purge leaves orphaned
    events rather than a call whose detail pages are empty.
    """
    if retention_days <= 0:
        return {}
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat()
    deleted: dict[str, int] = {}
    async with aiosqlite.connect(str(db_path)) as db:
        for table, ts_column in TELEMETRY_TABLES.items():
            exists = await db.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not exists:
                continue
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE {ts_column} < ?",  # noqa: S608 - identifiers from module constant
                (cutoff,),
            )
            if cursor.rowcount > 0:
                deleted[table] = cursor.rowcount
        await db.commit()
    if deleted:
        logger.info("Telephony telemetry purge (cutoff=%s): %s", cutoff, deleted)
    return deleted


async def run_retention_purge(settings: Settings) -> dict[str, int]:
    """Run the purge against the configured DB and audit any deletions."""
    retention_days = settings.voice_transcript_retention_days
    deleted = await purge_expired_voice_data(settings.db_path, retention_days)
    deleted.update(
        await purge_expired_telemetry(
            settings.db_path,
            int(getattr(settings, "telephony_telemetry_retention_days", 30) or 0),
        )
    )

    if deleted:
        from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger

        audit = await get_audit_logger()
        await audit.log(
            AuditEntry(
                user_id="system",
                action=AuditAction.RETENTION_PURGE,
                input_summary=f"retention_days={retention_days}",
                output_summary=", ".join(f"{table}: {count} row(s)" for table, count in deleted.items()),
                metadata={"deleted": deleted, "retention_days": retention_days},
            )
        )
    return deleted


def make_retention_handler(settings: Settings) -> Any:
    """Build a CronScheduler action handler for ``retention_purge``."""

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        try:
            await run_retention_purge(settings)
        except Exception:
            logger.exception("Voice retention purge failed")
        return None  # nothing to send to the user

    return _handler

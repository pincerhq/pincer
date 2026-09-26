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

if TYPE_CHECKING:
    import aiosqlite

    from pincer.config import Settings

from pincer.services import retention as retention_policy
from pincer.services.retention import RetentionService

logger = logging.getLogger(__name__)

# Table -> timestamp column, derived from the policy in
# `pincer.services.retention` so the two cannot drift. What is absent is the
# point of this note:
#
# * `do_not_call_numbers` — it records an Art. 21 objection; purging it would
#   silently re-enable calls the callee refused.
# * `call_analytics` — talk ratio, silence and sentiment are derived numbers
#   about a call, not a recording of it, so they outlive the transcript like
#   the Sprint 13 thread summaries do. Its `sentiment_rationale` is the
#   exception: that sentence may quote what was said, so the purge NULLs it on
#   the same schedule as the transcript it was drawn from.
# * `call_threads` / `call_thread_members` — a thread's rolling summary and
#   commitments are DERIVED facts (the Sprint 3 T3.3 memory-note precedent),
#   and the member rows are what keeps a purged call visible in its thread as a
#   stub (sid, date, outcome code) with no transcript. Threads are closed by
#   the §5 auto-close job, not by the transcript purge.
RETENTION_TABLES: dict[str, str] = {
    str(model.__tablename__): column.key for model, column in retention_policy.VOICE_TABLES
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
    service = await RetentionService.for_path(Path(str(db_path)))
    deleted = await service.purge_voice(cutoff)

    if deleted:
        logger.info("Retention purge (cutoff=%s): %s", cutoff, deleted)
    return deleted


#: Telephony telemetry is TECHNICAL data — stage timings, span names, failure
#: codes, masked numbers. It carries no transcript and no audio, so it keeps its
#: own window (`PINCER_TELEPHONY_TELEMETRY_RETENTION_DAYS`) rather than the
#: transcript's. Diagnosing a latency regression needs weeks of history;
#: keeping what was *said* that long would not be justifiable.
TELEMETRY_TABLES: dict[str, str] = {
    str(model.__tablename__): column.key for model, column in retention_policy.TELEMETRY_TABLES
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
    service = await RetentionService.for_path(Path(str(db_path)))
    deleted = await service.purge_telemetry(cutoff)
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

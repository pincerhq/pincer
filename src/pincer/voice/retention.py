"""
Transcript retention — GDPR storage limitation (Art. 5(1)(e)) for voice data.

Purges rows older than PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS from the voice
call tables. Runs as a scheduled cron action (``retention_purge``); every purge
that deletes data is written to the audit log.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

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
}

VOICE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS voice_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL DEFAULT 'inbound',
    from_number TEXT DEFAULT '',
    to_number TEXT DEFAULT '',
    pincer_user_id TEXT DEFAULT '',
    recording_enabled INTEGER DEFAULT 0,
    consent_given INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    -- Sprint 9 (T9.3): stable failure taxonomy, one code per terminated call.
    -- '' until the call ends; 'none' means it completed successfully.
    failure_code TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    language TEXT DEFAULT '',
    report_delivered_at TEXT,
    -- Sprint 12: receptionist intent
    inbound_intent TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS call_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    is_final INTEGER DEFAULT 1,
    state TEXT DEFAULT '',
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    tool_name TEXT DEFAULT '',
    input_summary TEXT DEFAULT '',
    output_summary TEXT DEFAULT '',
    user_confirmed INTEGER,
    timestamp TEXT NOT NULL,
    -- Sprint 11 (in-call tools): policy tier, approval mode, deny reason
    tier TEXT DEFAULT '',
    approval_mode TEXT DEFAULT '',
    deny_reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT NOT NULL,
    caller_name TEXT DEFAULT '',
    caller_name_unverified INTEGER DEFAULT 0,
    callback_number TEXT DEFAULT '',
    callback_unverified INTEGER DEFAULT 0,
    matter TEXT DEFAULT '',
    urgent INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_to_owner_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_call ON inbound_messages(call_sid);
CREATE INDEX IF NOT EXISTS idx_call_transcripts_call ON call_transcripts(call_id);
CREATE INDEX IF NOT EXISTS idx_call_transcripts_ts ON call_transcripts(timestamp);
CREATE INDEX IF NOT EXISTS idx_call_actions_call ON call_actions(call_id);
CREATE INDEX IF NOT EXISTS idx_call_actions_ts ON call_actions(timestamp);
CREATE INDEX IF NOT EXISTS idx_voice_calls_started ON voice_calls(started_at);
"""


# Sprint 9 columns added to a table that already exists in every deployment.
# SQLite has no `ADD COLUMN IF NOT EXISTS`, so the project-wide pattern is to
# try and swallow the "duplicate column" error.
_VOICE_CALLS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("failure_code", "TEXT DEFAULT ''"),
    ("engine", "TEXT DEFAULT ''"),
    ("language", "TEXT DEFAULT ''"),
    # T9.5: when the post-call report actually reached the initiating user.
    # The gap from ended_at is the report-delivery SLI.
    ("report_delivered_at", "TEXT"),
    # Sprint 12: question|message|appointment|human|unknown|after_hours ('' = not a receptionist call)
    ("inbound_intent", "TEXT DEFAULT ''"),
)


# Sprint 11 (in-call tools): mirrors docs/migrations/011_in_call_tools.sql.
_CALL_ACTIONS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("tier", "TEXT DEFAULT ''"),
    ("approval_mode", "TEXT DEFAULT ''"),
    ("deny_reason", "TEXT DEFAULT ''"),
)


async def _add_columns(db: aiosqlite.Connection, table: str, migrations: tuple[tuple[str, str], ...]) -> None:
    for column, ddl in migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # noqa: S608 - module constants
        except aiosqlite.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


async def ensure_voice_tables(db: aiosqlite.Connection) -> None:
    """Create the voice call tables if they don't exist, and migrate old ones."""
    await db.executescript(VOICE_TABLES_SQL)
    await _add_columns(db, "voice_calls", _VOICE_CALLS_MIGRATIONS)
    await _add_columns(db, "call_actions", _CALL_ACTIONS_MIGRATIONS)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_failure ON voice_calls(failure_code)")
    await db.commit()


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
        await db.commit()

    if deleted:
        logger.info("Retention purge (cutoff=%s): %s", cutoff, deleted)
    return deleted


async def run_retention_purge(settings: Settings) -> dict[str, int]:
    """Run the purge against the configured DB and audit any deletions."""
    retention_days = settings.voice_transcript_retention_days
    deleted = await purge_expired_voice_data(settings.db_path, retention_days)

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

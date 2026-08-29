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
    inbound_intent TEXT DEFAULT '',
    -- Sprint 13 (call threads): the matter this call belongs to ('' = threadless)
    thread_id TEXT DEFAULT '',
    thread_attach_kind TEXT DEFAULT '',
    -- Call briefing: what the agent was told to do, verbatim ('' = inbound/legacy)
    briefing_json TEXT DEFAULT ''
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
CREATE TABLE IF NOT EXISTS call_threads (
    thread_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    origin TEXT NOT NULL,
    primary_number TEXT DEFAULT '',
    contact_name TEXT DEFAULT '',
    language TEXT DEFAULT '',
    rolling_summary TEXT DEFAULT '',
    open_commitments TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS call_thread_members (
    call_sid TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    attach_kind TEXT NOT NULL DEFAULT '',
    attached_at TEXT NOT NULL,
    call_started_at TEXT DEFAULT '',
    direction TEXT DEFAULT '',
    outcome_code TEXT DEFAULT '',
    task_result TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS call_analytics (
    call_sid TEXT PRIMARY KEY,
    agent_speech_ms INTEGER,
    caller_speech_ms INTEGER,
    silence_ms INTEGER,
    overlap_ms INTEGER,
    interruptions INTEGER DEFAULT 0,
    talk_ratio REAL,
    method TEXT NOT NULL,
    sentiment TEXT,
    sentiment_trajectory TEXT,
    sentiment_rationale TEXT,
    sentiment_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_analytics_sentiment ON call_analytics(sentiment);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_call ON inbound_messages(call_sid);
CREATE INDEX IF NOT EXISTS idx_threads_number_status ON call_threads(primary_number, status);
CREATE INDEX IF NOT EXISTS idx_thread_members_thread ON call_thread_members(thread_id);
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
    # Sprint 13 (call threads): '' = threadless (pre-Sprint-13 calls stay that
    # way — §2 forbids retroactive heuristic grouping).
    ("thread_id", "TEXT DEFAULT ''"),
    ("thread_attach_kind", "TEXT DEFAULT ''"),
    # Call briefing: the task the agent was given, stored verbatim so the
    # dashboard can show exactly what it was told.
    ("briefing_json", "TEXT DEFAULT ''"),
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
    # Sprint 13: indexes the thread columns the migration above just added —
    # they cannot live in VOICE_TABLES_SQL, which runs before _add_columns.
    await db.execute("CREATE INDEX IF NOT EXISTS idx_calls_thread ON voice_calls(thread_id)")
    await db.commit()


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

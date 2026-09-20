"""Tests for the Alembic-managed schema in pincer.db."""

import contextlib
import importlib.util
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from pincer.db import build_config, engine, ensure_schema_current

EXPECTED_TABLES = {
    "conversations",
    "memories",
    "memories_fts",
    "entities",
    "sessions",
    "identity_profiles",
    "channel_identities",
    "audit_logs",
    "schedules",
    "event_triggers",
    "briefing_configs",
    "cost_logs",
    "image_cost_logs",
    "registry_skills",
    "expenses",
    "habits",
    "habit_checkins",
    "pomodoro_sessions",
    "discord_threads",
    "voice_calls",
    "call_transcripts",
    "call_actions",
    "phone_contacts",
}


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def test_ensure_schema_current_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)

    tables = _tables(db_path)
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables


def test_ensure_schema_current_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)
    engine._ensured_paths.clear()  # force a genuine second Alembic run, not the in-process cache guard
    ensure_schema_current(db_path)  # must not raise or duplicate anything

    con = sqlite3.connect(str(db_path))
    try:
        count = con.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_downgrade_base_then_upgrade_head_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "head")
    assert _tables(db_path) >= EXPECTED_TABLES

    command.downgrade(cfg, "base")
    tables_after_downgrade = _tables(db_path)
    assert not (EXPECTED_TABLES & tables_after_downgrade)

    command.upgrade(cfg, "head")
    assert _tables(db_path) >= EXPECTED_TABLES


def test_memories_fts5_and_sync_triggers_present(tmp_path: Path) -> None:
    """FTS5 is SQLite-only — confirm the virtual table and its sync triggers exist."""
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("INSERT INTO memories (id, user_id, content, created_at) VALUES ('m1', 'u1', 'hello world', 0)")
        con.commit()
        rows = con.execute("SELECT content FROM memories_fts WHERE memories_fts MATCH 'hello'").fetchall()
    finally:
        con.close()
    assert rows == [("hello world",)]


def test_legacy_identity_map_is_migrated_and_dropped(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"

    legacy = sqlite3.connect(str(db_path))
    try:
        legacy.execute(
            """
            CREATE TABLE identity_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pincer_user_id TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER,
                whatsapp_phone TEXT,
                display_name TEXT,
                preferred_channel TEXT DEFAULT 'telegram'
            )
            """
        )
        legacy.execute(
            "INSERT INTO identity_map (pincer_user_id, telegram_user_id, whatsapp_phone, display_name) "
            "VALUES ('usr_abc', 555111, '491234567890', 'Alice')"
        )
        legacy.commit()
    finally:
        legacy.close()

    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='identity_map'").fetchone() is None
        meta = con.execute("SELECT pincer_user_id, preferred_channel, display_name FROM identity_profiles").fetchall()
        assert meta == [("usr_abc", "telegram", "Alice")]
        links = {
            (channel, channel_user_id)
            for channel, channel_user_id in con.execute(
                "SELECT channel, channel_user_id FROM channel_identities WHERE pincer_user_id = 'usr_abc'"
            ).fetchall()
        }
        assert links == {("telegram", "555111"), ("whatsapp", "491234567890")}
    finally:
        con.close()


def test_identity_profiles_has_email_timezone_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(identity_profiles)").fetchall()}
    finally:
        con.close()
    assert {"email", "timezone"} <= cols


def test_legacy_audit_db_is_imported_into_unified_db(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    legacy_audit_path = tmp_path / "audit.db"

    legacy_audit = sqlite3.connect(str(legacy_audit_path))
    try:
        legacy_audit.execute(
            """
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_id TEXT,
                action TEXT NOT NULL,
                tool TEXT,
                input_summary TEXT,
                output_summary TEXT,
                approved INTEGER DEFAULT 1,
                cost_usd REAL DEFAULT 0.0,
                duration_ms INTEGER,
                ip_address TEXT,
                channel TEXT,
                metadata_json TEXT
            )
            """
        )
        legacy_audit.execute(
            "INSERT INTO audit_log (timestamp, user_id, action) VALUES ('2026-01-01T00:00:00', 'usr_abc', 'tool_call')"
        )
        legacy_audit.commit()
    finally:
        legacy_audit.close()

    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT user_id, action FROM audit_logs").fetchall()
    finally:
        con.close()
    assert rows == [("usr_abc", "tool_call")]
    # Legacy file is left untouched on disk, not deleted.
    assert legacy_audit_path.is_file()


def test_voice_schema_reconcile_migrates_0001_legacy_rows(tmp_path: Path) -> None:
    """0005 must upgrade the dormant 0001 voice schema without losing data."""
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    # Reproduce the exact schema state described in the PR review:
    # Alembic baseline is present, but the active voice schema is not.
    command.upgrade(cfg, "0004")

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO voice_calls (
                id,
                user_id,
                direction,
                caller_number,
                target_number,
                status,
                engine,
                started_at,
                ended_at,
                recording_consent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CA_legacy",
                "usr_legacy",
                "outbound",
                "+491111111111",
                "+492222222222",
                "completed",
                "twilio",
                "2026-09-01T10:00:00+00:00",
                "2026-09-01T10:02:00+00:00",
                1,
            ),
        )
        con.execute(
            """
            INSERT INTO call_transcripts (
                call_id,
                speaker,
                text,
                confidence,
                is_final,
                state,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CA_legacy",
                "caller",
                "legacy transcript",
                0.95,
                1,
                "final",
                "2026-09-01T10:01:00+00:00",
            ),
        )
        con.execute(
            """
            INSERT INTO call_actions (
                call_id,
                action_type,
                tool_name,
                input_summary,
                output_summary,
                user_confirmed,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CA_legacy",
                "tool_call",
                "calendar_today",
                "",
                "ok",
                1,
                "2026-09-01T10:01:30+00:00",
            ),
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(str(db_path))
    try:
        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(voice_calls)").fetchall()}
        assert {
            "call_sid",
            "direction",
            "from_number",
            "to_number",
            "pincer_user_id",
            "started_at",
            "ended_at",
            "failure_code",
            "engine",
            "language",
            "report_delivered_at",
        } <= voice_cols

        row = con.execute(
            """
            SELECT
                call_sid,
                direction,
                from_number,
                to_number,
                pincer_user_id,
                engine,
                consent_given,
                started_at,
                ended_at
            FROM voice_calls
            WHERE call_sid = 'CA_legacy'
            """
        ).fetchone()
        assert row == (
            "CA_legacy",
            "outbound",
            "+491111111111",
            "+492222222222",
            "usr_legacy",
            "twilio",
            1,
            "2026-09-01T10:00:00+00:00",
            "2026-09-01T10:02:00+00:00",
        )

        transcript = con.execute(
            """
            SELECT call_id, speaker, text
            FROM call_transcripts
            WHERE call_id = 'CA_legacy'
            """
        ).fetchone()
        assert transcript == (
            "CA_legacy",
            "caller",
            "legacy transcript",
        )

        action = con.execute(
            """
            SELECT
                call_id,
                action_type,
                tool_name,
                tier,
                approval_mode,
                deny_reason
            FROM call_actions
            WHERE call_id = 'CA_legacy'
            """
        ).fetchone()
        assert action == (
            "CA_legacy",
            "tool_call",
            "calendar_today",
            "",
            "",
            "",
        )

        tables = _tables(db_path)
        assert {
            "do_not_call_numbers",
            "outbound_call_logs",
        } <= tables
    finally:
        con.close()


def test_voice_receptionist_schema_is_alembic_managed(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "0006")

    con = sqlite3.connect(str(db_path))
    try:
        tables = _tables(db_path)
        assert "inbound_messages" in tables

        message_cols = {row[1] for row in con.execute("PRAGMA table_info(inbound_messages)").fetchall()}
        assert {
            "call_sid",
            "caller_name",
            "caller_name_unverified",
            "callback_number",
            "callback_unverified",
            "matter",
            "urgent",
            "created_at",
            "delivered_to_owner_at",
        } <= message_cols

        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(voice_calls)").fetchall()}
        assert "inbound_intent" in voice_cols

        indexes = {row[1] for row in con.execute("PRAGMA index_list(inbound_messages)").fetchall()}
        assert "idx_inbound_messages_call" in indexes
    finally:
        con.close()


def test_voice_threads_schema_is_alembic_managed(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "0007")

    con = sqlite3.connect(str(db_path))
    try:
        assert {
            "call_threads",
            "call_thread_members",
        } <= _tables(db_path)

        thread_cols = {row[1] for row in con.execute("PRAGMA table_info(call_threads)").fetchall()}
        assert {
            "thread_id",
            "subject",
            "status",
            "origin",
            "primary_number",
            "rolling_summary",
            "open_commitments",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
        } <= thread_cols

        member_cols = {row[1] for row in con.execute("PRAGMA table_info(call_thread_members)").fetchall()}
        assert {
            "call_sid",
            "thread_id",
            "attach_kind",
            "attached_at",
            "call_started_at",
            "direction",
            "outcome_code",
            "task_result",
        } <= member_cols

        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(voice_calls)").fetchall()}
        assert {
            "thread_id",
            "thread_attach_kind",
        } <= voice_cols

        indexes = {row[1] for row in con.execute("PRAGMA index_list(call_threads)").fetchall()}
        assert "idx_threads_number_status" in indexes

        member_indexes = {row[1] for row in con.execute("PRAGMA index_list(call_thread_members)").fetchall()}
        assert "idx_thread_members_thread" in member_indexes
    finally:
        con.close()


def test_voice_briefing_schema_is_alembic_managed(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "0008")

    con = sqlite3.connect(str(db_path))
    try:
        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(voice_calls)").fetchall()}
        assert "briefing_json" in voice_cols

        con.execute(
            """
            INSERT INTO voice_calls (
                call_sid,
                started_at,
                briefing_json
            )
            VALUES (?, ?, ?)
            """,
            (
                "CA_brief",
                "2026-09-12T10:00:00+00:00",
                '{"task":"book appointment"}',
            ),
        )
        con.commit()

        value = con.execute(
            """
            SELECT briefing_json
            FROM voice_calls
            WHERE call_sid = 'CA_brief'
            """
        ).fetchone()
        assert value == ('{"task":"book appointment"}',)
    finally:
        con.close()


def test_voice_analytics_schema_is_alembic_managed(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "0009")

    con = sqlite3.connect(str(db_path))
    try:
        assert "call_analytics" in _tables(db_path)

        cols = {row[1] for row in con.execute("PRAGMA table_info(call_analytics)").fetchall()}
        assert {
            "call_sid",
            "agent_speech_ms",
            "caller_speech_ms",
            "silence_ms",
            "overlap_ms",
            "interruptions",
            "talk_ratio",
            "method",
            "sentiment",
            "sentiment_trajectory",
            "sentiment_rationale",
            "sentiment_reason",
            "created_at",
        } <= cols

        indexes = {row[1] for row in con.execute("PRAGMA index_list(call_analytics)").fetchall()}
        assert "idx_call_analytics_sentiment" in indexes
    finally:
        con.close()


def test_unversioned_modern_voice_schema_upgrades_to_head(
    tmp_path: Path,
) -> None:
    """A pre-Alembic call_sid voice DB must upgrade without legacy collisions."""
    db_path = tmp_path / "pincer.db"

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE voice_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT NOT NULL UNIQUE,
                direction TEXT NOT NULL DEFAULT 'inbound',
                from_number TEXT DEFAULT '',
                to_number TEXT DEFAULT '',
                pincer_user_id TEXT DEFAULT '',
                recording_enabled INTEGER DEFAULT 0,
                consent_given INTEGER DEFAULT 0,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """
        )
        con.execute(
            """
            INSERT INTO voice_calls (
                call_sid,
                direction,
                started_at,
                ended_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "CA_pre_alembic",
                "outbound",
                "2026-08-20T16:43:37+00:00",
                "2026-08-20T16:44:00+00:00",
            ),
        )
        con.commit()
    finally:
        con.close()

    cfg = build_config(db_path)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            """
            SELECT call_sid, direction
            FROM voice_calls
            WHERE call_sid = 'CA_pre_alembic'
            """
        ).fetchone()
        assert row == ("CA_pre_alembic", "outbound")

        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(voice_calls)").fetchall()}
        assert {
            "call_sid",
            "pincer_user_id",
            "failure_code",
            "inbound_intent",
            "thread_id",
            "briefing_json",
        } <= voice_cols

        assert {
            "call_transcripts",
            "call_actions",
            "phone_contacts",
            "inbound_messages",
            "call_threads",
            "call_thread_members",
            "call_analytics",
        } <= _tables(db_path)

        transcript_cols = {row[1] for row in con.execute("PRAGMA table_info(call_transcripts)").fetchall()}
        assert {
            "call_id",
            "speaker",
            "text",
            "confidence",
            "is_final",
            "state",
            "timestamp",
        } <= transcript_cols

        action_cols = {row[1] for row in con.execute("PRAGMA table_info(call_actions)").fetchall()}
        assert {
            "call_id",
            "action_type",
            "tool_name",
            "tier",
            "approval_mode",
            "deny_reason",
        } <= action_cols
    finally:
        con.close()


def test_unversioned_modern_voice_schema_backfills_missing_columns(
    tmp_path: Path,
) -> None:
    """An older pre-Alembic modern schema must gain the columns runtime DDL used to add.

    Before this branch, `voice_calls`/`call_actions` columns were reconciled by
    a runtime `ALTER TABLE ... ADD COLUMN` that swallowed SQLite's "duplicate
    column" error. Revision 0005 has to do the same job for a database created
    by one of those older runtime versions, without touching existing rows.
    """
    db_path = tmp_path / "pincer.db"

    con = sqlite3.connect(str(db_path))
    try:
        # The modern shape as it stood before Sprint 9/11 added their columns.
        con.execute(
            """
            CREATE TABLE voice_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT NOT NULL UNIQUE,
                direction TEXT NOT NULL DEFAULT 'inbound',
                from_number TEXT DEFAULT '',
                to_number TEXT DEFAULT '',
                pincer_user_id TEXT DEFAULT '',
                recording_enabled INTEGER DEFAULT 0,
                consent_given INTEGER DEFAULT 0,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE call_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                is_final INTEGER DEFAULT 1,
                state TEXT DEFAULT '',
                timestamp TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE call_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                tool_name TEXT DEFAULT '',
                input_summary TEXT DEFAULT '',
                output_summary TEXT DEFAULT '',
                user_confirmed INTEGER,
                timestamp TEXT NOT NULL
            )
            """
        )
        con.execute(
            "INSERT INTO voice_calls (call_sid, direction, started_at) VALUES (?, ?, ?)",
            ("CA_old_runtime", "inbound", "2026-08-21T09:00:00+00:00"),
        )
        con.execute(
            """
            INSERT INTO call_transcripts (call_id, speaker, text, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            ("CA_old_runtime", "caller", "kept across the upgrade", "2026-08-21T09:00:05+00:00"),
        )
        con.execute(
            """
            INSERT INTO call_actions (call_id, action_type, tool_name, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            ("CA_old_runtime", "tool_call", "calendar_today", "2026-08-21T09:00:10+00:00"),
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(build_config(db_path), "head")

    con = sqlite3.connect(str(db_path))
    try:
        # Rows survive: 0005 must backfill columns, not rebuild these tables.
        assert con.execute(
            """
            SELECT call_sid, failure_code, engine, language, report_delivered_at
            FROM voice_calls
            """
        ).fetchall() == [("CA_old_runtime", "", "", "", None)]

        assert con.execute("SELECT text FROM call_transcripts").fetchall() == [("kept across the upgrade",)]

        assert con.execute("SELECT call_id, tier, approval_mode, deny_reason FROM call_actions").fetchall() == [
            ("CA_old_runtime", "", "", "")
        ]

        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
        assert {
            "idx_voice_calls_started",
            "idx_voice_calls_failure",
            "idx_call_actions_call",
            "idx_call_transcripts_call",
            "idx_outbound_log_day",
        } <= indexes
    finally:
        con.close()


# The exact schema `pincer.voice.retention.ensure_voice_tables` used to create
# at runtime, before revisions 0005-0009 took ownership of it. Kept verbatim
# here as the fixture for the real-world upgrade path: a database built by that
# code has every modern voice table but no `alembic_version` row.
_PRE_ALEMBIC_RUNTIME_VOICE_SCHEMA = """
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
        failure_code TEXT DEFAULT '',
        engine TEXT DEFAULT '',
        language TEXT DEFAULT '',
        report_delivered_at TEXT,
        inbound_intent TEXT DEFAULT '',
        thread_id TEXT DEFAULT '',
        thread_attach_kind TEXT DEFAULT '',
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


def test_full_pre_alembic_runtime_voice_db_upgrades_to_head(tmp_path: Path) -> None:
    """The production upgrade path: a DB built by the old runtime DDL, with rows.

    Revision 0001 must skip its incompatible legacy voice block, 0005 must
    leave the existing tables in place, and 0006-0009 must be no-ops on the
    columns and tables that are already there — with every row intact.
    """
    db_path = tmp_path / "pincer.db"

    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(_PRE_ALEMBIC_RUNTIME_VOICE_SCHEMA)
        con.execute(
            """
            INSERT INTO voice_calls (call_sid, direction, from_number, to_number, started_at, inbound_intent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("CA_runtime", "inbound", "+493333333333", "+494444444444", "2026-09-02T08:00:00+00:00", "question"),
        )
        con.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, timestamp) VALUES (?, ?, ?, ?)",
            ("CA_runtime", "agent", "Guten Tag", "2026-09-02T08:00:03+00:00"),
        )
        con.execute(
            """
            INSERT INTO call_actions (call_id, action_type, tool_name, timestamp, tier, deny_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("CA_runtime", "tool_denied", "email_send", "2026-09-02T08:00:20+00:00", "X", "tier_x"),
        )
        con.execute(
            "INSERT INTO inbound_messages (call_sid, caller_name, matter, created_at) VALUES (?, ?, ?, ?)",
            ("CA_runtime", "Anna", "Rueckruf", "2026-09-02T08:01:00+00:00"),
        )
        con.execute(
            """
            INSERT INTO call_threads (thread_id, subject, origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("th_1", "Angebot", "inbound", "2026-09-02T08:00:00+00:00", "2026-09-02T08:02:00+00:00"),
        )
        con.execute(
            "INSERT INTO call_thread_members (call_sid, thread_id, attached_at) VALUES (?, ?, ?)",
            ("CA_runtime", "th_1", "2026-09-02T08:00:05+00:00"),
        )
        con.execute(
            "INSERT INTO call_analytics (call_sid, method, created_at) VALUES (?, ?, ?)",
            ("CA_runtime", "exact", "2026-09-02T08:02:10+00:00"),
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(build_config(db_path), "head")

    con = sqlite3.connect(str(db_path))
    try:
        assert con.execute(
            "SELECT call_sid, direction, from_number, inbound_intent, thread_id FROM voice_calls"
        ).fetchall() == [("CA_runtime", "inbound", "+493333333333", "question", "")]

        assert con.execute("SELECT speaker, text FROM call_transcripts").fetchall() == [("agent", "Guten Tag")]
        assert con.execute("SELECT tier, deny_reason FROM call_actions").fetchall() == [("X", "tier_x")]
        assert con.execute("SELECT caller_name, matter FROM inbound_messages").fetchall() == [("Anna", "Rueckruf")]
        assert con.execute("SELECT thread_id, subject FROM call_threads").fetchall() == [("th_1", "Angebot")]
        assert con.execute("SELECT call_sid, thread_id FROM call_thread_members").fetchall() == [("CA_runtime", "th_1")]
        assert con.execute("SELECT call_sid, method FROM call_analytics").fetchall() == [("CA_runtime", "exact")]

        # 0001 must still have created the non-voice schema it owns.
        assert {"memories", "identity_profiles", "audit_logs", "phone_contacts"} <= _tables(db_path)

        # 0005's own tables are additive here.
        assert {"do_not_call_numbers", "outbound_call_logs"} <= _tables(db_path)

        # The database is genuinely at head, not merely stamped.
        head = ScriptDirectory.from_config(build_config(db_path)).get_current_head()
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
    finally:
        con.close()


# ── 0011: plural table names ─────────────────────────────────────────


def _renames() -> tuple[tuple[str, str], ...]:
    """0011's own list, so the test cannot drift from the migration."""
    path = Path(engine.__file__).parent / "migrations" / "versions" / "0011_plural_table_names.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RENAMES


#: One row per renamed table, written at 0010 under the old name.
_SEED_0010 = {
    "audit_log": "INSERT INTO audit_log (timestamp, user_id, action) VALUES ('2026-01-01T00:00:00', 'usr_a', 'x')",
    "cost_log": "INSERT INTO cost_log (timestamp, provider, model, input_tokens, output_tokens, cost_usd) "
    "VALUES (1.0, 'anthropic', 'm', 1, 2, 0.5)",
    "image_cost_log": "INSERT INTO image_cost_log (timestamp, provider, model, cost_usd) VALUES (1.0, 'p', 'm', 0.1)",
    "outbound_call_log": "INSERT INTO outbound_call_log (phone_number, placed_at, local_day) "
    "VALUES ('+4930111', '2026-01-01T00:00:00', '2026-01-01')",
    "briefing_config": "INSERT INTO briefing_config (pincer_user_id) VALUES ('usr_a')",
    "identity_meta": "INSERT INTO identity_meta (pincer_user_id, display_name) VALUES ('usr_a', 'Alice')",
    "do_not_call": "INSERT INTO do_not_call (phone_number, added_at) VALUES ('+4930111', '2026-01-01T00:00:00')",
    "skill_registry": "INSERT INTO skill_registry (skill_id, name, version, install_path) VALUES ('s', 'n', '1', '/p')",
}
_LINK = "INSERT INTO channel_identities (channel, channel_user_id, pincer_user_id) VALUES ('telegram', '1', 'usr_a')"


def _uuid7_tables() -> tuple[tuple[str, str, str], ...]:
    """The table list migration 0017 converts, read from the revision itself."""
    spec = importlib.util.spec_from_file_location(
        "_rev_0017", Path(engine.__file__).parent / "migrations" / "versions" / "0017_uuid7_row_ids.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.TABLES)


def _shape(inspector: sa.Inspector, table: str) -> dict[str, object]:
    """Everything about a table except the type of its `id` column."""
    return {
        "columns": sorted((c["name"], bool(c["nullable"])) for c in inspector.get_columns(table) if c["name"] != "id"),
        "indexes": sorted(
            (i["name"], tuple(i["column_names"]), bool(i.get("unique"))) for i in inspector.get_indexes(table)
        ),
        "unique": sorted(
            (u.get("name") or "", tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
        ),
        "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
    }


@contextlib.contextmanager
def _connect(url: str):
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _config(url: str, tmp_path: Path):
    cfg = build_config(tmp_path / "unused.db")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_0011_renames_every_singular_table_and_keeps_its_rows(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0010")
    with _connect(migration_url) as conn:
        for insert in _SEED_0010.values():
            conn.execute(sa.text(insert))
        conn.execute(sa.text(_LINK))

    command.upgrade(cfg, "0011")

    with _connect(migration_url) as conn:
        inspector = sa.inspect(conn)
        tables = set(inspector.get_table_names())
        for old, new in _renames():
            assert old not in tables, old
            assert new in tables, new
            assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {new}")).scalar_one() == 1, new  # noqa: S608
        # The foreign key followed the table instead of dangling.
        (fk,) = inspector.get_foreign_keys("channel_identities")
        assert fk["referred_table"] == "identity_profiles"
        joined = conn.execute(
            sa.text(
                "SELECT p.display_name FROM channel_identities c "
                "JOIN identity_profiles p ON p.pincer_user_id = c.pincer_user_id"
            )
        ).scalar_one()
        assert joined == "Alice"


def test_0011_downgrades_back_to_the_old_names(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0011")
    command.downgrade(cfg, "0010")
    with _connect(migration_url) as conn:
        tables = set(sa.inspect(conn).get_table_names())
    for old, new in _renames():
        assert old in tables and new not in tables, (old, new)
    command.upgrade(cfg, "0011")  # and forward again


def test_0011_skips_tables_that_are_already_renamed(tmp_path):
    """A database that is partway there (or never had a table) still upgrades."""
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)
    command.upgrade(cfg, "0010")
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("ALTER TABLE cost_log RENAME TO cost_logs")
        con.execute("DROP TABLE skill_registry")
        con.commit()
    finally:
        con.close()
    command.upgrade(cfg, "0011")
    tables = _tables(db_path)
    assert "cost_logs" in tables and "cost_log" not in tables
    assert "registry_skills" not in tables and "skill_registry" not in tables
    assert "audit_logs" in tables


# ── 0017: autoincrement primary keys become UUIDv7 ───────────────────

#: One row per converted table, seeded at 0016 with the old integer keys.
#: `call_transcripts` and `inbound_messages` get several rows sharing one
#: timestamp, because the ordering they rely on is the point of the backfill.
_SEED_0016 = (
    "INSERT INTO audit_logs (id, timestamp, user_id, action) VALUES (1, '2026-09-01T10:00:00+00:00', 'usr_a', 'act')",
    "INSERT INTO cost_logs (id, timestamp, provider, model, input_tokens, output_tokens, cost_usd) "
    "VALUES (1, 1788256800.0, 'anthropic', 'm', 1, 1, 0.5)",
    "INSERT INTO image_cost_logs (id, timestamp, provider, model, cost_usd) "
    "VALUES (1, 1788256800.0, 'openai', 'm', 0.5)",
    "INSERT INTO schedules (id, pincer_user_id, name, cron_expr, action, next_run_at) "
    "VALUES (1, 'usr_a', 'nightly', '0 0 * * *', '{}', '2026-09-02T00:00:00+00:00')",
    "INSERT INTO event_triggers (id, trigger_type, trigger_key, pincer_user_id) VALUES (1, 'webhook', 'wh_1', 'usr_a')",
    "INSERT INTO briefing_configs (id, pincer_user_id) VALUES (1, 'usr_a')",
    "INSERT INTO appointment_outcomes (id, task_id, result, recorded_at) "
    "VALUES (1, 'task-1', 'booked', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO canary_runs (id, ran_at, ok) VALUES (1, '2026-09-01T10:00:00+00:00', 1)",
    "INSERT INTO voice_calls (id, call_sid, direction, started_at) "
    "VALUES (1, 'CA_one', 'inbound', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO call_actions (id, call_id, action_type, timestamp) "
    "VALUES (1, 'CA_one', 'dial', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO phone_contacts (id, name, phone_number) VALUES (1, 'Ada', '+15550001111')",
    "INSERT INTO outbound_call_logs (id, phone_number, placed_at, local_day) "
    "VALUES (1, '+15550001111', '2026-09-01T10:00:00+00:00', '2026-09-01')",
)

#: Five utterances in one second. Only the key keeps them in spoken order.
_SEED_TRANSCRIPTS = tuple(
    "INSERT INTO call_transcripts (id, call_id, speaker, text, timestamp) "
    f"VALUES ({index}, 'CA_one', 'caller', 'line {index}', '2026-09-01T10:00:00+00:00')"
    for index in range(1, 6)
)

_SEED_MESSAGES = tuple(
    "INSERT INTO inbound_messages (id, call_sid, created_at) "
    f"VALUES ({index}, 'CA_{index}', '2026-09-01T10:00:00+00:00')"
    for index in range(1, 4)
)

_CONVERTED = [table for table, _column, _kind in _uuid7_tables()]


def test_0017_gives_every_row_a_v7_uuid_and_keeps_it(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0016, *_SEED_TRANSCRIPTS, *_SEED_MESSAGES):
            conn.execute(sa.text(insert))

    command.upgrade(cfg, "0017")

    with _connect(migration_url) as conn:
        for table in _CONVERTED:
            ids = [row[0] for row in conn.execute(sa.text(f"SELECT id FROM {table}")).all()]  # noqa: S608
            assert ids, f"{table} lost its rows"
            for value in ids:
                assert uuid.UUID(str(value)).version == 7, f"{table}.id is not a v7 uuid: {value!r}"


def test_0017_keeps_same_timestamp_rows_in_their_original_order(migration_url, tmp_path):
    """The key is the tiebreaker `call_transcripts` and `inbound_messages` use.

    Fresh random ids would pass every other assertion here and silently
    scramble a call's transcript.
    """
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0016, *_SEED_TRANSCRIPTS, *_SEED_MESSAGES):
            conn.execute(sa.text(insert))

    command.upgrade(cfg, "0017")

    with _connect(migration_url) as conn:
        spoken = conn.execute(sa.text("SELECT text FROM call_transcripts ORDER BY timestamp, id")).all()
        assert [row[0] for row in spoken] == [f"line {index}" for index in range(1, 6)]

        calls = conn.execute(sa.text("SELECT call_sid FROM inbound_messages ORDER BY created_at DESC, id DESC")).all()
        assert [row[0] for row in calls] == ["CA_3", "CA_2", "CA_1"]


def test_0017_stamps_each_id_with_the_row_s_own_creation_time(migration_url, tmp_path):
    """Not merely "in order" — the id has to carry *when the row was made*.

    Minting fresh ids during the migration would keep the relative order
    (uuid7 is monotonic per process) while claiming every historical row was
    written the moment the migration ran. Then `ORDER BY id` stops agreeing
    with `ORDER BY created_at`, and a 2026 row is indistinguishable from a
    2020 one.
    """
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0016, *_SEED_TRANSCRIPTS, *_SEED_MESSAGES):
            conn.execute(sa.text(insert))

    command.upgrade(cfg, "0017")

    seeded_ms = int(datetime(2026, 9, 1, 10, 0, tzinfo=UTC).timestamp() * 1000)
    with _connect(migration_url) as conn:
        for table in ("audit_logs", "voice_calls", "call_transcripts", "canary_runs", "cost_logs"):
            row_id = conn.execute(sa.text(f"SELECT id FROM {table} LIMIT 1")).scalar_one()  # noqa: S608
            stamped = uuid.UUID(str(row_id)).int >> 80
            assert stamped == seeded_ms, f"{table}.id carries {stamped}, not the row's own time {seeded_ms}"


def test_0017_leaves_every_table_otherwise_exactly_as_it_was(migration_url, tmp_path):
    """The SQLite half rebuilds each table from reflection rather than from
    frozen DDL, so this is what guarantees nothing was dropped on the way:
    same columns, same nullability, same indexes, same unique constraints."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        before = {table: _shape(sa.inspect(conn), table) for table in _CONVERTED}

    command.upgrade(cfg, "0017")

    with _connect(migration_url) as conn:
        after = {table: _shape(sa.inspect(conn), table) for table in _CONVERTED}

    for table in _CONVERTED:
        assert after[table]["columns"] == before[table]["columns"], table
        assert after[table]["indexes"] == before[table]["indexes"], table
        assert after[table]["unique"] == before[table]["unique"], table
        assert after[table]["pk"] == before[table]["pk"], table


def test_0017_round_trips(migration_url, tmp_path):
    """Down is lossy by design — ids are renumbered, not restored — so this
    asserts the shape survives and the rows do, not the original values."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0016, *_SEED_TRANSCRIPTS, *_SEED_MESSAGES):
            conn.execute(sa.text(insert))

    command.upgrade(cfg, "0017")
    command.downgrade(cfg, "0016")

    with _connect(migration_url) as conn:
        ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM call_transcripts ORDER BY id")).all()]
        assert ids == [1, 2, 3, 4, 5]

    command.upgrade(cfg, "0017")
    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM call_transcripts")).scalar_one() == 5

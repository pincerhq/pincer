"""Tests for the Alembic-managed schema in pincer.db."""

import sqlite3
from pathlib import Path

from alembic import command

from pincer.db import build_config, engine, ensure_schema_current

EXPECTED_TABLES = {
    "conversations",
    "memories",
    "memories_fts",
    "entities",
    "sessions",
    "identity_meta",
    "channel_identities",
    "audit_log",
    "schedules",
    "event_triggers",
    "briefing_config",
    "cost_log",
    "image_cost_log",
    "skill_registry",
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
        meta = con.execute("SELECT pincer_user_id, preferred_channel, display_name FROM identity_meta").fetchall()
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


def test_identity_meta_has_email_timezone_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(identity_meta)").fetchall()}
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
        rows = con.execute("SELECT user_id, action FROM audit_log").fetchall()
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
        voice_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(voice_calls)"
            ).fetchall()
        }
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
                engine
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
            "do_not_call",
            "outbound_call_log",
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

        message_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(inbound_messages)"
            ).fetchall()
        }
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

        voice_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(voice_calls)"
            ).fetchall()
        }
        assert "inbound_intent" in voice_cols

        indexes = {
            row[1]
            for row in con.execute(
                "PRAGMA index_list(inbound_messages)"
            ).fetchall()
        }
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

        thread_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(call_threads)"
            ).fetchall()
        }
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

        member_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(call_thread_members)"
            ).fetchall()
        }
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

        voice_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(voice_calls)"
            ).fetchall()
        }
        assert {
            "thread_id",
            "thread_attach_kind",
        } <= voice_cols

        indexes = {
            row[1]
            for row in con.execute(
                "PRAGMA index_list(call_threads)"
            ).fetchall()
        }
        assert "idx_threads_number_status" in indexes

        member_indexes = {
            row[1]
            for row in con.execute(
                "PRAGMA index_list(call_thread_members)"
            ).fetchall()
        }
        assert "idx_thread_members_thread" in member_indexes
    finally:
        con.close()


def test_voice_briefing_schema_is_alembic_managed(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)

    command.upgrade(cfg, "0008")

    con = sqlite3.connect(str(db_path))
    try:
        voice_cols = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(voice_calls)"
            ).fetchall()
        }
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

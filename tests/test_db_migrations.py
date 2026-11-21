"""Tests for the Alembic-managed schema in pincer.db."""

import contextlib
import importlib.util
import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from pincer.db import build_config, engine, ensure_schema_current
from pincer.db.metadata import LEGACY_VERSION_TABLE, MIGRATION_ADVISORY_KEY, VERSION_TABLE

# Runs the real Alembic upgrade rather than copying the template database.
pytestmark = pytest.mark.real_migrations

EXPECTED_TABLES = {
    "pincer_conversations",
    "pincer_memories",
    "pincer_memories_fts",
    "pincer_entities",
    "pincer_sessions",
    "pincer_identity_profiles",
    "pincer_identity_channels",
    "pincer_identity_credentials",
    "pincer_auth_sessions",
    "pincer_onboarding",
    "pincer_audit_logs",
    "pincer_schedules",
    "pincer_event_triggers",
    "pincer_briefing_configs",
    "pincer_cost_logs",
    "pincer_image_cost_logs",
    "pincer_voice_calls",
    "pincer_call_transcripts",
    "pincer_call_actions",
    "pincer_phone_contacts",
}

#: Columns migrations after 0018 add on purpose to the tables 0017/0018 rebuild.
_ADDED_AFTER_0018 = {
    "call_transcripts": {"delivery_reason"},  # 0027
    "cost_logs": {"cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"},  # 0029
}

_DORMANT = {
    "pincer_registry_skills",
    "pincer_expenses",
    "pincer_habits",
    "pincer_habit_checkins",
    "pincer_pomodoro_sessions",
    "pincer_discord_threads",
}

_THREAD_0032 = "01890000-0000-7000-8000-000000000001"
_CALL_0032 = "01890000-0000-7000-8000-000000000002"

_SEED_0032 = (
    "INSERT INTO pincer_call_threads (thread_id, subject, origin, created_at, updated_at) "
    f"VALUES ('{_THREAD_0032}', 's', 'inbound', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
    # Inserted newest first, so key order and time order disagree.
    "INSERT INTO pincer_call_costs (call_sid, recorded_at) VALUES ('CA1', '2026-03-01T00:00:00')",
    "INSERT INTO pincer_call_costs (call_sid, recorded_at) VALUES ('CA2', '2026-02-01T00:00:00')",
    "INSERT INTO pincer_call_analytics (call_sid, method, created_at) VALUES ('CA1', 'vad', '2026-03-01T00:00:00')",
    "INSERT INTO pincer_call_origins (call_sid, user_id, created_at) VALUES ('CA1', 'chat-1', '2026-03-01T00:00:00')",
    "INSERT INTO pincer_call_thread_members (call_sid, thread_id, attached_at) "
    f"VALUES ('CA1', '{_THREAD_0032}', '2026-03-01T00:00:00')",
    "INSERT INTO pincer_do_not_call_numbers (phone_number, added_at) VALUES ('+491', '2026-03-01T00:00:00')",
    # Same timestamp: the monotonic clock and then `seq` decide.
    "INSERT INTO pincer_telephony_events (event_id, call_id, name, ts_utc, mono_ns, seq) "
    f"VALUES ('e-a', '{_CALL_0032}', 'n', '2026-03-01T00:00:00', 5, 1)",
    "INSERT INTO pincer_telephony_events (event_id, call_id, name, ts_utc, mono_ns, seq) "
    f"VALUES ('e-b', '{_CALL_0032}', 'n', '2026-03-01T00:00:00', 2, 1)",
    "INSERT INTO pincer_telephony_spans (span_id, call_id, name, start_utc) "
    f"VALUES ('s1', '{_CALL_0032}', 'llm', '2026-03-01T00:00:00')",
)

#: (table, old name) of the UUIDv7 keys 0032 renames `id`.
_RENAMED_0032 = (
    ("pincer_telephony_turns", "turn_id"),
    ("pincer_telephony_calls", "call_id"),
    ("pincer_call_threads", "thread_id"),
)

#: (table, old name, new name) of the outside keys 0032 renames.
_REFERENCES_0032 = (
    ("pincer_telephony_events", "event_id", "reference_event_id"),
    ("pincer_telephony_spans", "span_id", "reference_span_id"),
)

_TURN = "01890000-0000-7000-8000-0000000000e1"
_TURN_CALL = "01890000-0000-7000-8000-0000000000e2"

_S1 = "01890000-0000-7000-8000-0000000000a1"
_S2 = "01890000-0000-7000-8000-0000000000a2"
_SESSION_0033 = "01890000-0000-7000-8000-0000000000b1"

_SEED_0033 = (
    "INSERT INTO pincer_identity_profiles (pincer_user_id, created_at) VALUES ('alice', '2026-01-02T00:00:00')",
    "INSERT INTO pincer_identity_profiles (pincer_user_id, created_at) VALUES ('bob', '2026-01-01T00:00:00')",
    "INSERT INTO pincer_channel_identities (channel, channel_user_id, pincer_user_id) "
    "VALUES ('telegram', '1', 'alice')",
    "INSERT INTO pincer_channel_identities (channel, channel_user_id, pincer_user_id) VALUES ('telegram', '2', 'bob')",
    "INSERT INTO pincer_identity_phones (phone, pincer_user_id, source) VALUES ('+491', 'alice', 'config')",
    "INSERT INTO pincer_identity_credentials (pincer_user_id, password_hash) VALUES ('alice', 'h')",
    "INSERT INTO pincer_auth_sessions (id, pincer_user_id, refresh_jti, expires_at) "
    f"VALUES ('{_SESSION_0033}', 'alice', 'j', 1.0)",
    "INSERT INTO pincer_schedules (id, pincer_user_id, name, cron_expr, action) "
    f"VALUES ('{_S1}', 'alice', 'a', '* * * * *', '{{}}')",
    # `system` names no profile: its schedule keeps the text and no profile_id.
    "INSERT INTO pincer_schedules (id, pincer_user_id, name, cron_expr, action) "
    f"VALUES ('{_S2}', 'system', 'b', '* * * * *', '{{}}')",
    "INSERT INTO pincer_memories (id, user_id, content, created_at) "
    "VALUES ('01890000-0000-7000-8000-0000000000c1', 'alice', 'likes tea', 0)",
)

#: A link to a profile that is gone: SQLite never enforced the foreign key.
_ORPHAN_0033 = (
    "INSERT INTO pincer_channel_identities (channel, channel_user_id, pincer_user_id) VALUES ('telegram', '9', 'ghost')"
)

_SEED_0034 = (
    "INSERT INTO pincer_identity_profiles (id, pincer_user_id) "
    "VALUES ('01890000-0000-7000-8000-0000000000d1', 'alice')",
    # Newest first, so key order and time order disagree.
    "INSERT INTO pincer_sessions (session_id, user_id, channel, created_at, updated_at) "
    "VALUES ('unified:alice', 'alice', 'telegram', 20.0, 20.0)",
    "INSERT INTO pincer_sessions (session_id, user_id, channel, created_at, updated_at) "
    "VALUES ('web:guest', 'guest', 'web', 10.0, 10.0)",
    "INSERT INTO pincer_session_images (session_id, sha256, media_type, data, created_at) "
    "VALUES ('unified:alice', 'aa', 'image/jpeg', X'FF', 21.0)",
    # Onboarding has no creation time: asked, or else closed.
    "INSERT INTO pincer_onboarding (user_id, channel, asked_at) VALUES ('alice', 'telegram', 5.0)",
    "INSERT INTO pincer_onboarding (user_id, channel, completed_at) VALUES ('guest', 'web', 1.0)",
)

#: A photo of a session that is gone: SQLite never enforced the foreign key.
_ORPHAN_0034 = (
    "INSERT INTO pincer_session_images (session_id, sha256, media_type, data, created_at) "
    "VALUES ('gone:x', 'bb', 'image/jpeg', X'FF', 1.0)"
)

#: The id of a row deleted behind the migration's back.
_GONE = "01890000-0000-7000-8000-0000000000e0"


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
    assert "pincer_alembic_version" in tables
    assert "alembic_version" not in tables


def test_ensure_schema_current_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)
    engine._ensured_paths.clear()  # force a genuine second Alembic run, not the in-process cache guard
    ensure_schema_current(db_path)  # must not raise or duplicate anything

    con = sqlite3.connect(str(db_path))
    try:
        count = con.execute("SELECT COUNT(*) FROM pincer_alembic_version").fetchone()[0]
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
    legacy_tables = {t.removeprefix("pincer_") for t in EXPECTED_TABLES}
    assert not ((EXPECTED_TABLES | legacy_tables) & tables_after_downgrade)

    command.upgrade(cfg, "head")
    assert _tables(db_path) >= EXPECTED_TABLES


def test_offline_sql_mode_is_explicitly_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "pincer.db"
    cfg = build_config(db_path)
    with pytest.raises(SystemExit, match="Offline --sql migrations are not supported"):
        command.upgrade(cfg, "head", sql=True)


def test_memories_fts5_and_sync_triggers_present(tmp_path: Path) -> None:
    """FTS5 is SQLite-only — confirm the virtual table and its sync triggers exist."""
    db_path = tmp_path / "pincer.db"
    ensure_schema_current(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO pincer_memories (id, user_id, content, created_at) VALUES ('m1', 'u1', 'hello world', 0)"
        )
        con.commit()
        rows = con.execute("SELECT content FROM pincer_memories_fts WHERE pincer_memories_fts MATCH 'hello'").fetchall()
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
        meta = con.execute(
            "SELECT pincer_user_id, preferred_channel, display_name FROM pincer_identity_profiles"
        ).fetchall()
        assert meta == [("usr_abc", "telegram", "Alice")]
        links = {
            (channel, channel_user_id)
            for channel, channel_user_id in con.execute(
                "SELECT c.channel, c.channel_user_id FROM pincer_identity_channels c "
                "JOIN pincer_identity_profiles p ON p.id = c.profile_id WHERE p.pincer_user_id = 'usr_abc'"
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
        cols = {row[1] for row in con.execute("PRAGMA table_info(pincer_identity_profiles)").fetchall()}
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
        rows = con.execute("SELECT user_id, action FROM pincer_audit_logs").fetchall()
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
        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(pincer_voice_calls)").fetchall()}
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
            FROM pincer_voice_calls
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
            FROM pincer_call_transcripts
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
            FROM pincer_call_actions
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
            "pincer_do_not_call_numbers",
            "pincer_outbound_call_logs",
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
            FROM pincer_voice_calls
            WHERE call_sid = 'CA_pre_alembic'
            """
        ).fetchone()
        assert row == ("CA_pre_alembic", "outbound")

        voice_cols = {row[1] for row in con.execute("PRAGMA table_info(pincer_voice_calls)").fetchall()}
        assert {
            "call_sid",
            "pincer_user_id",
            "failure_code",
            "inbound_intent",
            "thread_id",
            "briefing_json",
        } <= voice_cols

        assert {
            "pincer_call_transcripts",
            "pincer_call_actions",
            "pincer_phone_contacts",
            "pincer_inbound_messages",
            "pincer_call_threads",
            "pincer_call_thread_members",
            "pincer_call_analytics",
        } <= _tables(db_path)

        transcript_cols = {row[1] for row in con.execute("PRAGMA table_info(pincer_call_transcripts)").fetchall()}
        assert {
            "call_id",
            "speaker",
            "text",
            "confidence",
            "is_final",
            "state",
            "timestamp",
        } <= transcript_cols

        action_cols = {row[1] for row in con.execute("PRAGMA table_info(pincer_call_actions)").fetchall()}
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
            FROM pincer_voice_calls
            """
        ).fetchall() == [("CA_old_runtime", "", "", "", None)]

        assert con.execute("SELECT text FROM pincer_call_transcripts").fetchall() == [("kept across the upgrade",)]

        assert con.execute("SELECT call_id, tier, approval_mode, deny_reason FROM pincer_call_actions").fetchall() == [
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
        # `thread_id` is NULL, not '': 0018 retired the empty-string sentinel,
        # which is not a uuid and would not cast on Postgres.
        assert con.execute(
            "SELECT call_sid, direction, from_number, inbound_intent, thread_id FROM pincer_voice_calls"
        ).fetchall() == [("CA_runtime", "inbound", "+493333333333", "question", None)]

        assert con.execute("SELECT speaker, text FROM pincer_call_transcripts").fetchall() == [("agent", "Guten Tag")]
        assert con.execute("SELECT tier, deny_reason FROM pincer_call_actions").fetchall() == [("X", "tier_x")]
        assert con.execute("SELECT caller_name, matter FROM pincer_inbound_messages").fetchall() == [
            ("Anna", "Rueckruf")
        ]
        # The thread keeps its subject and loses its old hand-made id: 0018
        # converts it, so the value is a v7 uuid rather than `th_1`.
        ((thread_id, subject),) = con.execute("SELECT id, subject FROM pincer_call_threads").fetchall()
        assert subject == "Angebot"
        assert uuid.UUID(thread_id).version == 7
        # The member still points at its thread — both sides rewritten from one map.
        assert con.execute("SELECT call_sid, thread_id FROM pincer_call_thread_members").fetchall() == [
            ("CA_runtime", thread_id)
        ]
        assert con.execute("SELECT call_sid, method FROM pincer_call_analytics").fetchall() == [("CA_runtime", "exact")]

        # 0001 must still have created the non-voice schema it owns.
        assert {
            "pincer_memories",
            "pincer_identity_profiles",
            "pincer_audit_logs",
            "pincer_phone_contacts",
        } <= _tables(db_path)

        # 0005's own tables are additive here.
        assert {"pincer_do_not_call_numbers", "pincer_outbound_call_logs"} <= _tables(db_path)

        # The database is genuinely at head, not merely stamped.
        head = ScriptDirectory.from_config(build_config(db_path)).get_current_head()
        assert con.execute("SELECT version_num FROM pincer_alembic_version").fetchone() == (head,)
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
    """Everything about a table except its column types.

    Index predicates included: a batch rebuild that re-emitted
    `idx_schedules_next_run` without its `WHERE enabled = 1` would otherwise
    look identical.
    """
    return {
        "columns": sorted((c["name"], bool(c["nullable"])) for c in inspector.get_columns(table) if c["name"] != "id"),
        "indexes": sorted(
            (i["name"], tuple(i["column_names"]), bool(i.get("unique")), _predicate(i))
            for i in inspector.get_indexes(table)
        ),
        "unique": sorted(
            (u.get("name") or "", tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
        ),
        "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
    }


def _predicate(index: dict[str, Any]) -> str:
    options = index.get("dialect_options") or {}
    where = options.get("sqlite_where") if options.get("sqlite_where") is not None else options.get("postgresql_where")
    if where is None:
        return ""
    text = " ".join(str(where).split())
    # Postgres hands the predicate back parenthesised; SQLite as written.
    return text[1:-1] if text.startswith("(") and text.endswith(")") else text


def _sqlite_ddl(conn: sa.Connection, name: str) -> str:
    return str(conn.execute(sa.text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": name}).scalar_one())


@contextlib.contextmanager
def _connect(url: str):
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _config(url: str, tmp_path: Path):
    cfg = build_config(tmp_path / "unused.db", url)
    return cfg


# ── migration_helpers.id_map ───────────────────────────────────────────


def test_id_map_lets_the_real_error_through_on_postgres(migration_url):
    """A statement that fails inside the `with` block aborts the Postgres
    transaction. Cleaning up the temporary table in a `finally` would then
    issue a `DROP` on that aborted transaction, which itself raises
    `InFailedSqlTransaction` — burying the statement that actually failed
    under a report about the cleanup instead. SQLite has no such state."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no aborted-transaction state to trip over")
    from pincer.db.migration_helpers import id_map

    engine = sa.create_engine(migration_url)
    try:
        with (
            engine.connect() as conn,
            conn.begin(),
            pytest.raises(sa.exc.ProgrammingError, match="nonexistent_table") as excinfo,
            id_map(conn, "widgets", {"a": "b"}),
        ):
            conn.execute(sa.text("SELECT * FROM nonexistent_table"))
        assert not isinstance(excinfo.value.__context__, sa.exc.InternalError)
    finally:
        engine.dispose()


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


#: What 0018 rebuilds on SQLite to drop the `''` default, besides `voice_calls`.
_REBUILT_BY_0018 = ["telephony_events", "telephony_spans"]


def test_0017_and_0018_leave_every_rebuilt_table_otherwise_exactly_as_it_was(migration_url, tmp_path):
    """The SQLite half rebuilds each table from reflection rather than from
    frozen DDL, so this is what guarantees nothing was dropped on the way to
    head: same columns, same nullability, same indexes — predicates included —
    and the same unique constraints."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        before = {table: _shape(sa.inspect(conn), table) for table in [*_CONVERTED, *_REBUILT_BY_0018]}

    # Up to the revision before 0032, which starts giving tables new keys and
    # columns on purpose; everything 0019-0031 did is accounted for below.
    command.upgrade(cfg, "0031")

    with _connect(migration_url) as conn:
        # 0019 has since prefixed every table; look each one up under its new
        # name but keep it filed under the old one, so it still lines up with
        # `before` and with the `after["schedules"]` lookup below.
        after = {table: _shape(sa.inspect(conn), f"pincer_{table}") for table in [*_CONVERTED, *_REBUILT_BY_0018]}
        for table, added in _ADDED_AFTER_0018.items():
            after[table]["columns"] = [c for c in after[table]["columns"] if c[0] not in added]
        if migration_url.startswith("sqlite"):
            assert "COLLATE NOCASE" in _sqlite_ddl(conn, "idx_phone_contacts_name")

    assert ("idx_schedules_next_run", ("next_run_at",), False, "enabled = 1") in after["schedules"]["indexes"]
    for table in before:
        assert after[table] == before[table], table


def test_0017_round_trips(migration_url, tmp_path):
    """Down is lossy by design — ids are renumbered, not restored — so this
    asserts the shape survives and the rows do, not the original values."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0016")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0016, *_SEED_TRANSCRIPTS, *_SEED_MESSAGES):
            conn.execute(sa.text(insert))
        before = {table: _shape(sa.inspect(conn), table) for table in _CONVERTED}

    command.upgrade(cfg, "0017")
    command.downgrade(cfg, "0016")

    with _connect(migration_url) as conn:
        ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM call_transcripts ORDER BY id")).all()]
        assert ids == [1, 2, 3, 4, 5]
        after = {table: _shape(sa.inspect(conn), table) for table in _CONVERTED}
        if migration_url.startswith("sqlite"):
            # What the rebuild cannot reflect: a deleted row's id must never
            # come back (the transcript tiebreaker), and contacts sort without case.
            for table in _CONVERTED:
                assert "AUTOINCREMENT" in _sqlite_ddl(conn, table).upper(), table
            assert "COLLATE NOCASE" in _sqlite_ddl(conn, "idx_phone_contacts_name")
    for table in _CONVERTED:
        assert after[table] == before[table], table

    command.upgrade(cfg, "0017")
    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM call_transcripts")).scalar_one() == 5


def test_an_interrupted_0014_does_not_wedge_every_later_upgrade(migration_url, tmp_path):
    """SQLite has no transactional DDL: a rebuild that died after its CREATE
    TABLE leaves the copy behind, and the retry — the app's own, at startup —
    died on "table already exists"."""
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres DDL rolls back with the transaction")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0013")
    with _connect(migration_url) as conn:
        conn.execute(sa.text("CREATE TABLE call_analytics_no_fk (call_sid TEXT PRIMARY KEY)"))

    command.upgrade(cfg, "0014")
    command.downgrade(cfg, "0013")  # the downgrade rebuilds through the same name

    with _connect(migration_url) as conn:
        conn.execute(sa.text("CREATE TABLE call_analytics_no_fk (call_sid TEXT PRIMARY KEY)"))
    command.upgrade(cfg, "head")


def test_an_interrupted_0017_downgrade_can_be_retried(migration_url, tmp_path):
    """The downgrade's batch rebuilds leave `_alembic_tmp_<t>` behind the same way."""
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres DDL rolls back with the transaction")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0017")
    with _connect(migration_url) as conn:
        conn.execute(sa.text("CREATE TABLE _alembic_tmp_schedules (id INTEGER)"))

    command.downgrade(cfg, "0016")


# ── 0018: the minted text keys become UUIDv7 ─────────────────────────

_T0 = "2026-09-01T10:00:00+00:00"

#: A call with its telemetry, a thread with a member, and a memory tagged
#: with that thread — every relation 0018 has to keep, plus the orphans and
#: sentinels it has to resolve before the values can cast.
_SEED_0017 = (
    "INSERT INTO telephony_calls (call_id, registered_at) VALUES ('call-old', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO telephony_turns (turn_id, call_id, created_at) "
    "VALUES ('turn-old', 'call-old', '2026-09-01T10:00:01+00:00')",
    "INSERT INTO telephony_events (event_id, call_id, turn_id, name, ts_utc) "
    "VALUES ('ev-1', 'call-old', 'turn-old', 'call.registered', '2026-09-01T10:00:02+00:00')",
    # turn_id carries the '' sentinel, which is not a uuid.
    "INSERT INTO telephony_events (event_id, call_id, turn_id, name, ts_utc) "
    "VALUES ('ev-2', 'call-old', '', 'call.ended', '2026-09-01T10:00:03+00:00')",
    # an event whose call was purged by retention — a real orphan
    "INSERT INTO telephony_events (event_id, call_id, name, ts_utc) "
    "VALUES ('ev-orphan', 'call-gone', 'call.ended', '2026-09-01T10:00:04+00:00')",
    "INSERT INTO telephony_spans (span_id, call_id, turn_id, name, start_utc) "
    "VALUES ('span-1', 'call-old', 'turn-old', 'llm', '2026-09-01T10:00:02+00:00')",
    "INSERT INTO call_threads (thread_id, subject, origin, created_at, updated_at) "
    "VALUES ('thr_old', 'Angebot', 'inbound', '2026-09-01T10:00:00+00:00', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO call_thread_members (call_sid, thread_id, attached_at) "
    "VALUES ('CA_1', 'thr_old', '2026-09-01T10:00:00+00:00')",
    "INSERT INTO voice_calls (id, call_sid, started_at, thread_id) "
    "VALUES ('01a00000-0000-7000-8000-000000000001', 'CA_1', '2026-09-01T10:00:00+00:00', 'thr_old')",
    "INSERT INTO memories (id, user_id, content, category, tags, created_at) "
    "VALUES ('mem-old', 'usr_a', 'Angebot besprochen', 'general', '[\"thread:thr_old\", \"call:CA_1\"]', 1788256800.0)",
    "INSERT INTO conversations (id, user_id, channel, messages_json, created_at, updated_at) "
    "VALUES ('conv-old', 'usr_a', 'voice', '[]', 1788256800.0, 1788256800.0)",
    "INSERT INTO entities (id, user_id, name, type, attributes_json, last_seen) "
    "VALUES ('ent-old', 'usr_a', 'Ada', 'person', '{}', 1788256800.0)",
)


def _seed_0017(url: str, tmp_path: Path):
    cfg = _config(url, tmp_path)
    command.upgrade(cfg, "0017")
    with _connect(url) as conn:
        for insert in _SEED_0017:
            conn.execute(sa.text(insert))
    return cfg


def test_0018_rewrites_both_sides_of_every_reference(migration_url, tmp_path):
    """The point of the revision: a key and everything pointing at it move
    together, from one map, so nothing is left naming a row that is gone."""
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        call_id = conn.execute(sa.text("SELECT call_id FROM telephony_calls")).scalar_one()
        turn_id = conn.execute(sa.text("SELECT turn_id FROM telephony_turns")).scalar_one()
        thread_id = conn.execute(sa.text("SELECT thread_id FROM call_threads")).scalar_one()

        for value in (call_id, turn_id, thread_id):
            assert uuid.UUID(str(value)).version == 7

        assert conn.execute(sa.text("SELECT call_id FROM telephony_turns")).scalar_one() == call_id
        assert conn.execute(sa.text("SELECT call_id FROM telephony_spans")).scalar_one() == call_id
        assert conn.execute(sa.text("SELECT turn_id FROM telephony_spans")).scalar_one() == turn_id
        assert (
            conn.execute(sa.text("SELECT turn_id FROM telephony_events WHERE event_id = 'ev-1'")).scalar_one()
            == turn_id
        )
        assert conn.execute(sa.text("SELECT thread_id FROM call_thread_members")).scalar_one() == thread_id
        assert conn.execute(sa.text("SELECT thread_id FROM voice_calls")).scalar_one() == thread_id


def test_0018_retires_the_empty_string_sentinel(migration_url, tmp_path):
    """`''` meant "no turn". It is not a uuid, so it becomes what it meant."""
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        assert (
            conn.execute(sa.text("SELECT turn_id FROM telephony_events WHERE event_id = 'ev-2'")).scalar_one() is None
        )


def test_0018_drops_telemetry_whose_call_is_already_gone(migration_url, tmp_path):
    """Retention deletes a call and leaves its events; `call_id` is NOT NULL,
    so an orphan cannot be nulled and cannot cast. It goes."""
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        remaining = conn.execute(sa.text("SELECT event_id FROM telephony_events")).all()
    assert sorted(row[0] for row in remaining) == ["ev-1", "ev-2"]


def test_0018_applies_each_orphan_policy_to_the_column_it_names(migration_url, tmp_path):
    """The `null` and `delete` policies differ by whether the column can be
    NULL, and getting one wrong deletes production rows instead of dropping a
    link. Only the telephony pair was covered; these are the thread ones."""
    cfg = _seed_0017(migration_url, tmp_path)
    with _connect(migration_url) as conn:
        # A call pointing at a thread that no longer exists: nullable, so the
        # call survives and loses the link.
        conn.execute(
            sa.text(
                "INSERT INTO voice_calls (id, call_sid, started_at, thread_id) "
                "VALUES ('01a00000-0000-7000-8000-000000000002', 'CA_2', :now, 'thr_gone')"
            ),
            {"now": _T0},
        )
        # A membership row pointing at one: NOT NULL, so the row goes. Only
        # SQLite can hold such a row at all — Postgres enforces the foreign
        # key, which is why these policies exist for the SQLite side.
        if migration_url.startswith("sqlite"):
            conn.execute(
                sa.text(
                    "INSERT INTO call_thread_members (call_sid, thread_id, attached_at) "
                    "VALUES ('CA_2', 'thr_gone', :now)"
                ),
                {"now": _T0},
            )
        # A span whose turn is gone: nullable, so the span survives.
        conn.execute(
            sa.text(
                "INSERT INTO telephony_spans (span_id, call_id, turn_id, name, start_utc) "
                "VALUES ('span-orphan', 'call-old', 'turn-gone', 'llm', :now)"
            ),
            {"now": _T0},
        )

    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        kept = conn.execute(sa.text("SELECT thread_id FROM voice_calls WHERE call_sid = 'CA_2'")).scalar_one()
        assert kept is None, "a call must survive losing its thread"
        members = conn.execute(sa.text("SELECT call_sid FROM call_thread_members")).all()
        assert [row[0] for row in members] == ["CA_1"], "the orphaned membership row should be gone"
        span = conn.execute(sa.text("SELECT turn_id FROM telephony_spans WHERE span_id = 'span-orphan'")).scalar_one()
        assert span is None, "a span must survive losing its turn"


def test_0018_rewrites_the_thread_id_copied_into_memory_tags(migration_url, tmp_path):
    """A thread id has a second home in `memories.tags`, and the thread's one
    note is found by that tag. A stale tag would split it in two silently."""
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        thread_id = conn.execute(sa.text("SELECT thread_id FROM call_threads")).scalar_one()
        tags = json.loads(conn.execute(sa.text("SELECT tags FROM memories")).scalar_one())

    assert tags == [f"thread:{thread_id}", "call:CA_1"]  # the call_sid is Twilio's and does not move


def test_0018_leaves_the_identifiers_that_are_not_ours_alone(migration_url, tmp_path):
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")

    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT span_id FROM telephony_spans")).scalar_one() == "span-1"
        assert (
            conn.execute(sa.text("SELECT event_id FROM telephony_events WHERE name = 'call.registered'")).scalar_one()
            == "ev-1"
        )
        assert conn.execute(sa.text("SELECT call_sid FROM voice_calls")).scalar_one() == "CA_1"


def test_0018_keeps_full_text_search_working_on_sqlite(migration_url, tmp_path):
    """`memories_fts` is keyed on the rowid, which an id rewrite does not move.
    0018 does not rely on that: it drops the triggers for the rewrite, puts them
    back and rebuilds the index — so search still finds the old row, and a row
    written afterwards is indexed too.

    None of that actually exercises the rebuild by itself: the id rewrite
    never moves a rowid, so the index stays correct even if the trigger
    cycle — and the `rebuild` — never ran. The row inserted below, with the
    triggers down, is the part that pins it: only `upgrade 0018`'s own
    `rebuild` can put a row into the index that no trigger ever wrote there.
    """
    if not migration_url.startswith("sqlite"):
        pytest.skip("FTS5 is the SQLite search path")
    cfg = _seed_0017(migration_url, tmp_path)

    # Desynchronise `memories_fts` before the upgrade ever touches it: with
    # every trigger down, this row lands in `memories` but nowhere else.
    with _connect(migration_url) as conn:
        for trigger in ("memories_ai", "memories_ad", "memories_au"):
            conn.execute(sa.text(f"DROP TRIGGER {trigger}"))
        conn.execute(
            sa.text(
                "INSERT INTO memories (id, user_id, content, category, created_at) "
                "VALUES (:id, 'usr_a', 'Vertrag unterschrieben', 'note', 1)"
            ),
            {"id": str(uuid.uuid4())},
        )

    command.upgrade(cfg, "0018")

    def search(conn: sa.Connection, term: str) -> list[str]:
        return [
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
                    "WHERE memories_fts MATCH :term"
                ),
                {"term": term},
            ).all()
        ]

    with _connect(migration_url) as conn:
        stored = conn.execute(sa.text("SELECT id FROM memories WHERE content = 'Angebot besprochen'")).scalar_one()
        assert search(conn, "Angebot") == [stored]

        # Never written by a trigger — only findable if 0018 actually rebuilt.
        stray = conn.execute(sa.text("SELECT id FROM memories WHERE content = 'Vertrag unterschrieben'")).scalar_one()
        assert search(conn, "Vertrag") == [stray]

        triggers = {
            row[0] for row in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).all()
        }
        assert {"memories_ai", "memories_ad", "memories_au"} <= triggers
        conn.execute(
            sa.text(
                "INSERT INTO memories (id, user_id, content, category, created_at) "
                "VALUES (:id, 'usr_a', 'Rechnung bezahlt', 'note', 1)"
            ),
            {"id": str(uuid.uuid7())},
        )
        assert len(search(conn, "Rechnung")) == 1


def test_0018_round_trips_without_losing_the_ids(migration_url, tmp_path):
    """Down only reverts the storage type: the ids stay UUIDv7 strings, which
    the pre-0018 code reads happily because it never parsed the format."""
    cfg = _seed_0017(migration_url, tmp_path)
    command.upgrade(cfg, "0018")
    with _connect(migration_url) as conn:
        before = conn.execute(sa.text("SELECT call_id FROM telephony_calls")).scalar_one()

    command.downgrade(cfg, "0017")
    with _connect(migration_url) as conn:
        assert str(conn.execute(sa.text("SELECT call_id FROM telephony_calls")).scalar_one()) == str(before)
        # and the sentinel is back where the old code expects it
        assert conn.execute(sa.text("SELECT turn_id FROM telephony_events WHERE event_id = 'ev-2'")).scalar_one() == ""

    command.upgrade(cfg, "0018")


# ── env.py: renaming only Pincer's own `alembic_version` ──────────────

_CREATE_LEGACY_VERSION_TABLE = (
    f"CREATE TABLE {LEGACY_VERSION_TABLE} (version_num VARCHAR(32) NOT NULL, "
    f"CONSTRAINT {LEGACY_VERSION_TABLE}_pkc PRIMARY KEY (version_num))"
)


def _version_rows(conn: sa.Connection) -> dict[str, list[str]]:
    """Every Alembic version table in the database and the revisions it records."""
    tables = set(sa.inspect(conn).get_table_names())
    return {
        table: list(conn.execute(sa.text(f"SELECT version_num FROM {table}")).scalars())
        for table in (LEGACY_VERSION_TABLE, VERSION_TABLE)
        if table in tables
    }


def _legacy_pincer_db(url: str, tmp_path: Path, revision: str):
    """Pincer's own database at `revision`, as migrated before `0019` named the
    version table: its history in Alembic's default `alembic_version`."""
    cfg = _config(url, tmp_path)
    command.upgrade(cfg, revision)
    with _connect(url) as conn:
        conn.execute(sa.text(f"ALTER TABLE {VERSION_TABLE} RENAME TO {LEGACY_VERSION_TABLE}"))
        if conn.dialect.name == "postgresql":
            # Its primary key under the name Alembic gave it back then.
            conn.execute(
                sa.text(
                    f"ALTER TABLE {LEGACY_VERSION_TABLE} "
                    f"RENAME CONSTRAINT {VERSION_TABLE}_pkc TO {LEGACY_VERSION_TABLE}_pkc"
                )
            )
    return cfg


def _foreign_app_db(url: str, tmp_path: Path, revision: str, table: str):
    """Another Alembic application's database: its history in `alembic_version`
    at `revision`, beside one table of its own."""
    with _connect(url) as conn:
        conn.execute(sa.text(_CREATE_LEGACY_VERSION_TABLE))
        conn.execute(sa.text(f"INSERT INTO {LEGACY_VERSION_TABLE} (version_num) VALUES (:v)"), {"v": revision})
        conn.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    return _config(url, tmp_path)


def test_pincer_s_own_legacy_version_table_is_renamed_with_its_revision(migration_url, tmp_path, caplog):
    """Upgrading to 0011 runs only 0011: the 0010 Alembic finds is the one the
    legacy table recorded, now under the new name."""
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0010")

    with caplog.at_level(logging.INFO, logger="pincer.db.migrations"):
        command.upgrade(cfg, "0011")

    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"pincer_alembic_version": ["0011"]}
    # Nobody else was migrating, so there was nothing to wait for or say.
    assert _waiting_lines(caplog) == []


def test_another_app_s_version_table_is_left_alone(migration_url, tmp_path):
    """Its revision id is one Pincer also has (other apps also number their
    revisions 0001, 0002, …), but no Pincer table is there."""
    cfg = _foreign_app_db(migration_url, tmp_path, "0005", "other_app_users")
    head = ScriptDirectory.from_config(cfg).get_current_head()

    command.upgrade(cfg, "head")

    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"alembic_version": ["0005"], "pincer_alembic_version": [head]}
        tables = set(sa.inspect(conn).get_table_names())
    assert "other_app_users" in tables
    assert EXPECTED_TABLES - {"pincer_memories_fts"} <= tables


def test_an_unknown_revision_beside_pincer_tables_is_refused(migration_url, tmp_path):
    """Pincer's own legacy database, at a revision this checkout doesn't have
    (a fork's, say). Neither renamed nor left for Alembic to replay `0001` over."""
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0010")
    with _connect(migration_url) as conn:
        conn.execute(sa.text(f"UPDATE {LEGACY_VERSION_TABLE} SET version_num = 'f00dfeedcafe'"))

    with pytest.raises(RuntimeError, match=r"records revision\(s\) \['f00dfeedcafe'\].*does not have"):
        command.upgrade(cfg, "head")

    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"alembic_version": ["f00dfeedcafe"]}


def _pre_alembic_pincer_db(url: str, tmp_path: Path):
    """Pincer's tables as an install from before Alembic left them: `0001`'s
    schema (so `channel_identities` is there) and no version table of Pincer's."""
    cfg = _config(url, tmp_path)
    command.upgrade(cfg, "0001")
    with _connect(url) as conn:
        conn.execute(sa.text(f"DROP TABLE {VERSION_TABLE}"))
    return cfg


def test_another_app_s_version_table_without_a_version_column_is_left_alone(migration_url, tmp_path):
    """Beside Pincer's tables, so the bootstrap gets as far as looking at it:
    no `version_num` column, so not Alembic's history of Pincer."""
    cfg = _pre_alembic_pincer_db(migration_url, tmp_path)
    with _connect(migration_url) as conn:
        conn.execute(sa.text(f"CREATE TABLE {LEGACY_VERSION_TABLE} (id INTEGER PRIMARY KEY, label TEXT)"))
        conn.execute(sa.text(f"INSERT INTO {LEGACY_VERSION_TABLE} (id, label) VALUES (1, 'theirs')"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    command.upgrade(cfg, "head")

    with _connect(migration_url) as conn:
        assert conn.execute(sa.text(f"SELECT id, label FROM {LEGACY_VERSION_TABLE}")).all() == [(1, "theirs")]
        assert conn.execute(sa.text(f"SELECT version_num FROM {VERSION_TABLE}")).scalars().all() == [head]


def test_another_role_s_unreadable_version_table_is_left_alone(migration_url, tmp_path):
    """Beside Pincer's tables, so the bootstrap reads it: Pincer's role has no
    grant on the other app's `alembic_version`, though the catalog still lists
    it, and the permission error means "not ours", not a crash."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no roles or grants")
    role = f"pincer_app_{uuid.uuid4().hex[:8]}"
    with _connect(migration_url) as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD 'pincer'"))
        conn.execute(sa.text(f"GRANT ALL ON SCHEMA public TO {role}"))
    as_role = sa.engine.make_url(migration_url).set(username=role, password="pincer")
    try:
        cfg = _pre_alembic_pincer_db(as_role.render_as_string(hide_password=False), tmp_path)
        _foreign_app_db(migration_url, tmp_path, "0005", "other_app_users")
        head = ScriptDirectory.from_config(cfg).get_current_head()

        command.upgrade(cfg, "head")

        with _connect(migration_url) as conn:
            assert _version_rows(conn) == {"alembic_version": ["0005"], "pincer_alembic_version": [head]}
    finally:
        with _connect(migration_url) as conn:
            conn.execute(sa.text(f"DROP OWNED BY {role}"))
            conn.execute(sa.text(f"DROP ROLE {role}"))


def test_a_failed_read_of_pincer_s_own_version_table_fails_the_upgrade(migration_url, tmp_path, monkeypatch):
    """A lock timeout on Pincer's own legacy `alembic_version` says nothing
    about whose table it is. Taking it as "not ours" would have Alembic replay
    every revision over Pincer's schema; the upgrade must fail instead, before
    `pincer_alembic_version` exists."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("needs a lock on one table; SQLite's locks cover the whole file, catalog included")
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0010")
    # libpq reads it on connect: every connection opened from here on gives up
    # on a lock after 200 ms instead of waiting.
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=200")

    holder = sa.create_engine(migration_url)
    try:
        with holder.connect() as other:
            other.execute(sa.text(f"LOCK TABLE {LEGACY_VERSION_TABLE} IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
                command.upgrade(cfg, "head")
            other.rollback()
    finally:
        holder.dispose()

    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"alembic_version": ["0010"]}


def test_a_second_start_beside_another_app_s_version_table_leaves_it_alone(migration_url, tmp_path):
    """Once Pincer's schema is built, the foreign table holds a revision id
    Pincer also has and a Pincer table is present: only `VERSION_TABLE`
    already existing keeps the bootstrap from taking it."""
    cfg = _foreign_app_db(migration_url, tmp_path, "0005", "other_app_users")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.upgrade(cfg, "head")

    command.upgrade(cfg, "head")

    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"alembic_version": ["0005"], "pincer_alembic_version": [head]}


def _wait_for_migration_lock_waiters(conn: sa.Connection, count: int, errors: list[BaseException]) -> None:
    """Return once `count` sessions are queued on the migration advisory lock in
    this database; fail after 30 s, or as soon as an upgrader has failed instead."""
    deadline = time.monotonic() + 30.0
    while True:
        waiting = conn.execute(
            sa.text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid::bigint = :high AND objid::bigint = :low"
            ),
            {"high": MIGRATION_ADVISORY_KEY >> 32, "low": MIGRATION_ADVISORY_KEY & 0xFFFFFFFF},
        ).scalar_one()
        if waiting >= count:
            return
        assert not errors, f"an upgrader failed before waiting on the lock: {errors!r}"
        assert time.monotonic() < deadline, f"{waiting} of {count} upgraders queued on the migration lock after 30 s"
        time.sleep(0.01)


def _waiting_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.levelname for r in caplog.records if r.getMessage().startswith("Waiting for another Pincer process")]


def test_a_concurrent_start_waits_for_the_other_rename(migration_url, tmp_path, caplog):
    """A second process holds the migration lock and renames the table itself
    meanwhile; this one must wait for it, then find the new name and carry on."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite serialises the whole upgrade behind ensure_schema_current's file lock")
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0010")
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            command.upgrade(cfg, "0011")
        except BaseException as exc:  # surfaced on the test thread below
            errors.append(exc)

    holder = sa.create_engine(migration_url)
    try:
        with holder.connect() as other:
            other.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_ADVISORY_KEY})
            worker = threading.Thread(target=upgrade)
            with caplog.at_level(logging.INFO, logger="pincer.db.migrations"):
                worker.start()
                _wait_for_migration_lock_waiters(other, 1, errors)
                other.execute(sa.text("ALTER TABLE alembic_version RENAME TO pincer_alembic_version"))
                other.commit()
                worker.join(timeout=60)
    finally:
        holder.dispose()

    assert not worker.is_alive()
    assert errors == []
    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"pincer_alembic_version": ["0011"]}
    assert _waiting_lines(caplog) == ["INFO"]


def test_a_lost_lock_connection_does_not_fail_a_committed_upgrade(migration_url, tmp_path, caplog):
    """The lock's connection dies while it sits idle mid-migration (a firewall,
    `idle_session_timeout`). The server released the lock with it; the
    migration still commits, and ending the lock's transaction must not then
    fail the upgrade."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite takes no advisory lock")
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0018")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            command.upgrade(cfg, "head")
        except BaseException as exc:  # surfaced on the test thread below
            errors.append(exc)

    in_this_database = "database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    holder = sa.create_engine(migration_url)
    try:
        with holder.connect() as other:
            # Holds 0019 up at its first rename, with the lock already taken.
            other.execute(sa.text("LOCK TABLE audit_logs IN ACCESS EXCLUSIVE MODE"))
            worker = threading.Thread(target=upgrade)
            with caplog.at_level(logging.WARNING, logger="pincer.db.migrations"):
                worker.start()
                deadline = time.monotonic() + 30.0
                while not other.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'relation' AND NOT granted "
                        f"AND {in_this_database}"
                    )
                ).scalar_one():
                    assert not errors, errors
                    assert time.monotonic() < deadline, "the migration never reached the locked table"
                    time.sleep(0.01)
                terminated = (
                    other.execute(
                        sa.text(
                            "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                            f"AND {in_this_database} AND classid::bigint = :high AND objid::bigint = :low"
                        ),
                        {"high": MIGRATION_ADVISORY_KEY >> 32, "low": MIGRATION_ADVISORY_KEY & 0xFFFFFFFF},
                    )
                    .scalars()
                    .all()
                )
                assert terminated == [True]
                other.rollback()
                worker.join(timeout=60)
    finally:
        holder.dispose()

    assert not worker.is_alive()
    assert errors == []
    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"pincer_alembic_version": [head]}
    assert [r.levelname for r in caplog.records if r.name == "pincer.db.migrations"] == ["WARNING"]


_UPGRADE_SCRIPT = """
import sys
from pathlib import Path

from alembic import command

from pincer.db import build_config

cfg = build_config(Path(sys.argv[2]) / "unused.db", sys.argv[1])
command.upgrade(cfg, "head")
"""


def test_two_concurrent_upgraders_of_a_legacy_database_both_come_up(migration_url, tmp_path):
    """`pincer` and `pincer-tasks` starting together on a database with `0019`
    and its version-table rename pending. Both are held at the lock until both
    are queued, then let go: the second must wait out the first's whole
    upgrade and find the database at head, not race it through `0019`."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite serialises the whole upgrade behind ensure_schema_current's file lock")
    cfg = _legacy_pincer_db(migration_url, tmp_path, "0018")
    head = ScriptDirectory.from_config(cfg).get_current_head()

    holder = sa.create_engine(migration_url)
    try:
        with holder.connect() as other:
            other.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_ADVISORY_KEY})
            upgraders = [
                subprocess.Popen(
                    [sys.executable, "-c", _UPGRADE_SCRIPT, migration_url, str(tmp_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for _ in range(2)
            ]
            _wait_for_migration_lock_waiters(other, 2, [])
            other.commit()
        outputs = [upgrader.communicate(timeout=120)[0] for upgrader in upgraders]
    finally:
        holder.dispose()

    assert [upgrader.returncode for upgrader in upgraders] == [0, 0], "\n\n".join(outputs)
    with _connect(migration_url) as conn:
        assert _version_rows(conn) == {"pincer_alembic_version": [head]}


# ── 0020: orphaned webhook_retry schedules ─────────────────────────────


def test_0020_deletes_webhook_retry_schedules_and_keeps_the_rest(migration_url, tmp_path):
    """Issue #217: the `webhook_retry` row has no handler anywhere, enabled or
    already self-disabled; every other schedule, including one whose action
    isn't valid JSON, is left alone."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0019")
    rows = {
        "webhook_retry": ('{"type": "webhook_retry"}', 1),
        "webhook_retry_disabled": ('{"type": "webhook_retry"}', 0),
        "morning_briefing": ('{"type": "briefing"}', 1),
        "untyped": ("{}", 1),
        "garbled": ("not json", 1),
    }
    with _connect(migration_url) as conn:
        for name, (action, enabled) in rows.items():
            conn.execute(
                sa.text(
                    "INSERT INTO pincer_schedules (id, pincer_user_id, name, cron_expr, action, enabled) "
                    "VALUES (:id, 'usr_test', :name, '* * * * *', :action, :enabled)"
                ),
                {"id": str(uuid.uuid4()), "name": name, "action": action, "enabled": enabled},
            )

    command.upgrade(cfg, "0020")
    with _connect(migration_url) as conn:
        left = set(conn.execute(sa.text("SELECT name FROM pincer_schedules")).scalars())
    assert left == {"morning_briefing", "untyped", "garbled"}


# ── 0021: identity credentials ─────────────────────────────────────────


def test_0021_identity_credentials_constraints(migration_url, tmp_path):
    """Issue #228: one credentials row per identity, an API key hash names at
    most one of them, and an identity with only a password has none.

    At 0032: 0033 re-keys credentials by the profile's id (see `test_0033_*`)."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0032")
    insert = sa.text(
        "INSERT INTO pincer_identity_credentials (pincer_user_id, password_hash, api_key_hash) "
        "VALUES (:uid, :password_hash, :api_key_hash)"
    )
    with _connect(migration_url) as conn:
        for uid in ("alice", "bob", "carol"):
            conn.execute(sa.text("INSERT INTO pincer_identity_profiles (pincer_user_id) VALUES (:uid)"), {"uid": uid})
        conn.execute(insert, {"uid": "alice", "password_hash": "$argon2id$x", "api_key_hash": "h1"})
        # Two identities without a key: NULL is not a duplicate of NULL.
        conn.execute(insert, {"uid": "bob", "password_hash": "$argon2id$y", "api_key_hash": None})
        conn.execute(insert, {"uid": "carol", "password_hash": None, "api_key_hash": None})
        row = conn.execute(
            sa.text("SELECT token_version, created_at FROM pincer_identity_credentials WHERE pincer_user_id = 'alice'")
        ).one()
        assert row.token_version == 0
        assert row.created_at is not None

    with pytest.raises(sa.exc.IntegrityError), _connect(migration_url) as conn:
        conn.execute(insert, {"uid": "alice", "password_hash": None, "api_key_hash": "h9"})
    with pytest.raises(sa.exc.IntegrityError), _connect(migration_url) as conn:
        conn.execute(sa.text("UPDATE pincer_identity_credentials SET api_key_hash = 'h1' WHERE pincer_user_id = 'bob'"))

    inspector_engine = sa.create_engine(migration_url)
    try:
        foreign_keys = sa.inspect(inspector_engine).get_foreign_keys("pincer_identity_credentials")
    finally:
        inspector_engine.dispose()
    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in foreign_keys] == [
        ("pincer_identity_profiles", "CASCADE")
    ]


def test_0021_round_trips(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0020")
    engine_ = sa.create_engine(migration_url)
    try:
        assert "pincer_identity_credentials" not in sa.inspect(engine_).get_table_names()
        command.upgrade(cfg, "head")
        assert "pincer_identity_credentials" in sa.inspect(engine_).get_table_names()
    finally:
        engine_.dispose()


# ── 0022: auth sessions ────────────────────────────────────────────────


def test_0022_auth_sessions_belong_to_credentials_and_round_trip(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "head")
    engine_ = sa.create_engine(migration_url)
    try:
        inspector = sa.inspect(engine_)
        foreign_keys = inspector.get_foreign_keys("pincer_auth_sessions")
        assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in foreign_keys] == [
            ("pincer_identity_credentials", "CASCADE")
        ]
        assert "idx_auth_sessions_user" in {index["name"] for index in inspector.get_indexes("pincer_auth_sessions")}

        command.downgrade(cfg, "0021")
        tables = sa.inspect(engine_).get_table_names()
        assert "pincer_auth_sessions" not in tables
        assert "pincer_identity_credentials" in tables
        command.upgrade(cfg, "head")
        assert "pincer_auth_sessions" in sa.inspect(engine_).get_table_names()
    finally:
        engine_.dispose()


# ── 0023: onboarding ───────────────────────────────────────────────────


def test_0023_onboarding_is_keyed_by_user_and_channel_and_round_trips(migration_url, tmp_path):
    """At 0033: 0034 gives the table an `id` and keeps (user, channel) unique (see `test_0034_*`)."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0033")
    engine_ = sa.create_engine(migration_url)
    try:
        inspector = sa.inspect(engine_)
        columns = {column["name"]: column["nullable"] for column in inspector.get_columns("pincer_onboarding")}
        assert columns == {
            "user_id": False,
            "channel": False,
            "asked_session_id": True,
            "asked_at": True,
            "completed_at": True,
            "closed_by": True,
            "attempts": False,
        }
        assert inspector.get_pk_constraint("pincer_onboarding")["constrained_columns"] == ["user_id", "channel"]

        command.downgrade(cfg, "0022")
        assert "pincer_onboarding" not in sa.inspect(engine_).get_table_names()
        command.upgrade(cfg, "head")
        assert "pincer_onboarding" in sa.inspect(engine_).get_table_names()
    finally:
        engine_.dispose()


def test_0024_preserves_existing_onboarding_closures(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0023")
    engine_ = sa.create_engine(migration_url)
    try:
        with engine_.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO pincer_onboarding (user_id, channel, completed_at) VALUES ('u', 'web', 1)")
            )
        command.upgrade(cfg, "head")
        with engine_.connect() as conn:
            assert conn.execute(sa.text("SELECT completed_at, closed_by FROM pincer_onboarding")).one() == (1, None)
        command.downgrade(cfg, "0023")
        assert "closed_by" not in {column["name"] for column in sa.inspect(engine_).get_columns("pincer_onboarding")}
        command.upgrade(cfg, "head")
    finally:
        engine_.dispose()


def test_identity_status_backfills_the_source(migration_url, tmp_path):
    """0028 (#348): `usr_` ids are auto, a credential without a channel is a CLI
    identity, the rest came from the config; everyone starts active."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0027")
    engine_ = sa.create_engine(migration_url)
    try:
        with engine_.begin() as con:
            for uid in ("usr_0123abcd", "usrbob", "usr_x", "alice", "dashboard", "revoked"):
                con.execute(sa.text("INSERT INTO pincer_identity_profiles (pincer_user_id) VALUES (:u)"), {"u": uid})
            for channel_user_id, uid in (("1", "usr_0123abcd"), ("2", "alice"), ("3", "usrbob")):
                con.execute(
                    sa.text(
                        "INSERT INTO pincer_channel_identities (channel, channel_user_id, pincer_user_id) "
                        "VALUES ('telegram', :c, :u)"
                    ),
                    {"c": channel_user_id, "u": uid},
                )
            for uid, password_hash in (("dashboard", "hash"), ("alice", "hash"), ("revoked", None), ("usr_x", "h")):
                con.execute(
                    sa.text(
                        "INSERT INTO pincer_identity_credentials (pincer_user_id, password_hash, token_version) "
                        "VALUES (:u, :p, 0)"
                    ),
                    {"u": uid, "p": password_hash},
                )

        command.upgrade(cfg, "0028")

        with engine_.connect() as con:
            rows = {
                uid: (source, bool(active))
                for uid, source, active in con.execute(
                    sa.text("SELECT pincer_user_id, source, active FROM pincer_identity_profiles")
                ).all()
            }
        assert rows == {
            "usr_0123abcd": ("auto", True),
            # `usr` without the underscore is a name, not an auto id: the `_` is escaped.
            "usrbob": ("config", True),
            # A channel-less auto id with a credential stays auto: only config rows become cli.
            "usr_x": ("auto", True),
            "alice": ("config", True),
            "dashboard": ("cli", True),
            "revoked": ("config", True),
        }

        command.downgrade(cfg, "0027")
        columns = {column["name"] for column in sa.inspect(engine_).get_columns("pincer_identity_profiles")}
        assert not {"active", "source"} & columns
    finally:
        engine_.dispose()


# ── 0029: cost log cache token columns ─────────────────────────────────

_CACHE_COLUMNS = {"cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"}


def test_0029_adds_the_cache_token_columns_and_leaves_old_rows_at_zero(migration_url, tmp_path):
    """#357: `input_tokens` is uncached input from 0029 on; rows written before it read 0
    in each cache column (not recorded), and the downgrade drops the three again."""
    from pincer.models.costs import CostLog

    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0028")
    engine_ = sa.create_engine(migration_url)
    columns = ("timestamp", "provider", "model", "input_tokens", "output_tokens", "cost_usd")
    old = (1.0, "anthropic", "claude-a", 10, 5, 0.5)
    try:
        with engine_.begin() as conn:
            conn.execute(CostLog.__table__.insert().values(dict(zip(columns, old, strict=True))))

        command.upgrade(cfg, "0029")
        inspector = sa.inspect(engine_)
        added = {c["name"]: c for c in inspector.get_columns("pincer_cost_logs") if c["name"] in _CACHE_COLUMNS}
        assert {name: column["nullable"] for name, column in added.items()} == dict.fromkeys(_CACHE_COLUMNS, False)
        with engine_.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT timestamp, provider, model, input_tokens, output_tokens, cost_usd, "
                    "cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens FROM pincer_cost_logs"
                )
            ).one()
        assert tuple(row) == (*old, 0, 0, 0)

        command.downgrade(cfg, "0028")
        assert not _CACHE_COLUMNS & {c["name"] for c in sa.inspect(engine_).get_columns("pincer_cost_logs")}
        with engine_.connect() as conn:
            assert conn.execute(sa.text("SELECT input_tokens, cost_usd FROM pincer_cost_logs")).one() == (10, 0.5)
        command.upgrade(cfg, "head")
        assert {c["name"] for c in sa.inspect(engine_).get_columns("pincer_cost_logs")} >= _CACHE_COLUMNS
    finally:
        engine_.dispose()


# ── 0030: session images ───────────────────────────────────────────────


def _version(engine_: sa.Engine) -> str:
    with engine_.connect() as conn:
        return str(conn.execute(sa.text(f"SELECT version_num FROM {VERSION_TABLE}")).scalar_one())


def test_0030_session_images_hang_off_their_session_and_the_downgrade_drops_them(migration_url, tmp_path):
    """#358: one photo per session and hash, gone with its session (Postgres cascades; SQLite
    is told by the code). The upgrade and downgrade must really run, not just exit 0: the table
    and the version are read back after each."""
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0029")
    engine_ = sa.create_engine(migration_url)
    try:
        assert "pincer_session_images" not in sa.inspect(engine_).get_table_names()
        assert _version(engine_) == "0029"

        command.upgrade(cfg, "0030")
        inspector = sa.inspect(engine_)
        assert _version(engine_) == "0030"
        assert "pincer_session_images" in inspector.get_table_names()
        assert {c["name"]: c["nullable"] for c in inspector.get_columns("pincer_session_images")} == {
            "session_id": False,
            "sha256": False,
            "media_type": False,
            "data": False,
            "created_at": False,
        }
        assert inspector.get_pk_constraint("pincer_session_images")["constrained_columns"] == ["session_id", "sha256"]
        (fk,) = inspector.get_foreign_keys("pincer_session_images")
        assert (fk["referred_table"], fk["referred_columns"], fk["constrained_columns"]) == (
            "pincer_sessions",
            ["session_id"],
            ["session_id"],
        )
        assert (fk["options"].get("ondelete"), fk["options"].get("onupdate")) == ("CASCADE", "CASCADE")

        with engine_.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO pincer_sessions (session_id, user_id, channel, created_at, updated_at) "
                    "VALUES ('web:alice', 'alice', 'web', 1.0, 1.0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO pincer_session_images (session_id, sha256, media_type, data, created_at) "
                    "VALUES ('web:alice', :sha, 'image/jpeg', :data, 1.0)"
                ),
                {"sha": "a" * 64, "data": b"\xff\xd8 photo bytes"},
            )

        command.downgrade(cfg, "0029")
        assert "pincer_session_images" not in sa.inspect(engine_).get_table_names()
        assert "pincer_sessions" in sa.inspect(engine_).get_table_names()
        assert _version(engine_) == "0029"

        command.upgrade(cfg, "0030")
        assert "pincer_session_images" in sa.inspect(engine_).get_table_names()
        assert _version(engine_) == "0030"
        with engine_.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_session_images")).scalar_one() == 0
    finally:
        engine_.dispose()


# ── 0031: dormant tables ───────────────────────────────────────────────


def test_0031_drops_the_dormant_tables_and_the_downgrade_recreates_them_empty(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0030")
    engine_ = sa.create_engine(migration_url)
    try:
        assert set(sa.inspect(engine_).get_table_names()) >= _DORMANT
        with engine_.begin() as conn:
            conn.execute(sa.text("INSERT INTO pincer_habits (pincer_user_id, habit_name) VALUES ('alice', 'run')"))

        command.upgrade(cfg, "0031")
        assert not (_DORMANT & set(sa.inspect(engine_).get_table_names()))
        assert _version(engine_) == "0031"

        command.downgrade(cfg, "0030")
        inspector = sa.inspect(engine_)
        assert set(inspector.get_table_names()) >= _DORMANT
        assert _version(engine_) == "0030"
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("pincer_expenses")} == {
            "pincer_identity_profiles"
        }
        with engine_.connect() as conn:
            # An expression index: SQLite's reflection skips it, so read the catalog.
            index_names = (
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                if engine_.dialect.name == "sqlite"
                else "SELECT indexname FROM pg_indexes"
            )
            assert "idx_habit_checkins_daily" in set(conn.execute(sa.text(index_names)).scalars())
            assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_habits")).scalar_one() == 0

        command.upgrade(cfg, "0031")
        assert not (_DORMANT & set(sa.inspect(engine_).get_table_names()))
    finally:
        engine_.dispose()


# ── 0032: UUIDv7 ids for the natural-key tables ────────────────────────


def _0032_tables() -> tuple[tuple[str, str, str, str, tuple[str, ...]], ...]:
    path = Path(__file__).parents[1] / "src/pincer/db/migrations/versions/0032_uuid7_natural_key_tables.py"
    spec = importlib.util.spec_from_file_location("rev_0032", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TABLES


def _member_fk(inspector: sa.Inspector) -> tuple[str, list[str]]:
    """What a thread member's foreign key points at."""
    (fk,) = [
        fk
        for fk in inspector.get_foreign_keys("pincer_call_thread_members")
        if fk["constrained_columns"] == ["thread_id"]
    ]
    return fk["referred_table"], fk["referred_columns"]


def test_0032_gives_each_natural_key_table_a_dated_id_and_keeps_the_old_key_unique(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0031")
    with _connect(migration_url) as conn:
        for insert in _SEED_0032:
            conn.execute(sa.text(insert))
        conn.execute(sa.text(f"INSERT INTO pincer_telephony_calls (call_id) VALUES ('{_TURN_CALL}')"))
        conn.execute(
            sa.text(
                f"INSERT INTO pincer_telephony_turns (turn_id, call_id, created_at) "
                f"VALUES ('{_TURN}', '{_TURN_CALL}', '2026-01-01T00:00:00')"
            )
        )
        before = {table: _shape(sa.inspect(conn), table) for table, *_ in _0032_tables()}

    command.upgrade(cfg, "0032")
    with _connect(migration_url) as conn:
        inspector = sa.inspect(conn)
        for table, key, *_ in _0032_tables():
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"], table
            assert [key] in [u["column_names"] for u in inspector.get_unique_constraints(table)], table
            columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
            assert (columns["id"], columns[key]) == (False, False), table
            assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE id IS NULL")).scalar_one() == 0  # noqa: S608

        def ids(table: str, key: str) -> list[str]:
            rows = conn.execute(sa.text(f"SELECT {key}, id FROM {table} ORDER BY id")).all()  # noqa: S608
            return [str(row[0]) for row in rows]

        # The UUIDv7 keys since 0018 are renamed `id`, not re-minted.
        for table, old in _RENAMED_0032:
            names = {c["name"] for c in inspector.get_columns(table)}
            assert "id" in names and old not in names, table
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"], table
        assert str(conn.execute(sa.text("SELECT id FROM pincer_telephony_turns")).scalar_one()) == _TURN
        assert str(conn.execute(sa.text("SELECT id FROM pincer_call_threads")).scalar_one()) == _THREAD_0032
        assert _member_fk(inspector) == ("pincer_call_threads", ["id"])
        # The outside keys say they are one, and their constraints say so too.
        for table, old, new in _REFERENCES_0032:
            assert old not in {c["name"] for c in inspector.get_columns(table)}, table
            assert {u["name"] for u in inspector.get_unique_constraints(table)} == {f"uq_{table[7:]}_{new}"}, table

        # Ordered by the row's own time, not its key.
        assert ids("pincer_call_costs", "call_sid") == ["CA2", "CA1"]
        assert ids("pincer_telephony_events", "reference_event_id") == ["e-b", "e-a"]

        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text(
                    "INSERT INTO pincer_call_costs (id, call_sid, recorded_at) "
                    "VALUES ('01890000-0000-7000-8000-0000000000ff', 'CA1', '2026-04-01T00:00:00')"
                )
            )

    command.downgrade(cfg, "0031")
    with _connect(migration_url) as conn:
        after = {table: _shape(sa.inspect(conn), table) for table, *_ in _0032_tables()}
        assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_call_costs")).scalar_one() == 2
        inspector = sa.inspect(conn)
        for table, old in _RENAMED_0032:
            assert inspector.get_pk_constraint(table)["constrained_columns"] == [old], table
        assert _member_fk(inspector) == ("pincer_call_threads", ["thread_id"])
    if migration_url.startswith("sqlite"):
        # SQLite let these TEXT primary keys hold NULL; the restored key says
        # NOT NULL (`unswap_primary_key`). Everything else is as it was.
        old_names = {new: old for _table, old, new in _REFERENCES_0032}
        for table, key, *_ in _0032_tables():
            old = old_names.get(key, key)
            before[table]["columns"] = [(n, False if n == old else null) for n, null in before[table]["columns"]]
    assert after == before

    command.upgrade(cfg, "0032")


def test_0032_drops_rows_whose_sqlite_text_key_is_null(migration_url, tmp_path):
    if not migration_url.startswith("sqlite"):
        pytest.skip("only SQLite let a TEXT primary key hold NULL")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0031")
    with _connect(migration_url) as conn:
        for phone in ("NULL", "NULL", "'+15550001'"):
            conn.execute(
                sa.text(
                    "INSERT INTO pincer_do_not_call_numbers (phone_number, added_at) "  # noqa: S608
                    f"VALUES ({phone}, '2026-01-01T00:00:00')"
                )
            )

    command.upgrade(cfg, "0032")
    with _connect(migration_url) as conn:
        rows = conn.execute(sa.text("SELECT phone_number, id FROM pincer_do_not_call_numbers")).all()
    assert [row[0] for row in rows] == ["+15550001"]
    assert rows[0][1] is not None


# ── 0033: identity profile ids ─────────────────────────────────────────


def test_0033_keys_identities_by_id_and_points_every_user_reference_at_it(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0032")
    sqlite = migration_url.startswith("sqlite")
    with _connect(migration_url) as conn:
        for insert in (*_SEED_0033, *((_ORPHAN_0033,) if sqlite else ())):
            conn.execute(sa.text(insert))
        before = {
            table: _shape(sa.inspect(conn), table)
            for table in ("pincer_identity_profiles", "pincer_channel_identities", "pincer_schedules")
        }

    command.upgrade(cfg, "0033")
    with _connect(migration_url) as conn:
        inspector = sa.inspect(conn)
        tables = set(inspector.get_table_names())
        assert "pincer_identity_channels" in tables
        assert "pincer_channel_identities" not in tables
        assert inspector.get_pk_constraint("pincer_identity_profiles")["constrained_columns"] == ["id"]
        for table in ("pincer_identity_channels", "pincer_identity_phones", "pincer_identity_credentials"):
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"], table
        for table in (
            "pincer_identity_channels",
            "pincer_identity_phones",
            "pincer_identity_credentials",
            "pincer_auth_sessions",
        ):
            columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
            assert "pincer_user_id" not in columns, table
            assert columns["profile_id"] is False, table

        ids = dict(conn.execute(sa.text("SELECT pincer_user_id, id FROM pincer_identity_profiles")).all())
        # Dated by `created_at`: bob was made first.
        assert str(ids["bob"]) < str(ids["alice"])

        def owner(sql: str) -> list[str]:
            return [str(row[0]) for row in conn.execute(sa.text(sql)).all()]

        assert owner(
            "SELECT p.pincer_user_id FROM pincer_identity_channels c "
            "JOIN pincer_identity_profiles p ON p.id = c.profile_id ORDER BY c.channel_user_id"
        ) == ["alice", "bob"]  # the orphan is gone
        assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_identity_channels")).scalar_one() == 2
        # Which phone a LID link stands for is not known yet: the next message records it.
        assert {c["name"]: c["nullable"] for c in inspector.get_columns("pincer_identity_channels")}["alt_id"]
        assert "idx_identity_channels_alt" in {i["name"] for i in inspector.get_indexes("pincer_identity_channels")}
        assert (
            conn.execute(sa.text("SELECT COUNT(*) FROM pincer_identity_channels WHERE alt_id IS NOT NULL")).scalar_one()
            == 0
        )
        assert owner("SELECT CAST(profile_id AS TEXT) FROM pincer_identity_phones") == [str(ids["alice"])]
        assert owner("SELECT CAST(profile_id AS TEXT) FROM pincer_auth_sessions") == [str(ids["alice"])]
        schedules = {
            str(row[0]): (row[1], None if row[2] is None else str(row[2]))
            for row in conn.execute(sa.text("SELECT id, pincer_user_id, profile_id FROM pincer_schedules")).all()
        }
        assert schedules == {_S1: ("alice", str(ids["alice"])), _S2: ("system", None)}
        assert owner("SELECT CAST(profile_id AS TEXT) FROM pincer_memories") == [str(ids["alice"])]
        if sqlite:
            # The rebuild kept the full-text index in step with the table.
            assert (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM pincer_memories_fts WHERE pincer_memories_fts MATCH 'tea'")
                ).scalar_one()
                == 1
            )

    command.downgrade(cfg, "0032")
    with _connect(migration_url) as conn:
        inspector = sa.inspect(conn)
        assert "pincer_channel_identities" in set(inspector.get_table_names())
        after = {table: _shape(inspector, table) for table in before}
        links = conn.execute(
            sa.text("SELECT channel_user_id, pincer_user_id FROM pincer_channel_identities ORDER BY channel_user_id")
        ).all()
        assert [tuple(row) for row in links] == [("1", "alice"), ("2", "bob")]
        assert conn.execute(sa.text("SELECT pincer_user_id FROM pincer_identity_credentials")).scalar_one() == "alice"
        assert conn.execute(sa.text("SELECT pincer_user_id FROM pincer_auth_sessions")).scalar_one() == "alice"
        assert "profile_id" not in {c["name"] for c in inspector.get_columns("pincer_schedules")}
    if sqlite:
        # The restored keys say NOT NULL, as `unswap_primary_key` documents.
        before["pincer_identity_profiles"]["columns"] = [
            (n, False if n == "pincer_user_id" else null) for n, null in before["pincer_identity_profiles"]["columns"]
        ]
        before["pincer_channel_identities"]["columns"] = [
            (n, False if n in ("channel", "channel_user_id") else null)
            for n, null in before["pincer_channel_identities"]["columns"]
        ]
    assert after == before

    command.upgrade(cfg, "0033")


# ── 0034: session ids ──────────────────────────────────────────────────


def test_0034_gives_sessions_photos_and_onboarding_an_id(migration_url, tmp_path):
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0033")
    sqlite = migration_url.startswith("sqlite")
    seed = [*_SEED_0034, *((_ORPHAN_0034,) if sqlite else ())]
    if not sqlite:
        seed = [insert.replace("X'FF'", "'\\xff'::bytea") for insert in seed]
    with _connect(migration_url) as conn:
        for insert in seed:
            conn.execute(sa.text(insert))
        before = {
            table: _shape(sa.inspect(conn), table)
            for table in ("pincer_sessions", "pincer_session_images", "pincer_onboarding")
        }

    command.upgrade(cfg, "0034")
    with _connect(migration_url) as conn:
        inspector = sa.inspect(conn)
        for table in ("pincer_sessions", "pincer_session_images", "pincer_onboarding"):
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"], table
        (fk,) = inspector.get_foreign_keys("pincer_session_images")
        assert (fk["constrained_columns"], fk["referred_columns"]) == (["session_ref"], ["id"])

        def ordered(sql: str) -> list[str]:
            return [str(row[0]) for row in conn.execute(sa.text(sql)).all()]

        assert ordered("SELECT session_id FROM pincer_sessions ORDER BY id") == ["web:guest", "unified:alice"]
        assert ordered("SELECT user_id FROM pincer_onboarding ORDER BY id") == ["guest", "alice"]
        photos = conn.execute(
            sa.text(
                "SELECT s.session_id, i.sha256 FROM pincer_session_images i "
                "JOIN pincer_sessions s ON s.id = i.session_ref"
            )
        ).all()
        assert [tuple(row) for row in photos] == [("unified:alice", "aa")]  # the orphan is gone
        assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_session_images")).scalar_one() == 1
        profiles = dict(conn.execute(sa.text("SELECT user_id, CAST(profile_id AS TEXT) FROM pincer_sessions")).all())
        assert profiles == {"alice": "01890000-0000-7000-8000-0000000000d1", "guest": None}

    command.downgrade(cfg, "0033")
    with _connect(migration_url) as conn:
        after = {table: _shape(sa.inspect(conn), table) for table in before}
        photos = conn.execute(sa.text("SELECT session_id, sha256 FROM pincer_session_images")).all()
        assert [tuple(row) for row in photos] == [("unified:alice", "aa")]
    if sqlite:
        # The restored keys say NOT NULL, as `unswap_primary_key` documents.
        before["pincer_sessions"]["columns"] = [
            (n, False if n == "session_id" else null) for n, null in before["pincer_sessions"]["columns"]
        ]
    assert after == before

    command.upgrade(cfg, "0034")


# ── 0031–0034: retryable on SQLite, and no silent drops ────────────────


def test_a_failed_sqlite_downgrade_rolls_back_whole_and_can_be_retried(migration_url, tmp_path, monkeypatch):
    """The 0034 downgrade starts with a DROP INDEX. Without `hold_transaction`
    the sqlite3 driver committed it on its own, so a failure further on left
    the index gone at revision 0034 and the retry died on "no such index"."""
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres DDL is transactional")
    import pincer.db.migration_helpers as helpers

    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0034")
    unswap = helpers.unswap_primary_key

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("fails part-way")

    monkeypatch.setattr(helpers, "unswap_primary_key", fail)
    with pytest.raises(RuntimeError, match="part-way"):
        command.downgrade(cfg, "0033")
    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT version_num FROM pincer_alembic_version")).scalar_one() == "0034"
        index = "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ix_pincer_onboarding_profile_id'"
        assert conn.execute(sa.text(index)).scalar_one() == 1

    monkeypatch.setattr(helpers, "unswap_primary_key", unswap)
    command.downgrade(cfg, "0033")


def test_0034_downgrade_drops_and_logs_photos_whose_session_is_gone(migration_url, tmp_path, caplog):
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres enforces the CASCADE: no such photo can exist")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0034")
    with _connect(migration_url) as conn:
        conn.execute(
            sa.text(
                "INSERT INTO pincer_sessions (id, session_id, user_id, channel, created_at, updated_at) "
                f"VALUES ('{_GONE}', 'unified:alice', 'alice', 'telegram', 1.0, 1.0)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO pincer_session_images (id, session_ref, sha256, media_type, data, created_at) "
                f"VALUES ('01890000-0000-7000-8000-0000000000e1', '{_GONE}', 'aa', 'image/jpeg', X'FF', 1.0)"
            )
        )
        conn.execute(sa.text("DELETE FROM pincer_sessions"))  # foreign keys off: the photo stays

    with caplog.at_level(logging.WARNING, logger="pincer.db.migrations"):
        command.downgrade(cfg, "0033")

    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_session_images")).scalar_one() == 0
    assert any("pincer_session_images" in line and _GONE in line for line in caplog.messages)


def test_0033_downgrade_drops_and_logs_identity_rows_whose_profile_is_gone(migration_url, tmp_path, caplog):
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres enforces the foreign keys: no such row can exist")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0033")
    with _connect(migration_url) as conn:
        conn.execute(sa.text(f"INSERT INTO pincer_identity_profiles (id, pincer_user_id) VALUES ('{_GONE}', 'alice')"))
        conn.execute(
            sa.text(
                "INSERT INTO pincer_identity_phones (id, phone, profile_id, source) "
                f"VALUES ('01890000-0000-7000-8000-0000000000e2', '+491', '{_GONE}', 'config')"
            )
        )
        conn.execute(sa.text("DELETE FROM pincer_identity_profiles"))  # foreign keys off: the phone stays

    with caplog.at_level(logging.WARNING, logger="pincer.db.migrations"):
        command.downgrade(cfg, "0032")

    with _connect(migration_url) as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM pincer_identity_phones")).scalar_one() == 0
    assert any("pincer_identity_phones" in line and _GONE in line for line in caplog.messages)


def test_0033_upgrade_logs_the_identity_rows_it_drops(migration_url, tmp_path, caplog):
    if not migration_url.startswith("sqlite"):
        pytest.skip("Postgres enforced the foreign key: no orphan link to drop")
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0032")
    with _connect(migration_url) as conn:
        conn.execute(sa.text(_ORPHAN_0033))

    with caplog.at_level(logging.WARNING, logger="pincer.db.migrations"):
        command.upgrade(cfg, "0033")

    assert any("pincer_identity_channels" in line and "ghost" in line for line in caplog.messages)


# ── 0035: session messages as rows, session times as millisecond datetimes ──


def _0035_tables():
    from pincer.db.types import JsonbDocument, JsonDocument, UtcDateTime, Uuid7

    sessions_at_0034 = sa.table(
        "pincer_sessions",
        sa.column("id", Uuid7()),
        sa.column("session_id", sa.Text()),
        sa.column("user_id", sa.Text()),
        sa.column("channel", sa.Text()),
        sa.column("messages", sa.Text()),
        sa.column("created_at", sa.Float()),
        sa.column("updated_at", sa.Float()),
    )
    sessions = sa.table(
        "pincer_sessions",
        sa.column("id", Uuid7()),
        sa.column("session_id", sa.Text()),
        sa.column("created_at", UtcDateTime()),
        sa.column("updated_at", UtcDateTime()),
    )
    messages = sa.table(
        "pincer_session_messages",
        sa.column("id", Uuid7()),
        sa.column("session_id", Uuid7()),
        sa.column("role", sa.Text()),
        sa.column("content", sa.Text()),
        sa.column("tool_call_id", sa.Text()),
        sa.column("is_error", sa.Boolean()),
        sa.column("tool_calls", JsonDocument),
        sa.column("images", JsonbDocument),
        sa.column("context", sa.Text()),
        sa.column("raw", JsonDocument),
        sa.column("wire", sa.Text()),
        sa.column("channel_message_id", sa.Text()),
        sa.column("created_at", UtcDateTime()),
        sa.column("updated_at", UtcDateTime()),
    )
    return sessions_at_0034, sessions, messages


#: A history with every field a stored message can have, keys deliberately out of alphabetical order:
#: the provider output and tool arguments must come back as they were written (they are sent again).
_HISTORY_0035 = [
    {
        "role": "system",
        "content": "[compacted]",
        "raw": [{"type": "compaction", "id": "cmp_1", "encrypted_content": "ENC"}],
        "wire": "openai:gpt-x",
    },
    {
        "role": "user",
        "content": "hi\x00there",
        "images": [{"sha256": "aa", "media_type": "image/jpeg"}],
        "context": "[memories]",
    },
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "name": "lookup", "arguments": {"zone": "eu", "after": 2}}],
        "raw": [
            {"type": "thinking", "thinking": "hm", "signature": "SIG"},
            {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"zone": "eu", "after": 2}},
        ],
        "wire": "anthropic:claude-x",
    },
    {"role": "tool_result", "content": "ok", "tool_call_id": "t1", "is_error": True},
    # A lone surrogate (half an emoji) cannot be encoded as UTF-8: Postgres would refuse it in text and json
    {"role": "assistant", "content": "done \ud83d", "raw": [{"type": "text", "text": "x \ude00"}], "wire": "w"},
]


def test_0035_moves_each_message_to_a_row_dated_across_its_session_s_life(migration_url, tmp_path):
    from pincer.db.ids import new_id, uuid7_ms
    from pincer.db.types import epoch_ms, from_epoch_seconds

    sessions_at_0034, sessions, messages = _0035_tables()
    cfg = _config(migration_url, tmp_path)
    command.upgrade(cfg, "0034")
    ids = {key: new_id() for key in ("alice", "guest", "broken", "odd")}
    seed = [
        ("alice", "unified:alice", json.dumps(_HISTORY_0035), 100.0, 100.4),
        ("guest", "web:guest", json.dumps([{"role": "user", "content": "only"}]), 50.123, 50.123),
        ("broken", "web:broken", "not json", 1.0, 2.0),
        ("odd", "web:odd", json.dumps([{"role": "tool", "content": "?"}, {"role": "user", "content": "kept"}]), 10, 11),
    ]
    with _connect(migration_url) as conn:
        conn.execute(
            sessions_at_0034.insert(),
            [
                {
                    "id": ids[n],
                    "session_id": k,
                    "user_id": n,
                    "channel": "web",
                    "messages": m,
                    "created_at": c,
                    "updated_at": u,
                }
                for n, k, m, c, u in seed
            ],
        )

    command.upgrade(cfg, "0035")
    with _connect(migration_url) as conn:

        def rows(name: str) -> list[Any]:
            stmt = sa.select(messages).where(messages.c.session_id == ids[name])
            return conn.execute(stmt.order_by(messages.c.created_at, messages.c.id)).mappings().all()

        alice = rows("alice")
        assert [r["role"] for r in alice] == [m["role"] for m in _HISTORY_0035]
        # Spread evenly from the session's creation to its last save, each id carrying its own millisecond
        assert [epoch_ms(r["created_at"]) for r in alice] == [100_000, 100_100, 100_200, 100_300, 100_400]
        assert [uuid7_ms(r["id"]) for r in alice] == [epoch_ms(r["created_at"]) for r in alice]
        window, user, call, result, reply = alice
        assert (window["raw"], window["wire"], window["content"]) == (
            _HISTORY_0035[0]["raw"],
            "openai:gpt-x",
            "[compacted]",
        )
        assert json.dumps(call["raw"]) == json.dumps(_HISTORY_0035[2]["raw"])  # key order kept, not sorted
        assert json.dumps(call["tool_calls"]) == json.dumps(_HISTORY_0035[2]["tool_calls"])
        assert (user["content"], user["context"], user["images"]) == (
            "hithere",  # Postgres takes no NUL in text
            "[memories]",
            [{"sha256": "aa", "media_type": "image/jpeg"}],
        )
        assert (result["tool_call_id"], result["is_error"], result["tool_calls"], result["raw"]) == (
            "t1",
            True,
            None,
            None,
        )
        assert (reply["content"], reply["raw"], reply["wire"], reply["context"]) == (
            "done ?",
            [{"type": "text", "text": "x ?"}],
            "w",
            None,
        )
        assert all(r["channel_message_id"] is None and r["updated_at"] is None for r in alice)

        (only,) = rows("guest")
        assert only["created_at"] == from_epoch_seconds(50.123)
        assert rows("broken") == []  # not a list: nothing to move, and the upgrade goes on
        assert [r["content"] for r in rows("odd")] == ["kept"]  # a role no code writes is dropped

        times = dict(
            (r["session_id"], (r["created_at"], r["updated_at"])) for r in conn.execute(sa.select(sessions)).mappings()
        )
        assert times["unified:alice"] == (from_epoch_seconds(100.0), from_epoch_seconds(100.4))
        assert times["web:guest"] == (from_epoch_seconds(50.123),) * 2
        assert times["web:guest"][0].microsecond == 123_000  # milliseconds kept, nothing finer
        if not migration_url.startswith("sqlite"):
            precision = conn.execute(
                sa.text(
                    "SELECT table_name, column_name, datetime_precision FROM information_schema.columns "
                    "WHERE table_name IN ('pincer_sessions', 'pincer_session_messages') "
                    "AND data_type LIKE 'timestamp%' ORDER BY 1, 2"
                )
            ).all()
            assert [tuple(row) for row in precision] == [
                ("pincer_session_messages", "created_at", 3),
                ("pincer_session_messages", "updated_at", 3),
                ("pincer_sessions", "created_at", 3),
                ("pincer_sessions", "updated_at", 3),
            ]

    with _connect(migration_url) as conn:  # the guest clears their conversation after the upgrade
        conn.execute(sa.delete(messages).where(messages.c.session_id == ids["guest"]))

    command.downgrade(cfg, "0034")
    with _connect(migration_url) as conn:
        assert "pincer_session_messages" not in sa.inspect(conn).get_table_names()
        back = {r["session_id"]: r for r in conn.execute(sa.select(sessions_at_0034)).mappings()}
    restored = json.loads(back["unified:alice"]["messages"])
    expected = [dict(m) for m in _HISTORY_0035]
    expected[1]["content"] = "hithere"
    expected[4] = {"role": "assistant", "content": "done ?", "raw": [{"type": "text", "text": "x ?"}], "wire": "w"}
    assert restored == expected
    assert [list(m) for m in restored] == [list(m) for m in expected]  # the keys in `to_dict`'s order
    # Every session's history is its rows: a cleared one does not get back what it had before the upgrade,
    # nor does one whose stored text never was a history
    assert back["web:guest"]["messages"] == "[]"
    assert back["web:broken"]["messages"] == "[]"
    assert json.loads(back["web:odd"]["messages"]) == [{"role": "user", "content": "kept"}]
    assert (back["unified:alice"]["created_at"], back["unified:alice"]["updated_at"]) == pytest.approx((100.0, 100.4))
    assert back["web:guest"]["created_at"] == pytest.approx(50.123)

    command.upgrade(cfg, "0035")  # and up again

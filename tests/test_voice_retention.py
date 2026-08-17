"""Tests for GDPR transcript retention purge (Sprint 0, DACH)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from pincer.voice.retention import (
    ensure_voice_tables,
    make_retention_handler,
    purge_expired_voice_data,
    run_retention_purge,
)


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


async def _seed(db_path) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT INTO voice_calls (call_sid, started_at) VALUES (?, ?), (?, ?)",
            ("CA_old", _iso(100), "CA_new", _iso(1)),
        )
        await db.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, timestamp) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            ("CA_old", "caller", "old utterance", _iso(100), "CA_new", "caller", "new utterance", _iso(1)),
        )
        await db.execute(
            "INSERT INTO call_actions (call_id, action_type, timestamp) VALUES (?, ?, ?), (?, ?, ?)",
            ("CA_old", "tool_call", _iso(100), "CA_new", "tool_call", _iso(1)),
        )
        await db.commit()


@pytest.fixture
def voice_db(tmp_path):
    return tmp_path / "pincer.db"


async def test_purge_deletes_only_expired_rows(voice_db):
    await _seed(voice_db)

    deleted = await purge_expired_voice_data(voice_db, retention_days=90)

    assert deleted == {"voice_calls": 1, "call_transcripts": 1, "call_actions": 1}
    remaining_keys = (("voice_calls", "call_sid"), ("call_transcripts", "call_id"), ("call_actions", "call_id"))
    async with aiosqlite.connect(str(voice_db)) as db:
        for table, key_col in remaining_keys:
            rows = await db.execute_fetchall(f"SELECT {key_col} FROM {table}")  # noqa: S608
            assert [r[0] for r in rows] == ["CA_new"], table


async def test_purge_zero_retention_is_noop(voice_db):
    await _seed(voice_db)

    deleted = await purge_expired_voice_data(voice_db, retention_days=0)

    assert deleted == {}
    async with aiosqlite.connect(str(voice_db)) as db:
        rows = await db.execute_fetchall("SELECT COUNT(*) FROM call_transcripts")
        assert rows[0][0] == 2


async def test_purge_skips_missing_tables(voice_db):
    async with aiosqlite.connect(str(voice_db)) as db:
        await db.execute("CREATE TABLE unrelated (id INTEGER)")
        await db.commit()

    deleted = await purge_expired_voice_data(voice_db, retention_days=90)
    assert deleted == {}


async def test_run_retention_purge_writes_audit_entry(voice_db, tmp_path, monkeypatch):
    import pincer.security.audit as audit_module
    from pincer.security.audit import AuditLogger

    await _seed(voice_db)

    audit = AuditLogger(db_path=tmp_path / "audit.db")
    await audit.initialize()
    monkeypatch.setattr(audit_module, "_audit_logger", audit)

    settings = SimpleNamespace(db_path=voice_db, voice_transcript_retention_days=90)
    try:
        deleted = await run_retention_purge(settings)
        assert sum(deleted.values()) == 3
        await audit._flush_pending()

        async with aiosqlite.connect(str(tmp_path / "audit.db")) as db:
            rows = await db.execute_fetchall(
                "SELECT user_id, output_summary FROM audit_log WHERE action = 'retention_purge'"
            )
        assert len(rows) == 1
        assert rows[0][0] == "system"
        assert "call_transcripts: 1 row(s)" in rows[0][1]
    finally:
        await audit.shutdown()


async def test_run_retention_purge_no_deletions_no_audit(voice_db, tmp_path, monkeypatch):
    import pincer.security.audit as audit_module
    from pincer.security.audit import AuditLogger

    async with aiosqlite.connect(str(voice_db)) as db:
        await ensure_voice_tables(db)

    audit = AuditLogger(db_path=tmp_path / "audit.db")
    await audit.initialize()
    monkeypatch.setattr(audit_module, "_audit_logger", audit)

    settings = SimpleNamespace(db_path=voice_db, voice_transcript_retention_days=90)
    try:
        deleted = await run_retention_purge(settings)
        assert deleted == {}
        await audit._flush_pending()
        async with aiosqlite.connect(str(tmp_path / "audit.db")) as db:
            rows = await db.execute_fetchall("SELECT COUNT(*) FROM audit_log")
        assert rows[0][0] == 0
    finally:
        await audit.shutdown()


async def test_retention_handler_swallows_errors():
    settings = SimpleNamespace(db_path="/nonexistent/dir/pincer.db", voice_transcript_retention_days=90)
    handler = make_retention_handler(settings)
    # must not raise, and must not message the user
    assert await handler(pincer_user_id="system", action={"type": "retention_purge"}, channel="telegram") is None

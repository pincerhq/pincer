"""Tests for the audit logger."""

import asyncio
import json

import pytest
import pytest_asyncio

from pincer.security.audit import AuditAction, AuditEntry, AuditLogger


@pytest_asyncio.fixture
async def audit_logger(tmp_path):
    logger = AuditLogger(db_path=tmp_path / "test_audit.db")
    await logger.initialize()
    yield logger
    await logger.shutdown()


@pytest.mark.asyncio
async def test_log_and_query(audit_logger):
    await audit_logger.log(
        AuditEntry(
            user_id="test_user",
            action=AuditAction.TOOL_CALL,
            tool="web_search",
            input_summary="query: 'hello'",
            output_summary="3 results",
            cost_usd=0.001,
        )
    )
    await audit_logger._flush_pending()
    results = await audit_logger.query(user_id="test_user")
    assert len(results) == 1
    assert results[0]["tool"] == "web_search"
    assert results[0]["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_track_context_manager(audit_logger):
    async with audit_logger.track(
        user_id="u1",
        action=AuditAction.LLM_REQUEST,
        tool="claude-sonnet",
    ) as entry:
        await asyncio.sleep(0.01)
        entry.output_summary = "done"

    await audit_logger._flush_pending()
    results = await audit_logger.query(action=AuditAction.LLM_REQUEST)
    assert len(results) == 1
    assert results[0]["duration_ms"] >= 10


@pytest.mark.asyncio
async def test_track_error_handling(audit_logger):
    with pytest.raises(ValueError):
        async with audit_logger.track(
            user_id="u1",
            action=AuditAction.TOOL_CALL,
            tool="bad_tool",
        ):
            raise ValueError("test error")

    await audit_logger._flush_pending()
    results = await audit_logger.query(user_id="u1")
    assert len(results) == 1
    assert results[0]["approved"] == 0
    assert "ValueError" in results[0]["output_summary"]


@pytest.mark.asyncio
async def test_export_json(audit_logger, tmp_path):
    for i in range(10):
        await audit_logger.log(
            AuditEntry(
                user_id="export_user",
                action=AuditAction.MESSAGE_RECEIVED,
                input_summary=f"msg {i}",
            )
        )
    await audit_logger._flush_pending()

    export_path = tmp_path / "export.json"
    count = await audit_logger.export_json(export_path)
    assert count == 10

    with open(export_path) as f:
        data = json.load(f)
    assert len(data) == 10


@pytest.mark.asyncio
async def test_stats(audit_logger):
    await audit_logger.log(
        AuditEntry(
            user_id="u1",
            action=AuditAction.TOOL_CALL,
            tool="search",
            cost_usd=0.01,
        )
    )
    await audit_logger.log(
        AuditEntry(
            user_id="u1",
            action=AuditAction.LLM_REQUEST,
            cost_usd=0.05,
        )
    )
    await audit_logger.log(
        AuditEntry(
            user_id="u1",
            action=AuditAction.ERROR,
            approved=False,
        )
    )
    await audit_logger._flush_pending()

    stats = await audit_logger.get_stats()
    assert stats["total_entries"] == 3
    assert stats["total_cost_usd"] == 0.06
    assert stats["failed_actions"] == 1


@pytest.mark.asyncio
async def test_query_with_filters(audit_logger):
    await audit_logger.log(AuditEntry(user_id="alice", action=AuditAction.TOOL_CALL, tool="search"))
    await audit_logger.log(AuditEntry(user_id="bob", action=AuditAction.LLM_REQUEST))
    await audit_logger.log(AuditEntry(user_id="alice", action=AuditAction.MESSAGE_RECEIVED))
    await audit_logger._flush_pending()

    alice_results = await audit_logger.query(user_id="alice")
    assert len(alice_results) == 2

    tool_results = await audit_logger.query(action=AuditAction.TOOL_CALL)
    assert len(tool_results) == 1

    paginated = await audit_logger.query(limit=1, offset=0)
    assert len(paginated) == 1


@pytest.mark.asyncio
async def test_summary_truncation(audit_logger):
    long_text = "x" * 5000
    await audit_logger.log(
        AuditEntry(
            user_id="u1",
            action=AuditAction.TOOL_CALL,
            input_summary=long_text,
        )
    )
    await audit_logger._flush_pending()
    results = await audit_logger.query(user_id="u1")
    assert len(results[0]["input_summary"]) == audit_logger.MAX_SUMMARY_LENGTH

"""Tests for MCPAuditLogger — event emission and truncation."""

from __future__ import annotations

from pincer.mcp.audit import MCPAuditLogger, _truncate_args  # noqa: PLC0415

# ── Argument truncation ───────────────────────────────────────────────────────


def test_truncate_args_short() -> None:
    result = _truncate_args({"query": "hello"})
    assert result is not None
    assert "hello" in result


def test_truncate_args_long() -> None:
    big_value = "x" * 1000
    result = _truncate_args({"data": big_value})
    assert result is not None
    assert len(result) <= 503  # 500 + "..."


def test_truncate_args_none() -> None:
    assert _truncate_args(None) is None


def test_truncate_args_empty() -> None:
    assert _truncate_args({}) is None


# ── No-op when audit_logger is None ──────────────────────────────────────────


async def test_log_connect_no_audit_logger() -> None:
    audit = MCPAuditLogger(None)
    # Should not raise
    await audit.log_connect("myserver", success=True)
    await audit.log_connect("myserver", success=False, error="refused")


async def test_log_tool_call_no_audit_logger() -> None:
    audit = MCPAuditLogger(None)
    audit.start_call("srv", "my_tool", "key1")
    await audit.log_tool_call("srv", "my_tool", "key1", {"x": 1}, success=True, output_length=100)


async def test_log_disconnect_no_audit_logger() -> None:
    audit = MCPAuditLogger(None)
    await audit.log_disconnect("myserver")
    await audit.log_disconnect("myserver", error="timeout")


async def test_log_health_check_no_audit_logger() -> None:
    audit = MCPAuditLogger(None)
    await audit.log_health_check("srv", healthy=True)
    await audit.log_health_check("srv", healthy=False)


async def test_log_reconnect_no_audit_logger() -> None:
    audit = MCPAuditLogger(None)
    await audit.log_reconnect("srv", attempt=1, success=True)
    await audit.log_reconnect("srv", attempt=2, success=False, error="refused")


# ── With real audit logger ────────────────────────────────────────────────────


async def test_log_tool_call_writes_to_audit(tmp_path) -> None:
    from pincer.security.audit import AuditLogger

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    audit = MCPAuditLogger(real_logger)
    audit.start_call("github", "list_repos", "k1")
    await audit.log_tool_call(
        "github",
        "list_repos",
        "k1",
        {"owner": "me"},
        success=True,
        output_length=42,
    )

    # Flush the write queue before querying
    await real_logger._flush_pending()
    results = await real_logger.query(user_id="mcp:github")
    assert len(results) == 1
    assert results[0]["tool"] == "list_repos"
    assert results[0]["approved"] == 1

    await real_logger.shutdown()


async def test_log_connect_tagged_with_server(tmp_path) -> None:
    from pincer.security.audit import AuditLogger

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    audit = MCPAuditLogger(real_logger)
    await audit.log_connect("myserver", success=True)

    await real_logger._flush_pending()
    results = await real_logger.query(user_id="mcp:myserver")
    assert len(results) == 1
    assert results[0]["user_id"] == "mcp:myserver"

    await real_logger.shutdown()


async def test_failed_tool_call_marked_as_not_approved(tmp_path) -> None:
    from pincer.security.audit import AuditLogger

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    audit = MCPAuditLogger(real_logger)
    audit.start_call("srv", "bad_tool", "k2")
    await audit.log_tool_call("srv", "bad_tool", "k2", {}, success=False, error="timeout")

    await real_logger._flush_pending()
    results = await real_logger.query(user_id="mcp:srv")
    assert len(results) == 1
    assert results[0]["approved"] == 0

    await real_logger.shutdown()


def test_duration_tracked_on_call() -> None:
    """start_call / log_tool_call records elapsed time."""
    import time

    audit = MCPAuditLogger(None)
    audit.start_call("srv", "tool", "key")
    time.sleep(0.01)
    # key should have been cleaned up after logging
    assert "key" in audit._timers


def test_truncate_args_non_serializable() -> None:
    """Non-JSON-serializable objects fall back to str() truncation."""
    result = _truncate_args({"data": object()})
    assert result is not None
    assert len(result) <= 503


async def test_log_health_check_with_real_logger(tmp_path) -> None:
    from pincer.security.audit import AuditLogger

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    audit = MCPAuditLogger(real_logger)
    await audit.log_health_check("myserver", healthy=True)
    await audit.log_health_check("myserver", healthy=False)

    await real_logger._flush_pending()
    results = await real_logger.query(user_id="mcp:myserver")
    assert len(results) == 2

    await real_logger.shutdown()


async def test_log_reconnect_with_real_logger(tmp_path) -> None:
    from pincer.security.audit import AuditLogger

    db = tmp_path / "audit.db"
    real_logger = AuditLogger(db_path=db)
    await real_logger.initialize()

    audit = MCPAuditLogger(real_logger)
    await audit.log_reconnect("srv", attempt=1, success=True)
    await audit.log_reconnect("srv", attempt=2, success=False, error="timeout")

    await real_logger._flush_pending()
    results = await real_logger.query(user_id="mcp:srv")
    assert len(results) == 2
    approved_vals = {r["approved"] for r in results}
    assert 1 in approved_vals  # success
    assert 0 in approved_vals  # failure

    await real_logger.shutdown()

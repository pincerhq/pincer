"""Tests for the db/schedule/mcp-server/audit CLI commands (typer.testing.CliRunner)."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


# ── db ────────────────────────────────────────────────────────────────────


def test_db_upgrade_applies_migrations(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr("alembic.command.upgrade", lambda cfg, rev: calls.append((cfg, rev)))
    monkeypatch.setattr("pincer.db.build_config", lambda db_path: "CONFIG")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0
    assert "Database at head" in result.output
    assert calls == [("CONFIG", "head")]


def test_db_current_shows_revision(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr("alembic.command.current", lambda cfg, verbose=False: calls.append((cfg, verbose)))
    monkeypatch.setattr("pincer.db.build_config", lambda db_path: "CONFIG")

    result = runner.invoke(app, ["db", "current"])

    assert result.exit_code == 0
    assert calls == [("CONFIG", True)]


def test_db_history_shows_log(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr("alembic.command.history", lambda cfg, verbose=False: calls.append((cfg, verbose)))
    monkeypatch.setattr("pincer.db.build_config", lambda db_path: "CONFIG")

    result = runner.invoke(app, ["db", "history"])

    assert result.exit_code == 0
    assert calls == [("CONFIG", True)]


# ── schedule ──────────────────────────────────────────────────────────────


def _make_schedules_db(path: object, rows: list[tuple[str, str, str, str, int]] | None = None) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY,
            pincer_user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            action TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'telegram',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    for row in rows or []:
        conn.execute(
            "INSERT INTO schedules (pincer_user_id, name, cron_expr, action, enabled) VALUES (?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


def test_schedule_list_missing_table(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()

    mock_settings = MagicMock()
    mock_settings.db_path = db_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["schedule", "list"])

    assert result.exit_code == 0
    assert "table not created yet" in result.output


def test_schedule_list_empty(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "withtable.db"
    _make_schedules_db(db_path)

    mock_settings = MagicMock()
    mock_settings.db_path = db_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["schedule", "list"])

    assert result.exit_code == 0
    assert "No scheduled tasks." in result.output


def test_schedule_list_shows_rows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "withrows.db"
    _make_schedules_db(db_path, rows=[("user1", "daily-digest", "0 8 * * *", "send_digest", 1)])

    mock_settings = MagicMock()
    mock_settings.db_path = db_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["schedule", "list"])

    assert result.exit_code == 0
    assert "daily-digest" in result.output
    assert "user1" in result.output


# ── mcp server ────────────────────────────────────────────────────────────


def test_mcp_server_status_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = False
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "server", "status"])

    assert result.exit_code == 0
    assert "disabled" in result.output


def test_mcp_server_status_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.enabled = True
    mock_cfg.server.host = "127.0.0.1"
    mock_cfg.server.port = 8090
    mock_cfg.server.path = "/mcp"
    mock_cfg.server.expose_tools = ["shell_exec"]
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "server", "status"])

    assert result.exit_code == 0
    assert "ENABLED" in result.output
    assert "127.0.0.1:8090/mcp" in result.output
    assert "shell_exec" in result.output


def test_mcp_server_config_prints_client_json(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cfg = MagicMock()
    mock_cfg.server.host = "127.0.0.1"
    mock_cfg.server.port = 8090
    mock_cfg.server.path = "/mcp"
    monkeypatch.setattr("pincer.mcp.config.load_mcp_config", lambda: mock_cfg)

    result = runner.invoke(app, ["mcp", "server", "config"])

    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert "http://127.0.0.1:8090/mcp" in result.output


# ── audit ─────────────────────────────────────────────────────────────────


def test_audit_default_shows_no_entries(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_logger = MagicMock()
    mock_logger.initialize = AsyncMock()
    mock_logger.shutdown = AsyncMock()
    mock_logger.query = AsyncMock(return_value=[])
    monkeypatch.setattr("pincer.security.audit.AuditLogger", lambda db_path: mock_logger)

    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 0
    assert "No audit entries found." in result.output
    mock_logger.shutdown.assert_awaited_once()


def test_audit_shows_table_with_entries(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_logger = MagicMock()
    mock_logger.initialize = AsyncMock()
    mock_logger.shutdown = AsyncMock()
    mock_logger.query = AsyncMock(
        return_value=[
            {
                "timestamp": "2026-01-01T00:00:00",
                "user_id": "user1",
                "action": "tool_call",
                "tool": "shell_exec",
                "input_summary": "ls -la",
                "cost_usd": 0.001,
            }
        ]
    )
    mock_logger.get_stats = AsyncMock(return_value={"total_entries": 1, "total_cost_usd": 0.001, "failed_actions": 0})
    monkeypatch.setattr("pincer.security.audit.AuditLogger", lambda db_path: mock_logger)

    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 0
    assert "user1" in result.output
    assert "Total: 1 entries" in result.output


def test_audit_invalid_action_filter(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_logger = MagicMock()
    mock_logger.initialize = AsyncMock()
    mock_logger.shutdown = AsyncMock()
    monkeypatch.setattr("pincer.security.audit.AuditLogger", lambda db_path: mock_logger)

    result = runner.invoke(app, ["audit", "--action", "not_a_real_action"])

    assert result.exit_code == 0
    assert "Invalid action" in result.output
    mock_logger.shutdown.assert_awaited_once()


def test_audit_export(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_path / "pincer.db"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_logger = MagicMock()
    mock_logger.initialize = AsyncMock()
    mock_logger.shutdown = AsyncMock()
    mock_logger.export_json = AsyncMock(return_value=5)
    monkeypatch.setattr("pincer.security.audit.AuditLogger", lambda db_path: mock_logger)

    export_path = str(tmp_path / "out.json")
    result = runner.invoke(app, ["audit", "--export", export_path])

    assert result.exit_code == 0
    assert "Exported 5 entries" in result.output

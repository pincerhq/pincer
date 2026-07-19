"""Tests for CLI commands using typer.testing.CliRunner."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    """--help exits 0 and output contains 'Pincer'."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Pincer" in result.output


def test_config_no_crash() -> None:
    """config command doesn't crash (may show error about missing keys)."""
    result = runner.invoke(app, ["config"])
    # Should not raise; may exit 0 or show config
    assert result.exit_code in (0, 1)
    assert "Configuration" in result.output or "Error" in result.output or "Provider" in result.output


def test_cost_shows_table() -> None:
    """cost command runs (may error but doesn't crash)."""
    result = runner.invoke(app, ["cost"])
    # May error if DB not initialized, but should not crash
    assert "Today" in result.output or "Total" in result.output or "Error" in result.output


def test_doctor_runs() -> None:
    """doctor command runs (exit code 0 or shows config table)."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Configuration" in result.output or "Check" in result.output


# ── patch-coverage: new lines from ms365-standalone-mcp branch ───────────────


def test_run_calls_setup_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() calls _setup_logging with the configured log level."""

    logged: list[str] = []
    monkeypatch.setattr("pincer.cli._setup_logging", lambda level: logged.append(level))

    async def _noop(settings):  # type: ignore[no-untyped-def]
        pass

    monkeypatch.setattr("pincer.cli._run_agent", _noop)

    runner.invoke(app, ["run"])

    assert logged, "_setup_logging was not called by run()"


@pytest.mark.asyncio
async def test_chat_loop_degrades_mcp_memory_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_chat_loop warns and closes MCPMemoryBackend when memory_backend='mcp'."""
    from unittest.mock import AsyncMock, MagicMock

    from pincer.memory.mcp import MCPMemoryBackend

    # Fake memory store that passes isinstance(obj, MCPMemoryBackend)
    fake_memory = AsyncMock()
    fake_memory.__class__ = MCPMemoryBackend

    # Minimal mock settings
    mock_settings = MagicMock()
    mock_settings.memory_enabled = True
    mock_settings.memory_backend = "mcp"
    mock_settings.default_provider.value = "anthropic"
    mock_settings.db_path = tmp_path / "test.db"
    mock_settings.daily_budget_usd = 100.0
    mock_settings.max_session_messages = 20
    mock_settings.agent_name = "TestAgent"
    mock_settings.log_level.value = "WARNING"

    mock_session = AsyncMock()
    mock_cost = AsyncMock()
    mock_llm = AsyncMock()
    mock_llm.close = AsyncMock()
    mock_agent = MagicMock()

    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)
    monkeypatch.setattr("pincer.cli._create_memory_backend", lambda _s: fake_memory)
    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm
    mock_router.get_summarizer.return_value = mock_llm

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=mock_session))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=mock_cost))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=mock_agent))

    # Exit the input loop immediately on first prompt
    import pincer.cli as _cli_mod

    monkeypatch.setattr(_cli_mod.console, "input", lambda _p: (_ for _ in ()).throw(EOFError()))

    from pincer.cli import _chat_loop

    await _chat_loop()

    # The MCPMemoryBackend degradation branch must have run
    fake_memory.close.assert_called_once()


@pytest.mark.asyncio
async def test_chat_loop_creates_summarizer_for_non_mcp_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_chat_loop creates Summarizer when memory_store is not an MCPMemoryBackend."""
    from unittest.mock import AsyncMock, MagicMock

    from pincer.memory.sqlite import SQLiteMemoryBackend

    # Fake memory that is NOT MCPMemoryBackend → hits the else branch
    fake_memory = AsyncMock()
    fake_memory.__class__ = SQLiteMemoryBackend

    mock_settings = MagicMock()
    mock_settings.memory_enabled = True
    mock_settings.memory_backend = "sqlite"
    mock_settings.default_provider.value = "anthropic"
    mock_settings.db_path = tmp_path / "test.db"
    mock_settings.daily_budget_usd = 100.0
    mock_settings.max_session_messages = 20
    mock_settings.agent_name = "TestAgent"
    mock_settings.log_level.value = "WARNING"
    mock_settings.summary_model = "test-model"
    mock_settings.summary_threshold = 10

    mock_llm = AsyncMock()
    mock_llm.close = AsyncMock()

    summarizer_calls: list[bool] = []

    def _mock_summarizer(**_kwargs):  # type: ignore[no-untyped-def]
        summarizer_calls.append(True)
        return MagicMock()

    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)
    monkeypatch.setattr("pincer.cli._create_memory_backend", lambda _s: fake_memory)
    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm
    mock_router.get_summarizer.return_value = mock_llm

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.memory.summarizer.Summarizer", _mock_summarizer)
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=MagicMock()))

    import pincer.cli as _cli_mod

    monkeypatch.setattr(_cli_mod.console, "input", lambda _p: (_ for _ in ()).throw(EOFError()))

    from pincer.cli import _chat_loop

    await _chat_loop()

    assert summarizer_calls, "Summarizer must be created for non-MCP memory backends"

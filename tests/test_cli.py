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


def test_skills_list_shows_header() -> None:
    """skills list output contains 'Installed Skills' or 'Name'."""
    result = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    assert "Installed Skills" in result.output or "Name" in result.output


def test_skills_create_scaffolds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """skills create scaffolds manifest.json and skill.py."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    result = runner.invoke(app, ["skills", "create", "testskill"])
    assert result.exit_code == 0
    assert (tmp_path / "skills" / "testskill" / "manifest.json").exists()
    assert (tmp_path / "skills" / "testskill" / "skill.py").exists()


def test_skills_create_fails_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """skills create fails when directory already exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "testskill").mkdir()
    result = runner.invoke(app, ["skills", "create", "testskill"])
    assert result.exit_code == 1
    assert "already exists" in result.output or "exists" in result.output.lower()


def test_skills_install_fails_nonexistent() -> None:
    """skills install /nonexistent fails."""
    result = runner.invoke(app, ["skills", "install", "/nonexistent"])
    assert result.exit_code == 1
    assert "Not a directory" in result.output or "directory" in result.output.lower()


def test_skills_install_fails_invalid(tmp_path: Path) -> None:
    """skills install fails for directory without manifest.json."""
    invalid_skill = tmp_path / "invalid_skill"
    invalid_skill.mkdir()
    # No manifest.json, no skill.py
    (invalid_skill / "random.txt").write_text("x")
    result = runner.invoke(app, ["skills", "install", str(invalid_skill)])
    assert result.exit_code == 1
    assert "Invalid skill" in result.output or "manifest" in result.output.lower()


def test_skills_scan_fails_nonexistent() -> None:
    """skills scan /nonexistent fails."""
    result = runner.invoke(app, ["skills", "scan", "/nonexistent"])
    assert result.exit_code == 1
    assert "Not a directory" in result.output or "directory" in result.output.lower()


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
    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=mock_session))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=mock_cost))
    monkeypatch.setattr("pincer.llm.anthropic_provider.AnthropicProvider", MagicMock(return_value=mock_llm))
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
    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.anthropic_provider.AnthropicProvider", MagicMock(return_value=mock_llm))
    monkeypatch.setattr("pincer.memory.summarizer.Summarizer", _mock_summarizer)
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=MagicMock()))

    import pincer.cli as _cli_mod

    monkeypatch.setattr(_cli_mod.console, "input", lambda _p: (_ for _ in ()).throw(EOFError()))

    from pincer.cli import _chat_loop

    await _chat_loop()

    assert summarizer_calls, "Summarizer must be created for non-MCP memory backends"

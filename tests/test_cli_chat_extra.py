"""Additional coverage for `pincer chat` — the settings-error path and the
interactive message loop (empty input, /clear, /cost, and a real message).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


def test_chat_command_reports_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> None:
        raise RuntimeError("bad config")

    monkeypatch.setattr("pincer.config.get_settings", _raise)

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert "Configuration error" in result.output


@pytest.mark.asyncio
async def test_chat_loop_processes_clear_cost_and_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock_settings = MagicMock()
    mock_settings.memory_enabled = False
    mock_settings.default_provider.value = "anthropic"
    mock_settings.db_path = tmp_path / "test.db"
    mock_settings.daily_budget_usd = 100.0
    mock_settings.max_session_messages = 20
    mock_settings.agent_name = "TestAgent"
    mock_settings.log_level.value = "WARNING"

    mock_session = AsyncMock()
    mock_session.get_or_create = AsyncMock(return_value="session-1")
    mock_session.clear = AsyncMock()

    mock_cost = AsyncMock()
    mock_cost.get_today_spend = AsyncMock(return_value=1.23)

    mock_llm = AsyncMock()
    mock_llm.close = AsyncMock()

    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm

    mock_response = MagicMock()
    mock_response.text = "Hello there"
    mock_response.cost_usd = 0.01
    mock_agent = MagicMock()
    mock_agent.handle_message = AsyncMock(return_value=mock_response)

    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)
    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=mock_session))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=mock_cost))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=mock_agent))

    import pincer.cli.chat as _cli_mod

    inputs = iter(["", "/clear", "/cost", "hello", "/quit"])
    monkeypatch.setattr(_cli_mod.console, "input", lambda _prompt: next(inputs))

    from pincer.cli.chat import _chat_loop

    await _chat_loop()

    mock_session.clear.assert_awaited_once()
    mock_cost.get_today_spend.assert_awaited_once()
    mock_agent.handle_message.assert_awaited_once_with(user_id="cli_user", channel="cli", text="hello")
    mock_llm.close.assert_awaited_once()

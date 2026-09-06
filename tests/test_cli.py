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
    monkeypatch.setattr("pincer.cli.run._setup_logging", lambda level: logged.append(level))

    async def _noop(settings):  # type: ignore[no-untyped-def]
        pass

    monkeypatch.setattr("pincer.cli.run._run_agent", _noop)

    runner.invoke(app, ["run"])

    assert logged, "_setup_logging was not called by run()"


def _mock_settings_for_run(monkeypatch: pytest.MonkeyPatch, *, task_broker: str) -> None:
    from unittest.mock import MagicMock

    mock_settings = MagicMock()
    mock_settings.log_level.value = "WARNING"
    mock_settings.telemetry_dsn = None
    mock_settings.task_broker = task_broker
    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)


def test_run_tasks_requires_redis_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pincer run tasks` exits 1 with a clear message when task_broker isn't redis."""
    _mock_settings_for_run(monkeypatch, task_broker="memory")

    result = runner.invoke(app, ["run", "tasks"])

    assert result.exit_code == 1
    assert "PINCER_TASK_BROKER=redis" in result.output


def test_run_tasks_dispatches_to_run_tasks_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pincer run tasks` calls _run_tasks_worker (not _run_agent) once the broker is redis."""
    _mock_settings_for_run(monkeypatch, task_broker="redis")

    called: list[object] = []

    async def _noop(settings):  # type: ignore[no-untyped-def]
        called.append(settings)

    monkeypatch.setattr("pincer.cli.run._run_tasks_worker", _noop)

    result = runner.invoke(app, ["run", "tasks"])

    assert called, "_run_tasks_worker was not called"
    assert result.exit_code == 0


def test_run_bad_component_rejected() -> None:
    """`pincer run <unknown>` is rejected by Typer's own choice validation."""
    result = runner.invoke(app, ["run", "badvalue"])
    assert result.exit_code == 2


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
    monkeypatch.setattr("pincer.cli.chat._create_memory_backend", lambda _s: fake_memory)
    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm
    mock_router.get_summarizer.return_value = mock_llm

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=mock_session))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=mock_cost))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=mock_agent))

    # Exit the input loop immediately on first prompt
    import pincer.cli.chat as _cli_mod

    monkeypatch.setattr(_cli_mod.console, "input", lambda _p: (_ for _ in ()).throw(EOFError()))

    from pincer.cli.chat import _chat_loop

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
    monkeypatch.setattr("pincer.cli.chat._create_memory_backend", lambda _s: fake_memory)
    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm
    mock_router.get_summarizer.return_value = mock_llm

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.memory.summarizer.Summarizer", _mock_summarizer)
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=MagicMock()))

    import pincer.cli.chat as _cli_mod

    monkeypatch.setattr(_cli_mod.console, "input", lambda _p: (_ for _ in ()).throw(EOFError()))

    from pincer.cli.chat import _chat_loop

    await _chat_loop()

    assert summarizer_calls, "Summarizer must be created for non-MCP memory backends"


# ── patch-coverage: _build_core / _register_channel_bound_tools ─────────────


def _mock_settings_for_build_core(tmp_path: Path):
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.db_path = tmp_path / "test.db"
    settings.max_session_messages = 20
    settings.daily_budget_usd = 100.0
    settings.audit_disabled = True
    settings.rate_messages_per_min = 20
    settings.rate_tools_per_min = 20
    settings.max_concurrent_llm = 2
    settings.memory_enabled = False
    settings.skills_dir = tmp_path / "skills"
    settings.skills_max_loaded_per_root = 50
    settings.skill_sandbox_disabled = False
    settings.data_dir = tmp_path
    return settings


@pytest.mark.asyncio
async def test_build_core_returns_core_components(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_build_core wires up the shared core and returns a CoreComponents with every field set."""
    from unittest.mock import AsyncMock, MagicMock

    from pincer.cli.run import CoreComponents, _build_core

    settings = _mock_settings_for_build_core(tmp_path)

    mock_llm = AsyncMock()
    mock_router = MagicMock()
    mock_router.get_llm.return_value = mock_llm
    mock_agent = MagicMock()

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.security.rate_limiter.get_rate_limiter", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.tools.bootstrap.register_default_tools", MagicMock(return_value={}))
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=mock_agent))
    monkeypatch.setattr("pincer.mcp.load_mcp_config", MagicMock(side_effect=RuntimeError("no config in test env")))

    core = await _build_core(settings)

    assert isinstance(core, CoreComponents)
    assert core.agent is mock_agent
    assert core.llm is mock_llm
    assert core.memory_store is None
    assert core.mcp_manager is None


@pytest.mark.asyncio
async def test_build_core_enables_audit_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from pincer.cli.run import _build_core

    settings = _mock_settings_for_build_core(tmp_path)
    settings.audit_disabled = False

    mock_router = MagicMock()
    mock_router.get_llm.return_value = AsyncMock()
    mock_audit_logger = MagicMock()

    monkeypatch.setattr("pincer.core.session.SessionManager", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr("pincer.security.rate_limiter.get_rate_limiter", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock(return_value=mock_router))
    monkeypatch.setattr("pincer.tools.bootstrap.register_default_tools", MagicMock(return_value={}))
    monkeypatch.setattr("pincer.core.agent.Agent", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("pincer.security.audit.get_audit_logger", AsyncMock(return_value=mock_audit_logger))
    monkeypatch.setattr("pincer.mcp.load_mcp_config", MagicMock(side_effect=RuntimeError("no config in test env")))

    core = await _build_core(settings)

    assert core.audit_logger is mock_audit_logger


def test_register_channel_bound_tools_send_file_and_send_image() -> None:
    """send_file/send_image close over channel_map and route through the right channel by name."""
    from unittest.mock import AsyncMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    fake_channel = AsyncMock()
    channel_map = {"telegram": fake_channel}
    _register_channel_bound_tools(tools, channel_map)

    assert {"send_file", "send_image"} <= set(tools.list_tools())


@pytest.mark.asyncio
async def test_send_file_handler_errors_without_channel(tmp_path: Path) -> None:
    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    _register_channel_bound_tools(tools, {})

    result = await tools.execute("send_file", {"path": str(tmp_path / "missing.txt")}, context={})
    assert "Error" in result


@pytest.mark.asyncio
async def test_send_file_handler_errors_when_file_exists_but_no_active_channel(tmp_path: Path) -> None:
    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    _register_channel_bound_tools(tools, {})

    file_path = tmp_path / "report.csv"
    file_path.write_text("a,b,c\n")

    result = await tools.execute("send_file", {"path": str(file_path)}, context={})
    assert "No active channel" in result


@pytest.mark.asyncio
async def test_send_file_handler_sends_existing_file(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    fake_channel = AsyncMock()
    channel_map = {"telegram": fake_channel}
    _register_channel_bound_tools(tools, channel_map)

    file_path = tmp_path / "report.csv"
    file_path.write_text("a,b,c\n")

    result = await tools.execute(
        "send_file",
        {"path": str(file_path), "caption": "here"},
        context={"user_id": "123", "channel": "telegram"},
    )

    fake_channel.send_file.assert_awaited_once_with("123", str(file_path), "here")
    assert "File sent" in result


@pytest.mark.asyncio
async def test_send_image_handler_errors_without_channel() -> None:
    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    _register_channel_bound_tools(tools, {})

    result = await tools.execute("send_image", {"url": "https://example.com/cat.png"}, context={})
    assert "Error" in result


@pytest.mark.asyncio
async def test_send_image_handler_sends_photo() -> None:
    from unittest.mock import AsyncMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    fake_channel = AsyncMock()
    channel_map = {"telegram": fake_channel}
    _register_channel_bound_tools(tools, channel_map)

    result = await tools.execute(
        "send_image",
        {"url": "https://example.com/cat.png", "caption": "cute"},
        context={"user_id": "123", "channel": "telegram"},
    )

    fake_channel.send_photo.assert_awaited_once_with("123", "https://example.com/cat.png", "cute")
    assert "Image sent" in result


@pytest.mark.asyncio
async def test_send_image_handler_sends_gif_via_send_animation() -> None:
    from unittest.mock import AsyncMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    fake_channel = AsyncMock()
    channel_map = {"telegram": fake_channel}
    _register_channel_bound_tools(tools, channel_map)

    result = await tools.execute(
        "send_image",
        {"url": "https://media.giphy.com/party.gif"},
        context={"user_id": "123", "channel": "telegram"},
    )

    fake_channel.send_animation.assert_awaited_once_with("123", "https://media.giphy.com/party.gif", "")
    assert "Image sent" in result


@pytest.mark.asyncio
async def test_send_image_handler_reports_error_on_send_failure() -> None:
    from unittest.mock import AsyncMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    tools = ToolRegistry()
    fake_channel = AsyncMock()
    fake_channel.send_photo.side_effect = RuntimeError("hotlink blocked")
    channel_map = {"telegram": fake_channel}
    _register_channel_bound_tools(tools, channel_map)

    result = await tools.execute(
        "send_image",
        {"url": "https://example.com/cat.png"},
        context={"user_id": "123", "channel": "telegram"},
    )

    assert "Error" in result
    assert "hotlink blocked" in result


@pytest.mark.asyncio
async def test_run_agent_wires_core_and_channel_bound_tools_before_channel_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_agent must build the core and register send_file/send_image before touching channels.

    Verifying the full startup/shutdown sequence end-to-end would mean mocking
    every subsystem it wires together (channel router, MCP, the repid task
    worker, signal handling) for little real assurance — instead this pins
    down the observable contract of its first few lines by forcing a
    controlled failure right after them.
    """
    from unittest.mock import AsyncMock, MagicMock

    from pincer.cli.run import CoreComponents, _run_agent

    mock_tools = MagicMock()
    core = CoreComponents(
        session_mgr=AsyncMock(),
        cost_tracker=AsyncMock(),
        audit_logger=None,
        rate_limiter=MagicMock(),
        llm_router=MagicMock(),
        llm=AsyncMock(),
        memory_store=None,
        summarizer=None,
        tools=mock_tools,
        skill_index=MagicMock(),
        mcp_manager=None,
        agent=MagicMock(),
    )

    registered_with: list[tuple[object, dict]] = []

    def _fake_register(tools, channel_map):
        registered_with.append((tools, channel_map))
        raise RuntimeError("stop here — rest of _run_agent is out of scope for this test")

    monkeypatch.setattr("pincer.cli.run._build_core", AsyncMock(return_value=core))
    monkeypatch.setattr("pincer.cli.run._register_channel_bound_tools", _fake_register)

    with pytest.raises(RuntimeError, match="stop here"):
        await _run_agent(MagicMock())

    assert registered_with == [(mock_tools, {})]

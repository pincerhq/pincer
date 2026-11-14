"""The entry points that serve traffic refuse to start with an unpriced model (issue #357)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from pincer.cli import app
from pincer.config import Settings
from pincer.config.pricing import TokenUsage

runner = CliRunner()

_RATES = "input = 1.0\ncache_write = 1.25\ncache_write_1h = 2.0\ncache_read = 0.1\noutput = 5.0\n"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        openai_api_key="",  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="claude-a",
        anthropic_model="",
        summary_model="claude-a",
        voice_enabled=False,
        voice_outbound_enabled=False,
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def pricing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setattr("pincer.config.pricing._config_dir", lambda: path)
    monkeypatch.chdir(path)  # `pincer doctor` reads the config of the directory it runs in
    settings = _settings(tmp_path)
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    return path


def _price(path: Path, *models: str) -> None:
    (path / "pincer.toml").write_text("".join(f'[pricing."{m}"]\n{_RATES}\n' for m in models), encoding="utf-8")


REFUSAL = (
    "Configuration error: No price for 1 model Pincer will call:\n"
    "  - claude-a: chat on anthropic, summary on anthropic\n"
    "Prices are matched by the exact model name. Add the block below to pincer.local.toml "
    "(or pincer.toml) in the directory Pincer runs from, fill in the rates, and start again:\n"
    "\n"
    '[pricing."claude-a"]\n'
    "input = nan           # USD per million tokens, fill in\n"
    "cache_write = nan     # USD per million tokens, fill in (5-minute cache write)\n"
    "cache_write_1h = nan  # USD per million tokens, fill in (1-hour cache write)\n"
    "cache_read = nan      # USD per million tokens, fill in\n"
    "output = nan          # USD per million tokens, fill in\n"
    'source = ""           # the pricing page and the date you read it\n'
)


@pytest.mark.parametrize("args", [["run"], ["run", "-d"], ["run", "--daemon-child"]])
def test_run_refuses_an_unpriced_model_before_starting_anything(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))
    monkeypatch.setattr("pincer.cli.run._setup_logging", lambda *a: started.append(a))
    _price(pricing_dir, "claude-other")

    result = runner.invoke(app, args, terminal_width=400)

    assert result.exit_code == 1
    assert result.output == REFUSAL
    assert started == []


def test_run_detach_starts_when_every_model_is_priced(pricing_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))
    _price(pricing_dir, "claude-a")

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 0, result.output
    assert len(started) == 1


def test_run_refuses_an_invalid_entry_naming_the_field(pricing_dir: Path) -> None:
    (pricing_dir / "pincer.toml").write_text('[pricing."claude-a"]\ninput = 1.0\n', encoding="utf-8")

    result = runner.invoke(app, ["run"], terminal_width=400)

    assert result.exit_code == 1
    assert result.output == (
        "Configuration error: Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n"
        '  - pricing."claude-a": missing cache_write, cache_write_1h, cache_read, output\n'
    )


def test_chat_refuses_an_unpriced_model_before_building_the_agent(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    monkeypatch.setattr("pincer.core.session.SessionManager.initialize", lambda self: built.append(self))
    _price(pricing_dir, "claude-other")

    result = runner.invoke(app, ["chat"], terminal_width=400)

    assert result.exit_code == 1
    assert result.output == REFUSAL
    assert built == []


def _standalone_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, list[int]]:
    from unittest.mock import MagicMock

    from pincer.api.server import create_app

    monkeypatch.chdir(tmp_path)
    builds: list[int] = []

    async def fake_build() -> Any:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr("pincer.api._deps.build_agent_from_settings", fake_build)
    return create_app(), builds


def test_the_standalone_api_refuses_to_start_with_an_unpriced_model(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    _price(pricing_dir, "claude-other")
    app, builds = _standalone_app(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as raised, TestClient(app):
        pass

    assert str(raised.value) == REFUSAL.removeprefix("Configuration error: ")
    assert builds == []


def test_the_standalone_api_starts_when_every_model_is_priced(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    _price(pricing_dir, "claude-a")
    app, builds = _standalone_app(monkeypatch, tmp_path)

    with TestClient(app):
        pass

    assert builds == [1]


async def test_the_api_agent_prices_calls_from_the_pricing_config(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from pincer.api import _deps

    _price(pricing_dir, "claude-a")
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock())
    agent = await _deps.build_agent_from_settings()
    try:
        assert await agent._costs.record(
            "anthropic", "claude-a", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        ) == pytest.approx(6.0)
    finally:
        await agent._sessions.close()
        await agent._costs.close()


class _Stop(Exception):
    """Ends a startup right after the cost tracker is built."""


def _capture_cost_tracker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    from unittest.mock import AsyncMock

    built: list[tuple[Any, ...]] = []

    def fake_tracker(*args: Any) -> Any:
        built.append(args)
        return AsyncMock()

    monkeypatch.setattr("pincer.llm.cost_tracker.CostTracker", fake_tracker)
    monkeypatch.setattr("pincer.core.session.SessionManager.initialize", AsyncMock())
    monkeypatch.setattr("pincer.llm.router.LLMRouter", lambda *a: (_ for _ in ()).throw(_Stop()))
    return built


async def test_run_gives_its_cost_tracker_the_configured_prices(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from pincer.cli.run import _build_core
    from pincer.config import get_settings

    _price(pricing_dir, "claude-a")
    built = _capture_cost_tracker(monkeypatch)
    monkeypatch.setattr("pincer.db.engine.init_database", AsyncMock())
    monkeypatch.setattr("pincer.security.audit.get_audit_logger", AsyncMock())
    monkeypatch.setattr("pincer.security.rate_limiter.get_rate_limiter", MagicMock())
    monkeypatch.setattr("pincer.cli.run._create_memory_backend", MagicMock(side_effect=_Stop()))

    with pytest.raises(_Stop):
        await _build_core(get_settings())

    assert len(built) == 1
    assert sorted(built[0][2].entries) == ["claude-a"]


async def test_chat_gives_its_cost_tracker_the_configured_prices(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pincer.cli.chat import _chat_loop

    _price(pricing_dir, "claude-a")
    built = _capture_cost_tracker(monkeypatch)

    with pytest.raises(_Stop):
        await _chat_loop()

    assert len(built) == 1
    assert sorted(built[0][2].entries) == ["claude-a"]


def test_run_following_a_running_daemon_is_not_refused(pricing_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attaching to a daemon calls no model: only a start is refused (the daemon itself checked)."""
    import sys
    from unittest.mock import MagicMock

    from pincer.daemon import DaemonState

    if sys.platform == "win32":
        pytest.skip("daemon mode is POSIX-only")
    attached: list[Any] = []
    monkeypatch.setattr("pincer.daemon.read_state", lambda data_dir: MagicMock(spec=DaemonState))
    monkeypatch.setattr("pincer.cli.run.attach_daemon", lambda *a: attached.append(a))
    _price(pricing_dir, "claude-other")

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert len(attached) == 1


def test_doctor_prints_the_block_to_paste_as_text_not_markup(pricing_dir: Path) -> None:
    """`[pricing."<model>"]` reads as a rich style tag; the doctor table must print it as is."""
    _price(pricing_dir, "claude-other")

    result = runner.invoke(app, ["doctor"], terminal_width=400)

    assert '[pricing."claude-a"]' in result.output


@pytest.mark.parametrize("multiplier", ["0", "-1"])
def test_run_refuses_an_invalid_provider_multiplier(
    pricing_dir: Path, monkeypatch: pytest.MonkeyPatch, multiplier: str
) -> None:
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))
    _price(pricing_dir, "claude-a")
    (pricing_dir / "pincer.local.toml").write_text(
        f'[pricing.provider."anthropic"]\nmultiplier = {multiplier}\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "-d"], terminal_width=400)

    assert result.exit_code == 1
    assert result.output == (
        "Configuration error: Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n"
        f'  - pricing.provider."anthropic": multiplier must be a number > 0, got {multiplier}\n'
    )
    assert started == []


def test_run_starts_with_a_valid_provider_multiplier(pricing_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))
    _price(pricing_dir, "claude-a")
    (pricing_dir / "pincer.local.toml").write_text(
        '[pricing.provider."anthropic"]\nmultiplier = 1.1\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 0, result.output
    assert len(started) == 1


# ── gpt-6 effort on Chat Completions (slice 7): the same refusal, the same entry points ──
# The endpoint is kept on Chat Completions here; on the Responses API there is nothing to refuse (slice 8).

GPT6_REFUSAL = (
    "Configuration error: 1 gpt-6 setting won't work with tools:\n"
    "  - chat on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high.\n"
    "    Fix: unset PINCER_REASONING_EFFORT; Pincer then turns reasoning off for this model's tool requests.\n"
    "Why: Pincer talks to OpenAI models through Chat Completions, which lets gpt-6 models use tools only "
    "with reasoning off. The summary model uses no tools, so PINCER_SUMMARY_REASONING_EFFORT still applies to it.\n"
    "To use reasoning with tools: add the endpoint's name to PINCER_RESPONSES_API_PROVIDERS if it serves "
    "/v1/responses (openai, azure and azure-openai already use it).\n"
)


@pytest.fixture
def gpt6_dir(pricing_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`pricing_dir` with terra as the openai chat and summary model and PINCER_REASONING_EFFORT=high."""
    settings = _settings(tmp_path).model_copy(
        update={
            "default_provider": "openai",
            "anthropic_api_key": SecretStr(""),
            "openai_api_key": SecretStr("sk-test"),
            "openai_model": "gpt-6.1-terra",
            "summary_model": "",
            "reasoning_effort": "high",
            "thinking": "",
            "responses_api_providers": [],  # on Chat Completions
        }
    )
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    _price(pricing_dir, "gpt-6.1-terra")
    return pricing_dir


@pytest.mark.parametrize("args", [["run"], ["run", "-d"], ["run", "--daemon-child"], ["chat"]])
def test_run_and_chat_refuse_a_gpt_6_chat_effort(
    gpt6_dir: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))
    monkeypatch.setattr("pincer.cli.run._setup_logging", lambda *a: started.append(a))
    monkeypatch.setattr("pincer.core.session.SessionManager.initialize", lambda self: started.append(self))

    result = runner.invoke(app, args, terminal_width=400)

    assert result.exit_code == 1
    assert result.output == GPT6_REFUSAL
    assert started == []


def test_the_standalone_api_refuses_a_gpt_6_chat_effort(
    gpt6_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    app_, builds = _standalone_app(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as raised, TestClient(app_):
        pass

    assert str(raised.value) == GPT6_REFUSAL.removeprefix("Configuration error: ").rstrip("\n")
    assert builds == []


def test_run_starts_with_a_gpt_6_chat_effort_on_the_responses_api(
    gpt6_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same settings as the refusal above, with `openai` on the Responses API (the default)."""
    settings = _settings(tmp_path).model_copy(
        update={
            "default_provider": "openai",
            "anthropic_api_key": SecretStr(""),
            "openai_api_key": SecretStr("sk-test"),
            "openai_model": "gpt-6.1-terra",
            "summary_model": "",
            "reasoning_effort": "high",
            "thinking": "",
            "responses_api_providers": ["openai", "azure", "azure-openai"],
        }
    )
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    started: list[Any] = []
    monkeypatch.setattr("pincer.cli.run.start_daemon", lambda *a: started.append(a))

    result = runner.invoke(app, ["run", "-d"], terminal_width=400)

    assert result.exit_code == 0, result.output
    assert len(started) == 1

"""Telemetry provider selection from settings, and `pincer run` refusing a bad telemetry config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from pincer_telemetry import PostHogProvider, TelemetryConfigError, UptraceProvider
from pydantic import SecretStr
from typer.testing import CliRunner

from pincer.cli import app
from pincer.observability.telemetry import (
    TelemetryState,
    inspect_telemetry,
    legacy_telemetry_dsn,
    resolve_telemetry_provider,
    telemetry_configured,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_UPTRACE = "https://s3cr3t@api.uptrace.dev?grpc=4317"


def _settings(uptrace: str | None = None, posthog: str | None = None) -> MagicMock:
    settings = MagicMock()
    settings.telemetry_uptrace_dsn = SecretStr(uptrace) if uptrace is not None else None
    settings.telemetry_posthog_dsn = SecretStr(posthog) if posthog is not None else None
    return settings


# ── resolve_telemetry_provider ───────────────────────────


def test_none_configured() -> None:
    settings = _settings()
    assert resolve_telemetry_provider(settings) is None
    assert not telemetry_configured(settings)


def test_empty_values_count_as_unset() -> None:
    settings = _settings(uptrace="", posthog="  ")
    assert resolve_telemetry_provider(settings) is None
    assert not telemetry_configured(settings)


def test_uptrace_dsn() -> None:
    provider = resolve_telemetry_provider(_settings(uptrace=_UPTRACE))
    assert isinstance(provider, UptraceProvider)
    assert provider.dsn == _UPTRACE


@pytest.mark.parametrize("region", ["us", "eu"])
def test_posthog_dsn(region: str) -> None:
    provider = resolve_telemetry_provider(_settings(posthog=f"https://phc_key@{region}.i.posthog.com"))
    assert isinstance(provider, PostHogProvider)
    assert provider.api_key == "phc_key"
    assert provider.host == f"https://{region}.i.posthog.com"


def test_both_providers_raise() -> None:
    settings = _settings(uptrace=_UPTRACE, posthog="https://phc_key@us.i.posthog.com")
    assert telemetry_configured(settings)
    with pytest.raises(TelemetryConfigError) as exc:
        resolve_telemetry_provider(settings)
    message = str(exc.value)
    assert "PINCER_TELEMETRY_UPTRACE_DSN" in message
    assert "PINCER_TELEMETRY_POSTHOG_DSN" in message
    assert "s3cr3t" not in message
    assert "phc_key" not in message


def test_posthog_dsn_without_key_raises() -> None:
    with pytest.raises(TelemetryConfigError, match="PINCER_TELEMETRY_POSTHOG_DSN"):
        resolve_telemetry_provider(_settings(posthog="https://us.i.posthog.com"))


def test_settings_read_the_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    monkeypatch.setenv("PINCER_TELEMETRY_POSTHOG_DSN", "https://phc_key@eu.i.posthog.com")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.telemetry_uptrace_dsn is None
    assert isinstance(resolve_telemetry_provider(settings), PostHogProvider)


# ── legacy_telemetry_dsn ─────────────────────────────────


def test_legacy_not_set(tmp_path: Path) -> None:
    assert legacy_telemetry_dsn(tmp_path) is None


def test_legacy_in_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_TELEMETRY_DSN", _UPTRACE)
    location = legacy_telemetry_dsn(tmp_path)
    assert location is not None
    assert str(location) == "PINCER_TELEMETRY_DSN in the environment"
    assert "s3cr3t" not in repr(location)


def test_legacy_empty_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_TELEMETRY_DSN", "")
    assert legacy_telemetry_dsn(tmp_path) is None


def test_legacy_in_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", ("../.env", ".env"))
    (tmp_path / "app").mkdir()
    (tmp_path / ".env").write_text(f"PINCER_TELEMETRY_DSN={_UPTRACE}\n")
    location = legacy_telemetry_dsn(tmp_path / "app")
    assert location is not None
    assert location.source.endswith(".env")


def test_legacy_dotenv_follows_settings(tmp_path: Path) -> None:
    """Only the .env files Settings reads count (the suite turns them off)."""
    (tmp_path / ".env").write_text(f"PINCER_TELEMETRY_DSN={_UPTRACE}\n")
    assert legacy_telemetry_dsn(tmp_path) is None


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        (f'telemetry_dsn = "{_UPTRACE}"\n', "telemetry_dsn"),
        (f'[telemetry]\ndsn = "{_UPTRACE}"\n', "[telemetry] dsn"),
    ],
)
def test_legacy_in_pincer_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, key: str) -> None:
    monkeypatch.setattr("pincer.config.telemetry.TELEMETRY_TOML_FILES", ("pincer.toml", "pincer.local.toml"))
    (tmp_path / "pincer.local.toml").write_text(toml)
    location = legacy_telemetry_dsn(tmp_path)
    assert location is not None
    assert location.key == key
    assert "s3cr3t" not in str(location)


# ── pincer.toml [telemetry] ──────────────────────────────


@pytest.fixture
def toml_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("pincer.config.telemetry.TELEMETRY_TOML_FILES", ("pincer.toml", "pincer.local.toml"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_settings_read_the_toml_section(toml_dir: Path) -> None:
    from pincer.config.main import Settings

    (toml_dir / "pincer.toml").write_text('[telemetry]\nposthog_dsn = "https://phc_key@eu.i.posthog.com"\n')
    settings = Settings()  # type: ignore[call-arg]
    provider = resolve_telemetry_provider(settings)
    assert isinstance(provider, PostHogProvider)
    assert provider.host == "https://eu.i.posthog.com"


def test_local_toml_overrides_and_env_wins(toml_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    (toml_dir / "pincer.toml").write_text('[telemetry]\nuptrace_dsn = "https://a@shipped"\nposthog_dsn = ""\n')
    (toml_dir / "pincer.local.toml").write_text('[telemetry]\nuptrace_dsn = "https://b@local"\n')
    settings = Settings()  # type: ignore[call-arg]
    assert settings.telemetry_uptrace_dsn is not None
    assert settings.telemetry_uptrace_dsn.get_secret_value() == "https://b@local"
    assert settings.telemetry_posthog_dsn is None  # empty counts as unset

    monkeypatch.setenv("PINCER_TELEMETRY_UPTRACE_DSN", "https://c@env")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.telemetry_uptrace_dsn is not None
    assert settings.telemetry_uptrace_dsn.get_secret_value() == "https://c@env"


def test_toml_and_env_providers_conflict(toml_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    (toml_dir / "pincer.toml").write_text(f'[telemetry]\nuptrace_dsn = "{_UPTRACE}"\n')
    monkeypatch.setenv("PINCER_TELEMETRY_POSTHOG_DSN", "https://phc_key@us.i.posthog.com")
    with pytest.raises(TelemetryConfigError):
        resolve_telemetry_provider(Settings())  # type: ignore[call-arg]


def test_unreadable_toml_does_not_break_settings(toml_dir: Path) -> None:
    from pincer.config.main import Settings

    (toml_dir / "pincer.toml").write_text("[telemetry\n")
    assert Settings().telemetry_uptrace_dsn is None  # type: ignore[call-arg]


# ── inspect_telemetry ────────────────────────────────────


@pytest.mark.parametrize(
    ("dsns", "state"),
    [
        ({}, TelemetryState.OFF),
        ({"uptrace": _UPTRACE}, TelemetryState.ENABLED),
        ({"posthog": "https://us.i.posthog.com"}, TelemetryState.INVALID),
        ({"uptrace": _UPTRACE, "posthog": "https://phc_key@us.i.posthog.com"}, TelemetryState.INVALID),
    ],
)
def test_inspect_states(tmp_path: Path, dsns: dict[str, str], state: TelemetryState) -> None:
    assert inspect_telemetry(_settings(**dsns), tmp_path).state is state


def test_inspect_legacy_comes_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_TELEMETRY_DSN", _UPTRACE)
    setup = inspect_telemetry(_settings(uptrace=_UPTRACE, posthog="https://phc_key@us.i.posthog.com"), tmp_path)
    assert setup.state is TelemetryState.LEGACY
    assert "PINCER_TELEMETRY_UPTRACE_DSN" in setup.message


def test_inspect_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pincer_telemetry", None)
    assert inspect_telemetry(_settings(uptrace=_UPTRACE), tmp_path).state is TelemetryState.NOT_INSTALLED
    assert inspect_telemetry(_settings(), tmp_path).state is TelemetryState.OFF


def test_unknown_agent_keys_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    monkeypatch.setenv("PINCER_TELEMETRY_TYPO_DSN", _UPTRACE)
    assert inspect_telemetry(Settings(), tmp_path).state is TelemetryState.OFF  # type: ignore[call-arg]


# ── pincer run ───────────────────────────────────────────


def _mock_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **dsns: str) -> dict[str, MagicMock]:
    """Settings for `pincer run`, with every step after the telemetry checks mocked to record it ran."""
    settings = _settings(**dsns)
    settings.data_dir = tmp_path
    settings.log_level.value = "WARNING"
    settings.daily_budget_usd = 5.0
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    steps = {
        "start_daemon": MagicMock(),
        "read_state": MagicMock(return_value=None),
        "refuse_unpriced_models": MagicMock(),
        "_setup_logging": MagicMock(),
        "_run_agent": AsyncMock(),
    }
    monkeypatch.setattr("pincer.cli.run.start_daemon", steps["start_daemon"])
    monkeypatch.setattr("pincer.daemon.read_state", steps["read_state"])
    monkeypatch.setattr("pincer.cli.run.refuse_unpriced_models", steps["refuse_unpriced_models"])
    monkeypatch.setattr("pincer.cli.run._setup_logging", steps["_setup_logging"])
    monkeypatch.setattr("pincer.cli.run._run_agent", steps["_run_agent"])
    return steps


def _assert_nothing_started(steps: dict[str, MagicMock]) -> None:
    for name, step in steps.items():
        assert not step.called, f"{name} ran before the telemetry refusal"


@pytest.mark.parametrize("args", [["run"], ["run", "-d"]])
def test_run_exits_on_two_providers_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    steps = _mock_run(monkeypatch, tmp_path, uptrace=_UPTRACE, posthog="https://phc_key@us.i.posthog.com")

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.output.startswith("Telemetry configuration error:")
    assert "PINCER_TELEMETRY_POSTHOG_DSN" in result.output
    assert "s3cr3t" not in result.output
    _assert_nothing_started(steps)


def test_run_exits_on_posthog_dsn_without_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    steps = _mock_run(monkeypatch, tmp_path, posthog="https://us.i.posthog.com")

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1
    assert "API key" in result.output
    _assert_nothing_started(steps)


@pytest.mark.parametrize("args", [["run"], ["run", "-d"]])
def test_run_exits_on_legacy_variable_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    steps = _mock_run(monkeypatch, tmp_path)
    monkeypatch.setenv("PINCER_TELEMETRY_DSN", _UPTRACE)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.output.startswith("Configuration error:")
    assert "PINCER_TELEMETRY_UPTRACE_DSN" in result.output
    assert "s3cr3t" not in result.output
    _assert_nothing_started(steps)


def test_run_starts_without_telemetry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    init = MagicMock()
    monkeypatch.setattr("pincer_telemetry.init", init)
    steps = _mock_run(monkeypatch, tmp_path)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert steps["_run_agent"].called
    assert not init.called
    assert "Telemetry" not in result.output

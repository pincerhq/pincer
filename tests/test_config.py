"""Tests for configuration system."""

from pathlib import Path

import pytest

from pincer.config import LLMProvider, Settings


def test_settings_load_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MAX_TOKENS", "8192")
    s = Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
    )
    assert s.default_provider == LLMProvider.ANTHROPIC
    assert s.anthropic_api_key.get_secret_value() == "sk-test"
    assert s.max_tokens == 8192


def test_settings_fallback_to_openai(tmp_path: Path) -> None:
    s = Settings(
        anthropic_api_key="",  # type: ignore[arg-type]
        openai_api_key="sk-openai-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
        default_provider=LLMProvider.ANTHROPIC,
    )
    assert s.default_provider == LLMProvider.OPENAI


def test_settings_no_keys_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="At least one LLM API key"):
        Settings(
            anthropic_api_key="",  # type: ignore[arg-type]
            openai_api_key="",  # type: ignore[arg-type]
            grok_api_key="",  # type: ignore[arg-type]
            data_dir=tmp_path,
        )


def test_parse_allowed_users() -> None:
    result = Settings.parse_allowed_users("123,456,789")
    assert result == [123, 456, 789]


def test_parse_allowed_users_empty() -> None:
    result = Settings.parse_allowed_users("")
    assert result == []


def test_data_dir_created(tmp_path: Path) -> None:
    s = Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path / "test-pincer",
    )
    s.ensure_dirs()
    assert (tmp_path / "test-pincer").exists()
    assert (tmp_path / "test-pincer" / "logs").exists()


# ── Regression: .env-only vars are visible through Settings ──────────────────


def test_settings_loads_var_from_dotenv_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root-cause regression for #117.

    A PINCER_* var set only in .env (not exported to shell) must be visible
    through Settings. os.environ.get() would return "" here; Settings must not.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("PINCER_DASHBOARD_TOKEN=secret-from-dotenv\nPINCER_ANTHROPIC_API_KEY=sk-test\n")

    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)

    s = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert s.dashboard_token.get_secret_value() == "secret-from-dotenv"


# ── Domain class imports ──────────────────────────────────────────────────────


def test_domain_classes_importable() -> None:
    from pincer.config import LLMSettings, ChannelSettings, ToolSettings, APISettings, CoreSettings  # noqa: F401


def test_llm_settings_zero_fields() -> None:
    from pincer.config import LLMSettings
    s = LLMSettings()
    assert s.default_model != ""


def test_channel_settings_zero_fields() -> None:
    from pincer.config import ChannelSettings
    s = ChannelSettings()
    assert s.telegram_bot_token.get_secret_value() == ""


def test_tool_settings_zero_fields() -> None:
    from pincer.config import ToolSettings
    s = ToolSettings()
    assert s.shell_enabled is True


def test_api_settings_zero_fields() -> None:
    from pincer.config import APISettings
    s = APISettings()
    assert s.dashboard_port == 8080


def test_core_settings_zero_fields() -> None:
    from pincer.config import CoreSettings
    s = CoreSettings()
    assert s.log_level is not None


# ── Backward-compat public API ────────────────────────────────────────────────


def test_public_api_importable() -> None:
    from pincer.config import (  # noqa: F401
        get_settings,
        get_settings_relaxed,
        Settings,
        LLMProvider,
        LogLevel,
    )


# ── get_settings_relaxed works without LLM key ───────────────────────────────


def test_get_settings_relaxed_no_llm_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)  # no .env file here — isolates from project root
    monkeypatch.delenv("PINCER_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PINCER_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PINCER_GROK_API_KEY", raising=False)

    get_settings_relaxed.cache_clear()
    try:
        s = get_settings_relaxed()
        assert s.anthropic_api_key.get_secret_value() == ""
    finally:
        get_settings_relaxed.cache_clear()


# ── Validators work after moving to domain classes ────────────────────────────


def test_parse_slack_allowlist_comma_string() -> None:
    result = Settings.parse_slack_allowlist("U123,U456, U789")
    assert result == ["U123", "U456", "U789"]


def test_parse_slack_allowlist_empty() -> None:
    result = Settings.parse_slack_allowlist("")
    assert result == []


def test_parse_slack_allowlist_list_passthrough() -> None:
    result = Settings.parse_slack_allowlist(["U123", "U456"])
    assert result == ["U123", "U456"]

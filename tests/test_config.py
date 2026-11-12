"""Tests for configuration system."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pincer.config import LLMProvider, Settings, TaskSettings


def test_settings_load_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MAX_TOKENS", "8192")
    monkeypatch.setenv("PINCER_DEFAULT_PROVIDER", "anthropic")
    s = Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
    )
    assert s.default_provider == LLMProvider.ANTHROPIC
    assert s.anthropic_api_key.get_secret_value() == "sk-test"
    assert s.max_tokens == 8192


def test_settings_accepts_openai_only(tmp_path: Path) -> None:
    s = Settings(
        anthropic_api_key="",  # type: ignore[arg-type]
        openai_api_key="sk-openai-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
        default_provider="openai",
    )
    assert s.default_provider == "openai"
    assert s.openai_api_key.get_secret_value() == "sk-openai-test"


def test_settings_no_providers_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="At least one LLM provider"):
        Settings(
            default_provider="anthropic",
            anthropic_api_key="",  # type: ignore[arg-type]
            openai_api_key="",  # type: ignore[arg-type]
            openai_compatible_base_url="",
            anthropic_compatible_base_url="",
            data_dir=tmp_path,
            _env_file=None,  # hermetic: ignore the developer's .env  # type: ignore[call-arg]
        )


def test_compatible_base_url_satisfies_validation(tmp_path: Path) -> None:
    # e.g. Ollama: no API key, just a compatible base URL.
    s = Settings(
        default_provider="ollama",
        anthropic_api_key="",  # type: ignore[arg-type]
        openai_api_key="",  # type: ignore[arg-type]
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        data_dir=tmp_path,
    )
    assert s.default_provider == "ollama"


def test_fallback_providers_from_env_comma_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: PINCER_FALLBACK_PROVIDERS must parse a comma string from the
    # environment (pydantic-settings would otherwise try to JSON-decode the list).
    monkeypatch.setenv("PINCER_FALLBACK_PROVIDERS", "openai,grok")
    s = Settings(anthropic_api_key="sk-test", data_dir=tmp_path)  # type: ignore[arg-type]
    assert s.fallback_providers == ["openai", "grok"]


def test_parse_fallback_providers_comma_string() -> None:
    assert Settings.parse_fallback_providers("openai, grok ,my-claude") == ["openai", "grok", "my-claude"]


def test_parse_fallback_providers_empty() -> None:
    assert Settings.parse_fallback_providers("") == []


def test_parse_fallback_providers_list_passthrough() -> None:
    assert Settings.parse_fallback_providers(["openai", "grok"]) == ["openai", "grok"]


def test_guests_allowed_defaults_false(tmp_path: Path) -> None:
    s = Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path / "guests-allowed-defaults",
    )
    assert s.telegram_guests_allowed is False
    assert s.whatsapp_guests_allowed is False
    assert s.signal_guests_allowed is False


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
    secret = "secret-from-dotenv-" + "x" * 32
    env_file = tmp_path / ".env"
    env_file.write_text(f"PINCER_JWT_SECRET={secret}\nPINCER_ANTHROPIC_API_KEY=sk-test\n")

    monkeypatch.delenv("PINCER_JWT_SECRET", raising=False)

    s = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert s.jwt_secret.get_secret_value() == secret


def test_auth_is_required_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINCER_AUTH_DISABLED", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.auth_disabled is False
    assert s.jwt_secret.get_secret_value() == ""
    assert (s.jwt_access_ttl_seconds, s.jwt_refresh_ttl_seconds) == (1800, 604800)
    # The shared tokens are gone, not merely defaulted.
    assert not hasattr(s, "dashboard_token")
    assert not hasattr(s, "web_chat_token")


def test_a_short_jwt_secret_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_JWT_SECRET", "too-short")
    with pytest.raises(ValueError, match="at least 32 characters"):
        Settings()  # type: ignore[call-arg]


# ── Domain class imports ──────────────────────────────────────────────────────


def test_domain_classes_importable() -> None:
    from pincer.config import APISettings, ChannelSettings, CoreSettings, LLMSettings, ToolSettings  # noqa: F401


def test_llm_settings_zero_fields() -> None:
    from pincer.config import LLMSettings

    s = LLMSettings()
    assert s.default_model != ""


def test_channel_settings_zero_fields() -> None:
    from pincer.config import ChannelSettings

    s = ChannelSettings()
    assert s.telegram_bot_token.get_secret_value() == ""


# ── TaskSettings: redis broker requires a broker URL ──────────────────────────


def test_task_broker_redis_requires_url() -> None:
    with pytest.raises(ValidationError, match="task_broker_url is required"):
        TaskSettings(task_broker="redis")


def test_task_broker_memory_allows_empty_url() -> None:
    s = TaskSettings(task_broker="memory")
    assert s.task_broker_url == ""


def test_task_broker_redis_with_url_is_valid() -> None:
    s = TaskSettings(task_broker="redis", task_broker_url="redis://localhost:6379/0")
    assert s.task_broker_url == "redis://localhost:6379/0"


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
        LLMProvider,
        LogLevel,
        Settings,
        get_settings,
        get_settings_relaxed,
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


def test_voice_max_response_tokens_defaults_to_1024(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Room for thinking by default (issue #345). Immune to the environment and to a developer's .env."""
    monkeypatch.delenv("PINCER_VOICE_MAX_RESPONSE_TOKENS", raising=False)
    s = Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert s.voice_max_response_tokens == 1024


@pytest.mark.parametrize(("value", "accepted"), [(31, False), (32, True), (128_000, True), (128_001, False)])
def test_voice_max_response_tokens_bounds(tmp_path: Path, value: int, accepted: bool) -> None:
    def build() -> Settings:
        return Settings(
            anthropic_api_key="sk-test",  # type: ignore[arg-type]
            data_dir=tmp_path,
            voice_max_response_tokens=value,
            _env_file=None,  # type: ignore[call-arg]
        )

    if accepted:
        assert build().voice_max_response_tokens == value
    else:
        with pytest.raises(ValidationError, match="voice_max_response_tokens"):
            build()


def _settings_from_env(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
        data_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )


def test_effort_and_thinking_default_to_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset sends nothing, so the model keeps its own default (issue #345)."""
    monkeypatch.delenv("PINCER_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PINCER_THINKING", raising=False)
    s = _settings_from_env(tmp_path)
    assert (s.reasoning_effort, s.thinking) == ("", "")


@pytest.mark.parametrize(
    ("env", "value"),
    [
        *(("PINCER_REASONING_EFFORT", v) for v in ("", "low", "medium", "high", "xhigh", "max")),
        *(("PINCER_THINKING", v) for v in ("", "adaptive", "disabled")),
        *(("PINCER_VOICE_REASONING_EFFORT", v) for v in ("", "low", "medium", "high", "xhigh", "max")),
        *(("PINCER_VOICE_THINKING", v) for v in ("", "adaptive", "disabled")),
        *(("PINCER_SUMMARY_REASONING_EFFORT", v) for v in ("", "low", "medium", "high", "xhigh", "max")),
        *(("PINCER_SUMMARY_THINKING", v) for v in ("", "adaptive", "disabled")),
    ],
)
def test_effort_and_thinking_accept_their_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, value: str
) -> None:
    monkeypatch.setenv(env, value)
    s = _settings_from_env(tmp_path)
    assert getattr(s, env.removeprefix("PINCER_").lower()) == value


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("PINCER_REASONING_EFFORT", "minimal"),  # an OpenAI level, not a setting value
        ("PINCER_REASONING_EFFORT", "LOW"),
        ("PINCER_THINKING", "enabled"),
        ("PINCER_THINKING", "on"),
        ("PINCER_VOICE_THINKING", "enabled"),
        ("PINCER_VOICE_THINKING", "on"),
        ("PINCER_SUMMARY_REASONING_EFFORT", "minimal"),
        ("PINCER_SUMMARY_REASONING_EFFORT", "LOW"),
        ("PINCER_SUMMARY_THINKING", "enabled"),
        ("PINCER_SUMMARY_THINKING", "on"),
    ],
)
def test_effort_and_thinking_reject_other_values_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, value: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError, match=env.removeprefix("PINCER_").lower()):
        _settings_from_env(tmp_path)


def test_voice_thinking_defaults_to_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset inherits PINCER_THINKING (issue #345)."""
    monkeypatch.delenv("PINCER_VOICE_THINKING", raising=False)
    assert _settings_from_env(tmp_path).voice_thinking == ""


def test_summary_effort_and_thinking_default_to_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset inherits PINCER_REASONING_EFFORT / PINCER_THINKING (issue #345)."""
    monkeypatch.delenv("PINCER_SUMMARY_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PINCER_SUMMARY_THINKING", raising=False)
    s = _settings_from_env(tmp_path)
    assert (s.summary_reasoning_effort, s.summary_thinking) == ("", "")


# ── WhatsApp session store / account ──────────────────────────────


def _wa_settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(anthropic_api_key="sk-test", data_dir=tmp_path, **kw)  # type: ignore[arg-type]


def test_whatsapp_session_defaults_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINCER_WHATSAPP_SESSION_DB", raising=False)
    monkeypatch.delenv("PINCER_WHATSAPP_JID", raising=False)
    s = _wa_settings(tmp_path, _env_file=None)
    assert s.whatsapp_session_db.get_secret_value() == ""
    assert s.whatsapp_jid == ""


@pytest.mark.parametrize("url", ["postgres://u:p@h/db?search_path=wa", "postgresql://u:p@h/db"])
def test_whatsapp_session_db_accepts_postgres(tmp_path: Path, url: str) -> None:
    assert _wa_settings(tmp_path, whatsapp_session_db=url).whatsapp_session_db.get_secret_value() == url


@pytest.mark.parametrize("url", ["sqlite:///data/wa.db", "/data/pincer-wa.db", "mysql://u:s3cret@h/db"])
def test_whatsapp_session_db_rejects_other_schemes(tmp_path: Path, url: str) -> None:
    from pincer.exceptions import ConfigError

    with pytest.raises((ConfigError, ValidationError), match="PINCER_WHATSAPP_SESSION_DB") as exc:
        _wa_settings(tmp_path, whatsapp_session_db=url)
    assert "s3cret" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+49 151 2345-678", "491512345678"), ("491512345678", "491512345678"), ("", "")],
)
def test_whatsapp_jid_normalised(tmp_path: Path, raw: str, expected: str) -> None:
    assert _wa_settings(tmp_path, whatsapp_jid=raw).whatsapp_jid == expected


def test_whatsapp_jid_rejects_non_numbers(tmp_path: Path) -> None:
    from pincer.exceptions import ConfigError

    with pytest.raises((ConfigError, ValidationError), match="PINCER_WHATSAPP_JID"):
        _wa_settings(tmp_path, whatsapp_jid="491512345678@s.whatsapp.net")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("POSTGRES://u:P@h/DB", "postgres://u:P@h/DB"),
        ("PostgreSQL://u:p@h/db?search_path=WA", "postgresql://u:p@h/db?search_path=WA"),
    ],
)
def test_whatsapp_session_db_lowercases_scheme_only(tmp_path: Path, url: str, expected: str) -> None:
    assert _wa_settings(tmp_path, whatsapp_session_db=url).whatsapp_session_db.get_secret_value() == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("postgres://h/db", True),
        ("postgresql://h/db", True),
        ("POSTGRES://h/db", True),
        ("postgres:/h/db", False),
        ("sqlite:///x.db", False),
        ("", False),
    ],
)
def test_is_postgres_url(value: str, expected: bool) -> None:
    from pincer.config.channels import is_postgres_url

    assert is_postgres_url(value) is expected

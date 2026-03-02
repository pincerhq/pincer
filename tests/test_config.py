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

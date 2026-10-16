"""Tests for the default target list of scripts/smoke_llm.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from pincer.config import Settings

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_llm.py"


@pytest.fixture(scope="module")
def default_targets():
    spec = importlib.util.spec_from_file_location("smoke_llm", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.default_targets


def _settings(**overrides) -> Settings:
    return Settings(telegram_bot_token="123456:TEST", **overrides)  # type: ignore[arg-type]


def test_openai_primary_uses_default_model(default_targets):
    settings = _settings(
        default_provider="openai",
        default_model="gpt-5",
        summary_model="gpt-5-mini",
        openai_api_key="sk-test",
    )
    assert default_targets(settings) == [("openai", "gpt-5"), ("openai", "gpt-5-mini")]


def test_anthropic_primary_keeps_vendor_prefixed_model(default_targets):
    settings = _settings(
        default_model="anthropic/claude-opus-4.7",
        summary_model="anthropic/claude-haiku-4.5",
        anthropic_api_key="sk-test",
    )
    assert default_targets(settings) == [
        ("anthropic", "anthropic/claude-opus-4.7"),
        ("anthropic", "anthropic/claude-haiku-4.5"),
    ]


def test_voice_turn_model_on_another_provider(default_targets):
    settings = _settings(
        default_model="claude-sonnet-5-5",
        summary_model="claude-haiku-5-5",
        anthropic_api_key="sk-test",
        openai_api_key="sk-test",
        voice_turn_model="openai:gpt-5-mini",
    )
    assert default_targets(settings) == [
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic", "claude-haiku-5-5"),
        ("openai", "gpt-5-mini"),
    ]


def test_bare_voice_turn_model_runs_on_default_provider(default_targets):
    settings = _settings(
        default_provider="openai",
        default_model="gpt-5",
        summary_model="gpt-5",
        openai_api_key="sk-test",
        voice_turn_model="gpt-5-mini",
    )
    assert default_targets(settings) == [("openai", "gpt-5"), ("openai", "gpt-5-mini")]


def test_no_keys_gives_nothing(default_targets):
    # Settings needs some provider to validate; a compatible endpoint that is
    # not in the pool leaves the keyless default provider with nothing to call.
    settings = _settings(
        anthropic_api_key="",
        openai_api_key="",
        openai_compatible_base_url="http://localhost:11434/v1",
    )
    assert default_targets(settings) == []


def test_openai_primary_ignores_claude_summary_default(default_targets):
    settings = _settings(default_provider="openai", default_model="gpt-5-mini", openai_api_key="sk-test")
    assert settings.summary_model.startswith("claude")
    assert default_targets(settings) == [("openai", "gpt-5-mini")]


def test_summary_model_provider_prefix(default_targets):
    settings = _settings(
        default_provider="openai",
        default_model="gpt-5-mini",
        openai_api_key="sk-test",
        anthropic_api_key="sk-test",
        summary_model="anthropic:claude-haiku-4-5",
    )
    assert default_targets(settings) == [("openai", "gpt-5-mini"), ("anthropic", "claude-haiku-4-5")]

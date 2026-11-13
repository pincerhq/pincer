"""Endpoint capabilities come from the provider lists in the settings (issue #358)."""

from __future__ import annotations

import pytest

from pincer.config import Settings
from pincer.llm.capabilities import endpoint_capabilities


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_by_default_anthropic_and_the_configured_compatible_name_take_the_betas():
    settings = _settings(anthropic_compatible_provider="foundry")

    assert settings.server_compaction_providers is None  # precondition: nothing configured
    assert endpoint_capabilities(settings, "anthropic").drop_block
    assert endpoint_capabilities(settings, "foundry").drop_block
    assert endpoint_capabilities(settings, "foundry").server_compaction
    assert not endpoint_capabilities(settings, "openai").drop_block
    assert not endpoint_capabilities(settings, "other").server_compaction


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        ("foundry", {"foundry": True, "anthropic": False}),
        ("anthropic, foundry", {"foundry": True, "anthropic": True}),
        ("none", {"foundry": False, "anthropic": False}),
        ([], {"foundry": False, "anthropic": False}),
    ],
    ids=["explicit list", "list with spaces", "none is off", "empty list is off"],
)
def test_an_explicit_list_replaces_the_default(listed, expected):
    settings = _settings(anthropic_compatible_provider="foundry", server_compaction_providers=listed)

    assert settings.server_compaction_providers is not None  # precondition: the list was read
    assert {name: endpoint_capabilities(settings, name).drop_block for name in expected} == expected
    assert {name: endpoint_capabilities(settings, name).server_compaction for name in expected} == expected


def test_none_in_the_environment_turns_the_list_off(monkeypatch):
    monkeypatch.setenv("PINCER_SERVER_COMPACTION_PROVIDERS", "None")

    settings = _settings(anthropic_compatible_provider="foundry")

    assert settings.server_compaction_providers == []
    assert not endpoint_capabilities(settings, "anthropic").drop_block


def test_an_unnamed_compatible_endpoint_does_not_make_an_empty_name_capable():
    settings = _settings()

    assert settings.anthropic_compatible_provider == ""  # precondition: no compatible endpoint configured
    assert not endpoint_capabilities(settings, "").drop_block

"""Tests for DoctrSettings — request-logging fields in particular."""

from __future__ import annotations

import pytest


def test_log_tool_requests_defaults() -> None:
    from doctr_mcp.config import DoctrSettings

    settings = DoctrSettings()
    assert settings.log_tool_requests is False
    assert settings.log_max_payload_length == 2000


def test_log_tool_requests_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp.config import DoctrSettings

    monkeypatch.setenv("DOCTR_LOG_TOOL_REQUESTS", "true")
    monkeypatch.setenv("DOCTR_LOG_MAX_PAYLOAD_LENGTH", "500")

    settings = DoctrSettings()
    assert settings.log_tool_requests is True
    assert settings.log_max_payload_length == 500

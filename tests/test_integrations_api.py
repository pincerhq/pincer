"""Tests for src/pincer/api/integrations.py."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from pincer.api.integrations import (
    _builtin_entries,
    _google_active,
    _google_categories,
    _integration_entries,
    _load_category,
    _mcp_skill_entries,
    _slack_active,
    _slack_categories,
    _ToolCollector,
)
from pincer.api.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

# ── helpers ────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


def _fake_module(*tool_names: str) -> types.ModuleType:
    """Return a fake integration module whose register_fn populates the collector."""
    mod = types.ModuleType("_fake")

    def register_fn(registry: _ToolCollector, _settings: object) -> None:
        for name in tool_names:
            registry.register(name, f"{name} description")

    mod.register_fn = register_fn
    return mod


# ── _ToolCollector ────────────────────────────────────────────────────────────


def test_tool_collector_stores_name_and_description() -> None:
    c = _ToolCollector()
    c.register("search_web", "Search the web", extra="ignored")
    assert c.tools == [{"name": "search_web", "description": "Search the web"}]


def test_tool_collector_default_description_is_empty() -> None:
    c = _ToolCollector()
    c.register("bare_tool")
    assert c.tools[0]["description"] == ""


def test_tool_collector_accumulates_multiple_registrations() -> None:
    c = _ToolCollector()
    c.register("a", "first")
    c.register("b", "second")
    assert len(c.tools) == 2
    assert c.tools[1]["name"] == "b"


# ── _load_category ────────────────────────────────────────────────────────────


def test_load_category_returns_empty_on_missing_package() -> None:
    assert _load_category("no.such.package.at.all", "mod", "fn") == []


def test_load_category_returns_empty_on_missing_function() -> None:
    assert _load_category("pincer.api.integrations", "integrations", "no_such_fn") == []


def test_load_category_returns_tools_from_register_fn() -> None:
    mod = _fake_module("tool_a", "tool_b")
    with patch("importlib.import_module", return_value=mod):
        result = _load_category("any.pkg", "any_mod", "register_fn")
    assert len(result) == 2
    assert result[0] == {"name": "tool_a", "description": "tool_a description"}


def test_load_category_returns_empty_when_register_fn_raises() -> None:
    mod = types.ModuleType("_raises")
    mod.bad_fn = lambda _reg, _s: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[attr-defined]
    with patch("importlib.import_module", return_value=mod):
        result = _load_category("any.pkg", "any_mod", "bad_fn")
    assert result == []


# ── _google_active ────────────────────────────────────────────────────────────


def test_google_active_true_with_workspace_token(tmp_path: Path) -> None:
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_workspace_token.json").write_text("{}")
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        assert _google_active() is True


def test_google_active_true_with_legacy_token(tmp_path: Path) -> None:
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")  # legacy path
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        assert _google_active() is True


def test_google_active_false_when_token_missing(tmp_path: Path) -> None:
    (tmp_path / "google_credentials.json").write_text("{}")
    # No token file
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        assert _google_active() is False


def test_google_active_false_when_creds_missing(tmp_path: Path) -> None:
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        assert _google_active() is False


def test_google_active_false_on_exception() -> None:
    with patch("pincer.config.get_settings_relaxed", side_effect=RuntimeError("boom")):
        assert _google_active() is False


# ── _slack_active ─────────────────────────────────────────────────────────────


def test_slack_active_true_when_bot_token_present() -> None:
    tokens = MagicMock(bot_token="xoxb-real-token")
    with patch("pincer.integrations.slack.auth.load_tokens", return_value=tokens):
        assert _slack_active() is True


def test_slack_active_false_when_no_bot_token() -> None:
    tokens = MagicMock(bot_token=None)
    with patch("pincer.integrations.slack.auth.load_tokens", return_value=tokens):
        assert _slack_active() is False


def test_slack_active_false_on_exception() -> None:
    with patch("pincer.integrations.slack.auth.load_tokens", side_effect=Exception("fail")):
        assert _slack_active() is False


# ── category loaders ──────────────────────────────────────────────────────────


def test_google_categories_returns_list() -> None:
    assert isinstance(_google_categories(), list)


def test_slack_categories_returns_list() -> None:
    assert isinstance(_slack_categories(), list)


def test_google_categories_includes_gmail_when_load_succeeds() -> None:
    mod = types.ModuleType("fake_gmail")
    mod.register_gmail_tools = lambda reg, _: reg.register("send_email", "Send email")  # type: ignore[attr-defined]
    with patch("importlib.import_module", return_value=mod):
        result = _google_categories()
    assert any(c["name"] == "Gmail" for c in result)


def test_slack_categories_includes_channels_when_load_succeeds() -> None:
    mod = types.ModuleType("fake_slack_channels")
    mod.register_channel_tools = lambda reg, _: reg.register("list_channels", "List channels")  # type: ignore[attr-defined]
    with patch("importlib.import_module", return_value=mod):
        result = _slack_categories()
    assert any(c["name"] == "Channels" for c in result)


# ── _mcp_skill_entries ────────────────────────────────────────────────────────


def test_mcp_skill_entries_returns_empty_when_import_fails() -> None:
    saved = sys.modules.get("pincer.mcp.config")
    sys.modules["pincer.mcp.config"] = None  # type: ignore[assignment]
    try:
        result = _mcp_skill_entries()
    finally:
        if saved is None:
            sys.modules.pop("pincer.mcp.config", None)
        else:
            sys.modules["pincer.mcp.config"] = saved
    assert result == []


def test_mcp_skill_entries_returns_empty_when_mcp_disabled() -> None:
    cfg = MagicMock(enabled=False)
    with patch("pincer.mcp.config.load_mcp_config", return_value=cfg):
        assert _mcp_skill_entries() == []


def test_mcp_skill_entries_returns_empty_on_config_error() -> None:
    with patch("pincer.mcp.config.load_mcp_config", side_effect=RuntimeError("bad config")):
        assert _mcp_skill_entries() == []


def _make_server(
    name: str = "weather",
    command: str | None = "python",
    args: list[str] | None = None,
    url: str | None = None,
    transport: str = "stdio",
    enabled: bool = True,
    approval_required: list[str] | None = None,
) -> MagicMock:
    srv = MagicMock()
    srv.name = name
    srv.command = command
    srv.args = args or ["server.py"]
    srv.url = url
    srv.transport = MagicMock(value=transport)
    srv.enabled = enabled
    srv.approval_required = approval_required or []
    return srv


def test_mcp_skill_entries_command_based_server() -> None:
    cfg = MagicMock(enabled=True, servers=[_make_server("weather", command="python", args=["w.py"])])
    with patch("pincer.mcp.config.load_mcp_config", return_value=cfg):
        result = _mcp_skill_entries()
    assert len(result) == 1
    assert result[0]["name"] == "weather"
    assert result[0]["source"] == "mcp"
    assert result[0]["status"] == "active"
    assert "python" in result[0]["description"]


def test_mcp_skill_entries_url_based_server() -> None:
    cfg = MagicMock(enabled=True, servers=[_make_server("remote", command=None, url="http://mcp.example.com")])
    with patch("pincer.mcp.config.load_mcp_config", return_value=cfg):
        result = _mcp_skill_entries()
    assert result[0]["description"] == "http://mcp.example.com"


def test_mcp_skill_entries_transport_fallback() -> None:
    cfg = MagicMock(enabled=True, servers=[_make_server("bare", command=None, url=None, transport="stdio")])
    with patch("pincer.mcp.config.load_mcp_config", return_value=cfg):
        result = _mcp_skill_entries()
    assert result[0]["description"] == "stdio"


def test_mcp_skill_entries_disabled_server_shows_disabled_status() -> None:
    cfg = MagicMock(enabled=True, servers=[_make_server("off", enabled=False)])
    with patch("pincer.mcp.config.load_mcp_config", return_value=cfg):
        result = _mcp_skill_entries()
    assert result[0]["status"] == "disabled"


# ── _builtin_entries ──────────────────────────────────────────────────────────


def test_builtin_entries_includes_core_tools() -> None:
    names = {e["name"] for e in _builtin_entries()}
    assert "file_read" in names
    assert "python_exec" in names


def test_builtin_entries_excludes_double_underscore_tools() -> None:
    for entry in _builtin_entries():
        assert "__" not in entry["name"]


def test_builtin_entries_have_required_fields() -> None:
    for entry in _builtin_entries():
        assert entry["source"] == "builtin"
        assert entry["status"] == "active"
        assert "description" in entry
        assert isinstance(entry["approval_required"], bool)


def test_builtin_entries_reflects_approval_flags() -> None:
    by_name = {e["name"]: e for e in _builtin_entries()}
    assert by_name["file_read"]["approval_required"] is False


def test_builtin_entries_respects_shell_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_SHELL_ENABLED", "false")
    from pincer.config import get_settings_relaxed

    get_settings_relaxed.cache_clear()
    names = {e["name"] for e in _builtin_entries()}
    get_settings_relaxed.cache_clear()
    assert "shell_exec" not in names


# ── _integration_entries ──────────────────────────────────────────────────────


def test_integration_entries_returns_exactly_two() -> None:
    assert len(_integration_entries()) == 2


def test_integration_entries_has_required_fields() -> None:
    for entry in _integration_entries():
        assert "name" in entry
        assert "status" in entry
        assert "tools" in entry
        assert "source" in entry
        assert entry["source"] == "integration"


def test_integration_entries_google_active_when_creds_exist(tmp_path: Path) -> None:
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_workspace_token.json").write_text("{}")
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        entries = _integration_entries()
    google = next(e for e in entries if "Google" in e["name"])
    assert google["status"] == "active"


def test_integration_entries_google_disabled_without_creds(tmp_path: Path) -> None:
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        entries = _integration_entries()
    google = next(e for e in entries if "Google" in e["name"])
    assert google["status"] == "disabled"


# ── GET /api/integrations ─────────────────────────────────────────────────────


def test_list_integrations_returns_200(client: TestClient) -> None:
    resp = client.get("/api/integrations")
    assert resp.status_code == 200
    assert "integrations" in resp.json()


def test_list_integrations_includes_builtin_names(client: TestClient) -> None:
    resp = client.get("/api/integrations")
    names = {e["name"] for e in resp.json()["integrations"]}
    assert "Google Workspace" in names
    assert "Slack" in names


def test_list_integrations_excludes_file_skills(client: TestClient) -> None:
    """File-based (SKILL.md) skills are served by /api/skills, not /api/integrations."""
    resp = client.get("/api/integrations")
    sources = {e["source"] for e in resp.json()["integrations"]}
    assert "file" not in sources


def test_list_integrations_includes_builtin_tools(client: TestClient) -> None:
    resp = client.get("/api/integrations")
    names = {e["name"] for e in resp.json()["integrations"]}
    assert "file_read" in names


# ── GET /api/integrations/{slug} ──────────────────────────────────────────────


def test_get_integration_unknown_slug_is_404(client: TestClient) -> None:
    assert client.get("/api/integrations/unknown-xyz").status_code == 404


def test_get_integration_google_shape(client: TestClient) -> None:
    resp = client.get("/api/integrations/google")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "google"
    assert data["name"] == "Google Workspace"
    assert "categories" in data
    assert "tool_count" in data
    assert "usage" in data
    assert "total_calls" in data["usage"]
    assert "by_tool" in data["usage"]


def test_get_integration_ms365_returns_404(client: TestClient) -> None:
    assert client.get("/api/integrations/ms365").status_code == 404


def test_get_integration_slack_shape(client: TestClient) -> None:
    resp = client.get("/api/integrations/slack")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Slack"


def test_get_integration_shows_active_status(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_workspace_token.json").write_text("{}")
    settings = MagicMock()
    settings.google_oauth_dir.return_value = tmp_path
    with patch("pincer.config.get_settings_relaxed", return_value=settings):
        resp = client.get("/api/integrations/google")
    assert resp.json()["status"] == "active"


def test_get_integration_tool_usage_filtered_by_prefix(client: TestClient) -> None:
    mock_audit = AsyncMock()
    mock_audit.get_stats = AsyncMock(
        return_value={
            "by_tool": {
                "google__send_email": 7,
                "google__list_emails": 3,
                "slack__post_message": 1,
            }
        }
    )
    with patch("pincer.security.audit.get_audit_logger", new=AsyncMock(return_value=mock_audit)):
        resp = client.get("/api/integrations/google")
    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert usage["total_calls"] == 10
    assert "google__send_email" in usage["by_tool"]
    assert "slack__post_message" not in usage["by_tool"]

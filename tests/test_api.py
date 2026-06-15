"""Tests for the FastAPI server."""

import os

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient

from pincer.api.server import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    # Isolate from the developer's .env so no dashboard token is loaded
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_status(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "agent_running" in data
    assert "channels" in data


def test_doctor_endpoint(client):
    response = client.get("/api/doctor")
    assert response.status_code == 200
    data = response.json()
    assert "score" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)


def test_auth_required_with_token(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DASHBOARD_TOKEN", "test-secret-token-1234")
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
        c = TestClient(app)

        # Health is always public
        assert c.get("/api/health").status_code == 200

        # Protected endpoint without token
        assert c.get("/api/status").status_code == 401

        # With correct token
        assert c.get("/api/status", headers={"Authorization": "Bearer test-secret-token-1234"}).status_code == 200

        # With wrong token
        assert c.get("/api/status", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    finally:
        get_settings_relaxed.cache_clear()


def test_costs_today(client):
    response = client.get("/api/costs/today")
    assert response.status_code == 200
    data = response.json()
    assert "date" in data
    assert "total_usd" in data
    assert "budget" in data


def test_costs_history(client):
    response = client.get("/api/costs/history?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert data["period_days"] == 7
    assert "data" in data
    assert "totals" in data


def test_costs_by_model(client):
    response = client.get("/api/costs/by-model?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert "models" in data


def test_costs_by_tool(client):
    response = client.get("/api/costs/by-tool?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert "tools" in data


# ── Regression: #117 — /api/status channel flags read from .env ──────────────


def test_status_channels_from_dotenv_only(tmp_path, monkeypatch):
    """Channel flags in /api/status must reflect .env, not just shell env."""
    from pincer.config import get_settings_relaxed

    (tmp_path / ".env").write_text("PINCER_TELEGRAM_BOT_TOKEN=123456:TEST\nPINCER_DISCORD_BOT_TOKEN=discord-token\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WHATSAPP_ENABLED", raising=False)
    monkeypatch.delenv("PINCER_VOICE_ENABLED", raising=False)
    monkeypatch.delenv("PINCER_SIGNAL_ENABLED", raising=False)
    # microsoft_teams calls load_dotenv() on import, which may populate os.environ
    # with variables from the project .env (including a dashboard token). Remove it.
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)

    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
        c = TestClient(app)
        resp = c.get("/api/status")
        assert resp.status_code == 200
        channels = resp.json()["channels"]
        assert channels["telegram"] is True
        assert channels["discord"] is True
        assert channels["whatsapp"] is False
    finally:
        get_settings_relaxed.cache_clear()


def test_teams_path_bypasses_auth(monkeypatch, tmp_path):
    """Requests to /api/apps/teams/* skip Bearer auth even when a token is configured."""
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DASHBOARD_TOKEN", "secret-token")
    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            # No Authorization header — would normally get 401
            resp = c.post("/api/apps/teams/api/messages", json={})
            # 404 (route not registered) rather than 401 confirms auth was bypassed
            assert resp.status_code != 401
    finally:
        get_settings_relaxed.cache_clear()


def test_ngrok_domain_falls_back_to_base_url_host(monkeypatch, tmp_path):
    """check_ngrok_domain fills ngrok_domain from base_url.host when authtoken is set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_BASE_URL", "https://mybot.example.com")
    monkeypatch.setenv("PINCER_NGROK_AUTHTOKEN", "tok_test")
    monkeypatch.delenv("PINCER_NGROK_DOMAIN", raising=False)
    from pincer.config import get_settings_relaxed

    get_settings_relaxed.cache_clear()
    try:
        settings = get_settings_relaxed()
        assert settings.ngrok_domain == "mybot.example.com"
    finally:
        get_settings_relaxed.cache_clear()


# ── _print_voice_webhook_urls ─────────────────────────────────────────────────


class _FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, line: str) -> None:
        self.lines.append(line)


def test_print_voice_webhook_urls_conversation_relay():
    from unittest.mock import MagicMock

    from pincer.cli import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = "https://abc.ngrok.io"
    settings.voice_engine = "conversation_relay"
    console = _FakeConsole()
    _print_voice_webhook_urls(settings, console)
    joined = "\n".join(console.lines)
    assert "https://abc.ngrok.io/api/apps/twilio/webhook" in joined
    assert "https://abc.ngrok.io/api/apps/twilio/relay-webhook" in joined
    assert "stream" not in joined


def test_print_voice_webhook_urls_media_streams():
    from unittest.mock import MagicMock

    from pincer.cli import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = "https://abc.ngrok.io"
    settings.voice_engine = "media_streams"
    console = _FakeConsole()
    _print_voice_webhook_urls(settings, console)
    joined = "\n".join(console.lines)
    assert "wss://abc.ngrok.io/api/apps/twilio/stream" in joined
    assert "relay-webhook" not in joined


def test_print_voice_webhook_urls_skips_when_empty():
    from unittest.mock import MagicMock

    from pincer.cli import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = ""
    console = _FakeConsole()
    _print_voice_webhook_urls(settings, console)
    assert console.lines == []

"""Tests for the google/signal/slack/whatsapp CLI app groups (typer.testing.CliRunner)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


# ── whatsapp ──────────────────────────────────────────────────────────────


def test_whatsapp_setup_pairs_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pincer.config.get_settings", lambda: MagicMock())

    mock_wa = MagicMock()
    mock_wa.start = AsyncMock()
    mock_wa.stop = AsyncMock()
    monkeypatch.setattr("pincer.channels.whatsapp.WhatsAppChannel", lambda settings: mock_wa)

    result = runner.invoke(app, ["whatsapp", "setup"])

    assert result.exit_code == 0
    assert "paired successfully" in result.output
    mock_wa.start.assert_awaited_once()
    mock_wa.stop.assert_awaited_once()


def test_whatsapp_setup_reports_pairing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pincer.config.get_settings", lambda: MagicMock())

    mock_wa = MagicMock()
    mock_wa.start = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("pincer.channels.whatsapp.WhatsAppChannel", lambda settings: mock_wa)

    result = runner.invoke(app, ["whatsapp", "setup"])

    assert result.exit_code == 0
    assert "Pairing failed" in result.output
    assert "boom" in result.output


# ── signal ────────────────────────────────────────────────────────────────


def test_signal_setup_unreachable_api(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = MagicMock()
    mock_settings.signal_pair_url = "http://127.0.0.1:8081"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    result = runner.invoke(app, ["signal", "setup"])

    assert result.exit_code == 1
    assert "Cannot reach signal-api" in result.output


def test_signal_setup_opens_browser_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = MagicMock()
    mock_settings.signal_pair_url = "http://127.0.0.1:8081"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    class _CtxMgr:
        def __enter__(self) -> _CtxMgr:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=3: _CtxMgr())

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    result = runner.invoke(app, ["signal", "setup"])

    assert result.exit_code == 0
    assert opened, "webbrowser.open was not called"
    assert "qrcodelink" in opened[0]


def test_signal_status_no_accounts_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = MagicMock()
    mock_settings.signal_api_url = "http://signal-api:8080"
    mock_settings.signal_phone_number = ""
    mock_settings.signal_enabled = False
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.health = AsyncMock(return_value={"status": "ok"})
    mock_client.list_accounts = AsyncMock(return_value=[])
    mock_client.about = AsyncMock(return_value={"version": "1.0"})
    monkeypatch.setattr(
        "pincer.channels.signal_client.SignalClient",
        lambda api_url, phone: mock_client,
    )

    result = runner.invoke(app, ["signal", "status"])

    assert result.exit_code == 0
    assert "none registered yet" in result.output
    mock_client.connect.assert_awaited_once()
    mock_client.disconnect.assert_awaited_once()


def test_signal_status_reports_accounts_and_health_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.channels.signal_client import SignalAPIError

    mock_settings = MagicMock()
    mock_settings.signal_api_url = "http://signal-api:8080"
    mock_settings.signal_phone_number = "+15551234567"
    mock_settings.signal_enabled = True
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.health = AsyncMock(side_effect=SignalAPIError("down"))
    mock_client.list_accounts = AsyncMock(return_value=["+15551234567"])
    mock_client.about = AsyncMock(return_value={"version": "1.0"})
    monkeypatch.setattr(
        "pincer.channels.signal_client.SignalClient",
        lambda api_url, phone: mock_client,
    )

    result = runner.invoke(app, ["signal", "status"])

    assert result.exit_code == 0
    assert "FAIL" in result.output
    assert "+15551234567" in result.output
    mock_client.disconnect.assert_awaited_once()


def test_signal_test_requires_phone_number(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = MagicMock()
    mock_settings.signal_phone_number = ""
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["signal", "test", "+15550001111"])

    assert result.exit_code == 1
    assert "not set" in result.output


def test_signal_test_sends_message(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = MagicMock()
    mock_settings.signal_phone_number = "+15551234567"
    mock_settings.signal_api_url = "http://signal-api:8080"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.send_message = AsyncMock()
    monkeypatch.setattr(
        "pincer.channels.signal_client.SignalClient",
        lambda api_url, phone: mock_client,
    )

    result = runner.invoke(app, ["signal", "test", "+15550001111"])

    assert result.exit_code == 0
    assert "Test message sent" in result.output
    mock_client.send_message.assert_awaited_once_with("+15550001111", "Hello from Pincer! Signal channel is working.")


def test_signal_test_reports_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.channels.signal_client import SignalAPIError

    mock_settings = MagicMock()
    mock_settings.signal_phone_number = "+15551234567"
    mock_settings.signal_api_url = "http://signal-api:8080"
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.send_message = AsyncMock(side_effect=SignalAPIError("offline"))
    monkeypatch.setattr(
        "pincer.channels.signal_client.SignalClient",
        lambda api_url, phone: mock_client,
    )

    result = runner.invoke(app, ["signal", "test", "+15550001111"])

    assert result.exit_code == 0
    assert "Send failed" in result.output
    mock_client.disconnect.assert_awaited_once()


# ── slack ─────────────────────────────────────────────────────────────────


def test_slack_setup_rejects_invalid_bot_token() -> None:
    result = runner.invoke(app, ["slack", "setup"], input="not-a-token\n")

    assert result.exit_code == 1
    assert "must start with 'xoxb-'" in result.output


def test_slack_setup_reports_validation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pincer.integrations.slack.auth.validate_bot_token",
        AsyncMock(side_effect=RuntimeError("invalid token")),
    )

    result = runner.invoke(app, ["slack", "setup"], input="xoxb-abc123\n")

    assert result.exit_code == 1
    assert "invalid token" in result.output


def test_slack_setup_success_without_channel(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setattr(
        "pincer.integrations.slack.auth.validate_bot_token",
        AsyncMock(return_value={"workspace": "Acme", "bot_user": "pincer"}),
    )
    monkeypatch.setattr(
        "pincer.integrations.slack.auth.save_tokens",
        lambda bot, user: tmp_path / "slack_tokens.json",  # type: ignore[operator]
    )

    result = runner.invoke(app, ["slack", "setup"], input="xoxb-abc123\nn\nn\n")

    assert result.exit_code == 0
    assert "Authenticated!" in result.output
    assert "71 Slack tools are now available" in result.output
    assert "Slack channel ready" not in result.output


def test_slack_setup_success_with_channel_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "pincer.integrations.slack.auth.validate_bot_token",
        AsyncMock(return_value={"workspace": "Acme", "bot_user": "pincer"}),
    )
    monkeypatch.setattr(
        "pincer.integrations.slack.auth.save_tokens",
        lambda bot, user: tmp_path / "slack_tokens.json",
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr("pincer.cli.slack._find_env_file", lambda: str(env_path))

    result = runner.invoke(
        app,
        ["slack", "setup"],
        input="xoxb-abc123\ny\nxoxp-user\ny\nxapp-token\n",
    )

    assert result.exit_code == 0
    assert "Slack channel ready" in result.output
    saved = env_path.read_text()
    assert "PINCER_SLACK_BOT_TOKEN=xoxb-abc123" in saved
    assert "PINCER_SLACK_APP_TOKEN=xapp-token" in saved


# ── google ────────────────────────────────────────────────────────────────


def test_google_setup_missing_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["google", "setup"])

    assert result.exit_code == 1
    assert "Missing:" in result.output


def test_google_setup_declines_reauth(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_workspace_token.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    result = runner.invoke(app, ["google", "setup"], input="n\n")

    assert result.exit_code == 0
    assert "Token already exists" in result.output


def test_google_setup_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_creds = MagicMock()
    mock_creds.refresh_token = "rt"
    mock_auth_instance = MagicMock()
    mock_auth_instance.run_auth_flow.return_value = mock_creds
    monkeypatch.setattr(
        "pincer.integrations.google.auth.GoogleAuth",
        lambda credentials_path, token_path: mock_auth_instance,
    )

    result = runner.invoke(app, ["google", "setup"])

    assert result.exit_code == 0
    assert "Google Workspace authenticated!" in result.output
    mock_auth_instance.run_auth_flow.assert_called_once_with(open_browser=True)


def test_google_setup_reports_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: mock_settings)

    mock_auth_instance = MagicMock()
    mock_auth_instance.run_auth_flow.side_effect = RuntimeError("consent denied")
    monkeypatch.setattr(
        "pincer.integrations.google.auth.GoogleAuth",
        lambda credentials_path, token_path: mock_auth_instance,
    )

    result = runner.invoke(app, ["google", "setup"])

    assert result.exit_code == 1
    assert "Authentication failed" in result.output


def test_google_auth_missing_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)

    result = runner.invoke(app, ["google", "auth"])

    assert result.exit_code == 1
    assert "Missing:" in result.output


def test_google_auth_declines_overwrite(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)

    result = runner.invoke(app, ["google", "auth"], input="n\n")

    assert result.exit_code == 0
    assert "Token already exists" in result.output


def test_google_auth_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)

    mock_creds = MagicMock()
    mock_creds.refresh_token = "rt"
    mock_creds.expiry = "2026-01-01"
    mock_creds.to_json.return_value = "{}"

    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds

    mock_flow_cls = MagicMock()
    mock_flow_cls.from_client_secrets_file.return_value = mock_flow
    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls)

    result = runner.invoke(app, ["google", "auth"])

    assert result.exit_code == 0
    assert "Google Calendar authorized!" in result.output
    assert (tmp_path / "google_token.json").exists()


def test_google_auth_falls_back_to_manual_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "google_credentials.json").write_text("{}")

    mock_settings = MagicMock()
    mock_settings.google_oauth_dir.return_value = tmp_path
    monkeypatch.setattr("pincer.config.get_settings", lambda: mock_settings)

    mock_creds = MagicMock()
    mock_creds.refresh_token = "rt"
    mock_creds.expiry = "2026-01-01"
    mock_creds.to_json.return_value = "{}"

    mock_flow = MagicMock()
    mock_flow.run_local_server.side_effect = [RuntimeError("no display"), mock_creds]

    mock_flow_cls = MagicMock()
    mock_flow_cls.from_client_secrets_file.return_value = mock_flow
    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls)

    result = runner.invoke(app, ["google", "auth"])

    assert result.exit_code == 0
    assert "Browser not available" in result.output
    assert "Google Calendar authorized!" in result.output

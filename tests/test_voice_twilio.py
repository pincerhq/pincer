"""Tests for TwiML server endpoints and webhook handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from pincer.voice.twiml_server import init_voice_routes, voice_router


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.get_active_calls = MagicMock(return_value={})
    engine.get_call_state = MagicMock(return_value=None)
    engine.on_call_start = AsyncMock()
    engine.on_speech_input = AsyncMock()
    engine.interrupt_speech = AsyncMock()
    engine.end_call = AsyncMock()
    return engine


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.voice_engine = "conversation_relay"
    settings.voice_webhook_base_url = "https://example.com"
    settings.voice_language = "en-US"
    settings.voice_allowed_callers = "*"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    return settings


@pytest.fixture
def app(mock_engine, mock_settings):
    from fastapi import FastAPI

    init_voice_routes(mock_engine, mock_settings)
    app = FastAPI()
    app.include_router(voice_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_check(self, client):
        response = client.get("/voice/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["engine"] == "conversation_relay"


class TestWebhookEndpoint:
    def test_inbound_call_webhook(self, client, mock_engine):
        response = client.post(
            "/voice/webhook",
            data={
                "CallSid": "CA123",
                "From": "+14155551234",
                "To": "+14155559876",
            },
        )
        assert response.status_code == 200
        assert "text/xml" in response.headers.get("content-type", "")
        assert "<Response>" in response.text
        mock_engine.on_call_start.assert_called_once()

    def test_rejected_caller(self, client, mock_settings, mock_engine):
        mock_settings.voice_allowed_callers = "+10000000000"
        response = client.post(
            "/voice/webhook",
            data={
                "CallSid": "CA123",
                "From": "+14155551234",
                "To": "+14155559876",
            },
        )
        assert response.status_code == 200
        assert "not authorized" in response.text
        mock_engine.on_call_start.assert_not_called()


class TestStatusEndpoint:
    def test_status_callback(self, client):
        response = client.post(
            "/voice/status",
            data={
                "CallSid": "CA123",
                "CallStatus": "ringing",
                "CallDuration": "0",
            },
        )
        assert response.status_code == 200


class TestFallbackEndpoint:
    def test_fallback(self, client):
        response = client.post(
            "/voice/fallback",
            data={
                "CallSid": "CA123",
                "ErrorCode": "12345",
                "ErrorMessage": "Something went wrong",
            },
        )
        assert response.status_code == 200
        assert "difficulties" in response.text


class TestRelayWebhook:
    def test_prompt_event(self, client, mock_engine):
        response = client.post(
            "/voice/relay-webhook",
            json={
                "type": "prompt",
                "CallSid": "CA123",
                "voicePrompt": "What's on my calendar?",
            },
        )
        assert response.status_code == 200
        mock_engine.on_speech_input.assert_called_once_with(
            "CA123",
            "What's on my calendar?",
        )

    def test_interrupt_event(self, client, mock_engine):
        response = client.post(
            "/voice/relay-webhook",
            json={
                "type": "interrupt",
                "CallSid": "CA123",
            },
        )
        assert response.status_code == 200
        mock_engine.interrupt_speech.assert_called_once_with("CA123")

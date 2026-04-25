"""Tests for /api/chat/message and /api/chat/stream."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient  # noqa: E402

from pincer.api import server  # noqa: E402
from pincer.api.server import create_app  # noqa: E402
from pincer.core.agent import AgentResponse, StreamChunk, StreamEventType  # noqa: E402

UUID = "11111111-1111-4111-8111-111111111111"


def _make_client(*, token: str = "", web_chat_token: str = "") -> TestClient:
    """Build a TestClient with a mocked agent attached.

    TestClient(app) does NOT run the FastAPI lifespan unless used as a
    context manager, so build_agent_from_settings is not called and we
    attach our mock directly to app.state.agent.
    """
    server.DASHBOARD_TOKEN = token
    server.WEB_CHAT_TOKEN = web_chat_token
    app = create_app()

    agent = MagicMock()
    agent.handle_message = AsyncMock(
        return_value=AgentResponse(text="hi back", cost_usd=0.01, model="test-model")
    )

    async def _stream(*_args, **_kwargs):
        yield StreamChunk(StreamEventType.TEXT, "hi ")
        yield StreamChunk(StreamEventType.TEXT, "back")
        yield StreamChunk(StreamEventType.DONE, "hi back")

    agent.handle_message_stream = _stream
    app.state.agent = agent
    return TestClient(app)


def test_chat_message_requires_token():
    client = _make_client(token="secret")
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 401


def test_chat_stream_requires_token():
    client = _make_client(token="secret")
    r = client.post(
        "/api/chat/stream",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 401


def test_chat_message_requires_user_header():
    client = _make_client()
    r = client.post("/api/chat/message", json={"text": "hi"})
    assert r.status_code == 400


def test_chat_message_rejects_malformed_user_header():
    client = _make_client()
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": "not-a-uuid"},
    )
    assert r.status_code == 400


def test_chat_message_ok():
    client = _make_client()
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "hi back"
    assert body["cost_usd"] == 0.01
    assert body["model"] == "test-model"


def test_chat_stream_ok():
    client = _make_client()
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_raw()).decode()
    assert "event: chunk" in body
    assert "event: done" in body


def test_chat_returns_503_without_agent():
    server.DASHBOARD_TOKEN = ""
    server.WEB_CHAT_TOKEN = ""
    app = create_app()
    app.state.agent = None
    client = TestClient(app)
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 503


def test_chat_message_accepts_web_chat_token():
    client = _make_client(token="dashtoken", web_chat_token="webtoken")
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={
            "X-Pincer-User": UUID,
            "Authorization": "Bearer webtoken",
        },
    )
    assert r.status_code == 200

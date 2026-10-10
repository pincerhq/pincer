"""Tests for /api/chat/message and /api/chat/stream."""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient  # noqa: E402

from pincer.api.server import create_app  # noqa: E402
from pincer.core.agent import AgentResponse, StreamChunk, StreamEventType  # noqa: E402

UUID = "11111111-1111-4111-8111-111111111111"


def _make_client() -> TestClient:
    """Build a TestClient with a mocked agent attached.

    TestClient(app) does NOT run the FastAPI lifespan unless used as a
    context manager, so build_agent_from_settings is not called and we
    attach our mock directly to app.state.agent.
    """
    from pincer.config import get_settings_relaxed

    # Isolate from the developer's .env
    old_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp()

    os.chdir(tmpdir)
    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
    finally:
        os.chdir(old_cwd)
        get_settings_relaxed.cache_clear()
        shutil.rmtree(tmpdir, ignore_errors=True)

    app.state.agent = _mock_agent()
    return TestClient(app)


def _mock_agent() -> MagicMock:
    agent = MagicMock()
    agent.handle_message = AsyncMock(return_value=AgentResponse(text="hi back", cost_usd=0.01, model="test-model"))

    async def _stream(*_args, **_kwargs):
        yield StreamChunk(StreamEventType.TEXT, "hi ")
        yield StreamChunk(StreamEventType.TEXT, "back")
        yield StreamChunk(StreamEventType.DONE, "hi back")

    agent.handle_message_stream = _stream
    return agent


def _authed_client(authed_app) -> TestClient:
    authed_app.client.app.state.agent = _mock_agent()
    return authed_app.client


def _chat_user(client: TestClient) -> str:
    """Whose conversation the agent was handed."""
    return client.app.state.agent.handle_message.call_args.args[0]


def test_chat_message_requires_credentials(authed_app):
    client = _authed_client(authed_app)
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 401
    client.app.state.agent.handle_message.assert_not_called()


def test_chat_stream_requires_credentials(authed_app):
    client = _authed_client(authed_app)
    r = client.post(
        "/api/chat/stream",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 401


def test_a_signed_in_session_chats_as_itself_whatever_the_header_says(authed_app):
    """Naming someone else in X-Pincer-User must not open their conversation."""
    client = _authed_client(authed_app)
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={**authed_app.jwt_headers, "X-Pincer-User": UUID},
    )
    assert r.status_code == 200
    assert _chat_user(client) == "alice"

    # ...and needs no header at all.
    assert client.post("/api/chat/message", json={"text": "hi"}, headers=authed_app.jwt_headers).status_code == 200
    assert _chat_user(client) == "alice"


def test_an_api_key_keeps_the_header_as_the_visitor_session(authed_app):
    """One key embedded in a widget serves many visitors; each keeps a
    conversation of their own."""
    client = _authed_client(authed_app)
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={**authed_app.api_key_headers, "X-Pincer-User": UUID},
    )
    assert r.status_code == 200
    assert _chat_user(client) == UUID

    # Without the header the key speaks for its own identity.
    assert client.post("/api/chat/message", json={"text": "hi"}, headers=authed_app.api_key_headers).status_code == 200
    assert _chat_user(client) == "alice"

    malformed = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={**authed_app.api_key_headers, "X-Pincer-User": "not-a-uuid"},
    )
    assert malformed.status_code == 400


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
    client = _make_client()
    client.app.state.agent = None
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 503

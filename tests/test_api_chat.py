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
CONVERSATION = "22222222-2222-4222-8222-222222222222"


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

    async def _stream(*args, **kwargs):
        agent.stream_calls.append((args, kwargs))
        yield StreamChunk(StreamEventType.TEXT, "hi ")
        yield StreamChunk(StreamEventType.TEXT, "back")
        yield StreamChunk(StreamEventType.DONE, "hi back")

    agent.stream_calls = []
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


def _session_key(client: TestClient) -> str | None:
    """The exact session the agent was handed (None: the user's main one)."""
    return client.app.state.agent.handle_message.call_args.kwargs["session_key"]


def test_a_conversation_id_is_a_conversation_of_the_signed_in_identity(authed_app):
    """New chat: a conversation id opens a fresh model context, scoped under
    the identity whatever else the request names."""
    client = _authed_client(authed_app)
    headers = {**authed_app.jwt_headers, "X-Pincer-User": UUID}

    assert client.post("/api/chat/message", json={"text": "hi"}, headers=headers).status_code == 200
    assert _session_key(client) is None

    r = client.post("/api/chat/message", json={"text": "hi", "conversation_id": CONVERSATION}, headers=headers)
    assert r.status_code == 200
    assert _chat_user(client) == "alice"
    assert _session_key(client) == f"web:alice:{CONVERSATION}"


def test_a_conversation_id_on_the_stream(authed_app):
    client = _authed_client(authed_app)
    with client.stream(
        "POST", "/api/chat/stream", json={"text": "hi", "conversation_id": CONVERSATION}, headers=authed_app.jwt_headers
    ) as r:
        assert r.status_code == 200
        b"".join(r.iter_raw())
    args, kwargs = client.app.state.agent.stream_calls[-1]
    assert args[0] == "alice"
    assert kwargs["session_key"] == f"web:alice:{CONVERSATION}"


def test_a_conversation_under_an_api_key_belongs_to_the_visitor():
    client = _make_client()
    r = client.post(
        "/api/chat/message", json={"text": "hi", "conversation_id": CONVERSATION}, headers={"X-Pincer-User": UUID}
    )
    assert r.status_code == 200
    assert _session_key(client) == f"web:{UUID}:{CONVERSATION}"


def test_memory_is_on_unless_the_request_turns_it_off(authed_app):
    """A call summary or a knowledge-base question sends memory=false, so the
    person's long-term memory is neither read for it nor written with it."""
    client = _authed_client(authed_app)
    agent = client.app.state.agent

    client.post("/api/chat/message", json={"text": "hi"}, headers=authed_app.jwt_headers)
    assert agent.handle_message.call_args.kwargs["memory"] is True

    body = {"text": "summarize", "conversation_id": CONVERSATION, "memory": False}
    assert client.post("/api/chat/message", json=body, headers=authed_app.jwt_headers).status_code == 200
    assert agent.handle_message.call_args.kwargs["memory"] is False
    assert _chat_user(client) == "alice"

    with client.stream("POST", "/api/chat/stream", json=body, headers=authed_app.jwt_headers) as r:
        assert r.status_code == 200
        b"".join(r.iter_raw())
    assert agent.stream_calls[-1][1]["memory"] is False


def test_a_request_without_memory_needs_its_own_conversation():
    """memory=false would mark the user's main chat memory-free for good."""
    client = _make_client()
    agent = client.app.state.agent
    body = {"text": "summarize", "memory": False}
    headers = {"X-Pincer-User": UUID}

    assert client.post("/api/chat/message", json=body, headers=headers).status_code == 422
    assert client.post("/api/chat/stream", json=body, headers=headers).status_code == 422
    agent.handle_message.assert_not_called()
    assert agent.stream_calls == []


def test_conversation_id_case_does_not_split_the_session(authed_app):
    """Upper- and lower-case spellings of one id are one conversation."""
    client = _authed_client(authed_app)
    upper = "2222AAAA-2222-4222-8222-222222222222"
    keys = []
    for conversation_id in (upper, upper.lower()):
        body = {"text": "hi", "conversation_id": conversation_id}
        assert client.post("/api/chat/message", json=body, headers=authed_app.jwt_headers).status_code == 200
        keys.append(_session_key(client))
        with client.stream("POST", "/api/chat/stream", json=body, headers=authed_app.jwt_headers) as r:
            assert r.status_code == 200
            b"".join(r.iter_raw())
        keys.append(client.app.state.agent.stream_calls[-1][1]["session_key"])
    assert len(keys) == 4
    assert set(keys) == {"web:alice:2222aaaa-2222-4222-8222-222222222222"}


def test_a_malformed_conversation_id_is_rejected():
    """Only the bare hyphenated form is accepted; the braced and urn
    spellings `uuid.UUID` would also parse are not."""
    client = _make_client()
    for conversation_id in ("not-a-uuid", f"{CONVERSATION}:x", "", f"{{{CONVERSATION}}}", f"urn:uuid:{CONVERSATION}"):
        r = client.post(
            "/api/chat/message",
            json={"text": "hi", "conversation_id": conversation_id},
            headers={"X-Pincer-User": UUID},
        )
        assert r.status_code == 422, conversation_id
    client.app.state.agent.handle_message.assert_not_called()


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


def test_chat_message_returns_the_calls_the_turn_placed():
    client = _make_client()
    client.app.state.agent.handle_message.return_value = AgentResponse(text="ringing", call_sids=["CA1"])
    r = client.post("/api/chat/message", json={"text": "call"}, headers={"X-Pincer-User": UUID})
    assert r.json()["call_sids"] == ["CA1"]


def test_a_placed_call_is_named_on_its_tool_done_frame():
    client = _make_client()

    async def _stream(*args, **kwargs):
        yield StreamChunk(StreamEventType.TOOL_DONE, "make_phone_call", "CA1")
        yield StreamChunk(StreamEventType.TOOL_DONE, "make_phone_call")
        yield StreamChunk(StreamEventType.DONE, "ringing")

    client.app.state.agent.handle_message_stream = _stream
    with client.stream("POST", "/api/chat/stream", json={"text": "call"}, headers={"X-Pincer-User": UUID}) as r:
        body = b"".join(r.iter_raw()).decode()
    frames = [line.removeprefix("data: ") for line in body.splitlines() if '"phase"' in line]
    assert frames == [
        '{"phase": "done", "name": "make_phone_call", "call_sid": "CA1"}',
        '{"phase": "done", "name": "make_phone_call"}',
    ]


def test_chat_returns_503_without_agent():
    client = _make_client()
    client.app.state.agent = None
    r = client.post(
        "/api/chat/message",
        json={"text": "hi"},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 503


async def test_call_reports_are_scoped_to_the_user_and_conversation(tmp_path):
    """Issue #303: web chat pulls the reports of the calls it asked for, and only those."""
    from pincer.services.voice import CallsService
    from pincer.services.voice.calls import get_calls_service

    service = await CallsService.for_path(tmp_path / "pincer.db")
    other_conversation = "33333333-3333-4333-8333-333333333333"
    rows = [
        ("CA_conv", UUID, f"web:{UUID}:{CONVERSATION}", "2026-11-02T10:00:00+00:00"),
        ("CA_late", UUID, f"web:{UUID}:{CONVERSATION}", "2026-11-02T12:00:00+00:00"),
        ("CA_main", UUID, f"web:{UUID}", "2026-11-02T10:00:00+00:00"),
        ("CA_other_conv", UUID, f"web:{UUID}:{other_conversation}", "2026-11-02T10:00:00+00:00"),
        ("CA_other_user", "someone-else", f"web:{UUID}:{CONVERSATION}", "2026-11-02T10:00:00+00:00"),
    ]
    for sid, owner, key, at in rows:
        await service.save_call_origin(
            {"call_sid": sid, "user_id": owner, "pincer_user_id": owner, "session_key": key, "created_at": at}
        )
        await service.complete_call_report(sid, "busy", f"report {sid}", at)
    await service.save_call_origin(
        {
            "call_sid": "CA_ringing",
            "user_id": UUID,
            "pincer_user_id": UUID,
            "session_key": f"web:{UUID}:{CONVERSATION}",
            "created_at": "2026-11-02T10:00:00+00:00",
        }
    )

    client = _make_client()
    client.app.dependency_overrides[get_calls_service] = lambda: service
    headers = {"X-Pincer-User": UUID}

    r = client.get("/api/chat/call-reports", params={"conversation_id": CONVERSATION}, headers=headers)
    assert r.status_code == 200
    assert [(x["call_sid"], x["outcome"], x["text"]) for x in r.json()] == [
        ("CA_conv", "busy", "report CA_conv"),
        ("CA_late", "busy", "report CA_late"),
    ]

    r = client.get(
        "/api/chat/call-reports",
        params={"conversation_id": CONVERSATION, "since": "2026-11-02T13:00:00+02:00"},
        headers=headers,
    )
    assert [x["call_sid"] for x in r.json()] == ["CA_late"]

    r = client.get("/api/chat/call-reports", headers=headers)
    assert [x["call_sid"] for x in r.json()] == ["CA_main"]


def test_call_reports_need_a_user_and_a_valid_conversation():
    client = _make_client()
    assert client.get("/api/chat/call-reports").status_code == 400
    r = client.get("/api/chat/call-reports", params={"conversation_id": "nope"}, headers={"X-Pincer-User": UUID})
    assert r.status_code == 422

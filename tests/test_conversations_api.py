"""Tests for the conversations API endpoints and helper functions."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient

from pincer.api.conversations import _channel_tag, _parse_messages, _preview
from pincer.api.server import create_app
from pincer.memory.base import Memory


def _mem(
    id: str = "abc123",
    user_id: str = "user1",
    content: str = '{"user": "hello", "assistant": "world"}',
    category: str = "exchange",
    tags: list[str] | None = None,
    created_at: float = 1_716_000_000.0,
) -> Memory:
    return Memory(
        id=id,
        user_id=user_id,
        content=content,
        category=category,
        created_at=created_at,
        tags=tags or [f"user:{user_id}", f"category:{category}"],
    )


# ── _parse_messages ────────────────────────────────────────────────────────────


def test_parse_messages_json_both_roles():
    msgs = _parse_messages(json.dumps({"user": "What is 2+2?", "assistant": "4"}))
    assert msgs == [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
    ]


def test_parse_messages_json_user_only():
    msgs = _parse_messages(json.dumps({"user": "just a question"}))
    assert msgs == [{"role": "user", "content": "just a question"}]


def test_parse_messages_json_assistant_only():
    msgs = _parse_messages(json.dumps({"assistant": "just a reply"}))
    assert msgs == [{"role": "assistant", "content": "just a reply"}]


def test_parse_messages_json_empty_dict_falls_through():
    msgs = _parse_messages(json.dumps({"other": "field"}))
    # No user/assistant keys — falls through to prefix or fallback
    assert isinstance(msgs, list)
    assert len(msgs) >= 1


def test_parse_messages_prefix_format():
    content = "User asked: What is Python?\nAssistant replied: A programming language."
    msgs = _parse_messages(content)
    assert {"role": "user", "content": "What is Python?"} in msgs
    assert {"role": "assistant", "content": "A programming language."} in msgs


def test_parse_messages_prefix_multiline_assistant():
    """Multi-line assistant content must not be truncated at first blank line."""
    content = "User asked: Explain gravity\nAssistant replied: Gravity pulls objects.\n\nIt follows F=ma."
    msgs = _parse_messages(content)
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert "It follows F=ma." in assistant["content"]


def test_parse_messages_prefix_user_only():
    content = "User asked: only a question"
    msgs = _parse_messages(content)
    assert any(m["role"] == "user" for m in msgs)


def test_parse_messages_fallback_plain_text():
    msgs = _parse_messages("just plain unstructured text")
    assert msgs == [{"role": "user", "content": "just plain unstructured text"}]


def test_parse_messages_invalid_json_uses_prefix_path():
    content = "User asked: hi\nAssistant replied: hello"
    msgs = _parse_messages(content)
    roles = {m["role"] for m in msgs}
    assert "user" in roles
    assert "assistant" in roles


# ── _channel_tag ──────────────────────────────────────────────────────────────


def test_channel_tag_telegram():
    assert _channel_tag("telegram", "123456") == "user:telegram:123456"


def test_channel_tag_whatsapp():
    assert _channel_tag("whatsapp", "+380991234567") == "user:whatsapp:+380991234567"


# ── _preview ──────────────────────────────────────────────────────────────────


def test_preview_extracts_user_field_from_json():
    mem = _mem(content=json.dumps({"user": "preview text", "assistant": "reply"}))
    assert _preview(mem) == "preview text"


def test_preview_truncates_to_200_chars():
    long_user = "x" * 300
    mem = _mem(content=json.dumps({"user": long_user}))
    assert len(_preview(mem)) == 200


def test_preview_fallback_for_non_json():
    mem = _mem(content="plain old text")
    assert _preview(mem) == "plain old text"


def test_preview_fallback_truncates_long_raw_content():
    mem = _mem(content="y" * 300)
    assert len(_preview(mem)) == 200


# ── API endpoint helpers ────────────────────────────────────────────────────────


def _build_client(monkeypatch, tmp_path, backend=None):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    if backend is not None:
        agent = MagicMock()
        agent._memory = backend
        app.state.agent = agent
    client = TestClient(app)
    return client, app


# ── GET /api/conversations ────────────────────────────────────────────────────


def test_list_conversations_requires_user_id(monkeypatch, tmp_path):
    client, _ = _build_client(monkeypatch, tmp_path)
    resp = client.get("/api/conversations")
    assert resp.status_code == 422


def test_list_conversations_returns_records(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.list_memories.return_value = [_mem()]

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["conversations"]) == 1
    conv = data["conversations"][0]
    assert conv["id"] == "abc123"
    assert conv["category"] == "exchange"
    assert "messages" in conv
    assert "preview" in conv
    assert "created_at" in conv


def test_list_conversations_messages_parsed(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    content = json.dumps({"user": "hi", "assistant": "hello"})
    mock_backend.list_memories.return_value = [_mem(content=content)]

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1")
    assert resp.status_code == 200
    msgs = resp.json()["conversations"][0]["messages"]
    assert {"role": "user", "content": "hi"} in msgs
    assert {"role": "assistant", "content": "hello"} in msgs


def test_list_conversations_channel_filter_passed_to_backend(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.list_memories.return_value = []

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1&channel=telegram&channel_user_id=999")
    assert resp.status_code == 200

    call_kwargs = mock_backend.list_memories.call_args.kwargs
    assert call_kwargs["tags"] == ["user:telegram:999"]
    assert call_kwargs["match_all_tags"] is True
    assert call_kwargs["category"] == "exchange"


def test_list_conversations_channel_without_id_disables_channel_filter(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.list_memories.return_value = []

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1&channel=telegram")
    assert resp.status_code == 200

    call_kwargs = mock_backend.list_memories.call_args.kwargs
    assert call_kwargs["tags"] is None


def test_list_conversations_offset_and_limit_forwarded(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.list_memories.return_value = []

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1&limit=5&offset=10")
    assert resp.status_code == 200

    call_kwargs = mock_backend.list_memories.call_args.kwargs
    assert call_kwargs["limit"] == 5
    assert call_kwargs["offset"] == 10


def test_list_conversations_backend_error_returns_empty(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.list_memories.side_effect = RuntimeError("db gone")

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations?user_id=user1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversations"] == []
    assert data["total"] == 0


# ── GET /api/conversations/:id ────────────────────────────────────────────────


def test_get_conversation_found(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.get_memory.return_value = _mem(id="conv-1")

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations/conv-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "conv-1"
    assert data["category"] == "exchange"
    assert "messages" in data
    assert "tags" in data


def test_get_conversation_not_found(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.get_memory.return_value = None

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations/missing-id")
    assert resp.status_code == 404


def test_get_conversation_wrong_category_returns_404(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.get_memory.return_value = _mem(id="sum-1", category="summary")

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations/sum-1")
    assert resp.status_code == 404


def test_get_conversation_backend_error_returns_503(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    mock_backend.get_memory.side_effect = RuntimeError("backend down")

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations/any-id")
    assert resp.status_code == 503


def test_get_conversation_messages_parsed(monkeypatch, tmp_path):
    mock_backend = AsyncMock()
    content = json.dumps({"user": "question", "assistant": "answer"})
    mock_backend.get_memory.return_value = _mem(id="q-1", content=content)

    client, _ = _build_client(monkeypatch, tmp_path, backend=mock_backend)
    resp = client.get("/api/conversations/q-1")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert {"role": "user", "content": "question"} in msgs
    assert {"role": "assistant", "content": "answer"} in msgs


# ── Lifespan: pre-injected agent skips build_agent_from_settings ─────────────


def test_lifespan_skips_build_when_agent_preinjected(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()

    build_called = {"n": 0}

    async def fake_build():
        build_called["n"] += 1
        return MagicMock()

    monkeypatch.setattr("pincer.api.server.lifespan.__wrapped__", None, raising=False)
    app = create_app()

    pre_agent = MagicMock()
    pre_agent._memory = AsyncMock()
    app.state.agent = pre_agent

    with (
        pytest.MonkeyPatch().context() as mp2,
    ):
        mp2.setattr("pincer.api._deps.build_agent_from_settings", fake_build)
        with TestClient(app):
            pass

    # build_agent_from_settings was NOT called because agent was pre-injected
    assert build_called["n"] == 0
    get_settings_relaxed.cache_clear()

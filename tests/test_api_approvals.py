"""Tests for the web-channel approval flow.

Covers the approvals registry (request/resolve/timeout) and the SSE
+ /approval endpoint plumbing in api.chat.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient  # noqa: E402

from pincer.api import approvals  # noqa: E402
from pincer.api.approvals import (  # noqa: E402
    pending_count,
    request_web_approval,
    reset_send_event,
    resolve_web_approval,
    set_send_event,
)
from pincer.api.server import create_app  # noqa: E402
from pincer.core.agent import StreamChunk, StreamEventType  # noqa: E402

UUID = "11111111-1111-4111-8111-111111111111"
OTHER_UUID = "22222222-2222-4222-8222-222222222222"


def _make_client(*, agent=None, token: str = "", web_chat_token: str = "") -> TestClient:
    from pincer.config import get_settings_relaxed

    old_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp()

    if token:
        os.environ["PINCER_DASHBOARD_TOKEN"] = token
    else:
        os.environ.pop("PINCER_DASHBOARD_TOKEN", None)

    if web_chat_token:
        os.environ["PINCER_WEB_CHAT_TOKEN"] = web_chat_token
    else:
        os.environ.pop("PINCER_WEB_CHAT_TOKEN", None)

    os.chdir(tmpdir)
    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
    finally:
        os.chdir(old_cwd)
        get_settings_relaxed.cache_clear()
        shutil.rmtree(tmpdir, ignore_errors=True)

    if agent is None:
        agent = MagicMock()
    app.state.agent = agent
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_pending_approvals():
    approvals._pending.clear()
    yield
    approvals._pending.clear()


# ─── approvals registry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_web_approval_resolves_when_approved():
    sent: list[tuple[str, dict]] = []

    async def send_event(name, data):
        sent.append((name, data))
        # Schedule the approval to be granted shortly after the SSE frame
        # is emitted. Mirrors the user clicking Approve in the browser.
        approval_id = data["approval_id"]
        asyncio.get_running_loop().call_soon(lambda: resolve_web_approval(approval_id, UUID, True))

    token = set_send_event(send_event)
    try:
        approved = await request_web_approval(
            "google__create_doc",
            {"title": "test"},
            UUID,
            "web",
            timeout=2.0,
        )
    finally:
        reset_send_event(token)

    assert approved is True
    assert sent[0][0] == "approval"
    assert sent[0][1]["tool"] == "google__create_doc"
    assert sent[0][1]["args"] == {"title": "test"}
    assert pending_count() == 0


@pytest.mark.asyncio
async def test_request_web_approval_returns_false_on_deny():
    async def send_event(name, data):
        approval_id = data["approval_id"]
        asyncio.get_running_loop().call_soon(lambda: resolve_web_approval(approval_id, UUID, False))

    token = set_send_event(send_event)
    try:
        approved = await request_web_approval("google__upload_file", {"file_path": "/tmp/x"}, UUID, "web", timeout=2.0)
    finally:
        reset_send_event(token)
    assert approved is False


@pytest.mark.asyncio
async def test_request_web_approval_times_out():
    async def send_event(_name, _data):
        pass  # never resolve

    token = set_send_event(send_event)
    try:
        approved = await request_web_approval("google__create_doc", {}, UUID, "web", timeout=0.05)
    finally:
        reset_send_event(token)
    assert approved is False
    assert pending_count() == 0


@pytest.mark.asyncio
async def test_request_web_approval_denies_non_web_channel():
    """Web approval is web-only; other channels must use their own UI."""
    approved = await request_web_approval("x", {}, UUID, "telegram")
    assert approved is False


@pytest.mark.asyncio
async def test_request_web_approval_denies_when_no_send_event_bound():
    """A web approval with no SSE stream attached fails closed (deny)."""
    approved = await request_web_approval("x", {}, UUID, "web", timeout=0.05)
    assert approved is False


def test_resolve_web_approval_unknown_id_returns_false():
    assert resolve_web_approval("does-not-exist", UUID, True) is False


@pytest.mark.asyncio
async def test_resolve_web_approval_rejects_other_user():
    """A different authenticated user cannot resolve another user's approval."""

    captured: dict[str, str] = {}

    async def send_event(_name, data):
        captured["approval_id"] = data["approval_id"]
        asyncio.get_running_loop().call_later(
            0.02,
            lambda: resolve_web_approval(captured["approval_id"], UUID, True),
        )

    token = set_send_event(send_event)
    try:
        # Spawn the request, give the send_event time to fire, then
        # try to resolve as the wrong user — should return False.
        task = asyncio.create_task(request_web_approval("x", {}, UUID, "web", timeout=2.0))
        await asyncio.sleep(0.005)  # let send_event run
        assert resolve_web_approval(captured["approval_id"], OTHER_UUID, True) is False
        approved = await task
    finally:
        reset_send_event(token)

    # The legitimate user's deferred resolve still wins.
    assert approved is True


def test_sanitize_args_truncates_long_strings_and_handles_bytes():
    from pincer.api.approvals import _sanitize_args

    out = _sanitize_args({"body": "x" * 1000, "blob": b"\x00\x01\x02", "n": 7})
    assert out["body"].endswith("… [truncated]")
    assert len(out["body"]) < 1000
    assert out["blob"].startswith("<bytes len=")
    assert out["n"] == 7


# ─── /api/chat/approval REST endpoint ────────────────────────────────


def test_approval_endpoint_requires_user_header():
    client = _make_client()
    r = client.post("/api/chat/approval", json={"approval_id": "x", "approved": True})
    assert r.status_code == 400


def test_approval_endpoint_returns_404_for_unknown_id():
    client = _make_client()
    r = client.post(
        "/api/chat/approval",
        json={"approval_id": "nope", "approved": True},
        headers={"X-Pincer-User": UUID},
    )
    assert r.status_code == 404


def test_approval_endpoint_resolves_pending_approval():
    """End-to-end: register a pending future, hit /approval, future resolves."""
    loop = asyncio.new_event_loop()
    try:
        fut: asyncio.Future[bool] = loop.create_future()
        approvals._pending["abcd"] = (UUID, fut)

        client = _make_client()
        r = client.post(
            "/api/chat/approval",
            json={"approval_id": "abcd", "approved": True},
            headers={"X-Pincer-User": UUID},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert fut.done() and fut.result() is True
    finally:
        loop.close()


def test_approval_endpoint_rejects_other_user():
    loop = asyncio.new_event_loop()
    try:
        fut: asyncio.Future[bool] = loop.create_future()
        approvals._pending["abcd"] = (UUID, fut)

        client = _make_client()
        r = client.post(
            "/api/chat/approval",
            json={"approval_id": "abcd", "approved": True},
            headers={"X-Pincer-User": OTHER_UUID},
        )
        assert r.status_code == 404
        assert not fut.done()
    finally:
        loop.close()


# ─── /api/chat/stream emits approval SSE events ──────────────────────


def test_chat_stream_forwards_approval_event_from_send_event():
    """When the agent calls the contextvar-bound send_event mid-stream,
    the SSE response forwards it as `event: approval` before the rest
    of the stream continues."""

    async def fake_stream(*_args, **_kwargs):
        # Mid-conversation, the agent's _execute_tool would invoke
        # request_web_approval, which calls _send_event_var.get()(...).
        # Simulate that by reading the contextvar and pushing an approval
        # event ourselves.
        from pincer.api.approvals import _send_event_var

        send = _send_event_var.get()
        assert send is not None, "chat.py should have bound send_event"
        await send(
            "approval",
            {
                "approval_id": "fake-id",
                "tool": "google__create_doc",
                "args": {"title": "Test"},
            },
        )
        yield StreamChunk(StreamEventType.TEXT, "ok")
        yield StreamChunk(StreamEventType.DONE, "ok")

    agent = MagicMock()
    agent.handle_message_stream = fake_stream

    client = _make_client(agent=agent)
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"text": "create a google doc"},
        headers={"X-Pincer-User": UUID},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_raw()).decode()

    assert "event: approval" in body
    assert "google__create_doc" in body
    assert "fake-id" in body
    assert "event: chunk" in body
    assert "event: done" in body

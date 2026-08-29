"""Sprint 11 §6.3/§6.5 — the voice approval broker, the Telegram card, and the REST endpoints."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from pincer.voice import approvals


@pytest.fixture(autouse=True)
def _clean():
    approvals._reset_for_tests()
    yield
    approvals._reset_for_tests()


async def _request(**overrides):
    kwargs = {
        "call_sid": "CA1",
        "tool_name": "google__create_event",
        "summary": "Termin erstellen: Di 18.08. 14:00–14:30, „Beratung“",
        "language": "de",
        "args_preview": {"start": "2026-08-18T14:00:00+02:00", "duration_min": 30, "blob": b"xx"},
        "user_id": "12345",
        "channel": "telegram",
        "timeout_s": 25,
    }
    kwargs.update(overrides)
    return await approvals.request(**kwargs)


async def test_payload_shape_and_sanitized_args():
    shown = []

    async def presenter(req):
        shown.append(req)
        return True

    approvals.set_presenter(presenter)
    req = await _request()
    payload = req.payload()
    assert payload["type"] == "voice_call_action"
    assert payload["call_sid"] == "CA1"
    assert payload["tool_name"] == "google__create_event"
    assert payload["summary_spoken_language"] == "de"
    assert payload["summary"].startswith("Termin erstellen")
    assert payload["args_preview"]["start"] == "2026-08-18T14:00:00+02:00"
    assert payload["args_preview"]["blob"].startswith("<bytes")
    assert payload["expires_at"]
    assert req.extra["presented"] is True and shown == [req]
    assert approvals.pending() == [req]


async def test_resolve_approved_and_denied_only_once():
    approvals.set_presenter(AsyncMock(return_value=True))
    req = await _request()
    assert approvals.resolve(req.approval_id, True)
    assert req.future.result() is True and req.final_state == "approved"
    assert not approvals.resolve(req.approval_id, False)  # already answered
    assert not approvals.resolve("nope", True)


async def test_resolve_rejects_wrong_user():
    approvals.set_presenter(AsyncMock(return_value=True))
    req = await _request(user_id="12345")
    assert not approvals.resolve(req.approval_id, True, by_user_id="999")
    assert req.is_pending
    assert approvals.resolve(req.approval_id, True, by_user_id="12345")


async def test_finalize_edits_card_and_forgets_request():
    finals = []

    async def finalizer(req, state):
        finals.append(state)

    approvals.set_presenter(AsyncMock(return_value=True))
    approvals.set_finalizer(finalizer)
    req = await _request()
    await approvals.finalize(req, "expired")
    assert finals == ["expired"]
    assert req.future.result() is False
    assert approvals.pending() == []


async def test_cancel_for_call_marks_call_ended():
    finals = []

    async def finalizer(req, state):
        finals.append((req.call_sid, state))

    approvals.set_presenter(AsyncMock(return_value=True))
    approvals.set_finalizer(finalizer)
    a = await _request(call_sid="CA1")
    b = await _request(call_sid="CA2")
    assert await approvals.cancel_for_call("CA1") == 1
    assert finals == [("CA1", "call_ended")]
    assert a.final_state == "call_ended" and b.is_pending


async def test_no_presenter_means_not_presented():
    req = await _request()
    assert req.extra["presented"] is False


async def test_presenter_exception_is_contained():
    async def boom(req):
        raise RuntimeError("telegram down")

    approvals.set_presenter(boom)
    req = await _request()
    assert req.extra["presented"] is False


# ── Telegram card ────────────────────────────────────────────────────


async def test_telegram_card_present_and_finalize():
    from pincer.channels.telegram import TelegramChannel

    settings = MagicMock()
    settings.telegram_allowed_users = []
    tg = TelegramChannel(settings)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    bot.edit_message_text = AsyncMock()
    tg._bot = bot

    req = await _request()
    assert await tg.present_voice_approval(req) is True
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["✅ Erlauben", "❌ Ablehnen"]
    assert buttons[0].callback_data == f"vcall_ok:{req.approval_id}"
    assert buttons[1].callback_data == f"vcall_no:{req.approval_id}"
    assert "Termin erstellen" in kwargs["text"]
    assert req.extra["telegram_message_id"] == 77

    await tg.finalize_voice_approval(req, "approved")
    edit = bot.edit_message_text.call_args.kwargs
    assert edit["chat_id"] == 12345 and edit["message_id"] == 77
    assert "Erlaubt" in edit["text"] and edit["reply_markup"] is None

    await tg.finalize_voice_approval(req, "call_ended")
    assert "Anruf beendet" in bot.edit_message_text.call_args.kwargs["text"]


async def test_telegram_card_non_numeric_user_not_presented():
    from pincer.channels.telegram import TelegramChannel

    settings = MagicMock()
    settings.telegram_allowed_users = []
    tg = TelegramChannel(settings)
    tg._bot = MagicMock()
    req = await _request(user_id="canonical-user")
    assert await tg.present_voice_approval(req) is False


# ── REST ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


def test_api_list_and_decide(client):
    loop = asyncio.new_event_loop()
    try:
        approvals.set_presenter(AsyncMock(return_value=True))
        req = loop.run_until_complete(_request(channel="web", user_id="dashboard"))
        listed = client.get("/api/voice/approvals").json()
        assert [a["approval_id"] for a in listed] == [req.approval_id]
        assert listed[0]["type" if "type" in listed[0] else "tool_name"]
        assert client.get("/api/voice/approvals", params={"call_sid": "other"}).json() == []

        decided = client.post(f"/api/voice/approvals/{req.approval_id}", json={"approved": True})
        assert decided.status_code == 200
        assert decided.json()["final_state"] == "approved"
        assert req.future.result() is True
        # second answer / unknown id → 404
        assert client.post(f"/api/voice/approvals/{req.approval_id}", json={"approved": False}).status_code == 404
        assert client.post("/api/voice/approvals/nope", json={"approved": True}).status_code == 404
    finally:
        loop.close()

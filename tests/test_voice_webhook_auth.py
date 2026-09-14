"""Twilio webhook signature enforcement (Sprint 8, T8.1).

Every /voice/* and /api/apps/twilio/* HTTP route must reject an unsigned or
forged request with 403 — including the deprecated aliases, which are just as
public as the current paths.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.voice import twiml_server
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.webhook_auth import (
    WebhookAuthError,
    body_sha256_matches,
    candidate_urls,
    check_replay,
    compute_signature,
    signed_ws_query,
    ws_token,
)

AUTH_TOKEN = "test-twilio-auth-token"


class FakeEngine:
    def __init__(self) -> None:
        self.states: dict[str, CallState] = {}
        self.started: list[str] = []
        self.ended: list[str] = []

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self.states.get(call_sid)

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self.states)

    async def on_call_start(self, call_sid: str, caller: str, direction: CallDirection, **kwargs: Any) -> CallState:
        state = CallState(call_sid=call_sid, direction=direction, caller_number=caller)
        self.states[call_sid] = state
        self.started.append(call_sid)
        return state

    async def end_call(self, call_sid: str) -> None:
        self.ended.append(call_sid)
        self.states.pop(call_sid, None)


def _settings(**overrides: Any) -> MagicMock:
    settings = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = AUTH_TOKEN
    settings.voice_webhook_validate = True
    settings.voice_ws_auth_required = True
    settings.voice_signature_max_age_s = 300
    settings.voice_webhook_base_url = "https://voice.example.com"
    settings.voice_allowed_callers = "*"
    settings.voice_engine = "conversation_relay"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture
def client(request) -> TestClient:
    overrides = getattr(request, "param", {}) or {}
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    app.include_router(twiml_server.voice_router)
    twiml_server.init_voice_routes(FakeEngine(), _settings(**overrides))
    return TestClient(app)


def _sign(path: str, form: dict[str, str]) -> str:
    return compute_signature(AUTH_TOKEN, f"http://testserver{path}", form)


SIGNED_ROUTES = [
    ("/api/apps/twilio/webhook", {"CallSid": "CA1", "From": "+15551110000", "To": "+15552220000"}),
    ("/api/apps/twilio/status", {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "12"}),
    ("/api/apps/twilio/fallback", {"CallSid": "CA1", "ErrorCode": "64111"}),
    ("/api/apps/twilio/transfer-result", {"CallSid": "CA1", "DialCallStatus": "completed"}),
    # Deprecated aliases are exactly as exposed as the current paths.
    ("/voice/webhook", {"CallSid": "CA1", "From": "+15551110000", "To": "+15552220000"}),
    ("/voice/status", {"CallSid": "CA1", "CallStatus": "completed"}),
    ("/voice/fallback", {"CallSid": "CA1", "ErrorCode": "64111"}),
]


# ── Enforcement on every route ───────────────────────────────────────


@pytest.mark.parametrize(("path", "form"), SIGNED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_unsigned_request_is_rejected(client, path, form):
    assert client.post(path, data=form).status_code == 403


@pytest.mark.parametrize(("path", "form"), SIGNED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_forged_signature_is_rejected(client, path, form):
    response = client.post(path, data=form, headers={"X-Twilio-Signature": "bm90LWEtcmVhbC1zaWc="})
    assert response.status_code == 403


@pytest.mark.parametrize(("path", "form"), SIGNED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_correctly_signed_request_is_accepted(client, path, form):
    response = client.post(path, data=form, headers={"X-Twilio-Signature": _sign(path, form)})
    assert response.status_code == 200


def test_tampered_body_invalidates_the_signature(client):
    """A signature captured from a real webhook cannot be replayed onto a
    different call SID — the body is part of what is signed."""
    path = "/api/apps/twilio/status"
    original = {"CallSid": "CA1", "CallStatus": "completed"}
    signature = _sign(path, original)
    tampered = {"CallSid": "CA_ATTACKER", "CallStatus": "completed"}
    assert client.post(path, data=tampered, headers={"X-Twilio-Signature": signature}).status_code == 403


def test_relay_webhook_json_requires_signature(client):
    body = {"CallSid": "CA1", "type": "prompt", "voicePrompt": "hello"}
    assert client.post("/api/apps/twilio/relay-webhook", json=body).status_code == 403


def test_legacy_relay_webhook_json_requires_signature(client):
    body = {"CallSid": "CA1", "type": "prompt", "voicePrompt": "hello"}
    assert client.post("/voice/relay-webhook", json=body).status_code == 403


def test_health_endpoint_stays_public(client):
    """Twilio's own console health probe has no signature to give."""
    assert client.get("/api/apps/twilio/health").status_code == 200


def test_uninitialized_routes_fail_closed(monkeypatch):
    """Routes mounted while `init_voice_routes` has not run (settings=None)
    must refuse — the Bearer-auth exemption's replacement may never be absent,
    so an uninitialized voice surface rejects rather than running unsigned."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(twiml_server, "_settings", None)
    monkeypatch.setattr(twiml_server, "_engine", None)
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    client = TestClient(app)

    form = {"CallSid": "CA1", "CallStatus": "completed"}
    assert client.post("/api/apps/twilio/status", data=form).status_code == 403
    signed = compute_signature("", "http://testserver/api/apps/twilio/status", form)
    assert client.post("/api/apps/twilio/status", data=form, headers={"X-Twilio-Signature": signed}).status_code == 403

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/apps/twilio/relay"):
        pass  # pragma: no cover — refused before accept


@pytest.mark.parametrize("client", [{"voice_webhook_validate": False}], indirect=True)
def test_validation_can_be_disabled_for_dev(client):
    """The escape hatch exists but is explicit — doctor reports it CRITICAL."""
    assert client.post("/api/apps/twilio/status", data={"CallSid": "CA1", "CallStatus": "completed"}).status_code == 200


def test_missing_auth_token_skips_validation(client_without_token=None):
    """A box with no Twilio account cannot verify anything; it also cannot
    receive real webhooks. Doctor flags this state CRITICAL when voice is on."""
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    settings = _settings()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    twiml_server.init_voice_routes(FakeEngine(), settings)
    client = TestClient(app)
    assert client.post("/api/apps/twilio/status", data={"CallSid": "CA1", "CallStatus": "completed"}).status_code == 200


# ── Signature primitives ─────────────────────────────────────────────


def test_compute_signature_matches_the_twilio_sdk():
    """Cross-check against Twilio's own RequestValidator.

    Our validator is hand-rolled (the SDK is an optional extra, and we need to
    try several proxy-corrected URL spellings), so the digest must be proven
    byte-identical to the vendor implementation rather than merely self-consistent.
    """
    validator = pytest.importorskip("twilio.request_validator").RequestValidator("12345")
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert compute_signature("12345", url, params) == validator.compute_signature(url, params)


def test_signature_covers_the_url_not_just_the_body():
    params = {"CallSid": "CA1"}
    assert compute_signature(AUTH_TOKEN, "https://a.example/x", params) != compute_signature(
        AUTH_TOKEN, "https://a.example/y", params
    )


def test_candidate_urls_include_the_proxy_corrected_form():
    """Behind Caddy the ASGI URL is http://internal — Twilio signed the public
    https URL. Both spellings must be candidates or every webhook 403s."""
    request = MagicMock()
    request.url.path = "/api/apps/twilio/status"
    request.url.query = ""
    request.__str__ = lambda self: "http://10.0.0.5:8080/api/apps/twilio/status"  # noqa: ARG005
    request.headers = {"x-forwarded-proto": "https", "x-forwarded-host": "voice.example.com"}
    urls = candidate_urls(request, _settings())
    assert "https://voice.example.com/api/apps/twilio/status" in urls


def test_body_sha256_guard():
    body = b'{"CallSid":"CA1"}'
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    assert body_sha256_matches(f"https://x/y?bodySHA256={digest}", body)
    assert not body_sha256_matches(f"https://x/y?bodySHA256={digest}", b"tampered")
    assert body_sha256_matches("https://x/y", body)  # no bodySHA256 -> nothing to check


# ── Replay guard ─────────────────────────────────────────────────────


def test_replay_guard_accepts_fresh_and_untimestamped():
    check_replay({}, 300)  # Twilio form webhooks carry no timestamp
    check_replay({"t": str(int(time.time()))}, 300)


def test_replay_guard_rejects_stale_request():
    with pytest.raises(WebhookAuthError, match="stale"):
        check_replay({"t": str(int(time.time()) - 600)}, 300)


def test_replay_guard_rejects_far_future_request():
    with pytest.raises(WebhookAuthError, match="future"):
        check_replay({"t": str(int(time.time()) + 600)}, 300)


def test_replay_guard_accepts_millisecond_timestamps():
    check_replay({"Timestamp": str(int(time.time() * 1000))}, 300)


# ── WebSocket token ──────────────────────────────────────────────────


def test_ws_token_is_path_and_time_bound():
    ts = int(time.time())
    assert ws_token(AUTH_TOKEN, "/a", ts) != ws_token(AUTH_TOKEN, "/b", ts)
    assert ws_token(AUTH_TOKEN, "/a", ts) != ws_token(AUTH_TOKEN, "/a", ts + 1)
    assert ws_token(AUTH_TOKEN, "/a", ts) != ws_token("other-secret", "/a", ts)


def test_signed_ws_query_is_empty_when_auth_disabled():
    assert signed_ws_query(_settings(voice_ws_auth_required=False), "/x") == ""


def test_signed_ws_query_is_empty_without_a_secret():
    settings = _settings()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    assert signed_ws_query(settings, "/x") == ""


def test_twiml_relay_url_carries_an_xml_escaped_token():
    """The `&` between t= and s= must be `&amp;` or Twilio rejects the TwiML."""
    from pincer.voice.twiml_builder import build_connect_twiml

    twiml = build_connect_twiml(_settings(), direction="outbound", language="en", counterparty="+15551110000")
    assert "?t=" in twiml
    assert "&amp;s=" in twiml
    assert "&s=" not in twiml.replace("&amp;s=", "")

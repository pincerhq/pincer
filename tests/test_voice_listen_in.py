"""Live listen-in (Sprint 15): TwiML fork, MonitorHub, monitor ingress, listener egress.

The audio source is a separate Twilio `<Start><Stream>` fork, so these tests
exercise the hub and both WebSocket surfaces without any conversation engine.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from pincer.api import voice as voice_api
from pincer.api.auth_guard import AuthGuard
from pincer.security.doctor import CheckStatus, SecurityDoctor
from pincer.voice import twiml_server
from pincer.voice.compliance import (
    MONITOR_ANNOUNCEMENT_DE,
    MONITOR_ANNOUNCEMENT_EN,
    build_call_opening,
    get_monitoring_announcement,
)
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.monitor import (
    CLOSE_CAPACITY,
    CLOSE_UNAVAILABLE,
    END_CALL_ENDED,
    END_CAPACITY,
    END_UNAVAILABLE,
    ListenerCapacityError,
    MonitorHub,
    MonitorUnavailableError,
    get_monitor_hub,
    parse_monitor_frame,
    reset_monitor_hub_for_tests,
)
from pincer.voice.twiml_builder import build_connect_twiml
from pincer.voice.webhook_auth import WS_MONITOR_PATH, signed_ws_query

TWILIO_TOKEN = "test-twilio-auth-token"
DASHBOARD_TOKEN = "dash-secret-token"
MONITOR_PATH = "/api/apps/twilio/monitor"
LISTEN_PATH = "/api/voice/listen"


# ── Settings & app fixtures ──────────────────────────────────────────


def _settings(**overrides: Any) -> SimpleNamespace:
    """Plain-attribute settings (no MagicMock: every flag is a real bool)."""
    base: dict[str, Any] = {
        "voice_enabled": True,
        "voice_engine": "conversation_relay",
        "voice_webhook_base_url": "https://voice.example.com",
        "voice_default_language": "en",
        "voice_supported_languages": "en,de,uk",
        "voice_language": "en-US",
        "voice_de_formality": "sie",
        "voice_consent_mode": "none",
        "voice_recording_enabled": False,
        "voice_consent_language": "",
        "voice_assistant_name": "",
        "voice_assistant_org": "",
        "voice_assistant_owner": "",
        "voice_intro_text": "",
        "elevenlabs_voice_id": "",
        "elevenlabs_voice_id_en": "",
        "elevenlabs_voice_id_de": "",
        "elevenlabs_voice_id_uk": "",
        "elevenlabs_model": "eleven_flash_v2_5",
        "cr_tts_provider": "",
        "receptionist_enabled": False,
        "inbound_recording": False,
        "voice_outbound_enabled": False,
        "twilio_auth_token": SecretStr(TWILIO_TOKEN),
        "voice_ws_auth_required": True,
        "voice_webhook_validate": True,
        "voice_signature_max_age_s": 300,
        "dashboard_token": SecretStr(""),
        "web_chat_token": SecretStr(""),
        "listen_in_enabled": True,
        "listen_in_max_listeners": 2,
        "listen_in_announce": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeEngine:
    def __init__(self) -> None:
        self.states: dict[str, CallState] = {}

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self.states.get(call_sid)

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self.states)


@pytest.fixture(autouse=True)
def _fresh_hub():
    """Fresh hub per test, and the twiml_server engine/settings globals restored
    afterwards (the API's `_get_engine()` reads them — a leaked FakeEngine
    would show phantom active calls to later test modules)."""
    previous = (twiml_server._engine, twiml_server._settings)
    reset_monitor_hub_for_tests()
    yield
    reset_monitor_hub_for_tests()
    twiml_server._engine, twiml_server._settings = previous


def _make_client(monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace, engine: Any | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    app.include_router(twiml_server.voice_router)
    app.include_router(voice_api.router)
    app.state.auth_guard = AuthGuard(max_failures=3, lockout_seconds=60)
    twiml_server.init_voice_routes(engine or FakeEngine(), settings)  # type: ignore[arg-type]
    monkeypatch.setattr(voice_api, "get_settings_relaxed", lambda: settings)
    return TestClient(app)


@pytest.fixture
def settings() -> SimpleNamespace:
    return _settings()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace) -> TestClient:
    return _make_client(monkeypatch, settings)


def _monitor_url(call_sid: str, settings: SimpleNamespace) -> str:
    return f"{MONITOR_PATH}/{call_sid}" + signed_ws_query(settings, WS_MONITOR_PATH)


def _start_event(call_sid: str, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": stream_sid,
            "start": {"callSid": call_sid, "streamSid": stream_sid, "tracks": ["inbound_track", "outbound_track"]},
        }
    )


def _media_event(track: str, payload: str, ts: str = "1000") -> str:
    return json.dumps(
        {"event": "media", "streamSid": "MZ123", "media": {"track": track, "payload": payload, "timestamp": ts}}
    )


def _ulaw(n: int = 160, byte: int = 0xFF) -> str:
    return base64.b64encode(bytes([byte]) * n).decode()


# ── TwiML: the fork (§3.1) ───────────────────────────────────────────


class TestTwimlFork:
    FORK_RE = re.compile(
        r'^<\?xml version="1\.0" encoding="UTF-8"\?><Response>'
        r'<Start><Stream url="wss://voice\.example\.com/api/apps/twilio/monitor/(?P<sid>[^?"]+)\?t=\d+&amp;s=[\w-]+" '
        r'track="both_tracks" /></Start>'
    )

    @pytest.mark.parametrize("engine", ["conversation_relay", "media_streams"])
    @pytest.mark.parametrize("direction", ["inbound", "outbound"])
    def test_enabled_prepends_monitor_fork(self, engine: str, direction: str) -> None:
        s = _settings(voice_engine=engine, listen_in_enabled=True)
        twiml = build_connect_twiml(s, call_sid="CA_fork" if direction == "inbound" else "", direction=direction)
        m = self.FORK_RE.match(twiml)
        assert m, twiml
        # Inbound: the real SID. Outbound: built before the call exists.
        assert m.group("sid") == ("CA_fork" if direction == "inbound" else "{CallSid}")
        # The fork never replaces the conversation element.
        assert "<Connect>" in twiml
        assert twiml.count("<Start>") == 1

    @pytest.mark.parametrize("engine", ["conversation_relay", "media_streams"])
    @pytest.mark.parametrize("direction", ["inbound", "outbound"])
    def test_disabled_emits_no_fork_at_all(self, engine: str, direction: str) -> None:
        s = _settings(voice_engine=engine, listen_in_enabled=False)
        twiml = build_connect_twiml(s, call_sid="CA_nofork", direction=direction)
        assert "<Start>" not in twiml
        assert "monitor" not in twiml

    def test_fork_precedes_announcement_on_media_streams(self) -> None:
        """The listener must hear the announcement too: the fork starts first."""
        s = _settings(voice_engine="media_streams", voice_consent_mode="two_party", voice_recording_enabled=True)
        twiml = build_connect_twiml(s, call_sid="CA1", direction="inbound")
        assert twiml.index("<Start>") < twiml.index("<Say")

    def test_fork_url_is_signed_for_monitor_surface(self) -> None:
        twiml = build_connect_twiml(_settings(), call_sid="CA1")
        query = re.search(r'monitor/CA1\?(t=\d+&amp;s=[\w-]+)"', twiml)
        assert query
        expected = signed_ws_query(_settings(), WS_MONITOR_PATH)  # same second, same token
        assert query.group(1).replace("&amp;", "&") == expected.lstrip("?")


# ── Compliance: announcement + doctor gate (§2) ──────────────────────


class TestAnnouncement:
    def test_opening_gains_monitoring_notice_per_language(self) -> None:
        s = _settings(voice_assistant_name="Pincer")
        assert build_call_opening(s, "+15550001111", language="en").endswith(MONITOR_ANNOUNCEMENT_EN)
        assert build_call_opening(s, "+491761234567", language="de").endswith(MONITOR_ANNOUNCEMENT_DE)
        assert MONITOR_ANNOUNCEMENT_DE == "Dieses Gespräch kann zur Qualitätssicherung mitgehört werden."

    def test_no_notice_when_disabled_or_silenced(self) -> None:
        assert get_monitoring_announcement(_settings(listen_in_enabled=False), "de") == ""
        assert get_monitoring_announcement(_settings(listen_in_announce=False), "de") == ""
        assert build_call_opening(_settings(listen_in_enabled=False), "+1", language="en") == ""

    def test_notice_follows_recording_announcement(self) -> None:
        s = _settings(voice_consent_mode="two_party", voice_recording_enabled=True)
        opening = build_call_opening(s, "+491761234567", language="de")
        assert opening.index("aufgezeichnet") < opening.index("mitgehört")


class TestDoctorAnnounceGate:
    def test_doctor_announce_gate(self) -> None:
        doc = SecurityDoctor()
        assert doc._check_listen_in_announce(_settings(listen_in_enabled=False)).status == CheckStatus.SKIPPED
        assert doc._check_listen_in_announce(_settings()).status == CheckStatus.PASS
        silenced = _settings(listen_in_announce=False)
        assert doc._check_listen_in_announce(silenced).status == CheckStatus.CRITICAL
        covered = _settings(listen_in_announce=False, voice_consent_mode="two_party", voice_recording_enabled=True)
        assert doc._check_listen_in_announce(covered).status == CheckStatus.PASS
        # two_party alone is not enough — the recording announcement must actually play
        half = _settings(listen_in_announce=False, voice_consent_mode="two_party", voice_recording_enabled=False)
        assert doc._check_listen_in_announce(half).status == CheckStatus.CRITICAL


# ── MonitorHub unit (§3.2) ───────────────────────────────────────────


class TestMonitorHub:
    async def test_backpressure_drops_not_grows(self) -> None:
        hub = MonitorHub(max_listeners=2, queue_size=50)
        hub.attach_source("CA1", object())
        slow = await hub.subscribe("CA1", "dashboard")  # never consumes
        for i in range(500):
            hub.publish("CA1", "inbound", f"f{i}")
        assert slow.queue.qsize() == 50  # bounded: ~1 s of audio
        assert slow.dropped == 450
        assert slow.frames == 500
        # drop-OLDEST: the newest frames survive
        assert slow.queue.get_nowait()["payload"] == "f450"

    async def test_listener_cap(self) -> None:
        hub = MonitorHub(max_listeners=2)
        hub.attach_source("CA1", object())
        a = await hub.subscribe("CA1", "u1")
        await hub.subscribe("CA1", "u2")
        with pytest.raises(ListenerCapacityError):
            await hub.subscribe("CA1", "u3")
        hub.unsubscribe(a)
        await hub.subscribe("CA1", "u3")  # a slot freed up
        assert hub.listener_count("CA1") == 2

    async def test_subscribe_requires_source(self) -> None:
        hub = MonitorHub()
        with pytest.raises(MonitorUnavailableError):
            await hub.subscribe("CA_unknown", "u")

    async def test_end_notifies_every_subscriber_once(self) -> None:
        hub = MonitorHub()
        hub.attach_source("CA1", object())
        subs = [await hub.subscribe("CA1", f"u{i}") for i in range(2)]
        hub.publish("CA1", "inbound", "x")
        assert hub.end("CA1", END_CALL_ENDED) == 2
        assert hub.end("CA1", END_CALL_ENDED) == 0  # idempotent
        for sub in subs:
            frames = []
            while not sub.queue.empty():
                frames.append(sub.queue.get_nowait())
            assert frames[-1] == {"type": "end", "reason": END_CALL_ENDED}
            assert sum(1 for f in frames if f["type"] == "end") == 1
        assert not hub.source_attached("CA1")
        assert hub.listener_count("CA1") == 0

    def test_parse_monitor_frame_normalises_tracks(self) -> None:
        assert parse_monitor_frame({"event": "media", "media": {"track": "outbound_track", "payload": "AA=="}}) == (
            "media",
            "outbound",
            "AA==",
            None,
        )
        assert parse_monitor_frame({"event": "start"}) is None
        assert parse_monitor_frame({"event": "media", "media": {"payload": ""}}) is None

    def test_singleton(self) -> None:
        assert get_monitor_hub() is get_monitor_hub()
        get_monitor_hub().configure(_settings(listen_in_max_listeners=5))
        assert get_monitor_hub().max_listeners == 5


# ── Monitor ingress (§3.2) ───────────────────────────────────────────


class TestMonitorIngress:
    def test_monitor_requires_twilio_signature(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(f"{MONITOR_PATH}/CA1"):
            pass  # pragma: no cover — the handshake never completes
        assert exc.value.code == 1008
        assert not get_monitor_hub().source_attached("CA1")

    def test_monitor_refused_when_listen_in_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _settings(listen_in_enabled=False)
        client = _make_client(monkeypatch, s)
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(_monitor_url("CA1", s)):
            pass  # pragma: no cover
        assert exc.value.code == 1008

    def test_start_event_sid_wins_over_placeholder_path(self, client: TestClient, settings: SimpleNamespace) -> None:
        """Outbound TwiML carries {CallSid}; the start event names the real call."""
        with client.websocket_connect(_monitor_url("{CallSid}", settings)) as ws:
            ws.send_text(_start_event("CA_real"))
            ws.send_text(_media_event("inbound", _ulaw()))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA_real"))
        assert not get_monitor_hub().source_attached("CA_real")  # ended on disconnect

    def test_legacy_alias(self, client: TestClient, settings: SimpleNamespace) -> None:
        url = "/voice/monitor/CA_legacy" + signed_ws_query(settings, WS_MONITOR_PATH)
        with client.websocket_connect(url) as ws:
            ws.send_text(_start_event("CA_legacy"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA_legacy"))


def _wait_until(pred: Any, timeout: float = 2.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ── Listener egress (§3.3) ───────────────────────────────────────────


class TestListenerEgress:
    def test_listen_requires_auth_before_accept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _settings(dashboard_token=SecretStr(DASHBOARD_TOKEN))
        client = _make_client(monkeypatch, s)
        # No token: denied before accept — never a single frame, 401 on the upgrade.
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(f"{LISTEN_PATH}/CA1"):
            pass  # pragma: no cover
        if isinstance(exc.value, WebSocketDenialResponse):
            assert exc.value.status_code == 401
        # Wrong token: same.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"{LISTEN_PATH}/CA1", headers={"Authorization": "Bearer nope"}),
        ):
            pass  # pragma: no cover
        # Right token (header) is accepted — the socket opens (and says "unavailable": no source).
        with client.websocket_connect(
            f"{LISTEN_PATH}/CA1", headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
        ) as ws:
            assert ws.receive_json()["reason"] == END_UNAVAILABLE
        # Right token as ?token= (browsers cannot set the header on a WebSocket).
        with client.websocket_connect(f"{LISTEN_PATH}/CA1?token={DASHBOARD_TOKEN}") as ws:
            assert ws.receive_json()["reason"] == END_UNAVAILABLE

    def test_listen_auth_failures_hit_brute_force_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _settings(dashboard_token=SecretStr(DASHBOARD_TOKEN))
        client = _make_client(monkeypatch, s)
        for _ in range(5):
            with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"{LISTEN_PATH}/CA1?token=bad"):
                pass  # pragma: no cover
        # Locked out now: even the right token is refused until the lockout expires.
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(f"{LISTEN_PATH}/CA1?token={DASHBOARD_TOKEN}"),
        ):
            pass  # pragma: no cover
        if isinstance(exc.value, WebSocketDenialResponse):
            assert exc.value.status_code == 429

    def test_unavailable_without_source(self, client: TestClient) -> None:
        with client.websocket_connect(f"{LISTEN_PATH}/CA_none") as ws:
            assert ws.receive_json() == {"type": "end", "reason": END_UNAVAILABLE}
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == CLOSE_UNAVAILABLE

    def test_fanout_two_listeners(self, client: TestClient, settings: SimpleNamespace) -> None:
        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            with (
                client.websocket_connect(f"{LISTEN_PATH}/CA1") as a,
                client.websocket_connect(f"{LISTEN_PATH}/CA1") as b,
            ):
                start_a = a.receive_json()
                start_b = b.receive_json()
                assert start_a["type"] == "start" and start_a["call_sid"] == "CA1"
                assert start_a["tracks"] == ["inbound", "outbound"]
                assert start_a["codec"] == "mulaw" and start_a["sample_rate"] == 8000
                assert start_a["listener_count"] == 1 and start_b["listener_count"] == 2

                caller = _ulaw(byte=0x12)
                agent = _ulaw(byte=0x34)
                src.send_text(_media_event("inbound", caller, "20"))
                src.send_text(_media_event("outbound", agent, "40"))

                for ws in (a, b):
                    f1 = ws.receive_json()
                    f2 = ws.receive_json()
                    assert f1 == {"type": "media", "track": "inbound", "payload": caller, "ts": "20"}
                    assert f2 == {"type": "media", "track": "outbound", "payload": agent, "ts": "40"}
                assert get_monitor_hub().listener_count("CA1") == 2
            assert _wait_until(lambda: get_monitor_hub().listener_count("CA1") == 0)

    def test_listener_cap_4001(self, client: TestClient, settings: SimpleNamespace) -> None:
        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            with (
                client.websocket_connect(f"{LISTEN_PATH}/CA1") as a,
                client.websocket_connect(f"{LISTEN_PATH}/CA1") as b,
            ):
                a.receive_json()
                b.receive_json()
                with client.websocket_connect(f"{LISTEN_PATH}/CA1") as c:
                    assert c.receive_json() == {"type": "end", "reason": END_CAPACITY}
                    with pytest.raises(WebSocketDisconnect) as exc:
                        c.receive_json()
                    assert exc.value.code == CLOSE_CAPACITY
                assert get_monitor_hub().listener_count("CA1") == 2

    def test_call_end_closes_listeners_with_reason(self, client: TestClient, settings: SimpleNamespace) -> None:
        src = client.websocket_connect(_monitor_url("CA1", settings))
        src.__enter__()
        src.send_text(_start_event("CA1"))
        assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
        with client.websocket_connect(f"{LISTEN_PATH}/CA1") as a:
            assert a.receive_json()["type"] == "start"
            src.send_text(_media_event("inbound", _ulaw()))
            assert a.receive_json()["type"] == "media"
            # Twilio ends the fork when the call ends.
            src.send_text(json.dumps({"event": "stop", "streamSid": "MZ123"}))
            assert a.receive_json() == {"type": "end", "reason": END_CALL_ENDED}
            with pytest.raises(WebSocketDisconnect) as exc:
                a.receive_json()
            assert exc.value.code == 1000
        src.__exit__(None, None, None)
        assert not get_monitor_hub().source_attached("CA1")

    def test_engine_call_end_hook_closes_listeners(self, client: TestClient, settings: SimpleNamespace) -> None:
        """Belt and braces: the engine's call-end path ends the fork too."""
        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            with client.websocket_connect(f"{LISTEN_PATH}/CA1") as a:
                a.receive_json()
                get_monitor_hub().end("CA1")  # what phone_calls._handle_call_end does
                assert a.receive_json() == {"type": "end", "reason": END_CALL_ENDED}

    def test_no_audio_persisted(
        self, client: TestClient, settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§2.2: frames are relayed, never written — no file or DB write on the hot path."""
        writes: list[str] = []
        real_open = builtins.open

        def watched_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(ch in str(mode) for ch in "wax+"):
                writes.append(f"open({file!r}, {mode!r})")
            return real_open(file, mode, *args, **kwargs)

        def watched_connect(*args: Any, **kwargs: Any) -> Any:
            writes.append(f"aiosqlite.connect{args!r}")
            raise AssertionError("DB touched on the listen-in hot path")

        monkeypatch.setattr(builtins, "open", watched_open)
        monkeypatch.setattr(aiosqlite, "connect", watched_connect)

        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            with client.websocket_connect(f"{LISTEN_PATH}/CA1") as a:
                a.receive_json()
                for i in range(20):
                    src.send_text(_media_event("inbound" if i % 2 else "outbound", _ulaw(byte=i)))
                for _ in range(20):
                    assert a.receive_json()["type"] == "media"
        assert writes == []

    def test_audit_session_written(
        self, client: TestClient, settings: SimpleNamespace, _isolate_audit_logger: Any
    ) -> None:
        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            with client.websocket_connect(f"{LISTEN_PATH}/CA1") as a:
                a.receive_json()
                src.send_text(_media_event("inbound", _ulaw()))
                a.receive_json()
            # listener hung up → one session row
            assert _wait_until(
                lambda: any(e.action.value == "listen_in_session" for e in _isolate_audit_logger.entries)
            )
        rows = [e for e in _isolate_audit_logger.entries if e.action.value == "listen_in_session"]
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == "dashboard"
        assert row.channel == "voice"
        meta = row.metadata
        assert meta["call_sid"] == "CA1" and meta["user"] == "dashboard"
        assert meta["reason"] == "stopped" and meta["frames"] == 1
        for key in ("started_at", "ended_at", "duration_s"):
            assert key in meta
        started = datetime.fromisoformat(meta["started_at"])
        assert started.tzinfo is not None and started <= datetime.now(UTC)
        # no audio in the audit row either
        assert "payload" not in json.dumps(meta)

    def test_listen_when_feature_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch, _settings(listen_in_enabled=False))
        with client.websocket_connect(f"{LISTEN_PATH}/CA1") as ws:
            assert ws.receive_json() == {"type": "end", "reason": END_UNAVAILABLE}


# ── API fields (§3.4) ────────────────────────────────────────────────


class TestActiveApiFields:
    def test_active_gains_listen_fields(self, monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace) -> None:
        engine = FakeEngine()
        engine.states["CA1"] = CallState(call_sid="CA1", direction=CallDirection.INBOUND, caller_number="+1")
        engine.states["CA2"] = CallState(call_sid="CA2", direction=CallDirection.OUTBOUND, caller_number="+1")
        client = _make_client(monkeypatch, settings, engine)
        monkeypatch.setattr(voice_api, "_get_engine", lambda: engine)

        rows = {r["call_sid"]: r for r in client.get("/api/voice/active").json()}
        assert rows["CA1"]["listen_available"] is False and rows["CA1"]["listener_count"] == 0

        with client.websocket_connect(_monitor_url("CA1", settings)) as src:
            src.send_text(_start_event("CA1"))
            assert _wait_until(lambda: get_monitor_hub().source_attached("CA1"))
            rows = {r["call_sid"]: r for r in client.get("/api/voice/active").json()}
            assert rows["CA1"]["listen_available"] is True
            assert rows["CA1"]["listener_capacity"] == 2
            assert rows["CA2"]["listen_available"] is False  # no fork attached for CA2
            with client.websocket_connect(f"{LISTEN_PATH}/CA1") as a:
                a.receive_json()
                rows = {r["call_sid"]: r for r in client.get("/api/voice/active").json()}
                assert rows["CA1"]["listener_count"] == 1

        status = client.get("/api/voice/status").json()
        assert status["listen_in_enabled"] is True

    def test_listen_available_false_when_feature_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _settings(listen_in_enabled=False)
        engine = FakeEngine()
        engine.states["CA1"] = CallState(call_sid="CA1", direction=CallDirection.INBOUND, caller_number="+1")
        client = _make_client(monkeypatch, s, engine)
        monkeypatch.setattr(voice_api, "_get_engine", lambda: engine)
        get_monitor_hub().attach_source("CA1", object())  # even with a stray source
        rows = client.get("/api/voice/active").json()
        assert rows[0]["listen_available"] is False


# ── Settings defaults (§2) ───────────────────────────────────────────


def test_settings_defaults_are_dark() -> None:
    from pincer.config.channels import ChannelSettings

    fields = ChannelSettings.model_fields
    assert fields["listen_in_enabled"].default is False
    assert fields["listen_in_max_listeners"].default == 2
    assert fields["listen_in_announce"].default is True


async def test_hub_queue_is_asyncio_safe_across_tasks() -> None:
    """Publish from one task, consume from another — no frame reordering."""
    hub = MonitorHub(queue_size=10)
    hub.attach_source("CA1", object())
    sub = await hub.subscribe("CA1", "u")

    async def producer() -> None:
        for i in range(10):
            hub.publish("CA1", "inbound", str(i))
            await asyncio.sleep(0)
        hub.end("CA1")

    asyncio.get_running_loop().create_task(producer())
    seen: list[str] = []
    while True:
        item = await asyncio.wait_for(sub.queue.get(), timeout=2)
        if item["type"] == "end":
            break
        seen.append(item["payload"])
    assert seen == [str(i) for i in range(10)]

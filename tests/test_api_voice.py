"""Tests for the Voice REST API (/api/voice/*)."""

import os
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from fastapi.testclient import TestClient

from pincer.api.server import create_app
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.retention import VOICE_TABLES_SQL


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


async def _seed_db(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(VOICE_TABLES_SQL)
        started = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
        for sid, direction, offset_min, ended in [
            ("CA001", "inbound", 0, True),
            ("CA002", "outbound", 5, True),
            ("CA003", "outbound", 10, False),
        ]:
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, from_number, to_number, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    direction,
                    "+15550001111",
                    "+15550002222",
                    (started + timedelta(minutes=offset_min)).isoformat(),
                    (started + timedelta(minutes=offset_min, seconds=90)).isoformat() if ended else None,
                ),
            )
        await db.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, confidence, is_final, state, timestamp) "
            "VALUES ('CA001', 'caller', 'My SSN is 123-45-6789 thanks', 0.9, 1, 'conversing', ?)",
            (started.isoformat(),),
        )
        await db.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, confidence, is_final, state, timestamp) "
            "VALUES ('CA001', 'agent', 'partial utterance', 1.0, 0, 'conversing', ?)",
            ((started + timedelta(seconds=5)).isoformat(),),
        )
        await db.execute(
            "INSERT INTO call_actions (call_id, action_type, tool_name, input_summary, output_summary, "
            "user_confirmed, timestamp) VALUES ('CA001', 'tool_call', 'get_weather', 'Berlin', 'Sunny', 1, ?)",
            ((started + timedelta(seconds=10)).isoformat(),),
        )
        await db.commit()


class _FakeEngine:
    def __init__(self):
        self._calls = {
            "CA-live": CallState(
                call_sid="CA-live",
                direction=CallDirection.OUTBOUND,
                caller_number="+15550001111",
                target_number="+15550003333",
                target_name="Dentist",
                purpose="Reschedule appointment",
            )
        }

    def get_active_calls(self):
        return dict(self._calls)


@pytest.fixture
def fake_engine(monkeypatch):
    engine = _FakeEngine()
    monkeypatch.setattr("pincer.voice.twiml_server._engine", engine)
    yield engine
    # monkeypatch restores _engine automatically


def test_status_shape(client):
    r = client.get("/api/voice/status")
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {
        "enabled",
        "engine",
        "language",
        "consent_mode",
        "outbound_enabled",
        "voice_configured",
        "webhook_base_configured",
        "active_call_count",
    }
    assert data["active_call_count"] == 0


def test_active_empty_without_engine(client):
    r = client.get("/api/voice/active")
    assert r.status_code == 200
    assert r.json() == []


def test_active_with_live_call(client, fake_engine):
    r = client.get("/api/voice/active")
    assert r.status_code == 200
    calls = r.json()
    assert len(calls) == 1
    call = calls[0]
    assert call["call_sid"] == "CA-live"
    assert call["direction"] == "outbound"
    assert call["target_name"] == "Dentist"
    assert call["purpose"] == "Reschedule appointment"
    assert call["duration_seconds"] >= 0

    status = client.get("/api/voice/status").json()
    assert status["active_call_count"] == 1


def test_list_calls_empty_when_tables_missing(client):
    r = client.get("/api/voice/calls")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_calls_seeded(client, tmp_path):
    await _seed_db(tmp_path / "pincer.db")

    r = client.get("/api/voice/calls")
    assert r.status_code == 200
    calls = r.json()
    assert [c["call_sid"] for c in calls] == ["CA003", "CA002", "CA001"]  # newest first
    by_sid = {c["call_sid"]: c for c in calls}
    assert by_sid["CA001"]["status"] == "completed"
    assert by_sid["CA001"]["duration_seconds"] == 90
    assert by_sid["CA003"]["status"] == "active"

    # direction filter
    r = client.get("/api/voice/calls", params={"direction": "outbound"})
    assert {c["call_sid"] for c in r.json()} == {"CA002", "CA003"}

    # status filter
    r = client.get("/api/voice/calls", params={"status": "completed"})
    assert {c["call_sid"] for c in r.json()} == {"CA001", "CA002"}

    # paging
    r = client.get("/api/voice/calls", params={"limit": 1, "offset": 1})
    assert [c["call_sid"] for c in r.json()] == ["CA002"]

    # invalid filter value rejected
    assert client.get("/api/voice/calls", params={"direction": "sideways"}).status_code == 422


async def test_call_detail_with_masked_transcript(client, tmp_path):
    await _seed_db(tmp_path / "pincer.db")

    r = client.get("/api/voice/calls/CA001")
    assert r.status_code == 200
    detail = r.json()
    assert detail["call_sid"] == "CA001"
    assert detail["status"] == "completed"

    # only final transcript lines, PII masked
    assert len(detail["transcript"]) == 1
    line = detail["transcript"][0]
    assert line["speaker"] == "caller"
    assert "123-45-6789" not in line["text"]
    assert "[SSN_REDACTED]" in line["text"]

    assert len(detail["actions"]) == 1
    action = detail["actions"][0]
    assert action["action_type"] == "tool_call"
    assert action["tool_name"] == "get_weather"
    assert action["user_confirmed"] is True


async def test_call_detail_404(client, tmp_path):
    # missing tables and missing row both read as 404
    assert client.get("/api/voice/calls/CA-nope").status_code == 404
    await _seed_db(tmp_path / "pincer.db")
    assert client.get("/api/voice/calls/CA-nope").status_code == 404


async def test_contacts(client, tmp_path):
    # table absent -> empty list, not an error
    r = client.get("/api/voice/contacts")
    assert r.status_code == 200
    assert r.json() == []

    async with aiosqlite.connect(tmp_path / "pincer.db") as db:
        await db.execute(
            "CREATE TABLE phone_contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT DEFAULT '', "
            "name TEXT NOT NULL, phone_number TEXT NOT NULL, category TEXT DEFAULT '', "
            "ivr_tree_json TEXT, notes TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "INSERT INTO phone_contacts (name, phone_number, category, notes) "
            "VALUES ('Zoe', '+15550009999', 'personal', ''), ('Dr. Ada', '+15550008888', 'doctor', 'dentist')"
        )
        await db.commit()

    r = client.get("/api/voice/contacts")
    assert r.status_code == 200
    contacts = r.json()
    assert [c["name"] for c in contacts] == ["Dr. Ada", "Zoe"]  # sorted by name
    assert contacts[0]["category"] == "doctor"


def test_initiate_call_success(client, monkeypatch):
    async def fake_make_phone_call(**kwargs):
        assert kwargs["target_number"] == "+15550004444"
        assert kwargs["purpose"] == "Book a table"
        assert kwargs["instructions"] == "Window seat if possible"
        assert kwargs["target_name"] == "Trattoria Roma"
        assert kwargs["context"]["user_id"] == "dashboard"
        return "Call initiated successfully.\nCall SID: CA-new-123\nTo: +15550004444\nPurpose: Book a table"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_make_phone_call)
    r = client.post(
        "/api/voice/calls",
        json={
            "target_number": "+15550004444",
            "purpose": "Book a table",
            "instructions": "Window seat if possible",
            "target_name": "Trattoria Roma",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["call_sid"] == "CA-new-123"
    assert data["status"] == "initiated"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ("Error: Outbound calling is disabled. Set PINCER_VOICE_OUTBOUND_ENABLED=true.", 403),
        ("Error: Invalid phone number format: 123. Use E.164 format (e.g. +14155551234).", 422),
        ("Error: Daily outbound call limit reached (10). Try again tomorrow.", 429),
        ("Error: Twilio SDK not installed. Install with: uv pip install 'pincer-agent[voice]'", 503),
        ("Error placing call: boom", 502),
    ],
)
def test_initiate_call_error_mapping(client, monkeypatch, error, expected_status):
    async def fake_make_phone_call(**kwargs):
        return error

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_make_phone_call)
    r = client.post("/api/voice/calls", json={"target_number": "+15550004444", "purpose": "x"})
    assert r.status_code == expected_status


def test_initiate_call_validation(client):
    assert client.post("/api/voice/calls", json={"target_number": "+15550004444"}).status_code == 422
    assert client.post("/api/voice/calls", json={"target_number": "+1", "purpose": "x"}).status_code == 422


def test_voice_api_requires_auth(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PINCER_DASHBOARD_TOKEN", "voice-test-token-1234")
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    try:
        c = TestClient(create_app())
        for path in ("/api/voice/status", "/api/voice/active", "/api/voice/calls", "/api/voice/contacts"):
            assert c.get(path).status_code == 401, path
        assert c.post("/api/voice/calls", json={"target_number": "+15550004444", "purpose": "x"}).status_code == 401
        headers = {"Authorization": "Bearer voice-test-token-1234"}
        assert c.get("/api/voice/status", headers=headers).status_code == 200
    finally:
        get_settings_relaxed.cache_clear()


class TestVoiceConfig:
    def test_get_config_defaults(self, client):
        r = client.get("/api/voice/config")
        assert r.status_code == 200
        data = r.json()
        assert data["voice_turn_model"] == ""
        assert data["default_model"]
        values = [c["value"] for c in data["choices"]]
        assert "" in values  # "Default (<model>)" always offered

    def test_put_config_applies_live_and_persists(self, client, tmp_path, monkeypatch):
        from types import SimpleNamespace

        # A running agent whose settings instance differs from the API's
        agent_settings = SimpleNamespace(voice_turn_model="")
        client.app.state.agent = SimpleNamespace(_settings=agent_settings, _tools=None)

        r = client.put("/api/voice/config", json={"voice_turn_model": "openai:gpt-5-mini"})
        assert r.status_code == 200
        assert r.json()["voice_turn_model"] == "openai:gpt-5-mini"

        # Live agent settings mutated -> the very next voice turn uses it
        assert agent_settings.voice_turn_model == "openai:gpt-5-mini"

        # Persisted for restarts, and apply_overrides picks it up
        import json as _json

        runtime = tmp_path / "voice_runtime.json"
        assert runtime.is_file()
        assert _json.loads(runtime.read_text())["voice_turn_model"] == "openai:gpt-5-mini"

        from pincer.voice.runtime_config import apply_overrides

        fresh = SimpleNamespace(voice_turn_model="", data_dir=tmp_path)
        apply_overrides(fresh)
        assert fresh.voice_turn_model == "openai:gpt-5-mini"

        # GET reflects the change; clearing back to default works
        assert client.get("/api/voice/config").json()["voice_turn_model"] == "openai:gpt-5-mini"
        assert client.put("/api/voice/config", json={"voice_turn_model": ""}).status_code == 200
        assert client.get("/api/voice/config").json()["voice_turn_model"] == ""

    @pytest.mark.parametrize("bad", ["has spaces", "UPPER:case-provider:x", "a:b:c:d$"])
    def test_put_config_rejects_garbage(self, client, bad):
        assert client.put("/api/voice/config", json={"voice_turn_model": bad}).status_code == 422


# ── Sprint 12: receptionist messages ────────────────────────────────


async def test_messages_endpoint_lists_masked_rows(client, tmp_path):
    from pincer.voice.receptionist.report import persist_inbound_message, stamp_delivered

    db_path = tmp_path / "pincer.db"
    await persist_inbound_message(
        str(db_path),
        "CA_msg",
        {
            "intent": "message",
            "slots": {
                "caller_name": "Schmidt",
                "callback_number": "+4917212345678",
                "matter": "Rückruf wegen Rechnung",
                "urgent": True,
            },
        },
    )
    await stamp_delivered(str(db_path), "CA_msg")
    body = client.get("/api/voice/messages").json()
    assert len(body) == 1
    row = body[0]
    assert row["call_sid"] == "CA_msg" and row["urgent"] is True
    assert row["matter"] == "Rückruf wegen Rechnung"
    assert "4917212345678" not in row["callback_number"]  # PII-masked on every read surface
    assert row["delivered_to_owner_at"]

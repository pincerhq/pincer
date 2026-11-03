"""Tests for the Voice REST API (/api/voice/*)."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from fastapi.testclient import TestClient
from support import SEED_ID_SQL

from pincer.api.server import create_app
from pincer.db import ensure_schema_current
from pincer.voice.engine import CallDirection, CallState


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


async def _seed_db(db_path):
    await asyncio.to_thread(
        ensure_schema_current,
        Path(db_path),
    )
    async with aiosqlite.connect(db_path) as db:
        # Relative to now so the calls stay inside the stats endpoints' 7-day window.
        started = datetime.now(UTC) - timedelta(days=1)
        for sid, direction, offset_min, ended in [
            ("CA001", "inbound", 0, True),
            ("CA002", "outbound", 5, True),
            ("CA003", "outbound", 10, False),
        ]:
            await db.execute(
                "INSERT INTO pincer_voice_calls (id, call_sid, direction, from_number, to_number, started_at, "
                f"ended_at) VALUES ({SEED_ID_SQL}, ?, ?, ?, ?, ?, ?)",
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
            "INSERT INTO pincer_call_transcripts (id, call_id, speaker, text, confidence, is_final, state, timestamp) "
            f"VALUES ({SEED_ID_SQL}, 'CA001', 'caller', 'My SSN is 123-45-6789 thanks', 0.9, 1, 'conversing', ?)",
            (started.isoformat(),),
        )
        await db.execute(
            "INSERT INTO pincer_call_transcripts (id, call_id, speaker, text, confidence, is_final, state, timestamp) "
            f"VALUES ({SEED_ID_SQL}, 'CA001', 'agent', 'partial utterance', 1.0, 0, 'conversing', ?)",
            ((started + timedelta(seconds=5)).isoformat(),),
        )
        await db.execute(
            "INSERT INTO pincer_call_actions (id, call_id, action_type, tool_name, input_summary, output_summary, "
            f"user_confirmed, timestamp) VALUES ({SEED_ID_SQL}, 'CA001', 'tool_call', 'get_weather', 'Berlin', "
            "'Sunny', 1, ?)",
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

        self.end_error: str | None = None  # what end_call / hang_up report (None: Twilio hung up)
        self.ended: list[str] = []
        self.hang_ups: list[str] = []  # REST hang-ups without teardown (the retry path)
        self.ringing: list[bool] = []  # the `ringing` flag of each hang-up, in order
        self.failed_hang_ups: dict[str, bool] = {}

    def get_active_calls(self):
        return dict(self._calls)

    def get_call_state(self, call_sid):
        return self._calls.get(call_sid)

    def note_hang_up_failed(self, call_sid, *, ringing=False):
        self.failed_hang_ups[call_sid] = ringing

    def hang_up_failed_recently(self, call_sid):
        return self.failed_hang_ups.get(call_sid)

    def forget_hang_up_failure(self, call_sid):
        self.failed_hang_ups.pop(call_sid, None)

    async def hang_up(self, call_sid, *, ringing=False):
        self.hang_ups.append(call_sid)
        self.ringing.append(ringing)
        return self.end_error

    async def end_call(self, call_sid, *, ringing=False):
        # like the real engine: unregistered whatever Twilio answered
        self.ended.append(call_sid)
        self.ringing.append(ringing)
        self._calls.pop(call_sid, None)
        return self.end_error


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
    assert data["capabilities"] == {"call_controls": True, "hang_up": True, "listen_in": False, "whisper": False}


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


@pytest.mark.parametrize(
    ("path", "service", "method"),
    [
        ("/api/voice/calls", "pincer.services.voice.CallsService", "page_with_thread"),
        ("/api/voice/calls/CA_x", "pincer.services.voice.CallsService", "detail"),
        ("/api/voice/messages", "pincer.services.voice.MessagesService", "newest"),
        ("/api/voice/contacts", "pincer.services.voice.ContactsService", "all"),
        ("/api/voice/threads", "pincer.voice.threads.ThreadManager", "list_threads"),
    ],
)
def test_a_failing_database_is_an_error_not_an_empty_history(monkeypatch, tmp_path, path, service, method):
    """An unreachable database must not answer "no calls" (or 404 for a call that exists)."""
    from sqlalchemy.exc import OperationalError

    from pincer.config import get_settings_relaxed

    async def down(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    get_settings_relaxed.cache_clear()
    monkeypatch.setattr(f"{service}.{method}", down)
    try:
        response = TestClient(create_app(), raise_server_exceptions=False).get(path)
    finally:
        get_settings_relaxed.cache_clear()
    assert response.status_code == 500


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
            f"INSERT INTO pincer_phone_contacts (id, name, phone_number, category, notes) "
            f"VALUES ({SEED_ID_SQL}, 'Zoe', '+15550009999', 'personal', ''), "
            f"({SEED_ID_SQL}, 'Dr. Ada', '+15550008888', 'doctor', 'dentist')"
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
    r = client.post(
        "/api/voice/calls",
        json={"target_number": "+15550004444", "purpose": "Ask what time they close today"},
    )
    assert r.status_code == expected_status


def test_initiate_call_validation(client):
    assert client.post("/api/voice/calls", json={"target_number": "+15550004444"}).status_code == 422
    assert (
        client.post("/api/voice/calls", json={"target_number": "+1", "purpose": "Ask when they close"}).status_code
        == 422
    )


@pytest.mark.parametrize("purpose", ["x", "call mum", "         "])
def test_initiate_call_rejects_an_unusable_task(client, purpose):
    """A call the agent cannot open with a task is refused at the door — it used
    to be accepted and turned into a generic assistant monologue on the phone."""
    r = client.post("/api/voice/calls", json={"target_number": "+15550004444", "purpose": purpose})
    assert r.status_code == 422
    assert "Purpose too short" in r.json()["detail"]


def test_initiate_call_rejects_an_oversized_task(client):
    r = client.post("/api/voice/calls", json={"target_number": "+15550004444", "purpose": "y" * 2001})
    assert r.status_code == 422


def test_schedule_rejects_an_unusable_task(client):
    r = client.post(
        "/api/voice/schedule",
        json={"target_number": "+15550004444", "contact_name": "A", "topic": "x"},
    )
    assert r.status_code == 422


def test_end_call_hangs_up_and_leaves_the_active_list(client, fake_engine):
    state = fake_engine._calls["CA-live"]
    state.metadata["answered_at"] = datetime.now(UTC)
    r = client.post("/api/voice/calls/CA-live/end")
    assert r.status_code == 204
    assert r.content == b""
    assert fake_engine.ended == ["CA-live"]
    assert state.metadata["ended_by"] == "dashboard"
    assert "end_code" not in state.metadata  # an answered call ended by its owner is not a failure
    assert client.get("/api/voice/active").json() == []


def test_end_call_before_an_answer_records_the_owner_as_the_cause(client, fake_engine):
    state = fake_engine._calls["CA-live"]  # outbound, still ringing
    assert client.post("/api/voice/calls/CA-live/end").status_code == 204
    assert state.metadata["end_code"] == "ended_by_owner"
    assert fake_engine.ringing == [True]  # cancelled, not only `completed`


async def test_end_call_before_an_answer_does_not_redial_an_appointment(client, fake_engine):
    """The `canceled` status Twilio posts after the owner's hang-up must not
    read as an unreachable contact and schedule the appointment redial."""
    from pincer.voice import scheduling

    scheduling._reset_for_tests()
    task = scheduling.AppointmentTask(
        task_id="t1",
        user_id="u1",
        channel="telegram",
        target_number="+15550002222",
        contact_name="Dr. Who",
        topic="checkup",
        timeframe="",
        duration_minutes=30,
        language="en",
    )
    scheduling.register_appointment("CA-live", task)
    try:
        assert client.post("/api/voice/calls/CA-live/end").status_code == 204
        assert scheduling.get_appointment("CA-live") is None
        assert await scheduling.handle_call_not_connected("CA-live", "canceled", SimpleNamespace()) is False
    finally:
        scheduling._reset_for_tests()


def test_end_call_after_the_call_ended_on_its_own_is_404(client, fake_engine):
    """A stale tab ending a call the caller already hung up: 404, no REST
    hang-up and no audit row claiming the owner ended it."""
    fake_engine._calls.clear()
    r = client.post("/api/voice/calls/CA-live/end")
    assert r.status_code == 404
    assert fake_engine.hang_ups == [] and fake_engine.ended == []


def test_end_call_unknown_sid_is_404(client, fake_engine):
    r = client.post("/api/voice/calls/CA-nope/end")
    assert r.status_code == 404
    assert r.json()["detail"] == "Call is not active"
    assert fake_engine.ended == []


def test_end_call_without_engine_is_404(client):
    assert client.post("/api/voice/calls/CA-live/end").status_code == 404


def test_end_call_twilio_rejection_is_502_and_still_untracked(client, fake_engine):
    fake_engine.end_error = "Unable to update record (Twilio error 20003: https://www.twilio.com/docs/errors/20003)"
    r = client.post("/api/voice/calls/CA-live/end")
    assert r.status_code == 502
    assert r.json()["detail"] == f"Twilio rejected the hang-up: {fake_engine.end_error}"
    assert client.get("/api/voice/active").json() == []


def test_end_call_can_be_retried_after_a_502(client, fake_engine):
    """Twilio may still have the caller on the line: a second click sends the
    REST hang-up again instead of a 404."""
    fake_engine.end_error = "TimeoutError"
    assert client.post("/api/voice/calls/CA-live/end").status_code == 502
    r = client.post("/api/voice/calls/CA-live/end")
    assert r.status_code == 502 and r.json()["detail"] == "Twilio rejected the hang-up: TimeoutError"
    fake_engine.end_error = None
    assert client.post("/api/voice/calls/CA-live/end").status_code == 204
    assert fake_engine.ended == ["CA-live"]  # torn down once
    assert fake_engine.hang_ups == ["CA-live", "CA-live"]
    assert fake_engine.ringing == [True, True, True]  # the retries still cancel a ringing call
    # Hung up at last: a further click is a 404 like any ended call.
    assert client.post("/api/voice/calls/CA-live/end").status_code == 404


def test_end_call_is_audited(client, fake_engine, monkeypatch):
    entries = []

    class _Audit:
        async def log(self, entry):
            entries.append(entry)

    async def _get():
        return _Audit()

    monkeypatch.setattr("pincer.security.audit.get_audit_logger", _get)
    assert client.post("/api/voice/calls/CA-live/end").status_code == 204
    [entry] = entries
    assert entry.action == "voice_call_end"
    assert entry.input_summary == "CA-live"
    assert entry.metadata == {"call_sid": "CA-live", "ended_by": "dashboard", "twilio_error": None}


def test_end_call_is_in_the_openapi_schema(client):
    paths = client.app.openapi()["paths"]
    op = paths["/api/voice/calls/{call_sid}/end"]["post"]
    assert {"204", "404", "502"} <= set(op["responses"])


def test_voice_api_requires_auth(authed_app):
    c = authed_app.client
    for path in ("/api/voice/status", "/api/voice/active", "/api/voice/calls", "/api/voice/contacts"):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/voice/calls", json={"target_number": "+15550004444", "purpose": "x"}).status_code == 401
    assert c.post("/api/voice/calls/CA-live/end").status_code == 401
    assert c.get("/api/voice/status", headers=authed_app.jwt_headers).status_code == 200


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

        from pincer.config import get_settings_relaxed

        monkeypatch.setenv("PINCER_OPENAI_API_KEY", "sk-test")  # openai:… must build to be selectable
        get_settings_relaxed.cache_clear()

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

    @staticmethod
    def _env(monkeypatch, **env: str) -> None:
        from pincer.config import get_settings_relaxed

        for key, value in env.items():
            monkeypatch.setenv(f"PINCER_{key.upper()}", value)
        get_settings_relaxed.cache_clear()

    def _gemma_default(self, monkeypatch) -> None:
        """A custom Anthropic-wire default provider with no OpenAI key (#305)."""
        self._env(
            monkeypatch,
            default_provider="gemma_anthropic",
            default_model="claude-sonnet-5-5",
            anthropic_compatible_provider="gemma_anthropic",
            anthropic_compatible_base_url="http://gemma.local",
            anthropic_compatible_model="local-model",
            openai_api_key="",
        )

    def test_put_unbuildable_provider_is_422(self, client, monkeypatch, tmp_path):
        self._gemma_default(monkeypatch)
        r = client.put("/api/voice/config", json={"voice_turn_model": "openai:gpt-5-mini"})
        assert r.status_code == 422
        assert "PINCER_OPENAI_API_KEY" in r.json()["detail"]
        assert not (tmp_path / "voice_runtime.json").exists()  # nothing persisted
        r = client.put("/api/voice/config", json={"voice_turn_model": "azure:gpt-5-mini"})
        assert r.status_code == 422

    def test_choices_are_provider_qualified_on_custom_default(self, client, monkeypatch):
        self._gemma_default(monkeypatch)
        choices = client.get("/api/voice/config").json()["choices"]
        assert choices[0] == {"value": "", "label": "Default (local-model)"}
        values = [c["value"] for c in choices]
        assert "gemma_anthropic:local-model" in values
        assert "anthropic:claude-haiku-4-5-20251001" in values  # the conftest Anthropic key
        assert "claude-haiku-4-5-20251001" not in values  # bare = sent to gemma
        assert not [v for v in values if v.startswith("openai:")]
        for value in values[1:]:  # every offered choice is accepted back
            assert client.put("/api/voice/config", json={"voice_turn_model": value}).status_code == 200

    def test_choices_bare_haiku_on_anthropic_default(self, client, monkeypatch):
        self._env(monkeypatch, default_provider="anthropic", openai_api_key="sk-test")
        values = [c["value"] for c in client.get("/api/voice/config").json()["choices"]]
        assert "claude-haiku-4-5-20251001" in values
        assert {"openai:gpt-5-mini", "openai:gpt-4o-mini"} <= set(values)

    def test_choices_offer_azure_when_it_is_the_default(self, client, monkeypatch):
        self._env(
            monkeypatch,
            default_provider="azure",
            default_model="gpt-5.4-mini",
            openai_compatible_provider="azure",
            openai_compatible_base_url="https://res.openai.azure.com/openai/v1/",
            openai_compatible_api_key="azure-key",
            anthropic_api_key="",
            openai_api_key="",
        )
        data = client.get("/api/voice/config").json()
        assert [c["value"] for c in data["choices"]] == ["", "azure:gpt-5.4-mini"]
        assert (data["effective_provider"], data["effective_model"]) == ("azure", "gpt-5.4-mini")

    def test_get_reports_effective_model(self, client, monkeypatch):
        from types import SimpleNamespace

        self._gemma_default(monkeypatch)
        data = client.get("/api/voice/config").json()
        assert (data["effective_provider"], data["effective_model"]) == ("gemma_anthropic", "local-model")

        # A stale override on the live agent (e.g. set from the env) is reported
        # as what really runs: the default provider and its model.
        client.app.state.agent = SimpleNamespace(_settings=SimpleNamespace(voice_turn_model="openai:gpt-5-mini"))
        data = client.get("/api/voice/config").json()
        assert data["voice_turn_model"] == "openai:gpt-5-mini"
        assert (data["effective_provider"], data["effective_model"]) == ("gemma_anthropic", "local-model")

        assert (
            client.put("/api/voice/config", json={"voice_turn_model": "anthropic:claude-haiku-4-5"}).status_code == 200
        )
        data = client.get("/api/voice/config").json()
        assert (data["effective_provider"], data["effective_model"]) == ("anthropic", "claude-haiku-4-5")


class TestApplyOverrides:
    def _stored(self, tmp_path, model: str):
        import json as _json
        from types import SimpleNamespace

        (tmp_path / "voice_runtime.json").write_text(_json.dumps({"voice_turn_model": model}))
        return SimpleNamespace(voice_turn_model="", data_dir=tmp_path)

    def test_unusable_stored_override_is_skipped(self, tmp_path, caplog):
        import logging

        settings = self._stored(tmp_path, "openai:gpt-5-mini")
        caplog.set_level(logging.INFO, logger="pincer.voice.runtime_config")
        from pincer.voice.runtime_config import apply_overrides

        apply_overrides(settings, unusable_reason=lambda m: "provider 'openai' needs PINCER_OPENAI_API_KEY")
        assert settings.voice_turn_model == ""  # env value kept
        assert (tmp_path / "voice_runtime.json").read_text().count("openai:gpt-5-mini") == 1  # file untouched
        assert [r.getMessage() for r in caplog.records if "Ignoring stored voice turn model" in r.getMessage()]

    def test_usable_stored_override_applies(self, tmp_path):
        from pincer.voice.runtime_config import apply_overrides

        settings = self._stored(tmp_path, "anthropic:claude-haiku-4-5")
        seen: list[str] = []
        apply_overrides(settings, unusable_reason=lambda m: seen.append(m) or None)
        assert settings.voice_turn_model == "anthropic:claude-haiku-4-5"
        assert seen == ["anthropic:claude-haiku-4-5"]

    def test_with_the_router_check(self, tmp_path):
        from pincer.config import Settings
        from pincer.llm.router import voice_turn_unusable_reason
        from pincer.voice.runtime_config import apply_overrides

        live = Settings(openai_api_key="", data_dir=tmp_path)  # type: ignore[arg-type]
        (tmp_path / "voice_runtime.json").write_text('{"voice_turn_model": "openai:gpt-5-mini"}')
        apply_overrides(live, unusable_reason=lambda m: voice_turn_unusable_reason(live, m))
        assert live.voice_turn_model == ""


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


# ── Sprint 13: call threads (§9) ────────────────────────────────────


@pytest.fixture
async def threads_api(client, tmp_path):
    """A seeded thread with two calls, one of which has been purged."""
    from pincer.voice import threads as th

    settings = SimpleNamespace(db_path=str(tmp_path / "pincer.db"))
    manager = th.ThreadManager(settings.db_path, settings=settings)
    th.set_thread_manager(manager)

    await _seed_db(tmp_path / "pincer.db")
    thread = await manager.create(
        "Termin Dr. Müller", primary_number="+15550002222", contact_name="Dr. Müller", language="de"
    )
    await manager.attach("CA002", thread.thread_id, th.KIND_ORIGIN)
    await manager.attach("CA_gone", thread.thread_id, th.KIND_FOLLOWUP)  # no voice_calls row = purged
    async with aiosqlite.connect(str(tmp_path / "pincer.db")) as db:
        await db.execute(
            "UPDATE pincer_call_threads SET rolling_summary = ?, open_commitments = ? WHERE thread_id = ?",
            (
                "Dienstag angerufen.\nStand: wartet auf Rückruf.",
                '[{"who":"callee","what":"ruft am Freitag zurück","due":null,'
                '"status":"open","source_call_sid":"CA002"}]',
                thread.thread_id,
            ),
        )
        await db.commit()
    return SimpleNamespace(client=client, manager=manager, thread=thread)


async def test_list_threads_and_search(threads_api):
    client = threads_api.client
    rows = client.get("/api/voice/threads").json()
    assert [r["thread_id"] for r in rows] == [threads_api.thread.thread_id]
    assert rows[0]["call_count"] == 2
    assert rows[0]["open_commitments"][0]["what"] == "ruft am Freitag zurück"

    assert client.get("/api/voice/threads", params={"q": "Müller"}).json()
    assert client.get("/api/voice/threads", params={"q": "nothing here"}).json() == []
    assert client.get("/api/voice/threads", params={"status": "closed"}).json() == []


async def test_thread_detail_lists_purged_stub(threads_api):
    body = threads_api.client.get(f"/api/voice/threads/{threads_api.thread.thread_id}").json()
    assert body["rolling_summary"].startswith("Dienstag angerufen")
    by_sid = {c["call_sid"]: c for c in body["calls"]}
    assert by_sid["CA002"]["purged"] is False
    assert by_sid["CA002"]["thread_attach_kind"] == "origin"
    assert by_sid["CA_gone"]["purged"] is True


async def test_thread_detail_404(threads_api):
    assert threads_api.client.get("/api/voice/threads/thr_nope").status_code == 404


async def test_patch_thread_subject_and_status(threads_api):
    client, tid = threads_api.client, threads_api.thread.thread_id
    assert client.patch(f"/api/voice/threads/{tid}", json={"subject": "Neuer Titel"}).json()["subject"] == (
        "Neuer Titel"
    )
    assert client.patch(f"/api/voice/threads/{tid}", json={"status": "resolved"}).json()["status"] == "resolved"
    assert client.patch(f"/api/voice/threads/{tid}", json={"status": "closed"}).json()["status"] == "closed"
    # Closed is final — the API refuses, it does not silently reopen.
    response = client.patch(f"/api/voice/threads/{tid}", json={"status": "open"})
    assert response.status_code == 409 and "closed is final" in response.json()["detail"]
    assert client.patch(f"/api/voice/threads/{tid}", json={"status": "archived"}).status_code == 422


async def test_create_assign_and_merge(threads_api):
    client = threads_api.client
    created = client.post("/api/voice/threads", json={"subject": "Manuelles Anliegen", "contact_name": "Praxis"})
    assert created.status_code == 201
    new_id = created.json()["thread_id"]

    # Reassigning a call moves it out of its previous thread (§4.4).
    detail = client.post(f"/api/voice/threads/{new_id}/assign", json={"call_sid": "CA002"}).json()
    assert [c["call_sid"] for c in detail["calls"]] == ["CA002"]
    assert [c["thread_attach_kind"] for c in detail["calls"]] == ["manual"]
    old = client.get(f"/api/voice/threads/{threads_api.thread.thread_id}").json()
    assert [c["call_sid"] for c in old["calls"]] == ["CA_gone"]

    # Merging folds the source in and closes it.
    merged = client.post(
        f"/api/voice/threads/{new_id}/merge", json={"source_thread_id": threads_api.thread.thread_id}
    ).json()
    assert sorted(c["call_sid"] for c in merged["calls"]) == ["CA002", "CA_gone"]
    assert client.get(f"/api/voice/threads/{threads_api.thread.thread_id}").json()["status"] == "closed"


async def test_calls_surface_carries_thread_fields(threads_api):
    client = threads_api.client
    rows = {r["call_sid"]: r for r in client.get("/api/voice/calls").json()}
    assert rows["CA002"]["thread_id"] == threads_api.thread.thread_id
    assert rows["CA002"]["thread_subject"] == "Termin Dr. Müller"
    assert rows["CA002"]["thread_attach_kind"] == "origin"
    assert rows["CA001"]["thread_id"] == ""

    filtered = client.get("/api/voice/calls", params={"thread_id": threads_api.thread.thread_id}).json()
    assert [r["call_sid"] for r in filtered] == ["CA002"]

    detail = client.get("/api/voice/calls/CA002").json()
    assert detail["thread_subject"] == "Termin Dr. Müller"


def test_threads_endpoint_requires_auth(authed_app):
    guarded = authed_app.client
    assert guarded.get("/api/voice/threads").status_code == 401
    assert guarded.get("/api/voice/threads", headers=authed_app.jwt_headers).status_code == 200


# ── Regression: the API reads a database the writer has not migrated ─


async def test_calls_survive_a_pre_sprint13_database(client, tmp_path):
    """A voice_calls table from before Sprint 13 must still serve its rows.

    The API is a reader; the migration runs on the writer side. Between
    deploying and the next call ending, `voice_calls` has no `thread_id` and
    `call_threads` does not exist — and the thread JOIN would raise
    OperationalError, which the handlers below turn into "no calls". That is
    the whole call history disappearing, reported as an empty list.
    """
    db_path = tmp_path / "pincer.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            # Unprefixed on purpose: this simulates a table Alembic has never
            # touched, which it only recognizes as its own under the
            # pre-`0019` name. `ensure_schema_current` below (triggered by the
            # app's own startup) reconciles it into `pincer_voice_calls` the
            # same way it backfills any other legacy shape.
            "CREATE TABLE voice_calls ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT NOT NULL UNIQUE, "
            "direction TEXT NOT NULL DEFAULT 'inbound', from_number TEXT DEFAULT '', "
            "to_number TEXT DEFAULT '', pincer_user_id TEXT DEFAULT '', "
            "recording_enabled INTEGER DEFAULT 0, consent_given INTEGER DEFAULT 0, "
            "started_at TEXT NOT NULL, ended_at TEXT)"
        )
        await db.execute(
            # A deliberately pre-Sprint-13 table: the key is still an INTEGER
            # here, so this one seeds the old way on purpose.
            "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at) "
            "VALUES ('CA_legacy', 'outbound', '2026-08-20T16:43:37+00:00', '2026-08-20T16:44:00+00:00')"
        )
        await db.commit()

    rows = client.get("/api/voice/calls").json()
    assert [r["call_sid"] for r in rows] == ["CA_legacy"]
    assert rows[0]["thread_id"] == ""  # threadless, and stays that way

    detail = client.get("/api/voice/calls/CA_legacy").json()
    assert detail["call_sid"] == "CA_legacy"
    assert client.get("/api/voice/threads").json() == []


# ── Thread list filters as the dashboard sends them ─────────────────


async def test_thread_status_filter_accepts_combined_views(threads_api):
    """ "Open + resolved" is one dashboard view; every encoding of it works."""
    client, tid = threads_api.client, threads_api.thread.thread_id
    resolved = client.post("/api/voice/threads", json={"subject": "Erledigte Sache"}).json()["thread_id"]
    client.patch(f"/api/voice/threads/{resolved}", json={"status": "resolved"})
    closed = client.post("/api/voice/threads", json={"subject": "Geschlossene Sache"}).json()["thread_id"]
    client.patch(f"/api/voice/threads/{closed}", json={"status": "closed"})

    def ids(**params):
        return sorted(t["thread_id"] for t in client.get("/api/voice/threads", params=params).json())

    assert ids(status="open") == sorted([tid])
    assert ids(status="open,resolved") == sorted([tid, resolved])
    assert ids(status="open resolved") == sorted([tid, resolved])  # what "open+resolved" decodes to
    assert ids(status=["open", "resolved"]) == sorted([tid, resolved])
    assert ids(status="all") == sorted([tid, resolved, closed])
    assert ids() == sorted([tid, resolved, closed])

    bad = client.get("/api/voice/threads", params={"status": "archived"})
    assert bad.status_code == 422 and "archived" in bad.json()["detail"]


async def test_expired_commitment_filter(threads_api):
    """The dashboard's "has expired commitments" chip (§6.2: flagged, not acted on)."""
    client = threads_api.client
    expired_id = client.post("/api/voice/threads", json={"subject": "Überfällig"}).json()["thread_id"]
    async with aiosqlite.connect(str(threads_api.manager.db_path)) as db:
        await db.execute(
            "UPDATE pincer_call_threads SET open_commitments = ? WHERE thread_id = ?",
            (
                '[{"who":"callee","what":"schickt die Unterlagen","due":"2026-01-01T10:00:00+00:00",'
                '"status":"expired","source_call_sid":"CA002"}]',
                expired_id,
            ),
        )
        await db.commit()

    rows = client.get("/api/voice/threads", params={"has_expired_commitments": "true"}).json()
    assert [t["thread_id"] for t in rows] == [expired_id]
    assert rows[0]["open_commitments"][0]["status"] == "expired"

    # Off by default: the unfiltered list still holds both threads.
    assert len(client.get("/api/voice/threads").json()) == 2


# ── Conversation analytics (§6) ─────────────────────────────────────


@pytest.fixture
async def analytics_seed(client, tmp_path):
    from pincer.voice import analytics as an

    db_path = str(tmp_path / "pincer.db")
    await _seed_db(tmp_path / "pincer.db")
    await an.save_analytics(
        db_path,
        "CA001",
        an.CallAnalytics(
            agent_speech_ms=12_000,
            caller_speech_ms=8_000,
            silence_ms=4_000,
            overlap_ms=500,
            interruptions=3,
            talk_ratio=0.6,
            method=an.METHOD_EXACT,
            sentiment="negative",
            sentiment_trajectory="declining",
            sentiment_rationale="Said the delay was unacceptable; left +4917212345678 for a callback.",
            created_at=datetime.now(UTC).isoformat(),
        ),
    )
    await an.save_analytics(
        db_path,
        "CA002",
        an.CallAnalytics(method=an.METHOD_ESTIMATED, sentiment_reason=an.REASON_TOO_SHORT),
    )
    return SimpleNamespace(client=client, db_path=db_path)


async def test_call_detail_exposes_analytics(analytics_seed):
    body = analytics_seed.client.get("/api/voice/calls/CA001").json()
    analytics = body["analytics"]
    assert analytics["method"] == "exact"
    assert analytics["talk_ratio"] == 0.6
    assert analytics["interruptions"] == 3
    assert analytics["sentiment"] == "negative"
    assert analytics["sentiment_trajectory"] == "declining"
    assert analytics["sentiment_reason"] == ""
    # The rationale quotes the call, so it is masked like every read surface.
    assert "4917212345678" not in analytics["sentiment_rationale"]
    assert "delay was unacceptable" in analytics["sentiment_rationale"]


async def test_call_detail_analytics_null_states(analytics_seed):
    short = analytics_seed.client.get("/api/voice/calls/CA002").json()["analytics"]
    assert short["sentiment"] is None
    assert short["sentiment_reason"] == "too_short"
    assert short["talk_ratio"] is None
    assert short["method"] == "estimated"

    # A call from before the feature has no record at all — and must not 500.
    assert analytics_seed.client.get("/api/voice/calls/CA003").json()["analytics"] is None


async def test_calls_list_carries_compact_analytics(analytics_seed):
    rows = {r["call_sid"]: r for r in analytics_seed.client.get("/api/voice/calls").json()}
    assert rows["CA001"]["sentiment"] == "negative"
    assert rows["CA001"]["talk_ratio"] == 0.6
    assert rows["CA001"]["method"] == "exact"
    # Null-safe for calls with no analytics.
    assert rows["CA003"]["sentiment"] is None
    assert rows["CA003"]["talk_ratio"] is None


async def test_receptionist_stats_sentiment_distribution(analytics_seed):
    body = analytics_seed.client.get("/api/voice/receptionist/stats", params={"days": 7}).json()
    distribution = body["sentiment_distribution"]
    assert body["window_days"] == 7
    # CA001 is the inbound call in the seed; CA002 was never assessed.
    assert distribution["negative"] == 1
    assert distribution["assessed"] == 1
    assert distribution["neutral"] == 0


async def test_receptionist_stats_is_empty_not_broken_without_data(client):
    body = client.get("/api/voice/receptionist/stats").json()
    assert body["sentiment_distribution"]["assessed"] == 0

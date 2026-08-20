"""Appointment scheduling workflow tests (Sprint 6) — tool entry, channel
negotiation with the FakeVoiceEngine, retry policy, post-call executor, and
the calendar-write failure paths (the worst case: verbal yes, write fails —
must be reported honestly, never claimed)."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from pincer.channels.phone_calls import VoiceChannel
from pincer.tools.registry import ToolRegistry
from pincer.voice import scheduling
from pincer.voice.engine import CallDirection
from pincer.voice.postcall import PostCallProcessor
from pincer.voice.scheduling import AppointmentTask, schedule_appointment_call

CANDIDATES = ["2026-08-25T14:00:00+02:00", "2026-08-26T09:00:00+02:00"]

CALL_OK = "Call initiated successfully.\nCall SID: CA_appt_1\nTo: +4930123456\nPurpose: appointment"


def _settings(**overrides):
    values = {
        "voice_timezone": "Europe/Berlin",
        "timezone": "",
        "voice_default_language": "en",
        "voice_supported_languages": "en,de,uk",
        "voice_de_formality": "sie",
        "voice_language": "en-US",
        "voice_stt_min_confidence": 0.55,
        "business_hours": "09:00-17:00",
        "business_days": "mon,tue,wed,thu,fri",
        "slot_buffer_min": 15,
        "scheduling_calendar_id": "primary",
        "voice_retry_attempts": 2,
        "voice_retry_delay_min": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(**overrides) -> AppointmentTask:
    values = dict(
        task_id="task-1",
        user_id="u1",
        channel="telegram",
        target_number="+4930123456",
        contact_name="Dr. Müller",
        topic="Zahnreinigung",
        timeframe="next_week",
        duration_minutes=30,
        language="de",
        attendees="user@example.com",
        candidates=list(CANDIDATES),
    )
    values.update(overrides)
    return AppointmentTask(**values)


@pytest.fixture(autouse=True)
def _clean_registry():
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


@pytest.fixture
def notifications(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def _send(user_id: str, channel: str, text: str) -> bool:
        sent.append((user_id, channel, text))
        return True

    monkeypatch.setattr("pincer.voice.status_notify.send_user_message", _send)
    return sent


def _google_registry(freebusy_text="primary: FREE", create_results=None):
    """Real ToolRegistry with fake Google Calendar handlers recording calls."""
    registry = ToolRegistry()
    calls = {"freebusy": [], "create": []}

    async def fake_freebusy(**kwargs):
        calls["freebusy"].append(kwargs)
        return freebusy_text

    results = list(create_results or ["Event created: 'x'\nID: ev1\nLink: https://cal.example/ev1"])

    async def fake_create(**kwargs):
        calls["create"].append(kwargs)
        result = results.pop(0) if results else results_default
        if isinstance(result, Exception):
            raise result
        return result

    results_default = "Event created: 'x'\nID: evN\nLink: https://cal.example/evN"
    registry.register("google__check_freebusy", "fb", fake_freebusy, {"type": "object", "properties": {}})
    registry.register("google__create_event", "ce", fake_create, {"type": "object", "properties": {}})
    return registry, calls


# ── T6.1/T6.2: tool entry point ──────────────────────────────────────


class TestScheduleTool:
    async def test_no_google_calendar_errors_before_any_call(self, monkeypatch):
        dialed = []

        async def fake_call(**kwargs):
            dialed.append(kwargs)
            return CALL_OK

        monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_call)
        result = await schedule_appointment_call(
            ToolRegistry(),
            _settings(),
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Termin",
            timeframe="next_week",
        )
        assert result.startswith("Error")
        assert "not connected" in result
        assert dialed == []

    async def test_unrecognized_freebusy_never_treated_as_free(self, monkeypatch):
        registry, _ = _google_registry(freebusy_text="HTTP 401: invalid credentials")
        dialed = []

        async def fake_call(**kwargs):
            dialed.append(kwargs)
            return CALL_OK

        monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_call)
        result = await schedule_appointment_call(
            registry,
            _settings(),
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Termin",
            timeframe="next_week",
        )
        assert result.startswith("Error")
        assert dialed == []

    async def test_zero_slots_errors_before_any_call(self, monkeypatch):
        # Calendar fully busy for the century -> no slots, date-independent
        registry, _ = _google_registry(
            freebusy_text="primary: BUSY at:\n    2000-01-01T00:00:00Z → 2100-01-01T00:00:00Z"
        )
        dialed = []

        async def fake_call(**kwargs):
            dialed.append(kwargs)
            return CALL_OK

        monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_call)
        result = await schedule_appointment_call(
            registry,
            _settings(),
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Termin",
            timeframe="tomorrow",
            language="de",
        )
        assert result.startswith("Error")
        assert "kein freier Slot" in result
        assert dialed == []

    async def test_happy_path_registers_task_for_call_sid(self, monkeypatch):
        registry, calls = _google_registry()
        dialed = []

        async def fake_call(**kwargs):
            dialed.append(kwargs)
            return CALL_OK

        monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_call)
        result = await schedule_appointment_call(
            registry,
            _settings(),
            target_number="+4930123456",
            contact_name="Dr. Müller",
            topic="Zahnreinigung",
            timeframe="next_week",
            language="de",
            attendees="user@example.com",
            user_id="u1",
            channel="telegram",
        )
        assert not result.startswith("Error")
        assert "Terminvorschläge" in result  # German report for a German call
        assert len(calls["freebusy"]) == 1
        assert dialed[0]["language"] == "de"
        assert dialed[0]["context"] == {"user_id": "u1", "channel": "telegram"}

        task = scheduling.get_appointment("CA_appt_1")
        assert task is not None
        assert task.attempts == 1
        assert len(task.candidates) == 3
        assert task.topic == "Zahnreinigung"


# ── T6.3: bounded in-call negotiation (channel level) ────────────────


class ScriptedHandler:
    def __init__(self, responses):
        self.responses = list(responses)
        self.received = []

    async def __call__(self, incoming):
        self.received.append(incoming)
        return self.responses.pop(0) if self.responses else "Okay."


async def _start_call(handler, task, call_sid="CA_appt_1", language="de"):
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    await channel.start(handler)
    scheduling.register_appointment(call_sid, task)
    state = await engine.on_call_start(call_sid, "+4930123456", CallDirection.OUTBOUND, language=language)
    return channel, engine, state


class TestInCallNegotiation:
    async def test_negotiation_rules_injected_into_system_context(self):
        handler = ScriptedHandler(["Guten Tag, ich rufe wegen eines Termins an."])
        channel, engine, _ = await _start_call(handler, _task())
        await engine.on_speech_input("CA_appt_1", "Praxis Dr. Müller, guten Tag?")
        extra = handler.received[0].extra_system
        assert "TERMIN-REGELN" in extra
        assert CANDIDATES[0] in extra
        await channel.stop()

    async def test_confirmed_slot_spoken_without_token_and_recorded(self):
        handler = ScriptedHandler(
            [
                "Ich schlage Dienstag, den 25. August um 14 Uhr vor — passt das?",
                "Also Dienstag, der 25. August um 14 Uhr, 30 Minuten — richtig?",
                f"[APPOINTMENT_CONFIRMED:{CANDIDATES[0]}] Wunderbar, dann bis Dienstag. Auf Wiederhören!",
            ]
        )
        task = _task()
        channel, engine, _ = await _start_call(handler, task)

        await engine.on_speech_input("CA_appt_1", "Guten Tag, worum geht es?")
        await engine.on_speech_input("CA_appt_1", "Dienstag 14 Uhr würde gehen.")
        await engine.on_speech_input("CA_appt_1", "Ja, das passt.")

        spoken = engine.spoken["CA_appt_1"]
        assert spoken[-1] == "Wunderbar, dann bis Dienstag. Auf Wiederhören!"
        assert all("[APPOINTMENT_CONFIRMED" not in s for s in spoken)
        assert task.status == "confirmed"
        assert task.agreed_start == CANDIDATES[0]
        transcript = channel.get_transcript("CA_appt_1")
        assert any(a.action_type == "appointment_confirmed" for a in transcript.actions)
        await channel.stop()

    async def test_out_of_slot_commitment_replaced_by_deferral(self):
        """Acceptance: the agent never commits to a slot outside the computed
        candidates — even if the LLM tries, the caller hears the deferral."""
        handler = ScriptedHandler(["[APPOINTMENT_CONFIRMED:2026-08-27T18:00:00+02:00] Abgemacht, Donnerstag 18 Uhr!"])
        task = _task()
        channel, engine, _ = await _start_call(handler, task)
        await engine.on_speech_input("CA_appt_1", "Es geht nur Donnerstag um 18 Uhr.")

        spoken = engine.spoken["CA_appt_1"]
        assert len(spoken) == 1
        assert "Abgemacht" not in spoken[0]
        assert "18 Uhr" not in spoken[0]
        assert task.status == "out_of_candidates"
        await channel.stop()


# ── T6.4: retry policy ───────────────────────────────────────────────


class TestRetryPolicy:
    async def test_non_appointment_call_is_ignored(self):
        assert await scheduling.handle_call_not_connected("CA_other", "no-answer", _settings()) is False

    async def test_voicemail_schedules_redial(self, monkeypatch, notifications):
        redials = []

        async def fake_place(task, settings):
            redials.append(task)
            scheduling.register_appointment("CA_appt_2", task)
            return "Call initiated successfully.\nCall SID: CA_appt_2"

        monkeypatch.setattr(scheduling, "_place_call", fake_place)

        task = _task(attempts=1)
        scheduling.register_appointment("CA_appt_1", task)
        settings = _settings(voice_retry_delay_min=1)

        # Shrink the sleep so the test observes the redial promptly
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await real_sleep(0)

        monkeypatch.setattr("pincer.voice.scheduling.asyncio.sleep", fast_sleep)

        handled = await scheduling.handle_call_not_connected("CA_appt_1", "voicemail", settings)
        assert handled is True
        assert scheduling.get_appointment("CA_appt_1") is None  # consumed
        assert any("voicemail" in text for _, _, text in notifications)

        await asyncio.gather(*scheduling._retry_tasks)
        assert len(redials) == 1
        assert scheduling.get_appointment("CA_appt_2") is task  # context re-attached

    async def test_gives_up_after_final_attempt_with_slot_summary(self, notifications):
        task = _task(attempts=3)  # initial + 2 retries already used
        scheduling.register_appointment("CA_appt_1", task)
        handled = await scheduling.handle_call_not_connected("CA_appt_1", "no-answer", _settings())
        assert handled is True
        assert scheduling._retry_tasks == set()
        final = notifications[-1][2]
        assert "3" in final
        assert "Dienstag, 25.08.2026 um 14:00 Uhr" in final  # discussed slots reported


# ── T6.4: post-call executor ─────────────────────────────────────────


class TestFinalizeAppointment:
    async def test_confirmed_writes_calendar_with_invitations_and_idempotency(self):
        registry, calls = _google_registry()
        task = _task(status="confirmed", agreed_start=CANDIDATES[0])

        note = await scheduling.finalize_appointment(registry, _settings(), task, "CA_appt_1", "de")

        assert "📅 Termin eingetragen" in note
        assert "https://cal.example/ev1" in note
        assert "user@example.com" in note
        args = calls["create"][0]
        assert "CA_appt_1" in args["description"]  # event links back to the call
        assert args["send_updates"] == "all"
        assert args["idempotency_key"] == "pincer-appointment-task-1"
        assert args["attendees"] == "user@example.com"
        assert args["start"] == CANDIDATES[0]
        assert args["end"] == "2026-08-25T14:30:00+02:00"
        assert args["timezone"] == "Europe/Berlin"

    async def test_meet_keyword_requests_meet_link(self):
        registry, calls = _google_registry()
        task = _task(status="confirmed", agreed_start=CANDIDATES[0], location_or_meet="meet")
        await scheduling.finalize_appointment(registry, _settings(), task, "CA1", "en")
        assert calls["create"][0].get("add_meet_link") is True

    async def test_write_failure_after_verbal_confirmation_is_honest(self, monkeypatch):
        """THE worst case: callee said yes, calendar write keeps failing.
        The report must say the event is NOT in the calendar and offer a
        retry — never claim success, never stay silent."""
        monkeypatch.setattr(scheduling, "CALENDAR_RETRY_DELAYS_S", (0.0, 0.0))
        boom = [RuntimeError("Google 503"), RuntimeError("Google 503"), RuntimeError("Google 503")]
        registry, calls = _google_registry(create_results=boom)
        task = _task(status="confirmed", agreed_start=CANDIDATES[0])

        note = await scheduling.finalize_appointment(registry, _settings(), task, "CA1", "de")

        assert len(calls["create"]) == 3  # initial + 2 retries with backoff
        assert "NICHT im Kalender" in note
        assert "erneut versuchen" in note
        assert "📅" not in note  # no success claim

    async def test_write_succeeds_on_retry(self, monkeypatch):
        monkeypatch.setattr(scheduling, "CALENDAR_RETRY_DELAYS_S", (0.0, 0.0))
        registry, calls = _google_registry(
            create_results=[RuntimeError("timeout"), "Event created: 'x'\nID: e2\nLink: https://cal.example/e2"]
        )
        task = _task(status="confirmed", agreed_start=CANDIDATES[0])
        note = await scheduling.finalize_appointment(registry, _settings(), task, "CA1", "en")
        assert len(calls["create"]) == 2
        assert "https://cal.example/e2" in note
        # Same idempotency key on both attempts — the retry can never duplicate
        assert calls["create"][0]["idempotency_key"] == calls["create"][1]["idempotency_key"]

    async def test_confirmed_without_google_reports_manual_step(self):
        task = _task(status="confirmed", agreed_start=CANDIDATES[0])
        note = await scheduling.finalize_appointment(ToolRegistry(), _settings(), task, "CA1", "en")
        assert "not connected" in note
        assert "manually" in note

    async def test_out_of_candidates_reports_deferral(self):
        task = _task(status="out_of_candidates", proposed_out_of_slot="2026-08-27T18:00:00+02:00")
        note = await scheduling.finalize_appointment(ToolRegistry(), _settings(), task, "CA1", "de")
        assert "außerhalb der freien Slots" in note
        assert "2026-08-27T18:00:00+02:00" in note

    async def test_no_agreement_lists_offered_slots(self):
        note = await scheduling.finalize_appointment(ToolRegistry(), _settings(), _task(), "CA1", "en")
        assert "No appointment was agreed" in note
        assert "Tuesday, August 25, 2026 at 14:00" in note


class TestPostCallIntegration:
    async def test_report_carries_appointment_note_and_clears_task(self):
        registry, _ = _google_registry()
        task = _task(status="confirmed", agreed_start=CANDIDATES[0])
        scheduling.register_appointment("CA_appt_1", task)

        state = MagicMock()
        state.language = "de"
        state.pincer_user_id = "u1"
        state.caller_number = "+4930123456"
        state.target_name = "Dr. Müller"
        state.target_number = "+4930123456"
        state.purpose = "Termin"
        state.duration_seconds = 120

        processor = PostCallProcessor(_settings(), llm=None, memory=None, db_path="", tool_registry=registry)
        report = await processor.process("CA_appt_1", state, None, completed=True)

        assert "📅 Termin eingetragen" in report
        assert "https://cal.example/ev1" in report
        assert scheduling.get_appointment("CA_appt_1") is None

    async def test_finalize_crash_still_reports_honestly(self, monkeypatch):
        scheduling.register_appointment("CA_appt_1", _task(status="confirmed", agreed_start=CANDIDATES[0]))

        async def crash(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(scheduling, "finalize_appointment", crash)
        state = MagicMock()
        state.language = "en"
        state.pincer_user_id = "u1"
        state.caller_number = "+4930123456"
        state.target_name = "Dr. Müller"
        state.target_number = "+4930123456"
        state.purpose = "Termin"
        state.duration_seconds = 120

        processor = PostCallProcessor(_settings(), llm=None, memory=None, db_path="", tool_registry=None)
        report = await processor.process("CA_appt_1", state, None, completed=True)
        assert "appointment processing failed" in report


# ── google__create_event: send_updates + idempotency ─────────────────


class TestCreateEventIdempotency:
    def _factory(self, existing_items=None):
        svc = MagicMock()
        svc.events.return_value.list.return_value.execute.return_value = {"items": existing_items or []}
        svc.events.return_value.insert.return_value.execute.return_value = {
            "id": "new-ev",
            "htmlLink": "https://cal.example/new-ev",
        }
        factory = MagicMock()

        async def get(_name):
            return svc

        factory.get = get
        return factory, svc

    async def test_existing_key_returns_without_insert(self):
        from pincer.integrations.google.tools_calendar import google__create_event

        factory, svc = self._factory(
            existing_items=[{"id": "old-ev", "summary": "Zahnreinigung", "htmlLink": "https://cal.example/old-ev"}]
        )
        result = await google__create_event(
            factory,
            summary="Zahnreinigung",
            start="2026-08-25T14:00:00+02:00",
            end="2026-08-25T14:30:00+02:00",
            idempotency_key="pincer-appointment-task-1",
        )
        assert "already exists" in result
        assert "old-ev" in result
        svc.events.return_value.insert.assert_not_called()

    async def test_new_event_carries_key_and_send_updates(self):
        from pincer.integrations.google.tools_calendar import google__create_event

        factory, svc = self._factory()
        await google__create_event(
            factory,
            summary="Zahnreinigung",
            start="2026-08-25T14:00:00+02:00",
            end="2026-08-25T14:30:00+02:00",
            attendees="user@example.com",
            send_updates="all",
            idempotency_key="key-1",
        )
        kwargs = svc.events.return_value.insert.call_args.kwargs
        assert kwargs["sendUpdates"] == "all"
        assert kwargs["body"]["extendedProperties"] == {"private": {"pincer_key": "key-1"}}
        assert kwargs["body"]["attendees"] == [{"email": "user@example.com"}]


# ── T6.1: dashboard endpoint ─────────────────────────────────────────


class TestScheduleEndpoint:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
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

    def test_503_without_agent(self, client):
        response = client.post(
            "/api/voice/schedule",
            json={"target_number": "+4930123456", "contact_name": "Dr. Müller", "topic": "Termin"},
        )
        assert response.status_code == 503

    def test_full_flow_from_dashboard(self, client, monkeypatch):
        registry, _ = _google_registry()

        async def fake_call(**kwargs):
            return CALL_OK

        monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_call)
        client.app.state.agent = SimpleNamespace(_tools=registry)

        response = client.post(
            "/api/voice/schedule",
            json={
                "target_number": "+4930123456",
                "contact_name": "Dr. Müller",
                "topic": "Zahnreinigung",
                "timeframe": "next_week",
                "language": "de",
                "attendees": "user@example.com",
            },
        )
        assert response.status_code == 201, response.text
        assert scheduling.get_appointment("CA_appt_1") is not None

    def test_zero_slots_maps_to_409(self, client):
        registry, _ = _google_registry(
            freebusy_text="primary: BUSY at:\n    2000-01-01T00:00:00Z → 2100-01-01T00:00:00Z"
        )
        client.app.state.agent = SimpleNamespace(_tools=registry)
        response = client.post(
            "/api/voice/schedule",
            json={
                "target_number": "+4930123456",
                "contact_name": "Dr. Müller",
                "topic": "Termin",
                "timeframe": "tomorrow",
            },
        )
        assert response.status_code == 409

    def test_validation_422(self, client):
        client.app.state.agent = SimpleNamespace(_tools=ToolRegistry())
        assert client.post("/api/voice/schedule", json={"target_number": "+4930123456"}).status_code == 422

    def test_requires_auth(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from pincer.api.server import create_app
        from pincer.config import get_settings_relaxed

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PINCER_DASHBOARD_TOKEN", "sched-token-1234")
        get_settings_relaxed.cache_clear()
        try:
            c = TestClient(create_app())
            response = c.post(
                "/api/voice/schedule",
                json={"target_number": "+4930123456", "contact_name": "X", "topic": "Y"},
            )
            assert response.status_code == 401
        finally:
            get_settings_relaxed.cache_clear()

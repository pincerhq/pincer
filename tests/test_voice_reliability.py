"""Unit tests for Sprint 1 voice reliability hardening.

Covers: phase-timeout spoken exits + force_terminal, the VoiceChannel
watchdog / brain-error escalation / call-end guarantees, latency metrics,
live status notifications, AMD voicemail handling, and the post-call
truthfulness assertion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from voice_harness.settings import apply_test_paths
from voice_harness.strict_async import FAIL_ON_UNAWAITED_COROUTINE

from pincer.channels.phone_calls import MAX_CONSECUTIVE_ERRORS, VoiceChannel
from pincer.voice import status_notify
from pincer.voice.metrics import CallMetrics, VoiceMetricsRegistry
from pincer.voice.state_machine import (
    PHASE_TIMEOUT_MESSAGES,
    PHASE_TIMEOUTS,
    CallPhase,
    CallStateMachine,
)
from pincer.voice.transcript import Speaker, TranscriptLogger
from pincer.voice.twiml_server import init_voice_routes, twilio_router

pytestmark = FAIL_ON_UNAWAITED_COROUTINE


@pytest.fixture(autouse=True)
def _clean_status_notify():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


# ── State machine: timeouts & forced terminal ─────────────────────────────────


class TestTimeoutMessages:
    def test_every_timed_phase_has_a_spoken_exit(self):
        for phase in PHASE_TIMEOUTS:
            if phase == CallPhase.RINGING:
                continue  # nobody is on the line yet
            sm = CallStateMachine("CA1")
            sm._state.phase = phase
            message = sm.get_timeout_message()
            assert message and message.strip(), f"{phase} timeout would be silence"
            assert "goodbye" in message.lower(), f"{phase} exit does not end the call politely"

    def test_messages_cover_all_defined_phases(self):
        for phase in PHASE_TIMEOUT_MESSAGES:
            assert PHASE_TIMEOUT_MESSAGES[phase].strip()


class TestForceTerminal:
    def test_force_terminal_from_any_phase(self):
        for phase in CallPhase:
            sm = CallStateMachine("CA1")
            sm._state.phase = phase
            sm.force_terminal(CallPhase.FAILED, reason="test")
            assert sm.is_terminal

    def test_force_terminal_records_transition(self):
        sm = CallStateMachine("CA1")
        sm.start_call()
        sm.force_terminal(CallPhase.FAILED, reason="hangup")
        assert sm.state.transitions[-1].reason == "hangup"
        assert sm.state.transitions[-1].to_phase == CallPhase.FAILED

    def test_force_terminal_rejects_non_terminal_target(self):
        sm = CallStateMachine("CA1")
        sm.force_terminal(CallPhase.EXECUTE, reason="bogus")
        assert sm.phase == CallPhase.FAILED

    def test_force_terminal_noop_when_already_terminal(self):
        sm = CallStateMachine("CA1")
        sm.force_terminal(CallPhase.COMPLETED, reason="done")
        sm.force_terminal(CallPhase.FAILED, reason="late")
        assert sm.phase == CallPhase.COMPLETED


# ── Metrics ───────────────────────────────────────────────────────────────────


class TestCallMetrics:
    def test_turn_latency_recorded(self):
        m = CallMetrics("CA1")
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        assert len(m.turn_latencies_s) == 1
        assert m.mean_turn_latency_s is not None

    def test_first_word_recorded_once(self):
        m = CallMetrics("CA1")
        m.mark_agent_speech_start()
        first = m.first_agent_word_s
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        assert m.first_agent_word_s == first

    def test_registry_summary(self):
        reg = VoiceMetricsRegistry()
        m = reg.start_call("CA1", engine="fake")
        m.mark_caller_utterance()
        m.mark_agent_speech_start()
        summary = reg.finish_call("CA1")
        assert summary is not None
        assert summary["engine"] == "fake"
        assert summary["turns"] == 1


# ── Status notifications (T1.5) ──────────────────────────────────────────────


class TestStatusNotify:
    async def test_three_stages_max_and_dedupe(self):
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA1", "user1", channel="telegram", target_number="+491234")

        assert await status_notify.notify_dialing("CA1")
        assert not await status_notify.notify_dialing("CA1")  # dedupe
        assert await status_notify.notify_connected("CA1")
        assert await status_notify.notify_ended("CA1", "completed (42s)")
        assert not await status_notify.notify_ended("CA1", "again")  # untracked after end

        assert len(sent) == 3
        assert sent[0].startswith("📞 Dialing")
        assert sent[1] == "📞 Connected"
        assert "completed (42s)" in sent[2]

    async def test_no_notifier_is_noop(self):
        status_notify.register_outbound_call("CA1", "user1")
        assert not await status_notify.notify_dialing("CA1")

    async def test_unknown_call_is_noop(self):
        async def notifier(user_id, channel, text):
            return True

        status_notify.set_status_notifier(notifier)
        assert not await status_notify.notify_connected("CA_unknown")

    async def test_origin_fields_are_tracked(self):
        status_notify.register_outbound_call(
            "CA1", "123", channel="telegram", pincer_user_id="alice", session_key="unified:alice"
        )
        info = status_notify.get_call_info("CA1")
        assert info is not None
        assert (info.pincer_user_id, info.session_key) == ("alice", "unified:alice")

    async def test_finalize_report_runs_once_per_call(self):
        sent: list[str] = []
        notes: list[tuple[str, str, str, str]] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        async def reporter(pincer_user_id, channel, session_key, text):
            notes.append((pincer_user_id, channel, session_key, text))

        status_notify.set_status_notifier(notifier)
        status_notify.set_session_reporter(reporter)
        status_notify.register_outbound_call(
            "CA1", "123", channel="telegram", target_name="Dentist", pincer_user_id="alice", session_key="k1"
        )

        assert await status_notify.finalize_report("CA1", "📞 Couldn't reach Dentist — busy.", "busy")
        assert not await status_notify.finalize_report("CA1", "again", "busy")
        assert not await status_notify.notify_ended("CA1", "late")
        await status_notify.drain()

        assert sent == ["📞 Couldn't reach Dentist — busy."]
        assert notes == [("alice", "telegram", "k1", "📞 Call report (Dentist):\n📞 Couldn't reach Dentist — busy.")]

    async def test_no_session_key_skips_the_session_report(self):
        """Dashboard, scheduled and demo calls have no chat to report into."""
        reporter = AsyncMock()
        status_notify.set_session_reporter(reporter)
        status_notify.register_outbound_call("CA1", "dashboard", channel="web")
        await status_notify.finalize_report("CA1", "report", "completed")
        await status_notify.drain()
        reporter.assert_not_awaited()

    async def test_a_failing_session_reporter_does_not_stop_the_push(self):
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.set_session_reporter(AsyncMock(side_effect=RuntimeError("db down")))
        status_notify.register_outbound_call("CA1", "123", channel="telegram", session_key="k1")
        assert await status_notify.finalize_report("CA1", "report", "completed")
        await status_notify.drain()
        assert sent == ["report"]
        assert status_notify.get_call_info("CA1") is None

    async def test_finalize_does_not_wait_for_a_busy_chat_session(self):
        """The chat's turn may be held for minutes (an approval prompt): the
        push and the caller (a voicemail hang-up, a webhook) go on at once."""
        import asyncio

        sent: list[str] = []
        release = asyncio.Event()
        notes: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        async def reporter(pincer_user_id, channel, session_key, text):
            await release.wait()
            notes.append(text)

        status_notify.set_status_notifier(notifier)
        status_notify.set_session_reporter(reporter)
        status_notify.register_outbound_call("CA1", "123", channel="web", session_key="web:alice:c1")

        assert await asyncio.wait_for(status_notify.finalize_report("CA1", "report", "voicemail"), 1)
        assert sent == ["report"] and notes == []

        release.set()
        await status_notify.drain()
        assert len(notes) == 1 and notes[0].endswith("report")


@pytest.mark.parametrize(
    ("completed", "end_code", "provider_status", "expected"),
    [
        (True, "", "", "completed"),
        (False, "", "", "failed"),
        (False, "llm_error", "", "failed"),
        (True, "shutdown", "", "completed"),
        (False, "busy", "busy", "busy"),
        (False, "no_answer", "", "no-answer"),
        (False, "voicemail", "", "voicemail"),
        (False, "call_setup", "canceled", "canceled"),
    ],
)
def test_final_outcome_code_is_the_same_on_every_path(completed, end_code, provider_status, expected):
    """The pipeline and the channel fallbacks store one code for the same call."""
    assert status_notify.final_outcome_code(completed, end_code, provider_status) == expected


class TestCallReportPersistence:
    """Issue #303: the routing of a call's final report survives a restart."""

    @pytest.fixture
    def store(self, tmp_path):
        db_path = tmp_path / "pincer.db"
        status_notify.set_report_store(db_path)
        return db_path

    @staticmethod
    async def _service(db_path):
        from pincer.services.voice import CallsService

        return await CallsService.for_path(db_path)

    @staticmethod
    def _capture() -> list[str]:
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        return sent

    @staticmethod
    def _restart() -> None:
        """What a process restart leaves of the in-memory tracking: nothing."""
        status_notify._outbound_calls.clear()

    async def test_origin_row_is_written_at_dial_time(self, store):
        await status_notify.track_outbound_call(
            "CA1",
            "123",
            channel="telegram",
            target_number="+4930123",
            target_name="Dentist",
            language="de",
            purpose="book",
            pincer_user_id="alice",
            session_key="unified:alice",
        )
        row = await (await self._service(store)).get_call_origin("CA1")
        assert row is not None
        assert (row["user_id"], row["pincer_user_id"], row["session_key"]) == ("123", "alice", "unified:alice")
        assert (row["channel"], row["language"], row["target_name"]) == ("telegram", "de", "Dentist")
        assert row["reported_at"] is None

    async def test_finalize_stores_the_report_once(self, store):
        sent = self._capture()
        await status_notify.track_outbound_call("CA1", "123", channel="telegram", session_key="k1")
        assert await status_notify.finalize_report("CA1", "report", "no-answer")

        # A second process (or a replayed webhook) finds the report already out.
        status_notify.register_outbound_call("CA1", "123", channel="telegram", session_key="k1")
        assert not await status_notify.finalize_report("CA1", "report again", "no-answer")

        assert sent == ["report"]
        row = await (await self._service(store)).get_call_origin("CA1")
        assert (row["report_outcome"], row["report_text"]) == ("no-answer", "report")
        assert row["reported_at"]

    async def test_a_report_without_an_origin_row_is_still_stored(self, store):
        """The dial-time write failed: the final report writes the whole row."""
        self._capture()
        status_notify.register_outbound_call("CA1", "123", channel="web", pincer_user_id="alice", session_key="k1")
        await status_notify.finalize_report("CA1", "report", "busy")
        reports = await (await self._service(store)).list_call_reports("alice", "k1")
        assert [(r["call_sid"], r["report_outcome"]) for r in reports] == [("CA1", "busy")]

    async def test_a_dial_time_row_landing_mid_claim_is_still_reported(self, store, monkeypatch):
        """The origin insert commits between the claim's UPDATE (no row yet)
        and its read: the report is claimed and sent, not taken as delivered."""
        from pincer.services.voice import CallsService

        sent = self._capture()
        status_notify.register_outbound_call("CA1", "123", channel="telegram")
        real_complete = CallsService.complete_call_report
        calls = 0

        async def complete_racing_the_insert(service, call_sid, outcome, text, reported_at):
            nonlocal calls
            calls += 1
            if calls == 1:
                await service.save_call_origin({"call_sid": call_sid, "user_id": "123", "created_at": reported_at})
                return False
            return await real_complete(service, call_sid, outcome, text, reported_at)

        monkeypatch.setattr(CallsService, "complete_call_report", complete_racing_the_insert)
        assert await status_notify.finalize_report("CA1", "report", "busy")

        assert sent == ["report"]
        row = await (await self._service(store)).get_call_origin("CA1")
        assert (row["report_outcome"], row["report_text"]) == ("busy", "report")
        assert row["reported_at"]

    async def test_a_database_failure_at_dial_time_keeps_the_call_tracked(self, store, monkeypatch):
        from pincer.services.voice import CallsService

        monkeypatch.setattr(CallsService, "save_call_origin", AsyncMock(side_effect=RuntimeError("db down")))
        await status_notify.track_outbound_call("CA1", "123", channel="telegram")
        assert status_notify.get_call_user("CA1") == "123"

    async def test_hydrate_loads_only_recent_unreported_calls(self, store):
        from datetime import UTC, datetime, timedelta

        service = await self._service(store)
        now = datetime.now(UTC)
        base = {"user_id": "123", "channel": "telegram", "language": "de", "session_key": "k1"}
        await service.save_call_origin({**base, "call_sid": "CA_live", "created_at": now.isoformat()})
        old = (now - timedelta(seconds=600 + status_notify.RINGING_ALLOWANCE_S + 60)).isoformat()
        await service.save_call_origin({**base, "call_sid": "CA_old", "created_at": old})
        await service.save_call_origin({**base, "call_sid": "CA_done", "created_at": now.isoformat()})
        await service.complete_call_report("CA_done", "completed", "report", now.isoformat())

        assert await status_notify.hydrate(600) == ["CA_live"]
        info = status_notify.get_call_info("CA_live")
        assert info is not None and info.restored
        assert (info.user_id, info.language, info.session_key) == ("123", "de", "k1")
        assert status_notify.get_call_language("CA_live") == "de"

    async def test_reconcile_finalizes_calls_that_ended_while_down(self, store):
        sent = self._capture()
        for sid in ("CA_busy", "CA_cut", "CA_ringing", "CA_unknown"):
            await status_notify.track_outbound_call(sid, "123", channel="telegram", target_name="Dentist")
        self._restart()
        loaded = await status_notify.hydrate(600)
        assert sorted(loaded) == ["CA_busy", "CA_cut", "CA_ringing", "CA_unknown"]

        statuses = {"CA_busy": "busy", "CA_cut": "completed", "CA_ringing": "ringing"}

        async def fetch(sid):
            if sid not in statuses:
                raise RuntimeError("twilio down")
            return statuses[sid]

        await status_notify.reconcile(loaded, fetch, lambda _sid: False)

        assert len(sent) == 2
        assert any("Couldn't reach Dentist" in text for text in sent)
        assert any("cut off by a Pincer restart" in text for text in sent)
        # Still live or unknown: left for the /status callback.
        assert status_notify.get_call_info("CA_ringing") is not None
        assert status_notify.get_call_info("CA_unknown") is not None
        service = await self._service(store)
        assert (await service.get_call_origin("CA_busy"))["report_outcome"] == "busy"
        assert (await service.get_call_origin("CA_cut"))["report_outcome"] == "interrupted"
        assert (await service.get_call_origin("CA_ringing"))["reported_at"] is None

    async def test_reconcile_skips_calls_with_live_state(self, store):
        sent = self._capture()
        await status_notify.track_outbound_call("CA1", "123", channel="telegram")
        fetch = AsyncMock(return_value="completed")
        await status_notify.reconcile(["CA1"], fetch, lambda _sid: True)
        fetch.assert_not_awaited()
        assert sent == []

    async def test_reconcile_is_bounded(self, store):
        import asyncio

        await status_notify.track_outbound_call("CA1", "123", channel="telegram")

        async def hang(_sid):
            await asyncio.sleep(10)
            return "completed"

        await asyncio.wait_for(status_notify.reconcile(["CA1"], hang, lambda _sid: False, timeout=0.05), 1)
        assert status_notify.get_call_info("CA1") is not None

    async def test_a_timeout_mid_finalize_still_delivers_the_claimed_report(self, store):
        """The reconcile timeout fires after the report was claimed: the push
        still happens, so the row is never marked reported without a message."""
        import asyncio

        sent: list[str] = []
        pushing = asyncio.Event()

        async def slow_notifier(user_id, channel, text):
            pushing.set()
            await asyncio.sleep(0.2)
            sent.append(text)
            return True

        status_notify.set_status_notifier(slow_notifier)
        await status_notify.track_outbound_call("CA1", "123", channel="telegram", target_name="Dentist")

        async def busy(_sid):
            return "busy"

        await status_notify.reconcile(["CA1"], busy, lambda _sid: False, timeout=0.05)
        assert pushing.is_set() and sent == []
        await status_notify.drain()
        assert len(sent) == 1 and "Couldn't reach Dentist" in sent[0]
        row = await (await self._service(store)).get_call_origin("CA1")
        assert row["report_outcome"] == "busy"

    async def test_status_callback_after_a_restart_reports_once(self, store):
        """Restart while connected: Twilio's `completed` arrives at a process
        with no state for the call. One interrupted report, never two."""
        sent = self._capture()
        await status_notify.track_outbound_call("CA1", "123", channel="telegram", target_name="Dentist")
        self._restart()
        await status_notify.hydrate(600)

        engine = MagicMock()
        engine.get_call_state.return_value = None
        engine.was_recently_ended.return_value = False
        settings = apply_test_paths(MagicMock(), store.parent)
        settings.twilio_auth_token.get_secret_value.return_value = ""
        init_voice_routes(engine, settings)
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(twilio_router)
        client = TestClient(app)
        for _ in range(2):
            r = client.post("/api/apps/twilio/status", data={"CallSid": "CA1", "CallStatus": "completed"})
            assert r.status_code == 200

        assert len(sent) == 1
        assert "cut off by a Pincer restart" in sent[0]

    async def test_status_callback_before_hydrate_still_finds_the_call(self, store):
        """Restart while ringing, the rejection arriving before the reload ran."""
        sent = self._capture()
        await status_notify.track_outbound_call("CA1", "123", channel="telegram", target_name="Dentist")
        self._restart()

        engine = MagicMock()
        engine.get_call_state.return_value = None
        engine.was_recently_ended.return_value = False
        settings = apply_test_paths(MagicMock(), store.parent)
        settings.twilio_auth_token.get_secret_value.return_value = ""
        init_voice_routes(engine, settings)
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(twilio_router)
        r = TestClient(app).post("/api/apps/twilio/status", data={"CallSid": "CA1", "CallStatus": "busy"})
        assert r.status_code == 200

        assert len(sent) == 1
        assert "busy" in sent[0].lower() or "besetzt" in sent[0]


# ── Truthfulness assertion (T1.4) ────────────────────────────────────────────


class TestCompletionClaims:
    def test_claim_without_action_is_flagged(self, caplog):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "Great, your appointment is booked for Tuesday.")
        with caplog.at_level("WARNING"):
            claims = t.verify_completion_claims()
        assert len(claims) == 1
        assert "Unverified completion claim" in caplog.text

    def test_claim_with_successful_action_passes(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "All set — I've booked it for Tuesday at three.")
        t.log_action("tool_call", "calendar_create", output_summary="Event created: link")
        assert t.verify_completion_claims() == []

    def test_claim_with_error_action_is_flagged(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "It's done, I've sent the email.")
        t.log_action("tool_call", "email_send", output_summary="Error: SMTP timeout")
        assert len(t.verify_completion_claims()) == 1

    def test_caller_claims_are_ignored(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.CALLER, "I've booked it myself already.")
        assert t.verify_completion_claims() == []

    def test_no_claim_no_flag(self):
        t = TranscriptLogger("CA1")
        t.log_utterance(Speaker.AGENT, "I'm calling about the appointment. Is Tuesday okay?")
        assert t.verify_completion_claims() == []


# ── VoiceChannel hardening ────────────────────────────────────────────────────


def _make_channel():
    from voice_harness.fake_engine import FakeVoiceEngine

    settings = apply_test_paths(MagicMock())
    settings.voice_language = "en-US"
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    return channel, engine


class TestVoiceChannelHardening:
    async def test_phase_timeout_speaks_and_ends(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Hello! How can I help?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)
        sm = channel._ensure_call_tracking("CA1", state)

        sm._state.phase_entered_at -= PHASE_TIMEOUTS[sm.phase] + 1
        assert sm.check_timeout()
        await channel._handle_phase_timeout("CA1", sm)

        assert sm.is_terminal
        assert "CA1" in engine.ended
        assert any("goodbye" in s.lower() for s in engine.spoken["CA1"])
        await channel.stop()

    async def test_watchdog_survives_one_failing_call(self, monkeypatch):
        """A failure handling one call's timeout must not stop timeout
        enforcement for the other concurrently active calls."""
        import asyncio

        from pincer.voice.engine import CallDirection

        monkeypatch.setattr("pincer.channels.phone_calls.WATCHDOG_INTERVAL_S", 0.01)
        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Hello! How can I help?"))
        state_bad = await engine.on_call_start("CA_bad", "+1", CallDirection.INBOUND)
        state_good = await engine.on_call_start("CA_good", "+2", CallDirection.INBOUND)
        sm_bad = channel._ensure_call_tracking("CA_bad", state_bad)
        sm_good = channel._ensure_call_tracking("CA_good", state_good)

        original = channel._handle_phase_timeout

        async def exploding(call_sid, sm):
            if call_sid == "CA_bad":
                raise RuntimeError("unexpected phase")
            await original(call_sid, sm)

        channel._handle_phase_timeout = exploding

        # Both time out; CA_bad is first in iteration order and raises.
        sm_bad._state.phase_entered_at -= PHASE_TIMEOUTS[sm_bad.phase] + 1
        sm_good._state.phase_entered_at -= PHASE_TIMEOUTS[sm_good.phase] + 1

        await asyncio.sleep(0.1)
        assert sm_good.is_terminal
        assert "CA_good" in engine.ended
        assert not channel._watchdog_task.done()
        await channel.stop()

    async def test_brain_errors_escalate_to_graceful_end(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        failing_handler = AsyncMock(side_effect=RuntimeError("LLM down"))
        await channel.start(failing_handler)
        await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)

        for _ in range(MAX_CONSECUTIVE_ERRORS):
            await channel._handle_speech("CA1", "Hello?")

        sm_phase_seen = False
        for spoken in engine.spoken["CA1"]:
            if "goodbye" in spoken.lower():
                sm_phase_seen = True
        assert sm_phase_seen, "no spoken apology/goodbye on repeated errors"
        assert "CA1" in engine.ended
        await channel.stop()

    async def test_call_end_forces_terminal_and_notifies(self):
        from pincer.voice.engine import CallDirection

        sent: list[str] = []

        async def notifier(user_id, channel_name, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA1", "user1", channel="telegram")

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Okay. Anything else?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.OUTBOUND)
        sm = channel._ensure_call_tracking("CA1", state)
        assert not sm.is_terminal

        # Callee hangs up mid-call
        await engine.end_call("CA1")

        assert sm.is_terminal
        assert channel.get_state_machine("CA1") is None
        assert any(msg.startswith("📞 Call ended") for msg in sent)
        await channel.stop()

    async def test_greeting_advances_on_speech(self):
        from pincer.voice.engine import CallDirection

        channel, engine = _make_channel()
        await channel.start(AsyncMock(return_value="Sure. What time works?"))
        state = await engine.on_call_start("CA1", "+1234", CallDirection.INBOUND)
        sm = channel._ensure_call_tracking("CA1", state)
        assert sm.phase == CallPhase.GREETING

        await channel._handle_speech("CA1", "Hi, I need to book something.")
        assert sm.phase == CallPhase.INTENT_CAPTURE
        await channel.stop()


# ── AMD / status webhook (T1.3) ──────────────────────────────────────────────


@pytest.fixture
def webhook_client():
    from fastapi import FastAPI

    engine = AsyncMock()
    engine.get_active_calls = MagicMock(return_value={})
    engine.get_call_state = MagicMock(return_value=None)
    # Sync on the real engine: left as an AsyncMock it returns an un-awaited
    # coroutine, and these tests exercise calls that never had state.
    engine.was_recently_ended = MagicMock(return_value=False)
    settings = MagicMock()
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token.get_secret_value.return_value = ""
    init_voice_routes(engine, settings)
    app = FastAPI()
    app.include_router(twilio_router)
    return TestClient(app), engine


class TestAnsweringMachineDetection:
    def _setup_notify(self) -> list[str]:
        sent: list[str] = []

        async def notifier(user_id, channel, text):
            sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call("CA_amd", "user1", channel="telegram")
        return sent

    def test_machine_detected_hangs_up_and_reports(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_called_once_with("CA_amd")
        assert any("voicemail" in msg for msg in sent)

    def test_completed_webhook_leaves_the_report_to_the_pipeline(self, webhook_client):
        """The teardown race: `end_call` pops the CallState and hands the final
        message to a background post-call task. Twilio's own `completed`
        webhook can land in that window, find no state, and send the generic
        "call ended" — which consumes the ENDED stage, so the voicemail report
        the pipeline then tries to deliver is silently dropped.
        """
        client, engine = webhook_client
        sent = self._setup_notify()
        # The AMD hangup already tore the call down moments ago.
        engine.was_recently_ended = MagicMock(return_value=True)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "completed"},
        )

        assert response.status_code == 200
        assert sent == [], "the generic ending must not pre-empt the pipeline's report"
        # The ENDED stage is still free, so the real report still gets through.
        import asyncio

        assert asyncio.run(status_notify.notify_ended("CA_amd", "voicemail detected")) is True
        assert any("voicemail" in msg for msg in sent)

    def test_completed_webhook_still_reports_a_call_that_never_connected(self, webhook_client):
        """The guard must not silence the case it was protecting: a call that
        genuinely never had state still gets its final status."""
        client, engine = webhook_client
        sent = self._setup_notify()
        engine.was_recently_ended = MagicMock(return_value=False)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "completed"},
        )

        assert response.status_code == 200
        assert any("call ended" in msg for msg in sent)

    def test_not_connected_webhook_leaves_the_report_to_the_pipeline(self, webhook_client):
        """Same race on the busy/no-answer/failed branch."""
        client, engine = webhook_client
        sent = self._setup_notify()
        engine.was_recently_ended = MagicMock(return_value=True)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "no-answer"},
        )

        assert response.status_code == 200
        assert sent == []

    def test_machine_verdict_ignored_when_caller_already_spoke(self, webhook_client):
        """AMD false positive: a late 'machine_start' must not kill a live
        conversation (hotfix — a real call died 20s in exactly this way)."""
        from pincer.voice.engine import CallDirection, CallState

        client, engine = webhook_client
        sent = self._setup_notify()

        state = CallState(call_sid="CA_amd", direction=CallDirection.OUTBOUND, caller_number="+1")
        state.metadata["caller_spoke"] = True
        engine.get_call_state = MagicMock(return_value=state)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_not_called()
        assert not any("voicemail" in msg for msg in sent)
        assert "📞 Connected" in sent  # falls through to normal in-progress handling

    def test_machine_verdict_hangs_up_despite_transcribed_greeting(self, webhook_client):
        """The transcribed voicemail greeting arrives within the AMD grace
        window, so it must not count as a conversing caller — the machine
        verdict still hangs up."""
        from pincer.voice.engine import CallDirection, CallState

        client, engine = webhook_client
        self._setup_notify()

        state = CallState(call_sid="CA_amd", direction=CallDirection.OUTBOUND, caller_number="+1")
        state.mark_caller_spoke()  # greeting transcribed seconds after answer
        engine.get_call_state = MagicMock(return_value=state)

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_called_once_with("CA_amd")
        # Live state -> the post-call pipeline delivers the report; the webhook
        # records the cause (a code, not display text) instead of notifying directly.
        assert state.metadata["end_code"] == "voicemail"

    def test_human_answer_notifies_connected(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "in-progress", "AnsweredBy": "human"},
        )
        assert response.status_code == 200
        engine.end_call.assert_not_called()
        assert "📞 Connected" in sent

    def test_no_answer_reports_final_status(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/status",
            data={"CallSid": "CA_amd", "CallStatus": "no-answer"},
        )
        assert response.status_code == 200
        assert any("no answer" in msg for msg in sent)

    def test_async_amd_endpoint_hangs_up_on_machine(self, webhook_client):
        """The async AMD verdict arrives on its own endpoint and still acts."""
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/amd",
            data={"CallSid": "CA_amd", "AnsweredBy": "machine_start"},
        )
        assert response.status_code == 200
        engine.end_call.assert_called_once_with("CA_amd")
        assert any("voicemail" in msg for msg in sent)

    def test_async_amd_endpoint_leaves_human_calls_alone(self, webhook_client):
        client, engine = webhook_client
        sent = self._setup_notify()

        response = client.post(
            "/api/apps/twilio/amd",
            data={"CallSid": "CA_amd", "AnsweredBy": "human"},
        )
        assert response.status_code == 200
        engine.end_call.assert_not_called()
        # The AMD callback reports nothing on its own — /status owns the
        # "Connected" notification, and duplicating it here would burn one of
        # the three status updates a call is allowed.
        assert sent == []


class TestConversationRelayProtocol:
    """CR protocol hygiene (hotfix): no Media-Streams messages on the CR socket."""

    async def test_interrupt_sends_nothing_over_websocket(self):
        # {"type": "clear"} is invalid on ConversationRelay (Twilio 64107);
        # Twilio handles barge-in itself, so interrupt must be a local no-op.
        from pincer.voice.engine import CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr", "+1", CallDirection.OUTBOUND)
        ws = AsyncMock()
        state.metadata["websocket"] = ws

        await engine.interrupt_speech("CA_cr")
        ws.send_text.assert_not_called()

    async def test_speech_input_marks_caller_spoke_after_amd_grace(self):
        from datetime import UTC, datetime, timedelta

        from pincer.voice.engine import AMD_GRACE_SECONDS, CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr2", "+1", CallDirection.OUTBOUND)
        assert "caller_spoke" not in state.metadata
        state.started_at = datetime.now(UTC) - timedelta(seconds=AMD_GRACE_SECONDS + 1)
        await engine.on_speech_input("CA_cr2", "Hello?")
        assert state.metadata["caller_spoke"] is True

    async def test_speech_input_within_amd_grace_does_not_mark_caller_spoke(self):
        """A voicemail greeting is transcribed like any speech — speech inside
        the AMD grace window must not shield the call from the AMD verdict."""
        from pincer.voice.engine import CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr3", "+1", CallDirection.OUTBOUND)
        await engine.on_speech_input("CA_cr3", "You've reached Jane, please leave a message.")
        assert "caller_spoke" not in state.metadata

    async def test_send_speech_reports_delivery(self):
        """F4: True only when the token reached the socket — silence is never
        reported as success, and a failure says why (#301)."""
        from pincer.voice.engine import SPEECH_FAILURE_KEY, CallDirection, ConversationRelayEngine

        engine = ConversationRelayEngine(MagicMock())
        state = await engine.on_call_start("CA_cr3", "+1", CallDirection.OUTBOUND)

        # No websocket attached yet → dropped
        assert await engine.send_speech("CA_cr3", "hello") is False
        assert state.metadata[SPEECH_FAILURE_KEY] == "no_websocket"

        ws = AsyncMock()
        state.metadata["websocket"] = ws
        assert await engine.send_speech("CA_cr3", "hello") is True
        ws.send_text.assert_called_once()
        assert state.metadata[SPEECH_FAILURE_KEY] == ""

        ws.send_text.side_effect = RuntimeError("socket closed")
        assert await engine.send_speech("CA_cr3", "hello") is False
        assert state.metadata[SPEECH_FAILURE_KEY] == "send_error"

        # Unknown call
        assert await engine.send_speech("CA_nope", "hello") is False


class TestUndeliveredTranscript:
    """F4: the transcript distinguishes 'text generated' from 'audio heard'."""

    async def test_undelivered_agent_turn_marked(self):
        from pincer.voice.engine import CallDirection, VoiceEngine

        class SilentEngine(VoiceEngine):
            """send_speech always fails to deliver (no socket)."""

            @property
            def engine_name(self):
                return "silent"

            async def on_call_start(self, call_sid, caller, direction, **kwargs):
                return await self._register_call(call_sid, caller, direction)

            async def on_speech_input(self, call_sid, text_or_audio):
                pass

            async def send_speech(self, call_sid, text_or_audio, *, last=True):
                return False

            async def interrupt_speech(self, call_sid):
                pass

            async def transfer_call(self, call_sid, target_number):
                pass

            async def end_call(self, call_sid):
                await self._unregister_call(call_sid)

            async def send_dtmf(self, call_sid, digits):
                pass

            async def close_media_stream(self, call_sid):
                pass

        settings = apply_test_paths(MagicMock())
        settings.voice_default_language = "en"
        settings.voice_supported_languages = "en,de,uk"
        settings.voice_language = "en-US"
        channel = VoiceChannel(settings)
        engine = SilentEngine(settings)
        channel.set_engine(engine)

        async def handler(incoming):
            return "I have booked your appointment."

        await channel.start(handler)
        await engine.on_call_start("CA_silent", "+1", CallDirection.OUTBOUND)
        await channel._handle_speech("CA_silent", "Please book it.")

        transcript = channel.get_transcript("CA_silent")
        agent_entries = [e for e in transcript.entries if e.speaker == Speaker.AGENT]
        assert agent_entries and agent_entries[-1].state == "undelivered"
        # the engine named no cause, so the line says so rather than guess
        assert agent_entries[-1].delivery_reason == "unknown"
        await channel.stop()


# ── Outbound AMD parameter ────────────────────────────────────────────────────


def _fake_outbound_dial(monkeypatch, tmp_path) -> dict:
    """Settings and a fake Twilio for `make_phone_call`; returns the dial kwargs once placed."""
    settings = MagicMock()
    settings.daily_budget_usd = 0.0  # no LLM budget
    settings.voice_enabled = True
    settings.voice_outbound_enabled = True
    settings.voice_webhook_base_url = "https://example.com"
    settings.voice_outbound_max_daily = 10
    settings.voice_max_call_duration = 600
    settings.voice_engine = "conversation_relay"
    settings.voice_language = "en-US"
    settings.voice_machine_detection = True
    settings.voice_consent_mode = "none"
    settings.voice_recording_enabled = False
    settings.voice_consent_language = ""
    settings.voice_assistant_name = ""
    settings.voice_intro_text = ""
    # Sprint 8: the outbound gate persists its call log here; without a real
    # path the MagicMock stringifies into a junk file in the repo root.
    settings.db_path = str(tmp_path / "pincer.db")
    settings.twilio_account_sid = "AC123"
    settings.twilio_auth_token.get_secret_value.return_value = "token"
    settings.twilio_phone_number = "+15550001111"
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)

    captured: dict = {}

    class FakeCalls:
        async def create_async(self, **kwargs):
            captured.update(kwargs)
            call = MagicMock()
            call.sid = "CA_out"
            return call

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = FakeCalls()

    from voice_harness.transport_fakes import install_fake_twilio

    install_fake_twilio(monkeypatch, FakeClient)
    # an engine another test left in twiml_server would take the dial instead
    monkeypatch.setattr("pincer.voice.twiml_server._engine", None)
    return captured


class TestOutboundMachineDetection:
    async def test_machine_detection_param_sent(self, monkeypatch, tmp_path):
        from pincer.voice import outbound

        captured = _fake_outbound_dial(monkeypatch, tmp_path)

        result = await outbound.make_phone_call("+14155551234", "test purpose", context={"user_id": "u1"})
        assert "Call initiated" in result
        assert captured.get("machine_detection") == "Enable"
        # Synchronous AMD (Twilio's default) blocks the call until detection
        # finishes, so the callee answers and hears silence — no greeting, no
        # relay — for up to machine_detection_timeout. The dial must always
        # ask for async AMD and take the verdict on the callback instead.
        assert captured.get("async_amd") == "true"
        assert captured.get("async_amd_status_callback") == "https://example.com/api/apps/twilio/amd"
        assert captured.get("async_amd_status_callback_method") == "POST"

    async def test_the_dial_records_the_chat_it_was_asked_in(self, monkeypatch, tmp_path):
        """Issue #303: the origin row carries the session key, and web chat is
        told the result comes into the chat (it gets no "connected" push)."""
        from pincer.services.voice import CallsService
        from pincer.voice import outbound

        _fake_outbound_dial(monkeypatch, tmp_path)
        status_notify.set_report_store(tmp_path / "pincer.db")
        context = {"user_id": "alice", "pincer_user_id": "alice", "channel": "web", "session_key": "web:alice:c1"}

        result = await outbound.make_phone_call("+14155551234", "test purpose", context=context)

        assert "The result will appear in this chat when the call ends." in result
        assert "when it connects" not in result
        row = await (await CallsService.for_path(tmp_path / "pincer.db")).get_call_origin("CA_out")
        assert row is not None
        assert (row["pincer_user_id"], row["channel"], row["session_key"]) == ("alice", "web", "web:alice:c1")

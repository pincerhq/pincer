"""Sprint 13 §4 — how calls actually get into threads.

Drives the real production paths (`make_phone_call`, the appointment
`_place_call` retry loop, the voice channel's call-start hook) against a fake
Twilio, so the linking rules are tested where they are enforced rather than in
a re-implementation of them.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from pincer.voice import outbound, scheduling
from pincer.voice import threads as th
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.threads import (
    KIND_FOLLOWUP,
    KIND_INBOUND_MATCHED,
    KIND_ORIGIN,
    KIND_RETRY,
    ThreadManager,
)

TARGET = "+4930111222"


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = True
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.voice_outbound_max_daily = 100
    cfg.voice_max_call_duration = 600
    cfg.voice_machine_detection = False
    cfg.voice_engine = "conversation_relay"
    cfg.voice_default_language = "de"
    cfg.voice_supported_languages = "en,de"
    cfg.voice_language = "de-DE"
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.voice_consent_mode = "two_party"
    cfg.voice_consent_language = ""
    cfg.voice_recording_enabled = False
    cfg.voice_assistant_name = "Pincer"
    cfg.voice_assistant_org = "3days.ai"
    cfg.voice_assistant_owner = ""
    cfg.voice_intro_text = ""
    cfg.voice_ws_auth_required = False
    cfg.twilio_account_sid = "AC123"
    cfg.twilio_auth_token.get_secret_value.return_value = "token"
    cfg.twilio_phone_number = "+4915212345678"
    cfg.voice_daily_call_limit = 20
    cfg.voice_target_cooldown_min = 0
    cfg.voice_quiet_hours = ""
    cfg.voice_quiet_hours_override_users = ""
    cfg.voice_retry_attempts = 2
    cfg.voice_retry_delay_min = 30
    # Sprint 13
    cfg.thread_match_window_days = 7
    cfg.thread_inbound_context = "off"
    cfg.thread_autoclose_days = 30
    cfg.dashboard_url = ""
    cfg.default_user_id = "u1"
    return cfg


@pytest.fixture
def manager(settings) -> ThreadManager:
    """The one ThreadManager the production code will find via the singleton."""
    mgr = ThreadManager(settings.db_path, settings=settings)
    th.set_thread_manager(mgr)
    return mgr


@pytest.fixture
def dialer(settings, manager, monkeypatch):
    """`make_phone_call` against a fake Twilio; returns the call helper."""
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    placed: list[dict[str, Any]] = []

    class FakeCalls:
        def create(self, **kwargs: Any) -> Any:
            placed.append(kwargs)
            call = MagicMock()
            call.sid = f"CA_{len(placed)}"
            return call

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = FakeCalls()

    twilio_rest = types.ModuleType("twilio.rest")
    twilio_rest.Client = FakeClient
    twilio_mod = types.ModuleType("twilio")
    twilio_mod.rest = twilio_rest
    monkeypatch.setitem(sys.modules, "twilio", twilio_mod)
    monkeypatch.setitem(sys.modules, "twilio.rest", twilio_rest)
    monkeypatch.setattr(outbound, "_daily_outbound_counts", {})

    async def call(**kwargs: Any) -> str:
        kwargs.setdefault("target_number", TARGET)
        kwargs.setdefault("purpose", "Termin bei Dr. Müller bestätigen")
        kwargs.setdefault("context", {"user_id": "u1", "channel": "telegram"})
        return await outbound.make_phone_call(**kwargs)

    call.placed = placed  # type: ignore[attr-defined]
    return call


# ── make_phone_call (§2, §4.2) ───────────────────────────────────────


async def test_task_call_opens_a_thread(dialer, manager):
    result = await dialer(target_name="Dr. Müller")
    assert "Call SID: CA_1" in result

    thread_id = await manager.thread_for_call("CA_1")
    assert thread_id
    thread = await manager.require(thread_id)
    assert thread.subject == "Termin bei Dr. Müller bestätigen"
    assert thread.origin == "user_task"
    assert thread.primary_number == TARGET
    assert thread.contact_name == "Dr. Müller"
    assert thread.language == "de"
    assert [c.attach_kind for c in await manager.calls(thread_id)] == [KIND_ORIGIN]


async def test_followup_param_attach(dialer, manager):
    """§4.2: a supplied thread_id attaches the new call as a follow-up."""
    await dialer()
    thread_id = await manager.thread_for_call("CA_1")

    await dialer(thread_id=thread_id, purpose="Nochmal wegen des Termins anrufen")
    assert await manager.thread_for_call("CA_2") == thread_id
    assert [c.attach_kind for c in await manager.calls(thread_id)] == [KIND_ORIGIN, KIND_FOLLOWUP]
    # No second thread was opened.
    assert len(await manager.list_threads()) == 1


async def test_followup_reopens_a_resolved_thread(dialer, manager):
    await dialer()
    thread_id = await manager.thread_for_call("CA_1")
    await manager.resolve(thread_id, reason="done")

    await dialer(thread_id=thread_id)
    assert (await manager.require(thread_id)).status == "open"


async def test_closed_thread_is_refused_before_the_dial(dialer, manager):
    """A rejected follow-up must not cost a phone call."""
    await dialer()
    thread_id = await manager.thread_for_call("CA_1")
    await manager.close(thread_id, reason="test")

    result = await dialer(thread_id=thread_id)
    assert result.startswith("Error:") and "closed" in result
    assert len(dialer.placed) == 1  # the second dial never happened


async def test_unknown_thread_is_refused_before_the_dial(dialer):
    result = await dialer(thread_id="thr_doesnotexist")
    assert result.startswith("Error:") and "does not exist" in result
    assert dialer.placed == []


async def test_thread_failure_never_breaks_the_call(dialer, manager, monkeypatch):
    """Thread bookkeeping is a convenience layer — if it dies, the call stands."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("thread store down")

    monkeypatch.setattr(ThreadManager, "create", _boom)
    result = await dialer()
    assert "Call SID: CA_1" in result
    assert await manager.thread_for_call("CA_1") == ""


# ── Appointment retries (§4.1) ───────────────────────────────────────


async def test_retry_inherits_thread(dialer, manager, settings):
    """Sprint 6's redial of the SAME booking stays in the SAME thread."""
    task = scheduling.AppointmentTask(
        task_id="t1",
        user_id="u1",
        channel="telegram",
        target_number=TARGET,
        contact_name="Dr. Müller",
        topic="Kontrolltermin",
        timeframe="next_week",
        duration_minutes=30,
        language="de",
        candidates=[datetime(2026, 8, 25, 10, 0, tzinfo=UTC).isoformat()],
    )

    assert "Call SID: CA_1" in await scheduling._place_call(task, settings)
    thread_id = await manager.thread_for_call("CA_1")
    assert thread_id and task.thread_id == thread_id
    assert (await manager.require(thread_id)).subject == "Kontrolltermin — Dr. Müller"

    # Voicemail → redial: same task, same thread, kind `retry`.
    assert "Call SID: CA_2" in await scheduling._place_call(task, settings)
    assert await manager.thread_for_call("CA_2") == thread_id
    assert [c.attach_kind for c in await manager.calls(thread_id)] == [KIND_ORIGIN, KIND_RETRY]
    assert len(await manager.list_threads()) == 1


async def test_appointment_can_continue_an_existing_thread(dialer, manager, settings):
    existing = await manager.create("Bestehendes Anliegen", primary_number=TARGET)
    task = scheduling.AppointmentTask(
        task_id="t2",
        user_id="u1",
        channel="telegram",
        target_number=TARGET,
        contact_name="Dr. Müller",
        topic="Kontrolltermin",
        timeframe="next_week",
        duration_minutes=30,
        language="de",
        thread_id=existing.thread_id,
    )
    await scheduling._place_call(task, settings)
    assert await manager.thread_for_call("CA_1") == existing.thread_id
    assert [c.attach_kind for c in await manager.calls(existing.thread_id)] == [KIND_FOLLOWUP]


# ── Inbound matching at call start (§4.3, §7) ────────────────────────


def _channel(settings):
    from pincer.channels.phone_calls import VoiceChannel

    return VoiceChannel(settings)


def _inbound_state(call_sid: str, caller: str) -> CallState:
    return CallState(
        call_sid=call_sid,
        caller_number=caller,
        direction=CallDirection.INBOUND,
        started_at=datetime.now(UTC),
        language="de",
    )


async def _seed_summarised_thread(manager, number: str, subject: str) -> str:
    thread = await manager.create(subject, primary_number=number, language="de")
    async with __import__("aiosqlite").connect(manager.db_path) as db:
        await db.execute(
            "UPDATE call_threads SET rolling_summary = ? WHERE thread_id = ?",
            ("Rechnung über 400 Euro offen.\nStand: wartet auf Rückruf.", thread.thread_id),
        )
        await db.commit()
    return thread.thread_id


async def test_inbound_call_attaches_to_the_single_open_thread(settings, manager):
    thread_id = await _seed_summarised_thread(manager, "+4930999", "Zahnarzttermin Dr. Müller")
    state = _inbound_state("CA_in", "+4930999")

    await _channel(settings)._prepare_thread_context("CA_in", state)

    assert state.metadata["thread_id"] == thread_id
    assert await manager.thread_for_call("CA_in") == thread_id
    calls = await manager.calls(thread_id)
    assert [c.attach_kind for c in calls] == [KIND_INBOUND_MATCHED]
    # `off` is the default: attaching is grouping, not disclosure.
    assert state.metadata["thread_context"] == ""


async def test_inbound_ambiguous_match_attaches_nothing(settings, manager):
    await manager.create("Matter A", primary_number="+4930999")
    await manager.create("Matter B", primary_number="+4930999")
    state = _inbound_state("CA_in", "+4930999")

    await _channel(settings)._prepare_thread_context("CA_in", state)

    assert state.metadata["thread_id"] == ""
    assert state.metadata["thread_context"] == ""
    assert await manager.thread_for_call("CA_in") == ""


async def test_inbound_ack_mode_speaks_only_the_ack(settings, manager):
    settings.thread_inbound_context = "ack"
    await _seed_summarised_thread(manager, "+4930999", "Zahnarzttermin Dr. Müller")
    state = _inbound_state("CA_in", "+4930999")

    await _channel(settings)._prepare_thread_context("CA_in", state)

    block = state.metadata["thread_context"]
    assert "Ich sehe, wir hatten dazu bereits Kontakt." in block
    for secret in ("Zahnarzttermin", "Dr. Müller", "Rechnung", "400", "Stand:"):
        assert secret not in block, f"inbound ack must not disclose {secret!r}"


async def test_matching_window_of_zero_disables_inbound_matching(settings, manager):
    settings.thread_match_window_days = 0
    await manager.create("Matter", primary_number="+4930999")
    state = _inbound_state("CA_in", "+4930999")

    await _channel(settings)._prepare_thread_context("CA_in", state)

    assert state.metadata["thread_id"] == ""


async def test_outbound_call_gets_the_thread_context_block(settings, manager, tmp_path):
    """§7: the continuity feature — the next call sees the prior calls."""
    thread_id = await _seed_summarised_thread(manager, TARGET, "Zahnarzttermin Dr. Müller")
    await manager.attach("CA_prev", thread_id, KIND_ORIGIN)
    await manager.attach("CA_next", thread_id, KIND_FOLLOWUP)

    state = CallState(
        call_sid="CA_next",
        caller_number="+4915212345678",
        direction=CallDirection.OUTBOUND,
        target_number=TARGET,
        started_at=datetime.now(UTC),
        language="de",
    )
    await _channel(settings)._prepare_thread_context("CA_next", state)

    block = state.metadata["thread_context"]
    assert "THREAD-KONTEXT" in block
    assert "Rechnung über 400 Euro offen." in block
    assert len(block) <= th.MAX_CONTEXT_BLOCK


async def test_thread_block_reaches_the_live_system_prompt(settings, manager):
    """The frozen block is actually composed into the per-turn system prompt."""
    from pincer.voice.state_machine import CallStateMachine

    thread_id = await _seed_summarised_thread(manager, TARGET, "Zahnarzttermin")
    await manager.attach("CA_next", thread_id, KIND_FOLLOWUP)
    state = CallState(
        call_sid="CA_next",
        caller_number="+4915212345678",
        direction=CallDirection.OUTBOUND,
        target_number=TARGET,
        purpose="Termin verschieben",
        started_at=datetime.now(UTC),
        language="de",
    )
    channel = _channel(settings)
    await channel._prepare_thread_context("CA_next", state)

    sm = CallStateMachine("CA_next", is_outbound=True)
    sm.start_call()
    prompt = channel._build_voice_system(state, sm)
    assert "THREAD-KONTEXT" in prompt
    assert "Rechnung über 400 Euro offen." in prompt


async def test_threadless_call_adds_nothing_to_the_prompt(settings, manager):
    from pincer.voice.state_machine import CallStateMachine

    state = CallState(
        call_sid="CA_solo",
        caller_number="+4915212345678",
        direction=CallDirection.OUTBOUND,
        target_number=TARGET,
        purpose="Termin verschieben",
        started_at=datetime.now(UTC),
        language="de",
    )
    channel = _channel(settings)
    await channel._prepare_thread_context("CA_solo", state)
    assert state.metadata["thread_context"] == ""

    sm = CallStateMachine("CA_solo", is_outbound=True)
    sm.start_call()
    assert "THREAD-KONTEXT" not in channel._build_voice_system(state, sm)


async def test_stale_thread_outside_the_window_is_not_matched(settings, manager):
    thread = await manager.create("Alte Sache", primary_number="+4930999")
    async with __import__("aiosqlite").connect(manager.db_path) as db:
        await db.execute(
            "UPDATE call_threads SET updated_at = ? WHERE thread_id = ?",
            ((datetime.now(UTC) - timedelta(days=30)).isoformat(), thread.thread_id),
        )
        await db.commit()
    state = _inbound_state("CA_in", "+4930999")

    await _channel(settings)._prepare_thread_context("CA_in", state)

    assert state.metadata["thread_id"] == ""

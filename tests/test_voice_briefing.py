"""Call briefing — validation, the setup race, and the binding prompt block.

The product promise of an outbound call is "the agent does what you told it".
These tests hold the three places that promise used to break: a task that was
never validated, a state that was registered too late, and a prompt block that
read as background instead of as an instruction.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from voice_harness.fake_engine import FakeVoiceEngine
from voice_harness.settings import apply_in_call_tool_defaults, apply_test_paths
from voice_harness.transport_fakes import FakeSocket, install_fake_twilio

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import briefing as bf
from pincer.voice import outbound, status_notify
from pincer.voice.briefing import (
    MAX_TASK_CHARS,
    MIN_TASK_CHARS,
    TASK_TOO_LONG,
    TASK_TOO_SHORT,
    BriefingError,
    CallBriefing,
    validate_task,
)
from pincer.voice.engine import CallDirection
from pincer.voice.transcript import Speaker

TARGET = "+4930111222"
TASK = "Ask what time they close today and whether they take walk-ins."


# ── §3.1 validation matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("task", "ok"),
    [
        ("y" * (MIN_TASK_CHARS - 1), False),
        ("y" * MIN_TASK_CHARS, True),
        ("y" * MAX_TASK_CHARS, True),
        ("y" * (MAX_TASK_CHARS + 1), False),
        ("", False),
        ("   \t\n  ", False),
        ("  " + "y" * MIN_TASK_CHARS + "  ", True),  # length is measured after stripping
        (None, False),
    ],
)
def test_task_validation_matrix(task, ok):
    if ok:
        assert validate_task(task) == str(task).strip()
        return
    with pytest.raises(BriefingError) as exc:
        validate_task(task)
    assert str(exc.value) in (TASK_TOO_SHORT, TASK_TOO_LONG)


def test_validation_message_is_actionable():
    """The message is shown to the user verbatim, so it must say what to do."""
    assert "concretely what to do" in TASK_TOO_SHORT
    assert str(MAX_TASK_CHARS) in TASK_TOO_LONG


def test_briefing_create_normalises_and_caps():
    briefing = CallBriefing.create(
        "  " + TASK + "  ", target_name="Dr. Müller", language="de", source="chat", instructions="i" * 9000
    )
    assert briefing.task == TASK  # verbatim apart from the surrounding whitespace
    assert briefing.target_name == "Dr. Müller"
    assert len(briefing.instructions) == bf.MAX_INSTRUCTIONS_CHARS


def test_briefing_round_trips_through_json():
    briefing = CallBriefing.create(TASK, target_name="Dr. Müller", source="dashboard", instructions="Be brief")
    restored = CallBriefing.from_json(briefing.to_json())
    assert restored is not None
    assert restored.task == TASK
    assert restored.source == "dashboard"
    assert restored.instructions == "Be brief"

    assert CallBriefing.from_json("") is None
    assert CallBriefing.from_json("{not json") is None
    assert CallBriefing.from_json(json.dumps({"task": "   "})) is None


def test_briefing_from_state_falls_back_to_the_call_state():
    """A call registered by an older path still reports its briefing."""
    state = MagicMock()
    state.metadata = {}
    state.purpose = TASK
    state.target_name = "Dr. Müller"
    state.language = "de"
    state.instructions = ""
    recovered = bf.briefing_from_state(state)
    assert recovered is not None and recovered.task == TASK

    state.purpose = ""
    assert bf.briefing_from_state(state) is None


# ── §2 the setup race ────────────────────────────────────────────────


def _settings(tmp_path) -> MagicMock:
    cfg = apply_test_paths(MagicMock(), tmp_path)
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = True
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.voice_outbound_max_daily = 100
    cfg.voice_machine_detection = False
    cfg.voice_engine = "conversation_relay"
    cfg.voice_default_language = "en"
    cfg.voice_supported_languages = "en,de,uk"
    cfg.voice_language = "en-US"
    cfg.voice_de_formality = "sie"
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.voice_consent_mode = "one_party"
    cfg.voice_consent_language = ""
    cfg.voice_recording_enabled = False
    cfg.voice_assistant_name = "Pincer"
    cfg.voice_assistant_org = "3days.ai"
    cfg.voice_assistant_owner = "Jane Doe"
    cfg.voice_intro_text = ""
    cfg.voice_ws_auth_required = False
    cfg.twilio_account_sid = "AC123"
    cfg.twilio_auth_token.get_secret_value.return_value = "token"
    cfg.twilio_phone_number = "+4915212345678"
    cfg.voice_daily_call_limit = 20
    cfg.voice_target_cooldown_min = 0
    cfg.voice_quiet_hours = ""
    cfg.voice_quiet_hours_override_users = ""
    cfg.voice_stt_min_confidence = 0.55
    cfg.voice_filler_phrases = ""
    cfg.receptionist_enabled = False
    cfg.thread_match_window_days = 7
    cfg.thread_inbound_context = "off"
    cfg.thread_autoclose_days = 30
    cfg.dashboard_url = ""
    cfg.default_user_id = ""
    return apply_in_call_tool_defaults(cfg, default_user_id="")


@pytest.fixture
def voice(tmp_path, monkeypatch):
    """A live engine + channel, with `make_phone_call` on a fake Twilio."""
    from pincer.voice import twiml_server

    settings = _settings(tmp_path)
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)

    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr(twiml_server, "_engine", engine)
    monkeypatch.setattr(twiml_server, "_settings", settings)
    monkeypatch.setattr(outbound, "_daily_outbound_counts", {})

    placed: list[dict[str, Any]] = []
    hold = asyncio.Event()
    hold.set()

    class FakeCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            placed.append(kwargs)
            call = MagicMock()
            call.sid = f"CA_{len(placed)}"
            return call

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = FakeCalls()

    install_fake_twilio(monkeypatch, FakeClient)

    status_notify._reset_for_tests()
    yield types.SimpleNamespace(
        settings=settings, engine=engine, channel=channel, placed=placed, hold=hold, server=twiml_server
    )
    status_notify._reset_for_tests()


async def test_state_is_registered_before_the_dial(voice):
    """The briefing must exist before Twilio can connect anything to it."""
    seen: list[str] = []

    original = voice.engine.register_pending_outbound

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("registered")
        return await original(*args, **kwargs)

    voice.engine.register_pending_outbound = _spy  # type: ignore[method-assign]

    class OrderedCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            seen.append("dialled")
            call = MagicMock()
            call.sid = "CA_1"
            return call

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=OrderedCalls())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "Call SID: CA_1" in result
    assert seen == ["registered", "dialled"]

    state = voice.engine.get_call_state("CA_1")
    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK


async def test_setup_before_registration_outbound_waits_then_binds(voice):
    """setup fires first, registration lands later → the briefing still binds.

    This is the race that produced the bug: the relay socket connected before
    `calls.create()` had returned, the handler found no state, invented an
    INBOUND one, and the agent ran a generic persona on a briefed call.
    """
    call_sid = "CA_race"

    async def _register_late() -> None:
        await asyncio.sleep(0.5)
        briefing = CallBriefing.create(TASK, target_name="Dr. Müller", source="chat")
        pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
        await voice.engine.promote_pending(pre, call_sid)

    later = asyncio.create_task(_register_late())
    state = await voice.server._resolve_setup_state(call_sid, TARGET, "outbound-api")
    await later

    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK

    sm = voice.channel._ensure_call_tracking(call_sid, state)
    assert TASK in voice.channel._build_voice_system(state, sm)


async def test_setup_during_an_awaited_dial_binds_the_briefing(voice):
    """The REST dial is awaited now, so Twilio's relay `setup` can be handled
    while `create_async` is still in flight — before `promote_pending` has
    re-keyed the briefed state to the CallSid. The setup must wait for it."""
    setups: list[asyncio.Task] = []

    class SetupFirstCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            setups.append(asyncio.create_task(voice.server._resolve_setup_state("CA_mid", TARGET, "outbound-api")))
            await asyncio.sleep(0.05)  # setup is now parked in await_call_state
            assert not setups[0].done()
            return types.SimpleNamespace(sid="CA_mid")

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=SetupFirstCalls())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    state = await setups[0]

    assert "Call SID: CA_mid" in result
    assert state is voice.engine.get_call_state("CA_mid")
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK


# regression: structurally untestable on dev @ 0a1c320 — the sync Client held
# the loop for the whole dial, and the sync fakes returned instantly
async def test_a_pending_dial_does_not_stall_a_live_relay_socket(voice, monkeypatch):
    from voice_harness.transport_fakes import FakeSocket, FakeTwilioRest

    fake = FakeTwilioRest()
    fake.hold = asyncio.Event()  # carrier dial setup takes a while
    voice.engine._rest = fake
    heard: list[str] = []

    async def _on_speech(_sid: str, text: str) -> None:
        heard.append(text)

    voice.engine.set_on_speech(_on_speech)

    async def _authenticated(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(voice.server, "_authenticate_ws", _authenticated)
    await voice.engine.on_call_start("CA_live", "+4917612345", CallDirection.INBOUND)
    sock = FakeSocket()
    relay = asyncio.create_task(voice.server.relay_ws(sock))
    sock.push(type="setup", callSid="CA_live", **{"from": "+4917612345"})

    dial = asyncio.create_task(outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"}))
    async with asyncio.timeout(2):
        while not fake.in_flight:
            await asyncio.sleep(0)

    sock.push(type="prompt", voicePrompt="Hallo, sind Sie noch da?", last=True)
    await asyncio.wait_for(sock.inbox.join(), timeout=2)

    assert heard == ["Hallo, sind Sie noch da?"]
    assert not dial.done()
    fake.hold.set()
    assert "Call SID: CA_fake" in await dial
    assert fake.creates[-1]["to"] == TARGET
    sock.hang_up()
    await asyncio.wait_for(relay, timeout=2)


async def test_setup_orphan_outbound_terminates(voice):
    """No briefing anywhere → the call is refused, never improvised."""
    notified: list[str] = []

    async def notifier(user_id, channel, text):
        notified.append(text)
        return True

    status_notify.set_status_notifier(notifier)
    status_notify.register_outbound_call("CA_orphan", user_id="u1", channel="telegram", target_number=TARGET)

    state = await voice.server._resolve_setup_state("CA_orphan", TARGET, "outbound-api")

    assert state is None, "an outbound call with no briefing must not get a state"
    assert voice.engine.get_call_state("CA_orphan") is None
    assert notified and "could not be recovered" in notified[0]

    async with aiosqlite.connect(str(voice.settings.db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT direction, failure_code FROM pincer_voice_calls WHERE call_sid = 'CA_orphan'")
        row = await cursor.fetchone()
    assert row is not None
    assert row["direction"] == "outbound"
    assert row["failure_code"] == "briefing_lost"


async def test_setup_recovers_the_briefing_from_the_status_record(voice):
    """The pre-existing recovery path still works and still carries the task."""
    status_notify.register_outbound_call(
        "CA_recover",
        user_id="u1",
        channel="telegram",
        purpose=TASK,
        target_number=TARGET,
        language="en",
        target_name="Dr. Müller",
    )
    state = await voice.server._resolve_setup_state("CA_recover", TARGET, "outbound-api")
    assert state is not None
    assert state.direction == CallDirection.OUTBOUND
    assert state.purpose == TASK


async def test_inbound_setup_is_untouched_by_the_termination_rule(voice):
    """A genuine inbound call was registered by the signature-verified
    /webhook before its TwiML — and hence its socket — existed; setup must
    find and reuse that state."""
    registered = await voice.engine.on_call_start("CA_in", "+4917612345", CallDirection.INBOUND)
    state = await voice.server._resolve_setup_state("CA_in", "+4917612345", "")
    assert state is registered
    assert state.direction == CallDirection.INBOUND


async def test_setup_with_unknown_sid_is_refused(voice):
    """No registered state and no status record → refused. Creating state
    from socket-supplied callSid/from would let anyone who reaches the relay
    endpoint mint a conversation under a chosen caller identity."""
    state = await voice.server._resolve_setup_state("CA_unknown", "+15550001111", "")
    assert state is None
    assert voice.engine.get_call_state("CA_unknown") is None


async def test_failed_dial_discards_the_pre_registration(voice):
    """Twilio raised → no orphaned pending state left behind."""

    class ExplodingCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            raise RuntimeError("twilio down")

    voice.engine._rest._client = types.SimpleNamespace(calls=ExplodingCalls())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error")
    assert voice.engine._pending_outbound == {}


class _TimedOutCalls:
    """create_async times out; list_async returns whatever Twilio "has"."""

    def __init__(self, listed: list[Any]) -> None:
        self.listed = listed

    async def create_async(self, **kwargs: Any) -> Any:
        raise TimeoutError

    async def list_async(self, **kwargs: Any) -> list[Any]:
        return self.listed


async def test_timed_out_dial_that_twilio_placed_is_bound(voice):
    """Twilio accepted the POST but answered late → the call keeps its briefing."""
    from datetime import UTC, datetime

    placed = types.SimpleNamespace(sid="CA_late", status="ringing", date_created=datetime.now(UTC))
    voice.engine._rest._client = types.SimpleNamespace(calls=_TimedOutCalls([placed]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "Call SID: CA_late" in result
    state = voice.engine.get_call_state("CA_late")
    assert state is not None and state.purpose == TASK
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_timed_out_dial_with_unknown_outcome_still_counts(voice):
    """No call found → not reported as rejected, and the attempt is counted."""
    from datetime import UTC, datetime, timedelta

    stale = types.SimpleNamespace(sid="CA_old", date_created=datetime.now(UTC) - timedelta(hours=1))
    voice.engine._rest._client = types.SimpleNamespace(calls=_TimedOutCalls([stale]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error") and "may still be ringing" in result
    assert voice.engine.get_call_state("CA_old") is None
    assert voice.engine._pending_outbound == {}
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


@pytest.fixture
def dial_outcomes(monkeypatch):
    """Which terminal dial hook make_phone_call called, and with what error."""
    from pincer.voice.telemetry import hooks

    seen: dict[str, list[str]] = {"rejected": [], "unknown": []}

    async def rejected(temp_id: str, *, error: str) -> None:
        seen["rejected"].append(error)

    async def unknown(temp_id: str, *, error: str) -> None:
        seen["unknown"].append(error)

    monkeypatch.setattr(hooks, "dial_rejected", rejected)
    # raising=False: before the hook existed this records the rejection instead of erroring in setup
    monkeypatch.setattr(hooks, "dial_unknown", unknown, raising=False)
    return seen


async def test_timed_out_dial_with_unknown_outcome_is_recorded_as_unknown(voice, dial_outcomes):
    """No call found → telemetry says "unknown", not "rejected"."""
    voice.engine._rest._client = types.SimpleNamespace(calls=_TimedOutCalls([]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "may still be ringing" in result
    assert dial_outcomes == {"rejected": [], "unknown": ["TimeoutError: dial outcome unknown"]}
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_timed_out_dial_whose_lookup_fails_is_recorded_as_unknown(voice, dial_outcomes):
    """The lookup itself failing leaves the outcome just as unknown."""

    class LookupFails(_TimedOutCalls):
        async def list_async(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("twilio list down")

    voice.engine._rest._client = types.SimpleNamespace(calls=LookupFails([]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "may still be ringing" in result
    assert dial_outcomes == {"rejected": [], "unknown": ["TimeoutError: dial outcome unknown"]}
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_a_dial_cancelled_mid_request_leaves_nothing_pending(voice, dial_outcomes, monkeypatch):
    """The turn that issued the tool is cancelled (a barge-in) while the POST
    is in flight. Twilio may still place the call, so it is an unknown
    outcome: counted, cooled down, its trace closed — and its pending entry
    gone, or a later setup to that number could claim the stale briefing."""
    from pincer.voice import safety_gates

    recorded: list[str] = []

    async def _record(settings: Any, number: str, **kwargs: Any) -> None:
        recorded.append(number)

    monkeypatch.setattr(safety_gates, "record_outbound_call", _record)
    released = asyncio.Event()

    class StuckCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            await released.wait()
            return types.SimpleNamespace(sid="CA_never")

    voice.engine._rest._client = types.SimpleNamespace(calls=StuckCalls())

    dial = asyncio.create_task(outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"}))
    async with asyncio.timeout(2):
        while not voice.engine._pending_outbound:
            await asyncio.sleep(0)
    dial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dial

    assert voice.engine._pending_outbound == {}
    assert dial_outcomes == {"rejected": [], "unknown": ["CancelledError: dial outcome unknown"]}
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1
    assert recorded == [TARGET]


@pytest.fixture
def dialing_notices(monkeypatch):
    """Call SIDs the user was told are ringing."""
    seen: list[str] = []

    async def _notify(call_sid: str) -> None:
        seen.append(call_sid)

    monkeypatch.setattr(status_notify, "notify_dialing", _notify)
    return seen


async def test_a_claimed_call_that_ended_before_the_timeout_is_a_placed_call(voice, dialing_notices):
    """Answered, talked, hung up — all inside the 10 s request window. The
    relay claimed it, so it is a call that happened, not "already ended"."""
    from datetime import UTC, datetime

    class ClaimedEndedThenTimedOut:
        async def create_async(self, **kwargs: Any) -> Any:
            await voice.server._resolve_setup_state("CA_done", "+1000", "outbound-api", to=TARGET, verified=True)
            await voice.engine.end_call("CA_done")
            raise TimeoutError

        async def list_async(self, **kwargs: Any) -> list[Any]:
            return [types.SimpleNamespace(sid="CA_done", status="completed", date_created=datetime.now(UTC))]

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=ClaimedEndedThenTimedOut())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Call placed and already finished.\nCall SID: CA_done\n"), result
    assert status_notify.get_call_info("CA_done") is not None
    assert dialing_notices == [], "no 'ringing' notice for a call that is over"
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_no_ringing_notice_for_a_claimed_call_that_already_ended(voice, dialing_notices):
    """Same, but the REST POST returns normally after the call is over."""

    class ClaimedEndedThenReturned:
        async def create_async(self, **kwargs: Any) -> Any:
            await voice.server._resolve_setup_state("CA_done", "+1000", "outbound-api", to=TARGET, verified=True)
            await voice.engine.end_call("CA_done")
            return types.SimpleNamespace(sid="CA_done")

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=ClaimedEndedThenReturned())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Call placed and already finished.\nCall SID: CA_done\n"), result
    assert dialing_notices == []


async def test_dial_cut_off_after_the_relay_bound_it_is_a_placed_call(voice, dialing_notices, dial_outcomes):
    """The callee answers at 2 s and the setup claims the dial; then the REST
    response dies. The call is live and briefed, so it is a placed call — not
    "Error placing call", which invites a redial of the person already talking."""
    import aiohttp

    class AnsweredThenDisconnected:
        async def create_async(self, **kwargs: Any) -> Any:
            await voice.server._resolve_setup_state("CA_live", "+1000", "outbound-api", to=TARGET, verified=True)
            raise aiohttp.ServerDisconnectedError()

        async def list_async(self, **kwargs: Any) -> list[Any]:
            raise AssertionError("the relay already bound the call; no lookup needed")

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=AnsweredThenDisconnected())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "Call SID: CA_live" in result
    state = voice.engine.get_call_state("CA_live")
    assert state is not None and state.purpose == TASK
    assert status_notify.get_call_info("CA_live") is not None
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1
    assert dial_outcomes == {"rejected": [], "unknown": []}


async def test_dial_cut_off_with_no_call_found_is_unknown_and_counts(voice, dial_outcomes):
    """A reset while reading the response: the POST went out, so Twilio may
    have placed the call. Counted, and recorded as unknown."""
    import aiohttp

    class Disconnected(_TimedOutCalls):
        async def create_async(self, **kwargs: Any) -> Any:
            raise aiohttp.ServerDisconnectedError()

    voice.engine._rest._client = types.SimpleNamespace(calls=Disconnected([]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "may still be ringing" in result
    assert dial_outcomes == {"rejected": [], "unknown": ["ServerDisconnectedError: dial outcome unknown"]}
    assert voice.engine._pending_outbound == {}
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_twilio_refusing_the_dial_is_still_a_rejection(voice, dial_outcomes):
    """Twilio answered with an error: nothing was placed, nothing is counted."""
    from twilio.base.exceptions import TwilioRestException

    class Refused(_TimedOutCalls):
        async def create_async(self, **kwargs: Any) -> Any:
            raise TwilioRestException(400, "/Calls.json", msg="Invalid 'To' number", code=21211)

    voice.engine._rest._client = types.SimpleNamespace(calls=Refused([]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error placing call")
    assert len(dial_outcomes["rejected"]) == 1 and dial_outcomes["unknown"] == []
    assert voice.engine._pending_outbound == {}
    assert sum(outbound._daily_outbound_counts.get("u1", {}).values()) == 0


async def test_dial_that_never_connected_is_still_a_rejection(voice, dial_outcomes):
    """No connection to Twilio at all: the POST was never sent."""
    import aiohttp

    class Unreachable(_TimedOutCalls):
        async def create_async(self, **kwargs: Any) -> Any:
            key = types.SimpleNamespace(host="api.twilio.com", port=443, ssl=True)
            raise aiohttp.ClientConnectorError(key, OSError(61, "Connection refused"))

    voice.engine._rest._client = types.SimpleNamespace(calls=Unreachable([]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error placing call")
    assert len(dial_outcomes["rejected"]) == 1 and dial_outcomes["unknown"] == []
    assert sum(outbound._daily_outbound_counts.get("u1", {}).values()) == 0


async def test_timed_out_dial_that_already_ended_is_not_promoted(voice):
    """The lookup finds the call, but it is over (declined, busy, hung up):
    promoting it would park a dead state in _active_calls that no status
    callback will ever remove, and tell the user the call is ringing."""
    from datetime import UTC, datetime

    done = types.SimpleNamespace(sid="CA_done", status="completed", date_created=datetime.now(UTC))
    voice.engine._rest._client = types.SimpleNamespace(calls=_TimedOutCalls([done]))

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert result.startswith("Error") and "CA_done" in result and "already ended" in result
    assert voice.engine.get_call_state("CA_done") is None
    assert voice.engine._pending_outbound == {}
    assert status_notify.get_call_info("CA_done") is None, "no 'ringing' notification for an ended call"
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_setup_during_a_slow_dial_claims_the_pending_state_by_number(voice):
    """The callee can pick up before Twilio has answered the REST POST. The
    relay setup must bind to the pending dial by its `to` number right away,
    not wait out a 3 s poll for a promotion that is up to 10 s away."""
    released = asyncio.Event()

    class SlowCalls:
        async def create_async(self, **kwargs: Any) -> Any:
            await released.wait()
            return types.SimpleNamespace(sid="CA_claim")

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=SlowCalls())

    dial = asyncio.create_task(outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"}))
    async with asyncio.timeout(2):
        while not voice.engine._pending_outbound:
            await asyncio.sleep(0)

    async with asyncio.timeout(1):  # well under SETUP_STATE_WAIT_S: no polling involved
        state = await voice.server._resolve_setup_state("CA_claim", "+1000", "outbound-api", to=TARGET, verified=True)

    assert state is not None and state.purpose == TASK
    assert voice.engine.get_call_state("CA_claim") is state
    assert voice.engine._pending_outbound == {}
    assert not dial.done()

    released.set()
    assert "Call SID: CA_claim" in await dial
    assert voice.engine.get_call_state("CA_claim") is state


async def test_timed_out_dial_already_bound_by_the_relay_needs_no_lookup(voice):
    """Answered inside the request timeout → the SID is already known."""

    class AnsweredThenTimedOut:
        async def create_async(self, **kwargs: Any) -> Any:
            await voice.server._resolve_setup_state("CA_bound", "+1000", "outbound-api", to=TARGET, verified=True)
            raise TimeoutError

        async def list_async(self, **kwargs: Any) -> list[Any]:
            raise AssertionError("the relay already bound the call; no lookup needed")

    voice.server._engine = voice.engine
    voice.engine._rest._client = types.SimpleNamespace(calls=AnsweredThenTimedOut())

    result = await outbound.make_phone_call(TARGET, TASK, context={"user_id": "u1"})
    assert "Call SID: CA_bound" in result
    state = voice.engine.get_call_state("CA_bound")
    assert state is not None and state.purpose == TASK
    assert status_notify.get_call_info("CA_bound") is not None
    assert sum(outbound._daily_outbound_counts["u1"].values()) == 1


async def test_setup_without_a_to_number_does_not_claim(voice):
    """A setup that names no callee cannot be matched to a pending dial."""
    briefing = CallBriefing.create(TASK)
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    assert await voice.engine.claim_pending_outbound("CA_x", "") is None
    assert await voice.engine.claim_pending_outbound("CA_x", "+4930999999") is None
    assert voice.engine._pending_outbound == {pre.call_sid: pre}


OTHER_TASK = "Ask whether the parcel for order 4711 can be picked up tomorrow morning."


async def test_two_pending_dials_to_one_number_are_not_claimed(voice):
    """The number cannot tell two dials to the same callee apart."""
    first = await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")
    second = await voice.engine.register_pending_outbound(CallBriefing.create(OTHER_TASK), TARGET, language="en")

    assert await voice.engine.claim_pending_outbound("CA_x", TARGET) is None
    assert voice.engine._pending_outbound == {first.call_sid: first, second.call_sid: second}


async def test_two_pending_dials_each_bind_to_their_own_sid(voice, monkeypatch):
    """Dial B's callee connects first. Its setup must not take dial A's
    briefing; it waits for B's own REST return to bind it by SID."""
    monkeypatch.setattr(voice.server, "SETUP_STATE_WAIT_S", 2.0)
    voice.server._engine = voice.engine
    await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")
    second = await voice.engine.register_pending_outbound(CallBriefing.create(OTHER_TASK), TARGET, language="en")

    setup = asyncio.create_task(
        voice.server._resolve_setup_state("CA_B", "+1000", "outbound-api", to=TARGET, verified=True)
    )
    await asyncio.sleep(0)
    await voice.engine.promote_pending(second, "CA_B")  # B's REST return
    state = await asyncio.wait_for(setup, timeout=3)

    assert state is not None and state.purpose == OTHER_TASK


class _UpgradeSocket(FakeSocket):
    """A relay socket the real `_authenticate_ws` can inspect."""

    def __init__(self, query: dict[str, str]) -> None:
        super().__init__()
        self.headers: dict[str, str] = {}
        self.query_params = query
        self.url = types.SimpleNamespace(path="/api/apps/twilio/relay", query="")
        self.client = None
        self.state = types.SimpleNamespace()


FORGED_SID = "CA" + "f" * 32


async def _forged_setup(voice, sock: _UpgradeSocket) -> Any:
    """Connect through the real auth gate, send a setup naming the callee, and
    return the call state the setup was bound to (before the socket closes and
    ends the call)."""
    relay = asyncio.create_task(voice.server.relay_ws(sock))
    sock.push(type="setup", callSid=FORGED_SID, direction="outbound-api", to=TARGET, **{"from": "+15550000000"})
    handled = asyncio.ensure_future(sock.inbox.join())
    await asyncio.wait([relay, handled], timeout=2, return_when=asyncio.FIRST_COMPLETED)
    handled.cancel()
    bound = voice.engine.get_call_state(FORGED_SID)
    sock.hang_up()
    await asyncio.wait_for(relay, timeout=2)
    return bound


@pytest.mark.parametrize("why", ["auth_off", "no_token"])
async def test_an_unverified_setup_cannot_claim_a_pending_dial(voice, monkeypatch, why):
    """WS auth off, or no token to check against: `callSid` and `to` are
    whatever the socket says, so naming the callee of a dial in flight must not
    hand over its briefing."""
    monkeypatch.setattr(voice.server, "SETUP_STATE_WAIT_S", 0.05)
    voice.server._engine = voice.engine
    voice.settings.voice_ws_auth_required = why != "auth_off"
    if why == "no_token":
        voice.settings.twilio_auth_token.get_secret_value.return_value = ""
    pre = await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")

    assert await _forged_setup(voice, _UpgradeSocket({})) is None
    assert voice.engine._pending_outbound == {pre.call_sid: pre}, "the briefing stays with the real dial"


async def test_a_verified_setup_still_claims_its_pending_dial(voice):
    """The same setup over a socket that passed the signed-token check."""
    from urllib.parse import parse_qsl

    from pincer.voice.webhook_auth import WS_RELAY_PATH, signed_ws_query

    voice.server._engine = voice.engine
    voice.settings.voice_ws_auth_required = True
    query = dict(parse_qsl(signed_ws_query(voice.settings, WS_RELAY_PATH).lstrip("?")))
    await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")

    state = await _forged_setup(voice, _UpgradeSocket(query))
    assert state is not None and state.purpose == TASK
    assert voice.engine._pending_outbound == {}


async def _relay_webhook_setup(voice, *, signed: bool) -> int:
    import hashlib

    import httpx
    from fastapi import FastAPI

    from pincer.voice.webhook_auth import compute_signature

    app = FastAPI()
    app.include_router(voice.server.twilio_router)
    body = json.dumps(
        {"type": "setup", "callSid": "CA" + "e" * 32, "direction": "outbound-api", "to": TARGET, "from": "+1555"}
    ).encode()
    url = f"http://test/api/apps/twilio/relay-webhook?bodySHA256={hashlib.sha256(body).hexdigest()}"
    headers = {"content-type": "application/json"}
    if signed:
        headers["X-Twilio-Signature"] = compute_signature("token", url, {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code


@pytest.mark.parametrize("why", ["validation_off", "no_token"])
async def test_an_unverified_relay_webhook_setup_cannot_claim(voice, monkeypatch, why):
    monkeypatch.setattr(voice.server, "SETUP_STATE_WAIT_S", 0.05)
    voice.server._engine = voice.engine
    voice.settings.voice_webhook_validate = why != "validation_off"
    if why == "no_token":
        voice.settings.twilio_auth_token.get_secret_value.return_value = ""
    pre = await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")

    assert await _relay_webhook_setup(voice, signed=False) == 200
    assert voice.engine.get_call_state("CA" + "e" * 32) is None
    assert voice.engine._pending_outbound == {pre.call_sid: pre}


async def test_a_signed_relay_webhook_setup_still_claims(voice):
    voice.server._engine = voice.engine
    voice.settings.voice_webhook_validate = True
    await voice.engine.register_pending_outbound(CallBriefing.create(TASK), TARGET, language="en")

    assert await _relay_webhook_setup(voice, signed=True) == 200
    state = voice.engine.get_call_state("CA" + "e" * 32)
    assert state is not None and state.purpose == TASK


async def test_promote_pending_is_idempotent(voice):
    briefing = CallBriefing.create(TASK)
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    first = await voice.engine.promote_pending(pre, "CA_p")
    second = await voice.engine.promote_pending(pre, "CA_p")
    assert first is second
    assert voice.engine._pending_outbound == {}


async def test_await_call_state_gives_up(voice):
    assert await voice.engine.await_call_state("CA_never", timeout=0.2, interval=0.05) is None


# ── §3.1 no unbriefed call can be placed ─────────────────────────────


@pytest.mark.parametrize("purpose", ["", "  ", "call mum", "x"])
async def test_make_phone_call_refuses_an_unusable_task(voice, purpose):
    result = await outbound.make_phone_call(TARGET, purpose, context={"user_id": "u1"})
    assert result.startswith("Error")
    assert "Purpose too short" in result
    assert voice.placed == [], "no dial may happen for a call with no task"


async def test_make_phone_call_binds_and_logs_the_briefing(voice, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="pincer.voice.briefing"):
        result = await outbound.make_phone_call(
            TARGET, TASK, target_name="Dr. Müller", context={"user_id": "u1"}, source="dashboard"
        )
    assert "Call SID: CA_1" in result
    assert any("BRIEFING_BOUND" in r.message and "source=dashboard" in r.message for r in caplog.records)


# ── §3.1 transcript audit line ───────────────────────────────────────


async def test_briefing_lands_in_the_transcript(voice):
    state = await voice.engine.on_call_start(
        "CA_t", TARGET, CallDirection.OUTBOUND, target_number=TARGET, purpose=TASK, language="en"
    )
    await voice.channel._handle_call_start("CA_t", state)

    transcript = voice.channel.get_transcript("CA_t")
    assert transcript is not None
    lines = [e.text for e in transcript.entries if e.speaker == Speaker.SYSTEM]
    assert lines and lines[0].startswith("[BRIEFING] ")
    assert TASK[:50] in lines[0]


async def test_inbound_call_gets_no_briefing_line(voice):
    state = await voice.engine.on_call_start("CA_ti", "+4917612345", CallDirection.INBOUND, language="en")
    await voice.channel._handle_call_start("CA_ti", state)
    transcript = voice.channel.get_transcript("CA_ti")
    assert transcript is not None
    assert not [e for e in transcript.entries if e.speaker == Speaker.SYSTEM]


# ── §4.2 adherence smoke detector ────────────────────────────────────


class _FakeTranscript:
    def __init__(self, agent_lines: list[str]) -> None:
        self.entries = [types.SimpleNamespace(speaker=Speaker.AGENT, text=t, is_final=True) for t in agent_lines]


def test_adherence_detects_a_capability_monologue():
    monologue = [
        "Hello, I am a digital assistant.",
        "I can help you with appointments, emails, reminders and much more.",
        "What would you like me to do for you today?",
    ]
    adhered, overlap = bf.check_adherence(TASK, _FakeTranscript(monologue))
    assert not adhered
    assert overlap < bf.ADHERENCE_THRESHOLD


def test_adherence_accepts_an_agent_that_stated_the_task():
    on_task = ["Hello, I am calling to ask what time you close today, and whether you take walk-ins."]
    adhered, _ = bf.check_adherence(TASK, _FakeTranscript(on_task))
    assert adhered


def test_adherence_defers_to_a_real_task_result():
    """A call that demonstrably did the job is adherent whatever it said."""
    adhered, _ = bf.check_adherence(
        TASK, _FakeTranscript(["Something else entirely."]), task_result="They close at 18:00."
    )
    assert adhered


def test_adherence_is_silent_when_there_is_nothing_to_judge():
    assert bf.check_adherence(TASK, None)[0] is True
    assert bf.check_adherence(TASK, _FakeTranscript([]))[0] is True
    assert bf.check_adherence("", _FakeTranscript(["anything"]))[0] is True


def test_report_adherence_warns_but_never_raises(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="pincer.voice.briefing"):
        assert bf.report_adherence("CA_x", TASK, _FakeTranscript(["I can help with many things."])) is False
    assert any("briefing_adherence_low" in r.message for r in caplog.records)


# ── Regression: registering a call is not answering it ───────────────


async def test_pre_registration_does_not_start_the_conversation_clock(voice):
    """The dial returns while the phone is still RINGING.

    Firing per-call tracking at promotion started the state machine's
    `outbound_greeting` clock during the ring; thirty seconds later the
    watchdog spoke its timeout goodbye and hung up on a call the callee had
    not answered yet. From the callee's side: they pick up and hear silence.
    """
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_ring")

    state = voice.engine.get_call_state("CA_ring")
    assert state is not None, "the briefed state must exist before the callee answers"
    assert state.metadata.get("answered_at") is None
    assert voice.channel.get_state_machine("CA_ring") is None, "no phase clock may run while the call is still ringing"
    assert voice.channel.get_transcript("CA_ring") is None


async def test_setup_marks_the_call_answered_and_starts_tracking(voice):
    """Twilio's `setup` is the answer signal — that is when clocks start."""
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_answered")

    state = await voice.engine.mark_call_answered("CA_answered")

    assert state is not None and state.metadata.get("answered_at") is not None
    sm = voice.channel.get_state_machine("CA_answered")
    assert sm is not None, "tracking starts on answer"
    assert not sm.is_terminal
    # And the briefing audit line was written at answer time.
    transcript = voice.channel.get_transcript("CA_answered")
    assert transcript is not None
    assert any(e.text.startswith("[BRIEFING]") for e in transcript.entries)


async def test_mark_call_answered_is_idempotent(voice):
    """A second `setup` (returning from a <Dial> transfer) must not restart
    the clocks — that would hand the call a fresh 30 s greeting timeout."""
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_twice")

    await voice.engine.mark_call_answered("CA_twice")
    sm_first = voice.channel.get_state_machine("CA_twice")
    first_answered = voice.engine.get_call_state("CA_twice").metadata["answered_at"]

    await voice.engine.mark_call_answered("CA_twice")

    assert voice.channel.get_state_machine("CA_twice") is sm_first
    assert voice.engine.get_call_state("CA_twice").metadata["answered_at"] == first_answered


async def test_talk_time_excludes_the_ringing(voice):
    """Ringing is not silence: the accumulator is re-anchored on answer."""
    import asyncio
    import time

    from pincer.voice.analytics import get_accumulator

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_ring2")

    await asyncio.sleep(0.05)  # stand-in for the ring
    # Compared against the moment of pickup, not an elapsed-time budget:
    # mark_call_answered's on_call_start callback runs the first database
    # touch, which migrates a fresh schema — hundreds of ms on a CI runner —
    # and that cost must not decide whether the anchor moved.
    answered = time.monotonic()
    await voice.engine.mark_call_answered("CA_ring2")

    accumulator = get_accumulator(voice.engine.get_call_state("CA_ring2"))
    assert accumulator is not None
    assert accumulator._t0 >= answered, "the talk-time clock starts at answer, not at dial"


async def test_started_at_is_re_anchored_on_answer(voice):
    """`started_at` is stamped before the dial, so it runs through the ring.

    Everything that reads it has to measure from pickup instead: the reported
    and billed duration, and the AMD grace window.
    """
    from datetime import UTC, datetime, timedelta

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_anchor")

    state = voice.engine.get_call_state("CA_anchor")
    dialed = datetime.now(UTC) - timedelta(seconds=18)  # a long but ordinary ring
    state.started_at = dialed

    await voice.engine.mark_call_answered("CA_anchor")

    assert state.duration_seconds < 2, "the ring must not be reported or billed as conversation"
    assert state.metadata["dialed_at"] == dialed, "the dial time stays available for diagnostics"


async def test_re_anchoring_is_idempotent(voice):
    """A second `setup` must not restart the duration clock either."""
    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_anchor2")

    await voice.engine.mark_call_answered("CA_anchor2")
    state = voice.engine.get_call_state("CA_anchor2")
    first = state.started_at

    await asyncio.sleep(0.05)
    await voice.engine.mark_call_answered("CA_anchor2")

    assert state.started_at == first


async def test_amd_grace_survives_a_long_ring(voice):
    """A voicemail greeting at pickup must not count as the caller speaking.

    With `started_at` left at the dial, any call ringing longer than
    AMD_GRACE_SECONDS had its grace already spent by the time the callee
    picked up — so the machine's own greeting set `caller_spoke`, which is
    exactly what `_handle_amd_verdict` treats as "a human is already
    conversing" before it declines to hang up.
    """
    from datetime import UTC, datetime, timedelta

    from pincer.voice.engine import AMD_GRACE_SECONDS

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_amd")

    state = voice.engine.get_call_state("CA_amd")
    state.started_at = datetime.now(UTC) - timedelta(seconds=AMD_GRACE_SECONDS + 8)

    await voice.engine.mark_call_answered("CA_amd")
    state.mark_caller_spoke()  # the answering machine's greeting, transcribed at pickup

    assert state.metadata.get("caller_spoke") is None, "the AMD shield must stay armed"


async def test_amd_grace_still_expires_after_answer(voice):
    """The shield must still arm once a real conversation is under way."""
    from datetime import UTC, datetime, timedelta

    from pincer.voice.engine import AMD_GRACE_SECONDS

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_amd2")
    await voice.engine.mark_call_answered("CA_amd2")

    state = voice.engine.get_call_state("CA_amd2")
    # Speech this far past the answer is a person, not a greeting.
    state.started_at = datetime.now(UTC) - timedelta(seconds=AMD_GRACE_SECONDS + 1)
    state.mark_caller_spoke()

    assert state.metadata.get("caller_spoke") is True


async def test_unanswered_call_still_measures_the_ring(voice):
    """No answer means no re-anchor: the ring is all that happened."""
    from datetime import UTC, datetime, timedelta

    briefing = CallBriefing.create(TASK, source="chat")
    pre = await voice.engine.register_pending_outbound(briefing, TARGET, language="en")
    await voice.engine.promote_pending(pre, "CA_noans")

    state = voice.engine.get_call_state("CA_noans")
    state.started_at = datetime.now(UTC) - timedelta(seconds=18)

    assert state.duration_seconds >= 18
    assert "dialed_at" not in state.metadata


async def test_inbound_calls_are_answered_on_registration(voice):
    """An inbound call is already live when we first see it, so nothing defers."""
    state = await voice.engine.on_call_start("CA_in2", "+4917612345", CallDirection.INBOUND, language="en")
    assert state.metadata.get("answered_at") is not None
    assert voice.channel.get_state_machine("CA_in2") is not None

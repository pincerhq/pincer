"""Unanswered outbound calls are not successful conversations.

Drives the real ConversationRelayEngine + VoiceChannel + twilio_router: the
call is pre-registered and promoted exactly as `outbound.py` does, then the
Twilio callback is posted over ASGI on the same event loop. Only the Twilio
REST client and the metric sink are replaced. The relay `setup` is reproduced
by `engine.mark_call_answered`, which is the one thing the setup handler does
for a call whose state already exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from voice_harness.settings import apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.observability.failure_codes import FailureCode
from pincer.voice import status_notify
from pincer.voice.briefing import CallBriefing
from pincer.voice.engine import CallDirection, CallState, ConversationRelayEngine, set_end_cause
from pincer.voice.outcome import render_not_connected_report
from pincer.voice.postcall import PostCallProcessor
from pincer.voice.state_machine import CallPhase, CallStateMachine
from pincer.voice.twiml_server import init_voice_routes, twilio_router

TARGET = "+4930123456"


@pytest.fixture(autouse=True)
def _clean_status_notify():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


class Rig:
    def __init__(self, settings: Any, engine: ConversationRelayEngine, channel: VoiceChannel) -> None:
        self.settings = settings
        self.engine = engine
        self.channel = channel
        self.ended: list[dict[str, Any]] = []
        self.sent: list[str] = []
        app = FastAPI()
        app.include_router(twilio_router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def dial(self, call_sid: str, *, language: str = "en") -> CallState:
        """Pre-register and promote, as `make_phone_call` does around the REST dial."""

        async def notifier(_user_id: str, _channel: str, text: str) -> bool:
            self.sent.append(text)
            return True

        status_notify.set_status_notifier(notifier)
        status_notify.register_outbound_call(
            call_sid, "user1", channel="telegram", target_number=TARGET, language=language
        )
        briefing = CallBriefing(task="Ask whether the order shipped", language=language)
        pre_state = await self.engine.register_pending_outbound(briefing, TARGET, language=language)
        state = await self.engine.promote_pending(pre_state, call_sid)
        assert state is not None
        return state

    async def answer(self, call_sid: str) -> CallStateMachine:
        await self.engine.mark_call_answered(call_sid)
        return self.channel._state_machines[call_sid]

    async def post(self, path: str, **data: str) -> None:
        response = await self.client.post(f"/api/apps/twilio/{path}", data=data)
        assert response.status_code == 200

    def with_post_call(self, llm: Any = None) -> None:
        self.channel.set_post_call_processor(
            PostCallProcessor(self.settings, llm=llm, memory=None, db_path=self.settings.db_path)
        )

    async def settle(self) -> None:
        """Let the background post-call task deliver its report."""
        for task in list(self.channel._postcall_tasks.values()):
            await task
        await asyncio.sleep(0)

    def ended_once(self) -> dict[str, Any]:
        assert len(self.ended) == 1, self.ended
        return self.ended[0]


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    settings = apply_test_paths(MagicMock(), tmp_path)
    settings.voice_language = "en-US"
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token.get_secret_value.return_value = ""
    monkeypatch.setattr("twilio.rest.Client", MagicMock())

    engine = ConversationRelayEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    init_voice_routes(engine, settings)

    built = Rig(settings, engine, channel)
    monkeypatch.setattr("pincer.observability.metrics.record_call_ended", lambda **kw: built.ended.append(kw))
    yield built
    await built.client.aclose()


# 1
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("busy", FailureCode.BUSY),
        ("no-answer", FailureCode.NO_ANSWER),
        ("failed", FailureCode.CALL_SETUP),
        ("canceled", FailureCode.CALL_SETUP),
    ],
)
async def test_not_connected_status_sets_failure_code(rig, status, expected):
    state = await rig.dial("CA_nc")
    await rig.post("status", CallSid="CA_nc", CallStatus=status)

    assert state.metadata["failure_code"] == str(expected)
    metric = rig.ended_once()
    assert metric["failure_code"] == str(expected)
    assert metric["outcome"] == "failed"


# 2
async def test_voicemail_before_setup(rig):
    state = await rig.dial("CA_vm")
    await rig.post("amd", CallSid="CA_vm", AnsweredBy="machine_end_beep")

    assert state.metadata["failure_code"] == "voicemail"
    assert rig.ended_once()["outcome"] == "failed"


# 3
async def test_voicemail_after_setup_is_not_callee_hangup(rig):
    """The state machine is in OUTBOUND_GREETING by now; the hang-up rule would
    call that `callee_hangup` and burn error budget for a voicemail box."""
    await rig.dial("CA_vm2")
    sm = await rig.answer("CA_vm2")
    assert sm.phase == CallPhase.OUTBOUND_GREETING

    await rig.post("amd", CallSid="CA_vm2", AnsweredBy="machine_end_beep")

    metric = rig.ended_once()
    assert metric["failure_code"] == "voicemail"
    assert metric["outcome"] == "failed"
    assert sm.phase == CallPhase.FAILED
    assert sm.state.transitions[-1].reason == "voicemail"


# 4
async def test_amd_false_positive_stays_conversational(rig):
    state = await rig.dial("CA_live")
    sm = await rig.answer("CA_live")
    state.metadata["caller_spoke"] = True

    await rig.post("amd", CallSid="CA_live", AnsweredBy="machine_start")
    assert rig.ended == [], "the call must stay up"
    assert "end_code" not in state.metadata

    assert sm.transition(CallPhase.FREEFORM, reason="conversation")
    await rig.post("status", CallSid="CA_live", CallStatus="completed")

    metric = rig.ended_once()
    assert metric["failure_code"] == "none"
    assert metric["outcome"] == "completed"


# 5
async def test_human_call_unchanged(rig):
    await rig.dial("CA_ok")
    sm = await rig.answer("CA_ok")
    assert sm.transition(CallPhase.FREEFORM, reason="conversation")
    await rig.post("status", CallSid="CA_ok", CallStatus="completed")
    metric = rig.ended_once()
    assert metric["failure_code"] == "none"
    assert metric["outcome"] == "completed"

    rig.ended.clear()
    await rig.dial("CA_verify")
    sm = await rig.answer("CA_verify")
    assert sm.transition(CallPhase.FREEFORM, reason="conversation")
    assert sm.transition(CallPhase.VERIFY, reason="action")
    await rig.post("status", CallSid="CA_verify", CallStatus="completed")
    metric = rig.ended_once()
    assert metric["failure_code"] == "callee_hangup"
    assert metric["outcome"] == "failed"


# 6
def test_end_cause_first_writer_wins():
    state = CallState(call_sid="CA_fw", direction=CallDirection.OUTBOUND, caller_number=TARGET)
    set_end_cause(state, FailureCode.BUSY)
    set_end_cause(state, FailureCode.SHUTDOWN)
    assert state.metadata["end_code"] == "busy"


async def test_end_cause_survives_later_end_signals(rig):
    """busy, then our own end_call; voicemail, then Twilio's `completed`."""
    await rig.dial("CA_b")
    await rig.post("status", CallSid="CA_b", CallStatus="busy")
    await rig.engine.end_call("CA_b")
    assert rig.ended_once()["failure_code"] == "busy"

    rig.ended.clear()
    await rig.dial("CA_v")
    await rig.post("amd", CallSid="CA_v", AnsweredBy="machine_end_beep")
    await rig.post("status", CallSid="CA_v", CallStatus="completed")
    assert rig.ended_once()["failure_code"] == "voicemail"


# 7
async def test_unanswered_without_cause_is_unknown(rig, caplog):
    await rig.dial("CA_unk")
    with caplog.at_level(logging.WARNING, logger="pincer.channels.phone_calls"):
        await rig.engine.end_call("CA_unk")

    metric = rig.ended_once()
    assert metric["failure_code"] == "unknown"
    assert metric["outcome"] == "failed"
    assert "CA_unk ended unanswered with no recorded cause" in caplog.text


# 8
async def test_shutdown_while_ringing(rig):
    await rig.dial("CA_down")
    await rig.channel.stop()

    metric = rig.ended_once()
    assert metric["failure_code"] == "shutdown"
    assert metric["outcome"] == "failed"


# 9 + 10
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", f"📞 Couldn't reach {TARGET} — the line was busy."),
        ("de", f"📞 {TARGET} nicht erreicht — die Leitung war besetzt."),
        ("uk", f"📞 Не вдалося додзвонитися до {TARGET} — лінія була зайнята."),
    ],
)
async def test_not_connected_report(rig, monkeypatch, language, expected):
    extract = AsyncMock()
    monkeypatch.setattr("pincer.voice.postcall.extract_outcome", extract)
    rig.with_post_call(llm=MagicMock())

    state = await rig.dial("CA_rep", language=language)
    await rig.post("status", CallSid="CA_rep", CallStatus="busy")
    await rig.settle()

    # The localized text is for the user only; the classification is the code.
    assert state.metadata["failure_code"] == "busy"
    report = "\n".join(rig.sent)
    assert expected in report
    assert "/transcript" not in report
    extract.assert_not_called()


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("no_answer", "no answer"),
        ("call_setup", "could not be placed"),
        ("voicemail", "voicemail detected"),
    ],
)
def test_not_connected_report_names_each_cause(code, fragment):
    report = render_not_connected_report(TARGET, "CA1", code, "en")
    assert fragment in report
    assert "/transcript" not in report


async def test_late_voicemail_report_carries_the_voicemail_line(rig):
    """Answered, then a machine: the normal report path, plus the reason."""
    rig.with_post_call()
    await rig.dial("CA_late", language="de")
    await rig.answer("CA_late")
    await rig.post("amd", CallSid="CA_late", AnsweredBy="machine_end_beep")
    await rig.settle()

    assert "Anrufbeantworter erkannt" in "\n".join(rig.sent)

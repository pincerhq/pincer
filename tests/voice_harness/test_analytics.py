"""§8 harness — analytics through a full simulated call, on both methods.

The measurement half runs against the real channel and engine; the sentiment
half uses a scripted extractor, because what is being tested here is the
plumbing (does a hostile call end up labelled negative, with a rationale that
quotes the call) rather than an LLM's judgement.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import analytics as an
from pincer.voice import status_notify
from pincer.voice.engine import CallDirection
from pincer.voice.postcall import PostCallProcessor
from pincer.voice.transcript import Speaker, TranscriptLogger

from .fake_engine import FakeVoiceEngine
from .personas import HostilePersona
from .personas_de import CooperativePersonaDe
from .settings import apply_in_call_tool_defaults, apply_test_paths

CALL = "CA_analytics"

REPLIES_EN = [
    "I understand, and I am sorry the order is late. Let me check what happened with the delivery and "
    "find out exactly when it will reach you, so you are not left guessing again.",
    "Thank you for bearing with me. The warehouse confirms the shipment left yesterday evening and it is "
    "scheduled to arrive with you tomorrow before noon, which I will note on your account.",
    "That is understandable. I will pass your complaint about the repeated delays on to the manager today "
    "and make sure somebody comes back to you about it. Thank you for your time. Goodbye.",
]

REPLIES_DE = [
    "Das verstehe ich, und es tut mir leid, dass die Lieferung sich verzögert hat. Ich schaue nach, was "
    "passiert ist, und sage Ihnen, wann genau die Ware bei Ihnen ankommt.",
    "Vielen Dank für Ihre Geduld. Das Lager bestätigt, dass die Sendung gestern Abend rausgegangen ist und "
    "morgen vormittag bei Ihnen sein sollte. Ich vermerke das auf Ihrem Konto.",
    "Das kann ich gut nachvollziehen. Ich gebe Ihre Rückmeldung heute noch an die Leitung weiter und sorge "
    "dafür, dass sich jemand bei Ihnen meldet. Vielen Dank für Ihre Zeit. Auf Wiederhören.",
]


def _settings(tmp_path, **overrides):
    settings = apply_test_paths(MagicMock(), tmp_path)
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_filler_phrases = ""
    settings.voice_assistant_owner = "Jane Doe"
    settings.voice_timezone = "Europe/Berlin"
    settings.receptionist_enabled = False
    settings.voice_auto_followup = False
    settings.thread_inbound_context = "off"
    settings.data_dir = None
    apply_in_call_tool_defaults(settings, default_user_id="owner")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class ScriptedExtractor:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls += 1
        self.last_user = messages[0].content
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _payload(sentiment: str, rationale: str, language: str = "en") -> dict:
    return {
        "outcome": "completed",
        "task_result": "The matter was discussed.",
        "key_facts": [],
        "commitments": [],
        "follow_up_suggestions": [],
        "language": language,
        "sentiment": sentiment,
        "sentiment_trajectory": "stable",
        "sentiment_rationale": rationale,
    }


@pytest.fixture(autouse=True)
def _clean():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


async def _run_call(tmp_path, persona_cls, payload: dict, *, method: str, language: str = "en"):
    """Drive a persona through the real channel, then the post-call pipeline."""
    settings = _settings(tmp_path)
    engine = FakeVoiceEngine(settings)
    engine.analytics_method = method
    channel = VoiceChannel(settings)
    channel.set_engine(engine)

    # Realistic reply lengths: the analytics withhold a sentiment reading below
    # ten seconds of total speech, so a harness call of three two-word lines
    # would test the null path rather than the one these scenarios are about.
    replies = iter(REPLIES_DE if language == "de" else REPLIES_EN)

    async def _handler(incoming):
        return next(replies, "Vielen Dank, auf Wiederhören." if language == "de" else "Thank you. Goodbye.")

    await channel.start(_handler)
    state = await engine.on_call_start(
        CALL,
        "+4930111222",
        CallDirection.OUTBOUND,
        target_number="+4930111222",
        purpose="Follow up on the delayed order.",
        language=language,
    )
    accumulator = an.get_accumulator(state)
    assert accumulator is not None, "every registered call gets an accumulator"

    persona = persona_cls()
    action = persona.opening()
    turns = 0
    while action.kind == "say" and turns < 4:
        # Simulate the speech both ways: the persona's line and the agent's reply.
        accumulator.caller_text(action.text, language)
        await engine.on_speech_input(CALL, action.text)
        spoken = engine.spoken.get(CALL, [])
        if spoken:
            accumulator.agent_text(spoken[-1], language)
        action = persona.react(spoken[-1] if spoken else "")
        turns += 1

    state.ended_at = state.started_at + timedelta(seconds=90)
    transcript = channel.get_transcript(CALL) or TranscriptLogger(CALL)
    for line in getattr(persona, "ATTACKS", []) or []:
        transcript.log_utterance(Speaker.CALLER, line)

    llm = ScriptedExtractor(payload)
    processor = PostCallProcessor(settings, llm=llm, memory=None, db_path=str(settings.db_path))
    report = await processor.process(CALL, state, transcript, completed=True)
    record = await an.load_analytics(str(settings.db_path), CALL)
    return SimpleNamespace(report=report, record=record, llm=llm, engine=engine)


@pytest.mark.parametrize("method", [an.METHOD_EXACT, an.METHOD_ESTIMATED])
async def test_hostile_call_is_negative_with_a_grounded_rationale(tmp_path, method):
    rationale = "Called the third delay unacceptable and demanded a manager."
    run = await _run_call(tmp_path, HostilePersona, _payload("negative", rationale), method=method)

    assert run.record is not None
    assert run.record.sentiment == "negative"
    assert run.record.sentiment_rationale == rationale
    assert run.record.method == method, "the record carries the engine's own provenance"
    # The owner is told, once, with the reason.
    assert "seemed dissatisfied" in run.report
    assert rationale in run.report


@pytest.mark.parametrize("method", [an.METHOD_EXACT, an.METHOD_ESTIMATED])
async def test_cooperative_call_is_positive_and_silent_in_the_report(tmp_path, method):
    run = await _run_call(
        tmp_path,
        CooperativePersonaDe,
        _payload("positive", "Bedankte sich und bestätigte den Termin.", language="de"),
        method=method,
        language="de",
    )
    assert run.record is not None
    assert run.record.sentiment == "positive"
    assert "unzufrieden" not in run.report
    assert "seemed dissatisfied" not in run.report


async def test_talk_time_is_measured_on_a_real_call(tmp_path):
    run = await _run_call(
        tmp_path, HostilePersona, _payload("negative", "Was angry about the delay."), method=an.METHOD_ESTIMATED
    )
    assert run.record is not None
    assert (run.record.agent_speech_ms or 0) > 0, "the agent spoke and it was counted"
    assert (run.record.caller_speech_ms or 0) > 0, "the caller spoke and it was counted"
    assert run.record.talk_ratio is not None
    assert 0.0 <= run.record.talk_ratio <= 1.0
    assert run.record.silence_ms is not None


async def test_analytics_cost_no_extra_llm_call(tmp_path):
    run = await _run_call(
        tmp_path, HostilePersona, _payload("negative", "Was angry about the delay."), method=an.METHOD_EXACT
    )
    assert run.llm.calls == 1

"""Outbound call briefing: purpose/instructions/target_name reach the live prompt."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from voice_harness.fake_engine import FakeVoiceEngine
from voice_harness.settings import apply_in_call_tool_defaults, apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.voice import status_notify
from pincer.voice.engine import CallDirection
from pincer.voice.prompts import de as de_pack
from pincer.voice.prompts import en as en_pack

CALL = "CA_brief"
PURPOSE = "Confirm Thursday's appointment, move it if 14:00 no longer works."
INSTRUCTIONS = "Accept any slot between 9 and 12. Do not agree to Friday."


def _settings(**overrides):
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_assistant_owner = "Jane Doe"
    settings.voice_timezone = "Europe/Berlin"
    settings.data_dir = None
    return apply_in_call_tool_defaults(settings, **overrides)


@pytest.fixture(autouse=True)
def _clean():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


async def _channel_and_state(direction=CallDirection.OUTBOUND, language="en", **call_kwargs):
    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    state = await engine.on_call_start(CALL, "+4930123456", direction, language=language, **call_kwargs)
    sm = channel._ensure_call_tracking(CALL, state)
    return channel, state, sm


def test_register_outbound_call_keeps_name_and_instructions():
    status_notify.register_outbound_call(
        CALL, user_id="u1", channel="web", purpose=PURPOSE, target_name="Dr. Müller", instructions=INSTRUCTIONS
    )
    info = status_notify.get_call_info(CALL)
    assert info is not None
    assert info.target_name == "Dr. Müller"
    assert info.instructions == INSTRUCTIONS


async def test_outbound_prompt_contains_the_briefing():
    channel, state, sm = await _channel_and_state(
        target_number="+4930123456", target_name="Dr. Müller", purpose=PURPOSE, instructions=INSTRUCTIONS
    )
    system = channel._build_voice_system(state, sm)
    assert "CALL BRIEFING" in system
    assert PURPOSE in system
    assert INSTRUCTIONS in system
    assert "You are calling Dr. Müller on behalf of Jane Doe." in system
    assert "OUTBOUND call" in system and "why you are calling" in system
    # the briefing sits before the phase instruction so the opening is anchored on it
    assert system.index("CALL BRIEFING") < system.index(en_pack.PHASE_INSTRUCTIONS[sm.phase.value])


async def test_briefing_without_name_uses_number_and_default_owner():
    channel, state, sm = await _channel_and_state(target_number="+4930123456", purpose=PURPOSE)
    channel._settings.voice_assistant_owner = ""
    system = channel._build_voice_system(state, sm)
    assert "You are calling +4930123456 on behalf of your user." in system
    assert "Additional instructions" not in system


async def test_briefing_is_localised():
    channel, state, sm = await _channel_and_state(
        language="de",
        target_number="+4930123456",
        target_name="Praxis Müller",
        purpose=PURPOSE,
        instructions=INSTRUCTIONS,
    )
    system = channel._build_voice_system(state, sm)
    assert "ANRUF-BRIEFING" in system
    assert "Sie rufen Praxis Müller im Auftrag von Jane Doe an." in system
    assert "Zusätzliche Anweisungen Ihres Nutzers: " + INSTRUCTIONS in system
    assert "CALL BRIEFING" not in system
    assert de_pack.CALL_BRIEF.split("{who}")[0] in system


async def test_inbound_call_has_no_briefing():
    channel, state, sm = await _channel_and_state(direction=CallDirection.INBOUND, purpose=PURPOSE)
    assert "CALL BRIEFING" not in channel._build_voice_system(state, sm)


async def test_outbound_without_purpose_has_no_briefing():
    channel, state, sm = await _channel_and_state(target_number="+4930123456")
    assert "CALL BRIEFING" not in channel._build_voice_system(state, sm)


def test_briefing_purpose_is_capped():
    settings = _settings()
    channel = VoiceChannel(settings)
    state = MagicMock()
    state.direction = CallDirection.OUTBOUND
    state.purpose = "x" * 5000
    state.instructions = "y" * 9000
    state.target_name = "A"
    state.target_number = "+1"
    text = channel._call_brief(state, "en", "sie")
    assert "x" * 2000 in text and "x" * 2001 not in text
    assert "y" * 4000 in text and "y" * 4001 not in text

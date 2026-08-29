"""Ending the call on goodbyes: [END_CALL] token, farewell detection, grace window."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from voice_harness.fake_engine import FakeVoiceEngine
from voice_harness.settings import apply_in_call_tool_defaults, apply_test_paths

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import StreamEventType
from pincer.voice import call_end, status_notify
from pincer.voice.engine import CallDirection
from pincer.voice.state_machine import CallPhase

CALL = "CA_end"
_REAL_ESTIMATE = call_end.estimate_speech_seconds  # the autouse fixture below zeroes the module attribute


# ── unit: token + farewell detection ─────────────────────────


@pytest.mark.parametrize(
    ("text", "present", "stripped"),
    [
        ("Thanks for your time, goodbye! [END_CALL]", True, "Thanks for your time, goodbye!"),
        ("[end_call] Tschüss!", True, "Tschüss!"),
        ("Bye [ END CALL ]", True, "Bye"),
        ("No token here.", False, "No token here."),
        ("", False, ""),
    ],
)
def test_parse_end_call_token(text, present, stripped):
    assert call_end.parse_end_call_token(text) == (present, stripped)


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        ("bye", "en", True),
        ("Okay, thanks — bye bye!", "en", True),
        ("Goodbye, have a nice day.", "en", True),
        ("bye, but one more question", "en", False),
        ("by the way, is Thursday free?", "en", False),
        ("Can you book it? Bye", "en", False),
        ("Tschüss!", "de", True),
        ("Alles klar, danke, auf Wiederhören.", "de", True),
        ("Tschüss, ach warte, noch eine Frage", "de", False),
        ("До побачення!", "uk", True),
        ("Дякую, бувайте.", "uk", True),
        ("Бувайте, але ще одне питання", "uk", False),
    ],
)
def test_is_farewell(text, lang, expected):
    assert call_end.is_farewell(text, lang) is expected


def test_contains_farewell_for_agent_lines():
    assert call_end.contains_farewell("I'll report back. Thanks — goodbye!", "en")
    assert call_end.contains_farewell("Vielen Dank, auf Wiederhören!", "de")
    assert not call_end.contains_farewell("Which day suits you?", "en")


def test_estimate_speech_seconds():
    assert _REAL_ESTIMATE("") == 0.0
    assert 0.7 < _REAL_ESTIMATE("Goodbye!") < 1.0
    assert _REAL_ESTIMATE("word " * 200) == 12.0


# ── integration: VoiceChannel + fake engine ───────────────────


@dataclass
class _Chunk:
    type: StreamEventType
    content: str = ""


class ScriptAgent:
    """stream_voice_turn stand-in: one canned reply per turn."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.extra_systems: list[str] = []

    async def stream_voice_turn(self, **kwargs):
        self.calls += 1
        self.extra_systems.append(str(kwargs.get("extra_system", "")))
        text = self.replies.pop(0) if self.replies else "Okay."
        yield _Chunk(StreamEventType.TEXT, text)
        yield _Chunk(StreamEventType.DONE, text)


def _settings():
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_hangup_grace_s = 0.05
    settings.voice_timezone = "Europe/Berlin"
    settings.data_dir = None
    return apply_in_call_tool_defaults(settings)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(call_end, "estimate_speech_seconds", lambda text: 0.0)
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


async def _start(agent, *, language="en", direction=CallDirection.OUTBOUND):
    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    channel.set_stream_agent(agent)

    async def _blocking(incoming):
        return "Fallback."

    await channel.start(_blocking)
    state = await engine.on_call_start(CALL, "+4930123456", direction, language=language, purpose="test")
    sm = channel._ensure_call_tracking(CALL, state)
    return channel, engine, sm


async def _say(engine, text, settle=0.05):
    await engine.on_speech_input(CALL, text)
    for _ in range(12):
        await asyncio.sleep(settle)
        if CALL in engine.ended:
            return


async def test_end_call_token_hangs_up_after_farewell():
    agent = ScriptAgent(["Anything else I can do for you?", "Thanks for your time — goodbye! [END_CALL]"])
    channel, engine, sm = await _start(agent)
    await _say(engine, "Hello?")
    assert CALL not in engine.ended
    await _say(engine, "No that's everything")
    assert CALL in engine.ended
    assert sm.phase == CallPhase.COMPLETED
    # the token is never spoken
    assert all("END_CALL" not in s for s in engine.spoken[CALL])
    assert any("goodbye" in s.lower() for s in engine.spoken[CALL])
    await channel.stop()


async def test_mutual_goodbye_hangs_up_without_llm_turn():
    agent = ScriptAgent(["All set. Thanks — goodbye!"])  # model forgot the token
    channel, engine, sm = await _start(agent)
    await _say(engine, "Perfect, that's all")
    assert CALL not in engine.ended and agent.calls == 1
    await _say(engine, "Okay bye!")
    assert CALL in engine.ended
    assert agent.calls == 1  # no second LLM round-trip for "bye"
    assert sm.phase == CallPhase.COMPLETED
    await channel.stop()


async def test_caller_farewell_first_gets_one_goodbye_then_hangup():
    agent = ScriptAgent(["Goodbye, take care! [END_CALL]"])
    channel, engine, sm = await _start(agent, language="de")
    await _say(engine, "Danke, tschüss!")
    assert agent.calls == 1
    assert "CALLER_FAREWELL_NOTE" not in agent.extra_systems[0]  # the rendered note, not the key
    assert "Abschiedssatz" in agent.extra_systems[0]
    assert CALL in engine.ended
    await channel.stop()


async def test_caller_farewell_first_hangs_up_even_without_token():
    agent = ScriptAgent(["Tschüss und einen schönen Tag!"])
    channel, engine, sm = await _start(agent, language="de")
    await _say(engine, "Alles klar, tschüss")
    assert CALL in engine.ended
    await channel.stop()


async def test_continuing_after_goodbye_cancels_hangup():
    settings_grace = 0.4
    agent = ScriptAgent(["Thanks — goodbye! [END_CALL]", "Sure — what else do you need?"])
    channel, engine, sm = await _start(agent)
    channel._settings.voice_hangup_grace_s = settings_grace
    await engine.on_speech_input(CALL, "That's all")
    await asyncio.sleep(0.1)
    assert channel._hangup_pending(CALL)
    await engine.on_speech_input(CALL, "Oh wait, one more thing about Friday")
    await asyncio.sleep(0.1)
    assert not channel._hangup_pending(CALL)
    await asyncio.sleep(settings_grace + 0.2)
    assert CALL not in engine.ended
    assert agent.calls == 2
    await channel.stop()


async def test_regular_turns_never_schedule_hangup():
    agent = ScriptAgent(["Sure, I can help with that.", "Thursday at ten works."])
    channel, engine, sm = await _start(agent)
    await _say(engine, "Hi, I need an appointment")
    await _say(engine, "Thursday please")
    assert CALL not in engine.ended and not channel._hangup_pending(CALL)
    await channel.stop()

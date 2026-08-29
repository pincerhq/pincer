"""§4.3 — the briefing survives the whole path into a live turn.

Scope note, stated plainly: the conversation brain here is scripted, so these
scenarios prove that the user's task arrives in the agent's system prompt
intact, in the right position, and that a brain acting on that prompt asks
what it was told to ask. They cannot prove a production LLM obeys — that is
what the §4.2 adherence metric watches for in the field.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import StreamChunk, StreamEventType
from pincer.voice import status_notify
from pincer.voice.engine import CallDirection

from .fake_engine import FakeVoiceEngine
from .settings import apply_in_call_tool_defaults, apply_test_paths

CALL = "CA_briefing"
TARGET = "+4930111222"

TASK_EN = "Ask what time they close today and whether they take walk-ins."
TASK_DE = "Frage, wann sie heute schließen und ob sie auch ohne Termin behandeln."

# §4.3: a real pasted brief — long, multi-point, unstructured.
_LONG_BACKGROUND = (
    "Background you do not need to say out loud: I have been a patient there for eleven years, "
    "the practice moved to a new building last spring, and parking behind the building is free after five. "
)
LONG_TASK = (
    "Please call the practice and sort out three things for me. "
    "First, ask what time they close today, because I might not make it before six. "
    "Second, confirm that my appointment on Thursday at fourteen hundred is still in their calendar, "
    "and if it is not, ask for the next available slot in the same week. "
    "Third, ask whether I need to bring the referral letter from my GP or whether they already have it. "
    + _LONG_BACKGROUND
    * 6
)

# The exact shape of the failure this whole mechanism exists to prevent: an
# agent that opens with what an assistant can do instead of why it called.
CAPABILITY_PHRASES = (
    "i can help you with",
    "i can assist you with",
    "here is what i can do",
    "my capabilities",
    "i am able to help with",
    "ich kann ihnen helfen bei",
    "ich kann folgendes",
    "meine fähigkeiten",
)


class BriefingAgent:
    """A brain that reads its task out of the system prompt and pursues it.

    Deliberately literal: it extracts the task block, opens the call by stating
    it, and answers "what can you do?" with the task rather than a feature
    list. If the prompt plumbing drops the briefing it has nothing to say and
    the assertions below fail — which is the point.
    """

    TASK_HEADINGS = ("YOUR TASK FOR THIS CALL (binding):", "IHRE AUFGABE FÜR DIESEN ANRUF (verbindlich):")

    def __init__(self, language: str = "en") -> None:
        self.language = language
        self.calls: list[dict[str, Any]] = []
        self.turn = 0

    def _task(self, extra_system: str) -> str:
        for heading in self.TASK_HEADINGS:
            if heading in extra_system:
                after = extra_system.split(heading, 1)[1].lstrip("\n")
                return after.split("\n")[0].strip()
        return ""

    async def stream_voice_turn(self, **kwargs: Any):
        self.calls.append(kwargs)
        self.turn += 1
        extra = str(kwargs.get("extra_system") or "")
        task = self._task(extra)
        heard = str(kwargs.get("text") or "").lower()

        if not task:
            # No briefing reached the prompt — the failure mode under test.
            reply = "Hello, I am a digital assistant. I can help you with appointments, emails and reminders."
        elif self.turn == 1:
            opener = "Guten Tag, ich rufe an: " if self.language == "de" else "Hello, I am calling to ask: "
            reply = opener + task
        elif "what can you do" in heard or "was können sie" in heard:
            # Answer with the task, briefly — never with a feature list.
            reply = (
                f"Ich rufe nur wegen einer Sache an: {task}"
                if self.language == "de"
                else f"I am only calling about one thing: {task}"
            )
        else:
            reply = (
                "Vielen Dank, das war alles. Auf Wiederhören."
                if self.language == "de"
                else "Thank you, that was all. Goodbye."
            )
        yield StreamChunk(StreamEventType.TEXT, reply)
        yield StreamChunk(StreamEventType.DONE, reply)


def _settings(**overrides: Any) -> MagicMock:
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "en-US"
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_filler_phrases = ""
    settings.voice_assistant_owner = "Jane Doe"
    settings.voice_assistant_name = "Pincer"
    settings.voice_assistant_org = "3days.ai"
    settings.voice_timezone = "Europe/Berlin"
    settings.receptionist_enabled = False
    settings.data_dir = None
    apply_in_call_tool_defaults(settings, default_user_id="owner")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture(autouse=True)
def _clean():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


async def _start(task: str, language: str = "en"):
    settings = _settings()
    engine = FakeVoiceEngine(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    agent = BriefingAgent(language)
    channel.set_stream_agent(agent)

    async def _blocking(incoming):  # pragma: no cover — the streaming path is used
        return "Fallback."

    await channel.start(_blocking)
    state = await engine.on_call_start(
        CALL,
        TARGET,
        CallDirection.OUTBOUND,
        target_number=TARGET,
        target_name="Praxis Müller",
        purpose=task,
        language=language,
    )
    return channel, engine, agent, state


def _spoken(engine: FakeVoiceEngine) -> list[str]:
    return engine.spoken.get(CALL, [])


# ── The scenarios ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "task", "must_mention"),
    [("en", TASK_EN, ("close", "walk")), ("de", TASK_DE, ("schließen", "Termin"))],
    ids=["en", "de"],
)
async def test_briefing_followed_freeform(language, task, must_mention):
    """A free-form purpose is what the agent actually opens the call with."""
    channel, engine, agent, _state = await _start(task, language)

    await engine.on_speech_input(CALL, "Praxis Müller, guten Tag." if language == "de" else "Hello, this is Müller.")

    spoken = " ".join(_spoken(engine))
    assert spoken, "the agent said nothing"
    for word in must_mention:
        assert word.lower() in spoken.lower(), f"{word!r} missing from: {spoken}"

    # And the prompt the brain saw carried the task verbatim.
    assert task in str(agent.calls[0]["extra_system"])


async def test_briefing_pasted_long():
    """A 1500-character pasted brief: point one is addressed in the opening turns."""
    assert len(LONG_TASK) > 1500
    channel, engine, agent, _state = await _start(LONG_TASK[:1999])

    await engine.on_speech_input(CALL, "Hello, this is Müller.")
    await engine.on_speech_input(CALL, "We close at six today.")

    opening = " ".join(_spoken(engine)[:3]).lower()
    assert "close" in opening, f"the first sub-point was not raised: {opening}"

    # The prompt kept the brief inside the documented bound rather than
    # silently dropping the tail into nothing.
    prompt = str(agent.calls[0]["extra_system"])
    assert LONG_TASK[:200] in prompt


@pytest.mark.parametrize("language", ["en", "de"])
async def test_no_capability_talk(language):
    """Asked "what can you do?", the agent answers with the task."""
    task = TASK_EN if language == "en" else TASK_DE
    channel, engine, agent, _state = await _start(task, language)

    await engine.on_speech_input(CALL, "Hallo?" if language == "de" else "Hello?")
    await engine.on_speech_input(CALL, "Was können Sie denn alles?" if language == "de" else "What can you do?")

    spoken = " ".join(_spoken(engine)).lower()
    for phrase in CAPABILITY_PHRASES:
        assert phrase not in spoken, f"capability talk leaked: {phrase!r} in {spoken}"
    key = "schließen" if language == "de" else "close"
    assert key in spoken, "the agent did not answer with its task"

    # The suppression rule is in the prompt too, not only in the brain.
    persona_rule = "DÜRFEN NICHT Ihre Funktionen" if language == "de" else "MUST NOT enumerate features"
    assert persona_rule in str(agent.calls[0]["extra_system"])


async def test_missing_briefing_is_visible_in_this_harness():
    """Control: with no task in the prompt, the scripted brain falls into the
    exact capability monologue the scenarios above assert against. Without
    this, a broken assertion could pass for the wrong reason."""
    channel, engine, agent, _state = await _start("")
    await engine.on_speech_input(CALL, "Hello?")

    spoken = " ".join(_spoken(engine)).lower()
    assert any(phrase in spoken for phrase in CAPABILITY_PHRASES)


# ── §3.2 one assembly function, both surfaces ────────────────────────


async def test_dashboard_and_tool_paths_produce_identical_prompts():
    """RC-2: the API path and the chat-tool path must not assemble prompts
    differently — that is what made "it ignores my purpose" reproducible on one
    surface and not the other. Same input, byte-identical prompt."""
    from pincer.voice.briefing import SOURCE_CHAT, SOURCE_DASHBOARD

    prompts = []
    for source in (SOURCE_DASHBOARD, SOURCE_CHAT):
        settings = _settings()
        engine = FakeVoiceEngine(settings)
        channel = VoiceChannel(settings)
        channel.set_engine(engine)
        state = await engine.on_call_start(
            f"CA_{source}",
            TARGET,
            CallDirection.OUTBOUND,
            target_number=TARGET,
            target_name="Praxis Müller",
            purpose=TASK_EN,
            instructions="Be brief.",
            language="en",
        )
        state.metadata["briefing"] = {"task": TASK_EN, "source": source}
        sm = channel._ensure_call_tracking(state.call_sid, state)
        prompts.append(channel._build_voice_system(state, sm))

    assert prompts[0] == prompts[1]
    assert TASK_EN in prompts[0]


def test_the_assembly_function_is_the_only_one():
    """A second prompt assembler is how RC-2 came back. The channel must
    delegate rather than build the prompt itself."""
    import inspect

    from pincer.channels import phone_calls

    body = inspect.getsource(phone_calls.VoiceChannel._build_voice_system)
    assert "build_voice_system_prompt" in body
    # No local re-assembly: the tell-tale is joining prompt parts in the channel.
    assert "VOICE_SYSTEM_PROMPT" not in body
    assert '"\\n\\n".join' not in body


def test_both_transports_share_one_setup_resolver():
    """The relay socket and the Media Streams socket must apply the same
    briefing rules — divergence here is how one transport ran unbriefed."""
    import inspect

    from pincer.voice import twiml_server

    for func in (twiml_server.relay_ws, twiml_server.media_stream_ws, twiml_server.relay_webhook):
        assert "_resolve_setup_state" in inspect.getsource(func), func.__name__


# ── §3.2 the greeting stays generic; the task is the first LLM turn ──


async def test_welcome_greeting_does_not_carry_the_task():
    """The task must pass through the briefed model, not a static template —
    a greeting that recites it would be right for the wrong reason and would
    keep working even after the prompt plumbing broke."""
    from pincer.voice.twiml_builder import build_connect_twiml

    settings = _settings()
    settings.voice_engine = "conversation_relay"
    settings.voice_consent_mode = "one_party"
    settings.voice_consent_language = ""
    settings.voice_recording_enabled = False
    settings.voice_intro_text = ""
    settings.voice_ws_auth_required = False
    settings.voice_webhook_base_url = "https://voice.example.com"

    twiml = build_connect_twiml(settings, direction="outbound", language="en", counterparty=TARGET)
    assert TASK_EN not in twiml
    assert "close today" not in twiml.lower()
    # …but it does disclose what it is.
    assert re.search(r"assistant|Pincer", twiml, re.IGNORECASE)

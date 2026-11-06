"""`default` opts a voice or summary request out of effort/thinking (issue #345, review rounds 1 and 2).

Empty inherits the global PINCER_REASONING_EFFORT / PINCER_THINKING; `default` sends nothing, so
the model runs on its own default even when chat has a global setting. `none` is rejected: on
OpenAI it is itself a reasoning level, so it would be read as "off" and mean the opposite.
"""

from __future__ import annotations

import pytest
from anthropic_sse import ScriptedAnthropic, text
from pydantic import ValidationError

from pincer.config import Settings
from pincer.config.llm import call_effort, call_thinking, effective_effort, effective_thinking
from pincer.core.agent import Agent

_NAMES = ["voice_reasoning_effort", "voice_thinking", "summary_reasoning_effort", "summary_thinking"]


@pytest.mark.parametrize("name", _NAMES)
def test_none_is_rejected_for_every_override_and_says_what_to_use(name, tmp_path):
    with pytest.raises(ValidationError) as excinfo:
        Settings(data_dir=tmp_path, **{name: "none"})  # type: ignore[arg-type]

    assert (
        "'none' is not accepted: use 'default' to send nothing (the model's own default), "
        "or 'disabled' to turn thinking off." in str(excinfo.value)
    )


@pytest.mark.parametrize("name", _NAMES)
def test_default_is_accepted_for_every_override(name, tmp_path):
    settings = Settings(data_dir=tmp_path, **{name: "default"})  # type: ignore[arg-type]

    assert getattr(settings, name) == "default"


@pytest.mark.parametrize("name", ["voice_reasoning_effort", "summary_thinking"])
def test_an_unknown_override_is_still_rejected(name, tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, **{name: "bogus"})  # type: ignore[arg-type]


def test_the_global_settings_do_not_take_none(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, reasoning_effort="none")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, thinking="none")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "call", "effective_over_high"),
    [("", None, "high"), ("default", "", ""), ("low", "low", "low")],
)
def test_effort_override_translation(override, call, effective_over_high):
    assert call_effort(override) == call  # type: ignore[arg-type]
    assert effective_effort(override, "high") == effective_over_high  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "call", "effective_over_adaptive"),
    [("", None, "adaptive"), ("default", "", ""), ("disabled", "disabled", "disabled")],
)
def test_thinking_override_translation(override, call, effective_over_adaptive):
    assert call_thinking(override) == call  # type: ignore[arg-type]
    assert effective_thinking(override, "adaptive") == effective_over_adaptive  # type: ignore[arg-type]


async def _voice_request(settings, **overrides) -> dict:
    settings.anthropic_model = "claude-opus-4-7"
    settings.reasoning_effort = "high"
    settings.thinking = "adaptive"
    settings.voice_turn_model = ""
    settings.voice_max_response_tokens = 220
    for name, value in overrides.items():
        setattr(settings, name, value)
    from pincer.tools.registry import ToolRegistry

    async with ScriptedAnthropic(settings, [([text("Gerne.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, _SM, _CT, ToolRegistry())
        _chunks = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Hallo")]
    return llm.requests[0]


@pytest.fixture(autouse=True)
def _managers(session_manager, cost_tracker):
    global _SM, _CT  # noqa: PLW0603
    _SM, _CT = session_manager, cost_tracker


async def test_a_voice_turn_with_default_sends_no_effort_and_no_thinking_despite_the_global_ones(settings):
    body = await _voice_request(settings, voice_reasoning_effort="default", voice_thinking="default")

    assert "output_config" not in body
    assert "thinking" not in body


async def test_a_voice_turn_with_empty_overrides_still_inherits_the_global_ones(settings):
    body = await _voice_request(settings)

    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive"}


async def test_default_applies_to_each_voice_setting_on_its_own(settings):
    body = await _voice_request(settings, voice_reasoning_effort="default")

    assert "output_config" not in body
    assert body["thinking"] == {"type": "adaptive"}

"""`none` opts a voice or summary request out of effort/thinking (issue #345, review round 1).

Empty inherits the global PINCER_REASONING_EFFORT / PINCER_THINKING; `none` sends nothing, so
the model runs on its own default even when chat has a global setting.
"""

from __future__ import annotations

import pytest
from anthropic_sse import ScriptedAnthropic, text
from pydantic import ValidationError

from pincer.config import Settings
from pincer.config.llm import call_effort, call_thinking, effective_effort, effective_thinking
from pincer.core.agent import Agent


@pytest.mark.parametrize(
    "name",
    ["voice_reasoning_effort", "voice_thinking", "summary_reasoning_effort", "summary_thinking"],
)
def test_none_is_accepted_for_every_override(name, tmp_path):
    settings = Settings(data_dir=tmp_path, **{name: "none"})  # type: ignore[arg-type]

    assert getattr(settings, name) == "none"


@pytest.mark.parametrize("name", ["voice_reasoning_effort", "summary_thinking"])
def test_an_unknown_override_is_still_rejected(name, tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, **{name: "default"})  # type: ignore[arg-type]


def test_the_global_settings_do_not_take_none(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, reasoning_effort="none")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, thinking="none")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "call", "effective_over_high"),
    [("", None, "high"), ("none", "", ""), ("low", "low", "low")],
)
def test_effort_override_translation(override, call, effective_over_high):
    assert call_effort(override) == call  # type: ignore[arg-type]
    assert effective_effort(override, "high") == effective_over_high  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "call", "effective_over_adaptive"),
    [("", None, "adaptive"), ("none", "", ""), ("disabled", "disabled", "disabled")],
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


async def test_a_voice_turn_with_none_sends_no_effort_and_no_thinking_despite_the_global_ones(settings):
    body = await _voice_request(settings, voice_reasoning_effort="none", voice_thinking="none")

    assert "output_config" not in body
    assert "thinking" not in body


async def test_a_voice_turn_with_empty_overrides_still_inherits_the_global_ones(settings):
    body = await _voice_request(settings)

    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive"}


async def test_none_applies_to_each_voice_setting_on_its_own(settings):
    body = await _voice_request(settings, voice_reasoning_effort="none")

    assert "output_config" not in body
    assert body["thinking"] == {"type": "adaptive"}

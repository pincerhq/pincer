"""Tests for per-model request parameter rules."""

from __future__ import annotations

import pytest

from pincer.llm.model_params import (
    DroppedSetting,
    DropReason,
    ReasoningParams,
    ReasoningWire,
    anthropic_accepts_sampling,
    openai_is_reasoning_model,
    resolve_reasoning,
)


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5-5",
        "claude-fable-5-1",
        "claude-mythos-5",
        "claude-haiku-5-5-20260801",  # dated snapshot
        "us.anthropic.claude-haiku-5-5-v1:0",  # vendor-prefixed
        "claude-nova-6",  # a family released later defaults to omit
        "anthropic/claude-opus-4.7",  # gateway form: dotted minor
        "anthropic/claude-sonnet-5.5",
        "Claude-Opus-4-7",
    ],
)
def test_anthropic_rejects_sampling(model):
    assert anthropic_accepts_sampling(model) is False


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5-20251001",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-20250514",  # date is not a minor version
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-opus-4-1-20250805",
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
        "anthropic/claude-sonnet-4.5",
        "claude-3.5-sonnet",
        "claude-x",
        "local-model",
        "",
    ],
)
def test_anthropic_accepts_sampling(model):
    assert anthropic_accepts_sampling(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5",
        "gpt-5-mini",
        "gpt-5.2",
        "gpt-5-chat-latest",
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-6.1-sol",
        "gpt-7",  # a family released later defaults to reasoning
        "o1",
        "o3-mini",
        "o4-mini",
        "ft:gpt-5-mini:org::id",  # fine-tune
        "ft:o4-mini:org::x",
        "openai/gpt-5",  # vendor-prefixed
        "openai/o3",
        "GPT-5",
    ],
)
def test_openai_reasoning_model(model):
    assert openai_is_reasoning_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
        "grok-3",
        "llama3.2",
        "gpt-oss:20b",
        "ft:gpt-4o-mini:org::id",
        "openai/gpt-4o",
        "omni-moderation-latest",
        "my-model",
        "",
    ],
)
def test_openai_non_reasoning_model(model):
    assert openai_is_reasoning_model(model) is False


# ── Effort and thinking (issue #345) ──────────────────────────────────────────

_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_ALL = _EFFORTS
_UP_TO_MAX = ("low", "medium", "high", "high", "max")  # Opus/Sonnet 4.6: no xhigh
_UP_TO_HIGH = ("low", "medium", "high", "high", "high")  # Opus 4.5
_NO_EFFORT = (None,) * 5

_OFF = "already off"  # disabled: nothing sent, nothing reported
_DROP = "dropped"  # disabled: dropped as unsupported
_UNRECOGNISED = "unrecognised"

# model: (effort sent for low..max, adaptive accepted, disabled form, adaptive drops temperature)
_CLAUDE_TABLE: dict[str, tuple[tuple[str | None, ...], bool, object, bool]] = {
    "claude-haiku-4-5-20251001": (_NO_EFFORT, False, _OFF, False),
    "claude-sonnet-4-5": (_NO_EFFORT, False, _OFF, False),
    "claude-opus-4-5": (_UP_TO_HIGH, False, _OFF, False),
    "claude-opus-4-6": (_UP_TO_MAX, True, _OFF, True),
    "claude-sonnet-4-6": (_UP_TO_MAX, True, _OFF, True),
    "claude-opus-4-7": (_ALL, True, _OFF, False),
    "claude-sonnet-5": (_ALL, True, {"type": "disabled"}, False),
    "claude-opus-5": (_ALL, True, {"type": "disabled"}, False),
    "claude-opus-5-1": (_ALL, True, _DROP, False),  # disabled forms are exact to their version
    "claude-sonnet-5-5": (_ALL, True, {"type": "between_tools"}, False),
    "claude-haiku-5-5": (_ALL, True, {"type": "disabled"}, False),
    "claude-haiku-6": (_ALL, True, _DROP, False),
    "claude-opus-5-5": (_ALL, True, _DROP, False),
    "claude-fable-5-1": (_ALL, True, _DROP, False),
    "claude-mythos-5-1": (_ALL, True, _DROP, False),  # Fable's counterpart
    "claude-3-5-sonnet-20241022": (_NO_EFFORT, False, _UNRECOGNISED, False),
}


def _expected(model: str, effort: str, thinking: str) -> ReasoningParams:
    """What the table above says `resolve_reasoning` returns."""
    efforts, adaptive_ok, disabled, drops_temperature = _CLAUDE_TABLE[model]
    if disabled == _UNRECOGNISED:
        dropped = [DroppedSetting("reasoning_effort", effort, DropReason.UNRECOGNISED_MODEL)] if effort else []
        if thinking:
            dropped.append(DroppedSetting("thinking", thinking, DropReason.UNRECOGNISED_MODEL))
        return ReasoningParams(fragment={}, drop_temperature=False, dropped=tuple(dropped))

    fragment: dict = {}
    dropped = []
    drop_temperature = False
    sent_effort = efforts[_EFFORTS.index(effort)] if effort else None
    if effort and sent_effort is None:
        dropped.append(DroppedSetting("reasoning_effort", effort, DropReason.UNSUPPORTED_BY_MODEL))
    elif sent_effort:
        fragment["output_config"] = {"effort": sent_effort}
    if thinking == "adaptive":
        if adaptive_ok:
            fragment["thinking"] = {"type": "adaptive"}
            drop_temperature = drops_temperature
        else:
            dropped.append(DroppedSetting("thinking", "adaptive", DropReason.UNSUPPORTED_BY_MODEL))
    elif thinking == "disabled":
        if disabled == _DROP:
            dropped.append(DroppedSetting("thinking", "disabled", DropReason.UNSUPPORTED_BY_MODEL))
        elif isinstance(disabled, dict):
            if sent_effort in ("xhigh", "max"):
                dropped.append(DroppedSetting("thinking", "disabled", DropReason.INVALID_COMBINATION))
            else:
                fragment["thinking"] = disabled
    return ReasoningParams(fragment=fragment, drop_temperature=drop_temperature, dropped=tuple(dropped))


@pytest.mark.parametrize("thinking", ["", "adaptive", "disabled"])
@pytest.mark.parametrize("effort", ["", *_EFFORTS])
@pytest.mark.parametrize("model", list(_CLAUDE_TABLE))
def test_resolve_reasoning_per_model(model, effort, thinking):
    result = resolve_reasoning(ReasoningWire.ANTHROPIC, model, effort, thinking)
    assert result == _expected(model, effort, thinking)


@pytest.mark.parametrize(
    ("model", "bare", "effort", "thinking"),
    [
        ("us.anthropic.claude-haiku-5-5-v1:0", "claude-haiku-5-5", "high", "disabled"),  # Bedrock-style
        ("azure:claude-haiku-5-5", "claude-haiku-5-5", "high", "disabled"),  # provider-prefixed
        ("claude-haiku-5-5-20260801", "claude-haiku-5-5", "high", "disabled"),  # dated snapshot
        ("claude-sonnet-5-20260801", "claude-sonnet-5", "high", "disabled"),  # date is not a minor version
        ("anthropic/claude-opus-4.6", "claude-opus-4-6", "xhigh", "adaptive"),  # gateway, dotted minor
        ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5", "high", "disabled"),
        ("Claude-Opus-4-6", "claude-opus-4-6", "xhigh", "adaptive"),
    ],
)
def test_resolve_reasoning_recognises_id_forms(model, bare, effort, thinking):
    result = resolve_reasoning(ReasoningWire.ANTHROPIC, model, effort, thinking)
    assert result.fragment  # the bare id's rule applied, not the unrecognised path
    assert result == resolve_reasoning(ReasoningWire.ANTHROPIC, bare, effort, thinking)


@pytest.mark.parametrize("model", ["voice-fast", "claude-nova-6", "claude-3-haiku-20240307", ""])
def test_resolve_reasoning_drops_everything_for_an_unrecognised_model(model):
    result = resolve_reasoning(ReasoningWire.ANTHROPIC, model, "low", "adaptive")
    assert result == ReasoningParams(
        fragment={},
        drop_temperature=False,
        dropped=(
            DroppedSetting("reasoning_effort", "low", DropReason.UNRECOGNISED_MODEL),
            DroppedSetting("thinking", "adaptive", DropReason.UNRECOGNISED_MODEL),
        ),
    )


def test_resolve_reasoning_leaves_the_openai_wire_alone_for_now():
    """The OpenAI mapping arrives in slice 8 of #345; until then nothing is sent or reported."""
    assert resolve_reasoning(ReasoningWire.OPENAI, "gpt-5.2", "high", "disabled") == ReasoningParams()

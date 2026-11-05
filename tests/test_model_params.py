"""Tests for per-model request parameter rules."""

from __future__ import annotations

import logging

import pytest

from pincer.llm.model_params import (
    DroppedSetting,
    DropReason,
    ReasoningParams,
    ReasoningWire,
    anthropic_accepts_sampling,
    openai_is_reasoning_model,
    resolve_and_warn,
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


# ── OpenAI reasoning_effort ───────────────────────────────────────────────────

_UP_TO_XHIGH = ("low", "medium", "high", "xhigh", "xhigh")  # gpt-5.2+: max becomes xhigh

# model: (reasoning_effort sent for low..max, what `disabled` sends)
_OPENAI_TABLE: dict[str, tuple[tuple[str | None, ...], str]] = {
    "o1": (_UP_TO_HIGH, _DROP),  # reasoning can't be turned off
    "o3-mini": (_UP_TO_HIGH, _DROP),
    "o4-mini": (_UP_TO_HIGH, _DROP),
    "gpt-5": (_UP_TO_HIGH, "minimal"),
    "gpt-5-mini": (_UP_TO_HIGH, "minimal"),
    "gpt-5-codex": (_UP_TO_HIGH, _DROP),  # no page confirms minimal/none for codex
    "gpt-5.1": (_UP_TO_HIGH, "none"),
    "gpt-5.1-codex-mini": (_UP_TO_HIGH, _DROP),
    "gpt-5.2": (_UP_TO_XHIGH, "none"),
    "gpt-5.4-mini": (_UP_TO_XHIGH, "none"),
    # Pro models are Responses API only: not served over Chat Completions
    "o3-pro": (_NO_EFFORT, _UNRECOGNISED),
    "gpt-5-pro": (_NO_EFFORT, _UNRECOGNISED),
    "gpt-5.1-pro": (_NO_EFFORT, _UNRECOGNISED),
    "gpt-5.2-pro": (_NO_EFFORT, _UNRECOGNISED),
    "gpt-5-chat-latest": (_NO_EFFORT, _UNRECOGNISED),  # off the allow-list
    "gpt-4o-mini": (_NO_EFFORT, _UNRECOGNISED),
    "voice-fast": (_NO_EFFORT, _UNRECOGNISED),  # an Azure deployment name
}


def _expected_openai(model: str, effort: str, thinking: str) -> ReasoningParams:
    """What the table above says `resolve_reasoning` returns on the OpenAI wire.
    `adaptive` is never sent and never reported."""
    efforts, disabled = _OPENAI_TABLE[model]
    dropped = []
    if disabled == _UNRECOGNISED:
        if effort:
            dropped.append(DroppedSetting("reasoning_effort", effort, DropReason.UNRECOGNISED_MODEL))
        if thinking == "disabled":
            dropped.append(DroppedSetting("thinking", "disabled", DropReason.UNRECOGNISED_MODEL))
        return ReasoningParams(dropped=tuple(dropped))

    fragment: dict = {}
    sent_effort = efforts[_EFFORTS.index(effort)] if effort else None
    if effort and sent_effort is None:
        dropped.append(DroppedSetting("reasoning_effort", effort, DropReason.UNSUPPORTED_BY_MODEL))
    elif sent_effort:
        fragment["reasoning_effort"] = sent_effort
    if thinking == "disabled":
        if disabled == _DROP:
            dropped.append(DroppedSetting("thinking", "disabled", DropReason.UNSUPPORTED_BY_MODEL))
        elif sent_effort in ("xhigh", "max"):
            dropped.append(DroppedSetting("thinking", "disabled", DropReason.INVALID_COMBINATION))
        else:
            fragment["reasoning_effort"] = disabled  # one knob: disabled replaces the effort
    return ReasoningParams(fragment=fragment, dropped=tuple(dropped))


@pytest.mark.parametrize("thinking", ["", "adaptive", "disabled"])
@pytest.mark.parametrize("effort", ["", *_EFFORTS])
@pytest.mark.parametrize("model", list(_OPENAI_TABLE))
def test_resolve_reasoning_per_openai_model(model, effort, thinking):
    result = resolve_reasoning(ReasoningWire.OPENAI, model, effort, thinking)
    assert result == _expected_openai(model, effort, thinking)


@pytest.mark.parametrize(
    ("model", "bare"),
    [
        ("openai/gpt-5.2", "gpt-5.2"),  # gateway
        ("ft:gpt-5.1:org::x", "gpt-5.1"),  # fine-tune
        ("gpt-5-2025-08-07", "gpt-5"),  # a date is not a minor version
        ("GPT-5.2", "gpt-5.2"),
    ],
)
def test_resolve_reasoning_recognises_openai_id_forms(model, bare):
    result = resolve_reasoning(ReasoningWire.OPENAI, model, "low", "disabled")
    assert result.fragment  # the bare id's row applied, not the unrecognised path
    assert result == resolve_reasoning(ReasoningWire.OPENAI, bare, "low", "disabled")


def test_adaptive_is_ignored_on_openai_with_a_debug_line(caplog):
    """OpenAI has no adaptive setting: nothing sent, nothing reported, one DEBUG line."""
    import logging

    caplog.set_level(logging.DEBUG, logger="pincer.llm.model_params")

    assert resolve_reasoning(ReasoningWire.OPENAI, "gpt-5.2", "", "adaptive") == ReasoningParams()

    lines = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert lines == [(logging.DEBUG, "Thinking adaptive ignored for gpt-5.2: OpenAI models have no such setting")]


def test_disabled_replacing_an_effort_on_openai_is_a_debug_line(caplog):
    """One knob: `disabled` wins over the effort without a warning (the doctor shows what is sent)."""
    import logging

    caplog.set_level(logging.DEBUG, logger="pincer.llm.model_params")

    result = resolve_reasoning(ReasoningWire.OPENAI, "gpt-5.1", "low", "disabled")

    assert result == ReasoningParams(fragment={"reasoning_effort": "none"})
    lines = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert lines == [(logging.DEBUG, "Effort low replaced by none (thinking disabled) for gpt-5.1")]


# ── resolve_and_warn: what both providers use ──────────────────────────────


def _dropped_warnings(caplog: pytest.LogCaptureFixture, model: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "Not sending" in r.getMessage() and model in r.getMessage()]


def test_resolve_and_warn_returns_what_resolve_reasoning_does_and_warns_once_per_model_and_value(caplog):
    caplog.set_level(logging.DEBUG)
    model = "acme-warn-once-1"

    first = resolve_and_warn(ReasoningWire.ANTHROPIC, model, "high", "")
    second = resolve_and_warn(ReasoningWire.ANTHROPIC, model, "high", "")

    assert first == resolve_reasoning(ReasoningWire.ANTHROPIC, model, "high", "")
    assert first.dropped and second == first
    records = _dropped_warnings(caplog, model)
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
    assert "reasoning_effort=high" in records[0].getMessage()
    assert first.dropped[0].reason in records[0].getMessage()


def test_resolve_and_warn_keeps_the_two_wires_apart(caplog):
    caplog.set_level(logging.DEBUG)
    model = "acme-warn-once-2"

    resolve_and_warn(ReasoningWire.ANTHROPIC, model, "high", "")
    resolve_and_warn(ReasoningWire.OPENAI, model, "high", "")

    assert [r.levelno for r in _dropped_warnings(caplog, model)] == [logging.WARNING, logging.WARNING]


def test_resolve_and_warn_stays_quiet_when_nothing_is_dropped(caplog):
    caplog.set_level(logging.DEBUG)

    params = resolve_and_warn(ReasoningWire.ANTHROPIC, "claude-opus-4-7", "high", "")

    assert params.fragment == {"output_config": {"effort": "high"}}
    assert not [r for r in caplog.records if "Not sending" in r.getMessage()]

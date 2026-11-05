"""
Per-model request parameter rules.

Newer models reject request params older ones accept (a 400, not a silent
ignore), so the providers ask here before putting them on the wire. Both rules
are written so that a model released later lands on the restrictive side:
dropping `temperature` from a model that would have taken it is harmless,
sending it to one that rejects it fails the request.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from pincer.config.llm import ReasoningEffort, ThinkingMode

logger = logging.getLogger(__name__)


class _ClaudeReasoningRule(NamedTuple):
    efforts: tuple[str, ...]  # effort levels the model accepts
    adaptive: bool  # accepts thinking {"type": "adaptive"}
    thinks_by_default: bool  # False: thinking is already off, so "disabled" needs nothing sent


# claude-<family>-<major>[-<minor>][-<date>], optionally behind a vendor prefix
# (e.g. "us.anthropic.claude-haiku-5-5-v1:0"). Gateways write the minor with a
# dot ("anthropic/claude-opus-4.7"). The pre-4 naming
# (claude-3-5-sonnet-20241022) puts the version first and does not match.
_CLAUDE_MODEL_RE = re.compile(r"claude-([a-z]+)-(\d+)(?:[.-](\d{1,2})(?!\d))?")

# Last (major, minor) of each family that still accepts `temperature`.
_CLAUDE_LAST_SAMPLING_VERSION: dict[str, tuple[int, int]] = {
    "opus": (4, 6),
    "sonnet": (4, 6),
    "haiku": (4, 5),
}

# OpenAI families that predate reasoning models and accept sampling params.
_OPENAI_SAMPLING_PREFIXES = ("gpt-4", "gpt-3.5")
_OPENAI_MODEL_RE = re.compile(r"gpt-\d|o\d")

# ── Effort and thinking on Claude (issue #345) ──
# Effort goes out as output_config.effort (GA); thinking as {"type": ...}.
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
# `disabled` is rejected together with these: dropped there.
_EFFORTS_THAT_KEEP_THINKING = frozenset({"xhigh", "max"})
_NO_EFFORT: tuple[str, ...] = ()
_EFFORT_TO_HIGH = ("low", "medium", "high")
_EFFORT_TO_MAX_NO_XHIGH = ("low", "medium", "high", "max")
_EFFORT_ALL = _EFFORT_ORDER

# Per family, (first version, rule) in ascending order. A model uses the row of
# the highest listed version at or below its own, so effort and adaptive extend
# to later versions. Versions below the first row, families not listed and the
# pre-4 names are unrecognised: nothing is sent for them.
_CLAUDE_REASONING_RULES: dict[str, tuple[tuple[tuple[int, int], _ClaudeReasoningRule], ...]] = {
    "haiku": (
        ((4, 0), _ClaudeReasoningRule(_NO_EFFORT, adaptive=False, thinks_by_default=False)),
        ((5, 5), _ClaudeReasoningRule(_EFFORT_ALL, adaptive=True, thinks_by_default=True)),
    ),
    "sonnet": (
        ((4, 0), _ClaudeReasoningRule(_NO_EFFORT, adaptive=False, thinks_by_default=False)),
        ((4, 6), _ClaudeReasoningRule(_EFFORT_TO_MAX_NO_XHIGH, adaptive=True, thinks_by_default=False)),
        ((5, 0), _ClaudeReasoningRule(_EFFORT_ALL, adaptive=True, thinks_by_default=True)),
    ),
    "opus": (
        ((4, 0), _ClaudeReasoningRule(_NO_EFFORT, adaptive=False, thinks_by_default=False)),
        ((4, 5), _ClaudeReasoningRule(_EFFORT_TO_HIGH, adaptive=False, thinks_by_default=False)),
        ((4, 6), _ClaudeReasoningRule(_EFFORT_TO_MAX_NO_XHIGH, adaptive=True, thinks_by_default=False)),
        ((4, 7), _ClaudeReasoningRule(_EFFORT_ALL, adaptive=True, thinks_by_default=False)),
        ((5, 0), _ClaudeReasoningRule(_EFFORT_ALL, adaptive=True, thinks_by_default=True)),
    ),
    "fable": (((5, 0), _ClaudeReasoningRule(_EFFORT_ALL, adaptive=True, thinks_by_default=True)),),
}
# Mythos has Fable's API surface and thinking rules.
_CLAUDE_FAMILY_ALIASES = {"mythos": "fable"}

# How `disabled` is sent to a model that thinks by default, by exact version:
# a later version may not allow thinking off, so it gets nothing (the
# restrictive side). Opus 5.5 and Fable/Mythos can't turn thinking off at all.
# Sonnet 5.5's form takes no other field.
_CLAUDE_DISABLED_FORMS: dict[tuple[str, int, int], dict[str, str]] = {
    ("haiku", 5, 5): {"type": "disabled"},
    ("sonnet", 5, 0): {"type": "disabled"},
    ("sonnet", 5, 5): {"type": "between_tools"},
    ("opus", 5, 0): {"type": "disabled"},
}


def anthropic_accepts_sampling(model: str) -> bool:
    """Whether `temperature` may be sent to this Anthropic-wire model.

    Opus 4.7+, Sonnet 5+, Haiku 5+ and every later family (Fable, Mythos, …)
    reject it. Names that don't follow the Claude naming scheme — the old
    claude-3 ids, or whatever a compatible endpoint serves — keep it.
    """
    version = _claude_version(model)
    if version is None:
        return True
    family, major, minor = version
    last = _CLAUDE_LAST_SAMPLING_VERSION.get(family)
    if last is None:
        return False
    return (major, minor) <= last


def openai_is_reasoning_model(model: str) -> bool:
    """Whether this OpenAI-wire model is a reasoning model, which rejects
    `temperature` and wants `max_completion_tokens` instead of `max_tokens`.

    Everything OpenAI-named counts (o-series, GPT-5, GPT-6, …) except the
    legacy GPT-4 / GPT-3.5 families. Other names (grok, llama, gpt-oss on a
    compatible endpoint) are not OpenAI's to judge and are left alone. A
    fine-tune `ft:` or gateway `vendor/` prefix is stripped before matching.
    """
    bare = model.lower().removeprefix("ft:").rsplit("/", 1)[-1]
    if bare.startswith(_OPENAI_SAMPLING_PREFIXES):
        return False
    return _OPENAI_MODEL_RE.match(bare) is not None


# OpenAI models known to take `reasoning_effort` (low/medium/high), optionally
# dated. A strict allow-list, unlike the temperature rule above: here a false
# positive ADDS a parameter, and a model that rejects it fails every request.
# Left out on purpose: o1-mini / o1-preview and gpt-5-chat-* (no reasoning
# knob). Unknown or later names get no effort — the API default — until added.
_OPENAI_EFFORT_RE = re.compile(
    r"(?:o1(?:-pro)?|o3(?:-mini|-pro)?|o4-mini|gpt-5(?:\.\d+)?(?:-(?:mini|nano|pro|codex(?:-mini)?))?)"
    r"(?:-\d{4}-\d{2}-\d{2})?$"
)


def openai_accepts_reasoning_effort(model: str) -> bool:
    """Whether `reasoning_effort` may be sent to this OpenAI-wire model.

    The model id has to look like an OpenAI one: on Azure that is the
    deployment name, so a deployment called e.g. ``voice-fast`` never gets it.
    A fine-tune ``ft:<base>:…`` or gateway ``vendor/`` prefix is stripped.
    """
    bare = model.lower().rsplit("/", 1)[-1]
    if bare.startswith("ft:"):
        bare = bare.removeprefix("ft:").split(":", 1)[0]
    return _OPENAI_EFFORT_RE.match(bare) is not None


# ── Effort and thinking (issue #345) ──────────────────────────────────────────


class ReasoningWire(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class DropReason(StrEnum):
    UNRECOGNISED_MODEL = "unrecognised model"
    UNSUPPORTED_BY_MODEL = "not supported by this model"
    INVALID_COMBINATION = "not allowed at effort xhigh or max"


@dataclass(frozen=True)
class DroppedSetting:
    setting: Literal["reasoning_effort", "thinking"]
    value: str
    reason: DropReason


@dataclass(frozen=True)
class ReasoningParams:
    """What to send: `fragment` is merged into the request kwargs as is."""

    fragment: dict[str, Any] = field(default_factory=dict)
    drop_temperature: bool = False
    dropped: tuple[DroppedSetting, ...] = ()


def resolve_reasoning(
    wire: ReasoningWire, model: str, effort: ReasoningEffort, thinking: ThinkingMode
) -> ReasoningParams:
    """Translate the effort/thinking settings into what `model` accepts.

    Unsupported effort levels step down to the nearest supported one. Anything
    the model would reject, or that can't be placed because the model isn't
    recognised, is dropped and listed in `dropped` with the reason; logging it
    is the caller's job. Empty settings always resolve to nothing.
    """
    if wire is not ReasoningWire.ANTHROPIC or not (effort or thinking):
        return ReasoningParams()  # the OpenAI mapping is slice 8 of #345
    version = _claude_version(model)
    rule = _claude_reasoning_rule(version) if version else None
    if version is None or rule is None:
        return ReasoningParams(dropped=_all_dropped(effort, thinking, DropReason.UNRECOGNISED_MODEL))

    fragment: dict[str, Any] = {}
    dropped: list[DroppedSetting] = []
    drop_temperature = False
    sent_effort = _step_down(effort, rule.efforts) if effort else ""
    if effort and not sent_effort:
        dropped.append(DroppedSetting("reasoning_effort", effort, DropReason.UNSUPPORTED_BY_MODEL))
    elif sent_effort:
        if sent_effort != effort:
            logger.debug("Effort %s stepped down to %s for %s", effort, sent_effort, model)
        fragment["output_config"] = {"effort": sent_effort}

    if thinking == "adaptive":
        if rule.adaptive:
            fragment["thinking"] = {"type": "adaptive"}
            drop_temperature = anthropic_accepts_sampling(model)
        else:
            dropped.append(DroppedSetting("thinking", thinking, DropReason.UNSUPPORTED_BY_MODEL))
    elif thinking == "disabled" and rule.thinks_by_default:
        disabled = _CLAUDE_DISABLED_FORMS.get(version)
        if disabled is None:
            dropped.append(DroppedSetting("thinking", thinking, DropReason.UNSUPPORTED_BY_MODEL))
        elif sent_effort in _EFFORTS_THAT_KEEP_THINKING:
            dropped.append(DroppedSetting("thinking", thinking, DropReason.INVALID_COMBINATION))
        else:
            fragment["thinking"] = dict(disabled)
    return ReasoningParams(fragment=fragment, drop_temperature=drop_temperature, dropped=tuple(dropped))


def _claude_version(model: str) -> tuple[str, int, int] | None:
    """(family, major, minor) of a Claude-named model id, or None."""
    match = _CLAUDE_MODEL_RE.search(model.lower())
    if match is None:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3) or 0)


def _claude_reasoning_rule(version: tuple[str, int, int]) -> _ClaudeReasoningRule | None:
    """The table row for the highest listed version at or below this one."""
    family, major, minor = version
    rows = _CLAUDE_REASONING_RULES.get(_CLAUDE_FAMILY_ALIASES.get(family, family), ())
    found = None
    for since, rule in rows:
        if (major, minor) >= since:
            found = rule
    return found


def _step_down(effort: str, supported: tuple[str, ...]) -> str:
    """The highest supported level at or below `effort`; "" when there is none."""
    allowed = [level for level in _EFFORT_ORDER[: _EFFORT_ORDER.index(effort) + 1] if level in supported]
    return allowed[-1] if allowed else ""


def _all_dropped(effort: str, thinking: str, reason: DropReason) -> tuple[DroppedSetting, ...]:
    dropped = [DroppedSetting("reasoning_effort", effort, reason)] if effort else []
    if thinking:
        dropped.append(DroppedSetting("thinking", thinking, reason))
    return tuple(dropped)

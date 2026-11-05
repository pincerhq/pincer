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

from pincer.core.log_once import LogOncePerKey

if TYPE_CHECKING:
    from pincer.config.llm import ReasoningEffort, ThinkingMode

logger = logging.getLogger(__name__)

# An effort/thinking setting a model can't take: one WARNING per wire, model and value
_reasoning_dropped = LogOncePerKey(logger, logging.WARNING)


class _ClaudeReasoningRule(NamedTuple):
    efforts: tuple[str, ...]  # effort levels the model accepts
    adaptive: bool  # accepts thinking {"type": "adaptive"}
    thinks_by_default: bool  # False: thinking is already off, so "disabled" needs nothing sent


class _OpenAIReasoningRule(NamedTuple):
    efforts: tuple[str, ...]  # reasoning_effort levels the model accepts
    disabled: str | None  # the level `disabled` sends; None: reasoning can't be turned off
    thinks_by_default: bool  # reasons when no reasoning_effort is sent


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

# ── Output limits (issue #345, review round 1) ──
# Synchronous Messages API "Max output", per family and first version, ascending; a model
# uses the row of the highest version at or below its own. Read off each model's page,
# checked 2026-11-05 (https://platform.claude.com/docs/en/models/<model>/overview):
# opus-4-5 and haiku-4-5 64K; opus-4-6, 4-7, 4-8, 5, 5-5, sonnet-4-6, 5, 5-5, haiku-5-5,
# fable-5-1 128K (models overview). Versions below the first row of a family (Opus 4 / 4.1,
# Sonnet 4 / 4.5, the pre-4 names) were not confirmed: unknown.
_CLAUDE_MAX_OUTPUT_TOKENS: dict[str, tuple[tuple[tuple[int, int], int], ...]] = {
    "opus": (((4, 5), 64_000), ((4, 6), 128_000)),
    "sonnet": (((4, 6), 128_000),),
    "haiku": (((4, 5), 64_000), ((5, 5), 128_000)),
    "fable": (((5, 0), 128_000),),
}
# OpenAI "max output tokens" by exact model name, from https://developers.openai.com/api/docs/models/<name>
# (checked 2026-11-05). Dated snapshots are left out: their limits differ (gpt-4o-2024-05-13 is lower).
_OPENAI_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "gpt-4o": 16_384,
    "gpt-4o-mini": 16_384,
    "gpt-4.1": 32_768,
    "gpt-4.1-mini": 32_768,
    "o1": 100_000,
    "o3": 100_000,
    "o4-mini": 100_000,
    "gpt-5": 128_000,
    "gpt-5-mini": 128_000,
    "gpt-5-nano": 128_000,
    "gpt-5.1": 128_000,
    "gpt-5.2": 128_000,
}

# ── Effort on OpenAI (issue #345) ──
# OpenAI models known to take `reasoning_effort`, optionally dated. A strict
# allow-list, unlike the temperature rule above: here a false positive ADDS a
# parameter, and a model that rejects it fails every request. Left out on
# purpose: o1-mini / o1-preview and gpt-5-chat-* (no reasoning knob), and every
# pro model (o1-pro, o3-pro, gpt-5.x-pro): Responses API only, which this
# Chat Completions provider can't serve. Unknown or later names get no effort
# (the API default) until added. The groups pick the gpt-5 row: the dotted
# minor (a date is not one) and the variant.
_OPENAI_EFFORT_RE = re.compile(
    r"(?:o1|o3(?:-mini)?|o4-mini"
    r"|(?P<gpt5>gpt-5)(?:\.(?P<minor>\d+))?(?:-(?P<variant>mini|nano|codex(?:-mini)?))?)"
    r"(?:-\d{4}-\d{2}-\d{2})?$"
)
# Per-model levels, checked 2026-11-05. The API reference
# (https://platform.openai.com/docs/api-reference/chat/create) returned 403; sources used:
# - https://developers.openai.com/api/docs/models/gpt-5: "minimal, low, medium, and high"
# - https://developers.openai.com/api/docs/models/gpt-5.1: "none (default), low, medium, and high"
# - https://developers.openai.com/api/docs/models/gpt-5.2: "none (default), low, medium, high and xhigh"
# - o1-pro, o3-pro, gpt-5-pro, gpt-5.2-pro: Responses API only (v1/chat/completions not
#   supported), per their model pages, checked 2026-11-05:
#   https://developers.openai.com/api/docs/models/o1-pro
#   https://developers.openai.com/api/docs/models/o3-pro
#   https://developers.openai.com/api/docs/models/gpt-5-pro
#   https://developers.openai.com/api/docs/models/gpt-5.2-pro
# - the openai SDK's docstring generated from the API spec (openai 2.32.0,
#   types/chat/completion_create_params.py): models before gpt-5.1 "do not
#   support none"; xhigh only after gpt-5.1-codex-max.
# `disabled` sends the level that turns reasoning (nearly) off. The o-series
# and codex (no page confirms none/minimal there) get no such level.
# Default effort: models before gpt-5.1 reason at medium (the SDK docstring);
# gpt-5.1 and gpt-5.2 default to none (their model pages), so they don't think unless asked.
_OPENAI_O_SERIES_RULE = _OpenAIReasoningRule(_EFFORT_TO_HIGH, disabled=None, thinks_by_default=True)
_EFFORT_TO_XHIGH = ("low", "medium", "high", "xhigh")
# gpt-5 rows by first minor version, ascending; a model uses the highest at or below its own.
_OPENAI_GPT5_RULES: tuple[tuple[int, _OpenAIReasoningRule], ...] = (
    (0, _OpenAIReasoningRule(_EFFORT_TO_HIGH, disabled="minimal", thinks_by_default=True)),
    (1, _OpenAIReasoningRule(_EFFORT_TO_HIGH, disabled="none", thinks_by_default=False)),
    (2, _OpenAIReasoningRule(_EFFORT_TO_XHIGH, disabled="none", thinks_by_default=False)),
)
# The thinking fragments that turn extended thinking off. Sonnet 5.5's between_tools
# counts: "the model does no extended thinking, and the short progress updates it
# writes between tool calls come back as thinking blocks" (Claude API docs, Sonnet
# 5.5 migration guide, breaking change 1).
_CLAUDE_THINKING_OFF_TYPES = frozenset({"disabled", "between_tools"})

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


def model_max_output_tokens(model: str) -> int | None:
    """The model's own limit on `max_tokens` (its synchronous output limit), or None when
    it isn't known. Only rows read off a model page are listed, so an unrecognised model,
    an Azure deployment name or a dated OpenAI snapshot is None, and a caller must not ask
    such a model for more than it already asks. An `ft:` or gateway `vendor/` prefix is
    stripped."""
    version = _claude_version(model)
    if version is not None:
        family, major, minor = version
        rows = _CLAUDE_MAX_OUTPUT_TOKENS.get(_CLAUDE_FAMILY_ALIASES.get(family, family), ())
        found = None
        for since, limit in rows:
            if (major, minor) >= since:
                found = limit
        return found
    bare = model.lower().rsplit("/", 1)[-1]
    if bare.startswith("ft:"):
        bare = bare.removeprefix("ft:").split(":", 1)[0]
    return _OPENAI_MAX_OUTPUT_TOKENS.get(bare)


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


def openai_accepts_reasoning_effort(model: str) -> bool:
    """Whether `reasoning_effort` may be sent to this OpenAI-wire model.

    The model id has to look like an OpenAI one: on Azure that is the
    deployment name, so a deployment called e.g. ``voice-fast`` never gets it.
    A fine-tune ``ft:<base>:…`` or gateway ``vendor/`` prefix is stripped.
    """
    return _openai_effort_match(model) is not None


def _openai_effort_match(model: str) -> re.Match[str] | None:
    """The allow-list match for a model id, with any ``ft:`` / ``vendor/`` prefix stripped."""
    bare = model.lower().rsplit("/", 1)[-1]
    if bare.startswith("ft:"):
        bare = bare.removeprefix("ft:").split(":", 1)[0]
    return _OPENAI_EFFORT_RE.match(bare)


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
    if not (effort or thinking):
        return ReasoningParams()
    if wire is ReasoningWire.OPENAI:
        return _resolve_openai(model, effort, thinking)
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


def resolve_and_warn(
    wire: ReasoningWire,
    model: str,
    effort: ReasoningEffort,
    thinking: ThinkingMode,
    *,
    pinned: bool = False,
) -> ReasoningParams:
    """`resolve_reasoning`, plus one WARNING per wire, model and dropped value (repeats at DEBUG).

    `pinned` is passed by the internal structured calls (see STRUCTURED_CALL_EFFORT): their pin
    is not the operator's setting, so a dropped value is logged at DEBUG only and a model that
    can't take it doesn't warn every deployment. An operator's setting always warns.
    """
    params = resolve_reasoning(wire, model, effort, thinking)
    for dropped in params.dropped:
        if pinned:
            logger.debug("Not sending %s=%s to %s: %s", dropped.setting, dropped.value, model, dropped.reason)
            continue
        _reasoning_dropped.log(
            f"{wire}|{model}|{dropped.setting}|{dropped.value}",
            "Not sending %s=%s to %s: %s",
            dropped.setting,
            dropped.value,
            model,
            dropped.reason,
        )
    return params


def model_thinks(wire: ReasoningWire, model: str, params: ReasoningParams) -> bool | None:
    """Whether `model` does extended thinking on a request carrying `params`
    (from `resolve_reasoning`): what the request turns on or off, else the
    model's default. None when the model isn't recognised."""
    if wire is ReasoningWire.OPENAI:
        rule = _openai_reasoning_rule(model)
        if rule is None:
            return None
        level = params.fragment.get("reasoning_effort")
        return rule.thinks_by_default if level is None else level != "none"
    version = _claude_version(model)
    claude_rule = _claude_reasoning_rule(version) if version else None
    if claude_rule is None:
        return None
    sent = params.fragment.get("thinking", {}).get("type")
    if sent is None:
        return claude_rule.thinks_by_default
    return sent not in _CLAUDE_THINKING_OFF_TYPES


def _resolve_openai(model: str, effort: ReasoningEffort, thinking: ThinkingMode) -> ReasoningParams:
    """The OpenAI-wire half of `resolve_reasoning`: one `reasoning_effort` knob.

    `adaptive` has no OpenAI form and is ignored. `disabled` sends the row's
    off level, which replaces any effort; it is dropped at effort xhigh/max.
    """
    if thinking == "adaptive":
        logger.debug("Thinking adaptive ignored for %s: OpenAI models have no such setting", model)
    off = "disabled" if thinking == "disabled" else ""
    rule = _openai_reasoning_rule(model)
    if rule is None:
        return ReasoningParams(dropped=_all_dropped(effort, off, DropReason.UNRECOGNISED_MODEL))

    fragment: dict[str, Any] = {}
    dropped: list[DroppedSetting] = []
    sent_effort = _step_down(effort, rule.efforts) if effort else ""
    if effort and not sent_effort:
        dropped.append(DroppedSetting("reasoning_effort", effort, DropReason.UNSUPPORTED_BY_MODEL))
    elif sent_effort:
        if sent_effort != effort:
            logger.debug("Effort %s stepped down to %s for %s", effort, sent_effort, model)
        fragment["reasoning_effort"] = sent_effort
    if off:
        if rule.disabled is None:
            dropped.append(DroppedSetting("thinking", off, DropReason.UNSUPPORTED_BY_MODEL))
        elif sent_effort in _EFFORTS_THAT_KEEP_THINKING:
            dropped.append(DroppedSetting("thinking", off, DropReason.INVALID_COMBINATION))
        else:
            if sent_effort:
                logger.debug("Effort %s replaced by %s (thinking disabled) for %s", sent_effort, rule.disabled, model)
            fragment["reasoning_effort"] = rule.disabled
    return ReasoningParams(fragment=fragment, dropped=tuple(dropped))


def _openai_reasoning_rule(model: str) -> _OpenAIReasoningRule | None:
    """The table row for an allow-listed model; None off the allow-list."""
    match = _openai_effort_match(model)
    if match is None:
        return None
    if match.group("gpt5") is None:
        return _OPENAI_O_SERIES_RULE
    minor = int(match.group("minor") or 0)
    rule = next(rule for since, rule in reversed(_OPENAI_GPT5_RULES) if minor >= since)
    if (match.group("variant") or "").startswith("codex"):
        return rule._replace(disabled=None)
    return rule


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

"""
Per-model request parameter rules.

Newer models reject request params older ones accept (a 400, not a silent
ignore), so the providers ask here before putting them on the wire. Both rules
are written so that a model released later lands on the restrictive side:
dropping `temperature` from a model that would have taken it is harmless,
sending it to one that rejects it fails the request.
"""

from __future__ import annotations

import re

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


def anthropic_accepts_sampling(model: str) -> bool:
    """Whether `temperature` may be sent to this Anthropic-wire model.

    Opus 4.7+, Sonnet 5+, Haiku 5+ and every later family (Fable, Mythos, …)
    reject it. Names that don't follow the Claude naming scheme — the old
    claude-3 ids, or whatever a compatible endpoint serves — keep it.
    """
    match = _CLAUDE_MODEL_RE.search(model.lower())
    if match is None:
        return True
    last = _CLAUDE_LAST_SAMPLING_VERSION.get(match.group(1))
    if last is None:
        return False
    return (int(match.group(2)), int(match.group(3) or 0)) <= last


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

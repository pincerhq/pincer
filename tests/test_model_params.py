"""Tests for per-model request parameter rules."""

from __future__ import annotations

import pytest

from pincer.llm.model_params import anthropic_accepts_sampling, openai_is_reasoning_model


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

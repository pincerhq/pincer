"""Tests for MCP prompt registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pincer.mcp.prompts import (
    PromptArgument,
    PromptRegistry,
    explain_prompt_handler,
    register_builtin_prompts,
    summarize_prompt_handler,
)

# ── PromptRegistry ────────────────────────────────────────────────────────────


def test_register_prompt():
    registry = PromptRegistry()

    async def handler(text: str):
        return [{"role": "user", "content": {"type": "text", "text": text}}]

    registry.register(
        name="test_prompt",
        handler=handler,
        description="A test prompt",
        arguments=[{"name": "text", "description": "Input text", "required": True}],
    )
    prompts = registry.list_all()
    assert len(prompts) == 1
    assert prompts[0].name == "test_prompt"
    assert prompts[0].description == "A test prompt"
    assert len(prompts[0].arguments) == 1
    assert prompts[0].arguments[0].name == "text"
    assert prompts[0].arguments[0].required is True


def test_register_prompt_with_argument_objects():
    registry = PromptRegistry()

    async def handler(**kwargs):
        return []

    registry.register(
        name="test",
        handler=handler,
        description="Test",
        arguments=[PromptArgument(name="arg1", description="First", required=False)],
    )
    assert registry.list_all()[0].arguments[0].name == "arg1"


def test_list_all_empty():
    registry = PromptRegistry()
    assert registry.list_all() == []


def test_export_to_fmcp_success():
    registry = PromptRegistry()

    async def handler(**kwargs):
        return []

    registry.register(name="p1", handler=handler, description="Prompt 1")

    fmcp = MagicMock()
    decorator_fn = MagicMock(return_value=MagicMock())
    fmcp.prompt = MagicMock(return_value=decorator_fn)

    count = registry.export_to(fmcp)
    assert count == 1


def test_export_to_fmcp_no_prompt_attr():
    registry = PromptRegistry()

    async def handler(**kwargs):
        return []

    registry.register(name="p1", handler=handler, description="Test")

    fmcp = MagicMock(spec=[])  # no attributes
    count = registry.export_to(fmcp)
    assert count == 0


def test_export_to_fmcp_partial_failure():
    registry = PromptRegistry()

    async def h(**kw):
        return []

    registry.register(name="bad", handler=h, description="Bad")
    registry.register(name="good", handler=h, description="Good")

    call_count = 0
    fmcp = MagicMock()

    def prompt_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs.get("name") == "bad":
            raise RuntimeError("bad prompt")
        return MagicMock(return_value=MagicMock())

    fmcp.prompt = MagicMock(side_effect=prompt_side_effect)
    count = registry.export_to(fmcp)
    assert count == 1


# ── Built-in prompt handlers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_prompt_handler():
    messages = await summarize_prompt_handler(text="Hello world")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "Hello world" in messages[0]["content"]["text"]


@pytest.mark.asyncio
async def test_explain_prompt_handler_with_language():
    messages = await explain_prompt_handler(code="print('hi')", language="python")
    assert len(messages) == 1
    text = messages[0]["content"]["text"]
    assert "python" in text.lower()
    assert "print" in text


@pytest.mark.asyncio
async def test_explain_prompt_handler_no_language():
    messages = await explain_prompt_handler(code="x = 1")
    assert len(messages) == 1
    assert "x = 1" in messages[0]["content"]["text"]


# ── register_builtin_prompts ──────────────────────────────────────────────────


def test_register_builtin_prompts():
    registry = PromptRegistry()
    register_builtin_prompts(registry)
    names = [p.name for p in registry.list_all()]
    assert "pincer_summarize" in names
    assert "pincer_explain_code" in names

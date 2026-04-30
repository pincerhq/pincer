"""
MCP Prompts — server-side prompt template registration and serving.

Prompts expose reusable LLM prompt templates to MCP clients.
Clients discover prompts via prompts/list and render them via prompts/get.

Usage::

    registry = PromptRegistry()
    registry.register(
        name="summarize",
        handler=summarize_handler,   # async (text: str) -> list[dict]
        description="Summarize a block of text",
        arguments=[{"name": "text", "description": "Text to summarize", "required": True}],
    )
    registry.export_to(fmcp)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("pincer.mcp.prompts")


@dataclass
class PromptArgument:
    """Describes a single argument for a prompt template."""

    name: str
    description: str = ""
    required: bool = False


@dataclass
class PromptRegistration:
    """A single prompt template registered for MCP serving."""

    name: str
    handler: Callable[..., Awaitable[list[dict[str, Any]]]]
    description: str
    arguments: list[PromptArgument] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptRegistry:
    """
    Stores prompt registrations and exports them to a FastMCP instance.

    Prompts are exposed via MCP's prompts/* protocol:
    - prompts/list  — enumerate available prompts
    - prompts/get   — render a prompt by name with arguments

    Handlers receive keyword arguments matching the prompt's argument names
    and must return a list of MCP message dicts:
        [{"role": "user", "content": {"type": "text", "text": "..."}}]
    """

    def __init__(self) -> None:
        self._prompts: list[PromptRegistration] = []

    def register(
        self,
        name: str,
        handler: Callable[..., Awaitable[list[dict[str, Any]]]],
        description: str,
        arguments: list[dict[str, Any]] | list[PromptArgument] | None = None,
    ) -> None:
        """Register a prompt template handler."""
        parsed_args: list[PromptArgument] = []
        for arg in arguments or []:
            if isinstance(arg, dict):
                parsed_args.append(
                    PromptArgument(
                        name=arg.get("name", ""),
                        description=arg.get("description", ""),
                        required=arg.get("required", False),
                    )
                )
            elif isinstance(arg, PromptArgument):
                parsed_args.append(arg)

        self._prompts.append(
            PromptRegistration(
                name=name,
                handler=handler,
                description=description,
                arguments=parsed_args,
            )
        )
        logger.debug("Registered MCP prompt: %s", name)

    def list_all(self) -> list[PromptRegistration]:
        """Return all registered prompts."""
        return list(self._prompts)

    def export_to(self, fmcp: Any) -> int:
        """
        Register all prompts on a FastMCP instance.

        Returns the number of prompts successfully registered.
        """
        count = 0
        for reg in self._prompts:
            try:
                _register_prompt_on_fmcp(fmcp, reg)
                count += 1
            except Exception as e:
                logger.warning("Failed to register prompt '%s' on FastMCP: %s", reg.name, e)
        if count:
            logger.info("MCP server: registered %d prompt(s)", count)
        return count


def _register_prompt_on_fmcp(fmcp: Any, reg: PromptRegistration) -> None:
    """Register a single PromptRegistration on a FastMCP instance."""
    prompt_decorator = getattr(fmcp, "prompt", None)
    if prompt_decorator is None:
        raise AttributeError("FastMCP instance has no 'prompt' method")

    handler = reg.handler
    name = reg.name
    description = reg.description

    async def _prompt_fn(**kwargs: Any) -> list[dict[str, Any]]:
        try:
            return await handler(**kwargs)
        except Exception as e:
            logger.exception("Prompt handler for '%s' raised: %s", name, e)
            return [{"role": "user", "content": {"type": "text", "text": f"[Error: {e}]"}}]

    _prompt_fn.__name__ = name.replace("-", "_").replace(" ", "_")
    _prompt_fn.__doc__ = description

    # Try with description keyword arg first, fall back to positional
    try:
        prompt_decorator(name=name, description=description)(_prompt_fn)
    except TypeError:
        try:
            prompt_decorator(name)(_prompt_fn)
        except TypeError:
            prompt_decorator()(_prompt_fn)


# ── Built-in prompt templates ──────────────────────────────────────────────────


async def summarize_prompt_handler(text: str) -> list[dict[str, Any]]:
    """Summarize the given text."""
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": f"Please provide a concise summary of the following:\n\n{text}",
            },
        }
    ]


async def explain_prompt_handler(code: str, language: str = "") -> list[dict[str, Any]]:
    """Explain what a piece of code does."""
    lang_note = f" ({language})" if language else ""
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": f"Explain what the following code{lang_note} does:\n\n```\n{code}\n```",
            },
        }
    ]


def register_builtin_prompts(registry: PromptRegistry) -> None:
    """Register Pincer's built-in prompt templates on a PromptRegistry."""
    registry.register(
        name="pincer_summarize",
        handler=summarize_prompt_handler,
        description="Summarize a block of text",
        arguments=[{"name": "text", "description": "Text to summarize", "required": True}],
    )
    registry.register(
        name="pincer_explain_code",
        handler=explain_prompt_handler,
        description="Explain what a piece of code does",
        arguments=[
            {"name": "code", "description": "Code to explain", "required": True},
            {"name": "language", "description": "Programming language", "required": False},
        ],
    )

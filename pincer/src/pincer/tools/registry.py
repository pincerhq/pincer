"""
Tool registry: register, discover, and execute tools.

Tools are async Python functions decorated with metadata.
The registry generates JSON schemas for the LLM and dispatches calls.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

from pincer.exceptions import ToolNotFoundError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Python type -> JSON Schema type
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolDef:
    """Definition of a registered tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Awaitable[str]]
    require_approval: bool = False


class ToolRegistry:
    """Manages available tools for the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    @property
    def has_tools(self) -> bool:
        return len(self._tools) > 0

    def list_tools(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[..., Awaitable[str]],
        parameters: dict[str, Any] | None = None,
        require_approval: bool = False,
    ) -> None:
        """Register a tool. If parameters is None, auto-generates schema from type hints."""
        if parameters is None:
            parameters = self._schema_from_hints(handler)

        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            require_approval=require_approval,
        )
        logger.info("Registered tool: %s", name)

    def get_schemas(self) -> list[dict[str, Any]]:
        """Get all tool schemas in Anthropic tool format."""
        schemas: list[dict[str, Any]] = []
        for tool in self._tools.values():
            schemas.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            })
        return schemas

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool by name with given arguments."""
        if name not in self._tools:
            raise ToolNotFoundError(
                f"Tool '{name}' not found. Available: {list(self._tools.keys())}"
            )

        tool = self._tools[name]

        # Inject 'context' if the handler accepts it
        sig = inspect.signature(tool.handler)
        if "context" in sig.parameters:
            arguments["context"] = context or {}

        result = await tool.handler(**arguments)

        # Truncate very long results
        if len(result) > 8000:
            result = result[:7900] + "\n...[truncated, output too long]"

        return result

    def _schema_from_hints(self, fn: Callable[..., Any]) -> dict[str, Any]:
        """Auto-generate JSON Schema from function type hints."""
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls", "context"):
                continue
            param_type = hints.get(param_name, str)
            json_type = _TYPE_MAP.get(param_type, "string")
            properties[param_name] = {"type": json_type}

            # Extract description from docstring (simple)
            if fn.__doc__:
                for line in fn.__doc__.splitlines():
                    stripped = line.strip()
                    if stripped.lower().startswith(f"{param_name}:"):
                        desc = stripped.split(":", 1)[-1].strip()
                        properties[param_name]["description"] = desc
                    elif stripped.lower().startswith(f"{param_name} —"):
                        desc = stripped.split("—", 1)[-1].strip()
                        properties[param_name]["description"] = desc

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

"""Minimal ToolRegistry Protocol used for type-checking in tools_*.py."""

from __future__ import annotations

from typing import Any, Protocol


class ToolRegistry(Protocol):
    def register(
        self,
        name: str,
        description: str,
        handler: Any,
        parameters: dict[str, Any] | None = ...,
        require_approval: bool = ...,
    ) -> None: ...

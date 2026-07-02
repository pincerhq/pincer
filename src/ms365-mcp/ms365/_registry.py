"""Minimal ToolRegistry Protocol used for type-checking in tools_*.py."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastmcp import Context

    from ms365.graph_client import GraphClient

# Resolves the caller's identity (read off `ctx`) to that identity's GraphClient,
# authenticating on first use if needed. Passed into every `register_*_tools()`
# instead of a single, fixed `GraphClient` so tools work across multiple
# Microsoft accounts in the same server process.
ClientResolver = Callable[["Context"], Awaitable["GraphClient"]]


class ToolRegistry(Protocol):
    def register(
        self,
        name: str,
        description: str,
        handler: Any,
        parameters: dict[str, Any] | None = ...,
        require_approval: bool = ...,
    ) -> None: ...

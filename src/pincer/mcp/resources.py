"""
MCP Resources — server-side resource registration and serving.

Resources expose live or static data to MCP clients via URI-addressed
read operations. Examples: recent memory, calendar events, inbox.

Usage::

    registry = ResourceRegistry()
    registry.register(
        uri="pincer://memory/recent",
        handler=get_recent_memory,
        description="Recent conversation memory",
        mime_type="text/plain",
    )
    registry.export_to(fmcp)   # Register on a FastMCP instance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("pincer.mcp.resources")


@dataclass
class ResourceRegistration:
    """A single resource registered for MCP serving."""

    uri: str
    handler: Callable[[], Awaitable[str]]
    description: str
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)


class ResourceRegistry:
    """
    Stores resource registrations and exports them to a FastMCP instance.

    Resources are exposed via MCP's resources/* protocol:
    - resources/list  — enumerate available resources
    - resources/read  — read a specific resource by URI

    In embedded mode, handlers return live data (calendar, memory, email).
    In standalone mode, handlers may return static or file-backed content.
    """

    def __init__(self) -> None:
        self._resources: list[ResourceRegistration] = []

    def register(
        self,
        uri: str,
        handler: Callable[[], Awaitable[str]],
        description: str,
        mime_type: str = "text/plain",
    ) -> None:
        """Register a resource handler for the given URI."""
        self._resources.append(
            ResourceRegistration(
                uri=uri,
                handler=handler,
                description=description,
                mime_type=mime_type,
            )
        )
        logger.debug("Registered MCP resource: %s", uri)

    def list_all(self) -> list[ResourceRegistration]:
        """Return all registered resources."""
        return list(self._resources)

    def export_to(self, fmcp: Any) -> int:
        """
        Register all resources on a FastMCP instance.

        Returns the number of resources successfully registered.
        Silently skips registration if FastMCP doesn't support resources.
        """
        count = 0
        for reg in self._resources:
            try:
                _register_resource_on_fmcp(fmcp, reg)
                count += 1
            except Exception as e:
                logger.warning("Failed to register resource '%s' on FastMCP: %s", reg.uri, e)
        if count:
            logger.info("MCP server: registered %d resource(s)", count)
        return count


def _register_resource_on_fmcp(fmcp: Any, reg: ResourceRegistration) -> None:
    """Register a single ResourceRegistration on a FastMCP instance."""
    handler = reg.handler
    uri = reg.uri
    description = reg.description
    mime_type = reg.mime_type

    # FastMCP resources are registered via @fmcp.resource(uri) decorator.
    # We call it programmatically: fmcp.resource(uri)(handler_fn)
    resource_decorator = getattr(fmcp, "resource", None)
    if resource_decorator is None:
        raise AttributeError("FastMCP instance has no 'resource' method")

    async def _resource_fn() -> str:
        try:
            return await handler()
        except Exception as e:
            logger.exception("Resource handler for '%s' raised: %s", uri, e)
            return f"[Error reading resource {uri}: {e}]"

    _resource_fn.__name__ = _uri_to_name(uri)
    _resource_fn.__doc__ = description

    # Try with keyword args first (newer FastMCP), fall back to positional
    try:
        resource_decorator(uri, description=description, mime_type=mime_type)(_resource_fn)
    except TypeError:
        resource_decorator(uri)(_resource_fn)


def _uri_to_name(uri: str) -> str:
    """Convert a URI to a valid Python identifier for use as function name."""
    return uri.replace("://", "_").replace("/", "_").replace("-", "_").replace(".", "_")

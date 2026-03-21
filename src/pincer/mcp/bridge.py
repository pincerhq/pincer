"""
MCPToolBridge — converts MCP tools into Pincer ToolRegistry entries.

For each tool discovered on an MCP server, creates an async callable that:
1. Checks approval requirements (config patterns vs. MCP annotations)
2. Calls the MCP server via MCPClientSession
3. Logs the call to the audit log
4. Returns formatted text output
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.mcp.audit import MCPAuditLogger
    from pincer.mcp.client import MCPClientSession
    from pincer.mcp.security import MCPSecurityGate
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger("pincer.mcp")


def _requires_approval(tool: Any, approval_patterns: list[str]) -> bool:
    """
    Determine if an MCP tool requires user approval before execution.

    Priority:
    1. ["none"] in patterns → never require approval (explicit trust)
    2. ["*"] in patterns → always require approval
    3. Glob patterns matched against tool name
    4. MCP tool annotations (destructiveHint → require, readOnlyHint → don't)
    5. Default: require approval (fail-safe)
    """
    if "none" in approval_patterns:
        return False

    if "*" in approval_patterns:
        return True

    tool_name: str = tool.name

    # Check positive patterns (require approval) and negative (exclude from approval)
    for pattern in approval_patterns:
        if pattern.startswith("!"):
            if fnmatch.fnmatch(tool_name, pattern[1:]):
                return False
        elif fnmatch.fnmatch(tool_name, pattern):
            return True

    # Fall back to MCP annotations if available
    annotations = getattr(tool, "annotations", None)
    if annotations:
        if getattr(annotations, "destructiveHint", False):
            return True
        if getattr(annotations, "readOnlyHint", False):
            return False

    return True  # Fail-safe default


def _format_result(result: Any) -> str:
    """Extract human-readable text from MCP CallToolResult."""
    content = getattr(result, "content", [])
    if not content:
        return "(empty response)"

    parts: list[str] = []
    for item in content:
        item_type = getattr(item, "type", "")
        if item_type == "text":
            parts.append(item.text)
        elif item_type == "image":
            mime = getattr(item, "mimeType", "image")
            parts.append(f"[Image: {mime}]")
        elif item_type == "resource":
            resource = getattr(item, "resource", None)
            uri = getattr(resource, "uri", "?") if resource else "?"
            parts.append(f"[Resource: {uri}]")
        else:
            parts.append(f"[{item_type}]")

    return "\n".join(parts) if parts else "(empty response)"


class MCPToolBridge:
    """
    Bridges MCP tools into Pincer's tool registry.

    Tracks all registered tool names so they can be deregistered cleanly
    on server disconnect or shutdown.
    """

    def __init__(
        self,
        session: MCPClientSession,
        tool_registry: ToolRegistry,
        audit_logger: MCPAuditLogger,
        prefix: bool = True,
        security_gate: MCPSecurityGate | None = None,
    ) -> None:
        self.session = session
        self.tool_registry = tool_registry
        self.audit_logger = audit_logger
        self.prefix = prefix
        self.security_gate = security_gate
        self._registered_names: list[str] = []

    def register_tools(self) -> int:
        """
        Register all MCP tools from this session into the tool registry.

        Returns number of tools registered. Skips tools with invalid schemas.
        """
        count = 0
        for tool in self.session.tools:
            pincer_name = self._make_name(tool.name)

            # Collision detection: refuse to shadow existing tools
            if pincer_name in self.tool_registry.list_tools():
                logger.error(
                    "MCP '%s' tool '%s' collides with existing tool '%s' — skipping",
                    self.session.name,
                    tool.name,
                    pincer_name,
                )
                continue

            approval = _requires_approval(tool, self.session.config.approval_required)

            try:
                schema = _convert_schema(getattr(tool, "inputSchema", None))
            except Exception:
                logger.warning("MCP '%s' tool '%s': invalid schema, skipping", self.session.name, tool.name)
                continue

            handler = self._make_handler(tool.name, pincer_name)
            description = (getattr(tool, "description", None) or f"MCP tool: {tool.name}") or f"MCP tool: {tool.name}"

            self.tool_registry.register(
                name=pincer_name,
                description=description,
                parameters=schema,
                handler=handler,
                require_approval=approval,
            )
            self._registered_names.append(pincer_name)
            count += 1

        if count:
            logger.info("MCP '%s': registered %d tools", self.session.name, count)
        return count

    def deregister_tools(self) -> None:
        """Remove all tools from this session from the registry."""
        for name in self._registered_names:
            self.tool_registry.deregister(name)
        removed = len(self._registered_names)
        self._registered_names.clear()
        if removed:
            logger.info("MCP '%s': deregistered %d tools", self.session.name, removed)

    def update_tools(self) -> int:
        """Deregister old tools and register the current tool list. Returns new count."""
        self.deregister_tools()
        return self.register_tools()

    def _make_name(self, tool_name: str) -> str:
        if self.prefix:
            return f"{self.session.name}__{tool_name}"
        return tool_name

    def _make_handler(self, mcp_name: str, pincer_name: str) -> Any:
        """Create an async callable that wraps an MCP tool call."""
        session = self.session
        audit = self.audit_logger
        security = self.security_gate

        async def handler(**kwargs: Any) -> str:
            kwargs.pop("context", None)

            # Rate limiting
            if security and not security.check_rate_limit(session.name, mcp_name):
                return f"[RateLimited] Too many calls to {session.name}::{mcp_name} — slow down"

            call_key = str(uuid.uuid4())
            audit.start_call(session.name, mcp_name, call_key)

            try:
                result = await session.call_tool(mcp_name, kwargs)
                output = _format_result(result)

                # Sanitize output for prompt injection
                if security:
                    output = security.sanitize_output(session.name, mcp_name, output)

                await audit.log_tool_call(
                    session.name,
                    mcp_name,
                    call_key,
                    kwargs,
                    success=True,
                    output_length=len(output),
                )
                return output
            except Exception as e:
                await audit.log_tool_call(
                    session.name,
                    mcp_name,
                    call_key,
                    kwargs,
                    success=False,
                    error=str(e),
                )
                raise

        handler.__name__ = pincer_name
        handler.__doc__ = f"MCP tool '{mcp_name}' from server '{session.name}'"
        return handler


def _convert_schema(input_schema: Any) -> dict[str, Any]:
    """
    Convert MCP tool inputSchema to Pincer parameter format (JSON Schema passthrough).

    MCP uses standard JSON Schema for inputSchema, which Pincer's registry
    accepts directly. Returns a minimal valid schema if none is provided.
    """
    if not input_schema:
        return {"type": "object", "properties": {}}

    # inputSchema may be a dict or a pydantic model
    schema: dict[str, Any]
    if hasattr(input_schema, "model_dump"):
        schema = dict(input_schema.model_dump(exclude_none=True))
    elif isinstance(input_schema, dict):
        schema = dict(input_schema)
    else:
        return {"type": "object", "properties": {}}

    # Ensure it's a valid object schema
    if "type" not in schema:
        schema["type"] = "object"
    if "properties" not in schema:
        schema["properties"] = {}

    return schema

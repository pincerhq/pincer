"""Tests for MCPToolBridge — tool registration, approval logic, result formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pincer.mcp.bridge import MCPToolBridge, _convert_schema, _format_result, _requires_approval

# ── Helpers / stubs ──────────────────────────────────────────────────────────


@dataclass
class FakeAnnotations:
    readOnlyHint: bool = False  # noqa: N815
    destructiveHint: bool = False  # noqa: N815


@dataclass
class FakeTool:
    name: str
    description: str = "A test tool"
    inputSchema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})  # noqa: N815
    annotations: FakeAnnotations | None = None


@dataclass
class FakeTextContent:
    type: str = "text"
    text: str = "hello"


@dataclass
class FakeImageContent:
    type: str = "image"
    mimeType: str = "image/png"  # noqa: N815


@dataclass
class FakeCallToolResult:
    content: list[Any] = field(default_factory=list)


def _make_session(tools: list[FakeTool], server_name: str = "testserver"):
    session = MagicMock()
    session.name = server_name
    session.tools = tools
    session.config = MagicMock()
    session.config.approval_required = ["*"]
    session.connected = True
    return session


def _make_registry():
    from pincer.tools.registry import ToolRegistry

    return ToolRegistry()


def _make_audit():
    from pincer.mcp.audit import MCPAuditLogger

    return MCPAuditLogger(None)  # No real audit logger in unit tests


# ── Approval logic ────────────────────────────────────────────────────────────


def test_wildcard_requires_all() -> None:
    tool = FakeTool("create_issue")
    assert _requires_approval(tool, ["*"]) is True


def test_none_skips_all() -> None:
    tool = FakeTool("create_issue")
    assert _requires_approval(tool, ["none"]) is False


def test_glob_pattern_match() -> None:
    tool = FakeTool("create_issue")
    assert _requires_approval(tool, ["create_*"]) is True


def test_glob_pattern_no_match() -> None:
    """When patterns are set but none match, tool is not in the allowlist → auto-approve."""
    tool = FakeTool("read_file")
    assert _requires_approval(tool, ["create_*"]) is False  # Not in allowlist → auto-approve


def test_negative_pattern_excludes() -> None:
    tool = FakeTool("read_file")
    assert _requires_approval(tool, ["!read_*"]) is False


def test_annotation_readonly_no_approval() -> None:
    tool = FakeTool("read_file", annotations=FakeAnnotations(readOnlyHint=True))
    # No patterns match "read_file" with patterns=[]...
    # But with empty patterns list we fall through to annotations
    assert _requires_approval(tool, []) is False


def test_annotation_destructive_requires_approval() -> None:
    tool = FakeTool("delete_all", annotations=FakeAnnotations(destructiveHint=True))
    assert _requires_approval(tool, []) is True


def test_fail_safe_default() -> None:
    """No patterns, no annotations → require approval (fail-safe)."""
    tool = FakeTool("unknown_tool")
    assert _requires_approval(tool, []) is True


# ── Result formatting ─────────────────────────────────────────────────────────


def test_format_text_content() -> None:
    result = FakeCallToolResult(content=[FakeTextContent(text="hello world")])
    assert _format_result(result) == "hello world"


def test_format_image_content() -> None:
    result = FakeCallToolResult(content=[FakeImageContent(mimeType="image/png")])
    assert _format_result(result) == "[Image: image/png]"


def test_format_multiple_content_items() -> None:
    result = FakeCallToolResult(
        content=[
            FakeTextContent(text="line 1"),
            FakeTextContent(text="line 2"),
        ]
    )
    assert _format_result(result) == "line 1\nline 2"


def test_format_empty_content() -> None:
    result = FakeCallToolResult(content=[])
    assert _format_result(result) == "(empty response)"


# ── Schema conversion ─────────────────────────────────────────────────────────


def test_convert_none_schema() -> None:
    schema = _convert_schema(None)
    assert schema == {"type": "object", "properties": {}}


def test_convert_dict_schema() -> None:
    raw = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    schema = _convert_schema(raw)
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]


def test_convert_schema_adds_missing_type() -> None:
    schema = _convert_schema({"properties": {"x": {"type": "integer"}}})
    assert schema["type"] == "object"


# ── Tool registration / deregistration ───────────────────────────────────────


def test_register_tools_adds_to_registry() -> None:
    tools = [FakeTool("list_repos", description="List GitHub repos")]
    session = _make_session(tools)
    session.config.approval_required = ["*"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    count = bridge.register_tools()

    assert count == 1
    assert "testserver__list_repos" in registry.list_tools()


def test_register_without_prefix() -> None:
    tools = [FakeTool("my_tool")]
    session = _make_session(tools)
    session.config.approval_required = ["none"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=False)
    bridge.register_tools()

    assert "my_tool" in registry.list_tools()
    assert "testserver__my_tool" not in registry.list_tools()


def test_deregister_tools_removes_from_registry() -> None:
    tools = [FakeTool("list_repos"), FakeTool("create_issue")]
    session = _make_session(tools)
    session.config.approval_required = ["*"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()
    assert len(registry.list_tools()) == 2

    bridge.deregister_tools()
    assert len(registry.list_tools()) == 0


def test_approval_flag_set_on_registry_tool() -> None:
    tools = [FakeTool("delete_repo")]
    session = _make_session(tools)
    session.config.approval_required = ["delete_*"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()

    assert registry.requires_approval("testserver__delete_repo") is True


def test_approval_not_required_for_readonly() -> None:
    tools = [FakeTool("get_file", annotations=FakeAnnotations(readOnlyHint=True))]
    session = _make_session(tools)
    session.config.approval_required = []  # No patterns → fall back to annotations
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()

    assert registry.requires_approval("testserver__get_file") is False


async def test_handler_calls_session() -> None:
    """The registered handler calls session.call_tool with correct args."""
    tool = FakeTool("echo_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    session.call_tool = AsyncMock(return_value=FakeCallToolResult(content=[FakeTextContent(text="echo: hello")]))

    registry = _make_registry()
    audit = _make_audit()
    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()

    result = await registry.execute("testserver__echo_tool", {"message": "hello"})
    assert "echo: hello" in result
    session.call_tool.assert_called_once_with("echo_tool", {"message": "hello"})


async def test_handler_logs_audit_on_success() -> None:
    """Successful tool call records an audit entry."""
    from pincer.security.audit import AuditLogger

    tool = FakeTool("my_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    session.call_tool = AsyncMock(return_value=FakeCallToolResult(content=[FakeTextContent(text="ok")]))

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        real_logger = AuditLogger(db_path=Path(tmp) / "audit.db")
        await real_logger.initialize()

        from pincer.mcp.audit import MCPAuditLogger

        audit = MCPAuditLogger(real_logger)
        registry = _make_registry()
        bridge = MCPToolBridge(session, registry, audit, prefix=True)
        bridge.register_tools()

        await registry.execute("testserver__my_tool", {})

        await real_logger._flush_pending()
        results = await real_logger.query(user_id="mcp:testserver")
        assert any(r["approved"] == 1 for r in results)

        await real_logger.shutdown()


async def test_handler_logs_audit_on_error() -> None:
    """Failed tool call records an audit entry with approved=False."""
    from pincer.security.audit import AuditLogger

    tool = FakeTool("bad_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    session.call_tool = AsyncMock(side_effect=RuntimeError("server error"))

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        real_logger = AuditLogger(db_path=Path(tmp) / "audit.db")
        await real_logger.initialize()

        from pincer.mcp.audit import MCPAuditLogger

        audit = MCPAuditLogger(real_logger)
        registry = _make_registry()
        bridge = MCPToolBridge(session, registry, audit, prefix=True)
        bridge.register_tools()

        import pytest

        with pytest.raises(RuntimeError):
            await registry.execute("testserver__bad_tool", {})

        await real_logger._flush_pending()
        results = await real_logger.query(user_id="mcp:testserver")
        assert any(r["approved"] == 0 for r in results)

        await real_logger.shutdown()


async def test_handler_measures_duration() -> None:
    """Handler records a non-zero duration in the audit logger."""
    import time

    from pincer.mcp.audit import MCPAuditLogger

    durations: list[float] = []

    class _CapturingAudit(MCPAuditLogger):
        async def log_tool_call(self, server, tool, call_key, arguments, *, success, output_length=None, error=None):
            start = self._timers.pop(call_key, None)
            if start:
                durations.append(time.monotonic() - start)

    tool = FakeTool("timed_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    session.call_tool = AsyncMock(return_value=FakeCallToolResult(content=[FakeTextContent(text="done")]))

    registry = _make_registry()
    audit = _CapturingAudit(None)
    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()

    await registry.execute("testserver__timed_tool", {})
    assert len(durations) == 1
    assert durations[0] >= 0


def test_collision_with_existing_tool_blocked() -> None:
    """An MCP tool that would shadow an existing registry tool is skipped."""
    registry = _make_registry()

    async def _builtin() -> str:
        return "builtin"

    registry.register(name="testserver__my_tool", description="Builtin tool", handler=_builtin)

    tool = FakeTool("my_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    count = bridge.register_tools()

    # Tool not re-registered
    assert count == 0
    # Original handler still present (not replaced)
    assert registry.list_tools() == ["testserver__my_tool"]


def test_format_resource_content() -> None:
    """Resource-type items are formatted as [Resource: uri]."""
    from dataclasses import dataclass

    @dataclass
    class FakeResource:
        uri: str = "file:///tmp/foo"

    @dataclass
    class FakeResourceContent:
        type: str = "resource"
        resource: FakeResource = None

        def __post_init__(self):
            if self.resource is None:
                self.resource = FakeResource()

    result = FakeCallToolResult(content=[FakeResourceContent()])
    assert _format_result(result) == "[Resource: file:///tmp/foo]"


def test_format_unknown_content_type() -> None:
    """Unknown content types produce a fallback label."""
    from dataclasses import dataclass

    @dataclass
    class FakeUnknown:
        type: str = "video"

    result = FakeCallToolResult(content=[FakeUnknown()])
    assert _format_result(result) == "[video]"


def test_register_skips_invalid_schema() -> None:
    """A tool whose inputSchema raises during conversion is skipped."""

    class BadSchema:
        def model_dump(self, **_):
            raise ValueError("broken schema")

    tool = FakeTool("bad_schema_tool")
    tool.inputSchema = BadSchema()

    session = _make_session([tool])
    session.config.approval_required = ["none"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    count = bridge.register_tools()

    assert count == 0
    assert registry.list_tools() == []


def test_update_tools_deregisters_and_reregisters() -> None:
    """update_tools() removes old tools and registers the current list."""
    tools = [FakeTool("tool_a"), FakeTool("tool_b")]
    session = _make_session(tools)
    session.config.approval_required = ["none"]
    registry = _make_registry()
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=True)
    bridge.register_tools()
    assert len(registry.list_tools()) == 2

    # Simulate tool list change: only tool_a remains
    session.tools = [FakeTool("tool_a")]
    count = bridge.update_tools()

    assert count == 1
    assert "testserver__tool_a" in registry.list_tools()
    assert "testserver__tool_b" not in registry.list_tools()


def test_convert_pydantic_schema() -> None:
    """Pydantic model inputSchema is converted via model_dump."""
    from unittest.mock import MagicMock

    mock_model = MagicMock()
    mock_model.model_dump.return_value = {"type": "object", "properties": {"x": {"type": "string"}}}

    schema = _convert_schema(mock_model)
    assert schema["properties"]["x"]["type"] == "string"


def test_convert_unknown_type_falls_back() -> None:
    """Non-dict, non-pydantic schema returns a minimal schema."""
    schema = _convert_schema(42)
    assert schema == {"type": "object", "properties": {}}


def test_convert_schema_adds_missing_properties() -> None:
    """Schema with type but no properties gets properties injected."""
    schema = _convert_schema({"type": "object"})
    assert schema["properties"] == {}


def test_collision_no_prefix_blocked() -> None:
    """Without prefix, an MCP tool shadowing a builtin is still blocked."""
    registry = _make_registry()

    async def _builtin() -> str:
        return "builtin"

    registry.register(name="shared_tool", description="Existing tool", handler=_builtin)

    tool = FakeTool("shared_tool")
    session = _make_session([tool])
    session.config.approval_required = ["none"]
    audit = _make_audit()

    bridge = MCPToolBridge(session, registry, audit, prefix=False)
    count = bridge.register_tools()

    assert count == 0
    assert registry.list_tools() == ["shared_tool"]

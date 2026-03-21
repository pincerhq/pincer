"""Tests for PincerToolExporter — tool registration, approval logic, ask_user."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pincer.mcp.exporter import PincerToolExporter

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_registry(tool_names: list[str] | None = None):
    from pincer.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for name in tool_names or []:

        def _make_handler(n: str):
            async def _handler(**_kwargs):
                return f"result:{n}"

            _handler.__name__ = n
            return _handler

        reg.register(name=name, description=f"Tool {name}", handler=_make_handler(name))
    return reg


def _make_fmcp():
    """Return a mock FastMCP that records add_tool calls."""
    fmcp = MagicMock()
    fmcp._registered: list[tuple] = []

    def _add_tool(fn, name=None, **kwargs):
        fmcp._registered.append((name or fn.__name__, fn))

    fmcp.add_tool.side_effect = _add_tool
    return fmcp


# ── export_to ─────────────────────────────────────────────────────────────────


def test_export_creates_mcp_tools():
    reg = _make_registry(["web_search", "email_check"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["web_search", "email_check"])
    count = exporter.export_to(fmcp)
    assert count == 2
    registered_names = [name for name, _ in fmcp._registered]
    assert "pincer_web_search" in registered_names
    assert "pincer_email_check" in registered_names


def test_export_count_matches_whitelist():
    reg = _make_registry(["web_search", "email_check", "calendar_today"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["web_search"])
    count = exporter.export_to(fmcp)
    assert count == 1


def test_export_skips_nonexistent_tool():
    reg = _make_registry(["web_search"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["web_search", "nonexistent_tool"])
    count = exporter.export_to(fmcp)
    # Only web_search registered; nonexistent skipped
    assert count == 1


def test_ask_user_tool_always_registered():
    reg = _make_registry([])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=[])
    exporter.export_to(fmcp)
    registered_names = [name for name, _ in fmcp._registered]
    assert "pincer_ask_user" in registered_names


def test_registered_names_includes_ask_user():
    reg = _make_registry(["web_search"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["web_search"])
    exporter.export_to(fmcp)
    assert "pincer_ask_user" in exporter.registered_names
    assert "pincer_web_search" in exporter.registered_names


def test_tool_gets_pincer_prefix():
    reg = _make_registry(["web_search"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["web_search"])
    exporter.export_to(fmcp)
    names = [name for name, _ in fmcp._registered]
    assert "pincer_web_search" in names
    assert "web_search" not in names


def test_already_prefixed_tool_not_double_prefixed():
    """If tool is already named 'pincer_foo', it's not exported as 'pincer_pincer_foo'."""
    reg = _make_registry(["pincer_web_search"])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=["pincer_web_search"])
    exporter.export_to(fmcp)
    names = [name for name, _ in fmcp._registered]
    assert "pincer_web_search" in names
    assert "pincer_pincer_web_search" not in names


# ── Wrapped handler execution ─────────────────────────────────────────────────


async def test_exported_tool_calls_pincer_handler():
    """The registered handler delegates to the Pincer tool registry."""
    reg = _make_registry(["web_search"])
    fmcp = _make_fmcp()

    exporter = PincerToolExporter(reg, expose_tools=["web_search"])
    exporter.export_to(fmcp)

    # Find the handler for pincer_web_search
    handler = next(fn for name, fn in fmcp._registered if name == "pincer_web_search")
    result = await handler(query="MCP security")
    assert "result:web_search" in result


async def test_exported_tool_with_approval_prompts():
    """Approval-required tools call the approval callback before executing."""
    reg = _make_registry(["email_send"])
    fmcp = _make_fmcp()
    approval_cb = AsyncMock(return_value=True)

    exporter = PincerToolExporter(
        reg,
        expose_tools=["email_send"],
        approval_callback=approval_cb,
    )
    exporter.export_to(fmcp)
    handler = next(fn for name, fn in fmcp._registered if name == "pincer_email_send")
    await handler(to="a@b.com", subject="Hi", body="Hello")

    approval_cb.assert_called_once()
    call_args = approval_cb.call_args
    assert call_args[0][0] == "email_send"  # tool name


async def test_exported_tool_approval_denied():
    """When approval is denied the handler returns a [Denied] message."""
    reg = _make_registry(["email_send"])
    fmcp = _make_fmcp()
    approval_cb = AsyncMock(return_value=False)

    exporter = PincerToolExporter(
        reg,
        expose_tools=["email_send"],
        approval_callback=approval_cb,
    )
    exporter.export_to(fmcp)
    handler = next(fn for name, fn in fmcp._registered if name == "pincer_email_send")
    result = await handler(to="a@b.com", subject="Hi", body="Hello")

    assert "[Denied]" in result


async def test_approval_not_called_for_non_approval_tools():
    """Read-only tools skip the approval callback entirely."""
    reg = _make_registry(["web_search"])
    fmcp = _make_fmcp()
    approval_cb = AsyncMock(return_value=True)

    exporter = PincerToolExporter(
        reg,
        expose_tools=["web_search"],
        approval_callback=approval_cb,
    )
    exporter.export_to(fmcp)
    handler = next(fn for name, fn in fmcp._registered if name == "pincer_web_search")
    await handler(query="hello")

    approval_cb.assert_not_called()


# ── pincer_ask_user ────────────────────────────────────────────────────────────


async def test_ask_user_proxies_to_callback():
    reg = _make_registry([])
    fmcp = _make_fmcp()
    ask_cb = AsyncMock(return_value="User says yes")

    exporter = PincerToolExporter(reg, expose_tools=[], ask_user_callback=ask_cb)
    exporter.export_to(fmcp)

    handler = next(fn for name, fn in fmcp._registered if name == "pincer_ask_user")
    result = await handler(question="Should I proceed?")

    assert "User says yes" in result
    ask_cb.assert_called_once_with("Should I proceed?")


async def test_ask_user_no_active_channel():
    """When no callback is configured, ask_user returns the unreachable message."""
    reg = _make_registry([])
    fmcp = _make_fmcp()
    exporter = PincerToolExporter(reg, expose_tools=[], ask_user_callback=None)
    exporter.export_to(fmcp)

    handler = next(fn for name, fn in fmcp._registered if name == "pincer_ask_user")
    result = await handler(question="Hello?")
    assert "unreachable" in result


async def test_ask_user_callback_exception_returns_error():
    reg = _make_registry([])
    fmcp = _make_fmcp()
    ask_cb = AsyncMock(side_effect=RuntimeError("channel down"))

    exporter = PincerToolExporter(reg, expose_tools=[], ask_user_callback=ask_cb)
    exporter.export_to(fmcp)

    handler = next(fn for name, fn in fmcp._registered if name == "pincer_ask_user")
    result = await handler(question="Hello?")
    assert "[Error" in result

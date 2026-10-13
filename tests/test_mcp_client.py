"""Tests for MCPClientSession — connection, tool discovery, timeout, health check."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp_fakes import FakeClientSession, FakeTransport

from pincer.mcp.client import MCPClientSession, _root_causes
from pincer.mcp.config import MCPServerConfig, MCPTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


def _make_stdio_config(sandbox: bool = True) -> MCPServerConfig:
    return MCPServerConfig(
        name="testserver",
        transport=MCPTransport.STDIO,
        command="echo",
        args=["hello"],
        sandbox=sandbox,
        timeout=5,
        max_retries=1,
    )


def _make_http_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="httpserver",
        transport=MCPTransport.STREAMABLE_HTTP,
        url="https://example.com/mcp",
        timeout=5,
        max_retries=1,
    )


# ── Basic properties ──────────────────────────────────────────────────────────


def test_initial_state() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    assert session.name == "testserver"
    assert session.connected is False
    assert session.tools == []


def test_name_matches_config() -> None:
    cfg = _make_http_config()
    session = MCPClientSession(cfg)
    assert session.name == "httpserver"


# ── Connection error handling ─────────────────────────────────────────────────


async def test_connect_failure_raises_connection_error() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    exc_ctx = pytest.raises(ConnectionError, match="connection failed")
    patch_ctx = patch("pincer.mcp.client.MCPClientSession._connect_stdio", side_effect=OSError("not found"))
    with patch_ctx, exc_ctx:
        await session.connect()

    assert session.connected is False
    assert session._connect_attempts == 1


async def test_connect_increments_attempts_on_failure() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    with patch("pincer.mcp.client.MCPClientSession._connect_stdio", side_effect=OSError("fail")):
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await session.connect()

    assert session._connect_attempts == 3


def _patch_sdk(transport: FakeTransport, session_cls: type = FakeClientSession) -> contextlib.ExitStack:
    async def _fake_connect_http(self: MCPClientSession, stack: contextlib.AsyncExitStack, _client: object) -> object:
        return await transport.connect_http(stack)

    patches = contextlib.ExitStack()
    patches.enter_context(patch("mcp.ClientSession", session_cls))
    patches.enter_context(patch("pincer.mcp.client.MCPClientSession._connect_http", _fake_connect_http))
    return patches


async def test_connect_transport_failure_raises_connection_error_and_unwinds() -> None:
    """A transport that fails inside its own task group must surface as ConnectionError,
    not as a cancellation leaking into the caller."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(fail_on_open=True)

    with _patch_sdk(transport), pytest.raises(ConnectionError, match="OSError: Name or service not known"):
        await session.connect()

    assert session.connected is False
    assert session._connect_attempts == 1
    assert session._owner is None
    assert session._session is None
    assert transport.closed is True
    # The transport's self-cancellation must not stick to the calling task.
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0
    await anyio.sleep(0)


async def test_connect_cancelled_from_outside_closes_transport() -> None:
    """Cancelling connect() itself propagates CancelledError after the transport is closed."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(hang=True)

    with _patch_sdk(transport):
        task = asyncio.create_task(session.connect())
        while session._session is None:  # until connect() reaches the handshake
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert transport.closed is True
    assert session.connected is False
    assert session._owner is None
    assert session._session is None
    assert session._connect_attempts == 0


async def test_connect_reports_handshake_error_over_teardown_error() -> None:
    """An error raised while closing the transport must not replace the reason the connect failed."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_error=RuntimeError("teardown blew up"))

    class _RejectingSession(FakeClientSession):
        handshake_error = PermissionError("401 Unauthorized")

    with _patch_sdk(transport, _RejectingSession), pytest.raises(ConnectionError) as exc_info:
        await session.connect()

    message = str(exc_info.value)
    assert "PermissionError: 401 Unauthorized" in message
    assert message.index("401 Unauthorized") < message.index("teardown blew up")
    assert isinstance(exc_info.value.__cause__, PermissionError)


async def test_disconnect_from_another_task_closes_transport_in_owner_task() -> None:
    """disconnect() is called from other tasks (manager.stop, reconnect); the contexts
    must still be exited by the task that entered them."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport()

    with _patch_sdk(transport):
        await asyncio.create_task(session.connect())
    owner = session._owner
    assert session.connected is True
    assert owner is not None

    await asyncio.create_task(session.disconnect())

    assert transport.closed is True
    assert transport.closed_in is owner
    assert owner.done() and owner.exception() is None
    assert session.connected is False


async def test_transport_failure_after_connect_marks_session_disconnected() -> None:
    """A transport that dies later must not cancel the task that called connect();
    the session just reports itself as disconnected so the health loop reconnects it."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport()

    with _patch_sdk(transport):
        await session.connect()
    assert await session.health_check() is True

    transport.fail()
    assert session._owner is not None
    await asyncio.wait({session._owner})

    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0
    assert transport.closed is True
    assert session.connected is False
    assert await session.health_check() is False
    await session.disconnect()


def test_root_causes_includes_cause_of_uninformative_wrapper() -> None:
    try:
        try:
            raise OSError(111, "Connection refused")
        except OSError as inner:
            raise RuntimeError("All connection attempts failed") from inner
    except RuntimeError as wrapped:
        group = ExceptionGroup("unhandled errors in a TaskGroup", [wrapped])

    text = _root_causes(group)

    assert text.startswith("RuntimeError: All connection attempts failed (caused by ")
    assert "Connection refused" in text


@pytest.mark.parametrize(
    ("outer_text", "inner_text", "expected"),
    [
        ("boom", "boom", "RuntimeError: boom"),
        ("outer", "inner", "RuntimeError: outer (caused by RuntimeError: inner)"),
    ],
)
def test_root_causes_terminates_on_cyclic_chain(outer_text: str, inner_text: str, expected: str) -> None:
    outer, inner = RuntimeError(outer_text), RuntimeError(inner_text)
    outer.__cause__ = inner
    inner.__cause__ = outer

    assert _root_causes(outer) == expected


def test_root_causes_shows_cause_shared_by_group_members() -> None:
    shared = OSError("Connection refused")
    first, second = RuntimeError("first"), RuntimeError("second")
    first.__cause__ = second.__cause__ = shared

    text = _root_causes(ExceptionGroup("unhandled errors in a TaskGroup", [first, second]))

    assert text.count("Connection refused") == 2


async def test_connect_fails_when_describing_the_error_raises() -> None:
    """connect() must get an answer even if building the failure message blows up."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(fail_on_open=True)

    describe = patch("pincer.mcp.client._leaf_causes", side_effect=RuntimeError("cannot describe"))
    with _patch_sdk(transport), describe, pytest.raises(ConnectionError, match="connection failed"):
        async with asyncio.timeout(2):
            await session.connect()

    assert session.connected is False
    assert session._owner is None
    assert transport.closed is True


async def test_connect_and_disconnect_started_together_both_finish() -> None:
    """disconnect() can cancel the owner before it has run a single step; connect()
    must not be left waiting for it."""
    session = MCPClientSession(_make_http_config())

    with _patch_sdk(FakeTransport()):
        async with asyncio.timeout(2):
            results = await asyncio.gather(session.connect(), session.disconnect(), return_exceptions=True)

    assert isinstance(results[0], ConnectionError)
    assert results[1] is None
    assert session.connected is False
    assert session._owner is None


async def test_disconnect_during_handshake_is_not_a_failed_attempt() -> None:
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(hang=True)

    with _patch_sdk(transport):
        task = asyncio.create_task(session.connect())
        while session._session is None:  # until connect() reaches the handshake
            await asyncio.sleep(0)
        await session.disconnect()
        with pytest.raises(ConnectionError, match="cancelled by disconnect"):
            await task

    assert transport.closed is True
    assert session.connected is False
    assert session._owner is None
    assert session._connect_attempts == 0


async def test_disconnect_cancelled_natively_still_closes_and_resets() -> None:
    """A Task.cancel() on the caller mid-teardown cuts the close short, but must not
    leave the session looking connected with its transport half closed."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=0.05)

    with _patch_sdk(transport):
        await session.connect()
    task = asyncio.create_task(session.disconnect())
    await asyncio.sleep(0.01)  # teardown is under way
    assert transport.closed is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.closed is True
    assert transport.close_interrupted is True
    assert session.connected is False
    assert session._owner is None

    replacement = FakeTransport()
    with _patch_sdk(replacement):
        await session.connect()
    assert session.connected is True
    assert replacement.closed is False
    await session.disconnect()
    assert replacement.closed is True


async def test_disconnect_after_failed_ping_closes_transport_cleanly() -> None:
    """A failed health check leaves the owner waiting on `stop`; disconnect() must
    ask it to stop rather than cancel it."""
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport()

    with _patch_sdk(transport):
        await session.connect()
    owner = session._owner
    session._session.send_ping = AsyncMock(side_effect=OSError("ping failed"))
    assert await session.health_check() is False

    await session.disconnect()

    assert transport.closed is True
    assert transport.closed_in is owner
    assert transport.exit_error is None


async def test_overlapping_disconnects_both_wait_for_the_transport_to_close() -> None:
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=0.05)
    closed_on_return: list[bool] = []

    async def _disconnect() -> None:
        await session.disconnect()
        closed_on_return.append(transport.closed)

    with _patch_sdk(transport):
        await session.connect()
    await asyncio.gather(_disconnect(), _disconnect())

    assert closed_on_return == [True, True]
    assert session._owner is None


@pytest.mark.parametrize("failed_ping", [False, True], ids=["healthy", "after-failed-ping"])
async def test_disconnect_cancels_owner_after_grace_period(monkeypatch: pytest.MonkeyPatch, failed_ping: bool) -> None:
    """A transport that hangs while closing must not hold disconnect() for as long
    as it likes: after the grace period the owner is cancelled."""
    monkeypatch.setattr("pincer.mcp.client._STOP_GRACE_SECONDS", 0.05)
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=3)

    with _patch_sdk(transport):
        await session.connect()
    owner = session._owner
    if failed_ping:
        session._session.send_ping = AsyncMock(side_effect=OSError("ping failed"))
        assert await session.health_check() is False

    started = time.monotonic()
    await session.disconnect()

    assert time.monotonic() - started < 1
    assert transport.close_interrupted is True
    assert transport.closed_in is owner
    assert session.connected is False
    assert session._owner is None


async def test_disconnect_under_timeout_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller's own deadline wins over the grace period."""
    monkeypatch.setattr("pincer.mcp.client._STOP_GRACE_SECONDS", 2.0)
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=3)

    with _patch_sdk(transport):
        await session.connect()
    owner = session._owner

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await session.disconnect()

    assert time.monotonic() - started < 1
    assert transport.close_interrupted is True
    assert transport.closed_in is owner
    assert session.connected is False
    assert session._owner is None

    replacement = FakeTransport()
    with _patch_sdk(replacement):
        await session.connect()
    assert session.connected is True
    assert session._owner is not owner
    await session.disconnect()
    assert replacement.closed is True
    assert replacement.close_interrupted is False


async def test_disconnect_in_cancelled_anyio_scope_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """manager.stop() cancels the health loop's task group; a disconnect running
    inside it must end with the scope rather than wait out the close."""
    monkeypatch.setattr("pincer.mcp.client._STOP_GRACE_SECONDS", 2.0)
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=3)

    with _patch_sdk(transport):
        await session.connect()
    owner = session._owner

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(session.disconnect)
        await anyio.sleep(0.05)  # teardown is under way
        assert transport.closed is False
        tg.cancel_scope.cancel()

    assert time.monotonic() - started < 1
    assert transport.close_interrupted is True
    assert transport.closed_in is owner
    assert session.connected is False
    assert session._owner is None


@contextlib.asynccontextmanager
async def _cancelled_anyio_scope() -> AsyncIterator[None]:
    with anyio.CancelScope() as scope:
        scope.cancel()
        yield


@contextlib.asynccontextmanager
async def _expired_asyncio_timeout() -> AsyncIterator[None]:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            yield


@pytest.mark.parametrize(
    "already_cancelled",
    [_cancelled_anyio_scope, _expired_asyncio_timeout],
    ids=["anyio-scope", "asyncio-timeout"],
)
async def test_disconnect_by_already_cancelled_caller_is_still_bounded(
    monkeypatch: pytest.MonkeyPatch,
    already_cancelled: Callable[[], contextlib.AbstractAsyncContextManager[None]],
) -> None:
    """The caller's cancel reaches the owner before the transport starts closing and
    is absorbed there, so the slow close needs a second cancel to be cut short."""
    monkeypatch.setattr("pincer.mcp.client._STOP_GRACE_SECONDS", 0.05)
    session = MCPClientSession(_make_http_config())
    transport = FakeTransport(close_delay=3)

    with _patch_sdk(transport):
        await session.connect()
    owner = session._owner

    started = time.monotonic()
    async with already_cancelled():
        await session.disconnect()

    assert time.monotonic() - started < 1
    assert transport.close_interrupted is True
    assert transport.closed_in is owner
    assert session.connected is False
    assert session._owner is None


# ── tool call ────────────────────────────────────────────────────────────────


async def test_call_tool_raises_when_not_connected() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    with pytest.raises(ConnectionError, match="not connected"):
        await session.call_tool("my_tool", {})


async def test_call_tool_forwards_identity_as_meta() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    session._session = MagicMock()
    session._session.call_tool = AsyncMock(return_value="result")

    await session.call_tool("my_tool", {"x": 1}, identity="usr_abc123")

    session._session.call_tool.assert_awaited_once_with("my_tool", {"x": 1}, meta={"identity": "usr_abc123"})


async def test_call_tool_omits_meta_when_no_identity() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    session._session = MagicMock()
    session._session.call_tool = AsyncMock(return_value="result")

    await session.call_tool("my_tool", {"x": 1})

    session._session.call_tool.assert_awaited_once_with("my_tool", {"x": 1}, meta=None)


async def test_call_tool_raises_timeout() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    session._session = MagicMock()

    async def _slow_call(*args, **kwargs):
        await asyncio.sleep(10)

    session._session.call_tool = _slow_call
    session.config = MCPServerConfig(
        name="testserver",
        transport=MCPTransport.STDIO,
        command="echo",
        timeout=1,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await session.call_tool("slow_tool", {})


# ── notification handler ──────────────────────────────────────────────────────


async def test_handle_notification_does_nothing_without_handler() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    # Should not raise even with no handler registered.
    await session._handle_notification(MagicMock())


async def test_handle_notification_forwards_to_registered_handler() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    handler = AsyncMock()
    session.set_notification_handler(handler)

    params = MagicMock()
    await session._handle_notification(params)

    handler.assert_awaited_once_with(params)


async def test_set_notification_handler_none_clears_it() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)

    handler = AsyncMock()
    session.set_notification_handler(handler)
    session.set_notification_handler(None)

    await session._handle_notification(MagicMock())
    handler.assert_not_awaited()


async def test_handle_notification_swallows_handler_exception() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session.set_notification_handler(AsyncMock(side_effect=RuntimeError("boom")))

    # Must not raise — a broken handler shouldn't take down the MCP session.
    await session._handle_notification(MagicMock())


async def test_connect_passes_handle_notification_as_logging_callback() -> None:
    """connect() must wire session._handle_notification as ClientSession's logging_callback."""
    cfg = _make_stdio_config(sandbox=False)
    session = MCPClientSession(cfg)

    captured_kwargs: dict[str, object] = {}

    class _FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

        async def __aenter__(self) -> _FakeClientSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> MagicMock:
            return MagicMock(instructions=None)

        async def list_tools(self) -> MagicMock:
            return MagicMock(tools=[])

    async def _fake_connect_stdio(*args: object, **kwargs: object) -> tuple[MagicMock, MagicMock]:
        return MagicMock(), MagicMock()

    with (
        patch("mcp.ClientSession", _FakeClientSession),
        patch("pincer.mcp.client.MCPClientSession._connect_stdio", _fake_connect_stdio),
    ):
        await session.connect()

    assert captured_kwargs.get("logging_callback") == session._handle_notification


# ── instructions ──────────────────────────────────────────────────────────────


async def test_connect_captures_server_instructions() -> None:
    """connect() must capture InitializeResult.instructions from the handshake."""
    cfg = _make_stdio_config(sandbox=False)
    session = MCPClientSession(cfg)
    assert session.instructions is None

    class _FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClientSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> MagicMock:
            return MagicMock(instructions="Use ocr_document_text for a quick plain-text read.")

        async def list_tools(self) -> MagicMock:
            return MagicMock(tools=[])

    async def _fake_connect_stdio(*args: object, **kwargs: object) -> tuple[MagicMock, MagicMock]:
        return MagicMock(), MagicMock()

    with (
        patch("mcp.ClientSession", _FakeClientSession),
        patch("pincer.mcp.client.MCPClientSession._connect_stdio", _fake_connect_stdio),
    ):
        await session.connect()

    assert session.instructions == "Use ocr_document_text for a quick plain-text read."


async def test_connect_captures_none_instructions() -> None:
    """A server that reports no instructions leaves .instructions as None, not an error."""
    cfg = _make_stdio_config(sandbox=False)
    session = MCPClientSession(cfg)

    class _FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClientSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> MagicMock:
            return MagicMock(instructions=None)

        async def list_tools(self) -> MagicMock:
            return MagicMock(tools=[])

    async def _fake_connect_stdio(*args: object, **kwargs: object) -> tuple[MagicMock, MagicMock]:
        return MagicMock(), MagicMock()

    with (
        patch("mcp.ClientSession", _FakeClientSession),
        patch("pincer.mcp.client.MCPClientSession._connect_stdio", _fake_connect_stdio),
    ):
        await session.connect()

    assert session.instructions is None


async def test_cleanup_resets_instructions() -> None:
    """disconnect() must clear instructions so a stale value can't leak into a reconnect."""
    cfg = _make_stdio_config(sandbox=False)
    session = MCPClientSession(cfg)
    session._instructions = "stale instructions from a previous connection"

    await session._cleanup()

    assert session.instructions is None


# ── Health check ──────────────────────────────────────────────────────────────


async def test_health_check_returns_false_when_disconnected() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    assert await session.health_check() is False


async def test_health_check_returns_true_on_successful_ping() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    mock_mcp_session = MagicMock()
    mock_mcp_session.send_ping = AsyncMock(return_value=None)
    session._session = mock_mcp_session

    result = await session.health_check()
    assert result is True


async def test_health_check_returns_false_on_ping_failure() -> None:
    cfg = _make_stdio_config()
    session = MCPClientSession(cfg)
    session._connected = True
    mock_mcp_session = MagicMock()
    mock_mcp_session.send_ping = AsyncMock(side_effect=Exception("connection lost"))
    session._session = mock_mcp_session

    result = await session.health_check()
    assert result is False
    assert session._connected is False


# ── Sandbox env ───────────────────────────────────────────────────────────────


def test_sandbox_env_excludes_pincer_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("MY_TOKEN", "ghp_123")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        env={"MY_TOKEN": "ghp_123"},
        sandbox=True,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    assert env is not None
    assert "PINCER_ANTHROPIC_API_KEY" not in env
    assert env.get("MY_TOKEN") == "ghp_123"


def test_unsandboxed_no_extra_env_inherits_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox=False + no declared env → full inherited env dict (including SOME_VAR)."""
    monkeypatch.setenv("SOME_VAR", "some_value")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    assert env is not None
    assert env.get("SOME_VAR") == "some_value"


def test_unsandboxed_with_declared_env_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox=False + declared env → merge declared vars on top of current env."""
    monkeypatch.setenv("EXISTING_VAR", "existing")

    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        env={"MY_TOKEN": "tok123"},
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()

    assert env is not None
    assert env.get("MY_TOKEN") == "tok123"
    assert env.get("EXISTING_VAR") == "existing"


def test_unsandboxed_no_extra_env_returns_full_env() -> None:
    cfg = MCPServerConfig(
        name="test",
        transport=MCPTransport.STDIO,
        command="echo",
        sandbox=False,
    )
    session = MCPClientSession(cfg)
    env = session._build_sandbox_env()
    # No extra env declared, sandbox=False → returns full inherited env dict
    assert env is not None
    assert isinstance(env, dict)

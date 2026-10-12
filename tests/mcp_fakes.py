"""Fakes for the MCP SDK's transport and session, shared by the MCP client tests."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class FakeTransport:
    """Mimics the SDK's streamable-HTTP transport: I/O runs in its own task group,
    and a failed request there cancels whoever entered the transport.

    `fail_on_open` fails the first request; `fail()` fails a later one;
    `hang` leaves the handshake unanswered; `close_error` is raised while the
    transport closes; `close_delay` makes closing take that many seconds, and
    `close_interrupted` records that a cancellation cut that wait short.
    """

    def __init__(
        self,
        *,
        fail_on_open: bool = False,
        hang: bool = False,
        close_error: Exception | None = None,
        close_delay: float = 0,
    ) -> None:
        self.hang = hang or fail_on_open
        self.closed = False
        self.closed_in: Any = None  # the task that ran the transport's teardown
        self.close_interrupted = False
        self.exit_error: BaseException | None = None  # what was thrown into the transport; None on a clean exit
        self._close_delay = close_delay
        self._broken = anyio.Event()
        self._fail_on_open = fail_on_open
        self._close_error = close_error

    def fail(self) -> None:
        self._broken.set()

    async def _io(self) -> None:
        if not self._fail_on_open:
            await self._broken.wait()
        raise OSError("Name or service not known")

    @contextlib.asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[FakeTransport, MagicMock, None]]:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._io)
                try:
                    yield self, MagicMock(), None
                except BaseException as e:
                    self.exit_error = e
                    raise
                tg.cancel_scope.cancel()
        finally:
            try:
                if self._close_delay:
                    await asyncio.sleep(self._close_delay)
            except asyncio.CancelledError:
                self.close_interrupted = True
                raise
            finally:
                self.closed = True
                self.closed_in = asyncio.current_task()
            if self._close_error is not None:
                raise self._close_error

    async def connect_http(self, stack: contextlib.AsyncExitStack) -> tuple[FakeTransport, MagicMock]:
        """Body for a patched `MCPClientSession._connect_http`."""
        read_stream, write_stream, _ = await stack.enter_async_context(self.open())
        return read_stream, write_stream


class FakeClientSession:
    """ClientSession stand-in. The handshake completes unless the transport it was
    given never answers (`FakeTransport.hang`)."""

    handshake_error: Exception | None = None

    def __init__(self, read_stream: FakeTransport, write_stream: object, **kwargs: object) -> None:
        self._transport = read_stream

    async def __aenter__(self) -> FakeClientSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def initialize(self) -> MagicMock:
        if self._transport.hang:
            await anyio.sleep_forever()
        await anyio.sleep(0.01)  # siblings are still mid-handshake when one fails
        if self.handshake_error is not None:
            raise self.handshake_error
        return MagicMock(instructions=None)

    async def list_tools(self) -> MagicMock:
        return MagicMock(tools=[])

    async def send_ping(self) -> None:
        return None

"""
MCPClientSession — manages a single MCP server connection.

Wraps the official mcp.ClientSession with:
- Transport setup (stdio / streamable-http)
- Session initialization and capability negotiation
- Tool discovery and caching
- Timeout enforcement per tool call
- Graceful shutdown and reconnection tracking
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import AsyncExitStack
from typing import Any

import anyio

from pincer.mcp.config import MCPServerConfig, MCPTransport
from pincer.mcp.sandbox import MCPSandbox, sandbox_streams

logger = logging.getLogger("pincer.mcp")


class MCPClientSession:
    """Manages a single MCP server connection with lifecycle control."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._session: Any = None  # mcp.ClientSession
        self._exit_stack: AsyncExitStack | None = None
        self._tools: list[Any] = []  # list[mcp.types.Tool]
        self._connected = False
        self._connect_attempts = 0
        self._tmpdir: str | None = None
        self._sandbox: MCPSandbox | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    @property
    def tools(self) -> list[Any]:
        return self._tools

    async def connect(self) -> None:
        """Establish connection and initialize MCP session."""
        if self._connected:
            return

        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:
            raise RuntimeError("MCP package not installed. Run: uv pip install 'pincer-agent[mcp]'") from e

        self._exit_stack = AsyncExitStack()
        try:
            if self.config.transport == MCPTransport.STDIO:
                if self.config.sandbox:
                    # sandbox_streams and ClientSession create persistent task groups —
                    # enter them outside any fail_after scope to avoid cancel scope ordering errors.
                    read_stream, write_stream = await self._connect_stdio_sandboxed(self._exit_stack)
                    # Readiness check is a pure coroutine (sleep + poll), safe to timeout.
                    with anyio.fail_after(self.config.startup_timeout):
                        await self._wait_for_sandbox_startup()
                else:
                    read_stream, write_stream = await self._connect_stdio(
                        self._exit_stack, StdioServerParameters, stdio_client
                    )
            else:
                read_stream, write_stream = await self._connect_http(self._exit_stack, streamablehttp_client)

            # Enter ClientSession outside fail_after — it spawns background tasks whose
            # cancel scope must outlive our timeout scope.
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            # Timeout only the pure protocol operations.
            with anyio.fail_after(self.config.timeout):
                await self._session.initialize()
                await self._discover_tools()

            self._connected = True
            self._connect_attempts = 0
            logger.info(
                "MCP '%s' connected — %d tools available",
                self.name,
                len(self._tools),
            )
        except Exception as e:
            self._connect_attempts += 1
            await self._cleanup()
            raise ConnectionError(f"MCP '{self.name}' connection failed (attempt {self._connect_attempts}): {e}") from e

    async def disconnect(self) -> None:
        """Gracefully close the MCP session."""
        await self._cleanup()
        logger.info("MCP '%s' disconnected", self.name)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Execute a tool call with timeout enforcement.

        Returns mcp.types.CallToolResult.
        Raises ConnectionError if not connected, TimeoutError if timeout exceeded.
        """
        if not self.connected:
            raise ConnectionError(f"MCP '{self.name}' is not connected")

        try:
            with anyio.fail_after(self.config.timeout):
                result = await self._session.call_tool(tool_name, arguments)
            return result
        except TimeoutError:
            raise TimeoutError(
                f"MCP '{self.name}' tool '{tool_name}' timed out after {self.config.timeout}s"
            ) from None

    async def health_check(self) -> bool:
        """Verify the session is still alive via a ping."""
        if not self.connected:
            return False
        try:
            with anyio.fail_after(5.0):
                await self._session.send_ping()
            return True
        except Exception:
            self._connected = False
            return False

    async def refresh_tools(self) -> None:
        """Re-discover tools (called on tools/list_changed notification)."""
        if not self.connected:
            return
        old_count = len(self._tools)
        await self._discover_tools()
        logger.info(
            "MCP '%s' tools refreshed: %d → %d",
            self.name,
            old_count,
            len(self._tools),
        )

    # ── Private helpers ───────────────────────────────────

    async def _wait_for_sandbox_startup(self) -> None:
        """Confirm sandbox process is alive before attempting MCP connection.

        Returns as soon as the process is confirmed running — does not wait
        the full startup_timeout. Raises RuntimeError if the process exited.
        """
        if self._sandbox is None:
            return
        # Brief delay so the process has time to either crash or stabilise.
        await anyio.sleep(0.2)
        if not self._sandbox.alive:
            stderr = self._sandbox.get_stderr()
            raise RuntimeError(
                f"MCP '{self.name}' server exited on startup: {stderr or '(no output)'}"
            )

    async def _connect_stdio(self, stack: AsyncExitStack, stdio_params_cls: Any, stdio_client: Any) -> tuple[Any, Any]:
        """Set up stdio transport.

        When sandbox=True: launches subprocess via MCPSandbox (resource limits,
        process-group kill) and bridges pipes to anyio message streams.
        When sandbox=False: uses the MCP SDK's stdio_client directly with the
        declared env vars merged on top of the current environment.
        """
        if self.config.sandbox:
            return await self._connect_stdio_sandboxed(stack)

        # Non-sandboxed: use stdio_client from MCP SDK with merged env.
        sanitized_env = self._build_sandbox_env()

        import tempfile as _tmpfile

        self._tmpdir = _tmpfile.mkdtemp(prefix=f"pincer_mcp_{self.name}_")

        server_params = stdio_params_cls(
            command=self.config.command,
            args=list(self.config.args),
            env=sanitized_env,
            cwd=self._tmpdir,
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
        return read_stream, write_stream

    async def _connect_stdio_sandboxed(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Launch the subprocess inside an MCPSandbox and bridge its pipes."""
        sb = MCPSandbox(
            command=self.config.command,  # type: ignore[arg-type]
            args=list(self.config.args),
            env=dict(self.config.env),
            memory_limit_mb=self.config.sandbox_memory_mb,
            server_name=self.name,
        )
        sb.start()
        self._sandbox = sb

        # Register sandbox cleanup: stop the subprocess when the stack closes.
        async def _stop_sandbox() -> None:
            stderr_output = sb.get_stderr()
            if stderr_output:
                logger.debug("MCPSandbox '%s' stderr: %s", self.name, stderr_output[:500])
            sb.stop()
            self._sandbox = None

        stack.push_async_callback(_stop_sandbox)

        read_stream, write_stream = await stack.enter_async_context(sandbox_streams(sb))
        return read_stream, write_stream

    async def _connect_http(self, stack: AsyncExitStack, streamablehttp_client: Any) -> tuple[Any, Any]:
        """Set up streamable-HTTP transport, with optional OAuth 2.1 auth."""
        headers: dict[str, str] = dict(self.config.headers) if self.config.headers else {}

        if self.config.oauth_enabled:
            try:
                from pincer.mcp.auth.client_flow import MCPOAuthClient
                from pincer.mcp.auth.token_store import TokenStore

                client_config: dict[str, Any] = {}
                if self.config.oauth_client_id:
                    client_config["client_id"] = self.config.oauth_client_id
                if self.config.oauth_client_secret:
                    client_config["client_secret"] = self.config.oauth_client_secret

                oauth = MCPOAuthClient(
                    mcp_server_url=str(self.config.url),
                    token_store=TokenStore(),
                    client_config=client_config,
                )
                auth_headers = await oauth.get_headers()
                headers.update(auth_headers)
                logger.debug("MCP '%s': obtained OAuth headers", self.name)
            except Exception as e:
                logger.warning("MCP '%s': OAuth failed, connecting without auth: %s", self.name, e)

        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(
                self.config.url,
                headers=headers or None,
                timeout=float(self.config.timeout),
            )
        )
        return read_stream, write_stream

    async def _discover_tools(self) -> None:
        result = await self._session.list_tools()
        self._tools = result.tools

    def _build_sandbox_env(self) -> dict[str, str] | None:
        """Build a sanitized environment for stdio subprocesses.

        When sandbox=True (default): start from a minimal base and only add
        declared env vars, preventing PINCER_* secrets from leaking.
        When sandbox=False: pass user-declared env vars on top of inherited env.
        """
        if not self.config.sandbox:
            if not self.config.env:
                return None
            # Unsandboxed: merge with current env
            import os

            env = dict(os.environ)
            env.update(self.config.env)
            return env

        # Sandboxed: minimal base + declared vars only
        import os

        base_env: dict[str, str] = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",  # nosec B108 — intentional sandbox restriction
            "LANG": "en_US.UTF-8",
            "TMPDIR": "/tmp",  # nosec B108 — intentional sandbox restriction
        }
        # Preserve PYTHONPATH / NODE_PATH / similar runtime paths if present
        for passthrough in ("PYTHONPATH", "NODE_PATH", "npm_config_cache"):
            val = os.environ.get(passthrough)
            if val:
                base_env[passthrough] = val

        base_env.update(self.config.env)
        return base_env

    async def _cleanup(self) -> None:
        self._connected = False
        self._session = None
        if self._exit_stack:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._exit_stack = None
        # Clean up temp dir (non-sandbox path)
        if self._tmpdir:
            with contextlib.suppress(Exception):
                import shutil

                shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        # Ensure sandbox process is stopped if exit stack didn't handle it
        if self._sandbox is not None:
            with contextlib.suppress(Exception):
                self._sandbox.stop()
            self._sandbox = None

"""
MCPSandbox — isolated subprocess launcher for MCP stdio servers.

Provides resource-limited, environment-sanitized, process-group-managed
subprocess execution for MCP stdio-transport servers.

Features:
- Minimal sanitized environment (blocks PINCER_*, AWS_*, ANTHROPIC_*, etc.)
- Memory limit via RLIMIT_AS (Linux; warning-only on macOS)
- Process group management for clean kill (SIGTERM → wait → SIGKILL)
- Isolated tmpdir as cwd, cleaned up on stop
- stderr capture for audit logging
- Async stream bridge (sandbox_streams) for integration with MCP ClientSession
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import IO, Any

logger = logging.getLogger("pincer.mcp")

# Environment variable prefixes blocked from passing to sandboxed subprocesses.
BLOCKED_ENV_PREFIXES: tuple[str, ...] = (
    "PINCER_",
    "AWS_",
    "GOOGLE_",
    "AZURE_",
    "OPENAI_",
    "ANTHROPIC_",
    "DATABASE_",
    "DB_",
    "SECRET_",
    "PRIVATE_",
    "SSH_",
    "GPG_",
)

# Specific full-name keys that are also blocked regardless of prefix.
_BLOCKED_ENV_EXACT: frozenset[str] = frozenset({"GITHUB_TOKEN"})

# How long to wait for SIGTERM before escalating to SIGKILL.
_SIGTERM_WAIT_SECONDS = 5


class MCPSandbox:
    """
    Manages an isolated subprocess for an MCP stdio-transport server.

    Usage::

        sandbox = MCPSandbox(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "ghp_..."},
            memory_limit_mb=0,
            server_name="github",
        )
        stdin_pipe, stdout_pipe = sandbox.start()
        # ... use pipes ...
        exit_code = sandbox.stop()
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str],
        memory_limit_mb: int,
        server_name: str,
    ) -> None:
        self.command = command
        self.args = args
        self.user_env = env
        self.memory_limit_mb = memory_limit_mb
        self.server_name = server_name

        self._process: subprocess.Popen[bytes] | None = None
        self._tmpdir: str | None = None

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> tuple[IO[bytes], IO[bytes]]:
        """
        Launch the subprocess with sandbox constraints.

        Returns (stdin_pipe, stdout_pipe) — raw binary file-like objects
        connected to the subprocess's stdin and stdout.
        Raises RuntimeError if already started.
        """
        if self._process is not None:
            raise RuntimeError(f"MCPSandbox '{self.server_name}' already started")

        self._tmpdir = tempfile.mkdtemp(prefix=f"pincer_mcp_{self.server_name}_")
        safe_env = self._build_safe_env()
        memory_limit_mb = self.memory_limit_mb

        def _preexec() -> None:
            # start_new_session=True already called setsid(); just set resource limits.
            self._apply_resource_limits(memory_limit_mb)

        cmd = [self.command, *self.args]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=safe_env,
            cwd=self._tmpdir,
            start_new_session=True,  # Also sets setsid; preexec_fn reinforces limits
            preexec_fn=_preexec if sys.platform != "win32" else None,
        )

        logger.debug(
            "MCPSandbox '%s' started — pid=%d, memory_limit=%dMB",
            self.server_name,
            self._process.pid,
            self.memory_limit_mb,
        )

        assert self._process.stdin is not None
        assert self._process.stdout is not None
        return self._process.stdin, self._process.stdout

    def stop(self) -> int:
        """
        Gracefully stop the subprocess.

        Sends SIGTERM to the process group, waits up to 5 seconds,
        then sends SIGKILL. Cleans up the tmpdir.
        Returns the process exit code (or -1 if unknown).
        """
        exit_code = -1

        if self._process is not None:
            pid = self._process.pid
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                deadline = time.monotonic() + _SIGTERM_WAIT_SECONDS
                while time.monotonic() < deadline:
                    rc = self._process.poll()
                    if rc is not None:
                        exit_code = rc
                        break
                    time.sleep(0.1)
                else:
                    # Escalate to SIGKILL
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)
                    exit_code = self._process.wait()
            except ProcessLookupError:
                # Process already gone.
                rc = self._process.poll()
                exit_code = rc if rc is not None else 0
            except Exception as e:
                logger.debug("MCPSandbox '%s' stop error: %s", self.server_name, e)
            finally:
                self._process = None

        self._cleanup_tmpdir()
        return exit_code

    @property
    def alive(self) -> bool:
        """True if the subprocess is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        """PID of the subprocess, or None if not running."""
        return self._process.pid if self._process is not None else None

    def get_stderr(self) -> str:
        """
        Non-blocking read of any available stderr output.

        Returns whatever bytes are buffered; empty string if nothing available.
        Useful for capturing error messages for audit logging.
        """
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            if sys.platform == "win32":
                # No select on Windows pipes; best-effort read.
                return self._process.stderr.read(4096).decode(errors="replace")
            readable, _, _ = select.select([self._process.stderr], [], [], 0)
            if readable:
                # Use os.read() for a single non-blocking syscall.  Python's
                # file.read(N) can block waiting for N bytes even after select()
                # reports data available; os.read() returns whatever is buffered.
                raw = os.read(self._process.stderr.fileno(), 4096)
                return raw.decode(errors="replace")
        except Exception:
            pass
        return ""

    # ── Private helpers ──────────────────────────────────────────────────────

    def _build_safe_env(self) -> dict[str, str]:
        """
        Build a sanitized environment for the subprocess.

        Starts from a minimal base (PATH, HOME=/tmp, LANG), adds safe
        passthrough vars (PYTHONPATH, NODE_PATH, etc.), then overlays
        user-declared vars from pincer.toml. User-declared vars always win —
        _is_blocked only prevents leaking the parent environment, not
        explicit declarations.
        """
        import sys

        # Use sys.executable / sys.prefix to locate the active Python — reliable
        # regardless of whether VIRTUAL_ENV is set (uv run, activated venv, or
        # system Python all work).
        python_bin_dir = os.path.dirname(sys.executable)
        in_venv = sys.prefix != sys.base_prefix

        # Prefer explicit VIRTUAL_ENV; fall back to sys.prefix when inside a venv.
        venv = os.environ.get("VIRTUAL_ENV") or os.environ.get("UV_PROJECT_ENVIRONMENT") or (sys.prefix if in_venv else "")

        # Build PATH: always include the actual Python's bin dir first.
        path_parts = [python_bin_dir]
        if venv and os.path.join(venv, "bin") != python_bin_dir:
            path_parts.append(os.path.join(venv, "bin"))
        path_parts += ["/usr/local/bin", "/usr/bin", "/bin"]
        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique_parts = [p for p in path_parts if p not in seen and not seen.add(p)]  # type: ignore[func-returns-value]

        base: dict[str, str] = {
            "PATH": ":".join(unique_parts),
            "HOME": "/tmp",  # nosec B108 — intentional sandbox restriction
            "LANG": "en_US.UTF-8",
            "TMPDIR": "/tmp",  # nosec B108 — intentional sandbox restriction
            # Disable Python output buffering so stdio-protocol responses
            # are written to the pipe immediately rather than held in a buffer.
            "PYTHONUNBUFFERED": "1",
        }
        if venv:
            base["VIRTUAL_ENV"] = venv

        # Safe passthrough from current env (runtime paths needed by tools)
        safe_passthroughs = ("PYTHONPATH", "NODE_PATH", "npm_config_cache", "NVM_DIR", "UV_PROJECT_ENVIRONMENT")
        for key in safe_passthroughs:
            val = os.environ.get(key)
            if val:
                base[key] = val

        # User-declared vars (from pincer.toml) are always passed through —
        # _is_blocked only guards against leaking the parent environment,
        # not against explicit declarations the user made intentionally.
        base.update(self.user_env)

        return base

    @staticmethod
    def _is_blocked(key: str) -> bool:
        """Return True if this env var key should not be passed to the sandbox."""
        upper = key.upper()
        if upper in _BLOCKED_ENV_EXACT:
            return True
        return any(upper.startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES)

    @staticmethod
    def _apply_resource_limits(memory_limit_mb: int) -> None:
        """
        Optionally apply RLIMIT_AS in the child process.

        Set memory_limit_mb=0 to skip (the default). RLIMIT_AS caps total virtual
        address space, which is the wrong limit for modern binaries: Rust, Go, and
        Node.js routinely map several GB of VA space at startup even when actual RSS
        is tiny. Setting this too low causes immediate OOM crashes before the server
        can process a single request. Only set it when you know the binary's VA usage.
        """
        if sys.platform == "win32" or memory_limit_mb == 0:
            return
        try:
            import resource

            limit_bytes = memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

            if sys.platform == "darwin":
                # macOS accepts the call but does not enforce RLIMIT_AS.
                logger.debug(
                    "MCPSandbox: RLIMIT_AS set to %dMB but NOT enforced on macOS",
                    memory_limit_mb,
                )
        except Exception as exc:
            # Non-fatal: subprocess still runs without memory limit.
            logger.warning("MCPSandbox: could not set RLIMIT_AS: %s", exc)

    def _cleanup_tmpdir(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


# ── Async stream bridge ───────────────────────────────────────────────────────


@asynccontextmanager
async def sandbox_streams(sandbox: MCPSandbox) -> Any:
    """
    Async context manager: bridge an MCPSandbox's stdio pipes to anyio
    memory object streams compatible with mcp.ClientSession.

    Yields (read_stream, write_stream) where:
    - read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
      data flowing subprocess stdout → MCP session reader
    - write_stream: MemoryObjectSendStream[SessionMessage]
      data flowing MCP session writer → subprocess stdin

    The subprocess must be started before entering this context.
    Background reader/writer tasks are cancelled on exit.

    Example::

        sandbox.start()
        async with sandbox_streams(sandbox) as (read_s, write_s):
            async with ClientSession(read_s, write_s) as session:
                await session.initialize()
    """
    try:
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage
    except ImportError as exc:
        raise RuntimeError("mcp package not installed. Run: uv pip install 'pincer-agent[mcp]'") from exc

    import anyio

    proc = sandbox._process
    if proc is None:
        raise RuntimeError(f"MCPSandbox '{sandbox.server_name}' is not started")

    assert proc.stdin is not None
    assert proc.stdout is not None
    stdin_pipe = proc.stdin
    stdout_pipe = proc.stdout

    # Streams carry SessionMessage (the MCP SDK's transport wrapper type).
    read_send, read_receive = anyio.create_memory_object_stream(max_buffer_size=100)
    write_send, write_receive = anyio.create_memory_object_stream(max_buffer_size=100)

    async def _stdout_reader() -> None:
        """Read newline-delimited JSON from subprocess stdout, wrap as SessionMessage."""
        async with read_send:
            try:
                while True:
                    line: bytes = await anyio.to_thread.run_sync(stdout_pipe.readline, abandon_on_cancel=True)
                    if not line:
                        break  # EOF
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        msg = JSONRPCMessage.model_validate_json(stripped)
                        await read_send.send(SessionMessage(msg))
                    except Exception as parse_exc:
                        await read_send.send(parse_exc)
            except anyio.EndOfStream:
                pass
            except Exception:
                logger.debug("MCPSandbox '%s' stdout reader exited", sandbox.server_name, exc_info=True)

    async def _stdin_writer() -> None:
        """Unwrap SessionMessage → JSON → write to subprocess stdin."""
        async with write_receive:
            try:
                async for session_message in write_receive:
                    # SessionMessage wraps the actual JSONRPCMessage.
                    rpc_msg = session_message.message
                    json_str = rpc_msg.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                    data = json_str.encode()

                    def _write(d: bytes = data) -> None:
                        stdin_pipe.write(d)
                        stdin_pipe.flush()

                    await anyio.to_thread.run_sync(_write, abandon_on_cancel=True)
            except Exception:
                logger.debug("MCPSandbox '%s' stdin writer exited", sandbox.server_name, exc_info=True)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdout_reader)
        tg.start_soon(_stdin_writer)
        try:
            yield read_receive, write_send
        finally:
            tg.cancel_scope.cancel()

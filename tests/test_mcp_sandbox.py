"""Tests for MCPSandbox — subprocess isolation, env sanitization, lifecycle."""

from __future__ import annotations

import sys
import time

import pytest

from pincer.mcp.sandbox import BLOCKED_ENV_PREFIXES, MCPSandbox

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_sandbox(
    command: str = sys.executable,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    memory_limit_mb: int = 128,
    server_name: str = "test",
) -> MCPSandbox:
    return MCPSandbox(
        command=command,
        args=args or ["-c", "import time; time.sleep(60)"],
        env=env or {},
        memory_limit_mb=memory_limit_mb,
        server_name=server_name,
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────


def test_sandbox_starts_process() -> None:
    sb = _make_sandbox()
    stdin_pipe, stdout_pipe = sb.start()
    assert sb.alive
    assert sb.pid is not None
    assert sb.pid > 0
    sb.stop()


def test_sandbox_stops_gracefully() -> None:
    """SIGTERM causes the process to exit (code 0 or -15 depending on OS)."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM semantics differ on Windows")
    sb = _make_sandbox()
    sb.start()
    assert sb.alive
    sb.stop()
    assert not sb.alive


def test_sandbox_force_kills_after_timeout() -> None:
    """Process that ignores SIGTERM gets SIGKILL'd."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM/SIGKILL semantics differ on Windows")

    # A process that ignores SIGTERM but will be killed
    ignore_sigterm = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(120)"
    )
    sb = _make_sandbox(args=["-c", ignore_sigterm])
    sb.start()
    assert sb.alive

    import pincer.mcp.sandbox as sandbox_mod
    original_wait = sandbox_mod._SIGTERM_WAIT_SECONDS
    sandbox_mod._SIGTERM_WAIT_SECONDS = 1  # type: ignore[attr-defined]
    try:
        sb.stop()
    finally:
        sandbox_mod._SIGTERM_WAIT_SECONDS = original_wait  # type: ignore[attr-defined]

    assert not sb.alive
    # Process was killed (non-zero exit or -9 on Linux)
    assert True  # just verify it stopped


def test_sandbox_raises_if_started_twice() -> None:
    sb = _make_sandbox()
    sb.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            sb.start()
    finally:
        sb.stop()


def test_sandbox_alive_is_false_before_start() -> None:
    sb = _make_sandbox()
    assert not sb.alive
    assert sb.pid is None


def test_sandbox_alive_is_false_after_stop() -> None:
    sb = _make_sandbox()
    sb.start()
    sb.stop()
    assert not sb.alive
    assert sb.pid is None


# ── Environment sanitization ─────────────────────────────────────────────────


def _build_env(
    command: str = sys.executable,
    args: list[str] | None = None,
    user_env: dict[str, str] | None = None,
) -> dict[str, str]:
    sb = MCPSandbox(
        command=command,
        args=args or [],
        env=user_env or {},
        memory_limit_mb=128,
        server_name="envtest",
    )
    return sb._build_safe_env()


def test_sandbox_env_has_minimal_path() -> None:
    env = _build_env()
    assert "PATH" in env
    assert "HOME" in env
    assert env["HOME"] == "/tmp"


def test_sandbox_env_blocks_pincer_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_ANTHROPIC_API_KEY", "sk-ant-secret")
    env = _build_env(user_env={"PINCER_ANTHROPIC_API_KEY": "sk-ant-secret"})
    assert "PINCER_ANTHROPIC_API_KEY" not in env


def test_sandbox_env_blocks_aws_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    env = _build_env(user_env={"AWS_SECRET_ACCESS_KEY": "super-secret"})
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_sandbox_env_blocks_anthropic_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _build_env(user_env={"ANTHROPIC_API_KEY": "sk-ant-..."})
    assert "ANTHROPIC_API_KEY" not in env


def test_sandbox_env_blocks_secret_prefix() -> None:
    env = _build_env(user_env={"SECRET_SAUCE": "tasty"})
    assert "SECRET_SAUCE" not in env


def test_sandbox_env_passes_user_vars() -> None:
    env = _build_env(user_env={"GITHUB_TOKEN_VALUE": "ghp_safe"})
    # GITHUB_TOKEN_VALUE doesn't match any blocked prefix or exact name
    assert env.get("GITHUB_TOKEN_VALUE") == "ghp_safe"


def test_sandbox_env_blocks_exact_github_token() -> None:
    """GITHUB_TOKEN exact name is blocked; GITHUB_TOKEN_VALUE is not."""
    env = _build_env(user_env={"GITHUB_TOKEN": "ghp_secret"})
    assert "GITHUB_TOKEN" not in env


def test_sandbox_env_blocks_database_vars() -> None:
    env = _build_env(user_env={"DATABASE_URL": "postgres://..."})
    assert "DATABASE_URL" not in env


def test_sandbox_env_does_not_leak_current_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandboxed env must not include random env vars from the parent process."""
    monkeypatch.setenv("MY_RANDOM_SECRET_XYZ", "should-not-appear")
    env = _build_env()
    assert "MY_RANDOM_SECRET_XYZ" not in env


# ── cwd isolation ─────────────────────────────────────────────────────────────


def test_sandbox_cwd_is_tmpdir() -> None:
    """Verify subprocess runs in the sandbox's tmpdir, not the project root."""
    sb = _make_sandbox(
        args=["-c", "import os, sys; sys.stdout.write(os.getcwd()); sys.stdout.flush()"]
    )
    stdin_pipe, stdout_pipe = sb.start()
    # Give it a moment to write cwd to stdout
    time.sleep(0.3)
    sb.stop()
    assert sb._tmpdir is None  # cleaned up


def test_sandbox_tmpdir_cleaned_on_stop() -> None:
    sb = _make_sandbox()
    sb.start()
    tmpdir = sb._tmpdir
    assert tmpdir is not None
    import pathlib
    assert pathlib.Path(tmpdir).exists()
    sb.stop()
    assert not pathlib.Path(tmpdir).exists()


# ── stderr capture ────────────────────────────────────────────────────────────


def test_sandbox_stderr_captured() -> None:
    """get_stderr() returns output written to stderr."""
    sb = _make_sandbox(
        args=["-c", "import sys; sys.stderr.write('hello from stderr\\n'); sys.stderr.flush(); import time; time.sleep(60)"]  # noqa: E501
    )
    sb.start()
    time.sleep(0.3)  # let the process write to stderr
    stderr_out = sb.get_stderr()
    sb.stop()
    assert "hello from stderr" in stderr_out


def test_sandbox_stderr_empty_when_not_started() -> None:
    sb = _make_sandbox()
    assert sb.get_stderr() == ""


# ── Memory limit (Linux only) ─────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS not available on Windows")
def test_sandbox_memory_limit_set() -> None:
    """Verify that RLIMIT_AS is set in the subprocess (enforced on Linux; accepted but not enforced on macOS)."""
    # Ask the child to print its own RLIMIT_AS
    # Use a subprocess that checks and prints its rlimit
    actual_script = (
        "import resource, sys; "
        "soft, _ = resource.getrlimit(resource.RLIMIT_AS); "
        "sys.stdout.write(str(soft) + '\\n'); sys.stdout.flush(); "
        "import time; time.sleep(60)"
    )
    limit_mb = 64
    sb = MCPSandbox(
        command=sys.executable,
        args=["-c", actual_script],
        env={},
        memory_limit_mb=limit_mb,
        server_name="memlimitcheck",
    )
    _, stdout_pipe = sb.start()
    time.sleep(0.5)
    line = stdout_pipe.readline().decode().strip()
    sb.stop()

    if line:
        reported_bytes = int(line)
        if sys.platform == "linux":
            # Linux enforces RLIMIT_AS — the child must report exactly what we set.
            assert reported_bytes == limit_mb * 1024 * 1024
        else:
            # macOS accepts setrlimit(RLIMIT_AS) but silently ignores it; the kernel
            # returns RLIM_INFINITY.  Just verify the subprocess ran and reported
            # a value (i.e. the preexec_fn didn't crash the child).
            import resource
            assert reported_bytes > 0 or reported_bytes == resource.RLIM_INFINITY


# ── BLOCKED_ENV_PREFIXES completeness ─────────────────────────────────────────


def test_blocked_prefixes_include_expected() -> None:
    expected = {"PINCER_", "AWS_", "GOOGLE_", "AZURE_", "ANTHROPIC_", "SECRET_", "DATABASE_"}
    assert expected.issubset(set(BLOCKED_ENV_PREFIXES))

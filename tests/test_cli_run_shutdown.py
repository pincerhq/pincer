"""A real `pincer run`, stopped by a real signal, must finish its whole graceful shutdown.

The agent runs in a subprocess, isolated from the developer's own config: a scratch HOME,
cwd and data dir, a dummy API key, and a Signal channel in poll mode aimed at a local HTTP
stub (an agent with no channel exits before it starts its API server, and the API server
is part of what is being tested here).
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_support import free_port, pincer_command, scratch_env, signal_channel_env, wait_for_output

from pincer import daemon

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

STARTUP_TIMEOUT = 90.0
SHUTDOWN_TIMEOUT = 60.0


def _signal_after_banner(
    tmp_path: Path, args: list[str], extra_env: dict[str, str], sig: signal.Signals, pincer_toml: str = ""
) -> tuple[int, str]:
    """Run `pincer <args>` in a scratch environment until its "is running!" banner, send `sig`, wait for the exit."""
    home, cwd, data = tmp_path / "home", tmp_path / "cwd", tmp_path / "data"
    for path in (home, cwd, data):
        path.mkdir()
    if pincer_toml:
        (cwd / "pincer.toml").write_text(pincer_toml)
    env = scratch_env(home, data, PYTHONUNBUFFERED="1", **extra_env)
    out_path = tmp_path / "pincer.out"
    with out_path.open("wb") as out:
        proc = subprocess.Popen(pincer_command(*args), cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        wait_for_output(proc, out_path, "is running!", STARTUP_TIMEOUT, "pincer")

        proc.send_signal(sig)
        exit_code = proc.wait(timeout=SHUTDOWN_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return exit_code, out_path.read_text(errors="replace")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_runs_the_whole_graceful_shutdown(tmp_path: Path, signal_stub: str, sig: signal.Signals) -> None:
    exit_code, output = _signal_after_banner(
        tmp_path,
        ["run"],
        {
            "PINCER_DASHBOARD_HOST": "127.0.0.1",
            "PINCER_DASHBOARD_PORT": str(free_port()),
            **signal_channel_env(signal_stub),
        },
        sig,
    )

    assert "Shutting down..." in output
    # The channels are stopped near the end of the `finally`, the banner is its last line.
    assert "SignalChannel stopped" in output, output
    assert "Shutdown complete" in output, output
    assert "Traceback" not in output, output
    assert exit_code == 0


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_runs_the_tasks_workers_whole_graceful_shutdown(
    tmp_path: Path, redis_url: str, sig: signal.Signals
) -> None:
    exit_code, output = _signal_after_banner(
        tmp_path, ["run", "tasks"], {"PINCER_TASK_BROKER": "redis", "PINCER_TASK_BROKER_URL": redis_url}, sig
    )

    assert "Shutting down..." in output, output
    # The last line of its `finally`: the repid worker was cancelled and every store closed before it.
    assert "Tasks worker shutdown complete" in output, output
    assert "Traceback" not in output, output
    assert exit_code == 0


def test_a_stopped_daemon_leaves_no_record_behind(tmp_path: Path, signal_stub: str) -> None:
    # `pincer run --daemon-child` is what `run -d` starts: the agent inside the lease.
    exit_code, output = _signal_after_banner(
        tmp_path,
        ["run", "--daemon-child"],
        {
            "PINCER_DASHBOARD_HOST": "127.0.0.1",
            "PINCER_DASHBOARD_PORT": str(free_port()),
            **signal_channel_env(signal_stub),
        },
        signal.SIGTERM,
    )

    assert "Shutdown complete" in output, output
    assert exit_code == 0
    assert not (tmp_path / "data" / daemon.PID_FILE).exists()


def test_the_embedded_api_server_leaves_sigterm_and_sigint_to_the_agent() -> None:
    import uvicorn

    from pincer.cli.run import _embedded_api_server

    server = _embedded_api_server(uvicorn.Config(app=lambda *_: None, port=free_port()))
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))

    with server.capture_signals():
        assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


@pytest.fixture
def slow_signal_stub() -> Iterator[str]:
    """A Signal stub whose health check never answers in time: the agent stays in its startup."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(60)  # a daemon thread; dropped with the server

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_during_startup_shuts_down_what_has_started(
    tmp_path: Path, slow_signal_stub: str, sig: signal.Signals
) -> None:
    home, cwd, data = tmp_path / "home", tmp_path / "cwd", tmp_path / "data"
    for path in (home, cwd, data):
        path.mkdir()
    env = scratch_env(
        home,
        data,
        PYTHONUNBUFFERED="1",
        PINCER_DASHBOARD_HOST="127.0.0.1",
        PINCER_DASHBOARD_PORT=str(free_port()),
        **signal_channel_env(slow_signal_stub),
    )
    out_path = tmp_path / "pincer.out"
    with out_path.open("wb") as out:
        proc = subprocess.Popen(pincer_command("run"), cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        # Discord's line comes right before the Signal channel, which then waits on the health check.
        wait_for_output(proc, out_path, "Discord skipped", STARTUP_TIMEOUT, "pincer")
        time.sleep(0.5)
        assert "is running!" not in out_path.read_text(errors="replace")

        proc.send_signal(sig)
        exit_code = proc.wait(timeout=SHUTDOWN_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    output = out_path.read_text(errors="replace")
    assert "is running!" not in output
    assert "Shutting down..." in output, output
    assert "Shutdown complete" in output, output
    assert "Traceback" not in output, output
    assert exit_code == 0


class _Exited(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


class _Part:
    """Stands in for one thing the agent started; records its stop, and can fail it."""

    def __init__(self, name: str, stopped: list[str], fail: bool = False) -> None:
        self.name, self._stopped, self._fail = name, stopped, fail

    async def stop(self) -> None:
        self._stopped.append(self.name)
        if self._fail:
            raise RuntimeError(f"{self.name} broke")


@pytest.mark.parametrize(
    "failing",
    [None, "triggers", "telephony telemetry", "telemetry import"],
    ids=["clean", "a step fails", "telemetry fails", "telemetry import fails"],
)
async def test_shutdown_stops_what_started_last_first_and_exits_1_only_when_a_step_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failing: str | None,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pincer.cli import run as run_module

    stopped: list[str] = []

    def exit_(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(run_module.os, "_exit", exit_)

    async def dispose_engines() -> None:
        stopped.append("database engines")

    monkeypatch.setattr("pincer.db.engine.dispose_engines", dispose_engines)
    if failing == "telephony telemetry":

        async def telemetry_breaks() -> None:
            raise RuntimeError("exporter flush failed")

        monkeypatch.setattr("pincer.voice.telemetry.runtime.shutdown", telemetry_breaks)
    if failing == "telemetry import":
        import builtins

        real_import = builtins.__import__

        def import_(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("pincer.voice.telemetry"):
                raise RuntimeError("a broken optional dependency")
            return real_import(name, *args, **kwargs)

        # Anything but ImportError: registering must not abort startup, and the step counts it.
        monkeypatch.setattr(builtins, "__import__", import_)
    core = SimpleNamespace(
        mcp_manager=_Part("MCP clients", stopped),
        llm=AsyncMock(),
        session_mgr=AsyncMock(),
        cost_tracker=AsyncMock(),
        memory_store=None,
        audit_logger=None,
    )
    # Registered in the order the agent starts them.
    shutdown = run_module._Shutdown()
    run_module._stop_core_on_shutdown(shutdown, core)  # type: ignore[arg-type]
    arm_mcp_server = run_module._reserve_stop(shutdown, "MCP server")
    run_module._stop_channels_on_shutdown(shutdown, SimpleNamespace(channels={"signal": _Part("signal", stopped)}))
    for name in ("triggers", "dispatcher", "result_relay"):
        shutdown.on_stop(name, _Part(name, stopped, fail=failing == name).stop)
    arm_mcp_server(_Part("MCP server", stopped).stop)  # it starts after the relay
    sigint = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(_Exited) as exited:
            await run_module._shut_down_agent(shutdown, None)
    finally:
        signal.signal(signal.SIGINT, sigint)

    # Last started first: the channels drain before the MCP clients (their tools) go, the shared
    # database engines go last, and every step ran.
    assert stopped == [
        "result_relay",
        "dispatcher",
        "triggers",
        "signal",
        "MCP server",
        "MCP clients",
        "database engines",
    ]
    core.llm.close.assert_awaited_once()
    assert "Shutdown complete" in capsys.readouterr().out
    if failing is None:
        assert exited.value.code == 0
    else:
        assert exited.value.code == 1
        step = "telephony telemetry" if failing == "telemetry import" else failing
        assert f"Shutdown step failed: {step}" in caplog.text


def test_the_channels_drain_before_the_mcp_server_stops(tmp_path: Path, signal_stub: str) -> None:
    # A call drained at shutdown may still use tools the MCP server exposes.
    exit_code, output = _signal_after_banner(
        tmp_path,
        ["run"],
        {
            "PINCER_DASHBOARD_HOST": "127.0.0.1",
            "PINCER_DASHBOARD_PORT": str(free_port()),
            **signal_channel_env(signal_stub),
        },
        signal.SIGTERM,
        pincer_toml=f"[mcp.server]\nenabled = true\nport = {free_port()}\n",
    )

    assert exit_code == 0, output
    assert "MCP server stopped" in output, output
    assert output.index("SignalChannel stopped") < output.index("MCP server stopped"), output

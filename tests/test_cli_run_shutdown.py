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
from pathlib import Path

import pytest
from daemon_support import free_port, pincer_command, scratch_env, signal_channel_env, wait_for_output

from pincer import daemon

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

STARTUP_TIMEOUT = 90.0
SHUTDOWN_TIMEOUT = 60.0


def _signal_after_banner(
    tmp_path: Path, args: list[str], extra_env: dict[str, str], sig: signal.Signals
) -> tuple[int, str]:
    """Run `pincer <args>` in a scratch environment until its "is running!" banner, send `sig`, wait for the exit."""
    home, cwd, data = tmp_path / "home", tmp_path / "cwd", tmp_path / "data"
    for path in (home, cwd, data):
        path.mkdir()
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

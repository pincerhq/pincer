"""End to end: a real `pincer run -d` → `status` → `logs` → `stop`, as an operator types them.

Opt-in (`PINCER_TEST_DAEMON_E2E=1`): it starts real Pincer processes. Each command runs from a
scratch HOME, cwd and data dir with an environment built from nothing (no developer shell
variables, no `.env`), a dummy API key, and a Signal channel in poll mode aimed at a local HTTP
stub, since an agent with no channel never starts. The tasks-worker case also needs
`PINCER_TEST_REDIS_URL`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from daemon_support import alive, free_port, scratch_env, signal_channel_env, wait_for_output

from pincer import __version__, daemon

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="daemon mode is POSIX-only"),
    pytest.mark.skipif(not os.environ.get("PINCER_TEST_DAEMON_E2E"), reason="PINCER_TEST_DAEMON_E2E not set"),
]

# Generous: a cold start loads models and runs migrations.
WAIT_TIMEOUT = 90
COMMAND_TIMEOUT = WAIT_TIMEOUT + 30


@dataclass
class _Scratch:
    """Where the commands run, and what they see."""

    cwd: Path
    data: Path
    port: int
    env: dict[str, str]
    pids: list[int] = field(default_factory=list)

    def pincer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-P", "-m", "pincer", *args],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )

    def log(self) -> str:
        return (self.data / "logs" / "pincer.log").read_text(errors="replace")


@pytest.fixture
def scratch(tmp_path: Path, signal_stub: str) -> Iterator[_Scratch]:
    home, cwd, data = tmp_path / "home", tmp_path / "cwd", tmp_path / "data"
    for path in (home, cwd, data):
        path.mkdir()
    port = free_port()
    env = scratch_env(
        home,
        data,
        PINCER_DASHBOARD_HOST="127.0.0.1",
        PINCER_DASHBOARD_PORT=str(port),
        **signal_channel_env(signal_stub),
    )
    yield (scratch := _Scratch(cwd, data, port, env))
    # A failed test must not leave Pincer running: kill what it started, by the pids it saw, plus
    # whatever daemon still holds this data dir's lease (a `run -d` that timed out printed no pid).
    running = daemon.read_state(data)
    for pid in [*scratch.pids, *([running.pid] if isinstance(running, daemon.DaemonState) else [])]:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


def _start(scratch: _Scratch) -> int:
    started = scratch.pincer("run", "-d", "--wait-timeout", str(WAIT_TIMEOUT))
    match = re.search(
        r"^Pincer started \(pid (\d+)\) — channels: signal  'pincer logs -f' to follow$", started.stdout, re.M
    )
    if match:
        scratch.pids.append(int(match.group(1)))
    assert started.returncode == 0, started.stdout + started.stderr
    assert match, started.stdout
    return int(match.group(1))


def test_run_detach_status_logs_and_stop_against_real_pincer(scratch: _Scratch) -> None:
    pid = _start(scratch)

    # Ready means the agent marked itself ready at its "is running!" banner, with its channels.
    record = json.loads((scratch.data / "pincer.pid").read_text())
    assert (record["pid"], record["state"], record["channels"]) == (pid, "ready", ["signal"])

    status = scratch.pincer("status")
    assert status.returncode == 0, status.stderr
    assert re.fullmatch(
        rf"Pincer is running \(pid {pid}, up \d+s\) — port {scratch.port}, channels: signal, "
        rf"version {re.escape(__version__)}, cwd {re.escape(str(scratch.cwd.resolve()))}\n",
        status.stdout,
    ), status.stdout

    logs = scratch.pincer("logs")
    assert logs.returncode == 0, logs.stderr
    assert logs.stderr == ""
    assert "is running! Channels: signal. Press Ctrl+C to stop." in logs.stdout

    tail = scratch.pincer("logs", "-n", "5")
    assert tail.returncode == 0, tail.stderr
    assert len(tail.stdout.splitlines()) == 5
    assert tail.stdout in scratch.log()  # the log may have grown since, by a poll warning

    stopped = scratch.pincer("stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert stopped.stdout.splitlines() == [f"Stopping Pincer (pid {pid})…", f"Pincer stopped (pid {pid})"]
    assert not alive(pid)
    with socket.create_server(("127.0.0.1", scratch.port)):  # the port is free again
        pass
    log = scratch.log()
    assert "SignalChannel stopped" in log
    assert "Shutdown complete" in log
    assert "Traceback" not in log
    assert scratch.pincer("status").stdout == "Pincer is not running\n"


def test_a_tasks_worker_outlives_pincer_stop(scratch: _Scratch, redis_url: str, tmp_path: Path) -> None:
    scratch.env |= {"PINCER_TASK_BROKER": "redis", "PINCER_TASK_BROKER_URL": redis_url}
    out_path = tmp_path / "worker.out"
    with out_path.open("wb") as out:
        # In a session of its own, like a worker started in another terminal.
        worker = subprocess.Popen(
            [sys.executable, "-P", "-m", "pincer", "run", "tasks"],
            cwd=scratch.cwd,
            env=scratch.env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    scratch.pids.append(worker.pid)
    try:
        wait_for_output(worker, out_path, "Tasks worker is running!", WAIT_TIMEOUT, "the worker")
        _start(scratch)

        stopped = scratch.pincer("stop")

        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert worker.poll() is None, out_path.read_text(errors="replace")
        worker.send_signal(signal.SIGTERM)
        assert worker.wait(timeout=60) == 0
        assert "Tasks worker shutdown complete" in out_path.read_text(errors="replace")
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()

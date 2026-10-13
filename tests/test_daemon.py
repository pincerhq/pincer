"""Tests for the daemon lifecycle module, against a fake child (tests/daemon_fake_child.py)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import daemon_fake_child
import pytest

from pincer import daemon
from pincer.daemon import StartOutcome, StopOutcome

WAIT_TIMEOUT = 20.0
# Stands in for `pincer run -d`: starts a daemon from its own cwd/environment, prints its PID and exits.
LAUNCHER = """
import os, sys
from pathlib import Path
from pincer import daemon

result = daemon.start(Path(sys.argv[1]), sys.argv[2:], Path.cwd(), dict(os.environ), 20.0)
print(result.state.pid)
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="daemon mode is POSIX-only")


def _start(data_dir: Path, mode: str = "ready", **kwargs: object) -> daemon.StartResult:
    options: dict[str, object] = {"cwd": data_dir, "env": dict(os.environ), "wait_timeout": WAIT_TIMEOUT}
    options.update(kwargs)
    return daemon.start(data_dir, daemon_fake_child.argv(mode, data_dir), **options)  # type: ignore[arg-type]


def _alive(pid: int) -> bool:
    """True while `pid` runs. Reaps it first when it is our own child, so a zombie reads as dead."""
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_state(data_dir: Path, state: str) -> daemon.DaemonState:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        current = daemon.read_state(data_dir)
        if current is not None and current.state == state:
            return current
        time.sleep(0.02)
    raise AssertionError(f"daemon never reached {state!r}")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _stop_leftovers(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A failing test must not leave a fake child running — found by its argv, not through the code under test."""
    yield
    daemon_fake_child.kill_all_under(tmp_path)


def test_start_returns_ready_child_and_stop_stops_it(data_dir: Path) -> None:
    result = _start(data_dir)

    assert result.outcome is StartOutcome.READY
    assert result.state is not None
    assert result.state.channels == ["telegram", "signal"]
    assert _alive(result.state.pid)
    # Already in the log while the daemon runs, not only once its output is flushed at exit.
    assert "Pincer is running! Channels: telegram, signal." in daemon.log_path(data_dir).read_text()

    stopped = daemon.stop(data_dir)

    assert stopped.outcome is StopOutcome.STOPPED
    assert stopped.pid == result.state.pid
    assert daemon.read_state(data_dir) is None


def test_record_goes_starting_then_ready_and_the_lock_survives_the_rewrite(data_dir: Path) -> None:
    from pincer import __version__

    child = subprocess.Popen(daemon_fake_child.argv("gated", data_dir), cwd=data_dir)
    try:
        starting = _wait_for_state(data_dir, "starting")
        assert set(asdict(starting)) == {"pid", "started_at", "cwd", "port", "version", "state", "channels"}
        assert starting.pid == child.pid
        assert starting.cwd == str(data_dir.resolve())
        assert starting.port == 8080
        assert starting.version == __version__
        assert starting.channels == []
        datetime.fromisoformat(starting.started_at)

        (data_dir / "mark-ready").touch()
        ready = _wait_for_state(data_dir, "ready")

        # The rewrite replaced the record's inode; the lease must still read as held.
        assert ready == replace(starting, state="ready", channels=["telegram", "signal"])
        assert json.loads((data_dir / daemon.PID_FILE).read_text()) == asdict(ready)
    finally:
        child.terminate()
        child.wait()


def test_stop_returns_only_after_the_daemon_has_exited(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]

    daemon.stop(data_dir)

    assert not _alive(pid)
    # It went through the child's graceful path, not a kill.
    assert "Shutdown complete" in daemon.log_path(data_dir).read_text()


def test_stop_with_nothing_running_reports_not_running(data_dir: Path) -> None:
    assert daemon.stop(data_dir).outcome is StopOutcome.NOT_RUNNING


def test_stop_never_signals_the_pid_of_a_leftover_record(data_dir: Path) -> None:
    # A dead daemon's record whose PID now belongs to an unrelated live process.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (data_dir / daemon.LOCK_FILE).touch()
        record = daemon.DaemonState(bystander.pid, "2026-10-13T00:00:00+00:00", "/", 8080, "0.8.0", "ready", [])
        (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(record)))

        result = daemon.stop(data_dir)

        assert result.outcome is StopOutcome.NOT_RUNNING
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_daemon_outlives_its_launcher_in_a_session_of_its_own(data_dir: Path) -> None:
    launcher = subprocess.run(
        [sys.executable, "-c", LAUNCHER, str(data_dir), *daemon_fake_child.argv("ready", data_dir)],
        capture_output=True,
        text=True,
        timeout=WAIT_TIMEOUT,
        check=True,
    )
    pid = int(launcher.stdout)

    # The launcher has exited; the daemon has not, and leads a session of its own.
    state = daemon.read_state(data_dir)
    assert state is not None
    assert state.pid == pid
    assert _alive(pid)
    assert os.getsid(pid) == pid
    assert os.getsid(pid) != os.getsid(0)

    daemon.stop(data_dir)


def test_daemon_runs_in_the_given_cwd_with_the_given_environment(data_dir: Path, tmp_path: Path) -> None:
    # Both differ from this process's own, so inheriting them would not pass.
    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    assert "FAKE_CHILD_MARKER" not in os.environ

    result = _start(data_dir, cwd=caller_cwd, env={**os.environ, "FAKE_CHILD_MARKER": "from-the-caller"})

    assert result.state is not None
    assert result.state.cwd == str(caller_cwd.resolve())
    assert "marker from the caller's environment: from-the-caller" in daemon.log_path(data_dir).read_text()
    daemon.stop(data_dir)


def test_log_captures_logging_banner_and_traceback_with_numbers_masked(data_dir: Path) -> None:
    _start(data_dir, mode="raise-on-sigterm")
    daemon.stop(data_dir)

    log = daemon.log_path(data_dir).read_text()
    assert "INFO:fake_child:inbound call from +49…89" in log
    assert "Pincer is running! Channels: telegram, signal." in log
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: fake child failed during shutdown" in log
    assert "+4915112345689" not in log


def test_each_start_rotates_the_log_keeping_five_previous_sessions(data_dir: Path) -> None:
    pids = []
    for _ in range(7):
        pids.append(_start(data_dir).state.pid)  # type: ignore[union-attr]
        daemon.stop(data_dir)

    logs = data_dir / "logs"
    assert sorted(p.name for p in logs.iterdir()) == [
        "pincer.log",
        "pincer.log.1",
        "pincer.log.2",
        "pincer.log.3",
        "pincer.log.4",
        "pincer.log.5",
    ]
    # pincer.log is the 7th session, .1 the 6th (newest previous) … .5 the 2nd.
    for name, pid in zip(["pincer.log", *(f"pincer.log.{n}" for n in range(1, 6))], reversed(pids), strict=False):
        assert f"fake child {pid} starting" in (logs / name).read_text(), name


def test_daemons_on_different_data_dirs_do_not_see_each_other(data_dir: Path, tmp_path: Path) -> None:
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    first = _start(data_dir).state.pid  # type: ignore[union-attr]
    try:
        assert daemon.read_state(other_dir) is None
        assert daemon.stop(other_dir).outcome is StopOutcome.NOT_RUNNING

        second = _start(other_dir)
        assert second.outcome is StartOutcome.READY
        assert second.state is not None
        assert second.state.pid != first

        daemon.stop(data_dir)
        assert not _alive(first)
        assert _alive(second.state.pid)
    finally:
        daemon.stop(other_dir)


def test_startup_lines_are_relayed_while_start_waits(data_dir: Path) -> None:
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line)
        # The gated child only marks ready once this exists, so READY proves the line arrived mid-wait.
        if "marker from the caller's environment" in line:
            (data_dir / "mark-ready").touch()

    result = _start(data_dir, mode="gated", wait_timeout=10.0, on_line=on_line)

    assert result.outcome is StartOutcome.READY
    assert any("starting in" in line for line in seen)
    assert seen[-1] == "Pincer is running! Channels: telegram, signal. Press Ctrl+C to stop."
    daemon.stop(data_dir)


def test_a_crash_before_ready_returns_the_last_lines_and_leaves_no_live_record(data_dir: Path) -> None:
    seen: list[str] = []

    result = _start(data_dir, mode="crash-on-start", on_line=seen.append)

    assert result.outcome is StartOutcome.CRASHED
    assert result.exit_code == 1
    log_lines = daemon.log_path(data_dir).read_text().splitlines()
    assert result.last_lines == log_lines[-20:]
    assert result.last_lines[-1] == "RuntimeError: fake child failed to start"
    assert "INFO:fake_child:startup step 30" in result.last_lines
    assert "INFO:fake_child:startup step 1" not in result.last_lines
    # Everything, through the traceback's last line, was relayed while waiting.
    assert seen == log_lines
    assert daemon.read_state(data_dir) is None


def test_a_slow_start_times_out_without_touching_the_child_even_with_its_port_open(data_dir: Path) -> None:
    result = _start(data_dir, mode="slow-start", wait_timeout=1.0)

    assert result.outcome is StartOutcome.TIMED_OUT
    assert result.state is not None
    assert result.state.state == "starting"
    # Listening already, yet not ready: the port says nothing about readiness.
    with socket.create_connection(("127.0.0.1", result.state.port), timeout=1):
        pass
    assert _alive(result.state.pid)
    assert _wait_for_state(data_dir, "ready").pid == result.state.pid
    daemon.stop(data_dir)


def test_a_crash_mid_line_still_relays_the_unterminated_last_line(data_dir: Path) -> None:
    seen: list[str] = []

    result = _start(data_dir, mode="die-mid-line", on_line=seen.append)

    assert result.outcome is StartOutcome.CRASHED
    assert result.exit_code == 3
    assert seen[-1] == "fatal: no newline"
    assert result.last_lines[-1] == "fatal: no newline"

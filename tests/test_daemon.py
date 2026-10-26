"""Tests for the daemon lifecycle module, against a fake child (tests/daemon_fake_child.py)."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import daemon_fake_child
import pytest
from daemon_support import (
    RAW_LOG,
    SRC_DIR,
    alive,
    append_log,
    child_env,
    lease_held_elsewhere,
    numbered,
    stop_within,
    within,
    write_log,
)

from pincer import daemon
from pincer.daemon import StartOutcome, StopOutcome

if TYPE_CHECKING:
    from collections.abc import Buffer, Callable, Iterator

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
    options: dict[str, object] = {"cwd": data_dir, "env": child_env(), "wait_timeout": WAIT_TIMEOUT}
    options.update(kwargs)
    return daemon.start(data_dir, daemon_fake_child.argv(mode, data_dir), **options)  # type: ignore[arg-type]


def _wait_for_state(data_dir: Path, state: str) -> daemon.DaemonState:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        current = daemon.read_state(data_dir)
        if isinstance(current, daemon.DaemonState) and current.state == state:
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
    assert alive(result.state.pid)
    # Already in the log while the daemon runs, not only once its output is flushed at exit.
    assert "Pincer is running! Channels: telegram, signal." in daemon.log_path(data_dir).read_text()

    stopped = stop_within(data_dir)

    assert stopped == daemon.StopResult(StopOutcome.STOPPED, result.state.pid, daemon.Identity.VERIFIED)
    assert daemon.read_state(data_dir) is None


def test_record_goes_starting_then_ready_and_the_lock_survives_the_rewrite(data_dir: Path) -> None:
    from pincer import __version__

    child = subprocess.Popen(daemon_fake_child.argv("gated", data_dir), cwd=data_dir, env=child_env())
    try:
        starting = _wait_for_state(data_dir, "starting")
        assert set(asdict(starting)) == {
            "pid",
            "started_at",
            "cwd",
            "port",
            "version",
            "state",
            "channels",
            "start_token",
        }
        assert starting.pid == child.pid
        assert starting.start_token == daemon._start_token(child.pid)
        assert starting.start_token is not None  # this platform has them
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

    stop_within(data_dir)

    assert not alive(pid)
    # It went through the child's graceful path, not a kill.
    assert "Shutdown complete" in daemon.log_path(data_dir).read_text()


def test_stop_with_nothing_running_reports_not_running(data_dir: Path) -> None:
    assert stop_within(data_dir).outcome is StopOutcome.NOT_RUNNING


def test_stop_never_signals_the_pid_of_a_leftover_record(data_dir: Path) -> None:
    # A dead daemon's record whose PID now belongs to an unrelated live process.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (data_dir / daemon.LOCK_FILE).touch()
        record = daemon.DaemonState(bystander.pid, "2026-10-13T00:00:00+00:00", "/", 8080, "0.8.0", "ready", [])
        (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(record)))

        result = stop_within(data_dir)

        assert result.outcome is StopOutcome.NOT_RUNNING
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_daemon_outlives_its_launcher_in_a_session_of_its_own(data_dir: Path) -> None:
    launcher = subprocess.run(
        [sys.executable, "-c", LAUNCHER, str(data_dir), *daemon_fake_child.argv("ready", data_dir)],
        env=child_env(),
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
    assert alive(pid)
    assert os.getsid(pid) == pid
    assert os.getsid(pid) != os.getsid(0)

    stop_within(data_dir)


def test_daemon_runs_in_the_given_cwd_with_the_given_environment(data_dir: Path, tmp_path: Path) -> None:
    # Both differ from this process's own, so inheriting them would not pass.
    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    assert "FAKE_CHILD_MARKER" not in os.environ

    result = _start(data_dir, cwd=caller_cwd, env={**os.environ, "FAKE_CHILD_MARKER": "from-the-caller"})

    assert result.state is not None
    assert result.state.cwd == str(caller_cwd.resolve())
    assert "marker from the caller's environment: from-the-caller" in daemon.log_path(data_dir).read_text()
    stop_within(data_dir)


def test_log_captures_logging_banner_and_traceback_with_numbers_masked(data_dir: Path) -> None:
    _start(data_dir, mode="raise-on-sigterm")
    stop_within(data_dir)

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
        stop_within(data_dir)

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

        stop_within(data_dir)
        assert not alive(first)
        assert alive(second.state.pid)
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
    stop_within(data_dir)


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
    assert alive(result.state.pid)
    assert _wait_for_state(data_dir, "ready").pid == result.state.pid
    stop_within(data_dir)


def test_a_crash_mid_line_still_relays_the_unterminated_last_line(data_dir: Path) -> None:
    seen: list[str] = []

    result = _start(data_dir, mode="die-mid-line", on_line=seen.append)

    assert result.outcome is StartOutcome.CRASHED
    assert result.exit_code == 3
    assert seen[-1] == "fatal: no newline"
    assert result.last_lines[-1] == "fatal: no newline"


def test_a_start_while_one_is_running_or_starting_spawns_nothing(data_dir: Path, tmp_path: Path) -> None:
    starting = _start(data_dir, mode="gated", wait_timeout=0.5)
    assert starting.outcome is StartOutcome.TIMED_OUT

    second = _start(data_dir)

    assert second.outcome is StartOutcome.ALREADY_RUNNING
    assert second.state is not None
    assert second.state.state == "starting"
    assert len(daemon_fake_child.children_under(tmp_path)) == 1
    # The running daemon's log was not rotated away.
    assert not daemon.log_path(data_dir).with_name("pincer.log.1").exists()

    (data_dir / "mark-ready").touch()
    _wait_for_state(data_dir, "ready")
    third = _start(data_dir)

    assert third.outcome is StartOutcome.ALREADY_RUNNING
    assert third.state is not None
    assert third.state.state == "ready"
    assert len(daemon_fake_child.children_under(tmp_path)) == 1
    stop_within(data_dir)


def test_a_start_racing_another_before_its_child_takes_the_lease_waits_and_rotates_nothing(
    data_dir: Path, tmp_path: Path
) -> None:
    results: list[daemon.StartResult] = []
    first = threading.Thread(target=lambda: results.append(_start(data_dir, mode="slow-lease")))
    first.start()
    # The first child exists but has not taken the lease yet: the window a second `run -d` can land in.
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not daemon_fake_child.children_under(tmp_path):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert daemon.read_state(data_dir) is None

    second = _start(data_dir)
    first.join()

    assert second.outcome is StartOutcome.ALREADY_RUNNING
    assert [r.outcome for r in results] == [StartOutcome.READY]
    winner = results[0].state
    assert winner is not None
    assert second.state is not None
    assert second.state.pid == winner.pid
    assert daemon_fake_child.children_under(tmp_path) == [winner.pid]
    assert f"fake child {winner.pid} starting" in daemon.log_path(data_dir).read_text()
    assert not daemon.log_path(data_dir).with_name("pincer.log.1").exists()
    stop_within(data_dir)


def test_a_daemon_killed_with_sigkill_reads_as_gone_and_the_next_start_succeeds(data_dir: Path) -> None:
    first = _start(data_dir).state
    assert first is not None
    os.kill(first.pid, signal.SIGKILL)
    while alive(first.pid):
        time.sleep(0.02)
    # Its record is still on disk, naming a dead pid.
    assert json.loads((data_dir / daemon.PID_FILE).read_text())["pid"] == first.pid

    assert daemon.read_state(data_dir) is None
    second = _start(data_dir)

    assert second.outcome is StartOutcome.READY
    assert second.state is not None
    assert second.state.pid != first.pid
    stop_within(data_dir)


def test_a_start_during_another_starts_wait_answers_at_once(data_dir: Path) -> None:
    results: list[daemon.StartResult] = []
    first = threading.Thread(target=lambda: results.append(_start(data_dir, mode="gated", wait_timeout=10.0)))
    first.start()
    try:
        starting = _wait_for_state(data_dir, "starting")

        began = time.monotonic()
        second = _start(data_dir)

        # Answered while the first start is still waiting for its child, not queued behind it.
        assert time.monotonic() - began < 3.0
        assert first.is_alive()
        assert second.outcome is StartOutcome.ALREADY_RUNNING
        assert second.state == starting
    finally:
        (data_dir / "mark-ready").touch()
        first.join()
    assert [r.outcome for r in results] == [StartOutcome.READY]
    stop_within(data_dir)


def test_stop_waits_out_a_slow_shutdown_within_its_timeout(data_dir: Path) -> None:
    pid = _start(data_dir, mode="slow-shutdown").state.pid  # type: ignore[union-attr]

    began = time.monotonic()
    result = daemon.stop(data_dir, timeout=10.0)

    assert result.outcome is StopOutcome.STOPPED
    assert time.monotonic() - began >= daemon_fake_child.SLOW_SHUTDOWN_SECONDS - 0.2
    assert not alive(pid)
    assert "Shutdown complete" in daemon.log_path(data_dir).read_text()


def test_stop_gives_up_after_its_timeout_and_never_kills(data_dir: Path) -> None:
    pid = _start(data_dir, mode="ignore-sigterm").state.pid  # type: ignore[union-attr]

    began = time.monotonic()
    result = within(10.0, lambda: daemon.stop(data_dir, timeout=1.0))

    assert result.outcome is StopOutcome.STILL_RUNNING
    assert result.pid == pid
    assert 1.0 <= time.monotonic() - began < 3.0
    time.sleep(0.5)  # a kill sent on the way out would have landed by now
    assert alive(pid)
    assert daemon.read_state(data_dir) is not None


def test_force_kills_at_once_and_the_next_start_works(data_dir: Path) -> None:
    pid = _start(data_dir, mode="ignore-sigterm").state.pid  # type: ignore[union-attr]

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=5.0, force=True))

    assert result.outcome is StopOutcome.KILLED
    assert result.pid == pid
    assert not alive(pid)
    assert daemon.read_state(data_dir) is None
    assert _start(data_dir).outcome is StartOutcome.READY
    stop_within(data_dir)


def test_a_second_stop_during_a_slow_shutdown_waits_for_the_same_exit(data_dir: Path) -> None:
    pid = _start(data_dir, mode="slow-shutdown").state.pid  # type: ignore[union-attr]
    results: list[daemon.StopResult] = []
    first = threading.Thread(target=lambda: results.append(daemon.stop(data_dir, timeout=10.0)))
    first.start()
    time.sleep(0.5)

    second = daemon.stop(data_dir, timeout=10.0)
    first.join()

    assert [r.outcome for r in results] == [StopOutcome.STOPPED]
    assert second.outcome is StopOutcome.STOPPED
    assert second.pid == pid
    assert daemon.log_path(data_dir).read_text().count("Shutdown complete") == 1


def test_stop_reports_its_wait_as_it_goes(data_dir: Path) -> None:
    pid = _start(data_dir, mode="slow-shutdown").state.pid  # type: ignore[union-attr]
    seen: list[tuple[int, float]] = []

    daemon.stop(data_dir, timeout=10.0, on_wait=lambda state, elapsed: seen.append((state.pid, elapsed)))

    assert seen[0][0] == pid
    assert seen[0][1] < 0.1  # right after signalling, before any wait
    assert len(seen) > 5
    assert all(later >= earlier for (_, earlier), (_, later) in zip(seen, seen[1:], strict=False))


def test_force_kills_the_whole_process_group(data_dir: Path) -> None:
    pid = _start(data_dir, mode="grandchild").state.pid  # type: ignore[union-attr]
    grandchild = int((data_dir / "grandchild.pid").read_text())
    assert os.getpgid(grandchild) == pid

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=5.0, force=True))

    assert result.outcome is StopOutcome.KILLED
    assert not alive(pid)
    # Reparented once the daemon died; give init a moment to reap it.
    deadline = time.monotonic() + 3.0
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)


def test_force_kills_only_the_daemon_when_it_does_not_lead_its_group(data_dir: Path) -> None:
    # Started by hand, not by start(), under a throwaway group leader that outlives it: were --force
    # to kill a group the daemon doesn't lead, the leader (not pytest's own group) would go too.
    leader = subprocess.Popen(
        ["sh", "-c", '"$@" & wait; sleep 30', "sh", *daemon_fake_child.argv("ignore-sigterm", data_dir)],
        env=child_env(),
        start_new_session=True,
    )
    try:
        pid = _wait_for_state(data_dir, "ready").pid
        assert os.getpgid(pid) == leader.pid != pid

        result = within(10.0, lambda: daemon.stop(data_dir, timeout=5.0, force=True))

        assert result.outcome is StopOutcome.KILLED
        deadline = time.monotonic() + 3.0  # the leader, not pytest, reaps it
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(pid)
        assert leader.poll() is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()


class _Follower:
    """Drains a `follow()` iterator on a thread, so a test can append to the log and stop the daemon meanwhile."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.data = b""
        self.done = threading.Event()
        threading.Thread(target=self._drain, args=(chunks,), daemon=True).start()

    def _drain(self, chunks: Iterator[bytes]) -> None:
        try:
            for chunk in chunks:
                self.data += chunk
        finally:
            self.done.set()

    def wait_for(self, needle: bytes, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while needle not in self.data:
            assert time.monotonic() < deadline, f"{needle!r} never followed; got {self.data[-300:]!r}"
            time.sleep(0.02)


def test_follow_without_until_exit_gives_the_whole_file_byte_for_byte(data_dir: Path) -> None:
    write_log(data_dir, RAW_LOG)

    assert b"".join(daemon.follow(data_dir, None, until_exit=False)) == RAW_LOG


@pytest.mark.parametrize(
    ("content", "lines", "expected"),
    [
        (numbered(1, 150), 100, numbered(51, 150)),
        (numbered(1, 150), 500, numbered(1, 150)),
        (numbered(1, 150), 0, b""),
        (b"a\nb\n", 1, b"b\n"),
        (b"a\nb\nc", 2, b"b\nc"),
        # Lines longer than the backward read's block.
        (
            b"x" * 200_000 + b"\n" + b"y" * 200_000 + b"\n" + b"z" * 200_000 + b"\n",
            2,
            b"y" * 200_000 + b"\n" + b"z" * 200_000 + b"\n",
        ),
    ],
    ids=["last-100-of-150", "more-than-there-are", "zero", "trailing-newline", "unterminated-last", "long-lines"],
)
def test_follow_from_the_last_n_lines(data_dir: Path, content: bytes, lines: int, expected: bytes) -> None:
    write_log(data_dir, content)

    assert b"".join(daemon.follow(data_dir, lines, until_exit=False)) == expected


def test_follow_finds_the_tail_of_a_huge_log_without_reading_all_of_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 64 MiB (sparse where the filesystem can), of which only the last lines are text.
    log_file = write_log(data_dir, b"")
    with log_file.open("r+b") as log:
        log.truncate(64 << 20)
        log.seek(64 << 20)
        log.write(numbered(1, 200))
    read: list[int] = []

    class _Counting(io.FileIO):
        """Counts every byte taken from the file, whichever way the buffered reader asks for it."""

        def readinto(self, buffer: Buffer) -> int | None:
            got = super().readinto(buffer)
            read.append(got or 0)
            return got

        def readall(self) -> bytes:
            got = super().readall()
            read.append(len(got))
            return got

        def read(self, size: int = -1) -> bytes | None:
            got = super().read(size)
            read.append(len(got or b""))
            return got

    real_open = Path.open

    def counting_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == log_file and mode == "rb":
            return io.BufferedReader(_Counting(path, "rb"))
        return real_open(path, mode, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", counting_open)

    tail = b"".join(daemon.follow(data_dir, 5, until_exit=False))

    assert tail == numbered(196, 200)
    # The tail is found reading backward from the end, not by reading the file through.
    assert 0 < sum(read) < 2 * daemon.READ_BLOCK


def test_follow_gives_the_last_lines_then_live_ones_and_ends_after_the_shutdown_traceback(data_dir: Path) -> None:
    _start(data_dir, mode="raise-on-sigterm")
    append_log(data_dir, numbered(1, 80))

    follower = _Follower(daemon.follow(data_dir, daemon.FOLLOW_LINES, until_exit=True))
    follower.wait_for(b"numbered 80\n")
    assert follower.data == numbered(31, 80)

    append_log(data_dir, b"live 1\n")
    follower.wait_for(b"live 1\n")
    assert not follower.done.is_set()

    stop_within(data_dir)

    assert follower.done.wait(10.0), "follow() kept going after the daemon exited"
    assert b"shutting down" in follower.data
    assert follower.data.endswith(b"RuntimeError: fake child failed during shutdown\n")


def test_follow_reads_once_more_after_seeing_the_daemon_gone(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_log(data_dir, b"first\n")
    probes: list[bool] = []
    real_daemon_running = daemon.daemon_running

    def lease_held(path: Path) -> bool:
        if path != data_dir:  # a follower some failed test left behind
            return real_daemon_running(path)
        # The daemon writes its last line and exits between two polls.
        if probes:
            append_log(data_dir, b"final line\n")
        probes.append(not probes)
        return probes[-1]

    monkeypatch.setattr(daemon, "daemon_running", lease_held)

    followed = within(5.0, lambda: b"".join(daemon.follow(data_dir, None, until_exit=True)))

    assert followed == b"first\nfinal line\n"


def test_follow_ends_when_a_restart_rotates_its_log_inside_one_poll(data_dir: Path) -> None:
    first = _start(data_dir).state
    assert first is not None
    following = daemon.follow(data_dir, daemon.FOLLOW_LINES, until_exit=True)
    seen = next(following)
    # The follower isn't polling while the old daemon exits and a new one takes the lease and rotates the log.
    stop_within(data_dir)
    second = _start(data_dir).state
    assert second is not None

    seen += within(5.0, lambda: b"".join(following))

    assert f"fake child {first.pid} starting".encode() in seen
    assert seen.endswith(b"Shutdown complete\n")
    assert f"fake child {second.pid}".encode() not in seen
    stop_within(data_dir)


def test_follow_with_no_daemon_running_gives_the_tail_and_ends(data_dir: Path) -> None:
    _start(data_dir)
    stop_within(data_dir)
    append_log(data_dir, numbered(1, 80))

    followed = within(2.0, lambda: b"".join(daemon.follow(data_dir, daemon.FOLLOW_LINES, until_exit=True)))

    assert followed == numbered(31, 80)


# ── probes never pass for a daemon (review #1) ─────────────────────────────


@contextlib.contextmanager
def _probe_in_progress(data_dir: Path, seconds: float | None = None) -> Iterator[None]:
    """Hold the lock file shared, as another `pincer status` (or start's own poll) does mid-probe.

    With `seconds`, a thread lets go after that long, so what runs inside overlaps the probe's end.
    """
    (data_dir / daemon.LOCK_FILE).touch()
    fd = os.open(data_dir / daemon.LOCK_FILE, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_SH)
    timer = threading.Timer(seconds, os.close, (fd,)) if seconds is not None else None
    if timer is not None:
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.join()
        else:
            os.close(fd)


def _leftover_record(data_dir: Path, pid: int) -> None:
    """A dead daemon's record, naming `pid`."""
    (data_dir / daemon.LOCK_FILE).touch()
    record = daemon.DaemonState(pid, "2026-10-13T00:00:00+00:00", "/", 8080, "0.8.0", daemon.DaemonPhase.READY)
    (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(record)))


def test_a_probe_in_progress_never_makes_a_leftover_record_look_alive(data_dir: Path) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _leftover_record(data_dir, bystander.pid)
        with _probe_in_progress(data_dir):
            assert daemon.read_state(data_dir) is None
            assert daemon.stop(data_dir, timeout=1.0).outcome is StopOutcome.NOT_RUNNING
        time.sleep(0.2)  # a SIGTERM sent on the way would have landed by now
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_start_overlapping_a_probe_cleans_up_and_its_daemon_takes_the_lease(data_dir: Path) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _leftover_record(data_dir, bystander.pid)
        # Held through the start's cleanup and past the moment the child reaches for the lease.
        with _probe_in_progress(data_dir, seconds=1.5):
            result = _start(data_dir)

        assert result.outcome is StartOutcome.READY, result
        assert result.state is not None
        assert result.state.pid != bystander.pid
        assert bystander.poll() is None
        stop_within(data_dir)
    finally:
        bystander.kill()
        bystander.wait()


def test_a_status_loop_running_throughout_does_not_keep_a_daemon_from_its_lease(data_dir: Path) -> None:
    done = threading.Event()

    def poll() -> None:
        while not done.is_set():
            daemon.read_state(data_dir)

    pollers = [threading.Thread(target=poll) for _ in range(3)]
    for poller in pollers:
        poller.start()
    try:
        result = _start(data_dir)
    finally:
        done.set()
        for poller in pollers:
            poller.join()

    assert result.outcome is StartOutcome.READY, result
    stop_within(data_dir)


# ── a daemon past its lease, or with a record this version can't read (review #2) ──


def _lingering(data_dir: Path) -> int:
    """A daemon that has left its lease (record gone) but not yet exited (lock still held); its pid."""
    pid = _start(data_dir, mode="linger").state.pid  # type: ignore[union-attr]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + WAIT_TIMEOUT
    while (data_dir / daemon.PID_FILE).exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert alive(pid)
    return pid


def test_a_daemon_past_its_lease_reads_as_running_without_a_record(data_dir: Path) -> None:
    _lingering(data_dir)

    assert isinstance(daemon.read_state(data_dir), daemon.DaemonWithoutRecord)


def test_a_start_while_a_daemon_lingers_spawns_nothing_and_says_so(data_dir: Path, tmp_path: Path) -> None:
    pid = _lingering(data_dir)

    result = _start(data_dir)

    assert result.outcome is StartOutcome.ALREADY_RUNNING
    assert isinstance(result.state, daemon.DaemonWithoutRecord)
    assert daemon_fake_child.children_under(tmp_path) == [pid]


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_daemon_without_a_record_and_waits_for_it(data_dir: Path, force: bool) -> None:
    # No record, no start token: nothing says which process holds the lease.
    pid = _lingering(data_dir)
    assert daemon.read_state(data_dir) == daemon.DaemonWithoutRecord()

    held = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))
    assert held == daemon.StopResult(StopOutcome.STILL_RUNNING, None, daemon.Identity.UNKNOWN)
    assert alive(pid)  # not even --force killed it
    gone = within(15.0, lambda: daemon.stop(data_dir, timeout=10.0, force=force))

    assert gone == daemon.StopResult(StopOutcome.STOPPED, None, daemon.Identity.UNKNOWN)
    assert not alive(pid)
    assert "left the lease" in daemon.log_path(data_dir).read_text()


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_daemon_whose_record_it_cant_read(data_dir: Path, force: bool) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    (data_dir / daemon.PID_FILE).write_text(json.dumps({"pid": pid, "phase": "?"}))
    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid=pid)

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

    assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, pid, daemon.Identity.UNKNOWN)
    assert alive(pid)
    assert "shutting down" not in daemon.log_path(data_dir).read_text()  # never got a SIGTERM
    os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_stops_a_daemon_whose_record_it_cant_read_when_its_pid_and_token_still_check_out(
    data_dir: Path, force: bool
) -> None:
    # A newer version's phase, say: the rest of the record still says exactly which process it is.
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "state": "draining"}))
    assert isinstance(daemon.read_state(data_dir), daemon.DaemonWithUnreadableRecord)

    result = within(15.0, lambda: daemon.stop(data_dir, timeout=10.0, force=force))

    outcome = StopOutcome.KILLED if force else StopOutcome.STOPPED
    assert result == daemon.StopResult(outcome, state.pid, daemon.Identity.VERIFIED)
    assert not alive(state.pid)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_pid_in_a_record_it_cant_read_that_now_names_another_process(
    data_dir: Path, force: bool
) -> None:
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "pid": bystander.pid, "state": "draining"}))

        result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

        assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, bystander.pid, daemon.Identity.NOT_PINCER)
        time.sleep(0.2)  # a signal sent to it would have landed by now
        assert bystander.poll() is None
        assert alive(state.pid)
    finally:
        bystander.kill()
        bystander.wait()


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_a_daemon_that_cant_read_its_own_start_token_is_stopped_by_its_record_pid(data_dir: Path, force: bool) -> None:
    # As where the platform has no start tokens: its record says so, and stop signals the pid it names.
    state = _start(data_dir, mode="no-self-token").state
    assert isinstance(state, daemon.DaemonState)
    recorded = json.loads((data_dir / daemon.PID_FILE).read_text())["start_token"]

    result = within(15.0, lambda: daemon.stop(data_dir, timeout=10.0, force=force))

    outcome = StopOutcome.KILLED if force else StopOutcome.STOPPED
    assert result == daemon.StopResult(outcome, state.pid, daemon.Identity.UNCHECKED)
    assert not alive(state.pid)
    assert recorded == daemon.NO_START_TOKEN


def test_a_record_without_the_start_token_field_is_never_taken_for_one_that_has_none(data_dir: Path) -> None:
    # A record from a build before start tokens: nothing says this daemon couldn't read its own.
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    del record["start_token"]
    (data_dir / daemon.PID_FILE).write_text(json.dumps(record))

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=True))

    assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, state.pid, daemon.Identity.UNKNOWN)
    assert alive(state.pid)
    os.kill(state.pid, signal.SIGTERM)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
@pytest.mark.parametrize("record", ["another process's pid", "no start token"])
def test_stop_answers_at_once_when_it_signals_nothing_to_a_live_daemon(
    data_dir: Path, force: bool, record: str
) -> None:
    # With the CLI's own 90 s timeout: waiting would only end in the same answer, 90 s later.
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        named = (
            replace(state, pid=bystander.pid) if record == "another process's pid" else replace(state, start_token=None)
        )
        (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(named)))
        began = time.monotonic()

        result = within(5.0, lambda: daemon.stop(data_dir, timeout=90.0, force=force))

        assert time.monotonic() - began < 2.0
        assert result.outcome is StopOutcome.STILL_RUNNING
        assert not result.signalled
        assert alive(state.pid)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()
        os.kill(state.pid, signal.SIGTERM)


def test_a_newer_record_with_extra_fields_still_reads(data_dir: Path) -> None:
    state = _start(data_dir).state
    assert state is not None
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "added_in_a_later_version": True}))

    assert daemon.read_state(data_dir) == state
    stop_within(data_dir)


def test_within_reraises_what_the_call_raised() -> None:
    def fail() -> None:
        raise LookupError("from the worker")

    with pytest.raises(LookupError, match="from the worker"):
        within(5.0, fail)


def test_a_spawned_daemon_imports_the_tree_under_test(data_dir: Path) -> None:
    _start(data_dir)

    assert f"imports pincer from {SRC_DIR / 'pincer' / '__init__.py'}" in daemon.log_path(data_dir).read_text()
    stop_within(data_dir)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
@pytest.mark.parametrize("bad_pid", [0, -1], ids=["pid 0", "pid -1"])
def test_a_record_naming_no_single_process_is_unreadable_and_nothing_is_signalled(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, bad_pid: int, force: bool
) -> None:
    # os.kill(0, …) signals the caller's own group and os.kill(-1, …) everything the user may
    # signal; should either be attempted, record it and refuse instead of sending it.
    attempted: list[tuple[str, int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg

    def guarded(name: str, real: Callable[[int, int], None]) -> Callable[[int, int], None]:
        def call(pid: int, sig: int) -> None:
            if pid <= 0:
                attempted.append((name, pid, sig))
                raise PermissionError(f"refused {name}({pid}, {sig}) in a test")
            real(pid, sig)

        return call

    monkeypatch.setattr(os, "kill", guarded("kill", real_kill))
    monkeypatch.setattr(os, "killpg", guarded("killpg", real_killpg))
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(replace(state, pid=bad_pid))))

    # The record names nothing usable: nothing is signalled, only waited for.
    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(None, state.start_token)
    result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

    assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, None, daemon.Identity.UNKNOWN)
    assert attempted == []
    assert alive(state.pid)
    assert "shutting down" not in daemon.log_path(data_dir).read_text()  # never got a SIGTERM
    monkeypatch.undo()
    os.kill(state.pid, signal.SIGTERM)


def test_a_daemon_that_exits_during_the_record_grace_reads_as_gone_at_once(data_dir: Path) -> None:
    # A lease holder with no record, as at the end of DaemonLease.__exit__, letting go of the lock
    # (exiting) well inside the grace read_state gives a missing record.
    with lease_held_elsewhere(data_dir / daemon.LOCK_FILE, seconds=0.2):
        began = time.monotonic()
        state = daemon.read_state(data_dir)
        took = time.monotonic() - began

    assert state is None
    assert took < daemon.RECORD_GRACE / 2


# ── PR #242 review ─────────────────────────────────────────────────────────


def test_a_start_that_stops_waiting_early_still_keeps_out_a_rival_until_its_child_holds_the_lease(
    data_dir: Path, tmp_path: Path
) -> None:
    # The first start gives up at once (as with --wait-timeout 0, or Ctrl+C) while its child is
    # still importing; a second start must not see the lease free, rotate that child's log away
    # and spawn a rival.
    first = _start(data_dir, mode="slow-lease", wait_timeout=0.0)
    assert first.outcome is StartOutcome.TIMED_OUT
    [child] = daemon_fake_child.children_under(tmp_path)

    second = _start(data_dir)

    assert second.outcome is StartOutcome.ALREADY_RUNNING, second
    assert isinstance(second.state, daemon.DaemonState)
    assert second.state.pid == child
    assert daemon_fake_child.children_under(tmp_path) == [child]
    assert f"fake child {child} starting" in daemon.log_path(data_dir).read_text()
    assert not daemon.log_path(data_dir).with_name("pincer.log.1").exists()
    stop_within(data_dir)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_pid_now_naming_another_process(data_dir: Path, force: bool) -> None:
    # The record names a live process that isn't the daemon (its pid reused after the daemon that
    # recorded it had gone; a hand-edited file): its start token gives it away.
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(replace(state, pid=bystander.pid))))
        assert daemon.read_state(data_dir) == replace(state, pid=bystander.pid)

        result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

        assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, bystander.pid, daemon.Identity.NOT_PINCER)
        time.sleep(0.2)  # a signal sent to it would have landed by now
        assert bystander.poll() is None
        assert alive(state.pid)
        assert "shutting down" not in daemon.log_path(data_dir).read_text()  # never got a SIGTERM
    finally:
        bystander.kill()
        bystander.wait()
        os.kill(state.pid, signal.SIGTERM)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_daemon_whose_record_has_no_start_token(data_dir: Path, force: bool) -> None:
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(replace(state, start_token=None))))

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

    assert result == daemon.StopResult(StopOutcome.STILL_RUNNING, state.pid, daemon.Identity.UNKNOWN)
    assert alive(state.pid)
    assert "shutting down" not in daemon.log_path(data_dir).read_text()  # never got a SIGTERM
    os.kill(state.pid, signal.SIGTERM)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", "yesterday"),
        ("started_at", 1760000000),
        ("port", "8080"),
        ("port", True),
        ("channels", "telegram"),
        ("channels", [1, 2]),
        ("cwd", 3),
        ("version", None),
        ("state", "booting"),
    ],
)
def test_a_record_with_a_malformed_field_is_unreadable(data_dir: Path, field: str, value: object) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, field: value}))

    assert not isinstance(daemon.read_state(data_dir), daemon.DaemonState)
    os.kill(pid, signal.SIGTERM)


def test_a_record_from_a_newer_version_reads_as_unreadable_with_its_pid_and_start_token(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "state": "draining"}))

    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid, record["start_token"])
    os.kill(pid, signal.SIGTERM)


def test_a_record_that_is_not_json_reads_as_unreadable_not_as_missing(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    (data_dir / daemon.PID_FILE).write_text("{garbled")

    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid=None)
    os.kill(pid, signal.SIGTERM)


def test_a_record_that_is_not_utf8_reads_as_unreadable(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    (data_dir / daemon.PID_FILE).write_bytes(b'{"pid": 1, \xff}')

    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid=None)
    os.kill(pid, signal.SIGTERM)


def test_a_record_whose_start_time_has_no_time_zone_is_unreadable(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "started_at": "2026-10-14T10:00:00"}))

    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid, record["start_token"])
    os.kill(pid, signal.SIGTERM)


def test_a_start_behind_one_stuck_before_its_lease_says_another_start_is_in_progress(
    data_dir: Path, tmp_path: Path
) -> None:
    first = _start(data_dir, mode="never-lease", wait_timeout=0.0)
    assert first.outcome is StartOutcome.TIMED_OUT
    [stuck] = daemon_fake_child.children_under(tmp_path)

    second = _start(data_dir, wait_timeout=0.5)

    assert second.outcome is StartOutcome.STARTING_ELSEWHERE
    assert second.starting_pid == stuck
    assert daemon_fake_child.children_under(tmp_path) == [stuck]
    assert daemon.read_state(data_dir) is None
    assert daemon.start_in_progress(data_dir) == daemon.StartInProgress(pid=stuck)


def test_no_start_in_progress_once_the_daemon_holds_its_lease(data_dir: Path) -> None:
    assert daemon.start_in_progress(data_dir) is None
    _start(data_dir)

    assert daemon.start_in_progress(data_dir) is None
    stop_within(data_dir)


# Asks `start_in_progress` over and over, as `pincer status` in a loop would, until its stdin
# closes; then prints how often it asked and how often it was told a start is in progress.
INSPECTOR = """
import sys, threading
from pathlib import Path
from pincer import daemon

closed = threading.Event()
threading.Thread(target=lambda: (sys.stdin.read(), closed.set()), daemon=True).start()
asked = seen = 0
print("asking", flush=True)
while not closed.is_set():
    asked += 1
    seen += daemon.start_in_progress(Path(sys.argv[1])) is not None
print(asked, seen, flush=True)
"""


@contextlib.contextmanager
def _inspecting(data_dir: Path) -> Iterator[list[int]]:
    """Another process asking whether a start is in progress, over and over, while the block runs;
    yields [times asked, times told one is] once the block ends."""
    inspector = subprocess.Popen(
        [sys.executable, "-c", INSPECTOR, str(data_dir)],
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    counts: list[int] = []
    try:
        assert inspector.stdout is not None and inspector.stdin is not None
        assert inspector.stdout.readline() == "asking\n"
        yield counts
        inspector.stdin.close()
        counts.extend(int(n) for n in inspector.stdout.readline().split())
    finally:
        inspector.kill()
        inspector.wait()


def test_asking_whether_a_start_is_in_progress_never_makes_a_start_say_one_is(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]

    with _inspecting(data_dir) as counts:
        outcomes = [_start(data_dir, wait_timeout=0.0).outcome for _ in range(3000)]

    asked, _ = counts
    assert asked > 3000  # it really was asking all along
    assert set(outcomes) == {StartOutcome.ALREADY_RUNNING}, outcomes.count(StartOutcome.STARTING_ELSEWHERE)
    assert daemon.read_state(data_dir).pid == pid  # type: ignore[union-attr]
    stop_within(data_dir)


def test_two_askers_never_see_a_start_in_progress_that_isnt(data_dir: Path) -> None:
    (data_dir / daemon.START_LOCK_FILE).touch()

    with _inspecting(data_dir) as counts:
        ours = [daemon.start_in_progress(data_dir) for _ in range(20000)]

    asked, theirs = counts
    assert asked > 200
    assert (ours.count(None), theirs) == (len(ours), 0)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_stops_the_daemon_where_ps_is_missing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    # A busybox image, say: who holds the lease must not depend on `ps`.
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "ps" else real_which(name, *a, **k))

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=5.0, force=force))

    assert result.outcome is (StopOutcome.KILLED if force else StopOutcome.STOPPED)
    assert result.pid == pid
    assert not alive(pid)


# From a process of its own: whether a daemon of the data dir is running, and whether a second
# daemon could take the lease.
OUTSIDE = """
import sys
from pathlib import Path
from pincer import daemon

data_dir = Path(sys.argv[1])
print(daemon.daemon_running(data_dir))
try:
    daemon.DaemonLease(data_dir, port=8080).__enter__()
except daemon.DaemonLeaseTaken:
    print("lease taken")
else:
    print("took the lease")
"""


def test_the_lease_survives_its_process_opening_and_closing_the_lock_file(data_dir: Path) -> None:
    # The daemon runs tools that may open any file, the lock file included (a Drive upload, a file
    # read): closing it again must not let go of the lease. Nor may the daemon's own probes.
    lease = daemon.DaemonLease(data_dir, port=8080)
    lease.__enter__()
    try:
        assert daemon.daemon_running(data_dir)
        assert isinstance(daemon.read_state(data_dir), daemon.DaemonState)
        assert daemon.stop(data_dir, timeout=0.2).outcome is StopOutcome.STILL_RUNNING  # never signals itself
        with (data_dir / daemon.LOCK_FILE).open("rb") as tool_reading_it:
            tool_reading_it.read()

        seen = subprocess.run(
            [sys.executable, "-c", OUTSIDE, str(data_dir)], env=child_env(), capture_output=True, text=True, check=True
        )

        assert seen.stdout.splitlines() == ["True", "lease taken"]
    finally:
        lease.__exit__(None, None, None)
        assert lease._fd is not None
        os.close(lease._fd)


# Tries, from a process of its own, to take the lock a daemon's lease takes, without waiting.
TAKE_THE_LEASE_LOCK = (
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n    print('busy')\n"
    "else:\n    print('taken')"
)


def test_no_daemon_can_take_the_lease_while_a_start_clears_the_last_ones_record_and_log(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One that did (a hand-run `pincer run --daemon-child`) would lose its fresh record and log.
    attempts: list[str] = []
    real_rotate = daemon._rotate

    def rotate(log: Path) -> None:
        tried = subprocess.run(
            [sys.executable, "-c", TAKE_THE_LEASE_LOCK, str(data_dir / daemon.LOCK_FILE)],
            capture_output=True,
            text=True,
            check=True,
        )
        attempts.append(tried.stdout.strip())
        real_rotate(log)

    monkeypatch.setattr(daemon, "_rotate", rotate)

    assert _start(data_dir).outcome is StartOutcome.READY
    assert attempts == ["busy"]
    stop_within(data_dir)


# ── start tokens (PR #242 review round 3) ──────────────────────────────────


def _started_at(token: str) -> float:
    """A start token as epoch seconds."""
    if sys.platform == "darwin":
        return float(token)
    with open("/proc/stat") as stat:
        boot = next(int(line.split()[1]) for line in stat if line.startswith("btime "))
    return boot + int(token) / os.sysconf("SC_CLK_TCK")


@pytest.mark.parametrize("name", ["python", "a) b (c"], ids=["plain", "parens and spaces in its name"])
def test_a_start_token_is_when_the_kernel_says_the_process_started(tmp_path: Path, name: str) -> None:
    # A command name can hold anything; /proc/<pid>/stat puts it in parentheses before the start time.
    program = tmp_path / name
    program.symlink_to(sys.executable)
    before = time.time()
    process = subprocess.Popen([program, "-c", "import time; time.sleep(30)"])
    after = time.time()
    try:
        token = daemon._start_token(process.pid)
        assert token is not None
        assert daemon._start_token(process.pid) == token  # it never changes

        assert before - 1 <= _started_at(token) <= after + 1, (before, token, after)
        if shutil.which("ps"):  # what `ps` shows, to the second
            shown = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
                check=True,
            ).stdout.strip()
            assert int(_started_at(token)) == int(time.mktime(time.strptime(shown, "%a %b %d %H:%M:%S %Y")))
    finally:
        process.kill()
        process.wait()


def test_a_pid_no_process_has_has_no_start_token() -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()

    with pytest.raises(ProcessLookupError):
        daemon._start_token(process.pid)


def test_without_start_tokens_the_lease_works_and_stop_signals_the_record_pid(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FreeBSD, say: nothing about the lock file may fail there, and stop works as it did before tokens.
    state = _start(data_dir).state
    assert isinstance(state, daemon.DaemonState)
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert daemon._start_token(state.pid) is None

    result = within(10.0, lambda: daemon.stop(data_dir, timeout=5.0))

    assert result == daemon.StopResult(StopOutcome.STOPPED, state.pid, daemon.Identity.UNCHECKED)
    assert not alive(state.pid)
    other = tmp_path / "foreground"
    with daemon.DaemonLease(other, port=8080) as lease:
        assert daemon.daemon_running(other)
        assert daemon.start_in_progress(other) is None
        recorded = daemon.read_state(other)
        assert isinstance(recorded, daemon.DaemonState)
        assert recorded.start_token == daemon.NO_START_TOKEN
    assert lease._fd is not None
    os.close(lease._fd)
    idle = tmp_path / "idle"
    idle.mkdir()
    (idle / daemon.LOCK_FILE).touch()
    assert not daemon.daemon_running(idle)
    assert daemon.read_state(idle) is None


@pytest.mark.parametrize("token", [7, "", "soon", "123.45", "-1"], ids=repr)
def test_a_start_token_that_isnt_one_is_dropped_from_a_record_it_cant_read(data_dir: Path, token: object) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "state": "draining", "start_token": token}))

    assert daemon.read_state(data_dir) == daemon.DaemonWithUnreadableRecord(pid, None)
    assert daemon.identify(daemon.DaemonWithUnreadableRecord(pid, None)) is daemon.Identity.UNKNOWN
    os.kill(pid, signal.SIGTERM)

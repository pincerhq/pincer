"""Tests for the daemon lifecycle module, against a fake child (tests/daemon_fake_child.py)."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import daemon_fake_child
import pytest
from daemon_support import RAW_LOG, SRC_DIR, alive, append_log, child_env, numbered, within, write_log

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

    stopped = daemon.stop(data_dir)

    assert stopped.outcome is StopOutcome.STOPPED
    assert stopped.pid == result.state.pid
    assert daemon.read_state(data_dir) is None


def test_record_goes_starting_then_ready_and_the_lock_survives_the_rewrite(data_dir: Path) -> None:
    from pincer import __version__

    child = subprocess.Popen(daemon_fake_child.argv("gated", data_dir), cwd=data_dir, env=child_env())
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

    assert not alive(pid)
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
    assert alive(result.state.pid)
    assert _wait_for_state(data_dir, "ready").pid == result.state.pid
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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

    daemon.stop(data_dir)

    assert follower.done.wait(10.0), "follow() kept going after the daemon exited"
    assert b"shutting down" in follower.data
    assert follower.data.endswith(b"RuntimeError: fake child failed during shutdown\n")


def test_follow_reads_once_more_after_seeing_the_daemon_gone(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_log(data_dir, b"first\n")
    probes: list[bool] = []
    real_lease_held = daemon._lease_held

    def lease_held(path: Path) -> bool:
        if path != data_dir:  # a follower some failed test left behind
            return real_lease_held(path)
        # The daemon writes its last line and exits between two polls.
        if probes:
            append_log(data_dir, b"final line\n")
        probes.append(not probes)
        return probes[-1]

    monkeypatch.setattr(daemon, "_lease_held", lease_held)

    followed = within(5.0, lambda: b"".join(daemon.follow(data_dir, None, until_exit=True)))

    assert followed == b"first\nfinal line\n"


def test_follow_ends_when_a_restart_rotates_its_log_inside_one_poll(data_dir: Path) -> None:
    first = _start(data_dir).state
    assert first is not None
    following = daemon.follow(data_dir, daemon.FOLLOW_LINES, until_exit=True)
    seen = next(following)
    # The follower isn't polling while the old daemon exits and a new one takes the lease and rotates the log.
    daemon.stop(data_dir)
    second = _start(data_dir).state
    assert second is not None

    seen += within(5.0, lambda: b"".join(following))

    assert f"fake child {first.pid} starting".encode() in seen
    assert seen.endswith(b"Shutdown complete\n")
    assert f"fake child {second.pid}".encode() not in seen
    daemon.stop(data_dir)


def test_follow_with_no_daemon_running_gives_the_tail_and_ends(data_dir: Path) -> None:
    _start(data_dir)
    daemon.stop(data_dir)
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
        daemon.stop(data_dir)
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
    daemon.stop(data_dir)


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


def test_stop_while_a_daemon_lingers_waits_for_it_without_signalling(data_dir: Path) -> None:
    pid = _lingering(data_dir)

    result = within(15.0, lambda: daemon.stop(data_dir, timeout=10.0))

    assert result.outcome is StopOutcome.STOPPED
    assert result.pid is None
    assert not alive(pid)
    assert "left the lease" in daemon.log_path(data_dir).read_text()


def test_a_record_this_version_cant_read_is_never_signalled(data_dir: Path) -> None:
    pid = _start(data_dir).state.pid  # type: ignore[union-attr]
    (data_dir / daemon.PID_FILE).write_text('{"pid": "not a number", "phase": "?"}')

    assert isinstance(daemon.read_state(data_dir), daemon.DaemonWithoutRecord)
    result = within(10.0, lambda: daemon.stop(data_dir, timeout=1.0))

    assert result.outcome is StopOutcome.STILL_RUNNING
    assert result.pid is None
    assert alive(pid)
    os.kill(pid, signal.SIGTERM)


def test_a_newer_record_with_extra_fields_still_reads(data_dir: Path) -> None:
    state = _start(data_dir).state
    assert state is not None
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "added_in_a_later_version": True}))

    assert daemon.read_state(data_dir) == state
    daemon.stop(data_dir)


def test_within_reraises_what_the_call_raised() -> None:
    def fail() -> None:
        raise LookupError("from the worker")

    with pytest.raises(LookupError, match="from the worker"):
        within(5.0, fail)


def test_a_spawned_daemon_imports_the_tree_under_test(data_dir: Path) -> None:
    _start(data_dir)

    assert f"imports pincer from {SRC_DIR / 'pincer' / '__init__.py'}" in daemon.log_path(data_dir).read_text()
    daemon.stop(data_dir)


@pytest.mark.parametrize("bad_pid", [0, -1], ids=["pid 0", "pid -1"])
def test_a_record_naming_no_single_process_is_unreadable_and_never_signalled(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, bad_pid: int
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

    assert isinstance(daemon.read_state(data_dir), daemon.DaemonWithoutRecord)
    for force in (False, True):
        result = within(10.0, lambda force=force: daemon.stop(data_dir, timeout=0.5, force=force))
        assert result.outcome is StopOutcome.STILL_RUNNING
        assert result.pid is None
    assert attempted == []
    assert alive(state.pid)
    real_kill(state.pid, signal.SIGTERM)


def test_a_daemon_that_exits_during_the_record_grace_reads_as_gone_at_once(data_dir: Path) -> None:
    # A lease holder with no record, as at the end of DaemonLease.__exit__, letting go of the lock
    # (exiting) well inside the grace read_state gives a missing record.
    (data_dir / daemon.LOCK_FILE).touch()
    fd = os.open(data_dir / daemon.LOCK_FILE, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    exits = threading.Timer(0.2, os.close, (fd,))
    exits.start()
    try:
        began = time.monotonic()
        state = daemon.read_state(data_dir)
        took = time.monotonic() - began
    finally:
        exits.join()

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
    daemon.stop(data_dir)


@pytest.mark.parametrize("force", [False, True], ids=["stop", "force"])
def test_stop_never_signals_a_pid_that_started_after_the_daemon_took_its_lease(
    data_dir: Path, tmp_path: Path, force: bool
) -> None:
    # The record names a process that started after the lease was taken: a pid reused once the
    # daemon it named had gone. While another daemon still holds the lease, stop must leave it be.
    state = _start(data_dir, mode="ignore-sigterm").state
    assert isinstance(state, daemon.DaemonState)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        earlier = (datetime.fromisoformat(state.started_at) - timedelta(minutes=5)).isoformat()
        record = replace(state, pid=bystander.pid, started_at=earlier)
        (data_dir / daemon.PID_FILE).write_text(json.dumps(asdict(record)))

        result = within(10.0, lambda: daemon.stop(data_dir, timeout=0.5, force=force))

        assert result.outcome is StopOutcome.STILL_RUNNING
        assert result.pid is None
        time.sleep(0.2)  # a signal sent on the way would have landed by now
        assert bystander.poll() is None
        assert alive(state.pid)
    finally:
        bystander.kill()
        bystander.wait()

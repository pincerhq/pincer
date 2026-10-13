"""Background ("daemon") lifecycle for a local Pincer.

`pincer run -d` calls `start()`, which re-spawns `pincer run --daemon-child` in a new
session with its stdout/stderr appended to `<data_dir>/logs/pincer.log`. The child holds a
`DaemonLease` for its whole life: an exclusive `flock` on `<data_dir>/pincer.lock` plus a
JSON record in `<data_dir>/pincer.pid`.

"A daemon is running" means "a probe cannot take the lock". Only the kernel drops the
lock, as the process exits (`kill -9` included), so a released lock means the process is
gone and a record left behind by a dead daemon is recognisable as stale: its PID is never
trusted. The lock sits on a sibling file rather than on
the record because the record is rewritten by rename, which replaces its inode: a lock
on the old inode would no longer be seen by a probe opening the path. The lock file is
never replaced or deleted for the same reason.

No Typer or Rich here — the CLI commands only format what these functions return.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess  # nosec B404 - re-spawns our own CLI with a fixed argv, no shell
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path
    from types import TracebackType

PID_FILE = "pincer.pid"
LOCK_FILE = "pincer.lock"
# Serialises concurrent starts; see start().
START_LOCK_FILE = "pincer.start.lock"
LOG_FILE = "pincer.log"
# Previous sessions kept by start-time rotation: pincer.log.1 (newest) … pincer.log.5.
LOG_KEEP = 5
TAIL_LINES = 20
POLL_INTERVAL = 0.1
# The lease writes the record right after taking the lock; a probe that wins that
# race waits this long for the record to appear.
RECORD_GRACE = 1.0


class DaemonLeaseTaken(RuntimeError):
    """Another daemon already holds the lease for this data directory."""


class StartOutcome(StrEnum):
    READY = "ready"
    ALREADY_RUNNING = "already_running"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"


class StopOutcome(StrEnum):
    NOT_RUNNING = "not_running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class DaemonState:
    """The daemon record, as the live daemon last wrote it."""

    pid: int
    started_at: str
    cwd: str
    port: int
    version: str
    state: str  # "starting" | "ready"
    channels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StartResult:
    outcome: StartOutcome
    state: DaemonState | None = None
    last_lines: list[str] = field(default_factory=list)
    exit_code: int | None = None


@dataclass(frozen=True)
class StopResult:
    outcome: StopOutcome
    pid: int | None = None


def log_path(data_dir: Path) -> Path:
    """The current daemon session's log file (matches `Settings.log_dir`)."""
    return data_dir / "logs" / LOG_FILE


def start(
    data_dir: Path,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    wait_timeout: float,
    on_line: Callable[[str], None] | None = None,
) -> StartResult:
    """Rotate the log, spawn `argv` as a daemon in its own session, and wait until it marks itself ready.

    `argv` must start a process that enters `DaemonLease` on `data_dir`. It runs in `cwd`
    with `env`, stdin from /dev/null and stdout/stderr appended to the log.
    """
    log = log_path(data_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    (data_dir / LOCK_FILE).touch(mode=0o600)
    # One start at a time, from the probe until the new child holds the lease. Without it a
    # second start landing in that window (the child is still importing) would see the lease
    # free, rotate the first child's fresh log away and spawn a rival.
    with _StartLock(data_dir / START_LOCK_FILE) as starting:
        return _start_locked(data_dir, log, argv, cwd, env, wait_timeout, on_line, starting)


def _start_locked(
    data_dir: Path,
    log: Path,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    wait_timeout: float,
    on_line: Callable[[str], None] | None,
    starting: _StartLock,
) -> StartResult:
    # Holding the lease lock while cleaning up means no daemon can be writing the record or the log.
    with _lock(data_dir) as held:
        if not held:
            return StartResult(StartOutcome.ALREADY_RUNNING, read_state(data_dir))
        (data_dir / PID_FILE).unlink(missing_ok=True)
        _rotate(log)

    log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(  # nosec B603 - argv is built by the caller, never from user input
            list(argv),
            cwd=cwd,
            # A block-buffered stdout would hold the banner back from the log.
            env={**env, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
    finally:
        os.close(log_fd)

    # Readiness comes from the record alone, never from the dashboard port: the API binds
    # before the channels have started.
    deadline = time.monotonic() + wait_timeout
    with _LogLines(log, on_line) as lines:
        while True:
            state = read_state(data_dir)
            lines.relay()
            if state is not None and state.pid == child.pid:
                # The child holds the lease now; a concurrent start sees it and stands down.
                starting.release()
            if state is not None and state.pid == child.pid and state.state == "ready":
                return StartResult(StartOutcome.READY, state)
            exit_code = child.poll()
            if exit_code is not None:
                lines.relay(final=True)
                return StartResult(StartOutcome.CRASHED, last_lines=_tail(log, TAIL_LINES), exit_code=exit_code)
            if time.monotonic() >= deadline:
                return StartResult(StartOutcome.TIMED_OUT, state)
            time.sleep(POLL_INTERVAL)


def read_state(data_dir: Path) -> DaemonState | None:
    """The live daemon's record, or None when no daemon holds the lease (a leftover record is ignored)."""
    with _lock(data_dir) as held:
        if held:
            return None
    deadline = time.monotonic() + RECORD_GRACE
    while True:
        with contextlib.suppress(FileNotFoundError, ValueError, TypeError):
            return DaemonState(**json.loads((data_dir / PID_FILE).read_text()))
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL / 5)


def stop(data_dir: Path) -> StopResult:
    """SIGTERM the daemon (the agent's graceful-shutdown path) and wait until it has exited."""
    state = read_state(data_dir)
    if state is None:
        return StopResult(StopOutcome.NOT_RUNNING)
    with contextlib.suppress(ProcessLookupError):
        os.kill(state.pid, signal.SIGTERM)
    while _lease_held(data_dir):
        time.sleep(POLL_INTERVAL)
    return StopResult(StopOutcome.STOPPED, state.pid)


class DaemonLease:
    """Held by the daemonized child for its whole life: the lock, plus the record describing it.

    Entering takes the lock (raising `DaemonLeaseTaken` if another daemon has it) and
    writes `state: "starting"`; `mark_ready` rewrites it as `"ready"` with the channels.
    Leaving removes the record but keeps the lock: whatever runs after it (a traceback
    being printed, interpreter teardown) is still the daemon, and `stop()` or a follower
    must not see it as gone until the process has exited.
    """

    def __init__(self, data_dir: Path, port: int) -> None:
        self._data_dir = data_dir
        self._port = port
        self._fd: int | None = None
        self._started_at = ""

    def __enter__(self) -> DaemonLease:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._data_dir / LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DaemonLeaseTaken(f"another Pincer daemon holds {self._data_dir / LOCK_FILE}") from None
        # os.open fds are non-inheritable, so the lock dies with this process, not with
        # whatever it spawns (MCP servers, sandboxed skills).
        self._fd = fd
        self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._write("starting", [])
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        (self._data_dir / PID_FILE).unlink(missing_ok=True)

    def mark_ready(self, channels: Sequence[str]) -> None:
        self._write("ready", list(channels))

    def _write(self, state: str, channels: list[str]) -> None:
        from pincer import __version__

        record = DaemonState(
            pid=os.getpid(),
            started_at=self._started_at,
            cwd=os.getcwd(),
            port=self._port,
            version=__version__,
            state=state,
            channels=channels,
        )
        tmp = self._data_dir / f"{PID_FILE}.tmp"
        tmp.write_text(json.dumps(asdict(record)))
        os.replace(tmp, self._data_dir / PID_FILE)


@contextlib.contextmanager
def _lock(data_dir: Path) -> Iterator[bool]:
    """Try the lease lock without blocking; yields True (and holds it) when no daemon has it."""
    lock_file = data_dir / LOCK_FILE
    if not lock_file.exists():
        yield True
        return
    fd = os.open(lock_file, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _lease_held(data_dir: Path) -> bool:
    with _lock(data_dir) as free:
        return not free


def _rotate(log: Path) -> None:
    """pincer.log → .1, .1 → .2, …; the oldest beyond LOG_KEEP is dropped."""
    log.with_name(f"{log.name}.{LOG_KEEP}").unlink(missing_ok=True)
    for n in range(LOG_KEEP - 1, 0, -1):
        older = log.with_name(f"{log.name}.{n}")
        if older.exists():
            older.rename(log.with_name(f"{log.name}.{n + 1}"))
    if log.exists():
        log.rename(log.with_name(f"{log.name}.1"))


class _StartLock:
    """A blocking exclusive `flock`, releasable before the `with` block ends (idempotently)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> _StartLock:
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:  # e.g. Ctrl+C while queued behind another start
            self.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class _LogLines:
    """Reads a growing log from its current start, handing each complete line to `on_line`."""

    def __init__(self, path: Path, on_line: Callable[[str], None] | None) -> None:
        self._path = path
        self._on_line = on_line
        self._pending = b""

    def __enter__(self) -> _LogLines:
        self._file = self._path.open("rb")
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()

    def relay(self, final: bool = False) -> None:
        """Pass on the lines written since the last call; `final` also flushes an unterminated last line."""
        self._pending += self._file.read()
        *complete, self._pending = self._pending.split(b"\n")
        if final and self._pending:
            complete.append(self._pending)
            self._pending = b""
        if self._on_line is not None:
            for raw in complete:
                self._on_line(raw.decode(errors="replace").rstrip("\r"))


def _tail(path: Path, lines: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []

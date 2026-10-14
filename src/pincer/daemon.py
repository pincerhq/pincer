"""Background ("daemon") lifecycle for a local Pincer.

`pincer run -d` calls `start()`, which re-spawns `pincer run --daemon-child` in a new
session with its stdout/stderr appended to `<data_dir>/logs/pincer.log`. The child holds a
`DaemonLease` for its whole life: an exclusive `flock` on `<data_dir>/pincer.lock` plus a
JSON record in `<data_dir>/pincer.pid`.

The lease is the only exclusive lock ever taken on that file. Everything else (status, stop, a
start's cleanup, its readiness poll) probes it shared, so probes never conflict with each other
and fail exactly when a daemon holds it: "a daemon is running" means "a shared probe fails". Only the kernel drops the
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
import shutil
import signal
import subprocess  # nosec B404 - re-spawns our own CLI with a fixed argv, no shell
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
    from pathlib import Path
    from types import TracebackType
    from typing import BinaryIO

PID_FILE = "pincer.pid"
LOCK_FILE = "pincer.lock"
# Serialises concurrent starts; see start().
START_LOCK_FILE = "pincer.start.lock"
LOG_FILE = "pincer.log"
# Previous sessions kept by start-time rotation: pincer.log.1 (newest) … pincer.log.5.
LOG_KEEP = 5
TAIL_LINES = 20
# Where `pincer logs -f` (and attaching) starts: this many lines back.
FOLLOW_LINES = 50
# Logs are read in blocks of this size, backward to find a tail and forward to follow.
READ_BLOCK = 64 * 1024
POLL_INTERVAL = 0.1
# The lease writes the record right after taking the lock; a probe that wins that
# race waits this long for the record to appear.
RECORD_GRACE = 1.0
# A starting daemon keeps reaching for the lease this long while shared probes hold it.
LEASE_GRACE = 2.0
LEASE_RETRY = 0.01
# Tells a spawned daemon which inherited fd holds the start lock, so it can let go of it the
# moment it holds the lease (see start()).
START_LOCK_FD_ENV = "PINCER_DAEMON_START_LOCK_FD"


class DaemonLeaseTaken(RuntimeError):
    """Another daemon already holds the lease for this data directory."""


class StartOutcome(StrEnum):
    READY = "ready"
    ALREADY_RUNNING = "already_running"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"


class DaemonPhase(StrEnum):
    """Where a live daemon is, as its record says: before or after its "is running!" banner."""

    STARTING = "starting"
    READY = "ready"


class StopOutcome(StrEnum):
    NOT_RUNNING = "not_running"
    STOPPED = "stopped"
    KILLED = "killed"
    STILL_RUNNING = "still_running"


@dataclass(frozen=True)
class DaemonState:
    """The daemon record, as the live daemon last wrote it."""

    pid: int
    started_at: str
    cwd: str
    port: int
    version: str
    state: DaemonPhase
    channels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DaemonWithoutRecord:
    """A daemon holds the lease, but there is no record this version can read.

    Either it has left its lease and is exiting (the lease removes the record first, the kernel
    drops the lock last), or it wrote a record this version can't parse. Its pid is unknown, so
    nothing may signal it; it can only be waited for.
    """


@dataclass(frozen=True)
class DaemonWithUnreadableRecord:
    """A daemon holds the lease and has a record, but not one this version can read (a newer
    version's phase, a malformed field). Never signalled; `pid` is shown only, when the record
    names one that could be a process."""

    pid: int | None


# What read_state says of a data dir whose lease is held.
type LiveDaemon = DaemonState | DaemonWithoutRecord | DaemonWithUnreadableRecord


@dataclass(frozen=True)
class StartResult:
    outcome: StartOutcome
    state: LiveDaemon | None = None
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
    # free, rotate the first child's fresh log away and spawn a rival. The child inherits the
    # lock too and lets go of it once it holds the lease, so it stays held even when this
    # start stops waiting first (a timeout, Ctrl+C) — and goes when the child dies.
    starting = _StartLock(data_dir / START_LOCK_FILE)
    if not starting.acquire(wait_timeout):
        # Another start's child hasn't reached the lease within our wait.
        return StartResult(StartOutcome.TIMED_OUT, read_state(data_dir))
    try:
        return _start_locked(data_dir, log, argv, cwd, env, wait_timeout, on_line, starting)
    finally:
        starting.release()


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
    # Holding the lock shared while cleaning up means no daemon holds the lease, and none can
    # take it (a lease waits) until the stale record and the old log are out of the way.
    with _probe(data_dir) as free:
        if not free:
            return StartResult(StartOutcome.ALREADY_RUNNING, read_state(data_dir))
        (data_dir / PID_FILE).unlink(missing_ok=True)
        _rotate(log)

    start_fd = starting.fd
    if start_fd is None:  # start() only gets here holding it
        raise RuntimeError("the start lock isn't held")
    log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(  # nosec B603 - argv is built by the caller, never from user input
            list(argv),
            cwd=cwd,
            # A block-buffered stdout would hold the banner back from the log.
            env={**env, "PYTHONUNBUFFERED": "1", START_LOCK_FD_ENV: str(start_fd)},
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            pass_fds=(start_fd,),
        )
    finally:
        os.close(log_fd)

    # Readiness comes from the record alone, never from the dashboard port: the API binds
    # before the channels have started.
    deadline = time.monotonic() + wait_timeout
    with log.open("rb") as log_file:
        lines = _LogLines(log_file)
        while True:
            state = read_state(data_dir)
            lines.relay(on_line)
            ours = isinstance(state, DaemonState) and state.pid == child.pid
            if ours:
                # The child holds the lease (and has let go of its copy of the start lock).
                starting.release()
            if ours and isinstance(state, DaemonState) and state.state is DaemonPhase.READY:
                return StartResult(StartOutcome.READY, state)
            exit_code = child.poll()
            if exit_code is not None:
                lines.relay(on_line, final=True)
                return StartResult(StartOutcome.CRASHED, last_lines=_tail(log, TAIL_LINES), exit_code=exit_code)
            if time.monotonic() >= deadline:
                return StartResult(StartOutcome.TIMED_OUT, state)
            time.sleep(POLL_INTERVAL)


def read_state(data_dir: Path) -> LiveDaemon | None:
    """The live daemon's record; DaemonWithUnreadableRecord if one holds the lease and has a record
    this version can't read; DaemonWithoutRecord if one holds the lease and has none (it is
    exiting); None when no daemon holds the lease (a leftover record is ignored)."""
    with _probe(data_dir) as free:
        if free:
            return None
    deadline = time.monotonic() + RECORD_GRACE
    while True:
        record = _read_record(data_dir / PID_FILE)
        if record is not None:
            return record
        # The usual way to be here is a daemon past its lease, exiting: once it has, nothing runs.
        if not _lease_held(data_dir):
            return None
        if time.monotonic() >= deadline:
            return DaemonWithoutRecord()
        time.sleep(POLL_INTERVAL / 5)


def _read_record(path: Path) -> DaemonState | DaemonWithUnreadableRecord | None:
    """The record at `path`; DaemonWithUnreadableRecord if it can't be read; None if there is none.
    Fields a later version added are ignored, so an upgrade doesn't turn a running daemon into an
    unreadable one. (The lease replaces the record whole, so there is no half-written one to see.)

    Every field must have its type, and `started_at` must be an ISO time, or the record counts as
    unreadable: what status and stop show or compute from it can't then fail half way. A pid that
    names no single process is unreadable too: `os.kill(0, …)` would signal the caller's own
    process group, and `os.kill(-1, …)` every process the user may signal."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return DaemonWithUnreadableRecord(pid=None)
    try:
        known = {field.name for field in fields(DaemonState)}
        state = DaemonState(**{key: value for key, value in record.items() if key in known})
        if not _is_int(state.pid) or state.pid <= 0 or not _is_int(state.port):
            raise ValueError("pid or port")
        if not all(isinstance(text, str) for text in (state.started_at, state.cwd, state.version)):
            raise ValueError("started_at, cwd or version")
        if not isinstance(state.channels, list) or not all(isinstance(name, str) for name in state.channels):
            raise ValueError("channels")
        datetime.fromisoformat(state.started_at)
        return replace(state, state=DaemonPhase(state.state))
    except (ValueError, TypeError, AttributeError):
        pid = record.get("pid") if isinstance(record, dict) else None
        return DaemonWithUnreadableRecord(pid=pid if _is_int(pid) and pid > 0 else None)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def follow(data_dir: Path, from_lines: int | None, until_exit: bool) -> Generator[bytes]:
    """The daemon log from its last `from_lines` lines (all of it for None), raw, in chunks of whole lines.

    With `until_exit` it then follows what the daemon writes until the daemon has exited, or until a
    new start has rotated the log away (the session being followed is over either way). An
    unterminated last line comes out only once nothing more can follow. A missing log yields nothing.
    """
    path = log_path(data_dir)
    try:
        log = path.open("rb")
    except FileNotFoundError:
        return
    with log:
        size = os.fstat(log.fileno()).st_size
        lines = _LogLines(log, start_at=0 if from_lines is None else _tail_offset(log, size, from_lines))
        while True:
            # Look before reading: the lock is released only as the process exits, so once it is
            # free every line the daemon wrote is already in the file, and this read gets them all.
            ended = not until_exit or not _lease_held(data_dir) or _rotated(path, log)
            yield from lines.read(final=ended)
            if ended:
                return
            time.sleep(POLL_INTERVAL)


def decode_lines(chunk: bytes) -> list[str]:
    """A chunk of whole log lines (as `follow` yields them) as text lines, for display: no line
    endings, and bytes that aren't UTF-8 replaced."""
    return [raw.decode(errors="replace").rstrip("\r") for raw in chunk.removesuffix(b"\n").split(b"\n")]


def stop(
    data_dir: Path,
    timeout: float | None = None,
    force: bool = False,
    on_wait: Callable[[LiveDaemon, float], None] | None = None,
) -> StopResult:
    """SIGTERM the daemon (the agent's graceful-shutdown path) and wait until it has exited.

    Gives up after `timeout` seconds with STILL_RUNNING and leaves it running: a call drain
    must never be cut short behind the operator's back. Only `force` kills: SIGKILL, at once, to
    the daemon's whole process group, so no MCP server or skill it spawned outlives it holding a
    port or file. A plain stop signals the daemon alone; its own shutdown stops those children.
    `on_wait(state, elapsed)` is called right after signalling and then on every poll.

    A daemon without a readable record is never signalled, `force` or not (its pid is unknown or
    can't be trusted); stop only waits for it, and reports no pid. Nor is the record's pid signalled unless, right
    before, the lease is still held and that pid's process started no later than the daemon took
    the lease: a process that reused the pid of a daemon since gone started after it.
    """
    state = read_state(data_dir)
    if state is None:
        return StopResult(StopOutcome.NOT_RUNNING)
    pid = state.pid if isinstance(state, DaemonState) else None
    if pid is not None and isinstance(state, DaemonState):
        if not _lease_held(data_dir):  # it exited on its own since the record was read
            return StopResult(StopOutcome.STOPPED, pid)
        if not _started_by(pid, state.started_at):
            pid = None
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            if force:
                _kill_group(pid)
            else:
                os.kill(pid, signal.SIGTERM)
    began = time.monotonic()
    while True:
        elapsed = time.monotonic() - began
        if on_wait is not None:
            on_wait(state, elapsed)
        if not _lease_held(data_dir):
            killed = force and pid is not None
            return StopResult(StopOutcome.KILLED if killed else StopOutcome.STOPPED, pid)
        if timeout is not None and elapsed >= timeout:
            return StopResult(StopOutcome.STILL_RUNNING, pid)
        time.sleep(POLL_INTERVAL)


def _started_by(pid: int, started_at: str) -> bool:
    """True when process `pid` started no later than `started_at` (the second the daemon took its
    lease), so it can be that daemon and not a later process that reused its pid. False when its
    start time can't be read (it is gone, or `ps` is missing)."""
    began = _process_start(pid)
    if began is None:
        return False
    try:
        lease_taken = datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return False
    # Both are whole seconds: `ps` prints them so, and the lease writes them so.
    return int(began) <= int(lease_taken)


def _process_start(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), as `ps -o lstart=` reports it; None if it can't tell."""
    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        shown = subprocess.run(  # nosec B603 - a fixed command on an int pid, no shell
            [ps, "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            # English month names for strptime; the same local time zone as mktime below.
            env={**os.environ, "LC_ALL": "C"},
        ).stdout.strip()
        return time.mktime(time.strptime(shown, "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _kill_group(pid: int) -> None:
    """SIGKILL the process group `pid` leads (a daemon leads its own session), or `pid` alone if it doesn't."""
    if os.getpgid(pid) == pid:
        os.killpg(pid, signal.SIGKILL)
    else:  # not started by start(), e.g. a hand-run `pincer run --daemon-child`
        os.kill(pid, signal.SIGKILL)


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
        # A shared probe (`pincer status`, a start's poll) holds the lock for a moment at a time; only
        # a lock still taken after LEASE_GRACE belongs to another daemon. Non-blocking tries, not a
        # blocking flock: that could wait on a live daemon for ever.
        deadline = time.monotonic() + LEASE_GRACE
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise DaemonLeaseTaken(f"another Pincer daemon holds {self._data_dir / LOCK_FILE}") from None
                time.sleep(LEASE_RETRY)
        # os.open fds are non-inheritable, so the lock dies with this process, not with
        # whatever it spawns (MCP servers, sandboxed skills).
        self._fd = fd
        _let_go_of_the_start_lock()
        self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._write(DaemonPhase.STARTING, [])
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        (self._data_dir / PID_FILE).unlink(missing_ok=True)

    def mark_ready(self, channels: Sequence[str]) -> None:
        self._write(DaemonPhase.READY, list(channels))

    def _write(self, state: DaemonPhase, channels: list[str]) -> None:
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


def _let_go_of_the_start_lock() -> None:
    """Close the start lock `start()` handed this process, if it did: holding the lease, it is
    no longer needed to keep a rival start out."""
    inherited = os.environ.pop(START_LOCK_FD_ENV, None)
    if inherited is not None:
        with contextlib.suppress(ValueError, OSError):
            os.close(int(inherited))


@contextlib.contextmanager
def _probe(data_dir: Path) -> Iterator[bool]:
    """Try the lock file shared, without blocking; yields True (holding it) when no daemon holds the lease."""
    lock_file = data_dir / LOCK_FILE
    if not lock_file.exists():
        yield True
        return
    fd = os.open(lock_file, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _lease_held(data_dir: Path) -> bool:
    with _probe(data_dir) as free:
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
    """An exclusive `flock` on the start lock file, waited for up to a deadline, and releasable early."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.fd: int | None = None

    def acquire(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds (at least one try) for the lock; False if another start still has it."""
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        return False
                    time.sleep(POLL_INTERVAL / 5)
        except BaseException:  # e.g. Ctrl+C while queued behind another start
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _LogLines:
    """Reads a growing log onwards from `start_at`, in whole lines."""

    def __init__(self, file: BinaryIO, start_at: int = 0) -> None:
        self._file = file
        self._file.seek(start_at)
        self._pending = b""

    def read(self, final: bool = False) -> Iterator[bytes]:
        """The lines written since the last call, raw, a block at a time; `final` adds an unterminated last line."""
        while block := self._file.read(READ_BLOCK):
            complete, newline, self._pending = (self._pending + block).rpartition(b"\n")
            if newline:
                yield complete + newline
        if final and self._pending:
            yield self._pending
            self._pending = b""

    def relay(self, on_line: Callable[[str], None] | None, final: bool = False) -> None:
        """Pass on the lines written since the last call, decoded, to `on_line`."""
        for chunk in self.read(final):
            if on_line is not None:
                for line in decode_lines(chunk):
                    on_line(line)


def _tail_offset(file: BinaryIO, size: int, lines: int) -> int:
    """Where the last `lines` lines of `file` begin, found reading backward from its end a block at a time."""
    if lines == 0:
        return size
    end = size
    if size:
        file.seek(size - 1)
        if file.read(1) == b"\n":
            end -= 1  # a newline ending the file closes the last line rather than starting another
    newlines = 0
    while end > 0:
        begin = max(end - READ_BLOCK, 0)
        file.seek(begin)
        block = file.read(end - begin)
        at = len(block)
        while (at := block.rfind(b"\n", 0, at)) != -1:
            newlines += 1
            if newlines == lines:
                return begin + at + 1
        end = begin
    return 0


def _rotated(path: Path, file: BinaryIO) -> bool:
    """True once `path` no longer names the file `file` has open (a start rotated it away)."""
    try:
        return os.stat(path).st_ino != os.fstat(file.fileno()).st_ino
    except FileNotFoundError:
        return True


def _tail(path: Path, lines: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []

"""Background ("daemon") lifecycle for a local Pincer.

`pincer run -d` calls `start()`, which re-spawns `pincer run --daemon-child` in a new
session with its stdout/stderr appended to `<data_dir>/logs/pincer.log`. The child holds a
`DaemonLease` for its whole life: an exclusive `flock` on `<data_dir>/pincer.lock` plus a
JSON record in `<data_dir>/pincer.pid`.

The lease is the only exclusive lock ever taken on that file. Everything else (status, stop, a
start's cleanup, its readiness poll) probes it shared, so probes never conflict with each other
and fail exactly when a daemon holds it: "a daemon is running" means "a shared probe fails". Only
the kernel drops the lock, as the process exits (`kill -9` included); closing some other
descriptor of the file, as a tool the agent runs might, never does. A record left behind by a dead
daemon is so recognisable as stale. The lock sits on a sibling file rather than on the record
because the record is rewritten by rename, which replaces its inode: a lock on the old inode would
no longer be seen by a probe opening the path. The lock file is never replaced or deleted for the
same reason.

A held lock doesn't say which process holds it. The record names the daemon's pid and the exact
time the kernel says it started (its start token); `stop` signals that pid only while the lease is
held and the process with that pid has that token, so a process that reused the pid of a daemon
since gone is never signalled. On a platform without start tokens (anything but Linux and macOS),
or by a daemon that couldn't read its own (its record says so), the record's pid is signalled on
the lease alone.

No Typer or Rich here — the CLI commands only format what these functions return.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import struct
import subprocess  # nosec B404 - re-spawns our own CLI with a fixed argv, no shell
import sys
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
# macOS `sysctl` names for one process's `struct kinfo_proc`, and that struct's size (64-bit). Its
# first member, `struct extern_proc`, opens with `p_starttime`, a `struct timeval`: verified against
# `ps -o lstart` by test_a_start_token_is_when_the_kernel_says_the_process_started.
CTL_KERN, KERN_PROC, KERN_PROC_PID = 1, 14, 1
KINFO_PROC_SIZE = 648
# What _start_token returns: clock ticks on Linux, `seconds.microseconds` on macOS.
START_TOKEN_SHAPE = re.compile(r"\d+(\.\d{6})?")
# A daemon's start token when it couldn't read one (a platform without them, no readable /proc): its
# record's pid is then signalled on the lease alone. Never the same as a record without the field.
NO_START_TOKEN = "none"
# A start waits at least this long for the start lock, however short its own wait: someone asking
# whether a start is in progress holds it for an instant.
START_LOCK_GRACE = 0.2
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
    STARTING_ELSEWHERE = "starting_elsewhere"


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
    # When the kernel says the daemon's process started (see _start_token); NO_START_TOKEN if the daemon
    # couldn't read its own; None in a record from before start tokens.
    start_token: str | None = None


@dataclass(frozen=True)
class DaemonWithoutRecord:
    """A daemon holds the lease but has no record: it has left its lease and is exiting (the lease
    removes the record first, the kernel drops the lock last). Its pid is unknown."""

    pid: None = None


@dataclass(frozen=True)
class DaemonWithUnreadableRecord:
    """A daemon holds the lease and has a record, but not one this version can read (a newer
    version's phase, a malformed field). `pid` and `start_token` are what the record names, where
    they still parse: unverified until `stop` checks them as it does a readable record's."""

    pid: int | None
    start_token: str | None = None


# What read_state says of a data dir whose lease is held.
type LiveDaemon = DaemonState | DaemonWithoutRecord | DaemonWithUnreadableRecord


@dataclass(frozen=True)
class StartResult:
    outcome: StartOutcome
    state: LiveDaemon | None = None
    last_lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    # STARTING_ELSEWHERE: the child of the start in progress, when that start has recorded it.
    starting_pid: int | None = None


@dataclass(frozen=True)
class StartInProgress:
    """A start holds the start lock, its child not yet in the lease (`pid`, once the start has spawned it)."""

    pid: int | None


class Identity(StrEnum):
    """Whether `stop` could tell that the pid its record names is the daemon's, right before signalling."""

    # The process with that pid has the start token the daemon recorded: signalled.
    VERIFIED = "verified"
    # No start token to check (a platform without them, or a daemon that couldn't read its own):
    # signalled on the lease alone (see the module docstring).
    UNCHECKED = "unchecked"
    # The pid now names another process (the record's daemon is gone): never signalled.
    NOT_PINCER = "not_pincer"
    # No process has that pid any more: nothing to signal.
    GONE = "gone"
    # No readable record, or one without a start token: nothing to check a pid against, so never signalled.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StopTarget:
    """The daemon `stop` found holding the lease, and whether it could tell its process."""

    state: LiveDaemon
    identity: Identity

    @property
    def pid(self) -> int | None:
        """The pid the record names (unverified unless `signalled`)."""
        return self.state.pid

    @property
    def signalled(self) -> bool:
        return self.identity in (Identity.VERIFIED, Identity.UNCHECKED)

    @property
    def waited_for(self) -> bool:
        """Whether `stop` waits for it to exit: once signalled, or when it is already exiting (it has
        left its lease). Anything else would hold the lease just as long after any wait."""
        return self.signalled or isinstance(self.state, DaemonWithoutRecord)


@dataclass(frozen=True)
class StopResult:
    outcome: StopOutcome
    # The pid the record names; whether it was signalled is `identity`'s to say.
    pid: int | None = None
    identity: Identity | None = None

    @property
    def signalled(self) -> bool:
        return self.identity in (Identity.VERIFIED, Identity.UNCHECKED)


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
        # Another start's child hasn't reached the lease within our wait: spawn nothing, name it.
        elsewhere = start_in_progress(data_dir)
        return StartResult(StartOutcome.STARTING_ELSEWHERE, starting_pid=elsewhere.pid if elsewhere else None)
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
    # Name the child in the start lock file, for whoever finds the lock held (status, a later start).
    os.pwrite(start_fd, str(child.pid).encode(), 0)

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


def start_in_progress(data_dir: Path) -> StartInProgress | None:
    """The start holding the start lock, if one does: its child hasn't taken the lease yet (it may
    be stuck before it). None when no start is in progress."""
    path = data_dir / START_LOCK_FILE
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        try:
            # Shared: askers never collide with each other, only with a start holding it.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            recorded = os.pread(fd, 32, 0).decode(errors="replace").strip()
            return StartInProgress(pid=int(recorded) if recorded.isdigit() and int(recorded) > 0 else None)
        return None
    finally:
        os.close(fd)


def read_state(data_dir: Path) -> LiveDaemon | None:
    """The live daemon's record; DaemonWithUnreadableRecord if one holds the lease and has a record
    this version can't read; DaemonWithoutRecord if one holds the lease and has none (it is
    exiting); None when no daemon holds the lease (a leftover record is ignored)."""
    if not daemon_running(data_dir):
        return None
    deadline = time.monotonic() + RECORD_GRACE
    while True:
        record = _read_record(data_dir / PID_FILE)
        if record is not None:
            return record
        # The usual way to be here is a daemon past its lease, exiting: once it has, nothing runs.
        if not daemon_running(data_dir):
            return None
        if time.monotonic() >= deadline:
            return DaemonWithoutRecord()
        time.sleep(POLL_INTERVAL / 5)


def _read_record(path: Path) -> DaemonState | DaemonWithUnreadableRecord | None:
    """The record at `path`; DaemonWithUnreadableRecord if it can't be read; None if there is none.
    Fields a later version added are ignored, so an upgrade doesn't turn a running daemon into an
    unreadable one. (The lease replaces the record whole, so there is no half-written one to see.)

    Every field must have its type, and `started_at` an ISO time with a time zone, or the record counts as
    unreadable: what status and stop show or compute from it can't then fail half way. A pid that
    names no single process is unreadable too: `os.kill(0, …)` would signal the caller's own
    process group, and `os.kill(-1, …)` every process the user may signal."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:  # not UTF-8: corrupted, or a partial copy
        return DaemonWithUnreadableRecord(pid=None)
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
        if state.start_token is not None and not isinstance(state.start_token, str):
            raise ValueError("start_token")
        if datetime.fromisoformat(state.started_at).tzinfo is None:  # uptime needs an absolute time
            raise ValueError("started_at")
        return replace(state, state=DaemonPhase(state.state))
    except (ValueError, TypeError, AttributeError):
        if not isinstance(record, dict):
            return DaemonWithUnreadableRecord(pid=None)
        pid, token = record.get("pid"), record.get("start_token")
        return DaemonWithUnreadableRecord(
            pid=pid if _is_int(pid) and pid > 0 else None,
            start_token=token if token == NO_START_TOKEN or _is_start_token(token) else None,
        )


def _is_start_token(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and START_TOKEN_SHAPE.fullmatch(value) is not None


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
            ended = not until_exit or not daemon_running(data_dir) or _rotated(path, log)
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
    on_wait: Callable[[StopTarget, float], None] | None = None,
) -> StopResult:
    """SIGTERM the daemon (the agent's graceful-shutdown path) and wait until it has exited.

    Gives up after `timeout` seconds with STILL_RUNNING and leaves it running: a call drain
    must never be cut short behind the operator's back. Only `force` kills: SIGKILL, at once, to
    the daemon's whole process group, so no MCP server or skill it spawned outlives it holding a
    port or file. A plain stop signals the daemon alone; its own shutdown stops those children.
    `on_wait(target, elapsed)` is called right after signalling and then on every poll.
    A daemon it signals nothing to and that isn't already exiting (see StopTarget.waited_for) gets
    STILL_RUNNING at once (or STOPPED, had it exited meanwhile): no wait would change that.

    The record's pid is signalled only if, right before, the lease is still held and the process
    with that pid has the start token the daemon recorded (see Identity); otherwise stop only waits
    for the lease to go. (What's left: the daemon exiting and its pid being reused in the instant
    between that check and the signal; and, where there are no start tokens, at any time while a
    lease is held by some other process than the one the record names.)
    """
    state = read_state(data_dir)
    if state is None:
        return StopResult(StopOutcome.NOT_RUNNING)
    if not daemon_running(data_dir):  # it exited on its own since the record was read
        return StopResult(StopOutcome.STOPPED, state.pid, Identity.GONE)
    target = StopTarget(state, identify(state))
    pid = target.pid
    if target.signalled and pid is not None and pid != os.getpid():
        with contextlib.suppress(ProcessLookupError):
            if force:
                _kill_group(pid)
            else:
                os.kill(pid, signal.SIGTERM)
    if not target.waited_for:
        if on_wait is not None:
            on_wait(target, 0.0)
        outcome = StopOutcome.STILL_RUNNING if daemon_running(data_dir) else StopOutcome.STOPPED
        return StopResult(outcome, pid, target.identity)
    began = time.monotonic()
    while True:
        elapsed = time.monotonic() - began
        if on_wait is not None:
            on_wait(target, elapsed)
        if not daemon_running(data_dir):
            killed = force and target.signalled
            return StopResult(StopOutcome.KILLED if killed else StopOutcome.STOPPED, pid, target.identity)
        if timeout is not None and elapsed >= timeout:
            return StopResult(StopOutcome.STILL_RUNNING, pid, target.identity)
        time.sleep(POLL_INTERVAL)


def identify(state: LiveDaemon) -> Identity:
    """Whether the process the record names is the daemon that wrote it (see Identity). A record
    this version can't read is checked the same way, by the pid and start token it still names."""
    if isinstance(state, DaemonWithoutRecord) or state.pid is None:
        return Identity.UNKNOWN
    if not _has_start_tokens() or state.start_token == NO_START_TOKEN:
        return Identity.UNCHECKED
    if state.start_token is None:
        return Identity.UNKNOWN
    try:
        token = _start_token(state.pid)
    except ProcessLookupError:
        return Identity.GONE
    except OSError:
        return Identity.UNKNOWN
    return Identity.VERIFIED if token == state.start_token else Identity.NOT_PINCER


def _has_start_tokens() -> bool:
    return sys.platform.startswith("linux") or sys.platform == "darwin"


def _start_token(pid: int) -> str | None:
    """When process `pid` started, exactly as the kernel keeps it: equal tokens, same process (a pid
    reused later has a later one). None on a platform without (see _has_start_tokens). Raises
    ProcessLookupError if no process has `pid`, another OSError if it can't be read."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as file:
                stat = file.read()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None
        # Field 22, starttime (clock ticks after boot). Fields 3 on follow the command name, which
        # is in parentheses and may itself hold spaces and parentheses: split after the last one.
        after_name = stat.rpartition(b")")[2].split()
        if len(after_name) < 20:
            raise OSError(f"/proc/{pid}/stat is cut short")
        return after_name[19].decode()
    if sys.platform == "darwin":
        sec, usec = _darwin_start_time(pid)
        return f"{sec}.{usec:06d}"
    return None


def _darwin_start_time(pid: int) -> tuple[int, int]:
    """`kp_proc.p_starttime` of process `pid`, from `sysctl` (see KINFO_PROC_SIZE)."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 4)(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid)
    buffer = ctypes.create_string_buffer(KINFO_PROC_SIZE)
    size = ctypes.c_size_t(KINFO_PROC_SIZE)
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if size.value == 0:  # the kernel's answer for a pid no process has
        raise ProcessLookupError(pid)
    if size.value != KINFO_PROC_SIZE:
        raise OSError(f"sysctl returned {size.value} bytes for pid {pid}, not a struct kinfo_proc")
    sec, usec = struct.unpack_from("@qi", buffer.raw)
    return sec, usec


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
        self._start_token: str | None = None

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
        try:
            self._start_token = _start_token(os.getpid()) or NO_START_TOKEN
        except OSError:  # unreadable here (no /proc, say): stop signals by the record alone, as without tokens
            self._start_token = NO_START_TOKEN
        self._write(DaemonPhase.STARTING, [])
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.remove_record()

    def remove_record(self) -> None:
        """Take the record away, keeping the lock: for a daemon about to end with `os._exit`, which
        never leaves the `with` block, so that it leaves no record behind."""
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
            start_token=self._start_token,
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


def daemon_running(data_dir: Path) -> bool:
    """Whether a daemon of `data_dir` is alive (it holds the lease). One lock probe, no record read:
    for callers that need only running or not, which `read_state` answers more slowly when a
    daemon holds the lease without a record yet (it waits up to RECORD_GRACE for one)."""
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
        """Wait up to `timeout` seconds (at least START_LOCK_GRACE) for the lock; False if another
        start still has it."""
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(timeout, START_LOCK_GRACE)
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
            os.ftruncate(fd, 0)  # no previous start's child pid
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

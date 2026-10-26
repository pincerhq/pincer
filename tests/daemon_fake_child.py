"""Stand-in for a daemonized Pincer, used by the daemon lifecycle tests.

Usage: python daemon_fake_child.py MODE DATA_DIR

Takes the real `DaemonLease`, writes to stdout/stderr the way Pincer does (logging
through `_setup_logging`, so the PII filter applies, plus a printed banner) and then
behaves according to MODE (tests build the command with `argv()`):

    ready             mark ready, idle until SIGTERM, shut down briefly, exit 0
    gated             like ready, but marks ready only once DATA_DIR/mark-ready exists
    slow-start        listens on a port (recorded in the lease) at once, marks ready only after SLOW_START_SECONDS
    crash-on-start    logs CRASH_STEPS startup lines, then fails with an uncaught exception before ready
    slow-lease        like ready, but waits SLOW_LEASE_SECONDS before taking the lease (Pincer's import time)
    never-lease       hangs before taking the lease (setup stuck on the network, say), holding the start lock
    die-mid-line      writes half a line (no newline) and exits 3 before ready
    raise-on-sigterm  like ready, but SIGTERM ends in an uncaught exception
    slow-shutdown     like ready, but its shutdown takes SLOW_SHUTDOWN_SECONDS
    ignore-sigterm    like ready, but never reacts to SIGTERM
    linger            like ready, but after leaving the lease (its record gone, its lock still held) it
                      takes LINGER_SECONDS to exit, as an interpreter tearing down does
    noisy-shutdown    like ready, but on SIGTERM prints NOISY_SHUTDOWN_LINES lines as fast as it can and exits
    no-self-token     like ready, but reading its own start token fails (as without a readable /proc)
    grandchild        like ignore-sigterm, plus a long-lived child process in its process group, whose pid
                      it writes to DATA_DIR/grandchild.pid (an MCP server or sandboxed skill stands in for it)

Like Pincer's own handler, only the first SIGTERM starts the shutdown; later ones are ignored.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

import pincer
from pincer import daemon
from pincer.cli._shared import _setup_logging
from pincer.daemon import DaemonLease

CHANNELS = ["telegram", "signal"]
PORT = 8080
# Long enough that a `stop()` returning right after SIGTERM still finds the process alive.
SHUTDOWN_SECONDS = 0.5
CALLER_NUMBER = "+4915112345689"
CRASH_STEPS = 30
SLOW_START_SECONDS = 2.0
SLOW_LEASE_SECONDS = 1.0
SLOW_SHUTDOWN_SECONDS = 2.0
LINGER_SECONDS = 4.0
# Enough that showing them takes longer than a poll: the daemon is gone while they are still being shown.
NOISY_SHUTDOWN_LINES = 5000
GRANDCHILD_MARK = "fake-grandchild"


def argv(mode: str, data_dir: Path) -> list[str]:
    return [sys.executable, __file__, mode, str(data_dir)]


def children_under(directory: Path) -> list[int]:
    """The pids of the fake children whose DATA_DIR lies under `directory`, found by their command line."""
    return _pgrep(f"{Path(__file__).name} .* {directory}")


def kill_all_under(directory: Path) -> None:
    """SIGKILL every fake child (and grandchild) under `directory` — test cleanup that doesn't trust the lease."""
    for pid in [*children_under(directory), *_pgrep(f"{GRANDCHILD_MARK} {directory}")]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _pgrep(pattern: str) -> list[int]:
    return [int(pid) for pid in subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()]


class _Terminated(Exception):
    pass


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise _Terminated


def main() -> None:
    mode, data_dir = sys.argv[1], Path(sys.argv[2])
    _setup_logging("INFO")
    log = logging.getLogger("fake_child")
    signal.signal(signal.SIGTERM, signal.SIG_IGN if mode in ("ignore-sigterm", "grandchild") else _on_sigterm)

    port = PORT
    if mode == "slow-start":
        # Like the dashboard API, which binds before the channels have started.
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    if mode == "no-self-token":

        def unreadable(pid: int) -> str:
            raise PermissionError(f"/proc/{pid}/stat")

        daemon._start_token = unreadable  # type: ignore[assignment]
    if mode == "slow-lease":
        time.sleep(SLOW_LEASE_SECONDS)
    while mode == "never-lease":
        time.sleep(1)

    with DaemonLease(data_dir, port=port) as lease:
        log.info("fake child %d starting in %s", os.getpid(), os.getcwd())
        log.info("fake child %d imports pincer from %s", os.getpid(), Path(pincer.__file__).resolve())
        log.info("marker from the caller's environment: %s", os.environ.get("FAKE_CHILD_MARKER"))
        log.info("inbound call from %s", CALLER_NUMBER)
        if mode == "die-mid-line":
            os.write(2, b"fatal: no newline")
            os._exit(3)
        if mode == "crash-on-start":
            for step in range(1, CRASH_STEPS + 1):
                log.info("startup step %d", step)
            raise RuntimeError("fake child failed to start")
        if mode == "slow-start":
            time.sleep(SLOW_START_SECONDS)
        while mode == "gated" and not (data_dir / "mark-ready").exists():
            time.sleep(0.05)
        if mode == "grandchild":
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(600)", GRANDCHILD_MARK, str(data_dir)],
                stdin=subprocess.DEVNULL,
            )
            (data_dir / "grandchild.pid").write_text(str(grandchild.pid))
        print(f"Pincer is running! Channels: {', '.join(CHANNELS)}. Press Ctrl+C to stop.")
        lease.mark_ready(CHANNELS)
        try:
            while True:
                time.sleep(0.05)
        except _Terminated:
            log.info("fake child %d shutting down", os.getpid())
            if mode == "raise-on-sigterm":
                raise RuntimeError("fake child failed during shutdown") from None
            if mode == "noisy-shutdown":
                print("\n".join(f"shutdown step {n}" for n in range(1, NOISY_SHUTDOWN_LINES + 1)))
                return
            time.sleep(SLOW_SHUTDOWN_SECONDS if mode == "slow-shutdown" else SHUTDOWN_SECONDS)
            print("Shutdown complete")
    if mode == "linger":
        print("left the lease")
        time.sleep(LINGER_SECONDS)


if __name__ == "__main__":
    main()

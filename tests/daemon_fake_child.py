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
    die-mid-line      writes half a line (no newline) and exits 3 before ready
    raise-on-sigterm  like ready, but SIGTERM ends in an uncaught exception
    slow-shutdown     like ready, but its shutdown takes SLOW_SHUTDOWN_SECONDS
    ignore-sigterm    like ready, but never reacts to SIGTERM
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
import threading
import time
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from pincer.cli._shared import _setup_logging
from pincer.daemon import DaemonLease

if TYPE_CHECKING:
    from collections.abc import Callable

CHANNELS = ["telegram", "signal"]
PORT = 8080
# Long enough that a `stop()` returning right after SIGTERM still finds the process alive.
SHUTDOWN_SECONDS = 0.5
CALLER_NUMBER = "+4915112345689"
CRASH_STEPS = 30
SLOW_START_SECONDS = 2.0
SLOW_LEASE_SECONDS = 1.0
SLOW_SHUTDOWN_SECONDS = 2.0
GRANDCHILD_MARK = "fake-grandchild"


def argv(mode: str, data_dir: Path) -> list[str]:
    return [sys.executable, __file__, mode, str(data_dir)]


def kill_all_under(directory: Path) -> None:
    """SIGKILL every fake child (and grandchild) under `directory` — test cleanup that doesn't trust the lease."""
    pids: list[str] = []
    for pattern in (f"{Path(__file__).name} .* {directory}", f"{GRANDCHILD_MARK} {directory}"):
        pids += subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGKILL)


def within[T](seconds: float, call: Callable[[], T]) -> T:
    """`call()`, failing (instead of hanging the suite) if it hasn't returned after `seconds`."""
    box: list[T] = []
    worker = threading.Thread(target=lambda: box.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, f"still blocked after {seconds} s"
    return box[0]


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

    if mode == "slow-lease":
        time.sleep(SLOW_LEASE_SECONDS)

    with DaemonLease(data_dir, port=port) as lease:
        log.info("fake child %d starting in %s", os.getpid(), os.getcwd())
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
            time.sleep(SLOW_SHUTDOWN_SECONDS if mode == "slow-shutdown" else SHUTDOWN_SECONDS)
            print("Shutdown complete")


if __name__ == "__main__":
    main()

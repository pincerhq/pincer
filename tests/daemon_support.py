"""Helpers shared by the tests that drive real processes: the daemon, `pincer run`, `pincer logs`.

Fixtures built on these (`signal_stub`, `redis_url`) live in conftest.py.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from subprocess import Popen

# A job a shell starts in the background inherits SIGINT ignored (and under nohup SIGHUP too), and
# Python then never turns a Ctrl+C into KeyboardInterrupt. A command typed in a terminal starts with
# both at their defaults: `pincer_command()` starts its process that way, whatever ran pytest.
AS_TYPED_IN_A_TERMINAL = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); signal.signal(signal.SIGHUP, signal.SIG_DFL); "
    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
)
# Rich markup, a CRLF line, a byte that isn't UTF-8 and no final newline: whatever a decode or a console would change.
RAW_LOG = b"plain line\n[bold]markup[/bold] kept\r\nbad byte \xff here\nno newline at the end"


def pincer_command(*args: str) -> list[str]:
    """`python -P -m pincer ARGS…`, started as if typed in a terminal (same pid, after an exec)."""
    return [sys.executable, "-c", AS_TYPED_IN_A_TERMINAL, "-P", "-m", "pincer", *args]


def scratch_env(home: Path, data: Path, **extra: str) -> dict[str, str]:
    """An environment built from nothing for a real Pincer: no developer shell variables, no `.env`
    (HOME is scratch), a dummy API key and offline model loading."""
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HF_HUB_OFFLINE": "1",
        "PINCER_DATA_DIR": str(data),
        "PINCER_ANTHROPIC_API_KEY": "sk-ant-test-dummy",
        "PINCER_LOG_LEVEL": "INFO",
        **extra,
    }


def signal_channel_env(stub_url: str) -> dict[str, str]:
    """A Signal channel in poll mode aimed at `signal_stub`: an agent with no channel never starts."""
    return {
        "PINCER_SIGNAL_ENABLED": "true",
        "PINCER_SIGNAL_API_URL": stub_url,
        "PINCER_SIGNAL_PHONE_NUMBER": "+15550000000",
        "PINCER_SIGNAL_RECEIVE_MODE": "poll",
    }


def wait_for_output(process: Popen[bytes], out_path: Path, text: str, seconds: float, what: str) -> None:
    """Until `text` is in `out_path` (where `process` writes), failing with the output if it exits or times out."""
    deadline = time.monotonic() + seconds
    while text not in out_path.read_text(errors="replace"):
        assert process.poll() is None, out_path.read_text(errors="replace")
        assert time.monotonic() < deadline, f"{what} never started:\n" + out_path.read_text(errors="replace")
        time.sleep(0.2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def alive(pid: int) -> bool:
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


def within[T](seconds: float, call: Callable[[], T]) -> T:
    """`call()`, failing (instead of hanging the suite) if it hasn't returned after `seconds`.

    What `call()` raises is raised here, so a failure inside it reads as itself, not as a hang.
    """
    box: list[T] = []
    raised: list[BaseException] = []

    def run() -> None:
        try:
            box.append(call())
        except BaseException as exc:  # handed back to the test's thread below
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if raised:
        raise raised[0]
    assert box, f"still blocked after {seconds} s"
    return box[0]


def write_log(data_dir: Path, content: bytes) -> Path:
    from pincer import daemon

    log = daemon.log_path(data_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(content)
    return log


def append_log(data_dir: Path, content: bytes) -> None:
    from pincer import daemon

    with daemon.log_path(data_dir).open("ab") as log:
        log.write(content)


def numbered(first: int, last: int) -> bytes:
    return b"".join(f"numbered {n}\n".encode() for n in range(first, last + 1))

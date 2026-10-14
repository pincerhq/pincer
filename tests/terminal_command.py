"""Start a real `pincer` process the way a command typed in a terminal starts.

A job a shell starts in the background inherits SIGINT ignored (and under nohup SIGHUP too), and
Python then never turns a Ctrl+C into KeyboardInterrupt. A command typed in a terminal starts with
both at their defaults: the `pincer` processes the signal tests drive are started that way, whatever
ran pytest.
"""

from __future__ import annotations

import sys

AS_TYPED_IN_A_TERMINAL = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); signal.signal(signal.SIGHUP, signal.SIG_DFL); "
    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
)


def pincer_command(*args: str) -> list[str]:
    """`python -P -m pincer ARGS…`, started as if typed in a terminal (same pid, after an exec)."""
    return [sys.executable, "-c", AS_TYPED_IN_A_TERMINAL, "-P", "-m", "pincer", *args]

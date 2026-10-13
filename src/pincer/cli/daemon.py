"""`pincer run -d` / `pincer stop` — format what the daemon lifecycle module (`pincer.daemon`) returns."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from pincer.cli._shared import console

if TYPE_CHECKING:
    from pincer.config import Settings

WAIT_TIMEOUT = 60.0


def _child_argv() -> list[str]:
    """The daemonized child: this interpreter running `pincer run --daemon-child`.

    `-P` keeps the caller's cwd off sys.path, so a `pincer/` directory there can't shadow the package.
    """
    return [sys.executable, "-P", "-m", "pincer", "run", "--daemon-child"]


def start_daemon(settings: Settings) -> None:
    """Start Pincer in the background from the caller's cwd and environment; return once it is up."""
    from pincer import daemon

    result = daemon.start(settings.data_dir, _child_argv(), Path.cwd(), os.environ, WAIT_TIMEOUT)
    state = result.state
    if result.outcome is daemon.StartOutcome.READY and state is not None:
        channels = ", ".join(state.channels) or "none"
        console.print(
            f"Pincer started (pid {state.pid}) — channels: {channels}  'pincer logs -f' to follow",
            soft_wrap=True,
            markup=False,
        )
        return
    if result.outcome is daemon.StartOutcome.ALREADY_RUNNING and state is not None:
        console.print(f"Pincer is already running (pid {state.pid})", soft_wrap=True, markup=False)
        return
    console.print(f"[red]Pincer did not start ({result.outcome}).[/red]")
    for line in result.last_lines:
        console.print(line, soft_wrap=True, markup=False, highlight=False)
    raise typer.Exit(1)


def stop() -> None:
    """Stop the background Pincer gracefully and wait until it has exited."""
    from pincer import daemon
    from pincer.config import get_settings_relaxed

    result = daemon.stop(get_settings_relaxed().data_dir)
    if result.outcome is daemon.StopOutcome.NOT_RUNNING:
        console.print("Pincer is not running")
        return
    console.print(f"Pincer stopped (pid {result.pid})")

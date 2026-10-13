"""`pincer run -d` / `pincer stop` — format what the daemon lifecycle module (`pincer.daemon`) returns."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from pincer.cli._shared import console

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.daemon import DaemonState

WAIT_TIMEOUT = 60.0


def format_uptime(seconds: float) -> str:
    """`42s`, `3m 07s`, `5h 02m`, `2d 4h` — the two largest units."""
    total = max(int(seconds), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _uptime(state: DaemonState) -> str:
    started = datetime.fromisoformat(state.started_at)
    return format_uptime((datetime.now(UTC) - started).total_seconds())


def _child_argv() -> list[str]:
    """The daemonized child: this interpreter running `pincer run --daemon-child`.

    `-P` keeps the caller's cwd off sys.path, so a `pincer/` directory there can't shadow the package.
    """
    return [sys.executable, "-P", "-m", "pincer", "run", "--daemon-child"]


def start_daemon(settings: Settings, wait_timeout: float = WAIT_TIMEOUT) -> None:
    """Start Pincer in the background from the caller's cwd and environment; return once it is up.

    Streams the startup log meanwhile. Exits 1 if it crashes first, or if `wait_timeout` passes
    first — then it is left running.
    """
    from rich.markup import escape

    from pincer import daemon
    from pincer.cli._shared import _port_in_use

    def show(line: str) -> None:
        console.print(line, style="dim", soft_wrap=True, markup=False, highlight=False)

    # A daemon of this data directory holds the port itself; start() reports it as already running.
    if daemon.read_state(settings.data_dir) is None and _port_in_use(settings.dashboard_host, settings.dashboard_port):
        console.print(
            f"[red]A foreground Pincer is running on :{settings.dashboard_port} — stop it with Ctrl+C in its "
            "terminal.[/red]\n"
            "[yellow]If something else owns the port, set PINCER_DASHBOARD_PORT.[/yellow]",
            soft_wrap=True,
        )
        raise typer.Exit(1)

    result = daemon.start(settings.data_dir, _child_argv(), Path.cwd(), os.environ, wait_timeout, on_line=show)
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
        console.print(
            f"Pincer is already running (pid {state.pid}, up {_uptime(state)}). "
            "Use 'pincer restart' to apply config changes.",
            soft_wrap=True,
            markup=False,
        )
        return
    if result.outcome is daemon.StartOutcome.TIMED_OUT:
        console.print(
            f"[yellow]Pincer is still starting after {wait_timeout:g} s — it keeps running; "
            "check 'pincer logs -f'[/yellow]",
            soft_wrap=True,
        )
        raise typer.Exit(1)
    console.print(
        f"[red]Pincer exited before it was ready (exit code {result.exit_code}). "
        f"Last {len(result.last_lines)} lines of {escape(str(daemon.log_path(settings.data_dir)))}:[/red]",
        soft_wrap=True,
    )
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

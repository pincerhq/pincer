"""`pincer run -d` / `stop` / `restart` / `logs` / `status`, and `pincer run` attaching to a running daemon.

They format what the daemon lifecycle module (`pincer.daemon`) returns.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from pincer.cli._shared import console, err_console

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.status import Status

    from pincer.config import Settings
    from pincer.daemon import DaemonState, DaemonWithoutRecord

WAIT_TIMEOUT = 60.0
# Sized to the voice channel's 60 s call drain plus worker and API grace.
STOP_TIMEOUT = 90.0
# Docker writes this into every container it starts.
DOCKERENV = Path("/.dockerenv")
# How often an attached `pincer run` looks up from waiting, so a Ctrl+C lands at once.
POLL_INTERVAL = 0.1


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


def refuse_unsupported(*, starting: bool) -> None:
    """Exit 1 where daemon mode can't work: on Windows (no `fork`/`flock` semantics), and, for a
    start, inside a container, where the container's main process is what should stay up.

    Called before anything is imported from or done by the lifecycle module, so nothing is
    spawned, signalled or written in a refused case.
    """
    if sys.platform == "win32":
        console.print("[red]Daemon mode isn't supported on Windows — use a service manager.[/red]")
        raise typer.Exit(1)
    if starting and (os.getpid() == 1 or DOCKERENV.exists()):
        console.print(
            "[red]Pincer is running inside a container — use 'docker compose up -d' to run it in the background.[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1)


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
    refuse_unsupported(starting=True)
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
    if result.outcome is daemon.StartOutcome.READY and isinstance(state, daemon.DaemonState):
        channels = ", ".join(state.channels) or "none"
        console.print(
            f"Pincer started (pid {state.pid}) — channels: {channels}  'pincer logs -f' to follow",
            soft_wrap=True,
            markup=False,
        )
        return
    if result.outcome is daemon.StartOutcome.ALREADY_RUNNING:
        if not isinstance(state, daemon.DaemonState):
            refuse_while_shutting_down(settings)
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


def refuse_while_shutting_down(settings: Settings) -> NoReturn:
    """Exit 1: a daemon holds the lease but has no readable record, so it is (all but) gone."""
    console.print(
        f"Pincer is still shutting down (data dir {settings.data_dir}) — try again in a moment.",
        soft_wrap=True,
        markup=False,
        highlight=False,
    )
    raise typer.Exit(1)


def attach_daemon(settings: Settings, state: DaemonState) -> None:
    """Show the running daemon's log as if it ran in this terminal (what `pincer run` does when one is up).

    The first Ctrl+C stops it gracefully, still showing its log until it has exited; a
    second one only detaches. A hangup only ends this process: the daemon has a session of its own.
    """
    # asyncio's runner turns a Ctrl+C into a cancel that this blocking wait would never see. Take
    # Ctrl+C over before anything is shown, so one pressed the moment attaching starts still stops it.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    relay = _LogRelay(settings)
    try:
        try:
            console.print(
                f"Pincer is already running in the background (pid {state.pid}) — attaching",
                markup=False,
                highlight=False,
            )
            relay.start()
            relay.wait()
        except KeyboardInterrupt:
            relay.start()  # if the Ctrl+C came before it did; never a second one
            try:
                stopped = _stop_daemon(settings, STOP_TIMEOUT, force=False, before_report=relay.wait)
                if stopped is None:
                    # Still shutting down (the --force hint is out) or not ours to signal: keep
                    # showing its log; a second Ctrl+C still only detaches.
                    relay.wait()
                    console.print(f"Pincer has exited (pid {state.pid})", markup=False, highlight=False)
                    return
            except KeyboardInterrupt:
                relay.detach()
                console.print(
                    f"Detached — Pincer (pid {state.pid}) keeps shutting down in the background",
                    markup=False,
                    highlight=False,
                )
                return
            if not stopped:
                _report_not_running(settings)
            return
        console.print(f"Pincer has exited (pid {state.pid})", markup=False, highlight=False)
    finally:
        signal.signal(signal.SIGINT, previous)


class _LogRelay:
    """Prints the daemon log from 50 lines back until the daemon exits, on a thread of its own.

    The main thread stays free for Ctrl+C, and for the stop routine the first one starts.
    """

    def __init__(self, settings: Settings) -> None:
        from pincer import daemon

        self._chunks = daemon.follow(settings.data_dir, daemon.FOLLOW_LINES, until_exit=True)
        self._detached = False
        self._start_tried = False
        self._printing = threading.Lock()
        self._thread = threading.Thread(target=self._relay, daemon=True)

    def start(self) -> None:
        """Start relaying, at most once.

        Only the first call starts the thread, even if a Ctrl+C interrupted it partway. In the
        instant between marking the start and starting the thread, a Ctrl+C means no relay at all,
        never two printing the same lines.
        """
        if self._start_tried:
            return
        self._start_tried = True
        self._thread.start()

    def _relay(self) -> None:
        from pincer.daemon import decode_lines

        with contextlib.closing(self._chunks):
            for chunk in self._chunks:
                with self._printing:
                    if self._detached:
                        return
                    for line in decode_lines(chunk):
                        console.print(line, soft_wrap=True, markup=False, highlight=False)

    def wait(self) -> None:
        """Until the daemon has exited and its last lines are shown; Ctrl+C interrupts it."""
        while self._thread.is_alive():
            self._thread.join(POLL_INTERVAL)

    def detach(self) -> None:
        """Print nothing more, so no line is half-written when this process exits."""
        with self._printing:
            self._detached = True


class _StopProgress:
    """One "Stopping Pincer…" line; on a terminal only, a transient spinner with the time waited.

    Piped output gets no spinner frames, so `pincer stop | tee` stays two lines.
    """

    def __init__(self) -> None:
        self._announced = False
        self._status: Status | None = None

    def __call__(self, state: DaemonState | DaemonWithoutRecord, elapsed: float) -> None:
        from pincer.daemon import DaemonState

        if not self._announced:
            self._announced = True
            if isinstance(state, DaemonState):
                console.print(f"Stopping Pincer (pid {state.pid})…", markup=False, highlight=False)
            else:
                console.print(
                    "Pincer is already shutting down — waiting for it to exit…", markup=False, highlight=False
                )
            if console.is_terminal:
                self._status = console.status("Waiting for it to exit")
                self._status.start()
        if self._status is not None:
            self._status.update(f"Waiting for it to exit — {format_uptime(elapsed)}")

    def close(self) -> None:
        if self._status is not None:
            self._status.stop()


def stop(
    timeout: float = typer.Option(
        STOP_TIMEOUT, "--timeout", help="Seconds to wait for the graceful shutdown; Pincer is never killed on its own."
    ),
    force: bool = typer.Option(False, "--force", help="Kill Pincer at once (SIGKILL) instead of shutting it down."),
) -> None:
    """Stop the background Pincer gracefully and wait until it has exited."""
    from pincer.config import get_settings_relaxed

    refuse_unsupported(starting=False)

    settings = get_settings_relaxed()
    stopped = _stop_daemon(settings, timeout, force)
    if stopped is None:
        raise typer.Exit(1)
    if not stopped:
        _report_not_running(settings)


def restart(
    timeout: float = typer.Option(
        STOP_TIMEOUT, "--timeout", help="Seconds to wait for the old Pincer's graceful shutdown; never killed."
    ),
    wait_timeout: float = typer.Option(
        WAIT_TIMEOUT, "--wait-timeout", help="Seconds to wait for the new one to start (a slow start is never killed)."
    ),
) -> None:
    """Stop the background Pincer, then start a new one from here (to apply .env / pincer.toml changes)."""
    from pincer.config import get_settings

    refuse_unsupported(starting=True)

    # The new config is checked before anything is stopped: a broken .env must not take
    # down the daemon that is working.
    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    stopped = _stop_daemon(settings, timeout, force=False)
    if stopped is None:
        console.print("Not starting a new one.")
        raise typer.Exit(1)
    if not stopped:
        console.print("Pincer is not running — starting it")
    start_daemon(settings, wait_timeout)


def _stop_daemon(
    settings: Settings,
    timeout: float,
    force: bool,
    *,
    before_report: Callable[[], None] | None = None,
) -> bool | None:
    """Stop the daemon of `settings.data_dir`, printing its progress and outcome as `pincer stop` does.

    True once it has exited; False if none was running (the caller says so, in its own words);
    None if it is still running or couldn't be signalled. `before_report` runs once it has
    exited (or none was running), before that is printed; never while it is still running.
    """
    from rich.markup import escape

    from pincer import daemon

    progress = _StopProgress()
    try:
        result = daemon.stop(settings.data_dir, timeout=timeout, force=force, on_wait=progress)
    except PermissionError:
        running = daemon.read_state(settings.data_dir)
        pid = running.pid if isinstance(running, daemon.DaemonState) else "?"
        console.print(f"[red]Not allowed to signal Pincer (pid {pid}) — it runs as another user.[/red]")
        return None
    finally:
        progress.close()
    if before_report is not None and result.outcome is not daemon.StopOutcome.STILL_RUNNING:
        before_report()
    if result.outcome is daemon.StopOutcome.NOT_RUNNING:
        return False
    if result.outcome is daemon.StopOutcome.STILL_RUNNING and result.pid is None:
        console.print(
            f"[yellow]Pincer still holds data dir {escape(str(settings.data_dir))} after {timeout:g} s, but its record "
            "can't be read — not signalling a process this version can't identify.[/yellow]",
            soft_wrap=True,
        )
        return None
    if result.outcome is daemon.StopOutcome.STILL_RUNNING:
        console.print(
            f"[yellow]Pincer is still shutting down after {timeout:g} s — 'pincer stop --force' to kill it[/yellow]",
            soft_wrap=True,
        )
        return None
    if result.pid is None:
        console.print("Pincer has exited")
        return True
    verb = "killed" if result.outcome is daemon.StopOutcome.KILLED else "stopped"
    console.print(f"Pincer {verb} (pid {result.pid})")
    return True


def _report_not_running(settings: Settings) -> None:
    """Say Pincer is not running, and who has the dashboard port if something does."""
    from pincer.cli._shared import _port_in_use

    console.print("Pincer is not running")
    if _port_in_use(settings.dashboard_host, settings.dashboard_port):
        console.print(
            f"[yellow]A foreground Pincer seems to be running on :{settings.dashboard_port} — stop it with "
            "Ctrl+C in its terminal.[/yellow]",
            soft_wrap=True,
        )


def logs(
    tail: int | None = typer.Option(None, "-n", "--tail", min=0, help="Only the last N lines (-f starts 50 back)."),
    follow: bool = typer.Option(
        False, "-f", "--follow", help="Keep printing new lines until Pincer exits; Ctrl+C only stops following."
    ),
) -> None:
    """Print the background Pincer's log, raw (the last session's, if it isn't running)."""
    refuse_unsupported(starting=False)
    from pincer import daemon
    from pincer.config import get_settings_relaxed

    data_dir = get_settings_relaxed().data_dir
    if not daemon.log_path(data_dir).exists():
        err_console.print("No logs yet — start with 'pincer run -d'", soft_wrap=True, markup=False, highlight=False)
        return
    if daemon.read_state(data_dir) is None:
        err_console.print(
            "Pincer is not running — showing the last session's log", style="dim", soft_wrap=True, highlight=False
        )
    from_lines = tail if tail is not None else (daemon.FOLLOW_LINES if follow else None)

    out = sys.stdout.buffer
    try:
        with contextlib.closing(daemon.follow(data_dir, from_lines, until_exit=follow)) as chunks:
            for chunk in chunks:
                out.write(chunk)
                out.flush()
    except KeyboardInterrupt:  # Ctrl+C only stops following; the daemon has a session of its own
        return
    except BrokenPipeError:  # `pincer logs | head`
        # Python's advice for SIGPIPE: point stdout at /dev/null, or the flush at exit fails again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)


def status() -> None:
    """Show whether the background Pincer is running or starting, and what it runs with."""
    refuse_unsupported(starting=False)
    from pincer import daemon
    from pincer.config import get_settings_relaxed

    data_dir = get_settings_relaxed().data_dir
    state = daemon.read_state(data_dir)
    if state is None:
        console.print("Pincer is not running")
        return
    if not isinstance(state, daemon.DaemonState):
        console.print(
            f"Pincer is shutting down (data dir {data_dir}) — its lock is held, but its record is gone or unreadable",
            soft_wrap=True,
            markup=False,
            highlight=False,
        )
        return
    ready = state.state is daemon.DaemonPhase.READY
    word = "running" if ready else "starting"
    channels = f"channels: {', '.join(state.channels) or 'none'}, " if ready else ""
    console.print(
        f"Pincer is {word} (pid {state.pid}, up {_uptime(state)}) — port {state.port}, {channels}"
        f"version {state.version}, cwd {state.cwd}",
        soft_wrap=True,
        markup=False,
        highlight=False,
    )

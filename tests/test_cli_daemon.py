"""Tests for `pincer run -d` / `pincer stop` (typer.testing.CliRunner), against the fake child."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import pty
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import daemon_fake_child
import pytest
from daemon_support import (
    RAW_LOG,
    SRC_DIR,
    alive,
    append_log,
    child_env,
    free_port,
    lease_held_elsewhere,
    numbered,
    pincer_command,
    stop_within,
    within,
    write_log,
)
from typer.testing import CliRunner, Result

import pincer
from pincer import config, daemon
from pincer.cli import app
from pincer.cli.daemon import format_uptime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pincer.config import Settings

    Spawn = Callable[..., subprocess.Popen[bytes]]

NOT_RUNNING_NOTE = "Pincer is not running — showing the last session's log"
NO_LOGS = "No logs yet — start with 'pincer run -d'"
WINDOWS_MESSAGE = "Daemon mode isn't supported on Windows — use a service manager."
CONTAINER_MESSAGE = "Pincer is running inside a container — use 'docker compose up -d' to run it in the background."
SHUTTING_DOWN = "Pincer is still shutting down (data dir {data_dir}) — try again in a moment."
START_IN_PROGRESS = "A start of Pincer is in progress (pid {pid}) and hasn't taken its lease yet"
UNREADABLE = (
    "A Pincer daemon (pid {pid}) holds data dir {data_dir}, but its record can't be read by this version"
    " — 'pincer stop' can still stop it."
)
# The same, when the record doesn't still name a start token to check the pid against.
UNREADABLE_UNIDENTIFIED = (
    "A Pincer daemon holds data dir {data_dir}, but its record can't be read by this version (it names pid {pid}),"
    " so its process can't be identified and 'pincer stop' won't signal it — 'lsof {data_dir}/pincer.lock' shows"
    " which process holds its lock."
)
# What `pincer stop` says of a daemon it can't tell the process of, once it has waited `timeout`.
NOT_SIGNALLED = (
    "Pincer holds data dir {data_dir}, but {why} — not signalling anything; waiting for it to exit"
    " ('lsof {data_dir}/pincer.lock' shows which process holds its lock)…"
)
# What `pincer stop` says, at once, of a live daemon it can't tell the process of.
NOT_SIGNALLED_AT_ONCE = (
    "Pincer holds data dir {data_dir}, but {why} — not signalling anything; 'lsof {data_dir}/pincer.lock' shows"
    " which process holds its lock."
)
STILL_HELD = (
    "Pincer still holds data dir {data_dir} after {timeout} s; nothing was signalled — 'lsof {data_dir}/pincer.lock'"
    " shows which process holds its lock."
)
# `pincer run -d --wait-timeout 30`, in a process whose daemon is the fake child given as the argv (a JSON list).
PATCHED_CHILD_RUN_DETACH = (
    "import json, sys; import pincer.cli.daemon as d; d._child_argv = lambda: json.loads(sys.argv[1]); "
    "from pincer.cli import app; app(['run', '-d', '--wait-timeout', '30'], prog_name='pincer')"
)
# `pincer run`, in a process whose attach gives up on a stop after {} seconds instead of 90.
PATCHED_STOP_TIMEOUT_RUN = (
    "import pincer.cli.daemon as d; d.STOP_TIMEOUT = {}; from pincer.cli import app; app(['run'], prog_name='pincer')"
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="daemon mode is POSIX-only")

runner = CliRunner()


def _child_mode(monkeypatch: pytest.MonkeyPatch, mode: str, data_dir: Path) -> None:
    monkeypatch.setattr("pincer.cli.daemon._child_argv", lambda: daemon_fake_child.argv(mode, data_dir))


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    settings = MagicMock()
    settings.data_dir = path
    settings.dashboard_host = "127.0.0.1"
    settings.dashboard_port = free_port()
    settings.log_level.value = "WARNING"
    settings.telemetry_dsn = None
    settings.daily_budget_usd = 5.0
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    _child_mode(monkeypatch, "ready", path)
    # Everything a test starts (`run -d`'s daemon, `pincer logs`, `pincer run`) inherits this environment.
    monkeypatch.setenv("PYTHONPATH", child_env()["PYTHONPATH"])
    yield path
    daemon_fake_child.kill_all_under(tmp_path)


def test_run_detach_returns_once_ready_and_stop_waits_for_exit(data_dir: Path) -> None:
    started = runner.invoke(app, ["run", "-d"])

    assert started.exit_code == 0, started.output
    match = re.search(r"Pincer started \(pid (\d+)\) — channels: telegram, signal", started.output)
    assert match, started.output
    assert "'pincer logs -f' to follow" in started.output
    pid = int(match.group(1))
    state = daemon.read_state(data_dir)
    assert state is not None
    assert state.pid == pid

    stopped = runner.invoke(app, ["stop"])

    assert stopped.exit_code == 0, stopped.output
    assert f"Pincer stopped (pid {pid})" in stopped.output
    assert daemon.read_state(data_dir) is None
    assert "Shutdown complete" in daemon.log_path(data_dir).read_text()


def test_stop_with_nothing_running_exits_zero(data_dir: Path) -> None:
    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0
    assert "Pincer is not running" in result.output


def test_foreground_run_creates_no_record_and_no_log(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    async def _run_agent(settings, lease=None):  # type: ignore[no-untyped-def]
        seen.append(lease)

    monkeypatch.setattr("pincer.cli.run._run_agent", _run_agent)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert sorted(p.name for p in data_dir.rglob("*")) == []


def test_daemon_child_runs_the_agent_inside_the_lease(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    states: list[str | None] = []

    async def _run_agent(settings, lease=None):  # type: ignore[no-untyped-def]
        states.append(getattr(daemon.read_state(data_dir), "state", None))
        lease.mark_ready(["telegram"])
        states.append(getattr(daemon.read_state(data_dir), "state", None))

    monkeypatch.setattr("pincer.cli.run._run_agent", _run_agent)

    result = within(10.0, lambda: runner.invoke(app, ["run", "--daemon-child"]))

    assert result.exit_code == 0, result.output
    assert states == ["starting", "ready"]


def test_run_detach_shows_the_startup_log_and_the_crash_tail(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _child_mode(monkeypatch, "crash-on-start", data_dir)

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1
    out = result.output
    assert "INFO:fake_child:startup step 1\n" in out
    header = f"Pincer exited before it was ready (exit code 1). Last 20 lines of {daemon.log_path(data_dir)}:"
    assert header in out
    tail = out[out.index(header) + len(header) :].strip().splitlines()
    assert tail == daemon.log_path(data_dir).read_text().splitlines()[-20:]
    assert daemon.read_state(data_dir) is None


def test_run_detach_wait_timeout_leaves_the_daemon_starting(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _child_mode(monkeypatch, "slow-start", data_dir)

    result = runner.invoke(app, ["run", "-d", "--wait-timeout", "1"])

    assert result.exit_code == 1
    assert "Pincer is still starting after 1 s — it keeps running; check 'pincer logs -f'" in result.output
    state = daemon.read_state(data_dir)
    assert state is not None
    assert state.state == "starting"
    stop_within(data_dir)


@pytest.mark.parametrize("first_mode", ["ready", "gated"], ids=["ready", "starting"])
def test_a_second_run_detach_reports_the_running_daemon_and_spawns_nothing(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_mode: str
) -> None:
    _child_mode(monkeypatch, first_mode, data_dir)
    first = runner.invoke(app, ["run", "-d", "--wait-timeout", "1"])
    state = daemon.read_state(data_dir)
    assert state is not None, first.output
    assert state.state == ("ready" if first_mode == "ready" else "starting")
    _child_mode(monkeypatch, "ready", data_dir)

    # The daemon's own API holds the dashboard port; that must not read as a foreground instance.
    with socket.create_server(("127.0.0.1", config.get_settings().dashboard_port)):
        second = runner.invoke(app, ["run", "-d"])

    assert second.exit_code == 0, second.output
    assert re.search(
        rf"Pincer is already running \(pid {state.pid}, up \d+s\)\. Use 'pincer restart' to apply config changes\.",
        second.output,
    ), second.output
    assert daemon_fake_child.children_under(tmp_path) == [state.pid]


def test_run_detach_refuses_while_something_else_holds_the_port(data_dir: Path, tmp_path: Path) -> None:
    port = config.get_settings().dashboard_port
    with socket.create_server(("127.0.0.1", port)):
        result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1
    assert f"A foreground Pincer is running on :{port} — stop it with Ctrl+C in its terminal" in result.output
    assert daemon_fake_child.children_under(tmp_path) == []
    assert not daemon.log_path(data_dir).exists()


async def test_the_foreground_port_guard_points_to_stop_not_pkill(capsys: pytest.CaptureFixture[str]) -> None:
    import typer

    from pincer.cli.run import _run_agent

    settings = MagicMock()
    settings.dashboard_host = "127.0.0.1"
    with socket.create_server(("127.0.0.1", 0)) as held:
        settings.dashboard_port = held.getsockname()[1]
        with pytest.raises(typer.Exit):
            await _run_agent(settings)

    out = " ".join(capsys.readouterr().out.split())
    assert "pkill" not in out
    assert "pincer stop" in out
    assert "Ctrl+C in its terminal" in out


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [(0, "0s"), (59, "59s"), (60, "1m 00s"), (3599, "59m 59s"), (3600, "1h 00m"), (86399, "23h 59m"), (90000, "1d 1h")],
)
def test_format_uptime(seconds: int, shown: str) -> None:
    assert format_uptime(seconds) == shown


def test_stop_times_out_without_killing_then_force_kills(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_mode(monkeypatch, "ignore-sigterm", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]

    timed_out = within(10.0, lambda: runner.invoke(app, ["stop", "--timeout", "1"]))

    assert timed_out.exit_code == 1
    assert "Pincer is still shutting down after 1 s — 'pincer stop --force' to kill it" in timed_out.output
    assert daemon_fake_child.children_under(tmp_path) == [pid]

    forced = within(10.0, lambda: runner.invoke(app, ["stop", "--force"]))

    assert forced.exit_code == 0, forced.output
    assert f"Pincer killed (pid {pid})" in forced.output
    assert daemon_fake_child.children_under(tmp_path) == []
    _child_mode(monkeypatch, "ready", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0


def test_stop_waits_out_a_slow_shutdown_with_one_progress_line_when_piped(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_mode(monkeypatch, "slow-shutdown", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    # CliRunner's output is not a terminal: no spinner frames, just the two lines.
    assert result.output.splitlines() == [f"Stopping Pincer (pid {pid})…", f"Pincer stopped (pid {pid})"]


def test_stop_with_nothing_running_points_to_a_foreground_instance_on_the_port(data_dir: Path) -> None:
    port = config.get_settings().dashboard_port
    with socket.create_server(("127.0.0.1", port)):
        result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0
    assert "Pincer is not running" in result.output
    assert f"A foreground Pincer seems to be running on :{port} — stop it with Ctrl+C in its terminal." in result.output


def test_stop_reports_a_daemon_it_may_not_signal(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0

    def refuse(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    with monkeypatch.context() as patched:
        patched.setattr("os.kill", refuse)
        result = runner.invoke(app, ["stop"])

    assert result.exit_code == 1
    assert "Not allowed to signal Pincer (pid " in result.output
    assert "Traceback" not in result.output
    assert daemon.read_state(data_dir) is not None


@pytest.fixture
def pincer_logs(data_dir: Path, tmp_path: Path) -> Iterator[Spawn]:
    """Runs a real `pincer logs …` process on this test's data_dir, from a scratch cwd (no `.env` there)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    started: list[subprocess.Popen[bytes]] = []

    def spawn(*args: str, stderr: int = subprocess.PIPE, **env: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            pincer_command("logs", *args),
            cwd=cwd,
            env={**os.environ, "PINCER_DATA_DIR": str(data_dir), **env},
            stdout=subprocess.PIPE,
            stderr=stderr,
            # Its own process group, like a command in a terminal: Ctrl+C goes to it, not to the daemon.
            start_new_session=True,
        )
        started.append(process)
        return process

    yield spawn
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_logs_prints_the_whole_file_raw(data_dir: Path) -> None:
    write_log(data_dir, RAW_LOG)

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == RAW_LOG


@pytest.mark.parametrize("flag", ["-n", "--tail"])
def test_logs_tail_prints_exactly_the_last_lines(data_dir: Path, flag: str) -> None:
    write_log(data_dir, numbered(1, 150))

    result = runner.invoke(app, ["logs", flag, "100"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == numbered(51, 150)


def test_logs_of_a_stopped_daemon_prints_its_last_session_under_a_note(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    assert runner.invoke(app, ["stop"]).exit_code == 0

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert b"Shutdown complete" in result.stdout_bytes
    assert result.stdout_bytes == daemon.log_path(data_dir).read_bytes()
    assert NOT_RUNNING_NOTE in result.stderr
    # stderr isn't a terminal here: the note comes without styling.
    assert "\x1b[" not in result.stderr


def test_logs_follow_with_no_daemon_running_prints_the_tail_and_exits(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    assert runner.invoke(app, ["stop"]).exit_code == 0
    append_log(data_dir, numbered(1, 80))

    result = within(10.0, lambda: runner.invoke(app, ["logs", "-f"]))

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == numbered(31, 80)
    assert NOT_RUNNING_NOTE in result.stderr


def test_logs_of_a_running_daemon_has_no_note(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == daemon.log_path(data_dir).read_bytes()
    assert result.stderr == ""
    stop_within(data_dir)


def test_logs_of_a_daemon_without_a_record_takes_it_as_running_without_waiting_for_one(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`logs` only needs to know whether the daemon is alive: it must not wait for a record."""
    write_log(data_dir, RAW_LOG)
    reads: list[Path] = []
    real_read_record = daemon._read_record

    def read_record(path: Path) -> object:
        reads.append(path)
        return real_read_record(path)

    monkeypatch.setattr(daemon, "_read_record", read_record)
    monkeypatch.setattr(daemon, "RECORD_GRACE", 30.0)  # waiting for a record would outlast `within`
    # Another process holds the lease, as a daemon past writing (or before reading) its record would.
    with lease_held_elsewhere(data_dir / daemon.LOCK_FILE):
        result = within(5.0, lambda: runner.invoke(app, ["logs"]))

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == RAW_LOG
    assert NOT_RUNNING_NOTE not in result.stderr  # alive: no "last session" note
    assert reads == []


@pytest.mark.parametrize("args", [[], ["-f"], ["-n", "5"]], ids=["plain", "follow", "tail"])
def test_logs_with_no_log_file_says_how_to_get_one(data_dir: Path, args: list[str]) -> None:
    result = runner.invoke(app, ["logs", *args])

    assert result.exit_code == 0, result.output
    assert NO_LOGS in result.stderr
    assert result.stdout_bytes == b""


def test_logs_follow_prints_the_last_50_then_live_lines_and_exits_when_the_daemon_stops(
    data_dir: Path, pincer_logs: Spawn
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    append_log(data_dir, numbered(1, 80))
    process = pincer_logs("-f")
    assert process.stdout is not None

    shown = b"".join(within(20.0, process.stdout.readline) for _ in range(50))
    assert shown == numbered(31, 80)
    append_log(data_dir, b"live 1\n")
    assert within(10.0, process.stdout.readline) == b"live 1\n"
    assert process.poll() is None

    stop_within(data_dir)

    out, err = process.communicate(timeout=10)
    assert process.returncode == 0, err
    assert out.endswith(b"Shutdown complete\n")
    assert err == b""


def test_logs_follow_from_zero_lines_prints_only_new_ones(data_dir: Path, pincer_logs: Spawn) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    process = pincer_logs("-f", "-n", "0")
    assert process.stdout is not None
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()  # type: ignore[union-attr]

    # Until it has opened the log, anything written is history to it; keep writing until a line comes out.
    deadline = time.monotonic() + 20.0
    for n in itertools.count():
        append_log(data_dir, f"live {n}\n".encode())
        with contextlib.suppress(queue.Empty):
            first = lines.get(timeout=0.3)
            break
        assert time.monotonic() < deadline, "nothing followed"

    assert re.fullmatch(rb"live \d+\n", first), first
    stop_within(data_dir)
    assert process.wait(timeout=10) == 0


def test_ctrl_c_in_logs_follow_only_detaches(data_dir: Path, pincer_logs: Spawn) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None
    process = pincer_logs("-f")
    assert process.stdout is not None
    within(20.0, process.stdout.readline)

    os.killpg(process.pid, signal.SIGINT)

    _, err = process.communicate(timeout=5)
    assert process.returncode == 0, err
    assert b"Traceback" not in err
    assert daemon.read_state(data_dir) == state
    os.kill(state.pid, 0)
    stop_within(data_dir)


def test_logs_writes_the_file_bytes_even_when_colour_is_forced(data_dir: Path, pincer_logs: Spawn) -> None:
    write_log(data_dir, RAW_LOG)
    process = pincer_logs(FORCE_COLOR="1", TERM="xterm-256color")

    out, err = process.communicate(timeout=20)

    assert process.returncode == 0, err
    assert out == RAW_LOG


@pytest.mark.parametrize("args", [[], ["-f"]], ids=["plain", "follow"])
def test_logs_into_a_pipe_closed_early_exits_quietly(data_dir: Path, pincer_logs: Spawn, args: list[str]) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    # Far more than a pipe buffers, so a plain `logs` is still writing when the reader goes.
    append_log(data_dir, numbered(1, 20000))
    process = pincer_logs(*args)
    assert process.stdout is not None and process.stderr is not None
    within(20.0, process.stdout.readline)

    process.stdout.close()  # `| head -1`
    append_log(data_dir, b"written after the reader left\n")

    assert process.wait(timeout=10) == 0, process.stderr.read()
    # No traceback, and no "Exception ignored … BrokenPipeError" from the interpreter's exit-time flush.
    assert process.stderr.read() == b""
    stop_within(data_dir)


def test_the_not_running_note_reaches_a_terminal_while_stdout_is_piped(data_dir: Path, pincer_logs: Spawn) -> None:
    write_log(data_dir, RAW_LOG)
    terminal, stderr = pty.openpty()
    shown: list[bytes] = []

    def read_terminal() -> None:
        # Read while the process runs: macOS drops what is buffered once the last writer closes.
        with contextlib.suppress(OSError):  # EIO once the process has closed its end
            while chunk := os.read(terminal, 4096):
                shown.append(chunk)

    reader = threading.Thread(target=read_terminal, daemon=True)
    reader.start()
    try:
        process = pincer_logs(stderr=stderr)
        os.close(stderr)
        out, _ = process.communicate(timeout=20)
        reader.join(5.0)
    finally:
        os.close(terminal)

    assert process.returncode == 0
    assert out == RAW_LOG
    assert NOT_RUNNING_NOTE.encode() in b"".join(shown)


def _status_line(result: Result) -> str:
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 1, result.output
    return lines[0]


def test_status_of_a_ready_daemon_shows_running_with_every_field(data_dir: Path) -> None:
    from pincer import __version__

    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None

    line = _status_line(runner.invoke(app, ["status"]))

    assert re.fullmatch(
        rf"Pincer is running \(pid {state.pid}, up \d+s\) — port 8080, channels: telegram, signal, "
        rf"version {re.escape(__version__)}, cwd {re.escape(state.cwd)}",
        line,
    ), line
    stop_within(data_dir)


def test_status_of_a_starting_daemon_shows_starting_without_channels(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pincer import __version__

    _child_mode(monkeypatch, "gated", data_dir)
    assert runner.invoke(app, ["run", "-d", "--wait-timeout", "0.5"]).exit_code == 1
    state = daemon.read_state(data_dir)
    assert state is not None
    assert state.state == "starting"

    line = _status_line(runner.invoke(app, ["status"]))

    assert re.fullmatch(
        rf"Pincer is starting \(pid {state.pid}, up \d+s\) — port 8080, "
        rf"version {re.escape(__version__)}, cwd {re.escape(state.cwd)}",
        line,
    ), line
    stop_within(data_dir)


def test_status_with_no_daemon_shows_not_running(data_dir: Path) -> None:
    assert _status_line(runner.invoke(app, ["status"])) == "Pincer is not running"


def test_status_after_the_daemon_was_killed_with_sigkill_shows_not_running(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None
    os.kill(state.pid, signal.SIGKILL)
    deadline = time.monotonic() + 10.0
    while daemon.read_state(data_dir) is not None:
        assert time.monotonic() < deadline, "the lease outlived the killed daemon"
        time.sleep(0.02)
    # Its record is still on disk, naming the dead pid.
    assert json.loads((data_dir / daemon.PID_FILE).read_text())["pid"] == state.pid

    assert _status_line(runner.invoke(app, ["status"])) == "Pincer is not running"


def test_status_shows_the_daemons_cwd_not_the_callers(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started_in = tmp_path / "started-here"
    called_from = tmp_path / "called-from-here"
    started_in.mkdir()
    called_from.mkdir()
    monkeypatch.chdir(started_in)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    monkeypatch.chdir(called_from)

    line = _status_line(runner.invoke(app, ["status"]))

    assert line.endswith(f", cwd {started_in.resolve()}"), line
    stop_within(data_dir)


# ── restart ───────────────────────────────────────────────────────────────


def _pid_in(output: str, pattern: str) -> int:
    match = re.search(pattern, output)
    assert match, output
    return int(match.group(1))


def test_restart_replaces_a_running_daemon(data_dir: Path, tmp_path: Path) -> None:
    old = _pid_in(runner.invoke(app, ["run", "-d"]).output, r"Pincer started \(pid (\d+)\)")

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 0, result.output
    assert f"Pincer stopped (pid {old})" in result.output
    new = _pid_in(result.output, r"Pincer started \(pid (\d+)\) — channels: telegram, signal")
    assert new != old
    assert result.output.index("Pincer stopped") < result.output.index("Pincer started")
    state = daemon.read_state(data_dir)
    assert state is not None
    assert (state.pid, state.state) == (new, "ready")
    assert daemon_fake_child.children_under(tmp_path) == [new]


def test_restart_with_nothing_running_just_starts(data_dir: Path) -> None:
    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 0, result.output
    assert "Pincer is not running — starting it" in result.output
    pid = _pid_in(result.output, r"Pincer started \(pid (\d+)\)")
    assert daemon.read_state(data_dir).pid == pid  # type: ignore[union-attr]


def test_restart_starts_nothing_while_the_old_daemon_will_not_exit(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child_mode(monkeypatch, "ignore-sigterm", data_dir)
    old = _pid_in(runner.invoke(app, ["run", "-d"]).output, r"Pincer started \(pid (\d+)\)")
    _child_mode(monkeypatch, "ready", data_dir)

    result = within(10.0, lambda: runner.invoke(app, ["restart", "--timeout", "1"]))

    assert result.exit_code == 1
    assert "Pincer is still shutting down after 1 s — 'pincer stop --force' to kill it" in result.output
    assert "Not starting a new one." in result.output
    assert "Pincer started" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [old]
    assert daemon.read_state(data_dir).pid == old  # type: ignore[union-attr]


def test_restart_starts_the_new_daemon_from_the_callers_cwd(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_cwd, caller_cwd = tmp_path / "first", tmp_path / "caller"
    first_cwd.mkdir()
    caller_cwd.mkdir()
    monkeypatch.chdir(first_cwd)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    monkeypatch.chdir(caller_cwd)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 0, result.output
    assert daemon.read_state(data_dir).cwd == str(caller_cwd.resolve())  # type: ignore[union-attr]


def test_a_crash_while_restart_starts_is_shown_as_run_detach_shows_it(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _pid_in(runner.invoke(app, ["run", "-d"]).output, r"Pincer started \(pid (\d+)\)")
    _child_mode(monkeypatch, "crash-on-start", data_dir)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert f"Pincer stopped (pid {old})" in result.output
    assert "INFO:fake_child:startup step 1\n" in result.output
    header = f"Pincer exited before it was ready (exit code 1). Last 20 lines of {daemon.log_path(data_dir)}:"
    assert header in result.output
    tail = result.output[result.output.index(header) + len(header) :].strip().splitlines()
    assert tail == daemon.log_path(data_dir).read_text().splitlines()[-20:]
    assert daemon.read_state(data_dir) is None
    assert daemon_fake_child.children_under(tmp_path) == []


def test_restart_with_a_broken_config_leaves_the_running_daemon_alone(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _pid_in(runner.invoke(app, ["run", "-d"]).output, r"Pincer started \(pid (\d+)\)")

    def broken() -> None:
        raise ValueError("PINCER_DEFAULT_PROVIDER: no such provider")

    monkeypatch.setattr("pincer.config.get_settings", broken)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert "Configuration error: PINCER_DEFAULT_PROVIDER: no such provider" in result.output
    assert "Stopping" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [old]


def _attach_line(pid: int) -> str:
    return f"Pincer is already running in the background (pid {pid}) — attaching"


class _Attached:
    """A real `pincer run` in a session of its own, like one typed in a terminal, with its stdout read as it comes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            self.lines.append(raw.decode(errors="replace").rstrip("\n"))

    def wait_for(self, text: str, seconds: float = 20.0) -> int:
        """The index of the first line containing `text`, once it has been printed."""
        deadline = time.monotonic() + seconds
        while True:
            for index, line in enumerate(list(self.lines)):
                if text in line:
                    return index
            assert time.monotonic() < deadline, f"{text!r} never printed; got {self.lines[-20:]}"
            time.sleep(0.02)

    def send(self, sig: int) -> None:
        os.killpg(self.process.pid, sig)

    def finish(self, seconds: float) -> tuple[int, bytes]:
        """The exit code and stderr, failing if it hasn't exited within `seconds`."""
        returncode = self.process.wait(timeout=seconds)
        self._reader.join(5.0)
        assert self.process.stderr is not None
        return returncode, self.process.stderr.read()


@pytest.fixture
def pincer_run(data_dir: Path, tmp_path: Path) -> Iterator[Callable[..., _Attached]]:
    """Starts a real `pincer run` on this test's data_dir, from a scratch cwd (no `.env` there)."""
    cwd = tmp_path / "run-cwd"
    cwd.mkdir()
    started: list[subprocess.Popen[bytes]] = []

    def spawn(stop_timeout: float | None = None) -> _Attached:
        # A shorter stop timeout is patched into this one process, not configured: there is no knob for it.
        command = (
            pincer_command("run")
            if stop_timeout is None
            else [*pincer_command()[:3], "-P", "-c", PATCHED_STOP_TIMEOUT_RUN.format(stop_timeout)]
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            # Should it start an agent after all, it stays inside this test.
            env={
                **os.environ,
                "PINCER_DATA_DIR": str(data_dir),
                "PINCER_SKILLS_DIR": str(tmp_path / "skills"),
                "PINCER_DASHBOARD_PORT": str(free_port()),
                "PINCER_ANTHROPIC_API_KEY": "sk-ant-test",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        started.append(process)
        return _Attached(process)

    yield spawn
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_run_attaches_to_a_ready_daemon_and_follows_its_log(
    data_dir: Path, tmp_path: Path, pincer_run: Callable[..., _Attached]
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None
    append_log(data_dir, numbered(1, 80))

    attached = pincer_run()
    attached.wait_for("numbered 80")

    # Before anything a foreground start prints: no banner, no logging set up, no second instance.
    assert attached.lines[0] == _attach_line(state.pid)
    assert attached.lines[1:51] == [f"numbered {n}" for n in range(31, 81)]
    append_log(data_dir, b"live 1\n")
    attached.wait_for("live 1")
    assert not any("starting..." in line for line in attached.lines)
    assert daemon_fake_child.children_under(tmp_path) == [state.pid]


def test_run_attaches_to_a_starting_daemon_and_shows_it_come_up(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, pincer_run: Callable[..., _Attached]
) -> None:
    _child_mode(monkeypatch, "gated", data_dir)
    assert runner.invoke(app, ["run", "-d", "--wait-timeout", "0.5"]).exit_code == 1
    state = daemon.read_state(data_dir)
    assert state is not None
    assert state.state == "starting"

    attached = pincer_run()
    attached.wait_for("starting in")
    (data_dir / "mark-ready").touch()

    attached.wait_for("Pincer is running! Channels: telegram, signal.")
    assert attached.lines[0] == _attach_line(state.pid)


def test_ctrl_c_while_attached_stops_the_daemon_and_shows_its_shutdown(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, pincer_run: Callable[..., _Attached]
) -> None:
    _child_mode(monkeypatch, "slow-shutdown", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run()
    attached.wait_for(_attach_line(pid))  # printed once Ctrl+C is ours to handle

    attached.send(signal.SIGINT)

    returncode, err = attached.finish(20.0)
    assert returncode == 0, err
    assert err == b""
    # It returned only once the daemon had exited, after showing the whole graceful shutdown.
    assert not alive(pid)
    lines = attached.lines
    stopping = lines.index(f"Stopping Pincer (pid {pid})…")
    shutting_down = attached.wait_for(f"fake child {pid} shutting down")
    complete = lines.index("Shutdown complete")
    assert stopping < shutting_down < complete
    assert lines[-1] == f"Pincer stopped (pid {pid})"


def test_ctrl_c_while_attached_shows_the_last_lines_before_saying_stopped(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, pincer_run: Callable[..., _Attached]
) -> None:
    _child_mode(monkeypatch, "noisy-shutdown", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run()
    attached.wait_for(_attach_line(pid))  # printed once Ctrl+C is ours to handle

    attached.send(signal.SIGINT)

    returncode, err = attached.finish(20.0)
    assert returncode == 0, err
    last = f"shutdown step {daemon_fake_child.NOISY_SHUTDOWN_LINES}"
    assert attached.lines[-2:] == [last, f"Pincer stopped (pid {pid})"]


def test_a_second_ctrl_c_during_the_shutdown_only_detaches(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, pincer_run: Callable[..., _Attached]
) -> None:
    _child_mode(monkeypatch, "slow-shutdown", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run()
    attached.wait_for(_attach_line(pid))  # printed once Ctrl+C is ours to handle
    attached.send(signal.SIGINT)
    attached.wait_for(f"Stopping Pincer (pid {pid})…")
    # Frozen mid-shutdown: it can't finish before the second Ctrl+C, however slow this machine is.
    os.kill(pid, signal.SIGSTOP)
    try:
        attached.send(signal.SIGINT)

        returncode, err = attached.finish(5.0)

        assert returncode == 0, err
        assert err == b""
        assert attached.lines[-1] == f"Detached — Pincer (pid {pid}) keeps shutting down in the background"
        assert alive(pid)
        assert daemon.read_state(data_dir) is not None
    finally:
        os.kill(pid, signal.SIGCONT)
    # Left alone, it finishes the graceful shutdown it was asked for.
    deadline = time.monotonic() + 10.0
    while daemon.read_state(data_dir) is not None:
        assert time.monotonic() < deadline, "the daemon never finished shutting down"
        time.sleep(0.05)
    assert "Shutdown complete" in daemon.log_path(data_dir).read_text()


def test_ctrl_c_the_moment_attach_starts_still_stops_the_daemon(
    data_dir: Path, pincer_run: Callable[..., _Attached]
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run()
    attached.wait_for(_attach_line(pid))

    attached.send(signal.SIGINT)

    returncode, err = attached.finish(20.0)
    assert returncode == 0, err
    assert err == b""
    assert attached.lines[-1] == f"Pincer stopped (pid {pid})"
    assert not alive(pid)


def test_an_attached_stop_that_times_out_says_so_at_once_and_keeps_following(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, pincer_run: Callable[..., _Attached]
) -> None:
    _child_mode(monkeypatch, "ignore-sigterm", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run(stop_timeout=1.0)
    attached.wait_for(_attach_line(pid))

    attached.send(signal.SIGINT)

    hint = attached.wait_for("Pincer is still shutting down after 1 s — 'pincer stop --force' to kill it", 10.0)
    assert attached.process.poll() is None
    append_log(data_dir, b"still here\n")
    assert attached.wait_for("still here") > hint
    attached.send(signal.SIGINT)
    returncode, err = attached.finish(5.0)
    assert returncode == 0, err
    assert attached.lines[-1] == f"Detached — Pincer (pid {pid}) keeps shutting down in the background"
    assert alive(pid)


def test_attach_takes_ctrl_c_over_before_it_shows_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the operator sees first, a Ctrl+C from then on is attach's to handle, not asyncio's.

    Stands in for asyncio's runner with a handler of its own, and notes which handler is in place at
    every write and when the log relay starts (no daemon needed: the relay is replaced too).
    """
    from rich.console import Console

    from pincer.cli import daemon as cli_daemon

    seen: list[tuple[str, object]] = []

    class _Noting(io.StringIO):
        def write(self, text: str) -> int:
            seen.append((text, signal.getsignal(signal.SIGINT)))
            return super().write(text)

    class _Relay:
        def __init__(self, settings: object) -> None:
            pass

        def start(self) -> None:
            seen.append(("relay starts", signal.getsignal(signal.SIGINT)))

        def wait(self) -> None:
            pass  # the daemon has exited

    def asyncio_runner_handler(signum: int, frame: object) -> None:
        pass

    monkeypatch.setattr(cli_daemon, "console", Console(file=_Noting()))
    monkeypatch.setattr(cli_daemon, "_LogRelay", _Relay)
    state = daemon.DaemonState(4182, "2026-10-14T00:00:00+00:00", "/", 8080, "0.8.0", daemon.DaemonPhase.READY)
    previous = signal.signal(signal.SIGINT, asyncio_runner_handler)
    try:
        cli_daemon.attach_daemon(MagicMock(), state)
        after = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert seen
    assert [what for what, handler in seen if handler is not signal.default_int_handler] == []
    assert after is asyncio_runner_handler


@pytest.mark.parametrize("ctrl_c", ["printing the attach line", "right after the relay thread started"])
def test_a_ctrl_c_while_attach_starts_up_never_starts_a_second_relay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctrl_c: str
) -> None:
    """In-process: the Ctrl+C is raised where it would land, and every relay thread started is counted."""
    from rich.console import Console

    from pincer.cli import daemon as cli_daemon

    relays: list[threading.Thread] = []
    real_thread = threading.Thread

    class _Thread(real_thread):
        def start(self) -> None:
            # Read before starting: a thread drops its target once it has run.
            is_relay = getattr(getattr(self, "_target", None), "__name__", "") == "_relay"
            super().start()
            if is_relay:
                relays.append(self)
                if ctrl_c == "right after the relay thread started" and len(relays) == 1:
                    raise KeyboardInterrupt

    class _Output(io.StringIO):
        def write(self, text: str) -> int:
            if ctrl_c == "printing the attach line" and "attaching" in text:
                raise KeyboardInterrupt
            return super().write(text)

    def stop_daemon(settings: object, timeout: float, force: bool, before_report: Callable[[], None]) -> bool:
        before_report()
        return True

    monkeypatch.setattr(threading, "Thread", _Thread)
    monkeypatch.setattr(cli_daemon, "console", Console(file=_Output()))
    monkeypatch.setattr(cli_daemon, "_stop_daemon", stop_daemon)
    settings = MagicMock()
    settings.data_dir = tmp_path  # no log: a relay ends at once
    state = daemon.DaemonState(4182, "2026-10-14T00:00:00+00:00", "/", 8080, "0.8.0", daemon.DaemonPhase.READY)

    cli_daemon.attach_daemon(settings, state)

    assert len(relays) == 1


def test_a_ctrl_c_while_the_relay_is_built_goes_where_it_went_before_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relay is built before attach takes Ctrl+C over, so a Ctrl+C then is still asyncio's, and
    attach neither leaks it as an exception nor leaves its own handler behind.

    `raise_signal` runs the handler in place before it returns, so the Ctrl+C lands exactly inside
    the constructor.
    """
    from rich.console import Console

    from pincer.cli import daemon as cli_daemon

    caught: list[int] = []

    class _Relay(cli_daemon._LogRelay):
        def __init__(self, settings: Settings) -> None:
            signal.raise_signal(signal.SIGINT)
            super().__init__(settings)

    def asyncio_runner_handler(signum: int, frame: object) -> None:
        caught.append(signum)

    monkeypatch.setattr(cli_daemon, "console", Console(file=io.StringIO()))
    monkeypatch.setattr(cli_daemon, "_LogRelay", _Relay)
    settings = MagicMock()
    settings.data_dir = tmp_path  # no log: the relay ends at once
    state = daemon.DaemonState(4182, "2026-10-14T00:00:00+00:00", "/", 8080, "0.8.0", daemon.DaemonPhase.READY)
    leaked: KeyboardInterrupt | None = None
    previous = signal.signal(signal.SIGINT, asyncio_runner_handler)
    try:
        cli_daemon.attach_daemon(settings, state)
    except KeyboardInterrupt as interrupt:  # reported below, rather than aborting the whole test run
        leaked = interrupt
    finally:
        after = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, previous)

    assert leaked is None
    assert after is asyncio_runner_handler
    assert caught == [signal.SIGINT]


def test_a_hangup_while_attached_leaves_the_daemon_running(
    data_dir: Path, pincer_run: Callable[..., _Attached]
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None
    attached = pincer_run()
    attached.wait_for("starting in")

    attached.send(signal.SIGHUP)  # the terminal went away

    returncode, _ = attached.finish(5.0)
    assert returncode == -signal.SIGHUP
    time.sleep(0.5)  # a stop sent on the way out would have landed by now
    assert daemon.read_state(data_dir) == state
    assert "shutting down" not in daemon.log_path(data_dir).read_text()


def test_attached_run_ends_when_the_daemon_is_stopped_elsewhere(
    data_dir: Path, pincer_run: Callable[..., _Attached]
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    attached = pincer_run()
    attached.wait_for("starting in")

    stop_within(data_dir)

    returncode, err = attached.finish(10.0)
    assert returncode == 0, err
    assert attached.lines[-2:] == ["Shutdown complete", f"Pincer has exited (pid {pid})"]


def _record_foreground_runs(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    async def _run_agent(settings, lease=None):  # type: ignore[no-untyped-def]
        seen.append(lease)

    monkeypatch.setattr("pincer.cli.run._run_agent", _run_agent)
    return seen


def test_run_with_only_a_dead_daemons_record_starts_in_the_foreground(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10.0
    while daemon.read_state(data_dir) is not None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    seen = _record_foreground_runs(monkeypatch)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert "attaching" not in result.output
    assert "starting..." in result.output


def test_run_tasks_never_attaches_to_the_agent_daemon(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    config.get_settings().task_broker = "redis"
    workers: list[object] = []

    async def _run_tasks_worker(settings):  # type: ignore[no-untyped-def]
        workers.append(settings)

    monkeypatch.setattr("pincer.cli.run._run_tasks_worker", _run_tasks_worker)

    # Attaching would follow the daemon until it exits; give up instead of hanging the suite.
    result = within(10.0, lambda: runner.invoke(app, ["run", "tasks"]))

    assert result.exit_code == 0, result.output
    assert workers == [config.get_settings()]
    assert "attaching" not in result.output
    stop_within(data_dir)


def test_a_daemon_child_never_attaches_to_a_running_daemon(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    seen = _record_foreground_runs(monkeypatch)

    result = within(10.0, lambda: runner.invoke(app, ["run", "--daemon-child"]))

    # As before: the lease is taken, so this child gives up instead of running a second agent.
    assert isinstance(result.exception, daemon.DaemonLeaseTaken)
    assert seen == []
    assert "attaching" not in result.output
    stop_within(data_dir)


# ── platform guards ───────────────────────────────────────────────────────


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """As on Windows: the platform says so and `fcntl`, which the lifecycle module needs, can't be imported."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "fcntl", None)
    # Both, so a stray `from pincer import daemon` really imports it again (and fails): that form
    # reads the package attribute first and would never look at sys.modules.
    monkeypatch.delitem(sys.modules, "pincer.daemon", raising=False)
    monkeypatch.delattr(pincer, "daemon", raising=False)


@pytest.mark.parametrize(
    "args", [["run", "-d"], ["logs"], ["logs", "-f"], ["stop"], ["status"], ["restart"]], ids=" ".join
)
def test_every_daemon_command_refuses_on_windows(
    data_dir: Path, tmp_path: Path, windows: None, args: list[str]
) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert WINDOWS_MESSAGE in result.output
    assert "Traceback" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == []
    assert list(data_dir.iterdir()) == []


def test_plain_run_on_windows_runs_in_the_foreground_without_looking_for_a_daemon(
    data_dir: Path, windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []

    async def _run_agent(settings, lease=None):  # type: ignore[no-untyped-def]
        ran.append(settings)

    monkeypatch.setattr("pincer.cli.run._run_agent", _run_agent)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0, result.output
    assert len(ran) == 1


@pytest.fixture
def dockerenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the container check looks for Docker's marker file, pointed at a scratch path (absent until made)."""
    marker = tmp_path / "dockerenv"
    monkeypatch.setattr("pincer.cli.daemon.DOCKERENV", marker)
    return marker


@pytest.mark.parametrize("how", ["dockerenv", "pid 1"])
def test_run_detach_refuses_inside_a_container(
    data_dir: Path, tmp_path: Path, dockerenv: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    if how == "dockerenv":
        dockerenv.touch()
    else:
        monkeypatch.setattr("pincer.cli.daemon.os.getpid", lambda: 1)

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1, result.output
    assert CONTAINER_MESSAGE in result.output
    assert daemon_fake_child.children_under(tmp_path) == []
    assert list(data_dir.iterdir()) == []


def test_outside_a_container_run_detach_still_starts(data_dir: Path, dockerenv: Path) -> None:
    assert not dockerenv.exists()
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0


def test_restart_inside_a_container_refuses_before_stopping_anything(
    data_dir: Path, tmp_path: Path, dockerenv: Path
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    dockerenv.touch()

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1, result.output
    assert CONTAINER_MESSAGE in result.output
    assert "Stopping" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [pid]


def test_run_tasks_detach_is_refused_and_starts_no_worker(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []

    async def _worker(settings):  # type: ignore[no-untyped-def]
        started.append(settings)

    monkeypatch.setattr("pincer.cli.run._run_tasks_worker", _worker)

    result = runner.invoke(app, ["run", "tasks", "-d"])

    assert result.exit_code == 1, result.output
    assert "Daemon mode applies to the full agent only — run 'pincer run tasks' in the foreground." in result.output
    assert started == []
    assert daemon_fake_child.children_under(tmp_path) == []
    assert list(data_dir.iterdir()) == []


@pytest.mark.parametrize("args", [["status"], ["stop"], ["logs"]], ids=" ".join)
def test_inside_a_container_the_read_and_stop_commands_still_answer(
    data_dir: Path, dockerenv: Path, args: list[str]
) -> None:
    dockerenv.touch()

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert CONTAINER_MESSAGE not in result.output


# ── a daemon past its lease (review #2) ────────────────────────────────────


def _lingering(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Through the CLI: a daemon that has left its lease but not yet exited; its pid."""
    _child_mode(monkeypatch, "linger", data_dir)
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    pid = daemon.read_state(data_dir).pid  # type: ignore[union-attr]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while (data_dir / daemon.PID_FILE).exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    _child_mode(monkeypatch, "ready", data_dir)
    return pid


def test_run_detach_while_a_daemon_lingers_says_it_is_shutting_down(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _lingering(data_dir, monkeypatch)

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1, result.output
    assert SHUTTING_DOWN.format(data_dir=data_dir) in result.output
    assert "exited before it was ready" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [pid]


def test_status_while_a_daemon_lingers_says_it_is_shutting_down(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lingering(data_dir, monkeypatch)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert f"Pincer is shutting down (data dir {data_dir}) — its lock is held, but its record is gone" in result.output
    assert "not running" not in result.output


def test_stop_while_a_daemon_lingers_waits_for_it_without_signalling(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lingering(data_dir, monkeypatch)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    why = "it has no record (it may be shutting down), so its process can't be identified"
    assert result.output.splitlines() == [NOT_SIGNALLED.format(data_dir=data_dir, why=why), "Pincer has exited"]
    assert daemon_fake_child.children_under(tmp_path) == []


def test_stop_while_a_daemon_lingers_gives_up_after_its_timeout(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _lingering(data_dir, monkeypatch)

    result = within(10.0, lambda: runner.invoke(app, ["stop", "--timeout", "0.5"]))

    assert result.exit_code == 1, result.output
    why = "it has no record (it may be shutting down), so its process can't be identified"
    assert result.output.splitlines() == [
        NOT_SIGNALLED.format(data_dir=data_dir, why=why),
        STILL_HELD.format(data_dir=data_dir, timeout=0.5),
    ]
    assert daemon_fake_child.children_under(tmp_path) == [pid]


def test_restart_while_a_daemon_lingers_waits_then_starts(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _lingering(data_dir, monkeypatch)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 0, result.output
    assert "Pincer has exited" in result.output
    new = _pid_in(result.output, r"Pincer started \(pid (\d+)\)")
    assert new != old
    assert daemon_fake_child.children_under(tmp_path) == [new]


def test_plain_run_while_a_daemon_lingers_says_it_is_shutting_down(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []

    async def _run_agent(settings, lease=None):  # type: ignore[no-untyped-def]
        ran.append(settings)

    _lingering(data_dir, monkeypatch)
    monkeypatch.setattr("pincer.cli.run._run_agent", _run_agent)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1, result.output
    assert SHUTTING_DOWN.format(data_dir=data_dir) in result.output
    assert ran == []


def test_run_detach_starts_a_daemon_that_imports_the_tree_under_test(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0

    assert f"imports pincer from {SRC_DIR / 'pincer' / '__init__.py'}" in daemon.log_path(data_dir).read_text()


def test_status_of_a_record_with_a_malformed_start_time_says_so_without_a_traceback(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "started_at": "yesterday"}))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Pincer is running" not in result.output


def _from_a_newer_version(data_dir: Path, *, start_token: bool = True) -> int:
    """A running daemon whose record names a phase this version doesn't know (and, unless
    `start_token` is False, its start token, so stop can still tell its process); its pid."""
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    if not start_token:
        del record["start_token"]
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "state": "draining"}))
    return int(record["pid"])


def test_run_detach_beside_a_daemon_with_a_record_it_cant_read_says_so(data_dir: Path, tmp_path: Path) -> None:
    pid = _from_a_newer_version(data_dir)

    result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1, result.output
    assert UNREADABLE.format(pid=pid, data_dir=data_dir) in result.output
    assert "shutting down" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [pid]


def test_status_of_a_daemon_with_a_record_it_cant_read_says_so(data_dir: Path) -> None:
    pid = _from_a_newer_version(data_dir)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert UNREADABLE.format(pid=pid, data_dir=data_dir) in result.output
    assert "shutting down" not in result.output


@pytest.mark.parametrize("force", [[], ["--force"]], ids=["stop", "force"])
def test_stop_stops_a_daemon_whose_record_it_cant_read_by_the_pid_and_token_it_still_names(
    data_dir: Path, tmp_path: Path, force: list[str]
) -> None:
    pid = _from_a_newer_version(data_dir)

    result = within(15.0, lambda: runner.invoke(app, ["stop", "--timeout", "10", *force]))

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"Stopping Pincer (pid {pid} — its record can't be read by this version)…",
        f"Pincer {'killed' if force else 'stopped'} (pid {pid})",
    ]
    assert daemon_fake_child.children_under(tmp_path) == []


@pytest.mark.parametrize("force", [[], ["--force"]], ids=["stop", "force"])
def test_stop_never_signals_a_daemon_whose_record_it_cant_read_and_names_no_start_token(
    data_dir: Path, tmp_path: Path, force: list[str]
) -> None:
    pid = _from_a_newer_version(data_dir, start_token=False)
    assert UNREADABLE_UNIDENTIFIED.format(pid=pid, data_dir=data_dir) in runner.invoke(app, ["status"]).output

    began = time.monotonic()
    result = within(10.0, lambda: runner.invoke(app, ["stop", *force]))  # the default 90 s timeout

    assert time.monotonic() - began < 2.0
    assert result.exit_code == 1, result.output
    why = f"its record can't be read by this version (it names pid {pid}), so its process can't be identified"
    assert result.output.splitlines() == [NOT_SIGNALLED_AT_ONCE.format(data_dir=data_dir, why=why)]
    assert daemon_fake_child.children_under(tmp_path) == [pid]


def test_plain_run_beside_a_daemon_with_a_record_it_cant_read_says_so(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _from_a_newer_version(data_dir)
    ran = _record_foreground_runs(monkeypatch)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1, result.output
    assert UNREADABLE.format(pid=pid, data_dir=data_dir) in result.output
    assert ran == []


def test_restart_replaces_a_daemon_whose_record_it_cant_read(data_dir: Path, tmp_path: Path) -> None:
    old = _from_a_newer_version(data_dir)

    result = runner.invoke(app, ["restart", "--timeout", "10"])  # in this thread: run -d's wait takes Ctrl+C over

    assert result.exit_code == 0, result.output
    assert f"Pincer stopped (pid {old})" in result.output
    new = _pid_in(result.output, r"Pincer started \(pid (\d+)\)")
    assert daemon_fake_child.children_under(tmp_path) == [new]


def test_restart_leaves_a_daemon_whose_record_it_cant_read_and_names_no_start_token_alone(
    data_dir: Path, tmp_path: Path
) -> None:
    old = _from_a_newer_version(data_dir, start_token=False)

    result = within(10.0, lambda: runner.invoke(app, ["restart"]))

    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[-1] == "Not starting a new one."
    assert daemon_fake_child.children_under(tmp_path) == [old]


def test_ctrl_c_while_run_detach_waits_stops_waiting_at_once_and_leaves_the_daemon_starting(
    data_dir: Path, tmp_path: Path
) -> None:
    cwd = tmp_path / "detach-cwd"
    cwd.mkdir()
    fake = json.dumps(daemon_fake_child.argv("gated", data_dir))  # never ready on its own
    process = subprocess.Popen(
        [*pincer_command()[:3], "-P", "-c", PATCHED_CHILD_RUN_DETACH, fake],
        cwd=cwd,
        env={
            **os.environ,
            "PINCER_DATA_DIR": str(data_dir),
            "PINCER_DASHBOARD_PORT": str(free_port()),
            "PINCER_ANTHROPIC_API_KEY": "sk-ant-test",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # like a command typed in a terminal: Ctrl+C goes to its group only
    )
    try:
        detaching = _Attached(process)
        detaching.wait_for("marker from the caller's environment")  # the daemon is up, starting
        [child] = daemon_fake_child.children_under(tmp_path)

        detaching.send(signal.SIGINT)
        returncode, err = detaching.finish(seconds=5)

        assert returncode == 130, err
        out = "\n".join(detaching.lines)
        assert "Stopped waiting" in out
        assert "Pincer started" not in out
        assert b"Traceback" not in err
        assert daemon_fake_child.children_under(tmp_path) == [child]
        assert getattr(daemon.read_state(data_dir), "state", None) == "starting"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.parametrize("force", [[], ["--force"]], ids=["stop", "force"])
def test_stop_never_signals_a_pid_its_record_names_that_now_belongs_to_another_process(
    data_dir: Path, tmp_path: Path, force: list[str]
) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    daemon_pid = record["pid"]
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "pid": bystander.pid}))

        began = time.monotonic()
        result = within(10.0, lambda: runner.invoke(app, ["stop", *force]))  # the default 90 s timeout

        assert time.monotonic() - began < 2.0
        assert result.exit_code == 1, result.output
        why = f"the pid its record names ({bystander.pid}) now belongs to another process, not Pincer"
        assert result.output.splitlines() == [NOT_SIGNALLED_AT_ONCE.format(data_dir=data_dir, why=why)]
        assert daemon_fake_child.children_under(tmp_path) == [daemon_pid]
        time.sleep(0.2)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_status_of_a_record_with_a_naive_start_time_says_so_without_a_traceback(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    record = json.loads((data_dir / daemon.PID_FILE).read_text())
    (data_dir / daemon.PID_FILE).write_text(json.dumps({**record, "started_at": "2026-10-14T10:00:00"}))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert UNREADABLE.format(pid=record["pid"], data_dir=data_dir) in result.output


def _a_start_stuck_before_its_lease(data_dir: Path, tmp_path: Path) -> int:
    result = daemon.start(
        data_dir, daemon_fake_child.argv("never-lease", data_dir), data_dir, dict(os.environ), wait_timeout=0.0
    )
    assert result.outcome is daemon.StartOutcome.TIMED_OUT
    [stuck] = daemon_fake_child.children_under(tmp_path)
    return stuck


def test_run_detach_behind_a_stuck_start_names_it_and_spawns_nothing(data_dir: Path, tmp_path: Path) -> None:
    stuck = _a_start_stuck_before_its_lease(data_dir, tmp_path)

    result = runner.invoke(app, ["run", "-d", "--wait-timeout", "0.5"])

    assert result.exit_code == 1, result.output
    assert START_IN_PROGRESS.format(pid=stuck) in result.output
    assert "keeps running" not in result.output
    assert daemon_fake_child.children_under(tmp_path) == [stuck]


@pytest.mark.parametrize(("args", "code"), [(["status"], 0), (["stop"], 1)], ids=["status", "stop"])
def test_status_and_stop_report_a_start_in_progress(data_dir: Path, tmp_path: Path, args: list[str], code: int) -> None:
    stuck = _a_start_stuck_before_its_lease(data_dir, tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == code, result.output
    assert START_IN_PROGRESS.format(pid=stuck) in result.output
    assert "not running" not in result.output


@pytest.mark.parametrize("args", [["restart"], ["run"]], ids=["restart", "plain run"])
def test_restart_and_plain_run_refuse_beside_a_start_in_progress(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    stuck = _a_start_stuck_before_its_lease(data_dir, tmp_path)
    ran = _record_foreground_runs(monkeypatch)

    result = within(10.0, lambda: runner.invoke(app, args))

    assert result.exit_code == 1, result.output
    assert START_IN_PROGRESS.format(pid=stuck) in result.output
    assert ran == []
    assert daemon_fake_child.children_under(tmp_path) == [stuck]

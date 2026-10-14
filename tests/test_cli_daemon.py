"""Tests for `pincer run -d` / `pincer stop` (typer.testing.CliRunner), against the fake child."""

from __future__ import annotations

import contextlib
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
from typer.testing import CliRunner, Result

from pincer import config, daemon
from pincer.cli import app
from pincer.cli.daemon import format_uptime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    Spawn = Callable[..., subprocess.Popen[bytes]]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="daemon mode is POSIX-only")

runner = CliRunner()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fake_children(tmp_path: Path) -> list[str]:
    found = subprocess.run(["pgrep", "-f", f"daemon_fake_child.py .* {tmp_path}"], capture_output=True, text=True)
    return found.stdout.split()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    settings = MagicMock()
    settings.data_dir = path
    settings.dashboard_host = "127.0.0.1"
    settings.dashboard_port = _free_port()
    settings.log_level.value = "WARNING"
    settings.telemetry_dsn = None
    settings.daily_budget_usd = 5.0
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    monkeypatch.setattr("pincer.cli.daemon._child_argv", lambda: daemon_fake_child.argv("ready", path))
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

    result = runner.invoke(app, ["run", "--daemon-child"])

    assert result.exit_code == 0, result.output
    assert states == ["starting", "ready"]


def _child_mode(monkeypatch: pytest.MonkeyPatch, mode: str, data_dir: Path) -> None:
    monkeypatch.setattr("pincer.cli.daemon._child_argv", lambda: daemon_fake_child.argv(mode, data_dir))


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
    daemon.stop(data_dir)


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
    assert _fake_children(tmp_path) == [str(state.pid)]


def test_run_detach_refuses_while_something_else_holds_the_port(data_dir: Path, tmp_path: Path) -> None:
    port = config.get_settings().dashboard_port
    with socket.create_server(("127.0.0.1", port)):
        result = runner.invoke(app, ["run", "-d"])

    assert result.exit_code == 1
    assert f"A foreground Pincer is running on :{port} — stop it with Ctrl+C in its terminal" in result.output
    assert _fake_children(tmp_path) == []
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

    timed_out = daemon_fake_child.within(10.0, lambda: runner.invoke(app, ["stop", "--timeout", "1"]))

    assert timed_out.exit_code == 1
    assert "Pincer is still shutting down after 1 s — 'pincer stop --force' to kill it" in timed_out.output
    assert _fake_children(tmp_path) == [str(pid)]

    forced = daemon_fake_child.within(10.0, lambda: runner.invoke(app, ["stop", "--force"]))

    assert forced.exit_code == 0, forced.output
    assert f"Pincer killed (pid {pid})" in forced.output
    assert _fake_children(tmp_path) == []
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


# Rich markup, a CRLF line, a byte that isn't UTF-8 and no final newline: whatever a decode or a console would change.
RAW_LOG = b"plain line\n[bold]markup[/bold] kept\r\nbad byte \xff here\nno newline at the end"
NOT_RUNNING_NOTE = "Pincer is not running — showing the last session's log"
NO_LOGS = "No logs yet — start with 'pincer run -d'"


def _write_log(data_dir: Path, content: bytes) -> None:
    log = daemon.log_path(data_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(content)


def _append_log(data_dir: Path, content: bytes) -> None:
    with daemon.log_path(data_dir).open("ab") as log:
        log.write(content)


def _numbered(first: int, last: int) -> bytes:
    return b"".join(f"numbered {n}\n".encode() for n in range(first, last + 1))


@pytest.fixture
def pincer_logs(data_dir: Path, tmp_path: Path) -> Iterator[Spawn]:
    """Runs a real `pincer logs …` process on this test's data_dir, from a scratch cwd (no `.env` there)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    started: list[subprocess.Popen[bytes]] = []

    def spawn(*args: str, stderr: int = subprocess.PIPE, **env: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "pincer", "logs", *args],
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
    _write_log(data_dir, RAW_LOG)

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == RAW_LOG


@pytest.mark.parametrize("flag", ["-n", "--tail"])
def test_logs_tail_prints_exactly_the_last_lines(data_dir: Path, flag: str) -> None:
    _write_log(data_dir, _numbered(1, 150))

    result = runner.invoke(app, ["logs", flag, "100"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == _numbered(51, 150)


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
    _append_log(data_dir, _numbered(1, 80))

    result = daemon_fake_child.within(10.0, lambda: runner.invoke(app, ["logs", "-f"]))

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == _numbered(31, 80)
    assert NOT_RUNNING_NOTE in result.stderr


def test_logs_of_a_running_daemon_has_no_note(data_dir: Path) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == daemon.log_path(data_dir).read_bytes()
    assert result.stderr == ""
    daemon.stop(data_dir)


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
    _append_log(data_dir, _numbered(1, 80))
    process = pincer_logs("-f")
    assert process.stdout is not None

    shown = b"".join(daemon_fake_child.within(20.0, process.stdout.readline) for _ in range(50))
    assert shown == _numbered(31, 80)
    _append_log(data_dir, b"live 1\n")
    assert daemon_fake_child.within(10.0, process.stdout.readline) == b"live 1\n"
    assert process.poll() is None

    daemon.stop(data_dir)

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
        _append_log(data_dir, f"live {n}\n".encode())
        with contextlib.suppress(queue.Empty):
            first = lines.get(timeout=0.3)
            break
        assert time.monotonic() < deadline, "nothing followed"

    assert re.fullmatch(rb"live \d+\n", first), first
    daemon.stop(data_dir)
    assert process.wait(timeout=10) == 0


def test_ctrl_c_in_logs_follow_only_detaches(data_dir: Path, pincer_logs: Spawn) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    state = daemon.read_state(data_dir)
    assert state is not None
    process = pincer_logs("-f")
    assert process.stdout is not None
    daemon_fake_child.within(20.0, process.stdout.readline)

    os.killpg(process.pid, signal.SIGINT)

    _, err = process.communicate(timeout=5)
    assert process.returncode == 0, err
    assert b"Traceback" not in err
    assert daemon.read_state(data_dir) == state
    os.kill(state.pid, 0)
    daemon.stop(data_dir)


def test_logs_writes_the_file_bytes_even_when_colour_is_forced(data_dir: Path, pincer_logs: Spawn) -> None:
    _write_log(data_dir, RAW_LOG)
    process = pincer_logs(FORCE_COLOR="1", TERM="xterm-256color")

    out, err = process.communicate(timeout=20)

    assert process.returncode == 0, err
    assert out == RAW_LOG


@pytest.mark.parametrize("args", [[], ["-f"]], ids=["plain", "follow"])
def test_logs_into_a_pipe_closed_early_exits_quietly(data_dir: Path, pincer_logs: Spawn, args: list[str]) -> None:
    assert runner.invoke(app, ["run", "-d"]).exit_code == 0
    # Far more than a pipe buffers, so a plain `logs` is still writing when the reader goes.
    _append_log(data_dir, _numbered(1, 20000))
    process = pincer_logs(*args)
    assert process.stdout is not None and process.stderr is not None
    daemon_fake_child.within(20.0, process.stdout.readline)

    process.stdout.close()  # `| head -1`
    _append_log(data_dir, b"written after the reader left\n")

    assert process.wait(timeout=10) == 0, process.stderr.read()
    # No traceback, and no "Exception ignored … BrokenPipeError" from the interpreter's exit-time flush.
    assert process.stderr.read() == b""
    daemon.stop(data_dir)


def test_the_not_running_note_reaches_a_terminal_while_stdout_is_piped(data_dir: Path, pincer_logs: Spawn) -> None:
    _write_log(data_dir, RAW_LOG)
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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)


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
    daemon.stop(data_dir)

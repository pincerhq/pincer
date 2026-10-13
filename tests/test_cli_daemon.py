"""Tests for `pincer run -d` / `pincer stop` (typer.testing.CliRunner), against the fake child."""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import daemon_fake_child
import pytest
from typer.testing import CliRunner

from pincer import config, daemon
from pincer.cli import app
from pincer.cli.daemon import format_uptime

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

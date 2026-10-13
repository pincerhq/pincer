"""Tests for `pincer run -d` / `pincer stop` (typer.testing.CliRunner), against the fake child."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import daemon_fake_child
import pytest
from typer.testing import CliRunner

from pincer import daemon
from pincer.cli import app

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="daemon mode is POSIX-only")

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    settings = MagicMock()
    settings.data_dir = path
    settings.dashboard_port = 8080
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

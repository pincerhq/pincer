"""Tests for CLI commands using typer.testing.CliRunner."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    """--help exits 0 and output contains 'Pincer'."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Pincer" in result.output


def test_config_no_crash() -> None:
    """config command doesn't crash (may show error about missing keys)."""
    result = runner.invoke(app, ["config"])
    # Should not raise; may exit 0 or show config
    assert result.exit_code in (0, 1)
    assert "Configuration" in result.output or "Error" in result.output or "Provider" in result.output


def test_cost_shows_table() -> None:
    """cost command runs (may error but doesn't crash)."""
    result = runner.invoke(app, ["cost"])
    # May error if DB not initialized, but should not crash
    assert "Today" in result.output or "Total" in result.output or "Error" in result.output


def test_doctor_runs() -> None:
    """doctor command runs (exit code 0 or shows config table)."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Configuration" in result.output or "Check" in result.output


def test_skills_list_shows_header() -> None:
    """skills list output contains 'Installed Skills' or 'Name'."""
    result = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    assert "Installed Skills" in result.output or "Name" in result.output


def test_skills_create_scaffolds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """skills create scaffolds manifest.json and skill.py."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    result = runner.invoke(app, ["skills", "create", "testskill"])
    assert result.exit_code == 0
    assert (tmp_path / "skills" / "testskill" / "manifest.json").exists()
    assert (tmp_path / "skills" / "testskill" / "skill.py").exists()


def test_skills_create_fails_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """skills create fails when directory already exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "testskill").mkdir()
    result = runner.invoke(app, ["skills", "create", "testskill"])
    assert result.exit_code == 1
    assert "already exists" in result.output or "exists" in result.output.lower()


def test_skills_install_fails_nonexistent() -> None:
    """skills install /nonexistent fails."""
    result = runner.invoke(app, ["skills", "install", "/nonexistent"])
    assert result.exit_code == 1
    assert "Not a directory" in result.output or "directory" in result.output.lower()


def test_skills_install_fails_invalid(tmp_path: Path) -> None:
    """skills install fails for directory without manifest.json."""
    invalid_skill = tmp_path / "invalid_skill"
    invalid_skill.mkdir()
    # No manifest.json, no skill.py
    (invalid_skill / "random.txt").write_text("x")
    result = runner.invoke(app, ["skills", "install", str(invalid_skill)])
    assert result.exit_code == 1
    assert "Invalid skill" in result.output or "manifest" in result.output.lower()


def test_skills_scan_fails_nonexistent() -> None:
    """skills scan /nonexistent fails."""
    result = runner.invoke(app, ["skills", "scan", "/nonexistent"])
    assert result.exit_code == 1
    assert "Not a directory" in result.output or "directory" in result.output.lower()

"""Tests for `pincer skills rebuild`."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from pincer.cli.main import app
from pincer.services.skills import SkillService, reset_skill_service

runner = CliRunner()


def _make_skill(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nx\n", encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path):
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    settings.skills_cache_dir = tmp_path / "cache"
    settings.skills_index_watch_interval_seconds = 300
    (tmp_path / "bundled").mkdir()
    reset_skill_service()
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", tmp_path / "bundled"),
    ):
        yield settings
    reset_skill_service()


def test_rebuild_lists_skills_and_announces_the_change(settings, tmp_path: Path) -> None:
    running = SkillService(bundled_dir=None, user_dir=tmp_path / "user", cache_dir=tmp_path / "cache")
    running.ensure_current()
    _make_skill(tmp_path / "user", "fresh")

    result = runner.invoke(app, ["skills", "rebuild"])

    assert result.exit_code == 0, result.output
    assert "fresh" in result.output
    assert "Rebuilt skill index: 1 skill(s)" in result.output
    assert "within 300s" in result.output
    # A process that was already running sees the rebuild without restarting.
    assert [e.name for e in running.ensure_current().all_skills()] == ["fresh"]


def test_rebuild_clears_remote_cache_unless_kept(settings) -> None:
    with patch("pincer.services.skills.clear_remote_cache") as clear:
        assert runner.invoke(app, ["skills", "rebuild"]).exit_code == 0
        assert clear.call_count == 1
        assert runner.invoke(app, ["skills", "rebuild", "--keep-cache"]).exit_code == 0
        assert clear.call_count == 1


def test_rebuild_reports_failure(settings) -> None:
    with patch.object(SkillService, "rebuild", side_effect=RuntimeError("bucket on fire")):
        result = runner.invoke(app, ["skills", "rebuild"])
    assert result.exit_code == 1
    assert "bucket on fire" in result.output


def test_rebuild_refuses_an_unusable_remote_root(settings) -> None:
    from pincer.exceptions import SkillLoadError

    settings.skills_dir = "s3://bucket/team"
    with patch(
        "pincer.services.skills.open_usable_skills_root",
        side_effect=SkillLoadError("Skills root s3://bucket/team is not usable: NoCredentialsError. Configure X"),
    ):
        result = runner.invoke(app, ["skills", "rebuild"])
    assert result.exit_code == 1
    assert "not usable" in result.output

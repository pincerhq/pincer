"""Tests for src/pincer/api/skills.py — the dedicated SKILL.md skills endpoint."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from pincer.api.server import create_app
from pincer.api.skills import _skill_detail, _skill_entries

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


# ── _skill_entries ────────────────────────────────────────────────────────────


def test_skill_entries_empty_when_no_dirs(tmp_path: Path) -> None:
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", tmp_path / "bundled"),
    ):
        assert _skill_entries() == []


def test_skill_entries_discovers_skill_md(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    (bundled / "demo").mkdir(parents=True)
    (bundled / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nbody\n", encoding="utf-8"
    )
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        entries = _skill_entries()

    assert len(entries) == 1
    assert entries[0]["name"] == "demo"
    assert entries[0]["description"] == "demo skill"
    assert entries[0]["source"] == "file"
    assert entries[0]["root"] == "bundled"
    assert entries[0]["dir"] == "demo"


def test_skill_entries_dir_reflects_disk_name_even_when_frontmatter_name_differs(tmp_path: Path) -> None:
    """Regression for PR #167 review: dashboard links must use `dir`, not `name`,
    since GET /api/skills/{name} matches the directory, not the frontmatter name."""
    bundled = tmp_path / "bundled"
    skill_dir = bundled / "on-disk-folder"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: A Totally Different Display Name\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        entries = _skill_entries()

    assert entries[0]["name"] == "A Totally Different Display Name"
    assert entries[0]["dir"] == "on-disk-folder"


def test_skill_entries_bundled_and_user(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    (bundled / "a").mkdir(parents=True)
    (bundled / "a" / "SKILL.md").write_text("---\nname: a\ndescription: d\n---\nx\n", encoding="utf-8")
    (user / "b").mkdir(parents=True)
    (user / "b" / "SKILL.md").write_text("---\nname: b\ndescription: d\n---\nx\n", encoding="utf-8")

    settings = MagicMock()
    settings.skills_dir = user
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        entries = _skill_entries()

    roots = {e["name"]: e["root"] for e in entries}
    assert roots == {"a": "bundled", "b": "user"}


# ── _skill_detail ────────────────────────────────────────────────────────────


def test_skill_detail_returns_none_for_unknown(tmp_path: Path) -> None:
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", tmp_path / "bundled"),
    ):
        assert _skill_detail("nope") is None


def test_skill_detail_returns_body_and_files(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    skill_dir = bundled / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n# Demo body\n", encoding="utf-8"
    )
    (skill_dir / "notes.txt").write_text("x", encoding="utf-8")

    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        detail = _skill_detail("demo")

    assert detail["name"] == "demo"
    assert detail["source"] == "file"
    assert detail["root"] == "bundled"
    assert detail["dir"] == "demo"
    assert detail["body"] == "# Demo body"
    assert detail["files"] == ["notes.txt"]


def test_skill_detail_matches_by_directory_name_not_frontmatter_name(tmp_path: Path) -> None:
    """Lookup is by the actual folder on disk, per the API contract."""
    bundled = tmp_path / "bundled"
    skill_dir = bundled / "on-disk-folder"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: different-frontmatter-name\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        assert _skill_detail("on-disk-folder") is not None
        assert _skill_detail("different-frontmatter-name") is None


# ── GET /api/skills ─────────────────────────────────────────────────────────────


def test_list_skills_returns_200(client: TestClient) -> None:
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    assert "skills" in resp.json()


def test_list_skills_is_not_deprecated(client: TestClient) -> None:
    """The endpoint's OpenAPI entry should not carry the legacy deprecated flag."""
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/skills"]["get"].get("deprecated") is not True


def test_list_skills_includes_bundled_starter_skills(client: TestClient) -> None:
    resp = client.get("/api/skills")
    bundled = [e for e in resp.json()["skills"] if e["root"] == "bundled"]
    assert len(bundled) == 5


def test_list_skills_only_returns_file_source(client: TestClient) -> None:
    resp = client.get("/api/skills")
    sources = {e["source"] for e in resp.json()["skills"]}
    assert sources <= {"file"}


def test_list_skills_excludes_builtin_tools(client: TestClient) -> None:
    """Built-in tools are served by /api/integrations, not /api/skills."""
    resp = client.get("/api/skills")
    names = {e["name"] for e in resp.json()["skills"]}
    assert "file_read" not in names


def test_list_skills_excludes_integrations(client: TestClient) -> None:
    resp = client.get("/api/skills")
    names = {e["name"] for e in resp.json()["skills"]}
    assert "Google Workspace" not in names
    assert "Slack" not in names


# ── GET /api/skills/{name} ───────────────────────────────────────────────────


def test_get_skill_returns_200_with_body(client: TestClient) -> None:
    """Looked up by directory name on disk — 'skill-authoring' regardless of frontmatter name."""
    resp = client.get("/api/skills/skill-authoring")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "file"
    assert data["root"] == "bundled"
    assert data["dir"] == "skill-authoring"
    assert "# Authoring a Pincer skill" in data["body"]
    assert isinstance(data["files"], list)


def test_get_skill_unknown_is_404(client: TestClient) -> None:
    resp = client.get("/api/skills/does-not-exist")
    assert resp.status_code == 404

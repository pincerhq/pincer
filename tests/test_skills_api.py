"""Tests for src/pincer/api/skills.py — the dedicated SKILL.md skills endpoint."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from pincer.api.server import create_app
from pincer.api.skills import _build_index, _skill_detail, _skill_entries
from pincer.services.skills import SkillService, reset_skill_service

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _fresh_skill_service():
    reset_skill_service()
    yield
    reset_skill_service()


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    # Real settings would read the developer's ~/.pincer/skills and leave the stamp
    # and disk ID in the shared PINCER_DATA_DIR; keep both inside this test.
    monkeypatch.setenv("PINCER_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("PINCER_SKILLS_CACHE_DIR", str(tmp_path / "skills-cache"))
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


# ── _skill_entries ────────────────────────────────────────────────────────────


def test_skill_entries_empty_when_no_dirs(tmp_path: Path) -> None:
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
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
    settings.skills_index_cache_ttl_seconds = 0
    settings.skills_cache_dir = tmp_path / "cache"
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", bundled),
    ):
        assert _skill_detail("on-disk-folder") is not None
        assert _skill_detail("different-frontmatter-name") is None


# ── index freshness ──────────────────────────────────────────────────────────


def _cache_settings(tmp_path: Path, ttl: int) -> MagicMock:
    settings = MagicMock()
    settings.skills_dir = tmp_path / "user"
    settings.skills_max_loaded_per_root = 100
    settings.skills_index_cache_ttl_seconds = ttl
    settings.skills_cache_dir = tmp_path / "cache"
    return settings


def _add_user_skill(tmp_path: Path, name: str) -> None:
    skill_dir = tmp_path / "user" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nx\n", encoding="utf-8")


def _user_names() -> list[str]:
    return [e["name"] for e in _skill_entries() if e["root"] == "user"]


@pytest.fixture
def api_settings(tmp_path: Path):
    settings = _cache_settings(tmp_path, ttl=60)
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", tmp_path / "bundled"),
    ):
        yield settings


def test_index_is_reused_within_max_age(api_settings, tmp_path: Path) -> None:
    with patch("pincer.services.skills.monotonic", return_value=100.0):
        assert _user_names() == []
    _add_user_skill(tmp_path, "late")

    with patch("pincer.services.skills.monotonic", return_value=159.0):
        assert _user_names() == []  # 59s: still the same scan
    with patch("pincer.services.skills.monotonic", return_value=160.0):
        assert _user_names() == ["late"]  # 60s: rescanned


def test_ttl_zero_rescans_every_request(api_settings, tmp_path: Path) -> None:
    api_settings.skills_index_cache_ttl_seconds = 0
    assert _user_names() == []
    _add_user_skill(tmp_path, "late")
    assert _user_names() == ["late"]


def test_index_change_in_another_process_invalidates_immediately(api_settings, tmp_path: Path) -> None:
    """`pincer skills rebuild` (another process) must not wait out the API's max age."""
    assert _user_names() == []
    _add_user_skill(tmp_path, "late")

    SkillService(bundled_dir=None, user_dir=tmp_path / "user", cache_dir=tmp_path / "cache").rebuild()

    assert _user_names() == ["late"]


def test_api_shares_the_process_index(api_settings) -> None:
    """The dashboard and the agent read one index per process."""
    from pincer.services.skills import get_skill_service

    assert _build_index() is get_skill_service().index


def test_skill_entries_from_mocked_s3_root(tmp_path: Path) -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client(local_storage_dir=tmp_path / "storage", local_cache_dir=tmp_path / "cache")
    root = client.CloudPath("s3://bucket/team")
    (root / "remote-skill" / "SKILL.md").write_text("---\nname: remote\ndescription: d\n---\nhi\n", encoding="utf-8")
    (root / "remote-skill" / "ref.md").write_text("x", encoding="utf-8")

    settings = _cache_settings(tmp_path, ttl=0)
    settings.skills_dir = "s3://bucket/team"
    with (
        patch("pincer.config.get_settings_relaxed", return_value=settings),
        patch("pincer.tools.skills.index.BUNDLED_SKILLS_DIR", tmp_path / "bundled"),
        patch("pincer.tools.skills.storage._build_client", return_value=client),
    ):
        entries = _skill_entries()
        detail = _skill_detail("remote-skill")

    assert entries == [
        {
            "name": "remote",
            "description": "d",
            "status": "active",
            "source": "file",
            "root": "user",
            "dir": "remote-skill",
        }
    ]
    assert detail["body"] == "hi"
    assert detail["files"] == ["ref.md"]


# ── POST /api/skills/rebuild ─────────────────────────────────────────────────


@pytest.fixture
def queued_jobs():
    """Captures what the endpoint sends to the task queue."""
    from unittest.mock import AsyncMock

    from pincer.tasks import app as task_app  # the Repid instance (it shadows the submodule)

    send = AsyncMock()
    with patch.object(task_app, "send_message_json", send):
        yield send


def test_rebuild_endpoint_rebuilds_in_app_then_queues_the_job(api_settings, tmp_path: Path, queued_jobs) -> None:
    from pincer.api.skills import rebuild_skills
    from pincer.services.skills import get_skill_service

    assert _user_names() == []
    _add_user_skill(tmp_path, "late")

    body = asyncio.run(rebuild_skills(keep_cache=False))

    # The app's own index is rebuilt before the job is queued.
    assert [s["name"] for s in body["skills"] if s["root"] == "user"] == ["late"]
    assert _user_names() == ["late"]
    assert body["generation"] == get_skill_service().generation
    assert body["generation"] == (tmp_path / "cache" / "index-generation").read_text(encoding="utf-8")
    assert body["cache_cleared"] is True
    assert body["job"] == {"queued": True, "topic": "rebuild_skill_index"}
    queued_jobs.assert_awaited_once_with(
        channel="pincer_tasks",
        payload={"generation": body["generation"], "clear_cache": True, "disk_id": get_skill_service().disk_id},
        headers={"topic": "rebuild_skill_index"},
    )


def test_rebuild_endpoint_keep_cache(api_settings, queued_jobs) -> None:
    from pincer.api.skills import rebuild_skills

    with patch("pincer.services.skills.clear_remote_cache") as clear:
        body = asyncio.run(rebuild_skills(keep_cache=True))
    clear.assert_not_called()
    assert body["cache_cleared"] is False
    assert queued_jobs.await_args.kwargs["payload"]["clear_cache"] is False


def test_rebuild_endpoint_reports_a_failed_enqueue_without_failing(api_settings, queued_jobs) -> None:
    from pincer.api.skills import rebuild_skills

    queued_jobs.side_effect = ConnectionError("redis down")
    body = asyncio.run(rebuild_skills(keep_cache=False))

    assert body["job"]["queued"] is False
    assert "redis down" in body["job"]["error"]
    assert body["generation"]  # the in-app rebuild still happened


def test_rebuild_endpoint_over_http(client: TestClient, queued_jobs) -> None:
    resp = client.post("/api/skills/rebuild?keep_cache=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cache_cleared"] is False
    assert data["job"]["queued"] is True
    assert {s["root"] for s in data["skills"]} == {"bundled"}


def test_rebuild_endpoint_failure_is_a_500(client: TestClient, queued_jobs) -> None:
    from pincer.services.skills import SkillService

    with patch.object(SkillService, "rebuild", side_effect=RuntimeError("bucket on fire")):
        resp = client.post("/api/skills/rebuild")
    assert resp.status_code == 500
    assert "bucket on fire" in resp.json()["detail"]
    queued_jobs.assert_not_awaited()


def test_rebuild_endpoint_unreachable_root_is_a_503_and_announces_nothing(
    client: TestClient, queued_jobs, tmp_path: Path
) -> None:
    """PR review: an outage during a rebuild must fail the request, not answer 200
    with bundled skills only and announce that to every process."""
    from pincer.tools.skills.index import SkillRootUnreachable

    with patch(
        "pincer.tools.skills.index.SkillIndex.discover",
        side_effect=SkillRootUnreachable("Skill root s3://b/t is unreachable: EndpointConnectionError"),
    ):
        resp = client.post("/api/skills/rebuild")

    assert resp.status_code == 503
    assert "index unchanged" in resp.json()["detail"]
    assert not (tmp_path / "skills-cache" / "index-generation").exists()
    queued_jobs.assert_not_awaited()


def test_client_fixture_is_isolated_from_the_developer_skills(client: TestClient, tmp_path: Path) -> None:
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    assert settings.skills_dir == tmp_path / "skills"
    assert settings.skills_cache_dir == tmp_path / "skills-cache"


@pytest.mark.asyncio
async def test_rebuild_job_round_trip_through_the_task_queue(api_settings, tmp_path: Path) -> None:
    """Endpoint → in-memory repid broker → worker → rebuild_skill_index, in a "worker
    process" with its own disk (a separate SkillService), which must end up rebuilt."""
    from repid import InMemoryServer

    from pincer.api.skills import rebuild_skills
    from pincer.services.skills import SkillService
    from pincer.tasks import app as task_app

    worker_service = SkillService(bundled_dir=None, user_dir=tmp_path / "user", cache_dir=tmp_path / "worker-cache")
    worker_service.ensure_current()
    _add_user_skill(tmp_path, "late")

    task_app.servers.register_server("default", InMemoryServer(), is_default=True)
    connection = task_app.servers.default.connection()
    await connection.__aenter__()
    try:
        body = await rebuild_skills(keep_cache=False)
        assert body["job"]["queued"] is True
        with patch("pincer.services.skills.get_skill_service", return_value=worker_service):
            await asyncio.wait_for(
                task_app.run_worker(messages_limit=1, graceful_shutdown_time=1.0, register_signals=[]),
                timeout=10,
            )
    finally:
        await connection.__aexit__(None, None, None)

    assert [e.name for e in worker_service.index.all_skills()] == ["late"]
    assert worker_service.generation == body["generation"]


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

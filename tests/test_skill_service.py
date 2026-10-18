"""Tests for pincer.services.skills.SkillService — index ownership and invalidation.

Two SkillService instances sharing a cache dir stand in for two processes (e.g.
a running `pincer run` and a `pincer skills rebuild`).
"""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pincer.services.skills import SkillIndexWatcher, SkillService, get_skill_service, reset_skill_service
from pincer.tools.skills.index import SkillIndex
from pincer.tools.skills.storage import SkillPath

SkillRoot = Callable[[str], SkillPath]


def _make_skill(root: SkillPath, name: str, body: str = "body") -> None:
    skill_md = root / name / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(f"---\nname: {name}\ndescription: d\n---\n{body}\n", encoding="utf-8")


def _names(index: SkillIndex) -> list[str]:
    return [e.name for e in index.all_skills()]


@pytest.fixture
def user(skill_root: SkillRoot) -> SkillPath:
    root = skill_root("user")
    _make_skill(root, "alpha")
    return root


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "skills-cache"


def _service(user: SkillPath, cache_dir: Path | None) -> SkillService:
    return SkillService(bundled_dir=None, user_dir=user, cache_dir=cache_dir)


def test_ensure_current_scans_once(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    with patch.object(SkillIndex, "discover", autospec=True, side_effect=SkillIndex.discover) as discover:
        assert _names(service.ensure_current()) == ["alpha"]
        service.ensure_current()
        service.ensure_current()
    assert discover.call_count == 1


def test_index_object_is_stable_across_rescans(user: SkillPath, cache_dir: Path) -> None:
    """Holders of service.index (agent, tools) see rescans without re-fetching it."""
    service = _service(user, cache_dir)
    index = service.ensure_current()
    _make_skill(user, "beta")
    service.rebuild()
    assert service.index is index
    assert _names(index) == ["alpha", "beta"]


def test_ensure_current_rescans_after_max_age(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    with patch("pincer.services.skills.monotonic", return_value=100.0):
        service.ensure_current()
    _make_skill(user, "beta")

    with patch("pincer.services.skills.monotonic", return_value=159.0):
        assert _names(service.ensure_current(max_age=60)) == ["alpha"]
    with patch("pincer.services.skills.monotonic", return_value=160.0):
        assert _names(service.ensure_current(max_age=60)) == ["alpha", "beta"]


def test_max_age_zero_always_rescans(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    service.ensure_current(max_age=0)
    _make_skill(user, "beta")
    assert _names(service.ensure_current(max_age=0)) == ["alpha", "beta"]


def test_rebuild_in_one_process_reaches_the_other(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    assert _names(server.ensure_current()) == ["alpha"]

    _make_skill(user, "beta")
    assert _names(server.ensure_current()) == ["alpha"]  # no change announced yet
    cli.rebuild()

    assert _names(server.ensure_current()) == ["alpha", "beta"]


def test_invalidate_marks_every_process_stale(user: SkillPath, cache_dir: Path) -> None:
    """What the planned upload API does: write files, then invalidate()."""
    uploader, other = _service(user, cache_dir), _service(user, cache_dir)
    uploader.ensure_current()
    other.ensure_current()

    _make_skill(user, "uploaded")
    uploader.invalidate()

    assert "uploaded" in _names(uploader.ensure_current())
    assert "uploaded" in _names(other.ensure_current())


def test_periodic_rescan_that_finds_changes_announces_them(user: SkillPath, cache_dir: Path) -> None:
    api, agent = _service(user, cache_dir), _service(user, cache_dir)
    api.ensure_current()
    agent.ensure_current()

    _make_skill(user, "alpha", body="edited")
    api.ensure_current(max_age=0)

    assert agent.ensure_current().get("alpha").skill.body == "edited"


def _stamp(cache_dir: Path) -> str | None:
    path = cache_dir / "index-generation"
    return path.read_text(encoding="utf-8") if path.exists() else None


def test_unchanged_rescan_does_not_announce(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    service.rebuild()
    stamp = _stamp(cache_dir)

    service.ensure_current(max_age=0)

    assert stamp is not None
    assert _stamp(cache_dir) == stamp


def test_rebuild_clears_the_remote_file_cache(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    with patch("pincer.services.skills.clear_remote_cache") as clear:
        service.rebuild()
        service.rebuild(clear_cache=False)
    clear.assert_called_once_with(user)


def test_rebuild_refetches_remote_files(skill_root: SkillRoot, cache_dir: Path) -> None:
    root = skill_root("user")
    if isinstance(root, Path):
        pytest.skip("local roots have no file cache")
    _make_skill(root, "alpha")
    (root / "alpha" / "ref.md").write_text("reference", encoding="utf-8")
    service = _service(root, cache_dir)
    service.ensure_current()
    (root / "alpha" / "ref.md").read_text(encoding="utf-8")
    cache = Path(root.client._local_cache_dir)
    assert {p.name for p in cache.rglob("*") if p.is_file()} == {"SKILL.md", "ref.md"}

    service.rebuild()

    # The cache was emptied; only what the rescan itself read is back, fetched fresh.
    assert {p.name for p in cache.rglob("*") if p.is_file()} == {"SKILL.md"}


def test_works_without_a_cache_dir(user: SkillPath) -> None:
    service = _service(user, None)
    assert _names(service.ensure_current()) == ["alpha"]
    _make_skill(user, "beta")
    service.invalidate()
    assert _names(service.rebuild()) == ["alpha", "beta"]


def test_get_skill_service_is_a_process_singleton(tmp_path: Path) -> None:
    settings = MagicMock(
        skills_dir=tmp_path / "user", skills_max_loaded_per_root=100, skills_cache_dir=tmp_path / "cache"
    )
    reset_skill_service()
    try:
        first = get_skill_service(settings)
        assert get_skill_service() is first
        reset_skill_service()
        assert get_skill_service(settings) is not first
    finally:
        reset_skill_service()


# ── SkillIndexWatcher ────────────────────────────────────────────────────────


def test_refresh_if_announced(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    server.ensure_current()
    assert server.refresh_if_announced() is False

    _make_skill(user, "beta")
    cli.rebuild()

    assert server.refresh_if_announced() is True
    assert _names(server.index) == ["alpha", "beta"]
    assert server.refresh_if_announced() is False


async def _wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_watcher_applies_a_rebuild_from_another_process(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    watcher = SkillIndexWatcher(server, interval=0.02)
    await watcher.start()
    try:
        _make_skill(user, "beta")
        cli.rebuild()
        await _wait_for(lambda: "beta" in _names(server.index))
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_while_watched_requests_never_scan(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    watcher = SkillIndexWatcher(server, interval=3600)
    await watcher.start()
    try:
        _make_skill(user, "beta")
        cli.rebuild()
        with patch.object(SkillIndex, "discover", side_effect=AssertionError("request path scanned")):
            assert _names(server.ensure_current()) == ["alpha"]  # the watcher's job, not the request's
        # Not even blocked by a scan in progress.
        with server._lock:
            assert server.ensure_current() is server.index
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_while_watched_the_api_still_checks_the_stamp(user: SkillPath, cache_dir: Path) -> None:
    """The dashboard's contract (≤60s, invalidated on any change) is independent of the watcher."""
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    watcher = SkillIndexWatcher(server, interval=3600)
    await watcher.start()
    try:
        _make_skill(user, "beta")
        cli.rebuild()
        assert _names(server.ensure_current(max_age=60)) == ["alpha", "beta"]
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_survives_a_failed_rescan(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    watcher = SkillIndexWatcher(server, interval=0.02)
    await watcher.start()
    try:
        cli.rebuild()
        calls = 0
        real = SkillService.refresh_if_announced

        def flaky(self: SkillService) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("bucket blip")
            return real(self)

        _make_skill(user, "beta")
        with patch.object(SkillService, "refresh_if_announced", flaky):
            await _wait_for(lambda: "beta" in _names(server.index))
        assert calls >= 2
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_stopping_the_watcher_restores_on_demand_checks(user: SkillPath, cache_dir: Path) -> None:
    server, cli = _service(user, cache_dir), _service(user, cache_dir)
    watcher = SkillIndexWatcher(server, interval=3600)
    await watcher.start()
    await watcher.stop()

    _make_skill(user, "beta")
    cli.rebuild()

    assert _names(server.ensure_current()) == ["alpha", "beta"]


def test_watch_interval_setting_defaults_to_five_minutes() -> None:
    from pydantic import ValidationError

    from pincer.config.main import _RelaxedSettings

    assert _RelaxedSettings().skills_index_watch_interval_seconds == 300
    assert _RelaxedSettings(skills_index_watch_interval_seconds=0).skills_index_watch_interval_seconds == 0
    with pytest.raises(ValidationError):
        _RelaxedSettings(skills_index_watch_interval_seconds=-1)


# ── apply_generation (the rebuild_skill_index background job) ────────────────


def test_rebuild_records_its_generation(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    assert service.generation is None
    service.rebuild()
    assert service.generation == _stamp(cache_dir)


def test_generations_sort_by_creation_time(user: SkillPath, cache_dir: Path) -> None:
    service = _service(user, cache_dir)
    service.rebuild()
    first = service.generation
    service.rebuild()
    assert first is not None
    assert first < service.generation


def test_disk_id_is_shared_per_cache_dir(user: SkillPath, cache_dir: Path, tmp_path: Path) -> None:
    a, b = _service(user, cache_dir), _service(user, cache_dir)
    other = _service(user, tmp_path / "other-disk")
    assert a.disk_id == b.disk_id
    assert a.disk_id != other.disk_id
    assert _service(user, cache_dir).disk_id == a.disk_id  # persisted, not per instance
    assert _service(user, None).disk_id is None


def test_apply_generation_is_a_noop_in_the_rebuilding_process(user: SkillPath, cache_dir: Path) -> None:
    app = _service(user, cache_dir)
    app.rebuild()
    with patch.object(SkillIndex, "discover", side_effect=AssertionError("rescanned")):
        assert app.apply_generation(app.generation, disk_id=app.disk_id) is False


def test_apply_generation_on_a_shared_disk_rescans_without_clearing(user: SkillPath, cache_dir: Path) -> None:
    app, worker = _service(user, cache_dir), _service(user, cache_dir)
    worker.ensure_current()
    _make_skill(user, "beta")
    app.rebuild()
    stamp = _stamp(cache_dir)

    with patch("pincer.services.skills.clear_remote_cache") as clear:
        assert worker.apply_generation(app.generation, disk_id=app.disk_id) is True

    clear.assert_not_called()  # the rebuilding process already cleared the shared cache
    assert _names(worker.index) == ["alpha", "beta"]
    assert worker.generation == app.generation
    assert _stamp(cache_dir) == stamp  # nothing new announced
    assert worker.apply_generation(app.generation, disk_id=app.disk_id) is False


def test_apply_generation_on_a_shared_disk_never_rolls_a_newer_stamp_back(user: SkillPath, cache_dir: Path) -> None:
    """Review finding: the job's generation X was superseded (stamp now Y) before the
    job ran. A worker on the shared disk must not clear the shared cache or write X back."""
    app, worker = _service(user, cache_dir), _service(user, cache_dir)
    worker.ensure_current()
    app.rebuild()
    job_generation = app.generation
    app.rebuild()  # a newer change lands before the job is consumed
    newest = _stamp(cache_dir)

    with patch("pincer.services.skills.clear_remote_cache") as clear:
        assert worker.apply_generation(job_generation, disk_id=app.disk_id) is True

    clear.assert_not_called()
    assert _stamp(cache_dir) == newest
    assert worker.generation == newest


def test_apply_generation_ignores_an_older_generation(user: SkillPath, cache_dir: Path, tmp_path: Path) -> None:
    app = _service(user, cache_dir)
    worker = _service(user, tmp_path / "worker-cache")
    app.rebuild()
    older = app.generation
    app.rebuild()
    assert worker.apply_generation(app.generation, disk_id=app.disk_id) is True

    with patch.object(SkillIndex, "discover", side_effect=AssertionError("rescanned")):
        assert worker.apply_generation(older, disk_id=app.disk_id) is False
    assert worker.generation == app.generation


def test_apply_generation_on_its_own_disk_clears_and_adopts_the_generation(
    user: SkillPath, cache_dir: Path, tmp_path: Path
) -> None:
    app = _service(user, cache_dir)
    worker_disk = tmp_path / "worker-cache"
    worker, worker_sibling = _service(user, worker_disk), _service(user, worker_disk)
    worker.ensure_current()
    worker_sibling.ensure_current()
    _make_skill(user, "beta")
    app.rebuild()

    with patch("pincer.services.skills.clear_remote_cache") as clear:
        assert worker.apply_generation(app.generation, clear_cache=True, disk_id=app.disk_id) is True
    clear.assert_called_once_with(user)

    # Processes sharing the worker's disk follow it once, via the stamp.
    assert _stamp(worker_disk) == app.generation
    assert "beta" in _names(worker_sibling.ensure_current())


def test_apply_generation_on_its_own_disk_keeps_a_newer_local_stamp(
    user: SkillPath, cache_dir: Path, tmp_path: Path
) -> None:
    app = _service(user, cache_dir)
    worker_disk = tmp_path / "worker-cache"
    worker, local_writer = _service(user, worker_disk), _service(user, worker_disk)
    app.rebuild()
    job_generation = app.generation
    local_writer.rebuild()  # a newer change on the worker's own disk
    newest = _stamp(worker_disk)

    worker.apply_generation(job_generation, clear_cache=False, disk_id=app.disk_id)

    assert _stamp(worker_disk) == newest


# ── review fixes ─────────────────────────────────────────────────────────────


def test_catching_up_with_an_announcement_does_not_reannounce(user: SkillPath, cache_dir: Path) -> None:
    """Review finding: a process that rescans *because* of an announcement must not
    write a new stamp, or every process sharing the disk keeps rescanning in turn."""
    api, worker, mcp = (_service(user, cache_dir) for _ in range(3))
    for service in (api, worker, mcp):
        service.ensure_current()

    _make_skill(user, "alpha", body="edited")
    api.ensure_current(max_age=0)  # finds the edit and announces it once
    announced = _stamp(cache_dir)
    assert announced is not None

    with patch.object(SkillIndex, "discover", autospec=True, side_effect=SkillIndex.discover) as discover:
        for service in (worker, mcp, api, worker, mcp):
            service.ensure_current()

    assert _stamp(cache_dir) == announced
    assert discover.call_count == 2  # worker and mcp catch up once each; nobody rescans again
    assert worker.index.get("alpha").skill.body == "edited"


def test_invalidate_without_a_cache_dir_marks_this_process_stale(user: SkillPath) -> None:
    service = _service(user, None)
    service.ensure_current()
    _make_skill(user, "beta")

    service.invalidate()

    assert _names(service.ensure_current()) == ["alpha", "beta"]


# ── startup: require_usable_root ─────────────────────────────────────────────


def _remote_settings(tmp_path: Path) -> MagicMock:
    return MagicMock(skills_dir="s3://bucket/team", skills_max_loaded_per_root=100, skills_cache_dir=tmp_path)


def test_from_settings_strict_raises_on_an_unusable_remote_root(tmp_path: Path) -> None:
    from pincer.exceptions import SkillLoadError

    with (
        patch(
            "pincer.services.skills.open_usable_skills_root",
            side_effect=SkillLoadError("Skills root s3://bucket/team is not usable"),
        ),
        pytest.raises(SkillLoadError, match="not usable"),
    ):
        SkillService.from_settings(_remote_settings(tmp_path), require_usable_root=True)


def test_from_settings_lenient_degrades_to_bundled_only(tmp_path: Path) -> None:
    with patch("pincer.services.skills.open_user_skills_root", return_value=None):
        service = SkillService.from_settings(_remote_settings(tmp_path))
    assert service.user_dir is None


def test_get_skill_service_strict_failure_creates_no_singleton(tmp_path: Path) -> None:
    from pincer.exceptions import SkillLoadError

    reset_skill_service()
    try:
        with (
            patch("pincer.services.skills.open_usable_skills_root", side_effect=SkillLoadError("nope")),
            pytest.raises(SkillLoadError),
        ):
            get_skill_service(_remote_settings(tmp_path), require_usable_root=True)
        with patch("pincer.services.skills.open_user_skills_root", return_value=None):
            assert get_skill_service(_remote_settings(tmp_path)).user_dir is None
    finally:
        reset_skill_service()


# ── outages (PR review: an unreachable root must not become an announced empty index) ──


def _user_outage(user: SkillPath):
    """Patch discovery so listing the user root fails like an expired STS token."""
    from pincer.tools.skills.storage import iter_skill_md

    def flaky(base):
        if base == user:
            raise ConnectionError("ExpiredToken")
        return iter_skill_md(base)

    return patch("pincer.tools.skills.index.iter_skill_md", side_effect=flaky)


def test_rebuild_during_an_outage_raises_and_announces_nothing(user: SkillPath, cache_dir: Path) -> None:
    from pincer.tools.skills.index import SkillRootUnreachable

    app, watcher_process = _service(user, cache_dir), _service(user, cache_dir)
    app.rebuild()
    watcher_process.ensure_current()
    stamp = _stamp(cache_dir)

    with _user_outage(user), pytest.raises(SkillRootUnreachable, match="ExpiredToken"):
        app.rebuild()

    assert _stamp(cache_dir) == stamp  # nothing announced
    assert _names(app.index) == ["alpha"]  # user skills kept
    assert _names(watcher_process.ensure_current()) == ["alpha"]


def test_outage_during_a_catch_up_scan_keeps_skills_and_retries(user: SkillPath, cache_dir: Path) -> None:
    api, worker = _service(user, cache_dir), _service(user, cache_dir)
    worker.ensure_current()
    _make_skill(user, "beta")
    with patch("pincer.services.skills.monotonic", return_value=1000.0):
        api.rebuild()
    announced = _stamp(cache_dir)

    with _user_outage(user), patch("pincer.services.skills.monotonic", return_value=1000.0):
        assert worker.refresh_if_announced() is True

    assert _names(worker.index) == ["alpha"]  # previous user skills kept, not emptied
    assert worker.generation != announced  # the announcement isn't marked as applied
    assert _stamp(cache_dir) == announced  # and the failed scan announced nothing

    with patch("pincer.services.skills.monotonic", return_value=1030.0):
        assert worker.refresh_if_announced() is False  # no retry storm during the outage
    with patch("pincer.services.skills.monotonic", return_value=1061.0):
        assert worker.refresh_if_announced() is True  # retried once the pause is over
    assert _names(worker.index) == ["alpha", "beta"]
    assert worker.generation == announced


def test_outage_during_a_max_age_rescan_announces_nothing(user: SkillPath, cache_dir: Path) -> None:
    api = _service(user, cache_dir)
    api.rebuild()
    stamp = _stamp(cache_dir)

    with _user_outage(user):
        assert _names(api.ensure_current(max_age=0)) == ["alpha"]

    assert _stamp(cache_dir) == stamp


def test_apply_generation_during_an_outage_raises_without_adopting(
    user: SkillPath, cache_dir: Path, tmp_path: Path
) -> None:
    from pincer.tools.skills.index import SkillRootUnreachable

    app = _service(user, cache_dir)
    worker = _service(user, tmp_path / "worker-cache")
    worker.ensure_current()
    app.rebuild()

    with _user_outage(user), pytest.raises(SkillRootUnreachable):
        worker.apply_generation(app.generation, disk_id=app.disk_id)

    assert worker.generation != app.generation
    assert _names(worker.index) == ["alpha"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a chmod 000 file")
def test_one_unreadable_skill_does_not_block_rebuilds_or_announcements(tmp_path: Path) -> None:
    """PR review: with one `chmod 000` SKILL.md, rebuilds and catch-up scans still
    succeed, and the announcement is adopted (not retried every minute)."""
    user, cache_dir = tmp_path / "user", tmp_path / "cache"
    _make_skill(user, "ok")
    _make_skill(user, "locked")
    (user / "locked" / "SKILL.md").chmod(0)
    try:
        app, worker = _service(user, cache_dir), _service(user, cache_dir)
        worker.ensure_current()
        _make_skill(user, "new")

        app.rebuild()  # must not raise

        assert worker.refresh_if_announced() is True
        assert worker.generation == app.generation
        assert worker.refresh_if_announced() is False  # adopted, not pending a retry
        assert _names(worker.index) == ["new", "ok"]
    finally:
        (user / "locked" / "SKILL.md").chmod(0o644)

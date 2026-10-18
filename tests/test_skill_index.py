"""Tests for SKILL.md discovery, hot-reload, and prompt-block rendering.

Every test runs once per storage backend (local, S3, Azure, GCS) via the
`skill_root` fixture.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from pincer.tools.skills.index import SkillIndex
from pincer.tools.skills.storage import SkillPath

SkillRoot = Callable[[str], SkillPath]


def _write(path: SkillPath, text: str) -> None:
    """Write a file, creating parents (a no-op on object storage)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_skill(root: SkillPath, name: str, description: str = "desc", body: str = "body") -> None:
    _write(root / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")


def test_discovers_bundled_and_user_skills(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    user = skill_root("user")
    _make_skill(bundled, "alpha")
    _make_skill(user, "beta")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    names = {e.name for e in index.all_skills()}
    assert names == {"alpha", "beta"}


def test_bundled_before_user_alphabetical_within_root(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    user = skill_root("user")
    _make_skill(bundled, "zeta")
    _make_skill(bundled, "alpha")
    _make_skill(user, "yankee")
    _make_skill(user, "bravo")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["alpha", "zeta", "bravo", "yankee"]


def test_user_skill_overrides_bundled_of_same_name(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    user = skill_root("user")
    _make_skill(bundled, "demo", description="bundled version")
    _make_skill(user, "demo", description="user version")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    entries = index.all_skills()
    assert len(entries) == 1
    assert entries[0].description == "user version"
    assert entries[0].skill.source_root == "user"


def test_unparseable_skill_is_skipped_not_fatal(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "good")
    _write(bundled / "bad" / "SKILL.md", "no frontmatter here")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["good"]


def test_max_per_root_cap(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    for i in range(5):
        _make_skill(bundled, f"skill-{i}")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=3)
    index.discover()

    assert len(index.all_skills()) == 3


def test_get_returns_entry_or_none(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert index.get("demo") is not None
    assert index.get("nope") is None


def test_list_files_excludes_skill_md(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "demo")
    _write(bundled / "demo" / "notes.txt", "x")
    _write(bundled / "demo" / "scripts" / "run.py", "x")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    files = index.get("demo").list_files()
    assert files == ["notes.txt", "scripts/run.py"]


def test_check_for_changes_hot_reloads_on_edit(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "demo", description="v1")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()
    assert index.get("demo").description == "v1"

    _make_skill(bundled, "demo", description="v2")
    reloaded = index.check_for_changes()

    assert reloaded == ["demo"]
    assert index.get("demo").description == "v2"


def test_check_for_changes_noop_when_unchanged(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert index.check_for_changes() == []


def test_check_for_changes_removes_deleted_skill(skill_root: SkillRoot) -> None:
    """Regression for PR #167 review: a skill whose SKILL.md is deleted from disk
    must stop being served (load_skill etc.) rather than lingering until a full
    discover() rescan replaces the whole index."""
    bundled = skill_root("bundled")
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()
    assert index.get("demo") is not None

    (bundled / "demo" / "SKILL.md").unlink()
    reloaded = index.check_for_changes()

    assert reloaded == ["demo"]
    assert index.get("demo") is None
    assert index.all_skills() == []


def test_render_prompt_block_empty_when_no_skills(skill_root: SkillRoot) -> None:
    index = SkillIndex(bundled_dir=skill_root("empty"), user_dir=None, max_per_root=100)
    index.discover()
    assert index.render_prompt_block() == ""


def test_render_prompt_block_lists_all_when_no_budget(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "alpha", description="Alpha desc")
    _make_skill(bundled, "beta", description="Beta desc")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    block = index.render_prompt_block()
    assert "- alpha: Alpha desc" in block
    assert "- beta: Beta desc" in block


def test_render_prompt_block_truncates_with_transparency_note(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    for i in range(20):
        _make_skill(bundled, f"skill-{i:02d}", description="A reasonably long description " * 3)
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    block = index.render_prompt_block(max_tokens=20)

    assert "more skills (truncated to fit context budget" in block
    assert "load_skill(name)" in block
    assert "skills_max_prompt_tokens" in block


def test_hidden_and_skip_dirs_are_ignored(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "visible")
    _make_skill(bundled, ".hidden")
    _make_skill(bundled, "node_modules")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["visible"]


def test_skill_md_must_sit_directly_under_a_skill_dir(skill_root: SkillRoot) -> None:
    bundled = skill_root("bundled")
    _make_skill(bundled, "top")
    _make_skill(bundled / "group", "nested")
    _write(bundled / "SKILL.md", "---\nname: root\ndescription: d\n---\nx\n")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["top"]


def test_unreachable_root_on_first_scan_yields_no_skills(skill_root: SkillRoot, caplog) -> None:
    class _Unreachable:
        def is_dir(self) -> bool:
            raise ConnectionError("bucket unreachable")

        def __str__(self) -> str:
            return "s3://gone/skills"

    bundled = skill_root("bundled")
    _make_skill(bundled, "alpha")
    index = SkillIndex(bundled_dir=bundled, user_dir=_Unreachable(), max_per_root=100)  # type: ignore[arg-type]
    assert index.discover() is False

    assert [e.name for e in index.all_skills()] == ["alpha"]
    assert "s3://gone/skills is unreachable" in caplog.text


def test_unreachable_root_keeps_its_previous_skills(skill_root: SkillRoot) -> None:
    """Review finding: an outage during a rescan must not empty the root's skills."""
    bundled, user = skill_root("bundled"), skill_root("user")
    _make_skill(bundled, "alpha")
    _make_skill(user, "beta")
    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    assert index.discover() is True

    from pincer.tools.skills.storage import iter_skill_md

    def flaky(base):
        if base == user:
            raise ConnectionError("S3 503 SlowDown")
        return iter_skill_md(base)

    with patch("pincer.tools.skills.index.iter_skill_md", side_effect=flaky):
        assert index.discover() is False

    assert {e.name: e.skill.source_root for e in index.all_skills()} == {"alpha": "bundled", "beta": "user"}


def test_strict_discover_raises_and_leaves_the_index_unchanged(skill_root: SkillRoot) -> None:
    from pincer.tools.skills.index import SkillRootUnreachable

    bundled, user = skill_root("bundled"), skill_root("user")
    _make_skill(bundled, "alpha")
    _make_skill(user, "beta")
    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()
    _make_skill(bundled, "gamma")

    with (
        patch("pincer.tools.skills.index.iter_skill_md", side_effect=ConnectionError("expired token")),
        pytest.raises(SkillRootUnreachable, match="expired token"),
    ):
        index.discover(strict=True)

    assert [e.name for e in index.all_skills()] == ["alpha", "beta"]


def test_unreadable_skill_md_is_never_fatal(skill_root: SkillRoot) -> None:
    """PR review: one SKILL.md that can't be read must not fail a strict rebuild or
    mark the scan incomplete. A cloud root keeps that skill's previous version (a
    one-object failure is usually transient); a local root skips it."""
    from pincer.exceptions import SkillLoadError
    from pincer.tools.skills.parser import read_skill_md

    bundled = skill_root("bundled")
    _make_skill(bundled, "alpha", description="v1")
    _make_skill(bundled, "beta")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    def alpha_unreadable(path):
        if path.parent.name == "alpha":
            raise SkillLoadError(f"Cannot read {path}") from ConnectionError("connection reset")
        return read_skill_md(path)

    expected = ["beta"] if isinstance(bundled, Path) else ["alpha", "beta"]
    with patch("pincer.tools.skills.index.read_skill_md", side_effect=alpha_unreadable):
        for strict in (False, True):
            assert index.discover(strict=strict) is True
            assert [e.name for e in index.all_skills()] == expected


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a chmod 000 file")
def test_local_skill_md_without_read_permission_is_skipped(tmp_path) -> None:
    """The reviewer's reproduction: a `chmod 000` SKILL.md in a local root."""
    user = tmp_path / "user"
    _make_skill(user, "ok")
    _make_skill(user, "locked")
    (user / "locked" / "SKILL.md").chmod(0)
    try:
        index = SkillIndex(bundled_dir=None, user_dir=user, max_per_root=100)
        assert index.discover(strict=True) is True
        assert [e.name for e in index.all_skills()] == ["ok"]
    finally:
        (user / "locked" / "SKILL.md").chmod(0o644)


def test_list_files_skips_symlinked_directories(tmp_path) -> None:
    """Review finding: Path.walk reports a symlink to a directory as a file."""
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo")
    _write(tmp_path / "shared" / "a.txt", "x")
    (bundled / "demo" / "assets").symlink_to(tmp_path / "shared", target_is_directory=True)
    _write(bundled / "demo" / "notes.txt", "x")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert index.get("demo").list_files() == ["notes.txt"]


def test_cloud_discovery_lists_only_the_top_level(skill_root: SkillRoot, caplog) -> None:
    """Review finding: a `*/SKILL.md` glob made cloudpathlib list every object under
    the root. Discovery now lists the root's children only, and entries without a
    SKILL.md (stray files, plain folders) are skipped silently."""
    bundled = skill_root("bundled")
    if isinstance(bundled, Path):
        pytest.skip("local roots glob the filesystem")
    _make_skill(bundled, "alpha")
    _write(bundled / "alpha" / "assets" / "deep" / "big.bin", "x")
    _write(bundled / "README.md", "not a skill")
    _write(bundled / "folder-without-skill" / "notes.txt", "x")

    listings: list[bool] = []
    real_list_dir = bundled.client._list_dir

    def spy(path, recursive=False):
        listings.append(recursive)
        return real_list_dir(path, recursive=recursive)

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    with patch.object(bundled.client, "_list_dir", side_effect=spy), caplog.at_level(logging.WARNING):
        index.discover()

    assert [e.name for e in index.all_skills()] == ["alpha"]
    assert True not in listings  # no recursive listing of the whole root
    assert "Skipped skill dir" not in caplog.text

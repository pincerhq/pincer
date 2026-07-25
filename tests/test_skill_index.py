"""Tests for SKILL.md discovery, hot-reload, and prompt-block rendering."""

from pathlib import Path

from pincer.tools.skills.index import SkillIndex


def _make_skill(root: Path, name: str, description: str = "desc", body: str = "body") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_discovers_bundled_and_user_skills(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _make_skill(bundled, "alpha")
    _make_skill(user, "beta")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    names = {e.name for e in index.all_skills()}
    assert names == {"alpha", "beta"}


def test_bundled_before_user_alphabetical_within_root(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _make_skill(bundled, "zeta")
    _make_skill(bundled, "alpha")
    _make_skill(user, "yankee")
    _make_skill(user, "bravo")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["alpha", "zeta", "bravo", "yankee"]


def test_user_skill_overrides_bundled_of_same_name(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _make_skill(bundled, "demo", description="bundled version")
    _make_skill(user, "demo", description="user version")

    index = SkillIndex(bundled_dir=bundled, user_dir=user, max_per_root=100)
    index.discover()

    entries = index.all_skills()
    assert len(entries) == 1
    assert entries[0].description == "user version"
    assert entries[0].skill.source_root == "user"


def test_unparseable_skill_is_skipped_not_fatal(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "good")
    bad_dir = bundled / "bad"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert [e.name for e in index.all_skills()] == ["good"]


def test_max_per_root_cap(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    for i in range(5):
        _make_skill(bundled, f"skill-{i}")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=3)
    index.discover()

    assert len(index.all_skills()) == 3


def test_get_returns_entry_or_none(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert index.get("demo") is not None
    assert index.get("nope") is None


def test_list_files_excludes_skill_md(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo")
    (bundled / "demo" / "notes.txt").write_text("x", encoding="utf-8")
    (bundled / "demo" / "scripts").mkdir()
    (bundled / "demo" / "scripts" / "run.py").write_text("x", encoding="utf-8")

    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    files = index.get("demo").list_files()
    assert files == ["notes.txt", "scripts/run.py"]


def test_check_for_changes_hot_reloads_on_edit(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo", description="v1")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()
    assert index.get("demo").description == "v1"

    _make_skill(bundled, "demo", description="v2")
    reloaded = index.check_for_changes()

    assert reloaded == ["demo"]
    assert index.get("demo").description == "v2"


def test_check_for_changes_noop_when_unchanged(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    assert index.check_for_changes() == []


def test_check_for_changes_removes_deleted_skill(tmp_path: Path) -> None:
    """Regression for PR #167 review: a skill whose SKILL.md is deleted from disk
    must stop being served (load_skill etc.) rather than lingering until a full
    discover() rescan replaces the whole index."""
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "demo")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()
    assert index.get("demo") is not None

    (bundled / "demo" / "SKILL.md").unlink()
    reloaded = index.check_for_changes()

    assert reloaded == ["demo"]
    assert index.get("demo") is None
    assert index.all_skills() == []


def test_render_prompt_block_empty_when_no_skills(tmp_path: Path) -> None:
    index = SkillIndex(bundled_dir=tmp_path / "empty", user_dir=None, max_per_root=100)
    index.discover()
    assert index.render_prompt_block() == ""


def test_render_prompt_block_lists_all_when_no_budget(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _make_skill(bundled, "alpha", description="Alpha desc")
    _make_skill(bundled, "beta", description="Beta desc")
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    block = index.render_prompt_block()
    assert "- alpha: Alpha desc" in block
    assert "- beta: Beta desc" in block


def test_render_prompt_block_truncates_with_transparency_note(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    for i in range(20):
        _make_skill(bundled, f"skill-{i:02d}", description="A reasonably long description " * 3)
    index = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    index.discover()

    block = index.render_prompt_block(max_tokens=20)

    assert "more skills (truncated to fit context budget" in block
    assert "load_skill(name)" in block
    assert "skills_max_prompt_tokens" in block

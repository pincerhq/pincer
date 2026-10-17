"""Tests for SKILL.md frontmatter/body parsing, on every storage backend."""

from collections.abc import Callable

import pytest

from pincer.exceptions import SkillLoadError
from pincer.tools.skills.parser import parse_skill_md
from pincer.tools.skills.storage import SkillPath

SkillRoot = Callable[[str], SkillPath]


def _write(skill_root: SkillRoot, content: str) -> SkillPath:
    skill_md = skill_root("demo") / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def test_parses_valid_skill(skill_root: SkillRoot) -> None:
    skill_md = _write(
        skill_root,
        "---\nname: demo\ndescription: A demo skill.\n---\n# Demo\n\nBody text.\n",
    )
    parsed = parse_skill_md(skill_md)
    assert parsed.name == "demo"
    assert parsed.description == "A demo skill."
    assert parsed.body == "# Demo\n\nBody text."
    assert parsed.path == skill_root("demo")
    assert parsed.source_root == "user"


def test_source_root_is_passed_through(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "---\nname: demo\ndescription: d\n---\nbody\n")
    parsed = parse_skill_md(skill_md, source_root="bundled")
    assert parsed.source_root == "bundled"


def test_extra_frontmatter_keys_preserved(skill_root: SkillRoot) -> None:
    skill_md = _write(
        skill_root,
        "---\nname: demo\ndescription: d\nlicense: MIT\n---\nbody\n",
    )
    parsed = parse_skill_md(skill_md)
    assert parsed.extra == {"license": "MIT"}


def test_missing_name_raises(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "---\ndescription: d\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="name"):
        parse_skill_md(skill_md)


def test_missing_description_raises(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "---\nname: demo\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="description"):
        parse_skill_md(skill_md)


def test_missing_frontmatter_raises(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "just some markdown, no frontmatter\n")
    with pytest.raises(SkillLoadError, match="frontmatter"):
        parse_skill_md(skill_md)


def test_malformed_yaml_raises(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "---\nname: [unclosed\n---\nbody\n")
    with pytest.raises(SkillLoadError):
        parse_skill_md(skill_md)


def test_non_mapping_frontmatter_raises(skill_root: SkillRoot) -> None:
    skill_md = _write(skill_root, "---\n- just\n- a\n- list\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="mapping"):
        parse_skill_md(skill_md)


def test_missing_file_raises(skill_root: SkillRoot) -> None:
    with pytest.raises(SkillLoadError):
        parse_skill_md(skill_root("demo") / "does-not-exist.md")

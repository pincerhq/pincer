"""Tests for SKILL.md frontmatter/body parsing."""

from pathlib import Path

import pytest

from pincer.exceptions import SkillLoadError
from pincer.tools.skills.parser import parse_skill_md


def _write(tmp_path: Path, content: str) -> Path:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def test_parses_valid_skill(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path,
        "---\nname: demo\ndescription: A demo skill.\n---\n# Demo\n\nBody text.\n",
    )
    parsed = parse_skill_md(skill_md)
    assert parsed.name == "demo"
    assert parsed.description == "A demo skill."
    assert parsed.body == "# Demo\n\nBody text."
    assert parsed.path == tmp_path
    assert parsed.source_root == "user"


def test_source_root_is_passed_through(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "---\nname: demo\ndescription: d\n---\nbody\n")
    parsed = parse_skill_md(skill_md, source_root="bundled")
    assert parsed.source_root == "bundled"


def test_extra_frontmatter_keys_preserved(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path,
        "---\nname: demo\ndescription: d\nlicense: MIT\n---\nbody\n",
    )
    parsed = parse_skill_md(skill_md)
    assert parsed.extra == {"license": "MIT"}


def test_missing_name_raises(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "---\ndescription: d\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="name"):
        parse_skill_md(skill_md)


def test_missing_description_raises(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "---\nname: demo\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="description"):
        parse_skill_md(skill_md)


def test_missing_frontmatter_raises(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "just some markdown, no frontmatter\n")
    with pytest.raises(SkillLoadError, match="frontmatter"):
        parse_skill_md(skill_md)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "---\nname: [unclosed\n---\nbody\n")
    with pytest.raises(SkillLoadError):
        parse_skill_md(skill_md)


def test_non_mapping_frontmatter_raises(tmp_path: Path) -> None:
    skill_md = _write(tmp_path, "---\n- just\n- a\n- list\n---\nbody\n")
    with pytest.raises(SkillLoadError, match="mapping"):
        parse_skill_md(skill_md)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SkillLoadError):
        parse_skill_md(tmp_path / "does-not-exist.md")

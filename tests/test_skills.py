"""Tests for the bundled SKILL.md skills under src/pincer/skills/."""

from pathlib import Path

import pytest
import yaml

SKILL_NAMES = [
    "skill-authoring",
    "memory-recall",
    "mcp-server-setup",
    "scheduler-briefings",
    "doctor-troubleshooting",
]

SKILLS_DIR = Path(__file__).parent.parent / "src" / "pincer" / "skills"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_dir_exists(skill_name: str) -> None:
    assert (SKILLS_DIR / skill_name).is_dir(), f"skills/{skill_name}/ should exist"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_md_exists(skill_name: str) -> None:
    assert (SKILLS_DIR / skill_name / "SKILL.md").is_file(), f"SKILL.md should exist in {skill_name}"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_frontmatter_has_required_fields(skill_name: str) -> None:
    raw = (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"{skill_name}: SKILL.md should start with frontmatter"
    _, frontmatter_raw, _ = raw.split("---\n", 2)
    data = yaml.safe_load(frontmatter_raw)
    assert data.get("name"), f"{skill_name}: frontmatter missing 'name'"
    assert data.get("description"), f"{skill_name}: frontmatter missing 'description'"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_body_non_empty(skill_name: str) -> None:
    from pincer.tools.skills.parser import parse_skill_md

    parsed = parse_skill_md(SKILLS_DIR / skill_name / "SKILL.md")
    assert parsed.body.strip(), f"{skill_name}: body should not be empty"


def test_bundled_index_discovers_all_starter_skills() -> None:
    """Discovery is directory-based, not frontmatter-name-based — a skill's
    frontmatter `name` is a free-form display name and need not match its
    directory (that's what /api/skills/{name} and this lookup key off)."""
    from pincer.tools.skills.index import SkillIndex

    index = SkillIndex(bundled_dir=SKILLS_DIR, user_dir=None, max_per_root=100)
    index.discover()
    dir_names = {e.skill.path.name for e in index.all_skills()}
    assert dir_names == set(SKILL_NAMES)

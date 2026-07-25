"""SKILL.md parsing: YAML frontmatter + markdown body.

A skill is a directory containing a single SKILL.md file: a YAML
frontmatter block (requires `name` and `description`) followed by a
markdown body. This module has no filesystem-scanning responsibility —
that belongs to `pincer.tools.skills.index`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import yaml

from pincer.exceptions import SkillLoadError

if TYPE_CHECKING:
    from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class SkillFrontmatter:
    """Parsed YAML frontmatter fields."""

    name: str
    description: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedSkill:
    """A fully parsed SKILL.md: frontmatter fields plus the markdown body."""

    name: str
    description: str
    body: str
    path: Path
    source_root: Literal["bundled", "user"]
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_frontmatter(raw: str, source: Path) -> tuple[SkillFrontmatter, str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillLoadError(
            f"{source}: missing YAML frontmatter (expected a '---' delimited block at the top of the file)"
        )

    frontmatter_raw, body = match.groups()

    try:
        data = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError as e:
        raise SkillLoadError(f"{source}: malformed YAML frontmatter: {e}") from e

    if not isinstance(data, dict):
        raise SkillLoadError(f"{source}: frontmatter must be a YAML mapping")

    for key in ("name", "description"):
        if not data.get(key):
            raise SkillLoadError(f"{source}: frontmatter missing required field '{key}'")

    name = str(data.pop("name"))
    description = str(data.pop("description"))
    return SkillFrontmatter(name=name, description=description, extra=data), body.strip()


def parse_skill_md(path: Path, source_root: Literal["bundled", "user"] = "user") -> ParsedSkill:
    """Parse a SKILL.md file into a ParsedSkill. Raises SkillLoadError on problems."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillLoadError(f"Cannot read {path}: {e}") from e

    frontmatter, body = _parse_frontmatter(raw, path)

    return ParsedSkill(
        name=frontmatter.name,
        description=frontmatter.description,
        body=body,
        path=path.parent,
        source_root=source_root,
        extra=frontmatter.extra,
    )

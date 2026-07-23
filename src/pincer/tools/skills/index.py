"""Skill discovery, hot-reload, and system-prompt rendering.

Skills are identified solely by the presence of a SKILL.md file in a
subdirectory of a bundled or user skills root. `SkillIndex` is the single
source of truth for discovery — the agent's tool wiring and the dashboard
API both go through it rather than re-scanning directories independently.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pincer.tools.skills.parser import ParsedSkill, parse_skill_md

logger = logging.getLogger(__name__)

# Bundled skills ship inside the installed package (see pyproject.toml's
# [tool.hatch.build.targets.wheel] packages = ["src/pincer"]), so this path
# resolves correctly both from a source checkout and a pip-installed distribution.
BUNDLED_SKILLS_DIR: Path = Path(__file__).resolve().parent.parent.parent / "skills"

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_DESC_TRUNCATE_LEN = 80
_ROOT_ORDER = {"bundled": 0, "user": 1}
_TRUNCATION_NOTE = (
    "... and {n} more skills (truncated to fit context budget — use load_skill(name) "
    "with the exact name, or increase skills_max_prompt_tokens)"
)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class SkillEntry:
    """A discovered skill: its parsed SKILL.md plus a content hash for hot-reload."""

    skill: ParsedSkill
    file_hash: str

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def description(self) -> str:
        return self.skill.description

    def list_files(self) -> list[str]:
        """Relative paths of files in the skill dir, excluding SKILL.md itself."""
        return sorted(
            str(p.relative_to(self.skill.path))
            for p in self.skill.path.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )


class SkillIndex:
    """Discovers SKILL.md-based skills from bundled and user directories."""

    def __init__(
        self,
        bundled_dir: Path | None,
        user_dir: Path | None,
        max_per_root: int = 100,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._user_dir = user_dir
        self._max_per_root = max_per_root
        self._skills: dict[str, SkillEntry] = {}

    def _discover_root(self, base: Path | None, source_root: Literal["bundled", "user"]) -> list[SkillEntry]:
        if base is None or not base.is_dir():
            return []

        entries: list[SkillEntry] = []
        skipped = 0
        for entry_dir in sorted(base.iterdir()):
            if not entry_dir.is_dir():
                continue
            if entry_dir.name.startswith(".") or entry_dir.name in _SKIP_DIRS:
                continue
            skill_md = entry_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            if len(entries) >= self._max_per_root:
                skipped += 1
                continue
            try:
                parsed = parse_skill_md(skill_md, source_root=source_root)
            except Exception:
                logger.warning("Skipped skill dir %s: failed to parse SKILL.md", entry_dir, exc_info=True)
                continue
            entries.append(SkillEntry(skill=parsed, file_hash=_hash_file(skill_md)))

        if skipped:
            logger.warning(
                "Skill root %s: %d skill(s) beyond skills_max_loaded_per_root=%d were skipped",
                base,
                skipped,
                self._max_per_root,
            )
        return entries

    def discover(self) -> None:
        """(Re)scan both roots and populate the index. User skills win name collisions."""
        skills: dict[str, SkillEntry] = {}
        for entry in self._discover_root(self._bundled_dir, "bundled"):
            skills[entry.name] = entry
        for entry in self._discover_root(self._user_dir, "user"):
            if entry.name in skills and skills[entry.name].skill.source_root == "bundled":
                logger.info("User skill '%s' overrides bundled skill of the same name", entry.name)
            skills[entry.name] = entry
        self._skills = skills
        logger.info("Skill index: discovered %d skill(s)", len(self._skills))

    def check_for_changes(self) -> list[str]:
        """Re-parse any skill whose SKILL.md hash changed, and drop any whose SKILL.md
        was deleted. Returns names reloaded or removed."""
        reloaded: list[str] = []
        for name, entry in list(self._skills.items()):
            skill_md = entry.skill.path / "SKILL.md"
            if not skill_md.is_file():
                del self._skills[name]
                reloaded.append(name)
                logger.info("Removed skill '%s' (SKILL.md no longer present)", name)
                continue
            current_hash = _hash_file(skill_md)
            if current_hash == entry.file_hash:
                continue
            try:
                parsed = parse_skill_md(skill_md, source_root=entry.skill.source_root)
            except Exception:
                logger.exception("Failed to hot-reload skill '%s', keeping old version", name)
                continue
            self._skills[name] = SkillEntry(skill=parsed, file_hash=current_hash)
            reloaded.append(name)
            logger.info("Hot-reloaded skill '%s'", name)
        return reloaded

    def all_skills(self) -> list[SkillEntry]:
        """All discovered skills, bundled before user, alphabetical within each root."""
        return sorted(self._skills.values(), key=lambda e: (_ROOT_ORDER.get(e.skill.source_root, 2), e.name))

    def get(self, name: str) -> SkillEntry | None:
        return self._skills.get(name)

    def render_prompt_block(self, max_tokens: int | None = None) -> str:
        """Render the 'Available Skills' system-prompt block, applying graceful
        truncation (description shortening, then dropping trailing entries with
        a transparency note) once the estimated size exceeds max_tokens."""
        entries = self.all_skills()
        if not entries:
            return ""

        def render(desc_limit: int | None) -> list[str]:
            lines = []
            for e in entries:
                desc = e.description
                if desc_limit is not None and len(desc) > desc_limit:
                    desc = desc[: desc_limit - 1].rstrip() + "…"
                lines.append(f"- {e.name}: {desc}")
            return lines

        lines = render(None)
        if max_tokens is None:
            return "\n".join(lines)

        budget_chars = max_tokens * 4  # rough token estimate: ~4 chars/token

        if sum(len(line) + 1 for line in lines) <= budget_chars:
            return "\n".join(lines)

        lines = render(_DESC_TRUNCATE_LEN)
        if sum(len(line) + 1 for line in lines) <= budget_chars:
            return "\n".join(lines)

        kept: list[str] = []
        total = 0
        for line in lines:
            if total + len(line) + 1 > budget_chars:
                break
            kept.append(line)
            total += len(line) + 1

        remaining = len(lines) - len(kept)
        if remaining > 0:
            kept.append(_TRUNCATION_NOTE.format(n=remaining))
        return "\n".join(kept)

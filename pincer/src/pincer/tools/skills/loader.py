"""
Skill loader: discover, validate, import, and manage skill plugins.

Skills are directories containing manifest.json + skill.py.
They are discovered from two locations:
  1. Bundled skills:  <project>/skills/
  2. User skills:     ~/.pincer/skills/

Each skill's tools are namespaced as skill_name__tool_name to avoid collisions
with built-in tools and other skills.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.exceptions import SkillLoadError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from pincer.tools.skills.scanner import SkillScanner

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}


@dataclass
class SkillManifest:
    """Parsed and validated skill manifest."""

    name: str
    version: str
    description: str
    author: str
    permissions: list[str]
    env_required: list[str]
    tools: list[dict[str, Any]]
    skill_id: str = ""
    install_path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], install_path: str) -> SkillManifest:
        """Parse and validate a manifest dict. Raises SkillLoadError on problems."""
        for key in ("name", "version", "description", "tools"):
            if key not in data or not data[key]:
                raise SkillLoadError(f"Manifest missing required field: '{key}'")

        tools = data["tools"]
        if not isinstance(tools, list) or len(tools) == 0:
            raise SkillLoadError("Manifest 'tools' must be a non-empty list")

        for i, tool in enumerate(tools):
            if "name" not in tool or not tool["name"]:
                raise SkillLoadError(f"Tool at index {i} missing 'name'")
            if "description" not in tool or not tool["description"]:
                raise SkillLoadError(f"Tool '{tool.get('name', i)}' missing 'description'")
            if "input_schema" not in tool:
                tool["input_schema"] = {"type": "object", "properties": {}}

        name = data["name"]
        raw = f"{name}:{install_path}"
        skill_id = hashlib.sha256(raw.encode()).hexdigest()[:16]

        return cls(
            name=name,
            version=data["version"],
            description=data["description"],
            author=data.get("author", "unknown"),
            permissions=data.get("permissions", []),
            env_required=data.get("env_required", []),
            tools=tools,
            skill_id=skill_id,
            install_path=install_path,
        )


@dataclass
class LoadedSkill:
    """A fully loaded and ready-to-use skill."""

    manifest: SkillManifest
    module: ModuleType
    tool_functions: dict[str, Callable[..., Any]]
    loaded_at: float = field(default_factory=time.time)
    file_hash: str = ""


class SkillLoader:
    """Discovers, validates, loads, and manages skill plugins."""

    def __init__(
        self,
        bundled_dir: Path | None = None,
        user_dir: Path | None = None,
        scanner: SkillScanner | None = None,
        min_safety_score: int = 50,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._user_dir = user_dir or (Path.home() / ".pincer" / "skills")
        self._scanner = scanner
        self._min_safety_score = min_safety_score
        self._skills: dict[str, LoadedSkill] = {}
        self._load_lock = asyncio.Lock()

    @property
    def skills(self) -> dict[str, LoadedSkill]:
        return dict(self._skills)

    def _discover_skill_dirs(self) -> list[Path]:
        """Scan bundled and user directories for valid skill directories."""
        dirs: list[Path] = []
        for base in (self._bundled_dir, self._user_dir):
            if base is None or not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                    continue
                if (entry / "manifest.json").is_file() and (entry / "skill.py").is_file():
                    dirs.append(entry)
        return dirs

    async def discover_and_load(self) -> dict[str, LoadedSkill]:
        """Discover all skills and load them. Returns loaded skills dict."""
        async with self._load_lock:
            skill_dirs = self._discover_skill_dirs()
            loaded = 0
            for skill_dir in skill_dirs:
                try:
                    skill = self._load_skill(skill_dir)
                    self._skills[skill.manifest.skill_id] = skill
                    loaded += 1
                except SkillLoadError as e:
                    logger.warning("Skipped skill %s: %s", skill_dir.name, e)
                except Exception:
                    logger.exception("Unexpected error loading skill %s", skill_dir.name)
            logger.info("Loaded %d of %d discovered skills", loaded, len(skill_dirs))
            return dict(self._skills)

    def _load_skill(self, skill_dir: Path) -> LoadedSkill:
        """Load a single skill from a directory. Raises SkillLoadError."""
        manifest_path = skill_dir / "manifest.json"
        skill_py_path = skill_dir / "skill.py"

        if not manifest_path.is_file():
            raise SkillLoadError(f"Missing manifest.json in {skill_dir}")
        if not skill_py_path.is_file():
            raise SkillLoadError(f"Missing skill.py in {skill_dir}")

        try:
            raw = manifest_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SkillLoadError(f"Invalid JSON in manifest.json: {e}") from e

        manifest = SkillManifest.from_dict(data, str(skill_dir))

        missing_env = [
            var for var in manifest.env_required if not os.environ.get(var)
        ]
        if missing_env:
            raise SkillLoadError(
                f"Missing required environment variables: {', '.join(missing_env)}"
            )

        if self._scanner is not None:
            result = self._scanner.scan_file(
                str(skill_py_path), manifest=manifest
            )
            if result.score < self._min_safety_score:
                raise SkillLoadError(
                    f"Safety scan failed (score={result.score}, "
                    f"min={self._min_safety_score}): {result.summary()}"
                )

        module = self._import_module(manifest.name, skill_py_path)

        tool_functions: dict[str, Callable[..., Any]] = {}
        for tool_def in manifest.tools:
            fn = getattr(module, tool_def["name"], None)
            if fn is None or not callable(fn):
                logger.warning(
                    "Skill '%s': function '%s' not found in skill.py, skipping",
                    manifest.name,
                    tool_def["name"],
                )
                continue
            tool_functions[tool_def["name"]] = fn

        if not tool_functions:
            raise SkillLoadError(
                f"No matching callable functions found in skill.py for skill '{manifest.name}'"
            )

        file_hash = hashlib.sha256(
            skill_py_path.read_bytes()
        ).hexdigest()

        return LoadedSkill(
            manifest=manifest,
            module=module,
            tool_functions=tool_functions,
            loaded_at=time.time(),
            file_hash=file_hash,
        )

    def _import_module(self, name: str, path: Path) -> ModuleType:
        """Import a skill module, clearing old version if present."""
        module_name = f"pincer_skill_{name}"

        if module_name in sys.modules:
            del sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise SkillLoadError(f"Cannot create module spec for {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise SkillLoadError(f"Failed to import skill '{name}': {e}") from e

        return module

    def check_for_changes(self) -> list[str]:
        """Check loaded skills for file changes and hot-reload if needed."""
        reloaded: list[str] = []
        for skill_id, skill in list(self._skills.items()):
            skill_py = Path(skill.manifest.install_path) / "skill.py"
            if not skill_py.is_file():
                logger.warning(
                    "Skill '%s' skill.py removed, keeping old version",
                    skill.manifest.name,
                )
                continue
            current_hash = hashlib.sha256(skill_py.read_bytes()).hexdigest()
            if current_hash != skill.file_hash:
                try:
                    new_skill = self._load_skill(Path(skill.manifest.install_path))
                    self._skills[skill_id] = new_skill
                    reloaded.append(skill_id)
                    logger.info("Hot-reloaded skill '%s'", skill.manifest.name)
                except Exception:
                    logger.exception(
                        "Failed to hot-reload skill '%s', keeping old version",
                        skill.manifest.name,
                    )
        return reloaded

    def unload(self, skill_id: str) -> bool:
        """Unload a skill by ID. Returns True if found and removed."""
        skill = self._skills.pop(skill_id, None)
        if skill is None:
            return False
        module_name = f"pincer_skill_{skill.manifest.name}"
        sys.modules.pop(module_name, None)
        logger.info("Unloaded skill '%s'", skill.manifest.name)
        return True

    def get_all_tool_functions(self) -> dict[str, Callable[..., Any]]:
        """Return flat dict: 'skill_name.tool_name' -> callable."""
        result: dict[str, Callable[..., Any]] = {}
        for skill in self._skills.values():
            for tool_name, fn in skill.tool_functions.items():
                key = f"{skill.manifest.name}.{tool_name}"
                result[key] = fn
        return result

    def get_all_tool_schemas(self) -> list[dict[str, Any]]:
        """Return LLM-compatible tool schemas for all loaded skills."""
        schemas: list[dict[str, Any]] = []
        for skill in self._skills.values():
            for tool_def in skill.manifest.tools:
                if tool_def["name"] not in skill.tool_functions:
                    continue
                schemas.append({
                    "name": f"{skill.manifest.name}__{tool_def['name']}",
                    "description": (
                        f"[Skill: {skill.manifest.name}] {tool_def['description']}"
                    ),
                    "input_schema": tool_def.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                })
        return schemas

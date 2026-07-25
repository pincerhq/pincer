"""Skill tools: progressive disclosure (load_skill / load_skill_reference) and
sandboxed script execution (run_skill_script) over a SkillIndex.

These are caller-wired (constructed with a live SkillIndex) rather than
registered via bootstrap.py's static builtin list — see that module's
docstring for why skills/MCP/channel tools need caller-owned state.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from pincer.tools.path_sandbox import confine_path
from pincer.tools.sandbox import SandboxConfig, execute_script

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from pincer.tools.skills.index import SkillIndex

logger = logging.getLogger(__name__)

_UNSANDBOXED_TIMEOUT = 60


async def _run_unsandboxed(script_path: Path, args: list[str]) -> str:
    """Run a script as a plain subprocess, no rlimits/network filtering."""
    argv = [sys.executable, str(script_path), *args] if script_path.suffix == ".py" else [str(script_path), *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(script_path.parent),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=_UNSANDBOXED_TIMEOUT)
    except TimeoutError:
        return "Error: script timed out"
    except Exception as e:
        return f"Error: failed to execute script: {e}"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return f"Error: {stderr or stdout or f'exit code {proc.returncode}'}"
    return stdout or "(script produced no output)"


def make_skills_tools(index: SkillIndex, sandbox_disabled: bool = False) -> dict[str, Callable[..., Awaitable[str]]]:
    """Build the load_skill / load_skill_reference / run_skill_script handlers
    bound to a specific SkillIndex."""

    async def load_skill(name: str) -> str:
        """
        Load a skill's full instructions.

        name: Exact skill name, as shown in the Available Skills list
        """
        entry = index.get(name)
        if entry is None:
            available = ", ".join(e.name for e in index.all_skills()) or "(none)"
            return f"Error: no skill named '{name}'. Available skills: {available}"

        files = entry.list_files()
        manifest = "\n".join(f"  - {f}" for f in files) if files else "  (none)"
        return (
            f"{entry.skill.body}\n\n---\nFiles in this skill's directory "
            f"(use load_skill_reference to read one):\n{manifest}"
        )

    async def load_skill_reference(name: str, path: str) -> str:
        """
        Read a file referenced by a skill (e.g. a reference doc or data file).

        name: Exact skill name
        path: File path relative to the skill's own directory
        """
        entry = index.get(name)
        if entry is None:
            return f"Error: no skill named '{name}'"

        try:
            target = confine_path(entry.skill.path, path, label=f"skill '{name}'")
        except ValueError as e:
            return f"Error: {e}"

        if not target.is_file():
            return f"Error: file not found: {path}"

        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

    async def run_skill_script(name: str, script: str, args: list[str] | None = None) -> str:
        """
        Run a script bundled with a skill in a sandboxed subprocess.

        name: Exact skill name
        script: Script path relative to the skill's own directory
        args: Command-line arguments to pass to the script
        """
        entry = index.get(name)
        if entry is None:
            return f"Error: no skill named '{name}'"

        try:
            script_path = confine_path(entry.skill.path, script, label=f"skill '{name}'")
        except ValueError as e:
            return f"Error: {e}"

        if not script_path.is_file():
            return f"Error: script not found: {script}"

        if sandbox_disabled:
            logger.warning(
                "Running skill script '%s/%s' without sandboxing (skill_sandbox_disabled=True)", name, script
            )
            return await _run_unsandboxed(script_path, args or [])

        result = await execute_script(
            str(script_path),
            args=args or [],
            config=SandboxConfig(skill_dir=str(entry.skill.path)),
        )

        if not result.success:
            if result.timed_out:
                return "Error: script timed out"
            return f"Error: {result.error}"

        return result.result or "(script produced no output)"

    return {
        "load_skill": load_skill,
        "load_skill_reference": load_skill_reference,
        "run_skill_script": run_skill_script,
    }

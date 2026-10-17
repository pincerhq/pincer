"""Skill tools: progressive disclosure (load_skill / load_skill_reference) and
sandboxed script execution (run_skill_script) over a SkillService's index.

Skill roots may be local or cloud (see `pincer.tools.skills.storage`). Blocking
path I/O runs in a worker thread so remote reads never stall the event loop, and
scripts from cloud roots run from cloudpathlib's cached copy of the skill dir,
refreshed against the bucket before every run.

These are caller-wired (constructed with a live SkillService) rather than
registered via bootstrap.py's static builtin list — see that module's
docstring for why skills/MCP/channel tools need caller-owned state.
"""

from __future__ import annotations

import asyncio
import logging
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pincer.tools.sandbox import SandboxConfig, execute_script
from pincer.tools.skills.storage import confine_skill_path, localize_skill_dir

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloudpathlib import CloudPath

    from pincer.services.skills import SkillService
    from pincer.tools.skills.storage import SkillPath

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


def _read_text(path: SkillPath) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def make_skills_tools(
    service: SkillService,
    sandbox_disabled: bool = False,
) -> dict[str, Callable[..., Awaitable[str]]]:
    """Build the load_skill / load_skill_reference / run_skill_script handlers
    bound to a SkillService. Each call reads the service's current index, so a
    rebuild elsewhere (e.g. `pincer skills rebuild`) is picked up immediately."""

    async def load_skill(name: str) -> str:
        """
        Load a skill's full instructions.

        name: Exact skill name, as shown in the Available Skills list
        """
        index = await asyncio.to_thread(service.ensure_current)
        entry = index.get(name)
        if entry is None:
            available = ", ".join(e.name for e in index.all_skills()) or "(none)"
            return f"Error: no skill named '{name}'. Available skills: {available}"

        try:
            files = await asyncio.to_thread(entry.list_files)
        except Exception as e:
            return f"Error listing files for skill '{name}': {e}"
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
        entry = (await asyncio.to_thread(service.ensure_current)).get(name)
        if entry is None:
            return f"Error: no skill named '{name}'"

        try:
            target = confine_skill_path(entry.skill.path, path, label=f"skill '{name}'")
        except ValueError as e:
            return f"Error: {e}"

        try:
            if not await asyncio.to_thread(target.is_file):
                return f"Error: file not found: {path}"
            return await asyncio.to_thread(_read_text, target)
        except Exception as e:
            return f"Error reading file: {e}"

    async def run_skill_script(name: str, script: str, args: list[str] | None = None) -> str:
        """
        Run a script bundled with a skill in a sandboxed subprocess.

        name: Exact skill name
        script: Script path relative to the skill's own directory
        args: Command-line arguments to pass to the script
        """
        entry = (await asyncio.to_thread(service.ensure_current)).get(name)
        if entry is None:
            return f"Error: no skill named '{name}'"

        skill_dir = entry.skill.path
        try:
            remote_script = confine_skill_path(skill_dir, script, label=f"skill '{name}'")
        except ValueError as e:
            return f"Error: {e}"

        try:
            if not await asyncio.to_thread(remote_script.is_file):
                return f"Error: script not found: {script}"
        except Exception as e:
            return f"Error: cannot access script {script}: {e}"

        if isinstance(remote_script, Path):
            local_dir, script_path = skill_dir, remote_script
        else:
            try:
                local_dir = await asyncio.to_thread(localize_skill_dir, skill_dir)
            except Exception as e:
                return f"Error: failed to download skill '{name}': {e}"
            # Confinement was checked against the remote dir; the relative part is
            # already normalised and cannot escape the cached copy.
            script_path = local_dir / str(remote_script.relative_to(cast("CloudPath", skill_dir)))
            if script_path.suffix != ".py":
                # Object storage keeps no file mode, so cached copies arrive non-executable.
                script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

        if sandbox_disabled:
            logger.warning(
                "Running skill script '%s/%s' without sandboxing (skill_sandbox_disabled=True)", name, script
            )
            return await _run_unsandboxed(script_path, args or [])

        result = await execute_script(
            str(script_path),
            args=args or [],
            config=SandboxConfig(skill_dir=str(local_dir)),
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

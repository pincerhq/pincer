"""Tests for the load_skill / load_skill_reference / run_skill_script builtin tools.

Every test runs once per storage backend (local, S3, Azure, GCS) via the
`skill_root` fixture; cloud-hosted scripts run from cloudpathlib's file cache.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from pincer.services.skills import SkillService
from pincer.tools.builtin.skills_tools import make_skills_tools
from pincer.tools.skills.storage import SkillPath


def _write(path: SkillPath, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def bundled(skill_root: Callable[[str], SkillPath]) -> SkillPath:
    root = skill_root("bundled")
    skill_dir = root / "demo"
    _write(skill_dir / "SKILL.md", "---\nname: demo\ndescription: demo skill\n---\n# Demo body\n")
    _write(skill_dir / "notes.txt", "secret notes")
    _write(skill_dir / "run.py", 'import sys\nprint("ran", sys.argv[1:])\n')
    _write(skill_dir / "scripts" / "hello.sh", '#!/bin/sh\necho "hello $1"\n')
    return root


@pytest.fixture
def service(bundled: SkillPath, tmp_path: Path) -> SkillService:
    return SkillService(bundled_dir=bundled, user_dir=None, cache_dir=tmp_path / "skills-cache")


def _skill_dir(service: SkillService) -> SkillPath:
    return service.ensure_current().get("demo").skill.path


@pytest.mark.asyncio
async def test_load_skill_returns_body_and_manifest(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill"]("demo")
    assert "# Demo body" in result
    assert "notes.txt" in result
    assert "run.py" in result
    assert "scripts/hello.sh" in result


@pytest.mark.asyncio
async def test_load_skill_missing_returns_error(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill"]("nope")
    assert "Error" in result
    assert "demo" in result  # lists available skills


@pytest.mark.asyncio
async def test_load_skill_reference_reads_file(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill_reference"]("demo", "notes.txt")
    assert result == "secret notes"


@pytest.mark.asyncio
async def test_load_skill_reference_missing_file(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill_reference"]("demo", "nope.txt")
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_load_skill_reference_rejects_path_escape(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill_reference"]("demo", "../../etc/passwd")
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_load_skill_reference_unknown_skill(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill_reference"]("nope", "notes.txt")
    assert "Error" in result


@pytest.mark.asyncio
async def test_run_skill_script_sandboxed(service: SkillService) -> None:
    tools = make_skills_tools(service, sandbox_disabled=False)
    result = await tools["run_skill_script"]("demo", "run.py", ["x", "y"])
    assert "ran" in result
    assert "x" in result and "y" in result


@pytest.mark.asyncio
async def test_run_skill_script_unsandboxed(service: SkillService) -> None:
    tools = make_skills_tools(service, sandbox_disabled=True)
    result = await tools["run_skill_script"]("demo", "run.py", ["z"])
    assert "ran" in result
    assert "z" in result


@pytest.mark.asyncio
async def test_run_skill_script_missing_script(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["run_skill_script"]("demo", "nope.py", [])
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_skill_script_rejects_path_escape(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["run_skill_script"]("demo", "../../../etc/passwd", [])
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_run_skill_script_unknown_skill(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["run_skill_script"]("nope", "run.py", [])
    assert "Error" in result


@pytest.mark.asyncio
async def test_load_skill_reference_reads_nested_file(service: SkillService) -> None:
    tools = make_skills_tools(service)
    result = await tools["load_skill_reference"]("demo", "scripts/../scripts/hello.sh")
    assert 'echo "hello $1"' in result


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_disabled", [False, True])
async def test_run_skill_script_non_python(service: SkillService, sandbox_disabled: bool) -> None:
    """Shell scripts run on every backend: cloud copies arrive without a mode,
    so the tool must restore the owner exec bit on the cached copy."""
    skill_dir = _skill_dir(service)
    if isinstance(skill_dir, Path):
        (skill_dir / "scripts" / "hello.sh").chmod(0o755)
    tools = make_skills_tools(service, sandbox_disabled=sandbox_disabled)
    result = await tools["run_skill_script"]("demo", "scripts/hello.sh", ["world"])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_skill_script_sees_script_only_changes(service: SkillService, skill_root) -> None:
    """Editing a script (without touching SKILL.md or rebuilding) takes effect on the next run.

    The edit goes through `skill_root.external`, behind this service's cache, as an
    upload from elsewhere would — writing through the service's own client would
    update its cached copy and prove nothing (PR review)."""
    tools = make_skills_tools(service, sandbox_disabled=True)
    assert "ran" in await tools["run_skill_script"]("demo", "run.py", [])

    _write(skill_root.external("bundled") / "demo" / "run.py", 'print("edited")\n')

    assert await tools["run_skill_script"]("demo", "run.py", []) == "edited"


@pytest.mark.asyncio
async def test_run_skill_script_cloud_runs_from_cloudpathlib_cache(service: SkillService, tmp_path: Path) -> None:
    """No second copy: a cloud script runs from cloudpathlib's own cache dir."""
    skill_dir = _skill_dir(service)
    if isinstance(skill_dir, Path):
        pytest.skip("local skills run in place")
    tools = make_skills_tools(service)
    assert "ran" in await tools["run_skill_script"]("demo", "run.py", [])

    cache_dir = Path(skill_dir.client._local_cache_dir)
    assert cache_dir.is_relative_to(tmp_path)
    assert [p.name for p in cache_dir.rglob("run.py")] == ["run.py"]
    assert not list(tmp_path.rglob("materialized"))


@pytest.mark.asyncio
async def test_run_skill_script_cloud_drops_files_deleted_from_bucket(service: SkillService) -> None:
    skill_dir = _skill_dir(service)
    if isinstance(skill_dir, Path):
        pytest.skip("local skills run in place")
    _write(skill_dir / "list.py", "import os\nprint(sorted(os.listdir('.')))\n")
    tools = make_skills_tools(service, sandbox_disabled=True)
    assert "notes.txt" in await tools["run_skill_script"]("demo", "list.py", [])

    (skill_dir / "notes.txt").unlink()

    assert "notes.txt" not in await tools["run_skill_script"]("demo", "list.py", [])


@pytest.mark.asyncio
async def test_tools_pick_up_a_rebuild(service: SkillService, bundled: SkillPath) -> None:
    tools = make_skills_tools(service)
    assert "Error" in await tools["load_skill"]("late")

    _write(bundled / "late" / "SKILL.md", "---\nname: late\ndescription: d\n---\nLate body\n")
    service.rebuild()

    assert "Late body" in await tools["load_skill"]("late")

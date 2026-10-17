"""Tests for the load_skill / load_skill_reference / run_skill_script builtin tools.

Every test runs once per storage backend (local, S3, Azure, GCS) via the
`skill_root` fixture; cloud-hosted scripts are materialized under `cache_root`.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from pincer.tools.builtin.skills_tools import make_skills_tools
from pincer.tools.skills.index import SkillIndex
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
def index(bundled: SkillPath) -> SkillIndex:
    idx = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    idx.discover()
    return idx


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    return tmp_path / "skill-cache"


@pytest.mark.asyncio
async def test_load_skill_returns_body_and_manifest(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill"]("demo")
    assert "# Demo body" in result
    assert "notes.txt" in result
    assert "run.py" in result
    assert "scripts/hello.sh" in result


@pytest.mark.asyncio
async def test_load_skill_missing_returns_error(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill"]("nope")
    assert "Error" in result
    assert "demo" in result  # lists available skills


@pytest.mark.asyncio
async def test_load_skill_reference_reads_file(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill_reference"]("demo", "notes.txt")
    assert result == "secret notes"


@pytest.mark.asyncio
async def test_load_skill_reference_missing_file(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill_reference"]("demo", "nope.txt")
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_load_skill_reference_rejects_path_escape(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill_reference"]("demo", "../../etc/passwd")
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_load_skill_reference_unknown_skill(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill_reference"]("nope", "notes.txt")
    assert "Error" in result


@pytest.mark.asyncio
async def test_run_skill_script_sandboxed(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, sandbox_disabled=False, cache_root=cache_root)
    result = await tools["run_skill_script"]("demo", "run.py", ["x", "y"])
    assert "ran" in result
    assert "x" in result and "y" in result


@pytest.mark.asyncio
async def test_run_skill_script_unsandboxed(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, sandbox_disabled=True, cache_root=cache_root)
    result = await tools["run_skill_script"]("demo", "run.py", ["z"])
    assert "ran" in result
    assert "z" in result


@pytest.mark.asyncio
async def test_run_skill_script_missing_script(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["run_skill_script"]("demo", "nope.py", [])
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_skill_script_rejects_path_escape(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["run_skill_script"]("demo", "../../../etc/passwd", [])
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_run_skill_script_unknown_skill(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["run_skill_script"]("nope", "run.py", [])
    assert "Error" in result


@pytest.mark.asyncio
async def test_load_skill_reference_reads_nested_file(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    result = await tools["load_skill_reference"]("demo", "scripts/../scripts/hello.sh")
    assert 'echo "hello $1"' in result


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_disabled", [False, True])
async def test_run_skill_script_non_python(index: SkillIndex, cache_root: Path, sandbox_disabled: bool) -> None:
    """Shell scripts run on every backend: cloud downloads arrive without a mode,
    so the tool must restore the owner exec bit on the materialized copy."""
    local = index.get("demo").skill.path
    if isinstance(local, Path):
        (local / "scripts" / "hello.sh").chmod(0o755)
    tools = make_skills_tools(index, sandbox_disabled=sandbox_disabled, cache_root=cache_root)
    result = await tools["run_skill_script"]("demo", "scripts/hello.sh", ["world"])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_skill_script_materializes_only_cloud_skills(index: SkillIndex, cache_root: Path) -> None:
    tools = make_skills_tools(index, cache_root=cache_root)
    assert "ran" in await tools["run_skill_script"]("demo", "run.py", [])

    copies = list((cache_root / "materialized").glob("*/*/run.py"))
    if isinstance(index.get("demo").skill.path, Path):
        assert copies == []
    else:
        assert len(copies) == 1
        assert copies[0].parent.name == index.get("demo").file_hash


@pytest.mark.asyncio
async def test_run_skill_script_cloud_without_cache_root(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["run_skill_script"]("demo", "run.py", [])
    if isinstance(index.get("demo").skill.path, Path):
        assert "ran" in result
    else:
        assert "no local cache configured" in result

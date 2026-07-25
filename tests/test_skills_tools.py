"""Tests for the load_skill / load_skill_reference / run_skill_script builtin tools."""

from pathlib import Path

import pytest

from pincer.tools.builtin.skills_tools import make_skills_tools
from pincer.tools.skills.index import SkillIndex


@pytest.fixture
def index(tmp_path: Path) -> SkillIndex:
    bundled = tmp_path / "bundled"
    skill_dir = bundled / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n# Demo body\n",
        encoding="utf-8",
    )
    (skill_dir / "notes.txt").write_text("secret notes", encoding="utf-8")
    (skill_dir / "run.py").write_text('import sys\nprint("ran", sys.argv[1:])\n', encoding="utf-8")

    idx = SkillIndex(bundled_dir=bundled, user_dir=None, max_per_root=100)
    idx.discover()
    return idx


@pytest.mark.asyncio
async def test_load_skill_returns_body_and_manifest(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill"]("demo")
    assert "# Demo body" in result
    assert "notes.txt" in result
    assert "run.py" in result


@pytest.mark.asyncio
async def test_load_skill_missing_returns_error(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill"]("nope")
    assert "Error" in result
    assert "demo" in result  # lists available skills


@pytest.mark.asyncio
async def test_load_skill_reference_reads_file(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill_reference"]("demo", "notes.txt")
    assert result == "secret notes"


@pytest.mark.asyncio
async def test_load_skill_reference_missing_file(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill_reference"]("demo", "nope.txt")
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_load_skill_reference_rejects_path_escape(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill_reference"]("demo", "../../etc/passwd")
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_load_skill_reference_unknown_skill(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["load_skill_reference"]("nope", "notes.txt")
    assert "Error" in result


@pytest.mark.asyncio
async def test_run_skill_script_sandboxed(index: SkillIndex) -> None:
    tools = make_skills_tools(index, sandbox_disabled=False)
    result = await tools["run_skill_script"]("demo", "run.py", ["x", "y"])
    assert "ran" in result
    assert "x" in result and "y" in result


@pytest.mark.asyncio
async def test_run_skill_script_unsandboxed(index: SkillIndex) -> None:
    tools = make_skills_tools(index, sandbox_disabled=True)
    result = await tools["run_skill_script"]("demo", "run.py", ["z"])
    assert "ran" in result
    assert "z" in result


@pytest.mark.asyncio
async def test_run_skill_script_missing_script(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["run_skill_script"]("demo", "nope.py", [])
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_run_skill_script_rejects_path_escape(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["run_skill_script"]("demo", "../../../etc/passwd", [])
    assert "Error" in result
    assert "outside" in result


@pytest.mark.asyncio
async def test_run_skill_script_unknown_skill(index: SkillIndex) -> None:
    tools = make_skills_tools(index)
    result = await tools["run_skill_script"]("nope", "run.py", [])
    assert "Error" in result

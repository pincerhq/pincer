"""Tests for bundled skills under skills/."""

import importlib.util
import json
from pathlib import Path

import pytest

SKILL_NAMES = [
    "weather",
    "news",
    "translate",
    "summarize_url",
    "youtube_summary",
    "expense_tracker",
    "habit_tracker",
    "pomodoro",
    "stock_price",
    "git_status",
]

SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _import_skill(skill_name: str):
    spec = importlib.util.spec_from_file_location(
        f"skill_{skill_name}",
        SKILLS_DIR / skill_name / "skill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Parameterized manifest validation (runs for all 10 skills) ──


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_dir_exists(skill_name: str) -> None:
    """skills/{name}/ directory exists."""
    skill_dir = SKILLS_DIR / skill_name
    assert skill_dir.is_dir(), f"skills/{skill_name}/ should exist"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_manifest_valid_json(skill_name: str) -> None:
    """manifest.json exists and parses as valid JSON."""
    manifest_path = SKILLS_DIR / skill_name / "manifest.json"
    assert manifest_path.is_file(), f"manifest.json should exist in {skill_name}"
    content = manifest_path.read_text()
    json.loads(content)


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_py_exists(skill_name: str) -> None:
    """skill.py exists."""
    skill_py = SKILLS_DIR / skill_name / "skill.py"
    assert skill_py.is_file(), f"skill.py should exist in {skill_name}"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_py_compiles(skill_name: str) -> None:
    """skill.py compiles (no syntax errors)."""
    skill_py = SKILLS_DIR / skill_name / "skill.py"
    source = skill_py.read_text()
    compile(source, str(skill_py), "exec")


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_manifest_required_fields(skill_name: str) -> None:
    """name, version, description, tools all present."""
    manifest_path = SKILLS_DIR / skill_name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert "name" in manifest, f"{skill_name}: manifest missing 'name'"
    assert "version" in manifest, f"{skill_name}: manifest missing 'version'"
    assert "description" in manifest, f"{skill_name}: manifest missing 'description'"
    assert "tools" in manifest, f"{skill_name}: manifest missing 'tools'"
    assert isinstance(manifest["tools"], list), f"{skill_name}: tools must be a list"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_manifest_name_matches_dir(skill_name: str) -> None:
    """manifest.name == directory name."""
    manifest_path = SKILLS_DIR / skill_name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["name"] == skill_name, f"manifest.name '{manifest['name']}' should match dir '{skill_name}'"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_tools_have_name_and_description(skill_name: str) -> None:
    """Each tool in manifest has name and description."""
    manifest_path = SKILLS_DIR / skill_name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for i, tool in enumerate(manifest.get("tools", [])):
        assert "name" in tool, f"{skill_name} tool[{i}]: missing 'name'"
        assert "description" in tool, f"{skill_name} tool[{i}]: missing 'description'"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_tool_functions_exist(skill_name: str) -> None:
    """Each tool name has a matching def tool_name( in skill.py."""
    manifest_path = SKILLS_DIR / skill_name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    skill_py = SKILLS_DIR / skill_name / "skill.py"
    source = skill_py.read_text()
    for tool in manifest.get("tools", []):
        tool_name = tool["name"]
        pattern = f"def {tool_name}("
        assert pattern in source, f"{skill_name}: tool '{tool_name}' should have def {tool_name}( in skill.py"


# ── Functional tests for specific skills ──


def test_expense_tracker_rejects_negative() -> None:
    """log_expense with amount=-5 returns error."""
    mod = _import_skill("expense_tracker")
    result = mod.log_expense(amount=-5, user_id="test")
    assert "error" in result


def test_expense_tracker_rejects_zero() -> None:
    """log_expense with amount=0 returns error."""
    mod = _import_skill("expense_tracker")
    result = mod.log_expense(amount=0, user_id="test")
    assert "error" in result


def test_summarize_url_rejects_ftp() -> None:
    """summarize_url rejects ftp:// URLs."""
    mod = _import_skill("summarize_url")
    result = mod.summarize_url(url="ftp://evil.com")
    assert "error" in result


def test_summarize_url_rejects_no_scheme() -> None:
    """summarize_url rejects URLs without http/https scheme."""
    mod = _import_skill("summarize_url")
    result = mod.summarize_url(url="not-a-url")
    assert "error" in result


def test_youtube_extract_video_id() -> None:
    """_extract_video_id extracts ID from various YouTube URL formats."""
    mod = _import_skill("youtube_summary")
    extract = mod._extract_video_id
    assert extract("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_invalid_url() -> None:
    """_extract_video_id returns None for invalid URL."""
    mod = _import_skill("youtube_summary")
    assert mod._extract_video_id("not-a-url") is None


def test_git_status_non_repo() -> None:
    """repo_status on non-repo path returns error."""
    mod = _import_skill("git_status")
    result = mod.repo_status(path="/tmp/not-a-repo-12345")
    assert "error" in result


def test_habit_tracker_rejects_empty_name() -> None:
    """add_habit with empty habit_name returns error."""
    mod = _import_skill("habit_tracker")
    result = mod.add_habit(user_id="test", habit_name="")
    assert "error" in result

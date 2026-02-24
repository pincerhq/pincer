"""Integration tests for the sandbox execution system."""

import os
from pathlib import Path

import pytest

from pincer.tools.sandbox import SandboxConfig, SandboxResult, execute

SAFE_SKILL = '''
def add(a, b):
    return {"sum": a + b}

def infinite_loop():
    while True:
        pass
    return {"result": "never"}

def raise_error():
    raise ValueError("intentional error")
'''


@pytest.fixture
def skill_path(tmp_path: Path) -> Path:
    """Create a temporary skill.py for testing."""
    path = tmp_path / "skill.py"
    path.write_text(SAFE_SKILL, encoding="utf-8")
    return path


async def test_basic_execution(skill_path: Path) -> None:
    """Execute add function, check success=True, result={"sum": 3}."""
    result = await execute(str(skill_path), "add", {"a": 1, "b": 2})
    assert result.success is True
    assert result.result == {"sum": 3}


async def test_timeout(skill_path: Path) -> None:
    """Execute infinite_loop with timeout=2, check timed_out=True."""
    config = SandboxConfig(timeout=2)
    result = await execute(str(skill_path), "infinite_loop", {}, config=config)
    assert result.timed_out is True
    assert result.success is False


async def test_function_not_found(skill_path: Path) -> None:
    """Execute nonexistent function, check error message."""
    result = await execute(str(skill_path), "nonexistent", {})
    assert result.success is False
    assert "not found" in (result.error or "").lower()


async def test_function_raises(skill_path: Path) -> None:
    """Execute raise_error, check success=False, error contains ValueError."""
    result = await execute(str(skill_path), "raise_error", {})
    assert result.success is False
    assert "ValueError" in (result.error or "")


async def test_env_isolation(skill_path: Path) -> None:
    """Verify HOME is set to sandbox temp dir, not real home."""
    real_home = os.environ.get("HOME", os.path.expanduser("~"))

    get_home_skill = '''
def get_home():
    import os
    return {"home": os.environ.get("HOME", "")}
'''
    path = skill_path.parent / "get_home_skill.py"
    path.write_text(get_home_skill, encoding="utf-8")

    result = await execute(str(path), "get_home", {})
    assert result.success is True
    assert result.result is not None
    sandbox_home = result.result.get("home", "")
    assert sandbox_home != real_home
    assert "pincer_sandbox" in sandbox_home or sandbox_home.startswith("/tmp")


async def test_execution_time_tracked(skill_path: Path) -> None:
    """Verify execution_time > 0."""
    result = await execute(str(skill_path), "add", {"a": 1, "b": 2})
    assert result.success is True
    assert result.execution_time > 0


async def test_network_blocking(skill_path: Path, tmp_path: Path) -> None:
    """Skill that tries socket.getaddrinfo, execute with allowed_domains, check blocked."""
    network_skill = '''
def try_network():
    import socket
    try:
        socket.getaddrinfo("evil.com", 80)
        return {"result": "connected"}
    except Exception as e:
        return {"error": str(e)}
'''
    path = tmp_path / "network_skill.py"
    path.write_text(network_skill, encoding="utf-8")

    config = SandboxConfig(allowed_domains=["good.com"])
    result = await execute(str(path), "try_network", {}, config=config)
    assert result.success is True
    assert result.result is not None
    error_msg = result.result.get("error", "")
    assert "blocked" in error_msg.lower()


async def test_network_allowing(skill_path: Path) -> None:
    """allowed_domains doesn't affect basic execution (no network needed)."""
    config = SandboxConfig(allowed_domains=["good.com"])
    result = await execute(str(skill_path), "add", {"a": 1, "b": 2}, config=config)
    assert result.success is True
    assert result.result == {"sum": 3}

"""Shared fixtures for sqlite-vec-memory-mcp tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_MEMORY_SERVER = Path(__file__).resolve().parents[1] / "memory_server.py"


@pytest.fixture(scope="session")
def memory_server_module() -> ModuleType:
    """memory_server.py (a script, not a package), loaded once and only for tests
    that ask for it. Tests that touch its tool index reset that state themselves."""
    spec = importlib.util.spec_from_file_location("sqlite_vec_memory_server_under_test", _MEMORY_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

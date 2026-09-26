"""Where raw SQLite access is still allowed, and nowhere else.

The point of the repository layer is that the runtime does not speak to one
database directly. Anything outside this list that reaches for a SQLite driver
has gone around it — and will not run on Postgres.

Both drivers count. `sqlite3` is the synchronous one and nothing under `src`
imports it today, so it is banned outright rather than exempted anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "pincer"

DRIVERS = ("aiosqlite", "sqlite3")

#: file -> why it may still name a SQLite driver.
ALLOWED = {
    "db/engine.py": "names the driver in the URL it builds",
    "voice/retention.py": "ensure_schema_for_connection() takes a caller's connection (a test seam)",
    "observability/call_costs.py": "ensure_call_costs_table() is the same seam",
    "voice/safety_gates.py": "ensure_outbound_tables() is the same seam",
}


def _driver_imports(source: str) -> list[str]:
    """Every way this file pulls in a SQLite driver, including dynamically.

    Parsed rather than matched line by line: `import aiosqlite as x`, an import
    inside a function, and `importlib.import_module("sqlite3")` are all exactly
    the bypass this guard exists to catch.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.split(".")[0] in DRIVERS]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in DRIVERS:
            found.append(str(node.module))
        elif isinstance(node, ast.Constant) and node.value in DRIVERS:
            # `import_module("aiosqlite")` / `__import__("sqlite3")`.
            found.append(str(node.value))
    return found


def test_only_the_documented_files_touch_sqlite_directly():
    offenders = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative in ALLOWED or relative.startswith("db/migrations/"):
            continue
        names = _driver_imports(path.read_text())
        if names:
            offenders[relative] = sorted(set(names))
    assert offenders == {}, f"these bypass the repository layer: {offenders}"


def test_the_allowed_list_has_no_stale_entries():
    """An entry that no longer needs the exemption should be removed."""
    missing = [name for name in ALLOWED if not (SOURCE / name).exists()]
    assert missing == [], f"these files are gone; drop them from ALLOWED: {missing}"
    stale = [name for name in ALLOWED if not _driver_imports((SOURCE / name).read_text())]
    assert stale == []

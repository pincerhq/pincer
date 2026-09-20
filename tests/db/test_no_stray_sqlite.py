"""Where raw SQLite access is still allowed, and nowhere else.

The point of the repository layer is that the runtime does not speak to one
database directly. Anything outside this list that imports `aiosqlite` has
gone around it — and will not run on Postgres.
"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "pincer"

#: file -> why it may still name aiosqlite.
ALLOWED = {
    "db/engine.py": "names the driver in the URL it builds",
    "voice/retention.py": "ensure_schema_for_connection() takes a caller's connection (a test seam)",
    "observability/call_costs.py": "ensure_call_costs_table() is the same seam",
    "voice/safety_gates.py": "ensure_outbound_tables() is the same seam",
}


def test_only_the_documented_files_touch_sqlite_directly():
    offenders = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative in ALLOWED or relative.startswith("db/migrations/"):
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import aiosqlite", "from aiosqlite")):
                offenders[relative] = stripped
                break
    assert offenders == {}, f"these bypass the repository layer: {offenders}"


def test_the_allowed_list_has_no_stale_entries():
    """An entry that no longer needs the exemption should be removed."""
    stale = [name for name in ALLOWED if "aiosqlite" not in (SOURCE / name).read_text()]
    assert stale == []

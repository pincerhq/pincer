"""The CI suite split in tests/suites.py covers every test file once."""

from __future__ import annotations

from fnmatch import fnmatch

import suites

# tests/db runs in the postgres job, not in a suite.
TEST_FILES = sorted(p for p in suites.TESTS_DIR.rglob("test_*.py") if "db" not in p.relative_to(suites.TESTS_DIR).parts)


def _rel(path) -> str:
    return path.relative_to(suites.TESTS_DIR).with_suffix("").as_posix()


def test_every_file_matches_at_most_one_suite() -> None:
    """Overlapping patterns would make a file's suite depend on dict order."""
    overlaps = {
        _rel(path): matched
        for path in TEST_FILES
        if len(matched := [name for name, pats in suites.SUITES.items() if any(fnmatch(_rel(path), p) for p in pats)])
        > 1
    }
    assert not overlaps


def test_every_pattern_matches_a_file() -> None:
    """A pattern left behind by a renamed or deleted file is dead weight."""
    rels = [_rel(path) for path in TEST_FILES]
    stale = [p for pats in suites.SUITES.values() for p in pats if not any(fnmatch(rel, p) for rel in rels)]
    assert not stale


def test_every_suite_has_files() -> None:
    assigned = {suites.suite_of(path) for path in TEST_FILES}
    assert assigned == {suites.CORE, *suites.SUITES}

"""Replayed pilot calls as regression tests (Sprint 10, T10.3).

Every fixture in `fixtures/` is a real call that once surfaced something the
imagination-written personas missed. Replaying them is what keeps a pilot fix
from regressing quietly six weeks later.

The suite is empty until the first pilot exports one — and it says so, rather
than passing silently, so "no fixtures" is visible as a gap rather than as green.
"""

from __future__ import annotations

import json

import pytest

from .fixtures import FIXTURE_DIR, ReplayPersona, load_fixture, load_personas
from .runner import Scenario, run_scenario

FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json")) if FIXTURE_DIR.is_dir() else []


def test_fixture_directory_exists():
    """The directory and its README ship even when empty, so the workflow is
    discoverable before the first pilot rather than after."""
    assert FIXTURE_DIR.is_dir()
    assert (FIXTURE_DIR / "README.md").is_file()


@pytest.mark.skipif(not FIXTURE_FILES, reason="no pilot fixtures exported yet")
@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_pii_review_cleared(path):
    """A committed fixture must have had its name review cleared."""
    load_fixture(path)  # raises when possible_names is non-empty


@pytest.mark.skipif(not FIXTURE_FILES, reason="no pilot fixtures exported yet")
@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_has_a_replayable_script(path):
    fixture = load_fixture(path)
    assert fixture.get("opening"), f"{path.name}: no opening line"
    assert fixture.get("source_call_sid"), f"{path.name}: no source call SID"


@pytest.mark.skipif(not FIXTURE_FILES, reason="no pilot fixtures exported yet")
@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
async def test_replayed_call_terminates_gracefully(path):
    """Whatever the real callee did, the call must still end cleanly.

    This is the reliability contract from Sprint 1, re-asserted against real
    behaviour instead of imagined behaviour.
    """
    fixture = load_fixture(path)
    persona = type("Fixture", (ReplayPersona,), {"__init__": lambda self: ReplayPersona.__init__(self, fixture)})
    result = await run_scenario(
        Scenario(f"replay_{fixture['name']}", persona, language=(fixture.get("language") or "en")[:2])
    )
    assert result.ok, f"{path.name} did not terminate gracefully (phase={result.terminal_phase})"
    assert not result.unverified_claims, f"{path.name} made unverified claims: {result.unverified_claims}"


def test_loader_refuses_an_unreviewed_fixture(tmp_path):
    """The guard itself — an unreviewed fixture must fail CI, not ship."""
    path = tmp_path / "unreviewed.json"
    path.write_text(
        json.dumps(
            {
                "name": "unreviewed",
                "opening": "Hallo?",
                "script": [],
                "review_required": {"possible_names": ["Schneider"]},
            }
        )
    )
    with pytest.raises(ValueError, match="possible personal names"):
        load_fixture(path)


def test_load_personas_is_empty_but_working():
    """No fixtures yet is fine; a crash in the loader is not."""
    assert isinstance(load_personas(), list)

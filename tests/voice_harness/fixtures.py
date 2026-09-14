"""Replay real pilot calls as harness personas (Sprint 10, T10.3).

`pincer pilot export-fixture <call_sid>` writes a PII-masked JSON fixture from a
real call; this turns one back into a `SimulatedCallee` the reliability suite can
run. That closes the loop the imagination-written Sprint 1 personas cannot: a
pilot bug becomes a permanent regression test instead of a fixed-once anecdote.

Fixtures live in `tests/voice_harness/fixtures/`. A fixture is a committed file,
so its `review_required.possible_names` must be cleared by a human before it
lands — `load_fixture` refuses one that still carries flagged names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .personas import HANGUP, PersonaAction, SimulatedCallee, say

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class ReplayPersona(SimulatedCallee):
    """Replays a real callee's turns in order, then hangs up.

    Deliberately not reactive: the point of a replayed fixture is to reproduce
    what a real person actually said, in order. A persona that adapts would no
    longer be the recorded case.
    """

    def __init__(self, fixture: dict[str, Any]) -> None:
        super().__init__()
        self.name = str(fixture.get("name") or "replay")
        self.language = str(fixture.get("language") or "en")
        self.source_call_sid = str(fixture.get("source_call_sid") or "")
        self.notes = str(fixture.get("notes") or "")
        self._opening = str(fixture.get("opening") or "Hello?")
        self._script = [str(line) for line in fixture.get("script") or []]

    def opening(self) -> PersonaAction:
        return say(self._opening)

    def _react(self, agent_text: str) -> PersonaAction:
        index = self.turn - 1
        if index >= len(self._script):
            return HANGUP
        return say(self._script[index])


def load_fixture(path: Path) -> dict[str, Any]:
    """Read a fixture, refusing one whose PII review has not been cleared."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    flagged = (fixture.get("review_required") or {}).get("possible_names") or []
    if flagged:
        raise ValueError(
            f"{path.name} still lists possible personal names {flagged}. Replace them with placeholders and "
            "empty `review_required.possible_names` before committing."
        )
    return fixture


def load_personas() -> list[type[SimulatedCallee]]:
    """Every committed fixture, as persona factories the runner can use."""
    if not FIXTURE_DIR.is_dir():
        return []

    factories: list[type[SimulatedCallee]] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fixture = load_fixture(path)

        def _factory(_fixture: dict[str, Any] = fixture) -> ReplayPersona:
            return ReplayPersona(_fixture)

        _factory.name = fixture.get("name", path.stem)  # type: ignore[attr-defined]
        factories.append(_factory)  # type: ignore[arg-type]
    return factories

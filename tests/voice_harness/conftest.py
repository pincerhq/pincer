"""Voice harness tests wait for voice telemetry writes before their loop closes."""

import pytest


@pytest.fixture(autouse=True)
def _drain_voice_telemetry_after_each(drain_voice_telemetry: None) -> None:
    """Applies `drain_voice_telemetry` (tests/conftest.py) to every harness test."""

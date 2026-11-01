"""LogOnce: the first occurrence at its level, repeats at DEBUG, until cleared."""

from __future__ import annotations

import logging

import pytest

from pincer.core.log_once import LogOnce

LOGGER = "tests.log_once"


@pytest.fixture
def records(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def _taken() -> list[tuple[int, str]]:
        found = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]
        caplog.clear()
        return found

    return _taken


def test_first_at_its_level_then_debug_until_cleared_then_its_level_again(records) -> None:
    once = LogOnce(logging.getLogger(LOGGER), logging.INFO)

    once.log("down (%s)", "503")
    once.log("down (%s)", "504")
    assert records() == [(logging.INFO, "down (503)"), (logging.DEBUG, "down (504)")]

    assert once.clear() is True
    once.log("down again")
    assert records() == [(logging.INFO, "down again")]


def test_clear_says_whether_anything_had_been_logged(records) -> None:
    once = LogOnce(logging.getLogger(LOGGER))

    assert once.clear() is False
    once.log("down")
    assert once.clear() is True
    assert once.clear() is False
    assert records() == [(logging.WARNING, "down")]  # the default level is WARNING


def test_a_traceback_rides_on_every_occurrence_and_a_recovery_line_ends_the_outage(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    once = LogOnce(logging.getLogger(LOGGER), recovery="back up")

    for _ in range(2):
        try:
            raise ConnectionError("down")
        except ConnectionError:
            once.log("failed", exc_info=True)
    once.clear()
    once.clear()  # nothing logged since: no second recovery line

    found = [(r.levelno, r.getMessage(), r.exc_info is not None) for r in caplog.records if r.name == LOGGER]
    assert found == [
        (logging.WARNING, "failed", True),
        (logging.DEBUG, "failed", True),
        (logging.INFO, "back up", False),
    ]

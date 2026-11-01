"""
Log a repeating condition once, then quietly — one idiom for every Google reader.

Background readers hit the same failure on every tick (the reminder every
60 s, the zone lookup each time its retry delay passes, the tool bootstrap on
every ``GET /api/integrations``). The first occurrence is logged at its real
level; repeats go to DEBUG until the condition clears, so an outage is one
line, not one a minute.
"""

from __future__ import annotations

import logging


class LogOnce:
    """The first ``log()`` at ``level``, repeats at DEBUG until ``clear()``."""

    def __init__(self, logger: logging.Logger, level: int = logging.WARNING) -> None:
        self._logger = logger
        self._level = level
        self._logged = False

    def log(self, msg: str, *args: object) -> None:
        self._logger.log(logging.DEBUG if self._logged else self._level, msg, *args)
        self._logged = True

    def clear(self) -> bool:
        """Back to logging at ``level``; True when something had been logged (the caller may note recovery)."""
        was_logged, self._logged = self._logged, False
        return was_logged

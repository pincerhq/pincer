"""
Log a repeating condition once, then quietly — one idiom for every background failure.

Some failures repeat on every tick or message: the calendar reminder every 60 s,
the zone lookup each time its retry delay passes, the tool bootstrap on every
``GET /api/integrations``, the memory backend on every prompt. The first
occurrence is logged at its real level; repeats go to DEBUG until the condition
clears, so an outage is one line, not one a minute.
"""

from __future__ import annotations

import logging
from collections import OrderedDict


class LogOnce:
    """The first ``log()`` at ``level``, repeats at DEBUG until ``clear()``.

    ``recovery``, when given, is logged at INFO by a ``clear()`` that ends an
    outage (something had been logged).
    """

    def __init__(self, logger: logging.Logger, level: int = logging.WARNING, *, recovery: str = "") -> None:
        self._logger = logger
        self._level = level
        self._recovery = recovery
        self._logged = False

    def log(self, msg: str, *args: object, exc_info: bool = False) -> None:
        """Log ``msg``; with ``exc_info`` (call it inside the ``except`` block) every
        occurrence carries the current traceback, the repeats at DEBUG too."""
        level = logging.DEBUG if self._logged else self._level
        if exc_info:
            self._logger.log(level, msg, *args, exc_info=True)
        else:
            self._logger.log(level, msg, *args)
        self._logged = True

    def clear(self) -> bool:
        """Back to logging at ``level``; True when something had been logged (the caller may note recovery)."""
        was_logged, self._logged = self._logged, False
        if was_logged and self._recovery:
            self._logger.info(self._recovery)
        return was_logged


class LogOncePerKey:
    """``LogOnce`` per key: the first ``log(key, …)`` for each key at ``level``,
    repeats for that key at DEBUG. For conditions that differ by value (e.g. one
    line per misconfigured model id), not one line for the whole outage.

    At most ``max_keys`` keys are remembered; the oldest is forgotten first, so
    a long-lived process never grows this without bound (a forgotten key just
    logs at ``level`` once more).
    """

    def __init__(self, logger: logging.Logger, level: int = logging.WARNING, *, max_keys: int = 256) -> None:
        self._logger = logger
        self._level = level
        self._max_keys = max_keys
        self._seen: OrderedDict[str, None] = OrderedDict()

    def log(self, key: str, msg: str, *args: object) -> None:
        if key in self._seen:
            self._seen.move_to_end(key)
            self._logger.debug(msg, *args)
            return
        self._seen[key] = None
        if len(self._seen) > self._max_keys:
            self._seen.popitem(last=False)
        self._logger.log(self._level, msg, *args)

    def __contains__(self, key: object) -> bool:
        return key in self._seen

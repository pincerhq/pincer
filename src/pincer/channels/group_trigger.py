"""
Group-chat trigger matching shared by the Telegram and WhatsApp channels.

A trigger setting is a comma-separated list of words or phrases. Each entry
matches as a whole token/phrase, case-insensitively:

- ``pincer`` matches "hey pincer," and "@pincer", but not "pincers" or "@pincer_bot".
- ``@pincer`` requires the ``@``: it matches "hey @pincer" but not "hey pincer".
- Words in a phrase may be separated by any whitespace, so ``hey pincer``
  matches "Hey   Pincer" and "hey\\npincer".

Entries cannot contain commas. An empty setting disables trigger matching.
"""

from __future__ import annotations

import re
from functools import lru_cache


def parse_group_triggers(raw: str) -> tuple[str, ...]:
    """Split a comma-separated trigger setting into trimmed, non-empty entries."""
    return tuple(entry for part in raw.split(",") if (entry := part.strip()))


@lru_cache(maxsize=32)
def _compile_group_triggers(raw: str) -> re.Pattern[str] | None:
    """Compile all entries into one alternation so a message is scanned once."""
    bodies = [r"\s+".join(re.escape(word) for word in entry.split()) for entry in parse_group_triggers(raw)]
    if not bodies:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(bodies)})(?!\w)", re.IGNORECASE)


def matches_group_trigger(text: str, raw: str) -> bool:
    """Return True if *text* contains any trigger from the *raw* setting."""
    if not text or not raw:
        return False
    pattern = _compile_group_triggers(raw)
    return bool(pattern and pattern.search(text))

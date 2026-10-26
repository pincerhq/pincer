"""
Group-chat trigger matching shared by the Telegram and WhatsApp channels.

A trigger setting is a comma-separated list of words or phrases. Each entry
matches as a whole token/phrase, case-insensitively:

- ``pincer`` matches "hey pincer," and "@pincer", but not "pincers" or "@pincer_bot".
- ``@pincer`` requires the ``@``: it matches "hey @pincer" but not "hey pincer".
- Words in a phrase may be separated by any whitespace, so ``hey pincer``
  matches "Hey   Pincer" and "hey\\npincer".
- Scripts written without spaces between words (CJK, kana, Hangul, Thai,
  Lao, Khmer, Myanmar) don't count as word characters for the boundary
  check, so ``钳子`` still matches inside "请钳子帮忙".

Entries cannot contain commas. An empty setting disables trigger matching.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Letters of scripts written without spaces between words. Python's Unicode
# ``\w`` includes them, so a plain ``(?<!\w)`` boundary would never match a
# trigger embedded in such text.
_NO_SPACE_SCRIPTS = (
    "\u0e00-\u0eff"  # Thai, Lao
    "\u1000-\u109f"  # Myanmar
    "\u1100-\u11ff"  # Hangul Jamo
    "\u1780-\u17ff"  # Khmer
    "\u2e80-\u2fdf"  # CJK radicals, Kangxi radicals
    "\u3005-\u3007"  # Iteration and closing marks, ideographic zero (々 〆 〇)
    "\u303b-\u303c"  # Vertical ideographic iteration mark, masu mark (〻 〼)
    "\u3040-\u31ff"  # Hiragana, Katakana, Bopomofo, Hangul compatibility Jamo
    "\u3400-\u4dbf"  # CJK Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\ua960-\ua97f"  # Hangul Jamo Extended-A
    "\uac00-\ud7ff"  # Hangul syllables, Hangul Jamo Extended-B
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff66-\uffdc"  # Halfwidth Katakana, halfwidth Hangul
    "\U00020000-\U000323af"  # CJK Extensions B-H
)
# A word character of a space-separated script (\w minus the scripts above).
_SPACED_WORD_CHAR = rf"[^\W{_NO_SPACE_SCRIPTS}]"


def parse_group_triggers(raw: str) -> tuple[str, ...]:
    """Split a comma-separated trigger setting into trimmed, non-empty entries."""
    return tuple(entry for part in raw.split(",") if (entry := part.strip()))


@lru_cache(maxsize=32)
def _compile_group_triggers(raw: str) -> re.Pattern[str] | None:
    """Compile all entries into one alternation so a message is scanned once."""
    bodies = [r"\s+".join(re.escape(word) for word in entry.split()) for entry in parse_group_triggers(raw)]
    if not bodies:
        return None
    alternation = "|".join(bodies)
    return re.compile(rf"(?<!{_SPACED_WORD_CHAR})(?:{alternation})(?!{_SPACED_WORD_CHAR})", re.IGNORECASE)


def matches_group_trigger(text: str, raw: str) -> bool:
    """Return True if *text* contains any trigger from the *raw* setting."""
    if not text or not raw:
        return False
    pattern = _compile_group_triggers(raw)
    return bool(pattern and pattern.search(text))

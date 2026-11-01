"""
Reading a yes/no answer to a tool-approval prompt, shared by the chat channels.

The answer is the first word of the message, so "yes please", "Yes!" and
"*no*" all count. Channels strip their own addressing (mentions, trigger
words) before calling :func:`parse_yes_no`.
"""

from __future__ import annotations

import re

YES_WORDS = frozenset(
    {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "approve", "approved", "allow", "так", "да", "ja", "👍", "✅"}
)
NO_WORDS = frozenset({"no", "n", "nope", "deny", "denied", "cancel", "stop", "abort", "ні", "нет", "nein", "👎", "❌"})

# Stripped from both ends of each word, so "yes!" and "*no*" still count.
_WORD_PUNCTUATION = ".,!?;:…-–—()[]{}\"'«»„“”*_~"

# Emoji variation selectors (U+FE0E/U+FE0F) and skin-tone modifiers
# (U+1F3FB-U+1F3FF): stripped so "❤️" matches "❤" and "👍🏽" matches "👍".
_EMOJI_MODIFIERS = re.compile("[\ufe0e\ufe0f\U0001f3fb-\U0001f3ff]")


def norm_emoji(text: str) -> str:
    """Drop emoji variation selectors and skin-tone modifiers from *text*."""
    return _EMOJI_MODIFIERS.sub("", text)


def parse_yes_no(text: str, *, max_words: int | None = None) -> bool | None:
    """True for a yes, False for a no, None when *text* is neither.

    ``max_words`` rejects longer messages, for chats where the person may be
    talking to someone else: "ok guys, see you at 8" is not an approval.
    """
    words = [w for w in (word.strip(_WORD_PUNCTUATION) for word in norm_emoji(text or "").lower().split()) if w]
    if not words or (max_words is not None and len(words) > max_words):
        return None
    if words[0] in YES_WORDS:
        return True
    if words[0] in NO_WORDS:
        return False
    return None

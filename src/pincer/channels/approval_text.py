"""
Reading a yes/no answer to a tool-approval prompt, shared by the chat channels.

An answer is made of yes words (or no words) and politeness fillers only, so
"yes please", "Yes!", "*no*" and "👍👍" count, while "yes but use the other
address", "ok wait" and "да нет" are not an answer and get the re-ask.
Channels strip their own addressing (mentions, trigger words) before calling
:func:`parse_yes_no`.
"""

from __future__ import annotations

import re
import unicodedata

YES_WORDS = frozenset(
    {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "approve", "approved", "allow", "так", "да", "ja"}
    | {"👍", "✅", "👌"}
)
NO_WORDS = frozenset({"no", "n", "nope", "deny", "denied", "cancel", "stop", "abort", "ні", "нет", "nein", "👎", "❌"})

# Words that may follow the answer without changing it: "yes, go ahead please".
FILLER_WORDS = frozenset(
    {"please", "pls", "plz", "go", "ahead", "and", "do", "send", "proceed", "it", "now", "thanks", "thank", "you"}
    | {"thx", "bitte", "danke", "давай", "будь", "ласка", "дякую", "пожалуйста", "спасибо"}
)

# Emoji variation selectors (U+FE0E/U+FE0F) and skin-tone modifiers
# (U+1F3FB-U+1F3FF): stripped so "❤️" matches "❤" and "👍🏽" matches "👍".
_EMOJI_MODIFIERS = re.compile("[\ufe0e\ufe0f\U0001f3fb-\U0001f3ff]")

# A run of letters or digits, or any single other non-space character, so
# "Yes👍" and "👍👍" split into separate tokens.
_TOKEN = re.compile(r"[^\W_]+|[^\w\s]|_")


def norm_emoji(text: str) -> str:
    """Drop emoji variation selectors and skin-tone modifiers from *text*."""
    return _EMOJI_MODIFIERS.sub("", text)


def _tokens(text: str) -> list[str] | None:
    """Words and emoji of *text*, without punctuation; None if it asks a question.

    A run of the same token counts once ("👍👍👍" is one "👍").
    """
    tokens: list[str] = []
    for token in _TOKEN.findall(norm_emoji(text or "").lower()):
        if token == "?":
            return None
        # Keep words and pictographs (So); drop punctuation, "~", "+" and the like.
        if not token[0].isalnum() and unicodedata.category(token[0]) != "So":
            continue
        if not tokens or tokens[-1] != token:
            tokens.append(token)
    return tokens


def parse_yes_no(text: str, *, max_words: int | None = None) -> bool | None:
    """True for a yes, False for a no, None when *text* is neither.

    The first word gives the answer, and every other word must be an answer
    word of the same kind or a filler. ``max_words`` rejects longer messages,
    for chats where the person may be talking to someone else.
    """
    words = _tokens(text)
    if not words or (max_words is not None and len(words) > max_words):
        return None
    for answer_words, decision in ((YES_WORDS, True), (NO_WORDS, False)):
        if words[0] in answer_words:
            return decision if all(w in answer_words or w in FILLER_WORDS for w in words[1:]) else None
    return None

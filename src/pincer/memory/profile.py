"""The user's preferred language, kept in their 'profile' memory.

Onboarding writes the profile as one sentence-per-field memory (see
``SQLiteMemoryBackend.add_profile``); these helpers read and update only its
"Preferred language: …." sentence, through the generic backend interface.
An update passes the entry's tags back, because the MCP backend replaces
them on every update.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend

PROFILE_CATEGORY = "profile"

_LANGUAGE_RE = re.compile(r"Preferred language: ([^.]+)\.")
# The value is put into every chat prompt, so nothing but a language tag
# ("uk", "pt-br") is ever stored or read back: free text there would be a
# standing prompt injection planted through a tool call.
_LANGUAGE_CODE_RE = re.compile(r"[a-z]{2,3}(-[a-z]{2,4})?")


def profile_language(content: str) -> str | None:
    """The preferred language stated in a profile memory, if it is a valid code."""
    match = _LANGUAGE_RE.search(content or "")
    return clean_language(match.group(1)) if match else None


def with_language(content: str, language: str) -> str:
    """``content`` with its preferred-language sentence set to ``language``."""
    sentence = f"Preferred language: {language}."
    if _LANGUAGE_RE.search(content or ""):
        return _LANGUAGE_RE.sub(lambda _: sentence, content, count=1)
    return f"{content.strip()} {sentence}".strip()


# Names a model or user may give instead of a code. Voice reads the stored
# value as a code (its first two letters), so "German" must not stay "German".
_NAME_TO_CODE = {
    "english": "en",
    "englisch": "en",
    "англійська": "en",
    "german": "de",
    "deutsch": "de",
    "німецька": "de",
    "ukrainian": "uk",
    "ukrainisch": "uk",
    "українська": "uk",
    "french": "fr",
    "français": "fr",
    "spanish": "es",
    "español": "es",
    "italian": "it",
    "italiano": "it",
    "polish": "pl",
    "polski": "pl",
    "portuguese": "pt",
    "português": "pt",
    "dutch": "nl",
    "nederlands": "nl",
    "turkish": "tr",
    "türkçe": "tr",
    "russian": "ru",
    "русский": "ru",
}


def clean_language(language: str | None) -> str | None:
    """``language`` as a lowercase language tag ("uk", "pt-br"), or None.

    Known language names become their ISO 639-1 code; any other text is
    rejected (see ``_LANGUAGE_CODE_RE``).
    """
    value = str(language or "").strip().strip(".").strip().lower()
    value = _NAME_TO_CODE.get(value, value)
    return value if _LANGUAGE_CODE_RE.fullmatch(value) else None


async def get_preferred_language(memory: BaseMemoryBackend, user_id: str) -> str | None:
    """The user's stored preferred language, or None."""
    for entry in await memory.list_memories(user_id=user_id, category=PROFILE_CATEGORY, limit=1):
        content = entry.content
        if isinstance(content, str):
            return profile_language(content)
    return None


async def set_preferred_language(memory: BaseMemoryBackend, user_id: str, language: str) -> None:
    """Store ``language`` as the user's preferred language, keeping the rest
    of their profile (name, use-case) as it was. Raises ValueError for a
    value that is not a language code."""
    code = clean_language(language)
    if code is None:
        raise ValueError(f"not a language code: {language!r}")
    entries = await memory.list_memories(user_id=user_id, category=PROFILE_CATEGORY, limit=1)
    if entries:
        entry = entries[0]
        # The tags go back as they were: the MCP backend replaces them on an
        # update, and a profile without its user/category tags is never found again.
        await memory.update_memory(entry.id, content=with_language(entry.content, code), tags=list(entry.tags) or None)
        return
    await memory.store_memory(
        user_id=user_id,
        content=with_language("", code),
        category=PROFILE_CATEGORY,
    )

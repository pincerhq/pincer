"""The user's preferred language, kept in their 'profile' memory.

Onboarding writes the profile as one sentence-per-field memory (see
``SQLiteMemoryBackend.add_profile``); these helpers read and update only its
"Preferred language: …." sentence, through the generic backend interface, so
they work on every memory backend.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend

PROFILE_CATEGORY = "profile"

_LANGUAGE_RE = re.compile(r"Preferred language: ([^.]+)\.")
MAX_LANGUAGE_LEN = 40


def profile_language(content: str) -> str | None:
    """The preferred language stated in a profile memory, if any."""
    match = _LANGUAGE_RE.search(content or "")
    return match.group(1).strip() if match else None


def with_language(content: str, language: str) -> str:
    """``content`` with its preferred-language sentence set to ``language``."""
    sentence = f"Preferred language: {language}."
    if _LANGUAGE_RE.search(content or ""):
        return _LANGUAGE_RE.sub(lambda _: sentence, content, count=1)
    return f"{content.strip()} {sentence}".strip()


def clean_language(language: str) -> str | None:
    """A storable language value: one short line, no sentence break (the
    profile is parsed by sentence). Two- or three-letter codes are lowercased."""
    value = " ".join(str(language or "").split()).strip(" .")
    if not value or len(value) > MAX_LANGUAGE_LEN or "." in value:
        return None
    return value.lower() if len(value) <= 3 else value


async def get_preferred_language(memory: BaseMemoryBackend, user_id: str) -> str | None:
    """The user's stored preferred language, or None."""
    for entry in await memory.list_memories(user_id=user_id, category=PROFILE_CATEGORY, limit=1):
        content = entry.content
        if isinstance(content, str):
            return profile_language(content)
    return None


async def set_preferred_language(memory: BaseMemoryBackend, user_id: str, language: str) -> None:
    """Store ``language`` as the user's preferred language, keeping the rest
    of their profile (name, use-case) as it was."""
    entries = await memory.list_memories(user_id=user_id, category=PROFILE_CATEGORY, limit=1)
    if entries:
        await memory.update_memory(entries[0].id, content=with_language(entries[0].content, language))
        return
    await memory.store_memory(
        user_id=user_id,
        content=with_language("", language),
        category=PROFILE_CATEGORY,
    )

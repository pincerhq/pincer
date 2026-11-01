"""Phone number normalisation shared by dialling and identity lookup."""

from __future__ import annotations

import re

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_e164(number: str) -> str | None:
    """`number` as `+E.164`, or None when it is not a valid one.

    Spaces, dashes and parentheses are dropped and a missing `+` is added, so
    `49 151 2345678` and `+49 (151) 234-5678` both become `+491512345678`.
    """
    cleaned = re.sub(r"[\s\-\(\)]", "", str(number or ""))
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if E164_PATTERN.match(cleaned):
        return cleaned
    return None

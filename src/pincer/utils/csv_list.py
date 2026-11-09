"""Parsing for comma-separated settings and tool arguments.

This is a leaf module: it imports nothing from pincer, so any layer
(config, channels, voice, tools, integrations) can use it.
"""

from __future__ import annotations


def split_csv(raw: str | None) -> list[str]:
    """Split a comma-separated setting into trimmed, non-empty entries."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]

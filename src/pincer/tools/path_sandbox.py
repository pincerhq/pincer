"""Shared path-confinement helper for tools that must stay within a base directory."""

from __future__ import annotations

from pathlib import Path


def confine_path(base: Path, requested: str, label: str = "directory") -> Path:
    """Resolve `requested` within `base`. Raises ValueError if it would escape."""
    base_resolved = base.resolve()

    if requested.startswith("~"):
        requested = requested.replace("~", str(base_resolved), 1)

    raw = Path(requested)
    target = raw.resolve() if raw.is_absolute() else (base_resolved / requested).resolve()

    if target != base_resolved and not str(target).startswith(str(base_resolved) + "/"):
        raise ValueError(f"Access denied: path '{requested}' is outside {label} ({base_resolved}).")
    return target

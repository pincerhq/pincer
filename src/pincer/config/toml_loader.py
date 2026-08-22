"""Shared raw-TOML reading helper for pincer.toml / pincer.local.toml loaders.

Split out of `pincer.mcp.config` so config-layer loaders (e.g.
`pincer.config.identity`) can read the same two files without importing
from `pincer.mcp`, a higher-level package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def read_toml_raw(toml_path: Path) -> dict[str, Any] | None:
    """Read a TOML file and return the parsed dict. Returns None if absent or no TOML parser."""
    if not toml_path.exists():
        logger.warning(f"Pincer {toml_path} not found. Ommit it")
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None

    with open(toml_path, "rb") as f:
        return tomllib.load(f)  # type: ignore[return-value]

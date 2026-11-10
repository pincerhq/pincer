"""Finding a file by name under Pincer's known folders.

A leaf module (imports nothing from pincer), shared by the Drive upload
resolver and the email attachment resolver. They apply different rules on
top, so each keeps its own candidate list and allowlist; this is only the walk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


def iter_named_files(search_dirs: Iterable[str | os.PathLike[str]], basename: str) -> Iterator[Path]:
    """Yield every entry called *basename* under each existing dir, in order.

    Hidden directories are skipped. Entries are yielded as found (not
    resolved), so callers decide how to treat symlinks.
    """
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if basename in files:
                yield Path(root) / basename

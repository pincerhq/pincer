"""Where the Google tools keep local files: Pincer's workspace (`pincer.storage.workspace`).

Drive and Gmail downloads go to the workspace's `downloads/` folder; uploads
to Drive are looked up across the whole workspace. For a cloud storage root
the bucket is the source of truth: a download is uploaded there, and a file
picked for upload is fetched from it first.

Everything here blocks (it may reach the bucket); call it via `asyncio.to_thread`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.storage.workspace import Workspace

DOWNLOADS = "downloads"
# Searched in this order for a bare filename, before a walk of the whole workspace.
_SEARCH_DIRS = (DOWNLOADS, "", "exec_output", "uploads", "screenshots")


def _workspace() -> Workspace:
    from pincer.config import get_settings
    from pincer.storage.workspace import get_workspace

    return get_workspace(get_settings())


def save_download(filename: str, data: bytes) -> str:
    """Save `data` as `downloads/<filename>` (`<name>_<n><ext>` when taken) and return its local path."""
    ws = _workspace()
    name, ext = os.path.splitext(os.path.basename(filename) or "download")
    candidate, counter = f"{name}{ext}", 1
    while ws.is_file(ws.local / DOWNLOADS / candidate):
        candidate = f"{name}_{counter}{ext}"
        counter += 1
    return str(ws.save(DOWNLOADS, candidate, data))


def resolve_upload_path(file_path: str) -> str | None:
    """Resolve a file to upload to Drive, fetched to a local path. Search order:

    1. The exact path (after ~ expansion), when it exists
    2. A path relative to the workspace (e.g. `downloads/report.pdf`)
    3. The basename in the workspace's downloads/, root, exec_output/, uploads/, screenshots/
    4. ~/Desktop/<basename>
    5. The basename anywhere in the workspace

    Returns the absolute local path, or None if not found.
    """
    ws = _workspace()
    expanded = os.path.expanduser(file_path)
    if ws.contains(expanded) and ws.is_file(expanded):
        return str(ws.fetch(expanded))
    if os.path.isfile(expanded):
        return os.path.abspath(expanded)

    basename = os.path.basename(file_path)
    candidates = [file_path, *(os.path.join(sub, basename) for sub in _SEARCH_DIRS)]
    for candidate in candidates:
        if candidate and ws.contains(candidate) and ws.is_file(candidate):
            return str(ws.fetch(candidate))

    desktop = os.path.expanduser(f"~/Desktop/{basename}")
    if os.path.isfile(desktop):
        return os.path.abspath(desktop)

    if basename:
        for rel, _local in ws.files():
            if rel.rsplit("/", 1)[-1] == basename:
                return str(ws.fetch(rel))
    return None


def list_files(pattern: str = "") -> list[tuple[str, int]]:
    """Files in the workspace (relative path, size), optionally filtered by a
    case-insensitive substring of the filename."""
    ws = _workspace()
    needle = pattern.lower()
    rels = [rel for rel, _local in ws.files() if not needle or needle in rel.rsplit("/", 1)[-1].lower()]
    return list(zip(rels, ws.sizes(rels), strict=True))

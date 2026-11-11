"""File operation tools — sandboxed to the workspace (`<storage root>/workspace`).

Paths are relative to the workspace's local directory (or absolute within it).
For a cloud storage root the bucket is the source of truth: reads fetch the
object, writes upload it, and listings come from the bucket, so files a user
uploads in Documents are visible here (see `pincer.storage.workspace`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pincer.config import get_settings
from pincer.storage.workspace import get_workspace
from pincer.tools.path_sandbox import SandboxDenied

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.storage.workspace import Workspace

MAX_READ_SIZE = 100_000  # 100KB


def _workspace() -> Workspace:
    ws = get_workspace(get_settings())
    ws.local.mkdir(parents=True, exist_ok=True)
    return ws


def _sandbox_path(path_str: str) -> Path:
    """Resolve path within sandbox. Raises SandboxDenied if escape attempted."""
    try:
        return _workspace().confine(path_str, label="workspace")
    except SandboxDenied as e:
        raise SandboxDenied(f"{e} All file operations are sandboxed.") from e


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _read(target: Path) -> str:
    ws = _workspace()
    if not ws.is_file(target):
        if ws.is_dir(target):
            return f"Error: Not a file: {target}"
        return f"Error: File not found: {target}"
    size = ws.size(target)
    if size > MAX_READ_SIZE:
        return f"Error: File too large ({size} bytes, max {MAX_READ_SIZE})"
    try:
        content = ws.fetch(target).read_text(encoding="utf-8", errors="replace")
        return f"File: {target.name} ({len(content)} chars)\n\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


async def file_read(path: str) -> str:
    """
    Read a file's content.

    path: File path (relative to workspace or absolute within sandbox)
    """
    target = _sandbox_path(path)
    return await asyncio.to_thread(_read, target)


async def file_write(path: str, content: str) -> str:
    """
    Write content to a file (creates or overwrites).

    path: File path within workspace
    content: Text content to write
    """
    target = _sandbox_path(path)
    try:
        await asyncio.to_thread(_workspace().write_text, target, content)
        return f"Successfully wrote {len(content)} chars to {target.name}"
    except Exception as e:
        return f"Error writing file: {e}"


def _list(target: Path) -> str:
    ws = _workspace()
    if not ws.is_dir(target):
        if ws.is_file(target):
            return f"Error: Not a directory: {target}"
        return f"Error: Directory not found: {target}"
    try:
        children = ws.list(target)
    except PermissionError:
        return f"Error: Permission denied for {target}"
    entries = [
        f"  {entry.name}/" if entry.is_dir else f"  {entry.name} ({_format_size(entry.size)})"
        for entry in children
        if not entry.name.startswith(".")
    ]
    if not entries:
        return f"Directory is empty: {target}"
    return f"Contents of {target.name}/:\n" + "\n".join(entries)


async def file_list(directory: str = ".") -> str:
    """
    List files in a directory.

    directory: Directory path within workspace (default: workspace root)
    """
    target = _sandbox_path(directory)
    return await asyncio.to_thread(_list, target)

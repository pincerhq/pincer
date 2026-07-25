"""File operation tools — sandboxed to workspace directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.config import get_settings
from pincer.tools.path_sandbox import confine_path

if TYPE_CHECKING:
    from pathlib import Path

MAX_READ_SIZE = 100_000  # 100KB


def _sandbox_path(path_str: str) -> Path:
    """Resolve path within sandbox. Raises ValueError if escape attempted."""
    settings = get_settings()
    workspace = settings.data_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    try:
        return confine_path(workspace, path_str, label="workspace")
    except ValueError as e:
        raise ValueError(f"{e} All file operations are sandboxed.") from e


async def file_read(path: str) -> str:
    """
    Read a file's content.

    path: File path (relative to workspace or absolute within sandbox)
    """
    target = _sandbox_path(path)

    if not target.exists():
        return f"Error: File not found: {target}"
    if not target.is_file():
        return f"Error: Not a file: {target}"
    if target.stat().st_size > MAX_READ_SIZE:
        return f"Error: File too large ({target.stat().st_size} bytes, max {MAX_READ_SIZE})"

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return f"File: {target.name} ({len(content)} chars)\n\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


async def file_write(path: str, content: str) -> str:
    """
    Write content to a file (creates or overwrites).

    path: File path within workspace
    content: Text content to write
    """
    target = _sandbox_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {target.name}"
    except Exception as e:
        return f"Error writing file: {e}"


async def file_list(directory: str = ".") -> str:
    """
    List files in a directory.

    directory: Directory path within workspace (default: workspace root)
    """
    target = _sandbox_path(directory)

    if not target.exists():
        return f"Error: Directory not found: {target}"
    if not target.is_dir():
        return f"Error: Not a directory: {target}"

    entries: list[str] = []
    try:
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                entries.append(f"  {item.name}/")
            else:
                size = item.stat().st_size
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.1f}KB"
                else:
                    size_str = f"{size / (1024 * 1024):.1f}MB"
                entries.append(f"  {item.name} ({size_str})")
    except PermissionError:
        return f"Error: Permission denied for {target}"

    if not entries:
        return f"Directory is empty: {target}"

    return f"Contents of {target.name}/:\n" + "\n".join(entries)

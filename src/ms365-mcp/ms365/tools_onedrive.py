"""
OneDrive / SharePoint Files tools — 14 tools for file management.
"""

from __future__ import annotations

import base64
import functools
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import Context

if TYPE_CHECKING:
    from collections.abc import Callable

    from ms365._registry import ClientResolver, ToolRegistry
    from ms365.graph_client import GraphClient

logger = logging.getLogger(__name__)


def _fmt_item(item: dict[str, Any]) -> str:
    """Format a drive item for display."""
    name = item.get("name", "?")
    size = item.get("size", 0)
    is_folder = "folder" in item
    modified = item.get("lastModifiedDateTime", "")
    item_type = "Folder" if is_folder else "File"

    if is_folder:
        child_count = item.get("folder", {}).get("childCount", 0)
        return f"  [{item_type}] {name} ({child_count} items) — modified {modified}\n    ID: {item.get('id', '')}"

    size_str = f"{size / 1024:.1f} KB" if size < 1048576 else f"{size / 1048576:.1f} MB"
    return f"  [{item_type}] {name} ({size_str}) — modified {modified}\n    ID: {item.get('id', '')}"


# ── Tool implementations ──────────────────────────────────────────────────────


async def onedrive__list_drive_items(
    client: GraphClient,
    path: str = "/",
    max_results: int = 50,
) -> str:
    """List files and folders in a OneDrive path."""
    if path == "/":
        api_path = "/me/drive/root/children"
    else:
        clean_path = path.strip("/")
        api_path = f"/me/drive/root:/{clean_path}:/children"

    params: dict[str, Any] = {
        "$top": str(max_results),
        "$select": "id,name,size,lastModifiedDateTime,folder,file",
        "$orderby": "name",
    }
    data = await client.get(api_path, params=params)
    items = data.get("value", [])
    if not items:
        return f"No items found at {path}."
    lines = [_fmt_item(i) for i in items]
    more = bool(data.get("@odata.nextLink"))
    header = f"{len(lines)} item(s) in {path}:"
    if more:
        header += f" (showing first {max_results})"
    return header + "\n" + "\n\n".join(lines)


async def onedrive__search_files(
    client: GraphClient,
    query: str,
    max_results: int = 25,
) -> str:
    """Search OneDrive files by name or content."""
    data = await client.get(
        f"/me/drive/root/search(q='{query}')",
        params={"$top": str(max_results), "$select": "id,name,size,lastModifiedDateTime,folder,file,parentReference"},
    )
    items = data.get("value", [])
    if not items:
        return f"No files matching '{query}'."
    lines = []
    for item in items:
        parent = item.get("parentReference", {}).get("path", "")
        lines.append(f"{_fmt_item(item)}\n    Path: {parent}/{item.get('name', '')}")
    return f"Search results for '{query}' ({len(lines)} found):\n" + "\n\n".join(lines)


async def onedrive__get_file_metadata(
    client: GraphClient,
    item_id: str,
) -> str:
    """Get file/folder properties."""
    item = await client.get(f"/me/drive/items/{item_id}")
    is_folder = "folder" in item
    lines = [
        f"Name: {item.get('name', '?')}",
        f"Type: {'Folder' if is_folder else 'File'}",
        f"Size: {item.get('size', 0)} bytes",
        f"Created: {item.get('createdDateTime', '')}",
        f"Modified: {item.get('lastModifiedDateTime', '')}",
        f"Web URL: {item.get('webUrl', '')}",
        f"ID: {item.get('id', '')}",
    ]
    if not is_folder:
        mime = item.get("file", {}).get("mimeType", "")
        lines.append(f"MIME Type: {mime}")
    else:
        lines.append(f"Children: {item.get('folder', {}).get('childCount', 0)}")
    parent = item.get("parentReference", {})
    if parent.get("path"):
        lines.append(f"Parent: {parent['path']}")
    return "\n".join(lines)


async def onedrive__download_file(
    client: GraphClient,
    item_id: str,
) -> str:
    """Download file content. Returns text for text files, metadata for binary."""
    # Get metadata first to check type
    meta = await client.get(f"/me/drive/items/{item_id}")
    mime = meta.get("file", {}).get("mimeType", "application/octet-stream")
    name = meta.get("name", "file")
    size = meta.get("size", 0)

    content = await client.get_binary(f"/me/drive/items/{item_id}/content")

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        text = content.decode("utf-8", errors="replace")
        return f"File: {name}\nContent:\n{text[:5000]}"

    encoded = base64.b64encode(content).decode("ascii")
    return f"File: {name}\nType: {mime}\nSize: {size} bytes\nContent: [base64, {len(encoded)} chars]"


async def onedrive__get_file_preview(
    client: GraphClient,
    item_id: str,
) -> str:
    """Get a preview/thumbnail URL for a file."""
    data = await client.get(f"/me/drive/items/{item_id}/thumbnails")
    thumbs = data.get("value", [])
    if not thumbs:
        return "No preview available for this file."
    thumb_set = thumbs[0]
    urls = []
    for size in ("large", "medium", "small"):
        if size in thumb_set:
            urls.append(f"  {size}: {thumb_set[size].get('url', '')}")
    return "Preview URLs:\n" + "\n".join(urls) if urls else "No preview available."


async def onedrive__list_shared_with_me(
    client: GraphClient,
    max_results: int = 25,
) -> str:
    """List files shared with the user."""
    data = await client.get(
        "/me/drive/sharedWithMe",
        params={"$top": str(max_results)},
    )
    items = data.get("value", [])
    if not items:
        return "No files shared with you."
    lines = [_fmt_item(i) for i in items]
    return f"{len(lines)} shared item(s):\n" + "\n\n".join(lines)


async def onedrive__list_recent_files(
    client: GraphClient,
    max_results: int = 25,
) -> str:
    """List recently accessed files."""
    data = await client.get("/me/drive/recent", params={"$top": str(max_results)})
    items = data.get("value", [])
    if not items:
        return "No recent files."
    lines = [_fmt_item(i) for i in items]
    return f"{len(lines)} recent file(s):\n" + "\n\n".join(lines)


async def onedrive__upload_file(
    client: GraphClient,
    path: str,
    content: str,
    content_type: str = "text/plain",
) -> str:
    """Upload a file to OneDrive. Content should be text or base64-encoded."""
    clean_path = path.strip("/")
    api_path = f"/me/drive/root:/{clean_path}:/content"

    if content_type.startswith("text/") or content_type in ("application/json", "application/xml"):
        file_bytes = content.encode("utf-8")
    else:
        file_bytes = base64.b64decode(content)

    result = await client.put(api_path, content=file_bytes, content_type=content_type)
    return f"File uploaded: {path}\nSize: {result.get('size', len(file_bytes))} bytes\nID: {result.get('id', '')}"


async def onedrive__create_folder(
    client: GraphClient,
    name: str,
    parent_path: str = "/",
) -> str:
    """Create a new folder in OneDrive."""
    if parent_path == "/":
        api_path = "/me/drive/root/children"
    else:
        clean = parent_path.strip("/")
        api_path = f"/me/drive/root:/{clean}:/children"

    body = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "rename",
    }
    result = await client.post(api_path, json=body)
    return f"Folder created: {name} (id={result.get('id', '')})"


async def onedrive__move_file(
    client: GraphClient,
    item_id: str,
    destination_path: str,
) -> str:
    """Move a file or folder to a new location."""
    clean = destination_path.strip("/")
    body: dict[str, Any] = {
        "parentReference": {"path": f"/drive/root:/{clean}"},
    }
    result = await client.patch(f"/me/drive/items/{item_id}", json=body)
    return f"Item moved to {destination_path} (id={result.get('id', item_id)})"


async def onedrive__rename_file(
    client: GraphClient,
    item_id: str,
    new_name: str,
) -> str:
    """Rename a file or folder."""
    result = await client.patch(f"/me/drive/items/{item_id}", json={"name": new_name})
    return f"Item renamed to '{new_name}' (id={result.get('id', item_id)})"


async def onedrive__copy_file(
    client: GraphClient,
    item_id: str,
    destination_path: str,
    new_name: str = "",
) -> str:
    """Copy a file or folder."""
    clean = destination_path.strip("/")
    body: dict[str, Any] = {
        "parentReference": {"path": f"/drive/root:/{clean}"},
    }
    if new_name:
        body["name"] = new_name
    # Copy returns 202 Accepted with a monitor URL
    await client.post(f"/me/drive/items/{item_id}/copy", json=body)
    return f"Copy initiated for item {item_id} to {destination_path}"


async def onedrive__delete_file(
    client: GraphClient,
    item_id: str,
) -> str:
    """Delete a file or folder (moves to recycle bin)."""
    await client.delete(f"/me/drive/items/{item_id}")
    return f"Item {item_id} deleted (moved to recycle bin)."


async def onedrive__share_file(
    client: GraphClient,
    item_id: str,
    link_type: str = "view",
    scope: str = "anonymous",
) -> str:
    """Create a sharing link for a file."""
    body = {
        "type": link_type,  # view, edit, embed
        "scope": scope,  # anonymous, organization
    }
    result = await client.post(f"/me/drive/items/{item_id}/createLink", json=body)
    link = result.get("link", {}).get("webUrl", "")
    return f"Sharing link created ({link_type}, {scope}):\n{link}"


# ── Registry ──────────────────────────────────────────────────────────────────


def register_onedrive_tools(registry: ToolRegistry, resolve_client: ClientResolver) -> int:
    """Register all 14 OneDrive tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(ctx: Context, **kwargs: Any) -> str:
            client = await resolve_client(ctx)
            return str(await fn(client, **kwargs))

        return wrapper

    registry.register(
        name="onedrive__list_drive_items",
        description="List files and folders in a OneDrive path.",
        handler=_h(onedrive__list_drive_items),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/", "description": "Folder path (e.g. /Documents)"},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    )
    registry.register(
        name="onedrive__search_files",
        description="Search OneDrive files by name or content.",
        handler=_h(onedrive__search_files),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 25},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="onedrive__get_file_metadata",
        description="Get OneDrive file/folder properties, size, and modification date.",
        handler=_h(onedrive__get_file_metadata),
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    )
    registry.register(
        name="onedrive__download_file",
        description="Download a file from OneDrive.",
        handler=_h(onedrive__download_file),
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    )
    registry.register(
        name="onedrive__get_file_preview",
        description="Get a preview/thumbnail URL for a OneDrive file.",
        handler=_h(onedrive__get_file_preview),
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    )
    registry.register(
        name="onedrive__list_shared_with_me",
        description="List OneDrive files shared with you.",
        handler=_h(onedrive__list_shared_with_me),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 25}},
            "required": [],
        },
    )
    registry.register(
        name="onedrive__list_recent_files",
        description="List recently accessed OneDrive files.",
        handler=_h(onedrive__list_recent_files),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 25}},
            "required": [],
        },
    )
    registry.register(
        name="onedrive__upload_file",
        description="Upload a file to OneDrive. Text content is uploaded directly; binary should be base64-encoded.",
        handler=_h(onedrive__upload_file),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Destination path (e.g. /Documents/report.txt)"},
                "content": {"type": "string", "description": "File content (text or base64)"},
                "content_type": {"type": "string", "default": "text/plain"},
            },
            "required": ["path", "content"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__create_folder",
        description="Create a new folder in OneDrive.",
        handler=_h(onedrive__create_folder),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent_path": {"type": "string", "default": "/"},
            },
            "required": ["name"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__move_file",
        description="Move a OneDrive file or folder to a new location.",
        handler=_h(onedrive__move_file),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "destination_path": {"type": "string"},
            },
            "required": ["item_id", "destination_path"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__rename_file",
        description="Rename a OneDrive file or folder.",
        handler=_h(onedrive__rename_file),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["item_id", "new_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__copy_file",
        description="Copy a OneDrive file or folder.",
        handler=_h(onedrive__copy_file),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "destination_path": {"type": "string"},
                "new_name": {"type": "string", "default": ""},
            },
            "required": ["item_id", "destination_path"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__delete_file",
        description="Delete a OneDrive file or folder (moves to recycle bin).",
        handler=_h(onedrive__delete_file),
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="onedrive__share_file",
        description="Create a sharing link for a OneDrive file.",
        handler=_h(onedrive__share_file),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "link_type": {"type": "string", "default": "view", "description": "view, edit, or embed"},
                "scope": {"type": "string", "default": "anonymous", "description": "anonymous or organization"},
            },
            "required": ["item_id"],
        },
        require_approval=True,
    )
    return 14

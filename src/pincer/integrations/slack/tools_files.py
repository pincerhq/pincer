"""File tools — 10 tools.

All functions are async, accept a SlackClient as first arg, and return str.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_file(f: dict[str, Any]) -> str:
    name = f.get("name", "?")
    fid = f.get("id", "?")
    size = f.get("size", 0)
    kind = f.get("pretty_type", f.get("filetype", "?"))
    user = f.get("user", "?")
    return f"{name} (ID: {fid}, {kind}, {size} bytes, uploaded by {user})"


# ── tool functions ─────────────────────────────────────────────────────────────


async def slack__list_files(
    client: SlackClient,
    channel: str = "",
    user: str = "",
    types: str = "",
    count: int = 20,
) -> str:
    """List files shared in the workspace, optionally filtered by channel/user/type."""
    try:
        kwargs: dict[str, Any] = {"count": min(count, 100)}
        if channel:
            kwargs["channel"] = channel
        if user:
            kwargs["user"] = user
        if types:
            kwargs["types"] = types
        resp = await client.bot.files_list(**kwargs)
        files = resp.get("files", [])
        if not files:
            return "No files found."
        lines = [_fmt_file(f) for f in files]
        return f"Found {len(lines)} file(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing files: {exc}"


async def slack__get_file_info(client: SlackClient, file: str) -> str:
    """Get details about a specific file: name, size, type, URL, channels shared in."""
    try:
        resp = await client.bot.files_info(file=file)
        f = resp.get("file", {})
        lines = [
            f"Name: {f.get('name', '?')}",
            f"ID: {f.get('id', '?')}",
            f"Type: {f.get('pretty_type', f.get('filetype', '?'))}",
            f"Size: {f.get('size', 0)} bytes",
            f"Title: {f.get('title', '')}",
            f"Uploaded by: {f.get('user', '?')}",
            f"Created: {f.get('created', '?')}",
            f"URL (private): {f.get('url_private', '')}",
            f"Channels: {', '.join(f.get('channels', []))}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error getting file info: {exc}"


async def slack__download_file(
    client: SlackClient, file: str, save_path: str = ""
) -> str:
    """Download a Slack file to disk.

    file: File ID (F...).
    save_path: Optional path to save. If omitted, saves to a temp file.
    Returns the saved file path.
    """
    try:
        import httpx

        # Get the download URL
        resp = await client.bot.files_info(file=file)
        f = resp.get("file", {})
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            return f"Error: No download URL for file {file}."

        # Get the bot token for auth
        bot_token = client.bot.token  # type: ignore[attr-defined]

        dest = Path(save_path) if save_path else Path(tempfile.mktemp(suffix="_" + f.get("name", "file")))
        async with httpx.AsyncClient() as http:
            r = await http.get(url, headers={"Authorization": f"Bearer {bot_token}"}, follow_redirects=True)
            r.raise_for_status()
            dest.write_bytes(r.content)

        return f"File downloaded to: {dest} ({dest.stat().st_size} bytes)"
    except Exception as exc:
        return f"Error downloading file: {exc}"


async def slack__upload_file(
    client: SlackClient,
    channel: str,
    file_path: str = "",
    content: str = "",
    filename: str = "",
    title: str = "",
    initial_comment: str = "",
) -> str:
    """Upload a file to a Slack channel.

    Provide either file_path (path to local file) or content (text content).
    """
    try:
        if not file_path and not content:
            return "Error: provide either file_path or content."
        kwargs: dict[str, Any] = {"channel": channel}
        if file_path:
            kwargs["file"] = file_path
        if content:
            kwargs["content"] = content
        if filename:
            kwargs["filename"] = filename
        if title:
            kwargs["title"] = title
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        resp = await client.bot.files_upload_v2(**kwargs)
        f = resp.get("file", {})
        return f"File uploaded: {f.get('name', '?')} (ID: {f.get('id', '?')}) to {channel}"
    except AttributeError:
        # Fall back to older files_upload if files_upload_v2 unavailable
        try:
            kwargs2: dict[str, Any] = {"channels": channel}
            if file_path:
                kwargs2["file"] = file_path
            if content:
                kwargs2["content"] = content
            if filename:
                kwargs2["filename"] = filename
            if title:
                kwargs2["title"] = title
            if initial_comment:
                kwargs2["initial_comment"] = initial_comment
            resp = await client.bot.files_upload(**kwargs2)
            f = resp.get("file", {})
            return f"File uploaded: {f.get('name', '?')} (ID: {f.get('id', '?')}) to {channel}"
        except Exception as exc2:
            return f"Error uploading file: {exc2}"
    except Exception as exc:
        return f"Error uploading file: {exc}"


async def slack__share_file(
    client: SlackClient, file: str, channels: str
) -> str:
    """Share an existing file to additional channels (comma-separated channel IDs)."""
    try:
        channel_list = [c.strip() for c in channels.split(",") if c.strip()]
        # Post a message with the file attached to each channel
        results = []
        for ch in channel_list:
            resp = await client.bot.chat_postMessage(
                channel=ch,
                text="",
                attachments=[{"fallback": f"Shared file {file}", "file_ids": [file]}],
            )
            results.append(f"{ch} (ts: {resp.get('ts', '?')})")
        return f"File {file} shared to: {', '.join(results)}"
    except Exception as exc:
        return f"Error sharing file: {exc}"


async def slack__delete_file(client: SlackClient, file: str) -> str:
    """Delete a file from Slack."""
    try:
        await client.bot.files_delete(file=file)
        return f"File {file} deleted."
    except Exception as exc:
        return f"Error deleting file: {exc}"


async def slack__list_remote_files(client: SlackClient, count: int = 20) -> str:
    """List external (remote) files added to Slack."""
    try:
        resp = await client.bot.files_remote_list(limit=min(count, 100))
        files = resp.get("files", [])
        if not files:
            return "No remote files found."
        lines = [
            f"{f.get('title', '?')} (ID: {f.get('id', '?')}, url: {f.get('external_url', '')})"
            for f in files
        ]
        return f"Found {len(lines)} remote file(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing remote files: {exc}"


async def slack__add_remote_file(
    client: SlackClient,
    title: str,
    external_url: str,
    external_id: str = "",
    filetype: str = "",
) -> str:
    """Add an external file reference (remote file) to Slack."""
    try:
        kwargs: dict[str, Any] = {
            "title": title,
            "external_url": external_url,
            "external_id": external_id or title.replace(" ", "_"),
        }
        if filetype:
            kwargs["filetype"] = filetype
        resp = await client.bot.files_remote_add(**kwargs)
        f = resp.get("file", {})
        return f"Remote file added: {f.get('title', '?')} (ID: {f.get('id', '?')})"
    except Exception as exc:
        return f"Error adding remote file: {exc}"


async def slack__update_remote_file(
    client: SlackClient,
    file: str,
    title: str = "",
    external_url: str = "",
) -> str:
    """Update a remote file's title or URL."""
    try:
        kwargs: dict[str, Any] = {"file": file}
        if title:
            kwargs["title"] = title
        if external_url:
            kwargs["external_url"] = external_url
        resp = await client.bot.files_remote_update(**kwargs)
        f = resp.get("file", {})
        return f"Remote file updated: {f.get('title', '?')} (ID: {f.get('id', '?')})"
    except Exception as exc:
        return f"Error updating remote file: {exc}"


async def slack__remove_remote_file(client: SlackClient, file: str) -> str:
    """Remove a remote file reference from Slack."""
    try:
        await client.bot.files_remote_remove(file=file)
        return f"Remote file {file} removed."
    except Exception as exc:
        return f"Error removing remote file: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_file_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 10 file tools. Returns 10."""

    def _h(
        fn: Callable[..., Awaitable[str]],
    ) -> Callable[..., Awaitable[str]]:
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__list_files",
        description="List files shared in the workspace, optionally filtered by channel, user, or type.",
        handler=_h(slack__list_files),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Filter by channel ID", "default": ""},
                "user": {"type": "string", "description": "Filter by user ID", "default": ""},
                "types": {
                    "type": "string",
                    "description": "Filter by type (all, spaces, snippets, images, gdocs, zips, pdfs)",
                    "default": "",
                },
                "count": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    )
    registry.register(
        name="slack__get_file_info",
        description="Get details about a specific file: name, size, type, URL, channels shared in.",
        handler=_h(slack__get_file_info),
        parameters={
            "type": "object",
            "properties": {"file": {"type": "string", "description": "File ID (F...)"}},
            "required": ["file"],
        },
    )
    registry.register(
        name="slack__download_file",
        description="Download a Slack file to disk. Returns the saved file path.",
        handler=_h(slack__download_file),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File ID (F...)"},
                "save_path": {"type": "string", "description": "Local path to save (optional)", "default": ""},
            },
            "required": ["file"],
        },
    )
    registry.register(
        name="slack__upload_file",
        description="Upload a local file or text content to a Slack channel.",
        handler=_h(slack__upload_file),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Destination channel ID"},
                "file_path": {"type": "string", "description": "Path to local file", "default": ""},
                "content": {"type": "string", "description": "Text content to upload as a snippet", "default": ""},
                "filename": {"type": "string", "description": "File name", "default": ""},
                "title": {"type": "string", "description": "File title", "default": ""},
                "initial_comment": {"type": "string", "description": "Message to post with the file", "default": ""},
            },
            "required": ["channel"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__share_file",
        description="Share an existing Slack file to additional channels.",
        handler=_h(slack__share_file),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File ID (F...)"},
                "channels": {"type": "string", "description": "Comma-separated channel IDs"},
            },
            "required": ["file", "channels"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__delete_file",
        description="Delete a file from Slack.",
        handler=_h(slack__delete_file),
        parameters={
            "type": "object",
            "properties": {"file": {"type": "string", "description": "File ID (F...)"}},
            "required": ["file"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__list_remote_files",
        description="List external (remote) file references added to Slack.",
        handler=_h(slack__list_remote_files),
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer", "default": 20}},
            "required": [],
        },
    )
    registry.register(
        name="slack__add_remote_file",
        description="Add an external file reference (remote file) to Slack.",
        handler=_h(slack__add_remote_file),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Display title"},
                "external_url": {"type": "string", "description": "URL of the external resource"},
                "external_id": {"type": "string", "description": "Unique ID for the file", "default": ""},
                "filetype": {"type": "string", "description": "File type hint", "default": ""},
            },
            "required": ["title", "external_url"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__update_remote_file",
        description="Update a remote file's title or external URL.",
        handler=_h(slack__update_remote_file),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File ID (F...)"},
                "title": {"type": "string", "description": "New title", "default": ""},
                "external_url": {"type": "string", "description": "New external URL", "default": ""},
            },
            "required": ["file"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__remove_remote_file",
        description="Remove a remote file reference from Slack.",
        handler=_h(slack__remove_remote_file),
        parameters={
            "type": "object",
            "properties": {"file": {"type": "string", "description": "File ID (F...)"}},
            "required": ["file"],
        },
        require_approval=True,
    )

    return 10

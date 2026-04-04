"""
Google Drive tools — 15 tools for file listing, searching, and management.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import mimetypes
import os
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.models import fmt_file, fmt_list
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_EXPORT_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "html": "text/html",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "epub": "application/epub+zip",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "tsv": "text/tab-separated-values",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "svg": "image/svg+xml",
}

# Google-native types that cannot be downloaded with get_media — must be exported
_GOOGLE_NATIVE_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.drawing",
        "application/vnd.google-apps.form",
    }
)

# Valid export formats per Google-native MIME type
_SUPPORTED_EXPORTS: dict[str, list[str]] = {
    "application/vnd.google-apps.document": ["pdf", "docx", "txt", "html", "odt", "rtf", "epub"],
    "application/vnd.google-apps.spreadsheet": ["pdf", "xlsx", "csv", "ods", "tsv"],
    "application/vnd.google-apps.presentation": ["pdf", "pptx", "odp", "txt"],
    "application/vnd.google-apps.drawing": ["pdf", "png", "jpg", "svg"],
}

_DRIVE_FIELDS = "id, name, mimeType, size, modifiedTime, owners, sharingUser, webViewLink"


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_drive_files(
    factory: GoogleServiceFactory,
    folder_id: str = "root",
    max_results: int = 50,
    page_token: str = "",
) -> str:
    """List files/folders in a Drive folder."""
    svc = await factory.get("drive")
    query = f"'{folder_id}' in parents and trashed=false"
    kwargs: dict[str, Any] = {
        "q": query,
        "fields": f"nextPageToken, files({_DRIVE_FIELDS})",
        "pageSize": max_results,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    result = await with_backoff(lambda: svc.files().list(**kwargs).execute())
    files = result.get("files", [])
    if not files:
        return f"No files found in folder {folder_id}."
    lines = [fmt_file(f) for f in files]
    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"{len(lines)} file(s):", more=more)


async def google__search_drive_files(
    factory: GoogleServiceFactory,
    query: str,
    max_results: int = 20,
    page_token: str = "",
) -> str:
    """Search Drive files. Supports: name contains, type, owner, modifiedTime, sharedWithMe, starred."""
    svc = await factory.get("drive")
    kwargs: dict[str, Any] = {
        "q": query,
        "fields": f"nextPageToken, files({_DRIVE_FIELDS})",
        "pageSize": max_results,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    result = await with_backoff(lambda: svc.files().list(**kwargs).execute())
    files = result.get("files", [])
    if not files:
        return f"No files found for: {query}"
    lines = [fmt_file(f) for f in files]
    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"Found {len(lines)} file(s):", more=more)


async def google__get_file_metadata(
    factory: GoogleServiceFactory,
    file_id: str,
) -> str:
    """Get metadata for a file (size, owner, modified time, sharing info)."""
    svc = await factory.get("drive")
    fields = (
        "id, name, mimeType, size, modifiedTime, createdTime, "
        "owners, sharingUser, webViewLink, parents, shared, permissions"
    )
    f = await with_backoff(lambda: svc.files().get(fileId=file_id, fields=fields).execute())
    owners = ", ".join(o.get("emailAddress", "?") for o in f.get("owners", []))
    lines = [
        f"Name:     {f.get('name', '')}",
        f"Type:     {f.get('mimeType', '')}",
        f"Size:     {f.get('size', 'N/A')} bytes",
        f"Created:  {f.get('createdTime', '')}",
        f"Modified: {f.get('modifiedTime', '')}",
        f"Owner(s): {owners}",
        f"Shared:   {f.get('shared', False)}",
        f"Link:     {f.get('webViewLink', '')}",
        f"ID:       {f.get('id', '')}",
    ]
    return "\n".join(lines)


async def google__download_file(
    factory: GoogleServiceFactory,
    file_id: str,
) -> str:
    """Download a non-Google file from Drive and save it to disk. Returns path and metadata."""
    svc = await factory.get("drive")

    # 1. Get file metadata
    metadata = await with_backoff(
        lambda: svc.files().get(fileId=file_id, fields="id,name,mimeType,size").execute()
    )
    filename = metadata.get("name", "download")
    mime_type = metadata.get("mimeType", "application/octet-stream")

    # 2. Guard: Google-native files must be exported, not downloaded
    if mime_type in _GOOGLE_NATIVE_TYPES:
        type_name = mime_type.split(".")[-1]
        return (
            f"Cannot download Google {type_name} directly. "
            f'Use google__export_google_doc(file_id="{file_id}", export_format="pdf") '
            f"to export it as PDF, DOCX, XLSX, etc."
        )

    # 3. Download file bytes
    file_bytes = await with_backoff(lambda: svc.files().get_media(fileId=file_id).execute())

    # 4. Save to disk
    download_dir = os.path.expanduser("~/.pincer/downloads")
    os.makedirs(download_dir, exist_ok=True)

    save_path = os.path.join(download_dir, filename)
    if os.path.exists(save_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(download_dir, f"{name}_{counter}{ext}")
            counter += 1

    with open(save_path, "wb") as f:
        f.write(file_bytes)

    size_kb = len(file_bytes) / 1024
    size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"

    # 5. Return ONLY metadata — never raw content
    return (
        f"File downloaded from Google Drive.\n"
        f"  Filename: {filename}\n"
        f"  Type: {mime_type}\n"
        f"  Size: {len(file_bytes)} bytes ({size_str})\n"
        f"  Saved to: {save_path}\n\n"
        f"The file is saved to disk. Do NOT use python_exec to download it again."
    )


async def google__export_google_doc(
    factory: GoogleServiceFactory,
    file_id: str,
    export_format: str = "pdf",
) -> str:
    """Export a Google Doc/Sheet/Slide to a standard format and save it to disk. Returns path and metadata."""
    svc = await factory.get("drive")

    export_format = export_format.lower().strip().lstrip(".")

    # 1. Validate export format
    if export_format not in _EXPORT_MIME:
        supported = ", ".join(sorted(_EXPORT_MIME.keys()))
        return f"Unsupported export format: '{export_format}'. Supported: {supported}"

    # 2. Get file metadata
    metadata = await with_backoff(
        lambda: svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    )
    original_name = metadata.get("name", "export")
    mime_type = metadata.get("mimeType", "")

    # 3. Validate: file must be a Google-native type
    if mime_type not in _SUPPORTED_EXPORTS:
        return (
            f"File '{original_name}' is not a Google Doc/Sheet/Slide (type: {mime_type}). "
            f'Use google__download_file(file_id="{file_id}") to download it directly.'
        )

    # 4. Validate: format is supported for this file type
    allowed = _SUPPORTED_EXPORTS[mime_type]
    if export_format not in allowed:
        type_name = mime_type.split(".")[-1]
        return (
            f"Google {type_name} cannot be exported as '{export_format}'. "
            f"Supported formats for this file: {', '.join(allowed)}"
        )

    # 5. Export via Drive API
    export_mime = _EXPORT_MIME[export_format]
    file_bytes = await with_backoff(
        lambda: svc.files().export_media(fileId=file_id, mimeType=export_mime).execute()
    )

    if not file_bytes:
        return f"Export returned empty content. The file '{original_name}' may be empty."

    # 6. Build filename: original name + new extension
    base_name = os.path.splitext(original_name)[0]
    filename = f"{base_name}.{export_format}"

    # 7. Save to disk
    download_dir = os.path.expanduser("~/.pincer/downloads")
    os.makedirs(download_dir, exist_ok=True)

    save_path = os.path.join(download_dir, filename)
    if os.path.exists(save_path):
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(download_dir, f"{base_name}_{counter}.{export_format}")
            counter += 1

    with open(save_path, "wb") as f:
        f.write(file_bytes)

    size_kb = len(file_bytes) / 1024
    size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"

    # 8. Return ONLY metadata — never raw content
    return (
        f"Google Doc exported successfully.\n"
        f"  Original: {original_name}\n"
        f"  Exported as: {filename}\n"
        f"  Format: {export_format.upper()} ({export_mime})\n"
        f"  Size: {len(file_bytes)} bytes ({size_str})\n"
        f"  Saved to: {save_path}\n\n"
        f"The file is saved to disk. Do NOT use python_exec to export it again."
    )


async def google__list_shared_drives(
    factory: GoogleServiceFactory,
    max_results: int = 20,
) -> str:
    """List shared/team drives."""
    svc = await factory.get("drive")
    result = await with_backoff(lambda: svc.drives().list(pageSize=max_results).execute())
    drives = result.get("drives", [])
    if not drives:
        return "No shared drives found."
    lines = [f"  {d.get('name', '?')} (id={d['id']})" for d in drives]
    return fmt_list(lines, header=f"{len(lines)} shared drive(s):")


async def google__get_file_permissions(
    factory: GoogleServiceFactory,
    file_id: str,
) -> str:
    """List who has access to a file."""
    svc = await factory.get("drive")
    result = await with_backoff(
        lambda: (
            svc.permissions()
            .list(
                fileId=file_id,
                fields="permissions(id, type, role, emailAddress, displayName)",
            )
            .execute()
        )
    )
    perms = result.get("permissions", [])
    if not perms:
        return f"No permissions found for {file_id}."
    lines = [f"  {p.get('emailAddress', p.get('type', '?'))} — {p.get('role', '?')}" for p in perms]
    return fmt_list(lines, header=f"{len(lines)} permission(s) on {file_id}:")


async def google__list_recent_files(
    factory: GoogleServiceFactory,
    max_results: int = 20,
) -> str:
    """List recently modified files."""
    svc = await factory.get("drive")
    result = await with_backoff(
        lambda: (
            svc.files()
            .list(
                orderBy="modifiedTime desc",
                fields=f"files({_DRIVE_FIELDS})",
                pageSize=max_results,
                q="trashed=false",
            )
            .execute()
        )
    )
    files = result.get("files", [])
    if not files:
        return "No recent files found."
    lines = [fmt_file(f) for f in files]
    return fmt_list(lines, header=f"{len(lines)} recent file(s):")


async def google__upload_file(
    factory: GoogleServiceFactory,
    local_path: str,
    parent_folder_id: str = "",
    name: str = "",
) -> str:
    """Upload a local file to Google Drive."""
    from pathlib import Path as _P

    from googleapiclient.http import MediaFileUpload

    svc = await factory.get("drive")
    path = _P(local_path)
    if not path.exists():
        return f"Error: File not found: {local_path}"
    file_name = name or path.name
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    metadata: dict[str, Any] = {"name": file_name}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    result = await asyncio.to_thread(
        lambda: svc.files().create(body=metadata, media_body=media, fields="id,name,webViewLink").execute()
    )
    return f"Uploaded '{file_name}' → ID: {result.get('id', '')}\nLink: {result.get('webViewLink', '')}"


async def google__create_folder(
    factory: GoogleServiceFactory,
    name: str,
    parent_folder_id: str = "",
) -> str:
    """Create a new folder in Google Drive."""
    svc = await factory.get("drive")
    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]
    result = await with_backoff(lambda: svc.files().create(body=metadata, fields="id, name, webViewLink").execute())
    return f"Folder created: '{name}' (id={result.get('id', '')})"


async def google__move_file(
    factory: GoogleServiceFactory,
    file_id: str,
    destination_folder_id: str,
) -> str:
    """Move a file to a different folder."""
    svc = await factory.get("drive")
    # Get current parents first
    f = await with_backoff(lambda: svc.files().get(fileId=file_id, fields="parents").execute())
    old_parents = ",".join(f.get("parents", []))
    result = await with_backoff(
        lambda: (
            svc.files()
            .update(
                fileId=file_id,
                addParents=destination_folder_id,
                removeParents=old_parents,
                fields="id, name",
            )
            .execute()
        )
    )
    return f"File '{result.get('name', file_id)}' moved to folder {destination_folder_id}."


async def google__rename_file(
    factory: GoogleServiceFactory,
    file_id: str,
    new_name: str,
) -> str:
    """Rename a file in Google Drive."""
    svc = await factory.get("drive")
    result = await with_backoff(
        lambda: svc.files().update(fileId=file_id, body={"name": new_name}, fields="id, name").execute()
    )
    return f"File renamed to '{result.get('name', new_name)}' (id={file_id})"


async def google__copy_file(
    factory: GoogleServiceFactory,
    file_id: str,
    name: str = "",
    parent_folder_id: str = "",
) -> str:
    """Copy a file in Google Drive."""
    svc = await factory.get("drive")
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if parent_folder_id:
        body["parents"] = [parent_folder_id]
    result = await with_backoff(
        lambda: svc.files().copy(fileId=file_id, body=body, fields="id, name, webViewLink").execute()
    )
    return f"File copied: '{result.get('name', '')}' (id={result.get('id', '')})"


async def google__trash_file(
    factory: GoogleServiceFactory,
    file_id: str,
) -> str:
    """Move a file to Drive Trash."""
    svc = await factory.get("drive")
    result = await with_backoff(
        lambda: svc.files().update(fileId=file_id, body={"trashed": True}, fields="id, name").execute()
    )
    return f"File '{result.get('name', file_id)}' moved to Trash."


async def google__share_file(
    factory: GoogleServiceFactory,
    file_id: str,
    email: str = "",
    role: str = "reader",
    link_sharing: bool = False,
) -> str:
    """Share a file with an email address or enable link sharing."""
    svc = await factory.get("drive")
    if link_sharing:
        body: dict[str, Any] = {"type": "anyone", "role": role}
        await with_backoff(lambda: svc.permissions().create(fileId=file_id, body=body, fields="id").execute())
        return f"Link sharing enabled for {file_id} (role={role})"
    if not email:
        return "Error: provide email or set link_sharing=true"
    body = {"type": "user", "role": role, "emailAddress": email}
    await with_backoff(
        lambda: svc.permissions().create(fileId=file_id, body=body, sendNotificationEmail=True, fields="id").execute()
    )
    return f"Shared {file_id} with {email} as {role}."


# ── Registry ──────────────────────────────────────────────────────────────────


def register_drive_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 15 Drive tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    registry.register(
        name="google__list_drive_files",
        description="List files and folders in a Google Drive folder (default: root).",
        handler=_h(google__list_drive_files),
        parameters={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "default": "root"},
                "max_results": {"type": "integer", "default": 50},
                "page_token": {"type": "string", "default": ""},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__search_drive_files",
        description=(
            "Search Google Drive files. Query examples: "
            "'name contains \"budget\"', 'mimeType=\"application/vnd.google-apps.spreadsheet\"', "
            "'sharedWithMe=true', 'starred=true', 'modifiedTime > \"2026-01-01\"'. "
            "Results include mimeType — if it starts with 'application/vnd.google-apps.' "
            "use google__export_google_doc; otherwise use google__download_file."
        ),
        handler=_h(google__search_drive_files),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Drive search query"},
                "max_results": {"type": "integer", "default": 20},
                "page_token": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="google__get_file_metadata",
        description="Get metadata for a Drive file: size, owner, modified time, sharing info.",
        handler=_h(google__get_file_metadata),
        parameters={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    )
    registry.register(
        name="google__download_file",
        description=(
            "Download a file from Google Drive and save it to disk. "
            "Use this for non-Google files (PDFs, images, ZIPs, Office docs, etc.). "
            "For Google Docs/Sheets/Slides, use google__export_google_doc instead. "
            "Returns the saved file path, filename, and size. "
            "The file is saved automatically — do NOT use python_exec to save it again."
        ),
        handler=_h(google__download_file),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
            },
            "required": ["file_id"],
        },
    )
    registry.register(
        name="google__export_google_doc",
        description=(
            "Export a Google Doc, Sheet, Slide, or Drawing to a standard file format "
            "and save it to disk. "
            "Supported formats: pdf, docx, xlsx, csv, pptx, txt, html, png, jpg, svg. "
            "Default export format is 'pdf'. "
            "Returns the saved file path, filename, and size. "
            "The file is saved automatically — do NOT use python_exec to save it again."
        ),
        handler=_h(google__export_google_doc),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "export_format": {
                    "type": "string",
                    "enum": ["pdf", "docx", "xlsx", "csv", "pptx", "txt", "html", "odt", "rtf", "epub", "ods", "tsv", "odp", "png", "jpg", "svg"],
                    "default": "pdf",
                },
            },
            "required": ["file_id"],
        },
    )
    registry.register(
        name="google__list_shared_drives",
        description="List shared/team drives in Google Drive.",
        handler=_h(google__list_shared_drives),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 20}},
            "required": [],
        },
    )
    registry.register(
        name="google__get_file_permissions",
        description="List who has access to a Drive file (emails and roles).",
        handler=_h(google__get_file_permissions),
        parameters={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    )
    registry.register(
        name="google__list_recent_files",
        description="List recently modified files in Google Drive.",
        handler=_h(google__list_recent_files),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 20}},
            "required": [],
        },
    )
    registry.register(
        name="google__upload_file",
        description="Upload a local file to Google Drive.",
        handler=_h(google__upload_file),
        parameters={
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "Absolute local path to the file"},
                "parent_folder_id": {
                    "type": "string",
                    "description": "Destination folder ID (default: root)",
                    "default": "",
                },
                "name": {
                    "type": "string",
                    "description": "Override filename (default: same as local)",
                    "default": "",
                },
            },
            "required": ["local_path"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__create_folder",
        description="Create a new folder in Google Drive.",
        handler=_h(google__create_folder),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent_folder_id": {"type": "string", "default": ""},
            },
            "required": ["name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__move_file",
        description="Move a Drive file to a different folder.",
        handler=_h(google__move_file),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "destination_folder_id": {"type": "string"},
            },
            "required": ["file_id", "destination_folder_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__rename_file",
        description="Rename a file in Google Drive.",
        handler=_h(google__rename_file),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["file_id", "new_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__copy_file",
        description="Copy a file in Google Drive.",
        handler=_h(google__copy_file),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "name": {"type": "string", "description": "Name for the copy", "default": ""},
                "parent_folder_id": {"type": "string", "default": ""},
            },
            "required": ["file_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__trash_file",
        description="Move a Drive file to Trash.",
        handler=_h(google__trash_file),
        parameters={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__share_file",
        description="Share a Drive file with someone by email, or enable link sharing.",
        handler=_h(google__share_file),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "email": {"type": "string", "description": "Email to share with", "default": ""},
                "role": {"type": "string", "enum": ["reader", "commenter", "writer", "owner"], "default": "reader"},
                "link_sharing": {"type": "boolean", "description": "Enable anyone-with-link access", "default": False},
            },
            "required": ["file_id"],
        },
        require_approval=True,
    )
    return 15

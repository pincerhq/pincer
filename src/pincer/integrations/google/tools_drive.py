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

# MIME type mapping for file_type parameter in google__search_drive_files
_FILE_TYPE_MIMES: dict[str, str | None] = {
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "document": "application/vnd.google-apps.document",
    "doc": "application/vnd.google-apps.document",
    "presentation": "application/vnd.google-apps.presentation",
    "slides": "application/vnd.google-apps.presentation",
    "folder": "application/vnd.google-apps.folder",
    "pdf": "application/pdf",
    "image": None,  # handled specially
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _build_drive_query(
    name: str = "",
    file_type: str = "any",
    shared_with_me: bool = False,
    starred: bool = False,
    drive_query: str = "",
    legacy_query: str = "",
) -> str:
    """Build a Drive API q parameter from friendly parameters.

    NEVER splits on whitespace. NEVER inserts 'and' between arbitrary words.
    Only joins parts that were individually constructed as complete Drive clauses.
    """
    # Advanced mode: raw query passed directly (zero processing)
    if drive_query:
        return drive_query

    # Legacy compatibility: detect whether the old 'query' param contains
    # structured Drive API syntax or is just a bare search string.
    if legacy_query:
        # Drive API query syntax markers: operators used in predicates
        has_contains = "contains" in legacy_query
        has_equals = "=" in legacy_query  # covers both name='x' and name = 'x'
        has_compare = "<" in legacy_query or ">" in legacy_query
        has_has = " has " in legacy_query
        if has_contains or has_equals or has_compare or has_has:
            # Looks like real Drive API syntax — pass through untouched, DO NOT SPLIT
            return legacy_query
        else:
            # Bare string like "GOOGLE_WORKSPACE_QA_CHECKLIST" — treat as name search
            name = legacy_query

    # Build query from friendly parameters
    parts: list[str] = []

    if name:
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"name contains '{safe_name}'")

    file_type_lower = file_type.lower().strip() if file_type else "any"
    if file_type_lower and file_type_lower != "any":
        if file_type_lower == "image":
            parts.append("mimeType contains 'image/'")
        elif file_type_lower in _FILE_TYPE_MIMES:
            mime = _FILE_TYPE_MIMES[file_type_lower]
            if mime:
                parts.append(f"mimeType = '{mime}'")

    if shared_with_me:
        parts.append("sharedWithMe = true")

    if starred:
        parts.append("starred = true")

    # Always exclude trash
    parts.append("trashed = false")

    return " and ".join(parts)


def _mime_to_label(mime: str) -> str:
    """Return a human-readable file type label for a MIME type."""
    labels = {
        "application/vnd.google-apps.spreadsheet": "Google Sheet",
        "application/vnd.google-apps.document": "Google Doc",
        "application/vnd.google-apps.presentation": "Google Slides",
        "application/vnd.google-apps.folder": "Folder",
        "application/pdf": "PDF",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel (.xlsx)",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (.docx)",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint (.pptx)",
        "text/csv": "CSV",
        "text/plain": "Text",
    }
    return labels.get(mime, mime.split("/")[-1] if "/" in mime else mime)


# ── File discovery helpers ─────────────────────────────────────────────────────


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    kb = size_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    return f"{kb / 1024:.1f} MB"


def _resolve_upload_path(file_path: str) -> str | None:
    """
    Resolve a file path for upload. Search order:

    1. Exact path (after ~ expansion)
    2. ~/.pincer/downloads/{basename}
    3. ~/.pincer/workspace/{basename}
    4. ~/.pincer/workspace/exec_output/{basename}
    5. ~/Desktop/{basename}
    6. ~/.pincer/{file_path} (relative to pincer home)
    7. Deep walk of ~/.pincer/downloads and ~/.pincer/workspace

    Returns the resolved absolute path, or None if not found.
    """
    expanded = os.path.expanduser(file_path)

    # 1. Exact path
    if os.path.isfile(expanded):
        return os.path.abspath(expanded)

    basename = os.path.basename(file_path)
    pincer_dir = os.path.expanduser("~/.pincer")

    # 2–6. Check known locations
    candidates = [
        os.path.join(pincer_dir, "downloads", basename),
        os.path.join(pincer_dir, "workspace", basename),
        os.path.join(pincer_dir, "workspace", "exec_output", basename),
        os.path.expanduser(f"~/Desktop/{basename}"),
        os.path.join(pincer_dir, file_path),
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    # 7. Deep walk of known directories
    for search_dir in [
        os.path.join(pincer_dir, "downloads"),
        os.path.join(pincer_dir, "workspace"),
    ]:
        if not os.path.isdir(search_dir):
            continue
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if basename in files:
                return os.path.abspath(os.path.join(root, basename))

    return None


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
    name: str = "",
    file_type: str = "any",
    shared_with_me: bool = False,
    starred: bool = False,
    drive_query: str = "",
    query: str = "",
    max_results: int = 20,
    page_token: str = "",
) -> str:
    """Search Drive files using friendly parameters or raw Drive API query syntax."""
    q = _build_drive_query(
        name=name,
        file_type=file_type,
        shared_with_me=shared_with_me,
        starred=starred,
        drive_query=drive_query,
        legacy_query=query,
    )
    svc = await factory.get("drive")
    kwargs: dict[str, Any] = {
        "q": q,
        "fields": f"nextPageToken, files({_DRIVE_FIELDS})",
        "pageSize": max_results,
        "orderBy": "modifiedTime desc",
    }
    if page_token:
        kwargs["pageToken"] = page_token
    try:
        result = await with_backoff(lambda: svc.files().list(**kwargs).execute())
    except Exception as e:
        error_msg = str(e)
        if "Invalid Value" in error_msg or "Invalid query" in error_msg or "400" in error_msg:
            return (
                f'Drive search failed. Query sent to API: q="{q}"\n\n'
                f"Use the simple 'name' parameter to avoid query syntax issues:\n"
                f'  google__search_drive_files(name="budget")\n'
                f'  google__search_drive_files(name="report", file_type="spreadsheet")\n'
                f'  google__search_drive_files(name="proposal", file_type="document")'
            )
        return f"Drive search error: {error_msg}"
    files = result.get("files", [])
    search_term = name or drive_query or query
    if not files:
        return f"No files found matching '{search_term}'."

    output = f"Found {len(files)} file(s):\n\n"
    for f in files:
        mime = f.get("mimeType", "")
        type_label = _mime_to_label(mime)
        size = f.get("size", "")
        size_str = f" ({_format_size(int(size))})" if size else ""
        output += f"  {f['name']}{size_str}\n   Type: {type_label}\n   ID: {f['id']}\n"
        # Next-step hint based on file type
        if mime == "application/vnd.google-apps.spreadsheet":
            output += f'   → Use: google__get_sheet_values(spreadsheet_id="{f["id"]}")\n'
        elif mime == "application/vnd.google-apps.document":
            output += f'   → Use: google__get_doc_content(file_id="{f["id"]}")\n'
        elif mime.startswith("application/vnd.google-apps."):
            output += f'   → Use: google__export_google_doc(file_id="{f["id"]}")\n'
        else:
            output += f'   → Use: google__download_file(file_id="{f["id"]}")\n'
        output += "\n"

    if result.get("nextPageToken"):
        output += "(More results available — use page_token to get the next page)\n"
    return output


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
    metadata = await with_backoff(lambda: svc.files().get(fileId=file_id, fields="id,name,mimeType,size").execute())
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
    metadata = await with_backoff(lambda: svc.files().get(fileId=file_id, fields="id,name,mimeType").execute())
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
    file_bytes = await with_backoff(lambda: svc.files().export_media(fileId=file_id, mimeType=export_mime).execute())

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


async def google__list_local_files(
    factory: GoogleServiceFactory,
    pattern: str = "",
) -> str:
    """List local files in Pincer's download and workspace directories available for upload."""
    pincer_dir = os.path.expanduser("~/.pincer")
    search_dirs = [
        os.path.join(pincer_dir, "downloads"),
        os.path.join(pincer_dir, "workspace"),
    ]

    found: list[str] = []

    for dir_path in search_dirs:
        if not os.path.isdir(dir_path):
            continue
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                if f.startswith("."):
                    continue
                if pattern and pattern.lower() not in f.lower():
                    continue
                full_path = os.path.join(root, f)
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0
                rel = os.path.relpath(full_path, pincer_dir)
                found.append(f"  {f} ({_format_size(size)}) → ~/.pincer/{rel}")

    if not found:
        if pattern:
            return (
                f"No files matching '{pattern}' found in downloads or workspace.\n"
                f"Ask the user for the exact file path, or use file_list to search the workspace."
            )
        return "No files found in ~/.pincer/downloads/ or ~/.pincer/workspace/."

    result = f"Found {len(found)} file(s):\n\n"
    result += "\n".join(found)
    result += '\n\nTo upload one of these, call:\ngoogle__upload_file(file_path="~/.pincer/<path from above>")'
    return result


async def google__upload_file(
    factory: GoogleServiceFactory,
    file_path: str,
    folder_id: str = "",
    name: str = "",
) -> str:
    """Upload a local file to Google Drive with automatic path resolution."""
    from googleapiclient.http import MediaFileUpload

    # 1. Resolve the file path across known Pincer directories
    resolved = _resolve_upload_path(file_path)

    if not resolved:
        basename = os.path.basename(file_path)
        return (
            f"File not found: '{file_path}'\n\n"
            f"Searched in:\n"
            f"  - Exact path: {file_path}\n"
            f"  - Downloads: ~/.pincer/downloads/{basename}\n"
            f"  - Workspace: ~/.pincer/workspace/{basename}\n\n"
            f"Use google__list_local_files() to see available files, "
            f"or ask the user for the correct path."
        )

    # 2. Detect MIME type
    mime, _ = mimetypes.guess_type(resolved)
    mime = mime or "application/octet-stream"

    # 3. Build upload request
    file_name = name or os.path.basename(resolved)
    file_size = os.path.getsize(resolved)
    size_str = _format_size(file_size)

    svc = await factory.get("drive")
    metadata: dict[str, Any] = {"name": file_name}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(resolved, mimetype=mime, resumable=True)

    # 4. Upload
    uploaded = await asyncio.to_thread(
        lambda: svc.files().create(body=metadata, media_body=media, fields="id,name,mimeType,webViewLink").execute()
    )

    # 5. Return metadata only — never raw content
    web_link = uploaded.get("webViewLink", "")
    link_line = f"  Link: {web_link}\n" if web_link else ""

    return (
        f"File uploaded to Google Drive.\n"
        f"  Filename: {uploaded.get('name', file_name)}\n"
        f"  Size: {size_str}\n"
        f"  Type: {mime}\n"
        f"  Drive ID: {uploaded.get('id', 'unknown')}\n"
        f"{link_line}"
    )


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
    """Register all 16 Drive tools. Returns count."""

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
            "Search for files and folders in Google Drive.\n\n"
            "Simple search (recommended): pass 'name' to find files by name.\n"
            "  Example: name='budget'  → finds files with 'budget' in the name\n"
            "  Example: name='QA checklist', file_type='spreadsheet'  → only Google Sheets\n\n"
            "Optional filters:\n"
            "  file_type: 'spreadsheet', 'document', 'presentation', 'pdf', "
            "'folder', 'image', 'xlsx', 'any' (default: 'any')\n"
            "  shared_with_me: True to show only shared files\n"
            "  starred: True to show only starred files\n\n"
            "Results include file type labels and next-step tool hints:\n"
            "  Google Sheet → use google__get_sheet_values(spreadsheet_id='...')\n"
            "  Google Doc → use google__get_doc_content(file_id='...')\n"
            "  .xlsx / PDF / other → use google__download_file(file_id='...')\n\n"
            "For advanced Drive API queries, use 'drive_query' with raw syntax "
            "(e.g., drive_query=\"modifiedTime > '2026-01-01'\").\n"
            "NEVER construct raw Drive query syntax via 'query' — use 'name' instead."
        ),
        handler=_h(google__search_drive_files),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Search by filename (substring match). Recommended for most searches.",
                    "default": "",
                },
                "file_type": {
                    "type": "string",
                    "description": (
                        "Filter by type: 'spreadsheet', 'document', 'presentation',"
                        " 'pdf', 'folder', 'image', 'xlsx', 'any'"
                    ),
                    "default": "any",
                },
                "shared_with_me": {
                    "type": "boolean",
                    "description": "Only return files shared with me",
                    "default": False,
                },
                "starred": {
                    "type": "boolean",
                    "description": "Only return starred files",
                    "default": False,
                },
                "drive_query": {
                    "type": "string",
                    "description": "Raw Drive API query string (advanced — passed through unchanged)",
                    "default": "",
                },
                "query": {
                    "type": "string",
                    "description": "Legacy: bare filename or raw Drive API query (use 'name' instead)",
                    "default": "",
                },
                "max_results": {"type": "integer", "default": 20},
                "page_token": {"type": "string", "default": ""},
            },
            "required": [],
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
                    "enum": [
                        "pdf",
                        "docx",
                        "xlsx",
                        "csv",
                        "pptx",
                        "txt",
                        "html",
                        "odt",
                        "rtf",
                        "epub",
                        "ods",
                        "tsv",
                        "odp",
                        "png",
                        "jpg",
                        "svg",
                    ],
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
        name="google__list_local_files",
        description=(
            "List files available for upload from Pincer's local directories. "
            "Searches ~/.pincer/downloads/ (files from google__download_file, "
            "google__export_google_doc, google__get_attachment) and "
            "~/.pincer/workspace/ (files from file_write or python_exec). "
            "Use this BEFORE google__upload_file when you don't know the file's exact path. "
            "Optionally filter by filename pattern."
        ),
        handler=_h(google__list_local_files),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional filename substring filter (case-insensitive)",
                    "default": "",
                },
            },
            "required": [],
        },
    )
    registry.register(
        name="google__upload_file",
        description=(
            "Upload a local file to Google Drive. "
            "Accepts paths relative to ~/.pincer/ (e.g., 'downloads/report.pdf'), "
            "bare filenames (e.g., 'report.pdf' — searched in downloads/ and workspace/), "
            "or absolute paths (e.g., '/Users/user/Documents/file.pdf'). "
            "Files from google__download_file are in ~/.pincer/downloads/. "
            "Files from file_write or python_exec are in ~/.pincer/workspace/. "
            "If you don't know the path, call google__list_local_files first. "
            "Optionally specify folder_id to upload into a specific Drive folder. "
            "MIME type is auto-detected from the file extension."
        ),
        handler=_h(google__upload_file),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Local file path. Accepts: bare filename ('report.pdf'), "
                        "path relative to ~/.pincer/ ('downloads/report.pdf'), "
                        "or absolute path ('/tmp/data.csv')"
                    ),
                },
                "folder_id": {
                    "type": "string",
                    "description": "Destination Drive folder ID (default: root)",
                    "default": "",
                },
                "name": {
                    "type": "string",
                    "description": "Override filename on Drive (default: same as local)",
                    "default": "",
                },
            },
            "required": ["file_path"],
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
    return 16

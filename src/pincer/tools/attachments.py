"""Outgoing email attachments: path allowlist, MIME typing, size limits.

Used by both the SMTP ``email_send`` tool and the Gmail send/reply/draft
tools. Attachments may only come from Pincer's own file areas
(``~/.pincer/downloads`` and ``~/.pincer/workspace``). Those are where Drive
exports, Gmail attachment downloads, ``file_write`` and ``python_exec`` put
their output. Anything that resolves outside them is rejected, including
``../`` traversal and symlinks that point elsewhere.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pincer.utils.csv_list import split_csv

# Raw bytes. Base64 inflates by ~4/3, so 18 MB of files stays under Gmail's 25 MB message cap.
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024
MAX_ATTACHMENTS = 10


class AttachmentError(ValueError):
    """An attachment was rejected (outside the allowlist, missing, or too large)."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    maintype: str
    subtype: str
    data: bytes


def allowed_attachment_dirs() -> list[Path]:
    """Directories attachments may be read from. Computed at call time so ``HOME`` overrides apply."""
    pincer_dir = Path.home() / ".pincer"
    return [pincer_dir / "downloads", pincer_dir / "workspace"]


def _within_allowed(path: Path, allowed: list[Path]) -> bool:
    return any(path.is_relative_to(d.resolve()) for d in allowed)


def _not_found(requested: str, allowed: list[Path]) -> AttachmentError:
    dirs = ", ".join(str(d) for d in allowed)
    return AttachmentError(
        f"Attachment not found: '{requested}'. Files can only be attached from {dirs}. "
        "Download or export the file first (e.g. google__export_google_doc), then pass the returned path."
    )


def _outside(requested: str, allowed: list[Path]) -> AttachmentError:
    dirs = ", ".join(str(d) for d in allowed)
    return AttachmentError(
        f"Attachment '{requested}' is outside the allowed folders ({dirs}). "
        "Only downloaded, exported, or workspace files can be attached."
    )


def resolve_attachment_path(requested: str) -> Path:
    """Resolve *requested* to a real file inside the attachment allowlist.

    Absolute paths (after ``~`` expansion) must already sit inside an allowed
    directory. Relative paths and bare filenames are tried against each allowed
    directory, then found by a basename search of those directories.

    Raises AttachmentError if the file is missing or resolves outside the allowlist.
    """
    allowed = allowed_attachment_dirs()
    requested = requested.strip()
    if not requested:
        raise AttachmentError("Attachment path is empty.")
    expanded = Path(os.path.expanduser(requested))

    candidates: list[Path] = []
    if expanded.is_absolute():
        candidates.append(expanded)
    else:
        candidates.extend(d / expanded for d in allowed)
        candidates.append(Path.home() / ".pincer" / expanded)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not _within_allowed(resolved, allowed):
            raise _outside(requested, allowed)
        return resolved

    if expanded.is_absolute():
        # Don't silently swap an absolute path for a same-named file elsewhere.
        if expanded.exists() and not _within_allowed(expanded.resolve(), allowed):
            raise _outside(requested, allowed)
        raise _not_found(requested, allowed)

    basename = expanded.name
    for search_dir in allowed:
        if not search_dir.is_dir():
            continue
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if basename in files:
                resolved = (Path(root) / basename).resolve()
                if resolved.is_file() and _within_allowed(resolved, allowed):
                    return resolved

    raise _not_found(requested, allowed)


def normalize_attachment_args(paths: list[str] | str | None) -> list[str]:
    """Accept a list, a comma-separated string (LLMs sometimes send one), or nothing."""
    if not paths:
        return []
    if isinstance(paths, str):
        return split_csv(paths)
    return [str(p).strip() for p in paths if str(p).strip()]


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def load_attachments(paths: list[str] | str | None) -> list[Attachment]:
    """Resolve, read and MIME-type the requested attachments.

    Raises AttachmentError with a message suitable to return to the model.
    """
    requested = normalize_attachment_args(paths)
    if not requested:
        return []

    resolved: list[Path] = []
    for p in requested:
        path = resolve_attachment_path(p)
        if path not in resolved:
            resolved.append(path)

    if len(resolved) > MAX_ATTACHMENTS:
        raise AttachmentError(f"Too many attachments ({len(resolved)}); the limit is {MAX_ATTACHMENTS} per email.")

    total = sum(p.stat().st_size for p in resolved)
    if total > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"Attachments total {_mb(total)}; limit is {_mb(MAX_ATTACHMENT_BYTES)}. "
            "Email can't carry files this large. Share a Drive link instead "
            "(e.g. google__share_file, then include the link in the body)."
        )

    attachments: list[Attachment] = []
    for path in resolved:
        mime, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        attachments.append(Attachment(filename=path.name, maintype=maintype, subtype=subtype, data=path.read_bytes()))
    return attachments


def attachment_names(attachments: list[Attachment]) -> str:
    """Suffix for success messages, e.g. `` (attachments: a.pdf, b.xlsx)``. Empty when there are none."""
    if not attachments:
        return ""
    return f" (attachments: {', '.join(a.filename for a in attachments)})"


def approval_display_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments as shown on an approval prompt: ``attachments`` becomes a list of file names.

    Every channel renders values its own way (``k=v`` text, Slack blocks, web JSON),
    so a list of full paths shows up as a Python ``repr``. A comma-joined string of
    basenames reads well everywhere. The tool itself still gets the original arguments.
    """
    raw = arguments.get("attachments")
    if not raw:
        return arguments
    names = [os.path.basename(os.path.expanduser(p)) or p for p in normalize_attachment_args(raw)]
    return {**arguments, "attachments": ", ".join(names)}

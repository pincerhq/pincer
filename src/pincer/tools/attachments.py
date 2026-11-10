"""Outgoing email attachments: path allowlist, MIME typing, size limits.

Used by both the SMTP ``email_send`` tool and the Gmail send/reply/draft
tools. Attachments may only come from Pincer's own file areas: the
``downloads`` and ``workspace`` folders under ``~/.pincer`` and under the
configured ``data_dir`` (they differ in Docker, where ``PINCER_DATA_DIR`` is
``/app/data``). Drive exports, Gmail attachment downloads, ``python_exec``,
``file_write`` and chat uploads all save there. Anything that resolves outside
them is rejected, including ``../`` traversal and symlinks that point elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pincer.utils.csv_list import split_csv

# Raw bytes. Base64 adds ~4/3 plus line breaks and headers, so 17 MB of files
# stays under Gmail's 25 MB message cap (18 MiB would reach ~25.8 MB).
MAX_ATTACHMENT_BYTES = 17_000_000
MAX_ATTACHMENTS = 10

# Tools whose ``attachments`` argument holds local file paths. Only these get
# the path-friendly rendering on approval prompts; other tools (MCP-bridged
# Slack, etc.) may use ``attachments`` for something else entirely.
ATTACHMENT_TOOLS = frozenset(
    {
        "email_send",
        "google__send_message",
        "google__reply_to_message",
        "google__reply_all",
        "google__forward_message",
        "google__create_draft",
    }
)


class AttachmentError(ValueError):
    """An attachment was rejected (outside the allowlist, missing, ambiguous, or too large)."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    maintype: str
    subtype: str
    data: bytes


def _configured_data_dir() -> Path | None:
    try:
        from pincer.config import get_settings_relaxed

        data_dir = get_settings_relaxed().data_dir
    except Exception:
        return None
    return data_dir if isinstance(data_dir, Path) else None


def _pincer_roots() -> list[Path]:
    """``~/.pincer`` and the configured ``data_dir`` (deduped, in that order)."""
    roots = [Path.home() / ".pincer"]
    data_dir = _configured_data_dir()
    if data_dir is not None and data_dir.expanduser().resolve() != roots[0].resolve():
        roots.append(data_dir.expanduser())
    return roots


def allowed_attachment_dirs() -> list[Path]:
    """Directories attachments may be read from. Computed per call so ``HOME``/settings changes apply."""
    return [root / sub for root in _pincer_roots() for sub in ("downloads", "workspace")]


def _within_allowed(path: Path, allowed: list[Path]) -> bool:
    return any(path.is_relative_to(d.resolve()) for d in allowed)


def _dirs_text(allowed: list[Path]) -> str:
    return ", ".join(str(d) for d in allowed)


def _not_found(requested: str, allowed: list[Path]) -> AttachmentError:
    return AttachmentError(
        f"Attachment not found: '{requested}'. Files can only be attached from {_dirs_text(allowed)}. "
        "Download or export the file first (e.g. google__export_google_doc), then pass the returned path."
    )


def _outside(requested: str, allowed: list[Path]) -> AttachmentError:
    return AttachmentError(
        f"Attachment '{requested}' is outside the allowed folders ({_dirs_text(allowed)}). "
        "Only downloaded, exported, uploaded, or workspace files can be attached."
    )


def _allowed_file(candidate: Path, allowed: list[Path]) -> Path | None:
    """The resolved path if *candidate* is a file inside the allowlist, else None."""
    if not candidate.is_file():
        return None
    resolved = candidate.resolve()
    return resolved if _within_allowed(resolved, allowed) else None


def resolve_attachment_path(requested: str) -> Path:
    """Resolve *requested* to a real file inside the attachment allowlist.

    Absolute paths (after ``~`` expansion) must already sit inside an allowed
    directory. Relative paths and bare filenames are tried against each allowed
    directory and each Pincer root (so ``downloads/a.pdf`` works), plus a
    basename search of the allowed directories. Candidates outside the
    allowlist are skipped, never reported. If more than one allowed file
    matches, the call fails and asks for the full path, so a bare name can
    never pick someone else's same-named upload.

    Raises AttachmentError if the file is missing, ambiguous, or outside the allowlist.
    """
    allowed = allowed_attachment_dirs()
    requested = requested.strip()
    if not requested:
        raise AttachmentError("Attachment path is empty.")
    expanded = Path(os.path.expanduser(requested))

    if expanded.is_absolute():
        found = _allowed_file(expanded, allowed)
        if found is not None:
            return found
        # Don't swap an absolute path for a same-named file elsewhere.
        if expanded.exists():
            raise _outside(requested, allowed)
        raise _not_found(requested, allowed)

    if ".." in expanded.parts:
        raise _outside(requested, allowed)

    matches: list[Path] = []

    def _add(path: Path | None) -> None:
        if path is not None and path not in matches:
            matches.append(path)

    for base in [*allowed, *_pincer_roots()]:
        _add(_allowed_file(base / expanded, allowed))

    basename = expanded.name
    for search_dir in allowed:
        if not search_dir.is_dir():
            continue
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if basename in files:
                _add(_allowed_file(Path(root) / basename, allowed))

    if not matches:
        raise _not_found(requested, allowed)
    if len(matches) > 1:
        listed = "\n".join(f"  - {m}" for m in matches)
        raise AttachmentError(
            f"Attachment '{requested}' matches {len(matches)} files:\n{listed}\nPass the full path of the one to send."
        )
    return matches[0]


def normalize_attachment_args(paths: list[str] | str | None) -> list[str]:
    """Accept a list, a comma-separated string (LLMs sometimes send one), or nothing."""
    if not paths:
        return []
    if isinstance(paths, str):
        return split_csv(paths)
    return [str(p).strip() for p in paths if str(p).strip()]


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _too_large(total: int) -> AttachmentError:
    return AttachmentError(
        f"Attachments total {_mb(total)}; limit is {_mb(MAX_ATTACHMENT_BYTES)}. "
        "Email can't carry files this large. Share a Drive link instead "
        "(e.g. google__share_file, then include the link in the body)."
    )


def _read_capped(path: Path, budget: int) -> bytes:
    """Read *path* without following a swapped-in symlink, at most ``budget + 1`` bytes."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        return f.read(budget + 1)


def _mime_type(filename: str) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(filename)
    maintype, _, subtype = (mime or "application/octet-stream").partition("/")
    # Only message/rfc822 can be embedded properly; other message/* types
    # (partial, external-body) can't be base64-encoded, so send them as bytes.
    if maintype == "message" and subtype != "rfc822":
        return "application", "octet-stream"
    return maintype, subtype


def load_attachments(paths: list[str] | str | None) -> list[Attachment]:
    """Resolve, read and MIME-type the requested attachments.

    The size limit is enforced on the bytes actually read, so a file that grows
    after it was found can't get past it. Raises AttachmentError with a message
    suitable to return to the model.
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

    stat_total = 0
    for path in resolved:
        with contextlib.suppress(OSError):
            stat_total += path.stat().st_size
    if stat_total > MAX_ATTACHMENT_BYTES:
        raise _too_large(stat_total)

    attachments: list[Attachment] = []
    total = 0
    for path in resolved:
        try:
            data = _read_capped(path, MAX_ATTACHMENT_BYTES - total)
        except OSError as e:
            raise AttachmentError(f"Could not read attachment '{path.name}': {e.strerror or e}") from e
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise _too_large(total)
        maintype, subtype = _mime_type(path.name)
        attachments.append(Attachment(filename=path.name, maintype=maintype, subtype=subtype, data=data))
    return attachments


async def load_attachments_async(paths: list[str] | str | None) -> list[Attachment]:
    """:func:`load_attachments` off the event loop (it walks folders and reads up to 17 MB)."""
    if not normalize_attachment_args(paths):
        return []
    return await asyncio.to_thread(load_attachments, paths)


def attachment_names(attachments: list[Attachment]) -> str:
    """Suffix for success messages, e.g. `` (attachments: a.pdf, b.xlsx)``. Empty when there are none."""
    if not attachments:
        return ""
    return f" (attachments: {', '.join(a.filename for a in attachments)})"


def _display_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def approval_display_args(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments as shown on an approval prompt.

    For the email tools, ``attachments`` becomes one comma-separated string,
    since channels render a list as a Python ``repr``. Each path is shown as
    given, with the home folder written as ``~``, so the approver can see which
    folder a file comes from. Other tools' arguments are returned unchanged.
    The tool itself always gets the original arguments.
    """
    raw = arguments.get("attachments")
    if tool_name not in ATTACHMENT_TOOLS or not raw or not isinstance(raw, (str, list)):
        return arguments
    return {**arguments, "attachments": ", ".join(_display_path(p) for p in normalize_attachment_args(raw))}

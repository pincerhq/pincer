"""Outgoing email attachments: workspace allowlist, MIME parts, size limits, approval display.

Used by both the SMTP ``email_send`` tool and the Gmail send/reply/draft
tools. Attachments may only come from Pincer's workspace,
``<storage root>/workspace`` (see :mod:`pincer.storage.workspace`): chat
uploads (``uploads/``), Drive exports and Gmail downloads (``downloads/``),
``python_exec`` output (``exec_output/``), screenshots and ``file_write``
files all live there. Anything that resolves outside it is rejected, including
``../`` traversal and symlinks that point elsewhere. For a cloud storage root
the bucket is the source of truth: a file is checked and sized there, and only
fetched into the local cache when it is about to be sent.

Trust model: the workspace is shared by the whole Pincer instance, not split
per user. Chat uploads from every channel and identity land in the same
``uploads/`` folder, and ``file_write``/``python_exec`` share it too. The
allowlist keeps attachments inside Pincer's own files; it does not separate
users. That is why the approval prompt shows the resolved path and size of
every file before anything is sent.
"""

from __future__ import annotations

import asyncio
import email
import json
import logging
import mimetypes
import os
import stat
import sys
from dataclasses import dataclass
from email import encoders
from email.mime.base import MIMEBase
from email.mime.message import MIMEMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.utils.csv_list import split_csv

if TYPE_CHECKING:
    from email.message import Message

    from pincer.storage.workspace import Workspace

logger = logging.getLogger(__name__)


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


# Raw bytes. Base64 adds ~4/3 plus line breaks and headers, so 17 MB of files
# stays under Gmail's 25 MB message cap (18 MiB would reach ~25.8 MB).
MAX_ATTACHMENT_BYTES = 17_000_000
MAX_ATTACHMENTS = 10
# Appended to the description of every attachment-capable send tool.
ATTACHMENTS_HINT = (
    " To attach files, pass their paths in `attachments`. To send a Google Doc, first call "
    "google__export_google_doc (e.g. format pdf), then pass the returned path. Files must be in "
    "Pincer's workspace (downloads/ for Drive exports and Gmail downloads, uploads/ for chat "
    "uploads, exec_output/ for python_exec output, or anything file_write saved); "
    f"total size up to {_mb(MAX_ATTACHMENT_BYTES)}."
)
# Approval prompts list at most this many files (then "+N more") and cut each path to
# this many characters, so the recipient and the file count fit the channels' 200-char preview.
_DISPLAY_MAX_FILES = 3
_DISPLAY_MAX_PATH = 60
# Shown ahead of the attachments on an approval prompt, so a long file list can't hide the recipient.
_DISPLAY_HEAD_KEYS = ("to", "cc", "bcc", "subject")


class AttachmentError(ValueError):
    """An attachment was rejected (outside the workspace, missing, ambiguous, or too large).

    ``str(e)`` is the full message for the model; ``reason`` is a short,
    single-line form for approval prompts.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason or message.split("\n", 1)[0]


@dataclass(frozen=True)
class Attachment:
    filename: str
    maintype: str
    subtype: str
    data: bytes


def attachments_param() -> dict[str, Any]:
    """JSON schema for the ``attachments`` parameter (a fresh dict per tool)."""
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Local file paths to attach, e.g. the 'Saved to' path from google__export_google_doc, "
            "google__download_file or google__get_attachment."
        ),
        "default": [],
    }


# ── The workspace ────────────────────────────────────────────────────────────


def _workspace() -> Workspace:
    """Pincer's workspace. Raises AttachmentError when the storage root can't be reached."""
    from pincer.config import get_settings
    from pincer.storage.errors import StorageUnavailable
    from pincer.storage.workspace import get_workspace

    try:
        return get_workspace(get_settings())
    except StorageUnavailable as e:
        raise AttachmentError(
            f"The workspace is unavailable, so nothing can be attached: {e}", "workspace unavailable"
        ) from e


def _workspace_text(ws: Workspace) -> str:
    return f"the workspace ({ws.local})"


def _not_found(requested: str, ws: Workspace) -> AttachmentError:
    return AttachmentError(
        f"Attachment not found: '{requested}'. Files can only be attached from {_workspace_text(ws)}. "
        "Download or export the file first (e.g. google__export_google_doc), then pass the returned path.",
        "not found",
    )


def _outside(requested: str, ws: Workspace) -> AttachmentError:
    return AttachmentError(
        f"Attachment '{requested}' is outside {_workspace_text(ws)}. "
        "Only downloaded, exported, uploaded, or workspace files can be attached.",
        "outside the workspace",
    )


def _ambiguous(requested: str, matches: list[str]) -> AttachmentError:
    listed = "\n".join(f"  - {m}" for m in matches)
    return AttachmentError(
        f"Attachment '{requested}' matches {len(matches)} files in the workspace:\n{listed}\n"
        "Pass the path of the one to send (e.g. downloads/report.pdf).",
        f"matches {len(matches)} files",
    )


def _file_in_workspace(ws: Workspace, candidate: str | Path) -> str | None:
    """The workspace-relative path of *candidate* if it is a file in the workspace, else None.

    ``candidate`` is absolute or relative to the workspace. Symlinks are
    resolved first, so one that points outside the workspace is not a match.
    """
    try:
        rel = ws.relative(candidate)
    except ValueError:  # SandboxDenied: escapes the workspace
        return None
    return rel if rel and ws.is_file(rel) else None


def resolve_attachment(requested: str) -> str:
    """Resolve *requested* to a file in the workspace and return its workspace-relative path.

    * Absolute paths (after ``~`` expansion) must point into the workspace.
    * Relative paths with a folder (``downloads/report.pdf``) are looked up
      relative to the workspace. A direct hit wins; the basename search only
      runs when there is none.
    * Bare filenames are searched for across the whole workspace. If more than
      one file has that name, the call fails and asks for the path.

    Candidates outside the workspace (symlinks pointing out) are skipped, never
    reported. Raises AttachmentError if the file is missing, ambiguous, or
    outside the workspace.
    """
    requested = requested.strip()
    if not requested:
        raise AttachmentError("Attachment path is empty.", "empty path")
    ws = _workspace()
    expanded = Path(os.path.expanduser(requested))

    if expanded.is_absolute():
        found = _file_in_workspace(ws, expanded)
        if found is not None:
            return found
        # Don't swap an absolute path for a same-named file in the workspace.
        if ws.contains(expanded):
            raise _not_found(requested, ws)
        raise _outside(requested, ws)

    if ".." in expanded.parts:
        raise _outside(requested, ws)

    if len(expanded.parts) > 1:
        found = _file_in_workspace(ws, expanded.as_posix())
        if found is not None:
            return found

    matches: list[str] = []
    for rel, _local in ws.files():
        if rel.rsplit("/", 1)[-1] == expanded.name:
            found = _file_in_workspace(ws, rel)
            if found is not None and found not in matches:
                matches.append(found)
    if not matches:
        raise _not_found(requested, ws)
    if len(matches) > 1:
        raise _ambiguous(requested, matches)
    return matches[0]


# ── Loading ──────────────────────────────────────────────────────────────────


def _bad_argument(value: object) -> AttachmentError:
    return AttachmentError(
        f"`attachments` must be a list of file paths, got {type(value).__name__}: {value!r:.100}",
        "not a list of file paths",
    )


def normalize_attachment_args(paths: object) -> list[str]:
    """The requested paths as a list of non-empty strings.

    Accepts a list of strings, nothing, or a string: a JSON array
    (``'["a.pdf", "b.pdf"]'``) or a comma-separated list, since LLMs sometimes
    send either. Anything else raises AttachmentError.
    """
    if paths is None:
        return []
    if isinstance(paths, str):
        text = paths.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None  # e.g. "[draft] report.pdf": a filename, not JSON
            if isinstance(parsed, list):
                return normalize_attachment_args(parsed)
        return split_csv(text)
    if isinstance(paths, list):
        if not all(isinstance(p, str) for p in paths):
            raise _bad_argument(paths)
        return [p.strip() for p in paths if p.strip()]
    raise _bad_argument(paths)


def _too_large(total: int) -> AttachmentError:
    return AttachmentError(
        f"Attachments total {_mb(total)}; limit is {_mb(MAX_ATTACHMENT_BYTES)}. "
        "Email can't carry files this large. Share a Drive link instead "
        "(e.g. google__share_file, then include the link in the body).",
        f"over {_mb(MAX_ATTACHMENT_BYTES)} in total",
    )


def _fd_real_path(fd: int) -> Path | None:
    """The path the kernel has for an open fd, where the platform can tell us."""
    if sys.platform == "darwin":
        import fcntl

        buf = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return Path(buf.split(b"\0", 1)[0].decode())
    proc = f"/proc/self/fd/{fd}"
    if os.path.exists(proc):
        return Path(os.readlink(proc))
    return None


def _read_verified(path: Path, budget: int, base: Path) -> bytes:
    """Read at most ``budget + 1`` bytes of *path*, checking what was actually opened.

    ``O_NOFOLLOW`` refuses a symlink in the last component and ``O_NONBLOCK``
    stops a FIFO swapped in from hanging the worker. After opening, the fd must
    be a regular file whose real path is still inside *base* (the workspace's
    local directory), which catches an intermediate directory swapped for a
    symlink after it was resolved.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AttachmentError(f"Attachment '{path.name}' is not a regular file.", "not a regular file")
        real = _fd_real_path(fd)
        if real is not None:
            inside = real.is_relative_to(base.resolve())
        else:
            current = os.stat(path)
            inside = path.resolve() == path and (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino)
        if not inside:
            raise AttachmentError(
                f"Attachment '{path.name}' is outside the workspace ({base}).", "outside the workspace"
            )
        with os.fdopen(fd, "rb") as f:
            fd = -1  # owned by f now
            return f.read(budget + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _mime_type(filename: str) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(filename)
    maintype, _, subtype = (mime or "application/octet-stream").partition("/")
    # Only message/rfc822 can be embedded properly; other message/* types
    # (partial, external-body) can't be base64-encoded, so send them as bytes.
    if maintype == "message" and subtype != "rfc822":
        return "application", "octet-stream"
    return maintype, subtype


def _safe_text(text: str) -> str:
    """Replace control characters, which break header serialisation (a newline in a
    filename) and could forge extra lines in an approval prompt."""
    return "".join("_" if ord(c) < 32 or ord(c) == 127 else c for c in text)


def _workspace_size(ws: Workspace, rel: str) -> int:
    """The size of *rel* in the workspace (0 when it is gone). Raises AttachmentError
    when it now points outside the workspace or the bucket can't be asked."""
    try:
        return ws.file_size(rel) or 0
    except ValueError as e:  # SandboxDenied
        raise _outside(rel, ws) from e
    except Exception as e:
        raise AttachmentError(f"Could not check attachment '{rel}': {e}", "could not be read") from e


def load_attachments(paths: object) -> list[Attachment]:
    """Resolve, read and MIME-type the requested attachments.

    The size limit is checked against the workspace's sizes before anything is
    fetched (on a cloud root nothing is downloaded for a request that is too
    large), and again on the bytes actually read, so a file that grows after it
    was found can't get past it. Raises AttachmentError with a message suitable
    to return to the model.
    """
    requested = normalize_attachment_args(paths)
    if not requested:
        return []

    rels: list[str] = []
    for p in requested:
        rel = resolve_attachment(p)
        if rel not in rels:
            rels.append(rel)

    if len(rels) > MAX_ATTACHMENTS:
        raise AttachmentError(
            f"Too many attachments ({len(rels)}); the limit is {MAX_ATTACHMENTS} per email.",
            f"more than {MAX_ATTACHMENTS} files",
        )

    ws = _workspace()
    stat_total = sum(_workspace_size(ws, rel) for rel in rels)
    if stat_total > MAX_ATTACHMENT_BYTES:
        raise _too_large(stat_total)

    attachments: list[Attachment] = []
    total = 0
    for rel in rels:
        try:
            local = ws.fetch(rel, known_file=True)
            data = _read_verified(local, MAX_ATTACHMENT_BYTES - total, ws.local)
        except AttachmentError:
            raise
        except ValueError as e:  # SandboxDenied: swapped for a symlink out of the workspace
            raise _outside(rel, ws) from e
        except OSError as e:
            raise AttachmentError(f"Could not read attachment '{rel}': {e.strerror or e}", "could not be read") from e
        except Exception as e:  # a bucket error while fetching
            raise AttachmentError(f"Could not fetch attachment '{rel}': {e}", "could not be read") from e
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise _too_large(total)
        name = rel.rsplit("/", 1)[-1]
        maintype, subtype = _mime_type(name)
        attachments.append(Attachment(filename=_safe_text(name), maintype=maintype, subtype=subtype, data=data))
    return attachments


async def load_attachments_async(paths: object) -> list[Attachment]:
    """:func:`load_attachments` off the event loop (it may list the bucket and reads up to 17 MB)."""
    if not normalize_attachment_args(paths):
        return []
    return await asyncio.to_thread(load_attachments, paths)


def attachment_part(att: Attachment) -> Message:
    """One MIME part for *att*, with ``Content-Disposition: attachment``.

    ``message/rfc822`` (``.eml``) is embedded as a parsed message, because
    RFC 2046 forbids base64 for that type. Everything else is base64-encoded.
    Shared by the SMTP and Gmail senders.
    """
    part: MIMEBase
    if (att.maintype, att.subtype) == ("message", "rfc822"):
        part = MIMEMessage(email.message_from_bytes(att.data))
    else:
        part = MIMEBase(att.maintype, att.subtype)
        part.set_payload(att.data)
        encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=att.filename)
    return part


def attachment_names(attachments: list[Attachment]) -> str:
    """Suffix for success messages, e.g. `` (attachments: a.pdf, b.xlsx)``. Empty when there are none."""
    if not attachments:
        return ""
    return f" (attachments: {', '.join(a.filename for a in attachments)})"


# ── Approval display ─────────────────────────────────────────────────────────


def _human_size(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.0f} KB"
    return _mb(n)


def _short(text: str) -> str:
    """*text* without control characters, cut to ``_DISPLAY_MAX_PATH`` characters
    (keeping the end, where the filename is)."""
    text = _safe_text(text)
    return text if len(text) <= _DISPLAY_MAX_PATH else "…" + text[-(_DISPLAY_MAX_PATH - 1) :]


def describe_attachments(paths: object) -> str:
    """What will actually be attached: a count and total size, then one line per file.

    Each file shows its workspace path and size; one that won't be attached
    shows a short reason, so the approver isn't misled into approving a file
    that won't be sent. One line per file, because a path can contain ", ".
    At most ``_DISPLAY_MAX_FILES`` lines, then "+N more". All text is cleaned
    of control characters, so a crafted path can't forge extra lines.
    """
    try:
        requested = normalize_attachment_args(paths)
    except AttachmentError as e:
        return f"none will be attached: {e.reason}"
    if not requested:
        return ""
    lines: list[str] = []
    found, total = 0, 0
    for item in requested:
        try:
            rel = resolve_attachment(item)
            size = _workspace_size(_workspace(), rel)
        except AttachmentError as e:
            lines.append(f"• {_short(item)} (will not be attached: {_safe_text(e.reason)})")
            continue
        found += 1
        total += size
        lines.append(f"• {_short(rel)} ({_human_size(size)})")
    summary = f"{found} file{'' if found == 1 else 's'}, {_human_size(total)}"
    if found < len(requested):
        summary += f"; {len(requested) - found} will not be attached"
    shown = lines[:_DISPLAY_MAX_FILES]
    if len(lines) > _DISPLAY_MAX_FILES:
        shown.append(f"+{len(lines) - _DISPLAY_MAX_FILES} more")
    return "\n".join([summary, *shown])


async def approval_display_args(arguments: dict[str, Any], file_path_params: tuple[str, ...]) -> dict[str, Any]:
    """Arguments as shown on an approval prompt, for a tool whose *file_path_params* hold local paths.

    Each such argument becomes the :func:`describe_attachments` text. Channels
    cut the preview at 200 characters, so the order is: recipient and subject
    first (``_DISPLAY_HEAD_KEYS``), then the attachments, then everything else
    (a long body can't push the files out of sight, and a long file list can't
    push out the recipient). The tool itself always gets the original arguments.
    """
    shown: dict[str, Any] = {}
    for name in file_path_params:
        raw = arguments.get(name)
        if raw not in (None, "", []):
            shown[name] = await asyncio.to_thread(describe_attachments, raw)
    if not shown:
        return arguments
    head = {k: arguments[k] for k in _DISPLAY_HEAD_KEYS if k in arguments}
    rest = {k: v for k, v in arguments.items() if k not in head and k not in shown}
    return {**head, **shown, **rest}

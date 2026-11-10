"""Outgoing email attachments: path allowlist, MIME parts, size limits, approval display.

Used by both the SMTP ``email_send`` tool and the Gmail send/reply/draft
tools. Attachments may only come from Pincer's own file areas: the
``downloads`` and ``workspace`` folders under ``~/.pincer`` and under the
configured ``data_dir`` (they differ in Docker, where ``PINCER_DATA_DIR`` is
``/app/data``). Drive exports, Gmail attachment downloads, ``python_exec``,
``file_write`` and chat uploads all save there. Anything that resolves outside
them is rejected, including ``../`` traversal and symlinks that point elsewhere.

Trust model: these folders are one shared workspace for the Pincer instance,
not per-user storage. Chat uploads from every channel and identity land in the
same flat ``workspace/uploads/`` folder, and ``file_write``/``python_exec``
share ``workspace/`` too. The allowlist keeps attachments inside Pincer's own
files; it does not separate users. That is why the approval prompt shows the
resolved path and size of every file before anything is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
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

from pincer.core.log_once import LogOnce
from pincer.utils.csv_list import split_csv
from pincer.utils.file_search import iter_named_files

if TYPE_CHECKING:
    from email.message import Message

logger = logging.getLogger(__name__)
_data_dir_failure = LogOnce(logger, logging.WARNING)

# Raw bytes. Base64 adds ~4/3 plus line breaks and headers, so 17 MB of files
# stays under Gmail's 25 MB message cap (18 MiB would reach ~25.8 MB).
MAX_ATTACHMENT_BYTES = 17_000_000
MAX_ATTACHMENTS = 10


class AttachmentError(ValueError):
    """An attachment was rejected (outside the allowlist, missing, ambiguous, or too large)."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    maintype: str
    subtype: str
    data: bytes


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


# ── Tool schema pieces shared by every attachment-capable send tool ──────────

ATTACHMENTS_HINT = (
    " To attach files, pass their paths in `attachments`. To send a Google Doc, first call "
    "google__export_google_doc (e.g. format pdf), then pass the returned path. Files must be in "
    "Pincer's downloads or workspace folders (downloads, exports, chat uploads, file_write or "
    f"python_exec output); total size up to {_mb(MAX_ATTACHMENT_BYTES)}."
)


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


# ── Allowed folders ──────────────────────────────────────────────────────────


def _configured_data_dir() -> Path | None:
    from pincer.config import get_settings_relaxed

    try:
        data_dir = get_settings_relaxed().data_dir
    except (ValueError, OSError):  # pydantic ValidationError is a ValueError
        _data_dir_failure.log(
            "Could not read data_dir from settings; attachments are limited to ~/.pincer", exc_info=True
        )
        return None
    _data_dir_failure.clear()
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


def _ambiguous(requested: str, matches: list[Path]) -> AttachmentError:
    listed = "\n".join(f"  - {m}" for m in matches)
    return AttachmentError(
        f"Attachment '{requested}' matches {len(matches)} files:\n{listed}\nPass the full path of the one to send."
    )


def _allowed_file(candidate: Path, allowed: list[Path]) -> Path | None:
    """The resolved path if *candidate* is a file inside the allowlist, else None."""
    if not candidate.is_file():
        return None
    resolved = candidate.resolve()
    return resolved if _within_allowed(resolved, allowed) else None


def resolve_attachment_path(requested: str) -> Path:
    """Resolve *requested* to a real file inside the attachment allowlist.

    * Absolute paths (after ``~`` expansion) must already sit inside an allowed
      directory.
    * Relative paths with a folder (``downloads/report.pdf``) are looked up
      directly under each allowed directory and each Pincer root. A single
      direct hit wins; the basename search only runs when there is none.
    * Bare filenames are looked up directly and by a basename search of the
      allowed directories. If more than one file matches, the call fails and
      asks for the full path.

    Candidates outside the allowlist are skipped, never reported. Raises
    AttachmentError if the file is missing, ambiguous, or outside the allowlist.
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

    has_folder = len(expanded.parts) > 1
    if not (has_folder and matches):
        for path in iter_named_files(allowed, expanded.name):
            _add(_allowed_file(path, allowed))

    if not matches:
        raise _not_found(requested, allowed)
    if len(matches) > 1:
        raise _ambiguous(requested, matches)
    return matches[0]


# ── Loading ──────────────────────────────────────────────────────────────────


def normalize_attachment_args(paths: list[str] | str | None) -> list[str]:
    """Accept a list, a comma-separated string (LLMs sometimes send one), or nothing."""
    if not paths:
        return []
    if isinstance(paths, str):
        return split_csv(paths)
    return [str(p).strip() for p in paths if str(p).strip()]


def _too_large(total: int) -> AttachmentError:
    return AttachmentError(
        f"Attachments total {_mb(total)}; limit is {_mb(MAX_ATTACHMENT_BYTES)}. "
        "Email can't carry files this large. Share a Drive link instead "
        "(e.g. google__share_file, then include the link in the body)."
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


def _read_verified(path: Path, budget: int, allowed: list[Path]) -> bytes:
    """Read at most ``budget + 1`` bytes of *path*, checking what was actually opened.

    ``O_NOFOLLOW`` refuses a symlink in the last component and ``O_NONBLOCK``
    stops a FIFO swapped in from hanging the worker. After opening, the fd must
    be a regular file whose real path is still inside the allowlist, which
    catches an intermediate directory swapped for a symlink after ``resolve()``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AttachmentError(f"Attachment '{path.name}' is not a regular file.")
        real = _fd_real_path(fd)
        if real is not None:
            inside = _within_allowed(real, allowed)
        else:
            current = os.stat(path)
            inside = path.resolve() == path and (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino)
        if not inside:
            raise _outside(path.name, allowed)
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


def _safe_filename(name: str) -> str:
    """Replace control characters, which break header serialisation (e.g. a newline in the name)."""
    return "".join("_" if ord(c) < 32 or ord(c) == 127 else c for c in name)


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

    allowed = allowed_attachment_dirs()
    attachments: list[Attachment] = []
    total = 0
    for path in resolved:
        try:
            data = _read_verified(path, MAX_ATTACHMENT_BYTES - total, allowed)
        except OSError as e:
            raise AttachmentError(f"Could not read attachment '{path.name}': {e.strerror or e}") from e
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise _too_large(total)
        maintype, subtype = _mime_type(path.name)
        attachments.append(
            Attachment(filename=_safe_filename(path.name), maintype=maintype, subtype=subtype, data=data)
        )
    return attachments


async def load_attachments_async(paths: list[str] | str | None) -> list[Attachment]:
    """:func:`load_attachments` off the event loop (it walks folders and reads up to 17 MB)."""
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


def _display_path(path: Path) -> str:
    home = Path.home().resolve()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _human_size(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.0f} KB"
    return _mb(n)


def describe_attachments(paths: list[str] | str) -> str:
    """What will actually be attached, one file per line: resolved path and size.

    A requested path that won't resolve is shown with the reason, so the
    approver isn't misled into approving a file that won't be sent. One line
    per file, because a path can contain ", ".
    """
    lines = []
    for requested in normalize_attachment_args(paths):
        try:
            path = resolve_attachment_path(requested)
            lines.append(f"• {_display_path(path)} ({_human_size(path.stat().st_size)})")
        except (AttachmentError, OSError) as e:
            reason = str(e).split(". ", 1)[0]
            lines.append(f"• {requested} (will not be attached: {reason})")
    return "\n" + "\n".join(lines) if lines else ""


async def approval_display_args(arguments: dict[str, Any], file_path_params: tuple[str, ...]) -> dict[str, Any]:
    """Arguments as shown on an approval prompt, for a tool whose *file_path_params* hold local paths.

    Each such argument becomes the :func:`describe_attachments` text and moves
    to the front, so channels that truncate the preview (Telegram cuts at 200
    characters) can't push it out of sight behind a long body. Other arguments
    keep their order. The tool itself always gets the original arguments.
    """
    shown: dict[str, Any] = {}
    for name in file_path_params:
        raw = arguments.get(name)
        if raw and isinstance(raw, (str, list)):
            shown[name] = await asyncio.to_thread(describe_attachments, raw)
    if not shown:
        return arguments
    return {**shown, **{k: v for k, v in arguments.items() if k not in shown}}

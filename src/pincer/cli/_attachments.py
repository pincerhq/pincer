"""File/image attachments of incoming messages: saved to the workspace's `uploads/`
folder, and described for the LLM prompt."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from pincer.storage.workspace import safe_upload_path as _safe_upload_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pincer.storage.workspace import Workspace

__all__ = ["_safe_upload_path", "describe_file_attachments", "describe_image_attachments"]

logger = logging.getLogger(__name__)

UPLOADS = "uploads"
_TEXT_MIME_TYPES = ("application/json", "application/xml", "application/x-yaml")
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".py", ".js", ".ts", ".json", ".csv", ".md", ".log", ".xml", ".html", ".css",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".sql", ".rs", ".go", ".java",
        ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt", ".env", ".gitignore",
    }
)  # fmt: skip
_MAX_TEXT_CHARS = 30_000


def _format_pdf_attachment(pages: list[str], filename: str, abs_path: str, max_chars: int = 30_000) -> str:
    """Format a PDF attachment's extracted text for the LLM prompt.

    A scanned/image-only PDF has no embedded text layer, so pymupdf's
    `page.get_text()` returns empty/near-empty strings per page — detect that
    case (average non-whitespace chars/page below a conservative threshold;
    real text pages have hundreds, so this only misfires if *every* page in
    the document is near-blank) and say so explicitly instead of silently
    reporting a blank code block, so the agent knows to reach for an
    OCR-capable tool rather than guessing at the contents from the filename.
    """
    content = "\n\n".join(pages)
    non_ws_chars = len("".join(content.split()))
    is_scanned = bool(pages) and non_ws_chars < 20 * len(pages)
    if is_scanned:
        return (
            f"[File: {filename} — {len(pages)} pages] saved to {abs_path}.\n"
            "No embedded text found; this PDF appears to be a scanned/image-only "
            "document (pages are images, not text). If the user wants its text "
            f"read/extracted, call the OCR tool with file_path='{abs_path}' — "
            "do not guess at the contents from the filename, and do not try to "
            "transcribe it yourself."
        )
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... [truncated, {len(pages)} pages total]"
    return f"[File: {filename} — {len(pages)} pages, saved to {abs_path}]\n```\n{content}\n```"


_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _format_image_attachment(filename: str, abs_path: str, size_bytes: int, media_type: str) -> str:
    """Format an image attachment note for the LLM prompt.

    Images have no text layer the model can inspect ahead of time, and unlike
    a vision content block, an OCR tool needs a concrete argument (a file
    path here) to act on — the model cannot regenerate the original bytes
    from having merely "seen" the image. Give it that path directly, and
    tell it not to transcribe the image itself via vision, which is slow
    and produces nothing an OCR tool call can use.
    """
    return (
        f"[Image: {filename}] saved to {abs_path} ({size_bytes} bytes, {media_type}).\n"
        "If the user wants text read/extracted from this image, call the OCR tool "
        f"with file_path='{abs_path}' — do not try to transcribe it yourself."
    )


def describe_file_attachments(ws: Workspace, files: Iterable[tuple[bytes, str, str]]) -> list[str]:
    """Save each `(raw bytes, mime type, filename)` to `uploads/` and describe it for the prompt.

    Text files are inlined, PDFs have their text extracted, anything else is
    named by its local path. An unsafe filename is rejected, never saved. A
    file that can't be stored (the bucket is unreachable) is still described,
    without a path, so one storage error never drops the whole message.
    Blocking (a cloud root uploads each file).
    """
    parts: list[str] = []
    for raw_bytes, mime, filename in files:
        try:
            save_path: str | None = str(ws.save(UPLOADS, filename, raw_bytes))
        except ValueError:
            logger.warning("Rejected unsafe attachment filename: %r", filename)
            parts.append(f"[File: {filename}] rejected — unsafe filename")
            continue
        except Exception:
            logger.warning("Could not save attachment %r to the workspace", filename, exc_info=True)
            save_path = None
        parts.append(_describe_file(raw_bytes, mime, filename, save_path))
    return parts


def _not_saved(filename: str, raw_bytes: bytes, mime: str) -> str:
    return (
        f"[File: {filename}] ({len(raw_bytes)} bytes, {mime}) could not be saved to the workspace "
        "(storage unavailable), so there is no file path for it. Tell the user and ask them to send it again later."
    )


def _describe_file(raw_bytes: bytes, mime: str, filename: str, abs_path: str | None) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if mime.startswith("text/") or mime in _TEXT_MIME_TYPES or ext in _TEXT_EXTENSIONS:
        content = raw_bytes.decode("utf-8", errors="replace")
        if len(content) > _MAX_TEXT_CHARS:
            content = content[:_MAX_TEXT_CHARS] + f"\n... [truncated, {len(raw_bytes)} bytes total]"
        return f"[File: {filename}]\n```\n{content}\n```"
    if abs_path is None:
        return _not_saved(filename, raw_bytes, mime)
    if ext == ".pdf":
        try:
            import pymupdf

            doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
            pages = [page.get_text() for page in doc]
            doc.close()
            return _format_pdf_attachment(pages, filename, abs_path)
        except ImportError:
            return (
                f"[File: {filename}] saved to {abs_path} ({len(raw_bytes)} bytes, {mime}). "
                f"PDF text extraction unavailable (install pymupdf)."
            )
        except Exception as exc:
            return (
                f"[File: {filename}] saved to {abs_path} ({len(raw_bytes)} bytes, {mime}). PDF extraction failed: {exc}"
            )
    return (
        f"[File: {filename}] saved to {abs_path} ({len(raw_bytes)} bytes, {mime}). "
        f"Use shell_exec to process it with the absolute path above."
    )


def describe_image_attachments(ws: Workspace, images: Iterable[tuple[bytes, str]]) -> list[str]:
    """Save each `(raw bytes, media type)` to `uploads/` under a generated name and
    describe it for the prompt. An image that can't be stored is described
    without a path. Blocking (a cloud root uploads each image)."""
    parts: list[str] = []
    for raw_bytes, media_type in images:
        filename = f"image_{uuid.uuid4().hex[:8]}{_IMAGE_EXTENSIONS.get(media_type, '.bin')}"
        try:
            save_path = ws.save(UPLOADS, filename, raw_bytes)
        except Exception:
            logger.warning("Could not save image %s to the workspace", filename, exc_info=True)
            parts.append(_not_saved(f"Image {filename}", raw_bytes, media_type))
            continue
        parts.append(_format_image_attachment(filename, str(save_path), len(raw_bytes), media_type))
    return parts

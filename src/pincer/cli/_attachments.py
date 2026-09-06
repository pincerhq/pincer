"""Formatting helpers for file/image attachments forwarded to the LLM prompt."""

from __future__ import annotations


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

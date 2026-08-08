"""IO helpers: decode tool inputs (base64 bytes or an absolute file path) into
doctr-ready page arrays, sniff image vs. PDF content, and flatten doctr's geometry
tuples for JSON serialization.

Mirrors mindee/doctr's own reference app (`api/app/utils.py`): mime-dispatch to
`DocumentFile.from_images`/`.from_pdf`, and `resolve_geometry`'s box/polygon
flattening — adapted here for MCP's base64/file_path input instead of FastAPI's
`UploadFile`.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from doctr_mcp.config import get_settings

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


class InputError(ValueError):
    """Any problem with caller-supplied image_b64/file_path input — reported back to
    the MCP caller as a clean tool error, not a stack trace."""


def _check_size(data: bytes) -> None:
    max_bytes = get_settings().max_input_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise InputError(
            f"Input is {len(data) / (1024 * 1024):.1f}MB, exceeding the "
            f"{get_settings().max_input_mb}MB limit (DOCTR_MAX_INPUT_MB)."
        )


def decode_input(image_b64: str | None, file_path: str | None) -> tuple[bytes, str | None]:
    """Return (raw_bytes, filename_hint) for exactly one of image_b64 or file_path.

    Mutual-exclusivity is primarily enforced by each tool's pydantic input model
    (`model_validator`) — this is a defensive second check plus the actual decode.
    """
    if image_b64 and file_path:
        raise InputError("Provide exactly one of image_b64 or file_path, not both.")
    if not image_b64 and not file_path:
        raise InputError("Provide one of image_b64 or file_path.")

    if file_path:
        path = Path(file_path)
        if not path.is_absolute():
            raise InputError(f"file_path must be an absolute path, got {file_path!r}.")
        if not path.is_file():
            raise InputError(f"file_path does not exist or is not a file: {file_path}")
        data = path.read_bytes()
        filename = path.name
    else:
        assert image_b64 is not None
        try:
            data = base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise InputError(f"image_b64 is not valid base64: {e}") from e
        filename = None

    _check_size(data)
    return data, filename


def _sniff_kind(data: bytes, filename: str | None) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if filename:
        ext = Path(filename).suffix.lower()
        if ext == ".pdf":
            return "pdf"
        if ext in _IMAGE_EXTENSIONS:
            return "image"
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            img.verify()
        return "image"
    except Exception:
        hint = f" for {filename!r}" if filename else ""
        raise InputError(
            f"Unsupported or unrecognized file content{hint}. Supported formats: PNG, JPEG, BMP, TIFF, WEBP, PDF."
        ) from None


def documents_from_bytes(
    data: bytes,
    filename: str | None,
    page_range: tuple[int, int] | None = None,
) -> tuple[list[Any], list[str]]:
    """Build (page_arrays, page_filenames) ready for a doctr predictor call.

    For PDFs, `page_range` is an optional (start, end) slice (end-exclusive) applied
    before running any model — a full multi-hundred-page PDF would otherwise blow
    past any reasonable tool-result size.
    """
    from doctr.io import DocumentFile

    kind = _sniff_kind(data, filename)
    label = filename or ("document.pdf" if kind == "pdf" else "image")

    if kind == "pdf":
        pages = list(DocumentFile.from_pdf(data))
        if page_range is not None:
            start, end = page_range
            pages = pages[start:end]
        if not pages:
            raise InputError("page_range selected zero pages.")
        return pages, [label] * len(pages)

    pages = list(DocumentFile.from_images([data]))
    return pages, [label]


def resolve_geometry(geom: Any) -> tuple[float, ...]:
    """Flatten doctr's polygon/box geometry (tuple-of-(x,y)-points) into a flat
    tuple of floats — never hand raw numpy arrays or nested tuples across the MCP
    boundary. Mirrors doctr's own `api/app/utils.resolve_geometry`: a 4-point
    polygon flattens to 8 floats, a 2-point (xmin,ymin)/(xmax,ymax) box to 4."""
    if len(geom) == 4:
        return (*geom[0], *geom[1], *geom[2], *geom[3])
    return (*geom[0], *geom[1])


def round_confidence(value: float) -> float:
    return round(float(value), 2)

"""Tests for base64/file_path decoding, mime sniffing, and geometry flattening."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from doctr_mcp import io_utils


def test_decode_input_requires_exactly_one_source() -> None:
    with pytest.raises(io_utils.InputError, match="exactly one"):
        io_utils.decode_input("Zm9v", "/tmp/x.png")
    with pytest.raises(io_utils.InputError, match="Provide one"):
        io_utils.decode_input(None, None)


def test_decode_input_invalid_base64() -> None:
    with pytest.raises(io_utils.InputError, match="not valid base64"):
        io_utils.decode_input("not-valid-base64!!!", None)


def test_decode_input_from_base64(tiny_png_b64: str) -> None:
    data, filename = io_utils.decode_input(tiny_png_b64, None)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert filename is None


def test_decode_input_from_file_path_requires_absolute(tmp_path: Path) -> None:
    rel = "relative/path.png"
    with pytest.raises(io_utils.InputError, match="absolute"):
        io_utils.decode_input(None, rel)


def test_decode_input_from_file_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.png"
    with pytest.raises(io_utils.InputError, match="does not exist"):
        io_utils.decode_input(None, str(missing))


def test_decode_input_from_file_path(tmp_path: Path, tiny_png_b64: str) -> None:
    raw = base64.b64decode(tiny_png_b64)
    path = tmp_path / "scan.png"
    path.write_bytes(raw)

    data, filename = io_utils.decode_input(None, str(path))
    assert data == raw
    assert filename == "scan.png"


def test_decode_input_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from doctr_mcp.config import get_settings

    monkeypatch.setenv("DOCTR_MAX_INPUT_MB", "0")
    get_settings.cache_clear()
    try:
        with pytest.raises(io_utils.InputError, match="exceeding"):
            io_utils.decode_input(base64.b64encode(b"x" * 1024).decode(), None)
    finally:
        get_settings.cache_clear()


def test_documents_from_bytes_image(tiny_png_b64: str) -> None:
    raw = base64.b64decode(tiny_png_b64)
    pages, names = io_utils.documents_from_bytes(raw, "scan.png")
    assert len(pages) == 1
    assert names == ["scan.png"]


def test_documents_from_bytes_unsupported_content() -> None:
    with pytest.raises(io_utils.InputError, match="Unsupported"):
        io_utils.documents_from_bytes(b"not a real image or pdf", "mystery.bin")


def test_resolve_geometry_box() -> None:
    box = ((0.1, 0.2), (0.3, 0.4))
    assert io_utils.resolve_geometry(box) == (0.1, 0.2, 0.3, 0.4)


def test_resolve_geometry_polygon() -> None:
    polygon = ((0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1))
    assert io_utils.resolve_geometry(polygon) == (0.0, 0.0, 0.1, 0.0, 0.1, 0.1, 0.0, 0.1)


def test_round_confidence() -> None:
    assert io_utils.round_confidence(0.94999) == 0.95

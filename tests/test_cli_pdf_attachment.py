"""Tests for _format_pdf_attachment — the scanned/image-only PDF detection
heuristic in cli.py's automatic PDF-attachment text extraction."""

from __future__ import annotations

from pincer.cli import _format_pdf_attachment


def test_normal_text_pdf_returns_code_fence() -> None:
    pages = ["This is a real page of extracted text with plenty of content. " * 5]
    result = _format_pdf_attachment(pages, "report.pdf", "/data/uploads/report.pdf")
    assert "```" in result
    assert "This is a real page" in result
    assert "scanned" not in result


def test_scanned_pdf_all_pages_empty_flags_no_text() -> None:
    pages = ["", "", ""]
    result = _format_pdf_attachment(pages, "scan.pdf", "/data/uploads/scan.pdf")
    assert "scanned/image-only" in result
    assert "```" not in result
    assert "3 pages" in result


def test_scanned_pdf_whitespace_only_pages_flags_no_text() -> None:
    """pymupdf commonly returns form-feed/whitespace artifacts, not truly empty strings."""
    pages = ["\n\n\x0c", "   \n", "\x0c"]
    result = _format_pdf_attachment(pages, "scan.pdf", "/data/uploads/scan.pdf")
    assert "scanned/image-only" in result


def test_single_blank_page_among_text_pages_does_not_trigger_scanned_path() -> None:
    """One blank page (e.g. a divider) in an otherwise text-rich document must not
    be misclassified — the heuristic averages across all pages."""
    text_page = "Real extracted paragraph text with plenty of words in it. " * 10
    pages = [text_page, "", text_page]
    result = _format_pdf_attachment(pages, "mixed.pdf", "/data/uploads/mixed.pdf")
    assert "```" in result
    assert "scanned" not in result


def test_short_but_real_single_page_pdf_does_not_trigger_scanned_path() -> None:
    """A short but genuinely non-empty single-page PDF (e.g. a cover page)
    should not be misclassified as scanned."""
    pages = ["Confidential — Draft v3 — Do not distribute"]
    result = _format_pdf_attachment(pages, "cover.pdf", "/data/uploads/cover.pdf")
    assert "```" in result
    assert "scanned" not in result


def test_empty_document_no_pages_does_not_crash() -> None:
    result = _format_pdf_attachment([], "empty.pdf", "/data/uploads/empty.pdf")
    assert "0 pages" in result
    assert "scanned" not in result


def test_long_content_is_truncated() -> None:
    pages = ["word " * 20_000]
    result = _format_pdf_attachment(pages, "huge.pdf", "/data/uploads/huge.pdf", max_chars=100)
    assert "truncated" in result
    assert "1 pages total" in result

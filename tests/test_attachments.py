"""Tests for outgoing email attachment resolution (pincer.tools.attachments)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pincer.tools import attachments as att
from pincer.tools.attachments import (
    AttachmentError,
    approval_display_args,
    load_attachments,
    resolve_attachment_path,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".pincer" / "downloads").mkdir(parents=True)
    (tmp_path / ".pincer" / "workspace" / "exec_output").mkdir(parents=True)
    return tmp_path


def _write(path: Path, data: bytes = b"hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── resolve_attachment_path ──────────────────────────────────────────────────


def test_absolute_path_in_downloads(home: Path) -> None:
    f = _write(home / ".pincer" / "downloads" / "report.pdf")
    assert resolve_attachment_path(str(f)) == f.resolve()


def test_tilde_path(home: Path) -> None:
    f = _write(home / ".pincer" / "workspace" / "notes.txt")
    assert resolve_attachment_path("~/.pincer/workspace/notes.txt") == f.resolve()


def test_bare_filename_found_in_allowed_dir(home: Path) -> None:
    f = _write(home / ".pincer" / "downloads" / "report.pdf")
    assert resolve_attachment_path("report.pdf") == f.resolve()


def test_bare_filename_found_by_walk(home: Path) -> None:
    f = _write(home / ".pincer" / "workspace" / "exec_output" / "chart.png")
    assert resolve_attachment_path("chart.png") == f.resolve()


def test_relative_to_pincer_home(home: Path) -> None:
    f = _write(home / ".pincer" / "downloads" / "a.csv")
    assert resolve_attachment_path("downloads/a.csv") == f.resolve()


def test_rejects_absolute_path_outside_allowed(home: Path) -> None:
    secret = _write(home / "secret.txt")
    with pytest.raises(AttachmentError, match="outside the allowed folders"):
        resolve_attachment_path(str(secret))


def test_rejects_etc_passwd(home: Path) -> None:
    with pytest.raises(AttachmentError):
        resolve_attachment_path("/etc/passwd")


def test_absolute_path_not_swapped_for_same_basename(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "report.pdf")
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment_path(str(home / "elsewhere" / "report.pdf"))


def test_rejects_dotdot_traversal(home: Path) -> None:
    _write(home / "secret.txt")
    with pytest.raises(AttachmentError, match="outside the allowed folders"):
        resolve_attachment_path("../../secret.txt")


def test_rejects_symlink_escape(home: Path) -> None:
    secret = _write(home / "secret.txt")
    link = home / ".pincer" / "downloads" / "innocent.pdf"
    link.symlink_to(secret)
    with pytest.raises(AttachmentError, match="outside the allowed folders"):
        resolve_attachment_path(str(link))
    with pytest.raises(AttachmentError, match="outside the allowed folders"):
        resolve_attachment_path("innocent.pdf")


def test_symlink_escape_not_found_by_walk(home: Path) -> None:
    secret = _write(home / "secret.txt")
    (home / ".pincer" / "workspace" / "sub").mkdir()
    (home / ".pincer" / "workspace" / "sub" / "leak.txt").symlink_to(secret)
    with pytest.raises(AttachmentError):
        resolve_attachment_path("leak.txt")


def test_missing_file(home: Path) -> None:
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment_path("nope.pdf")


def test_empty_path(home: Path) -> None:
    with pytest.raises(AttachmentError, match="empty"):
        resolve_attachment_path("   ")


# ── load_attachments ─────────────────────────────────────────────────────────


def test_load_none_and_empty(home: Path) -> None:
    assert load_attachments(None) == []
    assert load_attachments([]) == []
    assert load_attachments("") == []


def test_load_mime_types(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "a.pdf", b"%PDF")
    _write(home / ".pincer" / "downloads" / "b.unknownext", b"x")
    result = load_attachments(["a.pdf", "b.unknownext"])
    assert [(a.filename, a.maintype, a.subtype) for a in result] == [
        ("a.pdf", "application", "pdf"),
        ("b.unknownext", "application", "octet-stream"),
    ]
    assert result[0].data == b"%PDF"


def test_load_csv_string(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "a.pdf")
    _write(home / ".pincer" / "downloads" / "b.txt")
    result = load_attachments("a.pdf, b.txt")
    assert [a.filename for a in result] == ["a.pdf", "b.txt"]


def test_load_dedupes(home: Path) -> None:
    f = _write(home / ".pincer" / "downloads" / "a.pdf")
    assert len(load_attachments(["a.pdf", str(f), "~/.pincer/downloads/a.pdf"])) == 1


def test_load_size_limit(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 10)
    _write(home / ".pincer" / "downloads" / "big.bin", b"x" * 11)
    with pytest.raises(AttachmentError, match=r"limit is .*Drive link"):
        load_attachments(["big.bin"])


def test_load_count_limit(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACHMENTS", 2)
    for name in ("a", "b", "c"):
        _write(home / ".pincer" / "downloads" / f"{name}.txt")
    with pytest.raises(AttachmentError, match="Too many attachments"):
        load_attachments(["a.txt", "b.txt", "c.txt"])


def test_load_rejects_disallowed_path(home: Path) -> None:
    with pytest.raises(AttachmentError):
        load_attachments(["/etc/passwd"])


# ── approval_display_args ────────────────────────────────────────────────────


def test_approval_display_args_basenames() -> None:
    args = {"to": "a@b.c", "attachments": ["/home/u/.pincer/downloads/Q3 report.pdf", "~/.pincer/workspace/x.xlsx"]}
    shown = approval_display_args(args)
    assert shown["attachments"] == "Q3 report.pdf, x.xlsx"
    assert shown["to"] == "a@b.c"
    assert args["attachments"][0].startswith("/home")  # original untouched


def test_approval_display_args_passthrough() -> None:
    args = {"to": "a@b.c"}
    assert approval_display_args(args) is args
    assert approval_display_args({"attachments": []}) == {"attachments": []}

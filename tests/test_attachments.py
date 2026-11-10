"""Tests for outgoing email attachment resolution (pincer.tools.attachments)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from pincer.tools import attachments as att
from pincer.tools.attachments import (
    AttachmentError,
    describe_attachments,
    load_attachments,
    resolve_attachment_path,
)
from pincer.tools.registry import ToolRegistry
from pincer.utils.file_search import iter_named_files

_REAL_CONFIGURED_DATA_DIR = att._configured_data_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(att, "_configured_data_dir", lambda: None)
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
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment_path("innocent.pdf")


def test_symlink_escape_not_found_by_walk(home: Path) -> None:
    secret = _write(home / "secret.txt")
    (home / ".pincer" / "workspace" / "sub").mkdir()
    (home / ".pincer" / "workspace" / "sub" / "leak.txt").symlink_to(secret)
    with pytest.raises(AttachmentError):
        resolve_attachment_path("leak.txt")


def test_outside_candidate_is_skipped_not_reported(home: Path) -> None:
    # ~/.pincer/config.toml exists but is outside the allowlist: keep searching.
    _write(home / ".pincer" / "config.toml", b"secret")
    f = _write(home / ".pincer" / "workspace" / "exec_output" / "config.toml", b"ok")
    assert resolve_attachment_path("config.toml") == f.resolve()


def test_relative_path_with_folder_wins_over_basename_matches(home: Path) -> None:
    direct = _write(home / ".pincer" / "downloads" / "report.pdf")
    _write(home / ".pincer" / "workspace" / "exec_output" / "report.pdf")
    assert resolve_attachment_path("downloads/report.pdf") == direct.resolve()
    with pytest.raises(AttachmentError, match="matches 2 files"):
        resolve_attachment_path("report.pdf")


def test_relative_path_with_folder_falls_back_to_basename_search(home: Path) -> None:
    f = _write(home / ".pincer" / "workspace" / "exec_output" / "chart.png")
    assert resolve_attachment_path("charts/chart.png") == f.resolve()


def test_data_dir_settings_failure_is_logged(home: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(att, "_configured_data_dir", _REAL_CONFIGURED_DATA_DIR)
    import pincer.config

    def broken():
        raise ValueError("bad config")

    monkeypatch.setattr(pincer.config, "get_settings_relaxed", broken)
    monkeypatch.setattr(att, "_data_dir_failure", att.LogOnce(att.logger, logging.WARNING))
    with caplog.at_level(logging.WARNING, logger=att.logger.name):
        assert att._configured_data_dir() is None
    assert "Could not read data_dir" in caplog.text


def test_iter_named_files(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "x.txt")
    _write(tmp_path / "a" / ".hidden" / "x.txt")
    _write(tmp_path / "b" / "deep" / "x.txt")
    found = list(iter_named_files([tmp_path / "a", tmp_path / "missing", tmp_path / "b"], "x.txt"))
    assert found == [tmp_path / "a" / "x.txt", tmp_path / "b" / "deep" / "x.txt"]


def test_outside_candidate_alone_is_not_found(home: Path) -> None:
    _write(home / ".pincer" / "pincer.db")
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment_path("pincer.db")


def test_ambiguous_bare_name_rejected(home: Path) -> None:
    a = _write(home / ".pincer" / "workspace" / "uploads" / "alice" / "contract.pdf")
    b = _write(home / ".pincer" / "workspace" / "uploads" / "bob" / "contract.pdf")
    with pytest.raises(AttachmentError, match="matches 2 files") as exc:
        resolve_attachment_path("contract.pdf")
    assert str(a.resolve()) in str(exc.value)
    assert str(b.resolve()) in str(exc.value)
    assert resolve_attachment_path(str(a)) == a.resolve()


def test_data_dir_workspace_allowed(home: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    data_dir = tmp_path_factory.mktemp("appdata")
    monkeypatch.setattr(att, "_configured_data_dir", lambda: data_dir)
    f = _write(data_dir / "workspace" / "uploads" / "report.pdf")
    assert resolve_attachment_path(str(f)) == f.resolve()
    assert resolve_attachment_path("report.pdf") == f.resolve()
    assert resolve_attachment_path("workspace/uploads/report.pdf") == f.resolve()
    with pytest.raises(AttachmentError, match="outside"):
        resolve_attachment_path(str(_write(data_dir / "pincer.db")))


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


def test_load_caps_bytes_read_not_just_stat(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A file that grows between the stat check and the read is still capped.
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 10)
    f = _write(home / ".pincer" / "downloads" / "growing.csv", b"x" * 5)
    real_read = att._read_verified

    def grow_then_read(path: Path, budget: int, allowed: list[Path]) -> bytes:
        f.write_bytes(b"x" * 50)
        data = real_read(path, budget, allowed)
        assert len(data) <= budget + 1
        return data

    monkeypatch.setattr(att, "_read_verified", grow_then_read)
    with pytest.raises(AttachmentError, match="limit is"):
        load_attachments(["growing.csv"])


def test_load_rejects_symlink_swapped_after_resolve(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = _write(home / "secret.txt")
    f = _write(home / ".pincer" / "downloads" / "a.txt")
    real_resolve = att.resolve_attachment_path

    def resolve_then_swap(p: str) -> Path:
        path = real_resolve(p)
        f.unlink()
        f.symlink_to(secret)
        return path

    monkeypatch.setattr(att, "resolve_attachment_path", resolve_then_swap)
    with pytest.raises(AttachmentError, match="Could not read"):
        load_attachments(["a.txt"])


def _resolve_to(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    """Make load_attachments see *fn*'s path, as if the file changed after resolve()."""
    monkeypatch.setattr(att, "resolve_attachment_path", fn)


def test_load_rejects_fifo_without_hanging(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fifo = home / ".pincer" / "downloads" / "pipe.pdf"
    os.mkfifo(fifo)
    _resolve_to(monkeypatch, lambda p: fifo.resolve())
    with pytest.raises(AttachmentError, match="not a regular file"):
        load_attachments(["pipe.pdf"])


def test_load_rejects_intermediate_dir_swapped_for_symlink(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = home / "outside"
    _write(outside / "a.txt", b"secret")
    sub = home / ".pincer" / "workspace" / "exec_output" / "sub"
    f = _write(sub / "a.txt", b"ok")
    resolved = f.resolve()

    def resolve_then_swap(p: str) -> Path:
        (sub / "a.txt").unlink()
        sub.rmdir()
        sub.symlink_to(outside, target_is_directory=True)
        return resolved

    _resolve_to(monkeypatch, resolve_then_swap)
    with pytest.raises(AttachmentError, match="outside the allowed folders"):
        load_attachments(["sub/a.txt"])


def test_control_characters_in_filename_replaced(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "a\nBcc: x@evil.com\n.pdf")
    [a] = load_attachments(["a\nBcc: x@evil.com\n.pdf"])
    assert a.filename == "a_Bcc: x@evil.com_.pdf"
    part = att.attachment_part(a)
    assert "\n" not in part.get_filename()
    part.as_bytes()  # serialises without HeaderParseError


def test_message_types(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "fwd.eml", b"Subject: hi\n\nbody")
    [eml] = load_attachments(["fwd.eml"])
    assert (eml.maintype, eml.subtype) == ("message", "rfc822")


async def test_load_attachments_async(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "a.pdf")
    assert [a.filename for a in await att.load_attachments_async(["a.pdf"])] == ["a.pdf"]
    assert await att.load_attachments_async(None) == []


def test_load_rejects_disallowed_path(home: Path) -> None:
    with pytest.raises(AttachmentError):
        load_attachments(["/etc/passwd"])


# ── Approval display ─────────────────────────────────────────────────────────


def test_describe_attachments_shows_resolved_path_and_size(home: Path) -> None:
    _write(home / ".pincer" / "downloads" / "Q3 report.pdf", b"x" * 2500)
    _write(home / ".pincer" / "downloads" / "a.pdf, ~/.pincer/downloads/b.pdf")
    text = describe_attachments(["Q3 report.pdf", "~/.pincer/downloads/a.pdf, ~/.pincer/downloads/b.pdf", "nope.pdf"])
    lines = text.strip().split("\n")
    assert lines == [
        "• ~/.pincer/downloads/Q3 report.pdf (2 KB)",
        "• ~/.pincer/downloads/a.pdf, ~/.pincer/downloads/b.pdf (5 B)",
        "• nope.pdf (will not be attached: Attachment not found: 'nope.pdf')",
    ]


def _registry() -> ToolRegistry:
    async def handler(**kwargs):
        return "ok"

    reg = ToolRegistry()
    reg.register("email_send", "send", handler, {"type": "object"}, True, file_path_params=("attachments",))
    reg.register("slack__post", "post", handler, {"type": "object"}, True)
    return reg


async def test_registry_approval_display_puts_attachments_first(home: Path) -> None:
    _write(home / ".pincer" / "workspace" / "uploads" / "secret.pdf")
    args = {"to": "a@b.c", "body": "B" * 300, "attachments": ["secret.pdf"]}
    shown = await _registry().approval_display_args("email_send", args)
    assert list(shown) == ["attachments", "to", "body"]
    assert "~/.pincer/workspace/uploads/secret.pdf" in shown["attachments"]
    # The preview a channel builds now names the file within its first 200 chars.
    assert "secret.pdf" in ", ".join(f"{k}={v}" for k, v in shown.items())[:200]
    assert args["attachments"] == ["secret.pdf"]  # the tool still gets the original


async def test_registry_approval_display_leaves_other_args_alone(home: Path) -> None:
    reg = _registry()
    slack_args = {"attachments": [{"text": "", "footer": "https://x/y"}]}
    assert await reg.approval_display_args("slack__post", slack_args) is slack_args
    assert await reg.approval_display_args("unknown_tool", slack_args) is slack_args
    plain = {"to": "a@b.c"}
    assert await reg.approval_display_args("email_send", plain) is plain
    weird = {"attachments": {"a": 1}}
    assert await reg.approval_display_args("email_send", weird) is weird

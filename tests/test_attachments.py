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
    real_read = att._read_capped

    def grow_then_read(path: Path, budget: int) -> bytes:
        f.write_bytes(b"x" * 50)
        data = real_read(path, budget)
        assert len(data) <= budget + 1
        return data

    monkeypatch.setattr(att, "_read_capped", grow_then_read)
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


# ── approval_display_args ────────────────────────────────────────────────────


def test_approval_display_args_email_tool(home: Path) -> None:
    args = {"to": "a@b.c", "attachments": [f"{home}/.pincer/downloads/Q3 report.pdf", "/app/data/workspace/x.xlsx"]}
    shown = approval_display_args("google__send_message", args)
    assert shown["attachments"] == "~/.pincer/downloads/Q3 report.pdf, /app/data/workspace/x.xlsx"
    assert shown["to"] == "a@b.c"
    assert isinstance(args["attachments"], list)  # original untouched
    assert approval_display_args("email_send", {"attachments": "a.pdf, b.pdf"})["attachments"] == "a.pdf, b.pdf"


def test_approval_display_args_other_tools_untouched() -> None:
    slack_args = {"attachments": [{"text": "", "footer": "https://x/y"}]}
    assert approval_display_args("slack__chat_postMessage", slack_args) is slack_args
    args = {"to": "a@b.c"}
    assert approval_display_args("email_send", args) is args
    weird = {"attachments": {"a": 1}}
    assert approval_display_args("email_send", weird) is weird

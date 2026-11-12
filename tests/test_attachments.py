"""Tests for outgoing email attachment resolution (pincer.tools.attachments)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pincer.storage.errors import StorageUnavailable
from pincer.storage.workspace import Workspace
from pincer.tools import attachments as att
from pincer.tools.attachments import (
    AttachmentError,
    describe_attachments,
    load_attachments,
    normalize_attachment_args,
    resolve_attachment,
)
from pincer.tools.registry import ToolRegistry


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local workspace (`<tmp>/storage/workspace`) the attachment tools use; returns its folder."""
    root = tmp_path / "storage" / "workspace"
    workspace = Workspace(root, root)
    workspace.ensure()
    monkeypatch.setattr(att, "_workspace", lambda: workspace)
    return root


def _write(path: Path, data: bytes = b"hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── resolve_attachment ───────────────────────────────────────────────────────


def test_absolute_path_in_workspace(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf")
    assert resolve_attachment(str(ws / "downloads" / "report.pdf")) == "downloads/report.pdf"


def test_tilde_path(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(ws.parent))
    _write(ws / "notes.txt")
    assert resolve_attachment("~/workspace/notes.txt") == "notes.txt"


def test_bare_filename_found_in_downloads(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf")
    assert resolve_attachment("report.pdf") == "downloads/report.pdf"


def test_bare_filename_found_deep(ws: Path) -> None:
    _write(ws / "exec_output" / "charts" / "chart.png")
    assert resolve_attachment("chart.png") == "exec_output/charts/chart.png"


def test_relative_path(ws: Path) -> None:
    _write(ws / "downloads" / "a.csv")
    assert resolve_attachment("downloads/a.csv") == "downloads/a.csv"


def test_rejects_absolute_path_outside_workspace(ws: Path) -> None:
    secret = _write(ws.parent.parent / "secret.txt")
    with pytest.raises(AttachmentError, match="outside the workspace") as exc:
        resolve_attachment(str(secret))
    assert exc.value.reason == "outside the workspace"


def test_rejects_etc_passwd(ws: Path) -> None:
    with pytest.raises(AttachmentError, match="outside the workspace"):
        resolve_attachment("/etc/passwd")


def test_legacy_pincer_home_path_is_outside(ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Files under ~/.pincer were migrated into the workspace (#375); the old path is not followed.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write(tmp_path / ".pincer" / "downloads" / "report.pdf")
    _write(ws / "downloads" / "report.pdf")
    with pytest.raises(AttachmentError, match="outside the workspace"):
        resolve_attachment("~/.pincer/downloads/report.pdf")


def test_absolute_path_not_swapped_for_same_basename(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf")
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment(str(ws / "uploads" / "report.pdf"))
    with pytest.raises(AttachmentError, match="outside"):
        resolve_attachment(str(ws.parent / "elsewhere" / "report.pdf"))


def test_rejects_dotdot_traversal(ws: Path) -> None:
    _write(ws.parent / "secret.txt")
    with pytest.raises(AttachmentError, match="outside the workspace"):
        resolve_attachment("../secret.txt")
    with pytest.raises(AttachmentError, match="outside the workspace"):
        resolve_attachment("downloads/../../secret.txt")


def test_rejects_symlink_escape(ws: Path) -> None:
    secret = _write(ws.parent / "secret.txt")
    link = ws / "downloads" / "innocent.pdf"
    link.symlink_to(secret)
    with pytest.raises(AttachmentError, match="outside the workspace"):
        resolve_attachment(str(link))
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment("innocent.pdf")


def test_symlinked_folder_escape_not_found_by_search(ws: Path) -> None:
    _write(ws.parent / "outside" / "leak.txt")
    (ws / "uploads" / "sub").symlink_to(ws.parent / "outside", target_is_directory=True)
    with pytest.raises(AttachmentError):
        resolve_attachment("leak.txt")
    with pytest.raises(AttachmentError):
        resolve_attachment("uploads/sub/leak.txt")


def test_relative_path_with_folder_wins_over_basename_matches(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf")
    _write(ws / "exec_output" / "report.pdf")
    assert resolve_attachment("downloads/report.pdf") == "downloads/report.pdf"
    with pytest.raises(AttachmentError, match="matches 2 files") as exc:
        resolve_attachment("report.pdf")
    assert exc.value.reason == "matches 2 files"


def test_relative_path_with_folder_falls_back_to_basename_search(ws: Path) -> None:
    _write(ws / "exec_output" / "chart.png")
    assert resolve_attachment("charts/chart.png") == "exec_output/chart.png"


def test_ambiguous_bare_name_lists_both(ws: Path) -> None:
    _write(ws / "uploads" / "alice" / "contract.pdf")
    _write(ws / "uploads" / "bob" / "contract.pdf")
    with pytest.raises(AttachmentError, match="matches 2 files") as exc:
        resolve_attachment("contract.pdf")
    assert "uploads/alice/contract.pdf" in str(exc.value)
    assert "uploads/bob/contract.pdf" in str(exc.value)
    assert resolve_attachment(str(ws / "uploads" / "alice" / "contract.pdf")) == "uploads/alice/contract.pdf"


def test_hidden_files_not_found_by_search(ws: Path) -> None:
    _write(ws / ".secrets" / "key.pem")
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment("key.pem")


def test_missing_file(ws: Path) -> None:
    with pytest.raises(AttachmentError, match="not found") as exc:
        resolve_attachment("nope.pdf")
    assert exc.value.reason == "not found"


def test_empty_path(ws: Path) -> None:
    with pytest.raises(AttachmentError, match="empty"):
        resolve_attachment("   ")


def test_unavailable_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_settings: Any) -> Workspace:
        raise StorageUnavailable("bucket unreachable")

    monkeypatch.setattr("pincer.storage.workspace.get_workspace", unavailable)
    with pytest.raises(AttachmentError, match="bucket unreachable") as exc:
        resolve_attachment("report.pdf")
    assert exc.value.reason == "workspace unavailable"


# ── normalize_attachment_args ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ([], []),
        (["a.pdf", " ", "b.pdf "], ["a.pdf", "b.pdf"]),
        ("a.pdf, b.pdf", ["a.pdf", "b.pdf"]),
        ('["a.pdf","b.pdf"]', ["a.pdf", "b.pdf"]),
        (' ["a, 1.pdf"] ', ["a, 1.pdf"]),
        ("[draft] report.pdf", ["[draft] report.pdf"]),
    ],
)
def test_normalize_attachment_args(value: object, expected: list[str]) -> None:
    assert normalize_attachment_args(value) == expected


@pytest.mark.parametrize("value", [5, {"a": 1}, ["a.pdf", 3], '["a.pdf", 3]', True])
def test_normalize_rejects_non_paths(value: object) -> None:
    with pytest.raises(AttachmentError, match="must be a list of file paths") as exc:
        normalize_attachment_args(value)
    assert exc.value.reason == "not a list of file paths"


# ── load_attachments ─────────────────────────────────────────────────────────


def test_load_none_and_empty(ws: Path) -> None:
    assert load_attachments(None) == []
    assert load_attachments([]) == []
    assert load_attachments("") == []


def test_load_mime_types(ws: Path) -> None:
    _write(ws / "downloads" / "a.pdf", b"%PDF")
    _write(ws / "downloads" / "b.unknownext", b"x")
    result = load_attachments(["a.pdf", "b.unknownext"])
    assert [(a.filename, a.maintype, a.subtype) for a in result] == [
        ("a.pdf", "application", "pdf"),
        ("b.unknownext", "application", "octet-stream"),
    ]
    assert result[0].data == b"%PDF"


def test_load_csv_and_json_strings(ws: Path) -> None:
    _write(ws / "downloads" / "a.pdf")
    _write(ws / "downloads" / "b.txt")
    assert [a.filename for a in load_attachments("a.pdf, b.txt")] == ["a.pdf", "b.txt"]
    assert [a.filename for a in load_attachments('["a.pdf", "b.txt"]')] == ["a.pdf", "b.txt"]


def test_load_dedupes(ws: Path) -> None:
    f = _write(ws / "downloads" / "a.pdf")
    assert len(load_attachments(["a.pdf", str(f), "downloads/a.pdf"])) == 1


def test_load_size_limit(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 10)
    _write(ws / "downloads" / "big.bin", b"x" * 11)
    with pytest.raises(AttachmentError, match=r"limit is .*Drive link"):
        load_attachments(["big.bin"])


def test_load_count_limit(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACHMENTS", 2)
    for name in ("a", "b", "c"):
        _write(ws / "downloads" / f"{name}.txt")
    with pytest.raises(AttachmentError, match="Too many attachments"):
        load_attachments(["a.txt", "b.txt", "c.txt"])


def test_load_caps_bytes_read_not_just_size(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A file that grows between the size check and the read is still capped.
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 10)
    f = _write(ws / "downloads" / "growing.csv", b"x" * 5)
    real_read = att._read_verified

    def grow_then_read(path: Path, budget: int, base: Path) -> bytes:
        f.write_bytes(b"x" * 50)
        data = real_read(path, budget, base)
        assert len(data) <= budget + 1
        return data

    monkeypatch.setattr(att, "_read_verified", grow_then_read)
    with pytest.raises(AttachmentError, match="limit is"):
        load_attachments(["growing.csv"])


def test_load_rejects_symlink_swapped_after_resolve(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = _write(ws.parent / "secret.txt")
    f = _write(ws / "downloads" / "a.txt")
    real_resolve = att.resolve_attachment

    def resolve_then_swap(p: str) -> str:
        rel = real_resolve(p)
        f.unlink()
        f.symlink_to(secret)
        return rel

    monkeypatch.setattr(att, "resolve_attachment", resolve_then_swap)
    with pytest.raises(AttachmentError, match="outside the workspace"):
        load_attachments(["a.txt"])


def test_load_rejects_symlink_swapped_before_read(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Swapped after the workspace checks, right before the open: O_NOFOLLOW refuses it.
    secret = _write(ws.parent / "secret.txt")
    f = _write(ws / "downloads" / "a.txt")
    real_read = att._read_verified

    def swap_then_read(path: Path, budget: int, base: Path) -> bytes:
        f.unlink()
        f.symlink_to(secret)
        return real_read(path, budget, base)

    monkeypatch.setattr(att, "_read_verified", swap_then_read)
    with pytest.raises(AttachmentError, match="Could not read"):
        load_attachments(["a.txt"])


def test_load_rejects_fifo_without_hanging(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    os.mkfifo(ws / "downloads" / "pipe.pdf")
    monkeypatch.setattr(att, "resolve_attachment", lambda p: "downloads/pipe.pdf")
    with pytest.raises(AttachmentError, match="not a regular file"):
        load_attachments(["pipe.pdf"])


def test_load_rejects_intermediate_dir_swapped_for_symlink(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = ws.parent / "outside"
    _write(outside / "a.txt", b"secret")
    sub = ws / "exec_output" / "sub"
    _write(sub / "a.txt", b"ok")

    real_read = att._read_verified

    def swap_then_read(path: Path, budget: int, base: Path) -> bytes:
        (sub / "a.txt").unlink()
        sub.rmdir()
        sub.symlink_to(outside, target_is_directory=True)
        return real_read(path, budget, base)

    monkeypatch.setattr(att, "_read_verified", swap_then_read)
    with pytest.raises(AttachmentError, match="outside the workspace"):
        load_attachments(["sub/a.txt"])


def test_load_bucket_error_is_an_attachment_error(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(ws / "downloads" / "a.txt")
    workspace = att._workspace()

    def broken(*_a: Any, **_k: Any) -> Path:
        raise RuntimeError("bucket says no")

    monkeypatch.setattr(workspace, "fetch", broken)
    with pytest.raises(AttachmentError, match="bucket says no"):
        load_attachments(["a.txt"])


def test_control_characters_in_filename_replaced(ws: Path) -> None:
    _write(ws / "downloads" / "a\nBcc: x@evil.com\n.pdf")
    [a] = load_attachments(["a\nBcc: x@evil.com\n.pdf"])
    assert a.filename == "a_Bcc: x@evil.com_.pdf"
    part = att.attachment_part(a)
    assert "\n" not in part.get_filename()
    part.as_bytes()  # serialises without HeaderParseError


def test_message_types(ws: Path) -> None:
    _write(ws / "downloads" / "fwd.eml", b"Subject: hi\n\nbody")
    [eml] = load_attachments(["fwd.eml"])
    assert (eml.maintype, eml.subtype) == ("message", "rfc822")


async def test_load_attachments_async(ws: Path) -> None:
    _write(ws / "downloads" / "a.pdf")
    assert [a.filename for a in await att.load_attachments_async(["a.pdf"])] == ["a.pdf"]
    assert await att.load_attachments_async(None) == []
    with pytest.raises(AttachmentError):
        await att.load_attachments_async(5)


def test_load_rejects_disallowed_path(ws: Path) -> None:
    with pytest.raises(AttachmentError):
        load_attachments(["/etc/passwd"])


# ── Cloud storage root ───────────────────────────────────────────────────────


@pytest.fixture
def cloud_ws(tmp_path: Path, mock_cloud_client: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A workspace on cloudpathlib's offline S3 mock, with a cache of its own."""
    from pincer.config.main import _RelaxedSettings

    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root="s3://ws-bucket/")  # type: ignore[call-arg]
    with patch("pincer.storage.clients.build_client", return_value=mock_cloud_client("s3")):
        workspace = Workspace.from_settings(settings)
    workspace.ensure()
    monkeypatch.setattr(att, "_workspace", lambda: workspace)
    return workspace


def test_cloud_file_only_in_bucket_is_fetched(cloud_ws: Workspace) -> None:
    # Uploaded through Documents: in the bucket, not yet in the local cache.
    (cloud_ws.remote / "uploads" / "contract.pdf").write_bytes(b"%PDF-cloud")
    (cloud_ws.local / "uploads" / "contract.pdf").unlink(missing_ok=True)
    assert resolve_attachment("contract.pdf") == "uploads/contract.pdf"
    [a] = load_attachments(["contract.pdf"])
    assert (a.filename, a.data) == ("contract.pdf", b"%PDF-cloud")


def test_cloud_absolute_cache_path_resolves(cloud_ws: Workspace) -> None:
    # A download returns the cached copy's path; that is what the model passes back.
    path = cloud_ws.save("downloads", "report.pdf", b"%PDF")
    assert resolve_attachment(str(path)) == "downloads/report.pdf"
    assert [a.data for a in load_attachments([str(path)])] == [b"%PDF"]


def test_cloud_size_limit_checked_before_download(cloud_ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 10)
    (cloud_ws.remote / "downloads" / "big.bin").write_bytes(b"x" * 11)
    fetched: list[Any] = []
    monkeypatch.setattr(cloud_ws, "fetch", lambda *a, **k: fetched.append(a))
    with pytest.raises(AttachmentError, match="limit is"):
        load_attachments(["big.bin"])
    assert fetched == []


def test_cloud_deleted_object_is_not_found(cloud_ws: Workspace) -> None:
    path = cloud_ws.save("downloads", "gone.pdf", b"%PDF")
    (cloud_ws.remote / "downloads" / "gone.pdf").unlink()
    assert path.is_file()  # a stale cached copy is not enough
    with pytest.raises(AttachmentError, match="not found"):
        resolve_attachment("gone.pdf")


# ── Approval display ─────────────────────────────────────────────────────────


def test_describe_attachments_shows_resolved_path_and_size(ws: Path) -> None:
    _write(ws / "downloads" / "Q3 report.pdf", b"x" * 2500)
    _write(ws / "downloads" / "a.pdf, downloads/b.pdf")
    text = describe_attachments(["Q3 report.pdf", "downloads/a.pdf, downloads/b.pdf", "nope.pdf"])
    assert text.split("\n") == [
        "2 files, 3 KB; 1 will not be attached",
        "• downloads/Q3 report.pdf (2 KB)",
        "• downloads/a.pdf, downloads/b.pdf (5 B)",
        "• nope.pdf (will not be attached: not found)",
    ]


def test_describe_attachments_caps_the_list(ws: Path) -> None:
    for name in "abcde":
        _write(ws / "downloads" / f"{name}.pdf", b"x")
    lines = describe_attachments([f"{n}.pdf" for n in "abcde"]).split("\n")
    assert lines[0] == "5 files, 5 B"
    assert lines[1:] == ["• downloads/a.pdf (1 B)", "• downloads/b.pdf (1 B)", "• downloads/c.pdf (1 B)", "+2 more"]


def test_describe_attachments_cannot_forge_lines(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf", b"x")
    text = describe_attachments(["nope\n• downloads/report.pdf (1 B)"])
    assert text.split("\n") == [
        "0 files, 0 B; 1 will not be attached",
        "• nope_• downloads/report.pdf (1 B) (will not be attached: not found)",
    ]


def test_describe_attachments_short_reasons(ws: Path) -> None:
    _write(ws / "downloads" / "report.pdf")
    _write(ws / "exec_output" / "report.pdf")
    text = describe_attachments(["report.pdf", "/etc/passwd"])
    assert text.split("\n")[1:] == [
        "• report.pdf (will not be attached: matches 2 files)",
        "• /etc/passwd (will not be attached: outside the workspace)",
    ]
    assert describe_attachments(5) == "none will be attached: not a list of file paths"


def test_describe_attachments_shortens_long_paths(ws: Path) -> None:
    name = "x" * 100 + ".pdf"
    _write(ws / "downloads" / name)
    line = describe_attachments([name]).split("\n")[1]
    assert line.startswith("• …") and line.endswith(".pdf (5 B)")
    assert len(line) < 80


def _registry() -> ToolRegistry:
    async def handler(**kwargs):
        return "ok"

    reg = ToolRegistry()
    reg.register("email_send", "send", handler, {"type": "object"}, True, file_path_params=("attachments",))
    reg.register("slack__post", "post", handler, {"type": "object"}, True)
    return reg


def _preview(shown: dict[str, Any]) -> str:
    """The approval preview Telegram, WhatsApp and Teams build: `k=v, ...` cut at 200 chars."""
    return ", ".join(f"{k}={v}" for k, v in shown.items())[:200]


async def test_registry_approval_display_order(ws: Path) -> None:
    _write(ws / "uploads" / "secret.pdf")
    args = {"body": "B" * 300, "attachments": ["secret.pdf"], "to": "a@b.c", "subject": "Hi"}
    shown = await _registry().approval_display_args("email_send", args)
    assert list(shown) == ["to", "subject", "attachments", "body"]
    assert "uploads/secret.pdf" in shown["attachments"]
    # A long body can't push the file out of the preview.
    assert "secret.pdf" in _preview(shown)
    assert args["attachments"] == ["secret.pdf"]  # the tool still gets the original


async def test_registry_approval_display_keeps_recipient_in_preview(ws: Path) -> None:
    # Many long paths, some rejected: the recipient still shows (review of #367).
    for i in range(3):
        _write(ws / "downloads" / f"quarterly-financial-report-{i}-final-version.pdf")
    files = [f"quarterly-financial-report-{i}-final-version.pdf" for i in range(3)] + ["/etc/passwd"] * 3
    args = {"attachments": files, "to": "ceo@corp.com", "subject": "Q3"}
    preview = _preview(await _registry().approval_display_args("email_send", args))
    assert preview.startswith("to=ceo@corp.com, subject=Q3, attachments=3 files")


async def test_registry_approval_display_leaves_other_args_alone(ws: Path) -> None:
    reg = _registry()
    slack_args = {"attachments": [{"text": "", "footer": "https://x/y"}]}
    assert await reg.approval_display_args("slack__post", slack_args) is slack_args
    assert await reg.approval_display_args("unknown_tool", slack_args) is slack_args
    plain = {"to": "a@b.c"}
    assert await reg.approval_display_args("email_send", plain) is plain


async def test_registry_approval_display_flags_bad_argument(ws: Path) -> None:
    shown = await _registry().approval_display_args("email_send", {"to": "a@b.c", "attachments": {"a": 1}})
    assert shown["attachments"] == "none will be attached: not a list of file paths"

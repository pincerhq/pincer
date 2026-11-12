"""Tests for the workspace under the storage root (`pincer.storage.workspace`),
on a local root and on cloudpathlib's offline S3, Azure and GCS mocks."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pincer.cli._attachments import describe_file_attachments, describe_image_attachments
from pincer.config.main import _RelaxedSettings
from pincer.storage import workspace as _workspace_module
from pincer.storage.errors import StorageUnavailable
from pincer.storage.keys import FOLDER_MARKER
from pincer.storage.workspace import Workspace, get_workspace, migrate_legacy, prepare_storage
from pincer.tools.path_sandbox import SandboxDenied

# Captured at import, before the autouse fixture swaps it for a temporary home.
_REAL_LEGACY_HOME_DIR = _workspace_module.legacy_home_dir

_ROOTS = {"s3": "s3://ws-bucket/", "azure": "az://ws-container/", "gs": "gs://ws-bucket/"}


@dataclass
class Env:
    settings: _RelaxedSettings
    ws: Workspace
    # The same storage root through another client with its own cache: a write
    # there changes an object behind the workspace's cache (like a Documents upload).
    external: Callable[[str], Any]


@pytest.fixture(params=["local", "s3", "azure", "gs"])
def env(request: pytest.FixtureRequest, tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> Iterator[Env]:
    backend = request.param
    data_dir = tmp_path / "data"
    if backend == "local":
        root = tmp_path / "root"
        settings = _RelaxedSettings(data_dir=data_dir, storage_root=str(root))  # type: ignore[call-arg]
        yield Env(settings, Workspace.from_settings(settings), lambda key: root / key)
        return
    settings = _RelaxedSettings(data_dir=data_dir, storage_root=_ROOTS[backend])  # type: ignore[call-arg]
    client = mock_cloud_client(backend)
    external = mock_cloud_client(backend, "external-cache")
    with patch("pincer.storage.clients.build_client", return_value=client):
        ws = Workspace.from_settings(settings)
        yield Env(settings, ws, lambda key: external.CloudPath(_ROOTS[backend] + key))


def _put(path: Any, data: bytes | str) -> None:
    """Write an object the way another service would (not through the workspace)."""
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)


# ── Opening ──────────────────────────────────────────────────────────────────


def test_workspace_is_under_the_storage_root(env: Env) -> None:
    assert env.ws.remote.name == "workspace"
    assert str(env.ws.remote.parent).rstrip("/") == str(env.external("")).rstrip("/")
    if env.ws.is_cloud:
        assert env.ws.local != env.ws.remote
    else:
        assert env.ws.local == env.ws.remote


def test_a_local_root_containing_the_data_dir_is_refused(tmp_path: Path) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path))  # type: ignore[call-arg]
    with pytest.raises(StorageUnavailable, match="contains the data dir"):
        Workspace.from_settings(settings)


def test_default_root_is_under_the_data_dir(tmp_path: Path) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    ws = get_workspace(settings)
    assert ws.remote == (tmp_path / "storage" / "workspace").resolve()
    assert get_workspace(settings) is ws


# ── Folders ──────────────────────────────────────────────────────────────────


def test_ensure_creates_the_folders_and_is_idempotent(env: Env) -> None:
    env.ws.ensure()
    env.ws.ensure()
    for sub in ("", "uploads", "downloads", "exec_output", "screenshots"):
        folder = env.external(f"workspace/{sub}" if sub else "workspace")
        assert folder.is_dir()
        if env.ws.is_cloud:
            assert (folder / FOLDER_MARKER).is_file()
        assert (env.ws.local / sub).is_dir()


def test_list_hides_markers(env: Env) -> None:
    env.ws.ensure()
    env.ws.save("uploads", "a.txt", b"abc")
    entries = env.ws.list("uploads")
    assert [(e.name, e.is_dir, e.size) for e in entries] == [("a.txt", False, 3)]
    assert {e.name for e in env.ws.list()} >= {"uploads", "downloads", "exec_output", "screenshots"}


def test_prepare_storage_creates_the_skills_folder_when_defaulted(tmp_path: Path) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    assert settings.skills_dir == tmp_path / "root" / "skills"
    with patch("pincer.storage.workspace.legacy_skills_dir", return_value=tmp_path / "no-legacy"):
        prepare_storage(settings)
    assert (tmp_path / "root" / "skills").is_dir()
    assert (tmp_path / "root" / "workspace" / "uploads").is_dir()


# ── Files ────────────────────────────────────────────────────────────────────


def test_save_puts_the_object_in_the_bucket(env: Env) -> None:
    local = env.ws.save("uploads", "report.pdf", b"%PDF-1")
    assert local.read_bytes() == b"%PDF-1"
    assert local.parent == env.ws.local / "uploads"
    assert env.external("workspace/uploads/report.pdf").read_bytes() == b"%PDF-1"


def test_save_confines_the_filename(env: Env) -> None:
    local = env.ws.save("uploads", "../../../etc/passwd", b"x")
    assert local == env.ws.local / "uploads" / "passwd"
    with pytest.raises(ValueError, match="unsafe"):
        env.ws.save("uploads", "..", b"x")


def test_fetch_picks_up_an_object_changed_behind_the_cache(env: Env) -> None:
    local = env.ws.save("uploads", "notes.txt", b"old")
    later = local.stat().st_mtime + 5
    target = env.external("workspace/uploads/notes.txt")
    _put(target, b"new")
    if isinstance(target, Path):
        os.utime(target, (later, later))
    assert env.ws.fetch(local).read_bytes() == b"new"


def test_fetch_finds_a_file_uploaded_through_documents(env: Env) -> None:
    _put(env.external("workspace/inbox/from-documents.txt"), b"hello")
    local = env.ws.fetch("inbox/from-documents.txt")
    assert local.read_bytes() == b"hello"
    assert env.ws.is_file("inbox/from-documents.txt")


def test_publish_uploads_a_file_written_into_local(env: Env) -> None:
    path = env.ws.local / "exec_output" / "chart.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PNG")
    env.ws.publish(path)
    assert env.external("workspace/exec_output/chart.png").read_bytes() == b"PNG"
    # The published copy is not taken for a local edit on the next read.
    assert env.ws.fetch(path).read_bytes() == b"PNG"


def test_write_text_writes_through(env: Env) -> None:
    target = env.ws.write_text("drafts/a.md", "# hi")
    assert target == (env.ws.local / "drafts" / "a.md").resolve()
    assert env.external("workspace/drafts/a.md").read_text() == "# hi"


@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/passwd", "uploads/../../x"])
def test_to_remote_refuses_an_escape(env: Env, escape: str) -> None:
    with pytest.raises(SandboxDenied):
        env.ws.to_remote(escape)
    assert not env.ws.contains(escape)


def test_files_walks_the_bucket(env: Env) -> None:
    env.ws.ensure()
    _put(env.external("workspace/downloads/deep/r.csv"), b"a,b")
    env.ws.save("uploads", "u.txt", b"u")
    assert sorted(rel for rel, _local in env.ws.files()) == ["downloads/deep/r.csv", "uploads/u.txt"]


# ── Attachments ──────────────────────────────────────────────────────────────


def test_file_attachments_land_in_uploads(env: Env) -> None:
    parts = describe_file_attachments(
        env.ws, [(b"hello", "text/plain", "hi.txt"), (b"\x00\x01", "application/zip", "a.zip")]
    )
    assert "hello" in parts[0]
    assert str(env.ws.local / "uploads" / "a.zip") in parts[1]
    assert env.external("workspace/uploads/hi.txt").read_bytes() == b"hello"
    assert env.external("workspace/uploads/a.zip").read_bytes() == b"\x00\x01"


def test_unsafe_attachment_names_are_rejected(env: Env) -> None:
    parts = describe_file_attachments(env.ws, [(b"x", "text/plain", "..")])
    assert parts == ["[File: ..] rejected — unsafe filename"]


def test_image_attachments_land_in_uploads(env: Env) -> None:
    [part] = describe_image_attachments(env.ws, [(b"PNGDATA", "image/png")])
    [(rel, local)] = [(rel, local) for rel, local in env.ws.files() if rel.startswith("uploads/")]
    assert rel.endswith(".png")
    assert str(local) in part
    assert env.external(f"workspace/{rel}").read_bytes() == b"PNGDATA"


# ── python_exec ──────────────────────────────────────────────────────────────


async def test_python_exec_publishes_its_output(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import python_exec as module

    monkeypatch.setattr(module, "_workspace", lambda: env.ws)
    result = await module.python_exec("open('out.txt', 'w').write('42')")
    assert f"[File saved: {env.ws.local / 'exec_output' / 'out.txt'}]" in result
    assert env.external("workspace/exec_output/out.txt").read_text() == "42"

    # A rerun that changes the file uploads the change, without reporting it as new.
    result = await module.python_exec("open('out.txt', 'w').write('43')")
    assert "[File saved" not in result
    assert env.external("workspace/exec_output/out.txt").read_text() == "43"


# ── Migration ────────────────────────────────────────────────────────────────


def _legacy(tmp_path: Path, data_dir: Path) -> Path:
    (data_dir / "workspace" / "uploads").mkdir(parents=True)
    (data_dir / "workspace" / "uploads" / "a.txt").write_text("a")
    (data_dir / "workspace" / "exec_output").mkdir()
    (data_dir / "workspace" / "exec_output" / "plot.png").write_bytes(b"P")
    (data_dir / "downloads").mkdir()
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    legacy_skills = tmp_path / "home" / ".pincer" / "skills"
    (legacy_skills / "greet").mkdir(parents=True)
    (legacy_skills / "greet" / "SKILL.md").write_text("---\nname: greet\ndescription: hi\n---\nHi")
    return legacy_skills


def test_migration_copies_once_and_renames(env: Env, tmp_path: Path) -> None:
    data_dir = Path(env.settings.data_dir)
    legacy_skills = _legacy(tmp_path, data_dir)
    _put(env.external("workspace/uploads/a.txt"), "already there")

    with patch("pincer.storage.workspace.legacy_skills_dir", return_value=legacy_skills):
        copied = migrate_legacy(env.settings, env.ws)
        assert copied == {"workspace": 1, "downloads": 1, "skills": 1}
        assert env.external("workspace/uploads/a.txt").read_text() == "already there"  # skipped, not overwritten
        assert env.external("workspace/exec_output/plot.png").read_bytes() == b"P"
        assert env.external("workspace/downloads/r.pdf").read_bytes() == b"R"
        assert "name: greet" in env.external("skills/greet/SKILL.md").read_text()
        assert not (data_dir / "workspace").exists()
        assert (data_dir / "workspace.migrated" / "uploads" / "a.txt").is_file()
        assert (data_dir / "downloads.migrated").is_dir()
        assert (legacy_skills.parent / "skills.migrated").is_dir()
        # The skills index is told to rebuild.
        assert (Path(env.settings.skills_cache_dir) / "index-generation").is_file()

        assert migrate_legacy(env.settings, env.ws) == {}


def test_skills_are_not_migrated_over_existing_skills(env: Env, tmp_path: Path) -> None:
    legacy_skills = _legacy(tmp_path, Path(env.settings.data_dir))
    _put(env.external("skills/mine/SKILL.md"), "---\nname: mine\ndescription: m\n---\n")
    with patch("pincer.storage.workspace.legacy_skills_dir", return_value=legacy_skills):
        copied = migrate_legacy(env.settings, env.ws)
    assert "skills" not in copied
    assert legacy_skills.is_dir()


def test_skills_are_not_migrated_with_an_explicit_skills_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = _RelaxedSettings(  # type: ignore[call-arg]
        data_dir=data_dir, storage_root=str(tmp_path / "root"), skills_dir=str(tmp_path / "mine")
    )
    legacy_skills = _legacy(tmp_path, data_dir)
    with patch("pincer.storage.workspace.legacy_skills_dir", return_value=legacy_skills):
        copied = migrate_legacy(settings, Workspace.from_settings(settings))
    assert set(copied) == {"workspace", "downloads"}
    assert legacy_skills.is_dir()


# ── Review fixes ─────────────────────────────────────────────────────────────


async def test_python_exec_publishes_output_of_a_run_that_times_out(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import python_exec as module

    monkeypatch.setattr(module, "_workspace", lambda: env.ws)
    code = "import time\nopen('partial.csv', 'w').write('1')\ntime.sleep(30)"
    result = await module.python_exec(code, timeout=2)
    assert "timed out" in result
    assert env.external("workspace/exec_output/partial.csv").read_text() == "1"


async def test_python_exec_sees_files_uploaded_to_exec_output(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import python_exec as module

    monkeypatch.setattr(module, "_workspace", lambda: env.ws)
    env.ws.ensure()
    _put(env.external("workspace/exec_output/input.csv"), b"a,b")
    result = await module.python_exec("print(open('input.csv').read())")
    assert "a,b" in result


def test_sync_drops_cached_files_whose_object_is_gone(env: Env) -> None:
    local = env.ws.save("exec_output", "old.png", b"P")
    env.external("workspace/exec_output/old.png").unlink()
    env.ws.sync("exec_output")
    assert not local.exists()


def test_fetch_drops_a_cached_copy_whose_object_is_gone(env: Env) -> None:
    local = env.ws.save("uploads", "gone.pdf", b"%PDF")
    env.external("workspace/uploads/gone.pdf").unlink()
    assert not env.ws.fetch(local).exists()


def test_file_size(env: Env) -> None:
    env.ws.save("uploads", "a.bin", b"12345")
    assert env.ws.file_size("uploads/a.bin") == 5
    assert env.ws.file_size("uploads/missing.bin") is None
    if not env.ws.is_cloud:
        # A real bucket answers a folder prefix with NoStatError (None here); cloudpathlib's
        # local mocks stat it like a directory on disk, so only the local root is checked.
        assert env.ws.file_size("uploads") is None


class _BrokenWorkspace:
    def save(self, *_args: Any) -> Path:
        raise OSError("bucket unreachable")


def test_a_storage_error_does_not_drop_the_message() -> None:
    ws: Any = _BrokenWorkspace()
    [text, binary] = describe_file_attachments(
        ws, [(b"hello", "text/plain", "hi.txt"), (b"\x00", "application/zip", "a.zip")]
    )
    assert "hello" in text  # text is still inlined
    assert "could not be saved" in binary
    [image] = describe_image_attachments(ws, [(b"PNG", "image/png")])
    assert "could not be saved" in image


def test_migration_covers_the_legacy_home_folders(env: Env, tmp_path: Path) -> None:
    home = tmp_path / "home" / ".pincer"
    (home / "workspace" / "exec_output").mkdir(parents=True)
    (home / "workspace" / "exec_output" / "plot.png").write_bytes(b"P")
    (home / "downloads").mkdir()
    (home / "downloads" / "drive.pdf").write_bytes(b"D")
    with patch("pincer.storage.workspace.legacy_home_dir", return_value=home):
        copied = migrate_legacy(env.settings, env.ws)
    assert copied == {"workspace": 1, "downloads": 1}
    assert env.external("workspace/exec_output/plot.png").read_bytes() == b"P"
    assert env.external("workspace/downloads/drive.pdf").read_bytes() == b"D"
    assert (home / "workspace.migrated").is_dir()


def test_the_legacy_home_is_not_migrated_twice_when_it_is_the_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / ".pincer"
    (data_dir / "downloads").mkdir(parents=True)
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    settings = _RelaxedSettings(data_dir=data_dir, storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    with patch("pincer.storage.workspace.legacy_home_dir", return_value=data_dir):
        assert migrate_legacy(settings, Workspace.from_settings(settings)) == {"downloads": 1}


def test_a_source_that_cannot_be_renamed_is_not_copied_again(env: Env) -> None:
    data_dir = Path(env.settings.data_dir)
    (data_dir / "downloads").mkdir(parents=True)
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    with patch("pincer.storage.workspace._mark_migrated", side_effect=OSError(16, "Device or resource busy")):
        assert migrate_legacy(env.settings, env.ws) == {"downloads": 1}
        # The user deletes the migrated file; the next start must not bring it back.
        env.external("workspace/downloads/r.pdf").unlink()
        assert migrate_legacy(env.settings, env.ws) == {}
    assert not env.external("workspace/downloads/r.pdf").exists()
    assert (data_dir / "storage-migration.json").is_file()


def test_prepare_storage_without_migration(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "downloads").mkdir(parents=True)
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    settings = _RelaxedSettings(data_dir=data_dir, storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    prepare_storage(settings, migrate=False)
    assert (tmp_path / "root" / "workspace" / "downloads").is_dir()
    assert (data_dir / "downloads" / "r.pdf").is_file()
    assert not (tmp_path / "root" / "workspace" / "downloads" / "r.pdf").exists()


async def test_an_unavailable_root_exits_with_a_message(tmp_path: Path) -> None:
    import typer

    from pincer.cli.run import _prepare_storage_or_exit

    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path))  # type: ignore[call-arg]
    with pytest.raises(typer.Exit):
        await _prepare_storage_or_exit(settings, migrate=False)


def test_mcp_file_paths_under_uploads_are_refreshed(env: Env) -> None:
    from pincer.mcp.bridge import _refresh_local_file_paths

    local = env.ws.save("uploads", "scan.png", b"old")
    later = local.stat().st_mtime + 5
    target = env.external("workspace/uploads/scan.png")
    _put(target, b"new")
    if isinstance(target, Path):
        os.utime(target, (later, later))
    args = {"file_path": str(local), "nested": [{"file_path": "/etc/hosts"}]}
    _refresh_local_file_paths(args, env.ws.local / "uploads", env.ws.fetch)
    assert local.read_bytes() == b"new"


def test_storage_root_for(tmp_path: Path) -> None:
    from pincer.storage.layout import storage_root_for

    assert storage_root_for(_RelaxedSettings(data_dir=tmp_path, storage_root="s3://b/")) == "s3://b/"  # type: ignore[call-arg]
    assert storage_root_for(_RelaxedSettings(data_dir=tmp_path, skills_dir="gs://g/team")) == "gs://g/"  # type: ignore[call-arg]
    assert storage_root_for(_RelaxedSettings(data_dir=tmp_path)) == tmp_path / "storage"  # type: ignore[call-arg]


# ── Coverage: file tools on every backend ────────────────────────────────────


@pytest.fixture
def file_tools(env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:
    from pincer.tools.builtin import files

    monkeypatch.setattr(files, "_workspace", lambda: env.ws)
    env.ws.ensure()
    return files


async def test_file_write_then_read(env: Env, file_tools: Any) -> None:
    assert "Successfully wrote 5 chars" in await file_tools.file_write("notes/a.txt", "hello")
    assert env.external("workspace/notes/a.txt").read_text() == "hello"
    result = await file_tools.file_read("notes/a.txt")
    assert result == "File: a.txt (5 chars)\n\nhello"


async def test_file_read_errors(env: Env, file_tools: Any) -> None:
    assert "File not found" in await file_tools.file_read("missing.txt")
    if not env.ws.is_cloud:  # cloudpathlib's mocks stat a folder like a file (real buckets don't)
        assert "Not a file" in await file_tools.file_read("uploads")
    env.ws.save("uploads", "big.txt", b"x" * (file_tools.MAX_READ_SIZE + 1))
    assert "File too large" in await file_tools.file_read("uploads/big.txt")


async def test_file_read_reports_a_failed_download(env: Env, file_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env.ws.save("uploads", "a.txt", b"a")
    monkeypatch.setattr(env.ws, "fetch", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    assert "Error reading file: boom" in await file_tools.file_read("uploads/a.txt")


async def test_file_write_reports_a_failed_upload(env: Env, file_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env.ws, "write_text", lambda *_a: (_ for _ in ()).throw(OSError("denied")))
    assert "Error writing file: denied" in await file_tools.file_write("a.txt", "x")


async def test_file_list(env: Env, file_tools: Any) -> None:
    env.ws.save("uploads", "small.txt", b"x" * 10)
    env.ws.save("uploads", "mid.bin", b"x" * 2048)
    env.ws.save("uploads", "big.bin", b"x" * (2 * 1024 * 1024))
    result = await file_tools.file_list("uploads")
    assert result.startswith("Contents of uploads/:")
    assert "small.txt (10B)" in result
    assert "mid.bin (2.0KB)" in result
    assert "big.bin (2.0MB)" in result
    assert "  uploads/" in await file_tools.file_list()


async def test_file_list_errors(env: Env, file_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Directory not found" in await file_tools.file_list("nowhere")
    env.ws.save("uploads", "a.txt", b"a")
    assert "Not a directory" in await file_tools.file_list("uploads/a.txt")
    assert "Directory is empty" in await file_tools.file_list("screenshots")
    monkeypatch.setattr(env.ws, "list", lambda *_a: (_ for _ in ()).throw(PermissionError()))
    assert "Permission denied" in await file_tools.file_list("uploads")


def test_file_tools_use_the_configured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import files

    settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr(files, "get_settings", lambda: settings)
    assert files._workspace() is get_workspace(settings)


# ── Coverage: PDF attachments ────────────────────────────────────────────────


def _pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_pdf_attachment_text_is_extracted(env: Env) -> None:
    text = "Quarterly revenue grew twelve percent over the previous year"
    [part] = describe_file_attachments(env.ws, [(_pdf(text), "application/pdf", "q.pdf")])
    assert text in part
    assert str(env.ws.local / "uploads" / "q.pdf") in part


def test_pdf_attachment_without_pymupdf(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pymupdf", None)
    [part] = describe_file_attachments(env.ws, [(b"%PDF", "application/pdf", "q.pdf")])
    assert "PDF text extraction unavailable" in part


def test_pdf_attachment_that_cannot_be_parsed(env: Env) -> None:
    [part] = describe_file_attachments(env.ws, [(b"not a pdf", "application/pdf", "bad.pdf")])
    assert "PDF extraction failed" in part


def test_binary_attachment_names_its_path(env: Env) -> None:
    [part] = describe_file_attachments(env.ws, [(b"\x00", "application/zip", "a.zip")])
    assert "Use shell_exec" in part


# ── Coverage: python_exec edge cases ─────────────────────────────────────────


@pytest.fixture
def py(env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:
    from pincer.tools.builtin import python_exec as module

    monkeypatch.setattr(module, "_workspace", lambda: env.ws)
    return module


async def test_python_exec_rejects_empty_code(py: Any) -> None:
    assert await py.python_exec("  ") == "Error: No code provided."


async def test_python_exec_reports_stderr_exit_code_and_truncates(py: Any) -> None:
    assert "[stderr]" in await py.python_exec("import sys; sys.stderr.write('oops')")
    assert "[Process exited with code 3]" in await py.python_exec("raise SystemExit(3)")
    result = await py.python_exec(f"print('x' * {py.MAX_OUTPUT_LENGTH + 100})")
    assert result.endswith("...[output truncated]")
    assert await py.python_exec("pass") == "(no output)"


async def test_python_exec_runs_on_the_cache_when_sync_fails(
    env: Env, py: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.ws, "sync", lambda _sub: (_ for _ in ()).throw(OSError("offline")))
    assert "42" in await py.python_exec("print(42)")


async def test_python_exec_reports_a_file_that_failed_to_upload_and_retries_it(
    env: Env, py: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not env.ws.is_cloud:
        pytest.skip("nothing is uploaded on a local root")
    local = env.ws.local / "exec_output" / "r.txt"
    with monkeypatch.context() as m:
        m.setattr(env.ws, "publish", lambda _p: (_ for _ in ()).throw(OSError("offline")))
        result = await py.python_exec("open('r.txt', 'w').write('1')")
        assert f"[File NOT uploaded: {local} (kept locally, retried on the next run)]" in result
        assert "[File saved:" not in result
        # Still offline on the next run: the file is not pruned by its sync.
        result = await py.python_exec("print(open('r.txt').read())")
        assert "1" in result
    assert local.is_file()
    assert not env.external("workspace/exec_output/r.txt").exists()
    # Back online: the next run uploads it.
    await py.python_exec("print('hi')")
    assert env.external("workspace/exec_output/r.txt").read_text() == "1"
    assert not env.ws.pending


async def test_python_exec_on_a_local_root_does_not_track_uploads(env: Env, py: Any) -> None:
    if env.ws.is_cloud:
        pytest.skip("local root only")
    result = await py.python_exec("open('r.txt', 'w').write('1')")
    assert "[File saved:" in result
    assert not env.ws.pending


async def test_concurrent_python_exec_runs_do_not_prune_each_other(env: Env, py: Any) -> None:
    import asyncio

    slow = "import time\nopen('a.csv', 'w').write('x,y')\ntime.sleep(1.5)\nprint(open('a.csv').read())"
    fast = "print('b')"

    async def later() -> str:
        await asyncio.sleep(0.5)
        return await py.python_exec(fast)

    a, b = await asyncio.gather(py.python_exec(slow), later())
    assert "x,y" in a
    assert "Traceback" not in a
    assert "b" in b
    assert env.external("workspace/exec_output/a.csv").read_text() == "x,y"


async def test_a_file_deleted_by_a_script_is_removed_from_the_workspace(env: Env, py: Any) -> None:
    await py.python_exec("open('gone.txt', 'w').write('1')")
    assert env.external("workspace/exec_output/gone.txt").exists()
    await py.python_exec("import os\nos.remove('gone.txt')")
    assert not env.external("workspace/exec_output/gone.txt").exists()
    result = await py.python_exec("import os\nprint(os.path.exists('gone.txt'))")
    assert "False" in result


async def test_a_failed_delete_is_logged(
    env: Env, py: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await py.python_exec("open('gone.txt', 'w').write('1')")
    monkeypatch.setattr(env.ws, "remove", lambda _p: (_ for _ in ()).throw(OSError("offline")))
    await py.python_exec("import os\nos.remove('gone.txt')")
    if env.ws.is_cloud:
        assert "Could not delete" in caplog.text


def test_sync_and_fetch_keep_pending_files(env: Env) -> None:
    if not env.ws.is_cloud:
        pytest.skip("sync and fetch only touch the cache of a cloud root")
    path = env.ws.local / "exec_output" / "p.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unpublished")
    env.ws.mark_pending(path)
    assert env.ws.is_pending(path)
    env.ws.sync("exec_output")
    assert path.read_text() == "unpublished"
    assert env.ws.fetch(path) == path
    assert path.read_text() == "unpublished"
    # An object changed behind the cache does not overwrite the pending copy either.
    _put(env.external("workspace/exec_output/p.txt"), "remote")
    env.ws.sync("exec_output")
    assert path.read_text() == "unpublished"
    env.ws.publish(path)
    assert not env.ws.is_pending(path)
    assert env.external("workspace/exec_output/p.txt").read_text() == "unpublished"


def test_retry_pending_drops_files_gone_locally_and_reports_failures(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = env.ws.local / "exec_output" / "gone.txt"
    stuck = env.ws.local / "exec_output" / "stuck.txt"
    stuck.parent.mkdir(parents=True, exist_ok=True)
    stuck.write_text("s")
    env.ws.mark_pending(gone)
    env.ws.mark_pending(stuck)
    monkeypatch.setattr(env.ws, "publish", lambda _p: (_ for _ in ()).throw(OSError("offline")))
    assert env.ws.retry_pending("exec_output") == [stuck]
    assert env.ws.pending == {"exec_output/stuck.txt"}


def test_remove_deletes_the_object(env: Env) -> None:
    path = env.ws.save("exec_output", "x.txt", b"x")
    env.ws.mark_pending(path)
    if not env.ws.is_cloud:
        path.unlink()
    env.ws.remove(path)
    assert not env.external("workspace/exec_output/x.txt").exists()
    assert not env.ws.pending
    env.ws.remove(path)  # already gone: no error


async def test_python_exec_reports_a_subprocess_that_cannot_start(py: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_a: Any, **_k: Any) -> Any:
        raise OSError("no python")

    monkeypatch.setattr(py.asyncio, "create_subprocess_exec", fail)
    assert "Error starting subprocess: OSError: no python" in await py.python_exec("print(1)")


def test_snapshot_skips_files_it_cannot_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import python_exec as module

    (tmp_path / "a.txt").write_text("a")
    real_is_file = Path.is_file

    def flaky(self: Path) -> bool:
        if self.name == "a.txt":
            raise OSError("gone")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flaky)
    assert module._snapshot(tmp_path) == {}


def test_python_exec_uses_the_configured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.tools.builtin import python_exec as module

    settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    assert module._workspace() is get_workspace(settings)


# ── Coverage: workspace edge cases ───────────────────────────────────────────


def test_ensure_sibling(env: Env) -> None:
    folder = env.ws.ensure_sibling("skills")
    assert folder.is_dir()
    if env.ws.is_cloud:
        assert (folder / FOLDER_MARKER).is_file()


def test_sizes_reports_zero_for_unreadable_files(env: Env) -> None:
    env.ws.save("uploads", "a.txt", b"abc")
    assert env.ws.sizes(["uploads/a.txt", "uploads/missing.txt"]) == [3, 0]


def test_files_skips_hidden_entries(env: Env) -> None:
    env.ws.ensure()
    _put(env.external("workspace/.hidden/secret.txt"), b"s")
    _put(env.external("workspace/uploads/.dotfile"), b"d")
    env.ws.save("uploads", "seen.txt", b"x")
    if not env.ws.is_cloud:
        (env.ws.local / "uploads" / "linked").symlink_to(env.ws.local / "downloads", target_is_directory=True)
    assert [rel for rel, _local in env.ws.files()] == ["uploads/seen.txt"]


def test_prepare_storage_wraps_unexpected_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    monkeypatch.setattr(Workspace, "ensure", lambda _self: (_ for _ in ()).throw(PermissionError("read-only")))
    with pytest.raises(StorageUnavailable, match="Cannot prepare the storage root .*read-only"):
        prepare_storage(settings)


def test_prepare_storage_passes_storage_errors_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    monkeypatch.setattr(Workspace, "ensure", lambda _self: (_ for _ in ()).throw(StorageUnavailable("down")))
    with pytest.raises(StorageUnavailable, match="^down$"):
        prepare_storage(settings)


def test_legacy_home_dir_is_under_the_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _REAL_LEGACY_HOME_DIR() == tmp_path / ".pincer"


def test_a_failed_copy_is_retried_at_the_next_start(env: Env) -> None:
    data_dir = Path(env.settings.data_dir)
    (data_dir / "downloads").mkdir(parents=True)
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    with patch("pincer.storage.workspace._copy_tree", side_effect=OSError("bucket down")):
        assert migrate_legacy(env.settings, env.ws) == {}
    assert (data_dir / "downloads").is_dir()
    assert migrate_legacy(env.settings, env.ws) == {"downloads": 1}


def test_an_unreadable_migration_state_is_ignored(env: Env) -> None:
    data_dir = Path(env.settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "storage-migration.json").write_text("{not json")
    (data_dir / "downloads").mkdir()
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    assert migrate_legacy(env.settings, env.ws) == {"downloads": 1}


def test_has_skills_on_a_missing_folder() -> None:
    from pincer.storage.workspace import _has_skills

    class Missing:
        def glob(self, _pattern: str) -> Any:
            raise FileNotFoundError

    assert _has_skills(Missing()) is False  # type: ignore[arg-type]


def test_migration_skips_symlinks_and_picks_a_free_name(env: Env, tmp_path: Path) -> None:
    data_dir = Path(env.settings.data_dir)
    (data_dir / "downloads").mkdir(parents=True)
    (data_dir / "downloads" / "r.pdf").write_bytes(b"R")
    (tmp_path / "outside.txt").write_text("secret")
    (data_dir / "downloads" / "link.txt").symlink_to(tmp_path / "outside.txt")
    (data_dir / "downloads.migrated").mkdir()  # taken by an earlier migration
    assert migrate_legacy(env.settings, env.ws) == {"downloads": 1}
    assert not env.external("workspace/downloads/link.txt").exists()
    assert (data_dir / "downloads.migrated.2" / "r.pdf").is_file()


# ── Coverage: Drive / Gmail lookups ──────────────────────────────────────────


def test_google_tools_use_the_configured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.integrations.google import workspace_files

    settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr("pincer.config.get_settings", lambda: settings)
    assert workspace_files._workspace() is get_workspace(settings)


def test_upload_lookup_by_absolute_workspace_path_and_desktop(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pincer.integrations.google import workspace_files

    monkeypatch.setattr(workspace_files, "_workspace", lambda: env.ws)
    local = env.ws.save("downloads", "r.pdf", b"R")
    assert workspace_files.resolve_upload_path(str(local)) == str(local)

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "slides.pptx").write_bytes(b"S")
    monkeypatch.setattr(
        workspace_files.os.path,
        "expanduser",
        lambda p: p.replace("~/Desktop", str(desktop)).replace("~", "/nonexistent"),
    )
    assert workspace_files.resolve_upload_path("slides.pptx") == str(desktop / "slides.pptx")


# ── Coverage: run.py helpers ─────────────────────────────────────────────────


def test_banner_labels_and_workspace_helpers(tmp_path: Path) -> None:
    from pincer.cli.run import _storage_root_label, _workspace_fetch, _workspace_local

    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root="s3://b/")  # type: ignore[call-arg]
    assert _storage_root_label(settings) == "s3://b/"
    assert _storage_root_label(settings, "workspace") == "s3://b/workspace"

    local = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    ws_dir = _workspace_local(local)
    assert ws_dir == (tmp_path / "root" / "workspace").resolve()
    (ws_dir / "a.txt").write_text("a")
    assert _workspace_fetch(local)(ws_dir / "a.txt") == ws_dir / "a.txt"


async def test_send_file_refreshes_workspace_files(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from pincer.cli.run import _register_channel_bound_tools
    from pincer.tools.registry import ToolRegistry

    monkeypatch.setattr("pincer.config.get_settings", lambda: env.settings)
    monkeypatch.setattr("pincer.storage.workspace.get_workspace", lambda _s: env.ws)
    local = env.ws.save("exec_output", "chart.png", b"PNG")
    channel = MagicMock(send_file=AsyncMock())
    registry = ToolRegistry()
    _register_channel_bound_tools(registry, {"telegram": channel})

    context = {"user_id": "u1", "channel": "telegram"}
    assert await registry.execute("send_file", {"path": str(local)}, context) == "File sent: chart.png"
    channel.send_file.assert_awaited_once_with("u1", str(local), "")

    env.external("workspace/exec_output/chart.png").unlink()
    result = await registry.execute("send_file", {"path": str(local)}, context)
    assert result.startswith("Error: File not found")


# ── Coverage: browser screenshots ────────────────────────────────────────────


async def test_screenshot_is_saved_to_the_workspace(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from pincer.tools.builtin import browser

    page = MagicMock(goto=AsyncMock(), screenshot=AsyncMock(return_value=b"PNGDATA"), close=AsyncMock())
    fake_browser = MagicMock(new_page=AsyncMock(return_value=page))
    monkeypatch.setattr(browser, "_ensure_browser", AsyncMock(return_value=fake_browser))
    monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("pincer.config.get_settings", lambda: env.settings)
    monkeypatch.setattr("pincer.storage.workspace.get_workspace", lambda _s: env.ws)

    result = await browser.screenshot("https://example.com")
    assert "Screenshot saved to:" in result
    [(rel, _local)] = list(env.ws.files())
    assert rel.startswith("screenshots/screenshot_") and rel.endswith(".png")
    assert env.external(f"workspace/{rel}").read_bytes() == b"PNGDATA"


# ── Second review ────────────────────────────────────────────────────────────


def test_prepare_storage_on_an_unreachable_root(tmp_path: Path) -> None:
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root="/nonexistent/root")  # type: ignore[call-arg]
    with pytest.raises(StorageUnavailable, match="/nonexistent/root"):
        prepare_storage(settings)


def test_prepare_storage_on_a_root_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "root").write_text("not a folder")
    settings = _RelaxedSettings(data_dir=tmp_path / "data", storage_root=str(tmp_path / "root"))  # type: ignore[call-arg]
    with pytest.raises(StorageUnavailable, match="storage root"):
        prepare_storage(settings)


def test_copy_tree_leaves_no_truncated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.storage.workspace import _copy_tree

    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    (source / "big.bin").write_bytes(b"x" * 1000)

    def broken_copy(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"x" * 10)
        raise OSError("disk full")

    monkeypatch.setattr("pincer.storage.workspace.shutil.copy2", broken_copy)
    with pytest.raises(OSError, match="disk full"):
        _copy_tree(source, target)
    assert not (target / "big.bin").exists()
    assert list(target.iterdir()) == []
    monkeypatch.undo()
    assert _copy_tree(source, target) == (1, 0, [])
    assert (target / "big.bin").read_bytes() == b"x" * 1000


def test_migration_names_the_symlinks_it_leaves_behind(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = Path(env.settings.data_dir)
    downloads = data_dir / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "r.pdf").write_bytes(b"R")
    (tmp_path / "outside.txt").write_text("secret")
    (tmp_path / "my-skill").mkdir()
    (tmp_path / "my-skill" / "SKILL.md").write_text("---\nname: s\n---\n")
    (downloads / "link.txt").symlink_to(tmp_path / "outside.txt")
    (downloads / "skill-link").symlink_to(tmp_path / "my-skill", target_is_directory=True)
    with caplog.at_level("INFO", logger="pincer.storage.workspace"):
        assert migrate_legacy(env.settings, env.ws) == {"downloads": 1}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "link.txt" in warnings[0]
    assert "skill-link" in warnings[0]
    assert "downloads.migrated" in warnings[0]
    assert "2 not copied" in caplog.text
    assert not env.external("workspace/downloads/skill-link/SKILL.md").exists()


def test_name_some_caps_the_list() -> None:
    from pincer.storage.workspace import _name_some

    names = [f"f{i}" for i in range(25)]
    text = _name_some(names)
    assert text.endswith("f19 and 5 more")
    assert "f20" not in text
    assert _name_some(["a", "b"]) == "a, b"


@pytest.mark.parametrize("name", ["..", ".", "", "a\0b", "\x01\x02"])
def test_save_download_cleans_unsafe_names(env: Env, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from pincer.integrations.google import workspace_files

    monkeypatch.setattr(workspace_files, "_workspace", lambda: env.ws)
    path = Path(workspace_files.save_download(name, b"data"))
    assert path.parent == env.ws.local / "downloads"
    assert "\0" not in path.name
    assert path.name not in ("", ".", "..")
    if name == "a\0b":
        assert path.name == "ab"
    else:
        assert path.name.startswith("attachment_")
    assert env.external(f"workspace/downloads/{path.name}").read_bytes() == b"data"


def test_concurrent_downloads_of_one_name_get_distinct_paths(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from pincer.integrations.google import workspace_files

    monkeypatch.setattr(workspace_files, "_workspace", lambda: env.ws)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda i: workspace_files.save_download("r.pdf", str(i).encode()), range(8)))
    assert len(set(paths)) == 8
    contents = {env.external(f"workspace/downloads/{Path(p).name}").read_bytes() for p in paths}
    assert contents == {str(i).encode() for i in range(8)}

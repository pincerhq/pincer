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
from pincer.storage.errors import StorageUnavailable
from pincer.storage.keys import FOLDER_MARKER
from pincer.storage.workspace import Workspace, get_workspace, migrate_legacy, prepare_storage
from pincer.tools.path_sandbox import SandboxDenied

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

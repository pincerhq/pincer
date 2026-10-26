"""Tests for src/pincer/api/storage.py — the storage browser's HTTP routes.

The browser itself (protection, moves, concurrent writes, the cloud SDK
writes) is covered in test_storage_browser.py; root resolution and settings in
test_storage_settings.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pincer.api.auth import current_identity
from pincer.api.server import create_app
from pincer.api.storage import storage_browser
from pincer.config import get_settings_relaxed
from pincer.services.auth import AuthIdentity
from pincer.storage import browser as storage_browser_module
from pincer.storage.browser import StorageBrowser

_BUCKETS = {"s3": "s3://docs-bucket", "azure": "az://docs-container", "gs": "gs://docs-bucket"}


@dataclass
class Storage:
    client: TestClient
    browser: StorageBrowser
    #: The storage root, for seeding and checking files behind the API.
    root: Any
    backend: str


@pytest.fixture(params=["local", "s3", "azure", "gs"])
def storage(request: pytest.FixtureRequest, tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> Iterator[Storage]:
    """The API over a storage root on every backend, signed in as alice, with
    the skills dir protected at `team/skills` under the root."""
    backend = request.param
    if backend == "local":
        root: Any = (tmp_path / "root").resolve()
        root.mkdir()
    else:
        root = mock_cloud_client(backend, file_cache_mode="close_file").CloudPath(_BUCKETS[backend] + "/")
    browser = StorageBrowser(root, ["team/skills"], 900, b"k" * 32)

    app = create_app()
    app.dependency_overrides[storage_browser] = lambda: browser
    app.dependency_overrides[current_identity] = lambda: AuthIdentity("alice", "jwt")
    yield Storage(TestClient(app), browser, root, backend)


@pytest.fixture
def local_storage(storage: Storage) -> Storage:
    if storage.backend != "local":
        pytest.skip("local roots only")
    return storage


@pytest.fixture
def cloud_storage(storage: Storage) -> Storage:
    if storage.backend == "local":
        pytest.skip("cloud roots only")
    return storage


def _write(root: Any, key: str, data: bytes | str) -> None:
    path = root / key
    if isinstance(root, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)


# ── Info and listing ─────────────────────────────────────────────────────────


def test_info_reports_backend_root_and_protected_prefix(storage: Storage) -> None:
    body = storage.client.get("/api/storage").json()
    assert body["backend"] == {"local": "file", "s3": "s3", "azure": "az", "gs": "gs"}[storage.backend]
    assert body["protected"] == ["team/skills"]
    assert body["presign_ttl"] == 900
    if storage.backend != "local":
        assert body["root"].rstrip("/") == _BUCKETS[storage.backend]


def test_list_empty_root(storage: Storage) -> None:
    body = storage.client.get("/api/storage/list").json()
    assert body == {"prefix": "", "folders": [], "files": [], "next_cursor": None, "truncated": False}


def test_list_folders_and_files(storage: Storage) -> None:
    _write(storage.root, "docs/b.txt", "bb")
    _write(storage.root, "docs/A.md", "# a")
    _write(storage.root, "docs/sub/c.txt", "c")
    _write(storage.root, "team/skills/demo/SKILL.md", "x")

    root = storage.client.get("/api/storage/list").json()
    assert [(f["key"], f["protected"]) for f in root["folders"]] == [("docs", False), ("team", True)]

    body = storage.client.get("/api/storage/list", params={"prefix": "docs/"}).json()
    assert body["prefix"] == "docs"
    assert [f["key"] for f in body["folders"]] == ["docs/sub"]
    files = {f["name"]: f for f in body["files"]}
    assert list(files) == ["A.md", "b.txt"]
    assert files["b.txt"]["size"] == 2
    assert files["b.txt"]["content_type"] == "text/plain"
    assert files["A.md"]["content_type"] == "text/markdown"
    assert files["b.txt"]["last_modified"]
    assert not files["b.txt"]["protected"]


def test_list_missing_folder_is_404(storage: Storage) -> None:
    assert storage.client.get("/api/storage/list", params={"prefix": "nope"}).status_code == 404


def test_list_is_capped(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_browser_module, "MAX_LIST_ENTRIES", 2)
    for name in ("a", "b", "c"):
        _write(storage.root, f"many/{name}.txt", name)
    body = storage.client.get("/api/storage/list", params={"prefix": "many"}).json()
    assert body["truncated"] is True
    assert len(body["files"]) == 2
    assert body["next_cursor"] is None


def _all_pages(storage: Storage, prefix: str, limit: int | None = None) -> list[dict[str, Any]]:
    pages, cursor = [], None
    while True:
        params: dict[str, Any] = {"prefix": prefix, "cursor": cursor, "limit": limit}
        res = storage.client.get("/api/storage/list", params={k: v for k, v in params.items() if v is not None})
        assert res.status_code == 200, res.text
        pages.append(res.json())
        cursor = pages[-1]["next_cursor"]
        if cursor is None:
            return pages


def test_list_pages_folders_first_then_files(storage: Storage) -> None:
    for name in ("b", "A", "c"):
        _write(storage.root, f"p/{name}-dir/x.txt", "x")
    for name in ("z.txt", "Y.txt", "m.txt", "a.txt"):
        _write(storage.root, f"p/{name}", "f")

    pages = _all_pages(storage, "p", limit=2)
    assert [len(p["folders"]) + len(p["files"]) for p in pages] == [2, 2, 2, 1]
    names = [e["name"] for p in pages for e in (*p["folders"], *p["files"])]
    assert names == ["A-dir", "b-dir", "c-dir", "a.txt", "m.txt", "Y.txt", "z.txt"]
    # The page that crosses from folders to files carries both.
    assert [f["name"] for f in pages[1]["folders"]] == ["c-dir"]
    assert [f["name"] for f in pages[1]["files"]] == ["a.txt"]


def test_list_default_and_max_page_size(storage: Storage) -> None:
    for i in range(60):
        _write(storage.root, f"big/f{i:03}.txt", "x")
    first = storage.client.get("/api/storage/list", params={"prefix": "big"}).json()
    assert len(first["files"]) == 50
    assert first["next_cursor"]
    rest = storage.client.get("/api/storage/list", params={"prefix": "big", "cursor": first["next_cursor"]}).json()
    assert [f["name"] for f in rest["files"]] == [f"f{i:03}.txt" for i in range(50, 60)]
    assert rest["next_cursor"] is None

    assert storage.client.get("/api/storage/list", params={"prefix": "big", "limit": 100}).status_code == 200
    assert storage.client.get("/api/storage/list", params={"prefix": "big", "limit": 101}).status_code == 422
    assert storage.client.get("/api/storage/list", params={"prefix": "big", "limit": 0}).status_code == 422


def test_list_cursor_survives_changes_between_pages(storage: Storage) -> None:
    for name in ("a", "b", "c", "d"):
        _write(storage.root, f"live/{name}.txt", name)
    first = storage.client.get("/api/storage/list", params={"prefix": "live", "limit": 2}).json()
    assert [f["name"] for f in first["files"]] == ["a.txt", "b.txt"]
    # A file before the cursor is deleted, one after it is added.
    (storage.root / "live" / "a.txt").unlink()
    _write(storage.root, "live/bb.txt", "bb")
    rest = storage.client.get("/api/storage/list", params={"prefix": "live", "cursor": first["next_cursor"]}).json()
    assert [f["name"] for f in rest["files"]] == ["bb.txt", "c.txt", "d.txt"]


def test_list_rejects_a_bad_cursor(storage: Storage) -> None:
    _write(storage.root, "c/a.txt", "a")
    assert storage.client.get("/api/storage/list", params={"prefix": "c", "cursor": "%%%"}).status_code == 400
    assert storage.client.get("/api/storage/list", params={"prefix": "c", "cursor": "WzJd"}).status_code == 400


# ── Upload and download ──────────────────────────────────────────────────────


def _upload(storage: Storage, prefix: str, *files: tuple[str, bytes], overwrite: bool = False) -> Any:
    return storage.client.post(
        "/api/storage/upload",
        params={"prefix": prefix, "overwrite": overwrite},
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )


def test_upload_then_download(storage: Storage) -> None:
    res = _upload(storage, "in", ("a.bin", b"\x00\x01"), ("nested/b.txt", b"bee"))
    assert res.status_code == 200, res.text
    assert [f["key"] for f in res.json()["files"]] == ["in/a.bin", "in/nested/b.txt"]
    assert (storage.root / "in" / "nested" / "b.txt").read_bytes() == b"bee"

    dl = storage.client.get("/api/storage/download", params={"key": "in/a.bin"})
    assert dl.status_code == 200
    assert dl.content == b"\x00\x01"
    assert dl.headers["content-disposition"] == "attachment; filename*=utf-8''a.bin"
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in dl.headers["content-security-policy"]


def test_upload_conflict_and_overwrite(storage: Storage) -> None:
    _write(storage.root, "in/a.txt", "old")
    assert _upload(storage, "in", ("a.txt", b"new")).status_code == 409
    assert (storage.root / "in" / "a.txt").read_text() == "old"
    assert _upload(storage, "in", ("a.txt", b"new"), overwrite=True).status_code == 200
    assert (storage.root / "in" / "a.txt").read_text() == "new"


def test_upload_over_the_size_limit_is_413_and_stores_nothing(storage: Storage) -> None:
    storage.browser.upload_max_bytes = 1024
    assert storage.client.get("/api/storage").json()["max_upload_bytes"] == 1024

    assert _upload(storage, "in", ("ok.bin", b"x" * 1024)).status_code == 200
    res = _upload(storage, "in", ("big.bin", b"x" * 1025))
    assert res.status_code == 413
    assert "1024 bytes" in res.json()["detail"]
    assert not (storage.root / "in" / "big.bin").exists()


@pytest.mark.parametrize(
    ("files", "status"),
    [
        ([("first.txt", b"1"), ("exists.txt", b"2")], 409),
        ([("first.txt", b"1"), ("big.bin", b"x" * 1025)], 413),
        ([("first.txt", b"1"), ("first.txt", b"1 again")], 409),
        ([("first.txt", b"1"), ("../escape.txt", b"2")], 400),
    ],
    ids=["exists", "too-large", "twice", "bad-name"],
)
def test_upload_refused_late_in_the_request_stores_nothing(
    storage: Storage, files: list[tuple[str, bytes]], status: int
) -> None:
    """Every file is checked before any is written, so a refusal of a later
    file does not leave the earlier ones stored."""
    storage.browser.upload_max_bytes = 1024
    _write(storage.root, "in/exists.txt", "old")
    assert _upload(storage, "in", *files).status_code == status
    assert not (storage.root / "in" / "first.txt").exists()
    assert (storage.root / "in" / "exists.txt").read_text() == "old"


@pytest.mark.parametrize(
    "files",
    [[("ok.txt", b"1"), ("a.txt/b.txt", b"2")], [("ok.txt", b"1"), ("x", b"2"), ("x/y", b"3")]],
    ids=["under-an-existing-file", "file-and-folder-in-one-upload"],
)
def test_upload_with_a_file_where_a_folder_is_needed_stores_nothing(
    storage: Storage, files: list[tuple[str, bytes]]
) -> None:
    _write(storage.root, "in/a.txt", "a")
    res = _upload(storage, "in", *files)
    assert res.status_code == 409, res.text
    assert str(storage.root) not in res.json()["detail"]
    assert not (storage.root / "in" / "ok.txt").exists()


def test_upload_refuses_traversing_filename(storage: Storage) -> None:
    assert _upload(storage, "in", ("../escape.txt", b"x")).status_code == 400


def test_download_missing_and_folder_are_404(storage: Storage) -> None:
    _write(storage.root, "docs/a.txt", "a")
    assert storage.client.get("/api/storage/download", params={"key": "docs/zz"}).status_code == 404
    assert storage.client.get("/api/storage/download", params={"key": "docs"}).status_code == 404


# ── Text ─────────────────────────────────────────────────────────────────────


def test_read_and_save_text_with_etag(storage: Storage) -> None:
    _write(storage.root, "notes/a.md", "# one")
    read = storage.client.get("/api/storage/text", params={"key": "notes/a.md"}).json()
    assert read["content"] == "# one"
    assert read["size"] == 5

    saved = storage.client.put(
        "/api/storage/text", json={"key": "notes/a.md", "content": "# two", "etag": read["etag"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["etag"] != read["etag"]
    assert (storage.root / "notes" / "a.md").read_text() == "# two"

    # A save based on the version before is refused...
    stale = storage.client.put("/api/storage/text", json={"key": "notes/a.md", "content": "#3", "etag": read["etag"]})
    assert stale.status_code == 409
    assert (storage.root / "notes" / "a.md").read_text() == "# two"
    # ...and goes through without an etag (the user chose to overwrite).
    forced = storage.client.put("/api/storage/text", json={"key": "notes/a.md", "content": "#3"})
    assert forced.status_code == 200
    assert (storage.root / "notes" / "a.md").read_text() == "#3"


def test_save_text_creates_a_new_file(storage: Storage) -> None:
    res = storage.client.put("/api/storage/text", json={"key": "new/file.txt", "content": "hello"})
    assert res.status_code == 200
    assert (storage.root / "new" / "file.txt").read_text() == "hello"


def test_save_with_etag_of_deleted_file_conflicts(storage: Storage) -> None:
    res = storage.client.put("/api/storage/text", json={"key": "gone.txt", "content": "x", "etag": "abc"})
    assert res.status_code == 409


def test_save_with_a_non_ascii_etag_is_a_conflict_not_an_error(storage: Storage) -> None:
    _write(storage.root, "notes/a.md", "# one")
    res = storage.client.put("/api/storage/text", json={"key": "notes/a.md", "content": "x", "etag": "é"})
    assert res.status_code == 409


def test_read_text_rejects_binary_and_large(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(storage.root, "bin.dat", b"\x00\xff\xfe")
    _write(storage.root, "latin1.txt", "café".encode("latin-1"))
    assert storage.client.get("/api/storage/text", params={"key": "bin.dat"}).status_code == 415
    assert storage.client.get("/api/storage/text", params={"key": "latin1.txt"}).status_code == 415

    monkeypatch.setattr(storage_browser_module, "MAX_TEXT_BYTES", 4)
    _write(storage.root, "big.txt", "12345")
    assert storage.client.get("/api/storage/text", params={"key": "big.txt"}).status_code == 413
    too_big = storage.client.put("/api/storage/text", json={"key": "big2.txt", "content": "12345"})
    assert too_big.status_code == 413


# ── Folders, move, delete ────────────────────────────────────────────────────


def test_mkdir_shows_an_empty_folder(storage: Storage) -> None:
    res = storage.client.post("/api/storage/folders", json={"prefix": "fresh/inner"})
    assert res.status_code == 201
    parent = storage.client.get("/api/storage/list", params={"prefix": "fresh"}).json()
    assert [f["key"] for f in parent["folders"]] == ["fresh/inner"]
    inner = storage.client.get("/api/storage/list", params={"prefix": "fresh/inner"}).json()
    # The object-store marker is not listed.
    assert inner["files"] == []
    assert storage.client.post("/api/storage/folders", json={"prefix": "fresh/inner"}).status_code == 409


def test_move_file_and_conflict(storage: Storage) -> None:
    _write(storage.root, "a.txt", "a")
    _write(storage.root, "b.txt", "b")
    assert storage.client.post("/api/storage/move", json={"from": "a.txt", "to": "b.txt"}).status_code == 409

    res = storage.client.post("/api/storage/move", json={"from": "a.txt", "to": "dir/renamed.txt"})
    assert res.status_code == 200
    assert res.json() == {"from": "a.txt", "to": "dir/renamed.txt"}
    assert not (storage.root / "a.txt").exists()
    assert (storage.root / "dir" / "renamed.txt").read_text() == "a"

    over = storage.client.post("/api/storage/move", json={"from": "b.txt", "to": "dir/renamed.txt", "overwrite": True})
    assert over.status_code == 200
    assert (storage.root / "dir" / "renamed.txt").read_text() == "b"


def test_move_folder(storage: Storage) -> None:
    _write(storage.root, "src/x.txt", "x")
    _write(storage.root, "src/deep/y.txt", "y")
    res = storage.client.post("/api/storage/move", json={"from": "src", "to": "dst"})
    assert res.status_code == 200, res.text
    assert (storage.root / "dst" / "deep" / "y.txt").read_text() == "y"
    assert (storage.root / "dst" / "x.txt").read_text() == "x"
    assert not (storage.root / "src").exists()
    into_itself = storage.client.post("/api/storage/move", json={"from": "dst", "to": "dst/inner"})
    assert into_itself.status_code == 400


@pytest.mark.parametrize("overwrite", [False, True])
def test_move_onto_a_folder_that_contains_the_source_is_refused(storage: Storage, overwrite: bool) -> None:
    """Replacing `a` with `a/b` would delete `a/b` itself and its siblings."""
    _write(storage.root, "a/b/x.txt", "x")
    _write(storage.root, "a/c.txt", "c")
    res = storage.client.post("/api/storage/move", json={"from": "a/b", "to": "a", "overwrite": overwrite})
    assert res.status_code == 409
    assert (storage.root / "a" / "b" / "x.txt").read_text() == "x"
    assert (storage.root / "a" / "c.txt").read_text() == "c"


def test_delete_file_and_folder(storage: Storage) -> None:
    _write(storage.root, "docs/a.txt", "a")
    _write(storage.root, "docs/sub/b.txt", "b")
    assert storage.client.delete("/api/storage/object", params={"key": "docs/a.txt"}).json() == {
        "key": "docs/a.txt",
        "kind": "file",
    }
    assert not (storage.root / "docs" / "a.txt").exists()
    # A folder needs `prefix`, a file `key`.
    assert storage.client.delete("/api/storage/object", params={"key": "docs/sub"}).status_code == 404
    assert storage.client.delete("/api/storage/object", params={"prefix": "docs"}).status_code == 200
    assert not (storage.root / "docs").exists()
    assert storage.client.delete("/api/storage/object", params={"prefix": "docs"}).status_code == 404
    assert storage.client.delete("/api/storage/object").status_code == 400


# ── Path safety ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["../outside.txt", "docs/../../outside.txt", "/etc/passwd", "~/x", "s3://other-bucket/x", "file:///etc/passwd"],
)
def test_traversal_is_rejected(storage: Storage, key: str) -> None:
    client = storage.client
    assert client.get("/api/storage/list", params={"prefix": key}).status_code == 400
    assert client.get("/api/storage/text", params={"key": key}).status_code == 400
    assert client.get("/api/storage/download", params={"key": key}).status_code == 400
    assert client.put("/api/storage/text", json={"key": key, "content": "x"}).status_code == 400
    assert client.post("/api/storage/folders", json={"prefix": key}).status_code == 400
    assert client.delete("/api/storage/object", params={"key": key}).status_code == 400
    assert client.post("/api/storage/move", json={"from": "a", "to": key}).status_code == 400


# ── Protected skills prefix ──────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["team/skills", "team/skills/demo", "team"])
def test_protected_folder_cannot_be_deleted(storage: Storage, target: str) -> None:
    _write(storage.root, "team/skills/demo/SKILL.md", "x")
    res = storage.client.delete("/api/storage/object", params={"prefix": target})
    assert res.status_code == 403
    assert (storage.root / "team" / "skills" / "demo" / "SKILL.md").exists()


def test_protected_file_cannot_be_deleted_or_moved(storage: Storage) -> None:
    _write(storage.root, "team/skills/demo/SKILL.md", "x")
    _write(storage.root, "other.txt", "o")
    key = "team/skills/demo/SKILL.md"
    assert storage.client.delete("/api/storage/object", params={"key": key}).status_code == 403
    assert storage.client.post("/api/storage/move", json={"from": key, "to": "x.md"}).status_code == 403
    assert storage.client.post("/api/storage/move", json={"from": "team", "to": "t2"}).status_code == 403
    # Nor can something be moved over or into it: the skills dir changes only
    # through upload and the editor.
    into = storage.client.post("/api/storage/move", json={"from": "other.txt", "to": "team/skills/o.txt"})
    assert into.status_code == 403
    assert (storage.root / key).exists()


def test_protected_files_can_still_be_uploaded_and_edited(storage: Storage) -> None:
    _write(storage.root, "team/skills/demo/SKILL.md", "x")
    res = storage.client.put("/api/storage/text", json={"key": "team/skills/demo/SKILL.md", "content": "y"})
    assert res.status_code == 200
    assert _upload(storage, "team/skills/demo", ("notes.md", b"n")).status_code == 200


def test_sibling_with_common_name_prefix_is_not_protected(storage: Storage) -> None:
    _write(storage.root, "team/skills-old/a.txt", "a")
    assert storage.client.delete("/api/storage/object", params={"prefix": "team/skills-old"}).status_code == 200


# ── Presigned links ──────────────────────────────────────────────────────────


def test_presign_cloud_uses_provider_url_with_ttl(cloud_storage: Storage) -> None:
    _write(cloud_storage.root, "img/p.png", b"png")
    with patch(
        "cloudpathlib.CloudPath.as_url", return_value="https://signed.example/p.png?X-Amz-Expires=900"
    ) as as_url:
        res = cloud_storage.client.get("/api/storage/presign", params={"key": "img/p.png", "disposition": "inline"})
    assert res.status_code == 200
    assert res.json()["url"] == "https://signed.example/p.png?X-Amz-Expires=900"
    as_url.assert_called_once_with(presign=True, expire_seconds=900)


def test_presign_missing_file_is_404(storage: Storage) -> None:
    assert storage.client.get("/api/storage/presign", params={"key": "nope.png"}).status_code == 404


def test_presign_local_round_trip(local_storage: Storage) -> None:
    _write(local_storage.root, "img/p.png", b"png-bytes")
    res = local_storage.client.get("/api/storage/presign", params={"key": "img/p.png", "disposition": "inline"})
    body = res.json()
    assert body["url"].startswith("/api/storage/signed/")
    served = local_storage.client.get(body["url"])
    assert served.status_code == 200
    assert served.content == b"png-bytes"
    assert served.headers["content-type"] == "image/png"
    assert served.headers["content-disposition"] == "inline; filename*=utf-8''p.png"


def test_presign_local_tampered_and_expired_tokens(local_storage: Storage) -> None:
    _write(local_storage.root, "a.txt", "a")
    url = local_storage.client.get("/api/storage/presign", params={"key": "a.txt"}).json()["url"]
    payload, signature = url.rsplit("/", 1)[1].split(".")
    assert local_storage.client.get(f"/api/storage/signed/{payload}.{signature[:-2]}xx").status_code == 403
    assert local_storage.client.get("/api/storage/signed/garbage").status_code == 403
    # A non-ASCII signature is refused like any wrong one, not a 503.
    assert local_storage.client.get("/api/storage/signed/abc.%C3%A9").status_code == 403

    browser = local_storage.browser
    expired = browser.sign("a.txt", int(time.time()) - 1, "attachment")
    assert local_storage.client.get(f"/api/storage/signed/{expired}").status_code == 410
    # A validly signed key is still confined to the root.
    escape = browser.sign("../x", int(time.time()) + 60, "attachment")
    assert local_storage.client.get(f"/api/storage/signed/{escape}").status_code == 400


def test_signed_route_is_404_for_cloud_roots(cloud_storage: Storage) -> None:
    token = cloud_storage.browser.sign("a.txt", int(time.time()) + 60, "inline")
    assert cloud_storage.client.get(f"/api/storage/signed/{token}").status_code == 404


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_storage_requires_authentication(authed_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Through the real dependency: the root comes from the settings."""
    monkeypatch.setenv("PINCER_STORAGE_ROOT", str(tmp_path / "root"))
    get_settings_relaxed.cache_clear()
    client = authed_app.client
    assert client.get("/api/storage/list").status_code == 401
    assert client.post("/api/storage/folders", json={"prefix": "x"}).status_code == 401

    headers = {"Authorization": f"Bearer {authed_app.access_token}"}
    assert client.get("/api/storage/list", headers=headers).status_code == 200
    assert client.post("/api/storage/folders", json={"prefix": "x"}, headers=headers).status_code == 201

    # The signed route is public, but only with a valid token.
    (tmp_path / "root" / "f.txt").write_text("f")
    url = client.get("/api/storage/presign", params={"key": "f.txt"}, headers=headers).json()["url"]
    assert client.get(url).status_code == 200
    assert client.get("/api/storage/signed/forged.token").status_code == 403


def test_an_api_key_can_read_but_not_change_files(
    authed_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A long-lived, often embedded API key must not be able to overwrite or
    delete every file under the root: changes need a signed-in session."""
    monkeypatch.setenv("PINCER_STORAGE_ROOT", str(tmp_path / "root"))
    get_settings_relaxed.cache_clear()
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "f.txt").write_text("f")
    client = authed_app.client
    headers = {"Authorization": f"Bearer {authed_app.api_key}"}

    assert client.get("/api/storage/list", headers=headers).status_code == 200
    assert client.get("/api/storage/text", params={"key": "f.txt"}, headers=headers).status_code == 200
    changes = [
        client.put("/api/storage/text", json={"key": "f.txt", "content": "x"}, headers=headers),
        client.post("/api/storage/upload", files=[("files", ("u.txt", b"u"))], headers=headers),
        client.post("/api/storage/folders", json={"prefix": "x"}, headers=headers),
        client.post("/api/storage/move", json={"from": "f.txt", "to": "g.txt"}, headers=headers),
        client.delete("/api/storage/object", params={"key": "f.txt"}, headers=headers),
    ]
    assert [res.status_code for res in changes] == [403] * 5
    assert (tmp_path / "root" / "f.txt").read_text() == "f"


def test_mutations_need_an_identity_even_with_auth_disabled(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    app = create_app()
    app.dependency_overrides[storage_browser] = lambda: StorageBrowser(root, [], 900, b"k" * 32)
    client = TestClient(app)
    assert client.get("/api/storage/list").status_code == 200
    assert client.post("/api/storage/folders", json={"prefix": "x"}).status_code == 401


def test_an_unusable_root_is_503(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A root the browser refuses to serve fails every route, with the reason."""
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PINCER_STORAGE_ROOT", str(tmp_path))
    get_settings_relaxed.cache_clear()
    try:
        res = TestClient(create_app()).get("/api/storage/list")
    finally:
        get_settings_relaxed.cache_clear()
    assert res.status_code == 503
    assert "contains the data dir" in res.json()["detail"]

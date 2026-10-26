"""Tests for src/pincer/storage — the storage browser below its HTTP routes."""

from __future__ import annotations

import inspect
import io
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from pincer.config.main import _RelaxedSettings
from pincer.storage import clients, signing
from pincer.storage.browser import StorageBrowser
from pincer.storage.errors import InvalidKey, InvalidSignature, KeyConflict, KeyNotFound, ProtectedKey, TooLarge
from pincer.storage.objects import AzureObjects, GSObjects, PathObjects, S3Objects, objects_for


def _local(tmp_path: Path, protected: list[str] | None = None) -> StorageBrowser:
    root = (tmp_path / "root").resolve()
    root.mkdir(exist_ok=True)
    return StorageBrowser(root, protected or [], 900, b"k" * 32)


def _write(path: Path, data: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def _case_insensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.touch()
    return (tmp_path / "caseprobe").exists()


@pytest.fixture(params=["local", "s3"])
def browser(request: pytest.FixtureRequest, tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> StorageBrowser:
    """A browser on a local root and on a cloud root (offline S3 mock)."""
    if request.param == "local":
        return _local(tmp_path)
    root = mock_cloud_client("s3", file_cache_mode="close_file").CloudPath("s3://docs-bucket/")
    return StorageBrowser(root, [], 900, b"k" * 32)


# ── Keys and protection ──────────────────────────────────────────────────────


def test_is_protected_covers_ancestors_and_descendants() -> None:
    browser = StorageBrowser(Path("/r"), ["a/b"], 900, b"k" * 32)
    assert browser.is_protected("a/b")
    assert browser.is_protected("a/b/c.txt")
    assert browser.is_protected("a")
    assert browser.is_protected("")
    assert not browser.is_protected("a/bc")
    assert not browser.is_protected("x/a/b")


def test_local_symlink_out_of_root_is_rejected(tmp_path: Path) -> None:
    browser = _local(tmp_path)
    (tmp_path / "secret.txt").write_text("s")
    (browser.root / "link.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(InvalidKey):
        browser.read_text("link.txt")


def test_a_symlink_into_the_skills_dir_cannot_delete_or_move_it(tmp_path: Path) -> None:
    """Protection follows the folder on disk, not the key: `alias/skills` is
    `team/skills` when `alias` links to `team`."""
    browser = _local(tmp_path, ["team/skills"])
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "team" / "skills" / "s1" / "SKILL.md")
    (root / "alias").symlink_to(root / "team")

    with pytest.raises(ProtectedKey):
        browser.delete("alias/skills", folder=True)
    with pytest.raises(ProtectedKey):
        browser.delete("alias/skills/s1/SKILL.md", folder=False)
    with pytest.raises(ProtectedKey):
        browser.move("alias/skills/s1", "elsewhere")
    assert (root / "team" / "skills" / "s1" / "SKILL.md").exists()
    # The link itself is just a name: moving it leaves the skills dir alone.
    browser.move("alias", "renamed-alias")
    assert (root / "renamed-alias").is_symlink()
    assert (root / "team" / "skills" / "s1" / "SKILL.md").exists()


def test_a_symlink_that_points_into_the_skills_dir_can_itself_be_removed(tmp_path: Path) -> None:
    browser = _local(tmp_path, ["team/skills"])
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "team" / "skills" / "s1" / "SKILL.md")
    (root / "shortcut").symlink_to(root / "team" / "skills" / "s1")

    browser.delete("shortcut", folder=False)
    assert not (root / "shortcut").is_symlink()
    assert (root / "team" / "skills" / "s1" / "SKILL.md").exists()


def test_a_different_case_key_cannot_delete_or_move_the_skills_dir(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    browser = _local(tmp_path, ["team/skills"])
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "team" / "skills" / "s1" / "SKILL.md")

    with pytest.raises(ProtectedKey):
        browser.delete("TEAM/Skills", folder=True)
    with pytest.raises(ProtectedKey):
        browser.delete("Team", folder=True)
    with pytest.raises(ProtectedKey):
        browser.move("team/SKILLS", "elsewhere")
    assert (root / "team" / "skills" / "s1" / "SKILL.md").exists()


# ── Move ─────────────────────────────────────────────────────────────────────


def test_case_only_rename_keeps_the_file(tmp_path: Path) -> None:
    """On a case-insensitive disk the target "exists" (it is the source):
    the move is a rename, with or without overwrite, never a delete."""
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    browser = _local(tmp_path)
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "readme.md", "keep me")

    browser.move("readme.md", "README.md", overwrite=True)
    assert [p.name for p in root.iterdir()] == ["README.md"]
    assert (root / "README.md").read_text() == "keep me"

    browser.move("README.md", "Readme.md")
    assert [p.name for p in root.iterdir()] == ["Readme.md"]


def test_moving_a_folder_into_itself_under_another_case_is_refused(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    browser = _local(tmp_path)
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "a" / "b" / "x.txt")
    _write(root / "a" / "c.txt")

    with pytest.raises(KeyConflict):
        browser.move("a/b", "A", overwrite=True)
    with pytest.raises(InvalidKey):
        browser.move("a", "A/inner")
    assert (root / "a" / "b" / "x.txt").exists()
    assert (root / "a" / "c.txt").exists()


# ── Concurrent writes ────────────────────────────────────────────────────────


def _race(monkeypatch: pytest.MonkeyPatch, method: str, *calls: Callable[[], Any]) -> list[Any]:
    """Run `calls` at once, each pausing inside StorageBrowser.`method` so that,
    without a lock, both would pass their checks before either writes."""
    original = getattr(StorageBrowser, method)

    def slow(self: StorageBrowser, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        time.sleep(0.2)
        return result

    monkeypatch.setattr(StorageBrowser, method, slow)
    results: list[Any] = [None] * len(calls)

    def run(i: int) -> None:
        try:
            results[i] = calls[i]()
        except Exception as e:
            results[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_saves_with_the_same_etag_cannot_both_succeed(
    browser: StorageBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser.write_text("doc.md", "v1")
    tag = browser.read_text("doc.md")["etag"]

    results = _race(
        monkeypatch,
        "_read",
        lambda: browser.write_text("doc.md", "from alice", tag),
        lambda: browser.write_text("doc.md", "from bob", tag),
    )
    conflicts = [r for r in results if isinstance(r, KeyConflict)]
    saved = [r for r in results if isinstance(r, dict)]
    assert len(conflicts) == 1 and len(saved) == 1
    assert browser.read_text("doc.md")["etag"] == saved[0]["etag"]


def test_saves_through_a_symlinked_name_share_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`alias/doc.md` with `alias -> team` is `team/doc.md`: the two names take one lock."""
    browser = _local(tmp_path)
    root = browser.root
    assert isinstance(root, Path)
    _write(root / "team" / "doc.md", "v1")
    (root / "alias").symlink_to(root / "team")
    tag = browser.read_text("team/doc.md")["etag"]

    results = _race(
        monkeypatch,
        "_read",
        lambda: browser.write_text("team/doc.md", "from alice", tag),
        lambda: browser.write_text("alias/doc.md", "from bob", tag),
    )
    assert sorted(type(r).__name__ for r in results) == ["KeyConflict", "dict"]


def test_two_uploads_of_a_new_name_cannot_both_succeed(
    browser: StorageBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = _race(
        monkeypatch,
        "_within_upload_limit",
        lambda: browser.upload("", "new.txt", io.BytesIO(b"alice")),
        lambda: browser.upload("", "new.txt", io.BytesIO(b"bob")),
    )
    assert sorted(type(r).__name__ for r in results) == ["KeyConflict", "dict"]


def test_create_only_local_write_is_atomic_without_the_lock(tmp_path: Path) -> None:
    """The hard link refuses a taken name even for a writer outside this process."""
    browser = _local(tmp_path)
    target = browser.root / "a.txt"
    assert isinstance(target, Path)
    browser._store(target, io.BytesIO(b"first"), if_absent=True)
    with pytest.raises(KeyConflict):
        browser._store(target, io.BytesIO(b"second"), if_absent=True)
    assert target.read_bytes() == b"first"
    assert [p.name for p in target.parent.iterdir()] == ["a.txt"]


# ── Upload ───────────────────────────────────────────────────────────────────


def test_upload_limit_spools_unseekable_sources(tmp_path: Path) -> None:
    class Pipe:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def seekable(self) -> bool:
            return False

        def read(self, size: int = -1) -> bytes:
            chunk, self._data = self._data[:size], self._data[size:]
            return chunk

    browser = _local(tmp_path)
    browser.upload_max_bytes = 10
    browser.upload("", "small.bin", Pipe(b"0123456789"))  # type: ignore[arg-type]
    assert (browser.root / "small.bin").read_bytes() == b"0123456789"
    with pytest.raises(TooLarge):
        browser.upload("", "big.bin", Pipe(b"0123456789A"))  # type: ignore[arg-type]
    assert not (browser.root / "big.bin").exists()


def test_a_filesystem_conflict_while_writing_is_a_conflict_without_the_server_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _local(tmp_path)
    original = StorageBrowser._store

    def store(self: StorageBrowser, path: Any, source: Any, **kwargs: Any) -> None:
        if path.name == "b.txt":
            raise FileExistsError(17, "File exists", str(path))
        original(self, path, source, **kwargs)

    monkeypatch.setattr(StorageBrowser, "_store", store)
    with pytest.raises(KeyConflict) as caught:
        browser.upload_files("in", [("a.txt", io.BytesIO(b"a")), ("b.txt", io.BytesIO(b"b"))])
    assert str(caught.value) == "Storing in/b.txt failed (FileExistsError: File exists); stored before it: in/a.txt"
    assert str(tmp_path) not in str(caught.value)


def test_a_failure_while_writing_names_the_files_already_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _local(tmp_path)
    original = StorageBrowser._store

    def store(self: StorageBrowser, path: Any, source: Any, **kwargs: Any) -> None:
        if path.name == "b.txt":
            raise OSError("disk full")
        original(self, path, source, **kwargs)

    monkeypatch.setattr(StorageBrowser, "_store", store)
    with pytest.raises(
        Exception, match=r"Storing in/b\.txt failed \(OSError: disk full\); stored before it: in/a\.txt"
    ):
        browser.upload_files("in", [("a.txt", io.BytesIO(b"a")), ("b.txt", io.BytesIO(b"b"))])


# ── Listing ──────────────────────────────────────────────────────────────────


def test_cloudpathlib_still_has_the_private_listing_the_browser_uses() -> None:
    """`_list_dir` is private cloudpathlib API (pinned below 0.27 for it). If a
    release renames it, listings fall back to `iterdir`; this test says so."""
    cloudpathlib = pytest.importorskip("cloudpathlib")
    for client in (cloudpathlib.S3Client, cloudpathlib.AzureBlobClient, cloudpathlib.GSClient):
        params = list(inspect.signature(client._list_dir).parameters)
        assert params[:3] == ["self", "cloud_path", "recursive"], client


def test_listing_falls_back_to_iterdir_without_the_private_api(
    tmp_path: Path, mock_cloud_client: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = mock_cloud_client("s3")
    root = client.CloudPath("s3://docs-bucket/")
    (root / "docs" / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    # A release without `_list_dir`, whose public `iterdir` still works.
    list_dir = client._list_dir
    monkeypatch.setattr(type(root), "iterdir", lambda self: (child for child, _ in list_dir(self, recursive=False)))
    monkeypatch.setattr(client, "_list_dir", None)

    body = StorageBrowser(root, [], 900, b"k" * 32).list_dir()
    assert [f["key"] for f in body["folders"]] == ["docs"]
    assert [f["key"] for f in body["files"]] == ["b.txt"]


# ── Signing ──────────────────────────────────────────────────────────────────


def test_a_non_ascii_token_is_an_invalid_signature() -> None:
    with pytest.raises(InvalidSignature):
        signing.verify(b"k" * 32, "abc.é")


# ── Presigned links ──────────────────────────────────────────────────────────


def _s3_client(tmp_path: Path) -> Any:
    """A real S3Client: building it, presigning and stubbing are all offline."""
    settings = _RelaxedSettings(  # type: ignore[call-arg]
        data_dir=tmp_path,
        storage_s3_endpoint_url="http://s3.example.test:9000",
        storage_s3_region="eu-central-1",
        storage_s3_access_key_id="AKIATEST",
        storage_s3_secret_access_key="secret",
    )
    return clients.build_client("s3", settings, tmp_path / "cache")


def test_presign_s3_signs_v4_with_response_disposition(tmp_path: Path) -> None:
    from urllib.parse import parse_qs, urlsplit

    client = _s3_client(tmp_path)
    url = S3Objects(client).presign(client.CloudPath("s3://b/dir/r é.pdf"), "attachment", 900)

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "s3.example.test:9000"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["response-content-disposition"] == ["attachment; filename*=UTF-8''r%20%C3%A9.pdf"]
    assert query["response-content-type"] == ["application/pdf"]
    assert "eu-central-1" in query["X-Amz-Credential"][0]


@pytest.mark.parametrize(
    ("name", "disposition"),
    [
        ("page.html", "attachment"),
        ("logo.svg", "attachment"),
        ("feed.rss", "attachment"),
        ("doc.rdf", "attachment"),
        ("f.atom", "attachment"),
        ("x.xslt", "attachment"),
        ("m.mathml", "attachment"),
        ("data.xml", "attachment"),
        ("script.js", "attachment"),
        ("photo.png", "inline"),
        ("report.pdf", "inline"),
        ("notes.txt", "inline"),
        ("clip.mp4", "inline"),
    ],
)
def test_presign_s3_serves_inline_only_what_is_safe_inline(tmp_path: Path, name: str, disposition: str) -> None:
    """A cloud link carries no CSP: HTML, SVG or any XML type shown inline could
    run script on the bucket's origin, so only an allowlist is served inline."""
    from urllib.parse import parse_qs, urlsplit

    client = _s3_client(tmp_path)
    url = S3Objects(client).presign(client.CloudPath(f"s3://b/{name}"), "inline", 900)
    assert parse_qs(urlsplit(url).query)["response-content-disposition"][0].startswith(f"{disposition};")


# ── Cloud object IO ──────────────────────────────────────────────────────────


def test_objects_for_picks_the_sdk_of_the_client(tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> None:
    cloudpathlib = pytest.importorskip("cloudpathlib")
    assert isinstance(objects_for(_s3_client(tmp_path).CloudPath("s3://b/")), S3Objects)
    # A dummy connection string: building the client never connects.
    azure = cloudpathlib.AzureBlobClient(
        connection_string="DefaultEndpointsProtocol=https;AccountName=t;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net"
    )
    assert isinstance(objects_for(azure.CloudPath("az://c/")), AzureObjects)
    gcs = cloudpathlib.GSClient(storage_client=MagicMock())
    assert isinstance(objects_for(gcs.CloudPath("gs://b/")), GSObjects)
    assert type(objects_for(mock_cloud_client("s3").CloudPath("s3://b/"))) is PathObjects


def _stubbed_s3(tmp_path: Path) -> tuple[Any, Any, S3Objects]:
    from botocore.stub import Stubber

    client = _s3_client(tmp_path)
    stub = Stubber(client.client)
    return client, stub, S3Objects(client)


def test_s3_read_and_conditional_writes_go_through_boto3(tmp_path: Path) -> None:
    from botocore.response import StreamingBody
    from botocore.stub import ANY

    client, stub, objects = _stubbed_s3(tmp_path)
    path = client.CloudPath("s3://b/doc.md")
    stub.add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(b"v1"), 2), "ETag": '"e1"'}, {"Bucket": "b", "Key": "doc.md"}
    )
    put = {"Bucket": "b", "Key": "doc.md", "Body": ANY, "ContentType": "text/markdown"}
    stub.add_response("put_object", {}, {**put, "IfMatch": '"e1"'})
    stub.add_response("put_object", {}, {**put, "IfNoneMatch": "*"})
    stub.add_client_error(
        "put_object", "PreconditionFailed", http_status_code=412, expected_params=put | {"IfMatch": "x"}
    )
    with stub:
        assert objects.read(path) == (b"v1", '"e1"')
        objects.write(path, io.BytesIO(b"v2"), content_type="text/markdown", if_version='"e1"')
        objects.write(path, io.BytesIO(b"v3"), content_type="text/markdown", if_absent=True)
        with pytest.raises(KeyConflict):
            objects.write(path, io.BytesIO(b"v4"), content_type="text/markdown", if_version="x")
    stub.assert_no_pending_responses()
    # Nothing went through cloudpathlib's shared cache.
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").rglob("*.md"))


def test_create_only_wins_over_a_version_on_every_provider(tmp_path: Path) -> None:
    """Passing both `if_absent` and `if_version` sends only the create-only condition."""
    from botocore.stub import ANY

    client, stub, s3 = _stubbed_s3(tmp_path)
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "b", "Key": "a.txt", "Body": ANY, "ContentType": "text/plain", "IfNoneMatch": "*"},
    )
    with stub:
        s3.write(
            client.CloudPath("s3://b/a.txt"),
            io.BytesIO(b"a"),
            content_type="text/plain",
            if_version='"e1"',
            if_absent=True,
        )
    stub.assert_no_pending_responses()

    service = MagicMock()
    AzureObjects(SimpleNamespace(service_client=service)).write(
        SimpleNamespace(container="c", blob="a.txt"),  # type: ignore[arg-type]
        io.BytesIO(b"a"),
        content_type="text/plain",
        if_version='"e1"',
        if_absent=True,
    )
    azure_kwargs = service.get_blob_client.return_value.upload_blob.call_args.kwargs
    assert azure_kwargs["overwrite"] is False
    assert "etag" not in azure_kwargs and "match_condition" not in azure_kwargs

    gcs = MagicMock()
    GSObjects(SimpleNamespace(client=gcs)).write(
        SimpleNamespace(bucket="b", blob="a.txt"),  # type: ignore[arg-type]
        io.BytesIO(b"a"),
        content_type="text/plain",
        if_version=7,
        if_absent=True,
    )
    assert gcs.bucket.return_value.blob.return_value.upload_from_file.call_args.kwargs["if_generation_match"] == 0


def test_s3_missing_object_is_key_not_found(tmp_path: Path) -> None:
    client, stub, objects = _stubbed_s3(tmp_path)
    stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    with stub, pytest.raises(KeyNotFound):
        objects.read(client.CloudPath("s3://b/gone.md"))


def test_s3_without_conditional_writes_checks_before_writing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """After a 501, writes go without conditions, but a create-only write or a
    versioned save still checks the object first (under the browser's lock)."""
    from botocore.stub import ANY

    client, stub, objects = _stubbed_s3(tmp_path)
    path = client.CloudPath("s3://b/new.txt")
    key = {"Bucket": "b", "Key": "new.txt"}
    put = key | {"Body": ANY, "ContentType": "text/plain"}
    stub.add_client_error(
        "put_object", "NotImplemented", http_status_code=501, expected_params=put | {"IfNoneMatch": "*"}
    )
    stub.add_client_error("head_object", "404", http_status_code=404, expected_params=key)
    stub.add_response("put_object", {}, put)
    # Now it exists: a second create-only write is refused without writing.
    stub.add_response("head_object", {"ETag": '"e1"'}, key)
    # A save of the version that is there goes through; a stale one does not.
    stub.add_response("head_object", {"ETag": '"e1"'}, key)
    stub.add_response("put_object", {}, put)
    stub.add_response("head_object", {"ETag": '"e2"'}, key)
    with stub:
        objects.write(path, io.BytesIO(b"data"), content_type="text/plain", if_absent=True)
        with pytest.raises(KeyConflict):
            objects.write(path, io.BytesIO(b"again"), content_type="text/plain", if_absent=True)
        objects.write(path, io.BytesIO(b"v2"), content_type="text/plain", if_version='"e1"')
        with pytest.raises(KeyConflict):
            objects.write(path, io.BytesIO(b"v3"), content_type="text/plain", if_version='"e1"')
    stub.assert_no_pending_responses()
    assert caplog.text.count("does not take conditional writes") == 1


def test_s3_uploads_are_capped_at_one_put(tmp_path: Path) -> None:
    """S3 takes at most 5 GiB in one PUT: a larger limit is lowered to that,
    so a bigger file is a 413 before anything is sent."""
    root = _s3_client(tmp_path).CloudPath("s3://b/")
    browser = StorageBrowser(root, [], 900, b"k" * 32, upload_max_bytes=10 * 1024**3)
    assert browser.info()["max_upload_bytes"] == 5 * 1024**3
    browser.upload_max_bytes = 1024
    assert browser.info()["max_upload_bytes"] == 1024


def test_azure_conditional_writes() -> None:
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

    service = MagicMock()
    blob = service.get_blob_client.return_value
    blob.download_blob.return_value = MagicMock(readall=lambda: b"v1", properties=SimpleNamespace(etag='"e1"'))
    objects = AzureObjects(SimpleNamespace(service_client=service))
    path = SimpleNamespace(container="c", blob="doc.md")

    assert objects.read(path) == (b"v1", '"e1"')  # type: ignore[arg-type]
    objects.write(path, io.BytesIO(b"v2"), content_type="text/markdown", if_version='"e1"')  # type: ignore[arg-type]
    kwargs = blob.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is True
    assert kwargs["etag"] == '"e1"'
    assert kwargs["match_condition"] == MatchConditions.IfNotModified
    assert kwargs["content_settings"].content_type == "text/markdown"

    objects.write(path, io.BytesIO(b"v3"), content_type="text/plain", if_absent=True)  # type: ignore[arg-type]
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    assert "etag" not in blob.upload_blob.call_args.kwargs

    for error in (ResourceModifiedError("412"), ResourceExistsError("409")):
        blob.upload_blob.side_effect = error
        with pytest.raises(KeyConflict):
            objects.write(path, io.BytesIO(b"v4"), content_type="text/plain", if_version='"e1"')  # type: ignore[arg-type]


def test_gcs_conditional_writes() -> None:
    from google.api_core.exceptions import PreconditionFailed

    gcs = MagicMock()
    blob = gcs.bucket.return_value.blob.return_value
    blob.download_as_bytes.return_value = b"v1"
    blob.generation = 7
    objects = GSObjects(SimpleNamespace(client=gcs))
    path = SimpleNamespace(bucket="b", blob="doc.md")

    assert objects.read(path) == (b"v1", 7)  # type: ignore[arg-type]
    objects.write(path, io.BytesIO(b"v2"), content_type="text/markdown", if_version=7)  # type: ignore[arg-type]
    assert blob.upload_from_file.call_args.kwargs == {"content_type": "text/markdown", "if_generation_match": 7}
    objects.write(path, io.BytesIO(b"v3"), content_type="text/plain", if_absent=True)  # type: ignore[arg-type]
    assert blob.upload_from_file.call_args.kwargs["if_generation_match"] == 0

    blob.upload_from_file.side_effect = PreconditionFailed("412")
    with pytest.raises(KeyConflict):
        objects.write(path, io.BytesIO(b"v4"), content_type="text/plain", if_version=7)  # type: ignore[arg-type]

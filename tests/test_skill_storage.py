"""Tests for pincer.tools.skills.storage — the local/cloud skill-root layer."""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import _FAKE_AZURE_CONNECTION_STRING

from pincer.config.main import _RelaxedSettings
from pincer.exceptions import SkillLoadError
from pincer.storage.clients import is_remote_uri
from pincer.tools.path_sandbox import SandboxDenied
from pincer.tools.skills.storage import (
    clear_remote_cache,
    confine_skill_path,
    localize_skill_dir,
    open_skills_root,
    open_usable_skills_root,
    open_user_skills_root,
)

cloudpathlib_local = pytest.importorskip("cloudpathlib.local")


def _settings(**overrides: object) -> _RelaxedSettings:
    return _RelaxedSettings(**overrides)  # type: ignore[arg-type]


@pytest.fixture
def s3_skill_dir(tmp_path: Path):
    client = cloudpathlib_local.LocalS3Client(
        local_storage_dir=tmp_path / "storage", local_cache_dir=tmp_path / "objects"
    )
    skill_dir = client.CloudPath("s3://bucket/team/demo")
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nv1\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def s3_external(tmp_path: Path, s3_skill_dir):
    """The same skill dir through a second client with its own cache: writes here
    change the object behind `s3_skill_dir`'s cache, like an upload from elsewhere."""
    client = cloudpathlib_local.LocalS3Client(
        local_storage_dir=tmp_path / "storage", local_cache_dir=tmp_path / "objects-external"
    )
    return client.CloudPath(str(s3_skill_dir))


# ── is_remote_uri / settings ────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["s3://bucket/prefix", "az://container/p", "gs://bucket", "S3://Bucket/x"])
def test_is_remote_uri_accepts_supported_schemes(value: str) -> None:
    assert is_remote_uri(value)


@pytest.mark.parametrize("value", ["/srv/skills", "~/skills", "skills", "https://example.com/x", "file:///tmp", ""])
def test_is_remote_uri_rejects_everything_else(value: str) -> None:
    assert not is_remote_uri(value)


def test_is_remote_uri_rejects_path_objects() -> None:
    assert not is_remote_uri(Path("s3://bucket"))


def test_settings_keep_remote_uri_intact() -> None:
    assert _settings(skills_dir="s3://bucket/team/skills").skills_dir == "s3://bucket/team/skills"


def test_settings_coerce_local_skills_dir_to_expanded_path() -> None:
    skills_dir = _settings(skills_dir="~/my-skills").skills_dir
    assert skills_dir == Path.home() / "my-skills"


def test_settings_default_skills_cache_under_data_dir(tmp_path: Path) -> None:
    settings = _settings(data_dir=tmp_path)
    assert settings.skills_cache_dir == tmp_path / "cache" / "skills"


@pytest.mark.parametrize("uri", ["s3://bucket/cache", "az://c/cache", "gs://b/cache", "file:///tmp/cache"])
def test_settings_reject_a_uri_for_the_skills_cache_dir(uri: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="skills_cache_dir must be a local directory"):
        _settings(skills_cache_dir=uri)


def test_settings_skills_cache_dir_is_an_expanded_local_path(tmp_path: Path) -> None:
    assert _settings(skills_cache_dir="~/pincer-cache").skills_cache_dir == Path.home() / "pincer-cache"
    assert _settings(skills_cache_dir=str(tmp_path)).skills_cache_dir == tmp_path


def test_storage_credentials_use_the_storage_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_STORAGE_S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("PINCER_STORAGE_AZURE_ACCOUNT_URL", "https://a.blob.core.windows.net")
    monkeypatch.setenv("PINCER_STORAGE_GCS_PROJECT", "proj")
    monkeypatch.setenv("PINCER_SKILLS_S3_ENDPOINT_URL", "http://old-name:9000")  # the old prefix is not read
    settings = _settings()
    assert settings.storage_s3_endpoint_url == "http://minio:9000"
    assert settings.storage_azure_account_url == "https://a.blob.core.windows.net"
    assert settings.storage_gcs_project == "proj"
    assert not hasattr(settings, "skills_s3_endpoint_url")


@pytest.mark.parametrize("ttl", [0, 30, 60])
def test_settings_api_index_ttl_accepts_up_to_60s(ttl: int) -> None:
    assert _settings(skills_index_cache_ttl_seconds=ttl).skills_index_cache_ttl_seconds == ttl


@pytest.mark.parametrize("ttl", [-1, 61, 3600])
def test_settings_api_index_ttl_rejects_out_of_range(ttl: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(skills_index_cache_ttl_seconds=ttl)


def test_settings_read_remote_skills_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_SKILLS_DIR", "gs://bucket/skills")
    monkeypatch.setenv("PINCER_STORAGE_S3_SECRET_ACCESS_KEY", "shh")
    settings = _settings()
    assert settings.skills_dir == "gs://bucket/skills"
    assert settings.storage_s3_secret_access_key.get_secret_value() == "shh"
    assert "shh" not in repr(settings)


# ── confine_skill_path ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requested",
    ["../x", "a/../../x", "..", "/etc/passwd", "~", "~/x", "s3://other/x", "gs://other", "\\\\server\\share"],
)
def test_confine_skill_path_rejects_escapes_on_cloud_base(s3_skill_dir, requested: str) -> None:
    with pytest.raises(SandboxDenied):
        confine_skill_path(s3_skill_dir, requested, label="skill 'demo'")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("notes.txt", "notes.txt"), ("scripts/run.sh", "scripts/run.sh"), ("a/../b.txt", "b.txt"), ("./c", "c")],
)
def test_confine_skill_path_allows_paths_inside_cloud_base(s3_skill_dir, requested: str, expected: str) -> None:
    assert confine_skill_path(s3_skill_dir, requested) == s3_skill_dir / expected


def test_confine_skill_path_dot_is_the_base(s3_skill_dir) -> None:
    assert confine_skill_path(s3_skill_dir, ".") == s3_skill_dir


def test_confine_skill_path_local_base_delegates_to_confine_path(tmp_path: Path) -> None:
    assert confine_skill_path(tmp_path, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()
    with pytest.raises(SandboxDenied):
        confine_skill_path(tmp_path, "../outside")


# ── localize_skill_dir / clear_remote_cache ─────────────────────────────────


def _cache_dir(skill_dir) -> Path:
    return Path(skill_dir.client._local_cache_dir)


def test_localize_returns_local_dirs_unchanged(tmp_path: Path) -> None:
    assert localize_skill_dir(tmp_path) == tmp_path


def test_localize_uses_cloudpathlib_cache_dir(s3_skill_dir) -> None:
    local = localize_skill_dir(s3_skill_dir)

    assert local == _cache_dir(s3_skill_dir) / "bucket" / "team" / "demo"
    assert (local / "SKILL.md").read_text(encoding="utf-8").endswith("v1\n")
    assert (local / "scripts" / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"
    # It *is* cloudpathlib's cache: a later read of the same object resolves to the same file.
    assert Path((s3_skill_dir / "scripts" / "run.sh").fspath) == local / "scripts" / "run.sh"


def test_localize_refreshes_changed_objects(s3_skill_dir, s3_external) -> None:
    cached = localize_skill_dir(s3_skill_dir) / "scripts" / "run.sh"
    assert cached.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"

    # Changed behind this client's cache (PR review: writing through the same
    # client updates the cached copy too, so it can't prove a refresh happened).
    (s3_external / "scripts" / "run.sh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    assert cached.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"  # stale until localized

    local = localize_skill_dir(s3_skill_dir)

    assert (local / "scripts" / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho changed\n"


def test_localize_picks_up_files_added_behind_the_cache(s3_skill_dir, s3_external) -> None:
    localize_skill_dir(s3_skill_dir)
    (s3_external / "scripts" / "new.sh").write_text("#!/bin/sh\necho new\n", encoding="utf-8")

    local = localize_skill_dir(s3_skill_dir)

    assert (local / "scripts" / "new.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"


def test_localize_removes_files_deleted_from_bucket(s3_skill_dir) -> None:
    (s3_skill_dir / "old" / "gone.txt").write_text("x", encoding="utf-8")
    local = localize_skill_dir(s3_skill_dir)
    assert (local / "old" / "gone.txt").is_file()

    (s3_skill_dir / "old" / "gone.txt").unlink()
    local = localize_skill_dir(s3_skill_dir)

    assert not (local / "old").exists()
    assert (local / "scripts" / "run.sh").is_file()


def test_localize_overwrites_locally_modified_copies(s3_skill_dir) -> None:
    """The bucket is the source of truth, even if a script edited its own cached copy."""
    local = localize_skill_dir(s3_skill_dir)
    cached = local / "scripts" / "run.sh"
    cached.write_text("tampered", encoding="utf-8")
    future = cached.stat().st_mtime + 3600
    os.utime(cached, (future, future))

    localize_skill_dir(s3_skill_dir)

    assert cached.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"


def test_clear_remote_cache_empties_cloudpathlib_cache(s3_skill_dir) -> None:
    localize_skill_dir(s3_skill_dir)
    assert any(_cache_dir(s3_skill_dir).iterdir())

    clear_remote_cache(s3_skill_dir)

    assert not any(_cache_dir(s3_skill_dir).iterdir())
    assert (s3_skill_dir / "SKILL.md").is_file()  # the bucket itself is untouched


def test_clear_remote_cache_ignores_local_and_missing_roots(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    clear_remote_cache(tmp_path)
    clear_remote_cache(None)
    assert (tmp_path / "keep.txt").is_file()


# ── open_skills_root ─────────────────────────────────────────────────────────


def test_open_skills_root_local_path(tmp_path: Path) -> None:
    root = open_skills_root(str(tmp_path), _settings())
    assert root == tmp_path
    assert open_skills_root("~/x", _settings()) == Path.home() / "x"


def test_open_skills_root_s3_passes_explicit_credentials(tmp_path: Path) -> None:
    settings = _settings(
        data_dir=tmp_path,
        storage_s3_endpoint_url="http://localhost:9000",
        storage_s3_region="eu-central-1",
        storage_s3_profile="team",
        storage_s3_access_key_id="AKIA",
        storage_s3_secret_access_key="secret",
        storage_s3_session_token="token",
    )
    with patch("boto3.Session") as session_cls, patch("cloudpathlib.S3Client") as client_cls:
        root = open_skills_root("s3://bucket/team", settings)

    session_cls.assert_called_once_with(
        aws_access_key_id="AKIA",
        aws_secret_access_key="secret",
        aws_session_token="token",
        profile_name="team",
        region_name="eu-central-1",
    )
    client_cls.assert_called_once_with(
        boto3_session=session_cls.return_value,
        endpoint_url="http://localhost:9000",
        local_cache_dir=str(tmp_path / "cache" / "skills" / "objects"),
    )
    client_cls.return_value.CloudPath.assert_called_once_with("s3://bucket/team")
    assert root is client_cls.return_value.CloudPath.return_value


def test_open_skills_root_s3_unset_credentials_use_default_chain(tmp_path: Path) -> None:
    with patch("boto3.Session") as session_cls, patch("cloudpathlib.S3Client") as client_cls:
        open_skills_root("s3://bucket/team", _settings(data_dir=tmp_path))

    session_cls.assert_called_once_with()
    assert "endpoint_url" not in client_cls.call_args.kwargs


def test_open_skills_root_azure_connection_string_wins(tmp_path: Path) -> None:
    settings = _settings(
        data_dir=tmp_path,
        storage_azure_connection_string="DefaultEndpointsProtocol=https;AccountName=a",
        storage_azure_account_url="https://a.blob.core.windows.net",
        storage_azure_credential="key",
    )
    with patch("cloudpathlib.AzureBlobClient") as client_cls:
        open_skills_root("az://container/skills", settings)

    client_cls.assert_called_once_with(
        connection_string="DefaultEndpointsProtocol=https;AccountName=a",
        local_cache_dir=str(tmp_path / "cache" / "skills" / "objects"),
    )


def test_open_skills_root_azure_account_url_and_credential(tmp_path: Path) -> None:
    settings = _settings(
        data_dir=tmp_path,
        storage_azure_account_url="https://a.blob.core.windows.net",
        storage_azure_credential="sas-token",
    )
    with patch("cloudpathlib.AzureBlobClient") as client_cls:
        open_skills_root("az://container/skills", settings)

    client_cls.assert_called_once_with(
        account_url="https://a.blob.core.windows.net",
        credential="sas-token",
        local_cache_dir=str(tmp_path / "cache" / "skills" / "objects"),
    )


def test_open_skills_root_gcs(tmp_path: Path) -> None:
    settings = _settings(
        data_dir=tmp_path,
        storage_gcs_credentials_file=tmp_path / "sa.json",
        storage_gcs_project="proj",
    )
    with patch("cloudpathlib.GSClient") as client_cls:
        open_skills_root("gs://bucket/skills", settings)

    client_cls.assert_called_once_with(
        application_credentials=str(tmp_path / "sa.json"),
        project="proj",
        local_cache_dir=str(tmp_path / "cache" / "skills" / "objects"),
    )


def test_open_skills_root_missing_extra_is_a_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cloudpathlib", None)
    with pytest.raises(SkillLoadError, match=r"pincer-agent\[remote-skills\]"):
        open_skills_root("s3://bucket/team", _settings())


def test_open_skills_root_wraps_client_errors_without_leaking_secrets(tmp_path: Path) -> None:
    settings = _settings(data_dir=tmp_path, storage_s3_secret_access_key="super-secret")
    with (
        patch("boto3.Session", side_effect=RuntimeError("profile not found")),
        pytest.raises(SkillLoadError, match="Cannot open skills root s3://bucket/team") as exc_info,
    ):
        open_skills_root("s3://bucket/team", settings)
    assert "super-secret" not in str(exc_info.value)


def test_open_user_skills_root_logs_and_disables_on_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "cloudpathlib", None)
    settings = MagicMock(skills_dir="s3://bucket/team")
    with caplog.at_level(logging.ERROR):
        assert open_user_skills_root(settings) is None
    assert "remote-skills" in caplog.text


# ── open_usable_skills_root (startup) ────────────────────────────────────────


def test_usable_root_local_path_is_returned_even_if_missing(tmp_path: Path) -> None:
    assert open_usable_skills_root(str(tmp_path / "nope"), _settings()) == tmp_path / "nope"


def test_usable_root_reachable_cloud_root_passes(s3_skill_dir) -> None:
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=s3_skill_dir.parent):
        assert open_usable_skills_root("s3://bucket/team", _settings()) == s3_skill_dir.parent


def test_usable_root_empty_cloud_prefix_passes(s3_skill_dir) -> None:
    empty = s3_skill_dir.parent / "nothing-here-yet"
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=empty):
        assert open_usable_skills_root("s3://bucket/team/nothing-here-yet", _settings()) == empty


@pytest.mark.parametrize(
    ("uri", "expected_settings"),
    [
        ("s3://bucket/team", ["PINCER_STORAGE_S3_ACCESS_KEY_ID", "PINCER_STORAGE_S3_ENDPOINT_URL"]),
        ("az://container/team", ["PINCER_STORAGE_AZURE_CONNECTION_STRING", "PINCER_STORAGE_AZURE_ACCOUNT_URL"]),
        ("gs://bucket/team", ["PINCER_STORAGE_GCS_CREDENTIALS_FILE", "PINCER_STORAGE_GCS_PROJECT"]),
    ],
)
def test_usable_root_failed_listing_names_the_storage_settings(uri: str, expected_settings: list[str]) -> None:
    root = MagicMock()
    root.iterdir.side_effect = PermissionError("AccessDenied")
    with (
        patch("pincer.tools.skills.storage.open_skills_root", return_value=root),
        pytest.raises(SkillLoadError, match=f"Skills root {uri} is not usable: PermissionError") as exc_info,
    ):
        open_usable_skills_root(uri, _settings())
    for name in expected_settings:
        assert name in str(exc_info.value)


# ── open_usable_skills_root: Azure with hierarchical namespace (ADLS Gen2) ───
#
# These run the real `AzureBlobClient` path logic (cloudpathlib 0.26) and mock
# only the Azure SDK clients under it, so `is_dir()` takes the same route as on
# an HNS account: DFS `get_file_properties`, then the `list_blobs` fallback.


@pytest.fixture
def hns_azure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a real AzureBlobClient over mocked SDK clients for an HNS account.

    Returns the mocked service client; `.data_lake` is the mocked DFS client.
    By default the container exists and `skills/` does not."""
    pytest.importorskip("azure.storage.filedatalake")
    from azure.core.exceptions import ResourceNotFoundError
    from cloudpathlib import AzureBlobClient

    client = AzureBlobClient(connection_string=_FAKE_AZURE_CONNECTION_STRING, local_cache_dir=tmp_path / "azure-cache")
    service = MagicMock(name="BlobServiceClient")
    service.get_account_information.return_value = {"is_hns_enabled": True}
    service.get_container_client.return_value.list_blobs.side_effect = lambda **_: iter([])
    data_lake = MagicMock(name="DataLakeServiceClient")
    file_system = data_lake.get_file_system_client.return_value
    file_system.get_file_client.return_value.get_file_properties.side_effect = ResourceNotFoundError(
        "The specified path does not exist. ErrorCode:PathNotFound"
    )
    file_system.get_paths.side_effect = ResourceNotFoundError(
        "The specified path does not exist. ErrorCode:PathNotFound"
    )
    client.service_client = service
    client.data_lake_client = data_lake
    service.data_lake = data_lake
    monkeypatch.setattr("pincer.tools.skills.storage._build_client", lambda *_a: client)
    return service


def _assert_nothing_written(service: MagicMock) -> None:
    file_system = service.data_lake.get_file_system_client.return_value
    file_system.get_directory_client.return_value.create_directory.assert_not_called()
    service.get_blob_client.return_value.upload_blob.assert_not_called()


def test_usable_root_hns_missing_root_starts_empty_without_writing(
    hns_azure: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from pincer.tools.skills.index import SkillIndex

    with caplog.at_level(logging.WARNING, logger="pincer.tools.skills.storage"):
        root = open_usable_skills_root("az://3days/skills", _settings(data_dir=tmp_path))
    assert str(root) == "az://3days/skills"
    assert "Skills root az://3days/skills does not exist" in caplog.text
    # Listing a missing HNS directory 404s, so it must not be listed.
    hns_azure.data_lake.get_file_system_client.return_value.get_paths.assert_not_called()
    _assert_nothing_written(hns_azure)

    index = SkillIndex(bundled_dir=None, user_dir=root, max_per_root=100)
    index.discover(strict=True)
    assert index.all_skills() == []


def test_usable_root_hns_missing_container_refuses_to_start(hns_azure: MagicMock, tmp_path: Path) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    hns_azure.get_container_client.return_value.list_blobs.side_effect = ResourceNotFoundError(
        "The specified container does not exist. ErrorCode:ContainerNotFound"
    )
    with pytest.raises(SkillLoadError, match="is not usable: ResourceNotFoundError.*ContainerNotFound"):
        open_usable_skills_root("az://3days/skills", _settings(data_dir=tmp_path))
    _assert_nothing_written(hns_azure)


def test_usable_root_hns_existing_root_is_listed(hns_azure: MagicMock, tmp_path: Path) -> None:
    file_system = hns_azure.data_lake.get_file_system_client.return_value
    file_system.get_file_client.return_value.get_file_properties.side_effect = None
    file_system.get_file_client.return_value.get_file_properties.return_value = {"is_directory": True}
    file_system.get_paths.side_effect = None
    file_system.get_paths.return_value = [MagicMock(is_directory=True)]
    file_system.get_paths.return_value[0].name = "skills/demo"

    root = open_usable_skills_root("az://3days/skills", _settings(data_dir=tmp_path))
    file_system.get_paths.assert_called_once_with(path="skills", recursive=False)
    _assert_nothing_written(hns_azure)
    assert str(root) == "az://3days/skills"


def test_usable_root_hns_auth_error_refuses_to_start(hns_azure: MagicMock, tmp_path: Path) -> None:
    from azure.core.exceptions import ClientAuthenticationError

    file_system = hns_azure.data_lake.get_file_system_client.return_value
    file_system.get_file_client.return_value.get_file_properties.side_effect = ClientAuthenticationError(
        "Server failed to authenticate the request"
    )
    with pytest.raises(SkillLoadError, match="is not usable: ClientAuthenticationError.*PINCER_STORAGE_AZURE"):
        open_usable_skills_root("az://3days/skills", _settings(data_dir=tmp_path))
    _assert_nothing_written(hns_azure)


def _s3_client_without_bucket(tmp_path: Path):
    from botocore.exceptions import ClientError
    from cloudpathlib import S3Client

    client = S3Client(aws_access_key_id="x", aws_secret_access_key="x", local_cache_dir=tmp_path / "s3-cache")
    exceptions = client.client.exceptions  # cloudpathlib catches exceptions.NoSuchKey
    client.client = MagicMock(name="boto3.client", exceptions=exceptions)
    client.client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    client.s3 = MagicMock(name="boto3.resource")
    client.s3.Bucket.return_value.objects.filter.return_value.limit.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"}}, "ListObjectsV2"
    )
    return client


def _gs_client_without_bucket(tmp_path: Path):
    from cloudpathlib import GSClient
    from google.api_core.exceptions import NotFound

    storage_client = MagicMock(name="storage.Client")
    storage_client.bucket.return_value.get_blob.return_value = None  # google-cloud-storage swallows the 404
    storage_client.bucket.return_value.list_blobs.side_effect = NotFound("The specified bucket does not exist.")
    return GSClient(storage_client=storage_client, local_cache_dir=tmp_path / "gs-cache")


@pytest.mark.parametrize(
    ("scheme", "make_client"), [("s3", _s3_client_without_bucket), ("gs", _gs_client_without_bucket)]
)
def test_usable_root_missing_bucket_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str, make_client
) -> None:
    """The real S3/GCS clients (cloudpathlib 0.26) over mocked SDKs: `is_dir()` on a
    key in a missing bucket falls back to a prefix listing, which raises."""
    monkeypatch.setattr("pincer.tools.skills.storage._build_client", lambda *_a: make_client(tmp_path))
    with pytest.raises(SkillLoadError, match="is not usable: (ClientError.*NoSuchBucket|NotFound)"):
        open_usable_skills_root(f"{scheme}://missing-bucket/skills", _settings(data_dir=tmp_path))


@pytest.mark.parametrize(("backend", "scheme"), [("s3", "s3"), ("azure", "az"), ("gs", "gs")])
def test_usable_root_flat_backends_missing_prefix_starts(mock_cloud_client, backend: str, scheme: str) -> None:
    client = mock_cloud_client(backend)
    client.CloudPath(f"{scheme}://bucket/other/SKILL.md").write_text("x", encoding="utf-8")
    missing = client.CloudPath(f"{scheme}://bucket/skills")
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=missing):
        assert open_usable_skills_root(f"{scheme}://bucket/skills", _settings()) == missing
    assert list(missing.iterdir()) == []


def test_client_construction_error_names_the_storage_settings(tmp_path: Path) -> None:
    with (
        patch("boto3.Session", side_effect=RuntimeError("The config profile (team) could not be found")),
        pytest.raises(SkillLoadError, match="PINCER_STORAGE_S3_PROFILE"),
    ):
        open_usable_skills_root("s3://bucket/team", _settings(data_dir=tmp_path))


def test_usable_root_missing_extra_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cloudpathlib", None)
    with pytest.raises(SkillLoadError, match=r"pincer-agent\[remote-skills\]"):
        open_usable_skills_root("s3://bucket/team", _settings())


def test_open_skills_root_azure_account_url_only_uses_default_azure_credential(tmp_path: Path) -> None:
    """PR review: cloudpathlib would connect anonymously with credential=None, so a
    managed-identity setup needs DefaultAzureCredential built explicitly."""
    settings = _settings(data_dir=tmp_path, storage_azure_account_url="https://a.blob.core.windows.net")
    with (
        patch("azure.identity.DefaultAzureCredential") as default_credential,
        patch("cloudpathlib.AzureBlobClient") as client_cls,
    ):
        open_skills_root("az://container/skills", settings)

    default_credential.assert_called_once_with()
    client_cls.assert_called_once_with(
        account_url="https://a.blob.core.windows.net",
        credential=default_credential.return_value,
        local_cache_dir=str(tmp_path / "cache" / "skills" / "objects"),
    )


def test_open_skills_root_azure_explicit_credential_skips_the_default_chain(tmp_path: Path) -> None:
    settings = _settings(
        data_dir=tmp_path,
        storage_azure_account_url="https://a.blob.core.windows.net",
        storage_azure_credential="account-key",
    )
    with patch("azure.identity.DefaultAzureCredential") as default_credential, patch("cloudpathlib.AzureBlobClient"):
        open_skills_root("az://container/skills", settings)
    default_credential.assert_not_called()


def test_open_skills_root_azure_without_any_settings_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(SkillLoadError, match="MissingCredentialsError.*PINCER_STORAGE_AZURE_CONNECTION_STRING"):
        open_skills_root("az://container/skills", _settings(data_dir=tmp_path))


def test_remote_skills_extra_requires_a_traversal_safe_cloudpathlib() -> None:
    """PR review: cloudpathlib < 0.25 maps an object key like `a/../../.bashrc` to a
    cache path outside the cache dir; localize_skill_dir relies on the guard."""
    import tomllib

    from packaging.requirements import Requirement
    from packaging.version import Version

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    extra = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["optional-dependencies"]["remote-skills"]
    requirements = {Requirement(spec).name: Requirement(spec) for spec in extra}

    assert not requirements["cloudpathlib"].specifier.contains(Version("0.24.9"))
    assert requirements["cloudpathlib"].specifier.contains(Version("0.25.0"))
    assert "azure-identity" in requirements

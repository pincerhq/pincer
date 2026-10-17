"""Tests for pincer.tools.skills.storage — the local/cloud skill-root layer."""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pincer.config.main import _RelaxedSettings
from pincer.exceptions import SkillLoadError
from pincer.tools.path_sandbox import SandboxDenied
from pincer.tools.skills.storage import (
    clear_remote_cache,
    confine_skill_path,
    is_remote_uri,
    localize_skill_dir,
    open_skills_root,
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
    monkeypatch.setenv("PINCER_SKILLS_S3_SECRET_ACCESS_KEY", "shh")
    settings = _settings()
    assert settings.skills_dir == "gs://bucket/skills"
    assert settings.skills_s3_secret_access_key.get_secret_value() == "shh"
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


def test_localize_refreshes_changed_objects(s3_skill_dir) -> None:
    localize_skill_dir(s3_skill_dir)
    (s3_skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")

    local = localize_skill_dir(s3_skill_dir)

    assert (local / "scripts" / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho changed\n"


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
        skills_s3_endpoint_url="http://localhost:9000",
        skills_s3_region="eu-central-1",
        skills_s3_profile="team",
        skills_s3_access_key_id="AKIA",
        skills_s3_secret_access_key="secret",
        skills_s3_session_token="token",
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
        skills_azure_connection_string="DefaultEndpointsProtocol=https;AccountName=a",
        skills_azure_account_url="https://a.blob.core.windows.net",
        skills_azure_credential="key",
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
        skills_azure_account_url="https://a.blob.core.windows.net",
        skills_azure_credential="sas-token",
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
        skills_gcs_credentials_file=tmp_path / "sa.json",
        skills_gcs_project="proj",
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
    settings = _settings(data_dir=tmp_path, skills_s3_secret_access_key="super-secret")
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

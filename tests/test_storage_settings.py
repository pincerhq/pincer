"""Tests for the storage browser's settings, root resolution and doctor check."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from pincer.config.main import _RelaxedSettings
from pincer.security.doctor import CheckStatus, SecurityDoctor
from pincer.storage.browser import StorageBrowser
from pincer.storage.errors import StorageUnavailable
from pincer.storage.limits import DEFAULT_UPLOAD_MAX_BYTES
from pincer.storage.root import resolve_storage_root


def _settings(tmp_path: Path, **overrides: object) -> _RelaxedSettings:
    return _RelaxedSettings(data_dir=tmp_path, **overrides)  # type: ignore[arg-type]


# ── Settings ─────────────────────────────────────────────────────────────────


def test_upload_limit_defaults_to_20mb() -> None:
    assert DEFAULT_UPLOAD_MAX_BYTES == 20 * 1024 * 1024
    assert _RelaxedSettings().storage_upload_file_max_size == DEFAULT_UPLOAD_MAX_BYTES  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20MB", 20 * 1024**2), ("20Mb", 20 * 1024**2), ("512 kb", 512 * 1024), ("1G", 1024**3), ("2048", 2048)],
)
def test_upload_limit_setting_accepts_sizes(raw: str, expected: int) -> None:
    assert _RelaxedSettings(storage_upload_file_max_size=raw).storage_upload_file_max_size == expected  # type: ignore[call-arg]


def test_upload_limit_setting_rejects_nonsense() -> None:
    with pytest.raises(ValidationError):
        _RelaxedSettings(storage_upload_file_max_size="lots")  # type: ignore[call-arg]


@pytest.mark.parametrize("ttl", [59, 7 * 24 * 3600 + 1])
def test_presign_ttl_is_bounded(ttl: int) -> None:
    with pytest.raises(ValidationError):
        _RelaxedSettings(storage_presign_ttl=ttl)  # type: ignore[call-arg]


def test_storage_root_setting_keeps_uris() -> None:
    assert _RelaxedSettings(storage_root="s3://bucket/x").storage_root == "s3://bucket/x"  # type: ignore[call-arg]
    assert _RelaxedSettings(storage_root="").storage_root is None  # type: ignore[call-arg]


# ── Root resolution ──────────────────────────────────────────────────────────


def test_root_falls_back_to_cloud_skills_bucket(tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> None:
    settings = _settings(tmp_path, skills_dir="s3://acme/teams/skills/")
    with patch("pincer.storage.clients.build_client", return_value=mock_cloud_client("s3")) as build:
        root, protected = resolve_storage_root(settings)
    assert str(root).rstrip("/") == "s3://acme"
    assert protected == ["teams/skills"]
    assert build.call_args.args[0] == "s3"
    assert build.call_args.kwargs == {"file_cache_mode": "close_file"}


def test_root_is_the_whole_bucket_when_skills_sit_at_its_top(
    tmp_path: Path, mock_cloud_client: Callable[..., Any]
) -> None:
    with patch("pincer.storage.clients.build_client", return_value=mock_cloud_client("gs")):
        root, protected = resolve_storage_root(_settings(tmp_path, skills_dir="gs://acme"))
    assert protected == [""]
    assert StorageBrowser(root, protected, 900, b"k" * 32).is_protected("anything")


def test_root_defaults_to_data_dir_storage_for_local_skills(tmp_path: Path) -> None:
    root, protected = resolve_storage_root(_settings(tmp_path, skills_dir=tmp_path / "skills"))
    assert root == (tmp_path / "storage").resolve()
    assert root.is_dir()
    assert protected == []


def test_explicit_root_protects_skills_inside_it(tmp_path: Path) -> None:
    data = tmp_path / "data"
    settings = _settings(data, storage_root=str(tmp_path / "files"), skills_dir=tmp_path / "files" / "skills")
    root, protected = resolve_storage_root(settings)
    assert root == (tmp_path / "files").resolve()
    assert protected == ["skills"]

    outside = _settings(data, storage_root=str(tmp_path / "files"), skills_dir="s3://acme/skills")
    assert resolve_storage_root(outside)[1] == []


def test_explicit_cloud_root_protects_skills_under_it(tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> None:
    settings = _settings(tmp_path, storage_root="az://box/shared", skills_dir="az://box/shared/agents/skills")
    with patch("pincer.storage.clients.build_client", return_value=mock_cloud_client("azure")):
        _root, protected = resolve_storage_root(settings)
    assert protected == ["agents/skills"]


@pytest.mark.parametrize("root", [".", ".."], ids=["the-data-dir", "a-parent-of-it"])
def test_a_root_that_contains_the_data_dir_is_refused(tmp_path: Path, root: str) -> None:
    """The data dir holds the JWT secret and the database: serving it would let
    any identity forge sessions or replace the database."""
    data = tmp_path / "app" / "data"
    data.mkdir(parents=True)
    with pytest.raises(StorageUnavailable, match="contains the data dir"):
        resolve_storage_root(_settings(data, storage_root=str(data / root)))


def test_a_root_that_contains_the_data_dir_under_another_case_is_refused(tmp_path: Path) -> None:
    """`resolve()` keeps the case it was given; on a case-insensitive disk
    `CASE-PROBE` is `case-probe`, the data dir's parent."""
    (tmp_path / "probe").touch()
    if not (tmp_path / "PROBE").exists():
        pytest.skip("needs a case-insensitive filesystem")
    data = tmp_path / "case-probe" / "data"
    data.mkdir(parents=True)
    settings = _settings(data, storage_root=str(tmp_path / "CASE-PROBE"))
    with pytest.raises(StorageUnavailable, match="contains the data dir"):
        resolve_storage_root(settings)
    assert SecurityDoctor()._check_storage_root(settings).status == CheckStatus.CRITICAL


# ── Doctor ───────────────────────────────────────────────────────────────────


def test_doctor_flags_a_storage_root_that_contains_the_data_dir(tmp_path: Path) -> None:
    doctor = SecurityDoctor()
    data = tmp_path / "data"
    assert doctor._check_storage_root(_settings(data, storage_root=str(tmp_path))).status == CheckStatus.CRITICAL
    assert doctor._check_storage_root(_settings(data, storage_root=str(tmp_path / "files"))).status == CheckStatus.PASS
    assert doctor._check_storage_root(_settings(data)).status == CheckStatus.SKIPPED
    assert doctor._check_storage_root(_settings(data, storage_root="s3://b/")).status == CheckStatus.SKIPPED

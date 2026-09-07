"""Tests for _safe_upload_path — the path-traversal guard around attachment
saving in cli/run.py's on_message handler (see PR #183 review discussion)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pincer.cli._attachments import _safe_upload_path


def test_normal_filename_resolves_under_uploads_dir(tmp_path: Path) -> None:
    save_path = _safe_upload_path(tmp_path, "report.pdf")
    assert save_path == tmp_path / "report.pdf"


def test_relative_traversal_is_confined_to_basename(tmp_path: Path) -> None:
    save_path = _safe_upload_path(tmp_path, "../../../pwned.txt")
    assert save_path == tmp_path / "pwned.txt"


def test_absolute_path_is_confined_to_basename(tmp_path: Path) -> None:
    save_path = _safe_upload_path(tmp_path, "/tmp/absolute_pwned.txt")
    assert save_path == tmp_path / "absolute_pwned.txt"


def test_bare_parent_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe attachment filename"):
        _safe_upload_path(tmp_path, "..")


def test_empty_filename_falls_back_to_generated_name(tmp_path: Path) -> None:
    save_path = _safe_upload_path(tmp_path, "")
    assert save_path.parent == tmp_path
    assert save_path.name.startswith("file_")

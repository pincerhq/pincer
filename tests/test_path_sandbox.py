"""Tests for path confinement and the file tools' sandbox denial behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from pincer.config.main import _RelaxedSettings
from pincer.tools.builtin import files
from pincer.tools.path_sandbox import SandboxDenied, confine_path


def test_confine_path_allows_path_inside_base(tmp_path: Path) -> None:
    target = confine_path(tmp_path, "subdir/file.txt", label="workspace")
    assert str(target).startswith(str(tmp_path.resolve()))


def test_confine_path_denies_path_outside_base(tmp_path: Path) -> None:
    with pytest.raises(SandboxDenied):
        confine_path(tmp_path, "/etc/passwd", label="workspace")


def test_confine_path_denies_traversal(tmp_path: Path) -> None:
    with pytest.raises(SandboxDenied):
        confine_path(tmp_path, "../outside.txt", label="workspace")


def test_sandbox_path_raises_sandbox_denied_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws_settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr(files, "get_settings", lambda: ws_settings)

    with pytest.raises(SandboxDenied) as excinfo:
        files._sandbox_path("/etc/passwd")

    assert "All file operations are sandboxed." in str(excinfo.value)


async def test_file_read_outside_workspace_raises_sandbox_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_settings = _RelaxedSettings(data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr(files, "get_settings", lambda: ws_settings)

    with pytest.raises(SandboxDenied, match="outside workspace"):
        await files.file_read("/etc/passwd")

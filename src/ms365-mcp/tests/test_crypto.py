"""Tests for the Fernet key load-or-generate logic (issue #155)."""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from ms365.config import MS365Settings
from ms365.crypto import load_or_generate_fernet


def test_generates_key_file_with_0600_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "token_encryption.key"
    settings = MS365Settings(token_encryption_key_path=key_path)  # type: ignore[arg-type]

    load_or_generate_fernet(settings)

    assert key_path.exists()
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_reload_is_idempotent(tmp_path: Path) -> None:
    key_path = tmp_path / "token_encryption.key"
    settings = MS365Settings(token_encryption_key_path=key_path)  # type: ignore[arg-type]

    first = load_or_generate_fernet(settings)
    second = load_or_generate_fernet(MS365Settings(token_encryption_key_path=key_path))  # type: ignore[arg-type]

    token = first.encrypt(b"payload")
    assert second.decrypt(token) == b"payload"


def test_env_key_overrides_file_and_never_touches_disk(tmp_path: Path) -> None:
    key_path = tmp_path / "does-not-exist" / "token_encryption.key"
    raw_key = Fernet.generate_key().decode()
    settings = MS365Settings(
        token_encryption_key=raw_key,  # type: ignore[arg-type]
        token_encryption_key_path=key_path,  # type: ignore[arg-type]
    )

    fernet = load_or_generate_fernet(settings)

    assert not key_path.exists()
    assert fernet.decrypt(Fernet(raw_key.encode()).encrypt(b"payload")) == b"payload"

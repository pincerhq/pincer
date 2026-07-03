"""Tests for Fernet key resolution (issue #155)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from ms365.config import MS365Settings
from ms365.crypto import resolve_fernet


def test_resolve_fernet_returns_none_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MS365_TOKEN_ENCRYPTION_KEY", raising=False)
    settings = MS365Settings(_env_file=None)  # type: ignore[call-arg]

    assert resolve_fernet(settings) is None


def test_resolve_fernet_returns_working_fernet_when_key_set() -> None:
    raw_key = Fernet.generate_key().decode()
    settings = MS365Settings(token_encryption_key=raw_key)  # type: ignore[arg-type]

    fernet = resolve_fernet(settings)

    assert fernet is not None
    assert fernet.decrypt(Fernet(raw_key.encode()).encrypt(b"payload")) == b"payload"


def test_resolve_fernet_raises_clear_error_for_invalid_key() -> None:
    settings = MS365Settings(token_encryption_key="not-a-valid-hex-key-1234567890")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Generate one with"):
        resolve_fernet(settings)

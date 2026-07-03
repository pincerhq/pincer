"""Tests for MS365 config (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ms365.config import _DEFAULT_SERVICES, MS365Settings, get_settings


def test_defaults() -> None:
    cfg = MS365Settings()
    assert cfg.client_id == ""
    assert cfg.tenant_id == "common"
    assert cfg.auth_method == "device_code"
    assert cfg.services == list(_DEFAULT_SERVICES)


def test_default_services_count() -> None:
    cfg = MS365Settings()
    assert len(cfg.services) == len(_DEFAULT_SERVICES)


def test_client_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "env-client-id")
    cfg = MS365Settings()
    assert cfg.client_id == "env-client-id"


def test_explicit_client_id_not_overwritten_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "env-id")
    cfg = MS365Settings(client_id="explicit-id")
    assert cfg.client_id == "explicit-id"


def test_tenant_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_TENANT_ID", "my-tenant")
    cfg = MS365Settings()
    assert cfg.tenant_id == "my-tenant"


def test_token_cache_dir_default() -> None:
    cfg = MS365Settings()
    assert cfg.token_cache_dir == Path.home() / ".pincer" / "ms365_mcp"


def test_token_cache_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_TOKEN_CACHE_DIR", "/tmp/ms365-identities")
    cfg = MS365Settings()
    assert cfg.token_cache_dir == Path("/tmp/ms365-identities")


def test_token_cache_dir_expands_home() -> None:
    cfg = MS365Settings(token_cache_dir="~/.pincer/other-ms365")  # type: ignore[arg-type]
    assert "~" not in str(cfg.token_cache_dir)
    assert cfg.token_cache_dir == Path.home() / ".pincer" / "other-ms365"


def test_services_csv_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_SERVICES", "email,calendar")
    cfg = MS365Settings()
    assert cfg.services == ["email", "calendar"]


def test_services_csv_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_SERVICES", " email , calendar ")
    cfg = MS365Settings()
    assert cfg.services == ["email", "calendar"]


def test_services_json_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_SERVICES", '["email","todo"]')
    cfg = MS365Settings()
    assert cfg.services == ["email", "todo"]


def test_empty_client_id_env_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "")
    cfg = MS365Settings()
    assert cfg.client_id == ""


def test_get_settings_returns_ms365settings() -> None:
    cfg = get_settings()
    assert isinstance(cfg, MS365Settings)


def test_get_settings_is_cached() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_get_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "from-env")
    cfg = get_settings()
    assert cfg.client_id == "from-env"


def test_client_id_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MS365Settings reads client_id from .env when env var is absent."""
    monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MS365_CLIENT_ID=dotenv-client-id\n")
    cfg = MS365Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert cfg.client_id == "dotenv-client-id"


def test_env_var_takes_priority_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variable wins over .env file value."""
    monkeypatch.setenv("MS365_CLIENT_ID", "env-wins")
    env_file = tmp_path / ".env"
    env_file.write_text("MS365_CLIENT_ID=dotenv-loses\n")
    cfg = MS365Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert cfg.client_id == "env-wins"

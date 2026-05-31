"""Tests for MS365 config loading and resolution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ms365.config import MS365IntegrationConfig, load_config, resolve_cache_path

if TYPE_CHECKING:
    import pytest


def test_default_tenant_and_services() -> None:
    cfg = MS365IntegrationConfig()
    assert cfg.tenant_id == "common"
    assert cfg.enabled is True
    assert len(cfg.services) == 7


def test_resolve_cache_path_default() -> None:
    cfg = MS365IntegrationConfig()
    assert resolve_cache_path(cfg) == Path.home() / ".pincer" / "ms365_token_cache.json"


def test_resolve_cache_path_custom() -> None:
    cfg = MS365IntegrationConfig(token_cache_path="/tmp/custom_tokens.json")
    assert resolve_cache_path(cfg) == Path("/tmp/custom_tokens.json")


def test_post_init_reads_client_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MS365_CLIENT_ID", "env-client-id")
    cfg = MS365IntegrationConfig()
    assert cfg.client_id == "env-client-id"


def test_post_init_explicit_client_id_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MS365_CLIENT_ID", "env-client-id")
    cfg = MS365IntegrationConfig(client_id="explicit-id")
    assert cfg.client_id == "explicit-id"


def test_post_init_reads_tenant_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MS365_TENANT_ID", "my-tenant")
    cfg = MS365IntegrationConfig()
    assert cfg.tenant_id == "my-tenant"


def test_post_init_common_tenant_not_overwritten_by_common_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINCER_MS365_TENANT_ID", "common")
    cfg = MS365IntegrationConfig()
    assert cfg.tenant_id == "common"


def test_load_config_returns_defaults_without_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_MS365_CLIENT_ID", raising=False)
    cfg = load_config()
    assert cfg.client_id == ""
    assert cfg.enabled is True


def test_load_config_parses_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pincer.toml").write_text(
        '[integrations.ms365]\nenabled = true\nclient_id = "toml-id"\ntenant_id = "my-org"\n'
    )
    cfg = load_config()
    assert cfg.client_id == "toml-id"
    assert cfg.tenant_id == "my-org"


def test_load_config_env_var_reference_in_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_CLIENT_ID", "from-env")
    (tmp_path / "pincer.toml").write_text('[integrations.ms365]\nclient_id = "${MY_CLIENT_ID}"\n')
    cfg = load_config()
    assert cfg.client_id == "from-env"

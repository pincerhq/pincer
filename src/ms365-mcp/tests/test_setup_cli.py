"""Tests for ms365 setup wizard helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from ms365.setup_cli import _get_ms365_config, _load_dotenv


class TestLoadDotenv:
    def test_parses_key_value(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=bar\nBAZ=qux\n")
        assert _load_dotenv(env) == {"FOO": "bar", "BAZ": "qux"}

    def test_ignores_comments_and_blanks(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# comment\n\nFOO=bar\n")
        assert _load_dotenv(env) == {"FOO": "bar"}

    def test_strips_quotes(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('FOO="quoted"\nBAR=\'single\'\n')
        assert _load_dotenv(env) == {"FOO": "quoted", "BAR": "single"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _load_dotenv(tmp_path / "nonexistent.env") == {}

    def test_value_with_equals_sign(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=a=b=c\n")
        assert _load_dotenv(env) == {"FOO": "a=b=c"}


class TestGetMs365Config:
    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MS365_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("PINCER_MS365_TENANT_ID", "env-tenant")
        client_id, tenant_id = _get_ms365_config()
        assert client_id == "env-client-id"
        assert tenant_id == "env-tenant"

    def test_falls_back_to_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
        monkeypatch.delenv("PINCER_MS365_TENANT_ID", raising=False)
        env = tmp_path / ".env"
        env.write_text("MS365_CLIENT_ID=file-client-id\nPINCER_MS365_TENANT_ID=file-tenant\n")
        monkeypatch.chdir(tmp_path)
        client_id, tenant_id = _get_ms365_config()
        assert client_id == "file-client-id"
        assert tenant_id == "file-tenant"

    def test_tenant_defaults_to_consumers(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
        monkeypatch.delenv("PINCER_MS365_TENANT_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        client_id, tenant_id = _get_ms365_config()
        assert tenant_id == "consumers"
        assert client_id == ""

    def test_env_takes_priority_over_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MS365_CLIENT_ID", "env-wins")
        monkeypatch.delenv("PINCER_MS365_TENANT_ID", raising=False)
        env = tmp_path / ".env"
        env.write_text("MS365_CLIENT_ID=file-loses\nPINCER_MS365_TENANT_ID=file-tenant\n")
        monkeypatch.chdir(tmp_path)
        client_id, tenant_id = _get_ms365_config()
        assert client_id == "env-wins"
        assert tenant_id == "file-tenant"


class TestMainExitsWhenClientIdMissing:
    def test_exits_with_code_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
        monkeypatch.delenv("PINCER_MS365_TENANT_ID", raising=False)
        monkeypatch.chdir(tmp_path)

        from ms365.setup_cli import main

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "MS365_CLIENT_ID" in captured.out
        assert "ms365-mcp-setup" in captured.out

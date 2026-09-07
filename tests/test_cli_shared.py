"""Direct unit tests for the helpers in pincer.cli._shared."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest

# ── _find_env_file ────────────────────────────────────────────────────────


def test_find_env_file_defaults_when_none_exist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pincer.cli._shared import _find_env_file

    subdir = tmp_path / "work"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fakehome")

    result = _find_env_file()

    assert result == str((subdir / ".env").resolve())


def test_find_env_file_finds_cwd_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pincer.cli._shared import _find_env_file

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("X=1\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fakehome")

    result = _find_env_file()

    assert result == str((tmp_path / ".env").resolve())


# ── _upsert_env ───────────────────────────────────────────────────────────


def test_upsert_env_creates_new_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pincer.cli._shared import _upsert_env

    env_path = str(tmp_path / ".env")
    _upsert_env(env_path, "FOO", "bar")

    assert (tmp_path / ".env").read_text() == "FOO=bar\n"


def test_upsert_env_updates_existing_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pincer.cli._shared import _upsert_env

    env_file = tmp_path / ".env"
    env_file.write_text("FOO=old\nBAR=baz\n")

    _upsert_env(str(env_file), "FOO", "new")

    content = env_file.read_text()
    assert "FOO=new" in content
    assert "BAR=baz" in content
    assert "FOO=old" not in content


# ── _port_in_use ──────────────────────────────────────────────────────────


def test_port_in_use_false_when_nothing_listening() -> None:
    from pincer.cli._shared import _port_in_use

    assert _port_in_use("127.0.0.1", 65432) is False


def test_port_in_use_true_when_listening() -> None:
    from pincer.cli._shared import _port_in_use

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _port_in_use("127.0.0.1", port) is True
    finally:
        srv.close()


def test_port_in_use_normalizes_wildcard_host() -> None:
    from pincer.cli._shared import _port_in_use

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _port_in_use("0.0.0.0", port) is True
    finally:
        srv.close()


# ── _print_voice_webhook_urls ────────────────────────────────────────────


def test_print_voice_webhook_urls_noop_without_base_url() -> None:
    from pincer.cli._shared import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = ""
    console = MagicMock()

    _print_voice_webhook_urls(settings, console)

    console.print.assert_not_called()


def test_print_voice_webhook_urls_conversation_relay() -> None:
    from pincer.cli._shared import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = "https://example.com/"
    settings.voice_engine = "conversation_relay"
    console = MagicMock()

    _print_voice_webhook_urls(settings, console)

    printed = "\n".join(call.args[0] for call in console.print.call_args_list)
    assert "ConversationRelay" in printed


def test_print_voice_webhook_urls_media_streams() -> None:
    from pincer.cli._shared import _print_voice_webhook_urls

    settings = MagicMock()
    settings.voice_webhook_base_url = "https://example.com"
    settings.voice_engine = "media_streams"
    console = MagicMock()

    _print_voice_webhook_urls(settings, console)

    printed = "\n".join(call.args[0] for call in console.print.call_args_list)
    assert "Media stream WS" in printed
    assert "wss://example.com" in printed


# ── _create_memory_backend ───────────────────────────────────────────────


def test_create_memory_backend_sqlite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pincer.cli._shared import _create_memory_backend
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = MagicMock()
    settings.memory_backend = "sqlite"
    settings.db_path = tmp_path / "pincer.db"

    backend = _create_memory_backend(settings)

    assert isinstance(backend, SQLiteMemoryBackend)


def test_create_memory_backend_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.cli._shared import _create_memory_backend

    settings = MagicMock()
    settings.memory_backend = "mcp"
    settings.memory_mcp_server = "sqlite-vec-memory"

    mock_backend = MagicMock()
    mock_cls = MagicMock(return_value=mock_backend)
    monkeypatch.setattr("pincer.memory.mcp.MCPMemoryBackend", mock_cls)

    backend = _create_memory_backend(settings)

    assert backend is mock_backend
    mock_cls.assert_called_once_with(server_name="sqlite-vec-memory")

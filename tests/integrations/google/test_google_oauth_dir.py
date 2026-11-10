"""
Where the Google OAuth files are looked up (issue #226) — not the working directory —
and how a token from PINCER_GOOGLE_TOKEN_JSON combines with an OAuth client from a
file or PINCER_GOOGLE_CREDENTIALS_JSON.

Real settings built from the environment, run from a working directory unrelated to
``data_dir``, as a service manager or another shell would start Pincer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pincer.config.main import _RelaxedSettings
from pincer.integrations.google import get_google_factory, google_status
from pincer.integrations.google.auth import ALL_SCOPES
from pincer.integrations.google.token_source import client_config, encode_json_blob, locate_token

FILE_CLIENT = {"installed": {"client_id": "file-cid", "client_secret": "file-secret"}}
ENV_CLIENT = {"installed": {"client_id": "env-cid", "client_secret": "env-secret"}}
# A token as `pincer google setup` writes it, trimmed to what is specific to the user.
TRIMMED_TOKEN = {
    "token": "ya29.fake",
    "refresh_token": "1//fake",
    "scopes": ALL_SCOPES,
    "expiry": "2099-01-01T00:00:00Z",
}


def _full_token(client_id: str = "file-cid", client_secret: str = "file-secret") -> dict:
    return {**TRIMMED_TOKEN, "client_id": client_id, "client_secret": client_secret}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """``data_dir`` and an unrelated working directory (the cwd for the test)."""
    data_dir, cwd = tmp_path / "home" / ".pincer", tmp_path / "elsewhere"
    data_dir.mkdir(parents=True)
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PINCER_DATA_DIR", str(data_dir))
    return data_dir, cwd


def _settings() -> _RelaxedSettings:
    return _RelaxedSettings(_env_file=None)  # type: ignore[call-arg]


def _legacy_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "deprecated" in r.getMessage()]


# ── google_oauth_dir() ────────────────────────────────────────────────────


def test_token_in_data_dir_is_found_from_any_working_directory(dirs, caplog):
    data_dir, _ = dirs
    (data_dir / "google_workspace_token.json").write_text(json.dumps(_full_token()))
    settings = _settings()
    assert settings.google_oauth_dir() == data_dir
    assert locate_token(settings)["token_path"] == str(data_dir / "google_workspace_token.json")
    assert _legacy_warnings(caplog) == []


def test_files_in_cwd_data_are_still_found_with_a_deprecation_warning(dirs, caplog):
    data_dir, cwd = dirs
    legacy = cwd / "data"
    legacy.mkdir()
    (legacy / "google_workspace_token.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="pincer.config.main"):
        assert _settings().google_oauth_dir() == legacy
        assert _settings().google_oauth_dir() == legacy
    [warning] = _legacy_warnings(caplog)  # once, not per lookup
    assert str(legacy) in warning.getMessage()
    assert str(data_dir) in warning.getMessage()
    assert "PINCER_GOOGLE_OAUTH_DIR" in warning.getMessage()


def test_data_dir_wins_over_cwd_data(dirs, caplog):
    data_dir, cwd = dirs
    (cwd / "data").mkdir()
    (cwd / "data" / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    assert _settings().google_oauth_dir() == data_dir
    assert _legacy_warnings(caplog) == []


def test_explicit_dir_wins_over_data_dir_and_cwd_data(dirs, tmp_path, monkeypatch):
    data_dir, cwd = dirs
    explicit = tmp_path / "oauth"
    for directory in (data_dir, cwd / "data", explicit):
        directory.mkdir(exist_ok=True)
        (directory / "google_workspace_token.json").write_text("{}")
    monkeypatch.setenv("PINCER_GOOGLE_OAUTH_DIR", str(explicit))
    settings = _settings()
    assert settings.google_oauth_dir() == explicit
    assert locate_token(settings)["token_path"] == str(explicit / "google_workspace_token.json")


def test_without_files_anywhere_setup_writes_to_data_dir(dirs, caplog):
    data_dir, cwd = dirs
    (cwd / "data").mkdir()  # an empty ./data does not count
    assert _settings().google_oauth_dir() == data_dir
    assert _legacy_warnings(caplog) == []


# ── OAuth client from a file, token from PINCER_GOOGLE_TOKEN_JSON ─────────


def test_env_token_with_its_own_client_is_used_as_is(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(_full_token()))
    with caplog.at_level(logging.WARNING):
        location = locate_token(_settings())
    assert location == {"token_info": _full_token(), "client_source": "token"}
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_trimmed_env_token_gets_the_client_from_the_file(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(TRIMMED_TOKEN))
    settings = _settings()
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    factory = get_google_factory()

    assert factory is not None
    assert factory.auth.client_source == f"file:{data_dir / 'google_credentials.json'}"
    creds = factory.auth.get_credentials()  # usable, not failing at the first tool call
    assert (creds.client_id, creds.client_secret) == ("file-cid", "file-secret")
    assert google_status(settings)["client_source"] == f"file:{data_dir / 'google_credentials.json'}"
    assert "client_id" not in TRIMMED_TOKEN  # filled into a copy


def test_trimmed_env_token_gets_the_client_from_the_env(dirs, monkeypatch):
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(TRIMMED_TOKEN))
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", json.dumps(ENV_CLIENT))
    location = locate_token(_settings())
    assert location["client_source"] == "env"
    assert (location["token_info"]["client_id"], location["token_info"]["client_secret"]) == ("env-cid", "env-secret")


def test_client_file_wins_over_client_env(dirs, monkeypatch):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", json.dumps(ENV_CLIENT))
    client = client_config(_settings())
    assert client is not None
    assert (client.client_id, client.source) == ("file-cid", f"file:{data_dir / 'google_credentials.json'}")


def test_trimmed_env_token_without_any_client_fails_at_startup(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(TRIMMED_TOKEN))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        assert locate_token(_settings()) is None
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "lacks client_id/client_secret" in warning
    assert str(data_dir / "google_credentials.json") in warning
    assert "PINCER_GOOGLE_CREDENTIALS_JSON" in warning
    assert "1//fake" not in caplog.text


def test_env_token_for_another_client_keeps_its_own_client(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(_full_token("old-cid", "old-secret")))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        location = locate_token(_settings())
        locate_token(_settings())
    assert location["client_source"] == "token"
    assert location["token_info"]["client_id"] == "old-cid"
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]  # once
    assert "old-cid" in warning
    assert "file-cid" in warning
    assert "re-running `pincer google setup` would issue a token for a different OAuth client" in warning


def test_broken_env_token_falls_back_to_the_token_file(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_workspace_token.json").write_text(json.dumps(_full_token()))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", "not-a-token")
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        location = locate_token(_settings())
    assert location["token_path"] == str(data_dir / "google_workspace_token.json")
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Ignoring PINCER_GOOGLE_TOKEN_JSON" in warning
    assert f"falling back to token files in {data_dir}" in warning


def test_status_reports_a_file_token_with_its_own_client(dirs):
    data_dir, _ = dirs
    (data_dir / "google_workspace_token.json").write_text(json.dumps(_full_token()))
    state = google_status(_settings())
    assert (state["status"], state["source"], state["client_source"]) == ("active", "file", "token")


def test_a_token_in_cwd_data_wins_over_a_bare_client_file_in_data_dir(dirs, caplog):
    """Only google_credentials.json in data_dir must not hide a working token in ./data."""
    data_dir, cwd = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    legacy = cwd / "data"
    legacy.mkdir()
    (legacy / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    (legacy / "google_workspace_token.json").write_text(json.dumps(_full_token()))
    with caplog.at_level(logging.WARNING, logger="pincer.config.main"):
        settings = _settings()
        assert settings.google_oauth_dir() == legacy
        assert locate_token(settings)["token_path"] == str(legacy / "google_workspace_token.json")
    assert len(_legacy_warnings(caplog)) == 1


def test_a_client_file_in_data_dir_wins_over_one_in_cwd_data(dirs, caplog):
    data_dir, cwd = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    (cwd / "data").mkdir()
    (cwd / "data" / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    assert _settings().google_oauth_dir() == data_dir
    assert _legacy_warnings(caplog) == []


def test_a_relative_explicit_dir_is_logged(dirs, monkeypatch, caplog):
    _, cwd = dirs
    monkeypatch.setenv("PINCER_GOOGLE_OAUTH_DIR", "oauth")
    with caplog.at_level(logging.WARNING, logger="pincer.config.main"):
        assert _settings().google_oauth_dir() == Path("oauth")
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "is relative" in warning
    assert str(cwd) in warning


def test_absolute_explicit_dir_is_not_logged(dirs, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PINCER_GOOGLE_OAUTH_DIR", str(tmp_path / "oauth"))
    with caplog.at_level(logging.WARNING, logger="pincer.config.main"):
        _settings().google_oauth_dir()
    assert caplog.records == []


def test_a_stray_client_secret_is_not_paired_with_another_clients_id(dirs, monkeypatch):
    """A token without client_id takes the id and the secret from one client, never half of each."""
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob({**TRIMMED_TOKEN, "client_secret": "stray"}))
    info = locate_token(_settings())["token_info"]
    assert (info["client_id"], info["client_secret"]) == ("file-cid", "file-secret")


def test_a_token_with_its_client_id_gets_only_the_secret_of_that_client(dirs, monkeypatch):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob({**TRIMMED_TOKEN, "client_id": "file-cid"}))
    location = locate_token(_settings())
    assert (location["token_info"]["client_secret"], location["client_source"]) == (
        "file-secret",
        f"file:{data_dir / 'google_credentials.json'}",
    )


def test_a_complete_env_token_reads_the_client_file_once(dirs, monkeypatch):
    """The client only feeds a once-only mismatch warning: later lookups skip reading it."""
    from pincer.integrations.google import token_source

    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(_full_token()))
    reads: list[object] = []
    real = token_source.oauth_clients
    monkeypatch.setattr(token_source, "oauth_clients", lambda *a: reads.append(a) or real(*a))
    settings = _settings()
    for _ in range(3):
        assert locate_token(settings)["client_source"] == "token"
    assert len(reads) == 1


# ── Review round 2 (PR #368) ──────────────────────────────────────────────


def test_a_token_with_a_client_id_gets_the_secret_from_the_env_client_with_that_id(dirs, monkeypatch, caplog):
    """The file holds another client: the env client with the token's id supplies the secret."""
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", json.dumps(ENV_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob({**TRIMMED_TOKEN, "client_id": "env-cid"}))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        location = locate_token(_settings())
    assert location is not None
    assert (location["token_info"]["client_id"], location["token_info"]["client_secret"]) == ("env-cid", "env-secret")
    assert location["client_source"] == "env"
    assert not any("no matching OAuth client" in r.getMessage() for r in caplog.records)


def test_a_token_with_a_client_id_no_source_has_is_turned_off(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", json.dumps(ENV_CLIENT))
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob({**TRIMMED_TOKEN, "client_id": "other-cid"}))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        assert locate_token(_settings()) is None
    assert any("no matching OAuth client" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("source", ["file", "env"])
def test_a_client_without_a_secret_is_not_used(dirs, monkeypatch, caplog, source):
    """Filling in only client_id would register the tools and fail at the first call."""
    data_dir, _ = dirs
    no_secret = json.dumps({"installed": {"client_id": "G"}})
    if source == "file":
        (data_dir / "google_credentials.json").write_text(no_secret)
    else:
        monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", no_secret)
    monkeypatch.setenv("PINCER_GOOGLE_TOKEN_JSON", encode_json_blob(TRIMMED_TOKEN))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        assert client_config(_settings()) is None
        assert locate_token(_settings()) is None
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "client_id and client_secret" in messages
    assert "Google Workspace tools off" in messages


def test_a_legacy_token_in_data_dir_does_not_hide_a_full_token_in_cwd_data(dirs):
    data_dir, cwd = dirs
    (data_dir / "google_token.json").write_text(json.dumps(_full_token()))
    (cwd / "data").mkdir()
    (cwd / "data" / "google_workspace_token.json").write_text(json.dumps(_full_token()))
    settings = _settings()
    assert settings.google_oauth_dir() == cwd / "data"
    assert locate_token(settings)["token_path"] == str(cwd / "data" / "google_workspace_token.json")


def test_a_legacy_token_in_data_dir_still_wins_over_a_bare_client_in_cwd_data(dirs):
    data_dir, cwd = dirs
    (data_dir / "google_token.json").write_text(json.dumps(_full_token()))
    (cwd / "data").mkdir()
    (cwd / "data" / "google_credentials.json").write_text(json.dumps(FILE_CLIENT))
    assert _settings().google_oauth_dir() == data_dir


def test_an_unknown_home_in_the_explicit_dir_is_a_config_error(dirs, monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("PINCER_GOOGLE_OAUTH_DIR", "~nouserxyz-pincer/x")
    with pytest.raises(ValidationError, match="PINCER_GOOGLE_OAUTH_DIR=~nouserxyz-pincer/x: cannot expand"):
        _settings()


def test_the_explicit_dir_expands_the_home(dirs, monkeypatch):
    monkeypatch.setenv("PINCER_GOOGLE_OAUTH_DIR", "~/pincer-oauth")
    assert _settings().google_oauth_dir() == Path.home() / "pincer-oauth"


def test_an_unreadable_client_file_falls_back_to_the_client_env(dirs, monkeypatch, caplog):
    data_dir, _ = dirs
    (data_dir / "google_credentials.json").write_text("{not json")
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", json.dumps(ENV_CLIENT))
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        client = client_config(_settings())
    assert client is not None
    assert client.source == "env"
    assert f"Ignoring {data_dir / 'google_credentials.json'}" in caplog.text


def test_an_undecodable_client_env_is_logged_without_leaking(dirs, monkeypatch, caplog):
    monkeypatch.setenv("PINCER_GOOGLE_CREDENTIALS_JSON", "secret-client-garbage")
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        assert client_config(_settings()) is None
    assert "Ignoring PINCER_GOOGLE_CREDENTIALS_JSON" in caplog.text
    assert "secret-client-garbage" not in caplog.text

"""
Tests for PINCER_GOOGLE_TOKEN_JSON / PINCER_GOOGLE_CREDENTIALS_JSON decoding and token lookup.
"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from pincer.integrations.google.token_source import (
    decode_json_blob,
    encode_json_blob,
    env_client_config,
    env_token_info,
    locate_token,
)

TOKEN = {"refresh_token": "1//secret-refresh", "client_id": "cid", "client_secret": "csecret"}


def _settings(token: str = "", credentials: str = "", oauth_dir=None):
    return SimpleNamespace(
        google_token_json=SecretStr(token),
        google_credentials_json=SecretStr(credentials),
        google_oauth_dir=lambda: oauth_dir,
    )


def test_decode_raw_json():
    assert decode_json_blob(json.dumps(TOKEN)) == TOKEN


def test_decode_base64():
    assert decode_json_blob(base64.b64encode(json.dumps(TOKEN).encode()).decode()) == TOKEN


def test_decode_urlsafe_base64_without_padding():
    blob = base64.urlsafe_b64encode(json.dumps({"k": "??>>"}).encode()).decode().rstrip("=")
    assert decode_json_blob(blob) == {"k": "??>>"}


def test_decode_tolerates_surrounding_whitespace():
    assert decode_json_blob(f"  {encode_json_blob(TOKEN)}\n") == TOKEN


def test_encode_round_trip():
    assert decode_json_blob(encode_json_blob(TOKEN)) == TOKEN
    assert decode_json_blob(encode_json_blob(json.dumps(TOKEN))) == TOKEN


@pytest.mark.parametrize("value", ["not json", "[1, 2]", "42", base64.b64encode(b"[1]").decode()])
def test_decode_rejects_non_objects(value):
    with pytest.raises(ValueError, match="JSON object"):
        decode_json_blob(value)


def test_env_token_info_unset_returns_none():
    assert env_token_info(_settings()) is None


def test_env_token_info_reads_setting():
    assert env_token_info(_settings(token=encode_json_blob(TOKEN))) == TOKEN


def test_env_token_info_invalid_logs_without_leaking(caplog):
    with caplog.at_level(logging.WARNING, logger="pincer.integrations.google.token_source"):
        assert env_token_info(_settings(token="1//secret-refresh-garbage")) is None
    assert "PINCER_GOOGLE_TOKEN_JSON" in caplog.text
    assert "secret-refresh" not in caplog.text


def test_env_token_info_ignores_non_secret_settings():
    # MagicMock-style settings (tests, partial configs) must not be treated as a token.
    assert env_token_info(SimpleNamespace(google_token_json=None)) is None
    assert env_token_info(SimpleNamespace()) is None


def test_env_client_config_reads_setting():
    client = {"installed": {"client_id": "cid"}}
    assert env_client_config(_settings(credentials=json.dumps(client))) == client


def test_locate_token_prefers_env_over_files(tmp_path):
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_workspace_token.json").write_text("{}")
    assert locate_token(_settings(token=json.dumps(TOKEN), oauth_dir=tmp_path)) == {"token_info": TOKEN}


def test_locate_token_files(tmp_path):
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")
    assert locate_token(_settings(oauth_dir=tmp_path)) == {
        "credentials_path": str(tmp_path / "google_credentials.json"),
        "token_path": str(tmp_path / "google_token.json"),
    }
    (tmp_path / "google_workspace_token.json").write_text("{}")
    assert locate_token(_settings(oauth_dir=tmp_path))["token_path"] == str(tmp_path / "google_workspace_token.json")


def test_locate_token_works_without_credentials_file(tmp_path):
    # The token JSON carries client_id/secret; the client file is only for the consent flow.
    (tmp_path / "google_workspace_token.json").write_text("{}")
    assert locate_token(_settings(oauth_dir=tmp_path))["token_path"] == str(tmp_path / "google_workspace_token.json")


def test_locate_token_none_without_token(tmp_path):
    (tmp_path / "google_credentials.json").write_text("{}")
    assert locate_token(_settings(oauth_dir=tmp_path)) is None

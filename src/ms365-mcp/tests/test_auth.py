"""Tests for MS365 authentication."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from ms365.auth import (
    SERVICE_SCOPES,
    MS365Auth,
    MS365AuthError,
    migrate_plaintext_caches,
    scopes_for_services,
)


def test_scopes_for_all_services() -> None:
    result = scopes_for_services(None)
    assert "User.Read" in result
    for scopes in SERVICE_SCOPES.values():
        for scope in scopes:
            assert scope in result


def test_scopes_for_email_service() -> None:
    result = scopes_for_services(["email"])
    assert "Mail.ReadWrite" in result
    assert "Mail.Send" in result
    assert "User.Read" in result
    assert "Calendars.ReadWrite" not in result


def test_scopes_deduplicated() -> None:
    result = scopes_for_services(["email", "email"])
    assert sum(1 for s in result if s == "Mail.ReadWrite") == 1


def test_scopes_complete() -> None:
    result = scopes_for_services(None)
    assert "User.Read" in result
    assert "Mail.ReadWrite" in result
    assert "Mail.Send" in result
    assert "Calendars.ReadWrite" in result
    assert "Files.ReadWrite.All" in result


def test_service_scopes_cover_all_services() -> None:
    expected = {"email", "calendar", "onedrive", "todo", "contacts", "onenote", "directory"}
    assert set(SERVICE_SCOPES.keys()) == expected


def test_tenant_default(tmp_path: Path) -> None:
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id", cache_path=str(tmp_path / "tokens.json"))
    assert auth.tenant_id == "common"


def test_tenant_custom(tmp_path: Path) -> None:
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id", cache_path=str(tmp_path / "tokens.json"), tenant_id="my-tenant-id")
    assert auth.tenant_id == "my-tenant-id"


def test_cache_path_custom(tmp_path: Path) -> None:
    custom = str(tmp_path / "tokens.json")
    with patch("ms365.auth.msal", create=True):
        auth = MS365Auth(client_id="test-id", cache_path=custom)
    assert auth.cache_path == Path(custom)


@pytest.mark.asyncio
async def test_get_token_no_cache_raises() -> None:
    mock_msal = MagicMock()
    mock_cache = MagicMock()
    mock_cache.has_state_changed = False
    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_msal.SerializableTokenCache.return_value = mock_cache
    mock_msal.PublicClientApplication.return_value = mock_app

    with patch.dict("sys.modules", {"msal": mock_msal}):
        auth = MS365Auth.__new__(MS365Auth)
        auth._client_id = "test-id"
        auth._tenant_id = "common"
        auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
        auth._scopes = ["User.Read"]
        auth._app = None
        auth._cache = None
        auth._pending_flow_message = ""
        auth._fernet = None
        auth._import_legacy_cache = False

        with pytest.raises(MS365AuthError, match="No valid Microsoft 365 token"):
            await auth.get_token()


def _bare_auth(fernet: Fernet | None = None, import_legacy_cache: bool = False) -> MS365Auth:
    """Build an MS365Auth with private attrs set directly (no msal import needed)."""
    auth = MS365Auth.__new__(MS365Auth)
    auth._client_id = "test-id"
    auth._tenant_id = "common"
    auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
    auth._scopes = ["User.Read"]
    auth._app = MagicMock()
    auth._cache = MagicMock()
    auth._cache.has_state_changed = False
    auth._pending_flow_message = ""
    auth._fernet = fernet
    auth._import_legacy_cache = import_legacy_cache
    return auth


def test_initiate_device_flow_sync_returns_flow_without_blocking() -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {
        "user_code": "ABC123",
        "message": "To sign in, use code ABC123",
        "expires_in": 900,
    }

    flow = auth.initiate_device_flow_sync()

    assert flow["user_code"] == "ABC123"
    assert "expires_at" in flow
    assert auth._pending_flow_message == "To sign in, use code ABC123"
    auth._app.acquire_token_by_device_flow.assert_not_called()


def test_initiate_device_flow_sync_raises_without_user_code() -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {"error_description": "bad client"}

    with pytest.raises(MS365AuthError, match="Device flow failed"):
        auth.initiate_device_flow_sync()


def test_complete_device_flow_sync_success_saves_cache_unencrypted() -> None:
    auth = _bare_auth()
    auth._cache.has_state_changed = True
    auth._cache.serialize.return_value = '{"fake": "cache"}'
    auth._app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}

    result = auth.complete_device_flow_sync({"device_code": "xyz"})

    assert result["access_token"] == "new-token"
    auth._cache.serialize.assert_called_once()
    assert auth._cache_path.read_text() == '{"fake": "cache"}'
    auth._cache_path.unlink()


def test_complete_device_flow_sync_success_saves_cache_encrypted() -> None:
    fernet = Fernet(Fernet.generate_key())
    auth = _bare_auth(fernet=fernet)
    auth._cache.has_state_changed = True
    auth._cache.serialize.return_value = '{"fake": "cache"}'
    auth._app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}

    result = auth.complete_device_flow_sync({"device_code": "xyz"})

    assert result["access_token"] == "new-token"
    raw = auth._cache_path.read_bytes()
    assert raw != b'{"fake": "cache"}'
    assert fernet.decrypt(raw) == b'{"fake": "cache"}'
    auth._cache_path.unlink()


def test_load_cache_decrypts_with_matching_key(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    cache_path = tmp_path / "tokens.json"
    cache_path.write_bytes(fernet.encrypt(b'{"real": "cache"}'))

    auth = _bare_auth(fernet=fernet)
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_called_once_with('{"real": "cache"}')


def test_load_cache_wrong_key_treated_as_no_cached_token(tmp_path: Path) -> None:
    cache_path = tmp_path / "tokens.json"
    cache_path.write_bytes(Fernet(Fernet.generate_key()).encrypt(b'{"real": "cache"}'))

    auth = _bare_auth(fernet=Fernet(Fernet.generate_key()))
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_not_called()


def test_load_cache_migrates_legacy_plaintext_cache_in_place(tmp_path: Path) -> None:
    """A leftover unencrypted per-identity cache is encrypted in place, not discarded."""
    cache_path = tmp_path / "tokens.json"
    cache_path.write_text('{"legacy": "plaintext cache"}')
    fernet = Fernet(Fernet.generate_key())

    auth = _bare_auth(fernet=fernet)
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_called_once_with('{"legacy": "plaintext cache"}')
    raw = cache_path.read_bytes()
    assert raw != b'{"legacy": "plaintext cache"}'
    assert fernet.decrypt(raw) == b'{"legacy": "plaintext cache"}'
    assert oct(cache_path.stat().st_mode)[-3:] == "600"


def test_migrate_plaintext_caches_encrypts_existing_plaintext_files(tmp_path: Path) -> None:
    """Startup sweep encrypts caches even when their identity never reconnects this run."""
    fernet = Fernet(Fernet.generate_key())
    plain = tmp_path / "alice_token_cache.json"
    plain.write_text(json.dumps({"AccessToken": {}}))

    migrated = migrate_plaintext_caches(tmp_path, fernet)

    assert migrated == ["alice_token_cache.json"]
    raw = plain.read_bytes()
    assert fernet.decrypt(raw) == b'{"AccessToken": {}}'
    assert oct(plain.stat().st_mode)[-3:] == "600"


def test_migrate_plaintext_caches_skips_already_encrypted_files(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    encrypted = tmp_path / "bob_token_cache.json"
    original = fernet.encrypt(json.dumps({"AccessToken": {}}).encode())
    encrypted.write_bytes(original)

    migrated = migrate_plaintext_caches(tmp_path, fernet)

    assert migrated == []
    assert encrypted.read_bytes() == original


def test_migrate_plaintext_caches_leaves_undecryptable_garbage_untouched(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    garbage = tmp_path / "carol_token_cache.json"
    garbage.write_bytes(b"not json and not fernet ciphertext")

    migrated = migrate_plaintext_caches(tmp_path, fernet)

    assert migrated == []
    assert garbage.read_bytes() == b"not json and not fernet ciphertext"


def test_migrate_plaintext_caches_ignores_other_files_and_missing_dir(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    (tmp_path / "not_a_cache.txt").write_text("irrelevant")

    assert migrate_plaintext_caches(tmp_path / "does_not_exist", fernet) == []
    assert migrate_plaintext_caches(tmp_path, fernet) == []


def test_load_cache_non_json_garbage_treated_as_no_cached_token(tmp_path: Path) -> None:
    cache_path = tmp_path / "tokens.json"
    cache_path.write_bytes(b"\xff\xfe\x00\x01")

    auth = _bare_auth(fernet=Fernet(Fernet.generate_key()))
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_not_called()


def test_load_cache_non_dict_json_treated_as_no_cached_token(tmp_path: Path) -> None:
    cache_path = tmp_path / "tokens.json"
    cache_path.write_text("[1, 2, 3]")

    auth = _bare_auth(fernet=Fernet(Fernet.generate_key()))
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_not_called()


def test_load_cache_fernet_none_with_stale_encrypted_file_deletes_it(tmp_path: Path) -> None:
    """Key removed after being used: the now-unreadable file is deleted, forcing re-auth."""
    cache_path = tmp_path / "tokens.json"
    cache_path.write_bytes(Fernet(Fernet.generate_key()).encrypt(b'{"real": "cache"}'))

    auth = _bare_auth(fernet=None)
    auth._cache_path = cache_path
    # Real MSAL's deserialize() calls json.loads(state) internally and raises on
    # invalid JSON — the MagicMock cache doesn't do that by default, so simulate it.
    auth._cache.deserialize.side_effect = json.loads

    auth._load_cache()

    assert not cache_path.exists()


def test_import_legacy_cache_plaintext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(b'{"legacy": "session"}')
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", legacy_path)
    target = tmp_path / "default_token_cache.json"

    from ms365.auth import _import_legacy_cache

    _import_legacy_cache(target, None)

    assert target.read_bytes() == b'{"legacy": "session"}'
    assert not legacy_path.exists()


def test_import_legacy_cache_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(b'{"legacy": "session"}')
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", legacy_path)
    target = tmp_path / "default_token_cache.json"
    fernet = Fernet(Fernet.generate_key())

    from ms365.auth import _import_legacy_cache

    _import_legacy_cache(target, fernet)

    assert fernet.decrypt(target.read_bytes()) == b'{"legacy": "session"}'
    assert not legacy_path.exists()


def test_import_legacy_cache_skips_when_legacy_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", tmp_path / "does-not-exist.json")
    target = tmp_path / "default_token_cache.json"

    from ms365.auth import _import_legacy_cache

    _import_legacy_cache(target, None)

    assert not target.exists()


def test_import_legacy_cache_skips_when_target_already_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(b'{"legacy": "session"}')
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", legacy_path)
    target = tmp_path / "default_token_cache.json"
    target.write_bytes(b'{"already": "here"}')

    from ms365.auth import _import_legacy_cache

    _import_legacy_cache(target, None)

    assert target.read_bytes() == b'{"already": "here"}'
    assert legacy_path.exists()


def test_load_cache_imports_legacy_single_cache_when_flag_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(b'{"legacy": "session"}')
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", legacy_path)
    cache_path = tmp_path / "default_token_cache.json"

    auth = _bare_auth(import_legacy_cache=True)
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_called_once_with('{"legacy": "session"}')
    assert not legacy_path.exists()


def test_load_cache_ignores_legacy_cache_when_flag_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(b'{"legacy": "session"}')
    monkeypatch.setattr("ms365.auth.LEGACY_CACHE_PATH", legacy_path)
    cache_path = tmp_path / "default_token_cache.json"

    auth = _bare_auth(import_legacy_cache=False)
    auth._cache_path = cache_path

    auth._load_cache()

    auth._cache.deserialize.assert_not_called()
    assert legacy_path.exists()
    assert not cache_path.exists()


def test_complete_device_flow_sync_failure_raises() -> None:
    auth = _bare_auth()
    auth._app.acquire_token_by_device_flow.return_value = {
        "error": "authorization_declined",
        "error_description": "user declined",
    }

    with pytest.raises(MS365AuthError, match="authorization_declined"):
        auth.complete_device_flow_sync({"device_code": "xyz"})


def test_device_code_flow_sync_runs_initiate_then_complete(capsys: pytest.CaptureFixture[str]) -> None:
    auth = _bare_auth()
    auth._app.initiate_device_flow.return_value = {
        "user_code": "ABC123",
        "message": "To sign in, use code ABC123",
        "expires_in": 900,
    }
    auth._app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}

    result = auth.device_code_flow_sync()

    assert result["access_token"] == "new-token"
    captured = capsys.readouterr()
    assert "ABC123" in captured.err


@pytest.mark.asyncio
async def test_get_token_from_cache() -> None:
    mock_msal = MagicMock()
    mock_cache = MagicMock()
    mock_cache.has_state_changed = False
    mock_app = MagicMock()
    mock_app.get_accounts.return_value = [{"username": "user@test.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "cached-token"}
    mock_msal.SerializableTokenCache.return_value = mock_cache
    mock_msal.PublicClientApplication.return_value = mock_app

    with patch.dict("sys.modules", {"msal": mock_msal}):
        auth = MS365Auth.__new__(MS365Auth)
        auth._client_id = "test-id"
        auth._tenant_id = "common"
        auth._cache_path = Path("/tmp/nonexistent_ms365_cache.json")
        auth._scopes = ["User.Read"]
        auth._app = mock_app
        auth._cache = mock_cache
        auth._pending_flow_message = ""
        auth._fernet = None
        auth._import_legacy_cache = False

        token = await auth.get_token()
        assert token == "cached-token"

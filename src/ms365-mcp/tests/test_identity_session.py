"""Tests for the per-identity session registry."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from ms365.identity_session import DEFAULT_IDENTITY, AuthPendingError, IdentitySessionManager, sanitize_identity

# ── sanitize_identity ─────────────────────────────────────────────────────────


def test_sanitize_identity_none_falls_back_to_default() -> None:
    assert sanitize_identity(None) == DEFAULT_IDENTITY


def test_sanitize_identity_empty_string_falls_back_to_default() -> None:
    assert sanitize_identity("") == DEFAULT_IDENTITY


def test_sanitize_identity_passthrough_for_safe_values() -> None:
    assert sanitize_identity("usr_abc123") == "usr_abc123"
    assert sanitize_identity("jane@example.com") == "jane@example.com"


def test_sanitize_identity_strips_path_traversal() -> None:
    result = sanitize_identity("../../etc/passwd")
    assert "/" not in result
    assert ".." not in result


def test_sanitize_identity_all_unsafe_falls_back_to_default() -> None:
    assert sanitize_identity("///") == DEFAULT_IDENTITY


# ── IdentitySessionManager ────────────────────────────────────────────────────


def _manager(tmp_path: Path, fernet: Any = None) -> IdentitySessionManager:
    return IdentitySessionManager(client_id="test-client", tenant_id="common", cache_dir=tmp_path, fernet=fernet)


def _patch_cached_auth(monkeypatch: pytest.MonkeyPatch, has_token: bool = True) -> list[str]:
    """Patch MS365Auth so has_cached_token() succeeds and no real MSAL calls happen.

    Returns the list of cache_path values MS365Auth was constructed with, in order.
    """
    constructed_cache_paths: list[str] = []

    class _FakeAuth:
        def __init__(
            self, client_id: str, tenant_id: str, cache_path: str, services: list[str] | None, fernet: Any = None
        ) -> None:
            constructed_cache_paths.append(cache_path)
            self.fernet = fernet

        def has_cached_token(self) -> bool:
            return has_token

        async def initiate_device_flow(self) -> dict[str, object]:
            raise AssertionError("initiate_device_flow should not run when has_cached_token() is True")

    monkeypatch.setattr("ms365.auth.MS365Auth", _FakeAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))
    return constructed_cache_paths


class _FakePendingAuth:
    """MS365Auth stand-in with no cached token and a controllable device flow."""

    def __init__(
        self, client_id: str, tenant_id: str, cache_path: str, services: list[str] | None, fernet: Any = None
    ) -> None:
        self.cache_path = cache_path
        self.completed = asyncio.Event()
        self.should_fail = False

    def has_cached_token(self) -> bool:
        return False

    async def initiate_device_flow(self) -> dict[str, object]:
        return {
            "user_code": "ABC123",
            "message": "To sign in, visit https://microsoft.com/devicelogin and enter code ABC123",
            "expires_at": time.time() + 900,
        }

    async def complete_device_flow(self, flow: dict[str, object]) -> dict[str, object]:
        await self.completed.wait()
        if self.should_fail:
            from ms365.auth import MS365AuthError

            raise MS365AuthError("authorization_declined")
        return {"access_token": "new-token"}


@pytest.mark.asyncio
async def test_get_or_create_returns_same_client_for_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cached_auth(monkeypatch)
    manager = _manager(tmp_path)

    client1 = await manager.get_or_create("usr_abc")
    client2 = await manager.get_or_create("usr_abc")

    assert client1 is client2


@pytest.mark.asyncio
async def test_get_or_create_returns_different_clients_for_different_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cached_auth(monkeypatch)
    manager = _manager(tmp_path)

    client1 = await manager.get_or_create("usr_abc")
    client2 = await manager.get_or_create("usr_xyz")

    assert client1 is not client2
    assert set(manager.known_identities()) == {"usr_abc", "usr_xyz"}


@pytest.mark.asyncio
async def test_get_or_create_uses_per_identity_cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_paths = _patch_cached_auth(monkeypatch)
    manager = _manager(tmp_path)

    await manager.get_or_create("usr_abc")
    await manager.get_or_create("usr_xyz")

    assert cache_paths == [
        str(tmp_path / "usr_abc_token_cache.json"),
        str(tmp_path / "usr_xyz_token_cache.json"),
    ]


@pytest.mark.asyncio
async def test_get_or_create_missing_identity_uses_default_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_paths = _patch_cached_auth(monkeypatch)
    manager = _manager(tmp_path)

    await manager.get_or_create(None)

    assert cache_paths == [str(tmp_path / f"{DEFAULT_IDENTITY}_token_cache.json")]


@pytest.mark.asyncio
async def test_get_or_create_shares_one_fernet_across_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Fernet instance is shared across identities; isolation comes from separate files."""
    cache_paths = _patch_cached_auth(monkeypatch)
    fernet = object()
    manager = _manager(tmp_path, fernet=fernet)

    await manager.get_or_create("usr_abc")
    await manager.get_or_create("usr_xyz")

    assert cast("Any", manager._auths["usr_abc"]).fernet is fernet  # noqa: SLF001
    assert cast("Any", manager._auths["usr_xyz"]).fernet is fernet  # noqa: SLF001
    assert cache_paths == [
        str(tmp_path / "usr_abc_token_cache.json"),
        str(tmp_path / "usr_xyz_token_cache.json"),
    ]


@pytest.mark.asyncio
async def test_get_or_create_concurrent_calls_construct_auth_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    construct_count = 0

    class _SlowAuth:
        def __init__(
            self, client_id: str, tenant_id: str, cache_path: str, services: list[str] | None, fernet: Any = None
        ) -> None:
            nonlocal construct_count
            construct_count += 1

        def has_cached_token(self) -> bool:
            return True

        async def initiate_device_flow(self) -> dict[str, object]:
            raise AssertionError("should not be called")

    monkeypatch.setattr("ms365.auth.MS365Auth", _SlowAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))

    manager = _manager(tmp_path)
    results = await asyncio.gather(*(manager.get_or_create("usr_race") for _ in range(5)))

    assert construct_count == 1
    assert len({id(r) for r in results}) == 1


# ── Non-blocking device-code flow ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_no_cached_token_raises_pending_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call must not block on the device-code poll — it raises right away."""
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError, match="ABC123"):
        await asyncio.wait_for(manager.get_or_create("usr_new"), timeout=1.0)

    assert manager.pending_identities() == ["usr_new"]
    assert manager.known_identities() == []

    # Clean up the still-running background task so it doesn't hang past the test.
    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_or_create_second_call_while_pending_reuses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError, match="ABC123"):
        await manager.get_or_create("usr_new")

    with pytest.raises(AuthPendingError, match="already in progress"):
        await manager.get_or_create("usr_new")

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_or_create_succeeds_after_background_flow_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new")

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001 (wait for background completion)

    client = await manager.get_or_create("usr_new")
    assert client is not None
    assert manager.pending_identities() == []
    assert manager.known_identities() == ["usr_new"]


@pytest.mark.asyncio
async def test_get_or_create_retries_fresh_after_background_flow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))

    instances: list[_FakePendingAuth] = []

    def _make_auth(
        client_id: str, tenant_id: str, cache_path: str, services: list[str] | None, fernet: Any = None
    ) -> _FakePendingAuth:
        auth = _FakePendingAuth(client_id, tenant_id, cache_path, services, fernet)
        instances.append(auth)
        return auth

    monkeypatch.setattr("ms365.auth.MS365Auth", _make_auth)
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new")

    first_auth = instances[0]
    first_auth.should_fail = True
    first_auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001

    assert manager.pending_identities() == []
    assert manager.known_identities() == []

    # Next call starts a brand-new flow (has_cached_token still False on the same auth instance).
    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new")

    second_auth = instances[0]  # _auth_for() reuses the same MS365Auth instance for the slug
    second_auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_or_create_expired_pending_flow_starts_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ms365.identity_session import _PendingFlow

    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    manager = _manager(tmp_path)

    async def _never_completes() -> None:
        await asyncio.sleep(3600)

    stale_task = asyncio.ensure_future(_never_completes())
    manager._pending["usr_stale"] = _PendingFlow(  # noqa: SLF001
        task=stale_task, message="stale message", expires_at=time.time() - 10
    )

    with pytest.raises(AuthPendingError, match="ABC123"):
        await manager.get_or_create("usr_stale")

    assert manager.pending_identities() == ["usr_stale"]
    stale_task.cancel()

    auth = cast("_FakePendingAuth", manager._auths["usr_stale"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_stale"].task  # noqa: SLF001


# ── status_for ────────────────────────────────────────────────────────────────


def test_status_for_unknown_identity_is_not_signed_in(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    status = manager.status_for("usr_never_seen")
    assert status.state == "not_signed_in"


@pytest.mark.asyncio
async def test_status_for_signed_in_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cached_auth(monkeypatch)
    manager = _manager(tmp_path)
    await manager.get_or_create("usr_abc")

    status = manager.status_for("usr_abc")
    assert status.state == "signed_in"


@pytest.mark.asyncio
async def test_status_for_pending_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new")

    status = manager.status_for("usr_new")
    assert status.state == "pending"
    assert "ABC123" in status.message

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001


@pytest.mark.asyncio
async def test_status_for_expired_pending_is_not_signed_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ms365.identity_session import _PendingFlow

    manager = _manager(tmp_path)

    async def _never_completes() -> None:
        await asyncio.sleep(3600)

    stale_task = asyncio.ensure_future(_never_completes())
    manager._pending["usr_stale"] = _PendingFlow(  # noqa: SLF001
        task=stale_task, message="stale message", expires_at=time.time() - 10
    )

    status = manager.status_for("usr_stale")
    assert status.state == "not_signed_in"
    stale_task.cancel()


# ── notification delivery ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_flow_notifies_ctx_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))
    manager = _manager(tmp_path)

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.error = AsyncMock()

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new", ctx=ctx)

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001

    ctx.info.assert_awaited_once()
    ctx.error.assert_not_awaited()
    assert "complete" in ctx.info.call_args.args[0]
    assert ctx.info.call_args.kwargs["extra"] == {"identity": "usr_new"}


@pytest.mark.asyncio
async def test_complete_flow_notifies_ctx_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    manager = _manager(tmp_path)

    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.error = AsyncMock()

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new", ctx=ctx)

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.should_fail = True
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001

    ctx.error.assert_awaited_once()
    ctx.info.assert_not_awaited()
    assert "failed" in ctx.error.call_args.args[0]
    assert ctx.error.call_args.kwargs["extra"] == {"identity": "usr_new"}


@pytest.mark.asyncio
async def test_complete_flow_notification_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnected/broken ctx shouldn't crash the background task."""
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))
    manager = _manager(tmp_path)

    ctx = MagicMock()
    ctx.info = AsyncMock(side_effect=RuntimeError("connection closed"))

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new", ctx=ctx)

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001  (must not raise)

    assert manager.known_identities() == ["usr_new"]


@pytest.mark.asyncio
async def test_get_or_create_without_ctx_skips_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ms365.auth.MS365Auth", _FakePendingAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", MagicMock(side_effect=lambda auth: MagicMock(auth=auth)))
    manager = _manager(tmp_path)

    with pytest.raises(AuthPendingError):
        await manager.get_or_create("usr_new")

    auth = cast("_FakePendingAuth", manager._auths["usr_new"])  # noqa: SLF001
    auth.completed.set()
    await manager._pending["usr_new"].task  # noqa: SLF001  (must not raise without ctx)

    assert manager.known_identities() == ["usr_new"]

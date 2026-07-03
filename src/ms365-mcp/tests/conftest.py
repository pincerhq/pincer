"""Shared fixtures for ms365-mcp tests. No real Microsoft API calls are made."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ms365.auth import MS365Auth
from ms365.graph_client import GraphClient


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Ensure each test gets a fresh MS365Settings instance from the environment."""
    from ms365.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def mock_auth() -> MagicMock:
    auth = MagicMock(spec=MS365Auth)
    auth.get_token = AsyncMock(return_value="fake-access-token")
    auth.has_cached_token.return_value = True
    auth.authenticated_account.return_value = "user@example.com"
    auth.scopes = ["User.Read", "Mail.ReadWrite"]
    auth.client_id = "test-client-id"
    auth.tenant_id = "common"
    return auth


@pytest.fixture
def mock_client(mock_auth: MagicMock) -> MagicMock:
    client: MagicMock = MagicMock(spec=GraphClient)
    client.get = AsyncMock(return_value={"value": []})
    client.post = AsyncMock(return_value={})
    client.patch = AsyncMock(return_value={})
    client.delete = AsyncMock(return_value={})
    client.put = AsyncMock(return_value={})
    client.get_binary = AsyncMock(return_value=b"file content")
    client.paginate = AsyncMock(return_value=[])
    return client


@pytest.fixture
def resolve_client(mock_client: MagicMock) -> Any:
    """A ClientResolver stub (see ms365._registry.ClientResolver) that always
    returns `mock_client`, regardless of the caller's identity/ctx."""

    async def _resolve(ctx: Any) -> MagicMock:
        return mock_client

    return _resolve

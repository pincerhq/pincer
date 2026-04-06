"""
Shared fixtures for Microsoft 365 integration tests.

All tests mock the GraphClient HTTP methods so no real Microsoft APIs are hit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.integrations.ms365.auth import MS365Auth
from pincer.integrations.ms365.graph_client import GraphClient


@pytest.fixture
def mock_auth():
    """Mock MS365Auth that returns a fake token."""
    auth = MagicMock(spec=MS365Auth)
    auth.get_token = AsyncMock(return_value="fake-access-token")
    auth.has_cached_token.return_value = True
    auth.authenticated_account.return_value = "user@example.com"
    auth.scopes = ["User.Read", "Mail.ReadWrite"]
    auth.client_id = "test-client-id"
    auth.tenant_id = "common"
    return auth


@pytest.fixture
def mock_client(mock_auth):
    """GraphClient with all HTTP methods mocked."""
    client = GraphClient(mock_auth)
    client.get = AsyncMock(return_value={"value": []})
    client.post = AsyncMock(return_value={})
    client.patch = AsyncMock(return_value={})
    client.delete = AsyncMock(return_value={})
    client.put = AsyncMock(return_value={})
    client.get_binary = AsyncMock(return_value=b"file content")
    client.paginate = AsyncMock(return_value=[])
    return client

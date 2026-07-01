"""Tests for the organization directory search tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ms365.tools_directory import ms365__search_users

if TYPE_CHECKING:
    from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_search_users_requires_a_search_term(mock_client: MagicMock) -> None:
    result = await ms365__search_users(mock_client)
    assert "Provide" in result
    mock_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_users_by_email_single_match(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": [{"id": "u1", "displayName": "Jane Doe", "mail": "jane@contoso.com"}]}
    result = await ms365__search_users(mock_client, email="jane@contoso.com")
    assert "Jane Doe" in result
    assert "jane@contoso.com" in result
    assert "u1" not in result
    assert "ID" not in result

    args, kwargs = mock_client.get.call_args
    assert args[0] == "/users"
    assert "mail eq 'jane@contoso.com'" in kwargs["params"]["$filter"]
    assert kwargs["headers"] == {"ConsistencyLevel": "eventual"}
    assert "id" not in kwargs["params"]["$select"].split(",")


@pytest.mark.asyncio
async def test_search_users_by_name_filter_clause(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": [{"id": "u1", "displayName": "Jane Doe", "mail": "jane@contoso.com"}]}
    await ms365__search_users(mock_client, first_name="Jane", last_name="Doe")

    _, kwargs = mock_client.get.call_args
    filter_clause = kwargs["params"]["$filter"]
    assert "startswith(givenName,'Jane')" in filter_clause
    assert "startswith(surname,'Doe')" in filter_clause


@pytest.mark.asyncio
async def test_search_users_no_matches(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": []}
    result = await ms365__search_users(mock_client, email="nobody@contoso.com")
    assert "No users found" in result


@pytest.mark.asyncio
async def test_search_users_multiple_matches_limited_to_five(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": f"u{i}", "displayName": f"User {i}", "mail": f"user{i}@contoso.com"} for i in range(6)]
    }
    result = await ms365__search_users(mock_client, first_name="User")

    for i in range(5):
        assert f"User {i}" in result
    assert "User 5" not in result
    assert "5 user(s) found" in result
    assert "more matches exist" in result
    assert "id=" not in result


@pytest.mark.asyncio
async def test_search_users_two_matches_no_truncation_note(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {"id": "u1", "displayName": "Jane Doe", "mail": "jane@contoso.com"},
            {"id": "u2", "displayName": "Jane Smith", "mail": "jane.smith@contoso.com"},
        ]
    }
    result = await ms365__search_users(mock_client, first_name="Jane")
    assert "Jane Doe" in result
    assert "Jane Smith" in result
    assert "more matches exist" not in result


@pytest.mark.asyncio
async def test_search_users_escapes_quotes(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": []}
    await ms365__search_users(mock_client, email="o'brien@contoso.com")

    _, kwargs = mock_client.get.call_args
    assert "o''brien@contoso.com" in kwargs["params"]["$filter"]

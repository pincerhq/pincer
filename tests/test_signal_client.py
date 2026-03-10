"""Unit tests for SignalClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.channels.signal_client import (
    SignalAPIError,
    SignalClient,
    SignalMessage,
)


# ── _parse_message ────────────────────────────────────────────────────────────


def test_parse_message_dm() -> None:
    envelope = {
        "envelope": {
            "source": "+491234567890",
            "sourceName": "Alice",
            "timestamp": 1700000000000,
            "dataMessage": {
                "message": "Hello!",
                "attachments": [],
            },
        }
    }
    msg = SignalClient._parse_message(envelope)
    assert msg.source == "+491234567890"
    assert msg.source_name == "Alice"
    assert msg.text == "Hello!"
    assert not msg.is_group
    assert msg.timestamp == 1700000000000


def test_parse_message_group() -> None:
    envelope = {
        "envelope": {
            "source": "+491234567890",
            "sourceName": "Bob",
            "timestamp": 1700000001000,
            "dataMessage": {
                "message": "Hey group",
                "groupInfo": {"groupId": "abc123"},
                "attachments": [],
            },
        }
    }
    msg = SignalClient._parse_message(envelope)
    assert msg.is_group
    assert msg.group_id == "abc123"


def test_parse_message_voice_attachment() -> None:
    envelope = {
        "envelope": {
            "source": "+491234567890",
            "timestamp": 1700000002000,
            "dataMessage": {
                "message": "",
                "attachments": [
                    {"id": "att-1", "contentType": "audio/aac", "size": 12345},
                ],
            },
        }
    }
    msg = SignalClient._parse_message(envelope)
    assert msg.has_voice
    assert len(msg.attachments) == 1
    assert msg.attachments[0].content_type == "audio/aac"


def test_is_data_message_true() -> None:
    envelope = {
        "envelope": {
            "dataMessage": {"message": "hi"},
        }
    }
    assert SignalClient._is_data_message(envelope)


def test_is_data_message_false_for_receipt() -> None:
    envelope = {
        "envelope": {
            "receiptMessage": {"isDelivery": True},
        }
    }
    assert not SignalClient._is_data_message(envelope)


# ── HTTP methods ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_ok() -> None:
    client = SignalClient("http://localhost:8080", "+491234567890")
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"status": "ok"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    result = await client.health()
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_fail() -> None:
    client = SignalClient("http://localhost:8080", "+491234567890")
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    with pytest.raises(SignalAPIError):
        await client.health()


@pytest.mark.asyncio
async def test_send_message_ok() -> None:
    client = SignalClient("http://localhost:8080", "+491234567890")
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 201
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_resp)
    client._session = mock_session

    await client.send_message("+491111111111", "Test message")
    mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_fail() -> None:
    client = SignalClient("http://localhost:8080", "+491234567890")
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 400
    mock_resp.text = AsyncMock(return_value="error")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_resp)
    client._session = mock_session

    with pytest.raises(SignalAPIError):
        await client.send_message("+491111111111", "Test")


@pytest.mark.asyncio
async def test_receive_ok() -> None:
    envelope = {
        "envelope": {
            "source": "+491111111111",
            "timestamp": 1700000010000,
            "dataMessage": {"message": "hi", "attachments": []},
        }
    }
    client = SignalClient("http://localhost:8080", "+491234567890")
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[envelope])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    messages = await client.receive()
    assert len(messages) == 1
    assert messages[0].text == "hi"


@pytest.mark.asyncio
async def test_ensure_session_raises_without_connect() -> None:
    client = SignalClient("http://localhost:8080", "+491234567890")
    with pytest.raises(SignalAPIError, match="not connected"):
        client._ensure_session()

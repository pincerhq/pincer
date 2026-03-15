"""Tests for GeminiImageProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.image.provider_gemini import GeminiImageProvider
from pincer.image.types import ImageRequest


def _make_fake_response(image_bytes: bytes = b"PNG_DATA", mime: str = "image/png"):
    part = MagicMock()
    part.inline_data = MagicMock()
    part.inline_data.data = image_bytes
    part.inline_data.mime_type = mime

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content

    response = MagicMock()
    response.candidates = [candidate]
    return response


def _make_genai_mock(client):
    mock_genai = MagicMock()
    mock_genai.Client.return_value = client
    mock_google = MagicMock()
    mock_google.genai = mock_genai

    mock_types = MagicMock()
    mock_types.GenerateContentConfig = MagicMock()
    mock_types.Modality = MagicMock()
    mock_types.Modality.TEXT = "TEXT"
    mock_types.Modality.IMAGE = "IMAGE"

    return {
        "google": mock_google,
        "google.genai": mock_genai,
        "google.genai.types": mock_types,
    }


@pytest.mark.asyncio
async def test_generate_returns_bytes():
    provider = GeminiImageProvider(api_key="fake-key")
    fake_response = _make_fake_response(b"PNG_DATA")

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with patch.dict("sys.modules", _make_genai_mock(mock_client)):
        result = await provider.generate(ImageRequest(prompt="a sunset"))

    assert len(result.images) == 1
    assert result.images[0].bytes == b"PNG_DATA"
    assert result.provider == "gemini"


@pytest.mark.asyncio
async def test_generate_raises_when_no_candidates():
    provider = GeminiImageProvider(api_key="fake-key")

    response = MagicMock()
    response.candidates = []

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=response)

    with patch.dict("sys.modules", _make_genai_mock(mock_client)):
        with pytest.raises(RuntimeError, match="no candidates"):
            await provider.generate(ImageRequest(prompt="x"))


def test_is_available_false_without_key():
    provider = GeminiImageProvider(api_key="")
    assert not provider.is_available()

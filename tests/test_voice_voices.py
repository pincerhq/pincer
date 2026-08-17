"""Tests for ElevenLabs voice management: list/validate/sample (Sprint 4, T4.4)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pincer.voice import voices
from pincer.voice.voices import (
    VoiceLookupError,
    configured_voice_ids,
    get_voice,
    list_voices,
    ulaw_to_wav,
    validate_configured_voices,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    voices._reset_validation_cache_for_tests()
    yield
    voices._reset_validation_cache_for_tests()


def _fake_client(responses):
    """httpx.Client stand-in: pops one canned response per request."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    calls = list(responses)

    def _next(*args, **kwargs):
        return calls.pop(0)

    client.get.side_effect = _next
    client.post.side_effect = _next
    return client


def _response(status_code=200, json_body=None, content=b""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.content = content
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


VOICE_JSON = {
    "voice_id": "abc123",
    "name": "My Clone",
    "category": "cloned",
    "verified_languages": [{"language": "de", "model_id": "eleven_flash_v2_5"}, {"language": "en"}],
}


class TestListVoices:
    def test_parses_voices(self):
        client = _fake_client([_response(json_body={"voices": [VOICE_JSON], "has_more": False})])
        with patch("httpx.Client", return_value=client):
            result = list_voices("key")
        assert len(result) == 1
        assert result[0].voice_id == "abc123"
        assert result[0].category == "cloned"
        assert result[0].languages == ["de", "en"]

    def test_pagination(self):
        client = _fake_client(
            [
                _response(json_body={"voices": [VOICE_JSON], "has_more": True, "next_page_token": "t2"}),
                _response(json_body={"voices": [dict(VOICE_JSON, voice_id="def456")], "has_more": False}),
            ]
        )
        with patch("httpx.Client", return_value=client):
            result = list_voices("key")
        assert [v.voice_id for v in result] == ["abc123", "def456"]

    def test_api_error_raises_lookup_error(self):
        client = _fake_client([_response(status_code=401)])
        with patch("httpx.Client", return_value=client), pytest.raises(VoiceLookupError):
            list_voices("bad-key")


class TestGetVoice:
    def test_found(self):
        client = _fake_client([_response(json_body=VOICE_JSON)])
        with patch("httpx.Client", return_value=client):
            info = get_voice("key", "abc123")
        assert info is not None and info.name == "My Clone"

    def test_unknown_id_returns_none(self):
        client = _fake_client([_response(status_code=404)])
        with patch("httpx.Client", return_value=client):
            assert get_voice("key", "nope") is None

    def test_network_failure_raises(self):
        import httpx

        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.side_effect = httpx.ConnectError("down")
        with patch("httpx.Client", return_value=client), pytest.raises(VoiceLookupError):
            get_voice("key", "abc123")


def _settings(**kwargs):
    api_key = kwargs.pop("api_key", "el-key")
    ns = SimpleNamespace(
        elevenlabs_voice_id=kwargs.pop("elevenlabs_voice_id", ""),
        elevenlabs_voice_id_en=kwargs.pop("elevenlabs_voice_id_en", ""),
        elevenlabs_voice_id_de=kwargs.pop("elevenlabs_voice_id_de", ""),
    )
    ns.elevenlabs_api_key = MagicMock()
    ns.elevenlabs_api_key.get_secret_value.return_value = api_key
    return ns


class TestValidation:
    def test_configured_ids_deduped(self):
        s = _settings(elevenlabs_voice_id="a", elevenlabs_voice_id_en="a", elevenlabs_voice_id_de="b")
        assert configured_voice_ids(s) == {"a", "b"}

    def test_bad_id_marked_invalid(self):
        s = _settings(elevenlabs_voice_id="dead")
        with (
            patch("pincer.voice.voices.get_voice", return_value=None),
            patch("pincer.voice.voices.probe_voice", return_value=False),
        ):
            problems = validate_configured_voices(s)
        assert "dead" in problems
        assert voices.is_voice_invalid("dead")

    def test_library_voice_passes_via_synthesis_probe(self):
        # Public-library/default voices 404 on /v1/voices/{id} but synthesize fine
        s = _settings(elevenlabs_voice_id="lib-voice")
        with (
            patch("pincer.voice.voices.get_voice", return_value=None),
            patch("pincer.voice.voices.probe_voice", return_value=True) as probe,
        ):
            assert validate_configured_voices(s) == {}
        probe.assert_called_once()
        assert not voices.is_voice_invalid("lib-voice")

    def test_probe_network_failure_does_not_condemn(self):
        s = _settings(elevenlabs_voice_id="maybe-lib")
        with (
            patch("pincer.voice.voices.get_voice", return_value=None),
            patch("pincer.voice.voices.probe_voice", side_effect=VoiceLookupError("down")),
        ):
            assert validate_configured_voices(s) == {}
        assert not voices.is_voice_invalid("maybe-lib")

    def test_good_id_cached_and_not_rechecked(self):
        s = _settings(elevenlabs_voice_id="live")
        with patch("pincer.voice.voices.get_voice", return_value=voices.VoiceInfo("live", "V", "premade")) as gv:
            assert validate_configured_voices(s) == {}
            assert validate_configured_voices(s) == {}  # second call hits the cache
        assert gv.call_count == 1
        assert not voices.is_voice_invalid("live")

    def test_network_failure_does_not_condemn_voice(self):
        s = _settings(elevenlabs_voice_id="maybe")
        with patch("pincer.voice.voices.get_voice", side_effect=VoiceLookupError("down")):
            problems = validate_configured_voices(s)
        assert problems == {}
        assert not voices.is_voice_invalid("maybe")

    def test_no_key_or_no_ids_is_noop(self):
        assert validate_configured_voices(_settings(api_key="")) == {}
        assert validate_configured_voices(_settings()) == {}


class TestUlawWav:
    def test_header_and_size(self):
        data = b"\x7f" * 160  # 20ms of 8kHz mu-law
        wav = ulaw_to_wav(data)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert b"fmt " in wav and b"fact" in wav and b"data" in wav
        assert wav.endswith(data)
        import struct

        riff_size = struct.unpack("<I", wav[4:8])[0]
        assert riff_size == len(wav) - 8
        fmt_tag = struct.unpack("<H", wav[20:22])[0]
        assert fmt_tag == 7  # WAVE_FORMAT_MULAW

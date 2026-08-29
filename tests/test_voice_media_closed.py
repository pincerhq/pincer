"""VoiceEngine.on_media_closed: the media socket closing ends the call."""

from __future__ import annotations

from unittest.mock import MagicMock

from voice_harness.fake_engine import FakeVoiceEngine

from pincer.voice.engine import CallDirection

CALL = "CA_media"


def _engine():
    settings = MagicMock()
    settings.voice_default_language = "en"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_language = "en-US"
    return FakeVoiceEngine(settings)


async def test_media_closed_ends_call_and_fires_callback():
    engine = _engine()
    ended = []

    async def on_end(call_sid, state):
        ended.append((call_sid, state.ended_at is not None))

    engine.set_on_call_end(on_end)
    await engine.on_call_start(CALL, "+1", CallDirection.INBOUND)
    await engine.on_media_closed(CALL)
    assert ended == [(CALL, True)]
    assert engine.get_call_state(CALL) is None
    # idempotent: a later status callback / second close does nothing
    await engine.on_media_closed(CALL)
    assert ended == [(CALL, True)]


async def test_media_closed_leaves_transferring_call_alive():
    engine = _engine()
    ended = []

    async def on_end(call_sid, state):
        ended.append(call_sid)

    engine.set_on_call_end(on_end)
    state = await engine.on_call_start(CALL, "+1", CallDirection.INBOUND)
    state.metadata["transferring"] = True
    await engine.on_media_closed(CALL)
    assert ended == [] and engine.get_call_state(CALL) is state

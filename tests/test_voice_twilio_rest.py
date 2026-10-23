"""Twilio REST off the event loop.

A blocking Twilio REST call on the loop froze audio for every live call: a
hangup on call B was a gap on call A. These tests keep it from coming back;
ruff's banned-api rule (TID251) keeps `twilio.rest` out of everything but the
façade. The mu-law ingest half of the same fix is in test_voice_mulaw_ingest.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from test_voice_streaming_turn import _settings
from voice_harness.transport_fakes import FakeSocket, FakeTwilio, install_fake_twilio

from pincer.voice import twilio_rest
from pincer.voice.engine import CallDirection, ConversationRelayEngine, MediaStreamEngine
from pincer.voice.twilio_rest import TwilioRest


class CountingTwilio(FakeTwilio):
    instances: list[CountingTwilio] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        CountingTwilio.instances.append(self)


@pytest.fixture
def twilio(monkeypatch):
    CountingTwilio.instances = []
    install_fake_twilio(monkeypatch, CountingTwilio)
    return CountingTwilio


def _twilio_settings():
    settings = _settings()
    settings.twilio_account_sid = "AC_test"
    settings.twilio_auth_token.get_secret_value.return_value = "tok"
    settings.twilio_edge = ""
    settings.twilio_region = ""
    return settings


async def _until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _live_call(engine, call_sid: str) -> FakeSocket:
    await engine.on_call_start(call_sid, "+4930123456", CallDirection.INBOUND, language="de")
    sock = FakeSocket()
    engine.get_call_state(call_sid).metadata["websocket"] = sock
    return sock


class TestTwilioRestOffTheLoop:
    # regression: on dev the REST call blocked the loop, so B's speech could
    # not be delivered until A's hangup had returned
    async def test_other_calls_speak_while_a_hangup_is_in_flight(self, twilio):
        engine = ConversationRelayEngine(_twilio_settings())
        await _live_call(engine, "CA_a")
        sock_b = await _live_call(engine, "CA_b")
        await engine._rest._get()
        twilio.instances[0].release.clear()  # api.twilio.com is slow

        hangup = asyncio.create_task(engine.end_call("CA_a"))
        await _until(lambda: twilio.instances[0].requests)
        assert await engine.send_speech("CA_b", "Einen Moment bitte.")

        assert sock_b.tokens() == ["Einen Moment bitte."]
        assert not hangup.done()
        twilio.instances[0].release.set()
        await hangup
        assert twilio.instances[0].requests == [("update", {"sid": "CA_a", "status": "completed"})]

    @pytest.mark.parametrize("engine_cls", [ConversationRelayEngine, MediaStreamEngine])
    async def test_one_client_per_engine(self, twilio, engine_cls):
        engine = engine_cls(_twilio_settings())
        await _live_call(engine, "CA_b")

        await engine.send_dtmf("CA_b", "1")
        await engine.transfer_call("CA_b", "+4930999999")
        await engine.end_call("CA_b")

        assert len(twilio.instances) == 1
        ops = [(op, sorted(params)) for op, params in twilio.instances[0].requests]
        assert ops == [("update", ["sid", "twiml"]), ("update", ["sid", "twiml"]), ("update", ["sid", "status"])]

    async def test_edge_and_region_reach_the_client(self, twilio):
        settings = _twilio_settings()
        settings.twilio_edge = "frankfurt"
        settings.twilio_region = "ie1"
        await TwilioRest(settings)._get()

        assert twilio.instances[0].kwargs["edge"] == "frankfurt"
        assert twilio.instances[0].kwargs["region"] == "ie1"

    async def test_unset_edge_and_region_are_none(self, twilio):
        await TwilioRest(_twilio_settings())._get()

        assert twilio.instances[0].kwargs["edge"] is None
        assert twilio.instances[0].kwargs["region"] is None

    async def test_hung_twilio_times_out_and_the_call_still_ends(self, twilio, monkeypatch):
        monkeypatch.setattr(twilio_rest, "REQUEST_TIMEOUT_S", 0.05)
        engine = ConversationRelayEngine(_twilio_settings())
        ended = []
        engine.set_on_call_end(lambda sid, _state: _record(ended, sid))
        await _live_call(engine, "CA_a")
        await engine._rest._get()
        twilio.instances[0].release.clear()  # never answers

        await engine.end_call("CA_a")

        assert engine.get_call_state("CA_a") is None
        assert ended == ["CA_a"]

    async def test_cancelled_hangup_still_unregisters_once(self, twilio):
        engine = ConversationRelayEngine(_twilio_settings())
        ended = []
        engine.set_on_call_end(lambda sid, _state: _record(ended, sid))
        await _live_call(engine, "CA_a")
        await engine._rest._get()
        twilio.instances[0].release.clear()

        hangup = asyncio.create_task(engine.end_call("CA_a"))
        await _until(lambda: twilio.instances[0].requests)
        hangup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hangup
        twilio.instances[0].release.set()
        await engine.end_call("CA_a")  # e.g. the status webhook's cleanup

        assert engine.get_call_state("CA_a") is None
        assert ended == ["CA_a"]

    async def test_transfer_errors_reach_the_caller(self, monkeypatch):
        class Broken:
            def __init__(self, *a, **k):
                self.calls = MagicMock(side_effect=RuntimeError("20404 not found"))

        install_fake_twilio(monkeypatch, Broken)
        engine = ConversationRelayEngine(_twilio_settings())
        await _live_call(engine, "CA_x")

        with pytest.raises(RuntimeError, match="20404"):
            await engine.transfer_call("CA_x", "+4930999999")

    async def test_missing_twilio_extra_is_a_clear_error(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "twilio.http.async_http_client", None)
        with pytest.raises(RuntimeError, match="twilio extra not installed"):
            await TwilioRest(_twilio_settings()).update_call("CA_x", status="completed")

    async def test_aclose_closes_the_real_aiohttp_session(self):
        rest = TwilioRest(_twilio_settings())
        session = (await rest._get()).http_client.session

        await rest.aclose()

        assert session.closed
        assert rest._client is None


class TestErrorSemanticsUnchanged:
    """REST failures behave as they did when the calls were synchronous."""

    async def test_failed_hangup_still_ends_the_call(self, fake_twilio_rest):
        engine = MediaStreamEngine(_twilio_settings())
        ended = []
        engine.set_on_call_end(lambda sid, _state: _record(ended, sid))
        await _live_call(engine, "CA_a")
        fake_twilio_rest.raise_on = {"update_call"}

        await engine.end_call("CA_a")

        assert engine.get_call_state("CA_a") is None
        assert ended == ["CA_a"]

    @pytest.mark.parametrize("op", ["transfer", "dtmf"])
    async def test_failed_transfer_and_dtmf_raise(self, fake_twilio_rest, op):
        engine = ConversationRelayEngine(_twilio_settings())
        await _live_call(engine, "CA_a")
        fake_twilio_rest.raise_on = {"update_call"}

        with pytest.raises(RuntimeError, match="fake twilio update_call failed"):
            if op == "transfer":
                await engine.transfer_call("CA_a", "+4930999999")
            else:
                await engine.send_dtmf("CA_a", "1")
        assert engine.get_call_state("CA_a") is not None  # the caller is still there

    async def test_failed_fallback_say_ends_the_call_directly(self, fake_twilio_rest):
        engine = ConversationRelayEngine(_twilio_settings())
        ended = []
        engine.set_on_call_end(lambda sid, _state: _record(ended, sid))
        await _live_call(engine, "CA_a")
        fake_twilio_rest.raise_on = {"update_call"}

        await engine.fallback_and_end("CA_a")

        assert engine.get_call_state("CA_a") is None
        assert ended == ["CA_a"]

    async def test_channel_stop_closes_the_rest_session(self, fake_twilio_rest):
        from pincer.channels.phone_calls import VoiceChannel

        engine = ConversationRelayEngine(_twilio_settings())
        channel = VoiceChannel(engine._settings)
        channel.set_engine(engine)

        await channel.stop()

        assert fake_twilio_rest.closed


async def _record(seen: list, sid: str) -> None:
    seen.append(sid)

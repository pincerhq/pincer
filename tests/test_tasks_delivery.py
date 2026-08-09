"""Tests for pincer.tasks.delivery — pub/sub result delivery."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.channels.base import ChannelType
from pincer.tasks.delivery import (
    TASK_RESULTS_CHANNEL,
    InMemoryDeliveryBackend,
    RedisDeliveryBackend,
    ResultEmitter,
    ResultRelay,
    create_delivery_backend,
)


@pytest.mark.asyncio
class TestInMemoryDeliveryBackend:
    async def test_publish_then_subscribe_roundtrip(self) -> None:
        backend = InMemoryDeliveryBackend()
        envelope = {"pincer_user_id": "u1", "text": "hi", "prefer": None, "max_active_age_seconds": None}

        await backend.publish(envelope)
        received = await asyncio.wait_for(backend.subscribe().__anext__(), timeout=1)

        assert received == envelope


@pytest.mark.asyncio
class TestRedisDeliveryBackend:
    async def test_publish_serializes_envelope(self) -> None:
        with patch("pincer.tasks.delivery.Redis") as mock_redis_cls:
            mock_client = AsyncMock()
            mock_redis_cls.from_url.return_value = mock_client

            backend = RedisDeliveryBackend("redis://localhost:6379/0")
            envelope = {"pincer_user_id": "u1", "text": "hi", "prefer": "telegram", "max_active_age_seconds": None}
            await backend.publish(envelope)

            mock_client.publish.assert_awaited_once_with(TASK_RESULTS_CHANNEL, json.dumps(envelope))

    async def test_subscribe_yields_parsed_messages_and_skips_non_message_types(self) -> None:
        envelope = {"pincer_user_id": "u1", "text": "hi", "prefer": None, "max_active_age_seconds": None}

        async def fake_listen():
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": json.dumps(envelope)}

        with patch("pincer.tasks.delivery.Redis") as mock_redis_cls:
            mock_client = AsyncMock()
            mock_redis_cls.from_url.return_value = mock_client
            mock_pubsub = MagicMock()
            mock_pubsub.subscribe = AsyncMock()
            mock_pubsub.listen = fake_listen
            mock_pubsub.unsubscribe = AsyncMock()
            mock_pubsub.aclose = AsyncMock()
            # Redis.pubsub() is sync (returns a PubSub immediately) — override
            # the AsyncMock auto-attribute so calling it doesn't return a coroutine.
            mock_client.pubsub = MagicMock(return_value=mock_pubsub)

            backend = RedisDeliveryBackend("redis://localhost:6379/0")
            received = await asyncio.wait_for(backend.subscribe().__anext__(), timeout=1)

            assert received == envelope

    async def test_subscribe_drops_malformed_json_without_raising(self) -> None:
        envelope = {"pincer_user_id": "u1", "text": "hi", "prefer": None, "max_active_age_seconds": None}

        async def fake_listen():
            yield {"type": "message", "data": "not-json"}
            yield {"type": "message", "data": json.dumps(envelope)}

        with patch("pincer.tasks.delivery.Redis") as mock_redis_cls:
            mock_client = AsyncMock()
            mock_redis_cls.from_url.return_value = mock_client
            mock_pubsub = MagicMock()
            mock_pubsub.subscribe = AsyncMock()
            mock_pubsub.listen = fake_listen
            mock_pubsub.unsubscribe = AsyncMock()
            mock_pubsub.aclose = AsyncMock()
            # Redis.pubsub() is sync (returns a PubSub immediately) — override
            # the AsyncMock auto-attribute so calling it doesn't return a coroutine.
            mock_client.pubsub = MagicMock(return_value=mock_pubsub)

            backend = RedisDeliveryBackend("redis://localhost:6379/0")
            received = await asyncio.wait_for(backend.subscribe().__anext__(), timeout=1)

            assert received == envelope


def test_create_delivery_backend_selects_by_broker() -> None:
    memory_settings = SimpleNamespace(task_broker="memory", task_broker_url="")
    assert isinstance(create_delivery_backend(memory_settings), InMemoryDeliveryBackend)

    with patch("pincer.tasks.delivery.Redis") as mock_redis_cls:
        mock_redis_cls.from_url.return_value = AsyncMock()
        redis_settings = SimpleNamespace(task_broker="redis", task_broker_url="redis://localhost:6379/0")
        assert isinstance(create_delivery_backend(redis_settings), RedisDeliveryBackend)


@pytest.mark.asyncio
class TestResultEmitter:
    async def test_send_to_user_publishes_envelope_and_returns_true(self) -> None:
        backend = AsyncMock()
        emitter = ResultEmitter(backend)

        delivered = await emitter.send_to_user("u1", "hello", prefer=ChannelType.TELEGRAM, max_active_age_seconds=30)

        assert delivered is True
        backend.publish.assert_awaited_once_with(
            {"pincer_user_id": "u1", "text": "hello", "prefer": "telegram", "max_active_age_seconds": 30}
        )


@pytest.mark.asyncio
class TestResultRelay:
    async def test_delivers_via_router(self) -> None:
        backend = InMemoryDeliveryBackend()
        router = AsyncMock()
        router.send_to_user = AsyncMock(return_value=True)

        relay = ResultRelay(backend, router)
        await relay.start()
        try:
            await backend.publish(
                {"pincer_user_id": "u1", "text": "hi", "prefer": "telegram", "max_active_age_seconds": None}
            )
            await asyncio.sleep(0.05)

            router.send_to_user.assert_awaited_once()
            args, kwargs = router.send_to_user.call_args
            assert args[0] == "u1"
            assert args[1] == "hi"
            assert kwargs["prefer"] == ChannelType.TELEGRAM
        finally:
            await relay.stop()

    async def test_unknown_prefer_channel_falls_back_to_none(self) -> None:
        backend = InMemoryDeliveryBackend()
        router = AsyncMock()
        router.send_to_user = AsyncMock(return_value=True)

        relay = ResultRelay(backend, router)
        await relay.start()
        try:
            await backend.publish(
                {
                    "pincer_user_id": "u1",
                    "text": "hi",
                    "prefer": "not-a-real-channel",
                    "max_active_age_seconds": None,
                }
            )
            await asyncio.sleep(0.05)

            router.send_to_user.assert_awaited_once()
            _, kwargs = router.send_to_user.call_args
            assert kwargs["prefer"] is None
        finally:
            await relay.stop()

    async def test_router_exception_does_not_crash_loop(self) -> None:
        backend = InMemoryDeliveryBackend()
        router = AsyncMock()
        router.send_to_user = AsyncMock(side_effect=[RuntimeError("boom"), True])

        relay = ResultRelay(backend, router)
        await relay.start()
        try:
            await backend.publish(
                {"pincer_user_id": "u1", "text": "first", "prefer": None, "max_active_age_seconds": None}
            )
            await asyncio.sleep(0.05)
            await backend.publish(
                {"pincer_user_id": "u1", "text": "second", "prefer": None, "max_active_age_seconds": None}
            )
            await asyncio.sleep(0.05)

            assert router.send_to_user.await_count == 2
        finally:
            await relay.stop()
